# Eval run

- when: 2026-09-19T07:11:05+00:00
- jev model: jev-1.13.0
- repeats per case: 3
- live Jev calls this run: 688
- cases: 86 (tune 62, held-out 24)

## all cases

| variant | n | task acc % | difficulty % | faith % | stable % | tier ok % | under % | over % | effort±1 % | cost |
|---|---|---|---|---|---|---|---|---|---|---|
| before_summary_v1 | 86 | 68.6 | 62.8 | 76.7 | 97.7 | 68.6 | 2.3 | 29.1 | 88.9 | 13.5 |
| before_last_message_only | 86 | 66.3 | 61.6 | 79.1 | 97.7 | 69.8 | 2.3 | 27.9 | 88.9 | 13.3 |
| before_tail_v1 | 86 | 72.1 | 60.5 | 79.1 | 97.7 | 70.9 | 1.2 | 27.9 | 86.7 | 14.0 |
| router_yaml | 86 | 81.4 | 86.0 | 75.6 | 96.5 | 90.7 | 1.2 | 8.1 | 93.3 | 9.1 |
| baseline:always_astra_medium | 86 | - | - | - | - | 59.3 | 0.0 | 40.7 | 91.1 | 14.0 |
| baseline:always_glm | 86 | - | - | - | - | 54.7 | 45.3 | 0.0 | 6.7 | 1.0 |
| baseline:rules | 86 | 18.6 | 36.0 | 66.3 | - | 62.8 | 23.3 | 14.0 | 37.8 | 6.2 |

## tuning split

| variant | n | task acc % | difficulty % | faith % | stable % | tier ok % | under % | over % | effort±1 % | cost |
|---|---|---|---|---|---|---|---|---|---|---|
| before_summary_v1 | 62 | 69.4 | 59.7 | 80.6 | 96.8 | 64.5 | 1.6 | 33.9 | 93.1 | 12.9 |
| before_last_message_only | 62 | 67.7 | 56.5 | 83.9 | 96.8 | 64.5 | 1.6 | 33.9 | 93.1 | 12.8 |
| before_tail_v1 | 62 | 75.8 | 56.5 | 83.9 | 96.8 | 67.7 | 0.0 | 32.3 | 89.7 | 13.6 |
| router_yaml | 62 | 82.3 | 83.9 | 79.0 | 96.8 | 91.9 | 0.0 | 8.1 | 93.1 | 8.2 |
| baseline:always_astra_medium | 62 | - | - | - | - | 53.2 | 0.0 | 46.8 | 89.7 | 14.0 |
| baseline:always_glm | 62 | - | - | - | - | 56.5 | 43.5 | 0.0 | 6.9 | 1.0 |
| baseline:rules | 62 | 19.4 | 33.9 | 67.7 | - | 64.5 | 21.0 | 14.5 | 37.9 | 6.0 |

## held-out split

| variant | n | task acc % | difficulty % | faith % | stable % | tier ok % | under % | over % | effort±1 % | cost |
|---|---|---|---|---|---|---|---|---|---|---|
| before_summary_v1 | 24 | 66.7 | 70.8 | 66.7 | 100.0 | 79.2 | 4.2 | 16.7 | 81.2 | 15.2 |
| before_last_message_only | 24 | 62.5 | 75.0 | 66.7 | 100.0 | 83.3 | 4.2 | 12.5 | 81.2 | 14.6 |
| before_tail_v1 | 24 | 62.5 | 70.8 | 66.7 | 100.0 | 79.2 | 4.2 | 16.7 | 81.2 | 15.2 |
| router_yaml | 24 | 79.2 | 91.7 | 66.7 | 95.8 | 87.5 | 4.2 | 8.3 | 93.8 | 11.6 |
| baseline:always_astra_medium | 24 | - | - | - | - | 75.0 | 0.0 | 25.0 | 93.8 | 14.0 |
| baseline:always_glm | 24 | - | - | - | - | 50.0 | 50.0 | 0.0 | 6.2 | 1.0 |
| baseline:rules | 24 | 16.7 | 41.7 | 62.5 | - | 58.3 | 29.2 | 12.5 | 37.5 | 6.8 |

## Targets from TESTPLAN.md

| metric | target | best variant | value | met |
|---|---|---|---|---|
| task acc % | 85 | router_yaml | 81.4 | NO |
| difficulty % | 80 | router_yaml | 86.0 | yes |
| faith % | 85 | before_last_message_only | 79.1 | NO |
| stable % | 95 | before_summary_v1 | 97.7 | yes |
| tier ok % | 90 | router_yaml | 90.7 | yes |
| effort±1 % | 80 | router_yaml | 93.3 | yes |

## before_summary_v1

- state builder: `summary_v1`, questions: task, difficulty, harm_if_wrong, task space: seven
- median state tokens: 201.0 (Jev counted 1025.5)
- Jev latency p50 118.9 ms, p95 192.7 ms
- injection traps held: 1/2; all trap cases tier-correct: 62.5%

### before_summary_v1: by client shape

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| bare | 22 | 68.2 | 50.0 | 59.1 |
| chat-ui | 30 | 76.7 | 73.3 | 73.3 |
| pipeline | 13 | 61.5 | 61.5 | 76.9 |
| terminal-agent | 21 | 61.9 | 61.9 | 66.7 |

### before_summary_v1: by language

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| de | 2 | 100.0 | 50.0 | 50.0 |
| en | 80 | 66.2 | 61.2 | 68.8 |
| es | 2 | 100.0 | 100.0 | 100.0 |
| zh | 2 | 100.0 | 100.0 | 50.0 |

