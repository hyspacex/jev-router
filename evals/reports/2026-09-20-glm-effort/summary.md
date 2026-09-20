# Moderate tool routing and GLM effort qualification

2026-09-20 UTC. Live fixed-model pilot, not a subscription-saving estimate.

## Decision and scope

**Deployed after resolving the shuffled-label control discrepancy.** The old
report computed equality between whole tier-label tuples, while accuracy
scores prediction membership in acceptable-tier sets. Fixed-prediction
permutation chance is 54.9% (95% null range 48.0–61.4%), not 34.4%.
Observed 58.5% is compatible with chance (one-sided p = 0.1683). The shuffle
also now preserves donor-resolved tier sets instead of recomputing them using
recipient metadata. Original annotations were not changed.

All four controls reran from cache with no paid calls. Corrected summaries
and manifests are prefixed `corrected-`. The unchanged real-answer policy
replay changed eight choices without changing tier correctness on either
split; see EXPERIMENTS.md for Wilson intervals and paired bootstrap tests.
This is routing validation, not proof of general GLM coding equivalence.

A fresh live admission selected `ollama/glm-5.3(high)` with rule
`moderate_tools` and lane `moderate-tools`; the smoke binding was closed
without sending an execution request. This smoke test spent one Jev call.
Existing session bindings were not changed. Config validation and 1,130
offline tests passed (3 skipped). Rollback config is preserved under
`~/.jev-router/backups/tool-policy-20260919-233917/`.

Admit `ollama/glm-5.3(high)` for moderate tool-enabled work after the existing
image, hard-work, high-harm and moderate-careful guards. This is a bounded
operational admission based on successful local fixtures and the owner's
request to enable GLM, not a claim of broad frontier equivalence. Leave the
classifier wording, confidence gate, numeric cutoffs, non-tool mid route,
strict-session invariants and existing bindings unchanged.

Remove the blanket `tool_loop` rule. Run `moderate_careful` before the new
`moderate_tools` rule, so non-code-edit work with difficulty >= 1.4 and harm
>= 0.45 can reach Astra low even when tools are supplied. No need to reduce
high-harm or uncertainty protection merely to increase low-effort traffic.

## Research

Primary source: https://z.ai/blog/glm-5.3 (retrieved 2026-09-20 UTC).
Z.ai reports strong coding/tool performance with reasoning enabled, recommends
max for coding, and documents low/high/max with no thinking-off mode. Its
private benchmark shows a quality/token tradeoff between high and max; it does
not establish equivalence to this proxy's Astra deployment. The published
aggregate scores lack confidence intervals, so no statistical comparison is
claimed here. Secondary marketing summaries were not used for admission.

The actual deployment is an Ollama-prefixed proxy with suffix effort controls,
not Z.ai's direct API. Earlier `(none)` results establish only that this proxy
accepted that wire name, not that native GLM reasoning was disabled. This run
verifies usable tool responses at explicit `(low)` and `(high)`; it cannot
prove what reasoning budget the upstream actually applied. Max is not added
without endpoint-specific testing.

The existing `evals/outcomes.md` reports Astra low mean 9.37 (interval
8.88–9.87), medium 9.51 (9.01–10.00). Those small-task results do not justify
removing the medium safety defaults. Low remains the existing careful-work
choice; removing the earlier tool rule makes it reachable.

## Live pilot

Eight existing synthetic coding tasks, one repeat, three fixed arms, rotating
order, serial calls. Trusted Docker image:
`sha256:f88ce06928ee50f396519fd424a5585879005cfafcd14a94d821b59a27bbed18`.
Generated commands run with no network, read-only container root, and no
credentials. Task checks were verified to fail on untouched fixtures.
Per-session caps: 12 model calls, 180 seconds, 4,096 output tokens per call.
Planned ceiling: 288 calls. Actual live-run calls: 120.

| Arm | Completed (95% Wilson interval) | Calls | Capped | Errors |
| --- | --- | ---: | ---: | ---: |
| GLM low | 7/8, 87.5% [52.9%, 97.8%] | 48 | 1 | 0 |
| GLM high | 8/8, 100% [67.6%, 100%] | 48 | 0 | 0 |
| Astra low | 4/8, 50% [21.5%, 78.5%] | 24 | 0 | 4 |

GLM low exhausted the call cap on `multi-turn-ladder`; high completed it.
GLM high versus low is only one discordant task, not a significant general
quality difference. High is selected as the conservative tested tool setting,
not as a statistically proven optimum.

Median whole-session wall time: GLM high 6.34 s (95% task-bootstrap interval
4.38–11.59 s), GLM low 4.92 s (4.12–8.83 s), seeded 10,000 resamples.
Do not compare Astra latency: its failures short-circuited sessions.

Astra completed the first four tasks, then had upstream connection timeouts
and `auth_unavailable` responses on the remaining four. These remain failures
in `results.json`; they are not evidence of inferior reasoning quality, and
no healthy-provider non-inferiority verdict is possible.

An initial attempt used the repository's localhost upstream default, where no
proxy was listening. All 24 sessions failed to connect, spending no provider
calls. `failed-preflight-manifest.json` retains this infrastructure failure.
The successful connection used the service's configured upstream override.

## Limits

No long-context, general-agent reliability, or repeated-run qualification.
No GLM membership in the frontier quality lane. No quota-based cross-lane
promotion. Existing sessions are not reselected. Native reasoning semantics
behind the proxy remain unverified. Published result JSON omits response and
check text; source hashes and the run manifest accompany it.
