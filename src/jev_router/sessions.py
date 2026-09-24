"""Strict session bindings, in the same sqlite file as the pins.

A legacy pin is a cache entry with a six hour TTL that the router may replace
when the conversation no longer fits it. A strict binding is a contract: the
model, the provider and the negotiated limits it names do not change, it does
not expire on the pin TTL, and a request that no longer fits it is refused
rather than sent somewhere else. The two live in separate tables and neither
reads the other.

Nothing recoverable is written here. A request fingerprint is a sha256 of the
canonical payload, the owner is a sha256 of the control credential, and no
message text, prompt or key is stored.

Three rules hold this together:

- Every write is conditional on the version the caller read. A loser rereads
  the winning binding instead of overwriting it.
- No database transaction is held across a network call. The row is written
  before the request goes out and updated after it comes back.
- One execution per session at a time, claimed in memory. A second concurrent
  request is a conflict, never a queue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import effort as E
from .config import SessionRoutingCfg
from .pins import Store, migrate

log = logging.getLogger("jev_router")

# --- the state machine ---------------------------------------------------

# absent -> prepared (a resolved contract, no inference yet)
#        -> active   (the first accepted upstream headers)
#        -> blocked  (the profile changed; the binding is kept, not replaced)
#        -> closed   (explicitly closed, expired, or pruned to a tombstone)
STATES = ("prepared", "active", "blocked", "closed")

# What one execution attempt is known to have done.
REQUEST_STATES = (
    "in_flight",  # claimed, nothing heard back yet
    "accepted",   # upstream returned 2xx headers, the body never finished
    "completed",  # the transport finished cleanly
    "rejected",   # a definitive refusal before acceptance; safe to retry
    "stream_failed",  # accepted, then the body broke
    "unknown",    # we cannot say whether the provider ran it
)
# Outcomes we are allowed to forget when the per-session history is full.
FORGETTABLE = ("completed", "rejected")


# --- the error vocabulary (spec 9.6) -------------------------------------

INVALID_ROUTER_INPUT = "INVALID_ROUTER_INPUT"
ROUTER_UNAUTHORIZED = "ROUTER_UNAUTHORIZED"
SESSION_CONFLICT = "SESSION_CONFLICT"
SESSION_UNKNOWN = "SESSION_UNKNOWN"
SESSION_CLOSED = "SESSION_CLOSED"
PROFILE_CHANGED = "PROFILE_CHANGED"
NO_SAFE_ADMISSION = "NO_SAFE_ADMISSION"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"
UNSUPPORTED_PROFILE = "UNSUPPORTED_PROFILE"
REQUEST_ALREADY_COMPLETED = "REQUEST_ALREADY_COMPLETED"
EXECUTION_OUTCOME_UNKNOWN = "EXECUTION_OUTCOME_UNKNOWN"
TURN_NOT_SETTLED = "TURN_NOT_SETTLED"
EFFORT_HISTORY_MISMATCH = "EFFORT_HISTORY_MISMATCH"

# The whole vocabulary and the status each code answers with. Two of these
# belong to the effort experiment and are unreachable today; they are here so
# that the codes are stable before anything starts returning them.
ERROR_STATUS: dict[str, int] = {
    INVALID_ROUTER_INPUT: 400,
    ROUTER_UNAUTHORIZED: 401,
    SESSION_CONFLICT: 409,
    SESSION_UNKNOWN: 409,
    SESSION_CLOSED: 410,
    PROFILE_CHANGED: 409,
    NO_SAFE_ADMISSION: 422,
    PROVIDER_UNAVAILABLE: 503,
    CONTEXT_BUDGET_EXCEEDED: 422,
    UNSUPPORTED_PROFILE: 422,
    REQUEST_ALREADY_COMPLETED: 409,
    EXECUTION_OUTCOME_UNKNOWN: 409,
    TURN_NOT_SETTLED: 409,
    EFFORT_HISTORY_MISMATCH: 409,
}


class SessionError(Exception):
    """A structured router error with a stable machine code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or ERROR_STATUS.get(code, 400)
        self.detail = detail or {}

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "type": "session_error",
            "code": self.code,
            "message": self.message,
        }
        error.update(self.detail)
        return {"error": error}


class StrictServiceBusy(RuntimeError):
    """Another strict service in this process already owns that database."""


