"""Regression for punctuation-equivalent proofreading answers. No API calls."""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.checks import run_checks  # noqa: E402


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_slack_proofread_accepts_straight_and_curly_apostrophes(apostrophe):
    specs = yaml.safe_load((ROOT / "evals/outcome_specs.yaml").read_text())
    checks = specs["cases"]["polish-slack-grammar-54"]["checks"]
    reply = (
        "Hey team, the deploy window has moved to Thursday. "
        f"Please don{apostrophe}t merge into main after 3pm Wednesday "
        f"unless it{apostrophe}s a hotfix."
    )
    assert run_checks(reply, checks)[0] == 10.0


def test_slack_proofread_still_rejects_uncorrected_text():
    specs = yaml.safe_load((ROOT / "evals/outcome_specs.yaml").read_text())
    checks = specs["cases"]["polish-slack-grammar-54"]["checks"]
    reply = (
        "the deploy window is move to thursday, please dont merge "
        "after 3pm wendesday unless its a hotfix."
    )
    assert run_checks(reply, checks)[0] < 7.0
