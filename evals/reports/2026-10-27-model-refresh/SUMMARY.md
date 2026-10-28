# DeepSeek 1017 and Sol 6.1: admission decision

> **Re-examined the same day.** The DeepSeek comparisons here mixed reasoning on
> and off, mis-graded a correct `re.VERBOSE` regex, and timed DeepSeek while the
> tool pilot loaded the same subscription. The
> [DeepSeek rerun](../2026-10-27-deepseek-rerun/SUMMARY.md) corrects all three and reaches the same verdict for a
> different reason: speed, not quality. The Sol 6.1 findings are unaffected.

2026-10-27. **378 HTTP attempts of the approved 600-call ceiling:** 5 transport
probes, 178 text/judge attempts including retries, and 195 tool-loop attempts.
The final result reconstruction and classifier controls made no provider calls.
Two text attempts timed out and one was cancelled with upstream outcome unknown.

## Config decision

- **Keep GLM Flash none for easy work.** DeepSeek 1017 low did not pass the
  quality/speed admission conditions.
- **Keep GLM none for moderate text.** DeepSeek low was not faster on the same
  subscription. DeepSeek max produced an empty truncated reply under the declared
  8,000-token cap and was screened out, with its stopped run retained.
- **Add GPT-6.1 Sol low as the first equivalent fallback for moderate text.**
  It passes the existing quality-gap and different-provider conditions. Retain
  GPT-6 Sol low after it and keep GLM as primary.
- **No new tool pair.** The pilot failed the predeclared all-task criterion and
  post-run inspection found hidden-test metadata leaking through pytest cache.
  Its successes and failures are not clean model-ranking evidence.
- **No hard-work change.** Sol 6.1 xhigh repeatedly timed out on the
  buy-versus-rent case. The hard stage was stopped, all remaining units named in
  the manifest, and no partial PASS is treated as qualification.
- **No change to careful-work, images, high harm, hardest-work, classifier
  questions or numeric thresholds.** The faithful-text test was small and does
  not establish full tool-bearing/high-consequence lane coverage. Existing GLM
  already supplies the measured cross-provider careful-work fallback.

## Completed text evidence

Scores are out of ten. Differences are candidate minus incumbent, with 95%
case-bootstrap intervals; two samples per case, 10,000 resamples, seed 11.
Historical and fresh control quality are combined. Latency below uses only
this run's fresh control sample, never the historical latency.

| Candidate and workload | Score difference, 95% CI | Admission |
| --- | --- | --- |
| DeepSeek low, 10 easy cases vs GLM Flash | -0.53 [-1.27, 0.00] | No: gap exceeds 0.5 and no speed/provider benefit |
| DeepSeek low, 12 moderate cases vs GLM none | -0.38 [-0.79, 0.00] | No: same provider, slower |
| Sol 6.1 low, 12 moderate cases vs GLM none | -0.16 [-0.43, +0.12] | Yes, moderate text only; different provider |

Fresh median seconds, 95% case-bootstrap intervals:

| Workload | Candidate | Incumbent |
| --- | --- | --- |
| Easy text | DeepSeek low 1.97 [0.87, 2.47] | GLM Flash 0.79 [0.48, 1.27] |
| Moderate text | DeepSeek low 9.66 [6.50, 20.42] | GLM none 4.35 [2.48, 5.01] |
| Moderate text | Sol 6.1 low 15.24 [11.74, 27.83] | GLM none 4.35 [2.48, 5.01] |

This is a capacity fallback, not a speed upgrade or a measured quota saving.
The hard-stage available-case numbers are in `measurements.md`, prominently
labelled incomplete; no conclusion is drawn from their near-ceiling scores.

## Tool-pilot validity

Descriptive only: DeepSeek max completed 4/8 tasks, 50.0% [21.5%, 78.5%]; GLM high
and Sol 6.1 low each completed 7/8, 87.5% [52.9%, 97.8%] (95% Wilson intervals).
Every arm hit the multi-turn cap. Hidden-test filenames left in pytest cache
were explicitly mentioned by model replies. The container isolation test passed,
but it did not test hidden-check isolation. These counts cannot establish clean
model equivalence or justify routing changes. Repair check isolation before a
new tool qualification; do not reinterpret a capped run as success.

## Implementation and validation

`router.yaml` adds Sol 6.1 low to `moderate-work` and the `mid` fallback chain,
and to the two existing auto alias allowlists. No old model is removed, preserving
existing strict bindings. `supports_vision` is false; only the admitted low effort
is exposed. The operational context limit remains a conservative 400k; no
long-context qualification is claimed. Standard API price ratio to Astra is 0.2,
not fitted to make a quota choice win. No adaptive-effort profile is verified.

The grammar grader now accepts curly apostrophes in valid contractions, with
positive and negative regression tests. Every model was regraded consistently.
Brevity penalties and outcome labels were not changed. A pre-existing admission
summary dict/list variable-name collision was fixed without behavior changes.

All four classifier controls reran before and after, entirely from cache. No
case changed its zero-pressure route. Tier accuracy is unchanged on tuning:
93.3% [87.4%, 96.6%], and held-out: 92.2% [81.5%, 96.9%], paired p = 1.000 for each
split. Shuffled labels remain within permutation chance; constant-state,
constant-policy and length-only do not match the real ladder.

Local validation: 1,365 offline tests passed, 3 skipped; Docker isolation test
passed separately; config validation and fresh type checks passed. Pressure
regressions prove Sol 6.1 enters moderate text, not tools or protected lanes.

## Artifacts

- `METHOD.md`: exact controls, budgets, cache salts, proxy effort limits and caveats.
- `manifest.json`: final cumulative budget, exclusions and unfinished units.
- `results.json`, `analysis.json`, `measurements.md`: projected measurements.
- `stopped-cross-workload/`, `stopped-empty/`: preserved stopped runs.
- `sessions/`: contaminated pilot, labelled as such; response/check text omitted.
- `controls/`, `controls-after/`: offline replays, summaries and manifests.
- `wire-attempts.json`: every attempted text request, including timeouts/cancellation.

The checked-in scripts are report-local orchestration, not production policy.
Live reruns spend quota; a cache-only reconstruction does not turn past live
measurements into new observations. See POOL.md for the durable verdict.
