"""An experimental client adapter for the Responses protocol.

The router's strict-session contract asks a client to do four things an
ordinary OpenAI client has never heard of: resolve a binding before the first
request, acknowledge it on every request, report a new-user-turn boundary, and
record the effort update item the router hands back at the exact position it
says. Most clients will never do any of that.

This module is the missing half, for a client that cannot: a small HTTP shim
that sits in front of the router, speaks the Responses protocol to whatever is
in front of it, and speaks the session contract to the router behind it.

    client  ->  adapter  ->  jev-router  ->  the model API

It is a **client**, not routing policy. It chooses nothing. It never picks a
model, never picks an effort, never retries on another profile and never
invents an update item: every item it inserts came out of a `/router/turn-plan`
answer, and every refusal the router gives it is passed straight back to the
client that asked. What it does own is the mechanical bookkeeping that the
contract needs and that a generic client does not do for itself:

- identify the conversation from the client's own stable identifier;
- resolve once, resume after a restart, and acknowledge the binding;
- tell a new user turn from a tool continuation;
- keep the request-level effort at the base effort for the session's whole
  life, and put the planned update item immediately before the new user
  message, at the same position on every later full-history replay.

It is experimental, it is for short local sessions, and it holds its state in
memory. Nothing it writes down is message text.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ... import protocols as P
from ...features import estimate_input

log = logging.getLogger("jev_router.adapter")

DEFAULT_PORT = 18320
DEFAULT_ROUTER = "http://127.0.0.1:18318"
DEFAULT_ADMIN_TOKEN_ENV = "JEV_ROUTER_ADMIN_TOKEN"
DEFAULT_CLIENT = "codex-adapter"

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
# Recomputed or owned by this hop.
DROP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}

# Where a client's own conversation identifier can be. Codex 0.155 sends
# `session-id` and `thread-id` headers and repeats the same value in the body
# as `prompt_cache_key`. None of these is message text, which is the point: a
# conversation is a thing the client names, never something to guess from what
# was said inside it.
CONVERSATION_HEADERS = (
    "session_id",
    "session-id",
    "conversation_id",
    "conversation-id",
    "x-session-id",
    "x-conversation-id",
    "thread_id",
    "thread-id",
)
CONVERSATION_BODY_FIELDS = (
    "prompt_cache_key",
    "conversation_id",
    "session_id",
    "conversation",
)

# Codex's own metadata about what a request is. It carries, among other
# identifiers, `request_kind`, which is `"turn"` for ordinary work and
# `"compaction"` when Codex is folding its own history up. Measured against
# codex-cli 0.155.1 on 2026-09-19: that header is the only thing that
# distinguishes a compaction from a turn, because the request itself is an
# ordinary `POST /v1/responses` whose last item is a summarising user message.
CODEX_TURN_METADATA = "x-codex-turn-metadata"
CODEX_COMPACTION_KIND = "compaction"

# Codex's beta opt-in header. It used to be dropped here, on the grounds that
# provider-side compaction would rewrite a history the ledger has to be able
# to find again. Owner decision, 2026-09-19: compaction stays exactly what
# Codex does, so the header is forwarded untouched like every other one. (On
# 0.155.1 it is also vestigial: `codex features list` reports
# `remote_compaction_v2` as removed, and the client compacts locally whatever
# the header says.)

# How much of a user message is sent to the router as turn evidence. The
# router cuts it again to the admission packet's bounds; this is a first bound
# so an adapter never ships a whole pasted file as "the current request".
TURN_TEXT_CHARS = 4000


class ClosingStream(StreamingResponse):
    """A streamed reply whose body generator is always finalized.

    The same guarantee the router makes for its own replies, made here
    because this hop is deliberately not built on the router's process. When
    the client goes away, Starlette cancels the sending task. If that lands
    while the generator is suspended at the `yield`, waiting for the socket
    to take a chunk, the generator is never resumed and its `finally` never
    runs: the router's reply is left open and nothing reads what the tap saw,
    so the turn is never recorded as unfinished. Closing it on the way out of
    the ASGI call runs that `finally` while the request is still being
    handled, whatever ended it.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            closing = getattr(self.body_iterator, "aclose", None)
            if closing is not None:
                try:
                    await closing()
                except Exception:  # noqa: BLE001 - the reply is already over
                    log.exception("could not close a streamed reply")


