# Growing the model pool

Written 2026-09-19. It covers five candidate models, the tiers the router
should have, and what each model had to show before it went live. The admission
test has run and `router.yaml` reflects the verdicts below. The sections after
"Verdicts" are the plan as it stood before the test, kept for the reasoning.

This file is also where the `quality_lanes` block in `router.yaml` points. Each
qualified model and effort pair there carries a `qualification_ref` naming a
heading below, and config load refuses a pair that names none. A verdict here
is what lets quota pressure move a request onto that pair; see
`docs/SEMANTIC_POLICY.md`, "Quality lanes". Note that "lane" means two things
in this repository: a named route in `router.yaml`, which is how the sections
below use it, and a `quality_lanes` entry, which is a set of measured pairs. A
route's failure fallbacks are not members of its quality lane.

## Verdicts (2026-09-19)

Full numbers are in `evals/ADMISSION.md`. Two samples per case, 5 to 12 cases
per step, so these show clear differences and nothing finer.

| Model | Verdict | Evidence |
| --- | --- | --- |
| glm-5.3 `(none)` | Takes the mid lane | 9.70 against the frontier model's 9.31 on 12 moderate code and analysis cases, at a third of the latency |
| gemma4-31b | Takes easy requests that carry an image. Not the general fast lane | Read all four test images. Scored 0.62 below the current fast model on plain easy work, mostly by over-answering |
| gpt-5.6-luna `(low)` | Fallback for the fast, image and mid lanes | Good answers (9.72) but slower than the frontier model on the same provider, so it buys nothing as a primary |
| grok-4.6 `(low)` | Fallback for the frontier lanes, never promoted by quota | 0.72 below the frontier model on nine hard cases |
| kimi-k3 `(none)` | Not admitted | Lost 11 of 12 blind pairwise comparisons on creative prose and ignored hard constraints in the prompt. Tested with thinking off only |

What the routing eval added after the models went in:

- Sending all moderate work to the mid lane under-routed nine
  faithfulness-sensitive cases (under-routing 1.2% to 5.8%). The
  `moderate_careful` rule keeps moderate work with `harm_if_wrong` at 0.45 or
  above on the frontier model. Code edits are exempt. With the rule, the 171
  cases score tier 92.4%, under-routed 1.2%, over-routed 6.4%, at a slightly
  lower cost than before.
- The shipped tool rule keeps moderate tool-bearing requests on frontier.
  The GLM pilot below adds bounded tool-loop evidence, but does not authorize
  broad promotion. The agentic slice contains 46 of the 171 cases.
- Only `acceptable_tiers` on the admission cases is a measured `mid` label. For
  other cases `evals/common.py` accepts mid where both neighbours are
  acceptable, or where the case is frontier-labelled at difficulty 2 or lower
  with no faithfulness need and no tool loop.

## GLM tool-driven coding pilot (2026-09-20 UTC)

**Pilot pass; broad qualification inconclusive. Routing unchanged.** The live
[GLM tool pilot](evals/reports/2026-09-20-glm-tool-pilot/summary.md) compared
`ollama/glm-5.3(none)` with `gpt-6-astra(medium)` on the eight existing isolated
coding tasks, once per model. Both completed 8/8 (95% Wilson interval
67.6%–100%); paired completion bootstrap p = 1.000 does not establish
equivalence. There were no capped or errored sessions. The run used 93 model
calls of the approved 192-call ceiling.

Median session wall time was 4.72 s for GLM (95% task-bootstrap interval
3.93–13.09 s) and 27.89 s for Astra (21.98–38.02 s). These are live session
measurements including tool overhead, not quota savings or repeated-run
latency estimates. GLM was slower on the CSV task. Both used the entire
12-call allowance on the multi-turn task.

This provides evidence of successful tool use on these bounded fixtures,
not general coding-agent reliability or long-context qualification. No
`quality_lanes` entry, routing rule, allowed-model list, live config or existing
binding was changed. Broader repeated tasks and predeclared acceptance
criteria are needed before promoting mid for general tool-driven traffic.

## GLM reasoning tool qualification (2026-09-20)

The owner requested enabling GLM for moderate tool traffic. The follow-up
[effort pilot](evals/reports/2026-09-20-glm-effort/summary.md) tested the actual
proxy at explicit low and high effort. GLM high completed 8/8 tasks (95%
Wilson interval 67.6%–100%); low completed 7/8 (52.9%–97.8%), with one call
cap. This is bounded operational admission, not broad frontier equivalence.
Astra low had four provider errors; no healthy-provider comparison is claimed.

