"""What each effort suffix does to each Ollama model, measured.

The proxy declares no thinking levels for its Ollama models, so the suffix's
effect is not observable from config. One fixed short prompt per
(model, suffix); the record is completion tokens, any reasoning field the
reply carries, the answer and the latency. Spends one call per row.
"""

import json
import sys
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT.parents[1]))
from common import upstream_base_url, upstream_key  # noqa: E402

# The proxy and key come from JEV_ROUTER_UPSTREAM and UPSTREAM_API_KEY, as
# for every other live eval. These runs used the deployed CLIProxyAPI.
URL = upstream_base_url().rstrip("/") + "/v1/chat/completions"
PROMPT = (
    "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than "
    "the ball. How much does the ball cost? Answer with the amount only."
)
MODELS = ["ollama/glm-5.3-flash", "ollama/glm-5.3", "ollama/deepseek-v4.1-flash:1017"]
SUFFIXES = ["none", "low", "medium", "high"]


def key() -> str:
    return upstream_key()


def main() -> None:
    k = key()
    rows = []
    plan = [(m, s) for m in MODELS for s in SUFFIXES]
    plan.append(("ollama/deepseek-v4.1-flash:1017", None))  # no suffix: provider default
    with httpx.Client(timeout=180) as client:
        for model, suffix in plan:
            name = f"{model}({suffix})" if suffix else model
            body = {"model": name, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 16000}
            started = time.perf_counter()
            row = {"model": name}
            try:
                resp = client.post(URL, json=body, headers={"Authorization": f"Bearer {k}"})
                row["status"] = resp.status_code
                row["latency_s"] = round(time.perf_counter() - started, 2)
                data = resp.json()
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                usage = data.get("usage") or {}
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                row.update(
                    model_returned=data.get("model"),
                    answer=(msg.get("content") or "")[:120],
                    completion_tokens=usage.get("completion_tokens"),
                    reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                    reasoning_chars=len(reasoning),
                    finish=(data.get("choices") or [{}])[0].get("finish_reason"),
                )
            except Exception as exc:  # noqa: BLE001 - record and continue
                row["error"] = f"{type(exc).__name__}: {exc}"[:200]
            rows.append(row)
            print(json.dumps(row), flush=True)
            (OUT / "probe-effort.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
