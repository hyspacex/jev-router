# jev-router: test plan

The router makes two decisions per conversation: which model, and how much reasoning effort. This plan measures how often those decisions are right, and describes how benchmark results and your own feedback change the routing policy over time.

## What "right" means

A route is right when it is the cheapest one that still gives a good answer. Two kinds of error matter, and they are not equal.

- Under-routing: a request that needed the strong model went to the fast one. You get a bad answer and have to redo it. This is the expensive mistake.
- Over-routing: an easy request went to the strong model at high effort. It costs quota and time, and nothing else.

The policy is tuned to keep under-routing low first, then to reduce over-routing.

## What the numbers can and cannot show

Every rate in this plan is reported with a Wilson 95% interval, following
"Adding Error Bars to Evals" (arXiv 2411.00640). The Wilson interval rather
than the normal approximation, because these rates sit near 0 and near 1 where
the normal approximation runs off the end of the scale.

The case count sets what a run can detect. On the 171-case set:

| comparison | smallest difference it can show |
| --- | --- |
| two independent runs, all 171 cases, a rate near 85% | about 11 points |
| two independent runs, the 51-case held-out split | about 20 points |
| two independent runs, a 16-case slice | about 36 points |
| the same cases under two configurations, paired | about 3 points |

One case is 0.6 points on the full set, 2.0 on the held-out split and 6.3 on a
16-case slice. So a slice table is for seeing *where* a router fails, never for
deciding that one config beats another.

The paired row is why every experiment in `evals/EXPERIMENTS.md` reports a
paired bootstrap p-value beside the interval. Two configs run on the same 171
cases share all the case-to-case variance, and removing it is worth roughly a
factor of four in sensitivity. Read the interval to know how well the router
does, and the paired test to know whether a change did anything.

Three things the numbers cannot show, however many cases are added:

- **Whether the labels are right.** Layer 3 is the only check on that, and it
  covers a quarter of the set.
- **Whether the case set resembles real traffic.** The cases are written, not
  sampled. `evals/import_public.py` samples public datasets to give a partial
  answer, and those results are reported separately as an out-of-distribution
  slice, never mixed into the headline.
- **Whether an attack that is not in the set would work.** 27 adversarial
  cases with 8 or 9 per attack goal means the lower bound on "held" is about
  68% even at 100% observed.

## Controls: catching a broken evaluation

Two runs that are supposed to score badly. If either scores well, the harness
is measuring something other than what it claims, and every other number in
this plan is void. Both are flags, and both have a unit test in
`tests/test_evals.py`.

```sh
uv run python evals/run_eval.py --variants router_yaml --control shuffled-labels
uv run python evals/run_eval.py --variants router_yaml --control constant-state
```

- **Shuffled labels.** Every case keeps its request and gets another case's
  labels, as a derangement. Classification accuracy must fall to the rate at
  which a shuffled permutation of this label mix matches itself, which is the
  sum of the squared label frequencies and not 1/k. Measured: task accuracy
  73.7% to 14.0%, against an expected 11.5%.
- **Constant state.** Every case is replaced by one bland request. The router
  is then a constant router, so routing accuracy must fall to the
  majority-class rate. Measured: 63.2% against a majority class of 63.2%, at a
  mean cost of 14.0 because every case routes to the default.

Difficulty accuracy falls less far under both controls (to about 35%) than task
accuracy does. That is the tolerance band: a case with
`difficulty_tolerance: 1` accepts three of four levels, so a third of the scale
is right by construction. It is a floor on that metric, not a leak.

## Slices

Every case carries a `slice`, which is the shape of the request rather than the
kind of work: `chat`, `agentic`, `multiturn`, `longcontext`, `multilingual`,
`adversarial`, `pipeline`. A case has exactly one shape and any of the ten task
labels. `evals/common.py` can infer one, and the inference is what backfilled
the original 86, but the value in the file is what counts.

| slice | n | what it is |
| --- | --- | --- |
| chat | 45 | one person typing into a chat box |
| agentic | 46 | tool schemas, prior tool calls, tool results |
| adversarial | 27 | something in the request argues for its own route |
| multiturn | 16 | a conversation with turns before the latest one |
| multilingual | 16 | the request is not in English |
| longcontext | 11 | 20,000 characters or more |
| pipeline | 10 | a program calling the API with a fixed instruction |

