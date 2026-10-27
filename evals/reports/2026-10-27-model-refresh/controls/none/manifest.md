# Run manifest: evals/run_eval.py

- **kind**: `policy_replay`  — answers came out of the disk cache, so this says what the policy does with them. It is not a live classifier or latency measurement.
- **written**: 2026-10-27T16:14:47+00:00
- **repo**: `86251c9162b96b8f7f4213cf2f00635207f177b7-dirty`
- **jev model**: `jev-1.13.0`
- **repeats**: 1
- **cases**: 171 (held_out 51, tune 120)
- **seeds**: shuffled_labels=4242, split=20260918, stability=seeded per perturbation, see evals/metrics.py
- **cache**: hits=171, jev=enabled, live_calls=0
- **caps**: concurrency=2, draft_max_calls=0
- **quota coverage**: not read; every case ran at pressure zero

## Variants

| variant | state builder | questions | question hash | policy hash |
| --- | --- | --- | --- | --- |
| router_yaml | continuation_aware_v1 | task, difficulty, harm_if_wrong | `f48085d613fa3bab` | `af4c44d7522af123` |

## Candidate profiles

| model | protocol | window | max output | revision |
| --- | --- | ---: | ---: | --- |
| gpt-6-astra | openai-chat | 400000 | - | - |
| gpt-6-luna | openai-chat | 400000 | - | - |
| gpt-6-sol | openai-chat | 400000 | - | - |
| grok-4.6 | openai-chat | 500000 | - | - |
| ollama/gemma4-31b | openai-chat | 256000 | - | - |
| ollama/glm-5.3 | openai-chat | 1000000 | - | - |
| ollama/glm-5.3-flash | openai-chat | 128000 | - | - |

## Unfinished and excluded

Nothing. Every unit of work in this run finished.
