"""The pure turn-plan policy and the ledger's rules.

No app, no store, no network: everything here is `effort.plan_turn` and the
status transitions, called directly with numbers.

**Owner decision, 2026-09-19.** Spec 10.5 starts an upward change at one rung
per eligible turn. The owner decided an upward change goes straight to the
rung the evidence asks for, so `low` may become `high` in a single move. The
tests below are written to that decision, and the assertions that used to
encode the one-rung climb say so where they changed. Everything else that
section lists still holds: one change per user turn, floors, a classifier
failure keeps the current effort, quota only above the floor, and a downward
change still needs low-risk evidence and its confirmations.
"""

from __future__ import annotations

import pytest

from jev_router.config import AdaptiveEffortCfg
from jev_router.effort import (
    ACCEPTED,
    CHANGE,
    CONFIRMED,
    KEEP,
    OUTCOME_UNKNOWN,
    PLANNED,
    REJECTED,
    Evidence,
    advance,
    confirmations_for,
    effective_after,
    open_change,
    plan_turn,
    recommendation_history,
)

LADDER = ["low", "medium", "high"]


def cfg(**fields):
    thresholds = fields.pop("thresholds", {})
    out = AdaptiveEffortCfg(mode="active", qualified_profiles=[], **fields)
    for name, value in thresholds.items():
        setattr(out.thresholds, name, value)
    return out


def easy(**fields):
    return Evidence(difficulty=0.2, harm_if_wrong=0.05, **fields)


def hard(**fields):
    return Evidence(difficulty=2.4, harm_if_wrong=0.2, **fields)


def middling(**fields):
    return Evidence(difficulty=1.2, harm_if_wrong=0.3, **fields)


def plan(current="medium", evidence=None, **kwargs):
    kwargs.setdefault("cfg", cfg())
    kwargs.setdefault("ladder", LADDER)
    kwargs.setdefault("base", "medium")
    kwargs.setdefault("floor", None)
    return plan_turn(current=current, evidence=evidence or middling(), **kwargs)


# --- the direction the evidence asks for ---------------------------------


def test_a_demanding_turn_raises_one_rung():
    out = plan(current="low", evidence=hard())
    assert out.action == CHANGE
    assert (out.from_effort, out.to_effort) == ("low", "medium")
    assert out.direction == "up"
    assert "difficulty 2.40" in out.reason


def test_an_undecided_turn_keeps_what_it_has():
    out = plan(current="medium", evidence=middling())
    assert out.action == KEEP
    assert out.to_effort == "medium"
    assert "asks for no change" in out.reason


def test_harm_alone_raises():
    out = plan(current="low", evidence=Evidence(difficulty=0.1, harm_if_wrong=0.9))
    assert out.action == CHANGE and out.to_effort == "medium"
    assert "harm_if_wrong" in out.reason


def test_top_mass_raises_even_when_the_mean_looks_moderate():
    # The same 1.2 mean that keeps above, with the mass sitting on level three.
    out = plan(current="low", evidence=Evidence(difficulty=1.2, harm_if_wrong=0.1, p_hard=0.5))
    assert out.action == CHANGE
    assert "top level" in out.reason


def test_a_corrective_follow_up_raises():
    out = plan(current="low", evidence=Evidence(
        difficulty=0.3, harm_if_wrong=0.1, corrective_followup=0.9
    ))
    # It used to raise one rung to medium. Owner decision, 2026-09-19: a
    # follow-up naming a defect in the earlier result asks for the top rung.
    assert out.action == CHANGE and out.to_effort == "high"
    assert "names a defect" in out.reason


def test_an_environment_failure_is_not_by_itself_a_reason_to_raise():
    out = plan(current="low", evidence=Evidence(
        difficulty=0.3,
        harm_if_wrong=0.1,
        corrective_followup=0.9,
        failure_mode="environment_problem",
    ))
    assert out.action == KEEP and out.to_effort == "low"


def test_missing_information_is_not_by_itself_a_reason_to_raise():
    out = plan(current="low", evidence=Evidence(
        difficulty=0.3,
        harm_if_wrong=0.1,
        corrective_followup=0.9,
        failure_mode="missing_information",
    ))
    assert out.action == KEEP


