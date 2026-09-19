"""The offline effort evals: the replay arms and the live qualification gate.

Nothing here makes a call. The qualification script is exercised only through
its planning mode, which is the only mode this repository has ever run it in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import effort_replay  # noqa: E402
import qualify_effort  # noqa: E402
import run_sessions  # noqa: E402


# --- the replay -----------------------------------------------------------


def sessions():
    return effort_replay.load_sessions(effort_replay.DEFAULT_FIXTURES)


def test_the_shipped_fixtures_are_valid_and_labelled_synthetic():
    rows = sessions()
    assert len(rows) >= 10
    assert effort_replay.validate(rows) == []
    for session in rows:
        assert session.provenance == "synthetic"
        assert session.description
    raw = yaml.safe_load(effort_replay.DEFAULT_FIXTURES.read_text())
    assert all(s["provenance"] == "synthetic" for s in raw["sessions"])


def test_a_fixture_that_claims_real_provenance_is_refused(tmp_path):
    path = tmp_path / "fixtures.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "sessions": [
                    {
                        "id": "recorded",
                        "provenance": "recorded",
                        "base": "low",
                        "turns": [{"id": "t1", "difficulty": 0.1, "wants": "low"}],
                    }
                ]
            }
        )
    )
    problems = effort_replay.validate(effort_replay.load_sessions(path))
    assert any("must not be checked in" in p for p in problems)


def test_a_label_off_the_ladder_is_refused(tmp_path):
    path = tmp_path / "fixtures.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "sessions": [
                    {
                        "id": "s",
                        "base": "low",
                        "turns": [{"id": "t1", "difficulty": 0.1, "wants": "max"}],
                    }
                ]
            }
        )
    )
    problems = effort_replay.validate(effort_replay.load_sessions(path))
    assert any("not \non the ladder" in p or "on the ladder" in p for p in problems)


@pytest.mark.parametrize("arm", effort_replay.ARMS)
def test_every_arm_runs_every_session(arm):
    rows = sessions()
    out = [row for s in rows for row in effort_replay.run_arm(arm, s)]
    assert len(out) == sum(len(s.turns) for s in rows)
    assert {r["sent"] for r in out} <= set(effort_replay.LADDER)


def test_the_fixed_arms_never_change_effort():
    rows = sessions()
    for arm in ("fixed_low", "fixed_medium", "fixed_high"):
        out = [row for s in rows for row in effort_replay.run_arm(arm, s)]
        assert not any(r["changed"] for r in out), arm


def test_the_fixed_high_arm_is_adequate_everywhere_and_costs_for_it():
    rows = sessions()
    high = effort_replay.score(
        "fixed_high", [r for s in rows for r in effort_replay.run_arm("fixed_high", s)]
    )
    low = effort_replay.score(
        "fixed_low", [r for s in rows for r in effort_replay.run_arm("fixed_low", s)]
    )
    assert high.under_rate == 0.0
    assert high.mean_over > low.mean_over
    assert low.under_rate > 0


def test_hysteresis_changes_effort_less_often_than_no_hysteresis():
    rows = sessions()
    with_h = effort_replay.score(
        "jev_hysteresis",
        [r for s in rows for r in effort_replay.run_arm("jev_hysteresis", s)],
    )
    without = effort_replay.score(
        "jev_no_hysteresis",
        [r for s in rows for r in effort_replay.run_arm("jev_no_hysteresis", s)],
    )
    assert with_h.changes < without.changes


def test_the_two_upward_modes_are_different_arms():
    """The jump reaches a clearly hard turn on the turn it arrives.

    Owner decision, 2026-09-19. The step arm is kept so the two mechanics can
    be measured against each other rather than asserted about.
    """
    session = next(s for s in sessions() if s.id == "cold-start-hard")
    jump = effort_replay.run_arm("jev_hysteresis", session)
    step = effort_replay.run_arm("jev_upward_step", session)
    assert jump[1]["sent"] == "high" and step[1]["sent"] == "medium"
    assert jump[1]["adequate"] and not step[1]["adequate"]


def test_a_floor_is_never_crossed_by_any_jev_arm():
    session = next(s for s in sessions() if s.id == "high-consequence-floor")
    for arm in ("jev_hysteresis", "jev_no_hysteresis", "jev_upward_step"):
        sent = {row["sent"] for row in effort_replay.run_arm(arm, session)}
        assert "low" not in sent, arm


def test_the_classifier_outage_session_never_drops_below_where_it_was():
    session = next(s for s in sessions() if s.id == "classifier-outage")
    rows = effort_replay.run_arm("jev_hysteresis", session)
    assert [r["sent"] for r in rows[1:3]] == ["high", "high"]


def test_the_resample_unit_is_the_session_not_the_turn():
    rows = sessions()
    out = [r for s in rows for r in effort_replay.run_arm("jev_hysteresis", s)]
    per = effort_replay.by_session(out, rows)
    assert len(per) == len(rows)
    assert all(0.0 <= value <= 1.0 for value in per)


def test_the_report_says_what_it_is_not():
    rows = sessions()
    arms = list(effort_replay.ARMS)
    per_arm = {a: [r for s in rows for r in effort_replay.run_arm(a, s)] for a in arms}
    text = effort_replay.report(
        {a: effort_replay.score(a, per_arm[a]) for a in arms},
        {a: effort_replay.by_session(per_arm[a], rows) for a in arms},
        rows,
        arms,
    )
    assert "is a measurement of a model or a deployment" in text
    assert "synthetic" in text
    assert "paired bootstrap p" in text
    assert "could not separate them" in text


def test_the_replay_writes_nothing_with_no_write(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["effort_replay.py", "--no-write"])
    assert effort_replay.main() == 0
    assert "Between-turn effort policies" in capsys.readouterr().out


# --- the live qualification gate -----------------------------------------


def plan_args(**fields):
    import argparse

    defaults = {
        "config": str(ROOT / "router.yaml"),
        "profile": "gpt-6-astra",
        "router": "http://127.0.0.1:8318",
        "upstream_key_env": "UPSTREAM_API_KEY",
        "max_calls": 12,
        "max_seconds": 600.0,
        "plan": True,
        "confirm_live": False,
        "report": None,
    }
    defaults.update(fields)
    return argparse.Namespace(**defaults)


def test_nothing_in_the_shipped_config_may_be_qualified():
    plan = qualify_effort.build_plan(plan_args())
    assert plan.problems
    assert any("not allowlisted" in p for p in plan.problems)
    assert any("declares no between-turn strategy" in p for p in plan.problems)
    assert qualify_effort.QUALIFY_ALLOWLIST == ()


def test_the_allowlist_is_not_satisfied_by_a_flag(monkeypatch):
    monkeypatch.delenv(qualify_effort.ALLOW_ENV, raising=False)
    plan = qualify_effort.build_plan(plan_args(profile="gpt-6-astra"))
    assert any("A flag alone does not authorise" in p for p in plan.problems)


def test_a_small_call_budget_refuses_the_run():
    plan = qualify_effort.build_plan(plan_args(max_calls=2))
    assert any("--max-calls is 2" in p for p in plan.problems)


def test_an_unknown_profile_is_refused():
    plan = qualify_effort.build_plan(plan_args(profile="not-a-model"))
    assert any("is not a model" in p for p in plan.problems)


def test_the_checks_cover_the_ten_the_template_lists():
    ids = [c.id for c in qualify_effort.checks()]
    assert ids == [f"Q{i:02d}" for i in range(1, 11)]
    template = qualify_effort.TEMPLATE.read_text()
    for check_id in ids:
        assert f"| {check_id} |" in template


def test_planning_makes_no_call_and_reports_what_it_would_not_establish(capsys):
    args = plan_args()
    qualify_effort.print_plan(qualify_effort.build_plan(args), args)
    out = capsys.readouterr().out
    assert "what this would NOT establish" in out
    assert "native cache preservation" in out
    assert "this run is refused" in out


def test_live_mode_needs_both_switches(monkeypatch, capsys):
    monkeypatch.delenv(qualify_effort.LIVE_ENV, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify_effort.py", "--profile", "gpt-6-astra", "--confirm-live"],
    )
    assert qualify_effort.main() == 2
    assert "two switches, on purpose" in capsys.readouterr().err


# --- the session-suite arm ------------------------------------------------


def test_the_adaptive_arm_holds_the_model_fixed():
    arm = run_sessions.POLICY_ARMS["fixed_effort_vs_adaptive"]
    assert arm["model"] == "gpt-6-astra"
    assert arm["adaptive_effort"] is True
    assert "never a cross-model gain" in arm["purpose"]


def test_the_adaptive_arm_needs_an_explicit_opt_in(capsys, monkeypatch):
    import asyncio

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_sessions.py",
            "--arms",
            "fixed_effort_vs_adaptive",
            "--dry-run",
        ],
    )
    args = _parsed_args(monkeypatch)
    assert asyncio.run(run_sessions.main_async(args)) == 2
    assert "--allow-adaptive-effort" in capsys.readouterr().err


def test_the_opt_in_flag_lets_the_arm_plan(capsys, monkeypatch):
    import asyncio

    args = _parsed_args(monkeypatch, allow_adaptive_effort=True)
    assert asyncio.run(run_sessions.main_async(args)) == 0
    assert "1 arms" in capsys.readouterr().out


def _parsed_args(monkeypatch, **fields):
    import argparse

    defaults = {
        "tasks": None,
        "task": None,
        "family": None,
        "arms": "fixed_effort_vs_adaptive",
        "repeats": 2,
        "max_calls": 12,
        "wall_clock": 300.0,
        "router": "http://127.0.0.1:8318",
        "timeout": 300.0,
        "list": False,
        "dry_run": True,
        "allow_adaptive_effort": False,
    }
    defaults.update(fields)
    return argparse.Namespace(**defaults)
