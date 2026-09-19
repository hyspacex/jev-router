"""The diagnostics: `sessions show`, `sessions reconcile` and `decisions`.

Read by a person, so the wording is part of the contract. "Model unchanged"
has to be said, not inferred from two columns that happen to match.
"""

from __future__ import annotations

import copy
import json

import session_harness as H
import yaml
from conftest import BASE_CONFIG, _merge
from jev_router.cli import main
from jev_router.config import load_config
from jev_router.pins import Store
from jev_router.sessions import Sessions, owner_key

ADMIN = H.ADMIN


def written(tmp_path, *, mode: str = "active"):
    """A router.yaml on disk, one adaptive binding, and one turn plan."""
    report = tmp_path / "qualification.md"
    report.write_text("# a dated deployment report\n")
    raw = copy.deepcopy(BASE_CONFIG)
    raw = _merge(raw, H.session_raw())
    raw["questions"] = _merge(raw["questions"], H.TURN_QUESTIONS)
    raw["models"]["astra"] = {
        "upstream_id": "vendor/astra",
        "context_window": 200000,
        "efforts": ["low", "medium", "high"],
        "effort_style": "param",
        "protocol": "openai-responses",
        "max_output_tokens": 8192,
        "compatibility_revision": "responses-astra-test-v1",
        "effort_control": {
            "between_turn": "native_configuration_update",
            "qualification": "verified",
            "cache_behavior": "unverified",
            "qualification_ref": str(report),
        },
    }
    raw["aliases"]["auto-adaptive"] = {
        "session_mode": "strict",
        "adaptive_effort": True,
        "state_builder": "summary_v1",
        "questions": ["task", "difficulty", "harm_if_wrong"],
        "rules": "policy",
        "allowed_models": ["astra"],
    }
    raw["experiments"] = {
        "adaptive_effort": {
            "mode": mode,
            "qualified_profiles": ["astra"],
            "ladder": ["low", "medium", "high"],
            "questions": ["difficulty", "harm_if_wrong", "corrective_followup"],
            "shadow_questions": ["failure_mode"],
        }
    }
    raw["settings"]["sqlite_path"] = str(tmp_path / "router.db")
    path = tmp_path / "router.yaml"
    path.write_text(yaml.safe_dump(raw))

    config = load_config(path)
    store = Store(config.settings.db_path)
    sessions = Sessions(store, config.session_routing, guard=False)
    sessions.create(
        {
            "session_id": "session-cli-effort",
            "owner": owner_key(ADMIN),
            "client": "coding-client",
            "alias": "auto-adaptive",
            "model_key": "astra",
            "provider": "default",
            "upstream_id": "vendor/astra",
            "wire_model": "vendor/astra",
            "protocol": "openai-responses",
            "context_window": 200000,
            "max_output_tokens": 8192,
            "supports_tools": True,
            "supports_vision": False,
            "binding_revision": "sha256:abc",
            "base_effort": "low",
            "effective_effort": "medium",
            "expected_effort": "medium",
            "confirmed_effort": "medium",
            "adaptation_mode": mode,
            "effort_mode": "fixed",
            "decision_id": "dec1",
            "decision_rule": "easy",
            "decision_source": "jev",
            "config_hash": config.config_hash,
            "state_builder": "summary_v1",
            "quota_status": json.dumps({"default": "unknown"}),
        }
    )
    sessions.activate("session-cli-effort", 1)
    sessions.record_plan(
        "session-cli-effort",
        {
            "plan_id": "plan-aaaa",
            "turn_id": "turn-0002",
            "sequence": 1,
            "mode": mode,
            "action": "change_effort",
            "status": "confirmed",
            "from_effort": "low",
            "to_effort": "medium",
            "base_effort": "low",
            "recommendation": "medium",
            "expected_effort": "medium",
            "confirmed_effort": "medium",
        },
    )
    sessions.record_plan(
        "session-cli-effort",
        {
            "plan_id": "plan-bbbb",
            "turn_id": "turn-0003",
            "sequence": 2,
            "mode": mode,
            "action": "change_effort",
            "status": "outcome_unknown",
            "from_effort": "medium",
            "to_effort": "high",
            "base_effort": "low",
            "recommendation": "high",
            "expected_effort": "high",
        },
    )
    sessions.close()
    store.close()
    return str(path), "session-cli-effort"


