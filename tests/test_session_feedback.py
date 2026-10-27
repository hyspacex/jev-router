"""POST /router/sessions/{id}/feedback: the verdict, and the same-model effort step."""

from __future__ import annotations

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Client, ResponsesClient, Service
from test_app import mock_jev
from test_turn_plan import turn_jev

from jev_router.scrub import SECRET

SK = "sk-" + "note-" + "Z1x2C3v4B5n6M7a8S9d0F1g2"


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    service = Service(tmp_path)
    service.router.store.log_state = True
    yield service
    await service.close()


async def bound(svc, difficulty: float = 2.0, **kwargs) -> Client:
    """A session bound to `big` at `high` (rule `hard`), with one execution."""
    mock_jev(task="code", difficulty=difficulty)
    H.upstream()
    caller = Client(svc, **kwargs)
    response = await caller.resolve()
    assert response.status_code == 200, response.text
    assert (await caller.execute()).status_code == 200
    return caller


async def feedback(svc, session_id: str, token: str | None = ADMIN, **body) -> httpx.Response:
    return await svc.client.post(
        f"/router/sessions/{session_id}/feedback",
        json=body,
        headers={"x-router-admin-token": token} if token else {},
    )


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


@respx.mock
async def test_more_steps_effort_up_on_the_same_model_and_execution_sends_it(svc):
    caller = await bound(svc)
    before = svc.sessions.get(caller.session_id)
    assert (before["model_key"], before["base_effort"]) == ("big", "high")

    response = await feedback(svc, caller.session_id, verdict="too_weak", adjust_effort=True,
                              request_id="execution-0001", note=f"missed the race; {SK}", source="pi")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "ok": True,
        "feedback_id": body["feedback_id"],
        "session_id": caller.session_id,
        "decision_id": before["decision_id"],
        "verdict": "too_weak",
        "turn_index": 1,
        "model_key": "big",
        "previous_effort": "high",
        "current_effort": "xhigh",
        "wire_model": "vendor/big(xhigh)",
        "effort_changed": True,
        "reason_code": None,
        "reason": None,
    }
    after = svc.sessions.get(caller.session_id)
    assert after["model_key"] == "big"
    assert after["binding_revision"] == before["binding_revision"]
    for field in ("base_effort", "effective_effort", "expected_effort", "confirmed_effort"):
        assert after[field] == "xhigh", field
    assert after["feedback_verdict"] == "too_weak"

    [row] = svc.sessions.feedback_rows(caller.session_id)
    assert row["verdict"] == "too_weak" and row["better_effort"] == "xhigh"
    assert row["request_id"] == "execution-0001" and row["turn_index"] == 1
    assert row["source"] == "pi" and row["note"] == f"missed the race; {SECRET}"
    assert SK not in "\n".join(svc.router.store.conn.iterdump())

    # The next request is sent at the new effort, on the same model.
    seen = H.upstream()
    caller.execution["wire_model"] = body["wire_model"]
    assert (await caller.execute()).status_code == 200
    assert seen[-1]["model"] == "vendor/big(xhigh)"
    # A client still holding the old wire model is told, not silently served.
    stale = await caller.execute(H.user_request("vendor/big(high)"))
    assert stale.status_code == 409 and code(stale) == "SESSION_CONFLICT"
    # A resume hands back the stepped contract with the same revision.
    resumed = (await caller.resume()).json()
    assert resumed["binding_revision"] == before["binding_revision"]
    assert resumed["execution"]["initial_effort"] == "xhigh"
    assert resumed["execution"]["intended_effort"] == "high"


@respx.mock
async def test_the_ladder_ends_are_refused_and_less_steps_down(svc):
    caller = await bound(svc)
    assert (await feedback(svc, caller.session_id, verdict="too_weak", adjust_effort=True)).json()["effort_changed"]
    top = (await feedback(svc, caller.session_id, verdict="too_weak", adjust_effort=True)).json()
    assert top["effort_changed"] is False and top["current_effort"] == "xhigh"
    assert top["reason_code"] == "at_highest" and "highest" in top["reason"]
    down = (await feedback(svc, caller.session_id, verdict="too_strong", adjust_effort=True)).json()
    assert (down["previous_effort"], down["current_effort"], down["effort_changed"]) == ("xhigh", "high", True)
    # Every one was recorded, whether or not it moved anything.
    assert [r["verdict"] for r in svc.sessions.feedback_rows(caller.session_id)] == [
        "too_weak", "too_weak", "too_strong",
    ]
    assert svc.sessions.get(caller.session_id)["feedback_verdict"] == "too_strong"


@respx.mock
async def test_a_client_floor_is_never_stepped_past(svc):
    caller = await bound(svc, client="floored")
    assert svc.sessions.get(caller.session_id)["base_effort"] == "high"
    body = (await feedback(svc, caller.session_id, verdict="too_strong", adjust_effort=True)).json()
    assert body["effort_changed"] is False and body["reason_code"] == "at_lowest"


