"""The audit view is read-only, bounded, authenticated and prompt-free."""

from __future__ import annotations

import httpx
import pytest
import respx
import session_harness as H
from conftest import make_config, write_config
from session_harness import ADMIN, Client, Service
from test_app import mock_jev
from test_decision_log import logged

from jev_router.app import create_app
from jev_router.config import load_config
from jev_router.dashboard import FACT_FIELDS
from jev_router.pins import Store
from jev_router.sessions import Sessions


@pytest.fixture
async def dashboard_app(monkeypatch):
    monkeypatch.setenv("AUDIT_TEST_TOKEN", "test-admin")
    app = create_app(make_config(settings={"admin_token_env": "AUDIT_TEST_TOKEN"}))
    yield app
    await app.state.router.aclose()


def client_for(app, *, local=False, token=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1" if local else "192.0.2.1", 1234)),
        base_url="http://router.test",
        headers={"X-Router-Admin-Token": token} if token else {},
    )


async def test_public_shell_has_no_data_but_data_needs_token(dashboard_app):
    async with client_for(dashboard_app) as client:
        for path in ("/dashboard", "/dashboard/", "/dashboard/assets/dashboard.js", "/dashboard/assets/dashboard.css"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert "test-admin" not in response.text
        assert (await client.get("/dashboard/assets/missing.js")).status_code == 404
        assert (await client.get("/router/audit")).status_code == 401
        assert (await client.get("/router/audit", headers={"X-Router-Admin-Token": "wrong"})).status_code == 401
        # Cross-origin code cannot bypass auth with forwarded headers.
        assert (await client.get("/router/audit", headers={"X-Forwarded-For": "127.0.0.1"})).status_code == 401
    async with client_for(dashboard_app, local=True) as client:
        assert (await client.get("/router/audit")).status_code == 401
    async with client_for(dashboard_app, token="test-admin") as client:
        response = await client.get("/router/audit")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in response.headers
        assert response.json()["decisions"] == []


async def test_without_token_only_direct_loopback_can_read(monkeypatch):
    monkeypatch.delenv("AUDIT_TEST_TOKEN", raising=False)
    app = create_app(make_config(settings={"admin_token_env": "AUDIT_TEST_TOKEN"}))
    try:
        async with client_for(app, local=True) as client:
            assert (await client.get("/router/audit")).status_code == 200
        async with client_for(app) as client:
            assert (await client.get("/router/audit")).status_code == 401
    finally:
        await app.state.router.aclose()


async def test_projection_excludes_debug_state_feedback_and_usage(dashboard_app):
    router = dashboard_app.state.router
    router.store.log_state = True
    logged(router.store, state={"prompt": "PRIVATE PROMPT"}, features={"unsafe": "PRIVATE FEATURE"})
    router.store.add_feedback(decision_id="d1", verdict="right", note="PRIVATE NOTE")
    router.store.update_decision(1, upstream_usage={"unexpected": "PRIVATE USAGE"})
    async with client_for(dashboard_app, token="test-admin") as client:
        response = await client.get("/router/audit")
        assert "PRIVATE" not in response.text
        row = response.json()["decisions"][0]
        assert not {"state", "features", "feedback", "upstream_usage"} & row.keys()
        assert row["answers"]["difficulty"]["score"] == 2.0
        assert row["rule_definition"]["when"] == {"difficulty": {"gte": 1.5}}


async def test_old_config_does_not_receive_current_rule_definition(dashboard_app):
    logged(dashboard_app.state.router.store, config_hash="old-config")
    async with client_for(dashboard_app, token="test-admin") as client:
        row = (await client.get("/router/audit")).json()["decisions"][0]
        assert "rule_definition" not in row


@pytest.mark.parametrize("limit", ["0", "-1", "501", "nope", "1.5"])
async def test_invalid_limit_is_rejected(dashboard_app, limit):
    async with client_for(dashboard_app, token="test-admin") as client:
        assert (await client.get(f"/router/audit?limit={limit}")).status_code == 400


async def test_snapshot_preserves_event_identity_and_reflects_stream_updates(dashboard_app):
    store = dashboard_app.state.router.store
    first = logged(store)
    second = logged(store, pinned=True, rule="pin")  # same decision ID, distinct request
    async with client_for(dashboard_app, token="test-admin") as client:
        rows = (await client.get("/router/audit?limit=2")).json()["decisions"]
        assert [row["id"] for row in rows] == [second, first]
        assert {row["decision_id"] for row in rows} == {"d1"}
        store.update_decision(first, upstream_status=200, accepted=True, stream_state="complete", response_ms=80)
        rows = (await client.get("/router/audit?limit=2")).json()["decisions"]
        assert rows[1]["stream_state"] == "complete"
        assert rows[1]["response_ms"] == 80
        assert len((await client.get("/router/audit?limit=1")).json()["decisions"]) == 1


async def test_strict_identifiers_survive_projection_and_reads_do_not_write(dashboard_app):
    router = dashboard_app.state.router
    row_id = logged(router.store)
    router.sessions.annotate_decision(row_id, event_type="execution", session_id="session-1", request_id="request-1", turn_id="turn-1", quality_lane="lane-1")
    before = router.store.conn.total_changes
    async with client_for(dashboard_app, token="test-admin") as client:
        row = (await client.get("/router/audit")).json()["decisions"][0]
        assert row["session_id"] == "session-1"
        assert row["request_id"] == "request-1"
        assert row["turn_id"] == "turn-1"
        assert row["quality_lane"] == "lane-1"
        assert row["event_type"] == "execution"
    assert router.store.conn.total_changes == before


# --- the session inspector ------------------------------------------------

MARKER = "zqx-inspector-marker-77c1"
TOOL = f"tool-{MARKER}"


def marked_request(model: str = "") -> dict:
    body = H.user_request(model, text=f"Fix the {MARKER} parser and add tests.")
    body["tools"] = [{
        "type": "function",
        "function": {"name": TOOL, "description": f"Reads {MARKER}.", "parameters": {"type": "object"}},
    }]
    return body


def file_config(tmp_path, name: str = "router.yaml", **changes):
    raw = H.session_raw()
    for key, value in changes.items():
        raw["aliases"]["auto-session"][key] = value
    folder = tmp_path / name.replace(".", "-")
    folder.mkdir(exist_ok=True)
    path = write_config(folder, raw)
    return load_config(path)


@pytest.fixture
def strict_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)


