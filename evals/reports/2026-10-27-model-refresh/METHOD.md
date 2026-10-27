# Model refresh method

## Scope and budget

The owner approved at most **600 live calls**, including probes, candidates,
judges and tool-loop requests. The runners count HTTP attempts before sending,
including retries; the older `Budget.spent` counter alone counts only successful
responses and is not used as the global spending ceiling.

Allocation: 5 preflight calls, 310 text-stage attempts across initial and resumed
runs, 250 tool-loop attempts, 35 unallocated. At most two text requests and one
tool-loop request execute concurrently. No production router binding is created,
closed or modified. Requests name concrete models directly at the existing proxy.

Exact candidates:

- `ollama/deepseek-v4.1-flash:1017(low)`
- `ollama/deepseek-v4.1-flash:1017(max)`
- `gpt-6.1-sol(low)`
- `gpt-6.1-sol(high)`
- `gpt-6.1-sol(xhigh)`

All five were listed or accepted by the deployed proxy. A tiny arithmetic probe
checks transport, not quality, tool support, maximum context or native reasoning
semantics. The versioned DeepSeek alias maps to the same `:1017` upstream name,
not the floating alias.

## Text admission

The four `refresh_*` steps in `evals/admission.yaml` reuse the established easy,
moderate, faithful and hard case sets. No outcome annotations or classifier
questions are changed. There are two samples per candidate and case, at an
8,000-token output cap. Controls use one historical cached answer and one fresh
answer under the salt `refresh-2026-10-27-control-1`. The cache keeps those distinct.
Historical control quality is useful; historical latency is not a current speed
measurement. The analysis reports **only the dated control sample's latency**,
against within-case averages of the two fresh candidate latencies.

The existing general admission rule is unchanged: no more than 0.5 points below
the incumbent, plus either 30% lower latency or a different provider. Ollama's
historical `provider: local` label is retained for the new DeepSeek routes so
switching names does not incorrectly count as another subscription. A partial
step cannot qualify a model, regardless of the harness's provisional verdict.
Passing a point-estimate threshold is not statistical proof of equivalence.

The hard set contains six programmatically checked cases and five judged cases.
Report their paired comparisons separately: the judge is from Sol's family.
`gpt-5.6-sol(high)` is the requested judge, but the inspected proxy has an
unconditional override to `medium` for that exact model. It also has a generic
GPT medium override gated on `X-Rakazo-Client: rakazo`, which these eval clients
do not send. Requested efforts must not be called verified native budgets.

Scores and candidate-minus-reference differences use case-level bootstrap
intervals, 10,000 draws with seed 11. The paired p-value resamples centred
**floating-point differences**, rather than using the boolean helper that casts
values to integers. Session success rates use Wilson intervals. No token count
or latency is described as a subscription saving.

## Stopped run and grading correction

The first attempt stopped after the latency guard compared the same DeepSeek low
route on short easy questions and longer SQL/code questions. Its results,
manifest and spend ledger remain in `stopped-cross-workload/`. The resume keeps
the original 3x per-route guard but starts the latency history at each workload
boundary. It reuses answers already paid for and continues the same cumulative
wire-attempt ledger. Provider failures still stop the run.

The resumed run then stopped on an empty DeepSeek max reply to
`adv-static-markup-rounding-99`: all 8,000 output tokens were consumed and
`finish_reason` was `length`. Its full partial results and manifest are preserved
in `stopped-empty/`. This is a failure under the declared budget, not a missing
sample to discard. DeepSeek max was screened out prospectively; remaining text
comparisons continue without it under the original cumulative call ceiling.
Its tool-loop failures also remain recorded. No result from the surviving models
is presented as a completed comparison with the excluded arm.

The proofreading check required ASCII apostrophes and marked correct `don’t`
and `it’s` as missing. `evals/outcome_specs.yaml` now accepts both apostrophes;
`tests/test_refresh_grading.py` tests both and rejects the uncorrected text.
Every candidate and control is regraded under that same correction. The original
stopped-run result remains uncorrected historical evidence, not the final score.
Brevity penalties on long answers are retained; checks are not loosened to admit
a candidate.

All four classifier controls reran from disk cache with no provider calls. They
are labelled `policy_replay`, not new classifier or latency measurements. See
`controls/*/manifest.json` and `controls/*/summary.md`. Published per-case records
omit their redundant prompt/state and repeated-answer fields and baseline
per-case rows, but retain router IDs, annotations, answers and routing decisions.
Baseline summaries and manifests remain. `controls-after/` repeats the same
checks after the config change, again without provider calls.

## Tool-loop qualification

The existing eight synthetic repositories and hidden checks run unchanged. Arms
are GLM high (fresh reference), DeepSeek 1017 max and Sol 6.1 low. One repeat;
order rotates by task. Caps are 12 model calls and 240 seconds per session, or a
stricter task cap, with the existing 4,096-token response cap. This is deliberately
a bounded pilot, not a general coding-agent or long-context benchmark.

Docker was started locally, `jev-eval:local` built from `evals/Dockerfile`, and
`tests/test_eval_isolation_live.py` passed before the pilot. Generated commands
run with no network, read-only root, only the disposable workspace mounted and
no provider credentials. Untouched fixtures must fail their hidden checks.
The image digest is recorded in the session manifest. Fixed arms bypass the
production router; no router policies or session database are needed.

**Post-run validity finding:** `Workspace.run_check` removes hidden source files,
but its preflight pytest run leaves `.pytest_cache` metadata (and potentially
bytecode) in the agent workspace. Replies explicitly referenced hidden-test names
from that cache. The OS isolation check passed, but that is not proof of hidden
check isolation. These session completion counts are therefore descriptive of a
contaminated pilot, **not clean capability evidence**. Do not use either the
successes or cap failures to rank general coding ability. No new tool pair is
admitted. A clean follow-up must execute checks on an isolated copy or otherwise
prove no test source, bytecode, cache, or feedback leaks into the model workspace;
that repair and rerun are outside this model-refresh deployment.

Before the pilot, the eligibility rule was recorded in `plan.json`: all tasks
must complete without provider errors or caps before this run can support a
new tool pair. Failed and capped sessions remain in the denominator. A weak
control does not excuse a failed candidate or retroactively lower that rule.

## Reproduction and provenance

The report-local runners preserve the orchestration used, with formatting and
path portability improvements. They are not new production routing machinery.
`run_text.py` resumes from its cumulative spend ledger; `run_tools.py` deliberately
refuses to reset an existing pilot ledger. Running them spends real quota and
requires renewed authorization once this experiment's budget is spent.
`run_controls.py` is cache-only; `analyse.py` is entirely offline.

`source-hashes.json` records source hashes and the captured repository revision.
Unrelated dashboard work changed the working tree during the experiment; no
claim is made that all runs used an otherwise clean or frozen repository.
Production config is not deployed by any of these scripts.
