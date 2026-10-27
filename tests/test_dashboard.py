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
                "decision_id": f"plan-{n}",
                "facts": {
                    "strategy": "native_configuration_update",
                    "evidence": {
                        "difficulty": 2.48, "p_hard": 0.48, "harm_if_wrong": "high",
                        "corrective_followup": 0.21, "failure_mode": None,
                        "short_continuation": False, "missing": ["turn_history", MARKER + " x"],
                        "error": f"timeout quoting {MARKER}",
                    },
                    "thresholds": {"upgrade_difficulty": 1.8, "note": MARKER},
                    "ladder": ["low", "medium", "high"],
                    "shadow_counterfactual": {
                        "question": "failure_mode", "answer": "reasoning_problem",
                        "action": "change", "to_effort": "xhigh", "recommendation": "up",
                        "reason": "x", "changed": True, "extra": MARKER,
                    },
                },
            })
        # The turn's own Jev call, as app.turn_plan logs it.
        row = svc.store.log_decision(
            decision_id="plan-1", alias="auto", client="pi", conversation_key=MARKER,
            state_builder="turn_state_v1",
            answers={"difficulty": {"type": "score", "score": 2.48, "confidence": 0.52}},
            features={"tool_names": [TOOL]}, config_hash="h", model="m", effort="low",
            rule="effort_keep", mode="active", fallback=False, pinned=False, jev_ms=61.8,
            jev_tokens=1317, est_tokens=10, message_count=1,
        )
        svc.sessions.annotate_decision(row, event_type="effort_plan", session_id=sid, turn_id="turn-1")
        response = await inspect(svc, sid, token=ADMIN)
    finally:
        await svc.close()
    assert MARKER not in response.text and TOOL not in response.text
    assert "request_fingerprint" not in response.text and "anchor_prefix_hash" not in response.text
    first = response.json()["plans"][0]
    assert first["judgment"]["answers"]["difficulty"]["score"] == 2.48
    assert first["judgment"]["jev_ms"] == 61.8
    assert "state_shape" in first["judgment"]
    assert "conversation_key" not in first["judgment"]
    evidence = first["evidence"]
    assert evidence["difficulty"] == 2.48 and evidence["harm_if_wrong"] is None
    assert evidence["missing"] == ["turn_history"]
    assert evidence["error"] is True
    assert evidence["thresholds"] == {"upgrade_difficulty": 1.8}
    assert evidence["ladder"] == ["low", "medium", "high"]
    assert response.json()["plans"][1]["judgment"] is None
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


# --- the session list's filters and the overview ----------------------------


async def audit_get(svc, path: str, **kwargs) -> httpx.Response:
    async with client_for(svc.app, **kwargs) as client:
        return await client.get(path)


async def two_sessions(svc) -> tuple[Client, Client]:
    """One session with a lost and a refused request, one clean and pinned."""
    first = await run_session(svc, executions=2)
    for request_id, status in (("never-sent", "rejected"), ("lost", "unknown")):
        svc.sessions.start_request(first.session_id, request_id, "f" * 64)
        svc.sessions.finish_request(first.session_id, request_id, status)
    second = Client(svc, session_id="session-9c1d0e55", client="other-client")
    assert (await second.resolve(request=marked_request())).status_code == 200
    assert (await second.execute(marked_request(second.wire_model()))).status_code == 200
    svc.sessions.add_bookmark(second.session_id, "Clean one")
    return first, second