async def run_session(svc, *, executions: int = 2, jev_fails: bool = False) -> Client:
    caller = Client(svc)
    if jev_fails:
        respx.post(svc.config.settings.jev_url).mock(return_value=httpx.Response(500))
    else:
        mock_jev(task="code", difficulty=2.0, harm=0.1)
    H.upstream(httpx.Response(200, json={
        "id": "cmpl-1", "choices": [], "usage": {"total_tokens": 42, "note": MARKER},
    }))
    response = await caller.resolve(request=marked_request())
    assert response.status_code == 200, response.text
    for _ in range(executions):
        executed = await caller.execute(marked_request(caller.wire_model()))
        assert executed.status_code == 200, executed.text
    return caller


async def inspect(svc, session_id: str | None = None, **kwargs) -> httpx.Response:
    path = "/router/audit/sessions" + (f"/{session_id}" if session_id else "")
    async with client_for(svc.app, **kwargs) as client:
        return await client.get(path)


@respx.mock
async def test_session_endpoints_need_the_admin_credential(tmp_path, strict_env):
    svc = Service(tmp_path)
    try:
        caller = await run_session(svc, executions=0)
        for sid in (None, caller.session_id):
            assert (await inspect(svc, sid)).status_code == 401
            assert (await inspect(svc, sid, token="wrong")).status_code == 401
            # A token is configured, so loopback is no way around it.
            assert (await inspect(svc, sid, local=True)).status_code == 401
            ok = await inspect(svc, sid, token=ADMIN)
            assert ok.status_code == 200
            assert ok.headers["cache-control"] == "no-store"
            assert "access-control-allow-origin" not in ok.headers
    finally:
        await svc.close()


