"""The Responses wire contract for the effort experiment.

Not a universal converter. This validates one experimental path and observes
its replies; nothing here rewrites a request into another format, and nothing
here touches a streamed byte.

Two jobs:

- **Validation.** For a session running the native strategy, check before
  forwarding that the request-level base effort is untouched, that every
  update the ledger says is in the history is still at its original position,
  that the new one sits immediately before the designated user message, and
  that the client has not written an update of its own. The router never
  inserts the item; the client owns the transcript and the router owns the
  plan.
- **Observation.** A bounded read-only tap on the reply that reads the
  response id, the terminal status and the usage. It yields every chunk
  unchanged, never assembles output or reasoning text, and a parse failure
  marks lineage unknown rather than retrying or altering anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

RESPONSES = "openai-responses"
CHAT = "openai-chat"

UPDATE_TYPE = "configuration_update"

# The provider's own compaction items, measured against this deployment on
# 2026-09-19. A `compaction_trigger` is the client asking for the history so
# far to be folded up, and it has to be the last input item; the reply is one
# `compaction` item whose `encrypted_content` is opaque. The router never
# reads inside either of them and never writes one.
TRIGGER_TYPE = "compaction_trigger"
COMPACTION_TYPE = "compaction"

# Settings that would let the provider rewrite the history with no request of
# its own for the router to see. Still refused: the owner asked for Codex's
# native compaction, which is a request like any other, not for an automatic
# rewrite nobody observes (spec 10.3, and the owner decision of 2026-09-19).
#
# `compaction_trigger` is deliberately not here. It is the name of the
# supported flow, and it arrives as an input item, not as a request field.
COMPACTION_FIELDS = (
    "context_management",
    "compaction",
    "auto_compaction",
    "auto_truncation",
)

# What the client may call a request that is maintenance rather than a turn.
# A compaction is the only one so far.
REQUEST_KIND_HEADER = "x-router-request-kind"
COMPACTION_KIND = "compaction"

# The most of a reply the observer will hold while looking for one event
# boundary. Past this the partial is dropped and lineage is unknown.
OBSERVER_BUFFER_BYTES = 256 * 1024


def configuration_update(effort: str) -> dict[str, Any]:
    """The provider's own item. Not a prompt, not an instruction."""
    return {"type": UPDATE_TYPE, "reasoning": {"effort": effort}}


