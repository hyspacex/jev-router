"""Reconciling an effort update by hand (spec 10.6, F12).

Reconciliation supplies a fact the router could not observe: whether a
submitted update reached the provider. It is not a way to declare something
true about a plan nobody ever sent, and it is not a second opinion on a plan
that already has its answer.
"""

from __future__ import annotations

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, ResponsesClient, Service
from test_turn_plan import turn_jev


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path, config=H.adaptive_config(tmp_path))
    yield svc
    await svc.close()


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def started(service, **kwargs) -> ResponsesClient:
    caller = ResponsesClient(service, **kwargs)
    response = await caller.start()
    assert response.status_code == 200, response.text
    return caller


async def submitted_and_silent(service) -> tuple[ResponsesClient, dict]:
    """One change_effort plan the provider received and never answered."""
    turn_jev(difficulty=1.9)
    H.responses_upstream(response=httpx.ReadTimeout("no answer"))
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    assert plan["action"] == "change_effort", plan
    caller.record(plan, "Investigate the escaped-quote failure.")
    assert (await caller.send(plan)).status_code == 502
    assert service.sessions.plans(caller.session_id)[-1]["status"] == "outcome_unknown"
    return caller, plan


# --- F12: what may be reconciled -----------------------------------------


@respx.mock
async def test_f12_a_plan_that_was_never_submitted_cannot_be_reconciled(service):
    """A `planned` plan has no provider-side fact to report."""
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    assert plan["action"] == "change_effort"
    before = service.sessions.get(caller.session_id)

    response = await caller.reconcile(plan["plan_id"], "applied")
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"
    assert plan["plan_id"] in response.json()["error"]["message"]
    assert response.json()["error"]["status"] == "planned"

    # Nothing moved: not the ledger row, not the session's efforts.
    after = service.sessions.get(caller.session_id)
    assert after["confirmed_effort"] == before["confirmed_effort"]
    assert after["effective_effort"] == before["effective_effort"]
    stored = service.sessions.plan_by_id(plan["plan_id"])
    assert stored["status"] == "planned"
    assert stored["confirmed_effort"] is None


@respx.mock
@pytest.mark.parametrize("outcome", ["applied", "not_applied"])
async def test_f12_a_settled_plan_is_not_reconciled_a_second_time(service, outcome):
    caller, plan = await submitted_and_silent(service)
    assert (await caller.reconcile(plan["plan_id"], "applied")).status_code == 200
    settled = service.sessions.get(caller.session_id)

    again = await caller.reconcile(plan["plan_id"], outcome)
    assert again.status_code == 409
    assert code(again) == "SESSION_CONFLICT"
    assert again.json()["error"]["status"] == "confirmed"
    assert service.sessions.get(caller.session_id)["confirmed_effort"] == (
        settled["confirmed_effort"]
    )
    assert service.sessions.plan_by_id(plan["plan_id"])["status"] == "confirmed"


@respx.mock
async def test_f12_an_accepted_plan_may_be_reconciled(service):
    """2xx headers mean the provider has it, so somebody can go and ask."""
    turn_jev(difficulty=1.9)
    H.responses_upstream(sse=True, status="failed")
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    assert (await caller.send(plan)).status_code == 200
    # A failed terminal event is not a completion, so this never confirmed.
    assert service.sessions.plans(caller.session_id)[-1]["status"] in (
        "accepted",
        "outcome_unknown",
    )

    settled = await caller.reconcile(plan["plan_id"], "applied")
    assert settled.status_code == 200
    assert settled.json()["confirmed_effort"] == "medium"


# --- F12: a reconciled update leaves a hole, not a brick -----------------


@respx.mock
async def test_f12_a_reconciled_update_still_lets_the_session_execute(service):
    """The session keeps running; only the next transition stops."""
    caller, plan = await submitted_and_silent(service)
    settled = await caller.reconcile(plan["plan_id"], "applied")
    assert settled.status_code == 200
    assert settled.json()["effective_effort"] == "medium"

    stored = service.sessions.plan_by_id(plan["plan_id"])
    assert stored["status"] == "confirmed" and stored["anchor_position"] is None

    # The client replays the history it kept, update item and all.
    H.responses_upstream()
    carried = await caller.continuation()
    assert carried.status_code == 200, carried.text


@respx.mock
async def test_f12_a_replay_that_drops_a_reconciled_update_is_still_refused(service):
    caller, plan = await submitted_and_silent(service)
    assert (await caller.reconcile(plan["plan_id"], "applied")).status_code == 200

    H.responses_upstream()
    caller.items = [item for item in caller.items if item.get("type") != "configuration_update"]
    dropped = await caller.continuation()
    assert code(dropped) == "EFFORT_HISTORY_MISMATCH"
    assert "reconciled update" in dropped.json()["error"]["message"]


@respx.mock
async def test_f12_a_reconciliation_hole_blocks_the_next_transition(service):
    caller, plan = await submitted_and_silent(service)
    assert (await caller.reconcile(plan["plan_id"], "applied")).status_code == 200

    turn_jev(difficulty=1.9)
    H.responses_upstream()
    following = await caller.plan("and now the other half", turn_id="turn-0009")
    assert following["_status"] == 200
    assert following["action"] == "blocked"
    assert plan["plan_id"] in following["reason"]
    # Blocked is not a reset: the effort the reconciliation confirmed stands.
    assert service.sessions.get(caller.session_id)["effective_effort"] == "medium"


@respx.mock
async def test_f12_not_applied_leaves_no_hole_and_adapting_carries_on(service):
    caller, plan = await submitted_and_silent(service)
    assert (await caller.reconcile(plan["plan_id"], "not_applied")).status_code == 200

    turn_jev(difficulty=1.9)
    H.responses_upstream()
    following = await caller.plan("and now the other half", turn_id="turn-0009")
    assert following["_status"] == 200
    assert following["action"] == "change_effort"
