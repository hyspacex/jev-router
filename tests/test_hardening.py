"""Offline HTTP/session regressions for transport, execution and policy boundaries."""

import asyncio
import gzip
import hashlib
import json

import httpx
import pytest
import respx
import test_app as app_fixtures
import test_fallbacks as fallback_fixtures
from conftest import make_config
from test_app import (  # noqa: F401 - shared pytest fixtures
    CHAT,
    JEV_URL,
    chat_body,
    jev_answers,
    mock_jev,
    upstream_ok,
)
from test_fallbacks import upstream_by_model

from jev_router.app import create_app
from jev_router.features import extract_features
from jev_router.pins import Store, conversation_key
from jev_router.policy import RoutingError, evaluate

app_and_store = app_fixtures.app_and_store
client = app_fixtures.client
laned = fallback_fixtures.laned
laned_client = fallback_fixtures.laned_client


def streamed_response(data, *, status=200, headers=None):
    async def chunks():
        for i in range(0, len(data), 7):
            yield data[i : i + 7]

    return httpx.Response(status, content=chunks(), headers=headers)


@respx.mock
@pytest.mark.parametrize("alias", [True, False])
@pytest.mark.parametrize("sse", [True, False])
@pytest.mark.parametrize("status", [200, 429])
async def test_gzip_representation_and_client_decoding(client, alias, sse, status):
    payload = (
        b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
        if sse
        else b'{"usage":{"total_tokens":77}}'
    )
    compressed = gzip.compress(payload)
    mock_jev()
    route = respx.post(CHAT).mock(
        side_effect=lambda _: streamed_response(
            compressed,
            status=status,
            headers={
                "content-encoding": "gzip",
                "content-type": "text/event-stream" if sse else "application/json",
            },
        )
    )
    body = chat_body(model="auto" if alias else "vendor/small", stream=sse)
    response = await client.post(
        "/v1/chat/completions", json=body, headers={"accept-encoding": "gzip"}
    )
    assert response.status_code == status
    assert route.calls[-1].request.headers["accept-encoding"] == "gzip"
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == payload
    if not sse:
        assert response.json()["usage"]["total_tokens"] == 77
    async with client.stream("POST", "/v1/chat/completions", json=body) as response:
        assert b"".join([chunk async for chunk in response.aiter_raw()]) == compressed


@respx.mock
async def test_absent_accept_encoding_is_identity(client):
    upstream = upstream_ok()
    request = client.build_request(
        "POST", "/v1/chat/completions", json=chat_body(model="vendor/small")
    )
    del request.headers["accept-encoding"]
    await client.send(request)
    assert upstream.calls[0].request.headers["accept-encoding"] == "identity"


@respx.mock
async def test_repeated_and_connection_specific_headers(client):
    async def reply(_request):
        return streamed_response(
            b"{}",
            headers=[
                ("content-type", "application/json"),
                ("set-cookie", "a=1"),
                ("set-cookie", "b=2"),
                ("connection", "x-hop"),
                ("x-hop", "remove"),
                ("transfer-encoding", "chunked"),
            ],
        )

    route = respx.post(CHAT).mock(side_effect=reply)
    response = await client.post(
        "/v1/chat/completions",
        json=chat_body(model="vendor/small"),
        headers={"connection": "x-hop", "x-hop": "remove"},
    )
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert "x-hop" not in response.headers
    assert "transfer-encoding" not in response.headers
    assert "x-hop" not in route.calls[0].request.headers


@respx.mock
@pytest.mark.parametrize("mode", ["active", "shadow"])
async def test_gzip_usage_and_completion_are_recorded(app_and_store, client, mode):
    _app, store, cfg = app_and_store
    cfg.settings.mode = mode
    mock_jev()
    raw = b'{"usage":{"total_tokens":77}}'
    compressed = gzip.compress(raw)
    respx.post(CHAT).mock(
        side_effect=lambda _: streamed_response(
            compressed, headers={"content-encoding": "gzip"}
        )
    )
    await client.post("/v1/chat/completions", json=chat_body())
    row = store.recent_decisions(1)[0]
    assert row["upstream_usage"]["total_tokens"] == 77
    assert row["accepted"] == 1
    assert row["stream_state"] == "completed"
    assert row["response_bytes"] == len(compressed)
    assert 0 <= row["first_byte_ms"] <= row["response_ms"]
    if mode == "shadow":
        assert store.get_pin(row["conversation_key"], 3600) is None


