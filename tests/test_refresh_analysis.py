"""Offline checks for the report-local paired bootstrap calculation."""

import importlib.util
from pathlib import Path

import pytest

PATH = (
    Path(__file__).resolve().parents[1]
    / "evals/reports/2026-10-27-model-refresh/analyse.py"
)
spec = importlib.util.spec_from_file_location("refresh_analysis", PATH)
assert spec is not None and spec.loader is not None
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_paired_difference_preserves_fractional_scores():
    result = analysis.compare([9.2, 9.7, 9.8], [9.1, 9.6, 9.7])
    assert result["difference"] == pytest.approx(0.1)
    assert result["ci95"] == pytest.approx([0.1, 0.1])
    assert result["paired_centred_bootstrap_p"] < 0.01


def test_equal_pairs_do_not_imply_superiority():
    result = analysis.compare([7.5, 9.0, 10.0], [7.5, 9.0, 10.0])
    assert result["difference"] == 0.0
    assert result["paired_centred_bootstrap_p"] == 1.0


def test_pair_count_mismatch_is_not_silently_truncated():
    with pytest.raises(ValueError):
        analysis.compare([8.0, 9.0], [8.0])
