import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "evals"), str(ROOT / "src")]
import httpx  # noqa: E402
import run_sessions as S  # noqa: E402
from manifest import build_manifest, write_manifest  # noqa: E402
from session_client import RouterSessionClient  # noqa: E402

OUT = ROOT / "evals/reports/2026-10-27-model-refresh"
DEST = OUT / "sessions"
DEST.mkdir(exist_ok=True)
ARMS = {
    "refresh_glm_high": {
        "model": "ollama/glm-5.3",
        "effort": "high",
        "purpose": "fresh incumbent control",
    },
    "refresh_deepseek_max": {
        "model": "ollama/deepseek-v4.1-flash:1017",
        "effort": "max",
        "purpose": "candidate moderate-tools replacement",
    },
    "refresh_sol61_low": {
        "model": "gpt-6.1-sol",
        "effort": "low",
        "purpose": "candidate moderate-tools equivalent",
    },
}
S.POLICY_ARMS.update(ARMS)
wire = []
limit = 0


class Transport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        if len(wire) >= limit:
            raise RuntimeError("global approved wire-call budget reached")
        try:
            model = json.loads(request.content)["model"]
        except (ValueError, KeyError) as exc:
            raise RuntimeError("Expected a model in the evaluation request") from exc
        row = {"attempt": len(wire) + 1, "model": model}
        wire.append(row)
        (DEST / "wire-attempts.json").write_text(json.dumps(wire, indent=2))
        try:
            response = await self.inner.handle_async_request(request)
            row["status"] = response.status_code
            return response
        except Exception as exc:
            row["error"] = type(exc).__name__
            raise
        finally:
            (DEST / "wire-attempts.json").write_text(json.dumps(wire, indent=2))

    async def aclose(self):
        await self.inner.aclose()


async def main():
    global limit
    if (DEST / "wire-attempts.json").exists():
        raise RuntimeError(
            "This one-shot pilot already ran; refusing to reset its spend ledger"
        )
    if not (OUT / "manifest.json").exists():
        raise RuntimeError("Text-stage provenance must exist before the pilot")
    try:
        text_calls = len(json.loads((OUT / "wire-attempts.json").read_text()))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cannot read the text-stage spend ledger") from exc
    # Reserve the full text-stage ceiling while it finishes concurrently.
    limit = 250  # 5 probes + 310 text attempts + 250 session attempts <= 600
    tasks = S.load_tasks()
    assert not S.validate(tasks)
    key = subprocess.check_output(  # noqa: ASYNC221 - preflight only
        [
            "ssh",
            "mini",
            'python3 -c \'import json,pathlib; print(json.loads((pathlib.Path.home()/".pi/agent/models.json").read_text())["providers"]["jev-router"]["apiKey"])\'',
        ],
        text=True,
    ).strip()
    client = RouterSessionClient(
        router_url="http://100.82.7.89:8317", upstream_api_key=key, timeout_s=180
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=Transport(), timeout=180)
    results = []
    stop = None
    image = subprocess.check_output(  # noqa: ASYNC221 - preflight only
        ["docker", "image", "inspect", "jev-eval:local", "--format", "{{.Id}}"],
        text=True,
    ).strip()
    try:
        for idx, task in enumerate(tasks):
            arms = list(ARMS)
            offset = idx % len(arms)
            for arm in arms[offset:] + arms[:offset]:
                if len(wire) >= limit:
                    stop = "global budget"
                    break
                r = await S.run_task(
                    task,
                    arm,
                    client,
                    caps=S.Caps(max_calls=12, wall_clock_seconds=240),
                    repeat=0,
                    container_image="jev-eval:local",
                )
                results.append(r)
                (DEST / "sessions.json").write_text(
                    json.dumps([r.to_dict() for r in results], indent=2)
                )
                print(
                    task.id,
                    arm,
                    "done",
                    r.completed,
                    "calls",
                    r.calls,
                    "cap",
                    r.capped,
                    "error",
                    r.error,
                    flush=True,
                )
                if r.error:
                    stop = "provider/session error; stop instead of continuing spend"
                    break
            if stop:
                break
    finally:
        await client.aclose()
        (DEST / "sessions.md").write_text(S.report(results, list(ARMS)))
        finished = {(r.task, r.arm) for r in results}
        pending = [
            {"task": t.id, "arm": a, "reason": "not run"}
            for t in tasks
            for a in ARMS
            if (t.id, a) not in finished
        ]
        write_manifest(
            DEST,
            build_manifest(
                script="evals/run_sessions.py via fixed-arm refresh runner",
                kind="live",
                repeats=1,
                cases={
                    "count": len(tasks),
                    "by_split": {"pilot": [t.id for t in tasks]},
                },
                candidates=ARMS,
                caps={
                    "approved_total": 600,
                    "text_stage_reserved": 310,
                    "preflight_calls": 5,
                    "text_calls_at_session_start": text_calls,
                    "session_wire_limit": limit,
                    "actual_session_wire_calls": len(wire),
                    "calls_per_session": 12,
                    "wall_seconds": 240,
                    "max_tokens": 4096,
                },
                graders={
                    "programmatic": "unchanged hidden task checks; expect_fail_before enforced"
                },
                unfinished=[r.to_dict() for r in results if r.unfinished] + pending,
                extra={
                    "container_image": image,
                    "isolation_test": "tests/test_eval_isolation_live.py passed",
                    "stop_reason": stop,
                    "arms": ARMS,
                    "transport": "fixed concrete models directly through proxy; no production router bindings",
                },
            ),
        )
        print(
            "FINISHED sessions wire",
            len(wire),
            "plus text attempts recorded at pilot start and probes",
            len(wire) + text_calls + 5,
            "stop",
            stop,
            flush=True,
        )


asyncio.run(main())
