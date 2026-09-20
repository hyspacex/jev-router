"""Permutation controls must use exactly the production eval scorer."""
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.common import load_cases, shuffle_labels
from evals.metrics import majority_class_rate, permutation_membership_null


def test_membership_chance_is_not_tuple_label_equality():
    result = permutation_membership_null(
        ["frontier"] * 4,
        [["frontier"], ["frontier", "mid"], ["fast", "frontier"], ["fast"]],
    )
    assert result["expected"] == 75
    assert result["low"] == result["high"] == 75
    assert result["p_upper"] == 1


def test_null_uses_predictions_not_assumed_label_frequencies():
    result = permutation_membership_null(["fast"] * 4,
                                         [["frontier"]] * 3 + [["fast"]])
    assert result["expected"] == 25


def test_obvious_label_leak_fails_control():
    predictions = ["fast"] * 20 + ["frontier"] * 20
    result = permutation_membership_null(predictions, [[p] for p in predictions])
    assert result["observed"] == 100
    assert result["expected"] == 50
    assert result["p_upper"] < .01


def test_mid_can_be_the_majority():
    assert majority_class_rate([["mid"], ["mid", "fast"], ["frontier"]]) == pytest.approx(200 / 3)


def test_shuffle_preserves_complete_label_bundles_and_requests():
    cases = load_cases()
    def bundle(c):
        return (c.task, c.difficulty, c.needs_faithfulness,
                tuple(c.acceptable_tiers), c.effort)
    before = [bundle(c) for c in cases if c.labelled]
    shuffled = shuffle_labels(cases)
    assert Counter(before) == Counter(bundle(c) for c in shuffled if c.labelled)
    assert [bundle(c) for c in cases if c.labelled] == before
    assert [c.body for c in shuffled] == [c.body for c in cases]
    assert [c.slice for c in shuffled] == [c.slice for c in cases]
    assert [bundle(c) for c in shuffled] == [bundle(c) for c in shuffle_labels(cases)]
    assert any(bundle(a) != bundle(b) for a, b in zip(cases, shuffled, strict=True))
