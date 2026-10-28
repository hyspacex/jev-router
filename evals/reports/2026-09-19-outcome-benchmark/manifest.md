# Run manifest: evals/run_outcomes.py

- **kind**: `mixed`
- **written**: 2026-09-19T22:17:17+00:00
- **repo**: `31932d7caf9f61a9776b784eb181c10c95311299-dirty`
- **jev model**: `-`
- **repeats**: 3
- **cases**: 19 (held_out 4, tune 15)
- **seeds**: sample_salt=sample index, see UpstreamClient.chat
- **cache**: live_calls=256, upstream=enabled
- **caps**: candidate_max_tokens=8000, concurrency=3, max_calls=400, stop_reason=ollama/glm-5.3-flash(high) median latency went from 5.6s to 16.9s, more than 3x, stopping
- **quota coverage**: not read; these are API token counts, not subscription consumption

## Candidate profiles

| model | protocol | window | max output | revision |
| --- | --- | ---: | ---: | --- |
| gpt-6-astra | openai-chat | 400000 | - | - |
| ollama/glm-5.3-flash | openai-chat | 128000 | - | - |

## Graders

- judge: gpt-5.6-sol(high)
- programmatic: evals/checks.py
- specs: evals/outcome_specs.yaml

## Unfinished and excluded

- unfinished: {"case": "design-eventbus-tradeoff-20", "reason": "capped or not reached"}
- unfinished: {"case": "design-auth-module-review-23", "reason": "capped or not reached"}
- unfinished: {"case": "design-naming-bikeshed-24", "reason": "capped or not reached"}
- unfinished: {"case": "writing-incident-customer-notice-37", "reason": "capped or not reached"}
- unfinished: {"case": "writing-medical-discharge-38", "reason": "capped or not reached"}
- unfinished: {"case": "writing-legal-clause-39", "reason": "capped or not reached"}
- unfinished: {"case": "writing-rsu-tax-explainer-40", "reason": "capped or not reached"}
- unfinished: {"case": "writing-security-advisory-41", "reason": "capped or not reached"}
- unfinished: {"case": "writing-regulator-response-42", "reason": "capped or not reached"}
- unfinished: {"case": "creative-limerick-printer-45", "reason": "capped or not reached"}
- unfinished: {"case": "creative-noir-opening-46", "reason": "capped or not reached"}
- unfinished: {"case": "creative-wedding-speech-49", "reason": "capped or not reached"}
- unfinished: {"case": "creative-parody-lyrics-50", "reason": "capped or not reached"}
- unfinished: {"case": "polish-product-blurb-55", "reason": "capped or not reached"}
- unfinished: {"case": "polish-readme-intro-58", "reason": "capped or not reached"}
- unfinished: {"case": "polish-translate-customer-reply-59", "reason": "capped or not reached"}
- unfinished: {"case": "summarise-blogpost-tldr-63", "reason": "capped or not reached"}
- unfinished: {"case": "summarise-ticket-export-themes-64", "reason": "capped or not reached"}
- unfinished: {"case": "other-simple-question-proof-78", "reason": "capped or not reached"}
- unfinished: {"case": "other-probability-puzzle-79", "reason": "capped or not reached"}
- unfinished: {"case": "other-gym-split-81", "reason": "capped or not reached"}
- unfinished: {"case": "other-buy-vs-rent-84", "reason": "capped or not reached"}
- unfinished: {"case": "other-explain-to-child-83", "reason": "capped or not reached"}
- unfinished: {"case": "code-chinese-hooks-08", "reason": "capped or not reached"}
- unfinished: {"case": "multilingual-pt-coorte-retencao-161", "reason": "capped or not reached"}
- unfinished: {"case": "multiturn-ratelimiter-continue-138", "reason": "capped or not reached"}
- unfinished: {"case": "adv-five-second-cron-tz-96", "reason": "capped or not reached"}
- unfinished: {"case": "design-index-strategy-26", "reason": "capped or not reached"}
- unfinished: {"case": "adv-static-markup-rounding-99", "reason": "capped or not reached"}
- unfinished: {"case": "quick-weakmap-vs-map-72", "reason": "capped or not reached"}
- unfinished: {"case": "adv-sow-dates-91", "reason": "capped or not reached"}
- unfinished: {"case": "quick-bash-for-loop-70", "reason": "capped or not reached"}
- unfinished: {"case": "code-shell-oneliner-06", "reason": "capped or not reached"}
- unfinished: {"case": "debug-nameerror-typo-14", "reason": "capped or not reached"}
- unfinished: {"case": "polish-slack-grammar-54", "reason": "capped or not reached"}
- unfinished: {"case": "quick-german-idempotent-76", "reason": "capped or not reached"}
- unfinished: {"case": "creative-npc-dialogue-48", "reason": "capped or not reached"}
- unfinished: {"case": "creative-pov-rewrite-51", "reason": "capped or not reached"}
- unfinished: {"case": "other-chitchat-85", "reason": "capped or not reached"}