### before_summary_v1: multi-turn

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| multi-turn | 6 | 16.7 | 33.3 | 66.7 |
| single-turn | 80 | 72.5 | 65.0 | 68.8 |

### before_summary_v1: task confusion (rows = label, columns = Jev)

| label | agentic_tool_task | analysis | code | debugging | other | quick_question | writing |
|---|---|---|---|---|---|---|---|
| agentic_tool_task | 5 |  |  | 2 | 1 |  |  |
| analysis |  | 5 | 3 |  |  |  |  |
| code |  |  | 9 |  | 1 |  | 1 |
| debugging | 1 |  |  | 7 |  | 1 |  |
| other |  | 6 |  |  | 1 | 1 | 1 |
| quick_question |  | 1 | 1 | 1 |  | 5 |  |
| writing |  | 3 |  |  | 2 | 1 | 27 |

### before_summary_v1: task confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 5 | 0.42 | 0.0 |
| 0.50-0.70 | 11 | 0.60 | 54.5 |
| 0.70-0.80 | 3 | 0.76 | 33.3 |
| 0.80-0.90 | 6 | 0.85 | 66.7 |
| 0.90-0.95 | 8 | 0.93 | 25.0 |
| 0.95-1.01 | 53 | 1.00 | 86.8 |

### before_summary_v1: difficulty confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 6 | 0.31 | 33.3 |
| 0.50-0.70 | 31 | 0.60 | 64.5 |
| 0.70-0.80 | 10 | 0.75 | 50.0 |
| 0.80-0.90 | 16 | 0.85 | 62.5 |
| 0.90-0.95 | 10 | 0.93 | 80.0 |
| 0.95-1.01 | 13 | 0.98 | 69.2 |

### before_summary_v1: cases that failed

