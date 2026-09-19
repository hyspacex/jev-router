"""Pins and the decision log, in one sqlite file.

Nothing recoverable is stored: no message text, no API keys. The conversation
key is a hash, and the log keeps lengths, Jev answers and the chosen route.
The state sent to Jev is stored only when settings.log_state is true.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

VERDICTS = ("right", "too_weak", "too_strong", "too_slow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
    conversation_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    effort TEXT,
    created REAL NOT NULL,
    decision_id TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    ts REAL NOT NULL,
    alias TEXT,
    client TEXT,
    conversation_key TEXT,
    state_builder TEXT,
    answers TEXT,
    features TEXT,
    config_hash TEXT,
    model TEXT,
    effort TEXT,
    rule TEXT,
    mode TEXT,
    fallback INTEGER,
    pinned INTEGER,
    jev_ms REAL,
    jev_tokens INTEGER,
    est_tokens INTEGER,
    message_count INTEGER,
    upstream_status INTEGER,
    upstream_usage TEXT,
    state TEXT,
    route TEXT,
    intended_model TEXT,
    intended_effort TEXT,
    fallback_index INTEGER,
    fallback_reason TEXT,
    pressures TEXT,
    shifted TEXT,
    reordered INTEGER,
    accepted INTEGER,
    stream_state TEXT,
    first_byte_ms REAL,
    response_ms REAL,
    response_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions (ts DESC);
CREATE INDEX IF NOT EXISTS decisions_decision_id ON decisions (decision_id);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    ts REAL NOT NULL,
    verdict TEXT NOT NULL,
    better_model TEXT,
    better_effort TEXT,
    note TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS feedback_decision_id ON feedback (decision_id);
CREATE INDEX IF NOT EXISTS feedback_ts ON feedback (ts DESC);
"""