class AdapterError(Exception):
    """Something this hop can state plainly, in the router's error shape."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def response(self) -> JSONResponse:
        return JSONResponse(
            {"error": {"type": "adapter_error", "code": self.code, "message": self.message}},
            status_code=self.status,
        )


class RouterRefused(Exception):
    """The router said no. Its answer is the answer; nothing is retried."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"router refused with {response.status_code}")
        self.status = response.status_code
        self.content = response.content
        self.media_type = response.headers.get("content-type", "application/json")

    def response(self) -> Response:
        return Response(self.content, status_code=self.status, media_type=self.media_type)


@dataclass
class Update:
    """One effort update item this adapter has put into a transcript.

    `position` is the index in the request body's `input` list, which is where
    the router recorded its anchor. Every later full-history replay puts the
    same item back at the same index.
    """

    position: int
    effort: str
    item: dict[str, Any]
    plan_id: str
    turn_id: str


@dataclass
class Conversation:
    """What this hop remembers about one client conversation.

    Identifiers, efforts, positions and counts. No message text, ever.
    """

    conversation_id: str
    session_id: str
    binding: str = ""
    execution: dict[str, Any] = field(default_factory=dict)
    updates: list[Update] = field(default_factory=list)
    turns: int = 0
    requests: int = 0
    # The identity of the user message the last new turn was about, so the
    # next request can be told apart from a tool continuation of this one.
    last_user_key: str = ""
    last_turn_id: str = ""
    # This turn's plan, until the request carrying it is accepted. It is kept
    # so that sending the same turn again after a refusal carries the same
    # plan and the same item rather than silently dropping both.
    pending_plan: dict[str, Any] = field(default_factory=dict)
    # Whether the previous response stream reached a terminal provider event.
    settled: bool = True
    # The turn whose reply this hop did not see finish, until somebody or
    # something says what became of it. It is kept apart from `settled` on
    # purpose: one is "is that turn still running here", which is a lifecycle
    # fact this hop owns, and the other is "did its response finish", which is
    # an outcome fact the router's ledger owns.
    unfinished_turn: str = ""
    # The effort this hop expects the session to be running at. The router's
    # ledger is the record; this is for the trace line.
    effective_effort: str = ""
    # The provider's own last reported counts, for the context estimate.
    last_total_tokens: int = 0
    # How many times this conversation's history has been folded up. Updates
    # placed in an earlier epoch are not replayed: their indexes belong to a
    # window the provider no longer has.
    epoch: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def wire_model(self) -> str:
        return str(self.execution.get("wire_model") or "")

    @property
    def base_effort(self) -> str:
        return str(self.execution.get("base_effort") or self.execution.get("initial_effort") or "")

    @property
    def short(self) -> str:
        return self.conversation_id[:8]


def session_id_for(conversation_id: str) -> str:
    """A router session id for a client conversation id.

    Derived, so that an adapter restart can resume the same binding with no
    state on disk, and derived from the client's own identifier rather than
    from anything anybody said in the conversation. It is not the client's id
    verbatim: the router's id is ours to choose, and a hash keeps a client id
    that happens to be short, long or oddly shaped out of the router's
    namespace.
    """
    digest = hashlib.sha256(f"jev-router-adapter:{conversation_id}".encode()).hexdigest()
    return f"session-{digest[:24]}"


def conversation_id_of(headers: dict[str, str], body: dict[str, Any]) -> str:
    """The client's own stable conversation identifier, or nothing.

    Headers first, then the body. Never message text: an adapter that hashed
    the transcript would give one conversation two identities the moment
    anything in it was edited, and two conversations one identity whenever
    they started the same way.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    for name in CONVERSATION_HEADERS:
        value = lower.get(name.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    for name in CONVERSATION_BODY_FIELDS:
        value = body.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# --- reading the client's transcript -------------------------------------


def item_key(item: Any) -> str:
    """An identity for one input item, for telling turns apart.

    A client that gives its items ids (Codex does) is taken at its word. One
    that does not gets a hash of the item, which is still not message text
    leaving this process: it is compared with the previous request's and then
    forgotten.
    """
    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
        return f"id:{item['id']}"
    return "sha256:" + hashlib.sha256(P.canonical(item)).hexdigest()[:32]


def item_text(item: Any) -> str:
    """The plain text of one message item, for the turn's evidence."""
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def codex_compaction(headers: dict[str, str]) -> bool:
    """Whether Codex says this request is its own compaction.

    Read from `x-codex-turn-metadata`, which is identifiers and flags, never
    message text. Codex 0.155.1 sets `request_kind` to `"compaction"` and adds
    a `compaction` block naming the trigger and the reason; everything else it
    sends is a `"turn"`. This hop reads those two field names and nothing
    else, and an unreadable header simply means "an ordinary turn".
    """
    raw = {k.lower(): v for k, v in headers.items()}.get(CODEX_TURN_METADATA, "")
    if not raw:
        return False
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    return str(meta.get("request_kind") or "") == CODEX_COMPACTION_KIND


