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
import sqlite3
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import SessionRoutingCfg
from .pins import Store, migrate

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
    fingerprint TEXT,
    estimate_method TEXT,
    estimated_input_tokens INTEGER,
    reserve_tokens INTEGER,
    quota_status TEXT,
    blocked_reason TEXT,
    created REAL NOT NULL,
    last_seen REAL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS sessions_state ON sessions (state);
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

# The decision-log events a session produces. `effort_plan` belongs to the
# effort experiment and nothing writes it yet.
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
            self._conn.commit()
        if guard and self.cfg.enabled:
            self._take_guard()

    # --- lifecycle -----------------------------------------------------

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
            self._conn.commit()
        return {"sessions": sessions, "session_requests": requests}


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