# --- schema --------------------------------------------------------------

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    client TEXT,
    alias TEXT,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    tombstone INTEGER NOT NULL DEFAULT 0,
    model_key TEXT,
    provider TEXT,
    upstream_id TEXT,
    wire_model TEXT,
    protocol TEXT,
    context_window INTEGER,
    max_output_tokens INTEGER,
    supports_tools INTEGER,
    supports_vision INTEGER,
    compatibility_revision TEXT,
    binding_revision TEXT,
    last_input_tokens INTEGER,
    last_output_tokens INTEGER,
    base_effort TEXT,
    effective_effort TEXT,
    effort_mode TEXT,
    envelope_model TEXT,
    decision_id TEXT,
    decision_rule TEXT,
    decision_reason TEXT,
    decision_source TEXT,
    quality_lane TEXT,
    quota_changed_choice INTEGER,
    config_hash TEXT,
    state_builder TEXT,
    question_hash TEXT,
    jev_model TEXT,
    fingerprint TEXT,
    estimate_method TEXT,
    estimated_input_tokens INTEGER,
    reserve_tokens INTEGER,
    quota_status TEXT,
    blocked_reason TEXT,
    adaptation_mode TEXT,
    expected_effort TEXT,
    confirmed_effort TEXT,
    effort_lineage TEXT,
    last_response_id TEXT,
    created REAL NOT NULL,
    last_seen REAL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS sessions_state ON sessions (state);
