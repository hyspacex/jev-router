# Historical benchmark results

[Documentation index](../README.md) · [Project home](../../README.md)

**Historical snapshot, not a scorecard for the current configuration.** This material was moved from the original README without rerunning its experiments. References below to “this config”, “shipped” or “this branch” describe the measured revisions, not today’s strict `auto` entrypoint. Relative route costs are not measured spending or subscription savings. Use the linked reports for provenance and uncertainty; unaccompanied point estimates are not new verified claims.

### Where it stands

The [2026-09-19 coding-session pilot](../../evals/reports/2026-09-19-session-pilot/summary.md)
ran fixed Astra medium, deterministic rules and strict Jev admission on the
same eight tasks, once per policy. Every session passed its hidden checks;
Jev selected Flash for some tasks while the other policies used Astra throughout.
This shows that all three paths can complete the small suite under its tested
limits. It does not establish quality equivalence or subscription savings,
and adaptive effort was off. The report includes per-session results and
provenance; broader tasks and repeated trials are still needed.

The classification results below are a separate measurement.
Over all 171 cases, with Wilson 95% intervals. On this many cases one case is
0.6 points and two independent runs cannot separate a difference smaller than
about 11 points, so read the intervals before reading the ranking.

| | task acc | difficulty | tier correct | under-routed | over-routed | cost |
| --- | --- | --- | --- | --- | --- | --- |
| this config | 73.7 [66.6, 79.7] | 74.9 [67.9, 80.8] | **92.4 [87.4, 95.5]** | 1.2 [0.3, 4.2] | 6.4 [3.6, 11.2] | 10.8 |
| before the confidence-gate change | 73.7 | 74.9 | 88.3 [82.6, 92.3] | 1.2 | 10.5 | 10.7 |
| cost-matched random (Zero Router) | - | - | 57.3 ± 3.4 | 24.3 | 18.4 | 10.9 |
| always gpt-6-astra(medium) | - | - | 63.2 [55.7, 70.0] | 0.0 | 36.8 | 14.0 |
| always ollama/glm-5.3-flash(none) | - | - | 46.2 [38.9, 53.7] | 53.8 | 0.0 | 1.0 |
| the no-network `rules` decider | 30.4 [24.0, 37.7] | 31.6 [25.1, 38.9] | 60.2 [52.8, 67.3] | 23.4 | 16.4 | 7.4 |
| majority class (ignore the request) | - | - | 63.2 | - | - | - |

The row that matters is the cost-matched random one, which is RouterBench's
Zero Router. It sends a request to the frontier tier with the router's own
frontier rate and draws the effort from the router's own mix, so it spends
10.9 quota units against the router's 10.8 and differs only in choosing at
random. The router is 35 points better at the same spend, so it is choosing
rather than spending.

Cost is relative, not currency. `ROUTE_COST` in `evals/common.py` puts
`gpt-6-astra(medium)` at 14 units and `ollama/glm-5.3-flash(none)` at 1.
Nothing measured those ratios.

By slice:

| slice | n | task acc | difficulty | tier correct | under | cost |
| --- | --- | --- | --- | --- | --- | --- |
| chat | 45 | 77.8 [63.7, 87.5] | 91.1 [79.3, 96.5] | 93.3 [82.1, 97.7] | 2.2 | 8.7 |
| agentic | 46 | 54.3 [40.2, 67.8] | 76.1 [62.1, 86.1] | 89.1 [77.0, 95.3] | 0.0 | 13.5 |
| adversarial | 27 | 74.1 [55.3, 86.8] | 51.9 [34.0, 69.3] | 96.3 [81.7, 99.3] | 0.0 | 11.3 |
| multiturn | 16 | 87.5 [64.0, 96.5] | 68.8 [44.4, 85.8] | 87.5 [64.0, 96.5] | 0.0 | 11.9 |
| multilingual | 16 | 87.5 [64.0, 96.5] | 81.2 [57.0, 93.4] | 87.5 [64.0, 96.5] | 6.2 | 9.1 |
| longcontext | 11 | 90.9 [62.3, 98.4] | 63.6 [35.4, 84.8] | 100.0 [74.1, 100.0] | 0.0 | 10.0 |
| pipeline | 10 | 80.0 [49.0, 94.3] | 70.0 [39.7, 89.2] | 100.0 [72.2, 100.0] | 0.0 | 8.6 |

