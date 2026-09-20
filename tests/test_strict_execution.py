"""Managed execution against a strict binding, over the real app and store.

The scripted client in `session_harness.py` drives it: resolve once, send
model requests and tool continuations, change quota and configuration,
restart, and confirm the same profile is still there.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, CHAT, Client, Service, upstream
from test_app import JEV_URL, chat_body, mock_jev, upstream_ok

from jev_router.sessions import fingerprint as session_fingerprint


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path)
    yield svc
    await svc.close()


@pytest.fixture
def caller(service) -> Client:
    return Client(service)


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def bound(caller: Client) -> list[dict]:
    """Resolve a session and return the list the fake upstream records into."""
    mock_jev()
    seen = upstream()
    assert (await caller.resolve()).status_code == 200
    return seen


# --- acknowledgement (C01) ----------------------------------------------


@respx.mock
@pytest.mark.parametrize("drop", ["x-router-session", "x-router-binding", "x-router-request-id"])
async def test_c01_execution_without_a_full_acknowledgement_is_rejected(caller, drop):
    await bound(caller)
    headers = caller.execution_headers()
    headers[drop] = ""
    response = await caller.service.client.post(
        "/v1/chat/completions",
        json=H.user_request(caller.wire_model()),
        headers=headers,
    )
    assert response.status_code == 400
    assert code(response) == "INVALID_ROUTER_INPUT"


@respx.mock
async def test_c01_an_unknown_session_id_cannot_execute(caller):
    await bound(caller)
    stranger = Client(caller.service, session_id="session-not-resolved")
    stranger.execution = caller.execution
    stranger.binding = caller.binding
    response = await stranger.execute()
    assert response.status_code == 409
    assert code(response) == "SESSION_UNKNOWN"


@respx.mock
async def test_a_binding_that_was_never_acknowledged_is_a_conflict(caller):
    await bound(caller)
    response = await caller.execute(binding="sha256:not-the-one")
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"


@respx.mock
async def test_the_model_must_be_the_one_the_binding_names(caller):
    await bound(caller)
    response = await caller.execute(H.user_request("vendor/big(medium)"))
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"
    # The model key and the bare upstream id are both the same profile.
    assert (await caller.execute(H.user_request("small"))).status_code == 200
    assert (await caller.execute(H.user_request("vendor/small"))).status_code == 200


# --- activation and the prepared contract (C04) --------------------------


@respx.mock
async def test_prepared_becomes_active_on_the_first_accepted_headers(caller, service):
    await bound(caller)
    assert service.sessions.get(caller.session_id)["state"] == "prepared"
    assert (await caller.execute()).status_code == 200
    assert service.sessions.get(caller.session_id)["state"] == "active"


@respx.mock
async def test_c04_a_prepared_profile_survives_a_rejected_first_inference(caller, service):
    mock_jev()
    upstream(httpx.Response(429, json={"error": "slow down"}))
    await caller.resolve()
    before = service.sessions.get(caller.session_id)
    response = await caller.execute()
    assert response.status_code == 429
    after = service.sessions.get(caller.session_id)
    assert after["state"] == "prepared"
    assert after["model_key"] == before["model_key"]
    assert after["binding_revision"] == before["binding_revision"]
    # And the disclosed contract is still the one the client holds.
    resumed = await caller.resume()
    assert resumed.json()["execution"]["wire_model"] == caller.wire_model()


# --- restart, TTL and resume (C05) ---------------------------------------


@respx.mock
async def test_c05_an_active_session_survives_restart_and_the_legacy_ttl(caller, service):
    seen = await bound(caller)
    assert (await caller.execute()).status_code == 200
    before = service.sessions.get(caller.session_id)

    await service.restart()
    mock_jev()
    seen = upstream()
    # An age well past the six hour pin TTL changes nothing: a binding is not
    # a cache entry.
    service.sessions.clock = lambda: time.time() + 30 * 3600
    resumed = await caller.resume()
    assert resumed.status_code == 200
    assert resumed.json()["execution"]["wire_model"] == before["wire_model"]
    assert resumed.json()["state"] == "active"
    assert (await caller.execute()).status_code == 200
    assert seen[0]["model"] == before["wire_model"]


@respx.mock
async def test_a_prepared_contract_expires_but_an_active_one_does_not(caller, service):
    await bound(caller)
    service.sessions.clock = lambda: time.time() + 10_000
    assert code(await caller.execute()) == "SESSION_CLOSED"

    later = Client(service, session_id="session-second-one")
    service.sessions.clock = time.time
    await later.resolve()
    assert (await later.execute()).status_code == 200
    service.sessions.clock = lambda: time.time() + 10_000
    assert (await later.execute()).status_code == 200


# --- the profile does not move (C07, C08, C09, C20) ----------------------


@respx.mock
@pytest.mark.parametrize(
    "change",
    ["upstream_id", "protocol", "provider", "context_window", "compatibility_revision", "revoked", "unpermitted"],
)
async def test_c07_model_account_and_protocol_changes_are_rejected(caller, service, change):
    await bound(caller)
    assert (await caller.execute()).status_code == 200
    model = service.config.models["small"]
    if change == "upstream_id":
        model.upstream_id = "vendor/small-v2"
    elif change == "protocol":
        model.protocol = "openai-responses"
    elif change == "provider":
        service.config.providers["other"] = service.config.providers.get("other") or _provider()
        model.provider = "other"
    elif change == "context_window":
        model.context_window = 8000
    elif change == "compatibility_revision":
        model.compatibility_revision = "responses-v2"
    elif change == "revoked":
        del service.config.models["small"]
    else:
        service.config.aliases["auto-session"].allowed_models = ["big"]

    response = await caller.execute()
    assert response.status_code == 409
    assert code(response) == "PROFILE_CHANGED"
    row = service.sessions.get(caller.session_id)
    assert row["state"] == "blocked"
    # The binding is kept, not replaced.
    assert row["model_key"] == "small"
    assert row["binding_revision"] == caller.binding
    assert code(await caller.execute()) == "PROFILE_CHANGED"


def _provider():
    from jev_router.config import ProviderCfg

    return ProviderCfg(description="another account")


@respx.mock
async def test_c08_an_upstream_token_refresh_does_not_reclassify(caller, service):
    seen = await bound(caller)
    jev = respx.routes[0]
    await caller.execute(headers={"authorization": "Bearer first-token"})
    await caller.execute(headers={"authorization": "Bearer refreshed-token"})
    assert H.models_sent(seen) == [caller.wire_model()] * 2
    assert jev.call_count == 1
    assert service.sessions.get(caller.session_id)["state"] == "active"


@respx.mock
async def test_c09_quota_changes_affect_new_sessions_only(caller, service):
    seen = await bound(caller)
    await caller.execute()
    before = service.sessions.get(caller.session_id)

    # Pressure that would move a fresh admission cannot move a bound one.
    service.router.pressures = lambda: {"default": 1.0}
    assert (await caller.execute()).status_code == 200
    after = service.sessions.get(caller.session_id)
    assert (after["model_key"], after["binding_revision"]) == (
        before["model_key"],
        before["binding_revision"],
    )
    assert H.models_sent(seen) == [before["wire_model"]] * 2


@respx.mock
async def test_c20_harmless_configuration_changes_do_not_block(caller, service):
    await bound(caller)
    await caller.execute()
    # Something bigger, something unrelated, and a wider window: none of it
    # invalidates a negotiated contract.
    service.config.models["small"].context_window = 400_000
    service.config.models["small"].description = "edited"
    service.config.aliases["auto"].max_effort = "low"
    response = await caller.execute()
    assert response.status_code == 200
    row = service.sessions.get(caller.session_id)
    assert row["state"] == "active" and row["context_window"] == 32000


@respx.mock
async def test_c20_a_reduced_output_ceiling_blocks(caller, service):
    await bound(caller)
    await caller.execute()
    service.config.models["small"].max_output_tokens = 512
    assert code(await caller.execute()) == "PROFILE_CHANGED"
    assert service.sessions.get(caller.session_id)["state"] == "blocked"


# --- no cross-model fallback (C10, C34) ----------------------------------


@respx.mock
@pytest.mark.parametrize("failure", [429, 500, 503, "timeout", "connect"])
async def test_c10_a_failure_in_strict_execution_never_reaches_another_model(
    caller, service, failure
):
    mock_jev()
    await caller.resolve()
    problem = {
        "timeout": httpx.ReadTimeout("too slow"),
        "connect": httpx.ConnectError("no route"),
    }.get(failure, httpx.Response(failure if isinstance(failure, int) else 500))
    seen = upstream(problem)
    response = await caller.execute()
    assert response.status_code == (502 if isinstance(failure, str) else failure)
    # One call, one model, and no second rung.
    assert H.models_sent(seen) == [caller.wire_model()] or seen == []
    assert service.sessions.get(caller.session_id)["state"] == "prepared"


@respx.mock
async def test_c34_a_bound_session_continues_when_the_selector_is_unavailable(
    caller, service
):
    await bound(caller)
    await caller.execute()
    jev = respx.post(JEV_URL).mock(side_effect=httpx.ConnectError("typesafe is down"))
    admissions = jev.call_count
    seen = upstream()
    for _ in range(3):
        assert (await caller.execute()).status_code == 200
    assert H.models_sent(seen) == [caller.wire_model()] * 3
    # Nothing asked the classifier again, so its outage cannot matter.
    assert jev.call_count == admissions
    assert (await caller.resume()).json()["execution"]["model_key"] == "small"


# --- passthrough and telemetry (C12, C13) --------------------------------


@respx.mock
async def test_c12_unmanaged_concrete_requests_keep_the_legacy_passthrough(service):
    route = upstream_ok()
    raw = json.dumps({"model": "vendor/small", "messages": [{"role": "user", "content": "hi"}]})
    response = await service.client.post(
        "/v1/chat/completions",
        content=raw,
        headers={"content-type": "application/json", "authorization": "Bearer client-key"},
    )
    assert response.status_code == 200
    sent = route.calls[0].request
    assert sent.content.decode() == raw
    assert sent.headers["authorization"] == "Bearer client-key"
    assert "X-Router-Model" not in response.headers
    assert service.sessions.recent(5) == []


@respx.mock
async def test_c12_a_legacy_alias_still_pins_beside_a_strict_alias(service):
    mock_jev()
    upstream_ok()
    response = await service.client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert response.headers["X-Router-Pinned"] == "false"
    again = await service.client.post("/v1/chat/completions", json=chat_body())
    assert again.headers["X-Router-Pinned"] == "true"
    assert service.sessions.recent(5) == []


@respx.mock
async def test_c13_a_managed_request_is_logged_like_an_alias_request(caller, service):
    await bound(caller)
    response = await caller.execute(request_id="execution-0007")
    assert response.status_code == 200
    row = service.store.recent_decisions(1)[0]
    assert row["event_type"] == "execution"
    assert row["session_id"] == caller.session_id
    assert row["request_id"] == "execution-0007"
    assert row["model"] == "small" and row["rule"] == "session_binding"
    assert row["accepted"] == 1
    assert row["upstream_status"] == 200
    assert row["stream_state"] == "completed"
    assert response.headers["X-Router-Model"] == caller.wire_model()
    assert response.headers["X-Router-Session-State"] == "active"


@respx.mock
async def test_c13_a_managed_request_on_another_endpoint_is_refused(caller):
    await bound(caller)
    response = await caller.service.client.post(
        "/v1/responses",
        json={"model": caller.wire_model(), "input": "hi"},
        headers=caller.execution_headers(),
    )
    assert response.status_code == 422
    assert code(response) == "UNSUPPORTED_PROFILE"


@respx.mock
async def test_c13_a_managed_request_feeds_provider_health(caller, service):
    mock_jev()
    upstream(httpx.Response(503))
    await caller.resolve()
    for _ in range(4):
        await caller.execute()
    assert not service.router.breakers.healthy("default")


# --- hard constraints (C18, C19) -----------------------------------------


@respx.mock
async def test_c18_overflow_blocks_the_same_profile_instead_of_repinning(caller, service):
    mock_jev()
    seen = upstream()
    await caller.resolve(limits={"context_window": 9000})
    assert (await caller.execute()).status_code == 200
    grown = H.user_request(caller.wire_model(), text="x" * 60_000)
    response = await caller.execute(grown)
    assert response.status_code == 422
    assert code(response) == "CONTEXT_BUDGET_EXCEEDED"
    row = service.sessions.get(caller.session_id)
    # The binding is preserved, and nothing larger was chosen for it.
    assert row["model_key"] == "small" and row["state"] == "active"
    assert row["binding_revision"] == caller.binding
    assert H.models_sent(seen) == [caller.wire_model()]
    # A request that fits still works afterwards.
    assert (await caller.execute()).status_code == 200


@respx.mock
async def test_an_image_on_a_text_profile_is_reported_not_removed(caller):
    seen = await bound(caller)
    body = H.user_request(caller.wire_model())
    body["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            ],
        }
    ]
    response = await caller.execute(body)
    assert response.status_code == 422
    assert code(response) == "UNSUPPORTED_PROFILE"
    assert seen == []


@respx.mock
async def test_c19_a_maintenance_request_keeps_identity_and_admits_nothing(
    caller, service
):
    jev = mock_jev()
    seen = upstream()
    await caller.resolve()
    await caller.execute(H.tool_request(caller.wire_model()))
    await caller.execute(H.tool_continuation(caller.wire_model()))
    before = service.sessions.get(caller.session_id)

    response = await caller.execute(H.maintenance_request(caller.wire_model()))
    assert response.status_code == 200
    after = service.sessions.get(caller.session_id)
    assert (after["model_key"], after["binding_revision"], after["decision_id"]) == (
        before["model_key"],
        before["binding_revision"],
        before["decision_id"],
    )
    # One admission for the whole session, and nothing since.
    assert jev.call_count == 1
    assert H.models_sent(seen) == [caller.wire_model()] * 3
    events = [row["event_type"] for row in service.store.recent_decisions(10)]
    assert events == ["execution", "execution", "execution", "admission"]


@respx.mock
async def test_a_tool_continuation_is_the_same_session_not_a_new_one(caller, service):
    jev = mock_jev()
    seen = upstream()
    await caller.resolve()
    assert (await caller.execute(H.tool_request(caller.wire_model()))).status_code == 200
    assert (await caller.execute(H.tool_continuation(caller.wire_model()))).status_code == 200
    assert jev.call_count == 1
    assert len(service.sessions.recent(10)) == 1
    assert H.models_sent(seen) == [caller.wire_model()] * 2


# --- the forwarded body (C21) --------------------------------------------


@respx.mock
async def test_c21_routing_metadata_never_changes_system_text_tools_or_history(caller):
    seen = await bound(caller)
    sent = H.tool_continuation(caller.wire_model())
    sent["messages"].insert(0, {"role": "system", "content": "You are terse. Obey #tags."})
    sent["metadata"] = {"trace": "abc"}
    response = await caller.execute(dict(sent))
    assert response.status_code == 200
    forwarded = seen[0]
    assert forwarded["messages"] == sent["messages"]
    assert forwarded["tools"] == sent["tools"]
    assert forwarded["metadata"] == sent["metadata"]
    assert forwarded["max_tokens"] == sent["max_tokens"]
    # Only the model field is normalised onto the bound profile.
    assert set(forwarded) == set(sent)
    assert forwarded["model"] == caller.wire_model()
    for message in forwarded["messages"]:
        assert "router" not in json.dumps(message).lower()


# --- separate facts (C30) and request ids (C31) --------------------------


@respx.mock
async def test_c30_acceptance_transport_completion_and_task_success_stay_separate(
    caller, service
):
    mock_jev()

    async def broken():
        yield b'data: {"choices":[]}\n\n'
        raise httpx.ReadError("stream ended early")

    respx.post(CHAT).mock(
        return_value=httpx.Response(
            200, content=broken(), headers={"content-type": "text/event-stream"}
        )
    )
    await caller.resolve()
    with pytest.raises(httpx.ReadError):
        await caller.execute(request_id="execution-0009")

    row = service.store.recent_decisions(1)[0]
    # Accepted headers, and a transport that did not finish: two facts.
    assert row["accepted"] == 1 and row["stream_state"] == "failed"
    assert service.sessions.get(caller.session_id)["state"] == "active"
    record = service.sessions.get_request(caller.session_id, "execution-0009")
    assert record["status"] == "stream_failed"
    assert record["upstream_status"] == 200


@respx.mock
async def test_c30_a_rejected_request_is_never_recorded_as_accepted(caller, service):
    mock_jev()
    upstream(httpx.Response(500))
    await caller.resolve()
    response = await caller.execute(request_id="execution-0011")
    assert response.status_code == 500
    assert service.store.recent_decisions(1)[0]["accepted"] == 0
    record = service.sessions.get_request(caller.session_id, "execution-0011")
    assert record["status"] == "rejected" and record["upstream_status"] == 500


@respx.mock
async def test_c31_a_completed_request_id_is_never_called_again(caller, service):
    seen = await bound(caller)
    assert (await caller.execute(request_id="execution-0021")).status_code == 200
    repeat = await caller.execute(request_id="execution-0021")
    assert repeat.status_code == 409
    assert code(repeat) == "REQUEST_ALREADY_COMPLETED"
    assert len(seen) == 1


@respx.mock
async def test_c31_an_ambiguous_outcome_must_be_reconciled(caller, service):
    seen = await bound(caller)
    body = H.user_request(caller.wire_model())
    service.sessions.start_request(
        caller.session_id, "execution-0031", session_fingerprint(body)
    )
    service.sessions.finish_request(caller.session_id, "execution-0031", "unknown")
    response = await caller.execute(request_id="execution-0031")
    assert response.status_code == 409
    assert code(response) == "EXECUTION_OUTCOME_UNKNOWN"
    assert seen == []


@respx.mock
async def test_c31_a_request_id_reused_for_another_payload_is_a_conflict(caller):
    seen = await bound(caller)
    await caller.execute(request_id="execution-0041")
    response = await caller.execute(
        H.user_request(caller.wire_model(), text="something else"),
        request_id="execution-0041",
    )
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"
    assert len(seen) == 1


@respx.mock
async def test_c31_a_definitively_rejected_attempt_may_be_retried(caller):
    mock_jev()
    upstream(httpx.Response(503))
    await caller.resolve()
    assert (await caller.execute(request_id="execution-0051")).status_code == 503
    seen = upstream()
    assert (await caller.execute(request_id="execution-0051")).status_code == 200
    assert len(seen) == 1


@respx.mock
async def test_only_one_execution_per_session_is_in_flight(caller, service):
    mock_jev()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(_request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={})

    respx.post(CHAT).mock(side_effect=slow)
    await caller.resolve()
    first = asyncio.create_task(caller.execute(request_id="execution-0061"))
    try:
        await entered.wait()
        second = await caller.execute(request_id="execution-0062")
        assert second.status_code == 409
        assert code(second) == "SESSION_CONFLICT"
    finally:
        release.set()
        assert (await first).status_code == 200
    # The claim is given back, so the session is usable again.
    assert service.sessions.inflight(caller.session_id) == 0
    assert (await caller.execute(request_id="execution-0063")).status_code == 200


# --- diagnostics ---------------------------------------------------------


@respx.mock
async def test_the_session_endpoints_report_identity_state_and_adaptation(caller, service):
    await bound(caller)
    await caller.execute()
    shown = await caller.show()
    assert shown.status_code == 200
    body = shown.json()
    assert body["model_key"] == "small"
    assert body["context_window"] == 32000
    assert body["base_effort"] == "none" and body["effective_effort"] == "none"
    assert body["state"] == "active"
    assert body["adaptation"] == "off"
    assert body["decision_rule"] == "easy"
    assert set(body["quota_now"].values()) == {"unknown"}

    listing = await service.client.get(
        "/router/sessions", headers=caller.control_headers()
    )
    assert [row["session_id"] for row in listing.json()["sessions"]] == [caller.session_id]

    closed = await caller.close_session()
    assert closed.json()["state"] == "closed" and closed.json()["was"] == "active"
    assert code(await caller.execute()) == "SESSION_CLOSED"


@respx.mock
async def test_compatibility_revision_change_is_rejected_after_restart(caller, service):
    await bound(caller)
    assert (await caller.execute()).status_code == 200
    binding = caller.binding
    service.config.models["small"].compatibility_revision = "chat-small-v2"
    await service.restart()
    response = await caller.execute()
    assert response.status_code == 409
    assert code(response) == "PROFILE_CHANGED"
    row = service.sessions.get(caller.session_id)
    assert row["binding_revision"] == binding
    assert row["compatibility_revision"] == "chat-small-v1"