def test_a_reasoning_failure_with_a_corrective_follow_up_still_raises():
    out = plan(current="low", evidence=Evidence(
        difficulty=0.3,
        harm_if_wrong=0.1,
        corrective_followup=0.9,
        failure_mode="reasoning_problem",
    ))
    assert out.action == CHANGE


def test_a_short_continue_is_not_downgrade_evidence():
    out = plan(current="high", evidence=easy(short_continuation=True))
    assert out.action == KEEP
    assert out.recommendation == "high"


def test_a_corrective_follow_up_blocks_a_downgrade():
    out = plan(
        current="high",
        evidence=easy(corrective_followup=0.8),
        recommendations=["medium"],
    )
    assert out.action == KEEP


# --- how far one upward change reaches (owner decision, 2026-09-19) -------


def test_a_clearly_hard_turn_goes_straight_to_the_top_rung():
    # 2.95 is what the live run of 2026-09-19 saw on a turn a person would
    # call clearly hard. It used to land on medium and need a second turn.
    out = plan(current="low", evidence=Evidence(difficulty=2.95, harm_if_wrong=0.3))
    assert out.action == CHANGE
    assert (out.from_effort, out.to_effort) == ("low", "high")
    assert out.direction == "up"
    assert out.target == "high"
    assert "asks for high" in out.reason


def test_a_moderately_hard_turn_still_asks_for_the_middle_rung():
    out = plan(current="low", evidence=Evidence(difficulty=1.9, harm_if_wrong=0.3))
    assert out.action == CHANGE and out.to_effort == "medium"


def test_the_step_switch_restores_the_one_rung_climb():
    out = plan(
        current="low",
        evidence=Evidence(difficulty=2.95, harm_if_wrong=0.3),
        cfg=cfg(upward="step"),
    )
    assert out.action == CHANGE and out.to_effort == "medium"
    assert out.to_facts()["upward"] == "step"


def test_top_difficulty_mass_asks_for_the_top_rung():
    out = plan(current="low", evidence=Evidence(difficulty=1.0, harm_if_wrong=0.1, p_hard=0.7))
    assert out.action == CHANGE and out.to_effort == "high"
    assert "top level" in out.reason


def test_protected_work_asks_for_the_top_rung_when_it_asks_for_more():
    out = plan(
        current="low",
        evidence=Evidence(difficulty=1.9, harm_if_wrong=0.3),
        protected=True,
    )
    assert out.action == CHANGE and out.to_effort == "high"
    assert "protected rule" in out.reason


def test_protected_work_that_asks_for_nothing_still_moves_nothing():
    out = plan(current="medium", evidence=middling(), protected=True)
    assert out.action == KEEP


def test_a_jump_never_crosses_the_alias_cap():
    # The ladder the caller hands in is already the permitted one.
    out = plan(
        current="low",
        evidence=Evidence(difficulty=2.95, harm_if_wrong=0.3),
        ladder=["low", "medium"],
    )
    assert out.action == CHANGE and out.to_effort == "medium"


def test_the_target_mapping_is_configurable():
    strict = cfg(targets={"difficulty": {"medium": 1.0, "high": 1.5}})
    out = plan(current="low", evidence=Evidence(difficulty=1.9, harm_if_wrong=0.3), cfg=strict)
    assert out.action == CHANGE and out.to_effort == "high"


def test_a_rung_the_ladder_does_not_offer_is_ignored():
    odd = cfg(targets={"difficulty": {"enormous": 1.0}})
    out = plan(current="low", evidence=Evidence(difficulty=2.0, harm_if_wrong=0.3), cfg=odd)
    assert out.action == CHANGE and out.to_effort == "medium"


def test_the_target_and_the_modes_are_recorded_on_the_plan():
    out = plan(current="low", evidence=Evidence(difficulty=2.95, harm_if_wrong=0.3))
    facts = out.to_facts()
    assert facts["target"] == "high"
    assert facts["upward"] == "jump" and facts["downward"] == "step"
    assert "2.6" in facts["target_reason"]


