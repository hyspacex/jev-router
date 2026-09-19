"""The strict session contract: resolve, resume, limits, and the store.

Test IDs from the specification's offline coverage table are in the test
names, so C07 is findable by grepping for c07.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
import session_harness as H
from conftest import make_config
from session_harness import ADMIN, Client, Service, session_config, upstream
from test_app import JEV_URL, jev_answers, mock_jev

from jev_router.config import RouterConfig, SessionRoutingCfg, load_config
from jev_router.features import estimate_input, text_tokens
from jev_router.pins import Store
from jev_router.policy import MIN_RESERVE_TOKENS, context_budget
from jev_router.sessions import (
    ERROR_STATUS,
    Sessions,
    StrictServiceBusy,
    binding_digest,
    owner_key,
)


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


# --- the resolve contract ------------------------------------------------


@respx.mock
async def test_resolve_returns_one_fixed_profile_and_its_limits(caller):
    mock_jev()
    response = await caller.resolve()
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1"
    assert body["state"] == "prepared"
    assert body["binding_revision"].startswith("sha256:")
    execution = body["execution"]
    assert execution["model_key"] == "small"
    assert execution["wire_model"] == "vendor/small(none)"
    assert execution["protocol"] == "openai-chat"
    assert execution["context_window"] == 32000
    assert execution["max_output_tokens"] == 2048
    assert execution["initial_effort"] == "none"
    assert execution["effort_mode"] == "fixed"
    assert body["decision"]["source"] == "jev"
    # Nothing polls quota, so nothing is known about it. Unknown is reported.
    assert set(body["decision"]["quota_status"].values()) == {"unknown"}
    assert body["limits"]["reserve_tokens"] >= MIN_RESERVE_TOKENS


@respx.mock
async def test_c02_fresh_resolve_selects_once_and_a_retry_returns_the_stored_result(
    caller, service
):
    jev = mock_jev()
    first = await caller.resolve()
    assert jev.call_count == 1
    second = await caller.resolve(request_id="resolve-0002")
    assert second.status_code == 200
    assert second.json()["binding_revision"] == first.json()["binding_revision"]
    assert second.json()["decision_id"] == first.json()["decision_id"]
    # A retry costs nothing and creates nothing.
    assert jev.call_count == 1
    assert len(service.store.recent_decisions(10)) == 1


@respx.mock
async def test_c02_a_resolve_writes_one_admission_event_to_the_decision_log(
    caller, service
):
    mock_jev()
    await caller.resolve()
    row = service.store.recent_decisions(1)[0]
    assert row["event_type"] == "admission"
    assert row["session_id"] == caller.session_id
    assert row["request_id"] == "resolve-0001"
    assert row["model"] == "small"
    assert row["features"]["session_mode"] == "strict"
    assert row["features"]["reserve_tokens"] >= MIN_RESERVE_TOKENS


@respx.mock
async def test_c03_concurrent_resolves_commit_one_binding(caller, service):
    jev = mock_jev()
    first, second = await asyncio.gather(caller.resolve(), caller.resolve())
    assert first.status_code == second.status_code == 200
    assert first.json()["binding_revision"] == second.json()["binding_revision"]
    assert first.json()["decision_id"] == second.json()["decision_id"]
    # Both may ask the classifier; only one binding is ever written.
    assert jev.call_count <= 2
    assert len(service.sessions.recent(10)) == 1


@respx.mock
async def test_c03_a_conflicting_payload_for_the_same_session_id_is_409(caller):
    mock_jev()
    await caller.resolve()
    clash = await caller.resolve(request={"messages": [{"role": "user", "content": "other"}]})
    assert clash.status_code == 409
    assert code(clash) == "SESSION_CONFLICT"


@respx.mock
async def test_resume_returns_the_stored_contract_without_a_classifier_call(caller):
    jev = mock_jev()
    first = await caller.resolve()
    again = await caller.resume()
    assert again.status_code == 200
    assert again.json()["execution"] == first.json()["execution"]
    assert jev.call_count == 1


@respx.mock
async def test_c06_an_unknown_id_is_never_silently_new(caller):
    stranger = Client(caller.service, session_id="session-never-seen")
    response = await stranger.resume()
    assert response.status_code == 409
    assert code(response) == "SESSION_UNKNOWN"


@respx.mock
async def test_c06_a_closed_id_is_gone_not_fresh(caller):
    mock_jev()
    await caller.resolve()
    assert (await caller.close_session()).status_code == 200
    resumed = await caller.resume()
    assert resumed.status_code == 410
    assert code(resumed) == "SESSION_CLOSED"
    # And it cannot be resolved as new work either.
    again = await caller.resolve()
    assert again.status_code == 410
    assert code(again) == "SESSION_CLOSED"


@respx.mock
async def test_c06_a_pruned_id_leaves_a_tombstone(caller, service):
    mock_jev()
    await caller.resolve()
    counts = service.sessions.prune(-1)
    assert counts["sessions"] == 1
    row = service.sessions.get(caller.session_id)
    assert row is not None and row["tombstone"] and row["model_key"] is None
    resumed = await caller.resume()
    assert resumed.status_code == 410
    assert code(resumed) == "SESSION_CLOSED"


@respx.mock
async def test_c06_an_expired_prepared_contract_is_410(caller, service):
    mock_jev()
    await caller.resolve()
    service.sessions.clock = lambda: 10**10
    resumed = await caller.resume()
    assert resumed.status_code == 410
    assert code(resumed) == "SESSION_CLOSED"


# --- freshness and schema ------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "2"},
        {"intent": "sideways"},
        {"session_id": "short"},
        {"unexpected": True},
        {"limits": {"max_output_tokens": 0}},
        {"client_contract": {"unexpected": 1}},
    ],
)
async def test_an_invalid_resolve_schema_is_rejected(caller, payload):
    response = await caller.resolve(**payload)
    assert response.status_code == 400
    assert code(response) == "INVALID_ROUTER_INPUT"


@respx.mock
@pytest.mark.parametrize(
    "request_body",
    [
        {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]},
        {"messages": [{"role": "tool", "tool_call_id": "c", "content": "x"}]},
        {"messages": [{"role": "user", "content": "hi"}], "previous_response_id": "resp-1"},
    ],
)
async def test_carried_history_is_not_new_work(caller, request_body):
    jev = mock_jev()
    response = await caller.resolve(request=request_body)
    assert response.status_code == 400
    assert code(response) == "INVALID_ROUTER_INPUT"
    assert jev.call_count == 0


@respx.mock
async def test_a_legacy_alias_does_not_resolve_a_binding(caller):
    response = await caller.resolve(alias="auto")
    assert response.status_code == 400
    assert code(response) == "INVALID_ROUTER_INPUT"


@respx.mock
async def test_candidate_models_are_intersected_with_the_alias(caller):
    mock_jev()
    response = await caller.resolve(candidate_models=["big"])
    assert response.status_code == 200
    # `easy` wants the small model; the client did not offer it.
    assert response.json()["execution"]["model_key"] == "big"


@respx.mock
async def test_an_empty_intersection_has_no_safe_admission(caller):
    jev = mock_jev()
    response = await caller.resolve(candidate_models=["paramy"])
    assert response.status_code == 422
    assert code(response) == "NO_SAFE_ADMISSION"
    assert jev.call_count == 0


@respx.mock
async def test_an_unknown_candidate_model_is_a_schema_error(caller):
    response = await caller.resolve(candidate_models=["small", "imaginary"])
    assert response.status_code == 400
    assert code(response) == "INVALID_ROUTER_INPUT"


@respx.mock
async def test_a_protocol_the_client_does_not_speak_is_unsupported(service):
    config = session_config(
        aliases={"auto-session": {"allowed_models": ["responsey"]}},
    )
    svc = Service(service.path.parent / "responses", config)
    try:
        mock_jev()
        caller = Client(svc)
        response = await caller.resolve(client_contract={"protocols": ["openai-chat"]})
        assert response.status_code == 422
        assert code(response) == "UNSUPPORTED_PROFILE"
    finally:
        await svc.close()


# --- the conservative admission fallback (spec 8.4) ----------------------


@respx.mock
async def test_c11_a_classifier_outage_uses_the_configured_fallback(caller, service):
    respx.post(JEV_URL).mock(return_value=httpx.Response(503))
    response = await caller.resolve()
    assert response.status_code == 200
    body = response.json()
    assert body["execution"]["model_key"] == "big"
    assert body["decision"]["source"] == "admission_fallback"
    row = service.store.recent_decisions(1)[0]
    # The answers are absent, not a synthetic Jev measurement.
    assert row["answers"] == {}
    assert row["fallback"] == 1


@respx.mock
async def test_c11_the_fallback_binding_is_as_fixed_as_any_other(caller):
    jev = respx.post(JEV_URL).mock(return_value=httpx.Response(503))
    first = await caller.resolve()
    jev.mock(return_value=httpx.Response(200, json=jev_answers()))
    again = await caller.resume()
    assert again.json()["execution"] == first.json()["execution"]
    # Recovery does not revisit a committed binding.
    assert again.json()["execution"]["model_key"] == "big"


@respx.mock
async def test_without_a_configured_fallback_there_is_no_safe_admission(service):
    respx.post(JEV_URL).mock(return_value=httpx.Response(503))
    caller = Client(service, alias="auto-session-bare")
    response = await caller.resolve()
    assert response.status_code == 422
    assert code(response) == "NO_SAFE_ADMISSION"


# --- negotiated limits (C15) ---------------------------------------------


@respx.mock
async def test_c15_the_negotiated_limit_is_the_smaller_of_the_two(caller):
    mock_jev()
    response = await caller.resolve(limits={"max_output_tokens": 99999, "context_window": 12000})
    execution = response.json()["execution"]
    assert execution["max_output_tokens"] == 2048  # the deployment's, not the client's
    assert execution["context_window"] == 12000  # the client asked for less
    assert response.json()["state"] == "prepared"


@respx.mock
async def test_c15_a_client_acknowledging_other_limits_cannot_execute(caller):
    mock_jev()
    upstream()
    await caller.resolve()
    other = binding_digest(
        {
            "model_key": "small",
            "provider": "default",
            "upstream_id": "vendor/small",
            "protocol": "openai-chat",
            "context_window": 128000,  # a window this binding never negotiated
            "max_output_tokens": 2048,
            "supports_tools": True,
            "supports_vision": False,
            "compatibility_revision": "chat-small-v1",
        }
    )
    assert other != caller.binding
    response = await caller.execute(binding=other)
    assert response.status_code == 409
    assert code(response) == "SESSION_CONFLICT"


def test_the_binding_digest_ignores_quota_timestamps_and_effort():
    profile = {
        "model_key": "big",
        "provider": "cloud",
        "upstream_id": "vendor/big",
        "protocol": "openai-chat",
        "context_window": 100000,
        "max_output_tokens": 8192,
        "supports_tools": True,
        "supports_vision": True,
        "compatibility_revision": "v1",
    }
    base = binding_digest(profile)
    assert base == binding_digest({**profile, "effort": "high", "quota": 0.9, "ts": 12})
    assert base != binding_digest({**profile, "context_window": 99999})


# --- the context budget (C16, C17) ---------------------------------------


def test_c16_boundary_budget_checks():
    # Exactly the reserve floor, and exactly full.
    exact = context_budget(context_window=10_000, input_tokens=4_000, output_tokens=1_904)
    assert exact.reserve_tokens == MIN_RESERVE_TOKENS
    assert exact.fits and exact.headroom == 0
    over = context_budget(context_window=10_000, input_tokens=4_000, output_tokens=1_905)
    assert not over.fits and "exceeds the negotiated window" in over.reason
    # Above the floor the reserve is a tenth of the input.
    big = context_budget(context_window=200_000, input_tokens=100_000, output_tokens=8_192)
    assert big.reserve_tokens == 10_000
    # A client may ask for more reserve, never less.
    asked = context_budget(
        context_window=10_000, input_tokens=1_000, output_tokens=1_000, requested_reserve=9_000
    )
    assert asked.reserve_tokens == 9_000 and not asked.fits
    smaller = context_budget(
        context_window=10_000, input_tokens=1_000, output_tokens=1_000, requested_reserve=10
    )
    assert smaller.reserve_tokens == MIN_RESERVE_TOKENS


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), -1, "4000", True, 10.5, None]
)
def test_c16_invalid_numbers_never_admit(bad):
    check = context_budget(context_window=100_000, input_tokens=bad, output_tokens=1_000)
    assert not check.fits
    assert "whole number" in check.reason


def test_c16_an_unknown_size_near_the_boundary_is_refused():
    # Room to spare: an unknown part is tolerated.
    roomy = context_budget(
        context_window=100_000, input_tokens=10_000, output_tokens=1_000, unknown=("image",)
    )
    assert roomy.fits
    # The same numbers that just fit exactly are refused once something is a guess.
    edge = context_budget(context_window=10_000, input_tokens=4_000, output_tokens=1_904)
    assert edge.fits
    guessed = context_budget(
        context_window=10_000,
        input_tokens=4_000,
        output_tokens=1_904,
        unknown=("image",),
    )
    assert not guessed.fits and "unknown size" in guessed.reason


def test_c17_tool_schemas_long_code_cjk_images_and_deltas_all_count():
    plain = {"messages": [{"role": "user", "content": "hello"}]}
    base = estimate_input(plain).tokens

    with_tools = estimate_input(
        {
            **plain,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read one file from the working tree.",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                }
            ],
        }
    )
    assert with_tools.tokens > base + 20

    code_body = {"messages": [{"role": "user", "content": "```python\n" + "x = 1\n" * 500 + "```"}]}
    assert estimate_input(code_body).tokens > 700

    # CJK costs about one token per character, not a quarter of one.
    cjk = estimate_input({"messages": [{"role": "user", "content": "測試" * 500}]})
    latin = estimate_input({"messages": [{"role": "user", "content": "ab" * 500}]})
    assert cjk.tokens > 900
    assert cjk.tokens > 3 * latin.tokens
    assert text_tokens("漢字") == 2 and text_tokens("abcd") == 1

    image = estimate_input(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                    ],
                }
            ]
        }
    )
    assert image.tokens > 1000
    assert image.unknown == ("image",)

    delta = {"messages": [{"role": "user", "content": "carry on"}], "previous_response_id": "resp-1"}
    assert estimate_input(delta).unknown == ("previous_response",)
    reported = estimate_input(delta, accumulated_input_tokens=12_000)
    assert reported.tokens > 12_000 and reported.unknown == ()
    assert "reported-usage" in reported.method


@respx.mock
async def test_a_request_that_cannot_fit_is_refused_at_admission(caller):
    mock_jev()
    response = await caller.resolve(
        request={"messages": [{"role": "user", "content": "x" * 60_000}], "max_tokens": 256},
        limits={"context_window": 9_000},
    )
    assert response.status_code == 422
    assert code(response) == "CONTEXT_BUDGET_EXCEEDED"
    assert response.json()["error"]["budget"]["reserve_tokens"] >= MIN_RESERVE_TOKENS


@respx.mock
async def test_a_client_count_can_raise_the_estimate_but_not_lower_it(caller):
    mock_jev()
    # A client claiming almost nothing does not get past the server's own count.
    low = await caller.resolve(
        request={"messages": [{"role": "user", "content": "x" * 60_000}], "max_tokens": 256},
        limits={"context_window": 9_000, "estimated_input_tokens": 10},
    )
    assert code(low) == "CONTEXT_BUDGET_EXCEEDED"
    # A client claiming more than the server counted is believed.
    high = await caller.resolve(
        session_id="session-client-count",
        limits={"estimated_input_tokens": 500_000, "estimate_method": "client-estimate-v1"},
    )
    assert code(high) == "CONTEXT_BUDGET_EXCEEDED"


# --- the static envelope (C14) -------------------------------------------


def test_c14_an_envelope_advertises_the_pool_minimum():
    config = session_config(
        aliases={
            "auto-session": {
                "allowed_models": ["small", "big"],
                "static_envelope": {"enabled": True, "model_name": "auto-pool"},
            }
        }
    )
    envelope = config.envelope_limits("auto-session", config.aliases["auto-session"])
    assert envelope["context_window"] == 32000  # the smaller of 32000 and 100000
    assert envelope["max_output_tokens"] == 2048
    assert envelope["protocol"] == "openai-chat"
    assert envelope["supports_vision"] is False  # `small` cannot read images
    assert envelope["model_name"] == "auto-pool"


def test_c14_an_envelope_over_mixed_protocols_is_a_config_error():
    with pytest.raises(ValueError, match="one wire protocol"):
        session_config(
            aliases={
                "auto-session": {
                    "allowed_models": ["small", "responsey"],
                    "static_envelope": {"enabled": True, "model_name": "auto-pool"},
                }
            }
        )


def test_c14_an_envelope_may_not_advertise_more_than_the_pool_holds():
    with pytest.raises(ValueError, match="larger than the smallest window"):
        session_config(
            aliases={
                "auto-session": {
                    "allowed_models": ["small", "big"],
                    "static_envelope": {"context_window": 100000, "model_name": "auto-pool"},
                }
            }
        )
    with pytest.raises(ValueError, match="smallest output ceiling"):
        session_config(
            aliases={
                "auto-session": {
                    "allowed_models": ["small", "big"],
                    "static_envelope": {"max_output_tokens": 8192, "model_name": "auto-pool"},
                }
            }
        )


@respx.mock
async def test_c14_an_envelope_session_advertises_and_accepts_its_descriptor(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    config = session_config(
        aliases={
            "auto-session": {
                "allowed_models": ["small", "big"],
                "static_envelope": {"enabled": True, "model_name": "auto-pool"},
            }
        }
    )
    svc = Service(tmp_path, config)
    try:
        mock_jev()
        seen = upstream()
        caller = Client(svc)
        resolved = await caller.resolve()
        assert resolved.json()["execution"]["context_window"] == 32000
        # The descriptor the client is registered with is accepted as the model.
        response = await caller.execute(H.user_request("auto-pool"))
        assert response.status_code == 200
        assert seen[0]["model"] == "vendor/small(none)"
    finally:
        await svc.close()


# --- configuration -------------------------------------------------------


def test_a_strict_alias_needs_the_session_service_enabled():
    with pytest.raises(ValueError, match="needs session_routing.enabled"):
        make_config(
            aliases={"auto-session": {"session_mode": "strict", "allowed_models": ["big"]}}
        )


def test_verified_effort_control_needs_a_qualification_reference():
    with pytest.raises(ValueError, match="qualification_ref"):
        make_config(models={"big": {"effort_control": {"qualification": "verified"}}})
    config = make_config(
        models={
            "big": {
                "effort_control": {
                    "between_turn": "native_configuration_update",
                    "qualification": "verified",
                    "cache_behavior": "verified_preserving",
                    "qualification_ref": "docs/qualification/2026-09-19-astra.md",
                }
            }
        }
    )
    assert config.models["big"].effort_control.qualification == "verified"


def test_unknown_keys_are_refused_everywhere_new():
    with pytest.raises(ValueError):
        make_config(session_routing={"enabled": True, "surprise": 1})
    with pytest.raises(ValueError):
        make_config(models={"big": {"effort_control": {"surprise": 1}}})
    with pytest.raises(ValueError):
        make_config(aliases={"auto": {"static_envelope": {"surprise": 1}}})


def test_every_shipped_model_declares_its_protocol_honestly():
    config = load_config(Path(__file__).resolve().parent.parent / "router.yaml")
    assert config.session_routing.enabled is True
    assert config.aliases["auto-session"].session_mode == "strict"
    assert config.aliases["auto"].session_mode == "legacy"
    for name, model in config.models.items():
        assert model.protocol == "openai-chat", name
        assert model.effort_control.between_turn == "fixed", name


def test_the_shipped_config_still_loads(tmp_path):
    config = load_config(Path(__file__).resolve().parent.parent / "router.yaml")
    assert isinstance(config, RouterConfig)


def test_a_bad_admission_fallback_is_caught_at_load():
    with pytest.raises(ValueError, match="admission_fallback"):
        session_config(
            aliases={"auto-session": {"admission_fallback": {"model": "nonesuch"}}}
        )


# --- authentication (C32) ------------------------------------------------


@respx.mock
async def test_c32_strict_endpoints_need_the_control_credential_on_loopback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.delenv("JEV_ROUTER_ADMIN_TOKEN", raising=False)
    svc = Service(tmp_path)
    try:
        mock_jev()
        # With no credential configured the ingress lets a loopback peer read
        # the router endpoints. The session service still refuses.
        assert (await svc.client.get("/router/health")).status_code == 200
        caller = Client(svc, token="")
        response = await caller.resolve()
        assert response.status_code == 401
        assert code(response) == "ROUTER_UNAUTHORIZED"
        assert (await caller.show()).status_code == 401
        assert (await caller.execute()).status_code == 401
    finally:
        await svc.close()


@respx.mock
async def test_c32_a_wrong_credential_reaches_nothing(service):
    mock_jev()
    wrong = Client(service, token="not-the-token")
    assert (await wrong.resolve()).status_code == 401
    assert (await wrong.show()).status_code == 401
    assert (await wrong.execute()).status_code == 401


@respx.mock
async def test_c32_the_control_token_never_reaches_the_upstream(caller):
    mock_jev()
    seen = upstream()
    route = respx.routes[0]
    await caller.resolve()
    response = await caller.execute()
    assert response.status_code == 200
    sent = route.calls[-1].request
    for header in ("x-router-admin-token", "x-router-session", "x-router-binding", "x-router-request-id"):
        assert header not in sent.headers
    assert seen


@respx.mock
async def test_c32_a_session_is_scoped_to_the_owner_of_the_credential(caller, service):
    mock_jev()
    await caller.resolve()
    stored = service.sessions.get(caller.session_id)
    assert stored["owner"] == owner_key(ADMIN)
    # Another owner does not see it at all, so it reads as unknown rather than
    # as somebody else's binding.
    assert service.sessions.get(caller.session_id, owner_key("another-owner")) is None
    assert service.sessions.get(caller.session_id, owner_key(ADMIN)) is not None


# --- the store -----------------------------------------------------------


def test_the_startup_guard_refuses_a_second_strict_service(tmp_path):
    store = Store(tmp_path / "router.db")
    first = Sessions(store, SessionRoutingCfg(enabled=True))
    try:
        with pytest.raises(StrictServiceBusy):
            Sessions(Store(tmp_path / "router.db"), SessionRoutingCfg(enabled=True))
    finally:
        first.close()
    # Once the first one lets go the database can be opened again.
    second = Sessions(Store(tmp_path / "router.db"), SessionRoutingCfg(enabled=True))
    second.close()
    store.close()


def test_a_disabled_service_takes_no_guard(tmp_path):
    a = Sessions(Store(tmp_path / "router.db"), SessionRoutingCfg(enabled=False))
    b = Sessions(Store(tmp_path / "router.db"), SessionRoutingCfg(enabled=False))
    a.close()
    b.close()


def test_a_conditional_write_only_lands_once(tmp_path):
    store = Store(tmp_path / "router.db")
    sessions = Sessions(store, SessionRoutingCfg(enabled=True))
    try:
        row, created = sessions.create(
            {"session_id": "session-aaaaaaaa", "owner": "o", "model_key": "big"}
        )
        assert created and row["version"] == 1
        assert sessions.activate("session-aaaaaaaa", 1) is True
        # The same stale version cannot write over the winner.
        assert sessions.activate("session-aaaaaaaa", 1) is False
        assert sessions.get("session-aaaaaaaa")["state"] == "active"
    finally:
        sessions.close()
        store.close()


def test_the_request_history_is_bounded_but_keeps_unresolved_outcomes(tmp_path):
    store = Store(tmp_path / "router.db")
    sessions = Sessions(store, SessionRoutingCfg(enabled=True, max_request_history=5))
    try:
        sessions.create({"session_id": "session-bbbbbbbb", "owner": "o"})
        sessions.start_request("session-bbbbbbbb", "keep-me", "fp")
        sessions.finish_request("session-bbbbbbbb", "keep-me", "unknown")
        for i in range(20):
            sessions.start_request("session-bbbbbbbb", f"done-{i}", "fp")
            sessions.finish_request("session-bbbbbbbb", f"done-{i}", "completed")
        rows = store.conn.execute(
            "SELECT request_id FROM session_requests WHERE session_id = ?",
            ("session-bbbbbbbb",),
        ).fetchall()
        assert len(rows) <= 6
        # An outcome nobody can state is never forgotten.
        assert sessions.get_request("session-bbbbbbbb", "keep-me")["status"] == "unknown"
    finally:
        sessions.close()
        store.close()


def test_every_error_code_of_the_vocabulary_is_defined():
    assert len(ERROR_STATUS) == 13
    assert ERROR_STATUS["SESSION_CLOSED"] == 410
    assert ERROR_STATUS["TURN_NOT_SETTLED"] == 409
    assert ERROR_STATUS["EFFORT_HISTORY_MISMATCH"] == 409


# --- migration (C33) -----------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE pins (
    conversation_key TEXT PRIMARY KEY, model TEXT NOT NULL, effort TEXT,
    created REAL NOT NULL, decision_id TEXT
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, ts REAL NOT NULL,
    alias TEXT, client TEXT, conversation_key TEXT, state_builder TEXT, answers TEXT,
    features TEXT, config_hash TEXT, model TEXT, effort TEXT, rule TEXT, mode TEXT,
    fallback INTEGER, pinned INTEGER, jev_ms REAL, jev_tokens INTEGER, est_tokens INTEGER,
    message_count INTEGER, upstream_status INTEGER, upstream_usage TEXT, state TEXT
);
CREATE TABLE feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, ts REAL NOT NULL,
    verdict TEXT NOT NULL, better_model TEXT, better_effort TEXT, note TEXT, source TEXT
);
"""

