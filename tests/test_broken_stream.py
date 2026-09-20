"""What happens to a strict session when a reply stops half way through.

Three ways a stream can end without a provider saying it finished: the
upstream closes the socket cleanly, the upstream breaks, and the client that
asked walks away. None of them may leave the session holding its in-flight
execution claim, none of them may record the attempt as completed, and all of
them have to leave a session somebody can carry on using.

The fakes are upstreams and ASGI callables. Nothing here mocks the code under
test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

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


async def started(service, **kwargs) -> ResponsesClient:
    caller = ResponsesClient(service, **kwargs)
    response = await caller.start()
    assert response.status_code == 200, response.text
    return caller


def created_only(response_id: str = "resp_0001") -> bytes:
    """An event stream that stops right after `response.created`."""
    event = {
        "type": "response.created",
        "response": {"id": response_id, "status": "in_progress"},
    }
    return f"event: response.created\ndata: {json.dumps(event)}\n\n".encode()


def request_rows(service, session_id: str) -> list[dict[str, Any]]:
    rows = service.sessions._conn.execute(
        "SELECT * FROM session_requests WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --- 1. the upstream closes the stream early ------------------------------


@respx.mock
async def test_a_stream_that_stops_after_response_created_releases_the_claim(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream(body_override=created_only())
    caller = await started(service)

    plan, response = await caller.turn("Investigate the escaped-quote failure.")
    assert response.status_code == 200
    assert response.content == created_only()

    assert service.sessions.inflight(caller.session_id) == 0
    row = service.sessions.get_plan(caller.session_id, plan_id=plan["plan_id"])
    assert row["status"] == "outcome_unknown"


@respx.mock
async def test_a_stream_that_stops_early_is_not_recorded_as_completed(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream(body_override=created_only())
    caller = await started(service)

    _plan, response = await caller.turn("Investigate the escaped-quote failure.")
    assert response.status_code == 200

    rows = request_rows(service, caller.session_id)
    assert [row["status"] for row in rows] == ["unknown"]
    assert service.sessions.unresolved(caller.session_id) == 1


# --- 3. the client that asked walks away ----------------------------------


class Caller:
    """One ASGI call we can interrupt exactly where we want to.

    `httpx`'s ASGI transport reads a reply to the end, so it cannot say what
    happens when a client goes away mid-stream. This drives the app itself:
    after `chunks` body messages it reports `http.disconnect` and then stops
    taking bytes, which is where a real client's socket leaves the server.
    """

    def __init__(
        self,
        app,
        scope: dict[str, Any],
        body: bytes,
        *,
        chunks: int = 1,
        before_body: bool = False,
    ):
        self.app = app
        self.scope = scope
        self.body = body
        self.chunks = chunks
        self.before_body = before_body
        self.messages: list[dict[str, Any]] = []
        self.sent_body = False
        self.gone = asyncio.Event()
        self.hung = asyncio.Event()

    async def receive(self) -> dict[str, Any]:
        if not self.sent_body:
            self.sent_body = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if self.before_body and message["type"] == "http.response.start":
            # Gone before the first byte. The body generator is never started,
            # so it has no `finally` to run at all.
            self.gone.set()
            self.hung.set()
            await asyncio.Event().wait()
        if message["type"] != "http.response.body":
            return
        self.chunks -= 1
        if self.chunks > 0:
            return
        # The client is gone. The next chunk never leaves the server: this is
        # the yield the body generator is suspended at when it is cancelled.
        self.gone.set()
        self.hung.set()
        await asyncio.Event().wait()

    async def run(self) -> None:
        await self.app(self.scope, self.receive, self.send)

    @property
    def status(self) -> int:
        return int(self.messages[0]["status"])


def asgi_scope(path: str, headers: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [
            (k.lower().encode(), v.encode())
            for k, v in {**headers, "content-type": "application/json"}.items()
        ],
        "client": ("127.0.0.1", 45678),
        "server": ("router.test", 80),
    }


def long_stream(response_id: str = "resp_0001") -> bytes:
    """A reply with enough events in it to be interrupted in the middle."""
    blocks = [
        "event: response.created\ndata: "
        + json.dumps(
            {
                "type": "response.created",
                "response": {"id": response_id, "status": "in_progress"},
            }
        )
        + "\n\n"
    ]
    for i in range(40):
        blocks.append(
            "event: response.output_text.delta\ndata: "
            + json.dumps({"type": "response.output_text.delta", "delta": f"{i} "})
            + "\n\n"
        )
    return "".join(blocks).encode()


@respx.mock
async def test_a_client_that_disconnects_mid_stream_releases_the_claim(service):
    turn_jev(difficulty=1.9)
    H.responses_upstream(body_override=long_stream())
    caller = await started(service)

    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    body = json.dumps(caller.request_body()).encode()
    headers = caller.execution_headers()
    headers["x-router-turn"] = plan["turn_id"]
    headers["x-router-turn-plan"] = plan["plan_id"]

    call = Caller(service.app, asgi_scope("/v1/responses", headers), body)
    await asyncio.wait_for(call.run(), timeout=5)

    assert call.status == 200
    assert service.sessions.inflight(caller.session_id) == 0
    rows = request_rows(service, caller.session_id)
    assert [row["status"] for row in rows] == ["unknown"]
    row = service.sessions.get_plan(caller.session_id, plan_id=plan["plan_id"])
    assert row["status"] == "outcome_unknown"


# --- 2. the upstream breaks mid-stream ------------------------------------


class Breaking(httpx.AsyncByteStream):
    """A reply that starts arriving and then stops being readable."""

    def __init__(self, head: bytes) -> None:
        self.head = head
        self.closed = False

    async def __aiter__(self):
        yield self.head
        raise httpx.ReadError("the connection went away")

    async def aclose(self) -> None:
        self.closed = True


def breaking_upstream(head: bytes = b""):
    """A Responses upstream whose body raises after the first chunk."""
    seen: list[dict[str, Any]] = []
    stream = Breaking(head or created_only())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    respx.post(H.RESPONSES).mock(side_effect=handler)
    return seen, stream


@respx.mock
async def test_an_upstream_that_breaks_mid_stream_releases_the_claim(service):
    turn_jev(difficulty=1.9)
    breaking_upstream()
    caller = await started(service)

    plan = await caller.plan("Investigate the escaped-quote failure.")
    caller.record(plan, "Investigate the escaped-quote failure.")
    with pytest.raises(httpx.ReadError):
        await caller.send(plan)

    assert service.sessions.inflight(caller.session_id) == 0
    rows = request_rows(service, caller.session_id)
    assert [row["status"] for row in rows] == ["stream_failed"]
    row = service.sessions.get_plan(caller.session_id, plan_id=plan["plan_id"])
    assert row["status"] == "outcome_unknown"


# --- a session somebody can carry on using --------------------------------


@respx.mock
async def test_a_keep_plan_can_be_sent_again_under_a_new_request_id(service):
    turn_jev(difficulty=0.2)  # keeps the effort: nothing is left in the air
    H.responses_upstream(body_override=created_only())
    caller = await started(service)

    plan, first = await caller.turn("Tidy the docstring.")
    assert plan["action"] == "keep"
    assert first.status_code == 200
    assert service.sessions.inflight(caller.session_id) == 0

    respx.post(H.RESPONSES).mock(
        side_effect=lambda request: httpx.Response(
            200,
            content=H.sse_bytes(H.response_payload("resp_0002")),
            headers={"content-type": "text/event-stream"},
        )
    )
    again = await caller.send(plan, request_id="execution-retry")
    assert again.status_code == 200, again.text

    rows = request_rows(service, caller.session_id)
    assert [row["status"] for row in rows] == ["unknown", "completed"]


@respx.mock
async def test_the_same_request_id_is_never_replayed_after_an_unknown_outcome(service):
    turn_jev(difficulty=0.2)
    H.responses_upstream(body_override=created_only())
    caller = await started(service)

    plan = await caller.plan("Tidy the docstring.")
    caller.record(plan, "Tidy the docstring.")
    first = await caller.send(plan, request_id="execution-once")
    assert first.status_code == 200
    sent = len(respx.calls)

    again = await caller.send(plan, request_id="execution-once")
    assert again.status_code == 409
    # Not REQUEST_ALREADY_COMPLETED: nothing said this one completed.
    assert again.json()["error"]["code"] == "EXECUTION_OUTCOME_UNKNOWN"
    assert "reconcile" in again.json()["error"]["message"]
    assert len(respx.calls) == sent  # nothing was sent to the provider again


@respx.mock
async def test_a_client_that_disconnects_before_the_first_byte_releases_the_claim(
    service,
):
    turn_jev(difficulty=0.2)
    H.responses_upstream()
    caller = await started(service)

    plan = await caller.plan("Tidy the docstring.")
    caller.record(plan, "Tidy the docstring.")
    headers = caller.execution_headers()
    headers["x-router-turn"] = plan["turn_id"]
    headers["x-router-turn-plan"] = plan["plan_id"]
    call = Caller(
        service.app,
        asgi_scope("/v1/responses", headers),
        json.dumps(caller.request_body()).encode(),
        before_body=True,
    )
    await asyncio.wait_for(call.run(), timeout=5)

    assert call.status == 200
    assert service.sessions.inflight(caller.session_id) == 0
    assert [row["status"] for row in request_rows(service, caller.session_id)] == [
        "unknown"
    ]


@respx.mock
async def test_a_client_that_disconnects_from_a_non_streamed_reply_releases_the_claim(
    service,
):
    turn_jev(difficulty=0.2)
    H.responses_upstream(sse=False)
    caller = await started(service)

    plan = await caller.plan("Tidy the docstring.")
    caller.record(plan, "Tidy the docstring.")
    headers = caller.execution_headers()
    headers["x-router-turn"] = plan["turn_id"]
    headers["x-router-turn-plan"] = plan["plan_id"]
    call = Caller(
        service.app,
        asgi_scope("/v1/responses", headers),
        json.dumps(caller.request_body()).encode(),
    )
    await asyncio.wait_for(call.run(), timeout=5)

    assert call.status == 200
    assert service.sessions.inflight(caller.session_id) == 0
    assert [row["status"] for row in request_rows(service, caller.session_id)] == [
        "unknown"
    ]


# --- the same, on the chat endpoint ---------------------------------------


@pytest.fixture
async def chat_service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path)
    yield svc
    await svc.close()


@respx.mock
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_chat_client_that_disconnects_mid_stream_releases_the_claim(
    chat_service, streamed
):
    from session_harness import Client
    from test_app import mock_jev

    mock_jev()
    if streamed:
        respx.post(H.CHAT).mock(
            return_value=httpx.Response(
                200,
                content=b'data: {"id":"cmpl-1"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        )
    else:
        H.upstream()
    caller = Client(chat_service)
    assert (await caller.resolve()).status_code == 200

    headers = caller.execution_headers()
    body = json.dumps(
        {**H.user_request(caller.wire_model()), "stream": streamed}
    ).encode()
    call = Caller(
        chat_service.app, asgi_scope("/v1/chat/completions", headers), body
    )
    await asyncio.wait_for(call.run(), timeout=5)

    assert call.status == 200
    assert chat_service.sessions.inflight(caller.session_id) == 0
    assert [
        row["status"] for row in request_rows(chat_service, caller.session_id)
    ] == ["unknown"]