Deployed after correcting the control's tuple-equality chance calculation to
prediction-versus-label-set membership. All four controls reran from cache;
the shuffled result is inside its permutation-null interval. A fresh live
admission selected GLM high via `moderate_tools`; no execution was sent.

The policy admits GLM high in a separate `moderate-tools` quality lane. Replace
`tool_loop` with `moderate_tools`, after `moderate_careful`, making Astra low
reachable for moderate careful work even when tools are present. Hard,
high-harm, image and low-confidence safeguards stay unchanged. GLM remains
outside `frontier-work`; existing strict bindings do not change. Non-tool
mid keeps its previously measured proxy `(none)` setting. Native Z.ai docs
require thinking, so the proxy's `(none)` must not be described as verified
native thinking-off behavior. See the report for sources and limitations.

## Per-rule lanes (2026-09-23)

`frontier-work` listed astra at low, medium, high and xhigh for five rules.
A lane lets any rule that names it be handed any of its pairs, so the file
claimed astra low was adequate for `very_hard` work. Nobody measured that. It
is now five lanes, one per rule, each holding only the pair that rule already
routes to:

| rule | lane | pair |
|---|---|---|
| `very_hard` | `hardest-work` | astra xhigh |
| `hard` | `hard-work` | astra high |
| `high_harm` | `high-consequence` | astra medium |
| `images_need_vision` | `vision-work` | astra medium |
| `moderate_careful` | `careful-moderate` | astra low |

No pair was added and no route changed, so admission picks the same model and
effort as before. One thing does change: an alias `max_effort` or a client
`min_effort` that pushes a frontier rule off its pair is now a routing error,
where before it quietly took another astra effort and was labelled qualified.
Neither the shipped nor the deployed configuration sets either.

A pair joins one of these lanes by passing on the whole population its rule
routes, never a slice of it. That is how quota can matter for strict `auto`
again: pressure only chooses between qualified pairs inside a lane, and
today every frontier lane has one. The unused `shift_with` on `hard` went
too; strict admission ignores it.

## grok effort for auto-conserve (2026-09-23)

`auto-conserve` sends grok-4.6 the work `auto` gives astra below the hardest
band. grok had only ever been configured and tested at `(low)`, picked for a
2.5 s first token. The `conserve` step in `evals/ADMISSION.md` asked whether a
higher effort earns its time: 17 frontier-labelled cases below the xhigh band,
all graded by their programmatic checks, 2 samples per route. Nothing was sent
to OpenAI, since that allowance was nearly spent; the astra reference is the
astra `(low)` scores earlier steps stored for 15 of the cases.

| grok-4.6 | mean, 17 cases | 95% CI | median latency | median out tokens |
|---|---|---|---|---|
| `(low)` | 9.15 | [8.52, 9.79] | 16.5 s | 799 |
| `(medium)` | 9.28 | [8.78, 9.79] | 46.1 s | 2555 |
| `(high)` | 9.34 | [8.89, 9.80] | 74.9 s | 4166 |

Paired against `(low)`, over all 17 cases neither higher effort is separable:
high +0.19 [-0.10, +0.53], p = 0.24. The gain sits on the six difficulty-3
cases, which is the population the `hard` rule routes: high +0.75
[+0.17, +1.39], paired bootstrap p = 0.034, better on 3 and worse on none.
Medium there is +0.54 [-0.20, +1.32], p = 0.14. On the 11 difficulty-2 cases
high is -0.12 [-0.30, 0.00] and takes three to five times as long. Six cases is
a small population, so treat the hard-band result as the best evidence there
is, not a settled one.

Verdict: in the `conserve` ruleset `hard` goes to grok `(high)` on the new
`frontier_grok_high` route, and every other rule that moved off astra stays on
grok `(low)`. Against the stored astra `(low)` scores, grok `(high)` sits
+0.17 [-0.40, +0.64] on the 15 shared cases and 8.95 against 9.03 on the six
hard ones. None of this qualifies grok for a lane, and those rules still record
`evidence: weak`: the `auto` rules use astra at medium and high, not low.

glm-5.3 `(high)` was a fourth route. It returned empty replies on two of the
first nine cases, which stops a run, and was dropped.

## Evidence so far