CREATE TABLE IF NOT EXISTS turn_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    mode TEXT,
    action TEXT,
    status TEXT NOT NULL,
    from_effort TEXT,
    to_effort TEXT,
    base_effort TEXT,
    recommendation TEXT,
    expected_effort TEXT,
    confirmed_effort TEXT,
    history_mode TEXT,
    anchor_position INTEGER,
    anchor_prefix_hash TEXT,
    parent_response_id TEXT,
    response_id TEXT,
    request_fingerprint TEXT,
    request_id TEXT,
    decision_id TEXT,
    reason TEXT,
    facts TEXT,
    jev_ms REAL,
    plan_ms REAL,
    created REAL NOT NULL,
    updated REAL,
    UNIQUE (session_id, plan_id)
);
CREATE INDEX IF NOT EXISTS turn_plans_session ON turn_plans (session_id, id);
CREATE INDEX IF NOT EXISTS turn_plans_turn ON turn_plans (session_id, turn_id);
CREATE TABLE IF NOT EXISTS session_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL,
    decision_id TEXT,
    upstream_status INTEGER,
    UNIQUE (session_id, request_id)
);
CREATE INDEX IF NOT EXISTS session_requests_session ON session_requests (session_id, id);
"""

# One plan per session and turn, enforced by the database rather than by the
# order two requests happened to arrive in (F07, spec 9.1). It is created
# apart from the schema above because a database written before this release
# may already hold two plans for one turn. Refusing to open such a file would
# be worse than opening it without the index: the router keeps its in-process
# guard either way, logs what it found, and carries `unique_turn_plans` for
# anything that wants to ask.
TURN_PLAN_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS turn_plans_one_per_turn"
    " ON turn_plans (session_id, turn_id)"
)

# Columns this release adds to tables an older database already has. Same
# contract as `pins.ADDED_COLUMNS`: every one is nullable, nothing is ever
# dropped or rewritten, and a database written before strict sessions existed
# keeps working. The session tables themselves are created above, so they only
# appear here for the columns a later release adds to them.
ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "decisions": [
        ("event_type", "TEXT"),
        ("session_id", "TEXT"),
        ("turn_id", "TEXT"),
        ("request_id", "TEXT"),
        ("quality_lane", "TEXT"),
    ],
}

# What the semantic packet and the quality guard add to a decision row. A
# separate map for a separate release, so each one's migration list says
# exactly what that release added. Same contract as the map above: every
# column is nullable, nothing is dropped, and an older database keeps working.
#
# `shadow_answers` holds values only. No message text, no prompt, no key ever
# reaches any of these.
SEMANTIC_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "decisions": [
        ("packet_version", "TEXT"),
        ("shadow_packet_version", "TEXT"),
        ("shadow_answers", "TEXT"),
        ("action", "TEXT"),
        ("experiment_routes", "TEXT"),
        ("quota_snapshot", "TEXT"),
        ("quota_status", "TEXT"),
        ("counterfactual", "TEXT"),
        ("exclusions", "TEXT"),
        ("evidence", "TEXT"),
        ("qualification_ref", "TEXT"),
    ],
}

# What the between-turn effort experiment adds. `turn_plans` is created by the
# schema above, so only the columns this release adds to a table an older
# database already has appear here. Same contract again: nullable, additive,
# never dropped, and a database written before the experiment keeps working
# with NULL in all of them, which reads as "this session predates it".
EFFORT_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "sessions": [
        # The mode agreed at resolve. A session resolved before the feature
        # was enabled never gains it, so this is stored, not recomputed.
        ("adaptation_mode", "TEXT"),
        # Kept apart on purpose: expected is what the last plan asked for,
        # confirmed is what a provider completion actually carried.
        ("expected_effort", "TEXT"),
        ("confirmed_effort", "TEXT"),
        # Set when lineage or usage could not be read. Further transitions
        # stop until it is reconciled.
        ("effort_lineage", "TEXT"),
        # The newest response id this session is known to have accepted. A
        # `previous_response_id` chain is checked against it.
        ("last_response_id", "TEXT"),
    ],
}

# What this release adds. Same contract once more: nullable, additive, never
# dropped. `turn_plans.reconciled` records that a person, not an observation,
# said what became of an update; NULL means nobody ever had to, which is what
# every row written before this release means.
RECONCILE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "turn_plans": [("reconciled", "TEXT")],
    "sessions": [
        # What the last reply this session got reported it had used. Two
        # counts and nothing else: no text, no ids, no reply. They are what a
        # `previous_response_id` request is budgeted against, because the
        # history is on the provider's side and a short delta is not it.
        # NULL means nobody has measured it, which is not zero.
        ("last_input_tokens", "INTEGER"),
        ("last_output_tokens", "INTEGER"),
    ],
}

# What keeping Codex's own compaction working adds (owner decision,
# 2026-09-19). Nullable, additive, never dropped, like every list above.
# NULL reads as "this session has never compacted", which is what every row
# written before this release means.
COMPACTION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "sessions": [
        # How many times the provider has folded this session's history up.
        # Anchors recorded in an earlier epoch no longer name anything, so
        # they are not position-validated and not required.
        ("compaction_epoch", "INTEGER"),
        ("compacted_at", "REAL"),
        # `known` once a compaction completed, `unknown` when one was sent and
        # nothing came back. An unknown outcome stops further effort changes
        # the same way an unreadable reply does.
        ("compaction_state", "TEXT"),
    ],
    # The epoch a plan was made in. A plan from an earlier epoch stays in the
    # ledger exactly as it was; it is simply no longer something a replayed
    # history has to carry.
    "turn_plans": [("compaction_epoch", "INTEGER")],
}

# The decision-log events a session produces.
EVENT_ADMISSION = "admission"
EVENT_EXECUTION = "execution"
EVENT_EFFORT_PLAN = "effort_plan"


# --- hashes --------------------------------------------------------------


def canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode()


def fingerprint(payload: Any) -> str:
    """A stable hash of a request. Never the request."""
    return hashlib.sha256(canonical(payload)).hexdigest()


def owner_key(credential: str) -> str:
    """Who this session belongs to. The credential itself is never stored."""
    return hashlib.sha256(("v1" + credential).encode()).hexdigest()


# The fields the binding digest covers: immutable profile identity and the
# negotiated limits. Not quota, not timestamps, and not effort, which the
# separate experiment is allowed to move without replacing the binding.
DIGEST_FIELDS = (
    "model_key",
    "provider",
    "upstream_id",
    "protocol",
    "context_window",
    "max_output_tokens",
    "supports_tools",
    "supports_vision",
    "compatibility_revision",
)


def binding_digest(profile: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        canonical({k: profile.get(k) for k in DIGEST_FIELDS})
    ).hexdigest()


# --- the resolve contract ------------------------------------------------


class Strict(BaseModel):
    """Unknown keys are refused: a client that sends one is not talking to us."""

    model_config = ConfigDict(extra="forbid")


class ClientContract(Strict):
    dynamic_metadata: bool = True
    protocols: list[str] = Field(default_factory=lambda: ["openai-chat"])
    fresh_execution_context: bool = True
    turn_boundary_reporting: bool = False


class ResolveLimits(Strict):
    # What the client will send as its own ceiling. The router returns the
    # smaller of this and the deployment's, and never the other way round.
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    # The client's own count. It can raise the server's estimate, never lower it.
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    estimate_method: str = ""
    # A client may ask for more uncertainty reserve than the router's floor.
    reserve_tokens: int | None = Field(default=None, ge=0)
    # Reported accumulated usage, for a history the provider holds.
    accumulated_input_tokens: int | None = Field(default=None, ge=0)


class ResolveRequest(Strict):
    schema_version: Literal["1"] = "1"
    intent: Literal["new", "resume"]
    session_id: str = Field(min_length=8, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    alias: str = ""
    client: str = ""
    candidate_models: list[str] | None = None
    client_contract: ClientContract = Field(default_factory=ClientContract)
    request: dict[str, Any] | None = None
    limits: ResolveLimits = Field(default_factory=ResolveLimits)

    @model_validator(mode="after")
    def _intent_needs_its_fields(self) -> ResolveRequest:
        if self.intent == "new":
            if not self.alias:
                raise ValueError("a new session needs an 'alias'")
            if self.request is None:
                raise ValueError("a new session needs the first 'request'")
        return self

    def idempotency_fingerprint(self) -> str:
        """Everything that decides the binding, and nothing that does not.

        The request id is left out on purpose: retrying a resolve with a new
        id and the same payload must return the stored result rather than
        spend another Jev call.
        """
        return fingerprint(
            {
                "intent": self.intent,
                "alias": self.alias,
                "client": self.client,
                "candidate_models": self.candidate_models,
                "client_contract": self.client_contract.model_dump(),
                "limits": self.limits.model_dump(),
                "request": self.request,
            }
        )


def parse_resolve(payload: Any) -> ResolveRequest:
    if not isinstance(payload, dict):
        raise SessionError(INVALID_ROUTER_INPUT, "body must be a JSON object")
    try:
        return ResolveRequest.model_validate(payload)
    except ValidationError as exc:
        raise SessionError(INVALID_ROUTER_INPUT, _first_problem(exc)) from None


# --- the turn-plan contract (spec 10.4) ---------------------------------


class PreviousTurn(Strict):
    """What the client says about the turn that just ended.

    These are lifecycle facts only the harness can know. The router checks its
    own in-flight state as well and refuses on either, but it cannot see the
    client's tool loop, so this part is the client's word.
    """

    id: str = ""
    settled: bool = False
    pending_tool_calls: int = Field(default=0, ge=0)
    active_requests: int = Field(default=0, ge=0)


class TurnState(Strict):
    """The bounded semantic packet for one turn. Processed, never stored."""

    task_request: str = ""
    current_user_request: str = ""
    user_constraints: list[str] = Field(default_factory=list)
    quoted_material: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)


class TurnContext(Strict):
    estimated_total_tokens: int | None = Field(default=None, ge=0)
    estimate_method: str = ""
    history_mode: Literal["full_history", "previous_response_id"] = "full_history"


class TurnPlanRequest(Strict):
    schema_version: Literal["1"] = "1"
    session_id: str = Field(min_length=8, max_length=200)
    binding_revision: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    previous_turn: PreviousTurn = Field(default_factory=PreviousTurn)
    state: TurnState = Field(default_factory=TurnState)
    context: TurnContext = Field(default_factory=TurnContext)

    def evidence_fingerprint(self) -> str:
        """What decides the plan, and nothing that does not.

        The request id is left out, so a retry with a fresh id is a retry. The
        previous turn's reported lifecycle is left out too: it gates whether a
        plan may be made at all, and a client that settles a turn between two
        otherwise identical calls has not changed the evidence.
        """
        return fingerprint(
            {
                "turn_id": self.turn_id,
                "state": self.state.model_dump(),
                "context": self.context.model_dump(),
            }
        )


def parse_turn_plan(payload: Any) -> TurnPlanRequest:
    if not isinstance(payload, dict):
        raise SessionError(INVALID_ROUTER_INPUT, "body must be a JSON object")
    try:
        return TurnPlanRequest.model_validate(payload)
    except ValidationError as exc:
        raise SessionError(INVALID_ROUTER_INPUT, _first_problem(exc)) from None


class ReconcileRequest(Strict):
    """What a client says it found out about an ambiguous update."""

    schema_version: Literal["1"] = "1"
    outcome: Literal["applied", "not_applied"]
    note: str = Field(default="", max_length=500)


def parse_reconcile(payload: Any) -> ReconcileRequest:
    if not isinstance(payload, dict):
        raise SessionError(INVALID_ROUTER_INPUT, "body must be a JSON object")
    try:
        return ReconcileRequest.model_validate(payload)
    except ValidationError as exc:
        raise SessionError(INVALID_ROUTER_INPUT, _first_problem(exc)) from None


def _first_problem(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"]) or "(root)"
    return f"{loc}: {err['msg']}"


# --- the process-local startup guard -------------------------------------

# One process, one strict service per database. Two workers sharing a file
# would each hold their own in-memory execution claims, and the "one request
# in flight per session" rule would stop meaning anything. Multi-process
# coordination is out of scope for this revision, so this refuses the setup
# rather than pretending to support it.
_OPEN_STRICT: dict[str, int] = {}
_OPEN_LOCK = threading.Lock()


class Sessions:
    """The strict session store, on the connection `pins.Store` already owns."""

    def __init__(
        self,
        store: Store,
        cfg: SessionRoutingCfg | None = None,
        *,
        guard: bool = True,
    ) -> None:
        self.store = store
        self.cfg = cfg or SessionRoutingCfg()
        self.clock = time.time
        self._conn: sqlite3.Connection = store.conn
        self._lock = store.lock
        self._inflight: dict[str, int] = {}
        self._claims = threading.Lock()
        self._guarding = ""
        with self._lock:
            self._conn.executescript(SESSION_SCHEMA)
            self.migrated = migrate(self._conn, ADDED_COLUMNS)
            self.migrated_semantic = migrate(self._conn, SEMANTIC_COLUMNS)
            self.migrated_effort = migrate(self._conn, EFFORT_COLUMNS)
            self.migrated_reconcile = migrate(self._conn, RECONCILE_COLUMNS)
            self.migrated_compaction = migrate(self._conn, COMPACTION_COLUMNS)
            self.unique_turn_plans = self._take_turn_plan_index()
            self._conn.commit()
        if guard and self.cfg.enabled:
            self._take_guard()

    # --- lifecycle -----------------------------------------------------

    def _take_turn_plan_index(self) -> bool:
        """One plan per session and turn, if this database can hold that.

        An older database may already have two plans for one turn, written
        before anything stopped it. Those rows are not rewritten and not
        dropped: the index is simply not created, and this returns False so
        `check-config` and the logs can say so. The router carries on either
        way, and the in-process guard keeps doing its part.
        """
        try:
            self._conn.execute(TURN_PLAN_UNIQUE)
            return True
        except sqlite3.IntegrityError:
            duplicates = self._conn.execute(
                "SELECT COUNT(*) FROM (SELECT session_id, turn_id FROM turn_plans"
                " GROUP BY session_id, turn_id HAVING COUNT(*) > 1)"
            ).fetchone()
            log.warning(
                "this database already holds %s turn(s) with more than one plan, "
                "so the one-plan-per-turn index was not created; the rows are "
                "left exactly as they are",
                duplicates[0] if duplicates else "some",
            )
            return False

    def _take_guard(self) -> None:
        path = str(self.store.path)
        if path == ":memory:":
            return  # every in-memory connection is its own database
        with _OPEN_LOCK:
            holder = _OPEN_STRICT.get(path)
            if holder is not None and holder != id(self):
                raise StrictServiceBusy(
                    f"another strict session service in this process already has "
                    f"{path} open; run one service per database"
                )
            _OPEN_STRICT[path] = id(self)
        self._guarding = path

    def close(self) -> None:
        """Release the startup guard. The connection belongs to the Store."""
        if self._guarding:
            with _OPEN_LOCK:
                if _OPEN_STRICT.get(self._guarding) == id(self):
                    del _OPEN_STRICT[self._guarding]
            self._guarding = ""

    # --- execution claims ----------------------------------------------

    def claim(self, session_id: str) -> bool:
        """Take the in-flight slot, or report that somebody else has it."""
        with self._claims:
            held = self._inflight.get(session_id, 0)
            if held >= self.cfg.max_inflight_per_session:
                return False
            self._inflight[session_id] = held + 1
            return True

    def release(self, session_id: str) -> None:
        with self._claims:
            held = self._inflight.get(session_id, 0) - 1
            if held > 0:
                self._inflight[session_id] = held
            else:
                self._inflight.pop(session_id, None)

    def inflight(self, session_id: str) -> int:
        with self._claims:
            return self._inflight.get(session_id, 0)

    # --- reads ---------------------------------------------------------

    def get(self, session_id: str, owner: str = "") -> dict[str, Any] | None:
        """One session, tombstones included. An owner mismatch reads as absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        item = _session_row(row)
        if owner and item["owner"] != owner:
            return None
        return item

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY COALESCE(last_seen, created) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_session_row(row) for row in rows]

    def expired(self, row: dict[str, Any]) -> bool:
        """A prepared contract that was never used. Active ones never expire."""
        if row["state"] != "prepared":
            return False
        return self.clock() - (row["created"] or 0) > self.cfg.prepared_ttl_seconds

    # --- writes --------------------------------------------------------

    def create(self, binding: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert one prepared binding, or report the one that got there first.

        Returns (row, created). Two concurrent admissions of the same id both
        come back with the same row, and only one of them wrote it.
        """
        now = self.clock()
        fields = dict(binding)
        fields.update(
            {"state": "prepared", "version": 1, "created": now, "last_seen": now}
        )
        names = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO sessions ({names}) VALUES ({marks})",
                tuple(fields.values()),
            )
            self._conn.commit()
            created = cur.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (fields["session_id"],),
            ).fetchone()
        return _session_row(row), created

    def update(self, session_id: str, version: int, **fields: Any) -> bool:
        """A conditional write. False means somebody else moved the row first."""
        fields = {k: v for k, v in fields.items() if v is not None}
        fields["last_seen"] = self.clock()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE sessions SET {sets}, version = version + 1"
                " WHERE session_id = ? AND version = ?",
                (*fields.values(), session_id, version),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def activate(self, session_id: str, version: int) -> bool:
        return self.update(session_id, version, state="active")

    def block(self, session_id: str, version: int, reason: str) -> bool:
        """Stop the session and keep the binding. Nothing is replaced."""
        return self.update(session_id, version, state="blocked", blocked_reason=reason)

    def close_session(self, session_id: str, reason: str = "closed") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET state = 'closed', closed_at = ?, version = version + 1,"
                " blocked_reason = COALESCE(blocked_reason, ?) WHERE session_id = ?"
                " AND state != 'closed'",
                (self.clock(), reason, session_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def touch(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_seen = ? WHERE session_id = ?",
                (self.clock(), session_id),
            )
            self._conn.commit()

    # --- request records -----------------------------------------------

    def unresolved(self, session_id: str) -> int:
        """Attempts nobody can call settled. They block a clean reconciliation."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM session_requests WHERE session_id = ?"
                " AND status IN ('in_flight', 'accepted', 'stream_failed', 'unknown')",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def get_request(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_requests WHERE session_id = ? AND request_id = ?",
                (session_id, request_id),
            ).fetchone()
        return dict(row) if row else None

    def start_request(
        self, session_id: str, request_id: str, body_fingerprint: str
    ) -> dict[str, Any] | None:
        """Record one attempt, or return the record that already exists."""
        now = self.clock()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO session_requests"
                " (session_id, request_id, fingerprint, status, created, updated)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, request_id, body_fingerprint, "in_flight", now, now),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                row = self._conn.execute(
                    "SELECT * FROM session_requests WHERE session_id = ? AND request_id = ?",
                    (session_id, request_id),
                ).fetchone()
                return dict(row) if row else None
        self._trim_requests(session_id)
        return None

    def finish_request(
        self,
        session_id: str,
        request_id: str,
        status: str,
        *,
        upstream_status: int | None = None,
        decision_id: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE session_requests SET status = ?, updated = ?,"
                " upstream_status = COALESCE(?, upstream_status),"
                " decision_id = COALESCE(?, decision_id)"
                " WHERE session_id = ? AND request_id = ?",
                (
                    status,
                    self.clock(),
                    upstream_status,
                    decision_id,
                    session_id,
                    request_id,
                ),
            )
            self._conn.commit()

    def _trim_requests(self, session_id: str) -> None:
        """Bound the per-session history, keeping every unresolved outcome.

        A record whose outcome we cannot state is never dropped, so a missing
        id is never read as proof that the request did not execute.
        """
        marks = ", ".join("?" for _ in FORGETTABLE)
        with self._lock:
            self._conn.execute(
                "DELETE FROM session_requests WHERE session_id = ?"
                f" AND status IN ({marks}) AND id NOT IN ("
                "   SELECT id FROM session_requests WHERE session_id = ?"
                "   ORDER BY id DESC LIMIT ?)",
                (session_id, *FORGETTABLE, session_id, self.cfg.max_request_history),
            )
            self._conn.commit()

    # --- the effort update ledger (spec 10.6) ---------------------------

    def plans(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """This session's turn plans, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM turn_plans WHERE session_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [_plan_row(row) for row in reversed(rows)]

    def get_plan(
        self, session_id: str, plan_id: str = "", turn_id: str = ""
    ) -> dict[str, Any] | None:
        """One plan, by its own id or by the turn it belongs to."""
        if plan_id:
            sql, args = "plan_id = ?", (session_id, plan_id)
        elif turn_id:
            sql, args = "turn_id = ?", (session_id, turn_id)
        else:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM turn_plans WHERE session_id = ? AND {sql}"
                " ORDER BY id DESC LIMIT 1",
                args,
            ).fetchone()
        return _plan_row(row) if row else None

    def plan_by_id(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turn_plans WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        return _plan_row(row) if row else None

    def next_sequence(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM turn_plans WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0]) + 1 if row else 1

    def record_plan(self, session_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Write one plan row, or return the one that got to this turn first.

        Hashes and identifiers only, never a transcript. Two concurrent plans
        for one turn both come back with the same row and only one of them
        wrote it, the same way `create` settles two concurrent admissions. A
        caller that needs to know which it was compares the `plan_id` it
        offered with the `plan_id` it got back.
        """
        now = self.clock()
        fields = {"session_id": session_id, "created": now, "updated": now, **plan}
        if isinstance(fields.get("facts"), (dict, list)):
            fields["facts"] = json.dumps(fields["facts"], default=str)
        names = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO turn_plans ({names}) VALUES ({marks})",
                tuple(fields.values()),
            )
            self._conn.commit()
            created = cur.rowcount == 1
        if created:
            return self.get_plan(session_id, plan_id=str(plan["plan_id"])) or {}
        # Somebody else holds this turn. Read theirs, never write over it.
        return (
            self.get_plan(session_id, turn_id=str(plan["turn_id"]))
            or self.get_plan(session_id, plan_id=str(plan["plan_id"]))
            or {}
        )

    def update_plan(self, plan_id: str, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        fields["updated"] = self.clock()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE turn_plans SET {sets} WHERE plan_id = ?",
                (*fields.values(), plan_id),
            )
            self._conn.commit()

    def reconcile(
        self, plan: dict[str, Any], row: dict[str, Any], outcome: str
    ) -> dict[str, Any]:
        """Close an ambiguous update by hand, in one direction or the other.

        The router cannot find this out for itself: a disconnected response
        may well have run. Somebody who can check the provider says which it
        was, and only then does the ledger move again.

        `applied` confirms the transition that was already expected.
        `not_applied` puts the ledger back to the previous confirmed state and
        leaves the same plan retryable. Either way the lineage flag is cleared
        and a competing transition is unblocked.

        Only a plan the provider actually received may be spoken about, which
        is `accepted` or `outcome_unknown` (see `effort.RECONCILABLE`). Any
        other status is refused and nothing at all is written: a plan that was
        never submitted has no provider-side fact to report, and a settled one
        already has its answer.
        """
        if plan["status"] not in E.RECONCILABLE:
            raise SessionError(
                SESSION_CONFLICT,
                f"plan {plan['plan_id']} is {plan['status']!r}; only an update "
                f"the provider received ({' or '.join(E.RECONCILABLE)}) can be "
                "reconciled by hand, and nothing was changed",
                detail={
                    "plan_id": plan["plan_id"],
                    "turn_id": plan["turn_id"],
                    "status": plan["status"],
                },
            )
        status = "confirmed" if outcome == "applied" else "rejected"
        effort = plan["to_effort"] if outcome == "applied" else None
        self.update_plan(
            plan["plan_id"],
            status=status,
            confirmed_effort=effort,
            # Said by hand, not observed. A row that carries this and no
            # anchor is a reconciliation hole: see `effort.reconciliation_hole`.
            reconciled=outcome,
        )
        current = self.get(row["session_id"]) or row
        if plan["action"] == "change_effort":
            keep = (
                effort
                if outcome == "applied"
                else (current.get("confirmed_effort") or current["base_effort"])
            )
            self.update(
                row["session_id"],
                current["version"],
                effective_effort=keep,
                confirmed_effort=keep,
                expected_effort=keep,
            )
        after = self.get(row["session_id"]) or current
        if after.get("effort_lineage") == "unknown":
            self.update(row["session_id"], after["version"], effort_lineage="known")
            after = self.get(row["session_id"]) or after
        return {
            "schema_version": "1",
            "session_id": row["session_id"],
            "plan_id": plan["plan_id"],
            "turn_id": plan["turn_id"],
            "outcome": outcome,
            "was": plan["status"],
            "status": (self.plan_by_id(plan["plan_id"]) or plan)["status"],
            "base_effort": after.get("base_effort"),
            "effective_effort": after.get("effective_effort"),
            "confirmed_effort": after.get("confirmed_effort"),
            "effort_lineage": after.get("effort_lineage") or "known",
        }

    def applied_updates(
        self, session_id: str, epoch: int = 0
    ) -> list[dict[str, Any]]:
        """Every effort update this session's history is still supposed to carry.

        A change that reached the provider is part of the transcript whatever
        happened afterwards, so accepted, confirmed and ambiguous rows all
        count. A plan that was never sent, or definitively refused before
        acceptance, is not in the history and is not expected back.

        Neither is one from before a compaction. The provider folded that part
        of the history up, the position its anchor names no longer exists, and
        the client cannot put the item back where it was. Those rows stay in
        the ledger and are simply not asked for again.
        """
        return [
            row
            for row in self.plans(session_id, limit=500)
            if row.get("action") == "change_effort"
            and row.get("status") in ("accepted", "confirmed", "outcome_unknown")
            and int(row.get("compaction_epoch") or 0) >= epoch
        ]

    def compact(self, session_id: str, state: str = "known") -> int:
        """Open a new compaction epoch on this session, and say which one.

        Called when a compaction request has been accepted, because from that
        moment the router can no longer honestly require the old anchors. It
        only ever relaxes what a later request is checked against.
        """
        row = self.get(session_id)
        if row is None:
            return 0
        epoch = int(row.get("compaction_epoch") or 0) + 1
        self.update(
            session_id,
            row["version"],
            compaction_epoch=epoch,
            compacted_at=self.clock(),
            compaction_state=state,
        )
        return epoch

    # --- decision log ---------------------------------------------------

    def annotate_decision(
        self,
        row_id: int,
        *,
        event_type: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
        quality_lane: str | None = None,
    ) -> None:
        """Add the session facts to a decision row already written by the Store."""
        if not row_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE decisions SET event_type = ?,"
                " session_id = COALESCE(?, session_id), turn_id = COALESCE(?, turn_id),"
                " request_id = COALESCE(?, request_id),"
                " quality_lane = COALESCE(?, quality_lane) WHERE id = ?",
                (event_type, session_id, turn_id, request_id, quality_lane, row_id),
            )
            self._conn.commit()

    def annotate_semantics(
        self,
        row_id: int,
        *,
        packet_version: str | None = None,
        shadow_packet_version: str | None = None,
        shadow_answers: dict[str, Any] | None = None,
        action: str | None = None,
        experiment_routes: list[dict[str, Any]] | None = None,
        quota_snapshot: dict[str, Any] | None = None,
        quota_status: dict[str, str] | None = None,
        counterfactual: tuple[str, str | None] | None = None,
        exclusions: list[dict[str, Any]] | None = None,
        evidence: str | None = None,
        qualification_ref: str | None = None,
    ) -> None:
        """Add the packet and quality-guard facts to a decision row.

        Everything here is optional and nullable. A row written before this
        release, or by a path that has none of it, reads back as NULL, which
        is what "this decision predates the feature" means.
        """
        if not row_id:
            return
        counter = (
            f"{counterfactual[0]}({counterfactual[1] or '-'})"
            if counterfactual
            else None
        )
        fields: dict[str, Any] = {
            "packet_version": packet_version or None,
            "shadow_packet_version": shadow_packet_version or None,
            "shadow_answers": _dump(shadow_answers),
            "action": action or None,
            "experiment_routes": _dump(experiment_routes),
            "quota_snapshot": _dump(quota_snapshot),
            "quota_status": _dump(quota_status),
            "counterfactual": counter,
            "exclusions": _dump(exclusions),
            "evidence": evidence or None,
            "qualification_ref": qualification_ref or None,
        }
        sets = ", ".join(f"{name} = COALESCE(?, {name})" for name in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE decisions SET {sets} WHERE id = ?",
                (*fields.values(), row_id),
            )
            self._conn.commit()

    # --- retention ------------------------------------------------------

    def prune(self, older_than_seconds: float) -> dict[str, int]:
        """Explicit retention. A pruned session leaves a tombstone behind.

        The binding details go; the id, its owner and the fact that it once
        existed stay, so resuming it answers "closed" and never "new".
        """
        cutoff = self.clock() - older_than_seconds
        blanked = [
            "alias",
            "model_key",
            "provider",
            "upstream_id",
            "wire_model",
            "protocol",
            "context_window",
            "max_output_tokens",
            "supports_tools",
            "supports_vision",
            "compatibility_revision",
            "binding_revision",
            "base_effort",
            "effective_effort",
            "envelope_model",
            "decision_rule",
            "decision_reason",
            "quality_lane",
            "fingerprint",
            "estimate_method",
            "estimated_input_tokens",
            "reserve_tokens",
            "quota_status",
        ]
        sets = ", ".join(f"{name} = NULL" for name in blanked)
        with self._lock:
            sessions = self._conn.execute(
                f"UPDATE sessions SET state = 'closed', tombstone = 1, {sets},"
                " closed_at = COALESCE(closed_at, ?),"
                " blocked_reason = COALESCE(blocked_reason, 'pruned'),"
                " version = version + 1"
                " WHERE tombstone = 0 AND COALESCE(last_seen, created) < ?",
                (self.clock(), cutoff),
            ).rowcount
            requests = self._conn.execute(
                "DELETE FROM session_requests WHERE created < ?", (cutoff,)
            ).rowcount
            plans = self._conn.execute(
                "DELETE FROM turn_plans WHERE created < ?", (cutoff,)
            ).rowcount
            self._conn.commit()
        return {
            "sessions": sessions,
            "session_requests": requests,
            "turn_plans": plans,
        }