def is_update(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == UPDATE_TYPE


def update_effort(item: Any) -> str | None:
    if not is_update(item):
        return None
    reasoning = item.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    effort = reasoning.get("effort")
    return effort if isinstance(effort, str) else None


def is_trigger(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == TRIGGER_TYPE


def is_compaction(item: Any) -> bool:
    """One of the provider's folded-up histories. Opaque, and left alone."""
    return isinstance(item, dict) and item.get("type") == COMPACTION_TYPE


def last_compaction_position(items: list[Any]) -> int:
    for i in range(len(items) - 1, -1, -1):
        if is_compaction(items[i]):
            return i
    return -1


def is_user_message(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("role") == "user":
        return True
    return item.get("type") == "message" and item.get("role") == "user"


def canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode()


def prefix_hash(items: list[Any], upto: int) -> str:
    """A hash of everything before a position.

    Repeated identical user text is not a unique anchor, so an update's place
    in the history is identified by the whole prefix that leads to it, not by
    the message that follows it.
    """
    return hashlib.sha256(canonical(items[:upto])).hexdigest()


def input_items(body: dict[str, Any]) -> list[Any]:
    """The Responses `input` as a list. A bare string is one user message."""
    value = body.get("input")
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        return [{"type": "message", "role": "user", "content": value}]
    return []


def update_positions(items: list[Any]) -> list[int]:
    return [i for i, item in enumerate(items) if is_update(item)]


def last_user_position(items: list[Any]) -> int:
    for i in range(len(items) - 1, -1, -1):
        if is_user_message(items[i]):
            return i
    return -1


# --- validation ----------------------------------------------------------


@dataclass(frozen=True)
class HistoryCheck:
    """What a request's history says about the effort updates in it."""

    ok: bool
    reason: str = ""
    # Where the new update sits, and the hash of everything before it. Both
    # go on the ledger row so a later full replay can be checked against them.
    position: int | None = None
    prefix: str = ""
    matched: tuple[str, ...] = ()

    @classmethod
    def refused(cls, reason: str) -> HistoryCheck:
        return cls(ok=False, reason=reason)


def check_base_effort(body: dict[str, Any], base: str | None) -> str:
    """The request-level effort is the base effort and stays there.

    An update item moves the effective effort. Moving the request field as
    well would be a second owner for the same setting, and the reply reports
    that field back, so the two would become impossible to tell apart.
    """
    reasoning = body.get("reasoning")
    sent = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if sent is None:
        return "" if base is None else (
            f"the request must carry reasoning.effort {base!r}, the base effort "
            "this session was bound at"
        )
    if base is not None and sent != base:
        return (
            f"reasoning.effort is {sent!r} and the base effort is {base!r}; the "
            "base setting does not move, the update item does"
        )
    return ""


def check_compaction(body: dict[str, Any]) -> str:
    """Automatic compaction and truncation are refused for these sessions.

    What is refused is a rewrite with no request of its own: `truncation:
    auto` and the automatic-management fields. A compaction the client asks
    for is a request the router can see, record an epoch for and pass through,
    and that one is supported.
    """
    if str(body.get("truncation") or "") == "auto":
        return (
            "truncation 'auto' lets the provider drop history with no request "
            "of its own for the ledger to see; it is not supported while the "
            "between-turn experiment is active on this session. A compaction "
            "the client asks for is"
        )
    present = [name for name in COMPACTION_FIELDS if body.get(name) is not None]
    if present:
        return (
            f"{', '.join(present)} rewrites the history with no request of its "
            "own for the ledger to see and is not supported while the "
            "between-turn experiment is active on this session"
        )
    return ""


def compaction_request(body: dict[str, Any], headers: Any = None) -> str:
    """Whether this request is a compaction, and how we can tell.

    Two shapes, both measured against Codex 0.155.1 and this deployment on
    2026-09-19:

    - `provider_trigger`: the last input item is a `compaction_trigger`, which
      is the provider's own documented flow. The reply is a `compaction` item.
    - `client_declared`: the client said so in `X-Router-Request-Kind`. Codex's
      own compaction is an ordinary Responses request carrying a summarising
      prompt, so nothing in the body distinguishes it and the client's word is
      the only signal there is. It is the same kind of trusted-adapter
      assertion as a reported turn boundary (spec 10.4), and it can only ever
      relax what a later request is checked against, never tighten it.

    An empty string means this is an ordinary turn.
    """
    items = input_items(body)
    if items and is_trigger(items[-1]):
        return "provider_trigger"
    declared = ""
    if headers is not None:
        try:
            declared = str(headers.get(REQUEST_KIND_HEADER, "") or "").strip().lower()
        except Exception:  # noqa: BLE001 - a header mapping that cannot be read says nothing
            declared = ""
    if declared == COMPACTION_KIND:
        return "client_declared"
    return ""


def check_trigger_placement(body: dict[str, Any]) -> str:
    """A `compaction_trigger` has to be the final input item.

    The provider says so itself, in so many words, with a 400. Saying it here
    costs nothing and means a client learns it before a request is forwarded.
    """
    items = input_items(body)
    stray = [i for i, item in enumerate(items) if is_trigger(item)]
    if not stray or stray == [len(items) - 1]:
        return ""
    return (
        f"a {TRIGGER_TYPE!r} item is at position {stray[0]} of {len(items)}; "
        "the provider requires it to be the final input item"
    )


def validate_full_history(
    items: list[Any],
    ledger: list[dict[str, Any]],
    *,
    expected: str | None,
) -> HistoryCheck:
    """Every earlier update where it was, and at most one new one (F08, F09).

    `ledger` is the applied updates of the **current compaction epoch**,
    oldest first, each carrying the position and prefix hash it was accepted
    at. Updates from before a compaction are not in it: the provider folded
    that part of the history up and the positions they were anchored at no
    longer name anything. `expected` is the effort this turn's plan asks for,
    or None when the plan changes nothing.

    An update item sitting before a compaction item in the replayed history is
    neither required nor refused, for the same reason: it belongs to a window
    that has been folded up, and the router will not pretend to know what the
    provider kept of it.
    """
    folded = last_compaction_position(items)
    found = [at for at in update_positions(items) if at > folded]
    claimed: set[int] = set()
    after = -1  # the position the ledger has reached so far
    for row in ledger:
        at = row.get("anchor_position")
        if at is None:
            # Nobody recorded where this update went: it was confirmed by
            # hand after the request that carried it went quiet. Its position
            # and the history before it cannot be checked, and saying so is
            # not a reason to refuse every later request. What can honestly be
            # checked is checked: the replay still has to carry an update
            # asking for that effort, after the previous ledger row and not
            # already claimed by another one.
            at = _unanchored(items, found, claimed, after, row.get("to_effort"))
            if at is None:
                return HistoryCheck.refused(
                    f"the reconciled update for turn {row.get('turn_id')!r} set "
                    f"{row.get('to_effort')!r} and the replayed history carries "
                    "no such update after the one before it"
                )
            claimed.add(at)
            after = at
            continue
        if at >= len(items) or not is_update(items[at]):
            return HistoryCheck.refused(
                f"the update for turn {row.get('turn_id')!r} is missing from "
                f"position {at} of the replayed history"
            )
        if update_effort(items[at]) != row.get("to_effort"):
            return HistoryCheck.refused(
                f"the update at position {at} asks for "
                f"{update_effort(items[at])!r} and the ledger recorded "
                f"{row.get('to_effort')!r}"
            )
        if prefix_hash(items, at) != row.get("anchor_prefix_hash"):
            return HistoryCheck.refused(
                f"the history before position {at} is not the history the "
                f"update for turn {row.get('turn_id')!r} was accepted against"
            )
        claimed.add(at)
        after = max(after, at)

    fresh = [at for at in found if at not in claimed]
    matched = tuple(str(row.get("plan_id")) for row in ledger)

    if expected is None:
        if fresh:
            return HistoryCheck.refused(
                f"the request carries an effort update at position {fresh[0]} "
                "that no plan asked for; while the router manages adaptation it "
                "is the only owner of these items"
            )
        return HistoryCheck(ok=True, matched=matched)

    if len(fresh) != 1:
        if not fresh and any(at <= folded for at in update_positions(items)):
            # The client put the item in, but on the wrong side of the folded
            # history, which the provider refuses outright.
            return HistoryCheck.refused(
                f"this turn's effort update is before the compaction item at "
                f"position {folded}; it has to come after the history the "
                "provider has folded up, immediately before the new user "
                "message"
            )
        return HistoryCheck.refused(
            f"this turn's plan expects exactly one new effort update and the "
            f"request carries {len(fresh)}"
        )
    at = fresh[0]
    if update_effort(items[at]) != expected:
        return HistoryCheck.refused(
            f"the new update asks for {update_effort(items[at])!r} and the plan "
            f"asks for {expected!r}"
        )
    problem = _placement(items, at)
    if problem:
        return HistoryCheck.refused(problem)
    return HistoryCheck(
        ok=True, position=at, prefix=prefix_hash(items, at), matched=matched
    )


def validate_maintenance(
    items: list[Any], ledger: list[dict[str, Any]]
) -> HistoryCheck:
    """A request that is not a turn: checked for ownership, not for position.

    A client assembles a compaction request for itself, and codex-cli 0.155.1
    assembles it differently from a turn: it declares a different tool set, so
    the very first item of the history carries a different id and every prefix
    hash in front of every update changes with it. Measured on 2026-09-19. The
    positions the ledger recorded describe a turn's replay, not this request,
    so they are not checked here - checking them would refuse a compaction on
    the strength of a difference that means nothing.

    What is still checked is the thing that matters. The router is the only
    owner of these items, so the history has to carry exactly the updates the
    ledger knows about, asking for the same efforts in the same order, and
    nothing else. A client-authored update still cannot get through.
    """
    sent = [update_effort(items[at]) for at in update_positions(items)]
    want = [row.get("to_effort") for row in ledger]
    if sent != want:
        return HistoryCheck.refused(
            f"this maintenance request carries effort updates {sent} and the "
            f"ledger holds {want}; while the router manages adaptation it is "
            "the only owner of these items"
        )
    return HistoryCheck(
        ok=True, matched=tuple(str(row.get("plan_id")) for row in ledger)
    )


def _unanchored(
    items: list[Any],
    found: list[int],
    claimed: set[int],
    after: int,
    effort: str | None,
) -> int | None:
    """Where an update with no recorded position could be, or nothing.

    The earliest unclaimed update asking for that effort that comes after the
    ledger row before it. Earliest, because the least this can assume about a
    position nobody wrote down is the most conservative reading of the rest.
    """
    for at in found:
        if at in claimed or at <= after:
            continue
        if update_effort(items[at]) == effort:
            return at
    return None


def _placement(items: list[Any], at: int) -> str:
    """Immediately before the designated new user message, and not adjacent."""
    if at < last_compaction_position(items):
        # Measured on 2026-09-19: the provider answers an update placed before
        # a compaction item with "The 'configuration_update' item type is not
        # supported with compaction without a subsequent configuration
        # update." A new update belongs after the folded-up history, which is
        # also where "immediately before the new user message" puts it.
        return (
            f"the new effort update at position {at} is before the compaction "
            "item at position "
            f"{last_compaction_position(items)}; an update has to come after "
            "the history the provider has folded up"
        )
    if at + 1 >= len(items) or not is_user_message(items[at + 1]):
        return (
            f"the new effort update at position {at} is not immediately before a "
            "user message"
        )
    if at + 1 != last_user_position(items):
        return (
            "the new effort update is before an earlier user message, not before "
            "the new one this turn is about"
        )
    if at and is_update(items[at - 1]):
        return f"two effort updates are adjacent at positions {at - 1} and {at}"
    if at + 1 < len(items) and is_update(items[at + 1]):
        return f"two effort updates are adjacent at positions {at} and {at + 1}"
    return ""


def validate_chain(
    items: list[Any],
    ledger: list[dict[str, Any]],
    *,
    previous_response_id: str,
    expected: str | None,
    lineage: set[str],
) -> HistoryCheck:
    """A `previous_response_id` delta (F10).

    The parent has to be a response this session is known to have accepted,
    an update the provider already holds must not be sent again, and the new
    one appears exactly once.
    """
    if lineage and previous_response_id not in lineage:
        return HistoryCheck.refused(
            f"previous_response_id {previous_response_id!r} is not a response "
            "this session is known to have accepted; the parent lineage has to "
            "be one the ledger can see"
        )
    found = update_positions(items)
    # An update the provider already has is on its side of the chain. The
    # delta carries what is new, so anything in it that the provider is
    # already holding is a duplicate. An update somebody reconciled as applied
    # is one the provider has too, even though no response id was ever read
    # for it.
    persisted = [
        row
        for row in ledger
        if row.get("response_id") or row.get("reconciled") == "applied"
    ]

    if expected is None:
        if found:
            return HistoryCheck.refused(
                "the delta carries an effort update that no plan asked for; "
                "while the router manages adaptation it is the only owner of "
                "these items"
            )
        return HistoryCheck(ok=True)
    if len(found) > 1:
        return HistoryCheck.refused(
            f"the delta carries {len(found)} effort updates and the plan asks "
            "for one"
        )
    if not found:
        return HistoryCheck.refused(
            "this turn's plan asks for an effort update and the delta carries none"
        )
    at = found[0]
    sent = update_effort(items[at])
    if sent != expected:
        return HistoryCheck.refused(
            f"the new update asks for {sent!r} and the plan asks for {expected!r}"
        )
    if persisted and persisted[-1].get("to_effort") == sent:
        return HistoryCheck.refused(
            f"the provider already holds an update setting {sent!r}; a chain "
            "delta must not resend it"
        )
    problem = _placement(items, at)
    if problem:
        return HistoryCheck.refused(problem)
    return HistoryCheck(ok=True, position=at, prefix=prefix_hash(items, at))


# --- the bounded read-only observer (spec 10.7) --------------------------


@dataclass
class Observation:
    """What the tap could read. Unknown is unknown, never a default."""

    response_id: str = ""
    status: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    parse_failed: bool = False
    events: int = 0

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def known(self) -> bool:
        return bool(self.response_id) and bool(self.status) and not self.parse_failed

    def to_facts(self) -> dict[str, Any]:
        return {
            "response_id": self.response_id,
            "status": self.status,
            "usage": dict(self.usage),
            "parse_failed": self.parse_failed,
            "events": self.events,
        }


# The events worth parsing. Everything else is passed over without being read.
TERMINAL_EVENTS = (
    "response.completed",
    "response.failed",
    "response.incomplete",
    "response.cancelled",
)


class Observer:
    """A read-only tap on a Responses reply.

    It never returns bytes and never holds output or reasoning text. The
    caller yields the chunk it was given, unchanged, whatever this does with
    it. A parse failure sets `parse_failed` and stops the tap; it never
    raises, never retries and never alters the reply.
    """

    def __init__(self, sse: bool = True, limit: int = OBSERVER_BUFFER_BYTES) -> None:
        self.observation = Observation()
        self.sse = sse
        self.limit = limit
        self._buffer = bytearray()
        self._stopped = False

    def feed(self, chunk: bytes) -> None:
        if self._stopped or not chunk:
            return
        if not self.sse:
            # A non-streamed reply is one JSON document. Hold it, bounded,
            # and read it once the body is complete.
            if len(self._buffer) + len(chunk) > self.limit:
                self._buffer.clear()
                self.observation.parse_failed = True
                self._stopped = True
                return
            self._buffer.extend(chunk)
            return
        try:
            self._buffer.extend(chunk)
            while True:
                at = self._buffer.find(b"\n\n")
                if at < 0:
                    break
                block = bytes(self._buffer[:at])
                del self._buffer[: at + 2]
                self._read_event(block)
            if len(self._buffer) > self.limit:
                # One event larger than the whole buffer. Drop the partial
                # rather than grow: the reply is untouched and lineage for
                # this request is simply unknown.
                self._buffer.clear()
                self.observation.parse_failed = True
                self._stopped = True
        except Exception:  # noqa: BLE001 - observation may never break a reply
            self.observation.parse_failed = True
            self._stopped = True

    def feed_json(self, body: bytes) -> None:
        """A non-streamed reply, read the same way."""
        if self._stopped:
            return
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            self.observation.parse_failed = True
            self._stopped = True
            return
        if isinstance(payload, dict):
            self.observation.events += 1
            self._take(payload)

    def finish(self) -> Observation:
        if not self.sse:
            body = bytes(self._buffer)
            self._buffer.clear()
            if body:
                self.feed_json(body)
            if not self.observation.status:
                self.observation.parse_failed = True
            self._stopped = True
            return self.observation
        if not self.observation.status:
            # A reply that ended mid-event, or said nothing at all, never told
            # us how it ended. Unknown is unknown.
            self.observation.parse_failed = True
        self._buffer.clear()
        self._stopped = True
        return self.observation

    # --- parsing ------------------------------------------------------

    def _read_event(self, block: bytes) -> None:
        payload: dict[str, Any] | None = None
        kind = ""
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                kind = line[6:].strip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                raw = line[5:].strip()
                if raw in (b"", b"[DONE]"):
                    continue
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    self.observation.parse_failed = True
                    self._stopped = True
                    return
                if isinstance(parsed, dict):
                    payload = parsed
        if payload is None:
            return
        self.observation.events += 1
        kind = str(payload.get("type") or kind)
        if kind and kind not in TERMINAL_EVENTS and not kind.startswith("response.created"):
            return
        self._take(payload.get("response") if isinstance(payload.get("response"), dict) else payload)

    def _take(self, response: Any) -> None:
        """Four fields, by name. Nothing else is read out of the reply."""
        if not isinstance(response, dict):
            return
        if isinstance(response.get("id"), str):
            self.observation.response_id = response["id"]
        if isinstance(response.get("status"), str):
            self.observation.status = response["status"]
        usage = response.get("usage")
        if isinstance(usage, dict):
            self.observation.usage = _usage(usage)


def _usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Counts only, including the cached input tokens the cache work needs."""
    out: dict[str, Any] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        if isinstance(usage.get(name), (int, float)):
            out[name] = usage[name]
    details = usage.get("input_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
        out["cached_input_tokens"] = details["cached_tokens"]
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), (int, float)):
        out["reasoning_tokens"] = details["reasoning_tokens"]
    return out
