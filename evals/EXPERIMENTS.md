# Experiment log

Every change to the classifier, in the order it was made, with the numbers
before and after. One change per experiment, each measured against the
previous best.

Method: `evals/run_eval.py`, 86 cases from `cases.yaml`, three Jev calls per
case, `jev-1.13.0`. Cases are split 70/30 by seed 20260918, stratified by task
label. The tuning split is 62 cases, the held-out split 24. Tuning decisions
were made on the tuning split; the held-out column is reported but was not
used to choose anything.

Metrics: **task** is task-label accuracy, **diff** is difficulty within the
case's tolerance, **faith** is the faithfulness flag at threshold 0.6,
**tier** is the routed tier being in `acceptable_tiers`, **under** and **over**
are the two routing errors, **cost** is the mean of the relative route costs in
`evals/common.py` (glm none = 1, astra medium = 14).

Reproduce any row with:

```sh
export TYPESAFE_API_KEY=...
uv run python evals/run_eval.py --variants <name> --repeats 3
```

## Starting point

`summary_v1`, the seven-category `task` question, the difficulty levels and the
policy ladder the router shipped with. Variant `before_summary_v1` pins that
config so it can be re-run beside the current one.

| split | task | diff | faith | tier | under | over | cost |
| --- | --- | --- | --- | --- | --- | --- | --- |
| tuning | 69.4 | 59.7 | 80.6 | 67.7 | 1.6 | 30.6 | 12.9 |
| held-out | 66.7 | 70.8 | 66.7 | 75.0 | 4.2 | 20.8 | 15.2 |

Four of the seven TESTPLAN targets were missed. Reading the failures showed
three separate problems, which became the first three experiments:

1. Multi-turn conversations were classified almost at random (task accuracy
   16.7% over the six multi-turn cases), because the state was only the latest
   user message, and on those turns that message is "yes do it" or "continue".
2. Difficulty piled up between 1.0 and 1.5. A variable rename scored 1.11 and a
   group-theory proof scored 1.37. The level descriptions were about how long
   the answer is, not how much thinking it takes.
3. Many task "errors" were not errors. `other` cases went to `analysis`,
   `design-or-review` went to `code`. The seven categories in the question and
   the ten labels in `cases.yaml` do not line up, and the mapping between them
   was doing the damage.

## 1. State builders

Question wording held fixed at the shipped seven-category version. Only the
state changed.

| variant | task | diff | tier | under | over | what changed |
| --- | --- | --- | --- | --- | --- | --- |
| summary_v1 | 69.4 | 59.7 | 67.7 | 1.6 | 30.6 | baseline |
| last_message_only | 67.7 | 56.5 | 67.7 | 1.6 | 30.6 | no facts, no system prompt |
| tail_v1 | 75.8 | 56.5 | 71.0 | 0.0 | 29.0 | last three turns verbatim |
| request_thread_v1 | 74.2 | 58.1 | 72.6 | 0.0 | 27.4 | earlier user message always included |
| **continuation_aware_v1** | **75.8** | **61.3** | 69.4 | 0.0 | 30.6 | earlier message only when the latest one continues it; pasted bulk in its own field |
| labelled_v1 | 69.4 | 58.1 | 67.7 | 1.6 | 30.6 | pasted bulk in its own field, nothing else |
| code_aware_v1 | 74.2 | 61.3 | 69.4 | 0.0 | 30.6 | as continuation_aware_v1, paste cut by keeping signature and error lines |

Kept: `continuation_aware_v1`, +6.4 task and +1.6 difficulty over the baseline.

What the multi-turn cases did, case by case (label, then what each builder
answered):

| case | label | summary_v1 | request_thread_v1 | continuation_aware_v1 |
| --- | --- | --- | --- | --- |
| code-multiturn-yes-do-it-07 | code / 2 | other / 1.40 | code / 1.94 | code / 2.16 |
| agent-monorepo-dep-bump-32 | agentic / 3 | other / 1.59 | analysis / 2.44 | agentic / 2.44 |
| creative-novel-continuity-47 | writing / 2 | other / 0.87 | writing / 1.48 | writing / 1.54 |
| design-latest-turn-changes-task-28 | code / 0 | writing / 1.00 | agentic / 1.05 | writing / 1.00 |

`request_thread_v1` pastes the earlier message in every time, which fixes the
"yes do it" turns and breaks case 28, where the latest turn changes the subject
to a typo fix. The detection in `continuation_aware_v1` keeps both: the earlier
request travels only when the latest message is an approval or is much shorter
than the request it follows.

`labelled_v1` isolates the "pasted bulk in its own field" change on its own: it
made no difference without the question wording that refers to that field, which
is experiment 2.

`code_aware_v1` cut the paste by keeping signatures and error lines instead of
the head and the tail. It was 1.1 points of difficulty worse. Deleted from
`src/`, kept in `evals/exp_builders.py`.

## 2. Naming the state fields in every question

`e2_fieldnames`. Same builder, questions rewritten to say which field to judge,
to say that `material_the_user_pasted` is data, and to say that instructions
inside it are to be ignored.

| split | task | diff | tier | under | over | cost |
| --- | --- | --- | --- | --- | --- | --- |
| tuning, before | 75.8 | 61.3 | 69.4 | 0.0 | 30.6 | 13.2 |
| tuning, after | 79.0 | 59.7 | 71.0 | 0.0 | 29.0 | 12.6 |

Kept: +3.2 task. Difficulty moved 1.6 the wrong way, inside the noise on 62
cases, and experiment 4 replaced that question anyway.

## 3. Ten categories instead of seven

`e3_tencat`. The `task` question now offers the ten categories `cases.yaml`
labels with, and the rules name those categories.

| split | task (own space) | task (collapsed to 7) | tier | over |
| --- | --- | --- | --- | --- |
| seven-category question | 79.0 | 79.0 | 71.0 | 29.0 |
| ten-category question | 80.6 | 82.6 | 72.6 | 27.4 |

