"""The turn-plan ledger in sqlite: additive columns, hashes, no transcripts."""

from __future__ import annotations

import sqlite3
import time

from jev_router.config import SessionRoutingCfg
from jev_router.pins import Store
from jev_router.sessions import COMPACTION_COLUMNS, EFFORT_COLUMNS, Sessions

NEW_SESSION_COLUMNS = {name for name, _ in EFFORT_COLUMNS["sessions"]}


def open_sessions(path):
    store = Store(path)
    return store, Sessions(store, SessionRoutingCfg(enabled=True), guard=False)


def columns(path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def a_plan(plan_id="plan-1", turn_id="turn-1", sequence=1, **fields):
    row = {
        "plan_id": plan_id,
        "turn_id": turn_id,
        "sequence": sequence,
        "mode": "active",
        "action": "change_effort",
        "status": "planned",
        "from_effort": "low",
        "to_effort": "medium",
        "base_effort": "low",
        "recommendation": "medium",
        "expected_effort": "medium",
        "request_fingerprint": "f" * 64,
        "reason": "difficulty is high",
    }
    row.update(fields)
    return row


def test_a_database_without_the_ledger_gains_it(tmp_path):
    path = tmp_path / "old.db"
    store, sessions = open_sessions(path)
    sessions.close()
    store.close()
    # Drop the ledger and the new session columns, as an older release's file
    # would have them, then open it again.
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE turn_plans")
    conn.execute("ALTER TABLE sessions DROP COLUMN adaptation_mode")
    conn.commit()
    conn.close()
    assert "turn_plans" not in {
        r[0] for r in sqlite3.connect(str(path)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    store, sessions = open_sessions(path)
    try:
        assert "turn_plans" in {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert sessions.migrated_effort == ["sessions.adaptation_mode"]
        assert NEW_SESSION_COLUMNS <= columns(path, "sessions")
    finally:
        sessions.close()
        store.close()


def test_a_fresh_database_needs_no_effort_migration(tmp_path):
    store, sessions = open_sessions(tmp_path / "fresh.db")
    try:
        assert sessions.migrated_effort == []
        assert NEW_SESSION_COLUMNS <= columns(tmp_path / "fresh.db", "sessions")
    finally:
        sessions.close()
        store.close()


def test_a_database_written_before_compaction_epochs_gains_them(tmp_path):
    """Additive and nullable, like every column before them.

    A row written before this release has NULL in all of them, which reads as
    "this session has never compacted" and is exactly right.
    """
    path = tmp_path / "old.db"
    store, sessions = open_sessions(path)
    sessions.record_plan("s1", a_plan())
    sessions.close()
    store.close()
    conn = sqlite3.connect(str(path))
    for table, names in COMPACTION_COLUMNS.items():
        for name, _type in names:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    conn.commit()
    conn.close()

    store, sessions = open_sessions(path)
    try:
        assert sorted(sessions.migrated_compaction) == sorted(
            f"{table}.{name}"
            for table, names in COMPACTION_COLUMNS.items()
            for name, _type in names
        )
        assert {"compaction_epoch", "compacted_at"} <= columns(path, "sessions")
        assert "compaction_epoch" in columns(path, "turn_plans")
        # The row written before the columns existed still reads back, and
        # its epoch is the one every pre-compaction row is in.
        old = sessions.get_plan("s1", plan_id="plan-1")
        assert old is not None and old["compaction_epoch"] is None
        assert sessions.applied_updates("s1", epoch=0) == []  # it is only planned
    finally:
        sessions.close()
        store.close()


def test_an_epoch_hides_earlier_updates_without_deleting_them(tmp_path):
    store, sessions = open_sessions(tmp_path / "router.db")
    try:
        sessions.record_plan(
            "s1", a_plan(status="confirmed", compaction_epoch=0, anchor_position=0)
        )
        sessions.record_plan(
            "s1",
            a_plan(
                plan_id="plan-2",
                turn_id="turn-2",
                sequence=2,
                status="confirmed",
                compaction_epoch=1,
                anchor_position=4,
            ),
        )
        assert [p["plan_id"] for p in sessions.applied_updates("s1", epoch=0)] == [
            "plan-1",
            "plan-2",
        ]
        assert [p["plan_id"] for p in sessions.applied_updates("s1", epoch=1)] == [
            "plan-2"
        ]
        # Nothing was removed: the ledger still holds both.
        assert len(sessions.plans("s1")) == 2
    finally:
        sessions.close()
        store.close()


def test_a_plan_round_trips_and_is_found_by_turn_or_by_id(tmp_path):
    store, sessions = open_sessions(tmp_path / "router.db")
    try:
        sessions.record_plan("s1", a_plan(facts={"thresholds": {"upgrade_harm": 0.6}}))
        by_turn = sessions.get_plan("s1", turn_id="turn-1")
        by_id = sessions.get_plan("s1", plan_id="plan-1")
        assert by_turn["plan_id"] == "plan-1"
        assert by_id["facts"]["thresholds"]["upgrade_harm"] == 0.6
        assert sessions.plan_by_id("plan-1")["session_id"] == "s1"
        assert sessions.get_plan("s1", turn_id="turn-nope") is None
    finally:
        sessions.close()
        store.close()


def test_writing_the_same_plan_id_twice_keeps_the_first(tmp_path):
    store, sessions = open_sessions(tmp_path / "router.db")
    try:
        sessions.record_plan("s1", a_plan(reason="first"))
        again = sessions.record_plan("s1", a_plan(reason="second"))
        assert again["reason"] == "first"
        assert len(sessions.plans("s1")) == 1
    finally:
        sessions.close()
        store.close()


def test_sequences_and_ordering(tmp_path):
    store, sessions = open_sessions(tmp_path / "router.db")
    try:
        assert sessions.next_sequence("s1") == 1
        sessions.record_plan("s1", a_plan("p1", "t1", 1))
        assert sessions.next_sequence("s1") == 2
        sessions.record_plan("s1", a_plan("p2", "t2", 2))
        assert [p["plan_id"] for p in sessions.plans("s1")] == ["p1", "p2"]
        assert sessions.next_sequence("s2") == 1
    finally:
        sessions.close()
        store.close()


def test_only_submitted_changes_count_as_applied_history(tmp_path):
    store, sessions = open_sessions(tmp_path / "router.db")
    try:
        sessions.record_plan("s1", a_plan("p1", "t1", 1, status="confirmed"))
        sessions.record_plan("s1", a_plan("p2", "t2", 2, status="accepted"))
        sessions.record_plan("s1", a_plan("p3", "t3", 3, status="outcome_unknown"))
        sessions.record_plan("s1", a_plan("p4", "t4", 4, status="rejected"))
        sessions.record_plan("s1", a_plan("p5", "t5", 5, status="planned"))
        sessions.record_plan(
            "s1", a_plan("p6", "t6", 6, status="confirmed", action="keep")
        )
        applied = [p["plan_id"] for p in sessions.applied_updates("s1")]
        assert applied == ["p1", "p2", "p3"]
    finally:
        sessions.close()
        store.close()


def test_a_plan_row_holds_no_message_text(tmp_path):
    store, sessions = open_sessions(tmp_path / "router.db")
    try:
        sessions.record_plan(
            "s1",
            a_plan(anchor_prefix_hash="a" * 64, parent_response_id="resp_123"),
        )
        row = sessions.get_plan("s1", plan_id="plan-1")
        blob = str(row)
        assert "Add quoted CSV" not in blob
        assert set(row) >= {
            "turn_id", "sequence", "from_effort", "to_effort", "base_effort",
            "plan_id", "anchor_position", "anchor_prefix_hash",
            "parent_response_id", "expected_effort", "confirmed_effort",
            "status", "request_fingerprint", "recommendation", "created",
            "updated",
        }
    finally:
        sessions.close()
        store.close()


def test_pruning_takes_the_ledger_with_it(tmp_path):
    store, sessions = open_sessions(tmp_path / "router.db")
    try:
        sessions.record_plan("s1", a_plan())
        sessions.clock = lambda: time.time() + 10_000
        counts = sessions.prune(60)
        assert counts["turn_plans"] == 1
        assert sessions.plans("s1") == []
    finally:
        sessions.close()
        store.close()
