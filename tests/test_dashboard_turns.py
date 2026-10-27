"""The turn-grouped session detail and the session list's new fields."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Client, Service
from test_app import mock_jev

from jev_router.dashboard import build_turns, execution_rollup, project_turn_state
from jev_router.scrub import SECRET

SK = "sk-" + "dash-" + "P0o9I8u7Y6t5R4e3W2q1A2s3"
USAGE = {
    "id": "cmpl-1", "choices": [],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
              "prompt_tokens_details": {"cached_tokens": 800}, "note": "PRIVATE-USAGE"},
}


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    service = Service(tmp_path)
    service.router.store.log_state = True
    yield service
    await service.close()


def call(name: str, args: dict, cid: str) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


async def coding_session(svc) -> Client:
    """Turn 1: a three-request tool loop. Turn 2: two requests."""
    mock_jev(task="code", difficulty=2.0)
    H.upstream(httpx.Response(200, json=USAGE))
    caller = Client(svc)
    first = {"messages": [{"role": "user", "content": f"Fix the retry loop in billing ({SK})."}],
             "max_tokens": 256}
    assert (await caller.resolve(request=first)).status_code == 200
    body = {"model": caller.wire_model(), **first}
    assert (await caller.execute(body)).status_code == 200
    for n, (name, args) in enumerate([("read_file", {"path": "billing/retry.py"}),
                                      ("bash", {"command": "pytest -q"})]):
        body = json.loads(json.dumps(body))
        body["messages"] += [
            {"role": "assistant", "content": None, "tool_calls": [call(name, args, f"c{n}")]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": "ok"},
        ]
        assert (await caller.execute(body)).status_code == 200
    body = json.loads(json.dumps(body))
    body["messages"] += [{"role": "assistant", "content": "Fixed: the backoff never reset."},
                         {"role": "user", "content": "Great. Now add a test for it."}]
    assert (await caller.execute(body)).status_code == 200
    body["messages"] += [
        {"role": "assistant", "content": None, "tool_calls": [call("edit_file", {"path": "tests/test_retry.py"}, "c9")]},
        {"role": "tool", "tool_call_id": "c9", "content": "ok"},
    ]
    assert (await caller.execute(body)).status_code == 200
    return caller


async def get(svc, path: str) -> httpx.Response:
    response = await svc.client.get(path, headers={"x-router-admin-token": ADMIN})
    assert response.status_code == 200, response.text
    return response


@respx.mock
async def test_the_detail_groups_requests_into_user_turns(svc):
    caller = await coding_session(svc)
    await svc.client.post(f"/router/sessions/{caller.session_id}/feedback",
                          json={"verdict": "too_weak", "note": "slow to find it"},
                          headers={"x-router-admin-token": ADMIN})
    conn = svc.router.store.conn
    before = conn.total_changes
    response = await get(svc, f"/router/audit/sessions/{caller.session_id}")
    assert conn.total_changes == before  # a read writes nothing
    body = response.json()

    assert body["turn_join"] == "by_index"
    first, second = body["turns"]
    assert (first["turn_index"], first["kind"], second["turn_index"], second["kind"]) == (
        1, "admission", 2, "user_turn")
    assert first["user_prompt"] == f"Fix the retry loop in billing ({SECRET})."
    assert first["state"]["latest_user_request"] == first["user_prompt"]
    assert isinstance(first["state"]["facts"], dict)
    assert first["jev"]["answers"]["difficulty"]["score"] == 2.0
    assert isinstance(first["jev"]["latency_ms"], float)
    assert first["decision"]["rule"] == "hard" and first["plan"] is None
    assert first["requests"] == [1, 2, 3]
    rollup = first["execution_rollup"]
    assert rollup["http_requests"] == 3 and rollup["usage_reported"] == 3
    assert (rollup["input_tokens"], rollup["cached_input_tokens"], rollup["output_tokens"]) == (3000, 2400, 150)
    assert rollup["tool_calls"] == [{"name": "read_file", "count": 1}, {"name": "bash", "count": 1}]
    assert rollup["assistant_summary"] == "Fixed: the backoff never reset."
    assert rollup["files_touched"] == ["billing/retry.py"]
    assert rollup["first_byte_ms_p50"] is not None and rollup["tps_p50"] is not None

    assert second["user_prompt"] == "Great. Now add a test for it."
    assert second["jev"] is None and second["state"] is None
    assert second["requests"] == [4, 5]
    assert second["execution_rollup"]["tool_calls"] == [{"name": "edit_file", "count": 1}]
    assert [f["verdict"] for f in second["feedback"]] == ["too_weak"]

    session = body["session"]
    assert session["title"] == f"Fix the retry loop in billing ({SECRET})."
    assert session["turn_count"] == 2 and session["request_count"] == 5
    assert session["cache_hit_ratio"] == pytest.approx(0.8)
    assert session["feedback"]["verdict"] == "too_weak" and session["feedback"]["turn_index"] == 2
    assert session["feedback"]["note"] == "slow to find it"
    assert [r["turn_index"] for r in body["requests"]] == [1, 1, 1, 2, 2]
    assert body["requests"][0]["facts"]["user_turns"] == 1

    for word in ("conversation_key", "owner", "fingerprint", "PRIVATE-USAGE", "upstream_usage", SK):
        assert word not in response.text, word


@respx.mock
async def test_the_list_carries_titles_and_q_searches_them(svc):
    caller = await coding_session(svc)
    listed = (await get(svc, "/router/audit/sessions")).json()
    [row] = listed["sessions"]
    assert row["title"] == f"Fix the retry loop in billing ({SECRET})."
    assert row["prompt_preview"].startswith("Fix the retry loop")
    assert row["turn_count"] == 2 and row["workspace_hint"] is None
    assert row["feedback_verdict"] is None
    for query, found in (("retry%20LOOP", True), ("billing%20(", True), ("nowhere%20near", False),
                         (caller.session_id[:12], True)):
        sessions = (await get(svc, f"/router/audit/sessions?q={query}")).json()["sessions"]
        assert bool(sessions) is found, query


def test_rows_written_before_turn_capture_join_by_log_order():
    decisions = [
        {"id": 1, "event_type": "admission", "turn_index": None, "ts": 1.0, "answers": {}},
        {"id": 2, "event_type": "execution", "turn_index": None},
        {"id": 3, "event_type": "effort_plan", "turn_index": None, "decision_id": "p1", "ts": 2.0},
        {"id": 4, "event_type": "execution", "turn_index": None},
        {"id": 5, "event_type": "execution", "turn_index": None},
    ]
    plans = [{"plan_id": "p1", "action": "keep", "from_effort": "low", "to_effort": "low", "facts": {}}]
    turns, joined = build_turns(decisions, {2: 1, 4: 2, 5: 3}, plans, [])
    assert joined == "by_order"
    assert [(t["turn_index"], t["kind"], t["requests"]) for t in turns] == [
        (1, "admission", [1]), (2, "plan", [2, 3]),
    ]
    assert turns[1]["plan"]["action"] == "keep"
    assert turns[0]["user_prompt"] is None and turns[0]["execution_rollup"]["input_tokens"] is None


def test_an_execution_whose_turn_row_is_missing_joins_the_turn_before():
    decisions = [
        {"id": 1, "event_type": "admission", "turn_index": 1},
        {"id": 2, "event_type": "execution", "turn_index": 3},
    ]
    turns, joined = build_turns(decisions, {2: 1}, [], [])
    assert joined == "by_index" and turns[0]["requests"] == [1]


def test_the_rollup_computes_throughput_and_keeps_gaps():
    rows = [
        {"upstream_usage": {"output_tokens": 100, "input_tokens": 10}, "response_ms": 1100.0, "first_byte_ms": 100.0},
        {"upstream_usage": None, "response_ms": 50.0, "first_byte_ms": 40.0},
    ]
    out = execution_rollup(rows, {"tool_calls_json": {"tool_calls": [{"name": "bash", "count": 2}, {"name": "<x>", "count": 1}]}})
    assert out["tps_p50"] == pytest.approx(100.0)
    assert out["input_tokens"] == 10 and out["cached_input_tokens"] is None
    assert out["usage_reported"] == 1 and out["first_byte_ms_p50"] == 40.0
    assert out["tool_calls"] == [{"name": "bash", "count": 2}]
    empty = execution_rollup([], {})
    assert empty["http_requests"] == 0 and empty["output_tokens"] is None and empty["tps_p50"] is None


def test_the_state_projection_keeps_the_named_text_fields_only():
    state = {
        "latest_user_request": "fix it",
        "material_the_user_pasted": "Traceback ...",
        "start_of_system_prompt": "PRIVATE SYSTEM",
        "quoted_material": [{"kind": "pasted_by_the_user", "content": "x", "secret": "PRIVATE"}],
        "observations": [{"source": "harness", "summary": "1 failed", "extra": {"a": 1}}],
        "user_constraints": ["keep the API", 3],
        "facts": {"has_tools": True, "tool_names": ["bash", "a b"], "truncation": "a long sentence about cuts",
                  "client": "pi", "nested": {"x": 1}},
    }
    out = project_turn_state(state)
    assert out == {
        "latest_user_request": "fix it",
        "material_the_user_pasted": "Traceback ...",
        "user_constraints": ["keep the API"],
        "quoted_material": [{"kind": "pasted_by_the_user", "content": "x"}],
        "observations": [{"source": "harness", "summary": "1 failed"}],
        "facts": {"has_tools": True, "tool_names": ["bash"], "client": "pi"},
    }
    assert project_turn_state("text") is None
