"""POST /router/turn-plan over the real app and store.

The model never changes here. Everything in this file is about whether an
effort decision is made at all, what it is allowed to say, and what it records.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Client, Service, upstream
from test_app import JEV_URL

QUESTION_ANSWERS = {
    "task": {"type": "choice", "choice": "code", "confidence": 0.9, "probabilities": {}},
    "harm_if_wrong": {"type": "noul", "noul": 0.1},
}


def turn_jev(
    difficulty=1.0,
    harm=0.1,
    corrective=0.0,
    failure_mode="no_failure_evidence",
    admission_difficulty=0.1,
):
    """One Jev mock that answers whatever the call asked for.

    Admission asks task/difficulty/harm_if_wrong; a turn boundary asks
    difficulty/harm_if_wrong/corrective_followup plus failure_mode in shadow
    when the turn carries a harness observation. The `task` question is only
    in the admission packet, which is how this tells the two apart, so a test
    can bind a session at one difficulty and then describe a turn at another.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)["questions"]
        admission = "task" in asked
        answers = {}
        for qid in asked:
            if qid == "difficulty":
                answers[qid] = {
                    "type": "score",
                    "score": admission_difficulty if admission else difficulty,
                    "confidence": 0.9,
                }
            elif qid == "corrective_followup":
                answers[qid] = {"type": "noul", "noul": corrective}
            elif qid == "harm_if_wrong":
                answers[qid] = {"type": "noul", "noul": 0.1 if admission else harm}
            elif qid == "failure_mode":
                answers[qid] = {
                    "type": "choice",
                    "choice": failure_mode,
                    "confidence": 0.8,
                }
            else:
                answers[qid] = QUESTION_ANSWERS[qid]
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 120}})

    return respx.post(JEV_URL).mock(side_effect=handler)


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path, config=H.adaptive_config(tmp_path))
    yield svc
    await svc.close()


@pytest.fixture
async def shadow_service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path, config=H.adaptive_config(tmp_path, mode="shadow"))
    yield svc
    await svc.close()


@pytest.fixture
async def off_service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path, config=H.adaptive_config(tmp_path, mode="off"))
    yield svc
    await svc.close()


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


def settle(service, caller, status="confirmed"):
    """Stand in for the execution that carries a plan out.

    The real path does this from the upstream reply; here it lets a test walk
    several turns without also driving a Responses upstream.
    """
    plan = service.sessions.plans(caller.session_id)[-1]
    service.sessions.update_plan(plan["plan_id"], status=status)
    if plan["action"] == "change_effort" and status == "confirmed":
        row = service.sessions.get(caller.session_id)
        service.sessions.update(
            caller.session_id,
            row["version"],
            effective_effort=plan["to_effort"],
            confirmed_effort=plan["to_effort"],
        )
    return plan


async def adaptive(service, *, alias="auto-adaptive", turn_boundaries=True, **kw):
    """Resolve one session that has opted in on both sides."""
    caller = Client(service, alias=alias)
    response = await caller.resolve(
        client_contract={
            "protocols": ["openai-responses"],
            "turn_boundary_reporting": turn_boundaries,
        },
        **kw,
    )
    assert response.status_code == 200, response.text
    return caller


# --- the three opt-ins (F01, F03) ----------------------------------------


@respx.mock
async def test_f01_off_adds_no_per_turn_jev_call_and_never_changes_effort(off_service):
    jev = turn_jev()
    upstream()
    caller = await adaptive(off_service)
    admissions = jev.call_count

    plan = await caller.turn_plan()
    assert plan.status_code == 200
    body = plan.json()
    assert body["action"] == "keep"
    assert body["adaptation"] == "off"
    assert body["next_effective_effort"] == body["previous_effective_effort"]
    assert body["plan_id"] == ""
    assert jev.call_count == admissions
    row = off_service.sessions.get(caller.session_id)
    assert row["effective_effort"] == row["base_effort"]
    assert off_service.sessions.plans(caller.session_id) == []


@respx.mock
async def test_f01_a_session_resolved_before_the_feature_was_on_never_gains_it(
    off_service,
):
    turn_jev()
    upstream()
    caller = await adaptive(off_service)
    # Somebody edits router.yaml and restarts into active mode. The session
    # that was resolved under `off` keeps the mode it agreed to.
    off_service.config.experiments.adaptive_effort.mode = "active"
    body = (await caller.turn_plan()).json()
    assert body["adaptation"] == "off" and body["action"] == "keep"


