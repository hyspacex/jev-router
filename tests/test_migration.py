"""Two promises about upgrading: same routes, and the old database still works.

`tests/fixtures/router_before_providers.yaml` is the shipped config as it was
before providers, routes, fallbacks and quota existed. Nothing in this file
may edit it.
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from jev_router.config import load_config
from jev_router.features import Features
from jev_router.pins import Store, migrate
from jev_router.policy import evaluate

ROOT = Path(__file__).resolve().parent.parent
OLD_CONFIG = Path(__file__).resolve().parent / "fixtures" / "router_before_providers.yaml"
# The first config written in the providers-and-routes schema: the same two
# models and the same rules as the old file. The shipped router.yaml has since
# gained models and lanes, so it is no longer expected to match the old one.
MIGRATED_CONFIG = Path(__file__).resolve().parent / "fixtures" / "router_two_models_with_routes.yaml"
NEW_CONFIG = ROOT / "router.yaml"

# The categories the shipped `task` question offers, plus one that is not in
# the codey list and one the classifier never emits.
TASKS = [
    "code-edit",
    "debugging",
    "agentic-tool-task",
    "design-or-review",
    "quick-question",
    "summarise-or-extract",
    "high-stakes-writing",
    "creative-prose",
    "everyday-polish",
    "other",
]
# Either side of every cutoff in the ladder, and the ends of the scale.
DIFFICULTIES = [0.0, 0.5, 1.39, 1.4, 1.41, 2.0, 2.39, 2.4, 2.41, 2.9, 3.0, 3.01, 3.5]
HARMS = [0.0, 0.3, 0.79, 0.8, 0.81, 1.0]
CONFIDENCES = [0.0, 0.2, 0.39, 0.4, 0.41, 0.9, 1.0]


def grid():
    """Every combination that could pick a different rung of the ladder."""
    for task, difficulty, harm, conf in itertools.product(
        TASKS, DIFFICULTIES, HARMS, CONFIDENCES
    ):
        yield {
            "task": {"type": "choice", "choice": task, "confidence": conf},
            "difficulty": {"type": "score", "score": difficulty, "confidence": conf},
            "harm_if_wrong": {"type": "noul", "noul": harm},
        }


FEATURE_SHAPES = [
    Features(model="auto", message_count=1, est_tokens=200),
    Features(model="auto", message_count=1, est_tokens=200, has_tools=True,
             tool_names=("read_file",)),
    Features(model="auto", message_count=1, est_tokens=200, has_images=True),
    Features(model="auto", message_count=1, est_tokens=200, has_code=True),
    Features(model="auto", message_count=9, est_tokens=300_000, max_tokens=4000),
    Features(model="auto", message_count=1, est_tokens=200, client="floored"),
]


@pytest.fixture(scope="module")
def old_config():
    return load_config(OLD_CONFIG)


@pytest.fixture(scope="module")
def migrated_config():
    return load_config(MIGRATED_CONFIG)


@pytest.fixture(scope="module")
def new_config():
    return load_config(NEW_CONFIG)


def test_the_old_config_still_loads(old_config):
    """A router.yaml written before providers existed is still valid."""
    assert old_config.provider_names() == ["default"]
    assert old_config.routes == {}
    assert set(old_config.models) == {"gpt-6-astra", "ollama/glm-5.3-flash"}


@pytest.mark.parametrize("alias", ["auto", "auto-fast", "auto-code"])
def test_the_new_config_routes_every_answer_exactly_as_the_old_one_did(
    old_config, migrated_config, alias
):
    """The whole point of the rewrite: the schema changed, the routing did not.

    Quota is off (no source is enabled), so every pressure is zero and every
    shifted cutoff sits on its configured number.
    """
    new_config = migrated_config
    old_alias = old_config.aliases[alias]
    new_alias = new_config.aliases[alias]
    checked = 0
    for answers in grid():
        for features in FEATURE_SHAPES:
            before = evaluate(old_config, old_alias, answers, features)
            after = evaluate(new_config, new_alias, answers, features)
            assert (after.model, after.effort) == (before.model, before.effort), (
                f"{alias}: {answers} {features.client or 'no client'} "
                f"moved from {before.model}({before.effort}) to "
                f"{after.model}({after.effort})"
            )
            assert after.rule == before.rule
            checked += 1
    assert checked > 5000


def test_with_no_pressure_nothing_is_shifted_and_no_route_is_reordered(new_config):
    alias = new_config.aliases["auto"]
    for answers in itertools.islice(grid(), 200):
        result = evaluate(new_config, alias, answers, FEATURE_SHAPES[0])
        assert result.shifts == []
        assert result.reordered is False
        assert result.pressure_changed_the_outcome is False


def test_the_shipped_config_ships_with_every_quota_source_disabled(new_config):
    """A stranger's install must not run a subprocess on its own."""
    for name in new_config.provider_names():
        quota = new_config.quota_cfg(name)
        assert quota is None or quota.enabled is False


def test_every_shipped_route_entry_names_a_model_and_an_effort_the_model_allows(new_config):
    for name, lane in new_config.routes.items():
        for entry in lane.entries():
            mcfg = new_config.models[entry.model]
            if not mcfg.efforts:
                assert entry.effort is None, f"{name}: {entry.model} takes no effort"
            else:
                assert entry.effort in mcfg.efforts, f"{name}: {entry.model} {entry.effort}"