Measured through the proxy on 2026-09-19. One streamed 400-token coding prompt
and one tool-call probe per model, so treat each number as a single sample.
Every model returned a correct tool call.

| Model | First token | Speed | Effort control that works | Vision | Context |
| --- | --- | --- | --- | --- | --- |
| glm-5.3-flash (in pool) | 0.6 s | ~157 tok/s | `(none)` | no | 128K |
| gpt-6-astra (in pool) | 1.6 s | ~32 tok/s | low to max | yes | 1.05M |
| gemma4-31b | 0.8 s | ~290 tok/s | has no reasoning by default | yes | 256K |
| glm-5.3 | 0.4 s | ~190 tok/s at `(none)` | `(none)` turns thinking off. Default effort is max, and it spent all 400 tokens thinking and returned nothing | not stated | 1M |
| kimi-k3 | 0.8 s | ~84 tok/s | `(none)` nearly off, `(low)` light | yes | 1M |
| grok-4.6 | 2.5 s at `(low)`, 11.5 s at default | ~41 tok/s | `(low)` | not stated | 500K |
| gpt-5.6-luna | 1.6 to 2.2 s | ~53 tok/s | none to max; low and none looked the same | yes | 1.05M |

Vendor claims, not independently checked here:

- GLM-5.3: Z.ai calls it their flagship and the strongest open-weights coding
  model, with large gains on long agentic tasks.
- Kimi K3: built for long coding sessions and agentic knowledge work; reads
  images and video. An earlier in-house writing comparison and EQ-Bench both put
  it at or near the top for creative prose.