| case | split | label task/diff/tier | Jev task/diff/harm | route | why |
|---|---|---|---|---|---|
| agent-bisect-regression-35 | tune | agentic-tool-task/3±0/frontier | debugging/2.99/0.51 | gpt-6-astra(xhigh) via very_hard | task |
| agent-changelog-from-git-log-36 | tune | agentic-tool-task/1±1/fast | agentic_tool_task/2.47/0.14 | gpt-6-astra(high) via hard | over-routed |
| agent-delete-orig-files-33 | tune | agentic-tool-task/0±0/fast | agentic_tool_task/1.17/0.63 | gpt-6-astra(medium) via high_harm | over-routed, difficulty |
| agent-monorepo-dep-bump-32 | tune | agentic-tool-task/3±0/frontier | other/1.59/0.6 | gpt-6-astra(medium) via low_confidence | task, difficulty |
| agent-run-tests-and-fix-29 | tune | agentic-tool-task/3±0/frontier | debugging/2.94/0.51 | gpt-6-astra(medium) via low_confidence | task |
| code-chinese-hooks-08 | tune | code-edit/1±0/fast | code/1.29/0.43 | gpt-6-astra(medium) via coding_middle | over-routed |
| code-multiturn-yes-do-it-07 | tune | code-edit/2±0/frontier | other/1.4/0.62 | gpt-6-astra(medium) via low_confidence | task, difficulty |
| code-regex-extract-04 | tune | code-edit/1±0/fast | code/1.13/0.53 | gpt-6-astra(medium) via coding_middle | over-routed |
| code-rename-trivial-01 | tune | code-edit/0±0/fast | code/1.11/0.55 | gpt-6-astra(medium) via coding_middle | over-routed, difficulty |
| code-shell-oneliner-06 | tune | code-edit/0±0/fast | code/1.0/0.14 | gpt-6-astra(medium) via coding_middle | over-routed, difficulty |
| creative-limerick-printer-45 | tune | creative-prose/0±0/fast | writing/1.03/0.02 | gpt-6-astra(low) via moderate | over-routed, difficulty |
| creative-novel-continuity-47 | tune | creative-prose/2±0/frontier | other/0.87/0.14 | glm-5.3-flash(low) via easy | under-routed, task, difficulty |
| creative-npc-dialogue-48 | held_out | creative-prose/1±0/fast | writing/1.45/0.03 | gpt-6-astra(low) via moderate | over-routed |
| creative-parody-lyrics-50 | tune | creative-prose/1±0/fast | writing/1.26/0.03 | gpt-6-astra(low) via moderate | over-routed |
| debug-css-overflow-17 | tune | debugging/1±0/fast | debugging/1.02/0.34 | gpt-6-astra(medium) via coding_middle | over-routed |
| debug-long-log-trivial-question-12 | tune | debugging/0±0/fast | quick_question/0.12/0.26 | glm-5.3-flash(none) via trivial | task |
| debug-multiturn-tool-result-15 | held_out | debugging/2±1/frontier | agentic_tool_task/2.94/0.91 | gpt-6-astra(xhigh) via very_hard | task |
| design-auth-module-review-23 | tune | design-or-review/3±0/frontier | code/2.24/0.87 | gpt-6-astra(high) via hard | task, difficulty |
| design-latest-turn-changes-task-28 | held_out | code-edit/0±0/fast | writing/1.0/0.09 | gpt-6-astra(medium) via low_confidence | over-routed, task, difficulty |
| design-pr-review-small-22 | tune | design-or-review/0±1/fast | code/0.54/0.33 | glm-5.3-flash(low) via easy_small_code | task |
| design-security-snippet-21 | tune | design-or-review/2±1/frontier | code/1.51/0.87 | gpt-6-astra(medium) via high_harm | task |
| extract-action-items-65 | tune | summarise-or-extract/1±0/fast | writing/1.21/0.29 | gpt-6-astra(low) via moderate | over-routed |
| extract-invoice-json-62 | tune | summarise-or-extract/1±0/fast | other/1.57/0.77 | gpt-6-astra(medium) via high_harm | task, difficulty |
| other-buy-vs-rent-84 | held_out | other/3±0/frontier | analysis/2.03/0.9 | gpt-6-astra(high) via hard | task, difficulty |
| other-chart-image-read-86 | tune | other/2±1/frontier | analysis/1.56/0.57 | gpt-6-astra(medium) via images_need_vision | task |
| other-explain-to-child-83 | tune | other/1±0/fast | quick_question/1.16/0.05 | gpt-6-astra(low) via moderate | over-routed, task |
| other-gym-split-81 | tune | other/1±0/fast | analysis/1.91/0.3 | gpt-6-astra(high) via hard | over-routed, task, difficulty |
| other-probability-puzzle-79 | tune | other/3±1/frontier | analysis/1.85/0.12 | gpt-6-astra(high) via hard | task |
| other-simple-question-proof-78 | held_out | other/3±1/frontier | analysis/1.37/0.07 | gpt-6-astra(low) via moderate | task, difficulty |
| other-translate-manual-82 | tune | other/2±0/frontier | writing/1.24/0.86 | gpt-6-astra(medium) via high_harm | task, difficulty |
| other-trip-itinerary-80 | held_out | other/1±1/fast | analysis/2.0/0.15 | gpt-6-astra(high) via hard | task |
| polish-commit-message-56 | tune | everyday-polish/0±0/fast | writing/1.11/0.11 | gpt-6-astra(medium) via tool_loop | over-routed, difficulty |
| polish-defuse-email-57 | held_out | everyday-polish/1±0/fast | writing/1.02/0.05 | gpt-6-astra(low) via moderate | over-routed |
| polish-docstring-tidy-53 | tune | everyday-polish/0±1/fast | writing/1.37/0.1 | gpt-6-astra(medium) via tool_loop | over-routed |
| polish-product-blurb-55 | tune | everyday-polish/1±0/fast | writing/1.05/0.06 | gpt-6-astra(low) via moderate | over-routed |
| polish-readme-intro-58 | tune | everyday-polish/1±0/fast | writing/1.11/0.06 | gpt-6-astra(low) via moderate | over-routed |
| quick-bash-for-loop-70 | tune | quick-question/0±0/fast | code/0.67/0.39 | glm-5.3-flash(low) via easy_small_code | task, difficulty |
| quick-borrow-checker-misleading-75 | held_out | quick-question/2±1/frontier | debugging/0.95/0.26 | glm-5.3-flash(low) via easy_small_code | under-routed, task |
| quick-drug-interaction-73 | tune | quick-question/1±1/fast | quick_question/0.99/0.93 | gpt-6-astra(medium) via high_harm | over-routed |
| quick-german-idempotent-76 | tune | quick-question/1±0/fast | quick_question/1.0/0.45 | gpt-6-astra(low) via moderate | over-routed |
| quick-weakmap-vs-map-72 | tune | quick-question/1±0/fast | analysis/1.48/0.42 | gpt-6-astra(low) via moderate | over-routed, task |
| summarise-adversarial-doc-69 | tune | summarise-or-extract/2±1/fast | writing/1.68/0.76 | gpt-6-astra(medium) via high_harm | over-routed |
| summarise-blogpost-tldr-63 | tune | summarise-or-extract/0±0/fast | writing/1.08/0.12 | gpt-6-astra(low) via moderate | over-routed, difficulty |
| summarise-diff-release-notes-68 | held_out | summarise-or-extract/1±0/fast | writing/1.15/0.15 | gpt-6-astra(medium) via tool_loop | over-routed |
| summarise-ticket-export-themes-64 | held_out | summarise-or-extract/2±0/frontier | analysis/2.38/0.26 | gpt-6-astra(high) via hard | task |
| writing-legal-clause-39 | tune | high-stakes-writing/3±0/frontier | analysis/1.8/0.92 | gpt-6-astra(high) via hard | task, difficulty |
| writing-medical-discharge-38 | held_out | high-stakes-writing/2±1/frontier | quick_question/1.66/0.94 | gpt-6-astra(medium) via low_confidence | task |
| writing-rsu-tax-explainer-40 | tune | high-stakes-writing/3±0/frontier | analysis/2.29/0.95 | gpt-6-astra(high) via hard | task, difficulty |

## before_last_message_only

- state builder: `last_message_only`, questions: task, difficulty, harm_if_wrong, task space: seven
- median state tokens: 83.0 (Jev counted 893.5)
- Jev latency p50 123.9 ms, p95 238.7 ms
- injection traps held: 1/2; all trap cases tier-correct: 50.0%

### before_last_message_only: by client shape

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| bare | 22 | 68.2 | 54.5 | 63.6 |
| chat-ui | 30 | 76.7 | 70.0 | 76.7 |
| pipeline | 13 | 61.5 | 61.5 | 69.2 |
| terminal-agent | 21 | 52.4 | 57.1 | 66.7 |

### before_last_message_only: by language

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| de | 2 | 100.0 | 50.0 | 50.0 |
| en | 80 | 63.8 | 60.0 | 70.0 |
| es | 2 | 100.0 | 100.0 | 100.0 |
| zh | 2 | 100.0 | 100.0 | 50.0 |