def _dump(value: Any) -> str | None:
    """JSON, or nothing at all. An empty value writes no column."""
    if not value:
        return None
    return json.dumps(value, default=str)


def _plan_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    if item.get("facts"):
        try:
            item["facts"] = json.loads(item["facts"])
        except (TypeError, ValueError):
            pass
    return item


def _session_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["tombstone"] = bool(item.get("tombstone"))
    for flag in ("supports_tools", "supports_vision"):
        if item.get(flag) is not None:
            item[flag] = bool(item[flag])
    if item.get("quota_status"):
        try:
            item["quota_status"] = json.loads(item["quota_status"])
        except (TypeError, ValueError):
            pass
    return item


# --- what a client is told ----------------------------------------------


def contract(row: dict[str, Any], quota_status: dict[str, str]) -> dict[str, Any]:
    """The resolve response: one fixed profile and what was decided."""
    return {
        "schema_version": "1",
        "session_id": row["session_id"],
        "state": row["state"],
        "binding_revision": row["binding_revision"],
        "decision_id": row["decision_id"],
        "execution": {
            "model_key": row["model_key"],
            "provider": row["provider"],
            "wire_model": row["wire_model"],
            "protocol": row["protocol"],
            "context_window": row["context_window"],
            "max_output_tokens": row["max_output_tokens"],
            "supports_tools": row["supports_tools"],
            "supports_vision": row["supports_vision"],
            "initial_effort": row["base_effort"],
            "effort_mode": row["effort_mode"] or "fixed",
            # Base effort is what every request carries. Effective effort is
            # what the session is actually running at, which only the
            # experiment can move and only between turns.
            "base_effort": row["base_effort"],
            "effective_effort": row["effective_effort"] or row["base_effort"],
            "adaptation": row.get("adaptation_mode") or "off",
            # How many times this session's history has been folded up. A
            # client that restarts needs it to know which of its recorded
            # update positions still mean anything.
            "compaction_epoch": int(row.get("compaction_epoch") or 0),
        },
        "decision": {
            "rule": row["decision_rule"],
            "source": row["decision_source"],
            "quality_lane": row["quality_lane"],
            "quota_changed_choice": bool(row.get("quota_changed_choice")),
            "quota_status": quota_status,
        },
        "limits": {
            "estimated_input_tokens": row["estimated_input_tokens"],
            "estimate_method": row["estimate_method"],
            "reserve_tokens": row["reserve_tokens"],
        },
    }