async def test_session_endpoints_without_a_token_are_loopback_only(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIT_TEST_TOKEN", raising=False)
    app = create_app(make_config(settings={"admin_token_env": "AUDIT_TEST_TOKEN"}))
    try:
        async with client_for(app, local=True) as client:
            assert (await client.get("/router/audit/sessions")).status_code == 200
            assert (await client.get("/router/audit/sessions/nope")).status_code == 404
        async with client_for(app) as client:
            assert (await client.get("/router/audit/sessions")).status_code == 401
            assert (await client.get("/router/audit/sessions/nope")).status_code == 401
    finally:
        await app.state.router.aclose()


@pytest.mark.parametrize("limit", ["0", "201", "nope"])
async def test_session_list_limit_is_bounded(dashboard_app, limit):
    async with client_for(dashboard_app, token="test-admin") as client:
        assert (await client.get(f"/router/audit/sessions?limit={limit}")).status_code == 400
        assert (await client.get("/router/audit/sessions?limit=200")).status_code == 200


async def test_unknown_session_is_404(dashboard_app):
    async with client_for(dashboard_app, token="test-admin") as client:
        response = await client.get("/router/audit/sessions/pi-unknown")
        assert response.status_code == 404
        assert response.json() == {"error": "unknown session"}


@respx.mock
async def test_allowlist_holds_and_reads_write_nothing(tmp_path, strict_env):
    svc = Service(tmp_path, config=file_config(tmp_path))
    try:
        svc.router.store.log_state = True  # the state column now holds the text
        caller = await run_session(svc)
        sid = caller.session_id
        svc.sessions.add_bookmark(sid, "Debugging")
        conn = svc.router.store.conn
        # The private material really is on file, so its absence below means something.
        dump = "\n".join(conn.iterdump())
        assert MARKER in dump

        before = conn.total_changes
        listed = await inspect(svc, token=ADMIN)
        detail = await inspect(svc, sid, token=ADMIN)
        assert conn.total_changes == before
        assert "\n".join(conn.iterdump()) == dump
    finally:
        await svc.close()

    for response in (listed, detail):
        assert response.status_code == 200
        assert MARKER not in response.text
        # The state shape keeps `tool_names` as a count ("names withheld"); never a list.
        assert '"tool_names":[' not in response.text
        for word in ("fingerprint", "owner", "conversation_key", "languages",
                     "system_prompt_chars", "last_user_message_chars", "total_chars",
                     "upstream_usage", "decision_reason", "unknown_parts"):
            assert f'"{word}"' not in response.text, word

    body = listed.json()
    assert body["config_hash"] == svc.config.config_hash
    assert body["bookmarks"] == [
        {"slug": "debugging", "label": "Debugging", "session_id": sid, "created": body["bookmarks"][0]["created"]}
    ]
    [row] = body["sessions"]
    assert row["session_id"] == sid and row["requests"] == {"completed": 2}
    assert row["rule"] == "hard" and row["model"] == "big" and row["effort"] == "high"
    assert row["config_matches"] is True

    body = detail.json()
    admission = body["admission"]
    assert not {"state", "features"} & admission.keys()
    assert admission["answers"]["difficulty"]["score"] == 2.0
    assert admission["facts"]["has_tools"] is True and admission["facts"]["tool_count"] == 1
    assert set(admission["facts"]) <= set(FACT_FIELDS)
    assert admission["state_shape"]["facts"]["tool_names"] == 1
    assert body["session"]["model"] == "big" and body["session"]["state"] == "active"
    assert len(body["requests"]) == 2
    for entry in body["requests"]:
        assert entry["usage_reported"] is True
        assert "answers" not in entry and "shadow_answers" not in entry
        assert entry["facts"]["has_tools"] is True
        assert entry["row"] is not None


@respx.mock
async def test_rejected_and_unknown_requests_appear_with_null_metrics(tmp_path, strict_env):
    svc = Service(tmp_path)
    try:
        caller = await run_session(svc, executions=1)
        for request_id, status in (("never-sent", "rejected"), ("lost", "unknown")):
            svc.sessions.start_request(caller.session_id, request_id, "f" * 64)
            svc.sessions.finish_request(caller.session_id, request_id, status)
        body = (await inspect(svc, caller.session_id, token=ADMIN)).json()
    finally:
        await svc.close()
    assert [(r["seq"], r["status"]) for r in body["requests"]] == [
        (1, "completed"), (2, "rejected"), (3, "unknown"),
    ]
    for entry in body["requests"][1:]:
        for key in ("row", "ts", "model", "effort", "stream_state", "accepted",
                    "first_byte_ms", "response_ms", "response_bytes", "usage_reported", "facts"):
            assert entry[key] is None, key
    assert body["requests"][0]["row"] is not None


@respx.mock
async def test_trace_under_the_running_config_reproduces(tmp_path, strict_env):
    svc = Service(tmp_path, config=file_config(tmp_path))
    try:
        caller = await run_session(svc, executions=0)
        body = (await inspect(svc, caller.session_id, token=ADMIN)).json()
    finally:
        await svc.close()
    trace = body["trace"]
    assert trace["available"] is True and trace["source"] == "running"
    assert trace["config_hash"] == svc.config.config_hash
    assert trace["reproduces"] is True
    assert trace["matched"] == "hard" and trace["stored"]["rule"] == "hard"
    assert trace["replayed"] == {"model": "big", "effort": "high", "rule": "hard", "lane": None}
    outcomes = {r["name"]: r["outcome"] for r in trace["rules"]}
    assert outcomes["harmful"] == "miss" and outcomes["hard"] == "hit"
    assert outcomes["easy"] == "not_reached"
    # Today's definition is attached only because the hash matches.
    assert body["admission"]["rule_definition"]["when"] == {"difficulty": {"gte": 1.5}}


@respx.mock
async def test_trace_uses_the_snapshot_when_the_config_has_changed(tmp_path, strict_env):
    old = file_config(tmp_path)
    svc = Service(tmp_path, config=old)
    caller = await run_session(svc, executions=0)
    await svc.close()
    # A new router.yaml in which the same answers would route elsewhere.
    raw = H.session_raw()
    raw["policy"]["rules"][1]["when"] = {"difficulty": {"gte": 2.5}}
    folder = tmp_path / "new"
    folder.mkdir()
    new = load_config(write_config(folder, raw))
    assert new.config_hash != old.config_hash
    svc = Service(tmp_path, config=new)
    try:
        body = (await inspect(svc, caller.session_id, token=ADMIN)).json()
    finally:
        await svc.close()
    trace = body["trace"]
    assert trace["available"] is True and trace["source"] == "snapshot"
    assert trace["config_hash"] == old.config_hash != body["config_hash"]
    assert trace["matched"] == "hard" and trace["reproduces"] is True
    hard = next(r for r in trace["rules"] if r["name"] == "hard")
    assert hard["conditions"][0]["threshold"] == 1.5  # the old threshold, not 2.5
    assert "rule_definition" not in body["admission"]
    assert body["session"]["config_hash"] == old.config_hash


@respx.mock
async def test_trace_is_unavailable_without_the_config_it_was_taken_under(tmp_path, strict_env):
    # Built in memory: no file text, so no snapshot was ever written.
    svc = Service(tmp_path)
    caller = await run_session(svc, executions=0)
    await svc.close()
    svc = Service(tmp_path, config=file_config(tmp_path))
    try:
        body = (await inspect(svc, caller.session_id, token=ADMIN)).json()
    finally:
        await svc.close()
    trace = body["trace"]
    assert trace["available"] is False and trace["reason"] == "not_on_file"
    assert "rules" not in trace and "matched" not in trace
    assert trace["stored"]["rule"] == "hard"
    assert body["admission"]["rule"] == "hard"


@respx.mock
async def test_trace_is_not_run_for_a_fallback_admission_or_missing_answers(tmp_path, strict_env):
    svc = Service(tmp_path, config=file_config(tmp_path))
    try:
        fallback = await run_session(svc, executions=0, jev_fails=True)
        body = (await inspect(svc, fallback.session_id, token=ADMIN)).json()
        assert body["admission"]["rule"] == "admission_fallback"
        assert body["trace"]["available"] is False
        assert body["trace"]["reason"] == "fallback_admission"

        other = Client(svc, session_id="session-noanswers")
        mock_jev(task="code", difficulty=2.0, harm=0.1)
        assert (await other.resolve(request=marked_request())).status_code == 200
        row = svc.sessions.admission_row(other.session_id)
        svc.router.store.conn.execute("UPDATE decisions SET answers = '{}' WHERE id = ?", (row["id"],))
        body = (await inspect(svc, other.session_id, token=ADMIN)).json()
        assert body["trace"] == {
            "available": False, "reason": "no_answers",
            "config_hash": svc.config.config_hash,
            "stored": {"model": "big", "effort": "high", "rule": "hard"},
        }
    finally:
        await svc.close()


async def test_a_session_with_no_admission_row(tmp_path, strict_env):
    store = Store(tmp_path / "router.db")
    sessions = Sessions(store, make_config().session_routing, guard=False)
    sessions.create({"session_id": "pi-bare", "owner": "o", "client": "pi", "alias": "auto"})
    sessions.close()
    store.close()
    svc = Service(tmp_path)
    try:
        body = (await inspect(svc, "pi-bare", token=ADMIN)).json()
    finally:
        await svc.close()
    assert body["admission"] is None
    assert body["trace"] == {"available": False, "reason": "no_admission_row"}
    assert body["requests"] == [] and body["plans"] == []


@respx.mock
async def test_plans_keep_expected_and_confirmed_effort_apart(tmp_path, strict_env):
    svc = Service(tmp_path)
    try:
        caller = await run_session(svc, executions=0)
        sid = caller.session_id
        for n, (expected, confirmed, status) in enumerate(
            [("high", "high", "confirmed"), ("xhigh", None, "planned")], start=1
        ):
            svc.sessions.record_plan(sid, {
                "plan_id": f"plan-{n}", "turn_id": f"turn-{n}", "sequence": n,
                "mode": "active", "action": "change", "status": status,
                "from_effort": "medium", "to_effort": expected, "base_effort": "medium",
                "recommendation": "up", "expected_effort": expected,
                "confirmed_effort": confirmed, "request_id": f"plan-req-{n}",
                "request_fingerprint": "f" * 64, "anchor_prefix_hash": "a" * 64,
                "reason": "medium -> high: the work is harder than its effort",
                "facts": {
                    "strategy": "native_configuration_update",
                    "shadow_counterfactual": {
                        "question": "failure_mode", "answer": "reasoning_problem",
                        "action": "change", "to_effort": "xhigh", "recommendation": "up",
                        "reason": "x", "changed": True, "extra": MARKER,
                    },
                },
            })
        response = await inspect(svc, sid, token=ADMIN)
    finally:
        await svc.close()
    assert MARKER not in response.text
    assert "request_fingerprint" not in response.text and "anchor_prefix_hash" not in response.text
    plans = response.json()["plans"]
    assert [(p["expected_effort"], p["confirmed_effort"]) for p in plans] == [
        ("high", "high"), ("xhigh", None),
    ]
    assert plans[1]["base_effort"] == "medium"
    assert plans[0]["shadow_counterfactual"]["answer"] == "reasoning_problem"
    assert "extra" not in plans[0]["shadow_counterfactual"]
    assert "strategy" not in plans[0]


def test_quota_and_shape_projections_drop_anything_textual():
    from jev_router.dashboard import project_facts, project_quota, project_state_shape

    quota = project_quota({
        "openai": {"status": "fresh", "pressure": 0.2, "age_seconds": 3.0, "reason": MARKER,
                   "windows": [{"name": "weekly", "used_percent": 20.0, "resets_at": 1.0,
                                "label": MARKER}, {"name": f"a {MARKER} b", "used": 1}]},
        f"bad {MARKER}": {"status": "fresh"},
        "x": {"status": MARKER, "pressure": "high"},
    })
    assert quota == {
        "openai": {"status": "fresh", "pressure": 0.2, "age_seconds": 3.0,
                   "windows": [{"used_percent": 20.0, "resets_at": 1.0, "name": "weekly"}, {"used": 1}]},
        "x": {"status": None, "pressure": None, "age_seconds": None, "windows": []},
    }
    shape = project_state_shape({
        "builder": "summary_v1", "parts": {"latest_user_request": 12, MARKER + " x": 3, "bad": MARKER},
        "facts": {"client": "pi", "tool_names": 3, "flag": True, "note": MARKER, "nested": {"a": 1}},
    })
    assert shape == {"builder": "summary_v1", "parts": {"latest_user_request": 12},
                     "facts": {"client": "pi", "tool_names": 3, "flag": True}}
    assert project_facts({"has_tools": True, "estimate_method": f"a {MARKER}", "tool_names": ["x"],
                          "session_mode": "strict"}) == {"has_tools": True, "session_mode": "strict"}
