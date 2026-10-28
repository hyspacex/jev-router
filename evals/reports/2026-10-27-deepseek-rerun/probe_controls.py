"""Which request field changes DeepSeek 1017's reasoning through the proxy.

The suffix probe showed DeepSeek reasons the same at none/low/medium/high.
This tries body fields the Ollama OpenAI-compatible API may honour, on a
prompt that needs some thought, and records reasoning volume. One call each.
"""

import json
import time
from pathlib import Path

import httpx
from probe_effort import URL, key

OUT = Path(__file__).resolve().parent
PROMPT = (
    "Table orders(id, customer_id, created_at, total). Write a PostgreSQL query "
    "that returns, for each customer, their three most recent orders and the "
    "running total of `total` across those three, newest first. Explain briefly."
)
DS = "ollama/deepseek-v4.1-flash:1017"
VARIANTS = [
    ("suffix none", {"model": f"{DS}(none)"}),
    ("suffix high", {"model": f"{DS}(high)"}),
    ("reasoning_effort none", {"model": DS, "reasoning_effort": "none"}),
    ("reasoning_effort low", {"model": DS, "reasoning_effort": "low"}),
    ("reasoning_effort high", {"model": DS, "reasoning_effort": "high"}),
    ("think false", {"model": DS, "think": False}),
    ("reasoning.enabled false", {"model": DS, "reasoning": {"enabled": False}}),
    ("glm-5.3 suffix none", {"model": "ollama/glm-5.3(none)"}),
]


def main() -> None:
    k = key()
    rows = []
    with httpx.Client(timeout=300) as client:
        for label, extra in VARIANTS:
            body = {"messages": [{"role": "user", "content": PROMPT}], "max_tokens": 16000, **extra}
            started = time.perf_counter()
            row = {"variant": label}
            try:
                resp = client.post(URL, json=body, headers={"Authorization": f"Bearer {k}"})
                row["status"] = resp.status_code
                row["latency_s"] = round(time.perf_counter() - started, 2)
                if resp.status_code >= 400:
                    row["body"] = resp.text[:200]
                else:
                    data = resp.json()
                    msg = (data.get("choices") or [{}])[0].get("message") or {}
                    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                    row.update(
                        model_returned=data.get("model"),
                        completion_tokens=(data.get("usage") or {}).get("completion_tokens"),
                        reasoning_chars=len(reasoning),
                        answer_words=len((msg.get("content") or "").split()),
                    )
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"{type(exc).__name__}: {exc}"[:200]
            rows.append(row)
            print(json.dumps(row), flush=True)
            (OUT / "probe-controls.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
