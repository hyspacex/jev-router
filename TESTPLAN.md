# jev-router: test plan

The router makes two decisions per conversation: which model, and how much reasoning effort. This plan measures how often those decisions are right, and describes how benchmark results and your own feedback change the routing policy over time.

## What "right" means

A route is right when it is the cheapest one that still gives a good answer. Two kinds of error matter, and they are not equal.

- Under-routing: a request that needed the strong model went to the fast one. You get a bad answer and have to redo it. This is the expensive mistake.
- Over-routing: an easy request went to the strong model at high effort. It costs quota and time, and nothing else.

The policy is tuned to keep under-routing low first, then to reduce over-routing.

## Layer 1: does Jev classify requests correctly?

Input: `evals/cases.yaml`, 70 to 90 labelled requests covering every task type, four client shapes, multi-turn conversations, and deliberate traps (a short message hiding a hard problem, a long paste with a trivial question, text inside a paste that tells the router what to pick, non-English, images).

Run:

```sh
export TYPESAFE_API_KEY=...
uv run python evals/run_eval.py --group final --repeats 3
```

It sends every case to the real Jev API once per variant, three times each to
measure how stable the answers are. A variant is one state builder plus one set
of question wordings, defined in `evals/variants.yaml`. Answers are cached by a
hash of the request; the repeats always go to the network. `--no-cache` forces
every call live. Results land in `evals/results/<timestamp>/summary.md` and
`per_case.jsonl`.

Measured per state builder:

| Metric | Target | Measured 2026-09-18 |
| --- | --- | --- |
| Task label accuracy | 85% or better | 81.4% — missed |
| Difficulty within the case's tolerance | 80% or better | 86.0% |
| Faithfulness flag accuracy (threshold 0.6) | 85% or better | 75.6% — missed |
| Same answer across 3 repeats | 95% or better | 98.8% |
| Trap cases where pasted instructions changed the answer | 0 | 0 |
| Jev latency, 95th percentile | 1.5 s or less | 0.25 s |
| State size, median tokens | reported, lower is better | 123 |

The two misses are explained in `evals/EXPERIMENTS.md`. Task accuracy is
measured against one person's labels and several of the remaining misses are
disagreements that route to the same place. The faithfulness number is
`harm_if_wrong` used as a proxy; the dedicated `needs_faithfulness` question
reaches 87.2% at threshold 0.7 but made routing worse, so no alias asks it.

Also reported: a confusion table for task labels, accuracy split by client shape and by language, and a calibration check (when Jev says 0.8 confidence, is it right about 80% of the time?). The calibration result sets the low-confidence cutoff in the policy.

The best state builder becomes the default. Failures are read one by one, because Jev reads questions literally and most errors come from question wording or from noise in the state.

## Layer 2: does the policy pick the right route?

The same run feeds Jev's answers through `policy.py` and compares the chosen model and effort with each case's `acceptable_tiers` and `effort`.

| Metric | Target | Tuning split | Held-out split |
| --- | --- | --- | --- |
| Tier correct | 90% or better | 91.9% | 87.5% — missed |
| Under-routed | 5% or less | 0.0% | 4.2% |
| Over-routed | 25% or less | 8.1% | 8.3% |
| Effort within one level of the label | 80% or better | 93.1% | 93.8% |

The held-out miss is three cases: one under-route (`quick-borrow-checker-misleading-75`, a Rust borrow-checker problem prefaced with "quick one") and two over-routes where the low-confidence gate fired on easy work. Scored against the outcome benchmark's revised tiers instead of the hand labels, both splits reach 91.9% and 91.7% with no under-routing.

Three baselines run alongside for comparison: always Astra at medium, always GLM, and the no-network rules decider. The Jev router has to beat all three on a combined score (quality kept, cost saved) to justify its extra hop. Over all 86 cases it does: tier correct 90.7% at cost 9.1, against 59.3% at 14.0, 54.7% at 1.0 with 45.3% under-routing, and 62.8% at 6.2 with 23.3% under-routing.

## Layer 3: are the labels themselves right? (outcome benchmark)

Labels in layer 1 are a person's opinion of what each request needs. This layer checks them against results.

```sh
uv run python evals/run_outcomes.py                    # every spec, cached
uv run python evals/run_outcomes.py --apply-labels     # write the clear-cut changes
```

1. Take 30 to 40 cases from `cases.yaml` whose answers can be graded: code with a test or a known fix, a regex with examples, SQL with an expected result, a proof, a summary with facts that must survive, a rewrite with a rubric. The chosen cases and their grading specs are in `evals/outcome_specs.yaml`; a spec is a list of programmatic checks from `evals/checks.py`, a rubric for the blind judge, or both mixed by `judge_weight`. Anything that runs model output runs it in a temporary directory with a timeout and no network.
2. Run each one on every candidate route: GLM-5.3 Flash at none, low, high, and GPT-6 Astra at low, medium, high, xhigh.
3. Grade with a programmatic check where one exists, otherwise with a blind judge (a strong model at high effort, one rubric per case). Record score, latency, and output tokens.
4. For each case, the cheapest adequate route is the cheapest candidate scoring within 0.5 points (of 10) of the best. Write these to `evals/outcomes.json`.
5. Where the outcome disagrees with the hand label, `evals/outcomes.md` lists the proposed change and the reason. `--apply-labels` writes only the clear-cut ones into `cases.yaml`, marking each changed case with `label_source: outcome`. The rest are left for a person.