### before_last_message_only: multi-turn

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| multi-turn | 6 | 0.0 | 33.3 | 83.3 |
| single-turn | 80 | 71.2 | 63.8 | 68.8 |

### before_last_message_only: task confusion (rows = label, columns = Jev)

| label | agentic_tool_task | analysis | code | debugging | other | quick_question | writing |
|---|---|---|---|---|---|---|---|
| agentic_tool_task | 4 |  | 1 | 2 | 1 |  |  |
| analysis |  | 5 | 3 |  |  |  |  |
| code |  |  | 9 |  | 1 |  | 1 |
| debugging | 1 |  |  | 7 |  | 1 |  |
| other |  | 6 |  |  | 1 | 1 | 1 |
| quick_question |  | 1 | 1 | 1 |  | 5 |  |
| writing |  | 3 | 1 |  | 2 | 1 | 26 |

### before_last_message_only: task confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 3 | 0.44 | 0.0 |
| 0.50-0.70 | 13 | 0.59 | 30.8 |
| 0.70-0.80 | 4 | 0.75 | 50.0 |
| 0.80-0.90 | 5 | 0.87 | 40.0 |
| 0.90-0.95 | 1 | 0.94 | 0.0 |
| 0.95-1.01 | 60 | 0.99 | 81.7 |

### before_last_message_only: difficulty confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 4 | 0.27 | 25.0 |
| 0.50-0.70 | 37 | 0.60 | 54.1 |
| 0.70-0.80 | 10 | 0.76 | 80.0 |
| 0.80-0.90 | 16 | 0.86 | 75.0 |
| 0.90-0.95 | 7 | 0.93 | 57.1 |
| 0.95-1.01 | 12 | 0.98 | 66.7 |

### before_last_message_only: cases that failed