Kept. This was the decision the brief asked for: change the question's
categories rather than map labels for scoring. Two reasons the numbers agree
with:

- Ten-way accuracy (80.6%) beat seven-way accuracy (79.0%), even though ten
  categories is the harder question. Collapsed onto the same seven for a
  like-for-like comparison it is 82.6% against 79.0%. The ten are more nearly
  mutually exclusive, which is what the Jev docs ask for.
- The seven collapse `high-stakes-writing`, `everyday-polish`,
  `creative-prose` and `summarise-or-extract` into one `writing`, and that
  distinction decides the route: every `high-stakes-writing` case is labelled
  frontier and most `everyday-polish` cases are labelled fast.

## 4. Difficulty levels

Three wordings, on top of experiment 3.

| variant | tuning diff | held-out diff | tier | over | cost |
| --- | --- | --- | --- | --- | --- |
| e3_tencat (levels as shipped) | 59.7 | 75.0 | 72.6 | 27.4 | 12.8 |
| e4_difficulty_v2 (levels as situations) | 80.6 | 83.3 | 80.6 | 19.4 | 12.6 |
| **e4_difficulty_examples** (`what` + `examples` objects) | **85.5** | **87.5** | 80.6 | 19.4 | 13.3 |
| e4_difficulty_5lvl (five levels) | 54.8 | 62.5 | 71.0 | 29.0 | 19.5 |

Kept `e4_difficulty_examples`: +25.8 difficulty on the tuning split, +12.5 on
the held-out split, the largest single gain in this work. Two things did it:

- Level 0 now covers mechanical work ("rename a variable", "fix a typo"), not
  only short answers. The old level 0 said "a single fact, a short definition",
  so every small edit fell into level 1 and nothing ever landed in level 0.
- The score docs say to replace level strings with `what` and `examples`
  objects when answers cluster between levels. They did cluster, and it was
  worth a further 4.9 points over the plain rewrite.

Five levels was much worse, and the mean cost nearly doubled: the rules on a
five-level scale no longer mean what they meant on a four-level one, and the
label space is four levels, so the rescaling loses information. Dropped.

The examples were written before the run and include shapes that appear in the
case set ("write a limerick", "read the last line of a log"). That is a real
overfitting risk. The held-out split moved the same way as the tuning split
(+12.5 against +25.8), so the wording generalises to cases it was not written
against, but this is the row to re-check first if the numbers ever drift.

## 5. A question about faithfulness of its own

`e5_faith_q` adds a `needs_faithfulness` noul beside `harm_if_wrong`, scored
against the `needs_faithfulness` label.

| signal | accuracy at 0.6 | best threshold | accuracy there |
| --- | --- | --- | --- |
| harm_if_wrong | 77.9 | 0.7 | 77.9 |
| needs_faithfulness | 80.2 | 0.7 | 87.2 |

The dedicated question is the better predictor of the label. It earned no place
in the policy: every ladder that routed on it came out worse than one routing
on `harm_if_wrong` (tier 95.2 against 96.8, over 4.8 against 3.2, at the same
zero under-routing). It is defined in `router.yaml` with a comment saying so,
and no alias asks it.

## 6. The shape of the state

All on top of experiment 5.

| variant | task | diff | faith | tier | over | what changed |
| --- | --- | --- | --- | --- | --- | --- |
| e5_faith_q | 80.6 | 85.5 | 83.9 | 80.6 | 19.4 | baseline |
| e6_facts_first | 80.6 | 85.5 | 83.9 | 80.6 | 19.4 | computed facts before the text |
| e6_no_facts | 79.0 | 85.5 | 82.3 | 82.3 | 17.7 | no computed facts at all |
| **e6_no_system_prompt** | **82.3** | 85.5 | **85.5** | 80.6 | 19.4 | system prompt left out |
| e6_one_string | 80.6 | 85.5 | 83.9 | 80.6 | 19.4 | every field flattened into one labelled string |
| e6_squeeze_only | 80.6 | 82.3 | 85.5 | 79.0 | 21.0 | paste folded back into the request field |
| e6_code_aware | 80.6 | 83.9 | 83.9 | 80.6 | 19.4 | paste cut by keeping signature and error lines |

Kept: leave the system prompt out. +1.7 task and +1.6 faith on the tuning
split, +4.2 task on the held-out split. The client's system prompt is a long
constant that says nothing about this request; the tool names in the facts
already carry the part that mattered. Folded into `continuation_aware_v1`.

Rejected, with what each one shows:

- **Facts first or last made no difference at all**, to three decimal places.
  Field order in the state is not a lever.
- **Dropping the facts** cost 1.6 task accuracy. The counts are worth their
  space.
- **One flat string** was no better than named fields, and named fields are
  what let the questions point at one part of the state, so the dict stays.
- **Folding the paste back** into the request field cost 3.2 difficulty and 1.6
  tier. The separate `material_the_user_pasted` field earns its place, but only
  once the questions name it (compare `labelled_v1` in experiment 1, which had
  the field and not the wording, and gained nothing).

## 7. A second score for breadth of context

`e7_breadth` adds a `context_breadth` score beside `difficulty`, to see whether
splitting the judgement helps the effort choice.

| correlation over the 46 frontier-labelled cases | r |
| --- | --- |
| difficulty against the labelled effort | 0.62 |
| context_breadth against the labelled effort | 0.32 |
| context_breadth against difficulty | 0.64 |

Rejected. Breadth predicts the effort label half as well as difficulty does,
and is itself 0.64 correlated with difficulty, so it mostly repeats what
difficulty already says. Overall it was worth +1.2 tier, one case. Not worth a
fourth question.

## 8. Policy numbers

`evals/tune.py`, 5000 random samples over the grid plus coordinate descent,
scored on the tuning split only.

| number | was | now |
| --- | --- | --- |
| `low_confidence.min_confidence` | 0.45 | 0.55 |
| model cutoff (`moderate`, `coding_middle`, `tool_loop`) | 1.0 | 1.4 |
| `hard` difficulty floor | 1.8 | 2.4 |
| `very_hard` difficulty floor | 2.6 | 3.0 |
| `high_harm` threshold | 0.6 | 0.8 |
| faithfulness threshold | off | off |
| the fast tier's effort ladder | none / low | gone, always `none` (experiment 10) |