Slices are reported two ways. A per-slice table in every run says where the
router fails. Separately, `tune.py --slice-holdout <name>` removes a whole
shape, tunes without it and reports on it, which is a harder question than the
70/30 row split: a row split leaves near-duplicates of a held-out case in the
tuning half and flatters the held-out number.

The 70/30 row split is kept as well, with the same seed and fraction, so the
numbers stay comparable with earlier rounds.

## Layer 1: does Jev classify requests correctly?

Input: `evals/cases.yaml`, 171 labelled requests covering every task type, four
client shapes, the seven slices above, and deliberate traps (a short message
hiding a hard problem, a long paste with a trivial question, text inside a
paste that tells the router what to pick, non-English, images).

The 10 long-context cases carry a `generated:` block instead of a `request:`
block, because 40,000 characters do not belong in a YAML file by hand.
`evals/generators.py` expands one from its seed, and the same seed always gives
the same bytes. The material is synthetic and original; the question asked
about it is written by hand and is the part the label is about.

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

Measured per state builder, on 171 cases:

| Metric | Target | Measured 2026-09-19 |
| --- | --- | --- |
| Task label accuracy | 85% or better | 73.7% [66.6, 79.7] — missed |
| Difficulty within the case's tolerance | 80% or better | 74.9% [67.9, 80.8] — missed |
| Faithfulness flag accuracy (threshold 0.6) | 85% or better | 70.8% [63.5, 77.1] — missed |
| Same answer across 3 repeats | 95% or better | 97.1% [93.3, 98.7] |
| Adversarial cases where the attack changed the route | 0 | 0 of 27 |
| Decision unchanged under a reworded request | reported | 95.0% [93.1, 96.4] |
| Jev latency, 95th percentile | 1.5 s or less | 0.25 s |
| State size, median tokens | reported, lower is better | 123 |

All three classification targets are missed on the expanded set, and the drop
from the 86-case round (81.4, 86.0, 75.6) is mostly real rather than noise.
`evals/EXPERIMENTS.md` section 11a breaks it down by slice. The short version:
task accuracy on the agentic slice is 54%, and the confusion table shows that
is a taxonomy disagreement rather than a routing failure, because Jev answers
`code-edit` on turns the labels call `agentic-tool-task` and the policy treats
the two identically. Tier accuracy on that slice is 89%.

### Calibration

Reported as one number per question rather than a table of buckets. ECE is the
bin-count-weighted mean gap between the confidence Jev claims and the accuracy
it delivers.

| question | ECE | overconfident by | accuracy below the gate | at or above |
| --- | --- | --- | --- | --- |
| task | 0.129 | +0.129 | 35.3% [17.3, 58.7] | 77.9% [70.7, 83.7] |
| difficulty | 0.084 | -0.053 | 62.2% [46.1, 75.9] | 78.4% [70.6, 84.5] |

The per-bin table is still written underneath, for reading where the
miscalibration sits.

This is what sets `policy.low_confidence.min_confidence`. A gate only earns its
place if the answers it rejects really are worse than the ones it keeps. On
`task` they are, by a wide margin. On `difficulty` they are barely worse, and
Jev is *under*confident there, so a high cutoff rejects answers that were fine.
The cutoff moved from 0.55 to 0.40 on that reading, which fixed 7 over-routes
and moved nothing else. See experiment 14.

Also reported: a confusion table for task labels, and accuracy split by slice,
by client shape and by language.

### Decision stability under perturbation

A separate run rewords each case's latest user message four ways with a fixed
seed, and asks the router again: two transposed letters, the opening paragraph
lowercased with punctuation stripped, a polite filler prefix and suffix, and up
to three one-way synonym swaps from a small fixed table. Nothing below the
first blank line is touched, so a pasted stack trace survives. No model is
asked to rewrite anything, so this costs four Jev calls per case.

```sh
uv run python evals/run_eval.py --variants router_yaml --stability
```

Two numbers, because they answer different questions: how often a single
reworded turn moves the route (95.0%), and how many requests are fragile at all
(85.4% never moved). The cases that move sit on a difficulty cutoff. The route
a user gets should not depend on whether they capitalised the sentence, and for
about one request in seven it does. The pin limits the damage: within one
conversation the route is fixed whatever the user types next.

