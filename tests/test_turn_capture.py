"""Turn text capture: what is stored, where, scrubbed, and how turns are counted."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx
import session_harness as H
from conftest import make_config
from pydantic import ValidationError
from session_harness import ADMIN, Client, ResponsesClient, Service
from test_app import chat_body, mock_jev, upstream_ok
from test_turn_plan import turn_jev

from jev_router import sessions as S
from jev_router.app import create_app
from jev_router.pins import FEEDBACK_SESSION_COLUMNS, SCHEMA, TURN_TEXT_COLUMNS, Store
from jev_router.scrub import SECRET
from jev_router.sessions import SESSION_TEXT_COLUMNS, Sessions

SK = "sk-" + "test-" + "Q1w2E3r4T5y6U7i8O9p0A1s2"
SYSTEM = "You are a coding agent.\nCurrent working directory: /Users/alice/Developer/billing-api\n"
PROMPT = (
    "Fix the flaky retry test in the billing client.\n"
    f"My key is {SK} if you need it.\n\n"
    "Traceback (most recent call last):\n"
    '  File "billing/client.py", line 88, in charge\n'
    "    raise RetryError(attempts)\n"
    "billing.errors.RetryError: 3 attempts\n" + "  at frame\n" * 60
)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)


@pytest.fixture
async def svc(tmp_path, env):
    service = Service(tmp_path)
    service.router.store.log_state = True
    yield service
    await service.close()


def first_request(model: str = "") -> dict:
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT},
        ],
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        "max_tokens": 256,
    }
    if model:
        body["model"] = model
    return body


def call(name: str, args: dict, cid: str) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def with_tool_loop(body: dict, *steps: tuple[str, dict]) -> dict:
    """The same turn after the assistant called each tool in turn."""
    out = json.loads(json.dumps(body))
    for n, (name, args) in enumerate(steps):
        cid = f"c{len(out['messages'])}-{n}"
        out["messages"].append({"role": "assistant", "content": None, "tool_calls": [call(name, args, cid)]})
        out["messages"].append({"role": "tool", "tool_call_id": cid, "content": "ok"})
    return out


def with_answer_and_next_prompt(body: dict, answer: str, prompt: str) -> dict:
    out = json.loads(json.dumps(body))
    out["messages"].append({"role": "assistant", "content": answer})
    out["messages"].append({"role": "user", "content": prompt})
    return out


def rows(svc, session_id: str) -> list[dict]:
    return svc.sessions.decision_rows(session_id)


def dump(store: Store) -> str:
    return "\n".join(store.conn.iterdump())


# --- schema ----------------------------------------------------------------


def test_an_older_database_gains_every_new_column_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executescript(S.SESSION_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (decision_id, ts, model, rule) VALUES ('d-old', 1.0, 'big', 'hard')"
    )
    conn.execute("INSERT INTO feedback (decision_id, ts, verdict) VALUES ('d-old', 1.0, 'right')")
    conn.execute(
        "INSERT INTO sessions (session_id, owner, state, version, created)"
        " VALUES ('session-old-0001', 'o', 'active', 1, 1.0)"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    sessions = Sessions(store, guard=False)
    try:
        assert set(store.migrated_turn_text) == {
            f"decisions.{name}" for name, _ in TURN_TEXT_COLUMNS["decisions"]
        }
        assert set(store.migrated_feedback) == {
            f"feedback.{name}" for name, _ in FEEDBACK_SESSION_COLUMNS["feedback"]
        }
        assert set(sessions.migrated_turn_text) == {
            f"sessions.{name}" for name, _ in SESSION_TEXT_COLUMNS["sessions"]
        }
        assert sessions.migrated_feedback == ["sessions.feedback_verdict"]
        indexes = {row[1] for row in store.conn.execute("PRAGMA index_list(feedback)")}
        assert "feedback_session_id" in indexes
        indexes = {row[1] for row in store.conn.execute("PRAGMA index_list(decisions)")}
        assert "decisions_session" in indexes
        old = store.get_decision("d-old")
        assert old["turn_index"] is None and old["user_prompt"] is None
        session = sessions.get("session-old-0001")
        assert session["title"] is None and session["turn_count"] is None
        # A session that predates the count is on its first turn.
        assert sessions.current_turn("session-old-0001") == 1
    finally:
        sessions.close()
        store.close()
    # Opening it again adds nothing more.
    again = Store(path)
    assert again.migrated_turn_text == [] and again.migrated_feedback == []
    again.close()


def test_settings_defaults_and_the_scrubbing_switch():
    cfg = make_config()
    assert cfg.settings.log_state is True
    assert cfg.settings.scrub_secrets is True
    assert cfg.settings.turn_text_max_chars == 2048
    with pytest.raises(ValidationError, match="scrub_secrets must be true"):
        make_config(settings={"scrub_secrets": False})
    with pytest.raises(ValidationError):
        make_config(settings={"turn_text_max_chars": 10})


def test_the_store_scrubs_and_caps_every_text_it_writes(tmp_path):
    store = Store(tmp_path / "r.db", log_state=True, text_max_chars=300)
    row = store.log_decision(
        decision_id="d", alias="a", client="c", conversation_key="", state_builder="b",
        answers={}, features={}, config_hash="h", model="m", effort=None, rule="r",
        mode="active", fallback=False, pinned=False, jev_ms=None, jev_tokens=None,
        est_tokens=0, message_count=1, state={"latest_user_request": f"use {SK} " + "x " * 500},
        user_prompt=f"use {SK} " + "y " * 500, turn_index=3,
    )
    saved = store.decision_by_row(row)
    assert saved["turn_index"] == 3
    assert saved["user_prompt"].startswith(f"use {SECRET}") and len(saved["user_prompt"]) <= 300
    assert len(saved["state"]["latest_user_request"]) <= 300
    assert SK not in dump(store)
    store.close()


# --- admission -------------------------------------------------------------


@respx.mock
async def test_admission_writes_the_title_preview_workspace_and_first_turn(svc):
    mock_jev(task="code", difficulty=2.0)
    H.upstream()
    caller = Client(svc)
    response = await caller.resolve(request=first_request())
    assert response.status_code == 200, response.text
    session = svc.sessions.get(caller.session_id)
    assert session["title"] == "Fix the flaky retry test in the billing client."
    assert session["prompt_preview"].startswith("Fix the flaky retry test")
    assert SECRET in session["prompt_preview"] and len(session["prompt_preview"]) <= 400
    assert session["workspace_hint"] == "billing-api"
    assert (session["turn_count"], session["escalation_count"], session["corrective_count"]) == (1, 0, 0)

    [admission] = rows(svc, caller.session_id)
    assert admission["event_type"] == "admission" and admission["turn_index"] == 1
    assert admission["user_prompt"].startswith("Fix the flaky retry test")
    assert len(admission["user_prompt"]) <= svc.config.settings.turn_text_max_chars
    assert admission["features"]["user_turns"] == 1
    # The state Jev was shown, stored scrubbed.
    assert "latest_user_request" in admission["state"]
    assert SK not in dump(svc.router.store)
    # The workspace is a basename; the path with a user name in it is not.
    assert "alice" not in json.dumps({k: session[k] for k in ("title", "prompt_preview", "workspace_hint")})


@respx.mock
async def test_with_log_state_off_no_text_is_stored_at_all(tmp_path, env):
    service = Service(tmp_path)  # a Store opened without log_state
    try:
        mock_jev(task="code", difficulty=2.0)
        H.upstream()
        caller = Client(service)
        assert (await caller.resolve(request=first_request())).status_code == 200
        body = with_answer_and_next_prompt(first_request(caller.wire_model()), "Done.", "Now the docs.")
        assert (await caller.execute(body)).status_code == 200
        session = service.sessions.get(caller.session_id)
        assert session["title"] is None and session["prompt_preview"] is None
        assert session["workspace_hint"] is None
        assert session["turn_count"] == 2  # counting is not text
        for row in rows(service, caller.session_id):
            assert row["user_prompt"] is None and row["state"] is None
            assert row["assistant_summary"] is None and row["tool_calls_json"] is None
        assert "flaky" not in dump(service.router.store)
    finally:
        await service.close()


# --- execution: the tool loop and new user turns -------------------------------


@respx.mock
async def test_a_tool_loop_stays_one_turn_and_a_new_prompt_opens_the_next(svc):
    mock_jev(task="code", difficulty=2.0)
    seen = H.upstream()
    caller = Client(svc)
    assert (await caller.resolve(request=first_request())).status_code == 200
    model = caller.wire_model()
    turn1 = first_request(model)
    loop = [turn1]
    loop.append(with_tool_loop(turn1, ("read_file", {"path": "billing/client.py"})))
    loop.append(with_tool_loop(loop[-1], ("bash", {"command": "pytest -q tests/test_retry.py"})))
    loop.append(with_tool_loop(loop[-1], ("edit_file", {"file_path": "billing/client.py"})))
    for body in loop:
        assert (await caller.execute(body)).status_code == 200

    admission_row = rows(svc, caller.session_id)[0]
    calls = admission_row["tool_calls_json"]
    assert calls["tool_calls"] == [
        {"name": "read_file", "count": 1}, {"name": "bash", "count": 1}, {"name": "edit_file", "count": 1},
    ]
    assert calls["files_touched"] == ["billing/client.py"]
    assert svc.sessions.current_turn(caller.session_id) == 1

    turn2 = with_answer_and_next_prompt(loop[-1], f"Fixed it; the key {SK} was not needed.", "Now add a changelog entry.")
    assert (await caller.execute(turn2)).status_code == 200
    assert (await caller.execute(with_tool_loop(turn2, ("edit_file", {"path": "CHANGELOG.md"})))).status_code == 200

    every = rows(svc, caller.session_id)
    kinds = [(r["event_type"], r["turn_index"]) for r in every]
    assert kinds == [
        ("admission", 1), ("execution", 1), ("execution", 1), ("execution", 1), ("execution", 1),
        ("user_turn", 2), ("execution", 2), ("execution", 2),
    ]
    user_turn = every[5]
    assert user_turn["user_prompt"] == "Now add a changelog entry."
    assert user_turn["answers"] == {} and user_turn["rule"] == "user_turn"
    assert user_turn["jev_ms"] is None
    assert user_turn["request_id"] == "execution-0005"
    # The opening request carried turn 1 in full, so turn 1 has its last word.
    first = svc.store.decision_by_row(every[0]["id"])
    assert first["assistant_summary"] == f"Fixed it; the key {SECRET} was not needed."
    second = svc.store.decision_by_row(every[5]["id"])
    assert second["tool_calls_json"]["tool_calls"] == [{"name": "edit_file", "count": 1}]
    assert svc.sessions.get(caller.session_id)["turn_count"] == 2
    assert len(seen) == 6  # no extra provider call for any of this
    assert SK not in dump(svc.router.store)


@respx.mock
async def test_a_repeated_request_id_never_opens_a_second_turn(svc):
    mock_jev(task="code", difficulty=2.0)
    caller = Client(svc)
    H.upstream()
    assert (await caller.resolve(request=first_request())).status_code == 200
    body = first_request(caller.wire_model())
    assert (await caller.execute(body)).status_code == 200
    respx.post(H.CHAT).mock(return_value=httpx.Response(503, json={"error": "busy"}))
    turn2 = with_answer_and_next_prompt(body, "Done.", "And the docs?")
    rejected = await caller.execute(turn2, request_id="retry-me")
    assert rejected.status_code == 503
    H.upstream()
    again = await caller.execute(turn2, request_id="retry-me")
    assert again.status_code == 200, again.text
    assert svc.sessions.current_turn(caller.session_id) == 2
    turn_rows = [r for r in rows(svc, caller.session_id) if r["event_type"] == "user_turn"]
    assert len(turn_rows) == 1
    # A fresh id with the same transcript is not a new turn either.
    assert (await caller.execute(turn2)).status_code == 200
    assert svc.sessions.current_turn(caller.session_id) == 2


# --- turn plans ---------------------------------------------------------------


@respx.mock
async def test_a_turn_plan_opens_its_turn_with_prompt_state_and_counts(tmp_path, env):
    service = Service(tmp_path, config=H.adaptive_config(tmp_path))
    service.router.store.log_state = True
    try:
        turn_jev(difficulty=1.9, corrective=0.9)
        H.responses_upstream()
        caller = ResponsesClient(service)
        assert (await caller.start()).status_code == 200
        caller.items.append(H.user_item("Add quoted CSV fields."))
        assert (await caller.send()).status_code == 200

        plan, sent = await caller.turn(f"That broke the escaped quotes; token {SK}.")
        assert plan["action"] == "change_effort", plan
        assert sent.status_code == 200, sent.text

        every = rows(service, caller.session_id)
        kinds = [(r["event_type"], r["turn_index"]) for r in every]
        # The plan opened turn 2, so the execution that carried it did not.
        assert kinds == [("admission", 1), ("execution", 1), ("effort_plan", 2), ("execution", 2)]
        plan_row = every[2]
        assert plan_row["user_prompt"] == f"That broke the escaped quotes; token {SECRET}."
        assert plan_row["state"]["current_user_request"] == plan_row["user_prompt"]
        session = service.sessions.get(caller.session_id)
        assert session["turn_count"] == 2
        assert session["escalation_count"] == 1 and session["corrective_count"] == 1
        # The Responses observer's counts land on the execution row.
        assert every[3]["upstream_usage"]["input_tokens"] == 1200
        assert SK not in dump(service.router.store)
    finally:
        await service.close()


@respx.mock
async def test_with_the_experiment_off_a_turn_plan_records_nothing_and_execution_counts_it(tmp_path, env):
    service = Service(tmp_path, config=H.adaptive_config(tmp_path, mode="off"))
    service.router.store.log_state = True
    try:
        turn_jev()
        H.responses_upstream()
        caller = ResponsesClient(service)
        assert (await caller.start()).status_code == 200
        caller.items.append(H.user_item("Add quoted CSV fields."))
        assert (await caller.send()).status_code == 200
        plan, sent = await caller.turn("Now the escaped quotes.")
        assert plan["plan_id"] == "" and sent.status_code == 200
        kinds = [(r["event_type"], r["turn_index"]) for r in rows(service, caller.session_id)]
        assert kinds == [("admission", 1), ("execution", 1), ("user_turn", 2), ("execution", 2)]

        # A compaction carries a summarising user message; it is not a turn.
        assert (await caller.compact()).status_code == 200
        assert service.sessions.current_turn(caller.session_id) == 2
        compaction = rows(service, caller.session_id)[-1]
        assert compaction["event_type"] == "execution" and compaction["turn_index"] == 2
        assert compaction["features"]["user_turns"] is None
        # The next real prompt after it still opens a turn.
        plan, sent = await caller.turn("Now the CRLF case.")
        assert sent.status_code == 200
        assert service.sessions.current_turn(caller.session_id) == 3
    finally:
        await service.close()


# --- legacy aliases, pruning and the contract -----------------------------------


@respx.mock
async def test_a_legacy_alias_row_keeps_its_prompt_and_nothing_else(tmp_path, env):
    store = Store(tmp_path / "r.db", log_state=True)
    app = create_app(make_config(), store)
    mock_jev()
    upstream_ok()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
        response = await client.post("/v1/chat/completions", json=chat_body(f"hello {SK}"))
    assert response.status_code == 200
    row = store.recent_decisions(1)[0]
    assert row["user_prompt"] == f"hello {SECRET}"
    assert row["turn_index"] is None and row["assistant_summary"] is None
    await app.state.router.aclose()
    store.close()


@respx.mock
async def test_pruning_a_session_drops_its_text_with_the_binding(svc):
    mock_jev(task="code", difficulty=2.0)
    H.upstream()
    caller = Client(svc)
    assert (await caller.resolve(request=first_request())).status_code == 200
    svc.sessions.clock = lambda: 4_000_000_000.0
    svc.sessions.prune(60)
    row = svc.sessions.get(caller.session_id)
    assert row["tombstone"] and row["title"] is None
    assert row["prompt_preview"] is None and row["workspace_hint"] is None


def test_the_contract_names_what_quota_steered_away_from():
    row = {
        "session_id": "s", "state": "active", "binding_revision": "b", "decision_id": "d",
        "model_key": "gpt-6-sol", "provider": "p", "wire_model": "w", "protocol": "openai-chat",
        "context_window": 1, "max_output_tokens": 1, "supports_tools": True,
        "supports_vision": False, "base_effort": "high", "effective_effort": "high",
        "effort_mode": "fixed", "decision_rule": "hard", "decision_source": "jev",
        "quality_lane": None, "estimated_input_tokens": 1, "estimate_method": "m",
        "reserve_tokens": 1, "quota_changed_choice": 1,
    }
    admission = {"model": "gpt-6-sol", "effort": "high", "counterfactual": "gpt-6-astra(medium)"}
    execution = S.contract(row, {}, admission)["execution"]
    assert execution["quota_steered"] is True
    assert (execution["intended_model"], execution["intended_effort"]) == ("gpt-6-astra", "medium")
    row["quota_changed_choice"] = 0
    execution = S.contract(row, {}, admission)["execution"]
    assert execution["quota_steered"] is False
    assert (execution["intended_model"], execution["intended_effort"]) == ("gpt-6-sol", "high")
    # No admission row (a session written before it): the binding itself.
    assert S.contract(row, {})["execution"]["intended_model"] == "gpt-6-sol"


@respx.mock
async def test_resolve_and_resume_carry_the_new_contract_fields(svc):
    mock_jev(task="code", difficulty=2.0)
    H.upstream()
    caller = Client(svc)
    first = (await caller.resolve(request=first_request())).json()["execution"]
    assert first["quota_steered"] is False
    assert first["intended_model"] == first["model_key"]
    assert first["intended_effort"] == first["base_effort"]
    resumed = (await caller.resume()).json()["execution"]
    assert resumed["intended_model"] == first["intended_model"]