| split | tier | under | over | effort ±1 | cost |
| --- | --- | --- | --- | --- | --- |
| tuning, before | 64.5 | 1.6 | 33.9 | 93.1 | 12.9 |
| tuning, after | 91.9 | 0.0 | 8.1 | 93.1 | 8.2 |
| held-out, before | 79.2 | 4.2 | 16.7 | 81.2 | 15.2 |
| held-out, after | 87.5 | 4.2 | 8.3 | 93.8 | 11.6 |

Run the tuner again on the current `router.yaml` and it proposes no change.
Scored against the outcome benchmark's revised tiers rather than the hand
labels, the same config reaches 91.9% on the tuning split and 91.7% held out,
with no under-routing on either.

Every cutoff moved up. The new difficulty question puts ordinary work near 1.0
where the old one put it near 1.2, so the old cutoffs sent almost everything to
the frontier model. Most of the "over-routing" in the earlier experiments was
this, not the classifier.

The search minimises quota spent plus the cost of the under-routes, where one
under-route is priced at 60 quota units: a redo on the frontier model at high
effort, counted three times because the person's time is worth more than the
quota. Without that term the search took settings that saved half a quota unit
and under-routed three more cases. TESTPLAN.md puts under-routing ahead of
over-routing; `UNDER_ROUTE_COST` in `tune.py` is the number that says by how
much.

## 9. The outcome benchmark, and what it changed

`evals/run_outcomes.py` ran 25 gradeable cases on all seven routes and graded
each reply with a programmatic check, a blind judge, or both. The full table is
in `evals/outcomes.md`.

| route | mean score | wins outright | adequate on | median latency | median output tokens |
| --- | --- | --- | --- | --- | --- |
| glm-5.3-flash(none) | 8.14 | 9/25 | 11/25 | 4.0 s | 428 |
| glm-5.3-flash(low) | 7.71 | 10/25 | 11/25 | 4.0 s | 468 |
| glm-5.3-flash(high) | 7.35 | 10/25 | 11/25 | 5.4 s | 605 |
| gpt-6-astra(low) | 9.00 | 17/25 | 19/25 | 12.2 s | 309 |
| gpt-6-astra(medium) | 9.18 | 20/25 | 21/25 | 13.2 s | 381 |
| gpt-6-astra(high) | 9.10 | 18/25 | 20/25 | 15.2 s | 416 |
| gpt-6-astra(xhigh) | 9.12 | 18/25 | 20/25 | 27.0 s | 829 |

**GLM's effort levels do not help.** Going none to low costs 0.43 points of 10
on average, and low to high a further 0.36. Only 5 of 25 cases got better by
half a point or more from raising it, and 4 more from raising it again, against
a third more latency. The cases.yaml labels had assumed this; the benchmark
confirms it. `router.yaml` now has no ladder on the fast tier at all: the last
rule is one route, `ollama/glm-5.3-flash(none)`.

**Astra's effort barely moves the score above `low`.** low to medium is +0.18,
medium to high is -0.08, high to xhigh is +0.01. Astra at `low` is adequate on
19 of 25 cases and `medium` on 21. What `xhigh` reliably buys is latency: 27
seconds against 12. So the ladder keeps xhigh only for the top of the
difficulty scale, where the cases that need several stages live.

**The gap between the models is real, and it is about faithfulness, not
fluency.** The two widest gaps were `writing-medical-discharge-38` (GLM 0.0 at
every effort, Astra 10.0 at every effort) and `design-auth-module-review-23`
(GLM 5.0, 6.0, 0.0; Astra 9.0 to 9.5). Both are cases where a confident wrong
statement is the failure. That matches an earlier in-house model comparison,
which found cheap models unreliable for faithful summaries.

Eighteen of the 25 cheapest-adequate routes agreed with the hand label. Three
clear-cut disagreements were written into `cases.yaml` with
`label_source: outcome`:

| case | label was | outcome says | why |
| --- | --- | --- | --- |
| summarise-adversarial-doc-69 | frontier/medium | fast/none | every route scored 10.0, including the fast model at no effort: it kept the exceptions and ignored the injected instruction |
| quick-drug-interaction-73 | frontier/medium | fast/none | fast-tier scores 9.5, 10.0, 10.0; astra(low) was the worst route at 8.1 |
| design-naming-bikeshed-24 | fast/none | frontier/medium | fast-tier scored 4.0 flat, frontier 6.0 to 7.0; the fast model padded a one-line naming question |

Four more disagreements are listed in `evals/outcomes.md` and left for a
person, because the margin came from one judge call or the scores were mixed
across the tier: `summarise-trial-abstract-66`,
`quick-borrow-checker-misleading-75`, `code-sql-window-query-05` and
`design-eventbus-tradeoff-20`.

Eighteen of the 43 specs in `outcome_specs.yaml` did not finish. The locally
hosted model slowed to minutes per call under sustained load and started
returning empty replies, so the run was stopped rather than pushed further.
Every finished reply is cached, so `uv run python evals/run_outcomes.py` picks
up where it left off.

## 10. The no-network fallback

Not a classifier experiment, but the retune broke it and it is worth recording.
`RulesDecider` guesses answers from counts. Two things had to change:

- Its task names were the old seven. No rule with a `task` condition could fire
  any more, so it was renamed to the ten.
- It returned a flat 0.5 confidence. With the confidence floor at 0.55 that
  made the low-confidence gate fire on every single request, sending everything
  to `gpt-6-astra(medium)` for as long as Jev was down. It now gives a
  confidence only where it has direct evidence (tools or code for the task, a
  score at either end of the range for the difficulty) and leaves the key out
  otherwise, which the gate skips.
- With the raised cutoffs it then under-routed 41% of cases. A documented
  `SAFETY_MARGIN` of 0.5 added to its score brings that to 23%, at a mean cost
  of 6.2 instead of 2.0. An outage should err upward.