SESSION_COLUMNS = {"event_type", "session_id", "turn_id", "request_id", "quality_lane"}


@respx.mock
async def test_c33_a_database_from_the_older_schema_keeps_working(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    path = tmp_path / "router.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (decision_id, ts, alias, model, effort, rule, mode)"
        " VALUES ('old1', 1000.0, 'auto', 'big', 'high', 'hard', 'active')"
    )
    conn.execute(
        "INSERT INTO pins (conversation_key, model, effort, created, decision_id)"
        " VALUES ('convkey', 'big', 'high', 1000.0, 'old1')"
    )
    conn.execute(
        "INSERT INTO feedback (decision_id, ts, verdict, source)"
        " VALUES ('old1', 1001.0, 'right', 'cli')"
    )
    conn.commit()
    conn.close()

    svc = Service(tmp_path)
    try:
        have = {row[1] for row in svc.store.conn.execute("PRAGMA table_info(decisions)")}
        assert SESSION_COLUMNS <= have
        # The rows written before any of this existed still read back.
        assert svc.store.get_decision("old1")["model"] == "big"
        assert svc.store.get_pin("convkey", 10**12)["model"] == "big"
        assert svc.store.recent_feedback(5)[0]["verdict"] == "right"
        # And the upgraded file takes a whole strict session.
        mock_jev()
        upstream()
        caller = Client(svc)
        assert (await caller.resolve()).status_code == 200
        assert (await caller.execute()).status_code == 200
        assert svc.sessions.get(caller.session_id)["state"] == "active"
    finally:
        await svc.close()


def test_c33_the_session_tables_are_added_without_touching_the_old_ones(tmp_path):
    path = tmp_path / "router.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()
    store = Store(path)
    sessions = Sessions(store, SessionRoutingCfg(enabled=True))
    try:
        assert sorted(sessions.migrated) == sorted(
            f"decisions.{c}" for c in SESSION_COLUMNS
        )
        tables = {
            row[0]
            for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"pins", "decisions", "feedback", "sessions", "session_requests"} <= tables
    finally:
        sessions.close()
        store.close()