@respx.mock
async def test_f01_a_client_that_reports_no_turn_boundaries_gets_no_adaptation(service):
    turn_jev()
    upstream()
    caller = await adaptive(service, turn_boundaries=False)
    assert (await caller.show()).json()["adaptation"] == "off"
    assert (await caller.turn_plan()).json()["adaptation"] == "off"


@respx.mock
async def test_f03_an_alias_that_did_not_opt_in_gets_no_adaptation(service, tmp_path):
    turn_jev()
    upstream()
    service.config.aliases["auto-adaptive"].adaptive_effort = False
    caller = await adaptive(service)
    assert (await caller.show()).json()["adaptation"] == "off"


@respx.mock
async def test_f03_a_profile_nobody_qualified_cannot_activate_it(service):
    turn_jev()
    upstream()
    service.config.experiments.adaptive_effort.qualified_profiles = []
    caller = await adaptive(service)
    assert (await caller.show()).json()["adaptation"] == "off"


@respx.mock
async def test_f03_a_profile_that_declares_no_strategy_is_blocked(service):
    turn_jev()
    upstream()
    caller = await adaptive(service)
    service.config.models["astra"].effort_control.between_turn = "fixed"
    body = (await caller.turn_plan()).json()
    assert body["action"] == "blocked"
    assert "declares no between-turn effort strategy" in body["reason"]
    assert body["next_effective_effort"] == body["previous_effective_effort"]


# --- shadow (F02) ---------------------------------------------------------


