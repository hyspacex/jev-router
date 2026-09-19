import json

import httpx
import pytest
import respx
from conftest import UPSTREAM, make_config

from jev_router.app import create_app
from jev_router.pins import Store

JEV_URL = "https://jev.test/v1/systemone"
CHAT = f"{UPSTREAM}/v1/chat/completions"


def jev_answers(task="quick_question", difficulty=0.1, harm=0.1, conf=0.9):
    return {
        "answers": {
            "task": {"type": "choice", "choice": task, "confidence": conf, "probabilities": {}},
            "difficulty": {"type": "score", "score": difficulty, "confidence": conf},
            "harm_if_wrong": {"type": "noul", "noul": harm},
        },
        "usage": {"input_tokens": 400},
    }


def mock_jev(**kwargs):
    return respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=jev_answers(**kwargs)))


def upstream_ok(body=None, headers=None):
    return respx.post(CHAT).mock(
        return_value=httpx.Response(
            200,
            json=body or {"id": "cmpl-1", "choices": [], "usage": {"total_tokens": 42}},
            headers=headers or {},
        )
    )


@pytest.fixture
def app_and_store(tmp_path, monkeypatch, request):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    overrides = getattr(request, "param", {})
    cfg = make_config(**overrides) if overrides else make_config()
    store = Store(tmp_path / "router.db")
    app = create_app(cfg, store)
    return app, store, cfg


@pytest.fixture
async def client(app_and_store):
    app, _store, _cfg = app_and_store
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://router.test") as c:
        yield c


def chat_body(text="hello", **extra):
    body = {"model": "auto", "messages": [{"role": "user", "content": text}], "max_tokens": 16}
    body.update(extra)
    return body


# --- pass-through -------------------------------------------------------


@respx.mock
async def test_non_alias_request_is_forwarded_byte_for_byte(client):
    route = upstream_ok()
    raw = json.dumps({"model": "vendor/small", "messages": [{"role": "user", "content": "hi"}]})
    resp = await client.post(
        "/v1/chat/completions",
        content=raw,
        headers={"content-type": "application/json", "authorization": "Bearer client-key"},
    )
    assert resp.status_code == 200
    sent = route.calls[0].request
    assert sent.content.decode() == raw
    assert sent.headers["authorization"] == "Bearer client-key"
    assert "X-Router-Model" not in resp.headers


@respx.mock
async def test_other_paths_pass_through_with_method_and_query(client):
    route = respx.route(method="POST", url=f"{UPSTREAM}/v1/embeddings").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )
    resp = await client.post("/v1/embeddings", json={"model": "vendor/small", "input": "x"})
    assert resp.status_code == 201
    assert route.called

    get_route = respx.get(f"{UPSTREAM}/anything?a=1").mock(
        return_value=httpx.Response(200, text="pong")
    )
    resp = await client.get("/anything?a=1")
    assert resp.text == "pong"
    assert get_route.called


@respx.mock
async def test_alias_on_another_endpoint_is_a_clear_400(client):
    resp = await client.post("/v1/responses", json={"model": "auto", "input": "hi"})
    assert resp.status_code == 400
    assert "auto" in resp.json()["error"]["message"]
    assert "chat/completions" in resp.json()["error"]["message"]


@respx.mock
async def test_upstream_failure_becomes_502(client):
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("no route"))
    resp = await client.post("/v1/chat/completions", json={"model": "vendor/small", "messages": []})
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "router"


# --- alias routing ------------------------------------------------------


@respx.mock
async def test_alias_is_rewritten_and_headers_are_added(client, app_and_store):
    mock_jev()
    route = upstream_ok()
    resp = await client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "vendor/small(none)"
    assert sent["messages"] == chat_body()["messages"]
    assert resp.headers["X-Router-Model"] == "vendor/small(none)"
    assert resp.headers["X-Router-Effort"] == "none"
    assert resp.headers["X-Router-Rule"] == "easy"
    assert resp.headers["X-Router-Pinned"] == "false"
    assert len(resp.headers["X-Router-Decision"]) == 12