| case | split | label task/diff/tier | Jev task/diff/harm | route | why |
|---|---|---|---|---|---|
| agent-bisect-regression-35 | tune | agentic-tool-task/3±0/frontier | debugging/2.99/0.53 | gpt-6-astra(xhigh) via very_hard | task |
| agent-changelog-from-git-log-36 | tune | agentic-tool-task/1±1/fast | agentic_tool_task/2.36/0.11 | gpt-6-astra(medium) via low_confidence | over-routed |
| agent-delete-orig-files-33 | tune | agentic-tool-task/0±0/fast | agentic_tool_task/1.1/0.7 | gpt-6-astra(medium) via high_harm | over-routed, difficulty |
| agent-monorepo-dep-bump-32 | tune | agentic-tool-task/3±0/frontier | other/1.02/0.28 | gpt-6-astra(medium) via low_confidence | task, difficulty |
| agent-multiturn-grep-result-31 | held_out | agentic-tool-task/2±1/frontier | code/2.87/0.88 | gpt-6-astra(xhigh) via very_hard | task |
| agent-run-tests-and-fix-29 | tune | agentic-tool-task/3±0/frontier | debugging/2.93/0.55 | gpt-6-astra(xhigh) via very_hard | task |
| code-chinese-hooks-08 | tune | code-edit/1±0/fast | code/1.33/0.46 | gpt-6-astra(medium) via coding_middle | over-routed |
| code-multiturn-yes-do-it-07 | tune | code-edit/2±0/frontier | other/0.5/0.39 | glm-5.3-flash(none) via trivial | under-routed, task, difficulty |
| code-regex-extract-04 | tune | code-edit/1±0/fast | code/1.13/0.58 | gpt-6-astra(medium) via coding_middle | over-routed |
| code-rename-trivial-01 | tune | code-edit/0±0/fast | code/1.02/0.62 | gpt-6-astra(medium) via high_harm | over-routed, difficulty |
| creative-limerick-printer-45 | tune | creative-prose/0±0/fast | writing/1.04/0.02 | gpt-6-astra(low) via moderate | over-routed, difficulty |
| creative-novel-continuity-47 | tune | creative-prose/2±0/frontier | other/1.06/0.17 | gpt-6-astra(medium) via low_confidence | task, difficulty |
| creative-npc-dialogue-48 | held_out | creative-prose/1±0/fast | writing/1.41/0.03 | gpt-6-astra(low) via moderate | over-routed |
| creative-parody-lyrics-50 | tune | creative-prose/1±0/fast | writing/1.28/0.03 | gpt-6-astra(low) via moderate | over-routed |
| debug-css-overflow-17 | tune | debugging/1±0/fast | debugging/1.03/0.35 | gpt-6-astra(medium) via coding_middle | over-routed |
| debug-long-log-trivial-question-12 | tune | debugging/0±0/fast | quick_question/0.14/0.3 | gpt-6-astra(medium) via low_confidence | over-routed, task |
| debug-multiturn-tool-result-15 | held_out | debugging/2±1/frontier | agentic_tool_task/2.89/0.92 | gpt-6-astra(xhigh) via very_hard | task |
| design-auth-module-review-23 | tune | design-or-review/3±0/frontier | code/2.3/0.87 | gpt-6-astra(high) via hard | task, difficulty |
| design-latest-turn-changes-task-28 | held_out | code-edit/0±0/fast | writing/0.96/0.1 | glm-5.3-flash(low) via easy | task, difficulty |
| design-pr-review-small-22 | tune | design-or-review/0±1/fast | code/0.55/0.27 | glm-5.3-flash(low) via easy_small_code | task |
| design-security-snippet-21 | tune | design-or-review/2±1/frontier | code/1.55/0.88 | gpt-6-astra(medium) via high_harm | task |
| extract-action-items-65 | tune | summarise-or-extract/1±0/fast | writing/1.19/0.33 | gpt-6-astra(low) via moderate | over-routed |
| extract-invoice-json-62 | tune | summarise-or-extract/1±0/fast | other/1.59/0.82 | gpt-6-astra(medium) via high_harm | task, difficulty |
| other-buy-vs-rent-84 | held_out | other/3±0/frontier | analysis/2.04/0.89 | gpt-6-astra(high) via hard | task, difficulty |
| other-chart-image-read-86 | tune | other/2±1/frontier | analysis/1.56/0.58 | gpt-6-astra(medium) via images_need_vision | task |
| other-explain-to-child-83 | tune | other/1±0/fast | quick_question/1.21/0.05 | gpt-6-astra(low) via moderate | over-routed, task |
| other-gym-split-81 | tune | other/1±0/fast | analysis/1.93/0.3 | gpt-6-astra(high) via hard | over-routed, task, difficulty |
| other-probability-puzzle-79 | tune | other/3±1/frontier | analysis/1.86/0.13 | gpt-6-astra(high) via hard | task |
| other-simple-question-proof-78 | held_out | other/3±1/frontier | analysis/1.37/0.07 | gpt-6-astra(low) via moderate | task, difficulty |
| other-translate-manual-82 | tune | other/2±0/frontier | writing/1.31/0.86 | gpt-6-astra(medium) via high_harm | task, difficulty |
| other-trip-itinerary-80 | held_out | other/1±1/fast | analysis/2.02/0.14 | gpt-6-astra(high) via hard | task |
| polish-commit-message-56 | tune | everyday-polish/0±0/fast | writing/1.31/0.09 | gpt-6-astra(medium) via tool_loop | over-routed, difficulty |
| polish-defuse-email-57 | held_out | everyday-polish/1±0/fast | writing/1.02/0.06 | gpt-6-astra(low) via moderate | over-routed |
| polish-docstring-tidy-53 | tune | everyday-polish/0±1/fast | code/1.3/0.07 | gpt-6-astra(medium) via coding_middle | over-routed, task |
| polish-product-blurb-55 | tune | everyday-polish/1±0/fast | writing/1.06/0.08 | gpt-6-astra(low) via moderate | over-routed |
| polish-readme-intro-58 | tune | everyday-polish/1±0/fast | writing/1.11/0.08 | gpt-6-astra(low) via moderate | over-routed |
| quick-bash-for-loop-70 | tune | quick-question/0±0/fast | code/0.53/0.4 | glm-5.3-flash(low) via easy_small_code | task, difficulty |
| quick-borrow-checker-misleading-75 | held_out | quick-question/2±1/frontier | debugging/0.9/0.24 | glm-5.3-flash(low) via easy_small_code | under-routed, task |
| quick-drug-interaction-73 | tune | quick-question/1±1/fast | quick_question/1.03/0.91 | gpt-6-astra(medium) via high_harm | over-routed |
| quick-german-idempotent-76 | tune | quick-question/1±0/fast | quick_question/1.05/0.57 | gpt-6-astra(low) via moderate | over-routed |
| quick-weakmap-vs-map-72 | tune | quick-question/1±0/fast | analysis/1.58/0.48 | gpt-6-astra(low) via moderate | over-routed, task, difficulty |
| summarise-adversarial-doc-69 | tune | summarise-or-extract/2±1/fast | writing/1.7/0.81 | gpt-6-astra(medium) via high_harm | over-routed |
| summarise-blogpost-tldr-63 | tune | summarise-or-extract/0±0/fast | writing/1.05/0.18 | gpt-6-astra(low) via moderate | over-routed, difficulty |
| summarise-diff-release-notes-68 | held_out | summarise-or-extract/1±0/fast | writing/1.23/0.18 | gpt-6-astra(medium) via tool_loop | over-routed |
| summarise-ticket-export-themes-64 | held_out | summarise-or-extract/2±0/frontier | analysis/2.37/0.3 | gpt-6-astra(high) via hard | task |
| writing-legal-clause-39 | tune | high-stakes-writing/3±0/frontier | analysis/1.84/0.92 | gpt-6-astra(high) via hard | task, difficulty |
| writing-medical-discharge-38 | held_out | high-stakes-writing/2±1/frontier | quick_question/1.67/0.93 | gpt-6-astra(medium) via low_confidence | task |
| writing-rsu-tax-explainer-40 | tune | high-stakes-writing/3±0/frontier | analysis/2.4/0.95 | gpt-6-astra(high) via hard | task, difficulty |

## before_tail_v1

- state builder: `tail_v1`, questions: task, difficulty, harm_if_wrong, task space: seven
- median state tokens: 211.5 (Jev counted 1052.5)
- Jev latency p50 132.9 ms, p95 266.2 ms
- injection traps held: 1/2; all trap cases tier-correct: 62.5%

### before_tail_v1: by client shape

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| bare | 22 | 68.2 | 50.0 | 63.6 |
| chat-ui | 30 | 80.0 | 70.0 | 76.7 |
| pipeline | 13 | 69.2 | 53.8 | 76.9 |
| terminal-agent | 21 | 66.7 | 61.9 | 66.7 |

### before_tail_v1: by language

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| de | 2 | 100.0 | 50.0 | 100.0 |
| en | 80 | 70.0 | 58.8 | 70.0 |
| es | 2 | 100.0 | 100.0 | 100.0 |
| zh | 2 | 100.0 | 100.0 | 50.0 |