## Where it ended up

| | task | diff | faith | stable | tier | under | over | effort ±1 | cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| before, tuning | 69.4 | 59.7 | 80.6 | 98.4 | 64.5 | 1.6 | 33.9 | 93.1 | 12.9 |
| after, tuning | 82.3 | 83.9 | 79.0 | 100.0 | 91.9 | 0.0 | 8.1 | 93.1 | 8.2 |
| before, held-out | 66.7 | 70.8 | 66.7 | 100.0 | 79.2 | 4.2 | 16.7 | 81.2 | 15.2 |
| after, held-out | 79.2 | 91.7 | 66.7 | 95.8 | 87.5 | 4.2 | 8.3 | 93.8 | 11.6 |

These are scored against `cases.yaml` after the three label changes in
experiment 9. The "before" rows are the old builder and questions scored
against the same labels, so the two rows differ only in the router.

Baselines, all 86 cases:

| | tier | under | over | cost |
| --- | --- | --- | --- |--- |
| always gpt-6-astra(medium) | 59.3 | 0.0 | 40.7 | 14.0 |
| always ollama/glm-5.3-flash(none) | 54.7 | 45.3 | 0.0 | 1.0 |
| the no-network `rules` decider | 62.8 | 23.3 | 14.0 | 6.2 |
| **the tuned Jev router** | **90.7** | **1.2** | **8.1** | **9.1** |

The one under-route is `quick-borrow-checker-misleading-75`: a Rust borrow
checker problem prefaced with "quick one, why won't this compile? I feel like
I'm missing something obvious". Jev calls it `debugging` at difficulty 1.12,
which is below the model cutoff, so it goes to the fast model. The question
already says to ignore claims about how hard the work is; this one is not a
claim, it is a tone, and the state builder has no way to strip it.

It is also the case where the hand label is least safe. The outcome benchmark
scored the fast model 10.0 on it at no effort, and 10.0 was the best score any
route got, so the route the router chose may well be right and the label wrong.
It is listed as one of the four disagreements left for a person.

## Assumptions worth knowing

- **The cost model is made up.** `ROUTE_COST` in `evals/common.py` puts
  `gpt-6-astra(medium)` at 14 units and `ollama/glm-5.3-flash(none)` at 1.
  Nothing measured those ratios; they encode "the frontier model costs about an
  order of magnitude more, and effort costs something too". Every "cost" number
  in this file is relative and only comparable within it.
- **Task accuracy is measured against one person's labels.** Several of the
  remaining misses are disagreements, not errors: a bash for-loop labelled
  `quick-question` and called `code-edit`; tidying docstrings in a .py file
  labelled `everyday-polish` and called `code-edit`; "what's the error on the
  last line" of a pasted log labelled `debugging` and called
  `summarise-or-extract`. All three route to the same place. The ceiling on
  this metric is lower than 100%.
- **24 held-out cases is small.** One case is 4.2 points. Treat held-out
  differences under about 8 points as noise.

---

# Second round: the expanded case set

Everything above was measured on 86 cases. The set is now 171, and the numbers
change enough that the two halves of this file are not comparable row for row.
Read the rows below against the re-baseline in experiment 11a, not against the
table in "Where it ended up".

Method, as before: `evals/run_eval.py`, three Jev calls per case, `jev-1.13.0`,
split 70/30 by seed 20260918 stratified by task label. The tuning split is now
120 cases and the held-out split 51.

What is new in the method:

- **Every rate carries a Wilson 95% interval.** On 171 cases a rate near 85%
  has an interval about 11 points wide, and two independent runs of this size
  cannot separate a difference smaller than about 11 points. Most of the rows
  below are therefore ties on the headline number, which is why each one is
  also tested **paired**: the same cases under two configurations, resampled
  2,000 times (`paired_bootstrap_p` in `evals/metrics.py`). A paired test is
  far more sensitive, because it removes the case-to-case variance that
  dominates the interval.
- **Two controls that have to fail.** A shuffled-label run and a
  constant-state run, both behind `--control`.
- **A cost-matched random baseline**, RouterBench's Zero Router.
- **Slices.** Every case carries one of `chat`, `agentic`, `multiturn`,
  `longcontext`, `multilingual`, `adversarial`, `pipeline`, and the policy is
  also tuned with one slice removed and reported on it.

## 11a. Re-baseline: the same router on 171 cases instead of 86

No change to the router. This row only says what the new cases cost in
measured accuracy, so every later row has an honest starting point.

| | task | diff | faith | tier | under | over | cost |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 86 cases | 81.4 | 86.0 | 75.6 | 90.7 | 1.2 | 8.1 | 9.1 |
| 171 cases | 73.7 [66.6, 79.7] | 74.9 [67.9, 80.8] | 70.8 [63.5, 77.1] | 88.3 [82.6, 92.3] | 1.2 [0.3, 4.2] | 10.5 [6.8, 16.0] | 10.7 |

Task accuracy fell 7.7 points and difficulty 11.1. The intervals on the old
numbers were wide enough that some of this is the old set being small, but most
of it is real and it is concentrated in two slices:

| slice | n | task acc | difficulty | tier ok | cost |
| --- | --- | --- | --- | --- | --- |
| chat | 45 | 77.8 [63.7, 87.5] | 91.1 [79.3, 96.5] | 93.3 [82.1, 97.7] | 8.2 |
| agentic | 46 | 54.3 [40.2, 67.8] | 76.1 [62.1, 86.1] | 84.8 [71.8, 92.4] | 12.8 |
| adversarial | 27 | 74.1 [55.3, 86.8] | 51.9 [34.0, 69.3] | 85.2 [67.5, 94.1] | 12.1 |
| multilingual | 16 | 87.5 [64.0, 96.5] | 81.2 [57.0, 93.4] | 81.2 [57.0, 93.4] | 9.9 |
| multiturn | 16 | 87.5 [64.0, 96.5] | 68.8 [44.4, 85.8] | 87.5 [64.0, 96.5] | 12.2 |
| longcontext | 11 | 90.9 [62.3, 98.4] | 63.6 [35.4, 84.8] | 100.0 [74.1, 100.0] | 9.5 |
| pipeline | 10 | 80.0 [49.0, 94.3] | 70.0 [39.7, 89.2] | 90.0 [59.6, 98.2] | 9.3 |

