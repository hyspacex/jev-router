"""Strict Responses execution while the effort experiment is on.

A scripted client keeps its own transcript in both of the histories the
experiment has to work with, and a fake Responses upstream answers with a
realistic event stream. Nothing here converts a format and nothing here
changes a model.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Client, ResponsesClient, Service
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


def updates(body: dict) -> list[dict]:
    return [
        item
        for item in body.get("input") or []
        if isinstance(item, dict) and item.get("type") == "configuration_update"
    ]


# --- the profile gate -----------------------------------------------------


@respx.mock
async def test_a_chat_binding_cannot_execute_on_the_responses_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    service = Service(tmp_path)
    try:
        turn_jev()
        H.upstream()
        caller = Client(service)
        assert (await caller.resolve()).status_code == 200
        response = await service.client.post(
            "/v1/responses",
            json={"model": caller.wire_model(), "input": "hi"},
            headers=caller.execution_headers(),
        )
        assert response.status_code == 422
        assert code(response) == "UNSUPPORTED_PROFILE"
        assert "does not convert formats" in response.json()["error"]["message"]
    finally:
        await service.close()


@respx.mock
async def test_a_responses_binding_cannot_execute_on_the_chat_endpoint(service):
    turn_jev()
    H.responses_upstream()
    caller = await started(service)
    response = await service.client.post(
        "/v1/chat/completions",
        json={"model": caller.wire_model(), "messages": []},
        headers=caller.execution_headers(),
    )
    assert response.status_code == 422
    assert code(response) == "UNSUPPORTED_PROFILE"


@respx.mock
async def test_an_unmanaged_responses_request_is_still_passed_through(service):
    route = respx.post(H.RESPONSES).mock(return_value=httpx.Response(200, json={"id": "r"}))
    raw = json.dumps({"model": "vendor/astra", "input": "hi"})
    response = await service.client.post(
        "/v1/responses",
        content=raw,
        headers={"content-type": "application/json", "authorization": "Bearer key"},
    )
    assert response.status_code == 200
    assert route.calls[0].request.content.decode() == raw
    assert "X-Router-Model" not in response.headers


# --- a whole turn ---------------------------------------------------------


@respx.mock
async def test_a_planned_change_reaches_the_provider_and_is_confirmed(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)

    plan, response = await caller.turn("Investigate the escaped-quote failure.")
    assert plan["action"] == "change_effort"
    assert response.status_code == 200

    body = seen[0]
    assert body["model"] == "vendor/astra"
    assert body["reasoning"]["effort"] == "low"        # the base does not move
    assert updates(body) == [{"type": "configuration_update", "reasoning": {"effort": "medium"}}]
    assert body["input"][-1]["role"] == "user"

    row = service.sessions.get(caller.session_id)
    assert row["effective_effort"] == "medium"
    assert row["confirmed_effort"] == "medium"
    assert row["model_key"] == "astra"
    assert row["base_effort"] == "low"
    ledger = service.sessions.plans(caller.session_id)
    assert ledger[-1]["status"] == "confirmed"
    assert ledger[-1]["response_id"] == "resp_0001"
    assert ledger[-1]["anchor_position"] == 0


# --- F06: a tool continuation is the same turn ---------------------------


@respx.mock
async def test_f06_a_tool_continuation_keeps_the_turn_and_makes_no_decision(service):
    jev = turn_jev(difficulty=1.9)
    seen = H.responses_upstream(tool_calls=True)
    caller = await started(service)
    plan, _ = await caller.turn("Investigate the escaped-quote failure.")
    calls = jev.call_count
    plans = len(service.sessions.plans(caller.session_id))

    assert (await caller.continuation()).status_code == 200
    assert (await caller.continuation("still failing")).status_code == 200

    # No fresh item, no fresh plan, no fresh classifier call.
    assert updates(seen[1]) == updates(seen[0])
    assert updates(seen[2]) == updates(seen[0])
    assert jev.call_count == calls
    assert len(service.sessions.plans(caller.session_id)) == plans
    row = service.sessions.get(caller.session_id)
    assert row["effective_effort"] == "medium"


# --- F08, F09: full replay -----------------------------------------------


@respx.mock
async def test_f09_a_full_replay_keeps_every_earlier_update_where_it_was(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    turn_jev(difficulty=0.1, harm=0.05)
    await caller.turn("thanks, now rename the helper")    # recommends low, keeps
    await caller.turn("and rename the other one")         # second confirmation
    row = service.sessions.get(caller.session_id)
    assert row["effective_effort"] == "low"

    last = seen[-1]
    positions = [
        i for i, item in enumerate(last["input"])
        if item.get("type") == "configuration_update"
    ]
    assert len(positions) == 2
    assert [last["input"][i]["reasoning"]["effort"] for i in positions] == ["medium", "low"]
    # The first one is still exactly where it was accepted.
    assert positions[0] == 0
    ledger = service.sessions.plans(caller.session_id)
    anchored = [p for p in ledger if p["action"] == "change_effort"]
    assert [p["anchor_position"] for p in anchored] == positions


@respx.mock
async def test_f09_a_replay_that_drops_an_earlier_update_is_refused(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    # The client rewrites its own history and loses the update.
    caller.items = [item for item in caller.items if item.get("type") != "configuration_update"]
    response = await caller.send()
    assert response.status_code == 409
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "missing from position 0" in response.json()["error"]["message"]


@respx.mock
async def test_f09_a_replay_that_moves_an_earlier_update_is_refused(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    caller.items.insert(0, H.user_item("an item that was never there"))
    response = await caller.send()
    assert code(response) == "EFFORT_HISTORY_MISMATCH"


@respx.mock
async def test_f08_the_new_item_goes_immediately_before_the_new_user_message(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    # The client puts it after the user message instead.
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    caller.items.append(plan["update"]["item"])
    response = await caller.send(plan)
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "immediately before" in response.json()["error"]["message"]


@respx.mock
async def test_f08_two_adjacent_updates_are_refused(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.items.append({"type": "configuration_update", "reasoning": {"effort": "medium"}})
    caller.items.append(plan["update"]["item"])
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    response = await caller.send(plan)
    assert code(response) == "EFFORT_HISTORY_MISMATCH"


@respx.mock
async def test_f08_an_update_before_an_earlier_user_message_is_refused(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.items.append(plan["update"]["item"])
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    caller.items.append(H.user_item("and one more thing"))
    response = await caller.send(plan)
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "not before the new one" in response.json()["error"]["message"]


# --- F13: one owner -------------------------------------------------------


@respx.mock
async def test_f13_a_client_authored_update_is_refused(service):
    turn_jev(difficulty=0.5)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("carry on")
    assert plan["action"] == "keep"
    # No plan asked for one, and the client writes one anyway.
    caller.items.append({"type": "configuration_update", "reasoning": {"effort": "high"}})
    caller.items.append(H.user_item("carry on"))
    response = await caller.send(plan)
    assert response.status_code == 409
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "no plan asked for" in response.json()["error"]["message"]


@respx.mock
async def test_f13_a_duplicate_of_the_planned_item_is_refused(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.items.append(plan["update"]["item"])
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    caller.items.append(dict(plan["update"]["item"]))
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    response = await caller.send(plan)
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "exactly one new effort update" in response.json()["error"]["message"]


@respx.mock
async def test_f13_the_router_never_inserts_the_item_itself(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    assert plan["action"] == "change_effort"
    # A client that ignores the plan and sends its history unchanged is
    # refused, not helpfully corrected.
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    response = await caller.send(plan)
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert seen == []


@respx.mock
async def test_the_wrong_base_effort_is_refused(service):
    turn_jev(difficulty=0.5)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("carry on")
    caller.items.append(H.user_item("carry on"))
    response = await caller.send(plan, reasoning={"effort": "high"})
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "the base setting does not move" in response.json()["error"]["message"]


# --- F10: the previous-response chain ------------------------------------


@respx.mock
async def test_f10_a_chain_sends_the_new_update_once_and_no_persisted_one(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service, history_mode="previous_response_id")

    first = await caller.turn("Investigate the escaped-quote failure.")
    assert first[1].status_code == 200
    assert "previous_response_id" not in seen[0]
    assert len(updates(seen[0])) == 1

    turn_jev(difficulty=0.5)
    _plan, response = await caller.turn("keep going on the same file")
    assert response.status_code == 200
    assert seen[1]["previous_response_id"] == "resp_0001"
    # The provider already holds the first update; the delta does not resend it.
    assert updates(seen[1]) == []


@respx.mock
async def test_f10_a_chain_with_an_unknown_parent_is_refused(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service, history_mode="previous_response_id")
    await caller.turn("Investigate the escaped-quote failure.")

    turn_jev(difficulty=0.5)
    plan = await caller.plan("keep going")
    caller.items.append(H.user_item("keep going"))
    response = await caller.send(plan, previous_response_id="resp_from_nowhere")
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "not a response this session is known to have accepted" in (
        response.json()["error"]["message"]
    )


@respx.mock
async def test_f10_a_chain_that_resends_a_persisted_update_is_refused(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service, history_mode="previous_response_id")
    await caller.turn("Investigate the escaped-quote failure.")

    turn_jev(difficulty=0.5)
    plan = await caller.plan("keep going")
    caller.items.append({"type": "configuration_update", "reasoning": {"effort": "medium"}})
    caller.items.append(H.user_item("keep going"))
    response = await caller.send(plan)
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "no plan asked for" in response.json()["error"]["message"]


# --- F11: the reported base effort ---------------------------------------


@respx.mock
async def test_f11_the_replys_reasoning_effort_never_overwrites_the_ledger(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream(base_effort="low")
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    # The provider reported the base effort, as it always does.
    assert caller.last_response["reasoning"]["effort"] == "low"
    row = service.sessions.get(caller.session_id)
    assert row["base_effort"] == "low"
    assert row["effective_effort"] == "medium"
    assert row["confirmed_effort"] == "medium"
    plan = service.sessions.plans(caller.session_id)[-1]
    assert plan["confirmed_effort"] == "medium"


# --- F12: acceptance is not application ----------------------------------


@respx.mock
async def test_f12_headers_only_acceptance_is_not_a_confirmed_change(service):
    turn_jev(difficulty=1.9)

    async def cut_short():
        yield b'event: response.created\ndata: {"type":"response.created",'
        yield b'"response":{"id":"resp_x","status":"in_progress"}}\n\n'
        raise httpx.ReadError("the stream ended early")

    respx.post(H.RESPONSES).mock(
        return_value=httpx.Response(
            200, content=cut_short(), headers={"content-type": "text/event-stream"}
        )
    )
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    with pytest.raises(httpx.ReadError):
        await caller.send(plan)

    row = service.sessions.get(caller.session_id)
    assert row["expected_effort"] == "medium"
    assert row["confirmed_effort"] == "low"
    assert row["effective_effort"] == "low"
    ledger = service.sessions.plans(caller.session_id)[-1]
    assert ledger["status"] == "outcome_unknown"


@respx.mock
async def test_f12_a_definitive_rejection_keeps_the_previous_state_and_retries(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream(response=httpx.Response(400, json={"error": "bad request"}))
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    assert (await caller.send(plan)).status_code == 400

    row = service.sessions.get(caller.session_id)
    assert row["confirmed_effort"] == "low" and row["effective_effort"] == "low"
    assert service.sessions.plans(caller.session_id)[-1]["status"] == "rejected"

    # The same plan can be sent again once the client restores the history.
    # A rejected attempt is settled, so the request id may be reused too.
    H.responses_upstream()
    again = await caller.send(plan)
    assert again.status_code == 200, again.text
    assert service.sessions.get(caller.session_id)["effective_effort"] == "medium"


@respx.mock
async def test_f12_an_ambiguous_outcome_blocks_the_next_change_until_reconciled(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream(response=httpx.ReadTimeout("no answer"))
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    assert (await caller.send(plan)).status_code == 502
    assert service.sessions.plans(caller.session_id)[-1]["status"] == "outcome_unknown"

    blocked = await caller.plan("now do the other half", turn_id="turn-0009")
    assert blocked["_status"] == 409
    assert blocked["error"]["code"] == "EXECUTION_OUTCOME_UNKNOWN"


@respx.mock
@pytest.mark.parametrize(
    "outcome,expected", [("applied", "medium"), ("not_applied", "low")]
)
async def test_f12_reconcile_settles_it_in_either_direction(service, outcome, expected):
    turn_jev(difficulty=1.9)
    H.responses_upstream(response=httpx.ReadTimeout("no answer"))
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    await caller.send(plan)

    settled = await caller.reconcile(plan["plan_id"], outcome)
    assert settled.status_code == 200
    body = settled.json()
    assert body["was"] == "outcome_unknown"
    assert body["effective_effort"] == expected
    assert body["confirmed_effort"] == expected
    row = service.sessions.get(caller.session_id)
    assert row["effective_effort"] == expected

    # And the session may plan again.
    turn_jev(difficulty=0.5)
    following = await caller.plan("carry on", turn_id="turn-0009")
    assert following["_status"] == 200
    assert following["action"] != "blocked"


# --- F17: compaction ------------------------------------------------------
#
# **Owner decision, 2026-09-19.** F17 said automatic compaction, truncation
# and the standalone compaction endpoint were all refused for the initial
# native experiment. The owner decided that a compaction the client asks for
# stays exactly what the client does natively: the router passes it through,
# records a compaction epoch, and still never compacts anything itself, never
# reads inside a provider compaction item and never changes model. What is
# still refused is a rewrite with no request of its own for the ledger to
# see - `truncation: auto` and the automatic-management fields - and that
# half of F17 is unchanged below.


@respx.mock
@pytest.mark.parametrize(
    "field", [{"truncation": "auto"}, {"context_management": {"mode": "auto"}}]
)
async def test_f17_automatic_compaction_and_truncation_are_still_refused(service, field):
    turn_jev(difficulty=0.5)
    seen = H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("carry on")
    caller.items.append(H.user_item("carry on"))
    response = await caller.send(plan, **field)
    assert response.status_code == 422
    assert code(response) == "UNSUPPORTED_PROFILE"
    assert seen == []


@respx.mock
async def test_f17_a_client_compaction_is_forwarded_unchanged(service):
    """Codex's own compaction: an ordinary request it declares as maintenance."""
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    assert updates(seen[0])  # the session is carrying an update

    response = await caller.compact()
    assert response.status_code == 200, response.text
    sent = seen[-1]
    # Forwarded as it arrived: the update this history already carries is
    # still in it, at its own position, and the router added nothing.
    assert updates(sent) == [
        {"type": "configuration_update", "reasoning": {"effort": "medium"}}
    ]
    assert sent["reasoning"]["effort"] == "low"
    assert sent["model"] == "vendor/astra"


