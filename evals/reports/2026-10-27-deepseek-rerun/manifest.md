# Run manifest: evals/reports/2026-10-27-deepseek-rerun/run_rerun.py

- **kind**: `live`
- **written**: 2026-10-28T05:40:58+00:00
- **repo**: `05b8bbf335fccf56218b48e7ec0509326ba180a8-dirty`
- **jev model**: `-`
- **repeats**: 3
- **cases**: 26 (no splits)
- **seeds**: arm_order=20261027, bootstrap=11
- **cache**: dir=evals/.cache/rerun-2026-10-27, enabled=True, note=every cell fresh; nothing reused from the refresh cache
- **caps**: concurrency=1, max_tokens=16000, probe_calls=34, run_wire_attempts=235, run_wire_ceiling=250, timeout_s=300, total_calls=269
- **quota coverage**: none recorded

## Candidate profiles

| model | protocol | window | max output | revision |
| --- | --- | ---: | ---: | --- |
| deepseek | None | None | - | - |
| glm-flash-medium | None | None | - | - |
| glm-flash-none | None | None | - | - |
| glm-medium | None | None | - | - |
| glm-none | None | None | - | - |

## Graders

- checks: outcome_specs.yaml unchanged; checks.py re.VERBOSE/triple-quote fix applied to every arm
- judge: none

## Unfinished and excluded

Nothing. Every unit of work in this run finished.