Slice intervals are wide enough that this table is for finding where the router
fails, not for deciding that one config beats another.

The 27 adversarial cases cover three attack goals, delivered six ways. All 27
hold their label: the route stays inside `acceptable_tiers` and the effort
stays within one level.

| attack goal | n | tier held | effort held |
| --- | --- | --- | --- |
| cost escalation | 8 | 100.0 [67.6, 100.0] | 100.0 [67.6, 100.0] |
| quality hijacking | 9 | 100.0 [70.1, 100.0] | 100.0 [70.1, 100.0] |
| harmful downgrade | 8 | 100.0 [67.6, 100.0] | 100.0 [67.6, 100.0] |

With 8 cases per goal the lower bound of the interval is 68% even at 100%
observed, so this says "no attack in this set works", not "no attack works".

Three targets in TESTPLAN.md are missed, all on classification:

- Task label accuracy is 73.7% against 85%. Most of the shortfall is the
  agentic slice, where Jev answers `code-edit` on turns the labels call
  `agentic-tool-task`. The policy treats those categories identically, so the
  tier is right 89% of the time on that slice.
- Difficulty within tolerance is 74.9% against 80%, concentrated on the
  adversarial slice, which exists to push that answer around.
- The faithfulness flag is 70.8% against 85%. It uses `harm_if_wrong` as a
  proxy; a dedicated `needs_faithfulness` question predicts the label better
  but made routing worse, so no alias asks it.

Two more measured properties:

- **Stability.** Reword a request four ways with a fixed seed and the route is
  unchanged 95.0% [93.1, 96.4] of the time, with 85.4% of requests never moving
  at all. The ones that move sit on a difficulty cutoff.
- **Calibration.** ECE is 0.129 on `task`, where Jev is overconfident, and
  0.084 on `difficulty`, where it is slightly underconfident. That is why
  `low_confidence.min_confidence` is 0.40 rather than 0.55.

The fast model is always used at `effort: none`. The outcome benchmark found
that raising its effort made answers slightly worse on average and cost a third
more latency. A rerun with three samples per route and the token cap raised
from 3,000 to 8,000 found the same signs, no truncation at all on any route,
and a blind judge that scores about 1.5 points below the programmatic check it
was measured against. `evals/outcomes.md` has the tables.

Caveats. The cases are written rather than sampled, and labelled by one person,
so task accuracy is measured against one opinion. A sample of 100 public
requests (WildChat, BFCL, LongBench v2, MGSM) routes at 86.0% [77.9, 91.5]
tier-correct with 5.0% under-routing, against 92.4% and 1.2% on the written
set, so the written set is somewhat easier than real traffic. The outcome
benchmark is partial.

Rerun the classification scores after any change to a question or a state
builder, and the tuner after that. The cutoffs only mean anything against one
wording. `settings.jev_model` is pinned to an exact version so a model update
on TypeSafe's side cannot move routing without a rerun.

The newer machinery has been measured and none of it was promoted. Over all 171
cases and all four policies, this branch and the release before it produced
identical states, identical Jev answers and identical routes: everything new is
opt-in or shadow. The four packet arms of spec 13.4 came out a tie —
`router_yaml` 92.4 [87.4, 95.5], `b1_coding_state` 90.6 [85.3, 94.2],
`b2_expanded_packet` 90.6 [85.3, 94.2], `b3_distribution` 91.8 [86.7, 95.1] —
with under-routing at 1.2% for the shipped packet against 4.1% for the
alternative, so the shipped one stayed. Asking three more questions over the
same state costs about 900 input tokens and 2 ms of median latency. Experiments
21 to 25 in `evals/EXPERIMENTS.md` have the rest, including the four negative
controls, the router's own added latency and the limits on every number.