### Cost-matched random, and the majority class

Two baselines that say whether the router is choosing or merely spending.

- **Cost-matched random**, RouterBench's Zero Router: route to the frontier
  tier with the router's own frontier rate, drawing the effort from the
  router's own mix, averaged over 20 seeded draws. It spends what the router
  spends. Measured 57.3% ± 3.4 against the router's 92.4%, both at a mean cost
  of about 10.8.
- **Majority class**: the best a router that ignores the request can do. 63.2%
  here, which is also exactly what `always gpt-6-astra(medium)` scores, because
  63% of cases accept the frontier tier.

The best state builder becomes the default. Failures are read one by one, because Jev reads questions literally and most errors come from question wording or from noise in the state.

## Layer 2: does the policy pick the right route?

The same run feeds Jev's answers through `policy.py` and compares the chosen model and effort with each case's `acceptable_tiers` and `effort`.

| Metric | Target | Tuning split (120) | Held-out split (51) |
| --- | --- | --- | --- |
| Tier correct | 90% or better | 93.3 [87.4, 96.6] | 90.2 [79.0, 95.7] |
| Under-routed | 5% or less | 0.0 [0.0, 3.1] | 3.9 [1.1, 13.2] |
| Over-routed | 25% or less | 6.7 [3.4, 12.6] | 5.9 [2.0, 15.9] |
| Effort within one level of the label | 80% or better | 93.1 [84.8, 97.0] | 82.8 [65.5, 92.4] |

All four targets are met on both splits, which was not true before the
confidence gate was loosened (experiment 14). The held-out under-route is two
cases of 51, so its interval reaches 13%.

Held out a whole slice at a time, and comparing the shipped policy with a
policy whose numbers were searched without ever seeing that shape:

| held-out slice | n | shipped policy | tuned without this slice |
| --- | --- | --- | --- |
| adversarial | 27 | 96.3 | 85.2 |
| agentic | 46 | 89.1 | 87.0 |
| multiturn | 16 | 87.5 | 87.5 |
| multilingual | 16 | 87.5 | 81.2 |
| longcontext | 11 | 100.0 | 100.0 |
| pipeline | 10 | 100.0 | 90.0 |
| chat | 45 | 93.3 | 84.4 |

Removing `chat` or `adversarial` costs the most, because those two slices are
what hold the cheap end of the ladder down. Remove them and the search raises
the model cutoff, and the held-out slice then over-routes.

Five baselines run alongside for comparison. The Jev router has to beat all of
them on quality kept for cost spent to justify its extra hop. Over all 171
cases it does:

| | tier correct | under | cost |
| --- | --- | --- | --- |
| the tuned Jev router | 92.4 [87.4, 95.5] | 1.2 | 10.8 |
| cost-matched random at the same spend | 57.3 ± 3.4 | 24.3 | 10.9 |
| always gpt-6-astra(medium) | 63.2 [55.7, 70.0] | 0.0 | 14.0 |
| always ollama/glm-5.3-flash(none) | 46.2 [38.9, 53.7] | 53.8 | 1.0 |
| the no-network `rules` decider | 60.2 [52.8, 67.3] | 23.4 | 7.4 |
| majority class | 63.2 | - | - |

## Layer 3: are the labels themselves right? (outcome benchmark)

Labels in layer 1 are a person's opinion of what each request needs. This layer checks them against results.

```sh
uv run python evals/run_outcomes.py --samples 3 --max-calls 400
uv run python evals/run_outcomes.py --apply-labels     # write the clear-cut changes
```

