"""The eval harness: the case file, the controls, the generators, the budget.

These run without network. They check the things that would quietly make every
reported number wrong: a case with a bad label, a control that does not fail, a
budget that never stops, a generator that is not reproducible.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1] / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

import generators  # noqa: E402
from common import (  # noqa: E402
    CASE_TASKS,
    EFFORT_ORDER,
    SLICES,
    Budget,
    assign_slice_holdout,
    constant_state_body,
    infer_slice,
    load_cases,
    shuffle_labels,
    total_chars,
)
from metrics import chance_rate, majority_class_rate, rate  # noqa: E402


@pytest.fixture(scope="module")
def cases():
    return load_cases()


# --- the case file ------------------------------------------------------


def test_every_case_has_a_known_slice(cases):
    bad = [c.id for c in cases if c.slice not in SLICES]
    assert not bad, bad


def test_case_ids_are_unique(cases):
    counts = collections.Counter(c.id for c in cases)
    assert [cid for cid, n in counts.items() if n > 1] == []


def test_every_slice_has_cases(cases):
    present = {c.slice for c in cases}
    assert present == set(SLICES), set(SLICES) - present


def test_labels_are_well_formed(cases):
    for c in cases:
        assert c.task in CASE_TASKS, c.id
        assert 0 <= c.difficulty <= 3, c.id
        assert c.tolerance in (0, 1), c.id
        assert c.tier in ("fast", "frontier"), c.id
        assert c.effort in EFFORT_ORDER, c.id
        # A fast route has no reasoning effort, and a frontier route always has one.
        assert (c.effort == "none") == (c.tier == "fast"), c.id
        assert c.acceptable_tiers, c.id
        assert set(c.acceptable_tiers) <= {"fast", "frontier"}, c.id
        assert c.tier in c.acceptable_tiers, c.id


def test_adversarial_cases_name_their_attack(cases):
    adv = [c for c in cases if c.slice == "adversarial"]
    assert len(adv) >= 25
    # The two cases that predate the attack taxonomy are allowed to have none.
    typed = [c for c in adv if c.attack]
    assert len(typed) >= 25 - 2
    assert {c.attack for c in typed} == {
        "cost_escalation",
        "quality_hijack",
        "harmful_downgrade",
    }
    assert len({c.attack_vector for c in typed}) >= 6


def test_multiturn_cases_really_have_several_turns(cases):
    for c in cases:
        if c.slice != "multiturn":
            continue
        roles = [m.get("role") for m in c.body.get("messages") or []]
        assert len([r for r in roles if r != "system"]) >= 3, c.id


def test_long_context_cases_are_long(cases):
    longs = [c for c in cases if c.slice == "longcontext"]
    assert len(longs) >= 10
    for c in longs:
        assert total_chars(c.body) >= 20000, (c.id, total_chars(c.body))


def test_generated_cases_are_reproducible(cases):
    """The same case file loaded twice gives byte-identical requests."""
    again = {c.id: c for c in load_cases()}
    for c in cases:
        assert c.body == again[c.id].body, c.id


def test_infer_slice_prefers_the_most_specific_shape():
    doc = {"messages": [{"role": "user", "content": "x" * 30000}]}
    assert infer_slice("a", doc, "") == "longcontext"
    assert infer_slice("a", doc, "ADVERSARIAL paste") == "adversarial"
    tools = {"messages": [{"role": "user", "content": "hi"}], "tools": [{}]}
    assert infer_slice("a", tools, "") == "agentic"
    chat = {"messages": [{"role": "user", "content": "hello"}]}
    assert infer_slice("a", chat, "") == "chat"


# --- splits -------------------------------------------------------------


def test_row_split_is_stable_across_loads(cases):
    again = {c.id: c.split for c in load_cases()}
    assert {c.id: c.split for c in cases} == again


def test_slice_holdout_removes_the_whole_slice():
    cs = load_cases()
    n = assign_slice_holdout(cs, "adversarial")
    assert n > 0
    held = [c for c in cs if c.split == "held_out"]
    assert {c.slice for c in held} == {"adversarial"}
    assert all(c.slice != "adversarial" for c in cs if c.split == "tune")


# --- the controls, which must fail --------------------------------------


def test_shuffled_labels_keeps_the_label_mix_but_moves_them():
    cs = load_cases()
    shuffled = shuffle_labels(cs, seed=1)
    before = collections.Counter(c.task for c in cs)
    after = collections.Counter(c.task for c in shuffled)
    assert before == after, "a shuffle must not invent or lose labels"
    moved = sum(1 for a, b in zip(cs, shuffled) if a.task != b.task)
    assert moved > 0.5 * len(cs)


def test_shuffled_labels_scores_near_chance():
    """The control the whole evaluation rests on.

    If a perfect classifier still scores well once the labels are shuffled,
    the scorer is not comparing the answer with the label.
    """
    cs = load_cases()
    shuffled = shuffle_labels(cs, seed=7)
    truth = {c.id: c.task for c in cs}
    # A classifier that is right every time, scored against shuffled labels.
    scored = rate(truth[c.id] == c.task for c in shuffled)
    expected = chance_rate([c.task for c in cs])
    assert scored.lo <= expected + 8.0
    assert scored.pct < 30.0


def test_constant_state_cannot_beat_the_majority_class():
    """Every case gets the same request, so any router is a constant router.

    A constant router picks one tier for everything, and the best it can do is
    the majority class. The assertion is on the arithmetic, not on a live run:
    a live run is the `--control constant-state` flag.
    """
    cs = load_cases()
    body = constant_state_body()
    assert body["messages"][0]["role"] == "user"
    assert not body.get("tools")
    majority = majority_class_rate([c.acceptable_tiers for c in cs])
    for tier in ("fast", "frontier"):
        got = rate(tier in c.acceptable_tiers for c in cs)
        assert got.pct <= majority + 1e-9


# --- generators ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(generators.GENERATORS))
def test_generator_is_deterministic_and_long_enough(name):
    spec = {"generator": name, "seed": 3, "chars": 21000, "ask": "What is this?"}
    a = generators.build(spec)
    b = generators.build(spec)
    assert a == b
    text = a["messages"][-1]["content"]
    assert len(text) >= 21000
    assert "What is this?" in text


def test_generator_seeds_give_different_material():
    a = generators.build({"generator": "incident_log", "seed": 1, "chars": 5000, "ask": "x"})
    b = generators.build({"generator": "incident_log", "seed": 2, "chars": 5000, "ask": "x"})
    assert a != b


def test_generator_envelopes():
    agent = generators.build(
        {"generator": "python_module", "seed": 1, "chars": 2000, "ask": "x", "envelope": "agent"}
    )
    assert agent["tools"]
    assert agent["messages"][0]["role"] == "system"
    bare = generators.build(
        {"generator": "python_module", "seed": 1, "chars": 2000, "ask": "x", "envelope": "bare"}
    )
    assert bare["messages"][0]["role"] == "user"
    assert "tools" not in bare


def test_generator_placement_puts_the_ask_where_asked():
    before = generators.build(
        {"generator": "csv_ledger", "seed": 1, "chars": 2000, "ask": "ASKED", "placement": "before"}
    )["messages"][-1]["content"]
    assert before.rstrip().endswith("ASKED")
    after = generators.build(
        {"generator": "csv_ledger", "seed": 1, "chars": 2000, "ask": "ASKED", "placement": "after"}
    )["messages"][-1]["content"]
    assert after.startswith("ASKED")


def test_unknown_generator_says_which_ones_exist():
    with pytest.raises(KeyError) as exc:
        generators.build({"generator": "nope"})
    assert "incident_log" in str(exc.value)


# --- the upstream budget ------------------------------------------------


def test_budget_stops_at_the_call_limit():
    b = Budget(max_calls=2)
    for _ in range(2):
        assert b.may_spend()
        b.note(100.0, "ok", route="m")
    assert not b.may_spend()
    assert "budget spent" in b.stop_reason


def test_budget_stops_on_repeated_empty_replies():
    b = Budget(max_calls=100, max_empty=2)
    b.note(100.0, "", route="m")
    assert b.may_spend()
    b.note(100.0, "  ", route="m")
    assert not b.may_spend()
    assert "empty replies" in b.stop_reason


def test_budget_stops_when_one_route_gets_three_times_slower():
    b = Budget(max_calls=1000, warmup=4)
    for _ in range(4):
        b.note(1000.0, "ok", route="m")
    for _ in range(5):
        b.note(1000.0, "ok", route="m")
    assert b.may_spend()
    for _ in range(5):
        b.note(9000.0, "ok", route="m")
    assert not b.may_spend()
    assert "3x" in b.stop_reason


def test_budget_does_not_stop_because_the_run_moved_to_a_slower_route():
    """The bug this guard had: a pooled median rises whenever the run moves
    from a cheap route to an expensive one, which is not the upstream getting
    slower. Each route is compared against its own earlier self."""
    b = Budget(max_calls=1000, warmup=4)
    for _ in range(20):
        b.note(1000.0, "ok", route="fast-model")
    for _ in range(20):
        b.note(20000.0, "ok", route="slow-model")
    assert b.may_spend()
    assert b.stop_reason is None


def test_budget_line_reports_what_was_spent():
    b = Budget(max_calls=10)
    b.note(500.0, "ok", route="m")
    assert "1/10" in b.line()


# --- the outcome benchmark's own arithmetic -----------------------------


def _route(name, score, cost, scores=None, checks=None, judges=None):
    scores = scores if scores is not None else [score]
    return {
        "route": name,
        "model": "ollama/glm" if name.startswith("glm") else "astra",
        "effort": "none",
        "cost": cost,
        "score": score,
        "scores": scores,
        "error": None,
        "check_scores": checks or [None] * len(scores),
        "judge_scores": judges or [None] * len(scores),
    }


def test_cheapest_adequate_prefers_the_cheap_route_within_the_gap():
    import run_outcomes as ro

    results = [_route("glm(none)", 9.7, 1.0), _route("astra(high)", 10.0, 20.0)]
    assert ro._winner(results, lambda r: r["score"])["route"] == "glm(none)"
    results = [_route("glm(none)", 8.0, 1.0), _route("astra(high)", 10.0, 20.0)]
    assert ro._winner(results, lambda r: r["score"])["route"] == "astra(high)"


def test_flip_rate_is_zero_when_every_sample_agrees():
    import run_outcomes as ro

    results = [
        _route("glm(none)", 9.0, 1.0, scores=[9.0, 9.0, 9.0]),
        _route("astra(high)", 5.0, 20.0, scores=[5.0, 5.0, 5.0]),
    ]
    flips = ro.flip_rates(results, draws=100)
    assert flips["tier_flip_rate"] == 0.0
    assert flips["route_flip_rate"] == 0.0


def test_flip_rate_catches_a_winner_that_is_a_coin_toss():
    import run_outcomes as ro

    results = [
        _route("glm(none)", 7.0, 1.0, scores=[10.0, 4.0, 7.0]),
        _route("astra(high)", 7.0, 20.0, scores=[4.0, 10.0, 7.0]),
    ]
    flips = ro.flip_rates(results, draws=300)
    assert flips["tier_flip_rate"] > 0.2


def test_a_single_sample_reports_no_flip_rate():
    import run_outcomes as ro

    results = [_route("glm(none)", 9.0, 1.0), _route("astra(high)", 5.0, 20.0)]
    assert ro.flip_rates(results)["draws"] == 0


def test_an_unstable_case_never_overrides_a_hand_label():
    import run_outcomes as ro

    row = {
        "case_id": "x",
        "label": {"tier": "frontier", "effort": "high", "acceptable_tiers": ["frontier"]},
        "cheapest_adequate": "glm(none)",
        "cheapest_adequate_tier": "fast",
        "cheapest_adequate_effort": "none",
        "best_score": 10.0,
        "routes": [_route("glm(none)", 10.0, 1.0), _route("astra(high)", 10.0, 20.0)],
        "outcome_label": "unstable",
    }
    assert ro.label_proposals([row]) == []
    assert ro.label_proposals([{**row, "outcome_label": "stable"}])


def test_discordance_counts_only_samples_that_have_both_scores():
    import run_outcomes as ro

    rows = [
        {
            "routes": [
                _route("glm(none)", 5.0, 1.0, scores=[5.0, 5.0],
                       checks=[10.0, 10.0], judges=[10.0, 2.0]),
                _route("astra(high)", 5.0, 20.0, scores=[5.0, 5.0]),
            ]
        }
    ]
    d = ro.discordance(rows)
    assert d["n"] == 2
    assert d["gap_rate"].pct == pytest.approx(50.0)
    assert d["pass_rate"].pct == pytest.approx(50.0)


def test_discordance_of_nothing_is_empty():
    import run_outcomes as ro

    assert ro.discordance([{"routes": [_route("glm(none)", 5.0, 1.0)]}])["n"] == 0
