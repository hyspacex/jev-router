# Outcome benchmark (layer 3)

- when: 2026-09-19T15:15:47+00:00
- cases: 10
- routes per case: 7
- samples per route: 3
- candidate token cap: 8000
- a route counts as adequate when it scores within 0.5 of the best route
- where a programmatic check exists it decides the score; the judge still runs so the two can be compared

## Route averages

A route's score is the mean of its k samples. `spread` is the mean within-route standard deviation across samples: it is the noise floor, and a difference between two routes smaller than this is not a difference. `truncated` counts samples that stopped at the token cap of 8000 rather than finishing.

| route | mean score | 95% CI | spread | wins outright | adequate on | truncated % | median latency ms | median output tokens |
|---|---|---|---|---|---|---|---|---|
| ollama/glm-5.3-flash(none) | 9.67 | [9.05, 10.29] | 0.24 | 9/10 | 9/10 | 0.0 | 2300 | 264 |
| ollama/glm-5.3-flash(low) | 9.50 | [8.57, 10.43] | 0.00 | 9/10 | 9/10 | 0.0 | 2468 | 258 |
| ollama/glm-5.3-flash(high) | 9.47 | [8.94, 10.00] | 0.65 | 7/10 | 7/10 | 0.0 | 3864 | 430 |
| gpt-6-astra(low) | 9.28 | [8.36, 10.21] | 0.09 | 7/10 | 7/10 | 0.0 | 10478 | 282 |
| gpt-6-astra(medium) | 9.50 | [8.57, 10.43] | 0.00 | 9/10 | 9/10 | 0.0 | 9876 | 271 |
| gpt-6-astra(high) | 9.38 | [8.46, 10.29] | 0.13 | 7/10 | 7/10 | 0.0 | 12208 | 340 |
| gpt-6-astra(xhigh) | 9.11 | [8.06, 10.16] | 0.51 | 7/10 | 7/10 | 0.0 | 21661 | 656 |

## Winner stability

The cheapest adequate route per case, recomputed on bootstrap resamples of the k samples. A case whose tier flips in more than 20% of draws is marked `unstable` and is not allowed to override a hand label.

- mean tier flip rate: 0.000
- mean route flip rate: 0.032
- unstable cases: 0/10

## Judge against the programmatic check

- samples with both: 83
- differ by 2 points or more: 32.5 [23.4, 43.2]
- disagree on whether the answer passes (7 of 10): 16.9 [10.3, 26.3]
- the judge is higher on: 6.0 [2.6, 13.3]
- mean signed difference, judge minus check: -1.51

Where the two disagree, the check is what the score used. The rate above is how much to discount the judge-only cases.

## Does effort change the answer?

| model | effort pair | mean score difference | cases where the higher effort was better by 0.5+ |
|---|---|---|---|
| glm-5.3-flash | none -> low | -0.17 | 0/10 |
| glm-5.3-flash | low -> high | -0.03 | 1/10 |
| gpt-6-astra | low -> medium | +0.22 | 2/10 |
| gpt-6-astra | medium -> high | -0.12 | 0/10 |
| gpt-6-astra | high -> xhigh | -0.27 | 0/10 |

## Cheapest adequate route per case, against the hand label

| case | label tier/effort | cheapest adequate | best score | tier flip | outcome | agrees |
|---|---|---|---|---|---|---|
| code-regex-extract-04 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| debug-adversarial-injection-in-log-16 | frontier/high | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |
| extract-action-items-65 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| extract-invoice-json-62 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| quick-percentage-74 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| quick-postgres-port-71 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| quick-timezone-abbrev-77 | fast/none | ollama/glm-5.3-flash(high) | 10.0 | 0.00 | stable | yes |
| summarise-adversarial-doc-69 | fast/none | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | yes |
| summarise-trial-abstract-66 | frontier/high | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |
| writing-investor-update-numbers-44 | frontier/high | ollama/glm-5.3-flash(none) | 10.0 | 0.00 | stable | NO |

## Proposed label changes

| case | label | outcome says | confidence | applied |
|---|---|---|---|---|
| writing-investor-update-numbers-44 | frontier/high | fast/none | clear | yes |
| debug-adversarial-injection-in-log-16 | frontier/high | fast/none | clear | yes |
| summarise-trial-abstract-66 | frontier/high | fast/none | mixed | no, left for a person |

- **writing-investor-update-numbers-44**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].
- **debug-adversarial-injection-in-log-16**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 10.0], frontier-tier scores [10.0, 10.0, 10.0, 10.0].
- **summarise-trial-abstract-66**: the fast model was good enough: cheapest adequate route is ollama/glm-5.3-flash(none), best score 10.0, fast-tier scores [10.0, 10.0, 8.67], frontier-tier scores [8.67, 10.0, 9.33, 6.67].