@respx.mock
@pytest.mark.parametrize("compressed", [False, True])
async def test_usage_capture_limit_bounds_decoded_and_wire_bodies(
    app_and_store, client, compressed
):
    _app, store, cfg = app_and_store
    cfg.settings.capture_usage_max_bytes = 128
    mock_jev()
    raw = json.dumps({"padding": "x" * 10000, "usage": {"total_tokens": 99}}).encode()
    wire = gzip.compress(raw) if compressed else raw
    respx.post(CHAT).mock(
        side_effect=lambda _: streamed_response(
            wire,
            headers={"content-encoding": "gzip"} if compressed else {},
        )
    )
    response = await client.post("/v1/chat/completions", json=chat_body())
    assert response.content == raw
    assert store.recent_decisions(1)[0]["upstream_usage"] is None


def bad_payloads():
    yield []
    yield {}
    yield {"answers": []}
    for question, field, values in [
        ("task", "choice", ["unknown", [], None]),
        ("task", "confidence", [None, "0.9", True, -1, 2, float("nan")]),
        ("difficulty", "score", [None, "1", True, -1, 3, float("inf"), float("nan")]),
        ("harm_if_wrong", "noul", [None, {}, -0.1, 1.1]),
        ("difficulty", "type", ["choice", None]),
        (
            "task",
            "probabilities",
            [[], {"unknown": 1}, {"code": float("nan")}, {"code": 0.1}],
        ),
    ]:
        for value in values:
            payload = jev_answers()
            payload["answers"][question][field] = value
            yield payload
    payload = jev_answers()
    del payload["answers"]["difficulty"]
    yield payload
    for value in [
        [],
        "bad",
        {"input_tokens": True},
        {"input_tokens": -1},
        {"input_tokens": "4"},
    ]:
        payload = jev_answers()
        payload["usage"] = value
        yield payload


@respx.mock
@pytest.mark.parametrize("payload", list(bad_payloads()))
async def test_malformed_jev_200_is_temporary_fallback(client, app_and_store, payload):
    _app, store, _cfg = app_and_store
    jev = respx.post(JEV_URL).mock(
        return_value=httpx.Response(200, content=json.dumps(payload).encode())
    )
    upstream_ok()
    first = await client.post("/v1/chat/completions", json=chat_body())
    assert first.status_code == 200
    assert first.headers["X-Router-Decider-Fallback"] == "true"
    row = store.recent_decisions(1)[0]
    assert row["answers"] == {}
    assert store.get_pin(row["conversation_key"], 3600) is None
    jev.mock(return_value=httpx.Response(200, json=jev_answers()))
    second = await client.post("/v1/chat/completions", json=chat_body())
    assert "X-Router-Decider-Fallback" not in second.headers
    assert second.headers["X-Router-Pinned"] == "false"
    assert jev.call_count == 2


@respx.mock
async def test_unsolicited_answers_do_not_influence_policy(client):
    payload = jev_answers(difficulty=0.1, harm=1)
    respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=payload))
    upstream_ok()
    response = await client.post(
        "/v1/chat/completions", json=chat_body(model="auto-fast")
    )
    assert response.headers["X-Router-Model"] == "vendor/small(none)"


@respx.mock
async def test_state_builder_and_policy_failures_fall_back(monkeypatch, client):
    import jev_router.deciders.jev as adapter

    upstream_ok()

    def broken(*_args, **_kwargs):
        raise RuntimeError("do not log payloads")

    monkeypatch.setattr(adapter, "build_state", broken)
    response = await client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert response.headers["X-Router-Decider-Fallback"] == "true"


@respx.mock
@pytest.mark.parametrize("failure", [400, 429, 503, "network"])
async def test_failed_upstream_never_establishes_pin(laned, laned_client, failure):
    _app, store, _cfg = laned
    jev = mock_jev()
    calls = upstream_by_model(
        {
            "vendor/": httpx.ConnectError("down")
            if failure == "network"
            else httpx.Response(failure)
        }
    )
    response = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == (502 if failure == "network" else failure)
    row = store.recent_decisions(1)[0]
    assert row["accepted"] == 0
    assert store.get_pin(row["conversation_key"], 3600) is None
    await laned_client.post("/v1/chat/completions", json=chat_body())
    assert jev.call_count == 2
    assert calls


@respx.mock
async def test_temporary_decision_cannot_pin_after_upstream_fallback(
    laned, laned_client
):
    _app, store, _cfg = laned
    jev = respx.post(JEV_URL).mock(return_value=httpx.Response(529))
    upstream_by_model({"vendor/big": httpx.Response(429)})
    for _ in range(2):
        response = await laned_client.post("/v1/chat/completions", json=chat_body())
        assert response.status_code == 200
        assert response.headers["X-Router-Fallback"] == "1"
        assert response.headers["X-Router-Decider-Fallback"] == "true"
    assert jev.call_count == 2
    row = store.recent_decisions(1)[0]
    assert store.get_pin(row["conversation_key"], 3600) is None


