"""DeepSeek 1017 vs GLM rerun: matched reasoning, clean latency, streamed.

Spends real Ollama quota through the proxy at JEV_ROUTER_UPSTREAM. The plan and decision
rules are in plan.json, written before the first call. Every HTTP attempt is
appended to wire-attempts.json before it is sent, and the run refuses to
pass the approved ceiling. Replies are kept in the git-ignored
evals/.cache/rerun-2026-10-27/, one file per (step, case, arm, sample), so a
resume never pays twice. One request at a time, arms shuffled per case and
sample with a fixed seed, so every arm sees the same load.
"""

import json
import random
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "evals"), str(ROOT / "src"), str(HERE)]
import admission as A  # noqa: E402
from common import load_cases  # noqa: E402
from probe_effort import URL, key  # noqa: E402

PLAN = json.loads((HERE / "plan.json").read_text())
CACHE = ROOT / "evals/.cache/rerun-2026-10-27"
LEDGER = HERE / "wire-attempts.json"
CEILING = PLAN["budget"]["run_wire_ceiling"]


def ledger() -> list[dict]:
    try:
        return json.loads(LEDGER.read_text())
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise RuntimeError("Refusing to reset an unreadable spend ledger") from exc


def cell_path(step: str, cid: str, arm: str, sample: int) -> Path:
    return CACHE / f"{step}__{cid}__{arm}__{sample}.json"


def stream_once(client: httpx.Client, k: str, model: str, messages: list) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": PLAN["max_tokens"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started = time.perf_counter()
    first_any = first_content = None
    content, reasoning_chars, usage, finish, returned = [], 0, {}, None, None
    with client.stream("POST", URL, json=body, headers={"Authorization": f"Bearer {k}"}) as resp:
        if resp.status_code >= 400:
            return {"status": resp.status_code, "error": resp.read().decode()[:300]}
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            returned = chunk.get("model") or returned
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                r = delta.get("reasoning_content") or delta.get("reasoning") or ""
                c = delta.get("content") or ""
                now = time.perf_counter() - started
                if (r or c) and first_any is None:
                    first_any = now
                if c and first_content is None:
                    first_content = now
                reasoning_chars += len(r)
                content.append(c)
                finish = choice.get("finish_reason") or finish
    text = "".join(content)
    return {
        "status": 200,
        "model_returned": returned,
        "text": text,
        "total_s": round(time.perf_counter() - started, 3),
        "first_token_s": None if first_any is None else round(first_any, 3),
        "first_answer_token_s": None if first_content is None else round(first_content, 3),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "reasoning_chars": reasoning_chars,
        "finish": finish,
        "error": None if text.strip() else "empty reply",
    }


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = ledger()
    cases = {c.id: c for c in load_cases()}
    order = []
    rng = random.Random(PLAN["seed"])
    for sample in range(PLAN["samples"]):
        for step, s in PLAN["steps"].items():
            for cid in s["cases"]:
                arms = list(s["arms"])
                rng.shuffle(arms)
                order += [(step, cid, arm, sample) for arm in arms]
    todo = [o for o in order if not cell_path(*o).exists()]
    print(f"{len(order)} cells, {len(todo)} to run, {len(rows)} attempts already in the ledger", flush=True)
    k = key()
    empties: dict[str, int] = {}
    with httpx.Client(timeout=PLAN["timeout_s"]) as client:
        for step, cid, arm, sample in todo:
            model = PLAN["steps"][step]["arms"][arm]
            messages = A.text_messages(cases[cid])
            for attempt in range(3):
                if len(rows) >= CEILING:
                    print("STOP: wire ceiling reached", flush=True)
                    return
                row = {"attempt": len(rows) + 1, "step": step, "case": cid, "arm": arm, "sample": sample, "model": model}
                rows.append(row)
                LEDGER.write_text(json.dumps(rows, indent=2))
                try:
                    rec = stream_once(client, k, model, messages)
                except (httpx.HTTPError, ValueError) as exc:
                    rec = {"status": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
                row.update(status=rec.get("status"), error=rec.get("error"), model_returned=rec.get("model_returned"))
                LEDGER.write_text(json.dumps(rows, indent=2))
                if rec.get("status") == 200:
                    break
                if rec.get("status") not in (None, 429, 500, 502, 503, 529):
                    print("STOP: provider error", rec, flush=True)
                    return
                time.sleep(5 * (attempt + 1))
            else:
                print("STOP: three failed attempts", step, cid, arm, rec.get("error"), flush=True)
                return
            rec.update(step=step, case=cid, arm=arm, sample=sample, model=model, attempts=attempt + 1,
                       at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            cell_path(step, cid, arm, sample).write_text(json.dumps(rec, indent=2))
            empties[arm] = empties.get(arm, 0) + 1 if rec["error"] else 0
            print(f"{step:9} {cid:40} {arm:14} s{sample} {rec['total_s']:6.2f}s "
                  f"ttfa={rec['first_answer_token_s']} tok={rec['completion_tokens']} {rec['error'] or ''}", flush=True)
            if empties[arm] >= 3:
                print("STOP: three empty replies in a row from", arm, flush=True)
                return
    print("FINISHED", len(rows), "attempts", flush=True)


if __name__ == "__main__":
    main()
