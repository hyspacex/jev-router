"""Tests for evals/tune.py.

The eval scripts are not part of the package, so they are imported by path.
Nothing here touches the network: the Jev answers are written straight into a
temporary sqlite file in the shape the router logs them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import tune  # noqa: E402
from jev_router.pins import Store  # noqa: E402


def log_one(store: Store, *, decision_id: str, task: str, difficulty: float, harm: float,
            model: str, effort: str, has_tools: bool = False) -> None:
    store.log_decision(
        decision_id=decision_id,
        alias="auto",
        client="agent-cli",
        conversation_key="k" + decision_id,
        state_builder="continuation_aware_v1",
        answers={
            "task": {"type": "choice", "choice": task, "confidence": 0.99},
            "difficulty": {"type": "score", "score": difficulty, "confidence": 0.9},
            "harm_if_wrong": {"type": "noul", "noul": harm},
        },
        features={
            "message_count": 2,
            "est_tokens": 400,
            "has_tools": has_tools,
            "has_images": False,
            "has_code": False,
            "tool_names": [],
            "languages": [],
            "client": "agent-cli",
            "stream": False,
            "max_tokens": 0,
            "requested_model": "auto",
        },
        config_hash="abc",
        model=model,
        effort=effort,
        rule="moderate",
        mode="active",
        fallback=False,
        pinned=False,
        jev_ms=120.0,
        jev_tokens=400,
        est_tokens=400,
        message_count=2,
    )


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "router.db")
    yield store
    store.close()


def test_no_feedback_rows_means_no_replay_cases(db, tmp_path):
    log_one(db, decision_id="a1", task="code-edit", difficulty=1.0, harm=0.2,
            model="ollama/glm-5.3-flash", effort="low")
    config = tune.config_with_overlay()
    assert tune.load_feedback_cases(tmp_path / "router.db", config) == []


def test_feedback_becomes_a_case_with_the_correction_as_its_label(db, tmp_path):
    log_one(db, decision_id="a1", task="code-edit", difficulty=1.0, harm=0.2,
            model="ollama/glm-5.3-flash", effort="low")
    db.add_feedback(
        decision_id="a1",
        verdict="too_weak",
        better_model="gpt-6-astra",
        better_effort="high",
        source="cli",
    )
    config = tune.config_with_overlay()
    cases = tune.load_feedback_cases(tmp_path / "router.db", config)

    assert len(cases) == 1
    case = cases[0]
    assert case.case_id == "feedback:a1"
    assert case.acceptable_tiers == ["frontier"]   # the model the person named
    assert case.effort == "high"
    assert case.source == "feedback"
    assert case.split == "tune"
    # The stored Jev answers come back, so the policy can be replayed on them.
    assert case.answers["task"]["choice"] == "code-edit"
    assert case.answers["difficulty"]["score"] == 1.0
    assert case.features.est_tokens == 400


def test_a_verdict_with_no_model_still_moves_the_label(db, tmp_path):
    log_one(db, decision_id="b1", task="code-edit", difficulty=1.0, harm=0.2,
            model="ollama/glm-5.3-flash", effort="low")
    log_one(db, decision_id="b2", task="creative-prose", difficulty=2.5, harm=0.1,
            model="gpt-6-astra", effort="high")
    db.add_feedback(decision_id="b1", verdict="too_weak")
    db.add_feedback(decision_id="b2", verdict="too_strong")
    db.add_feedback(decision_id="b2", verdict="too_slow")

    config = tune.config_with_overlay()
    by_id = {}
    for case in tune.load_feedback_cases(tmp_path / "router.db", config):
        by_id.setdefault(case.case_id, []).append(case)

    weak = by_id["feedback:b1"][0]
    assert weak.acceptable_tiers == ["frontier"]   # too_weak moves off the fast model
    assert weak.effort == "medium"                 # one step up from low

    verdicts = sorted(c.effort for c in by_id["feedback:b2"])
    assert verdicts == ["medium", "medium"]        # too_strong and too_slow both step down


def test_right_confirms_what_was_chosen(db, tmp_path):
    log_one(db, decision_id="c1", task="quick-question", difficulty=0.2, harm=0.1,
            model="ollama/glm-5.3-flash", effort="none")
    db.add_feedback(decision_id="c1", verdict="right")
    config = tune.config_with_overlay()
    case = tune.load_feedback_cases(tmp_path / "router.db", config)[0]
    assert case.acceptable_tiers == ["fast"]
    assert case.effort == "none"


def test_feedback_counts_three_times_in_the_score(db, tmp_path):
    """One feedback row must move the metrics as much as three eval cases."""
    log_one(db, decision_id="d1", task="creative-prose", difficulty=0.3, harm=0.1,
            model="ollama/glm-5.3-flash", effort="none")
    db.add_feedback(
        decision_id="d1", verdict="too_weak", better_model="gpt-6-astra", better_effort="high"
    )
    config = tune.config_with_overlay()
    feedback = tune.load_feedback_cases(tmp_path / "router.db", config)
    assert feedback[0].weight == tune.FEEDBACK_WEIGHT == 3.0

    # Three synthetic cases the current ladder routes correctly, plus the one
    # feedback case it gets wrong. Weighted, that is 3 right and 3 wrong.
    ok = [
        tune.TuneCase(
            case_id=f"ok{i}",
            split="tune",
            answers=feedback[0].answers,
            features=feedback[0].features,
            acceptable_tiers=["fast"],
            effort="none",
        )
        for i in range(3)
    ]
    overlay = {}
    ladder = tune.Ladder()

    without = tune.score(ladder, ok, overlay)
    assert without["n"] == 3
    assert without["tier_correct"] == 100.0

    with_feedback = tune.score(ladder, ok + feedback, overlay)
    assert with_feedback["n"] == 6          # 3 eval + 3 from one weighted row
    assert with_feedback["tier_correct"] == 50.0
    assert with_feedback["under_routed"] == 50.0


def test_feedback_from_a_fallback_decision_is_skipped(db, tmp_path):
    """A fallback decision has no Jev answers worth replaying."""
    db.log_decision(
        decision_id="e1",
        alias="auto",
        client="",
        conversation_key="k",
        state_builder="continuation_aware_v1",
        answers={},
        features={},
        config_hash="abc",
        model="gpt-6-astra",
        effort="medium",
        rule="fallback",
        mode="active",
        fallback=True,
        pinned=False,
        jev_ms=None,
        jev_tokens=None,
        est_tokens=10,
        message_count=1,
    )
    db.add_feedback(decision_id="e1", verdict="too_strong")
    config = tune.config_with_overlay()
    assert tune.load_feedback_cases(tmp_path / "router.db", config) == []


def test_a_missing_database_is_not_an_error(tmp_path):
    config = tune.config_with_overlay()
    assert tune.load_feedback_cases(tmp_path / "nope.db", config) == []


# --- the ladder ---------------------------------------------------------


def test_the_ladder_renders_a_config_the_router_accepts():
    ladder = tune.Ladder(
        conf_floor=0.55, model_cutoff=1.4, hard_min=2.2, very_hard_min=3.0
    )
    metrics = tune.score(ladder, [], {})
    assert metrics == {"n": 0}     # no cases, but it built and validated

    policy = ladder.policy()
    names = [r["name"] for r in policy["rules"]]
    assert names[0] == "images_need_vision"
    assert names[-1] == "easy"
    assert policy["low_confidence"]["min_confidence"] == 0.55


def test_a_ladder_out_of_order_is_rejected():
    assert not tune.Ladder(model_cutoff=2.0, hard_min=1.6).sane()
    assert tune.Ladder(model_cutoff=1.4, hard_min=2.2, very_hard_min=3.0).sane()


def test_the_router_yaml_now_matches_the_ladder_it_was_tuned_to():
    """A guard against router.yaml and the tuner drifting apart."""
    ladder = tune.current_ladder_from_config()
    assert ladder.sane()
    assert ladder.conf_floor == 0.55
    assert ladder.model_cutoff == 1.4
    assert ladder.hard_min == 2.4


def test_the_proposed_diff_is_a_diff_and_nothing_is_written():
    path = ROOT / "router.yaml"
    before = path.read_text()
    changed = tune.Ladder(
        conf_floor=0.35, model_cutoff=1.2, hard_min=2.0, very_hard_min=2.8
    )
    diff = tune.propose_diff(changed, path)
    assert diff.startswith("---")
    assert "+    min_confidence: 0.35" in diff
    assert path.read_text() == before


def test_outcomes_widen_acceptable_tiers_but_never_narrow(tmp_path):
    cases = [
        tune.TuneCase("c1", "tune", {}, tune.features_from_facts({}), ["frontier"], "high"),
        tune.TuneCase("c2", "tune", {}, tune.features_from_facts({}), ["fast"], "none"),
    ]
    outcomes = tmp_path / "outcomes.json"
    outcomes.write_text(
        json.dumps(
            {
                "cases": [
                    {"case_id": "c1", "cheapest_adequate_tier": "fast"},
                    {"case_id": "c2", "cheapest_adequate_tier": "frontier"},
                ]
            }
        )
    )
    widened = tune.apply_outcomes(cases, outcomes)
    assert widened == 2
    assert sorted(cases[0].acceptable_tiers) == ["fast", "frontier"]
    assert sorted(cases[1].acceptable_tiers) == ["fast", "frontier"]