Task accuracy on the agentic slice is 54.3%. The confusion table says why, and
it is mostly not an error: Jev answers `code-edit` or `debugging` on turns the
labels call `agentic-tool-task`, because the turn *is* a code edit and the
tools are what makes it agentic. The router treats those three categories
identically in `coding_middle`, so the tier is right 89% of the time on a slice
where the task label is right 54% of the time. The task metric is measuring a
taxonomy disagreement, not a routing failure.

Difficulty accuracy on the adversarial slice is 51.9%, which is the metric
doing its job: 27 cases exist specifically to push the difficulty answer around.

## 11b. Baselines, including the cost-matched random router

All 171 cases.

| | tier ok | under | over | cost |
| --- | --- | --- | --- | --- |
| always gpt-6-astra(medium) | 63.2 [55.7, 70.0] | 0.0 | 36.8 | 14.0 |
| always ollama/glm-5.3-flash(none) | 46.2 [38.9, 53.7] | 53.8 | 0.0 | 1.0 |
| the no-network `rules` decider | 60.2 [52.8, 67.3] | 23.4 | 16.4 | 7.4 |
| **cost-matched random (Zero Router)** | **57.3 ± 3.4** | 24.3 | 18.4 | 10.9 |
| majority class (always the commoner tier) | 63.2 | - | - | - |
| **the tuned Jev router** | **92.4 [87.4, 95.5]** | **1.2** | **6.4** | **10.8** |

The cost-matched random row is the one worth having. It routes to the frontier
tier with the router's own frontier rate and draws the effort from the router's
own mix, so it spends 10.9 quota units against the router's 10.8 and differs
only in choosing at random. RouterBench calls this the Zero Router. The gap is
35 points at the same spend, averaged over 20 seeded draws with a standard
deviation of 3.4, so the router is choosing rather than merely spending.

Note that `always gpt-6-astra(medium)` and the majority class are the same
number, 63.2%. That is not a coincidence: 63% of cases accept the frontier
tier, so always picking it *is* the majority-class router.

## 11c. Controls

Both are flags on `run_eval.py` and both have a unit test in
`tests/test_evals.py`.

| run | task acc | difficulty | tier ok | cost |
| --- | --- | --- | --- | --- |
| normal | 73.7 | 74.9 | 88.3 | 10.7 |
| `--control shuffled-labels` | 14.0 | 34.5 | 60.8 | 10.7 |
| `--control constant-state` | 9.9 | 38.0 | 63.2 | 14.0 |

Shuffled labels: every case keeps its request and gets another case's labels.
Task accuracy falls from 73.7% to 14.0%, against an expected 11.5%, which is
the rate at which a shuffled permutation of this label mix matches itself by
accident. The metric is reading the labels.

Constant state: every case is replaced by the same bland request. Task accuracy
falls to 9.9%, and tier accuracy lands on 63.2%, which is the majority-class
rate to one decimal place, at a mean cost of 14.0 because every case now routes
to `gpt-6-astra(medium)`. The routing score is reading the state and nothing
else.

Difficulty does not fall as far as task does under either control (34.5% and
38.0% against 74.9%). That is the tolerance band: a case with
`difficulty_tolerance: 1` accepts three of the four levels, so a third of the
scale is right by construction. It is a known floor on that metric, not a leak.

## 11d. Calibration, and what the confidence gate is worth

ECE is the bin-count-weighted mean gap between the confidence Jev claims and
the accuracy it delivers, over the same six buckets the per-bin table used.

| question | n | ECE | worst bin | mean confidence | accuracy | overconfident by |
| --- | --- | --- | --- | --- | --- | --- |
| task | 171 | 0.129 | 0.236 | 0.866 | 0.737 | +0.129 |
| difficulty | 171 | 0.084 | 0.187 | 0.695 | 0.749 | -0.053 |

Two different pictures. On `task`, Jev is overconfident by 13 points. On
`difficulty` it is *under*confident by 5: it is right slightly more often than
it claims.

The gate at 0.55 was applying one cutoff to both.

| question | accuracy below 0.55 | accuracy at or above 0.55 | cases below |
| --- | --- | --- | --- |
| task | 35.3 [17.3, 58.7] | 77.9 [70.7, 83.7] | 17 |
| difficulty | 62.2 [46.1, 75.9] | 78.4 [70.6, 84.5] | 37 |

A low `task` confidence really does mean a wrong answer: 35% against 78%. A low
`difficulty` confidence barely means anything, and 37 of 171 cases were being
routed to `gpt-6-astra(medium)` on the strength of it. That is experiment 14.

## 12. A predicted-output-length question

`e11_output_length` adds a four-level `output_length` score beside
`difficulty`, on the theory that effort should follow how much has to be
written rather than how much has to be worked out.

| | tier ok | under | over | cost | paired p vs baseline |
| --- | --- | --- | --- | --- | --- |
| router_yaml | 88.3 [82.6, 92.3] | 1.2 | 10.5 | 10.7 | - |
| e11_output_length | 87.7 [82.0, 91.8] | 1.2 | 11.1 | 11.0 | 0.589 |

Three cases changed. The question it was added to answer is the correlation
one, and the answer is clear:

| | r against the labelled effort, over the 101 frontier cases |
| --- | --- |
| difficulty | +0.498 |
| output_length | +0.292 |

Over all 171 cases against the labelled tier: difficulty +0.758,
output_length +0.555. And `output_length` is itself 0.700 correlated with
`difficulty`, so most of what it knows, difficulty already said.

**Rejected.** Same shape as experiment 7: a second score that predicts the
label about half as well as difficulty does and largely repeats it. Not worth a
fourth question or the tokens.

