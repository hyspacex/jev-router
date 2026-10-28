# DeepSeek 1017 vs GLM rerun

2026-10-27. 269 live Ollama calls: 34 probes (13 of them a mistaken rerun of
the suffix probe) and 235 run attempts against a ceiling of 250, one a
retried timeout. Plan and decision rules were written to `plan.json` before
the first run call. Measurements are in `measurements.md` and `analysis.json`.

## Verdict

**Keep GLM-5.3 Flash and GLM-5.3 with reasoning off as the easy and moderate
primaries.** DeepSeek V4.1 Flash 1017 matches them on quality and is slower,
because it always reasons and they do not. It fails the admission rule on speed
alone, now under clean, matched load.

**As a reasoning model, DeepSeek is the stronger one.** Against GLM with
reasoning on (`medium`), it is two to three times faster and scores the same or
better. That agrees with the public benchmarks, which all run at maximum
reasoning. Our production routes do not use GLM's reasoning on easy or
moderate text, so that comparison does not decide them.

## What changed from the refresh

- **Effort was measured, not assumed** (`probe-effort.json`,
  `probe-controls.json`). GLM `(none)` turns reasoning off, `(medium)` reasons,
  and `(low)` and `(high)` barely reason through this proxy. DeepSeek reasons
  the same at every suffix, and under `reasoning_effort`, `think: false` and
  `reasoning.enabled: false`. A no-reasoning DeepSeek arm cannot be sent, so
  GLM `(medium)` was added as the reasoning-matched comparison.
- **The regex grader was fixed.** It now reads triple-quoted literals whole and
  honours flags the reply passes to `re.compile`, with tests. Regraded from
  cache, the refresh's moderate gap goes from -0.38 to -0.14 [-0.49, +0.16]
  (`rescore-old.json`).
- **Load was clean.** One request at a time, nothing else in the run, arm
  order shuffled per case and sample, streaming so time to the first answer
  token is visible. In the refresh, DeepSeek's latency was taken while a
  195-call tool pilot ran on the same subscription.
- **Grading had more cases and samples.** There were 26 cases (13 easy,
  13 moderate), the refresh's sets plus every unused check-graded case at
  difficulty 0 to 2, with 3 fresh samples per arm. The specs were not changed.
  Results without the unprompted word caps are reported beside the primary
  score and never used for the verdict.

## Results

Scores out of 10, 95% case-bootstrap intervals. Differences are DeepSeek minus
GLM; time ratios are DeepSeek over GLM, paired by case.

| | DeepSeek vs GLM, reasoning off (production) | DeepSeek vs GLM medium (reasoning on) |
|---|---|---|
| Easy, score | -0.43 [-1.03, +0.12], p=0.14 | -0.45 [-0.95, -0.05] |
| Easy, without word caps | -0.03 [-0.53, +0.37] | -0.14 [-0.58, +0.15] |
| Easy, total time | 1.66x [1.30, 1.80] (1.66 s vs 1.08 s) | 0.47x [0.40, 0.74] |
| Moderate, score | -0.12 [-0.53, +0.14], p=0.62 | +0.76 [-0.02, +2.12] |
| Moderate, total time | 2.55x [1.60, 3.70] (7.95 s vs 4.51 s) | 0.35x [0.26, 0.51] |
| Moderate, first answer token | 7.9x (5.6 s vs 0.54 s) | 0.30x |

Under the official rule, both steps clear the quality bar (within 0.5) and fail
the speed bar (needs 30% faster; DeepSeek is 66% and 155% slower). The
interval-based readings in the plan are reported, not binding: neither step is
non-inferior at a -0.5 margin, because the easy interval reaches -1.03 and the
moderate one -0.53. Neither is superior. GLM `medium` on moderate text ran out
of its 16,000-token budget three times and timed out once; DeepSeek returned
no empty or truncated replies.

## Caveats

- **Two more pattern misses count against DeepSeek, found after the run.** On
  `quick-drug-interaction-73` it wrote "blood-pressure effect"; the pattern
  wants "blood pressure", without the hyphen. On `quick-weakmap-vs-map-72` it put
  "not iterable" and "no `.size`" in a table. Both answers are correct. The
  specs were left alone, because two fixes in a row that help the candidate is
  how a grader drifts. Fixing them would shrink the easy gap to about -0.15. The
  verdict would not change, because speed decides it.
- **The no-reasoning arms are near the ceiling.** GLM is at 9.8 on both steps,
  so these sets cannot show a quality advantage for anyone.
- **Latency is one evening on Ollama Cloud through one proxy.** It is an
  observed ratio, not a quota saving.
- **The tool lane was not tested.** The refresh's tool pilot leaked hidden test
  names. Also, the production tool route `ollama/glm-5.3(high)` barely reasons
  through this proxy, so DeepSeek's always-on reasoning may be worth more there
  than on text. A clean tool qualification needs `evals/run_sessions.py` to run
  checks on an isolated copy first.

## Files and reproduction

| File | What it is |
| --- | --- |
| `plan.json` | Arms, cases, budget and decision rules, written before the run |
| `probe-effort.json`, `probe-controls.json` | What each suffix and reasoning field did |
| `rescore-old.json` | The refresh's easy and moderate replies regraded under the fixed checker |
| `measurements.md`, `analysis.json` | Scores, latencies and paired comparisons |
| `wire-attempts.json`, `manifest.json` | Every HTTP attempt, and the run manifest |

Reply text stays in the git-ignored `evals/.cache/rerun-2026-10-27/` and is not
published. The live scripts spend quota and read `JEV_ROUTER_UPSTREAM` and
`UPSTREAM_API_KEY`:

```sh
uv run python evals/reports/2026-10-27-deepseek-rerun/probe_effort.py
uv run python evals/reports/2026-10-27-deepseek-rerun/probe_controls.py
uv run python evals/reports/2026-10-27-deepseek-rerun/run_rerun.py     # resumes from the cache
uv run python evals/reports/2026-10-27-deepseek-rerun/rescore_old.py   # offline
uv run python evals/reports/2026-10-27-deepseek-rerun/analyse_rerun.py # offline
```
