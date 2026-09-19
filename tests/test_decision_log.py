"""The semantic columns on the decision log, and replaying a row from it.

The columns this release adds are nullable and are written through a map of
their own, so a database from any earlier version keeps working and each
release's migration list still says exactly what that release added.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx

from conftest import make_config, write_config
from jev_router.config import SessionRoutingCfg, load_config
from jev_router.pins import Store
from jev_router.semantic import replay_decision
from jev_router.sessions import SEMANTIC_COLUMNS, Sessions

NEW_COLUMNS = {name for name, _ in SEMANTIC_COLUMNS["decisions"]}

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


def columns(path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


# --- migration -----------------------------------------------------------


def test_the_new_columns_are_added_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (decision_id, ts, alias, model, effort, rule, mode)"
        " VALUES ('old1', 1000.0, 'auto', 'big', 'high', 'hard', 'active')"
    )
    conn.commit()
    conn.close()
    assert not (columns(path, "decisions") & NEW_COLUMNS)

    store = Store(path)
    sessions = Sessions(store, SessionRoutingCfg(enabled=True))
    try:
        assert NEW_COLUMNS <= columns(path, "decisions")
        assert sorted(sessions.migrated_semantic) == sorted(
            f"decisions.{c}" for c in NEW_COLUMNS
        )
        # The row written before any of this reads back with nothing in them.
        row = store.get_decision("old1")
        assert row["model"] == "big"
        for name in NEW_COLUMNS:
            assert row[name] is None
    finally:
        sessions.close()
        store.close()


def test_migrating_twice_adds_nothing_the_second_time(tmp_path):
    store = Store(tmp_path / "fresh.db")
    first = Sessions(store, SessionRoutingCfg(enabled=True))
    first.close()
    second = Sessions(store, SessionRoutingCfg(enabled=True))
    try:
        assert second.migrated_semantic == []
    finally:
        second.close()
        store.close()


# --- writing -------------------------------------------------------------


def logged(store: Store, **overrides):
    fields = dict(
        decision_id="d1",
        alias="auto",
        client="",
        conversation_key="k",
        state_builder="summary_v1",
        answers={"difficulty": {"type": "score", "score": 2.0, "confidence": 0.9}},
        features={"est_tokens": 100, "message_count": 1},
        config_hash="testhash",
        model="big",
        effort="high",
        rule="hard",
        mode="active",
        fallback=False,
        pinned=False,
        jev_ms=12.0,
        jev_tokens=400,
        est_tokens=100,
        message_count=1,
    )
    fields.update(overrides)
    return store.log_decision(**fields)


def test_the_semantic_facts_are_written_and_read_back(tmp_path):
    store = Store(tmp_path / "router.db")
    sessions = Sessions(store, SessionRoutingCfg(enabled=True))
    try:
        row_id = logged(store)
        sessions.annotate_semantics(
            row_id,
            packet_version="pkt1:abc",
            shadow_packet_version="pkt1:def",
            shadow_answers={"mechanical_transform": {"type": "noul", "value": 0.9}},
            action="route",
            experiment_routes=[{"policy": "mean_only", "model": "big", "changed": True}],
            quota_snapshot={"cloud": {"status": "stale"}},
            quota_status={"cloud": "stale"},
            counterfactual=("small", "none"),
            exclusions=[{"model": "small", "reason": "the request carries an image"}],
            evidence="qualified",
            qualification_ref="POOL.md: somewhere",
        )
        row = store.get_decision("d1")
        assert row["packet_version"] == "pkt1:abc"
        assert row["shadow_answers"]["mechanical_transform"]["value"] == 0.9
        assert row["experiment_routes"][0]["policy"] == "mean_only"
        assert row["quota_status"] == {"cloud": "stale"}
        assert row["counterfactual"] == "small(none)"
        assert row["exclusions"][0]["model"] == "small"
        assert row["evidence"] == "qualified"
        assert row["action"] == "route"
    finally:
        sessions.close()
        store.close()


def test_annotating_with_nothing_leaves_the_row_alone(tmp_path):
    store = Store(tmp_path / "router.db")
    sessions = Sessions(store, SessionRoutingCfg(enabled=True))
    try:
        row_id = logged(store)
        sessions.annotate_semantics(row_id, packet_version="pkt1:abc")
        sessions.annotate_semantics(row_id)
        row = store.get_decision("d1")
        assert row["packet_version"] == "pkt1:abc"
        assert row["evidence"] is None
        # A zero row id is a decision that was never logged; it is not an error.
        sessions.annotate_semantics(0, packet_version="pkt1:xyz")
    finally:
        sessions.close()
        store.close()


JEV_URL = "https://jev.test/v1/systemone"


def jev_answers(difficulty=0.1):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "task": {"type": "choice", "choice": "quick_question", "confidence": 0.9},
            "difficulty": {"type": "score", "score": difficulty, "confidence": 0.9},
            "harm_if_wrong": {"type": "noul", "noul": 0.1},
        },
        "usage": {"input_tokens": 100},
    }


@respx.mock
async def test_a_served_request_records_its_packet_and_its_quota(tmp_path, monkeypatch):
    from jev_router.app import create_app

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=jev_answers()))
    respx.post("http://upstream.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    config = make_config(settings={"sqlite_path": str(tmp_path / "router.db")})
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
        async with app.router.lifespan_context(app):
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            )
    assert resp.status_code == 200

    store = Store(tmp_path / "router.db")
    try:
        row = store.recent_decisions(1)[0]
        assert row["action"] == "route"
        assert row["packet_version"].startswith("pkt1:")
        assert row["quota_status"] == {"default": "unknown"}
        assert row["counterfactual"] == "small(none)"
        assert row["evidence"] == "weak"  # no lane is configured here
    finally:
        store.close()


# --- replay --------------------------------------------------------------


def test_replay_reproduces_a_decision_from_its_own_row(config, tmp_path):
    store = Store(tmp_path / "router.db")
    try:
        logged(
            store,
            answers={
                "task": {"type": "choice", "choice": "code", "confidence": 0.9},
                "difficulty": {"type": "score", "score": 2.0, "confidence": 0.9},
                "harm_if_wrong": {"type": "noul", "noul": 0.1},
            },
            features={"est_tokens": 100, "message_count": 1, "requested_model": "auto"},
            model="big",
            effort="high",
        )
        row = store.get_decision("d1")
    finally:
        store.close()

    result = replay_decision(config, row)
    assert result.same_config
    assert result.reproduced
    assert result.replayed == ("big", "high")
    assert "reproduced" in result.verdict()


def test_replay_replays_at_the_pressure_the_decision_was_taken_under(tmp_path):
    """A deliberate quota saving must not be scored as a routing change."""
    config = make_config(
        providers={"cloud": {}, "local": {}},
        models={
            "small": {"provider": "local"},
            "big": {"provider": "cloud"},
            "paramy": {"provider": "local"},
        },
        policy={
            "rules": [
                {
                    "name": "hard",
                    "when": {
                        "difficulty": {"gte": 1.5, "shift_with": "cloud", "max_shift": 1.0}
                    },
                    "use": {"model": "big", "effort": "high"},
                },
                {"name": "easy", "when": {}, "use": {"model": "small", "effort": "none"}},
            ],
            "default": {"model": "big", "effort": "medium"},
        },
    )
    store = Store(tmp_path / "router.db")
    try:
        logged(
            store,
            answers={"difficulty": {"type": "score", "score": 2.0, "confidence": 0.9}},
            features={"est_tokens": 100, "message_count": 1},
            model="small",
            effort="none",
            rule="easy",
            pressures={"cloud": 1.0},
        )
        row = store.get_decision("d1")
    finally:
        store.close()

    result = replay_decision(config, row)
    assert result.pressures == {"cloud": 1.0}
    assert result.reproduced


def test_replay_says_so_when_the_config_has_moved(config, tmp_path):
    store = Store(tmp_path / "router.db")
    try:
        logged(store, config_hash="a-different-hash")
        row = store.get_decision("d1")
    finally:
        store.close()
    result = replay_decision(config, row)
    assert result.same_config is False
    assert "config has changed" in result.verdict()
    assert "matches" not in result.verdict()


def test_replay_reports_an_alias_that_no_longer_exists(config, tmp_path):
    store = Store(tmp_path / "router.db")
    try:
        logged(store, alias="gone")
        row = store.get_decision("d1")
    finally:
        store.close()
    result = replay_decision(config, row)
    assert "not in this router.yaml" in result.error
    assert "cannot replay" in result.verdict()


def test_replay_says_a_fallback_is_not_replay_evidence(config, tmp_path):
    store = Store(tmp_path / "router.db")
    try:
        logged(store, answers={}, model="big", effort="medium", rule="fallback")
        row = store.get_decision("d1")
    finally:
        store.close()
    result = replay_decision(config, row)
    assert any("not replay evidence" in n for n in result.notes)


def test_replay_needs_no_prompt(config, tmp_path):
    """Invariant I13: counts and flags are enough, and they are all there is."""
    store = Store(tmp_path / "router.db")
    try:
        logged(store)
        row = store.get_decision("d1")
    finally:
        store.close()
    assert row["state"] is None
    blob = json.dumps(row, default=str)
    assert "content" not in blob and "messages" not in blob
    assert replay_decision(config, row).replayed is not None


# --- the command line ----------------------------------------------------


def written_decision(tmp_path):
    path = write_config(tmp_path)
    config = load_config(path)
    store = Store(config.settings.db_path)
    sessions = Sessions(store, config.session_routing, guard=False)
    row_id = logged(
        store,
        config_hash=config.config_hash,
        answers={
            "task": {"type": "choice", "choice": "code", "confidence": 0.9},
            "difficulty": {"type": "score", "score": 2.0, "confidence": 0.9},
            "harm_if_wrong": {"type": "noul", "noul": 0.1},
        },
        features={"est_tokens": 100, "message_count": 1},
    )
    sessions.annotate_semantics(
        row_id,
        packet_version="pkt1:abc",
        shadow_answers={"mechanical_transform": {"type": "noul", "value": 0.9}},
        action="route",
        quota_status={"default": "unknown"},
        counterfactual=("small", "none"),
        evidence="weak",
        exclusions=[{"model": "small", "reason": "the request carries an image"}],
    )
    sessions.close()
    store.close()
    return path


def test_decisions_shows_the_shadow_answers_and_the_counterfactual(tmp_path, capsys):
    from jev_router.cli import main

    path = written_decision(tmp_path)
    assert main(["-c", path, "decisions"]) == 0
    out = capsys.readouterr().out
    assert "shadow: mechanical_transform=0.90" in out
    assert "without pressure=small(none)" in out
    assert "evidence=weak" in out
    assert "default=unknown" in out
    assert "ruled out small: the request carries an image" in out


def test_decisions_replay_reports_a_match(tmp_path, capsys):
    from jev_router.cli import main

    path = written_decision(tmp_path)
    assert main(["-c", path, "decisions", "replay", "d1"]) == 0
    out = capsys.readouterr().out
    assert "reproduced" in out
    assert "big(high)" in out


def test_decisions_replay_takes_last_and_json(tmp_path, capsys):
    from jev_router.cli import main

    path = written_decision(tmp_path)
    assert main(["-c", path, "decisions", "replay", "last", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reproduced"] is True
    assert payload["stored"] == ["big", "high"]


def test_decisions_replay_reports_an_unknown_id(tmp_path, capsys):
    from jev_router.cli import main

    path = written_decision(tmp_path)
    assert main(["-c", path, "decisions", "replay", "nope"]) == 1
    assert "unknown decision id" in capsys.readouterr().err


def test_decisions_replay_needs_an_id(tmp_path, capsys):
    from jev_router.cli import main

    path = written_decision(tmp_path)
    assert main(["-c", path, "decisions", "replay"]) == 2
    assert "needs a decision id" in capsys.readouterr().err


def test_decisions_still_tails_without_an_action(tmp_path, capsys):
    from jev_router.cli import main

    path = written_decision(tmp_path)
    assert main(["-c", path, "decisions", "-n", "5"]) == 0
    assert "d1" in capsys.readouterr().out


@respx.mock
def test_explain_shows_active_shadow_lane_and_quota(tmp_path, capsys, monkeypatch):
    from jev_router.cli import main

    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    raw = {
        "questions": {
            "mechanical_transform": {
                "type": "noul",
                "instructions": "Is the change stated?",
            }
        },
        "semantic_policy": {"shadow_questions": ["mechanical_transform"]},
    }
    config = make_config(**raw)
    path = write_config(tmp_path, json.loads(config.model_dump_json()))
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    )
    respx.post(JEV_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    **jev_answers()["answers"],
                    "mechanical_transform": {"type": "noul", "noul": 0.95},
                },
                "usage": {},
            },
        )
    )
    assert main(["-c", path, "explain", str(request)]) == 0
    out = capsys.readouterr().out
    assert "jev answers (active, these decide)" in out
    assert "jev answers (shadow, recorded and read by nothing)" in out
    assert "0.95" in out
    assert "packet version: pkt1:" in out
    assert "hard constraints:" in out
    assert "quota:" in out
    assert "without pressure:" in out


@pytest.mark.parametrize("word", ["unknown", "stale"])
def test_quota_freshness_never_reads_as_spare_capacity(word):
    from jev_router.quota import QuotaSnapshot, snapshot_status

    snapshot = (
        None if word == "unknown"
        else QuotaSnapshot(provider="p", fetched_at=0.0, used_percent=5.0)
    )
    assert snapshot_status(snapshot, 10_000.0, 1800) == word
