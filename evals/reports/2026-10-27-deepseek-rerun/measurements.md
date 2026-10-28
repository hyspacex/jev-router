# DeepSeek 1017 vs GLM rerun: measurements

95% case-bootstrap intervals, 10,000 draws, seed 11. Each arm's per-case value is the mean (scores) or median (seconds) of its 3 samples. Differences are DeepSeek minus the named GLM arm. Latency is streamed end to end through the deployed CLIProxyAPI proxy, one request at a time, arms interleaved.

## easy (13 cases)

| arm | score, 95% CI | without word caps | median seconds, 95% CI | first answer token s | median completion tokens | empty | truncated | model returned |
|---|---|---|---|---|---:|---:|---:|---|
| glm-flash-none | 9.76 [9.4643, 10.0] | 9.74 [9.4414, 10.0] | 1.08 [0.857, 1.401] | 0.52 [0.484, 0.587] | 130 | 0 | 0 | glm-5.3-flash |
| glm-flash-medium | 9.78 [9.5491, 9.9573] | 9.85 [9.6154, 10.0] | 3.35 [1.521, 3.965] | 2.74 [1.415, 3.245] | 567 | 0 | 0 | glm-5.3-flash |
| deepseek | 9.33 [8.6282, 9.8718] | 9.71 [9.1346, 10.0] | 1.66 [1.061, 2.131] | 0.91 [0.703, 1.17] | 291 | 0 | 0 | deepseek-v4.1-flash:1017 |

| DeepSeek vs | score difference, 95% CI, p | without word caps | total-time ratio, 95% CI | first-answer ratio, 95% CI |
|---|---|---|---|---|
| glm-flash-none | -0.43 [-1.0299, 0.1172], p=0.143 | -0.03 [-0.5256, 0.3736] | 1.66 [1.297, 1.803] | 1.65 [1.022, 1.981] |
| glm-flash-medium | -0.45 [-0.953, -0.0513], p=0.055 | -0.14 [-0.5769, 0.1538] | 0.47 [0.398, 0.743] | 0.34 [0.256, 0.442] |

Against production `glm-flash-none`: official rule FAIL (quality within 0.5: True; 30% faster: False). Non-inferior: False. Superior: False. Proposed quality path: False.

## moderate (13 cases)

| arm | score, 95% CI | without word caps | median seconds, 95% CI | first answer token s | median completion tokens | empty | truncated | model returned |
|---|---|---|---|---|---:|---:|---:|---|
| glm-none | 9.79 [9.5314, 9.9715] | 9.82 [9.5599, 10.0] | 4.51 [2.945, 7.513] | 0.54 [0.439, 3.171] | 560 | 0 | 0 | glm-5.3 |
| glm-medium | 8.90 [7.2964, 9.8916] | 8.93 [7.3313, 9.8916] | 42.78 [15.557, 63.713] | 39.50 [13.391, 59.108] | 5381 | 3 | 3 | glm-5.3 |
| deepseek | 9.67 [9.1453, 10.0] | 9.74 [9.3565, 10.0] | 7.95 [5.772, 22.99] | 5.63 [3.972, 21.275] | 1950 | 0 | 0 | deepseek-v4.1-flash:1017 |

| DeepSeek vs | score difference, 95% CI, p | without word caps | total-time ratio, 95% CI | first-answer ratio, 95% CI |
|---|---|---|---|---|
| glm-none | -0.12 [-0.5322, 0.1387], p=0.617 | -0.08 [-0.4094, 0.1387] | 2.55 [1.595, 3.695] | 7.93 [6.835, 12.506] |
| glm-medium | +0.76 [-0.0155, 2.1158], p=0.132 | +0.81 [0.0466, 2.1391] | 0.35 [0.256, 0.511] | 0.30 [0.23, 0.383] |

Against production `glm-none`: official rule FAIL (quality within 0.5: True; 30% faster: False). Non-inferior: False. Superior: False. Proposed quality path: False.

