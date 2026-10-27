"""Storage for the session inspector: replay, config snapshots, state shape, bookmarks.

Offline: Jev and the upstream are fakes, and nothing here calls a provider.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
import session_harness as H
from conftest import BASE_CONFIG, write_config
from session_harness import ADMIN, Client, Service
from test_app import chat_body, mock_jev, upstream_ok

from jev_router.app import create_app
from jev_router.cli import main
from jev_router.config import RouterConfig, config_text_hash, load_config
from jev_router.pins import Store
from jev_router.sessions import INSPECTOR_COLUMNS, Sessions
from jev_router.state import state_shape

SECRET = "sk-SECRET-91af"
MARKER = "zqx-marker-5c1e-never-stored"


def with_quota_command(raw: dict) -> dict:
    raw = copy.deepcopy(raw)
    raw["providers"] = {
        "vendor": {
            "quota": {
                "source": "command",
                "enabled": False,
                "config": {
                    "argv": ["codexbar", "usage", "--token", SECRET],
                    "timeout_ms": 5000,
                },
            }
        }
    }
    for model in raw["models"].values():
        model["provider"] = "vendor"
    return raw


def dump_db(path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)


@pytest.fixture
async def strict(tmp_path, env):
    """A strict service whose config came from a file, so hashes match the CLI's."""
    path = write_config(tmp_path, with_quota_command(H.session_raw()))
    svc = Service(tmp_path, config=load_config(path))
    yield svc, path
    await svc.close()


async def run_strict_session(svc, text: str = "Add quoted CSV fields and tests.") -> Client:
    caller = Client(svc)
    mock_jev(task="code", difficulty=2.0, harm=0.1)
    H.upstream()
    response = await caller.resolve(request=H.user_request(text=text))
    assert response.status_code == 200, response.text
    for _ in range(2):
        executed = await caller.execute(H.user_request(caller.wire_model(), text=text))
        assert executed.status_code == 200, executed.text
    return caller


# --- 1. replay picks the deciding row -------------------------------------