1. Take 30 to 40 cases from `cases.yaml` whose answers can be graded: code with a test or a known fix, a regex with examples, SQL with an expected result, a proof, a summary with facts that must survive, a rewrite with a rubric. The chosen cases and their grading specs are in `evals/outcome_specs.yaml`; a spec is a list of programmatic checks from `evals/checks.py`, a rubric for the blind judge, or both. Anything that runs model output runs it in a temporary directory with a timeout and no network.
2. Run each one on every candidate route: GLM-5.3 Flash at none, low, high, and GPT-6 Astra at low, medium, high, xhigh. Run each route **k times** (`--samples`, default 3). Each sample gets its own cache key, so k samples are k different replies to the same bytes rather than one reply read back k times. The score is their mean, and the per-sample scores and their standard deviation are kept.
3. Grade with a programmatic check where one exists. **The check decides.** The judge still runs when the spec also has a rubric, so the two can be compared, but its score is not mixed in. Record score, latency, output tokens and `finish_reason`.
4. For each case, the cheapest adequate route is the cheapest candidate whose mean score is within 0.5 points (of 10) of the best.
5. **Resample the winner.** Bootstrap over the k samples, 500 draws, taking one sample per route each time and recomputing the winner. Report the route flip rate and the tier flip rate. A case whose tier flips in more than 20% of draws is labelled `unstable` and is never allowed to override a hand label.
6. Where a stable outcome disagrees with the hand label, `evals/outcomes.md` lists the proposed change and the reason. `--apply-labels` writes only the clear-cut ones into `cases.yaml`, marking each changed case with `label_source: outcome`. The rest are left for a person.

### Three numbers that say whether to believe the rest

- **Truncation rate per route.** The candidate token cap was raised from 3,000
  to 8,000. At 3,000 a reasoning model at `xhigh` could run out of tokens
  mid-answer, and that looks identical to "more effort did not help". Recording
  `finish_reason` answers the question either way, and the rate is reported per
  route.
- **Winner flip rate.** With k=1 every "cheapest adequate route" was a single
  draw. The flip rate says how many of those conclusions were coin tosses.
- **Judge against check discordance.** On samples that have both, the share
  where they differ by 2 points or more, and the share where one calls the
  answer a pass at 7 of 10 and the other does not. This is the discount to
  apply to every case graded by the judge alone, which is most of them.

### The upstream is shared and degrades

The proxy serves subscription quota. Under sustained load it does not refuse,
it slows down and starts returning empty replies. So a run carries a budget and
a health check:

- `--max-calls N` stops after N live calls. Cached calls are free and do not
  count.
- The run also stops if a route's **own** median latency triples, comparing the
  first twelve calls on that route with the last twelve. Per route, not pooled:
  a pooled median rises whenever the run moves from a cheap route to an
  expensive one, which is a change in the work and not in the upstream. That
  bug stopped a run after four cases before it was fixed.
- Two empty replies also stop the run.
- Cases run one at a time in priority order, so a stop leaves whole cases
  finished. Everything finished is cached, so rerunning the same command
  resumes.

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

### The 2026-09-19 rerun at k=3, with the cap at 8,000

Ten cases finished, each on all seven routes with three samples per route: 210
candidate samples plus judge calls. The run then hit its budget with the
upstream taking 20 seconds a call on the larger prompts, and stopped. Raising
the token cap changed the cache key, so nothing from the earlier run was
reusable and these numbers are fresh.

**Truncation is 0.0% on every route.** Median output runs 264 to 656 tokens
against a cap of 8,000, and no sample of 210 stopped on `length`. The 3,000-
token cap was not hiding anything, and the earlier effort findings are not an
artefact of it.

**The effort findings hold.** Same signs as before, smaller magnitudes:
glm none to low -0.17, low to high -0.03; astra low to medium +0.22, medium to
high -0.12, high to xhigh -0.27. Read them against the within-route spread,
which is 0.00 to 0.65 over three samples: only the directions are meaningful.

**Winner flip rate is 0.000 on tier and 0 of 10 cases are unstable**, but that
is selection rather than a result. The ten cases that finished are the ten
cheapest to run, and nine scored 10.0 on every route, so their winners could
not have flipped. The machinery is in place; it has not yet been pointed at a
case that could test it.

**Judge against check: they differ by 2 points or more on 32.5% [23.4, 43.2]
of the 83 samples that have both, and disagree on whether the answer passes on
16.9% [10.3, 26.3].** The judge is higher on only 6.0%, and its mean signed
difference is -1.51, so it is biased low by about a point and a half rather
than merely noisy. Thirty of the 43 specs are judge-only, so that bias sits in
most of layer 3, and it runs in the direction that makes the fast model look
worse than it is. That is the discount to apply to every judge-only case.

