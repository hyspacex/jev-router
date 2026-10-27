# Run manifest: evals/admission.py; bounded live runs then offline cache reconstruction

- **kind**: `mixed`
- **written**: 2026-10-27T16:53:43+00:00
- **repo**: `7281ea525fd012dcb57a3170262310a3e4a3641f-dirty`
- **jev model**: `-`
- **repeats**: 2
- **cases**: 38 (admission 38)
- **cache**: candidates=two distinct samples, controls=sample 0 historical; sample 1 dated fresh, finalization_live_calls=0
- **caps**: approved_total_wire_calls=600, max_tokens=8000, preflight=5, text_ceiling=310, text_wire_attempts=178, tool_wire_attempts=195, tools_ceiling=250, total_wire_attempts=378
- **quota coverage**: none recorded

## Candidate profiles

| model | protocol | window | max output | revision |
| --- | --- | ---: | ---: | --- |
| astra | None | None | - | - |
| astra-high | None | None | - | - |
| astra-medium | None | None | - | - |
| deepseek41-low | None | None | - | - |
| deepseek41-max | None | None | - | - |
| gemma4-31b | None | None | - | - |
| glm-5.3 | None | None | - | - |
| glm-5.3-flash | None | None | - | - |
| glm-5.3-high | None | None | - | - |
| grok-4.6 | None | None | - | - |
| grok-4.6-high | None | None | - | - |
| grok-4.6-medium | None | None | - | - |
| kimi-k3 | None | None | - | - |
| luna | None | None | - | - |
| luna6 | None | None | - | - |
| sol6 | None | None | - | - |
| sol6-high | None | None | - | - |
| sol6-xhigh | None | None | - | - |
| sol61-high | None | None | - | - |
| sol61-low | None | None | - | - |
| sol61-xhigh | None | None | - | - |

## Graders

- checks: outcome_specs.yaml; apostrophe correction applied equally
- judge_effective_override: medium; judged hard cases not reached
- judge_requested: gpt-5.6-sol(high)

## Unfinished and excluded

- unfinished: {"step": "refresh_hard", "case": "other-buy-vs-rent-84", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "astra-high", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "sol6-xhigh", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "astra-high", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "sol6-xhigh", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "astra-high", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "sol6-xhigh", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "astra-high", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "sol6-xhigh", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "astra-high", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "sol6-xhigh", "samples": 1, "planned": 2}
- excluded: {"route": "deepseek41-max", "reason": "empty 8000-token truncated reply; retained in stopped-empty; no qualification"}