### before_tail_v1: multi-turn

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| multi-turn | 6 | 50.0 | 50.0 | 83.3 |
| single-turn | 80 | 73.8 | 61.2 | 70.0 |

### before_tail_v1: task confusion (rows = label, columns = Jev)

| label | agentic_tool_task | analysis | code | debugging | other | quick_question | writing |
|---|---|---|---|---|---|---|---|
| agentic_tool_task | 6 |  | 1 | 1 |  |  |  |
| analysis |  | 5 | 3 |  |  |  |  |
| code |  |  | 10 |  |  |  | 1 |
| debugging | 1 |  |  | 8 |  |  |  |
| other |  | 6 |  |  | 1 | 1 | 1 |
| quick_question |  | 1 | 1 | 1 |  | 5 |  |
| writing |  | 3 | 1 |  | 1 | 1 | 27 |

### before_tail_v1: task confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 4 | 0.42 | 0.0 |
| 0.50-0.70 | 7 | 0.62 | 71.4 |
| 0.70-0.80 | 5 | 0.75 | 40.0 |
| 0.80-0.90 | 7 | 0.83 | 71.4 |
| 0.90-0.95 | 6 | 0.93 | 50.0 |
| 0.95-1.01 | 57 | 0.99 | 82.5 |

### before_tail_v1: difficulty confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 1 | 0.49 | 100.0 |
| 0.50-0.70 | 35 | 0.61 | 51.4 |
| 0.70-0.80 | 9 | 0.74 | 44.4 |
| 0.80-0.90 | 16 | 0.83 | 68.8 |
| 0.90-0.95 | 8 | 0.92 | 75.0 |
| 0.95-1.01 | 17 | 0.97 | 70.6 |

### before_tail_v1: cases that failed

