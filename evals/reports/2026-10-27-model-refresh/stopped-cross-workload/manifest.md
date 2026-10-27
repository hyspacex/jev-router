# Run manifest: evals/admission.py via bounded refresh runner

- **kind**: `mixed`
- **written**: 2026-10-27T16:14:34+00:00
- **repo**: `86251c9162b96b8f7f4213cf2f00635207f177b7-dirty`
- **jev model**: `-`
- **repeats**: 2
- **cases**: 38 (admission 38)
- **cache**: control_sample_0=historical cache when available, control_sample_1=fresh dated salt, enabled=True, live_successes=48
- **caps**: actual_text_wire_calls=48, approved_total_wire_calls=600, candidate_max_tokens=8000, concurrency=2, preflight_calls=5, text_stage_wire_cap=310, timeout_s=180
- **quota coverage**: none recorded

## Candidate profiles

| model | protocol | window | max output | revision |
| --- | --- | ---: | ---: | --- |
| astra-high | None | None | - | - |
| astra-medium | None | None | - | - |
| deepseek41-low | None | None | - | - |
| deepseek41-max | None | None | - | - |
| glm-5.3 | None | None | - | - |
| glm-5.3-flash | None | None | - | - |
| sol6-xhigh | None | None | - | - |
| sol61-high | None | None | - | - |
| sol61-low | None | None | - | - |
| sol61-xhigh | None | None | - | - |

## Graders

- checks: unchanged outcome_specs.yaml
- judge_proxy_override: medium
- judge_requested: gpt-5.6-sol(high)

## Unfinished and excluded

- unfinished: {"step": "refresh_mid", "case": "code-prose-shaped-really-code-09", "route": "deepseek41-low", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "code-prose-shaped-really-code-09", "route": "deepseek41-max", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "code-prose-shaped-really-code-09", "route": "sol61-low", "samples": 1, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "code-chinese-hooks-08", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "code-chinese-hooks-08", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "code-chinese-hooks-08", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "code-chinese-hooks-08", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "debug-css-overflow-17", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "debug-css-overflow-17", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "debug-css-overflow-17", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "debug-css-overflow-17", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-borrow-checker-misleading-75", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-borrow-checker-misleading-75", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-borrow-checker-misleading-75", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-borrow-checker-misleading-75", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multilingual-pt-coorte-retencao-161", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multilingual-pt-coorte-retencao-161", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multilingual-pt-coorte-retencao-161", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multilingual-pt-coorte-retencao-161", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multiturn-ratelimiter-continue-138", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multiturn-ratelimiter-continue-138", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multiturn-ratelimiter-continue-138", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "multiturn-ratelimiter-continue-138", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-five-second-cron-tz-96", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-five-second-cron-tz-96", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-five-second-cron-tz-96", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-five-second-cron-tz-96", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "design-index-strategy-26", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "design-index-strategy-26", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "design-index-strategy-26", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "design-index-strategy-26", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-static-markup-rounding-99", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-static-markup-rounding-99", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-static-markup-rounding-99", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "adv-static-markup-rounding-99", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-weakmap-vs-map-72", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-weakmap-vs-map-72", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-weakmap-vs-map-72", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_mid", "case": "quick-weakmap-vs-map-72", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "writing-medical-discharge-38", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "writing-medical-discharge-38", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "writing-medical-discharge-38", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "writing-medical-discharge-38", "route": "astra-medium", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "writing-medical-discharge-38", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-trial-abstract-66", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-trial-abstract-66", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-trial-abstract-66", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-trial-abstract-66", "route": "astra-medium", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-trial-abstract-66", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "extract-invoice-json-62", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "extract-invoice-json-62", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "extract-invoice-json-62", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "extract-invoice-json-62", "route": "astra-medium", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "extract-invoice-json-62", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-meeting-notes-dates-61", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-meeting-notes-dates-61", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-meeting-notes-dates-61", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-meeting-notes-dates-61", "route": "astra-medium", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "summarise-meeting-notes-dates-61", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "adv-sow-dates-91", "route": "deepseek41-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "adv-sow-dates-91", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "adv-sow-dates-91", "route": "sol61-low", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "adv-sow-dates-91", "route": "astra-medium", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_faithful", "case": "adv-sow-dates-91", "route": "glm-5.3", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-go-deadlock-short-40-11", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-go-deadlock-short-40-11", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-go-deadlock-short-40-11", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-go-deadlock-short-40-11", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-go-deadlock-short-40-11", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-adversarial-injection-in-log-16", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-adversarial-injection-in-log-16", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-adversarial-injection-in-log-16", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-adversarial-injection-in-log-16", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-adversarial-injection-in-log-16", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-investor-update-numbers-44", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-investor-update-numbers-44", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-investor-update-numbers-44", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-investor-update-numbers-44", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-investor-update-numbers-44", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-simple-question-proof-78", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-simple-question-proof-78", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-simple-question-proof-78", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-simple-question-proof-78", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-simple-question-proof-78", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-probability-puzzle-79", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-probability-puzzle-79", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-probability-puzzle-79", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-probability-puzzle-79", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-probability-puzzle-79", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-buy-vs-rent-84", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-buy-vs-rent-84", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-buy-vs-rent-84", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-buy-vs-rent-84", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "other-buy-vs-rent-84", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "debug-spanish-slow-query-18", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "design-eventbus-tradeoff-20", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-legal-clause-39", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-regulator-response-42", "route": "sol6-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "deepseek41-max", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "sol61-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "sol61-xhigh", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "astra-high", "samples": 0, "planned": 2}
- unfinished: {"step": "refresh_hard", "case": "writing-rsu-tax-explainer-40", "route": "sol6-xhigh", "samples": 0, "planned": 2}