- Gemma 4 31B: MMLU Pro 85.2, GPQA Diamond 84.3, LiveCodeBench v6 80.0, MMMU Pro
  76.9, MRCR v2 66.4 (Google's numbers).
- Grok 4.6: xAI calls it their most intelligent and fastest model, for code and
  chat.
- GPT-5.6 Luna: OpenAI's cheapest 5.6 model, "optimized for cost-sensitive
  workloads". Sol and Terra sit above it.

No independent benchmark data was gathered for these five. The admission test
below is how this project gets its own.

## How many tiers

Three tiers and one specialist lane. No more.

More candidates make a router worse unless the models differ in ways that
matter, and the classifier is right about the tier roughly nine times in ten
with two tiers. Each extra lane adds a boundary it can get wrong. So a lane has
to pay for itself with a measured difference.

| Lane | Takes | Today | Proposed | Why |
| --- | --- | --- | --- | --- |
| Fast | difficulty below 1.4, low harm | glm-5.3-flash `(none)` | keep, or gemma4-31b | Gemma is about twice as fast and reads images. Today every image request goes to Astra, even a trivial one |
| Mid (new) | difficulty 1.4 to 2.4, low harm | gpt-6-astra `(low/medium)` | glm-5.3 `(none)` | This band is most of the Astra traffic. Astra streams at ~32 tok/s. GLM-5.3 streams about six times faster and is claimed to be a strong coder. This is the largest practical gain on offer |
| Frontier | difficulty 2.4 and up, or harm 0.8 and up | gpt-6-astra | keep | Cheap models failed the faithful-summary case outright. Harm stays a frontier-only rule |
| Creative prose (specialist) | task is creative-prose | whatever the difficulty picks | kimi-k3 `(none)` or `(low)` | The one task where a non-frontier model is reported to beat the frontier one. The classifier already emits this label, so the lane needs no new question |

Effort. The outcome benchmark found no gain from extra effort on either pool
model, and xhigh doubled Astra's latency. So each lane gets one effort, and the
frontier lane gets two: `low` by default and `high` at difficulty 2.4 and up.
Keep `xhigh` only at the very top of the scale until a run on hard cases says
otherwise. That earlier run was small and mostly easy cases.

Not a lane:

- grok-4.6 is an overflow for the frontier lane on a different subscription. The
  classifier should never choose it. See fallbacks below.
- gpt-5.6-luna measured the same speed as its larger siblings. Its selling
  point is price, which a flat subscription does not pay. It belongs in the pool
  only if the admission test shows it matches glm-5.3 in the mid band, where it
  would be the mid-lane fallback on a second subscription.

## Fallbacks (built)

Built on 2026-09-19, along with providers, the circuit breaker and
quota-aware routing. README.md has the configuration reference and DESIGN.md
has a "Fallbacks and quota" section on the choices. What shipped:

- `providers:` in `router.yaml`. A provider is one account or subscription,
  and every model names one. Rate limits, outages and quota belong to it.
- Named `routes:`. Each has a primary and an ordered `fallbacks` list. A rule
  says `use: {route: name}`, and `use: {model, effort}` still works.
- The forwarder moves to the next entry on 429, any 5xx, a refused connection
  or a timeout before the first byte, and never after a byte has been sent.
  It sets `X-Router-Fallback: <n>` and logs why the earlier entries failed.
- One difference from the plan above: a conversation served by a fallback **is**
  pinned, to the model that actually answered. Leaving the pin on a failing
  primary would send every later turn into the same failure and would throw
  away the serving provider's prompt cache. The decision row keeps the
  intended primary and the reason separately.
- A circuit breaker per provider: three consecutive failures open it for 120 s,
  doubling to a cap, then one half-open trial. `GET /router/providers`.
- Both config guards from the plan: `efforts` already excluded levels that do
  not work, and `default_effort` is now a model field. A model that declares
  one is never sent bare, and the value is validated against its `efforts`.

The lane names shipped as `fast` and `frontier_low` through `frontier_xhigh`,
matching the rules that already existed. Every lane has an empty `fallbacks`
list today, because there is one model per provider. Adding the models below
is a config edit.

Proposed order once the admission test passes: frontier is astra, then
grok-4.6 `(low)`. Mid is glm-5.3, then luna `(low)`, then astra `(low)`. Fast
is the fast model, then the mid model. Creative is kimi-k3, then the frontier
route. Mark an entry `equivalent: true` only when the admission test says it
is as good as the primary for that lane; that flag is the only thing that lets
quota pressure promote it ahead of the primary.

## Admission test

A model earns a lane by running the outcome benchmark on that lane's cases. It
passes if its mean score is within 0.5 points of the incumbent and it is at
least 30% faster, or sits on a different subscription. A specialist lane has a
higher bar: it must beat the incumbent by 0.5 or more in blind pairwise judging.
Programmatic checks override the judge, because the judge disagreed with checks
on a third of cases.

The proxy slows down after roughly 300 calls in a session, so the test is sized
to fit one session. Two samples per case and route.

| Order | Question | Cases | Routes | Calls |
| --- | --- | --- | --- | --- |
| 1 | Can glm-5.3 or luna hold the mid band? | 12 cases at difficulty 1 to 2, mostly code | glm-5.3 `(none)`, luna `(low)`, astra `(low)` | 72 |
| 2 | Do the cheap models fail faithful work? | 5 faithful-summary and extraction cases | glm-5.3, gemma4, kimi-k3 | 30 |
| 3 | Is gemma4 a better fast model? | 10 easy cases, plus 4 with images | gemma4, glm-5.3-flash | 48 |
| 4 | Does kimi-k3 win creative prose? | 6 creative cases, judged pairwise | kimi-k3 `(none)`, astra `(low)` | 24 |
| 5 | Is grok-4.6 a safe frontier overflow? | 10 hard cases | grok-4.6 `(low)`; astra from cache | 20 |

That is about 195 model calls and up to 80 judge calls. Step 2 decides whether
the harm rule must also keep requests away from the mid lane.

## Work on the classifier side

The mid lane uses the difficulty score that already exists, so it needs no new
question. It does need new labels. `acceptable_tiers` in `cases.yaml` knows only
fast and frontier, so the outcome run in step 1 has to supply mid-tier labels
before `tune.py` can place the two cutoffs. Difficulty lands within tolerance
on about 75% of cases. With three tiers, more decisions sit near a cutoff.
Expect tier accuracy to drop a few points, and judge the change on
under-routing and on latency at the chosen route.

## Order of work

1. ~~Add `fallbacks` and `default_effort` to the config and the forwarder,
   with tests.~~ Built. Quota-aware routing came with it, so a second
   subscription can be added and load can be shifted between the two without
   more code.
2. Run admission steps 1 and 2. Add the mid lane if glm-5.3 passes.
3. Run steps 3 to 5 in a second session. Add what passes.
4. Relabel, rerun `run_eval.py`, run `tune.py`, and review a week of feedback
   before adding anything else.

One note for step 2 and after. Every lane's difficulty cutoff can now be made
sensitive to a provider's quota pressure. Tune the calm numbers first, with
every quota source disabled, and only then decide what `max_shift` each rule
should carry. The tuner searches the base thresholds and does not touch the
shifts.
