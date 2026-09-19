"""Upstream fallbacks and the breaker, through the real HTTP path."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from conftest import UPSTREAM, make_config
from jev_router.app import create_app
from jev_router.pins import Store
from test_app import (  # noqa: F401 - imported so pytest sees the fixtures
    app_and_store,
    chat_body,
    client,
    mock_jev,
    upstream_ok,
)

CHAT = f"{UPSTREAM}/v1/chat/completions"

# Two providers, and one lane that falls from the frontier model to the fast
# one. `easy` is the only rule, so every request takes the lane.
LANED = {
    "providers": {"cloud": {}, "local": {}},
    "models": {
        "small": {"provider": "local"},
        "big": {"provider": "cloud"},
        "paramy": {"provider": "cloud"},
    },
    "routes": {
        "lane": {
            "primary": {"model": "big", "effort": "medium"},
            "fallbacks": [{"model": "small", "effort": "low"}],
        }
    },
    "policy": {
        "low_confidence": None,
        "rules": [{"name": "always", "when": {}, "use": {"route": "lane"}}],
        "default": {"route": "lane", "model": None, "effort": None},
    },
}


@pytest.fixture
def laned(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = make_config(**LANED)
    store = Store(tmp_path / "router.db")
    return create_app(cfg, store), store, cfg


@pytest.fixture
async def laned_client(laned):
    app, _store, _cfg = laned
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://router.test") as c:
        yield c


def upstream_by_model(mapping, default=None):
    """Answer differently depending on which model the body names."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        for prefix, response in mapping.items():
            if model.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                return response
        return default or httpx.Response(200, json={"id": "ok"})

    respx.post(CHAT).mock(side_effect=handler)
    return calls


# --- when a fallback is taken -------------------------------------------


@respx.mock
async def test_a_429_moves_to_the_next_entry(laned_client):
    mock_jev()
    calls = upstream_by_model({"vendor/big": httpx.Response(429, json={"error": "slow down"})})
    resp = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    assert calls == ["vendor/big(medium)", "vendor/small(low)"]
    assert resp.headers["X-Router-Fallback"] == "1"
    assert resp.headers["X-Router-Route"] == "lane"


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
async def test_every_retryable_status_moves_on(laned_client, status):
    mock_jev()
    calls = upstream_by_model({"vendor/big": httpx.Response(status, json={"error": "x"})})
    resp = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    assert len(calls) == 2


@respx.mock
async def test_a_connection_error_moves_on(laned_client):
    mock_jev()
    calls = upstream_by_model({"vendor/big": httpx.ConnectError("refused")})
    resp = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    assert len(calls) == 2


@respx.mock
async def test_a_timeout_before_the_first_byte_moves_on(laned_client):
    mock_jev()
    calls = upstream_by_model({"vendor/big": httpx.ReadTimeout("nothing came back")})
    resp = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    assert len(calls) == 2


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_a_client_error_is_the_answer_and_is_not_retried(laned_client, status):
    """A wrong request is wrong at every provider. Retrying it wastes quota."""
    mock_jev()
    calls = upstream_by_model({"vendor/big": httpx.Response(status, json={"error": "nope"})})
    resp = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == status
    assert calls == ["vendor/big(medium)"]
    assert "X-Router-Fallback" not in resp.headers


@respx.mock
async def test_the_last_entry_keeps_its_answer_however_bad(laned_client):
    mock_jev()
    calls = upstream_by_model(
        {"vendor/": httpx.Response(503, json={"error": "everything is down"})}
    )
    resp = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 503
    assert calls == ["vendor/big(medium)", "vendor/small(low)"]
    assert resp.headers["X-Router-Fallback"] == "1"


@respx.mock
async def test_a_route_with_no_fallback_behaves_as_it_always_did(client):
    mock_jev(difficulty=2.0)
    respx.post(CHAT).mock(return_value=httpx.Response(429, json={"error": "slow down"}))
    resp = await client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 429
    assert "X-Router-Fallback" not in resp.headers


# --- what a fallback does to the pin and the log ------------------------