@respx.mock
async def test_fallback_outcome_drives_headers_log_and_pin(laned, laned_client):
    _app, store, _cfg = laned
    mock_jev()
    upstream_by_model({"vendor/big": httpx.Response(429)})
    response = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert response.headers["X-Router-Model"] == "vendor/small(low)"
    assert response.headers["X-Router-Effort"] == "low"
    row = store.recent_decisions(1)[0]
    assert row["model"] == "small" and row["intended_model"] == "big"
    pin = store.get_pin(row["conversation_key"], 3600)
    assert pin is not None and pin["model"] == "small"


@respx.mock
async def test_pin_is_not_written_until_accepted(app_and_store, client):
    _app, store, _cfg = app_and_store
    mock_jev()

    def response(_request):
        row = store.recent_decisions(1)[0]
        assert store.get_pin(row["conversation_key"], 3600) is None
        return httpx.Response(200, json={})

    respx.post(CHAT).mock(side_effect=response)
    await client.post("/v1/chat/completions", json=chat_body())
    row = store.recent_decisions(1)[0]
    assert store.get_pin(row["conversation_key"], 3600) is not None


@respx.mock
async def test_failed_stream_is_not_completed_or_retried(app_and_store, client):
    _app, store, _cfg = app_and_store
    mock_jev()

    async def body():
        yield b'data: {"choices":[]}\n\n'
        raise httpx.ReadError("stream ended early")

    route = respx.post(CHAT).mock(
        return_value=httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )
    )
    with pytest.raises(httpx.ReadError):
        await client.post("/v1/chat/completions", json=chat_body(stream=True))
    row = store.recent_decisions(1)[0]
    assert row["accepted"] == 1 and row["stream_state"] == "failed"
    assert route.call_count == 1
    assert store.get_pin(row["conversation_key"], 3600) is not None


@respx.mock
async def test_pin_scope_changes_alias_client_and_auth(app_and_store, client):
    jev = mock_jev(difficulty=2)
    upstream_ok()
    first = await client.post("/v1/chat/completions", json=chat_body())
    fast = await client.post("/v1/chat/completions", json=chat_body(model="auto-fast"))
    assert fast.headers["X-Router-Effort"] == "low"
    assert fast.headers["X-Router-Decision"] != first.headers["X-Router-Decision"]
    await client.post(
        "/v1/chat/completions", json=chat_body(), headers={"x-router-client": "bigonly"}
    )
    await client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"authorization": "Bearer different"},
    )
    assert jev.call_count == 4


@respx.mock
async def test_pin_capability_change_can_use_smaller_window(app_and_store, client):
    _app, store, cfg = app_and_store
    cfg.models["small"].context_window = 1_000_000
    jev = mock_jev()
    upstream_ok()
    first = await client.post("/v1/chat/completions", json=chat_body())
    body = chat_body()
    body["messages"].append(
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
            ],
        }
    )
    second = await client.post("/v1/chat/completions", json=body)
    assert second.headers["X-Router-Model"].startswith("vendor/big")
    assert second.headers["X-Router-Rule"] == "repin_constraints"
    assert second.headers["X-Router-Decision"] != first.headers["X-Router-Decision"]
    assert jev.call_count == 1
    assert store.recent_decisions(1)[0]["accepted"] == 1


@respx.mock
async def test_failed_repin_leaves_original_pin_for_revalidation(app_and_store, client):
    _app, store, _cfg = app_and_store
    mock_jev()
    upstream_ok()
    await client.post("/v1/chat/completions", json=chat_body())
    key = store.recent_decisions(1)[0]["conversation_key"]
    old = store.get_pin(key, 3600)
    grown = chat_body()
    grown["messages"].append({"role": "user", "content": "x" * 20000})
    upstream_by_model({"vendor/big": httpx.Response(503)})
    response = await client.post("/v1/chat/completions", json=grown)
    assert response.status_code == 503
    assert store.get_pin(key, 3600) == old
    upstream_ok()
    response = await client.post("/v1/chat/completions", json=grown)
    assert response.status_code == 200
    pin = store.get_pin(key, 3600)
    assert pin is not None and pin["model"] == "big"