@respx.mock
async def test_f02_shadow_logs_a_recommendation_and_changes_nothing(shadow_service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(shadow_service)
    before = shadow_service.sessions.get(caller.session_id)

    body = (await caller.turn_plan()).json()
    assert body["action"] == "keep"
    assert body["adaptation"] == "shadow"
    assert body["hypothetical_effort"] == "medium"
    assert body["next_effective_effort"] == before["effective_effort"]
    assert "update" not in body and "headers" not in body

    after = shadow_service.sessions.get(caller.session_id)
    assert after["effective_effort"] == before["effective_effort"]
    assert after["expected_effort"] == before["expected_effort"]
    # The recommendation is recorded, so a replay can count it.
    plan = shadow_service.sessions.plans(caller.session_id)[0]
    assert plan["recommendation"] == "medium" and plan["action"] == "keep"


# --- a change (F04) -------------------------------------------------------


@respx.mock
async def test_f04_a_change_moves_effort_and_nothing_else(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    before = service.sessions.get(caller.session_id)

    body = (await caller.turn_plan()).json()
    assert body["action"] == "change_effort", body
    assert (body["previous_effective_effort"], body["next_effective_effort"]) == (
        "low",
        "medium",
    )
    assert body["base_effort"] == "low"
    assert body["update"]["item"] == {
        "type": "configuration_update",
        "reasoning": {"effort": "medium"},
    }
    assert "immediately before" in body["update"]["rule"]
    assert body["headers"]["X-Router-Turn-Plan"] == body["plan_id"]

    after = service.sessions.get(caller.session_id)
    for field in (
        "model_key",
        "provider",
        "upstream_id",
        "protocol",
        "context_window",
        "max_output_tokens",
        "binding_revision",
        "base_effort",
    ):
        assert after[field] == before[field], field
    # Expected moves, confirmed does not: nothing has run yet.
    assert after["expected_effort"] == "medium"
    assert after["confirmed_effort"] == "low"
    assert after["effective_effort"] == "low"


@respx.mock
async def test_the_parameter_strategy_returns_a_parameter_not_an_item(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service, alias="auto-adaptive-param")
    body = (await caller.turn_plan()).json()
    assert body["action"] == "change_effort"
    assert "item" not in body["update"]
    assert body["update"]["parameter"] == "reasoning.effort"
    assert body["update"]["value"] == body["next_effective_effort"]


# --- settled turns (F05) --------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    "previous",
    [
        {"id": "turn-0001", "settled": False},
        {"id": "turn-0001", "settled": True, "pending_tool_calls": 2},
        {"id": "turn-0001", "settled": True, "active_requests": 1},
    ],
)
async def test_f05_no_plan_while_the_previous_turn_is_unresolved(service, previous):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    response = await caller.turn_plan(previous=previous)
    assert response.status_code == 409
    assert code(response) == "TURN_NOT_SETTLED"
    assert service.sessions.plans(caller.session_id) == []


@respx.mock
async def test_f05_no_plan_while_a_request_is_in_flight(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    service.sessions.claim(caller.session_id)
    try:
        response = await caller.turn_plan()
        assert code(response) == "TURN_NOT_SETTLED"
    finally:
        service.sessions.release(caller.session_id)
    assert (await caller.turn_plan()).status_code == 200


@respx.mock
async def test_f05_the_first_turn_of_a_session_needs_no_previous_one(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    response = await caller.turn_plan(previous={})
    assert response.status_code == 200


# --- idempotency (F07) ----------------------------------------------------


@respx.mock
async def test_f07_the_same_plan_request_twice_is_one_decision(service):
    jev = turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    first = (await caller.turn_plan()).json()
    calls = jev.call_count

    second = (await caller.turn_plan(request_id="plan-retry")).json()
    assert second == first
    assert jev.call_count == calls
    assert len(service.sessions.plans(caller.session_id)) == 1


@respx.mock
async def test_f07_the_same_turn_with_different_evidence_is_a_conflict(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    await caller.turn_plan()
    response = await caller.turn_plan(request="something else entirely")
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"
    assert len(service.sessions.plans(caller.session_id)) == 1


@respx.mock
async def test_f07_only_one_change_per_user_turn(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    assert (await caller.turn_plan("turn-0002")).json()["action"] == "change_effort"
    # While that change is still in the air, another one is blocked.
    held = (await caller.turn_plan("turn-0003")).json()
    assert held["action"] == "blocked" and "one change at a time" in held["reason"]
    # Once it has been carried out, the next turn may plan again.
    settle(service, caller)
    second = await caller.turn_plan("turn-0004")
    assert second.status_code == 200
    plans = service.sessions.plans(caller.session_id)
    assert [p["turn_id"] for p in plans] == ["turn-0002", "turn-0004"]
    assert [p["sequence"] for p in plans] == [1, 2]


# --- unknown evidence (F14) -----------------------------------------------


@respx.mock
async def test_f14_a_classifier_timeout_keeps_the_current_effort(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    respx.post(JEV_URL).mock(side_effect=httpx.ConnectTimeout("typesafe is slow"))

    body = (await caller.turn_plan()).json()
    assert body["action"] == "keep"
    assert body["next_effective_effort"] == "low"
    assert "jev" in body["reason"].lower()


@respx.mock
async def test_f14_malformed_output_keeps_the_current_effort_not_a_lower_one(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service, alias="auto-adaptive")
    # Get it up to medium first, so "keep" and "lower" are different answers.
    assert (await caller.turn_plan("turn-0002")).json()["action"] == "change_effort"
    settle(service, caller)
    respx.post(JEV_URL).mock(return_value=httpx.Response(200, json={"answers": "nonsense"}))

    body = (await caller.turn_plan("turn-0003")).json()
    assert body["action"] == "keep"
    assert body["next_effective_effort"] == "medium"


# --- the context safety limit (F18) ---------------------------------------


@respx.mock
async def test_f18_approaching_the_context_limit_stops_the_experiment(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    before = service.sessions.get(caller.session_id)

    body = (
        await caller.turn_plan(context={"estimated_total_tokens": 190_000})
    ).json()
    assert body["action"] == "blocked"
    assert "safety limit" in body["reason"]
    assert body["next_effective_effort"] == before["effective_effort"]

    after = service.sessions.get(caller.session_id)
    assert after["model_key"] == before["model_key"]
    assert after["binding_revision"] == before["binding_revision"]
    assert after["effective_effort"] == before["effective_effort"]


# --- floors (F15) ---------------------------------------------------------


@respx.mock
async def test_f15_a_client_minimum_effort_is_a_floor(service):
    # The shared test config's `floored` client asks for high effort or better.
    turn_jev(difficulty=0.1, harm=0.05)
    upstream()
    caller = Client(service, alias="auto-adaptive", client="floored")
    assert (
        await caller.resolve(
            client_contract={
                "protocols": ["openai-responses"],
                "turn_boundary_reporting": True,
            }
        )
    ).status_code == 200
    row = service.sessions.get(caller.session_id)
    assert row["base_effort"] == "high"

    for turn in ("turn-0002", "turn-0003", "turn-0004"):
        body = (await caller.turn_plan(turn)).json()
        assert body["action"] == "keep", body
        assert body["next_effective_effort"] == "high"


@respx.mock
async def test_a_downgrade_needs_two_eligible_turns(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    assert (await caller.turn_plan("turn-0002")).json()["action"] == "change_effort"
    settle(service, caller)

    turn_jev(difficulty=0.1, harm=0.05)
    first = (await caller.turn_plan("turn-0003")).json()
    assert first["action"] == "keep"
    assert "1 of 2 consecutive turns" in first["reason"]
    assert service.sessions.plans(caller.session_id)[-1]["recommendation"] == "low"

    second = (await caller.turn_plan("turn-0004")).json()
    assert second["action"] == "change_effort"
    assert second["next_effective_effort"] == "low"


# --- the record it leaves -------------------------------------------------


@respx.mock
async def test_a_plan_is_logged_as_its_own_decision_event(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    body = (await caller.turn_plan()).json()

    row = service.store.recent_decisions(1)[0]
    assert row["event_type"] == "effort_plan"
    assert row["session_id"] == caller.session_id
    assert row["turn_id"] == "turn-0002"
    assert row["decision_id"] == body["plan_id"]
    assert row["model"] == "astra"
    assert row["effort"] == "medium"
    assert row["rule"] == "effort_change_effort"
    assert row["intended_model"] == "astra"
    # Jev latency for the plan and the end-to-end plan time are separate.
    assert row["jev_ms"] is not None
    assert row["response_ms"] is not None
    assert row["features"]["thresholds"]["upgrade_difficulty"] == 1.8


@respx.mock
async def test_the_shadow_failure_mode_is_only_asked_with_a_harness_observation(service):
    jev = turn_jev(difficulty=1.0, failure_mode="environment_problem")
    upstream()
    caller = await adaptive(service)

    await caller.turn_plan("turn-0002")
    asked = json.loads(jev.calls[-1].request.content)["questions"]
    assert "failure_mode" not in asked
    settle(service, caller)

    jev = turn_jev(difficulty=1.0, corrective=0.9, failure_mode="environment_problem")
    await caller.turn_plan(
        "turn-0003",
        observations=[
            {
                "source": "harness",
                "kind": "test_result",
                "status": "failed",
                "summary": "pip install refused: no network",
            }
        ],
    )
    asked = json.loads(jev.calls[-1].request.content)["questions"]
    assert "failure_mode" in asked
    # And it did its job as an experiment. It is a shadow answer, so it is
    # measured and never applied (C25): the plan raises on the active answers
    # alone, and the counterfactual records that an environment failure would
    # have held it.
    plan = service.sessions.plans(caller.session_id)[-1]
    assert plan["action"] == "change_effort"
    assert plan["facts"]["shadow_counterfactual"] == {
        "question": "failure_mode",
        "answer": "environment_problem",
        "action": "keep",
        "to_effort": "low",
        "recommendation": "low",
        "reason": "keeping low: the evidence asks for no change",
        "changed": True,
    }


@respx.mock
async def test_a_plan_stores_no_message_text(service):
    turn_jev(difficulty=1.9)
    upstream()
    caller = await adaptive(service)
    await caller.turn_plan(request="the secret passphrase is hunter2")
    blob = json.dumps(service.sessions.plans(caller.session_id), default=str)
    assert "hunter2" not in blob
    rows = json.dumps(service.store.recent_decisions(5), default=str)
    assert "hunter2" not in rows


# --- authentication and validation ---------------------------------------


@respx.mock
async def test_a_turn_plan_needs_the_control_credential(service):
    turn_jev()
    upstream()
    caller = await adaptive(service)
    caller.token = ""
    response = await caller.turn_plan()
    # The ingress boundary refuses every /router endpoint without it, before
    # any of this code is reached.
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "router_auth"


@respx.mock
async def test_an_unknown_field_is_refused(service):
    turn_jev()
    upstream()
    caller = await adaptive(service)
    response = await caller.turn_plan(model="something-else")
    assert response.status_code == 400
    assert code(response) == "INVALID_ROUTER_INPUT"


@respx.mock
async def test_a_wrong_binding_is_a_conflict(service):
    turn_jev()
    upstream()
    caller = await adaptive(service)
    response = await caller.turn_plan(binding="sha256:not-the-one")
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"


@respx.mock
async def test_an_unknown_session_cannot_plan_a_turn(service):
    turn_jev()
    upstream()
    await adaptive(service)
    stranger = Client(service, session_id="session-never-resolved")
    stranger.binding = "sha256:whatever"
    response = await stranger.turn_plan()
    assert response.status_code == 409
    assert code(response) == "SESSION_UNKNOWN"