# --- how far one downward change reaches ---------------------------------


def test_a_confirmed_downgrade_steps_one_rung():
    out = plan(current="high", evidence=easy(), recommendations=["medium"])
    assert out.action == CHANGE and out.to_effort == "medium"


def test_the_jump_switch_takes_a_downgrade_to_the_floor():
    out = plan(
        current="high",
        evidence=easy(),
        cfg=cfg(downward="jump"),
        recommendations=["low"],
    )
    assert out.action == CHANGE and out.to_effort == "low"


def test_a_downward_jump_still_stops_at_the_floor():
    out = plan(
        current="high",
        evidence=easy(),
        cfg=cfg(downward="jump"),
        floor="medium",
        recommendations=["medium"],
    )
    assert out.action == CHANGE and out.to_effort == "medium"


# --- hysteresis ----------------------------------------------------------


def test_a_downgrade_needs_two_consecutive_recommendations():
    first = plan(current="high", evidence=easy())
    assert first.action == KEEP
    assert first.recommendation == "medium"
    assert (first.confirmations, first.needed_confirmations) == (1, 2)

    second = plan(current="high", evidence=easy(), recommendations=["medium"])
    assert second.action == CHANGE and second.to_effort == "medium"
    assert second.confirmations == 2


def test_a_turn_that_asks_for_something_else_breaks_the_run():
    out = plan(
        current="high",
        evidence=easy(),
        recommendations=["medium", "high", "medium"],
    )
    assert out.confirmations == 2 and out.action == CHANGE
    broken = plan(
        current="high", evidence=easy(), recommendations=["medium", "high"]
    )
    assert broken.confirmations == 1 and broken.action == KEEP


def test_without_hysteresis_one_recommendation_is_enough():
    out = plan(current="high", evidence=easy(), cfg=cfg(hysteresis=False))
    assert out.action == CHANGE and out.to_effort == "medium"
    assert out.needed_confirmations == 1


def test_an_upgrade_never_waits_for_a_confirmation():
    out = plan(current="low", evidence=hard(), recommendations=[])
    assert out.action == CHANGE


def test_the_confirmation_count_is_configurable():
    three = cfg(downgrade_confirmations=3)
    out = plan(current="high", evidence=easy(), cfg=three, recommendations=["medium"])
    assert out.action == KEEP and out.needed_confirmations == 3
    ready = plan(
        current="high", evidence=easy(), cfg=three, recommendations=["medium", "medium"]
    )
    assert ready.action == CHANGE


# --- floors and ladders --------------------------------------------------


def test_a_floor_is_never_crossed():
    out = plan(
        current="medium", evidence=easy(), floor="medium", recommendations=["medium"]
    )
    assert out.action == KEEP
    assert out.recommendation == "medium"
    assert "the floor is already here" in out.reason


def test_a_downgrade_stops_at_the_floor():
    out = plan(
        current="high",
        evidence=easy(),
        floor="medium",
        recommendations=["medium"],
    )
    assert out.action == CHANGE and out.to_effort == "medium"


def test_an_upgrade_stops_at_the_top_of_the_ladder():
    out = plan(current="high", evidence=hard())
    assert out.action == KEEP and out.to_effort == "high"


def test_a_one_rung_ladder_never_moves():
    out = plan(current="low", evidence=hard(), ladder=["low"])
    assert out.action == KEEP
    assert "no second rung" in out.reason


def test_an_effort_that_is_not_on_the_ladder_keeps():
    out = plan(current="xhigh", evidence=easy())
    assert out.action == KEEP
    assert "not on this profile's ladder" in out.reason


# --- unknown evidence (F14) ----------------------------------------------


def test_a_classifier_error_keeps_the_current_effort():
    out = plan(current="high", evidence=Evidence(error="jev timed out"))
    assert out.action == KEEP and out.to_effort == "high"
    assert "jev timed out" in out.reason


def test_missing_answers_keep_the_current_effort():
    out = plan(current="high", evidence=Evidence(difficulty=0.1))
    assert out.action == KEEP and out.to_effort == "high"
    assert "harm_if_wrong" in out.reason


