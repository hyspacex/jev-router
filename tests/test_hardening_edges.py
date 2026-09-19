"""Edge coverage for the mandatory execution gate and upgrade lifecycle."""

import asyncio
import time
from dataclasses import replace

import httpx
import pytest
import respx
import test_app as shared
from conftest import make_config, write_config
from test_app import JEV_URL, chat_body, jev_answers, mock_jev, upstream_ok
from test_pins import log

from jev_router.cli import main
from jev_router.config import Route
from jev_router.deciders import build_decider
from jev_router.deciders.jev import JevDecider
from jev_router.features import extract_features
from jev_router.policy import PlanEntry, RoutingError, finalize, validate_entry

app_and_store = shared.app_and_store
client = shared.client


def test_gate_rejects_invalid_entries():
    cfg = make_config()
    features = extract_features(chat_body(model="auto-fast"), {})
    entry = PlanEntry(model="big", effort="high", provider="default")
    with pytest.raises(RoutingError):
        validate_entry(cfg, cfg.aliases["auto-fast"], features, entry)
    with pytest.raises(RoutingError):
        validate_entry(
            cfg, cfg.aliases["auto"], features, replace(entry, model="paramy")
        )


@respx.mock
async def test_shadow_default_is_guarded(app_and_store, client):
    _app, store, cfg = app_and_store
    cfg.settings.mode = "shadow"
    cfg.aliases["auto"].allowed_models = ["small"]
    mock_jev()
    upstream = upstream_ok()
    response = await client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert b"vendor/small" in upstream.calls[0].request.content
    assert store.recent_decisions(1)[0]["model"] == "small"


def test_fallbacks_are_filtered_and_effort_guarded():
    from test_fallbacks import LANED

    cfg = make_config(**LANED)
    features = extract_features(chat_body(), {})
    result = finalize(
        cfg, cfg.aliases["auto-fast"], Route(route="lane"), "test", "test", features
    )
    assert [e.effort for e in result.plan] == ["low", "low"]
    cfg.aliases["auto"].allowed_models = ["big"]
    result = finalize(
        cfg, cfg.aliases["auto"], Route(route="lane"), "test", "test", features
    )
    assert [e.model for e in result.plan] == ["big"]


@respx.mock
async def test_reused_pin_ttl_is_not_extended(app_and_store, client):
    _app, store, _cfg = app_and_store
    mock_jev()
    upstream_ok()
    await client.post("/v1/chat/completions", json=chat_body())
    key = store.recent_decisions(1)[0]["conversation_key"]
    original = store.get_pin(key, 3600)
    await client.post("/v1/chat/completions", json=chat_body())
    assert store.get_pin(key, 3600) == original


@respx.mock
async def test_policy_exception_uses_nonpinnable_fallback(monkeypatch, client):
    import jev_router.deciders.jev as adapter

    mock_jev()
    upstream_ok()

    def broken(*args, **kwargs):
        raise RuntimeError("failure")

    monkeypatch.setattr(adapter, "evaluate", broken)
    response = await client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert response.headers["X-Router-Decider-Fallback"] == "true"


@respx.mock
async def test_failed_fallback_decider_still_returns_a_decision(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = make_config()
    respx.post(JEV_URL).mock(return_value=httpx.Response(529))
    async with httpx.AsyncClient() as http:
        decider = build_decider("jev", cfg, {"jev_client": http})
        assert isinstance(decider, JevDecider) and decider.fallback is not None

        async def broken(*args):
            raise RuntimeError("failed local decider")

        monkeypatch.setattr(decider.fallback, "decide", broken)
        result = await decider.decide(
            extract_features(chat_body(), {}), cfg.aliases["auto"]
        )
        assert result.fallback and not result.routing_error
        cfg.aliases["auto"].allowed_models = []
        result = await decider.decide(
            extract_features(chat_body(), {}), cfg.aliases["auto"]
        )
        assert result.fallback and result.routing_error


@respx.mock
async def test_optional_draft_cannot_bypass_allowed_models(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = make_config(
        aliases={
            "auto": {
                "allowed_models": ["big"],
                "draft": {"enabled": True, "model": "small"},
            }
        }
    )
    respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=jev_answers()))
    async with httpx.AsyncClient() as http:
        decision = await build_decider("jev", cfg, {"jev_client": http}).decide(
            extract_features(chat_body(), {}), cfg.aliases["auto"]
        )
    assert not decision.fallback and decision.model == "big"
    # respx rejects any unexpected upstream call.


def test_model_update_clears_effort_for_effortless_model(store):
    row_id, decision_id = log(store)
    store.update_decision(row_id, model="plain", effort=None, accepted=True)
    row = store.get_decision(decision_id)
    assert row is not None and row["model"] == "plain" and row["effort"] is None
    store.update_decision(row_id, stream_state="completed", response_bytes=0)
    assert store.get_decision(decision_id) == row | {
        "stream_state": "completed",
        "response_bytes": 0,
    }


def test_explicit_retention_keeps_recent_rows(store, monkeypatch):
    monkeypatch.setattr("jev_router.pins.time.time", lambda: 100)
    _, old = log(store)
    store.set_pin("old", "big", "high", old)
    store.add_feedback(decision_id=old, verdict="right")
    monkeypatch.setattr("jev_router.pins.time.time", lambda: 10000)
    _, recent = log(store)
    store.set_pin("new", "big", "high", recent)
    store.add_feedback(decision_id=recent, verdict="right")
    assert store.prune(1000) == {"pins": 1, "decisions": 1, "feedback": 1}
    assert store.get_decision(old) is None
    assert store.get_decision(recent) is not None
    assert store.recent_feedback()[0]["decision_id"] == recent


def test_prune_cli_requires_positive_age(tmp_path):
    path = write_config(tmp_path)
    assert main(["-c", path, "prune", "--older-than-days", "0"]) == 2
    assert main(["-c", path, "prune", "--older-than-days", "30"]) == 0


async def test_cancelled_stream_records_disconnect_and_closes_upstream(app_and_store):
    app, store, _cfg = app_and_store
    row_id, decision_id = log(store)
    entered = asyncio.Event()

    class Interrupted(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            entered.set()
            await asyncio.Future()
            yield b"unused"

        async def aclose(self):
            self.closed = True

    stream = Interrupted()
    response = httpx.Response(200, stream=stream)

    async def consume():
        async for _ in app.state.router._stream(
            response, row_id, started=time.perf_counter()
        ):
            pass

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    row = store.get_decision(decision_id)
    assert row is not None and row["stream_state"] == "disconnected"