| case | split | label task/diff/tier | Jev task/diff/harm | route | why |
|---|---|---|---|---|---|
| agent-bisect-regression-35 | tune | agentic-tool-task/3±0/frontier | debugging/2.98/0.5 | gpt-6-astra(xhigh) via very_hard | task |
| agent-changelog-from-git-log-36 | tune | agentic-tool-task/1±1/fast | agentic_tool_task/2.58/0.15 | gpt-6-astra(high) via hard | over-routed, difficulty |
| agent-delete-orig-files-33 | tune | agentic-tool-task/0±0/fast | agentic_tool_task/1.29/0.63 | gpt-6-astra(medium) via high_harm | over-routed, difficulty |
| agent-multiturn-grep-result-31 | held_out | agentic-tool-task/2±1/frontier | code/2.95/0.83 | gpt-6-astra(xhigh) via very_hard | task |
| code-chinese-hooks-08 | tune | code-edit/1±0/fast | code/1.23/0.44 | gpt-6-astra(medium) via coding_middle | over-routed |
| code-regex-extract-04 | tune | code-edit/1±0/fast | code/1.14/0.48 | gpt-6-astra(medium) via coding_middle | over-routed |
| code-rename-trivial-01 | tune | code-edit/0±0/fast | code/1.1/0.53 | gpt-6-astra(medium) via coding_middle | over-routed, difficulty |
| code-shell-oneliner-06 | tune | code-edit/0±0/fast | code/1.0/0.18 | gpt-6-astra(medium) via coding_middle | over-routed, difficulty |
| creative-limerick-printer-45 | tune | creative-prose/0±0/fast | writing/1.02/0.02 | gpt-6-astra(low) via moderate | over-routed, difficulty |
| creative-npc-dialogue-48 | held_out | creative-prose/1±0/fast | writing/1.41/0.03 | gpt-6-astra(low) via moderate | over-routed |
| creative-parody-lyrics-50 | tune | creative-prose/1±0/fast | writing/1.26/0.03 | gpt-6-astra(low) via moderate | over-routed |
| debug-css-overflow-17 | tune | debugging/1±0/fast | debugging/1.0/0.33 | gpt-6-astra(medium) via coding_middle | over-routed |
| debug-multiturn-tool-result-15 | held_out | debugging/2±1/frontier | agentic_tool_task/2.8/0.89 | gpt-6-astra(xhigh) via very_hard | task |
| design-auth-module-review-23 | tune | design-or-review/3±0/frontier | code/2.35/0.85 | gpt-6-astra(high) via hard | task, difficulty |
| design-latest-turn-changes-task-28 | held_out | code-edit/0±0/fast | writing/1.01/0.32 | gpt-6-astra(medium) via low_confidence | over-routed, task, difficulty |
| design-pr-review-small-22 | tune | design-or-review/0±1/fast | code/0.67/0.33 | glm-5.3-flash(low) via easy_small_code | task |
| design-security-snippet-21 | tune | design-or-review/2±1/frontier | code/1.55/0.86 | gpt-6-astra(medium) via high_harm | task |
| extract-action-items-65 | tune | summarise-or-extract/1±0/fast | writing/1.18/0.3 | gpt-6-astra(low) via moderate | over-routed |
| extract-invoice-json-62 | tune | summarise-or-extract/1±0/fast | other/1.55/0.77 | gpt-6-astra(medium) via high_harm | task, difficulty |
| other-buy-vs-rent-84 | held_out | other/3±0/frontier | analysis/2.04/0.89 | gpt-6-astra(high) via hard | task, difficulty |
| other-chart-image-read-86 | tune | other/2±1/frontier | analysis/1.41/0.6 | gpt-6-astra(medium) via images_need_vision | task |
| other-explain-to-child-83 | tune | other/1±0/fast | quick_question/1.17/0.04 | gpt-6-astra(low) via moderate | over-routed, task |
| other-gym-split-81 | tune | other/1±0/fast | analysis/1.93/0.31 | gpt-6-astra(high) via hard | over-routed, task, difficulty |
| other-probability-puzzle-79 | tune | other/3±1/frontier | analysis/1.84/0.12 | gpt-6-astra(high) via hard | task |
| other-simple-question-proof-78 | held_out | other/3±1/frontier | analysis/1.31/0.06 | gpt-6-astra(low) via moderate | task, difficulty |
| other-translate-manual-82 | tune | other/2±0/frontier | writing/1.3/0.85 | gpt-6-astra(medium) via high_harm | task, difficulty |
| other-trip-itinerary-80 | held_out | other/1±1/fast | analysis/1.99/0.15 | gpt-6-astra(high) via hard | task |
| polish-commit-message-56 | tune | everyday-polish/0±0/fast | writing/1.11/0.11 | gpt-6-astra(medium) via tool_loop | over-routed, difficulty |
| polish-defuse-email-57 | held_out | everyday-polish/1±0/fast | writing/1.02/0.06 | gpt-6-astra(low) via moderate | over-routed |
| polish-docstring-tidy-53 | tune | everyday-polish/0±1/fast | code/1.37/0.11 | gpt-6-astra(medium) via low_confidence | over-routed, task |
| polish-product-blurb-55 | tune | everyday-polish/1±0/fast | writing/1.05/0.07 | gpt-6-astra(low) via moderate | over-routed |
| polish-readme-intro-58 | tune | everyday-polish/1±0/fast | writing/1.08/0.06 | gpt-6-astra(low) via moderate | over-routed |
| quick-bash-for-loop-70 | tune | quick-question/0±0/fast | code/0.65/0.4 | glm-5.3-flash(low) via easy_small_code | task, difficulty |
| quick-borrow-checker-misleading-75 | held_out | quick-question/2±1/frontier | debugging/0.96/0.26 | glm-5.3-flash(low) via easy_small_code | under-routed, task |
| quick-drug-interaction-73 | tune | quick-question/1±1/fast | quick_question/0.92/0.93 | gpt-6-astra(medium) via high_harm | over-routed |
| quick-weakmap-vs-map-72 | tune | quick-question/1±0/fast | analysis/1.53/0.45 | gpt-6-astra(medium) via low_confidence | over-routed, task, difficulty |
| summarise-adversarial-doc-69 | tune | summarise-or-extract/2±1/fast | writing/1.76/0.75 | gpt-6-astra(medium) via high_harm | over-routed |
| summarise-blogpost-tldr-63 | tune | summarise-or-extract/0±0/fast | writing/1.02/0.18 | gpt-6-astra(low) via moderate | over-routed, difficulty |
| summarise-diff-release-notes-68 | held_out | summarise-or-extract/1±0/fast | writing/1.14/0.18 | gpt-6-astra(medium) via tool_loop | over-routed |
| summarise-ticket-export-themes-64 | held_out | summarise-or-extract/2±0/frontier | analysis/2.3/0.27 | gpt-6-astra(high) via hard | task |
| writing-legal-clause-39 | tune | high-stakes-writing/3±0/frontier | analysis/1.8/0.91 | gpt-6-astra(high) via hard | task, difficulty |
| writing-medical-discharge-38 | held_out | high-stakes-writing/2±1/frontier | quick_question/1.65/0.94 | gpt-6-astra(medium) via high_harm | task |
| writing-rsu-tax-explainer-40 | tune | high-stakes-writing/3±0/frontier | analysis/2.27/0.96 | gpt-6-astra(high) via hard | task, difficulty |

## router_yaml

- state builder: `continuation_aware_v1`, questions: task, difficulty, harm_if_wrong, task space: ten
- median state tokens: 122.5 (Jev counted 1589.5)
- Jev latency p50 142.2 ms, p95 261.7 ms
- injection traps held: 1/2; all trap cases tier-correct: 62.5%

### router_yaml: by client shape

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| bare | 22 | 72.7 | 81.8 | 86.4 |
| chat-ui | 30 | 86.7 | 93.3 | 100.0 |
| pipeline | 13 | 84.6 | 76.9 | 84.6 |
| terminal-agent | 21 | 81.0 | 85.7 | 85.7 |

### router_yaml: by language

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| de | 2 | 100.0 | 50.0 | 100.0 |
| en | 80 | 80.0 | 86.2 | 90.0 |
| es | 2 | 100.0 | 100.0 | 100.0 |
| zh | 2 | 100.0 | 100.0 | 100.0 |

### router_yaml: multi-turn

| group | n | task acc % | difficulty % | tier ok % |
|---|---|---|---|---|
| multi-turn | 6 | 66.7 | 66.7 | 83.3 |
| single-turn | 80 | 82.5 | 87.5 | 91.2 |

### router_yaml: task confusion (rows = label, columns = Jev)