@respx.mock
async def test_session_list_filters_narrow_and_report_aggregates(tmp_path, strict_env):
    svc = Service(tmp_path, config=file_config(tmp_path))
    try:
        first, second = await two_sessions(svc)

        async def ids(query: str = "") -> list[str]:
            response = await audit_get(svc, "/router/audit/sessions?limit=200" + query, token=ADMIN)
            assert response.status_code == 200, response.text
            return [row["session_id"] for row in response.json()["sessions"]]

        assert set(await ids()) == {first.session_id, second.session_id}
        assert await ids("&problem=unknown") == [first.session_id]
        assert await ids("&problem=rejected") == [first.session_id]
        assert await ids("&problem=any") == [first.session_id]
        assert await ids("&problem=stream_failed") == []
        assert await ids("&problem=fallback") == []
        assert await ids("&pinned=1") == [second.session_id]
        assert await ids("&client=other-client") == [second.session_id]
        assert await ids("&q=9c1d") == [second.session_id]
        assert await ids("&state=closed") == []
        assert set(await ids("&adaptive=off&rule=hard&model=big")) == {first.session_id, second.session_id}
        assert await ids("&window=1h&problem=unknown") == [first.session_id]

        body = (await audit_get(svc, "/router/audit/sessions?limit=1", token=ADMIN)).json()
        assert body["matched"] == 2 and len(body["sessions"]) == 1
        assert {f["value"] for f in body["facets"]["client"]} == {"coding-client", "other-client"}
        assert body["facets"]["rule"] == [{"value": "hard", "n": 2}]
        rows = {
            row["session_id"]: row
            for row in (await audit_get(svc, "/router/audit/sessions", token=ADMIN)).json()["sessions"]
        }
        assert rows[first.session_id]["problems"] == ["unknown", "rejected"]
        assert rows[first.session_id]["requests"] == {"completed": 2, "rejected": 1, "unknown": 1}
        assert rows[first.session_id]["pinned"] is None
        assert rows[second.session_id]["pinned"] == {"slug": "clean-one", "label": "Clean one"}
        assert rows[second.session_id]["problems"] == []
        assert rows[second.session_id]["response_ms"]["n"] == 1
        assert rows[second.session_id]["response_ms"]["p50"] is not None
    finally:
        await svc.close()


@pytest.mark.parametrize("query", [
    "state=gone", "adaptive=maybe", "problem=bad", "pinned=yes", "window=2h", "window=",
    "q=a%25b", "q=" + "a" * 65, "client=" + "c" * 65, "model=a%20b", "rule=%3Cx%3E",
    "nope=1", "limit=201", "limit=0",
])
async def test_session_list_filters_are_bounded(dashboard_app, query):
    async with client_for(dashboard_app, token="test-admin") as client:
        response = await client.get("/router/audit/sessions?" + query)
        assert response.status_code == 400, query
        assert set(response.json()) == {"error"}


@pytest.mark.parametrize("query", ["window=2h", "window=", "window=24H", "window=24h&x=1", "limit=5"])
async def test_overview_window_is_bounded(dashboard_app, query):
    async with client_for(dashboard_app, token="test-admin") as client:
        assert (await client.get("/router/audit/overview?" + query)).status_code == 400


async def test_overview_needs_the_admin_credential(dashboard_app):
    async with client_for(dashboard_app) as client:
        assert (await client.get("/router/audit/overview")).status_code == 401
    async with client_for(dashboard_app, local=True) as client:
        assert (await client.get("/router/audit/overview")).status_code == 401
    async with client_for(dashboard_app, token="wrong") as client:
        assert (await client.get("/router/audit/overview")).status_code == 401
    async with client_for(dashboard_app, token="test-admin") as client:
        for window, buckets in (("1h", 12), ("24h", 24), ("7d", 28), ("30d", 30)):
            response = await client.get(f"/router/audit/overview?window={window}")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["window"] == window and len(body["series"]) == buckets
            # An empty database is no data, not zero latency.
            assert body["latency"]["jev_admission_ms"] == {"n": 0, "p50": None, "p95": None}
            assert body["executions"]["completion_rate"] is None
        assert (await client.get("/router/audit/overview")).json()["window"] == "24h"


