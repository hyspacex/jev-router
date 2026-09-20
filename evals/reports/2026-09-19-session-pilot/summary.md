# Eight-task coding-session pilot

**24 of 24 sessions passed their hidden checks.** Each of eight tasks ran once under each policy. There were 0 errors and 0 capped sessions.

| Policy | Passed | Astra sessions | Flash sessions | Model calls | Median elapsed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fixed Astra medium | 8/8 | 8 | 0 | 48 | 29.9s |
| Deterministic rules | 8/8 | 8 | 0 | 48 | 28.9s |
| Strict Jev admission | 8/8 | 5 | 3 | 45 | 28.8s |

## Results by task

Every timing includes task setup, routing where applicable, tools, model calls, and final checks.

| Task | Fixed strong | Rules | Jev | Jev model / effort |
| --- | ---: | ---: | ---: | --- |
| mechanical-rename | pass / 18.8s | pass / 18.8s | pass / 18.7s | `gpt-6-astra` / medium |
| mechanical-transform | pass / 11.4s | pass / 9.9s | pass / 3.8s | `ollama/glm-5.3-flash` / none |
| implementation-csv | pass / 38.1s | pass / 36.9s | pass / 35.5s | `gpt-6-astra` / medium |
| implementation-money | pass / 22.9s | pass / 33.7s | pass / 6.9s | `ollama/glm-5.3-flash` / none |
| diagnosis-interacting | pass / 28.8s | pass / 29.9s | pass / 25.7s | `gpt-6-astra` / medium |
| context-growth-audit | pass / 34.9s | pass / 27.0s | pass / 34.0s | `gpt-6-astra` / medium |
| multi-turn-ladder | pass / 71.3s | pass / 79.1s | pass / 31.9s | `ollama/glm-5.3-flash` / none |
| environment-missing | pass / 30.9s | pass / 27.9s | pass / 34.2s | `gpt-6-astra` / medium |

## Observed execution tokens

These are provider-returned execution-token counts, not subscription quota charges. They exclude Jev classification. Prompt tokens can include cached input; cache namespaces were not isolated.

| Policy | Prompt tokens | Completion tokens | Total execution tokens | Astra execution tokens |
| --- | ---: | ---: | ---: | ---: |
| Fixed Astra medium | 45,567 | 4,671 | 50,238 | 50,238 |
| Deterministic rules | 44,453 | 4,687 | 49,140 | 49,140 |
| Strict Jev admission | 39,123 | 4,362 | 43,485 | 24,747 |

## What this shows

All three paths can complete this small executable suite under the tested limits. Jev selected Flash for `mechanical-transform`, `implementation-money`, `multi-turn-ladder`. Those substitutions passed the same hidden checks as Astra.

This is evidence for those tasks, not a demonstrated quality-loss bound or an estimate of general coding performance. The suite contains only eight small repositories, with one attempt per policy and task. It needs broader tasks and repeated trials before using small score or timing differences to change qualification.

Observed OpenAI pressure was 0.24 throughout routed admissions. OpenAI and xAI telemetry were fresh; Ollama telemetry was unknown. This did not exercise quota exhaustion. Reduced Astra session or token counts do not establish subscription-capacity savings.

Arm order rotated by task. Runs were sequential, but caching and unrelated provider load were not controlled. Latency differences are descriptive.

## Run contract

- Source: `d2e72a935a2af2629fd6b1323f799e527a29fafb`; full offline suite: 1,100 passed, 2 skipped.
- Three arms, eight tasks, one repeat: 24 attempts; every planned attempt is retained.
- Fixed baseline: Astra medium. Rules use a dedicated strict alias with `decider: rules`. Jev uses `auto-session` with the deployed policy and model pool.
- Admission sees the first prompt and actual tool schemas. All arms receive the same tools and a 4,096-token output limit. Later turns keep the initial binding.
- Per-task call/time caps apply, bounded by 24 calls and 600 seconds. Adaptive effort is off.
- Docker runs commands and hidden checks without networking or credentials. A separate router process and SQLite database were used; production was not modified.
- Before model calls, a Docker/pytest sanity check passed and all eight untouched fixtures failed their hidden checks.
- The missing-credential prompt was clarified before the run to request `FINDINGS.md`, which its existing hidden checker requires.
- The harness forwards upstream authentication, verifies the rules admission source, records provider usage, checkpoints each attempt, and closes strict sessions. All 16 routed sessions were closed.

[Per-session results](results.json) and [provenance](provenance.json) contain the counts, configuration fingerprint, image identity and task hashes. Raw local output remains in `evals/results/sessions-1789868187/`.