def test_the_fast_model_declares_a_default_effort(new_config):
    """It reasons without limit when it is sent bare."""
    assert new_config.models["ollama/glm-5.3-flash"].default_effort == "none"


# --- the database -------------------------------------------------------

# The `decisions` table exactly as the first release wrote it.
OLD_SCHEMA = """
CREATE TABLE pins (
    conversation_key TEXT PRIMARY KEY, model TEXT NOT NULL, effort TEXT,
    created REAL NOT NULL, decision_id TEXT
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, ts REAL NOT NULL,
    alias TEXT, client TEXT, conversation_key TEXT, state_builder TEXT, answers TEXT,
    features TEXT, config_hash TEXT, model TEXT, effort TEXT, rule TEXT, mode TEXT,
    fallback INTEGER, pinned INTEGER, jev_ms REAL, jev_tokens INTEGER, est_tokens INTEGER,
    message_count INTEGER, upstream_status INTEGER, upstream_usage TEXT, state TEXT
);
CREATE TABLE feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, ts REAL NOT NULL,
    verdict TEXT NOT NULL, better_model TEXT, better_effort TEXT, note TEXT, source TEXT
);
"""

NEW_COLUMNS = {
    "route",
    "intended_model",
    "intended_effort",
    "fallback_index",
    "fallback_reason",
    "pressures",
    "shifted",
    "reordered",
}


def old_database(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (decision_id, ts, alias, client, conversation_key,"
        " state_builder, answers, features, config_hash, model, effort, rule, mode,"
        " fallback, pinned, jev_ms, jev_tokens, est_tokens, message_count)"
        " VALUES ('old1', 1000.0, 'auto', '', 'convkey', 'summary_v1',"
        " '{\"difficulty\": {\"type\": \"score\", \"score\": 2.0}}', '{}', 'hash',"
        " 'gpt-6-astra', 'high', 'hard', 'active', 0, 0, 120.0, 400, 400, 2)"
    )
    conn.execute(
        "INSERT INTO pins (conversation_key, model, effort, created, decision_id)"
        " VALUES ('convkey', 'gpt-6-astra', 'high', 1000.0, 'old1')"
    )
    conn.execute(
        "INSERT INTO feedback (decision_id, ts, verdict, source)"
        " VALUES ('old1', 1001.0, 'right', 'cli')"
    )
    conn.commit()
    conn.close()
    return path


def columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_a_database_from_the_old_schema_gains_the_new_columns(tmp_path):
    path = old_database(tmp_path / "old.db")
    assert not (columns(path, "decisions") & NEW_COLUMNS)

    store = Store(path)
    try:
        assert NEW_COLUMNS <= columns(path, "decisions")
        assert sorted(store.migrated) == sorted(f"decisions.{c}" for c in NEW_COLUMNS)
    finally:
        store.close()


def test_the_rows_in_an_old_database_still_read_back(tmp_path):
    store = Store(old_database(tmp_path / "old.db"))
    try:
        row = store.get_decision("old1")
        assert row["model"] == "gpt-6-astra"
        assert row["answers"]["difficulty"]["score"] == 2.0
        # A decision taken before quota existed has no pressure recorded,
        # which is exactly what "it was taken at pressure zero" looks like.
        assert row["pressures"] is None
        assert row["fallback_index"] is None
        assert row["reordered"] is False
        assert store.recent_feedback(5)[0]["verdict"] == "right"
        assert store.get_pin("convkey", ttl_seconds=10**12)["model"] == "gpt-6-astra"
    finally:
        store.close()


def test_an_old_database_takes_new_rows_and_new_updates(tmp_path):
    store = Store(old_database(tmp_path / "old.db"))
    try:
        row_id = store.log_decision(
            decision_id="new1", alias="auto", client="", conversation_key="k2",
            state_builder="summary_v1", answers={}, features={}, config_hash="h",
            model="big", effort="high", rule="hard", mode="active", fallback=False,
            pinned=False, jev_ms=None, jev_tokens=None, est_tokens=1, message_count=1,
            route="frontier", pressures={"cloud": 0.7}, shifted=[{"rule": "hard"}],
            reordered=True,
        )
        store.update_decision(
            row_id, model="small", effort="low", intended_model="big",
            intended_effort="high", fallback_index=1, fallback_reason="big: HTTP 429",
        )
        row = store.get_decision("new1")
        assert row["model"] == "small"
        assert row["intended_model"] == "big"
        assert row["fallback_index"] == 1
        assert row["pressures"] == {"cloud": 0.7}
        assert row["reordered"] is True
        assert len(store.recent_decisions(10)) == 2   # the old row is still there
    finally:
        store.close()


def test_migrating_twice_does_nothing_the_second_time(tmp_path):
    path = old_database(tmp_path / "old.db")
    store = Store(path)
    try:
        assert store.migrated
        conn = sqlite3.connect(str(path))
        try:
            assert migrate(conn) == []
        finally:
            conn.close()
    finally:
        store.close()


def test_a_fresh_database_needs_no_migration(tmp_path):
    store = Store(tmp_path / "fresh.db")
    try:
        assert store.migrated == []
        assert NEW_COLUMNS <= columns(tmp_path / "fresh.db", "decisions")
    finally:
        store.close()