@respx.mock
async def test_the_conversation_is_pinned_to_the_model_that_answered(laned, laned_client):
    _app, store, _cfg = laned
    mock_jev()
    calls = upstream_by_model({"vendor/big": httpx.Response(429, json={"error": "x"})})
    first = await laned_client.post("/v1/chat/completions", json=chat_body())
    assert first.headers["X-Router-Fallback"] == "1"

    row = store.recent_decisions(1)[0]
    # The row says what actually served and what was meant to.
    assert row["model"] == "small"
    assert row["effort"] == "low"
    assert row["intended_model"] == "big"
    assert row["intended_effort"] == "medium"
    assert row["fallback_index"] == 1
    assert "429" in row["fallback_reason"]
    assert row["route"] == "lane"

    pin = store.get_pin(row["conversation_key"], 3600)
    assert (pin["model"], pin["effort"]) == ("small", "low")

    # The next turn stays on the model that answered, and does not try the
    # primary again.
    calls.clear()
    second_body = chat_body()
    second_body["messages"].append({"role": "assistant", "content": "hi"})
    second_body["messages"].append({"role": "user", "content": "more"})
    second = await laned_client.post("/v1/chat/completions", json=second_body)
    assert second.headers["X-Router-Pinned"] == "true"
    assert calls == ["vendor/small(low)"]


@respx.mock
async def test_a_pinned_conversation_is_never_moved_by_a_429(laned, laned_client):
    _app, _store, _cfg = laned
    mock_jev()
    upstream_ok()
    await laned_client.post("/v1/chat/completions", json=chat_body())

    calls = upstream_by_model({"vendor/big": httpx.Response(429, json={"error": "x"})})
    second_body = chat_body()
    second_body["messages"].append({"role": "assistant", "content": "hi"})
    second_body["messages"].append({"role": "user", "content": "more"})
    second = await laned_client.post("/v1/chat/completions", json=second_body)
    assert second.headers["X-Router-Pinned"] == "true"
    assert second.status_code == 429
    assert calls == ["vendor/big(medium)"]   # it did not shop around


# --- streaming ----------------------------------------------------------


@respx.mock
async def test_a_stream_that_dies_after_it_started_is_not_retried(laned_client):
    """The choice of entry is made on the response status, which arrives
    before any byte is streamed. Once a response has been accepted the model
    cannot change: the client may already hold part of an answer, and two
    answers cannot be spliced together."""
    async def body():
        yield b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
        raise httpx.ReadError("the connection dropped")

    mock_jev()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )

    respx.post(CHAT).mock(side_effect=handler)

    with pytest.raises(httpx.ReadError):
        async with laned_client.stream(
            "POST", "/v1/chat/completions", json=chat_body(stream=True)
        ) as resp:
            assert resp.status_code == 200
            async for _piece in resp.aiter_raw():
                pass
    assert calls == ["vendor/big(medium)"]   # the fast model was never asked


@respx.mock
async def test_a_fallback_streams_its_bytes_through_unchanged(laned_client):
    chunks = [
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def body():
        for chunk in chunks:
            yield chunk

    mock_jev()

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"].startswith("vendor/big"):
            return httpx.Response(429, json={"error": "x"})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )

    respx.post(CHAT).mock(side_effect=handler)

    received = bytearray()
    async with laned_client.stream(
        "POST", "/v1/chat/completions", json=chat_body(stream=True)
    ) as resp:
        assert resp.headers["X-Router-Fallback"] == "1"
        async for piece in resp.aiter_raw():
            received.extend(piece)
    assert bytes(received) == b"".join(chunks)


# --- the breaker, end to end --------------------------------------------


@respx.mock
async def test_a_provider_that_keeps_failing_is_skipped(laned, laned_client):
    app, _store, _cfg = laned
    router = app.state.router
    mock_jev()
    calls = upstream_by_model({"vendor/big": httpx.Response(503, json={"error": "x"})})

    # Three separate conversations, so each one takes a fresh decision.
    for text in ("one", "two", "three"):
        await laned_client.post("/v1/chat/completions", json=chat_body(text))
    assert router.breakers.report()["cloud"]["state"] == "open"

    calls.clear()
    resp = await laned_client.post("/v1/chat/completions", json=chat_body("four"))
    assert resp.status_code == 200
    assert calls == ["vendor/small(low)"]   # the frontier model was not tried
    assert resp.headers["X-Router-Fallback"] == "1"


