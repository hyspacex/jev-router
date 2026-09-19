"""The shared metrics, and the controls that are supposed to fail.

`evals/` is not a package, so the path is set up the way the eval scripts do.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1] / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

from metrics import (  # noqa: E402
    PERTURBATIONS,
    Rate,
    accuracy_below_confidence,
    calibration,
    chance_rate,
    cost_matched_random,
    majority_class_rate,
    min_detectable_difference,
    outcome_scores,
    paired_bootstrap_p,
    perturb,
    rate,
    stability,
    wilson,
)


# --- intervals ----------------------------------------------------------


def test_wilson_matches_known_values():
    lo, hi = wilson(80, 100)
    assert lo == pytest.approx(0.7111, abs=0.001)
    assert hi == pytest.approx(0.8666, abs=0.001)


def test_wilson_stays_inside_zero_and_one():
    for k, n in ((0, 10), (10, 10), (0, 1), (1, 1)):
        lo, hi = wilson(k, n)
        assert 0.0 <= lo <= hi <= 1.0


def test_wilson_of_nothing_is_empty():
    assert wilson(0, 0) == (0.0, 0.0)


def test_interval_narrows_as_the_case_count_grows():
    assert Rate(85, 100).half_width > Rate(850, 1000).half_width


def test_rate_skips_none():
    r = rate([True, False, None, True])
    assert r.n == 3
    assert r.pct == pytest.approx(66.667, abs=0.01)


def test_rate_cell_shows_the_interval():
    assert Rate(9, 10).cell().startswith("90.0 [")
    assert Rate(0, 0).cell() == "-"


def test_min_detectable_difference_shrinks_with_n():
    assert min_detectable_difference(24) > min_detectable_difference(171)
    assert min_detectable_difference(0) == 100.0


def test_paired_bootstrap_separates_a_real_shift_from_noise():
    same = [True] * 80 + [False] * 20
    identical = list(same)
    assert paired_bootstrap_p(same, identical) > 0.5
    better = [True] * 95 + [False] * 5
    assert paired_bootstrap_p(better, same) < 0.05


# --- calibration --------------------------------------------------------


def test_perfect_calibration_has_zero_error():
    # Two bins, and in each one the accuracy equals the claimed confidence.
    pairs = [(0.9, True)] * 9 + [(0.9, False)] * 1
    pairs += [(0.6, True)] * 6 + [(0.6, False)] * 4
    cal = calibration(pairs)
    assert cal.ece == pytest.approx(0.0, abs=0.001)
    assert cal.overconfident_by == pytest.approx(0.0, abs=0.001)


def test_overconfidence_shows_as_a_positive_gap():
    cal = calibration([(0.95, False)] * 10)
    assert cal.ece == pytest.approx(0.95, abs=0.01)
    assert cal.overconfident_by > 0


def test_calibration_of_nothing_is_zero():
    assert calibration([]).n == 0
    assert calibration([(None, True), (0.9, None)]).n == 0


def test_accuracy_below_a_cutoff_is_reported_separately():
    pairs = [(0.3, False)] * 10 + [(0.9, True)] * 10
    below, above = accuracy_below_confidence(pairs, 0.55)
    assert below.pct == 0.0
    assert above.pct == 100.0
    assert below.n == above.n == 10


# --- baselines ----------------------------------------------------------


def _cost(model, effort):
    return 1.0 if model.startswith("ollama/") else 14.0


def _tier(model):
    return "fast" if model.startswith("ollama/") else "frontier"


def test_cost_matched_random_spends_what_the_router_spends():
    acceptable = [["fast"]] * 50 + [["frontier"]] * 50
    routes = [("ollama/glm", "none")] * 50 + [("astra", "medium")] * 50
    b = cost_matched_random(acceptable, routes, _cost, _tier, draws=40)
    assert b.frontier_rate == pytest.approx(50.0)
    # The router here is perfect; random at the same spend is near chance.
    assert 35.0 < b.tier_correct < 65.0
    assert b.mean_cost == pytest.approx(7.5, abs=1.5)


def test_cost_matched_random_is_reproducible():
    acceptable = [["fast"], ["frontier"], ["fast", "frontier"]] * 10
    routes = [("ollama/glm", "none"), ("astra", "medium"), ("astra", "high")] * 10
    a = cost_matched_random(acceptable, routes, _cost, _tier, draws=10, seed=5)
    b = cost_matched_random(acceptable, routes, _cost, _tier, draws=10, seed=5)
    assert a == b


def test_cost_matched_random_handles_a_router_that_used_one_tier():
    acceptable = [["fast"]] * 10
    routes = [("ollama/glm", "none")] * 10
    b = cost_matched_random(acceptable, routes, _cost, _tier, draws=5)
    assert b.tier_correct == 100.0


def test_majority_class_and_chance_rates():
    assert majority_class_rate([["fast"]] * 7 + [["frontier"]] * 3) == pytest.approx(70.0)
    assert majority_class_rate([]) == 0.0
    # Half the labels are "a", so a shuffle matches itself 0.5^2 + 0.5^2 = 50%.
    assert chance_rate(["a"] * 5 + ["b"] * 5) == pytest.approx(50.0)
    assert chance_rate(list("abcd")) == pytest.approx(25.0)


# --- outcome scores -----------------------------------------------------


def _rows():
    return [
        {
            "routes": [
                {"route": "cheap", "score": 9.0, "cost": 1.0, "latency_ms": 100},
                {"route": "dear", "score": 10.0, "cost": 14.0, "latency_ms": 900},
            ]
        },
        {
            "routes": [
                {"route": "cheap", "score": 4.0, "cost": 1.0, "latency_ms": 120},
                {"route": "dear", "score": 10.0, "cost": 14.0, "latency_ms": 800},
            ]
        },
    ]


def test_gap_at_oracle_is_zero_for_an_oracle_router():
    s = outcome_scores(_rows(), lambda row: "dear", lambda r: 14.0)
    assert s.gap_at_oracle == pytest.approx(0.0)
    assert s.n == 2


def test_gap_at_oracle_grows_when_the_router_picks_worse():
    s = outcome_scores(_rows(), lambda row: "cheap", lambda r: 1.0)
    # (1 - 9/10) and (1 - 4/10), averaged.
    assert s.gap_at_oracle == pytest.approx(0.35)
    assert s.gain_at_best_single < 0
    assert s.best_single_route == "dear"
    assert s.mean_cost == pytest.approx(1.0)


def test_outcome_scores_skip_a_route_that_was_not_measured():
    assert outcome_scores(_rows(), lambda row: "unrun", lambda r: 1.0) is None
    assert outcome_scores([], lambda row: "cheap", lambda r: 1.0) is None


def test_latency_percentiles_come_from_the_chosen_route():
    s = outcome_scores(_rows(), lambda row: "dear", lambda r: 14.0)
    assert 800 <= s.latency_p50 <= 900


# --- perturbation and stability -----------------------------------------


TEXT = "Please fix the broken parser quickly, it is failing on empty input."


@pytest.mark.parametrize("kind", PERTURBATIONS)
def test_perturbation_is_deterministic_and_changes_the_text(kind):
    a = perturb(TEXT, kind, seed=1)
    b = perturb(TEXT, kind, seed=1)
    assert a == b
    assert a != TEXT


def test_perturbation_leaves_a_pasted_block_alone():
    text = TEXT + "\n\nTraceback (most recent call last):\n  File \"a.py\", line 3\n"
    for kind in PERTURBATIONS:
        out = perturb(text, kind, seed=2)
        assert "Traceback (most recent call last):" in out
        assert 'File "a.py", line 3' in out


def test_perturbing_empty_text_is_a_no_op():
    for kind in PERTURBATIONS:
        assert perturb("   ", kind, seed=1) == "   "


def test_unknown_perturbation_is_an_error():
    with pytest.raises(ValueError):
        perturb(TEXT, "shout", seed=1)


def test_stability_counts_both_ways():
    rows = [
        {"case_id": "a", "base": ("m", "low"), "variants": {"typos": ("m", "low"), "casing": ("m", "low")}},
        {"case_id": "b", "base": ("m", "low"), "variants": {"typos": ("m", "high"), "casing": ("m", "low")}},
    ]
    st = stability(rows)
    assert st.unchanged.k == 3 and st.unchanged.n == 4
    assert st.unchanged_all.k == 1 and st.unchanged_all.n == 2
    assert st.flipped == ("b",)
    assert st.detail[0]["moves"] == {"typos": "m(high)"}


def test_stability_of_nothing_is_empty():
    st = stability([])
    assert st.n_cases == 0
    assert st.flipped == ()