def test_check_config_reports_the_experiment_and_its_profiles(tmp_path, capsys):
    path, _ = written(tmp_path)
    assert main(["-c", path, "check-config"]) == 0
    out = capsys.readouterr().out
    assert "adaptive_effort=active" in out
    assert "profiles=[astra]" in out
    assert "astra: native_configuration_update verified" in out
    assert "rungs=[low, medium, high]" in out


def test_the_shipped_config_reports_the_experiment_as_off(capsys):
    assert main(["-c", "router.yaml", "check-config"]) == 0
    out = capsys.readouterr().out
    assert "adaptive_effort=off" in out
    assert "profiles=[-]" in out


def test_sessions_show_prints_the_mode_the_efforts_and_the_ledger(tmp_path, capsys):
    path, session_id = written(tmp_path)
    assert main(["-c", path, "sessions", "show", session_id]) == 0
    out = capsys.readouterr().out
    assert "base=low effective=medium expected=medium confirmed=medium" in out
    assert "adaptation=active" in out
    assert "turns:" in out
    # The wording distinguishes an effort change from a model change.
    assert "model unchanged (astra); the next user turn requests medium effort" in out
    assert "turn-0003 [outcome_unknown]" in out


def test_sessions_list_carries_the_same_summary(tmp_path, capsys):
    path, _ = written(tmp_path)
    assert main(["-c", path, "sessions", "list"]) == 0
    out = capsys.readouterr().out
    assert "adaptation=active" in out
    assert "model unchanged (astra)" in out


def test_sessions_reconcile_applies_an_ambiguous_update(tmp_path, capsys):
    path, session_id = written(tmp_path)
    assert (
        main(
            [
                "-c", path, "sessions", "reconcile", session_id,
                "--plan", "plan-bbbb", "--outcome", "applied",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "plan-bbbb: outcome_unknown -> confirmed" in out
    assert "effective=high" in out
    assert "(model unchanged)" in out


def test_sessions_reconcile_can_say_it_did_not_apply(tmp_path, capsys):
    path, session_id = written(tmp_path)
    assert (
        main(
            [
                "-c", path, "sessions", "reconcile", session_id,
                "--outcome", "not_applied", "--json",
            ]
        )
        == 0
    )
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "rejected"
    assert body["effective_effort"] == "medium"
    assert body["confirmed_effort"] == "medium"


def test_sessions_reconcile_refuses_to_guess_which_plan(tmp_path, capsys):
    path, session_id = written(tmp_path)
    store = Store(load_config(path).settings.db_path)
    sessions = Sessions(store, guard=False)
    sessions.update_plan("plan-aaaa", status="accepted")
    sessions.close()
    store.close()
    assert (
        main(["-c", path, "sessions", "reconcile", session_id, "--outcome", "applied"])
        == 2
    )
    assert "2 unsettled update(s)" in capsys.readouterr().err


def test_decisions_shows_an_effort_plan_event(tmp_path, capsys):
    path, session_id = written(tmp_path)
    config = load_config(path)
    store = Store(config.settings.db_path)
    sessions = Sessions(store, config.session_routing, guard=False)
    row_id = store.log_decision(
        decision_id="plan-aaaa",
        alias="auto-adaptive",
        client="coding-client",
        conversation_key="",
        state_builder="turn_state_v1",
        answers={},
        features={
            "action": "change_effort",
            "from_effort": "low",
            "to_effort": "medium",
            "base_effort": "low",
            "recommendation": "medium",
            "confirmations": 0,
            "needed_confirmations": 2,
        },
        config_hash=config.config_hash,
        model="astra",
        effort="medium",
        rule="effort_change_effort",
        mode="active",
        fallback=False,
        pinned=False,
        jev_ms=140.0,
        jev_tokens=120,
        est_tokens=0,
        message_count=0,
    )
    store.update_decision(row_id, response_ms=210.0)
    sessions.annotate_decision(
        row_id,
        event_type="effort_plan",
        session_id=session_id,
        turn_id="turn-0002",
        request_id="plan-turn-0002",
    )
    sessions.close()
    store.close()

    assert main(["-c", path, "decisions", "-n", "5"]) == 0
    out = capsys.readouterr().out
    assert "effort plan: low -> medium" in out
    assert "(model unchanged, base low)" in out
    assert "turn=turn-0002" in out
    assert "jev 140ms, plan end to end 210ms" in out
