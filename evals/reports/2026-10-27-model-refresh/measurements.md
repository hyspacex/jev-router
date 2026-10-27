# Model refresh: paired measurements

95% case-bootstrap intervals, 10,000 draws, seed 11. Differences are candidate minus reference. Fresh latency uses only this run's dated control sample; candidates use their two fresh samples averaged within case. Neither latency nor output tokens measure subscription savings.

## refresh_fast

| route | cases | score, 95% CI | fresh median seconds, 95% CI (n) |
|---|---:|---|---|
| deepseek41-low | 10 | 9.47 [8.6667, 10.0] | 1.97 [0.8673, 2.4694] (n=10) |
| glm-5.3-flash | 10 | 10.00 [10.0, 10.0] | 0.79 [0.4809, 1.2653] (n=10) |

| candidate vs reference | paired cases | difference, 95% CI | paired p |
|---|---:|---|---:|
| deepseek41-low vs glm-5.3-flash | 10 | -0.53 [-1.2667, 0.0] | 0.2014 |

## refresh_mid

| route | cases | score, 95% CI | fresh median seconds, 95% CI (n) |
|---|---:|---|---|
| deepseek41-low | 12 | 9.34 [8.7165, 9.8485] | 9.66 [6.4952, 20.4176] (n=12) |
| sol61-low | 12 | 9.55 [9.1235, 9.9167] | 15.24 [11.7358, 27.8306] (n=12) |
| glm-5.3 | 12 | 9.71 [9.4776, 9.9306] | 4.35 [2.4779, 5.008] (n=12) |

| candidate vs reference | paired cases | difference, 95% CI | paired p |
|---|---:|---|---:|
| deepseek41-low vs glm-5.3 | 12 | -0.38 [-0.7897, 0.0] | 0.0698 |
| sol61-low vs glm-5.3 | 12 | -0.16 [-0.4331, 0.1184] | 0.2536 |
| sol61-low vs deepseek41-low | 12 | +0.22 [-0.1367, 0.7672] | 0.3815 |
| glm-5.3 vs deepseek41-low | 12 | +0.38 [0.0, 0.7897] | 0.0698 |

## refresh_faithful

| route | cases | score, 95% CI | fresh median seconds, 95% CI (n) |
|---|---:|---|---|
| deepseek41-low | 5 | 9.50 [9.0317, 9.9] | 12.00 [3.9935, 20.0539] (n=5) |
| sol61-low | 5 | 9.43 [8.9429, 9.9167] | 11.65 [5.8203, 23.0263] (n=5) |
| astra-medium | 5 | 9.21 [8.7841, 9.6944] | 17.76 [4.1501, 35.6244] (n=5) |
| glm-5.3 | 5 | 9.50 [9.0317, 9.9] | 1.71 [1.0746, 3.5122] (n=5) |

| candidate vs reference | paired cases | difference, 95% CI | paired p |
|---|---:|---|---:|
| deepseek41-low vs astra-medium | 5 | +0.29 [0.0833, 0.5056] | 0.0117 |
| sol61-low vs astra-medium | 5 | +0.22 [0.0, 0.6667] | 0.5921 |
| glm-5.3 vs astra-medium | 5 | +0.29 [0.0833, 0.5056] | 0.0117 |

## refresh_hard

**Incomplete, no admission verdict.** Sol 6.1 xhigh repeatedly timed out; remaining cases were not run. Historical controls cover more cases than candidates. Scores below are available-case descriptions, not a completed comparison; failed attempts are retained in the wire ledger. Fresh latency intervals exclude unknown timeout durations and must not be used to excuse them.

| route | cases | score, 95% CI | fresh median seconds, 95% CI (n) |
|---|---:|---|---|
| sol61-high | 6 | 9.75 [9.4872, 10.0] | 45.33 [16.8467, 88.6856] (n=6) |
| sol61-xhigh | 5 | 9.72 [9.4135, 10.0] | 86.45 [17.2023, 102.2361] (n=5) |
| astra-high | 11 | 9.39 [8.7727, 9.875] | 39.17 [16.3976, 102.8363] (n=6) |
| sol6-xhigh | 11 | 9.29 [8.4755, 9.9301] | 20.74 [9.5196, 45.9982] (n=6) |

| candidate vs reference | paired cases | difference, 95% CI | paired p |
|---|---:|---|---:|
| sol61-high vs astra-high | 6 | +0.04 [-0.1923, 0.3125] | 0.8048 |
| sol61-xhigh vs astra-high | 5 | +0.07 [-0.2308, 0.45] | 0.8013 |
| sol6-xhigh vs astra-high | 11 | -0.09 [-0.3654, 0.1923] | 0.5228 |
| sol61-high vs sol6-xhigh | 6 | -0.12 [-0.375, 0.0] | 0.6054 |
| sol61-xhigh vs sol6-xhigh | 5 | -0.12 [-0.375, 0.0] | 0.5930 |
| astra-high vs sol6-xhigh | 11 | +0.09 [-0.1923, 0.3654] | 0.5228 |