@respx.mock
async def test_pin_revalidates_current_allowed_set_and_effort(app_and_store, client):
    _app, _store, cfg = app_and_store
    jev = mock_jev()
    upstream_ok()
    await client.post("/v1/chat/completions", json=chat_body())
    cfg.aliases["auto"].allowed_models = ["big"]
    cfg.aliases["auto"].max_effort = "low"
    response = await client.post("/v1/chat/completions", json=chat_body())
    assert response.headers["X-Router-Model"] == "vendor/big(low)"
    assert jev.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    "constraint", ["intersection", "vision", "context", "effort", "empty"]
)
async def test_unsatisfiable_constraints_never_reach_jev_or_upstream(
    app_and_store, client, constraint
):
    _app, _store, cfg = app_and_store
    cfg.aliases["auto"].allowed_models = ["small"]
    headers = {}
    body = chat_body()
    if constraint == "intersection":
        headers = {"x-router-client": "bigonly"}
    elif constraint == "vision":
        body["messages"][0]["content"] = [
            {"type": "image_url", "image_url": {"url": "x"}}
        ]
    elif constraint == "context":
        body["max_tokens"] = 2000
    elif constraint == "effort":
        cfg.aliases["auto"].max_effort = "low"
        headers = {"x-router-client": "floored"}
    else:
        cfg.aliases["auto"].allowed_models = []
    response = await client.post("/v1/chat/completions", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "routing_error"


def test_effort_bounds_apply_after_default_and_ladder_clamping():
    cfg = make_config(models={"small": {"default_effort": "high"}})
    features = extract_features(chat_body(), {})
    result = evaluate(cfg, cfg.aliases["auto-fast"], {}, features)
    assert result.effort == "low"
    # A hard floor may require rounding up, never down beneath the floor.
    cfg.clients["floored"].min_effort = "medium"
    features = extract_features(chat_body(), {"x-router-client": "floored"})
    result = evaluate(
        cfg,
        cfg.aliases["auto"],
        {"difficulty": {"type": "score", "score": 0}},
        features,
    )
    assert result.model == "small" and result.effort == "high"
    cfg.aliases["auto"].max_effort = "low"
    with pytest.raises(RoutingError):
        evaluate(cfg, cfg.aliases["auto"], {}, features)


def test_scoped_identity_cannot_reuse_legacy_or_ambiguous_keys():
    args = ("Bearer key", "system", "first")
    auth_hash = hashlib.sha256(args[0].encode()).hexdigest()
    old = hashlib.sha256((auth_hash + "\x00system\x00first\x00").encode()).hexdigest()
    assert conversation_key(*args, alias="auto") != old
    assert conversation_key("", "a\x00b", "c") != conversation_key("", "a", "b\x00c")
    assert conversation_key(*args, alias="auto") != conversation_key(
        *args, alias="auto-fast"
    )


@respx.mock
async def test_remote_router_endpoints_need_separate_admin_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", "test-admin")
    cfg = make_config()
    app = create_app(cfg, Store(tmp_path / "db"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=("100.64.0.2", 123)),
        base_url="http://router.test",
    ) as remote:
        assert (await remote.get("/router/health")).status_code == 401
        assert (await remote.post("/router/feedback", json={})).status_code == 401
        assert (
            await remote.get(
                "/router/health", headers={"authorization": "Bearer test-admin"}
            )
        ).status_code == 401
        assert (
            await remote.get(
                "/router/health", headers={"x-router-admin-token": "test-admin"}
            )
        ).status_code == 200
        upstream = upstream_ok()
        response = await remote.post(
            "/v1/chat/completions",
            json=chat_body(model="vendor/small"),
            headers={
                "authorization": "Bearer upstream-key",
                "x-router-admin-token": "test-admin",
            },
        )
        assert response.status_code == 200
        sent = upstream.calls[0].request
        assert sent.headers["authorization"] == "Bearer upstream-key"
        assert "x-router-admin-token" not in sent.headers
    await app.state.router.aclose()


@respx.mock
async def test_without_admin_token_only_loopback_can_read_router_endpoints(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("JEV_ROUTER_ADMIN_TOKEN", raising=False)
    app = create_app(make_config(), Store(tmp_path / "db"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=("100.64.0.2", 1)),
        base_url="http://router.test",
    ) as remote:
        assert (await remote.get("/router/health")).status_code == 401
    await app.state.router.aclose()


@respx.mock
async def test_body_limits_cover_known_and_chunked_uploads(app_and_store, client):
    _app, _store, cfg = app_and_store
    cfg.settings.max_body_bytes = 100
    response = await client.post("/v1/chat/completions", content=b"x" * 101)
    assert response.status_code == 413

    async def chunks():
        yield b"x" * 50
        yield b"x" * 51

    response = await client.post("/v1/chat/completions", content=chunks())
    assert response.status_code == 413


@respx.mock
async def test_concurrency_limit_releases_after_response(app_and_store, client):
    _app, _store, cfg = app_and_store
    cfg.settings.max_concurrent_requests = 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(_request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={})

    respx.post(CHAT).mock(side_effect=slow)
    first = asyncio.create_task(
        client.post("/v1/chat/completions", json=chat_body(model="vendor/small"))
    )
    try:
        await entered.wait()
        response = await client.post(
            "/v1/chat/completions", json=chat_body(model="vendor/small")
        )
        assert response.status_code == 503
    finally:
        release.set()
        assert (await first).status_code == 200
    assert (await client.get("/router/health")).status_code == 200
