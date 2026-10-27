"""The config workbench: read, preview, apply, the Jev sandbox and eval export.

The preview is a pure replay, so it must spend no Jev call and write nothing.
Apply writes router.yaml and a backup and swaps the running config. The
sandbox spends exactly one Jev call and writes nothing either.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx
import session_harness as H
import yaml
from conftest import make_config
from session_harness import ADMIN, Client, Service
from test_app import JEV_URL, jev_answers
from test_dashboard import client_for, file_config, strict_env  # noqa: F401 - fixture

from jev_router.app import create_app
from jev_router.cli import main
from jev_router.config import config_text_hash, load_config

SECRET = "sk-live-0123456789abcdefghij"


def jev(**answers):
    return respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=jev_answers(**answers)))


async def admit(svc, session_id: str, **answers) -> Client:
    jev(**answers)
    H.upstream(httpx.Response(200, json={"id": "c", "choices": [], "usage": {"total_tokens": 1}}))
    caller = Client(svc, session_id=session_id)
    response = await caller.resolve(request=H.user_request())
    assert response.status_code == 200, response.text
    return caller


def admission(svc, session_id: str) -> dict:
    return svc.sessions.admission_row(session_id)


async def call(svc, method: str, path: str, body=None, **kwargs) -> httpx.Response:
    async with client_for(svc.app, token=kwargs.pop("token", ADMIN), **kwargs) as client:
        if method == "GET":
            return await client.get(path)
        return await client.post(path, json=body)


def draft_text(path: str, hard_at: float) -> str:
    """The running file with the `hard` cutoff moved."""
    raw = yaml.safe_load(Path(path).read_text())
    for rule in raw["policy"]["rules"]:
        if rule["name"] == "hard":
            rule["when"]["difficulty"]["gte"] = hard_at
    return yaml.safe_dump(raw)


@pytest.fixture
def svc(tmp_path, strict_env):  # noqa: F811 - the fixture imported above
    service = Service(tmp_path, config=file_config(tmp_path))
    yield service


# --- GET /router/config ---------------------------------------------------


@respx.mock
async def test_config_lists_rules_with_lines_and_hits(svc):
    try:
        await admit(svc, "session-aaaa0001", task="code", difficulty=2.0)
        await admit(svc, "session-aaaa0002", task="quick_question", difficulty=0.1)
        response = await call(svc, "GET", "/router/config?window=24h")
        assert response.status_code == 200, response.text
        body = response.json()
        text = Path(svc.config.config_path).read_text()
        assert body["yaml"] == text and body["yaml_source"] == "loaded"
        assert body["config_hash"] == svc.config.config_hash
        assert body["path"] == svc.config.config_path
        lines = text.splitlines()
        rules = {r["name"]: r for r in body["rules"]}
        assert rules["hard"]["hits"] == 1 and rules["easy"]["hits"] == 1
        assert rules["hard"]["share"] == 0.5 and rules["harmful"]["hits"] == 0
        assert "name: hard" in lines[rules["hard"]["line"] - 1]
        assert rules["hard"]["when"] == {"difficulty": {"gte": 1.5}}
        assert body["admissions"] == 2 and body["default"]["hits"] == 0
        assert body["thresholds"] == {"difficulty": [0.5, 1.5], "harm_if_wrong": [0.6]}
        assert body["models"]["big"]["quota_cost"] is None
        assert body["adaptive_effort"]["mode"] == "off"
        assert body["questions"]["difficulty"]["max"] == 2
        # The running wording, for the Jev sandbox to start from.
        assert body["questions"]["difficulty"]["instructions"] == svc.config.questions["difficulty"]["instructions"]
        for query in ("window=2h", "nope=1"):
            assert (await call(svc, "GET", "/router/config?" + query)).status_code == 400
        assert (await call(svc, "GET", "/router/config", token="wrong")).status_code == 401
    finally:
        await svc.close()


async def test_config_never_serves_a_snapshot(monkeypatch):
    monkeypatch.setenv("WB_TOKEN", "t")
    app = create_app(make_config(settings={"admin_token_env": "WB_TOKEN"}))
    try:
        async with client_for(app, token="t") as client:
            body = (await client.get("/router/config")).json()
        assert body["yaml"] is None and body["yaml_source"] is None and body["path"] is None
        assert body["rules"][0]["line"] is None
    finally:
        await app.state.router.aclose()


# --- POST /router/config/preview -------------------------------------------


async def test_invalid_drafts_come_back_with_lines(svc):
    try:
        response = await call(svc, "POST", "/router/config/preview", {"yaml": "a: 1\n  b: 2\n"})
        assert response.status_code == 422
        body = response.json()
        assert body["valid"] is False and body["errors"][0]["line"] == 2

        text = Path(svc.config.config_path).read_text()
        raw = yaml.safe_load(text)
        raw["policy"]["rules"][1]["use"] = {"model": "nope"}
        bad = yaml.safe_dump(raw)
        response = await call(svc, "POST", "/router/config/preview", {"yaml": bad})
        assert response.status_code == 422
        [error] = response.json()["errors"]
        # A problem named by rule name lands on that rule's `use:` key.
        assert error["path"] == "policy.rules[hard].use"
        lines = bad.splitlines()
        assert lines[error["line"] - 1].strip() == "use:"
        assert "nope" in lines[error["line"]]

        raw = yaml.safe_load(text)
        raw["aliases"]["auto-session"]["state_builder"] = "nope_v9"
        bad = yaml.safe_dump(raw)
        errors = (await call(svc, "POST", "/router/config/preview", {"yaml": bad})).json()["errors"]
        [error] = errors
        assert error["path"] == "aliases.auto-session.state_builder"
        assert "state_builder: nope_v9" in bad.splitlines()[error["line"] - 1]

        for body in ({"yaml": text, "limit": 0}, {"yaml": text, "window": "2h"},
                     {"yaml": text, "extra": 1}, ["not", "an", "object"]):
            assert (await call(svc, "POST", "/router/config/preview", body)).status_code == 400
    finally:
        await svc.close()


@respx.mock
async def test_preview_replays_without_jev_and_scores_feedback(svc):
    try:
        weak = await admit(svc, "session-bbbb0001", task="code", difficulty=2.0)
        strong = await admit(svc, "session-bbbb0002", task="code", difficulty=2.0)
        await admit(svc, "session-bbbb0003", task="quick_question", difficulty=0.1)
        store = svc.router.store
        store.add_feedback(decision_id=admission(svc, weak.session_id)["decision_id"], verdict="too_weak")
        store.add_feedback(decision_id=admission(svc, strong.session_id)["decision_id"], verdict="too_strong")
        route = jev()
        calls = route.call_count
        before = store.conn.total_changes
        draft = draft_text(svc.config.config_path, 2.5)
        response = await call(svc, "POST", "/router/config/preview", {"yaml": draft, "window": "24h"})
        assert store.conn.total_changes == before
        assert route.call_count == calls
    finally:
        await svc.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True and body["config_hash_draft"] == config_text_hash(draft)
    assert (body["evaluated"], body["changed"], body["running_reproduces"]) == (3, 2, 3)
    assert body["tier_share_before"] == pytest.approx({"big": 2 / 3, "small": 1 / 3})
    assert body["tier_share_after"] == body["tier_share_before"]
    assert body["quota_saved_before"]["unpriced"] == 3
    assert body["feedback"] == {
        "rated": 2, "resolved_too_weak": 0, "resolved_too_strong": 1,
        "conflicts": 1, "right_changed": 0, "unranked": 0,
    }
    diffs = {d["session_id"]: d for d in body["diffs"]}
    assert set(diffs) == {weak.session_id, strong.session_id}
    moved = diffs[strong.session_id]
    assert moved["before"] == {"rule": "hard", "route": None, "model": "big", "effort": "high", "lane": None}
    assert moved["after"]["rule"] == "confident_code" and moved["after"]["effort"] == "low"
    assert moved["direction"] == "down" and moved["answers"] == {"difficulty": 2.0, "harm_if_wrong": 0.1}
    assert moved["feedback_verdict"] == "too_strong"
    assert moved["feedback_effect"] == "resolved_too_strong"
    assert diffs[weak.session_id]["feedback_effect"] == "conflicts"
    assert moved["title"] is None


def test_direction_needs_an_order_the_config_gives():
    from jev_router.workbench import direction

    cfg = make_config(models={"big": {"quota_cost": 1.0}, "small": {"quota_cost": 0.1}})
    big_high = {"model": "big", "effort": "high"}
    assert direction(big_high, {"model": "big", "effort": "low"}, cfg) == "down"
    assert direction({"model": "small", "effort": "none"}, big_high, cfg) == "up"
    assert direction(big_high, {"model": "paramy", "effort": "high"}, cfg) == "unranked"
    assert direction(big_high, {"error": "routing_error"}, cfg) == "unranked"
    unpriced = make_config()
    assert direction({"model": "small", "effort": "none"}, big_high, unpriced) == "unranked"


# --- POST /router/config/apply ---------------------------------------------


@respx.mock
async def test_apply_backs_up_writes_and_swaps(svc, tmp_path):
    try:
        path = Path(svc.config.config_path)
        old_text = path.read_text()
        old_hash = svc.router.config.config_hash
        old_quota = svc.router.quota
        draft = draft_text(str(path), 2.5)

        response = await call(svc, "POST", "/router/config/apply", {"yaml": "nope: ["})
        assert response.status_code == 422 and path.read_text() == old_text

        response = await call(svc, "POST", "/router/config/apply", {"yaml": draft})
        assert response.status_code == 200, response.text
        body = response.json()
        new_hash = config_text_hash(draft)
        assert body["applied"] is True and body["config_hash"] == new_hash
        assert body["previous_hash"] == old_hash and body["restart_required"] == []
        backup = Path(body["backup_path"])
        assert backup.parent == tmp_path / "config-backups"
        assert backup.read_text() == old_text
        assert oct(backup.parent.stat().st_mode & 0o777) == "0o700"
        assert path.read_text() == draft
        assert not [p for p in path.parent.iterdir() if ".tmp-" in p.name]
        router = svc.router
        assert router.config.config_hash == new_hash
        assert router.config.config_path == str(path)
        assert router.quota is not old_quota and router.breakers.config is router.config
        assert router.decider.config is router.config
        assert new_hash in {s["config_hash"] for s in router.store.config_snapshots()}

        # The next admission decides under the new cutoff and records its hash.
        caller = await admit(svc, "session-cccc0001", task="code", difficulty=2.0)
        row = admission(svc, caller.session_id)
        assert row["config_hash"] == new_hash and row["rule"] == "confident_code"

        # The same text again changes nothing and writes no backup.
        again = (await call(svc, "POST", "/router/config/apply", {"yaml": draft})).json()
        assert again["applied"] is False and again["unchanged"] is True

        # Somebody edited the file by hand: refuse unless told to overwrite.
        path.write_text(old_text)
        refused = await call(svc, "POST", "/router/config/apply", {"yaml": old_text})
        assert refused.status_code == 409 and refused.json()["on_disk_hash"] == old_hash
        stale = await call(svc, "POST", "/router/config/apply",
                           {"yaml": old_text, "base_hash": old_hash, "force": True})
        assert stale.status_code == 409
        forced = await call(svc, "POST", "/router/config/apply", {"yaml": old_text, "force": True})
        assert forced.status_code == 200 and svc.router.config.config_hash == old_hash
        assert len(list((tmp_path / "config-backups").iterdir())) == 2
    finally:
        await svc.close()


async def test_apply_refuses_a_config_with_no_file(monkeypatch):
    monkeypatch.setenv("WB_TOKEN", "t")
    app = create_app(make_config(settings={"admin_token_env": "WB_TOKEN"}))
    try:
        async with client_for(app, token="t") as client:
            response = await client.post("/router/config/apply", json={"yaml": "a: 1"})
        assert response.status_code == 409
        async with client_for(app) as client:
            assert (await client.post("/router/config/apply", json={"yaml": "a: 1"})).status_code == 401
    finally:
        await app.state.router.aclose()


async def test_apply_reports_settings_that_need_a_restart(svc):
    try:
        raw = yaml.safe_load(Path(svc.config.config_path).read_text())
        raw["settings"]["port"] = 9999
        response = await call(svc, "POST", "/router/config/apply", {"yaml": yaml.safe_dump(raw)})
        assert response.status_code == 200, response.text
        assert response.json()["restart_required"] == ["settings.port"]
    finally:
        await svc.close()


# --- POST /router/sandbox/jev ----------------------------------------------


@respx.mock
async def test_sandbox_asks_jev_once_and_writes_nothing(svc):
    try:
        svc.router.store.log_state = True
        caller = await admit(svc, "session-dddd0001", task="code", difficulty=2.0)
        row = admission(svc, caller.session_id)
        assert row["state"]
        route = jev(task="quick_question", difficulty=0.1)
        calls = route.call_count
        before = svc.router.store.conn.total_changes
        body = {"decision_row": row["id"],
                "questions": {"difficulty": {"instructions": "Probe wording."}}}
        response = await call(svc, "POST", "/router/sandbox/jev", body)
        assert response.status_code == 200, response.text
        assert route.call_count == calls + 1
        sent = json.loads(route.calls.last.request.content)
        assert sent["questions"]["difficulty"]["instructions"] == "Probe wording."
        assert sent["questions"]["difficulty"]["criteria"] == svc.config.questions["difficulty"]["criteria"]
        assert sent["state"] == row["state"]
        result = response.json()
        assert result["original_route"]["rule"] == "hard"
        assert result["sandbox_route"]["rule"] == "easy" and result["sandbox_route"]["model"] == "small"
        assert result["sandbox_answers"]["difficulty"]["score"] == 0.1
        assert result["original_answers"]["difficulty"]["score"] == 2.0
        assert result["overridden"] == ["difficulty"] and result["jev_ms"] is not None

        # A supplied state is scrubbed before it leaves.
        state = {"latest_user_request": f"use token {SECRET} please"}
        response = await call(svc, "POST", "/router/sandbox/jev", {"decision_id": row["decision_id"], "state": state})
        assert response.status_code == 200, response.text
        assert SECRET not in route.calls.last.request.content.decode()
        assert svc.router.store.conn.total_changes == before
        # The running config's wording is untouched.
        assert svc.router.config.questions["difficulty"]["instructions"] != "Probe wording."

        bad = {"decision_row": row["id"], "questions": {"nope": {"instructions": "x"}}}
        assert (await call(svc, "POST", "/router/sandbox/jev", bad)).status_code == 400
        bad = {"decision_row": row["id"], "questions": {"difficulty": {"type": "noul"}}}
        assert (await call(svc, "POST", "/router/sandbox/jev", bad)).status_code == 400
        assert (await call(svc, "POST", "/router/sandbox/jev", {"decision_row": 9999})).status_code == 404
    finally:
        await svc.close()


@respx.mock
async def test_sandbox_without_a_stored_state_says_so(svc):
    try:
        caller = await admit(svc, "session-dddd0002", task="code", difficulty=2.0)
        row = admission(svc, caller.session_id)
        route = jev()
        calls = route.call_count
        response = await call(svc, "POST", "/router/sandbox/jev", {"decision_row": row["id"]})
        assert response.status_code == 422
        assert response.json()["reason"] == "no_stored_state"
        assert route.call_count == calls
    finally:
        await svc.close()


# --- POST /router/eval-cases -----------------------------------------------


@respx.mock
async def test_eval_cases_append_beside_the_database(svc, tmp_path):
    try:
        caller = await admit(svc, "session-eeee0001", task="code", difficulty=2.0)
        row = admission(svc, caller.session_id)
        before = svc.router.store.conn.total_changes
        body = {"session_id": caller.session_id, "verdict": "too_weak",
                "note": f"needs more, key {SECRET}" + "x" * 900}
        response = await call(svc, "POST", "/router/eval-cases", body)
        assert response.status_code == 200, response.text
        saved = response.json()
        path = tmp_path / "golden_cases.jsonl"
        assert saved == {"saved": True, "path": str(path), "total_cases": 1}
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        again = await call(svc, "POST", "/router/eval-cases", {"decision_row": row["id"], "verdict": "right"})
        assert again.json()["total_cases"] == 2
        assert svc.router.store.conn.total_changes == before

        first, second = (json.loads(line) for line in path.read_text().splitlines())
        assert first["expected"] == {"verdict": "too_weak", "min_quality_lane": None, "max_quality_lane": None}
        assert first["chosen"]["rule"] == "hard" and first["chosen"]["model"] == "big"
        assert first["answers"]["difficulty"]["score"] == 2.0
        assert first["config_hash"] == svc.config.config_hash and first["state"] is None
        assert SECRET not in path.read_text() and len(first["note"]) <= 500
        assert second["session_id"] == caller.session_id and second["note"] is None

        for bad in ({"session_id": caller.session_id, "verdict": "meh"},
                    {"session_id": caller.session_id, "verdict": "right", "min_quality_lane": "nope"},
                    {"verdict": "right"}, {"session_id": "a b", "verdict": "right"}):
            assert (await call(svc, "POST", "/router/eval-cases", bad)).status_code == 400
        missing = {"session_id": "session-none", "verdict": "right"}
        assert (await call(svc, "POST", "/router/eval-cases", missing)).status_code == 404
    finally:
        await svc.close()


# --- the CLI ---------------------------------------------------------------


def test_cli_config_show_and_preview(tmp_path, capsys):
    config = file_config(tmp_path)
    path = config.config_path
    assert main(["-c", path, "config", "show"]) == 0
    out = capsys.readouterr().out
    assert "hard" in out and "line=" in out
    draft = tmp_path / "draft.yaml"
    draft.write_text(draft_text(path, 2.5))
    assert main(["-c", path, "config", "preview", str(draft)]) == 0
    assert "0 of 0 admission(s)" in capsys.readouterr().out
    draft.write_text("a: [1,\n")
    assert main(["-c", path, "config", "preview", str(draft)]) == 1
    assert "line 2" in capsys.readouterr().out
    assert main(["-c", path, "config", "preview"]) == 2
    assert load_config(path).config_hash == config.config_hash
    assert os.path.exists(path)