@respx.mock
async def test_right_and_unadjusted_feedback_change_nothing(svc):
    caller = await bound(svc)
    right = (await feedback(svc, caller.session_id, verdict="right", adjust_effort=True)).json()
    assert right["effort_changed"] is False and right["reason_code"] == "verdict_keeps_effort"
    plain = (await feedback(svc, caller.session_id, verdict="too_weak")).json()
    assert plain["effort_changed"] is False and plain["reason"] is None
    assert svc.sessions.get(caller.session_id)["base_effort"] == "high"


@respx.mock
async def test_a_closed_session_takes_feedback_but_not_an_effort_step(svc):
    caller = await bound(svc)
    assert (await caller.close_session()).status_code == 200
    body = (await feedback(svc, caller.session_id, verdict="too_weak", adjust_effort=True)).json()
    assert body["ok"] and body["effort_changed"] is False and body["reason_code"] == "session_closed"
    assert len(svc.sessions.feedback_rows(caller.session_id)) == 1


@respx.mock
async def test_an_active_adaptive_session_keeps_its_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    service = Service(tmp_path, config=H.adaptive_config(tmp_path))
    try:
        turn_jev()
        H.responses_upstream()
        caller = ResponsesClient(service)
        assert (await caller.start()).status_code == 200
        before = service.sessions.get(caller.session_id)
        assert before["adaptation_mode"] == "active"
        body = (await feedback(service, caller.session_id, verdict="too_weak", adjust_effort=True)).json()
        assert body["effort_changed"] is False and body["reason_code"] == "adaptive_effort_active"
        assert service.sessions.get(caller.session_id)["base_effort"] == before["base_effort"]
    finally:
        await service.close()


@respx.mock
async def test_turn_index_comes_from_the_request_or_the_current_turn(svc):
    caller = await bound(svc)
    body = H.user_request(caller.wire_model())
    body["messages"] += [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "next"}]
    assert (await caller.execute(body)).status_code == 200
    old = (await feedback(svc, caller.session_id, verdict="right", request_id="execution-0001")).json()
    assert old["turn_index"] == 1
    now = (await feedback(svc, caller.session_id, verdict="right")).json()
    assert now["turn_index"] == 2
    unknown = (await feedback(svc, caller.session_id, verdict="right", request_id="nope")).json()
    assert unknown["turn_index"] == 2


@respx.mock
@pytest.mark.parametrize("body", [
    {},
    {"verdict": "meh"},
    {"verdict": "right", "adjust_effort": "yes"},
    {"verdict": "right", "source": "Pi Agent!"},
    {"verdict": "right", "request_id": "has spaces"},
    {"verdict": "right", "note": 3},
    {"verdict": "right", "decision_id": "x"},
])
async def test_bad_bodies_are_refused_and_write_nothing(svc, body):
    caller = await bound(svc)
    response = await feedback(svc, caller.session_id, **body)
    assert response.status_code == 400 and code(response) == "INVALID_ROUTER_INPUT"
    assert svc.sessions.feedback_rows(caller.session_id) == []


@respx.mock
async def test_credential_and_session_are_checked(svc):
    caller = await bound(svc)
    # The ingress credential stops both before the endpoint runs.
    assert (await feedback(svc, caller.session_id, token=None, verdict="right")).status_code == 401
    assert (await feedback(svc, caller.session_id, token="wrong", verdict="right")).status_code == 401
    assert svc.sessions.feedback_rows(caller.session_id) == []
    unknown = await feedback(svc, "session-nobody-0000", verdict="right")
    assert unknown.status_code == 409 and code(unknown) == "SESSION_UNKNOWN"
    raw = await svc.client.post(f"/router/sessions/{caller.session_id}/feedback", content=b"{",
                                headers={"x-router-admin-token": ADMIN})
    assert raw.status_code == 400


@respx.mock
async def test_the_old_feedback_endpoint_still_works(svc):
    caller = await bound(svc)
    response = await svc.client.post(
        "/router/feedback", json={"decision_id": "last", "verdict": "too_slow"},
        headers={"x-router-admin-token": ADMIN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision_id"] == svc.sessions.get(caller.session_id)["decision_id"]


@respx.mock
async def test_the_session_list_filters_and_counts_by_feedback(svc):
    rated = await bound(svc)
    other = Client(svc, session_id="session-unrated-0001")
    assert (await other.resolve()).status_code == 200
    await feedback(svc, rated.session_id, verdict="too_weak")

    async def listed(query: str = "") -> dict:
        response = await svc.client.get(
            "/router/audit/sessions" + query, headers={"x-router-admin-token": ADMIN}
        )
        assert response.status_code == 200, response.text
        return response.json()

    assert [s["session_id"] for s in (await listed("?feedback=too_weak"))["sessions"]] == [rated.session_id]
    assert [s["session_id"] for s in (await listed("?feedback=unrated"))["sessions"]] == [other.session_id]
    assert (await listed("?feedback=right"))["sessions"] == []
    body = await listed()
    assert {f["value"]: f["n"] for f in body["facets"]["feedback"]} == {"too_weak": 1, "unrated": 1}
    row = next(s for s in body["sessions"] if s["session_id"] == rated.session_id)
    assert row["feedback_verdict"] == "too_weak"