@respx.mock
async def test_overview_allowlist_holds_counts_apart_and_writes_nothing(tmp_path, strict_env):
    svc = Service(tmp_path, config=file_config(tmp_path))
    try:
        svc.router.store.log_state = True
        first, _second = await two_sessions(svc)
        # Legacy alias traffic: no event type, counted on its own.
        logged(svc.router.store, conversation_key=MARKER, state={"prompt": MARKER},
               features={"note": MARKER}, client=MARKER, rule="easy", model="small")
        conn = svc.router.store.conn
        dump = "\n".join(conn.iterdump())
        assert MARKER in dump
        before = conn.total_changes
        response = await audit_get(svc, "/router/audit/overview?window=1h", token=ADMIN)
        listed = await audit_get(svc, "/router/audit/sessions?problem=any&window=24h", token=ADMIN)
        assert conn.total_changes == before
        assert "\n".join(conn.iterdump()) == dump
    finally:
        await svc.close()

    for item in (response, listed):
        assert item.status_code == 200
        assert MARKER not in item.text and TOOL not in item.text
        for word in ("fingerprint", "owner", "conversation_key", "languages", "answers",
                     "features", "state_shape", "tool_names"):
            assert f'"{word}"' not in item.text, word
    assert first.session_id not in response.text and "never-sent" not in response.text
    body = response.json()
    assert body["sessions"]["admitted"] == 2
    assert body["sessions"]["adaptation"] == {"off": 2, "shadow": 0, "active": 0}
    executions = body["executions"]
    assert executions["by_status"]["completed"] == 3
    assert executions["by_status"]["unknown"] == 1 and executions["by_status"]["rejected"] == 1
    assert executions["total"] == 5 and executions["completion_rate"] == 3 / 5
    assert executions["rows"] == 3 and executions["usage_reported"] == 3
    assert body["decisions"]["admissions"] == 2
    assert body["decisions"]["rules"] == [{"rule": "hard", "n": 2}]
    assert body["decisions"]["choices"] == [{"model": "big", "effort": "high", "n": 2}]
    assert body["decisions"]["fallback"] == 0 and body["decisions"]["quota_recorded"] == 2
    [model] = body["latency"]["models"]
    assert model["model"] == "big" and model["n"] == 3 and model["response_ms"]["n"] == 3
    assert body["latency"]["jev_admission_ms"]["n"] == 2
    assert sum(b["requests"] for b in body["series"]) == 5
    assert sum(b["problems"] for b in body["series"]) == 2
    # The legacy row is in its own block and nowhere else.
    assert body["legacy"]["decisions"] == 1
    assert body["legacy"]["jev_ms"]["n"] == 1
    assert body["effort"]["shadow"]["plans"] == 0 and body["effort"]["active"]["plans"] == 0
    assert [row["session_id"] for row in listed.json()["sessions"]] == [first.session_id]


def test_build_overview_keeps_gaps_and_windows_apart():
    from jev_router.dashboard import build_overview, percentiles

    assert percentiles([None, "x", True]) == {"n": 0, "p50": None, "p95": None}
    assert percentiles([3.0, 1.0, 2.0, 4.0]) == {"n": 4, "p50": 2.0, "p95": 4.0}
    since = 1_000_000.0
    raw = {
        "sessions": [{"created": since + 10, "adaptation_mode": None, "quota_changed_choice": None}],
        "requests": [{"created": since + 10, "status": "completed"},
                     {"created": since + 400, "status": "stream_failed"}],
        "decisions": [
            {"ts": since + 5, "event_type": "admission", "rule": "admission_fallback",
             "model": "m", "effort": None, "jev_ms": 1500.0, "fallback": 1},
            {"ts": since + 11, "event_type": "execution", "model": "m", "first_byte_ms": None,
             "response_ms": 900.0, "usage_reported": 0},
            {"ts": since + 401, "event_type": "execution", "model": f"bad {MARKER}",
             "first_byte_ms": None, "response_ms": None, "usage_reported": 0},
            {"ts": since + 402, "event_type": "something_new", "model": "m"},
        ],
        "plans": [{"mode": "shadow", "action": "keep", "status": "confirmed",
                   "from_effort": "low", "recommendation": "high", "jev_ms": 80.0},
                  {"mode": "active", "action": "change_effort", "status": "outcome_unknown",
                   "from_effort": "low", "to_effort": "high", "recommendation": "high"}],
        "quota": [{"ts": since, "snapshot": {"openai": {"status": "stale", "pressure": 0.9,
                   "windows": [{"name": "weekly", "used_percent": 80.0, "resets_at": 5.0},
                               {"name": "hourly", "used_percent": 10.0}]}}}],
    }
    body = build_overview(raw, "1h", since)
    assert MARKER not in str(body)
    assert body["sessions"]["adaptation_unrecorded"] == 1
    assert body["decisions"]["fallback"] == 1 and body["decisions"]["quota_recorded"] == 0
    # A failed call's time is not Jev's answer time.
    assert body["latency"]["jev_admission_ms"]["n"] == 0
    assert body["executions"]["first_byte_recorded"] == 0
    assert body["executions"]["by_status"]["stream_failed"] == 1
    by_model = {m["model"]: m for m in body["latency"]["models"]}
    assert by_model[None]["response_ms"]["p50"] is None and by_model["m"]["n"] == 1
    first, second = body["series"][0], body["series"][1]
    assert (first["requests"], first["problems"], first["response_p50_ms"]) == (1, 0, 900.0)
    assert (second["requests"], second["problems"], second["timed"]) == (1, 1, 0)
    assert second["response_p50_ms"] is None
    assert body["effort"]["shadow"]["recommended"] == 1 and body["effort"]["shadow"]["applied"] == 0
    assert body["effort"]["active"]["applied"] == 1 and body["effort"]["active"]["confirmed"] == 0
    assert body["latency"]["jev_plan_ms"]["n"] == 1
    # Each window keeps its own usage beside its own reset; stale stays stale.
    quota = body["quota"]["providers"]["openai"]
    assert quota["status"] == "stale"
    assert quota["windows"] == [
        {"used_percent": 80.0, "resets_at": 5.0, "name": "weekly"},
        {"used_percent": 10.0, "name": "hourly"},
    ]
    # Unknown event types are neither session nor legacy traffic.
    assert body["legacy"]["decisions"] == 0


