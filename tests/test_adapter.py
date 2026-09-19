"""The experimental Responses client adapter, against a fake router.

Nothing here talks to a network, and nothing here runs the real router: the
adapter is a client, so what these tests pin down is what it sends and what it
remembers. The fake router records every call and answers with the shapes the
real one answers with.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from jev_router import protocols as P
from jev_router.adapter import (
    Adapter,
    conversation_id_of,
    create_adapter_app,
    read_key_file,
    session_id_for,
)

MARKER = "escaped quotes fail across chunk boundaries"
ADMIN = "test-admin"


# --- a fake router -------------------------------------------------------


def sse(response_id: str, *, status: str = "completed") -> bytes:
    payload = {
        "id": response_id,
        "object": "response",
        "status": status,
        "model": "gpt-6-astra",
        "reasoning": {"effort": "low"},
        "output": [],
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 64,
            "total_tokens": 1264,
            "input_tokens_details": {"cached_tokens": 900},
            "output_tokens_details": {"reasoning_tokens": 92},
        },
    }
    blocks = [
        ("response.created", {"type": "response.created", "response": {**payload, "status": "in_progress"}}),
        (f"response.{status}", {"type": f"response.{status}", "response": payload}),
    ]
    return "".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in blocks).encode()


class FakeRouter:
    """Records what the adapter sends and answers as the router would."""

    def __init__(self) -> None:
        self.resolves: list[dict[str, Any]] = []
        self.plans: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.known: set[str] = set()
        self.plan_answers: list[dict[str, Any] | Response] = []
        self.execution_answer: Response | None = None
        self.report: dict[str, Any] = {}
        self.counter = 0

    # --- endpoints ---------------------------------------------------

    async def resolve(self, request: Request) -> Response:
        payload = await request.json()
        self.resolves.append(payload)
        session_id = payload["session_id"]
        if payload["intent"] == "resume" and session_id not in self.known:
            return JSONResponse(
                {
                    "error": {
                        "type": "session_error",
                        "code": "SESSION_UNKNOWN",
                        "message": "never resolved here",
                    }
                },
                status_code=409,
            )
        self.known.add(session_id)
        return JSONResponse(
            {
                "schema_version": "1",
                "session_id": session_id,
                "state": "prepared",
                "binding_revision": "sha256:" + session_id[-8:],
                "decision_id": "d0001",
                "execution": {
                    "model_key": "gpt-6-astra-responses",
                    "provider": "openai",
                    "wire_model": "gpt-6-astra",
                    "protocol": "openai-responses",
                    "context_window": 400000,
                    "max_output_tokens": 16384,
                    "supports_tools": True,
                    "supports_vision": True,
                    "initial_effort": "low",
                    "effort_mode": "adaptive",
                    "base_effort": "low",
                    "effective_effort": "low",
                    "adaptation": "active",
                },
                "decision": {},
                "limits": {},
            }
        )

    async def turn_plan(self, request: Request) -> Response:
        payload = await request.json()
        self.plans.append(payload)
        answer = self.plan_answers.pop(0) if self.plan_answers else keep_plan(payload)
        if isinstance(answer, Response):
            return answer
        return JSONResponse({**answer, "turn_id": payload["turn_id"]})

    async def sessions(self, request: Request) -> Response:
        return JSONResponse(self.report)

    async def responses(self, request: Request) -> Response:
        self.executions.append(json.loads(await request.body()))
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        if self.execution_answer is not None:
            return self.execution_answer
        self.counter += 1
        return Response(
            sse(f"resp_{self.counter:04d}"), media_type="text/event-stream"
        )

    async def models(self, request: Request) -> Response:
        return JSONResponse({"object": "list", "data": []})

    @property
    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/router/resolve", self.resolve, methods=["POST"]),
                Route("/router/turn-plan", self.turn_plan, methods=["POST"]),
                Route("/router/sessions/{sid}", self.sessions, methods=["GET"]),
                Route("/v1/responses", self.responses, methods=["POST"]),
                Route("/v1/models", self.models, methods=["GET"]),
            ]
        )


def keep_plan(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "session_id": payload["session_id"],
        "turn_id": payload["turn_id"],
        "plan_id": "plan" + payload["turn_id"][-4:],
        "action": "keep",
        "adaptation": "active",
        "model_key": "gpt-6-astra-responses",
        "base_effort": "low",
        "previous_effective_effort": "low",
        "next_effective_effort": "low",
        "reason": "the evidence asks for no change",
        "status": "planned",
    }


def change_plan(to: str = "medium", plan_id: str = "planchange") -> dict[str, Any]:
    return {
        "schema_version": "1",
        "session_id": "",
        "turn_id": "",
        "plan_id": plan_id,
        "action": "change_effort",
        "adaptation": "active",
        "model_key": "gpt-6-astra-responses",
        "base_effort": "low",
        "previous_effective_effort": "low",
        "next_effective_effort": to,
        "reason": f"low -> {to}: difficulty 1.90 is at or above 1.8",
        "status": "planned",
        "update": {
            "item": P.configuration_update(to),
            "rule": "insert this item immediately before the designated new user message",
        },
        "headers": {"X-Router-Turn-Plan": plan_id},
    }


# --- fixtures ------------------------------------------------------------


@pytest.fixture
def router() -> FakeRouter:
    return FakeRouter()


@pytest.fixture
async def rig(router):
    """An adapter wired to the fake router, and a client wired to the adapter."""
    lines: list[str] = []
    adapter = Adapter(
        router_url="http://router.test",
        alias="astra-adaptive",
        admin_token=ADMIN,
        http=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router.app), base_url="http://router.test"
        ),
        trace=lines.append,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_adapter_app(adapter)),
        base_url="http://adapter.test",
    )
    client.trace = lines  # type: ignore[attr-defined]
    client.adapter = adapter  # type: ignore[attr-defined]
    yield client
    await client.aclose()
    await adapter.aclose()


# --- the bodies a Responses client sends ---------------------------------


def user_item(text: str, item_id: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }
    if item_id:
        item["id"] = item_id
    return item


def body(*items: Any, effort: str = "low", **extra: Any) -> dict[str, Any]:
    out = {
        "model": "gpt-6-astra",
        "input": list(items),
        "reasoning": {"effort": effort, "context": "all_turns"},
        "store": False,
        "stream": True,
    }
    out.update(extra)
    return out


async def send(client, payload, conversation="conv-1", headers=None):
    sent = {"content-type": "application/json", "session-id": conversation}
    if headers is not None:
        sent = {**sent, **headers}
        sent = {k: v for k, v in sent.items() if v is not None}
    return await client.post("/v1/responses", json=payload, headers=sent)


# --- conversation identity -----------------------------------------------


def test_conversation_id_prefers_a_header_then_the_body():
    assert conversation_id_of({"Session-Id": "abc"}, {"prompt_cache_key": "zzz"}) == "abc"
    assert conversation_id_of({}, {"prompt_cache_key": "zzz"}) == "zzz"
    assert conversation_id_of({"x-conversation-id": " p "}, {}) == "p"
    assert conversation_id_of({}, {}) == ""


def test_conversation_id_is_never_derived_from_message_text():
    """Two conversations that say the same thing are two conversations."""
    assert session_id_for("conv-a") != session_id_for("conv-b")
    assert session_id_for("conv-a") == session_id_for("conv-a")
    assert conversation_id_of({}, {"input": [user_item("hello")]}) == ""


async def test_a_request_with_no_conversation_identifier_is_refused(rig, router):
    reply = await rig.post(
        "/v1/responses", json=body(user_item("hi")), headers={"content-type": "application/json"}
    )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "ADAPTER_NO_CONVERSATION_ID"
    assert router.resolves == []
    assert router.executions == []


async def test_the_body_cache_key_identifies_a_conversation_with_no_headers(rig, router):
    reply = await rig.post(
        "/v1/responses",
        json=body(user_item("hi"), prompt_cache_key="from-the-body"),
        headers={"content-type": "application/json"},
    )
    assert reply.status_code == 200
    assert router.resolves[0]["session_id"] == session_id_for("from-the-body")


# --- resolving once ------------------------------------------------------


async def test_one_resolve_per_conversation(rig, router):
    first = user_item("Add quoted CSV fields.", "msg-1")
    await send(rig, body(first))
    await send(rig, body(first, user_item(MARKER, "msg-2")))
    await send(rig, body(first, user_item(MARKER, "msg-2"), user_item("Again.", "msg-3")))
    intents = [r["intent"] for r in router.resolves]
    # One resume that finds nothing, then one new. Never a second binding.
    assert intents == ["resume", "new"]
    assert router.resolves[1]["client_contract"]["turn_boundary_reporting"] is True
    assert router.resolves[1]["client_contract"]["protocols"] == ["openai-responses"]


async def test_two_conversations_resolve_two_sessions(rig, router):
    await send(rig, body(user_item("hi", "m1")), conversation="conv-a")
    await send(rig, body(user_item("hi", "m1")), conversation="conv-b")
    sessions = {payload["session_id"] for payload in router.resolves}
    assert sessions == {session_id_for("conv-a"), session_id_for("conv-b")}
    assert len(router.executions) == 2


async def test_request_ids_are_unique(rig, router):
    first = user_item("one", "m1")
    await send(rig, body(first))
    await send(rig, body(first, user_item("two", "m2")))
    await send(rig, body(first, user_item("two", "m2"), user_item("three", "m3")))
    ids = [h["x-router-request-id"] for h in router.headers]
    assert len(ids) == 3
    assert len(set(ids)) == 3
    for headers in router.headers:
        assert headers["x-router-session"] == session_id_for("conv-1")
        assert headers["x-router-binding"].startswith("sha256:")
        assert headers["x-router-admin-token"] == ADMIN


# --- new turn against continuation ---------------------------------------


async def test_a_new_user_message_is_a_new_turn(rig, router):
    first = user_item("one", "m1")
    await send(rig, body(first))
    await send(rig, body(first, {"type": "message", "role": "assistant", "content": []}, user_item("two", "m2")))
    assert [p["turn_id"] for p in router.plans] == ["turn-0001", "turn-0002"]
    assert router.plans[1]["previous_turn"]["id"] == "turn-0001"


async def test_a_continuation_reports_the_effort_the_turn_moved_to(rig, router):
    """The trace follows the router, not the effort the session was bound at."""
    router.plan_answers = [change_plan("high")]
    first = user_item("hard work", "m1")
    await send(rig, body(first))
    await send(
        rig,
        body(
            first,
            {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ),
    )
    line = next(l for l in rig.trace if "action=continuation" in l)
    assert "base=low" in line
    assert "effective=high" in line


async def test_a_tool_continuation_stays_in_the_same_turn(rig, router):
    first = user_item("read the parser", "m1")
    await send(rig, body(first))
    assert len(router.plans) == 1
    continuation = body(
        first,
        {"type": "function_call", "call_id": "call_1", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    )
    await send(rig, continuation)
    # No second plan, the same turn id, and no fresh item.
    assert len(router.plans) == 1
    assert router.headers[-1]["x-router-turn"] == "turn-0001"
    assert "x-router-turn-plan" not in router.headers[-1]
    assert P.update_positions(P.input_items(router.executions[-1])) == []


async def test_an_unsettled_tool_loop_is_reported_as_unsettled(rig, router):
    first = user_item("read the parser", "m1")
    await send(rig, body(first))
    # A new user message arriving while a tool call has no result yet.
    await send(
        rig,
        body(
            first,
            {"type": "function_call", "call_id": "call_1", "name": "exec", "arguments": "{}"},
            user_item("actually, stop", "m2"),
        ),
    )
    previous = router.plans[-1]["previous_turn"]
    assert previous["pending_tool_calls"] == 1
    assert previous["settled"] is False


# --- the update item -----------------------------------------------------


async def test_the_planned_item_is_inserted_once_before_the_new_user_message(rig, router):
    router.plan_answers = [change_plan("medium")]
    await send(rig, body(user_item("Improve the parser.", "m1")))
    items = P.input_items(router.executions[0])
    assert [P.update_effort(i) for i in items if P.is_update(i)] == ["medium"]
    at = P.update_positions(items)[0]
    assert P.is_user_message(items[at + 1])
    assert at + 1 == P.last_user_position(items)
    assert router.headers[0]["x-router-turn-plan"] == "planchange"
    assert router.headers[0]["x-router-turn"] == "turn-0001"


async def test_every_later_replay_carries_the_item_at_the_same_position(rig, router):
    router.plan_answers = [change_plan("medium")]
    first = user_item("Improve the parser.", "m1")
    await send(rig, body(first))
    at = P.update_positions(P.input_items(router.executions[0]))[0]

    # Turn two: the client replays its own transcript, which never held the
    # item. The adapter puts it back where the router recorded the anchor.
    reply = {"type": "message", "role": "assistant", "id": "a1", "content": []}
    await send(rig, body(first, reply, user_item(MARKER, "m2")))
    items = P.input_items(router.executions[1])
    assert P.update_positions(items) == [at]
    assert P.update_effort(items[at]) == "medium"
    assert items[:at] == P.input_items(router.executions[0])[:at]

    # Turn three, with a second change on top of the first.
    router.plan_answers = [change_plan("high", "planhigh")]
    await send(rig, body(first, reply, user_item(MARKER, "m2"), reply, user_item("more", "m3")))
    items = P.input_items(router.executions[2])
    assert [P.update_effort(items[i]) for i in P.update_positions(items)] == [
        "medium",
        "high",
    ]
    assert P.update_positions(items)[0] == at


async def test_a_keep_plan_inserts_nothing(rig, router):
    await send(rig, body(user_item("one", "m1")))
    assert P.update_positions(P.input_items(router.executions[0])) == []
    assert router.headers[0]["x-router-turn-plan"] == "plan0001"


async def test_a_blocked_plan_inserts_nothing(rig, router):
    blocked = {**keep_plan({"session_id": "s", "turn_id": "turn-0001"}), "action": "blocked"}
    router.plan_answers = [blocked]
    await send(rig, body(user_item("one", "m1")))
    assert P.update_positions(P.input_items(router.executions[0])) == []


async def test_the_adapter_never_writes_an_item_of_its_own(rig, router):
    """Only what a plan returned, and never a second copy."""
    router.plan_answers = [change_plan("medium"), keep_plan({"session_id": "s", "turn_id": "turn-0002"})]
    first = user_item("Improve the parser.", "m1")
    await send(rig, body(first))
    await send(rig, body(first, user_item("carry on", "m2")))
    items = P.input_items(router.executions[1])
    assert len(P.update_positions(items)) == 1


# --- the base effort -----------------------------------------------------


async def test_the_request_level_effort_is_forced_to_the_base_effort(rig, router):
    router.plan_answers = [change_plan("high")]
    await send(rig, body(user_item("hard work", "m1"), effort="high"))
    reasoning = router.executions[0]["reasoning"]
    assert reasoning["effort"] == "low"
    # Everything else the client put in that block is left alone.
    assert reasoning["context"] == "all_turns"
    assert router.executions[0]["model"] == "gpt-6-astra"


async def test_a_parameter_strategy_moves_the_request_field_instead(rig, router):
    plan = {
        **change_plan("medium"),
        "update": {"parameter": "reasoning.effort", "value": "medium", "rule": "set it"},
    }
    router.plan_answers = [plan]
    await send(rig, body(user_item("hard work", "m1")))
    assert router.executions[0]["reasoning"]["effort"] == "medium"
    assert P.update_positions(P.input_items(router.executions[0])) == []


# --- errors --------------------------------------------------------------


async def test_a_router_refusal_is_passed_through_untouched(rig, router):
    refusal = {
        "error": {
            "type": "session_error",
            "code": "EFFORT_HISTORY_MISMATCH",
            "message": "the update for turn 'turn-0001' is missing",
        }
    }
    router.execution_answer = JSONResponse(refusal, status_code=409)
    reply = await send(rig, body(user_item("one", "m1")))
    assert reply.status_code == 409
    assert reply.json() == refusal


async def test_a_refused_request_does_not_remember_its_item(rig, router):
    router.plan_answers = [change_plan("medium")]
    router.execution_answer = JSONResponse(
        {"error": {"type": "session_error", "code": "SESSION_CONFLICT", "message": "no"}},
        status_code=409,
    )
    first = user_item("one", "m1")
    assert (await send(rig, body(first))).status_code == 409
    router.execution_answer = None
    router.plan_answers = [keep_plan({"session_id": "s", "turn_id": "turn-0002"})]
    await send(rig, body(first, user_item("two", "m2")))
    # The item never reached a transcript, so it is never replayed into one.
    assert P.update_positions(P.input_items(router.executions[-1])) == []


async def test_a_turn_plan_refusal_is_passed_through(rig, router):
    router.plan_answers = [
        JSONResponse(
            {"error": {"type": "session_error", "code": "TURN_NOT_SETTLED", "message": "wait"}},
            status_code=409,
        )
    ]
    reply = await send(rig, body(user_item("one", "m1")))
    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "TURN_NOT_SETTLED"
    # Nothing was forwarded to the model on the strength of a refused plan.
    assert router.executions == []


async def test_a_refusal_is_never_retried_on_another_model(rig, router):
    router.execution_answer = JSONResponse(
        {"error": {"type": "session_error", "code": "CONTEXT_BUDGET_EXCEEDED", "message": "no"}},
        status_code=422,
    )
    await send(rig, body(user_item("one", "m1")))
    assert len(router.executions) == 1
    assert {e["model"] for e in router.executions} == {"gpt-6-astra"}


async def test_a_history_that_shrank_is_refused_rather_than_guessed(rig, router):
    router.plan_answers = [change_plan("medium")]
    first = user_item("one", "m1")
    await send(rig, body(first, user_item("two", "m2"), user_item("three", "m3")))
    spent = len(router.plans)
    # The client comes back with a compacted history that cannot hold the
    # item at the position the router anchored it at.
    reply = await send(rig, body(user_item("summary", "m9")))
    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "ADAPTER_HISTORY_SHRANK"
    # Said before a classifier call was spent, and before a plan was left
    # waiting on a request that could never be made.
    assert len(router.plans) == spent
    assert router.executions[-1]["input"][-1]["id"] == "m3"


async def test_the_same_turn_sent_again_carries_the_same_plan(rig, router):
    """A refused attempt leaves the turn where it was, item and all."""
    router.plan_answers = [change_plan("medium")]
    router.execution_answer = JSONResponse(
        {"error": {"type": "session_error", "code": "SESSION_CONFLICT", "message": "no"}},
        status_code=409,
    )
    first = user_item("Improve the parser.", "m1")
    assert (await send(rig, body(first))).status_code == 409
    router.execution_answer = None
    assert (await send(rig, body(first))).status_code == 200
    # The same plan, the same item, and no second classifier call.
    assert len(router.plans) == 1
    assert router.headers[-1]["x-router-turn-plan"] == "planchange"
    items = P.input_items(router.executions[-1])
    assert [P.update_effort(i) for i in items if P.is_update(i)] == ["medium"]


# --- resume --------------------------------------------------------------


async def test_a_restart_resumes_and_rebuilds_the_update_ledger(router):
    router.known.add(session_id_for("conv-1"))
    router.report = {
        "session_id": session_id_for("conv-1"),
        "turn_plans": [
            {
                "plan_id": "p1",
                "turn_id": "turn-0001",
                "sequence": 1,
                "action": "change_effort",
                "status": "confirmed",
                "to_effort": "medium",
                "anchor_position": 2,
            },
            {
                "plan_id": "p2",
                "turn_id": "turn-0002",
                "sequence": 2,
                "action": "keep",
                "status": "confirmed",
                "to_effort": "medium",
                "anchor_position": None,
            },
        ],
    }
    adapter = Adapter(
        router_url="http://router.test",
        alias="astra-adaptive",
        admin_token=ADMIN,
        http=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router.app), base_url="http://router.test"
        ),
        trace=lambda line: None,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_adapter_app(adapter)),
        base_url="http://adapter.test",
    )
    try:
        await send(
            client,
            body(user_item("a", "m1"), user_item("b", "m2"), user_item("c", "m3")),
        )
    finally:
        await client.aclose()
        await adapter.aclose()
    assert [r["intent"] for r in router.resolves] == ["resume"]
    items = P.input_items(router.executions[0])
    assert P.update_positions(items)[0] == 2
    assert P.update_effort(items[2]) == "medium"
    # Turn numbering carries on from the ledger rather than starting again.
    assert router.plans[0]["turn_id"] == "turn-0003"


# --- credentials ---------------------------------------------------------


async def test_the_clients_own_credential_is_forwarded_unchanged(rig, router):
    await send(rig, body(user_item("one", "m1")), headers={"authorization": "Bearer real-one"})
    assert router.headers[0]["authorization"] == "Bearer real-one"


async def test_a_placeholder_token_is_replaced_with_the_configured_key(rig, router):
    rig.adapter.upstream_key = "the-proxy-key"
    await send(rig, body(user_item("one", "m1")), headers={"authorization": "Bearer placeholder"})
    assert router.headers[0]["authorization"] == "Bearer the-proxy-key"


async def test_a_real_token_is_kept_even_when_a_key_is_configured(rig, router):
    rig.adapter.upstream_key = "the-proxy-key"
    await send(rig, body(user_item("one", "m1")), headers={"authorization": "Bearer mine"})
    assert router.headers[0]["authorization"] == "Bearer mine"


def test_the_key_file_is_read_and_stripped(tmp_path):
    path = tmp_path / "upstream.key"
    path.write_text("  sk-not-a-real-key\n")
    assert read_key_file(str(path)) == "sk-not-a-real-key"
    empty = tmp_path / "empty.key"
    empty.write_text("\n")
    with pytest.raises(ValueError):
        read_key_file(str(empty))


# --- what leaves this process --------------------------------------------


async def test_the_compaction_opt_in_header_is_dropped(rig, router):
    await send(
        rig,
        body(user_item("one", "m1")),
        headers={"x-codex-beta-features": "remote_compaction_v2"},
    )
    assert "x-codex-beta-features" not in router.headers[0]


async def test_the_reply_bytes_are_returned_unchanged(rig, router):
    reply = await send(rig, body(user_item("one", "m1")))
    assert reply.status_code == 200
    assert reply.content == sse("resp_0001")


async def test_nothing_with_message_text_is_written_down(rig, router, tmp_path, monkeypatch):
    """The trace and the remembered state hold identifiers and counts only."""
    monkeypatch.chdir(tmp_path)
    router.plan_answers = [change_plan("medium")]
    first = user_item(MARKER, "m1")
    await send(rig, body(first))
    await send(rig, body(first, user_item(f"and also {MARKER}", "m2")))

    assert list(tmp_path.iterdir()) == []
    assert rig.trace, "the adapter prints a trace line per request"
    for line in rig.trace:
        assert MARKER not in line
    remembered = [
        {
            "conversation_id": c.conversation_id,
            "session_id": c.session_id,
            "binding": c.binding,
            "execution": c.execution,
            "turns": c.turns,
            "last_user_key": c.last_user_key,
            "last_turn_id": c.last_turn_id,
            "updates": [u.__dict__ for u in c.updates],
        }
        for c in rig.adapter.conversations.values()
    ]
    assert MARKER not in json.dumps(remembered, default=str)
    # The one place the text does go is the turn evidence, which the router
    # processes and drops. It is never stored here.
    assert MARKER in json.dumps(router.plans)


async def test_the_trace_names_the_turn_the_action_and_the_efforts(rig, router):
    router.plan_answers = [change_plan("medium")]
    await send(rig, body(user_item("one", "m1")))
    line = next(l for l in rig.trace if "action=change_effort" in l)
    assert "turn=turn-0001" in line
    assert "base=low" in line
    assert "effective=medium" in line
    assert "plan=planchange" in line


async def test_another_path_is_passed_through_to_the_router(rig, router):
    reply = await rig.get("/v1/models")
    assert reply.status_code == 200
    assert reply.json() == {"object": "list", "data": []}