Three label changes were proposed and **none were applied**. All three rest on
every route scoring 10.0, which does not show the fast model is good enough; it
shows the grading spec cannot tell the routes apart. Those three specs need a
harder rubric.

## Layer 3b: public traffic, as an out-of-distribution check

The cases in `cases.yaml` were written, not sampled, which means they can only
tell you how the router does on requests someone thought of. `evals/import_public.py`
samples datasets whose licence allows it and writes them to
`evals/cases_public.yaml`.

```sh
uv sync --group evals
uv run python evals/import_public.py --limit 25
uv run python evals/run_eval.py --variants router_yaml --public evals/cases_public.yaml
```

`cases_public.yaml` is git-ignored and must stay that way. WildChat is real
conversations with real people in them, and republishing a sample of it from
this repository is not something the licence or good sense allows. The script
drops any row the dataset flags as toxic and any row matching an email address,
a phone number, an IBAN or a card-shaped number, and it writes no labels of its
own. Labels go in a separate `cases_public_labels.yaml`, so the requests can be
resampled without losing the labelling work.

These results are reported **separately** and never mixed into the headline.
They are the out-of-distribution slice, and their labels are `llm-assisted`
rather than hand-written.

Sampled 2026-09-19, 25 cases each from four sources:

| | task acc | difficulty | tier correct | under | over | cost |
| --- | --- | --- | --- | --- | --- | --- |
| written cases (171) | 73.7 [66.6, 79.7] | 74.9 [67.9, 80.8] | 92.4 [87.4, 95.5] | 1.2 | 6.4 | 10.8 |
| public sample (100) | 85.0 [76.7, 90.7] | 86.0 [77.9, 91.5] | 86.0 [77.9, 91.5] | 5.0 | 9.0 | 7.8 |

Classification is *easier* on the public sample and routing is *harder*. Both
make sense. Public requests are more homogeneous within a source, so the task
label is easier to get right; but the labels are one LLM's opinion rather than
a careful person's, and the tier is the thing that opinion is least reliable
about.

| source | n | tier correct | under | over | cost |
| --- | --- | --- | --- | --- | --- |
| BFCL | 25 | 96.0 [80.5, 99.3] | 0.0 | 4.0 | 3.3 |
| WildChat | 25 | 88.0 [70.0, 95.8] | 0.0 | 12.0 | 7.6 |
| LongBench v2 | 25 | 80.0 [60.9, 91.1] | 20.0 | 0.0 | 10.8 |
| MGSM | 25 | 80.0 [60.9, 91.1] | 0.0 | 20.0 | 9.8 |

Two caveats on this table, both of which make it weaker than it looks:

- **LongBench's 20% under-routing is partly an import artefact.** The importer
  truncates a message at 60,000 characters, and in 22 of the 25 rows the actual
  question sat past the cut. Those cases were labelled from the document type
  and the stated domain rather than from a question anyone could read. Fix the
  truncation before drawing a conclusion from that row.
- **`lmsys/arena-human-preference-55k` could not be sampled.** It needs a Hub
  login or accepted terms, so it was skipped rather than worked around. That
  removes the one source that would have carried preference-labelled pairs.

ECE on the public sample is 0.072 on `task` and 0.387 on `difficulty`, both in
the underconfident direction. Jev is much more accurate on these requests than
it claims to be, which is the opposite of its behaviour on `task` on the
written set, and another reason not to set a confidence gate high.

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
| Question wording or state builder changed | Layers 1, 2, and both controls |
| Policy numbers changed | Layer 2, plus feedback replay |
| Model added or upgraded | Layers 2, 3 |
| Jev version changed (`jev-latest` moves) | Layers 1, 2, and the calibration numbers |
| Cases added | Layer 1 as a re-baseline before any experiment |
| The harness itself changed | Both controls, then everything |
| Code changed | Layer 6 |

Two habits that go with the table. Re-baseline before experimenting: adding
cases moves every number, and an experiment measured against the old baseline
is measuring the case set. And run the controls after touching the harness, not
before: they are the only thing that catches a scorer that stopped reading the
labels.

Pin `jev-1.13.0` in `router.yaml` instead of `jev-latest`, so a model update on TypeSafe's side cannot change routing without a rerun.