@respx.mock
async def test_strict_replay_reads_the_admission_not_the_newest_execution(strict, capsys):
    svc, path = strict
    caller = await run_strict_session(svc)
    decision_id = svc.sessions.get(caller.session_id)["decision_id"]

    # The regression: the newest row for the id is an execution with no answers.
    newest = svc.store.get_decision(decision_id)
    assert newest["event_type"] == "execution" and newest["answers"] == {}
    deciding = svc.store.deciding_decision(decision_id)
    assert deciding["event_type"] == "admission"
    assert deciding["answers"]

    await svc.close()
    assert main(["-c", path, "decisions", "replay", decision_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["event_type"] == "admission"
    assert out["row"] == deciding["id"]
    assert out["reproduced"] is True and out["same_config"] is True
    svc.open()


@respx.mock
async def test_replay_by_row_id_replays_that_exact_row(strict, capsys):
    svc, path = strict
    caller = await run_strict_session(svc)
    rows = svc.sessions.decision_rows(caller.session_id)
    admission, execution = rows[0], rows[-1]
    await svc.close()

    assert main(["-c", path, "decisions", "replay", f"#{admission['id']}", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["row"] == admission["id"]

    # An execution row is replayed as asked, and honestly does not reproduce.
    assert main(["-c", path, "decisions", "replay", "--row", str(execution["id"]), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["row"] == execution["id"] and out["event_type"] == "execution"
    assert out["reproduced"] is False

    assert main(["-c", path, "decisions", "replay", "--row", "999999"]) == 1
    assert "unknown decision row #999999" in capsys.readouterr().err
    assert main(["-c", path, "decisions", "replay", "#notanumber"]) == 2
    assert main(["-c", path, "decisions", "replay"]) == 2
    svc.open()


@respx.mock
async def test_legacy_pinned_replay_reads_the_first_unpinned_row(tmp_path, env, capsys):
    path = write_config(tmp_path)
    cfg = load_config(path)
    store = Store(cfg.settings.db_path)
    app = create_app(cfg, store)
    mock_jev(task="quick_question", difficulty=0.1)
    upstream_ok()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as client:
        first = await client.post("/v1/chat/completions", json=chat_body())
        second = await client.post("/v1/chat/completions", json=chat_body())
    decision_id = first.headers["X-Router-Decision"]
    assert second.headers["X-Router-Decision"] == decision_id

    assert store.get_decision(decision_id)["pinned"] is True
    deciding = store.deciding_decision(decision_id)
    assert deciding["pinned"] is False and deciding["answers"]
    await app.state.router.aclose()
    store.close()

    assert main(["-c", path, "decisions", "replay", decision_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["row"] == deciding["id"] and out["reproduced"] is True


def test_deciding_row_on_a_store_without_session_columns(tmp_path):
    store = Store(tmp_path / "plain.db")
    common = {
        "alias": "auto", "client": "", "conversation_key": "k",
        "state_builder": "summary_v1", "answers": {}, "features": {},
        "config_hash": "h", "model": "big", "effort": "low", "rule": "r",
        "mode": "active", "fallback": False, "jev_ms": None, "jev_tokens": None,
        "est_tokens": 1, "message_count": 1,
    }
    pinned_first = store.log_decision(decision_id="d", pinned=True, **common)
    unpinned = store.log_decision(decision_id="d", pinned=False, **common)
    store.log_decision(decision_id="d", pinned=True, **common)
    assert store.deciding_decision("d")["id"] == unpinned
    assert store.deciding_decision("only-pinned") is None
    store.log_decision(decision_id="p", pinned=True, **common)
    assert store.deciding_decision("p")["pinned"] is True
    assert pinned_first < unpinned
    store.close()


# --- 2. config snapshots --------------------------------------------------


def snapshot_rows(path) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT config_hash, first_seen, text FROM config_snapshots").fetchall()
    finally:
        conn.close()


@respx.mock
async def test_snapshot_is_written_once_scrubbed_and_loads(strict):
    svc, path = strict
    original = Path(path).read_text()
    assert SECRET in original
    await svc.restart()  # a second startup against the same file

    rows = snapshot_rows(svc.path)
    assert len(rows) == 1
    key, _first_seen, text = rows[0]
    assert key == svc.config.config_hash == config_text_hash(original)
    assert SECRET not in text and "<scrubbed>" in text

    loaded = svc.store.load_config_snapshot(key)
    assert isinstance(loaded, RouterConfig)
    assert loaded.config_hash == key
    assert loaded.aliases.keys() == svc.config.aliases.keys()
    assert loaded.providers["vendor"].quota.config["argv"] == ["<scrubbed>"]

    # A restart keeps the first row exactly as it was.
    await svc.restart()
    assert snapshot_rows(svc.path) == rows


def test_a_config_built_in_memory_writes_no_snapshot(tmp_path, env):
    store = Store(tmp_path / "router.db")
    create_app(H.session_config(), store)
    assert store.config_snapshots() == []
    store.close()


def test_snapshot_load_reports_reasons_and_never_raises(tmp_path):
    store = Store(tmp_path / "router.db")
    assert store.load_config_snapshot("0123456789abcdef") == "not_on_file"
    bad = copy.deepcopy(BASE_CONFIG)
    bad["a_section_this_code_never_heard_of"] = {"x": 1}
    import yaml

    store.save_config_snapshot("feedfacefeedface", yaml.safe_dump(bad))
    assert store.load_config_snapshot("feedfacefeedface") == "unloadable: ConfigError"
    store.close()


def test_prune_never_deletes_snapshots(tmp_path, capsys):
    path = write_config(tmp_path, with_quota_command(BASE_CONFIG))
    cfg = load_config(path)
    store = Store(cfg.settings.db_path)
    store.snapshot_running_config(cfg)
    store.log_decision(
        decision_id="old", alias="auto", client="", conversation_key="k",
        state_builder="summary_v1", answers={}, features={}, config_hash=cfg.config_hash,
        model="big", effort="low", rule="r", mode="active", fallback=False, pinned=False,
        jev_ms=None, jev_tokens=None, est_tokens=1, message_count=1,
    )
    store._conn.execute("UPDATE decisions SET ts = ts - 90 * 86400")
    store._conn.execute("UPDATE config_snapshots SET first_seen = first_seen - 90 * 86400")
    store._conn.commit()
    store.close()

    assert main(["-c", path, "prune", "--older-than-days", "30"]) == 0
    assert json.loads(capsys.readouterr().out)["decisions"] == 1
    assert len(snapshot_rows(cfg.settings.db_path)) == 1


def test_import_only_inserts_a_file_some_decision_was_taken_under(tmp_path, capsys):
    path = write_config(tmp_path)
    cfg = load_config(path)
    old = tmp_path / "old-router.yaml"
    old_raw = with_quota_command(BASE_CONFIG)
    old_raw["settings"]["sqlite_path"] = str(cfg.settings.db_path)
    import yaml

    old.write_text("# an older router.yaml\n" + yaml.safe_dump(old_raw))
    old_hash = config_text_hash(old.read_text())
    unused = tmp_path / "unused.yaml"
    unused.write_text(yaml.safe_dump(BASE_CONFIG) + "# never deployed\n")

    store = Store(cfg.settings.db_path)
    store.log_decision(
        decision_id="d1", alias="auto", client="", conversation_key="k",
        state_builder="summary_v1", answers={}, features={}, config_hash=old_hash,
        model="big", effort="low", rule="r", mode="active", fallback=False, pinned=False,
        jev_ms=None, jev_tokens=None, est_tokens=1, message_count=1,
    )
    store.close()

    missing = tmp_path / "missing.yaml"
    code = main(["-c", path, "config-snapshots", "import", str(old), str(unused), str(missing)])
    out = capsys.readouterr().out
    assert code == 1  # the missing file is reported as a failure
    assert f"{old_hash} imported" in out
    assert "skipped, no decision was taken under it" in out
    assert "unreadable" in out

    rows = snapshot_rows(cfg.settings.db_path)
    assert [r[0] for r in rows] == [old_hash]
    assert SECRET not in rows[0][2]

    assert main(["-c", path, "config-snapshots", "import", str(old)]) == 0
    assert "already on file" in capsys.readouterr().out

    assert main(["-c", path, "config-snapshots", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["config_hash"] == old_hash
    assert listed[0]["loads"] is True and listed[0]["decisions"] == 1
    assert "text" not in listed[0]


def test_an_old_database_without_the_new_tables_still_opens(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE pins (conversation_key TEXT PRIMARY KEY, model TEXT NOT NULL,
            effort TEXT, created REAL NOT NULL, decision_id TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL, ts REAL NOT NULL, alias TEXT, client TEXT,
            conversation_key TEXT, state_builder TEXT, answers TEXT, features TEXT,
            config_hash TEXT, model TEXT, effort TEXT, rule TEXT, mode TEXT,
            fallback INTEGER, pinned INTEGER, jev_ms REAL, jev_tokens INTEGER,
            est_tokens INTEGER, message_count INTEGER, upstream_status INTEGER,
            upstream_usage TEXT, state TEXT);
        INSERT INTO decisions (decision_id, ts, alias, answers, features, model, rule,
            fallback, pinned) VALUES ('old1', 1.0, 'auto', '{}', '{}', 'big', 'easy', 0, 0);
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    sessions = Sessions(store, guard=False)
    assert "decisions.state_shape" in sessions.migrated_inspector
    row = store.deciding_decision("old1")
    assert row["model"] == "big" and row["state_shape"] is None
    assert store.config_snapshots() == [] and sessions.bookmarks() == []
    sessions.close()
    store.close()

    # Opening it again adds nothing more.
    store = Store(path)
    sessions = Sessions(store, guard=False)
    assert sessions.migrated_inspector == []
    sessions.close()
    store.close()


def test_the_inspector_map_is_its_own_release():
    assert INSPECTOR_COLUMNS == {"decisions": [("state_shape", "TEXT")]}


# --- 3. state shape -------------------------------------------------------


def test_state_shape_has_counts_and_never_text():
    state = {
        "latest_user_request": "x" * 127,
        "material_the_user_pasted": "",
        "quoted_material": [{"kind": "pasted", "content": "abcd"}],
        "facts": {
            "message_count": 2,
            "contains_code_blocks": True,
            "tool_names": ["a", "b", "c"],
            "code_block_languages": ["py"],
            "client": "pi",
            "truncation": "the head and the tail were kept; the middle was cut",
            "nested": {"x": 1},
        },
    }
    shape = state_shape("continuation_aware_v1", state)
    assert shape == {
        "builder": "continuation_aware_v1",
        "parts": {
            "latest_user_request": 127,
            "material_the_user_pasted": 0,
            "quoted_material": 10,
        },
        "facts": {
            "message_count": 2,
            "contains_code_blocks": True,
            "tool_names": 3,
            "code_block_languages": 1,
            "client": "pi",
        },
    }
    assert state_shape("b", None) is None
    assert state_shape("b", 42) is None
    assert state_shape("b", "some text") == {"builder": "b", "parts": {"state": 9}, "facts": {}}


@respx.mock
async def test_legacy_row_stores_the_shape_without_log_state_and_no_text(tmp_path, env):
    path = write_config(tmp_path)
    cfg = load_config(path)
    assert cfg.settings.log_state is False
    store = Store(cfg.settings.db_path)
    app = create_app(cfg, store)
    mock_jev()
    upstream_ok()
    text = f"Please look at {MARKER} and tell me what it means."
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as client:
        response = await client.post("/v1/chat/completions", json=chat_body(text))
    assert response.status_code == 200
    row = store.recent_decisions(1)[0]
    assert row["state"] is None
    shape = row["state_shape"]
    assert shape["builder"] == "summary_v1"
    assert shape["parts"]["latest_user_request"] == len(text)
    assert shape["facts"]["message_count"] == 1
    await app.state.router.aclose()
    store.close()
    assert MARKER not in dump_db(cfg.settings.db_path)


@respx.mock
async def test_strict_session_shapes_only_the_admission_and_stores_no_text(strict):
    svc, _path = strict
    text = f"Implement the {MARKER} parser with tests."
    caller = await run_strict_session(svc, text=text)
    rows = svc.sessions.decision_rows(caller.session_id)
    assert rows[0]["event_type"] == "admission"
    assert rows[0]["state_shape"]["parts"]["latest_user_request"] == len(text)
    assert all(r["state_shape"] is None for r in rows[1:])
    await svc.close()
    assert MARKER not in dump_db(svc.path)
    svc.open()


# --- 4 and 5. bookmarks and the read helpers ------------------------------


@respx.mock
async def test_read_helpers_for_the_inspector(strict):
    svc, _path = strict
    caller = await run_strict_session(svc)
    sessions = svc.sessions
    sid = caller.session_id

    listed = sessions.audit_sessions(50)
    assert [s["session_id"] for s in listed] == [sid]
    assert listed[0]["requests"] == {"completed": 2}
    assert "owner" not in listed[0] and "fingerprint" not in listed[0]

    one = sessions.audit_session(sid)
    assert one["model_key"] and "owner" not in one
    assert sessions.audit_session("nope") is None

    admission = sessions.admission_row(sid)
    rows = sessions.decision_rows(sid)
    assert admission["id"] == rows[0]["id"]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert [r["event_type"] for r in rows] == ["admission", "execution", "execution"]

    requests = sessions.request_rows(sid)
    assert [r["status"] for r in requests] == ["completed", "completed"]
    assert [r["decision"]["id"] for r in requests] == [rows[1]["id"], rows[2]["id"]]
    assert all("fingerprint" not in r for r in requests)
    assert sessions.all_plans(sid) == []


@respx.mock
async def test_a_rejected_request_has_no_decision_row(strict):
    svc, _path = strict
    caller = await run_strict_session(svc)
    svc.sessions.start_request(caller.session_id, "never-sent", "f" * 64)
    svc.sessions.finish_request(caller.session_id, "never-sent", "rejected")
    last = svc.sessions.request_rows(caller.session_id)[-1]
    assert last["request_id"] == "never-sent" and last["decision"] is None


def test_bookmarks_cli(tmp_path, capsys):
    path = write_config(tmp_path)
    cfg = load_config(path)
    store = Store(cfg.settings.db_path)
    sessions = Sessions(store, cfg.session_routing, guard=False)
    sessions.create({"session_id": "pi-abcd1234", "owner": "o", "client": "pi", "alias": "auto"})
    sessions.close()
    store.close()

    def run(*argv: str) -> int:
        return main(["-c", path, "bookmarks", *argv])

    assert run("add", "pi-abcd1234", "--label", "Debugging the parser") == 0
    assert "'debugging-the-parser'" in capsys.readouterr().out
    assert run("add", "pi-abcd1234", "--label", "Second", "--slug", "second") == 0
    capsys.readouterr()

    # Validation, each refused with a reason and nothing written.
    assert run("add", "pi-abcd1234", "--label", "Debugging the parser") == 2
    assert "already exists" in capsys.readouterr().err
    assert run("add", "pi-abcd1234", "--label", "x", "--slug", "Bad Slug") == 2
    assert "slug" in capsys.readouterr().err
    assert run("add", "pi-abcd1234", "--label", "x" * 41) == 2
    assert "label" in capsys.readouterr().err
    assert run("add", "pi-abcd1234", "--label", "   ") == 2
    capsys.readouterr()
    assert run("add", "pi-unknown", "--label", "Ghost") == 2
    assert "unknown session" in capsys.readouterr().err
    assert run("add", "pi-abcd1234") == 2
    capsys.readouterr()

    assert run("list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [b["slug"] for b in listed] == ["debugging-the-parser", "second"]
    assert listed[0]["label"] == "Debugging the parser"
    assert listed[0]["session_id"] == "pi-abcd1234"

    assert run("remove", "second") == 0
    capsys.readouterr()
    assert run("remove", "second") == 1
    assert "no bookmark" in capsys.readouterr().err
    assert run("list") == 0
    assert "debugging-the-parser" in capsys.readouterr().out