def pending_tool_calls(items: list[Any]) -> int:
    """Tool calls in this history that have no result yet.

    The router cannot see a client's tool loop and says so; this is the
    adapter's honest reading of the transcript it was handed, and it is the
    only thing it reports as a settled fact about the previous turn.
    """
    called: list[str] = []
    answered: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        call_id = str(item.get("call_id") or item.get("id") or "")
        if kind.endswith("_call_output"):
            answered.add(call_id)
        elif kind.endswith("_call") or kind == "function_call":
            called.append(call_id)
    return len([c for c in called if c not in answered])


# --- the adapter ---------------------------------------------------------


class Adapter:
    """One adapter process. Its state is the conversations it has seen."""

    def __init__(
        self,
        *,
        router_url: str = DEFAULT_ROUTER,
        alias: str,
        client_name: str = DEFAULT_CLIENT,
        admin_token: str = "",
        upstream_key: str = "",
        placeholder_token: str = "placeholder",
        timeout: float = 600.0,
        http: httpx.AsyncClient | None = None,
        trace: Any = None,
    ) -> None:
        self.router_url = router_url.rstrip("/")
        self.alias = alias
        self.client_name = client_name
        self.admin_token = admin_token
        self.upstream_key = upstream_key
        self.placeholder_token = placeholder_token
        self.http = http or httpx.AsyncClient(base_url=self.router_url, timeout=timeout)
        self.conversations: dict[str, Conversation] = {}
        self._trace = trace or (lambda line: print(line, flush=True))

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- talking to the router -----------------------------------------

    def control_headers(self) -> dict[str, str]:
        return {"x-router-admin-token": self.admin_token} if self.admin_token else {}

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        return await self.http.post(path, json=payload, headers=self.control_headers())

    # --- binding --------------------------------------------------------

    async def bind(self, conv: Conversation, body: dict[str, Any]) -> None:
        """Resolve this conversation's binding, or pick the stored one back up.

        `resume` is tried first so that restarting this process rejoins the
        session it already has instead of asking for a second one. An unknown
        session is the only answer that leads to `new`; everything else the
        router says is passed back to the client untouched.
        """
        resumed = await self._post(
            "/router/resolve",
            {
                "schema_version": "1",
                "intent": "resume",
                "session_id": conv.session_id,
                "request_id": f"resume-{uuid.uuid4().hex[:12]}",
                "client": self.client_name,
            },
        )
        if resumed.status_code == 200:
            self._take_contract(conv, resumed.json())
            await self._restore_updates(conv)
            self.trace(
                f"conv={conv.short} resumed session={conv.session_id} "
                f"model={conv.wire_model} base={conv.base_effort} "
                f"effective={conv.effective_effort} "
                f"updates={len(conv.updates)} epoch={conv.epoch}"
            )
            return
        if not _is_unknown_session(resumed):
            raise RouterRefused(resumed)

        items = P.input_items(body)
        estimate = estimate_input(body)
        created = await self._post(
            "/router/resolve",
            {
                "schema_version": "1",
                "intent": "new",
                "session_id": conv.session_id,
                "request_id": f"resolve-{uuid.uuid4().hex[:12]}",
                "alias": self.alias,
                "client": self.client_name,
                "client_contract": {
                    "dynamic_metadata": True,
                    "protocols": [P.RESPONSES],
                    "fresh_execution_context": True,
                    "turn_boundary_reporting": True,
                },
                # What the router budgets against: the request's own input and
                # tools, as they arrived.
                "request": {"input": items, "tools": body.get("tools") or []},
                "limits": {
                    "estimated_input_tokens": estimate.tokens,
                    "estimate_method": "adapter-serialized-v1",
                },
            },
        )
        if created.status_code != 200:
            raise RouterRefused(created)
        self._take_contract(conv, created.json())
        self.trace(
            f"conv={conv.short} resolved session={conv.session_id} "
            f"model={conv.wire_model} base={conv.base_effort} "
            f"adaptation={conv.execution.get('adaptation')}"
        )

    def _take_contract(self, conv: Conversation, contract: dict[str, Any]) -> None:
        conv.binding = str(contract.get("binding_revision") or "")
        conv.execution = dict(contract.get("execution") or {})
        conv.effective_effort = str(
            conv.execution.get("effective_effort") or conv.base_effort
        )

    async def _restore_updates(self, conv: Conversation) -> None:
        """Rebuild the update ledger from the router's own record.

        After a restart this hop knows nothing about where it put the items it
        inserted, and a full-history replay that dropped one would be refused.
        The router wrote down the position and the effort of every update it
        accepted, so they are read back from there. An update the router could
        not place either — a plan somebody reconciled by hand — cannot be
        restored, and the trace says so rather than guessing a position.
        """
        report = await self.http.get(
            f"/router/sessions/{conv.session_id}", headers=self.control_headers()
        )
        if report.status_code != 200:
            return
        row = report.json()
        plans = row.get("turn_plans") or []
        conv.epoch = int(row.get("compaction_epoch") or 0)
        restored: list[Update] = []
        unplaced = 0
        for plan in sorted(plans, key=lambda p: p.get("sequence") or 0):
            if plan.get("action") != "change_effort":
                continue
            if plan.get("status") not in ("accepted", "confirmed"):
                continue
            if int(plan.get("compaction_epoch") or 0) < conv.epoch:
                # Placed before a compaction. The window it was anchored in
                # has been folded up, so there is no index to put it back at
                # and nothing is expecting it.
                continue
            at = plan.get("anchor_position")
            effort = str(plan.get("to_effort") or "")
            if at is None or not effort:
                unplaced += 1
                continue
            restored.append(
                Update(
                    position=int(at),
                    effort=effort,
                    item=P.configuration_update(effort),
                    plan_id=str(plan.get("plan_id") or ""),
                    turn_id=str(plan.get("turn_id") or ""),
                )
            )
        conv.updates = sorted(restored, key=lambda u: u.position)
        conv.turns = max((int(p.get("sequence") or 0) for p in plans), default=0)
        conv.last_turn_id = str(plans[-1].get("turn_id")) if plans else ""
        if unplaced:
            self.trace(
                f"conv={conv.short} WARNING {unplaced} earlier update(s) have no "
                "recorded position; a full-history replay will be refused"
            )

    # --- one request ----------------------------------------------------

    async def handle(self, request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise AdapterError(
                "ADAPTER_INVALID_REQUEST", "the request body must be a JSON object"
            )
        conversation_id = conversation_id_of(dict(request.headers), body)
        if not conversation_id:
            raise AdapterError(
                "ADAPTER_NO_CONVERSATION_ID",
                "this request carries no stable conversation identifier. The "
                "adapter needs one of the headers "
                f"{', '.join(CONVERSATION_HEADERS[:4])} or a body field "
                f"{', '.join(CONVERSATION_BODY_FIELDS[:2])}. It will not derive "
                "an identity from message text: that would give one "
                "conversation a new binding whenever its history was edited.",
            )
        conv = self.conversations.get(conversation_id)
        if conv is None:
            conv = self.conversations[conversation_id] = Conversation(
                conversation_id=conversation_id,
                session_id=session_id_for(conversation_id),
            )
        async with conv.lock:
            return await self._turn(request, conv, body)

    async def _turn(
        self, request: Request, conv: Conversation, body: dict[str, Any]
    ) -> Response:
        if not conv.binding:
            await self.bind(conv, body)

        items = P.input_items(body)
        at = P.last_user_position(items)
        # Codex's own compaction is an ordinary Responses request whose last
        # item is a summarising user message it never keeps, so without this
        # it would look exactly like a new user turn: a classifier call would
        # be spent on it and an effort update would be inserted into a request
        # that exists only to be thrown away.
        compacting = P.compaction_request(body) or (
            P.COMPACTION_KIND if codex_compaction(dict(request.headers)) else ""
        )
        fresh_user = (
            not compacting
            and at >= 0
            and at == len(items) - 1
            and item_key(items[at]) != conv.last_user_key
        )
        # Before anything is decided or paid for: can every update this hop has
        # already placed still go back where it belongs? If the client has
        # rewritten its history, say so now rather than after a classifier call
        # has been spent and a plan is waiting on a request that cannot be made.
        self._check_placeable(conv, items)

        if fresh_user:
            conv.turns += 1
            turn_id = f"turn-{conv.turns:04d}"
            plan = await self.plan_turn(conv, turn_id, items, body)
            conv.last_turn_id = turn_id
            conv.last_user_key = item_key(items[at])
            conv.pending_plan = plan
        elif compacting:
            # Maintenance, not a turn: no plan, no item, no turn id. It still
            # replays every update of this epoch, because it carries the
            # history as it stands right now.
            turn_id = ""
            plan = {}
        else:
            # A tool continuation, or this turn's request being sent again
            # after a refusal. Either way it is the same turn: the plan it
            # already has, no second plan call, and a fresh item only if the
            # first attempt never reached the provider.
            turn_id = conv.last_turn_id
            plan = dict(conv.pending_plan)

        forwarded, pending = self.compose(conv, body, plan)
        request_id = f"adapter-{conv.turns:04d}-{conv.requests:04d}-{uuid.uuid4().hex[:8]}"
        conv.requests += 1
        headers = self.forward_headers(
            request, conv, request_id, turn_id, plan, maintenance=compacting
        )
        action = str(
            plan.get("action")
            or (compacting and "compaction")
            or ("continuation" if not fresh_user else "-")
        )
        # What this request is expected to run at. The router's ledger is the
        # authority on what it did run at; this is the trace, not the record.
        effective = str(plan.get("next_effective_effort") or conv.effective_effort)
        self.trace(
            f"conv={conv.short} turn={turn_id or '-'} action={action} "
            f"base={conv.base_effort} effective={effective} "
            f"plan={plan.get('plan_id') or '-'} req={request_id} "
            f"items={len(P.input_items(forwarded))} updates={len(conv.updates)}"
        )
        return await self.forward(
            conv,
            forwarded,
            headers,
            pending,
            turn_id,
            expected=effective,
            compacting=bool(compacting),
        )

    def _check_placeable(self, conv: Conversation, items: list[Any]) -> None:
        """Every earlier update still has an index in this history to go to.

        A client that compacted, forked or trimmed its own transcript has
        taken away the positions the router's ledger anchored those updates
        at. This hop will not guess a new one: it says what happened and the
        session keeps its binding.
        """
        grown = len(items)
        for update in sorted(conv.updates, key=lambda u: u.position):
            if update.position > grown:
                raise AdapterError(
                    "ADAPTER_HISTORY_SHRANK",
                    f"the effort update for turn {update.turn_id!r} belongs at "
                    f"position {update.position} and this request carries "
                    f"{grown} items; the client has rewritten or compacted a "
                    "history the router's ledger has to be able to find again",
                    status=409,
                )
            grown += 1

    # --- the turn boundary ----------------------------------------------

    async def plan_turn(
        self,
        conv: Conversation,
        turn_id: str,
        items: list[Any],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Ask the router what this turn's effort should be.

        Everything reported here is something this hop actually knows: it saw
        the previous response stream reach a terminal event, and it can count
        the tool calls in the history it was handed. Nothing else is asserted.

        When it did not see the last reply finish it says so, and goes on
        saying so until the router's own record says that turn is no longer
        outstanding. It never decides that for itself and it never waits it
        out: see `_router_settled`.
        """
        waiting = pending_tool_calls(items)
        settled = conv.settled
        if not settled and waiting == 0 and conv.unfinished_turn:
            settled = await self._router_settled(conv)
        previous = {
            "id": conv.last_turn_id,
            "settled": bool(settled and waiting == 0),
            "pending_tool_calls": waiting,
            "active_requests": 0,
        }
        estimate = estimate_input(body)
        answer = await self._post(
            "/router/turn-plan",
            {
                "schema_version": "1",
                "session_id": conv.session_id,
                "binding_revision": conv.binding,
                "turn_id": turn_id,
                "request_id": f"plan-{turn_id}-{uuid.uuid4().hex[:8]}",
                "previous_turn": previous,
                "state": {
                    "task_request": "",
                    "current_user_request": item_text(items[-1])[:TURN_TEXT_CHARS],
                    "user_constraints": [],
                    "quoted_material": [],
                    # An HTTP shim cannot tell a failing test from a passing
                    # one, and a guess promoted to a harness observation is
                    # exactly what the router's own rules refuse. So: none.
                    "observations": [],
                },
                "context": {
                    "estimated_total_tokens": max(estimate.tokens, conv.last_total_tokens),
                    "estimate_method": "adapter-serialized-v1",
                    "history_mode": "previous_response_id"
                    if body.get("previous_response_id")
                    else "full_history",
                },
            },
        )
        if answer.status_code != 200:
            raise RouterRefused(answer)
        return answer.json()

    async def _router_settled(self, conv: Conversation) -> bool:
        """Ask the router whether the turn it did not see finish is outstanding.

        A reply that stops half way through leaves this hop with one thing it
        cannot find out: whether the provider ran that turn. It will not
        pretend to know, so it never calls such a turn settled on its own
        account, and it never simply forgets that it happened. What it can do
        is ask the side that keeps the ledger.

        The router records an interrupted attempt as an ambiguous one and
        holds the next effort change until somebody who can ask the provider
        says what became of it. While it is holding one, the turn stays
        unsettled here too and the trace says exactly what closes it. Once the
        router's record is clear - because nothing was in the air, or because
        somebody reconciled what was - the turn is over on both sides, and
        what this hop then reports is the router's record, not a guess of its
        own. A router that cannot be read says nothing, which leaves the turn
        unsettled.
        """
        turn = conv.unfinished_turn
        try:
            report = await self.http.get(
                f"/router/sessions/{conv.session_id}", headers=self.control_headers()
            )
            row = report.json() if report.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            row = {}
        if not row:
            self.trace(
                f"conv={conv.short} turn={turn or '-'} WARNING its reply never "
                "reached a terminal event and the router's record could not be "
                "read; reporting the turn unsettled"
            )
            return False
        if int(row.get("in_flight") or 0):
            self.trace(
                f"conv={conv.short} turn={turn or '-'} the router is still "
                "executing this session; reporting the turn unsettled"
            )
            return False
        update = row.get("unsettled_update") or {}
        if update:
            plan_id = update.get("plan_id")
            self.trace(
                f"conv={conv.short} turn={turn or '-'} WARNING its reply never "
                f"reached a terminal event and the router still holds plan "
                f"{plan_id} ({update.get('status')}) for turn "
                f"{update.get('turn_id')}. Settle it with `jev-router sessions "
                f"reconcile {conv.session_id} --plan {plan_id} --outcome "
                "applied|not_applied`, or POST "
                f"/router/turn-plan/{plan_id}/reconcile, and send this turn again"
            )
            return False
        self.trace(
            f"conv={conv.short} turn={turn or '-'} its reply never reached a "
            "terminal event; the router's record has nothing outstanding for "
            "this session, so the turn is over on both sides"
        )
        conv.unfinished_turn = ""
        conv.settled = True
        return True

    # --- the body this hop sends ----------------------------------------

    def compose(
        self, conv: Conversation, body: dict[str, Any], plan: dict[str, Any]
    ) -> tuple[dict[str, Any], Update | None]:
        """The forwarded body: the client's, with three things settled.

        The model is the bound profile. The request-level effort is the base
        effort and stays there for the session's whole life, because the reply
        reports that field back and moving it would make the base and the
        effective setting impossible to tell apart. And the `input` carries
        every update this hop has inserted, each one back at the index the
        router recorded its anchor at.
        """
        out = dict(body)
        if conv.wire_model:
            out["model"] = conv.wire_model
        if conv.base_effort:
            reasoning = dict(out.get("reasoning") or {})
            reasoning["effort"] = conv.base_effort
            out["reasoning"] = reasoning

        items = P.input_items(body)
        for update in sorted(conv.updates, key=lambda u: u.position):
            items.insert(update.position, update.item)

        pending: Update | None = None
        update = plan.get("update") if plan.get("action") == "change_effort" else None
        if isinstance(update, dict) and "parameter" in update:
            # The other tested strategy: the same model, one request field.
            # There is no item and nothing to replay.
            if update.get("parameter") == "reasoning.effort":
                reasoning = dict(out.get("reasoning") or {})
                reasoning["effort"] = str(update.get("value") or "")
                out["reasoning"] = reasoning
        elif isinstance(update, dict) and isinstance(update.get("item"), dict):
            at = P.last_user_position(items)
            if at < 0:
                raise AdapterError(
                    "ADAPTER_NO_USER_MESSAGE",
                    "this turn's plan asks for an effort update and the request "
                    "carries no user message to put it before",
                    status=409,
                )
            # Exactly the item the router returned, exactly where it says.
            # This hop never writes one of its own.
            item = update["item"]
            items.insert(at, item)
            pending = Update(
                position=at,
                effort=str(plan.get("next_effective_effort") or ""),
                item=item,
                plan_id=str(plan.get("plan_id") or ""),
                turn_id=str(plan.get("turn_id") or ""),
            )
        out["input"] = items
        return out, pending

    def forward_headers(
        self,
        request: Request,
        conv: Conversation,
        request_id: str,
        turn_id: str,
        plan: dict[str, Any],
        maintenance: str = "",
    ) -> dict[str, str]:
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in DROP_REQUEST_HEADERS
        }
        headers["content-type"] = "application/json"
        # The observer needs to read the event stream, so this hop asks for it
        # unencoded. The bytes it hands back are still exactly the bytes it was
        # given.
        headers["accept-encoding"] = "identity"
        headers.update(self.control_headers())
        headers["x-router-session"] = conv.session_id
        headers["x-router-binding"] = conv.binding
        headers["x-router-request-id"] = request_id
        if turn_id:
            headers["x-router-turn"] = turn_id
        if plan.get("plan_id"):
            headers["x-router-turn-plan"] = str(plan["plan_id"])
        if maintenance:
            # Codex's compaction looks like an ordinary turn on the wire, so
            # the router is told what it is. This hop is the only thing in the
            # chain that can read Codex's metadata header, and reading the
            # client's own dialect is exactly what it is for.
            headers[P.REQUEST_KIND_HEADER] = maintenance
        authorization = self.authorization(request)
        if authorization:
            headers["authorization"] = authorization
        else:
            headers.pop("authorization", None)
        return headers

    def authorization(self, request: Request) -> str:
        """The client's credential, forwarded unchanged.

        The one exception is a client that has no real key to send and was
        configured with a placeholder, which is the ordinary case for a local
        CLI pointed at a proxy that holds the account. Then, and only then,
        the configured key is put in its place. The value is never logged,
        never stored and never written anywhere.
        """
        sent = request.headers.get("authorization", "")
        if not self.upstream_key:
            return sent
        token = sent.split(" ", 1)[1].strip() if " " in sent else sent.strip()
        if not sent or token == self.placeholder_token:
            return f"Bearer {self.upstream_key}"
        return sent

    # --- forwarding -----------------------------------------------------

    async def forward(
        self,
        conv: Conversation,
        body: dict[str, Any],
        headers: dict[str, str],
        pending: Update | None,
        turn_id: str,
        expected: str = "",
        compacting: bool = False,
    ) -> Response:
        content = json.dumps(body).encode()
        started = time.perf_counter()
        upstream = self.http.build_request(
            "POST", "/v1/responses", content=content, headers=headers
        )
        try:
            response = await self.http.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            conv.settled = False
            raise AdapterError(
                "ADAPTER_ROUTER_UNREACHABLE",
                f"the router could not be reached: {exc}",
                status=502,
            ) from None

        if response.status_code // 100 != 2:
            payload = await response.aread()
            await response.aclose()
            # The router refused before a byte was sent. The item this request
            # was going to carry is not remembered: the plan stays retryable
            # and the ledger records no update that is not in a transcript.
            self.trace(
                f"conv={conv.short} turn={turn_id or '-'} refused "
                f"status={response.status_code}"
            )
            return Response(
                payload,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )

        # 2xx headers. The router has recorded the anchor for this update, so
        # this hop records the same position and replays the item there from
        # now on.
        if pending is not None:
            conv.updates.append(pending)
            conv.updates.sort(key=lambda u: u.position)
        conv.pending_plan = {}
        conv.settled = False
        conv.unfinished_turn = turn_id or conv.last_turn_id
        # The router says what effort it forwarded this at. Its word, not this
        # hop's arithmetic, and the reply's own `reasoning.effort` is the base
        # effort and is never read as this.
        conv.effective_effort = (
            response.headers.get("x-router-effective-effort") or expected or conv.effective_effort
        )
        if compacting:
            # The provider has been asked to fold this history up. Whatever
            # the client sends next is a new window: the indexes this hop
            # recorded its items at name nothing in it, so it stops replaying
            # them. The router's ledger keeps the rows; this is a client
            # forgetting where it put something that no longer exists.
            dropped = len(conv.updates)
            conv.updates = []
            conv.epoch = int(
                response.headers.get("x-router-compaction-epoch") or conv.epoch + 1
            )
            self.trace(
                f"conv={conv.short} compacted epoch={conv.epoch} "
                f"dropped={dropped} effective={conv.effective_effort}"
            )

        observer = P.Observer(
            sse="text/event-stream" in response.headers.get("content-type", "")
        )
        out_headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in DROP_RESPONSE_HEADERS
        }

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    observer.feed(chunk)
                    yield chunk  # exactly the bytes the router sent
            finally:
                # Read what the tap saw first. It is what records an
                # unfinished turn, and closing a connection must not be able
                # to come between the reply ending and that being written down.
                self._settle(conv, observer, turn_id, started)
                await response.aclose()

        return ClosingStream(
            stream(),
            status_code=response.status_code,
            headers=out_headers,
            media_type=response.headers.get("content-type"),
        )

    def _settle(
        self, conv: Conversation, observer: P.Observer, turn_id: str, started: float
    ) -> None:
        """Read what the tap saw, and say so in one line. Counts only."""
        seen = observer.finish()
        conv.settled = seen.completed
        # A reply that stopped before the provider said how it ended. The turn
        # it belongs to is remembered so the next one can ask the router what
        # became of it rather than assume either answer.
        conv.unfinished_turn = "" if seen.completed else (turn_id or conv.last_turn_id)
        total = seen.usage.get("total_tokens")
        if isinstance(total, (int, float)):
            conv.last_total_tokens = int(total)
        self.trace(
            f"conv={conv.short} turn={turn_id or '-'} status={seen.status or 'unknown'} "
            f"response={seen.response_id or '-'} "
            f"reasoning_tokens={seen.usage.get('reasoning_tokens', '-')} "
            f"input_tokens={seen.usage.get('input_tokens', '-')} "
            f"cached_input_tokens={seen.usage.get('cached_input_tokens', '-')} "
            f"output_tokens={seen.usage.get('output_tokens', '-')} "
            f"ms={int((time.perf_counter() - started) * 1000)}"
        )

    # --- anything else --------------------------------------------------

    async def passthrough(self, request: Request) -> Response:
        """Every other path, forwarded to the router as it arrived."""
        raw = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS
        }
        headers.update(self.control_headers())
        authorization = self.authorization(request)
        if authorization:
            headers["authorization"] = authorization
        upstream = self.http.build_request(
            request.method,
            request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            content=raw or None,
            headers=headers,
        )
        try:
            response = await self.http.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            raise AdapterError(
                "ADAPTER_ROUTER_UNREACHABLE",
                f"the router could not be reached: {exc}",
                status=502,
            ) from None
        payload = await response.aread()
        await response.aclose()
        return Response(
            payload,
            status_code=response.status_code,
            headers={
                k: v
                for k, v in response.headers.items()
                if k.lower() not in DROP_RESPONSE_HEADERS
            },
        )

    def trace(self, line: str) -> None:
        self._trace(f"[adapter] {line}")


def _is_unknown_session(response: httpx.Response) -> bool:
    """Only `SESSION_UNKNOWN` means "resolve a new one"."""
    if response.status_code != 409:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return (payload.get("error") or {}).get("code") == "SESSION_UNKNOWN"


def read_key_file(path: str) -> str:
    """The upstream credential, read from a file at run time.

    A file, so the value is never a command-line argument, never an
    environment variable this process passes on, and never anything that could
    end up in a config, a log or a commit.
    """
    value = Path(path).expanduser().read_text().strip()
    if not value:
        raise ValueError(f"{path} is empty")
    return value


def create_adapter_app(adapter: Adapter) -> Starlette:
    async def responses(request: Request) -> Response:
        return await adapter.handle(request)

    async def anything(request: Request) -> Response:
        return await adapter.passthrough(request)

    async def adapter_error(_request: Request, exc: Exception) -> Response:
        return exc.response()  # type: ignore[attr-defined]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await adapter.aclose()

    app = Starlette(
        routes=[
            Route("/v1/responses", responses, methods=["POST"]),
            Route(
                "/{path:path}",
                anything,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            ),
        ],
        lifespan=lifespan,
        exception_handlers={AdapterError: adapter_error, RouterRefused: adapter_error},
    )
    app.state.adapter = adapter
    return app
