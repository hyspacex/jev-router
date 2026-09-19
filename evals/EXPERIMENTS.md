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