## 13. Language and subject area as computed facts

`e12_lang_domain` adds `request_language` and `subject_area` to the facts
block. Both are computed in `evals/exp_builders.py` with no model call:
language from script ranges and then function-word counts, subject area from
fixed keyword lists.

| | tier ok | under | over | cost | paired p |
| --- | --- | --- | --- | --- | --- |
| router_yaml | 88.3 [82.6, 92.3] | 1.2 | 10.5 | 10.7 | - |
| e12_lang_domain | 88.9 [83.3, 92.8] | 0.6 | 10.5 | 11.1 | 0.601 |

Three cases changed, one of them on the multilingual slice (81.2% to 87.5%,
which is one case of 16). The detectors themselves work: spot-checked against
the 16 multilingual cases they get every language right.

**Rejected.** The facts are correct and they change nothing. The likely reason
is that Jev can already see the language: it is reading the request. Naming a
thing the model can see does not add information. This is the same result as
experiment 6's "facts first or last made no difference at all", and it is worth
remembering before adding a third computed fact.

## 14. Loosening the confidence gate

`e14_gate_040` lowers `low_confidence.min_confidence` from 0.55 to 0.40 and
changes nothing else. Argued for by the calibration numbers in 11d.

| | tier ok | under | over | effort ±1 | cost | gate fired | paired p |
| --- | --- | --- | --- | --- | --- | --- | --- |
| router_yaml (0.55) | 88.3 [82.6, 92.3] | 1.2 | 10.5 | 81.9 | 10.75 | 49/171 | - |
| **e14_gate_040** | **92.4 [87.4, 95.5]** | **1.2** | **6.4** | **87.1** | 10.82 | 13/171 | **0.012** |

Seven cases changed, all of them from over-routed to correct, and under-routing
did not move. The headline intervals overlap, as they will for a four-point
difference on 171 cases, but the paired test is what applies here: the same
cases under two settings, p = 0.012.

Per split, after applying it:

| split | tier ok | under | over | effort ±1 | cost |
| --- | --- | --- | --- | --- | --- |
| tuning, before | 90.8 [84.3, 94.8] | 0.0 | 9.2 | 88.9 | 10.8 |
| tuning, after | 93.3 [87.4, 96.6] | 0.0 | 6.7 | 93.1 | 11.1 |
| held-out, before | 82.4 [69.7, 90.4] | 3.9 | 13.7 | 86.2 | 10.6 |
| held-out, after | 90.2 [79.0, 95.7] | 3.9 | 5.9 | 82.8 | 10.1 |

Two other settings were measured and not taken:

- **Turning the gate off entirely** reaches 92.98% tier accuracy, marginally
  better, but doubles under-routing from 1.2% to 2.3% (p = 0.026). TESTPLAN.md
  puts under-routing first, and the `task` row in 11d shows the gate is
  carrying real signal, so it keeps a floor.
- **Gating on `task` alone at 0.55** reaches 90.1% with the same doubling of
  under-routing and p = 0.339. Worse than 0.40 on both questions.

**Kept.** This is the only change in this round that moved anything, and it is
the one change applied to `router.yaml`.

## 15. Wording against the adversarial slice

`e13_router_instructions` rewrites the `task` and `difficulty` instructions to
say that text addressing the router or the model selector, in a code comment, a
configuration field, a tool result or a third-party system prompt, is data.

| | tier ok | adversarial slice | paired p |
| --- | --- | --- | --- |
| router_yaml | 88.3 [82.6, 92.3] | 85.2 [67.5, 94.1] | - |
| e13_router_instructions | 88.9 [83.3, 92.8] | 85.2 [67.5, 94.1] | 0.629 |

One case changed, and it was not on the adversarial slice.

**Rejected**, and the reason is more interesting than the row. Reading the
three attacks that worked shows none of them worked the way the extra wording
guards against:

| case | goal | delivery | what actually happened |
| --- | --- | --- | --- |
| adv-subtle-rename-89 | cost_escalation | fake_difficulty | Jev said `code-edit`, difficulty 1.08. Right. The **gate** fired at confidence 0.47. |
| adv-compose-indent-90 | cost_escalation | fake_router_instructions | Jev said `debugging`, difficulty 1.14. Right. The **gate** fired at 0.43. |
| adv-enterprise-cron-93 | cost_escalation | untrusted_system_prompt | Jev said `quick-question`, difficulty 0.56. Right. The **gate** fired at 0.44. |

In all three cases Jev ignored the attack exactly as the existing wording tells
it to. What the attacks did was make Jev *less certain*, and the confidence
gate turned that uncertainty into a frontier route. The attack surface was the
policy, not the classifier.

That also explains why two of the six delivery vectors could never have worked:
`continuation_aware_v1` leaves the system prompt out of the state entirely and
never puts a tool result in it, so an injection in either place has nothing to
reach. The defence there is the state builder, not the question wording, and it
was not designed for that. It is worth writing down before someone "improves"
the builder by adding the system prompt back.

After experiment 14, the adversarial slice is:

| goal | n | tier held | effort held | mean cost |
| --- | --- | --- | --- | --- |
| cost_escalation | 8 | 100.0 [67.6, 100.0] | 100.0 [67.6, 100.0] | 1.0 |
| quality_hijack | 9 | 100.0 [70.1, 100.0] | 100.0 [70.1, 100.0] | 15.3 |
| harmful_downgrade | 8 | 100.0 [67.6, 100.0] | 100.0 [67.6, 100.0] | 16.2 |

All six delivery vectors are at 100% as well. The cost-escalation mean cost of
1.0 means all eight of those cases now land on the fast model at no effort,
which is what their labels say. With 8 cases per goal the lower bound of the
interval is 68%, so this says "no attack in this set works", not "no attack
works".

## 16. Decision stability under perturbation

