import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src"))
import admission as A  # noqa: E402
import httpx  # noqa: E402
import yaml  # noqa: E402
from common import Budget, UpstreamClient, load_cases, upstream_key  # noqa: E402
from manifest import build_manifest, run_kind, write_manifest  # noqa: E402

OUT = ROOT / "evals/reports/2026-10-27-model-refresh"
CFG = yaml.safe_load(A.SPEC_FILE.read_text())
STEPS = ["refresh_fast", "refresh_mid", "refresh_faithful", "refresh_hard"]
# Prospectively screen out the empty/truncated DeepSeek max arm. Its original
# partial results remain in stopped-empty/, and it is never reported qualified.
for name in STEPS:
    if "deepseek41-max" in CFG["steps"][name]["routes"]:
        CFG["steps"][name]["routes"].remove("deepseek41-max")
CONTROLS = {
    CFG["routes"][r]["upstream"]
    for r in ["glm-5.3-flash", "glm-5.3", "astra-medium", "astra-high", "sol6-xhigh"]
}
try:
    wire_rows = json.loads((OUT / "wire-attempts.json").read_text())
except FileNotFoundError:
    wire_rows = []
except ValueError as exc:
    raise RuntimeError("Refusing to reset an unreadable spend ledger") from exc
wire_calls = len(wire_rows)
wire_limit = 310  # total across resumes, incl. judges; 5 preflight calls already spent


class CountedTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        global wire_calls
        if wire_calls >= wire_limit:
            raise RuntimeError("hard text-stage wire-attempt budget reached")
        wire_calls += 1
        try:
            body = json.loads(request.content)
        except ValueError as exc:
            raise RuntimeError("Expected a JSON evaluation request") from exc
        row = {"attempt": wire_calls, "model": body["model"]}
        wire_rows.append(row)
        (OUT / "wire-attempts.json").write_text(json.dumps(wire_rows, indent=2))
        try:
            response = await self.inner.handle_async_request(request)
            row["status"] = response.status_code
            return response
        except Exception as exc:
            row["error"] = type(exc).__name__
            raise
        finally:
            (OUT / "wire-attempts.json").write_text(json.dumps(wire_rows, indent=2))

    async def aclose(self):
        await self.inner.aclose()


class Client(UpstreamClient):
    async def chat(
        self, model, messages, max_tokens=8000, temperature=None, cache_salt=""
    ):
        # One historical control sample plus one fresh matched sample per case.
        # Never describe the mixed median as a fresh latency measurement.
        if model in CONTROLS and cache_salt == "sample-1":
            cache_salt = "refresh-2026-10-27-control-1"
        result = await super().chat(
            model, messages, max_tokens, temperature, cache_salt
        )
        if result.error and "not run" not in result.error and self.budget is not None:
            self.budget.stop_reason = "provider error: " + result.error[:100]
        return result


async def main():
    # The proxy and its key come from JEV_ROUTER_UPSTREAM and UPSTREAM_API_KEY.
    upstream_key()
    budget = Budget(max_calls=wire_limit)
    client = Client(concurrency=2, cache=True, budget=budget, timeout_s=180)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=CountedTransport(), timeout=180)
    cases = {c.id: c for c in load_cases()}
    specs = yaml.safe_load((ROOT / "evals/outcome_specs.yaml").read_text())["cases"]
    payload = {"steps": {}}
    error = None
    try:
        for name in STEPS:
            # A workload change is not provider degradation. Keep the same
            # per-route 3x guard, but begin its history at the step boundary.
            budget = Budget(max_calls=wire_limit - wire_calls)
            client.budget = budget
            step = A.Step(name, CFG, CFG["steps"][name])
            # Prior stopped/cached failures must remain failures, but must not
            # trigger another live retry. The screened arm is omitted above.
            result = await A.run_step(step, CFG, client, cases, specs, {}, budget)
            payload["steps"][name] = result
            (OUT / "results.json").write_text(json.dumps(payload, indent=2))
            print("STEP DONE", name, json.dumps(result["verdicts"]), flush=True)
            if budget.stop_reason:
                break
    except Exception as exc:  # noqa: BLE001 - persist failed-run manifest
        error = type(exc).__name__ + ": " + str(exc)
        print("STOP", error, flush=True)
    finally:
        await client.aclose()
        unfinished = []
        for name in STEPS:
            rows = {
                r["case_id"]: r for r in payload["steps"].get(name, {}).get("rows", [])
            }
            for cid in CFG["steps"][name]["cases"]:
                for route in CFG["steps"][name]["routes"]:
                    n = (
                        rows.get(cid, {})
                        .get("routes", {})
                        .get(route, {})
                        .get("samples", 0)
                    )
                    if n < 2:
                        unfinished.append(
                            {
                                "step": name,
                                "case": cid,
                                "route": route,
                                "samples": n,
                                "planned": 2,
                            }
                        )
        write_manifest(
            OUT,
            build_manifest(
                script="evals/admission.py via bounded refresh runner",
                kind=run_kind(
                    client.calls,
                    sum(
                        c["samples"]
                        for s in payload["steps"].values()
                        for r in s["rows"]
                        for c in r["routes"].values()
                    ),
                ),
                cases={
                    "count": len({c for s in STEPS for c in CFG["steps"][s]["cases"]}),
                    "by_split": {
                        "admission": [
                            c for s in STEPS for c in CFG["steps"][s]["cases"]
                        ]
                    },
                },
                candidates={
                    r: CFG["routes"][r]
                    for s in STEPS
                    for r in CFG["steps"][s]["routes"]
                },
                repeats=2,
                caps={
                    "approved_total_wire_calls": 600,
                    "preflight_calls": 5,
                    "text_stage_wire_cap": wire_limit,
                    "actual_text_wire_calls": wire_calls,
                    "health_baseline": "per workload and route; original 3x guard retained",
                    "candidate_max_tokens": 8000,
                    "concurrency": 2,
                    "timeout_s": 180,
                },
                cache={
                    "enabled": True,
                    "control_sample_0": "historical cache when available",
                    "control_sample_1": "fresh dated salt",
                    "live_successes": client.calls,
                },
                graders={
                    "checks": "outcome_specs.yaml; curly-apostrophe correction applied equally",
                    "judge_requested": CFG["judge_model"],
                    "judge_proxy_override": "medium",
                },
                unfinished=unfinished,
                exclusions=[
                    {
                        "route": "deepseek41-max",
                        "reason": "screened out after 8000-token empty truncated reply; see stopped-empty and tool pilot",
                    }
                ],
                extra={
                    "stop_reason": error or budget.stop_reason,
                    "steps": STEPS,
                    "limitations": [
                        "Text-only, not tool qualification",
                        "Mixed historical/fresh control latencies are not fresh measurements",
                        "Same-family Sol judge; report checked and judged populations separately",
                        "Provider effort internals not observable from completion replies",
                    ],
                },
            ),
        )
        print(
            "FINISHED",
            "wire attempts",
            wire_calls,
            "stop",
            error or budget.stop_reason,
            flush=True,
        )


asyncio.run(main())
