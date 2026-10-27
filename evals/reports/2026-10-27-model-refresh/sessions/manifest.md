# Run manifest: evals/run_sessions.py via fixed-arm refresh runner

- **kind**: `live`
- **written**: 2026-10-27T16:25:46+00:00
- **repo**: `7281ea525fd012dcb57a3170262310a3e4a3641f-dirty`
- **jev model**: `-`
- **repeats**: 1
- **cases**: 8 (pilot 8)
- **caps**: actual_session_wire_calls=195, approved_total=600, calls_per_session=12, max_tokens=4096, preflight_calls=5, session_wire_limit=250, text_calls_at_session_start=58, text_stage_reserved=310, wall_seconds=240
- **quota coverage**: none recorded

## Candidate profiles

| model | protocol | window | max output | revision |
| --- | --- | ---: | ---: | --- |
| refresh_deepseek_max | None | None | - | - |
| refresh_glm_high | None | None | - | - |
| refresh_sol61_low | None | None | - | - |

## Graders

- programmatic: unchanged hidden task checks; expect_fail_before enforced

## Unfinished and excluded

- unfinished: {"task": "implementation-csv", "family": "implementation", "arm": "refresh_deepseek_max", "repeat": 0, "completed": false, "capped": "call cap: 12 of 12", "calls": 12, "wall_clock_s": 37.05, "tool_calls": 16, "model": "ollama/deepseek-v4.1-flash:1017", "effort": "max", "turns": "0/1", "error": "", "admission": {}, "usage": {"prompt_tokens": 26811, "completion_tokens": 5921, "total_tokens": 32732}}
- unfinished: {"task": "implementation-money", "family": "implementation", "arm": "refresh_deepseek_max", "repeat": 0, "completed": false, "capped": "call cap: 12 of 12", "calls": 12, "wall_clock_s": 20.01, "tool_calls": 14, "model": "ollama/deepseek-v4.1-flash:1017", "effort": "max", "turns": "0/1", "error": "", "admission": {}, "usage": {"prompt_tokens": 21369, "completion_tokens": 2293, "total_tokens": 23662}}
- unfinished: {"task": "multi-turn-ladder", "family": "multi-turn", "arm": "refresh_glm_high", "repeat": 0, "completed": false, "capped": "call cap: 12 of 12", "calls": 12, "wall_clock_s": 19.48, "tool_calls": 12, "model": "ollama/glm-5.3", "effort": "high", "turns": "1/3", "error": "", "admission": {}, "usage": {"prompt_tokens": 18314, "completion_tokens": 2588, "total_tokens": 20902}}
- unfinished: {"task": "multi-turn-ladder", "family": "multi-turn", "arm": "refresh_deepseek_max", "repeat": 0, "completed": false, "capped": "call cap: 12 of 12", "calls": 12, "wall_clock_s": 31.71, "tool_calls": 17, "model": "ollama/deepseek-v4.1-flash:1017", "effort": "max", "turns": "1/3", "error": "", "admission": {}, "usage": {"prompt_tokens": 19326, "completion_tokens": 5816, "total_tokens": 25142}}
- unfinished: {"task": "multi-turn-ladder", "family": "multi-turn", "arm": "refresh_sol61_low", "repeat": 0, "completed": false, "capped": "call cap: 12 of 12", "calls": 12, "wall_clock_s": 66.66, "tool_calls": 11, "model": "gpt-6.1-sol", "effort": "low", "turns": "1/3", "error": "", "admission": {}, "usage": {"prompt_tokens": 18802, "completion_tokens": 1239, "total_tokens": 20041}}
- unfinished: {"task": "environment-missing", "family": "environment", "arm": "refresh_deepseek_max", "repeat": 0, "completed": false, "capped": "call cap: 12 of 12", "calls": 12, "wall_clock_s": 51.34, "tool_calls": 23, "model": "ollama/deepseek-v4.1-flash:1017", "effort": "max", "turns": "0/1", "error": "", "admission": {}, "usage": {"prompt_tokens": 24140, "completion_tokens": 7804, "total_tokens": 31944}}