@pytest.mark.parametrize("missing", [Evidence(), Evidence(harm_if_wrong=0.0)])
def test_no_evidence_never_picks_a_cheaper_heuristic_effort(missing):
    out = plan(current="high", evidence=missing)
    assert out.to_effort == "high"


# --- quota (spec 10.5) ---------------------------------------------------


def test_quota_resolves_an_undecided_turn_downward():
    out = plan(current="high", evidence=middling(), pressure=0.9, recommendations=["medium"])
    assert out.action == CHANGE and out.to_effort == "medium"
    assert "quota pressure" in out.reason


def test_quota_cannot_make_a_demanding_turn_easy():
    out = plan(current="low", evidence=hard(), pressure=1.0)
    assert out.action == CHANGE and out.to_effort == "medium"
    assert out.direction == "up"


def test_quota_cannot_cross_the_floor():
    out = plan(
        current="medium",
        evidence=middling(),
        pressure=1.0,
        floor="medium",
        recommendations=["medium"],
    )
    assert out.action == KEEP and out.to_effort == "medium"


def test_quota_below_the_threshold_changes_nothing():
    out = plan(current="high", evidence=middling(), pressure=0.2)
    assert out.action == KEEP


def test_everything_the_plan_was_decided_under_is_recorded():
    out = plan(current="high", evidence=easy(), pressure=0.3)
    facts = out.to_facts()
    assert facts["thresholds"]["upgrade_difficulty"] == 1.8
    assert facts["ladder"] == LADDER
    assert facts["evidence"]["difficulty"] == 0.2
    assert facts["quota_pressure"] == 0.3
    assert facts["needed_confirmations"] == 2


def test_the_plan_never_names_a_model():
    assert "model" not in plan(current="low", evidence=hard()).to_facts()


# --- the ledger's rules --------------------------------------------------


def test_confirmations_count_only_the_trailing_run():
    assert confirmations_for(["low", "low"], "low") == 2
    assert confirmations_for(["low", "high", "low"], "low") == 1
    assert confirmations_for([], "low") == 0
    assert confirmations_for(["low", None], "low") == 0


@pytest.mark.parametrize(
    "status,event,expected",
    [
        (PLANNED, "accepted", ACCEPTED),
        (PLANNED, "rejected", REJECTED),
        (PLANNED, "completed", PLANNED),     # never sent, so never completed
        (PLANNED, "unknown", OUTCOME_UNKNOWN),
        (ACCEPTED, "completed", CONFIRMED),
        (ACCEPTED, "rejected", OUTCOME_UNKNOWN),
        (ACCEPTED, "unknown", OUTCOME_UNKNOWN),
        (ACCEPTED, "accepted", ACCEPTED),
        (CONFIRMED, "unknown", CONFIRMED),   # terminal
        (REJECTED, "completed", REJECTED),   # terminal
        (OUTCOME_UNKNOWN, "completed", OUTCOME_UNKNOWN),
    ],
)
def test_the_status_transitions(status, event, expected):
    assert advance(status, event) == expected


def test_only_an_unsettled_change_blocks_another_one():
    rows = [
        {"sequence": 1, "action": "change_effort", "status": CONFIRMED},
        {"sequence": 2, "action": "keep", "status": PLANNED},
    ]
    assert open_change(rows) is None
    rows.append({"sequence": 3, "action": "change_effort", "status": ACCEPTED})
    assert open_change(rows)["sequence"] == 3


def test_only_a_confirmed_change_moves_the_confirmed_effort():
    assert effective_after(
        {"status": ACCEPTED, "action": "change_effort", "to_effort": "high"}, "low"
    ) == "low"
    assert effective_after(
        {"status": CONFIRMED, "action": "change_effort", "to_effort": "high"}, "low"
    ) == "high"
    assert effective_after(
        {"status": CONFIRMED, "action": "keep", "to_effort": "high"}, "low"
    ) == "low"


def test_the_recommendation_history_is_oldest_first():
    rows = [
        {"sequence": 2, "recommendation": "medium"},
        {"sequence": 1, "recommendation": "high"},
    ]
    assert recommendation_history(rows) == ["high", "medium"]