@respx.mock
async def test_an_open_provider_is_still_tried_when_it_is_the_only_entry(client, app_and_store):
    app, _store, _cfg = app_and_store
    router = app.state.router
    for _ in range(5):
        router.breakers.record_failure("default", "HTTP 503")
    assert router.breakers.available("default") is False
    mock_jev()
    route = upstream_ok()
    resp = await client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    assert route.called   # refusing to serve at all would be worse


@respx.mock
async def test_the_providers_endpoint_reports_the_breaker(laned, laned_client):
    app, _store, _cfg = laned
    app.state.router.breakers.record_failure("cloud", "HTTP 429")
    body = (await laned_client.get("/router/providers")).json()
    assert body["providers"]["cloud"]["consecutive_failures"] == 1
    assert body["providers"]["cloud"]["state"] == "closed"
    assert body["providers"]["local"]["models"] == ["small"]
    assert body["pressures"]["cloud"] == 0.0


@respx.mock
async def test_the_quota_endpoint_reports_every_provider(laned_client):
    body = (await laned_client.get("/router/quota")).json()
    assert body["enabled"] is True
    assert set(body["providers"]) == {"cloud", "local"}
    assert body["providers"]["cloud"]["enabled"] is False
    assert body["providers"]["cloud"]["snapshot"] is None
    assert body["effective_pressure"] == {"cloud": 0.0, "local": 0.0}


# --- quota, through the HTTP path ---------------------------------------


@respx.mock
async def test_quota_pressure_reroutes_a_new_conversation_and_shows_a_header(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    cfg = make_config(
        providers={
            "cloud": {"quota": {"source": "static", "enabled": True,
                                "config": {"used_percent": 95, "expected_used_percent": 10}}},
            "local": {},
        },
        models={"small": {"provider": "local"}, "big": {"provider": "cloud"},
                "paramy": {"provider": "cloud"}},
        routes={"fast": {"primary": {"model": "small", "effort": "none"}},
                "frontier": {"primary": {"model": "big", "effort": "high"}}},
        policy={
            "low_confidence": None,
            "rules": [
                {"name": "hard",
                 "when": {"difficulty": {"gte": 1.5, "shift_with": "cloud", "max_shift": 1.0}},
                 "use": {"route": "frontier"}},
                {"name": "easy", "when": {}, "use": {"route": "fast"}},
            ],
            "default": {"route": "frontier", "model": None, "effort": None},
        },
    )
    app = create_app(cfg, Store(tmp_path / "db.sqlite"))
    router = app.state.router
    mock_jev(difficulty=2.0)
    upstream_ok()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as c:
        calm = await c.post("/v1/chat/completions", json=chat_body("first"))
        assert calm.headers["X-Router-Model"] == "vendor/big(high)"
        assert "X-Router-Pressure" not in calm.headers

        await router.quota.poll_once("cloud")
        assert router.pressures()["cloud"] > 0.6

        pressed = await c.post("/v1/chat/completions", json=chat_body("second"))
        assert pressed.headers["X-Router-Model"] == "vendor/small(none)"
        assert pressed.headers["X-Router-Pressure"].startswith("cloud=")

        # The first conversation is pinned and quota does not touch it.
        again = chat_body("first")
        again["messages"].append({"role": "assistant", "content": "x"})
        again["messages"].append({"role": "user", "content": "carry on"})
        third = await c.post("/v1/chat/completions", json=again)
        assert third.headers["X-Router-Pinned"] == "true"
        assert third.headers["X-Router-Model"] == "vendor/big(high)"

    row = [r for r in router.store.recent_decisions(5) if r["rule"] == "easy"][0]
    assert row["pressures"]["cloud"] > 0.6
    assert row["shifted"][0]["base"] == 1.5


@respx.mock
async def test_health_names_the_providers_and_the_unhealthy_ones(laned, laned_client):
    app, _store, _cfg = laned
    body = (await laned_client.get("/router/health")).json()
    assert body["providers"] == ["cloud", "local"]
    assert body["routes"] == ["lane"]
    assert body["unhealthy_providers"] == []

    for _ in range(3):
        app.state.router.breakers.record_failure("cloud", "HTTP 503")
    body = (await laned_client.get("/router/health")).json()
    assert body["unhealthy_providers"] == ["cloud"]