| label | agentic-tool-task | code-edit | creative-prose | debugging | design-or-review | everyday-polish | high-stakes-writing | other | quick-question | summarise-or-extract |
|---|---|---|---|---|---|---|---|---|---|---|
| agentic-tool-task | 6 |  |  | 1 | 1 |  |  |  |  |  |
| code-edit |  | 11 |  |  |  |  |  |  |  |  |
| creative-prose |  |  | 8 |  |  |  |  |  |  |  |
| debugging | 1 |  |  | 7 |  |  |  |  |  | 1 |
| design-or-review |  |  |  |  | 8 |  |  |  |  |  |
| everyday-polish |  | 1 |  |  |  | 5 | 2 |  |  |  |
| high-stakes-writing |  |  |  |  | 1 |  | 5 | 1 | 1 |  |
| other |  |  |  |  |  |  | 1 | 6 | 1 | 1 |
| quick-question |  | 1 |  | 1 |  |  |  | 1 | 5 |  |
| summarise-or-extract |  |  |  |  |  |  |  |  |  | 9 |

### router_yaml: task confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 7 | 0.44 | 57.1 |
| 0.50-0.70 | 7 | 0.61 | 42.9 |
| 0.70-0.80 | 2 | 0.74 | 100.0 |
| 0.80-0.90 | 10 | 0.84 | 70.0 |
| 0.90-0.95 | 7 | 0.92 | 71.4 |
| 0.95-1.01 | 53 | 0.99 | 92.5 |

### router_yaml: difficulty confidence calibration

| confidence bucket | n | mean confidence | accuracy % |
|---|---|---|---|
| 0.00-0.50 | 6 | 0.37 | 50.0 |
| 0.50-0.70 | 25 | 0.60 | 88.0 |
| 0.70-0.80 | 15 | 0.75 | 93.3 |
| 0.80-0.90 | 19 | 0.85 | 78.9 |
| 0.90-0.95 | 6 | 0.92 | 100.0 |
| 0.95-1.01 | 15 | 0.98 | 93.3 |

### router_yaml: cases that failed

| case | split | label task/diff/tier | Jev task/diff/harm | route | why |
|---|---|---|---|---|---|
| agent-bisect-regression-35 | tune | agentic-tool-task/3±0/frontier | debugging/2.97/0.49 | gpt-6-astra(high) via hard | task |
| agent-changelog-from-git-log-36 | tune | agentic-tool-task/1±1/fast | agentic-tool-task/2.1/0.11 | gpt-6-astra(medium) via low_confidence | over-routed |
| agent-lookup-node-version-30 | held_out | agentic-tool-task/0±0/fast | agentic-tool-task/0.46/0.29 | gpt-6-astra(medium) via low_confidence | over-routed |
| agent-monorepo-dep-bump-32 | tune | agentic-tool-task/3±0/frontier | design-or-review/2.89/0.73 | gpt-6-astra(medium) via low_confidence | task |
| debug-long-log-trivial-question-12 | tune | debugging/0±0/fast | summarise-or-extract/0.12/0.28 | gpt-6-astra(medium) via low_confidence | over-routed, task |
| debug-multiturn-tool-result-15 | held_out | debugging/2±1/frontier | agentic-tool-task/2.53/0.9 | gpt-6-astra(medium) via low_confidence | task |
| design-latest-turn-changes-task-28 | held_out | code-edit/0±0/fast | code-edit/0.03/0.14 | gpt-6-astra(medium) via low_confidence | over-routed |
| other-chart-image-read-86 | tune | other/2±1/frontier | summarise-or-extract/1.88/0.58 | gpt-6-astra(medium) via images_need_vision | task |
| other-explain-to-child-83 | tune | other/1±0/fast | quick-question/1.07/0.04 | glm-5.3-flash(none) via easy | task |
| other-gym-split-81 | tune | other/1±0/fast | other/1.95/0.3 | gpt-6-astra(low) via moderate | over-routed, difficulty |
| other-translate-manual-82 | tune | other/2±0/frontier | high-stakes-writing/1.08/0.84 | gpt-6-astra(medium) via high_harm | task, difficulty |
| polish-docstring-tidy-53 | tune | everyday-polish/0±1/fast | code-edit/0.76/0.1 | glm-5.3-flash(none) via easy | task |
| polish-payment-error-copy-60 | held_out | everyday-polish/1±1/frontier | high-stakes-writing/1.72/0.53 | gpt-6-astra(low) via moderate | task |
| polish-translate-customer-reply-59 | tune | everyday-polish/2±1/frontier | high-stakes-writing/1.26/0.81 | gpt-6-astra(medium) via high_harm | task |
| quick-bash-for-loop-70 | tune | quick-question/0±0/fast | code-edit/0.01/0.38 | glm-5.3-flash(none) via easy | task |
| quick-borrow-checker-misleading-75 | held_out | quick-question/2±1/frontier | debugging/1.12/0.26 | glm-5.3-flash(none) via easy | under-routed, task |
| quick-drug-interaction-73 | tune | quick-question/1±1/fast | quick-question/0.89/0.92 | gpt-6-astra(medium) via low_confidence | over-routed |
| quick-percentage-74 | held_out | quick-question/0±0/fast | other/0.2/0.24 | glm-5.3-flash(none) via easy | task |
| summarise-adversarial-doc-69 | tune | summarise-or-extract/2±1/fast | summarise-or-extract/1.99/0.72 | gpt-6-astra(low) via moderate | over-routed |
| writing-legal-clause-39 | tune | high-stakes-writing/3±0/frontier | design-or-review/2.09/0.92 | gpt-6-astra(medium) via high_harm | task, difficulty |
| writing-medical-discharge-38 | held_out | high-stakes-writing/2±1/frontier | quick-question/1.7/0.93 | gpt-6-astra(medium) via low_confidence | task |
| writing-rsu-tax-explainer-40 | tune | high-stakes-writing/3±0/frontier | other/2.77/0.94 | gpt-6-astra(medium) via low_confidence | task |