This is also where existing benchmark knowledge enters. An earlier in-house model comparison found that coding rank does not predict writing rank, that cheap models are unreliable for faithful summaries, and that proofreading is easy for every model. Those findings seed the first policy. Layer 3 confirms or overturns them for these two models specifically. Public benchmark scores for a new model only set its starting position in the table; it earns its rows by running this layer.

Run layer 3 when a model is added, when a model version changes, and otherwise once a quarter. It costs roughly 40 cases times 7 routes, about 280 calls, plus a judge call for each case that has a rubric.

Results of the 2026-09-18 run, over the 25 cases that completed (`evals/outcomes.md`):

| route | mean score | wins outright | adequate on | median latency |
| --- | --- | --- | --- | --- |
| glm-5.3-flash(none) | 8.14 | 9/25 | 11/25 | 4.0 s |
| glm-5.3-flash(low) | 7.71 | 10/25 | 11/25 | 4.0 s |
| glm-5.3-flash(high) | 7.35 | 10/25 | 11/25 | 5.4 s |
| gpt-6-astra(low) | 9.00 | 17/25 | 19/25 | 12.2 s |
| gpt-6-astra(medium) | 9.18 | 20/25 | 21/25 | 13.2 s |
| gpt-6-astra(high) | 9.10 | 18/25 | 20/25 | 15.2 s |
| gpt-6-astra(xhigh) | 9.12 | 18/25 | 20/25 | 27.0 s |

Two findings changed the policy. GLM's effort levels do not help: none to low is -0.43 points of 10 on average and low to high a further -0.36, so `router.yaml` now uses the fast model only at `effort: none`. Astra's effort barely moves the score above `low` (low to medium +0.18, medium to high -0.08, high to xhigh +0.01) while xhigh doubles the latency, so xhigh is reserved for the top of the difficulty scale.

Eighteen of the 25 cheapest-adequate routes agreed with the hand label. Three clear-cut disagreements were written back into `cases.yaml` with `label_source: outcome`: `summarise-adversarial-doc-69` and `quick-drug-interaction-73` moved to fast, `design-naming-bikeshed-24` moved to frontier. Four more are listed in `evals/outcomes.md` for a person to read.

## Layer 4: feedback from real use

Every routed response carries an `X-Router-Decision` id. You record a verdict with one command:

```sh
jev-router feedback last too_weak --model gpt-6-astra --effort high --note "missed the race condition"
```

Verdicts are `right`, `too_weak`, `too_strong`, and `too_slow`. The decision log keeps Jev's answers, the computed features, the rule that fired, and a hash of the config, but never the message text. That is enough to replay the policy against past decisions.

Two passive signals are also logged, since most turns will never get an explicit verdict: a conversation that restarts within ten minutes with an explicit stronger model (a likely under-route), and a request that was regenerated.

Shadow mode comes first. For a week the router logs what it would have chosen while still sending everything to the default. You grade 50 of those decisions by hand. This measures accuracy on your real traffic, which the synthetic cases can only approximate.

## Layer 5: tuning loop

```sh
uv run python evals/tune.py --variant router_yaml --samples 5000
```

It reads three sources: the cached answers in `evals/results/<run>/per_case.jsonl`, `evals/outcomes.json`, and the `decisions` and `feedback` tables in the sqlite log.

1. Each feedback row with a `better_model` or `better_effort` becomes a replayable case (stored answers plus features, with your correction as the label). Feedback cases count three times as much as synthetic ones, since they come from real work.
2. The tool searches the policy's numeric settings: the difficulty cutoffs between effort levels, the difficulty cutoff between models, the confidence floor, and the harm and faithfulness thresholds. It looks for the setting with the lowest total spend that keeps under-routing at 5% or less, where total spend is quota plus the cost of the under-routes. One under-route is priced at 60 quota units (`UNDER_ROUTE_COST` in `tune.py`): a redo on the frontier model at high effort, counted three times because the person's time is worth more than the quota. Without that term the search happily trades an under-route for half a quota unit.
3. It tunes on 70% of cases and reports on the other 30%, so a change that only fits the tuning set is visible.
4. It prints a proposed diff to `router.yaml` with before and after metrics. It never edits the file. You apply the change, and the config hash in the log marks the boundary between policy versions.
5. Changes that tuning cannot make (a new question, new wording, a new task label, a new state builder) are made by hand, one at a time, and must pass layer 1 again before going live.

Review schedule: weekly for the first month, then monthly. Stop tuning a setting when two reviews in a row propose no change.

## Layer 6: the service itself

Unit tests (no network) cover features, state builders, rule matching, effort rewriting, pinning, context overflow, fallback, shadow mode, feedback, and byte-for-byte pass-through of non-alias requests and streams.

Live smoke test through the real proxy:

1. A named model passes through unchanged.
2. `auto` with a trivial question goes to GLM.
3. `auto` with a hard coding request goes to Astra at high effort.
4. A streamed `auto` response arrives in order.
5. A second turn reuses the pin and makes no Jev call.
6. With the Jev key unset, the request still succeeds on the default model.
7. With Jev forced to time out, added latency stays under the timeout plus 100 ms.
8. 20 concurrent `auto` requests complete without errors.

## When to rerun what

| Event | Rerun |
| --- | --- |
| Question wording or state builder changed | Layers 1, 2 |
| Policy numbers changed | Layer 2, plus feedback replay |
| Model added or upgraded | Layers 2, 3 |
| Jev version changed (`jev-latest` moves) | Layers 1, 2 |
| Code changed | Layer 6 |

Pin `jev-1.13.0` in `router.yaml` instead of `jev-latest`, so a model update on TypeSafe's side cannot change routing without a rerun.
