# Outcome benchmark (layer 3)

- when: 2026-09-19T22:17:17+00:00
- cases: 19
- routes per case: 7
- samples per route: 3
- candidate token cap: 8000
- a route counts as adequate when it scores within 0.5 of the best route
- where a programmatic check exists it decides the score; the judge still runs so the two can be compared

## Route averages

A route's score is the mean of its k samples. `spread` is the mean within-route standard deviation across samples: it is the noise floor, and a difference between two routes smaller than this is not a difference. `truncated` counts samples that stopped at the token cap of 8000 rather than finishing.

| route | mean score | 95% CI | spread | wins outright | adequate on | truncated % | median latency ms | median output tokens |
|---|---|---|---|---|---|---|---|---|
| ollama/glm-5.3-flash(none) | 9.59 | [9.12, 10.05] | 0.24 | 17/19 | 17/19 | 0.0 | 3835 | 407 |
| ollama/glm-5.3-flash(low) | 9.74 | [9.50, 9.98] | 0.11 | 17/19 | 18/19 | 0.0 | 3637 | 353 |
| ollama/glm-5.3-flash(high) | 9.34 | [8.85, 9.83] | 0.58 | 13/19 | 14/19 | 0.0 | 5760 | 642 |
| gpt-6-astra(low) | 9.37 | [8.88, 9.87] | 0.31 | 13/19 | 13/19 | 0.0 | 10753 | 301 |
| gpt-6-astra(medium) | 9.51 | [9.01, 10.00] | 0.29 | 15/19 | 17/19 | 0.0 | 10748 | 303 |
| gpt-6-astra(high) | 9.60 | [9.24, 9.95] | 0.34 | 13/19 | 16/19 | 0.0 | 12918 | 350 |
| gpt-6-astra(xhigh) | 9.31 | [8.61, 10.00] | 0.41 | 13/19 | 15/19 | 0.0 | 29257 | 918 |

## Winner stability

The cheapest adequate route per case, recomputed on bootstrap resamples of the k samples. A case whose tier flips in more than 20% of draws is marked `unstable` and is not allowed to override a hand label.

- mean tier flip rate: 0.070
- mean route flip rate: 0.087
- unstable cases: 2/19 (debug-long-log-trivial-question-12, debug-spanish-slow-query-18)

## Judge against the programmatic check

- samples with both: 147
- differ by 2 points or more: 38.8 [31.3, 46.8]
- disagree on whether the answer passes (7 of 10): 29.3 [22.5, 37.1]
- the judge is higher on: 17.7 [12.4, 24.7]
- mean signed difference, judge minus check: -1.90

Where the two disagree, the check is what the score used. The rate above is how much to discount the judge-only cases.

## Does effort change the answer?

| model | effort pair | mean score difference | cases where the higher effort was better by 0.5+ |
|---|---|---|---|
| glm-5.3-flash | none -> low | +0.15 | 2/19 |
| glm-5.3-flash | low -> high | -0.40 | 0/19 |
| gpt-6-astra | low -> medium | +0.13 | 3/19 |
| gpt-6-astra | medium -> high | +0.09 | 1/19 |
| gpt-6-astra | high -> xhigh | -0.29 | 0/19 |

## Cheapest adequate route per case, against the hand label

| case | label tier/effort | cheapest adequate | best score | tier flip | outcome | agrees |
|---|---|---|---|---|---|---|
| code-prose-shaped-really-code-09 | frontier/medium | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| code-regex-extract-04 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| code-sql-window-query-05 | frontier/medium | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |
| debug-adversarial-injection-in-log-16 | frontier/high | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |
| debug-css-overflow-17 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| debug-go-deadlock-short-40-11 | frontier/high | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |
| debug-long-log-trivial-question-12 | fast/none | gpt-6-astra(low) | 10.0 | 0.33 | unstable | NO |
| debug-spanish-slow-query-18 | frontier/high | ollama/glm-5.3-flash(low) | 8.3 | 0.97 | unstable | NO |
| extract-action-items-65 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| extract-invoice-json-62 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| quick-borrow-checker-misleading-75 | frontier/medium | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |
| quick-drug-interaction-73 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| quick-percentage-74 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| quick-postgres-port-71 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| quick-timezone-abbrev-77 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| summarise-adversarial-doc-69 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| summarise-meeting-notes-dates-61 | frontier/medium | ollama/glm-5.3-flash(none) | 8.6 | 0.00 | stable | NO |
| summarise-trial-abstract-66 | frontier/high | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |
| writing-investor-update-numbers-44 | frontier/high | ollama/glm-5.3-flash(none) | 9.7 | 0.04 | stable | NO |

## Proposed label changes

| case | label | outcome says | confidence | applied |
|---|---|---|---|---|
| writing-investor-update-numbers-44 | frontier/high | fast/none | clear | yes |
| debug-adversarial-injection-in-log-16 | frontier/high | fast/none | clear | yes |
| summarise-trial-abstract-66 | frontier/high | fast/none | mixed | no, left for a person |
| summarise-meeting-notes-dates-61 | frontier/medium | fast/none | clear | yes |
| debug-go-deadlock-short-40-11 | frontier/high | fast/none | clear | yes |
| quick-borrow-checker-misleading-75 | frontier/medium | fast/none | clear | yes |
| code-sql-window-query-05 | frontier/medium | fast/none | clear | yes |

- **writing-investor-update-numbers-44**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 9.7, fast-tier scores [9.74, 9.49, 9.49], frontier-tier scores [9.23, 9.49, 9.49, 9.49].
- **debug-adversarial-injection-in-log-16**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].
- **summarise-trial-abstract-66**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 9.33], frontier-tier scores [9.33, 10.0, 9.67, 10.0].
- **summarise-meeting-notes-dates-61**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 8.6, fast-tier scores [8.57, 8.57, 8.57], frontier-tier scores [8.57, 8.57, 8.57, 8.57].
- **debug-go-deadlock-short-40-11**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 10.0], frontier-tier scores [10.0, 10.0, 9.58, 9.58].
- **quick-borrow-checker-misleading-75**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].
- **code-sql-window-query-05**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].

