"""The audit view is read-only, bounded, authenticated and prompt-free."""

from __future__ import annotations

import httpx
import pytest
from conftest import make_config
from test_decision_log import logged

from jev_router.app import create_app


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