# --- the live quota strip ------------------------------------------------


def test_live_quota_projects_by_allowlist_and_keeps_gaps():
    from jev_router.dashboard import live_quota

    class Monitor:
        def all_snapshots(self, now):
            return {
                "as_of": now, "quota_routing": True, "demote_above": 0.6,
                "exhausted_at": 100.0, "secret": "PRIVATE",
                "routed": [
                    {
                        "provider": "openai", "enabled": True, "source": "codexbar",
                        "codexbar_provider": "codex", "via": "snapshot", "status": "fresh",
                        "steering": "calm", "pressure": 0.0, "age_seconds": 12.0,
                        "fetched_at": 1.0, "used_percent": 40.0, "demote_above": 0.6,
                        "exhausted_at": 100.0, "polls": 1, "errors": 0,
                        "reason": "PRIVATE reason", "last_error": "PRIVATE error",
                        "snapshot": {"PRIVATE": 1}, "exhausted": None,
                        "windows": [{
                            "name": "secondary", "used_percent": 40.0,
                            "expected_percent": None, "pace_delta": None,
                            "resets_at": 5.0, "window_minutes": 10080.0,
                            "reset_description": "PRIVATE <b>long sentence that is not a label at all</b>",
                            "extra": "PRIVATE",
                        }],
                        "models": [
                            {"model": "big", "quota_cost": 1.0, "pressure": 0.0, "x": "PRIVATE"},
                            {"model": "PRIVATE MODEL NAME WITH SPACES"},
                        ],
                    },
                    {"provider": "bad name!", "status": "fresh"},
                    {"provider": "xai", "status": "weird", "steering": "panic",
                     "via": "browser", "pressure": float("nan"), "enabled": "yes"},
                ],
                "tracked": [
                    {
                        "id": "claude", "status": "fresh", "via": "cli", "age_seconds": 3.0,
                        "windows": [], "plans": [{"title": "Max", "used_percent": 12.0,
                                                  "detail": "PRIVATE"}],
                        "tokens": {"last_30_days_cost_usd": 812.0, "account": "PRIVATE"},
                        "currency": "usd-PRIVATE", "credits_remaining": None,
                        "quotaOwnerKey": "PRIVATE",
                    },
                    {"id": None},
                ],
            }

    body = live_quota(Monitor(), 100.0)
    assert "PRIVATE" not in repr(body)
    assert set(body) == {"as_of", "quota_routing", "demote_above", "exhausted_at",
                         "routed", "tracked"}
    openai, xai = body["routed"]
    assert set(openai) == {
        "provider", "enabled", "source", "codexbar_provider", "via", "status", "steering",
        "pressure", "age_seconds", "fetched_at", "used_percent", "demote_above",
        "exhausted_at", "polls", "errors", "exhausted", "windows", "models",
    }
    assert openai["windows"] == [{
        "name": "secondary", "used_percent": 40.0, "expected_percent": None,
        "pace_delta": None, "resets_at": 5.0, "window_minutes": 10080.0,
        "reset_description": None,
    }]
    assert openai["models"] == [{"model": "big", "quota_cost": 1.0, "pressure": 0.0}]
    # Values outside their vocabularies become gaps, never guesses.
    assert (xai["status"], xai["steering"], xai["via"], xai["pressure"], xai["enabled"]) == (
        None, None, None, None, False,
    )
    [claude] = body["tracked"]
    assert claude["plans"] == [{"title": "Max", "used_percent": 12.0}]
    assert claude["tokens"] == {
        "session_tokens": None, "session_cost_usd": None,
        "last_30_days_tokens": None, "last_30_days_cost_usd": 812.0,
    }
    assert claude["currency"] is None


