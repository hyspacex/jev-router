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
    state TEXT
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


def new_decision_id() -> str:
    """Short, stable, easy to paste into a feedback command."""
    return secrets.token_hex(6)


def conversation_key(auth_header: str, system_prompt: str, first_user_message: str) -> str:
    """Stable across the turns of one conversation, and not reversible."""
    auth_hash = hashlib.sha256(auth_header.encode()).hexdigest()
    digest = hashlib.sha256()
    for part in (auth_hash, system_prompt, first_user_message):
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


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
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO decisions (decision_id, ts, alias, client, conversation_key,"
                " state_builder, answers, features, config_hash, model, effort, rule, mode,"
                " fallback, pinned, jev_ms, jev_tokens, est_tokens, message_count, state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    int(fallback),
                    int(pinned),
                    jev_ms,
                    jev_tokens,
                    est_tokens,
                    message_count,
                    json.dumps(state, default=str) if (self.log_state and state) else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def update_decision(
        self,
        row_id: int,
        *,
        upstream_status: int | None = None,
        upstream_usage: dict[str, Any] | None = None,
    ) -> None:
        if not row_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE decisions SET upstream_status = COALESCE(?, upstream_status),"
                " upstream_usage = COALESCE(?, upstream_usage) WHERE id = ?",
                (
                    upstream_status,
                    json.dumps(upstream_usage) if upstream_usage else None,
                    row_id,
                ),
            )
            self._conn.commit()

    def recent_decisions(self, limit: int = 20, with_feedback: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = [_decision_row(row) for row in rows]
        if with_feedback and out:
            ids = {item["decision_id"] for item in out}
            marks: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
            placeholders = ",".join("?" * len(ids))
            with self._lock:
                fb_rows = self._conn.execute(
                    f"SELECT * FROM feedback WHERE decision_id IN ({placeholders})"
                    " ORDER BY id ASC",
                    tuple(ids),
                ).fetchall()
            for fb in fb_rows:
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
                (decision_id, time.time(), verdict, better_model, better_effort, note, source),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

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
                try:
                    item["answers"] = json.loads(item["answers"])
                except (TypeError, ValueError):
                    pass
            out.append(item)
        return out


def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for field in ("answers", "features", "upstream_usage", "state"):
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, ValueError):
                pass
    item["fallback"] = bool(item.get("fallback"))
    item["pinned"] = bool(item.get("pinned"))
    return item