@respx.mock
async def test_hard_request_routes_to_the_big_model(client):
    mock_jev(task="code", difficulty=2.0, harm=0.2)
    route = upstream_ok()
    resp = await client.post("/v1/chat/completions", json=chat_body("rewrite the parser"))
    assert json.loads(route.calls[0].request.content)["model"] == "vendor/big(high)"
    assert resp.headers["X-Router-Rule"] == "hard"


@respx.mock
async def test_param_effort_style_sets_a_body_field(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    cfg = make_config(
        policy={"rules": [], "default": {"model": "paramy", "effort": "high"}},
        aliases={"auto": {"allowed_models": ["paramy"]}},
    )
    app = create_app(cfg, Store(tmp_path / "db.sqlite"))
    mock_jev()
    route = upstream_ok()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as c:
        resp = await c.post("/v1/chat/completions", json=chat_body())
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "vendor/paramy"
    assert sent["reasoning_effort"] == "high"
    assert resp.headers["X-Router-Model"] == "vendor/paramy"


@respx.mock
async def test_client_sent_reasoning_effort_is_replaced_for_an_alias(client):
    mock_jev(difficulty=2.0)
    route = upstream_ok()
    await client.post("/v1/chat/completions", json=chat_body(reasoning_effort="minimal"))
    sent = json.loads(route.calls[0].request.content)
    assert "reasoning_effort" not in sent  # big uses the suffix style
    assert sent["model"] == "vendor/big(high)"


# --- pins ---------------------------------------------------------------


@respx.mock
async def test_second_turn_reuses_the_pin(client):
    jev = mock_jev()
    upstream_ok()
    first = await client.post("/v1/chat/completions", json=chat_body())
    second_body = chat_body()
    second_body["messages"].append({"role": "assistant", "content": "4"})
    second_body["messages"].append({"role": "user", "content": "and again?"})
    second = await client.post("/v1/chat/completions", json=second_body)

    assert jev.call_count == 1  # the pin skips Jev
    assert second.headers["X-Router-Pinned"] == "true"
    assert second.headers["X-Router-Model"] == first.headers["X-Router-Model"]
    assert second.headers["X-Router-Decision"] == first.headers["X-Router-Decision"]


@respx.mock
async def test_a_different_conversation_is_decided_again(client):
    jev = mock_jev()
    upstream_ok()
    await client.post("/v1/chat/completions", json=chat_body("one question"))
    await client.post("/v1/chat/completions", json=chat_body("a different first message"))
    assert jev.call_count == 2


@respx.mock
async def test_pin_moves_up_when_the_context_no_longer_fits(client, app_and_store):
    _app, store, _cfg = app_and_store
    mock_jev()
    route = upstream_ok()
    first = await client.post("/v1/chat/completions", json=chat_body())
    assert first.headers["X-Router-Model"] == "vendor/small(none)"

    grown = chat_body()
    grown["messages"].append({"role": "user", "content": "x" * 20000})
    second = await client.post("/v1/chat/completions", json=grown)
    assert second.headers["X-Router-Rule"] == "repin_context"
    assert second.headers["X-Router-Model"].startswith("vendor/big")
    assert second.headers["X-Router-Decision"] != first.headers["X-Router-Decision"]

    # The move is remembered: a third turn stays on the bigger model.
    third = await client.post("/v1/chat/completions", json=grown)
    assert third.headers["X-Router-Rule"] == "pin"
    assert third.headers["X-Router-Model"].startswith("vendor/big")
    assert json.loads(route.calls[-1].request.content)["model"].startswith("vendor/big")


@respx.mock
async def test_a_fallback_decision_is_not_pinned(client):
    respx.post(JEV_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    upstream_ok()
    first = await client.post("/v1/chat/completions", json=chat_body())
    # X-Router-Fallback now counts upstream fallbacks, so a decider fallback
    # has its own header.
    assert first.headers["X-Router-Decider-Fallback"] == "true"
    assert "X-Router-Fallback" not in first.headers
    second = await client.post("/v1/chat/completions", json=chat_body())
    assert second.headers["X-Router-Pinned"] == "false"


@respx.mock
async def test_jev_down_still_serves_the_request(client):
    respx.post(JEV_URL).mock(return_value=httpx.Response(529))
    route = upstream_ok()
    resp = await client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    assert route.called


# --- shadow mode --------------------------------------------------------


@respx.mock
async def test_shadow_mode_logs_the_decision_but_sends_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    cfg = make_config(settings={"mode": "shadow"})
    store = Store(tmp_path / "db.sqlite")
    app = create_app(cfg, store)
    mock_jev()
    route = upstream_ok()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as c:
        resp = await c.post("/v1/chat/completions", json=chat_body())

    assert json.loads(route.calls[0].request.content)["model"] == "vendor/big(medium)"
    assert resp.headers["X-Router-Shadow-Model"] == "small"
    rows = store.recent_decisions(5)
    assert rows[0]["model"] == "big"  # actual execution, including shadow mode
    assert rows[0]["intended_model"] == "small"  # classifier recommendation
    assert rows[0]["mode"] == "shadow"
    assert store.get_pin(rows[0]["conversation_key"], 3600) is None


# --- streaming ----------------------------------------------------------


@respx.mock
async def test_streaming_chunks_arrive_in_order_and_unmodified(client):
    chunks = [
        b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"three"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def body():
        for chunk in chunks:
            yield chunk

    mock_jev()
    respx.post(CHAT).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )
    )

    received = bytearray()
    async with client.stream(
        "POST", "/v1/chat/completions", json=chat_body(stream=True)
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream"
        assert resp.headers["X-Router-Model"] == "vendor/small(none)"
        async for piece in resp.aiter_raw():
            received.extend(piece)
    assert bytes(received) == b"".join(chunks)


@respx.mock
async def test_upstream_status_and_headers_are_kept(client):
    mock_jev()
    respx.post(CHAT).mock(
        return_value=httpx.Response(
            429,
            json={"error": "rate limited"},
            headers={"retry-after": "3", "transfer-encoding": "chunked"},
        )
    )
    resp = await client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "3"
    assert "transfer-encoding" not in {k.lower() for k in resp.headers}


@respx.mock
async def test_usage_is_logged_for_a_non_streaming_reply(client, app_and_store):
    _app, store, _cfg = app_and_store
    mock_jev()
    upstream_ok({"id": "x", "usage": {"total_tokens": 77}})
    await client.post("/v1/chat/completions", json=chat_body())
    row = store.recent_decisions(1)[0]
    assert row["upstream_status"] == 200
    assert row["upstream_usage"]["total_tokens"] == 77


# --- /v1/models ---------------------------------------------------------


@respx.mock
async def test_models_merges_aliases(client):
    respx.get(f"{UPSTREAM}/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "vendor/small", "object": "model"}]},
        )
    )
    resp = await client.get("/v1/models")
    ids = [m["id"] for m in resp.json()["data"]]
    assert ids[0] == "vendor/small"
    assert set(ids) == {"vendor/small", "auto", "auto-fast"}


@respx.mock
async def test_models_passes_an_upstream_error_through(client):
    respx.get(f"{UPSTREAM}/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "Missing API key"})
    )
    resp = await client.get("/v1/models")
    assert resp.status_code == 401


# --- router endpoints ---------------------------------------------------


@respx.mock
async def test_health(client):
    resp = await client.get("/router/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "active"
    assert body["jev_key_present"] is True
    assert "auto" in body["aliases"]
    assert "test-key" not in json.dumps(body)


@respx.mock
async def test_decisions_endpoint(client):
    mock_jev()
    upstream_ok()
    await client.post("/v1/chat/completions", json=chat_body())
    resp = await client.get("/router/decisions?limit=5")
    rows = resp.json()["decisions"]
    assert len(rows) == 1
    assert rows[0]["alias"] == "auto"
    assert rows[0]["answers"]["difficulty"]["score"] == 0.1
    assert rows[0]["features"]["message_count"] == 1
    assert rows[0]["state"] is None  # log_state is off


# --- feedback -----------------------------------------------------------


async def route_once(client, text="hello"):
    mock_jev()
    upstream_ok()
    resp = await client.post("/v1/chat/completions", json=chat_body(text))
    return resp.headers["X-Router-Decision"]


@respx.mock
async def test_feedback_by_decision_id(client, app_and_store):
    _app, store, _cfg = app_and_store
    decision_id = await route_once(client)
    resp = await client.post(
        "/router/feedback",
        json={"decision_id": decision_id, "verdict": "too_weak",
              "better_model": "big", "better_effort": "high", "note": "missed an edge case"},
    )
    assert resp.status_code == 200
    assert resp.json()["decision"]["alias"] == "auto"
    rows = store.recent_feedback(5)
    assert rows[0]["verdict"] == "too_weak"
    assert rows[0]["better_model"] == "big"
    assert rows[0]["source"] == "api"
    assert rows[0]["note"] == "missed an edge case"


@respx.mock
async def test_feedback_last_uses_the_newest_decision(client, app_and_store):
    _app, store, _cfg = app_and_store
    await route_once(client, "first conversation")
    newest = await route_once(client, "second conversation")
    resp = await client.post("/router/feedback", json={"decision_id": "last", "verdict": "right"})
    assert resp.json()["decision_id"] == newest


@respx.mock
async def test_feedback_last_is_per_client(client, app_and_store):
    mock_jev()
    upstream_ok()
    mine = (
        await client.post(
            "/v1/chat/completions",
            json=chat_body("from agent-cli"),
            headers={"X-Router-Client": "agent-cli"},
        )
    ).headers["X-Router-Decision"]
    await client.post(
        "/v1/chat/completions",
        json=chat_body("from chat-ui"),
        headers={"X-Router-Client": "chat-ui"},
    )
    resp = await client.post(
        "/router/feedback",
        json={"decision_id": "last", "verdict": "right"},
        headers={"X-Router-Client": "agent-cli"},
    )
    assert resp.json()["decision_id"] == mine


@respx.mock
async def test_feedback_validation_errors(client):
    decision_id = await route_once(client)
    bad_verdict = await client.post(
        "/router/feedback", json={"decision_id": decision_id, "verdict": "meh"}
    )
    assert bad_verdict.status_code == 400
    assert "too_weak" in bad_verdict.json()["error"]["message"]

    bad_model = await client.post(
        "/router/feedback",
        json={"decision_id": decision_id, "verdict": "right", "better_model": "ghost"},
    )
    assert bad_model.status_code == 400
    assert "not configured" in bad_model.json()["error"]["message"]

    bad_effort = await client.post(
        "/router/feedback",
        json={"decision_id": decision_id, "verdict": "right",
              "better_model": "small", "better_effort": "xhigh"},
    )
    assert bad_effort.status_code == 400
    assert "does not allow effort" in bad_effort.json()["error"]["message"]

    missing = await client.post("/router/feedback", json={"verdict": "right"})
    assert missing.status_code == 400


@respx.mock
async def test_feedback_unknown_decision_is_404(client):
    resp = await client.post(
        "/router/feedback", json={"decision_id": "deadbeef0000", "verdict": "right"}
    )
    assert resp.status_code == 404


@respx.mock
async def test_feedback_last_with_no_decisions_is_404(client):
    resp = await client.post("/router/feedback", json={"decision_id": "last", "verdict": "right"})
    assert resp.status_code == 404


@respx.mock
async def test_feedback_listing_joins_the_decision(client):
    decision_id = await route_once(client)
    await client.post(
        "/router/feedback", json={"decision_id": decision_id, "verdict": "too_slow"}
    )
    resp = await client.get("/router/feedback?limit=10")
    row = resp.json()["feedback"][0]
    assert row["decision_id"] == decision_id
    assert row["model"] == "small"
    assert row["rule"] == "easy"


@respx.mock
async def test_feedback_on_a_pinned_turn_lands_on_the_first_decision(client, app_and_store):
    _app, store, _cfg = app_and_store
    first_id = await route_once(client)
    second = await client.post("/v1/chat/completions", json=chat_body())
    assert second.headers["X-Router-Decision"] == first_id
    await client.post("/router/feedback", json={"decision_id": "last", "verdict": "right"})
    assert store.recent_feedback(5)[0]["decision_id"] == first_id