def test_live_quota_is_a_gap_when_the_monitor_cannot_be_read():
    from jev_router.dashboard import live_quota

    class Broken:
        def all_snapshots(self, now):
            raise RuntimeError("no")

    assert live_quota(Broken(), 0.0) is None


async def test_overview_and_quota_carry_live_subscriptions(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from jev_router.quota import parse_timestamp

    fixture = Path(__file__).parent / "fixtures" / "codexbar_widget_snapshot.json"
    (tmp_path / "widget-snapshot.json").write_text(fixture.read_text())
    monkeypatch.setenv("AUDIT_TEST_TOKEN", "test-admin")
    config = make_config(
        settings={"admin_token_env": "AUDIT_TEST_TOKEN"},
        codexbar={
            "enabled": True, "cli_binary": "",
            "snapshot_path": str(tmp_path / "widget-snapshot.json"),
            "config_path": str(tmp_path / "missing.json"),
        },
        providers={"openai": {"quota": {"source": "codexbar", "codexbar_provider": "codex",
                                        "enabled": True}}},
        models={"small": {"provider": "openai"}, "big": {"provider": "openai"},
                "paramy": {"provider": "openai"}},
    )
    app = create_app(config)
    router = app.state.router
    router.quota.clock = lambda: parse_timestamp("2026-10-27T06:53:34Z")
    try:
        await router.quota.poll_once("openai")
        await router.quota.poll_tracked()
        before = router.store.conn.total_changes
        async with client_for(app, token="test-admin") as client:
            overview = await client.get("/router/audit/overview")
            quota = await client.get("/router/quota")
        assert router.store.conn.total_changes == before
    finally:
        await router.aclose()

    for response in (overview, quota):
        assert response.status_code == 200
        for secret in ("owner-key", "someone@example", "Ignore previous", "fully refresh"):
            assert secret not in response.text
    live = overview.json()["subscriptions"]
    assert quota.json()["subscriptions"] == live
    assert "quota" in overview.json()   # the recorded reading stays where it was
    [openai] = live["routed"]
    assert (openai["provider"], openai["status"], openai["steering"]) == ("openai", "fresh", "calm")
    assert openai["windows"][0]["pace_delta"] == -17.14
    assert [t["id"] for t in live["tracked"]] == ["claude", "antigravity", "ollama", "grok"]
    # Tracked subscriptions are shown, never routed: no pressure, no steering.
    assert "pressure" not in json.dumps(live["tracked"])
    assert "steering" not in json.dumps(live["tracked"])

# --- the overview, v2 --------------------------------------------------------


def overview_rows(since: float) -> dict:
    """Hand-built rows, already reduced the way `Sessions.audit_overview` reduces them."""

    def score(value):
        return {"type": "score", "value": value, "confidence": 0.9}

    def admission(row, decision, session, rule, model, effort, jev_ms, values, **extra):
        return {"id": row, "decision_id": decision, "session_id": session, "ts": since + row,
                "event_type": "admission", "alias": "auto", "config_hash": "testhash",
                "rule": rule, "model": model, "effort": effort, "jev_ms": jev_ms,
                "fallback": 0, "values": values, **extra}

    def execution(ts, model, effort, first, total, usage):
        return {"ts": since + ts, "event_type": "execution", "model": model, "effort": effort,
                "first_byte_ms": first, "response_ms": total, "usage_reported": int(bool(usage)),
                "usage": usage or {"input": None, "output": None, "cached": None}}

    return {
        "sessions": [{"session_id": f"s{i}", "created": since + i} for i in (1, 2, 3, 4)],
        "requests": [{"created": since + 10 + i, "status": "completed"} for i in range(4)],
        "decisions": [
            admission(1, "d1", "s1", "hard", "big", "high", 200.0,
                      {"difficulty": score(1.55), "task": {"type": "choice", "value": "code"}}),
            admission(2, "d2", "s2", "easy", "small", "none", 100.0,
                      {"difficulty": score(0.1), "task": {"type": "choice", "value": "quick_question"}}),
            admission(3, "d3", "s3", "admission_fallback", "big", "medium", 1500.0, {}, fallback=1),
            admission(4, "d4", "s4", "low_confidence", "big", "medium", 300.0,
                      {"difficulty": score(1.5)}, config_hash="old"),
            execution(10, "big", "high", 100.0, 1100.0, {"input": 1000, "output": 500, "cached": 800}),
            execution(11, "big", "high", 300.0, 2300.0, {"input": 1000, "output": 1000, "cached": None}),
            execution(12, "small", "none", None, 400.0, None),
            execution(13, "paramy", "low", 50.0, 60.0, None),
            {"ts": since + 21, "event_type": "effort_plan", "model": "big", "effort": "high",
             "shadow_values": {"failure_mode": {"type": "choice", "value": "reasoning_problem"},
                               "ignored": {"type": "noul", "value": 0.4}}},
        ],
        "plans": [
            {"session_id": "s1", "created": since + 20, "mode": "shadow", "direction": "up",
             "corrective_followup": 0.7, "jev_ms": 50.0, "plan_ms": 60.0},
            {"session_id": "s1", "created": since + 30, "mode": "shadow", "direction": "hold",
             "corrective_followup": 0.1, "jev_ms": 70.0, "plan_ms": 80.0},
            {"session_id": "s2", "created": since + 25, "mode": "shadow", "direction": "hold",
             "corrective_followup": None, "jev_ms": None, "plan_ms": 40.0},
        ],
        "feedback": [
            {"decision_id": "d1", "ts": 1.0, "verdict": "too_weak"},
            {"decision_id": "d1", "ts": 2.0, "verdict": "right"},
            {"decision_id": "elsewhere", "session_id": "s2", "ts": 3.0, "verdict": "too_strong"},
            {"decision_id": "d3", "ts": 4.0, "verdict": "<b>bad</b>"},
        ],
        "quota": [],
    }


def overview_config():
    return make_config(models={"big": {"quota_cost": 1.0}, "small": {"quota_cost": 0.25}})


def test_overview_v2_router_tax_and_upstream():
    from jev_router.dashboard import build_overview

    since = 1_000_000.0
    body = build_overview(overview_rows(since), "1h", since, config=overview_config())
    tax = body["router_tax"]
    # Fallback time is router tax but not Jev's answer time.
    assert tax["admission_ms"] == {"n": 3, "p50": 200.0, "p95": 300.0}
    assert tax["plan_ms"] == {"n": 3, "p50": 60.0, "p95": 80.0}
    assert tax["plan_jev_ms"]["n"] == 2
    assert tax["jev_calls_timed"] == 6 and tax["jev_ms_total"] == 2220.0
    assert tax["execution_requests"] == 4 and tax["amortized_ms_per_request"] == 555.0

    pairs = {(p["model"], p["effort"]): p for p in body["upstream"]["pairs"]}
    big = pairs[("big", "high")]
    assert big["n"] == 2
    assert big["first_byte_ms"] == {"n": 2, "p50": 100.0, "p95": 300.0}
    # 500 tokens over 1000 ms of streaming, and 1000 over 2000 ms.
    assert big["tps"] == {"n": 2, "p50": 500.0}
    assert big["response_ms"] == {"n": 2, "p50": 1100.0}
    small = pairs[("small", "none")]
    assert small["tps"] == {"n": 0, "p50": None}
    assert small["first_byte_ms"]["p50"] is None
    # The series carries time to first byte beside the response time.
    assert body["series"][0]["first_byte_p50_ms"] == 100.0
    assert body["series"][1]["first_byte_p50_ms"] is None


def test_overview_v2_quality():
    from jev_router.dashboard import build_overview

    since = 1_000_000.0
    quality = build_overview(overview_rows(since), "1h", since, config=overview_config())["quality"]
    corrective = quality["corrective_followup"]
    assert corrective["threshold"] == 0.6
    assert (corrective["numerator"], corrective["denominator"], corrective["rate"]) == (1, 2, 0.5)
    assert corrective["by_rule"] == [{"rule": "hard", "numerator": 1, "denominator": 2, "rate": 0.5}]
    early = quality["early_escalation"]
    assert (early["numerator"], early["denominator"]) == (1, 2)
    assert {r["rule"]: r["rate"] for r in early["by_rule"]} == {"hard": 1.0, "easy": 0.0}
    edge = quality["knife_edge"]
    # 1.55 is within 0.10 of `hard`'s 1.5; 0.1 is nowhere near `easy`'s 0.5.
    assert (edge["numerator"], edge["denominator"]) == (1, 2)
    assert edge["unassessed"] == 2
    assert edge["unassessed_by_reason"] == {"no_ladder_rule": 1, "config_changed": 1}
    # The newest verdict wins; a session-level verdict joins by session; junk is dropped.
    assert quality["feedback"] == {
        "admissions": 4,
        "counts": {"right": 1, "too_weak": 0, "too_strong": 1, "too_slow": 0, "unrated": 2},
    }
    shadow = quality["shadow"]
    assert set(shadow) == {"failure_mode"}
    assert shadow["failure_mode"]["counts"] == {"reasoning_problem": 1}
    assert shadow["failure_mode"]["by_pair"][0]["model"] == "big"


def test_overview_v2_efficiency_matrix_and_reliability():
    from jev_router.dashboard import build_overview

    since = 1_000_000.0
    breakers = {"default": {"state": "open", "healthy": False, "consecutive_failures": 3,
                            "retry_in_seconds": 12.5, "last_error": f"upstream {MARKER}",
                            "models": ["big"]}}
    body = build_overview(overview_rows(since), "1h", since, config=overview_config(),
                          breakers=breakers)
    assert MARKER not in str(body)
    efficiency = body["efficiency"]
    # big, big and small are priced (1 + 1 + 0.25); paramy has no quota_cost.
    assert efficiency["quota_saved"] == {
        "saved_rate": 0.25, "quota_cost_sum": 2.25, "priced": 3, "unpriced": 1,
    }
    cache = efficiency["cache"]
    assert (cache["rows"], cache["cached_tokens"], cache["input_tokens"], cache["rate"]) == (
        1, 800, 1000, 0.8)
    by_model = {m["model"]: m for m in cache["by_model"]}
    assert by_model["small"]["rate"] is None and by_model["small"]["rows"] == 0
    assert efficiency["usage_reported"] == {
        "executions": 4, "reported": 2, "input": 2, "output": 2, "cached": 1,
    }

    matrix = body["matrix"]
    assert matrix["question"] == "task" and matrix["tasks"] == ["code", "quick_question"]
    cells = {(c["task"], c["model"], c["effort"]): c for c in matrix["cells"]}
    assert cells[("code", "big", "high")]["corrective"]["rate"] == 0.5
    assert cells[("quick_question", "small", "none")]["corrective"] == {
        "numerator": 0, "denominator": 0, "rate": None}
    assert cells[(None, "big", "medium")]["n"] == 2

    reliability = body["reliability"]
    assert reliability["fallback"] == {"numerator": 1, "denominator": 4, "rate": 0.25}
    assert reliability["low_confidence"]["numerator"] == 1
    breaker = reliability["breakers"]["default"]
    assert breaker["state"] == "open" and breaker["consecutive_failures"] == 3
    assert "last_error" not in breaker and "models" not in breaker


def test_overview_v2_without_config_reports_gaps():
    from jev_router.dashboard import build_overview

    since = 1_000_000.0
    body = build_overview({"sessions": [], "requests": [], "decisions": [], "plans": []}, "1h", since)
    assert body["router_tax"]["amortized_ms_per_request"] is None
    assert body["efficiency"]["quota_saved"]["saved_rate"] is None
    assert body["efficiency"]["cache"]["rate"] is None
    assert body["quality"]["corrective_followup"]["rate"] is None
    assert body["quality"]["knife_edge"]["rate"] is None
    assert body["reliability"]["breakers"] is None
    body = build_overview(overview_rows(since), "1h", since)
    # Without the running config nothing can be compared with a cutoff or a price.
    assert body["quality"]["knife_edge"]["unassessed"] == 4
    assert body["efficiency"]["quota_saved"]["unpriced"] == 4


def test_usage_and_answers_reduce_to_numbers_and_labels():
    from jev_router.sessions import answer_values, plan_signals, usage_counts

    chat = {"prompt_tokens": 120, "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 100}, "note": MARKER}
    responses = {"input_tokens": 90, "output_tokens": 10,
                 "input_tokens_details": {"cached_tokens": 0}}
    assert usage_counts(chat) == {"input": 120, "output": 30, "cached": 100}
    assert usage_counts(responses) == {"input": 90, "output": 10, "cached": 0}
    assert usage_counts({"total_tokens": 42}) == {"input": None, "output": None, "cached": None}
    assert usage_counts("nonsense") == {"input": None, "output": None, "cached": None}
    stored = {
        "difficulty": {"type": "score", "score": 1.2, "confidence": 0.8},
        "harm_if_wrong": {"type": "noul", "noul": 0.3},
        "task": {"type": "choice", "choice": "code", "confidence": 0.9},
        "odd": {"type": "choice", "choice": f"free text {MARKER}"},
    }
    assert answer_values(stored) == {
        "difficulty": {"type": "score", "value": 1.2, "confidence": 0.8},
        "harm_if_wrong": {"type": "noul", "value": 0.3, "confidence": None},
        "task": {"type": "choice", "value": "code", "confidence": 0.9},
    }
    shadow = {"failure_mode": {"type": "choice", "value": "unclear", "confidence": 0.5}}
    assert answer_values(shadow)["failure_mode"]["value"] == "unclear"
    facts = {"direction": "up", "evidence": {"corrective_followup": 0.7, "difficulty": 2.0},
             "thresholds": {"corrective_followup": 0.6}, "reason": MARKER}
    assert plan_signals(facts) == {
        "corrective_followup": 0.7, "corrective_threshold": 0.6, "direction": "up",
    }
    assert plan_signals(None)["direction"] is None


@respx.mock
async def test_overview_endpoint_reads_usage_and_breakers(tmp_path, strict_env):
    svc = Service(tmp_path, config=file_config(tmp_path))
    try:
        caller = await run_session(svc, executions=1)
        row = svc.sessions.request_rows(caller.session_id)[0]["decision"]
        svc.router.store.update_decision(
            row["id"], upstream_usage={"input_tokens": 10, "output_tokens": 4,
                                       "input_tokens_details": {"cached_tokens": 6}})
        before = svc.router.store.conn.total_changes
        body = (await audit_get(svc, "/router/audit/overview?window=1h", token=ADMIN)).json()
        assert svc.router.store.conn.total_changes == before
    finally:
        await svc.close()
    assert body["efficiency"]["cache"]["rate"] == 0.6
    assert body["efficiency"]["usage_reported"]["output"] == 1
    assert body["matrix"]["cells"][0]["task"] == "code"
    assert set(body["reliability"]["breakers"]) == {"default"}
    assert body["quality"]["knife_edge"]["denominator"] == 1