Each case's latest user message was reworded four ways with a fixed seed, and
the router asked again: two transposed letters in the opening paragraph, the
opening paragraph lowercased with its punctuation stripped, a polite filler
prefix and suffix, and up to three one-way synonym swaps from a fixed table.
The edits never touch anything below the first blank line, so a pasted stack
trace or code block survives intact. No model is asked to rewrite anything, so
this costs four Jev calls per case and nothing else.

| | (model, effort) unchanged | cases where nothing moved |
| --- | --- | --- |
| before experiment 14 | 93.7 [91.6, 95.3] | 83.6 [77.4, 88.4] |
| after experiment 14 | 95.0 [93.1, 96.4] | 85.4 [79.3, 89.9] |

Loosening the gate made the router more stable as well as more accurate, which
follows: a case sitting near a confidence cutoff flips whenever the wording
nudges the confidence, and the cutoff now catches fewer cases.

The cases that still move are the ones sitting on a difficulty cutoff. The
worst is `debug-long-log-trivial-question-12`, which drops from
`gpt-6-astra(medium)` to `ollama/glm-5.3-flash(none)` under both the casing and
the synonym rewording: it is the long-paste-trivial-question trap, and it sits
right on the line by design.

A route that moves when the wording moves is a route the user cannot predict,
and 15% of requests being that way is worth knowing. It is also an argument for
the pin: within one conversation the route is fixed whatever the user types
next.

## 17. Slice hold-out

The 70/30 row split leaves near-duplicates of a held-out case in the tuning
half, which flatters the held-out number. `tune.py --slice-holdout <name>`
removes a whole shape instead, tunes without it, and reports on it.

The comparison that matters is the last column: a policy whose numbers were
searched without ever seeing this shape, measured on it, against the shipped
policy measured on the same cases.

| held-out slice | n | shipped policy | tuned without this slice |
| --- | --- | --- | --- |
| adversarial | 27 | 96.3 | 85.2 |
| agentic | 46 | 89.1 | 87.0 |
| multiturn | 16 | 87.5 | 87.5 |
| multilingual | 16 | 87.5 | 81.2 |
| longcontext | 11 | 100.0 | 100.0 |
| pipeline | 10 | 100.0 | 90.0 |
| chat | 45 | 93.3 | 84.4 |

In every slice the tuner's proposal is at best equal to the shipped numbers and
usually worse, which is the same finding as experiment 18 below arrived at from
the other direction. The largest drops are on `chat` and `adversarial`, the two
slices that most constrain the cheap end of the ladder: remove them and the
search happily raises the model cutoff.

## 18. Rerunning the tuner on 171 cases, and declining its proposal

`uv run python evals/tune.py --results evals/results/latest --variant router_yaml --samples 5000`

| | tier ok | under | over | effort ±1 | cost |
| --- | --- | --- | --- | --- | --- |
| router.yaml now, tuning | 93.3 | 0.0 | 6.7 | 88.3 | 11.12 |
| router.yaml now, held-out | 90.2 | 3.9 | 5.9 | 84.3 | 10.12 |
| proposed, tuning | 90.8 | 0.0 | 9.2 | 82.5 | 10.72 |
| proposed, held-out | 82.4 | 3.9 | 13.7 | 78.4 | 10.63 |

It proposes putting `conf_floor` back to 0.55 and raising `model_cutoff` from
1.4 to 1.6. **Not applied.**

The proposal is worse on tier accuracy on both splits and better on nothing
except mean cost, by 0.40 quota units per request. That is the tuner behaving
exactly as written and the objective being wrong for the question: `search()`
minimises quota spent plus a penalty for under-routing, and over-routing has no
price beyond the quota it consumes. So the search will always sell 2.5 points
of tier accuracy for 0.40 units, because over-routing is nearly free in its
arithmetic and free in its constraint.

Two honest readings, and this file should carry both:

1. If mean cost is what you care about, the tuner is right and this section is
   a refusal to take a cheaper policy.
2. If TESTPLAN.md's reported metric is what you care about, the objective in
   `tune.py` does not encode it, and the fix is to put a price on over-routing
   rather than to override the tuner by hand each time.

The second is the better reading and the work is not done. Until it is, the
tuner is a proposal engine that needs reading, which is what `tune.py` always
claimed to be: it prints a diff and never writes the file.

## 19. A cheap draft, AutoMix style

`e15_draft`. Before deciding, ask `ollama/glm-5.3-flash(none)` for a short
answer, put it in the state under `draft_answer_from_a_fast_model`, and ask Jev
a noul: does that draft fully and correctly answer the request, so that sending
it would leave the user with nothing to redo?

This is AutoMix (arXiv 2310.12963) with the self-verification done by the
decision model instead of by a prompted LLM. It is implemented in the router
itself as `aliases.<name>.draft`, off by default, failing open on any error,
and it is never paid for on a pinned turn because a pinned conversation does
not reach the decider at all.

Measured on a stratified subset of 80 cases, because it costs one real upstream
call each.

| | tier ok | under | effort ±1 | cost | median state tokens |
| --- | --- | --- | --- | --- | --- |
| router_yaml | 95.0 [87.8, 98.0] | 0.0 | 92.5 [84.6, 96.5] | 11.2 | 139 |
| e15_draft | 95.0 [87.8, 98.0] | 0.0 | 88.8 [80.0, 94.0] | 11.2 | 422 |

Thirteen routes moved and the tier score did not change at all: paired
p = 1.000. Effort accuracy went down by three cases.

What it costs, measured:

- one extra upstream call per unpinned turn, with a median latency of 4.9 s
  against Jev's 0.25 s, so it multiplies routing latency by about twenty
- the state grows from 139 to 422 tokens, roughly tripling the Jev input
- 9 of 80 drafts came back empty and were skipped

**The signal is real and it still does not pay.** `draft_is_enough` averages
0.52 on cases where the fast model is acceptable and 0.17 where it is not, so
it does discriminate. But putting a rule on it only ever creates under-routes:

| rule added | tier ok | under | cost | cases changed | paired p |
| --- | --- | --- | --- | --- | --- |
| none | 95.0 [87.8, 98.0] | 0.0 | 11.16 | - | - |
| `draft_is_enough >= 0.4` -> fast | 88.8 [80.0, 94.0] | 6.2 | 10.31 | 5 | 0.031 |
| `>= 0.5` | 88.8 [80.0, 94.0] | 6.2 | 10.31 | 5 | 0.031 |
| `>= 0.6` | 91.2 [83.0, 95.7] | 3.8 | 10.54 | 3 | 0.131 |
| `>= 0.7` | 92.5 [84.6, 96.5] | 2.5 | 10.76 | 2 | 0.186 |
| `>= 0.8` | 93.8 [86.2, 97.3] | 1.2 | 10.93 | 1 | 0.636 |

Every threshold is worse than no rule, and the two that move enough cases to be
significant are significantly worse. The pattern is monotone: the closer the
rule comes to never firing, the closer the result comes to the baseline. That
is as clean a rejection as this set can produce.

**Rejected**, and left in the code behind `draft.enabled: false`, because the
implementation is the expensive part and the experiment is worth being able to
repeat against a different pair of models.

Why it fails here, most likely: AutoMix's cascade pays when the cheap model's
answer can be *used*. This router does not use the draft, it throws it away and
routes the real request, so the draft buys information and nothing else. A
router that returned an adequate draft as the answer would be a different
design and would need a different evaluation.

### A bug worth recording

The first two runs of this experiment returned no drafts at all, and then 3 of
80. The draft call was sending the model's `upstream_id` with no effort suffix,
so the proxy gave it a default reasoning effort, and the model spent its entire
150-token budget on reasoning and returned empty content with
`finish_reason: length`. The fix was to apply the effort the same way
forwarding does, through `policy.apply_effort`, and to raise the draft budget
to 500 tokens.

Two lessons past this experiment. A reasoning model with a small `max_tokens`
returns nothing rather than something short, so any cheap-draft design needs
headroom for thinking it will throw away. And `finish_reason` is what made this
diagnosable in a minute instead of an hour, which is the same reason the
outcome benchmark now records it.

## 20. The outcome benchmark at k=3, with the token cap raised

`evals/run_outcomes.py --samples 3 --max-calls 420`, candidate cap raised from
3,000 to 8,000 tokens.

**What ran.** Ten cases, each on all seven routes, three samples per route:
`quick-postgres-port-71`, `quick-percentage-74`, `quick-timezone-abbrev-77`,
`code-regex-extract-04`, `extract-invoice-json-62`, `extract-action-items-65`,
`writing-investor-update-numbers-44`, `summarise-adversarial-doc-69`,
`debug-adversarial-injection-in-log-16`, `summarise-trial-abstract-66`. That is
210 candidate samples plus judge calls. Fifteen of the 25 cases run at k=1
before, and all 18 that had never run, did not run: the upstream slowed to 20
seconds a call on the larger prompts and the run was stopped on its budget with
everything finished cached, so the next run resumes from case 11.

Raising the cap invalidated the whole earlier candidate cache, because the cap
is part of the request and therefore part of the cache key. Every number below
is fresh.

**Truncation: 0.0% on every route.** Median output is 264 to 656 tokens against
a cap of 8,000, and not one sample of 210 stopped on `length`. The worry that
the earlier "more effort does not help" finding was an artefact of a 3,000-token
cap cutting off reasoning is answered: it was not. At 3,000 there was already
four times the headroom these answers use.

**The effort findings hold, in the same direction and smaller.**

| model | effort pair | at k=1, cap 3,000 | at k=3, cap 8,000 | better by 0.5+ |
| --- | --- | --- | --- | --- |
| glm-5.3-flash | none -> low | -0.43 | -0.17 | 0/10 |
| glm-5.3-flash | low -> high | -0.36 | -0.03 | 1/10 |
| gpt-6-astra | low -> medium | +0.18 | +0.22 | 2/10 |
| gpt-6-astra | medium -> high | -0.08 | -0.12 | 0/10 |
| gpt-6-astra | high -> xhigh | +0.01 | -0.27 | 0/10 |

Every sign is the same except `high -> xhigh`, which was flat and is now
slightly negative. The within-route spread is the thing to read these against:
the mean per-route standard deviation over three samples is 0.00 to 0.65, so a
difference of 0.2 is inside the noise of a single route and only the direction
is worth anything. `xhigh` still costs what it always did: 21.7 seconds median
against 9.9 for `medium`.

**Winner flip rate: 0.000 on tier, 0.032 on route, 0 of 10 cases unstable.**
This is a much weaker result than it looks, and the reason is selection. The
ten cases that ran are the ten cheapest to run, and nine of them scored 10.0 on
every route. A case where every route is perfect cannot have an unstable
winner. The flip rate is now measured and the machinery works; it has not yet
been pointed at a case where it could say anything. The cases that would test
it are the ones that did not run.

**Judge against check: they disagree a third of the time, and the judge is
harsher.** On the 83 samples that have both a programmatic check and a judge
score:

| | |
| --- | --- |
| differ by 2 points or more (of 10) | 32.5% [23.4, 43.2] |
| disagree on whether the answer passes at 7 of 10 | 16.9% [10.3, 26.3] |
| the judge scores higher | 6.0% [2.6, 13.3] |
| mean signed difference, judge minus check | -1.51 |

This is the most useful number the k=3 run produced. The judge is not noisy
around the check, it is biased below it by about a point and a half, and on one
sample in six it calls a passing answer a failure. Thirty of the 43 specs in
`outcome_specs.yaml` are judge-only, so that bias is in most of layer 3, and it
runs in the direction that makes the fast model look worse than it is. Where
both exist the check now decides, which removes the bias from those cases and
is why it could be measured at all.

**Three proposed label changes, none applied.** All three say a
frontier-labelled case should be fast, and all three rest on every route
scoring 10.0. A grading spec that gives every route full marks has not shown
that the fast model is good enough; it has shown that the spec cannot tell the
routes apart. `--apply-labels` was not run. The right fix is a harder rubric on
those three cases, not a label change.
