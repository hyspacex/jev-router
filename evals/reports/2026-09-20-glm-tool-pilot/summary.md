# GLM-5.3 tool-driven coding pilot

## Verdict

**Pilot pass; broad tool-loop qualification remains inconclusive.**
`ollama/glm-5.3(none)` completed every existing bounded task with the same
observed completion count as `gpt-6-astra(medium)`. This establishes live
multi-step tool-use evidence for these fixtures. It does not establish
non-inferiority on arbitrary coding work, large repositories, long sessions,
image inputs, or high-risk changes. No routing configuration or quality-lane
membership was changed. A wider repeated evaluation is needed before broad
promotion of the mid model into agent traffic.

## Design and provenance

Run timestamp: 2026-09-20 UTC (September 19 local time). Live, not policy
replay or simulation. Eight existing tasks across six families; one session
per model per task; arm order alternated by task. Both fixed models called
the existing proxy directly, bypassing router classification, pins and
fallbacks. No Jev calls were needed. The only runner change was an opt-in
`fixed_mid` arm; default arms, prompts, task checks and scoring were unchanged.

The approved ceiling was 192 model calls across 16 sessions, with 12 calls
and 300 seconds per session; tighter task caps still applied. Each response
had a 4,096-token ceiling. Actual usage was 93 model calls (46 GLM, 47 Astra).
These are exact run counts, not estimates of subscription savings.

Generated commands and hidden checks ran in the local `jev-eval:local` Docker
image with networking disabled and a scrubbed environment. Hidden checks
were verified to fail on untouched fixtures before provider calls. The Docker
isolation and HTTP-contract preflight passed. Both models completed all three
turns of the multi-turn fixture at exactly 12 calls, leaving no call headroom.

See [results.json](results.json) for per-session measurements,
[manifest.json](manifest.json) for the live-run manifest,
[plan.json](plan.json) for the task plan and
[provenance.json](provenance.json) for source hashes and container identity.
Published results omit generated response text and check output; no prompts
or credentials are included. The source run is
`evals/results/sessions-1789882602` (git-ignored).

## Results

| Model | Completed (95% Wilson interval) | Capped | Errored | Model calls | Tool calls | Median session wall time (95% bootstrap interval) |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| GLM-5.3 `(none)` | 8/8; 100% [67.6%, 100%] | 0 | 0 | 46 | 51 | 4.72 s [3.93, 13.09] |
| Astra `(medium)` | 8/8; 100% [67.6%, 100%] | 0 | 0 | 47 | 53 | 27.89 s [21.98, 38.02] |

Completion paired-bootstrap p = 1.000: the pilot cannot tell their completion
rates apart. This is not proof of equivalence; there was no predeclared
non-inferiority margin. Latency intervals use 20,000 task resamples with
Python `random.Random(20260920)`, processing GLM then Astra, percentile
endpoints at sorted indices 500 and 19499. They describe task-mix uncertainty,
not repeated-run/provider variability. Wall time includes tool/check overhead,
not just model latency.

Both models passed mechanical rename, mechanical transformation, CSV
implementation, money implementation, interacting-cause diagnosis,
context-growth audit, multi-turn changes and missing-environment handling.
Per-task measurements are descriptive single observations, not slice-level
claims. GLM was slower on CSV implementation despite its lower overall median.
No session was capped, errored, excluded or dropped.

## Limits and follow-up

These are small synthetic repositories, not representative large real-world
coding sessions. One repeat cannot measure intermittent failures. Completion
is determined by hidden checks, not subjective answer quality. Long-context,
vision, production safety, adversarial tools and native cache preservation
were not qualified. Provider-reported token usage is retained in results but
is not converted into quota or cost savings.

The classification harness and classifier policy were untouched. Its four
negative-control runs were not rerun within this model-call pilot budget;
no new classifier-accuracy claim is made. Before any routing-policy change,
run the required controls and a wider repeated tool-task qualification with
predeclared acceptance criteria. This report does not authorize deployment.