@respx.mock
async def test_f17_a_compaction_opens_an_epoch_and_returns_to_the_base_effort(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    before = service.sessions.get(caller.session_id)
    assert before["effective_effort"] == "medium"

    response = await caller.compact()
    assert response.headers["x-router-compaction-epoch"] == "1"

    after = service.sessions.get(caller.session_id)
    assert after["compaction_epoch"] == 1
    # Measured on this deployment: an update does not survive a compaction.
    assert after["effective_effort"] == "low"
    assert after["confirmed_effort"] == "low"
    # The model and the binding did not move, and the ledger was not erased.
    assert after["model_key"] == before["model_key"]
    assert after["binding_revision"] == before["binding_revision"]
    assert len(service.sessions.plans(caller.session_id)) == 1


@respx.mock
async def test_f17_a_replay_after_a_compaction_need_not_carry_the_old_update(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    await caller.compact()

    # The client's new window has no update item in it at all, which before
    # the epoch would have been a missing update at position 0.
    turn_jev(difficulty=0.5)
    plan, response = await caller.turn("And now the simple half.")
    assert response.status_code == 200, response.text
    assert plan["action"] == "keep"
    assert updates(seen[-1]) == []


@respx.mock
async def test_f17_the_next_user_turn_may_ask_for_the_effort_again(service):
    """Post-compaction reapplication (spec 10.3)."""
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    await caller.compact()

    # A follow-up naming a defect asks for the top rung outright.
    turn_jev(difficulty=1.9, corrective=0.9)
    plan, response = await caller.turn("That is still wrong; derive it properly.")
    assert plan["action"] == "change_effort"
    assert (plan["previous_effective_effort"], plan["next_effective_effort"]) == (
        "low",
        "high",
    )
    assert response.status_code == 200
    assert updates(seen[-1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "high"}}
    ]
    row = service.sessions.get(caller.session_id)
    assert row["effective_effort"] == "high"
    assert row["compaction_epoch"] == 1


@respx.mock
async def test_f17_the_providers_explicit_trigger_flow_passes_through(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    response = await caller.compact(trigger=True, declare=False)
    assert response.status_code == 200, response.text
    # The trigger went out as the final item and nothing was added.
    assert seen[-1]["input"][-1] == {"type": "compaction_trigger"}
    # The client now replays one opaque item, and the router neither reads
    # inside it nor asks for the old update back.
    assert caller.items[0]["type"] == "compaction"
    assert service.sessions.get(caller.session_id)["compaction_epoch"] == 1

    turn_jev(difficulty=1.9, corrective=0.9)
    plan, following = await caller.turn("That is still wrong; derive it properly.")
    assert following.status_code == 200, following.text
    items = seen[-1]["input"]
    assert items[0]["type"] == "compaction"
    # The new update is after the folded-up history, which is where the
    # provider requires it.
    assert items[1] == {"type": "configuration_update", "reasoning": {"effort": "high"}}
    assert plan["next_effective_effort"] == "high"


@respx.mock
async def test_f17_a_trigger_that_is_not_the_last_item_is_refused(service):
    turn_jev(difficulty=0.5)
    seen = H.responses_upstream()
    caller = await started(service)
    caller.items = [H.trigger_item(), H.user_item("carry on")]
    response = await caller.send(headers={"x-router-request-kind": "compaction"})
    assert response.status_code == 409
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert "final input item" in response.json()["error"]["message"]
    assert seen == []


@respx.mock
async def test_f17_a_compaction_still_refuses_a_client_authored_update(service):
    """The router is the only owner of these items, compaction or not."""
    turn_jev(difficulty=0.5)
    seen = H.responses_upstream()
    caller = await started(service)
    caller.items = [
        {"type": "configuration_update", "reasoning": {"effort": "high"}},
        H.user_item("Summarise the work so far."),
    ]
    response = await caller.compact()
    assert response.status_code == 409
    assert code(response) == "EFFORT_HISTORY_MISMATCH"
    assert seen == []


@respx.mock
async def test_f17_an_ambiguous_compaction_stops_the_next_effort_change(service):
    turn_jev(difficulty=1.9)
    broken = b"event: response.completed\ndata: {not json at all\n\n"
    H.responses_upstream(body_override=broken)
    caller = await started(service)

    response = await caller.compact()
    assert response.status_code == 200
    assert response.content == broken  # the reply is untouched

    row = service.sessions.get(caller.session_id)
    assert row["compaction_state"] == "unknown"
    assert row["compaction_epoch"] == 1

    turn_jev(difficulty=1.9, corrective=0.9)
    blocked = await caller.plan("That is still wrong; derive it properly.")
    assert blocked["action"] == "blocked"
    assert "compaction" in blocked["reason"]


@respx.mock
async def test_f17_the_standalone_compaction_endpoint_is_maintenance_now(service):
    route = respx.post(H.COMPACT).mock(
        return_value=httpx.Response(
            200, json=H.response_payload("resp_c1", compaction=True)
        )
    )
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    response = await service.client.post(
        "/v1/responses/compact",
        json={
            "model": caller.wire_model(),
            "reasoning": {"effort": caller.base_effort},
            "input": list(caller.items),
        },
        headers=caller.execution_headers("compact-0001"),
    )
    assert response.status_code == 200, response.text
    assert route.called
    row = service.sessions.get(caller.session_id)
    assert row["compaction_epoch"] == 1
    assert row["effective_effort"] == "low"


@respx.mock
async def test_f18_the_safety_limit_stops_the_experiment_and_lets_a_compaction_through(
    service,
):
    """The experiment stops near the window; the way out of it does not."""
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    crowded = {"estimated_total_tokens": 190_000, "history_mode": "full_history"}
    blocked = await caller.plan("and now the hard part", context=crowded)
    assert blocked["action"] == "blocked"
    assert "safety limit" in blocked["reason"]

    # The client's answer to a full window is to compact, and that is allowed.
    response = await caller.compact()
    assert response.status_code == 200, response.text
    row = service.sessions.get(caller.session_id)
    assert row["compaction_epoch"] == 1
    assert row["model_key"] == "astra"


@respx.mock
async def test_an_unmanaged_compaction_request_is_passed_through(service):
    route = respx.post(H.COMPACT).mock(return_value=httpx.Response(200, json={"id": "r"}))
    response = await service.client.post(
        "/v1/responses/compact", json={"model": "vendor/astra"}
    )
    assert response.status_code == 200
    assert route.called


@respx.mock
async def test_the_router_never_writes_a_compaction_item_of_its_own(service):
    """Whatever else happens, the router does not build a compactor (I11).

    Codex's own compaction carries no provider item in either direction, so
    every forwarded body in this run should be free of one. If the router had
    grown a compactor of its own, this is where it would show.
    """
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    await caller.compact()
    turn_jev(difficulty=0.5)
    await caller.turn("carry on")
    for body in seen:
        kinds = [
            item.get("type")
            for item in body.get("input") or []
            if isinstance(item, dict)
        ]
        assert "compaction" not in kinds
        assert "compaction_trigger" not in kinds


# --- F19: the observer ----------------------------------------------------


@respx.mock
async def test_f19_the_observer_leaves_every_byte_alone(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    plan, response = await caller.turn("Investigate the escaped-quote failure.")
    assert response.content == seen.raw[0]


@respx.mock
async def test_f19_a_non_streamed_reply_is_observed_the_same_way(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream(sse=False)
    caller = await started(service)
    _plan, response = await caller.turn("Investigate the escaped-quote failure.")
    assert response.content == seen.raw[0]
    row = service.sessions.get(caller.session_id)
    assert row["effective_effort"] == "medium"
    assert row["last_response_id"] == "resp_0001"


@respx.mock
async def test_f19_a_parse_failure_marks_lineage_unknown_and_stops_transitions(service):
    turn_jev(difficulty=1.9)
    broken = b"event: response.completed\ndata: {not json at all\n\n"
    seen = H.responses_upstream(body_override=broken)
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    response = await caller.send(plan)

    # The reply is untouched and was not retried.
    assert response.status_code == 200
    assert response.content == broken
    assert len(seen) == 1

    row = service.sessions.get(caller.session_id)
    assert row["effort_lineage"] == "unknown"
    assert row["confirmed_effort"] == "low"
    assert service.sessions.plans(caller.session_id)[-1]["status"] == "outcome_unknown"

    blocked = await caller.plan("now the other half", turn_id="turn-0009")
    assert blocked["_status"] == 409 or blocked["action"] == "blocked"


@respx.mock
async def test_the_observer_never_keeps_output_text(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    blob = json.dumps(service.sessions.plans(caller.session_id), default=str)
    assert "Done." not in blob
    assert json.dumps(service.sessions.get(caller.session_id), default=str).count("Done.") == 0


# --- F20: the parameter strategy -----------------------------------------


@respx.mock
async def test_f20_a_parameter_change_is_never_reported_as_cache_preserving(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = ResponsesClient(service, alias="auto-adaptive-param")
    assert (await caller.start()).status_code == 200

    plan = await caller.plan("Investigate the escaped-quote failure.")
    assert plan["action"] == "change_effort"
    assert "item" not in plan["update"]
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    # The parameter, not an item: the request carries the planned effective
    # effort and the history carries no update.
    response = await caller.send(plan, reasoning={"effort": "medium"})
    assert response.status_code == 200
    assert updates(seen[0]) == []
    assert seen[0]["reasoning"]["effort"] == "medium"

    profile = service.config.models["paramy-native"]
    assert profile.effort_control.cache_behavior == "verified_breaking"
    shown = (await caller.show()).json()
    assert shown["effective_effort"] == "medium"


@respx.mock
async def test_f20_the_parameter_strategy_refuses_the_wrong_value(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = ResponsesClient(service, alias="auto-adaptive-param")
    await caller.start()
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.items.append(H.user_item("Investigate the escaped-quote failure."))
    response = await caller.send(plan, reasoning={"effort": "low"})
    assert code(response) == "EFFORT_HISTORY_MISMATCH"


# --- headers and bodies ---------------------------------------------------


@respx.mock
async def test_a_plan_id_from_another_session_is_a_conflict(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    response = await caller.send(plan, headers={"x-router-turn-plan": "not-a-plan"})
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"


@respx.mock
async def test_the_turn_header_must_match_the_plan(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    response = await caller.send(plan, headers={"x-router-turn": "turn-9999"})
    assert code(response) == "SESSION_CONFLICT"
    assert "different turns" in response.json()["error"]["message"]


@respx.mock
async def test_the_router_headers_never_reach_the_provider(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    sent = respx.routes[1].calls[0].request
    for name in (
        "x-router-admin-token",
        "x-router-session",
        "x-router-binding",
        "x-router-request-id",
        "x-router-turn",
        "x-router-turn-plan",
    ):
        assert name not in sent.headers
    assert set(seen[0]) == {"model", "reasoning", "input"}


# --- F16: disabling the experiment ---------------------------------------


@respx.mock
async def test_f16_switching_the_mode_off_keeps_the_effort_and_the_ledger(service):
    turn_jev(difficulty=1.9)
    seen = H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    assert service.sessions.get(caller.session_id)["effective_effort"] == "medium"

    service.config.experiments.adaptive_effort.mode = "off"

    # No new decisions.
    plan = await caller.plan("now do the other half")
    assert plan["action"] == "keep"
    assert plan["adaptation"] == "off"

    # The effective effort and the ledger are exactly where they were.
    row = service.sessions.get(caller.session_id)
    assert row["effective_effort"] == "medium"
    assert row["confirmed_effort"] == "medium"
    assert len(service.sessions.plans(caller.session_id)) == 1
    assert service.sessions.plans(caller.session_id)[0]["status"] == "confirmed"

    # And the earlier item is still required in a full replay.
    caller.items.append(H.user_item("now do the other half"))
    assert (await caller.send()).status_code == 200
    assert updates(seen[-1])[0]["reasoning"]["effort"] == "medium"

    dropped = [i for i in caller.items if i.get("type") != "configuration_update"]
    caller.items = dropped
    refused = await caller.send()
    assert code(refused) == "EFFORT_HISTORY_MISMATCH"


@respx.mock
async def test_f16_taking_the_alias_opt_in_away_stops_new_decisions(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    service.config.aliases["auto-adaptive"].adaptive_effort = False

    plan = await caller.plan("now do the other half")
    assert plan["adaptation"] == "off" and plan["action"] == "keep"
    assert service.sessions.get(caller.session_id)["effective_effort"] == "medium"


@respx.mock
async def test_f16_dropping_to_shadow_stops_changing_and_keeps_measuring(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")
    service.config.experiments.adaptive_effort.mode = "shadow"

    plan = await caller.plan("and the same in the other parser")
    assert plan["adaptation"] == "shadow"
    assert plan["action"] == "keep"
    assert plan["hypothetical_effort"] == "high"
    assert service.sessions.get(caller.session_id)["effective_effort"] == "medium"


# --- F15: resume ----------------------------------------------------------


@respx.mock
async def test_f15_resume_reports_base_effective_effort_and_the_mode(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    await service.restart()
    turn_jev()
    H.responses_upstream()
    resumed = await caller.resume()
    assert resumed.status_code == 200
    execution = resumed.json()["execution"]
    assert execution["base_effort"] == "low"
    assert execution["effective_effort"] == "medium"
    assert execution["adaptation"] == "active"
    assert execution["model_key"] == "astra"


@respx.mock
async def test_f15_a_downgrade_confirmation_survives_a_restart(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    turn_jev(difficulty=0.1, harm=0.05)
    first = await caller.plan("thanks, rename the helper")
    assert first["action"] == "keep"
    caller.record(first, "thanks, rename the helper")
    await caller.send(first)

    await service.restart()
    turn_jev(difficulty=0.1, harm=0.05)
    H.responses_upstream()
    await caller.resume()
    second = await caller.plan("and rename the other one")
    assert second["action"] == "change_effort"
    assert second["next_effective_effort"] == "low"


@respx.mock
async def test_f15_a_protected_floor_holds_under_quota_pressure(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream()
    caller = await started(service)
    await caller.turn("Investigate the escaped-quote failure.")

    # A pressure high enough to resolve an undecided turn downward still
    # cannot cross the floor, and cannot touch a demanding turn at all.
    service.router.pressures = lambda: {"default": 1.0}
    turn_jev(difficulty=1.9)
    plan = await caller.plan("and the same in the other parser")
    assert plan["action"] == "change_effort"
    assert plan["next_effective_effort"] == "high"


# --- the Milestone C acceptance scenario ---------------------------------


@respx.mock
async def test_milestone_c_low_high_low_changes_effort_and_nothing_else(service):
    """A low, then high, then low sequence of demands on one session."""
    seen = H.responses_upstream()
    turn_jev(difficulty=0.1, harm=0.05)
    caller = await started(service)
    binding = service.sessions.get(caller.session_id)
    assert binding["base_effort"] == "low"

    def unchanged():
        row = service.sessions.get(caller.session_id)
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
            assert row[field] == binding[field], field
        return row

    # 1. A low-demand turn. Already at the bottom rung, so nothing moves.
    plan, response = await caller.turn("rename the helper to parse_row")
    assert plan["action"] == "keep" and response.status_code == 200
    assert unchanged()["effective_effort"] == "low"

    # 2. A demanding turn. Effort goes up at the boundary, once.
    turn_jev(difficulty=1.9, harm=0.3)
    plan, response = await caller.turn(
        "Escaped quotes fail across chunk boundaries; work out why."
    )
    assert plan["action"] == "change_effort"
    assert (plan["previous_effective_effort"], plan["next_effective_effort"]) == (
        "low",
        "medium",
    )
    assert response.status_code == 200
    assert unchanged()["effective_effort"] == "medium"

    # A tool continuation inside that turn keeps it, with no new item.
    before = len(seen)
    assert (await caller.continuation()).status_code == 200
    assert updates(seen[-1]) == updates(seen[before - 1])

    # 3. Two low-demand turns. The first confirms, the second acts.
    turn_jev(difficulty=0.1, harm=0.05)
    plan, _ = await caller.turn("now tidy the imports")
    assert plan["action"] == "keep"
    assert "1 of 2 consecutive turns" in plan["reason"]
    assert unchanged()["effective_effort"] == "medium"

    plan, _ = await caller.turn("and sort them alphabetically")
    assert plan["action"] == "change_effort"
    assert plan["next_effective_effort"] == "low"
    row = unchanged()
    assert row["effective_effort"] == "low"
    assert row["confirmed_effort"] == "low"

    # The history carries every update, in the order it was accepted in, and
    # the base effort never moved.
    final = seen[-1]
    assert final["reasoning"]["effort"] == "low"
    assert [u["reasoning"]["effort"] for u in updates(final)] == ["medium", "low"]
    assert [p["status"] for p in service.sessions.plans(caller.session_id)] == [
        "confirmed"
    ] * 4
