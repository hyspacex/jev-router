# Outcome benchmark (layer 3)

- when: 2026-09-19T07:08:17+00:00
- cases: 25
- routes per case: 7
- a route counts as adequate when it scores within 0.5 of the best route

## Route averages

| route | mean score | median score | wins outright | adequate on | median latency ms | median output tokens |
|---|---|---|---|---|---|---|
| ollama/glm-5.3-flash(none) | 8.14 | 9.20 | 9/25 | 11/25 | 3963 | 428 |
| ollama/glm-5.3-flash(low) | 7.71 | 9.00 | 10/25 | 11/25 | 4003 | 468 |
| ollama/glm-5.3-flash(high) | 7.35 | 8.80 | 10/25 | 11/25 | 5373 | 605 |
| gpt-6-astra(low) | 9.00 | 10.00 | 17/25 | 19/25 | 12188 | 309 |
| gpt-6-astra(medium) | 9.18 | 10.00 | 20/25 | 21/25 | 13210 | 381 |
| gpt-6-astra(high) | 9.10 | 10.00 | 18/25 | 20/25 | 15238 | 416 |
| gpt-6-astra(xhigh) | 9.12 | 10.00 | 18/25 | 20/25 | 26964 | 829 |

## Does effort change the answer?

| model | effort pair | mean score difference | cases where the higher effort was better by 0.5+ |
|---|---|---|---|
| glm-5.3-flash | none -> low | -0.43 | 5/25 |
| glm-5.3-flash | low -> high | -0.36 | 4/25 |
| gpt-6-astra | low -> medium | +0.18 | 4/25 |
| gpt-6-astra | medium -> high | -0.08 | 1/25 |
| gpt-6-astra | high -> xhigh | +0.01 | 3/25 |

## Cheapest adequate route per case, against the hand label

| case | label tier/effort | cheapest adequate | best score | agrees |
|---|---|---|---|---|
| code-prose-shaped-really-code-09 | frontier/medium | ollama/glm-5.3-flash(low) | 10.0 | yes |
| code-regex-extract-04 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | yes |
| code-sql-window-query-05 | frontier/medium | ollama/glm-5.3-flash(high) | 9.0 | NO |
| debug-adversarial-injection-in-log-16 | frontier/high | gpt-6-astra(low) | 10.0 | yes |
| debug-css-overflow-17 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | yes |
| debug-go-deadlock-short-40-11 | frontier/high | gpt-6-astra(low) | 10.0 | yes |
| debug-long-log-trivial-question-12 | fast/none | ollama/glm-5.3-flash(low) | 4.0 | yes |
| debug-spanish-slow-query-18 | frontier/high | gpt-6-astra(low) | 10.0 | yes |
| design-auth-module-review-23 | frontier/high | gpt-6-astra(low) | 9.5 | yes |
| design-eventbus-tradeoff-20 | frontier/xhigh | ollama/glm-5.3-flash(none) | 10.0 | NO |
| design-naming-bikeshed-24 | fast/none | gpt-6-astra(medium) | 7.0 | NO |
| extract-action-items-65 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | yes |
| extract-invoice-json-62 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | yes |
| quick-borrow-checker-misleading-75 | frontier/medium | ollama/glm-5.3-flash(none) | 10.0 | NO |
| quick-drug-interaction-73 | frontier/medium | ollama/glm-5.3-flash(none) | 10.0 | NO |
| quick-percentage-74 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | yes |
| quick-postgres-port-71 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | yes |
| quick-timezone-abbrev-77 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | yes |
| summarise-adversarial-doc-69 | frontier/medium | ollama/glm-5.3-flash(none) | 10.0 | NO |
| summarise-meeting-notes-dates-61 | frontier/medium | gpt-6-astra(low) | 10.0 | yes |
| summarise-trial-abstract-66 | frontier/high | ollama/glm-5.3-flash(low) | 10.0 | NO |
| writing-incident-customer-notice-37 | frontier/high | gpt-6-astra(low) | 10.0 | yes |
| writing-investor-update-numbers-44 | frontier/high | gpt-6-astra(low) | 10.0 | yes |
| writing-legal-clause-39 | frontier/high | gpt-6-astra(low) | 8.0 | yes |
| writing-medical-discharge-38 | frontier/high | gpt-6-astra(low) | 10.0 | yes |

## Proposed label changes

| case | label | outcome says | confidence | applied |
|---|---|---|---|---|
| summarise-adversarial-doc-69 | frontier/medium | fast/none | clear | yes |
| summarise-trial-abstract-66 | frontier/high | fast/low | mixed | no, left for a person |
| quick-drug-interaction-73 | frontier/medium | fast/none | clear | yes |
| quick-borrow-checker-misleading-75 | frontier/medium | fast/none | mixed | no, left for a person |
| code-sql-window-query-05 | frontier/medium | fast/high | mixed | no, left for a person |
| design-eventbus-tradeoff-20 | frontier/xhigh | fast/none | mixed | no, left for a person |
| design-naming-bikeshed-24 | fast/none | frontier/medium | clear | yes |

- **summarise-adversarial-doc-69**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].
- **summarise-trial-abstract-66**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(low), best score 10.0, fast-tier scores [9.2, 9.6, 8.8], frontier-tier scores [8.0, 10.0, 10.0, 10.0].
- **quick-drug-interaction-73**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [9.5, 10.0, 10.0], frontier-tier scores [8.12, 10.0, 10.0, 10.0].
- **quick-borrow-checker-misleading-75**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 7.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].
- **code-sql-window-query-05**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(high), best score 9.0, fast-tier scores [7.0, 2.0, 8.5], frontier-tier scores [8.0, 9.0, 9.0, 8.0].
- **design-eventbus-tradeoff-20**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [9.5, 9.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].
- **design-naming-bikeshed-24**: the fast model was not good enough: cheapest adequate route is gpt-6-astra(medium), best score 7.0, fast-tier scores [4.0, 4.0, 4.0], frontier-tier scores [6.0, 7.0, 6.0, 7.0].