# Columns added after the first release. Every one is nullable, so a database
# written by an older version keeps working: the old rows read back with NULL
# in the new columns, which is what "this decision predates the feature"
# means. Nothing is ever dropped or rewritten here.
ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "decisions": [
        ("route", "TEXT"),
        ("intended_model", "TEXT"),
        ("intended_effort", "TEXT"),
        ("fallback_index", "INTEGER"),
        ("fallback_reason", "TEXT"),
        ("pressures", "TEXT"),
        ("shifted", "TEXT"),
        ("reordered", "INTEGER"),
        ("accepted", "INTEGER"),
        ("stream_state", "TEXT"),
        ("first_byte_ms", "REAL"),
        ("response_ms", "REAL"),
        ("response_bytes", "INTEGER"),
    ],
}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any column this version knows about and the file does not."""
    added: list[str] = []
    for table, columns in ADDED_COLUMNS.items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            continue
        if not rows:
            continue
        have = {row[1] for row in rows}
        for name, ddl in columns:
            if name in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added.append(f"{table}.{name}")
    return added


def new_decision_id() -> str:
    """Short, stable, easy to paste into a feedback command."""
    return secrets.token_hex(6)


def conversation_key(
    auth_header: str,
    system_prompt: str,
    first_user_message: str,
    *,
    alias: str = "",
    client: str = "",
) -> str:
    """Versioned, unambiguously framed identity scoped to auth and policy.

    Legacy unscoped pins deliberately miss; no unsafe compatibility lookup.
    """
    auth_hash = hashlib.sha256(auth_header.encode()).hexdigest()
    parts = ["v2", auth_hash, alias, client, system_prompt, first_user_message]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path, log_state: bool = False) -> None:
        self.path = Path(path)
        self.log_state = log_state
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self.migrated = migrate(self._conn)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- pins ----------------------------------------------------------

    def get_pin(self, key: str, ttl_seconds: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT model, effort, created, decision_id FROM pins WHERE conversation_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        if time.time() - row["created"] > ttl_seconds:
            self.delete_pin(key)
            return None
        return {
            "model": row["model"],
            "effort": row["effort"],
            "created": row["created"],
            "decision_id": row["decision_id"],
        }

    def set_pin(
        self, key: str, model: str, effort: str | None, decision_id: str | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO pins (conversation_key, model, effort, created, decision_id) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(conversation_key) DO UPDATE SET "
                "model = excluded.model, effort = excluded.effort, created = excluded.created, "
                "decision_id = excluded.decision_id",
                (key, model, effort, time.time(), decision_id),
            )
            self._conn.commit()

    def delete_pin(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pins WHERE conversation_key = ?", (key,))
            self._conn.commit()

    # --- decision log --------------------------------------------------

    def log_decision(
        self,
        *,
        decision_id: str,
        alias: str,
        client: str,
        conversation_key: str,
        state_builder: str,
        answers: dict[str, Any],
        features: dict[str, Any],
        config_hash: str,
        model: str,
        effort: str | None,
        rule: str,
        mode: str,
        fallback: bool,
        pinned: bool,
        jev_ms: float | None,
        jev_tokens: int | None,
        est_tokens: int,
        message_count: int,
        state: Any = None,
        route: str | None = None,
        pressures: dict[str, float] | None = None,
        shifted: list[dict[str, Any]] | None = None,
        reordered: bool = False,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO decisions (decision_id, ts, alias, client, conversation_key,"
                " state_builder, answers, features, config_hash, model, effort, rule, mode,"
                " fallback, pinned, jev_ms, jev_tokens, est_tokens, message_count, state,"
                " route, pressures, shifted, reordered)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    time.time(),
                    alias,
                    client,
                    conversation_key,
                    state_builder,
                    json.dumps(answers, default=str),
                    json.dumps(features, default=str),
                    config_hash,
                    model,
                    effort,
                    rule,
                    mode,
                    fallback,
                    pinned,
                    jev_ms,
                    jev_tokens,
                    est_tokens,
                    message_count,
                    json.dumps(state, default=str)
                    if (self.log_state and state)
                    else None,
                    route,
                    json.dumps(pressures, default=str) if pressures else None,
                    json.dumps(shifted, default=str) if shifted else None,
                    bool(reordered),
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def update_decision(
        self,
        row_id: int,
        *,
        upstream_status: int | None = None,
        upstream_usage: dict[str, Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
        intended_model: str | None = None,
        intended_effort: str | None = None,
        fallback_index: int | None = None,
        fallback_reason: str | None = None,
        accepted: bool | None = None,
        stream_state: str | None = None,
        first_byte_ms: float | None = None,
        response_ms: float | None = None,
        response_bytes: int | None = None,
    ) -> None:
        if not row_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE decisions SET upstream_status = COALESCE(?, upstream_status),"
                " upstream_usage = COALESCE(?, upstream_usage),"
                " model = COALESCE(?, model),"
                " effort = CASE WHEN ? IS NOT NULL THEN ? ELSE COALESCE(?, effort) END,"
                " intended_model = COALESCE(?, intended_model),"
                " intended_effort = COALESCE(?, intended_effort),"
                " fallback_index = COALESCE(?, fallback_index),"
                " fallback_reason = COALESCE(?, fallback_reason),"
                " accepted = COALESCE(?, accepted), stream_state = COALESCE(?, stream_state),"
                " first_byte_ms = COALESCE(?, first_byte_ms), response_ms = COALESCE(?, response_ms),"
                " response_bytes = COALESCE(?, response_bytes)"
                " WHERE id = ?",
                (
                    upstream_status,
                    json.dumps(upstream_usage) if upstream_usage else None,
                    model,
                    model,
                    effort,
                    effort,
                    intended_model,
                    intended_effort,
                    fallback_index,
                    fallback_reason,
                    accepted,
                    stream_state,
                    first_byte_ms,
                    response_ms,
                    response_bytes,
                    row_id,
                ),
            )
            self._conn.commit()

    def recent_decisions(
        self, limit: int = 20, with_feedback: bool = True
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = [_decision_row(row) for row in rows]
        if with_feedback and out:
            ids = {item["decision_id"] for item in out}
            marks: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
            with self._lock:
                fb_rows = self._conn.execute(
                    "SELECT * FROM feedback WHERE decision_id IN "
                    "(SELECT decision_id FROM decisions ORDER BY id DESC LIMIT ?) "
                    "ORDER BY id ASC",
                    (limit,),
                ).fetchall()
            for fb in fb_rows:
                if fb["decision_id"] in marks:
                    marks[fb["decision_id"]].append(dict(fb))
            for item in out:
                item["feedback"] = marks.get(item["decision_id"], [])
        return out

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE decision_id = ? ORDER BY id DESC LIMIT 1",
                (decision_id,),
            ).fetchone()
        return _decision_row(row) if row else None

    def last_decision_id(self, client: str | None = None) -> str | None:
        """The newest decision, for this client header when one is given."""
        with self._lock:
            if client:
                row = self._conn.execute(
                    "SELECT decision_id FROM decisions WHERE client = ?"
                    " ORDER BY id DESC LIMIT 1",
                    (client,),
                ).fetchone()
                if row:
                    return str(row["decision_id"])
            row = self._conn.execute(
                "SELECT decision_id FROM decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return str(row["decision_id"]) if row else None

    def prune(self, older_than_seconds: float) -> dict[str, int]:
        """Explicit retention: remove old logs/feedback and expired pins."""
        cutoff = time.time() - older_than_seconds
        with self._lock:
            counts = {
                "feedback": self._conn.execute(
                    "DELETE FROM feedback WHERE ts < ?", (cutoff,)
                ).rowcount,
                "decisions": self._conn.execute(
                    "DELETE FROM decisions WHERE ts < ?", (cutoff,)
                ).rowcount,
                "pins": self._conn.execute(
                    "DELETE FROM pins WHERE created < ?", (cutoff,)
                ).rowcount,
            }
            self._conn.commit()
            return counts

    # --- feedback ------------------------------------------------------

    def add_feedback(
        self,
        *,
        decision_id: str,
        verdict: str,
        better_model: str | None = None,
        better_effort: str | None = None,
        note: str | None = None,
        source: str = "api",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO feedback (decision_id, ts, verdict, better_model, better_effort,"
                " note, source) VALUES (?,?,?,?,?,?,?)",
                (
                    decision_id,
                    time.time(),
                    verdict,
                    better_model,
                    better_effort,
                    note,
                    source,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def recent_feedback(self, limit: int = 20) -> list[dict[str, Any]]:
        """Feedback rows with the decision they are about."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.id, f.decision_id, f.ts, f.verdict, f.better_model, f.better_effort,"
                " f.note, f.source, d.alias, d.client, d.model, d.effort, d.rule, d.mode,"
                " d.answers, d.ts AS decision_ts"
                " FROM feedback f LEFT JOIN decisions d"
                " ON d.id = (SELECT MAX(id) FROM decisions WHERE decision_id = f.decision_id)"
                " ORDER BY f.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if item.get("answers"):
                item["answers"] = _load_json(item["answers"])
            out.append(item)
        return out


def _load_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for field in (
        "answers",
        "features",
        "upstream_usage",
        "state",
        "pressures",
        "shifted",
    ):
        if item.get(field):
            item[field] = _load_json(item[field])
    item["fallback"] = bool(item.get("fallback"))
    item["pinned"] = bool(item.get("pinned"))
    item["reordered"] = bool(item.get("reordered"))
    return item
