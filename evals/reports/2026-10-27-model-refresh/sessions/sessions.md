# Coding-session pilot — contaminated, not qualification evidence

**Post-run finding:** preflight hidden checks leave pytest cache metadata in the
agent workspace. Candidate replies referred to those hidden-test names. The
counts below describe this run but cannot establish a clean model ranking.
No new tool pair is admitted. See [method and limits](../METHOD.md).

Spec 13.5. This is a pilot: it is for finding failures and measuring what a session costs, not for proving a small improvement. Eight tasks cannot separate two policies that are close, and a capped or unfinished session is reported below rather than dropped.

| arm | completed | capped | errored | calls | tool calls | median wall clock |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| refresh_glm_high | 87.5 [52.9, 97.8] | 1 | 0 | 59 | 77 | 10s |
| refresh_deepseek_max | 50.0 [21.5, 78.5] | 4 | 0 | 85 | 121 | 22s |
| refresh_sol61_low | 87.5 [52.9, 97.8] | 1 | 0 | 51 | 51 | 29s |

## By task

| task | family | refresh_glm_high | refresh_deepseek_max | refresh_sol61_low |
| --- | --- |---|---|---|
| context-growth-audit | context-growth | 1/1 | 1/1 | 1/1 |
| diagnosis-interacting | diagnosis | 1/1 | 1/1 | 1/1 |
| environment-missing | environment | 1/1 | 0/1 (capped) | 1/1 |
| implementation-csv | implementation | 1/1 | 0/1 (capped) | 1/1 |
| implementation-money | implementation | 1/1 | 0/1 (capped) | 1/1 |
| mechanical-rename | mechanical | 1/1 | 1/1 | 1/1 |
| mechanical-transform | mechanical | 1/1 | 1/1 | 1/1 |
| multi-turn-ladder | multi-turn | 0/1 (capped) | 0/1 (capped) | 0/1 (capped) |

## Paired comparison

- refresh_glm_high against refresh_deepseek_max: 8 paired tasks, paired bootstrap p = 0.064. With this few tasks a p above 0.05 means the pilot could not tell them apart, not that they are the same.
- refresh_glm_high against refresh_sol61_low: 8 paired tasks, paired bootstrap p = 1.000. With this few tasks a p above 0.05 means the pilot could not tell them apart, not that they are the same.

## Capped, unfinished and errored

- `implementation-csv` / `refresh_deepseek_max` repeat 0: call cap: 12 of 12 (turns 0/1)
- `implementation-money` / `refresh_deepseek_max` repeat 0: call cap: 12 of 12 (turns 0/1)
- `multi-turn-ladder` / `refresh_glm_high` repeat 0: call cap: 12 of 12 (turns 1/3)
- `multi-turn-ladder` / `refresh_deepseek_max` repeat 0: call cap: 12 of 12 (turns 1/3)
- `multi-turn-ladder` / `refresh_sol61_low` repeat 0: call cap: 12 of 12 (turns 1/3)
- `environment-missing` / `refresh_deepseek_max` repeat 0: call cap: 12 of 12 (turns 0/1)
