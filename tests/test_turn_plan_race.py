"""Two plan requests for one turn produce one plan (F07, spec 9.1).

The Jev fake here really awaits, because a race that only exists while a
network call is in flight cannot be reproduced against a mock that answers
before it returns.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Client, Service, upstream
from test_app import JEV_URL
from test_turn_plan import adaptive

from jev_router.pins import Store
from jev_router.sessions import Sessions


def slow_jev(difficulty: float = 1.9, delay: float = 0.02):
    """The same answers as `turn_jev`, from a call that actually waits."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        asked = json.loads(request.content)["questions"]
        admission = "task" in asked
        answers = {}
        for qid in asked:
            if qid == "difficulty":
                answers[qid] = {
                    "type": "score",
                    "score": 0.1 if admission else difficulty,
                    "confidence": 0.9,
                }
            elif qid == "corrective_followup":
                answers[qid] = {"type": "noul", "noul": 0.0}
            elif qid == "harm_if_wrong":
                answers[qid] = {"type": "noul", "noul": 0.1}
            elif qid == "task":
                answers[qid] = {
                    "type": "choice",
                    "choice": "code",
                    "confidence": 0.9,
                    "probabilities": {},
                }
            else:
                answers[qid] = {
                    "type": "choice",
                    "choice": "no_failure_evidence",
                    "confidence": 0.8,
                }
        return httpx.Response(
            200, json={"answers": answers, "usage": {"input_tokens": 120}}
        )

    return respx.post(JEV_URL).mock(side_effect=handler)


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path, config=H.adaptive_config(tmp_path))
    yield svc
    await svc.close()


@respx.mock
async def test_f07_two_concurrent_plans_for_one_turn_make_one_plan(service):
    jev = slow_jev()
    upstream()
    caller = await adaptive(service)
    before = jev.call_count

    first, second = await asyncio.gather(
        caller.turn_plan("turn-0002", request_id="plan-a"),
        caller.turn_plan("turn-0002", request_id="plan-b"),
    )

    plans = service.sessions.plans(caller.session_id)
    assert len(plans) == 1
    assert first.status_code == 200 and second.status_code == 200
    # Both callers are told about the one plan that exists.
    assert first.json()["plan_id"] == second.json()["plan_id"] == plans[0]["plan_id"]
    assert first.json()["action"] == second.json()["action"] == "change_effort"
    # And the loser did not buy a classifier call of its own.
    assert jev.call_count - before == 1
    # The session's expected effort moved once, to the one plan's rung.
    assert service.sessions.get(caller.session_id)["expected_effort"] == "medium"


@respx.mock
async def test_f07_concurrent_plans_with_different_evidence_conflict(service):
    slow_jev()
    upstream()
    caller = await adaptive(service)

    first, second = await asyncio.gather(
        caller.turn_plan("turn-0002", request="Investigate the escaped quotes."),
        caller.turn_plan("turn-0002", request="Something else entirely."),
    )

    codes = sorted(r.status_code for r in (first, second))
    assert codes == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "SESSION_CONFLICT"
    assert "different evidence" in loser.json()["error"]["message"]
    assert len(service.sessions.plans(caller.session_id)) == 1


@respx.mock
async def test_f07_a_second_session_is_not_held_up_by_the_first(service):
    """The guard is per session and turn, not a queue in front of the router."""
    slow_jev()
    upstream()
    a = await adaptive(service)
    b = Client(service, session_id="session-bbbbbbbb", alias="auto-adaptive")
    assert (
        await b.resolve(
            client_contract={
                "protocols": ["openai-responses"],
                "turn_boundary_reporting": True,
            }
        )
    ).status_code == 200

    plans = await asyncio.gather(a.turn_plan("turn-0002"), b.turn_plan("turn-0002"))
    assert [p.status_code for p in plans] == [200, 200]
    assert len(service.sessions.plans(a.session_id)) == 1
    assert len(service.sessions.plans(b.session_id)) == 1
    assert plans[0].json()["plan_id"] != plans[1].json()["plan_id"]


@respx.mock
async def test_f07_the_guard_is_released_when_a_plan_fails(service):
    slow_jev()
    upstream()
    caller = await adaptive(service)
    unsettled = {"id": "turn-0001", "settled": False}
    assert (await caller.turn_plan("turn-0002", previous=unsettled)).status_code == 409
    assert service.router._turn_locks == {}

    assert (await caller.turn_plan("turn-0002")).status_code == 200


# --- the database's own guarantee ----------------------------------------


def test_f07_the_store_refuses_a_second_plan_for_one_turn(tmp_path):
    store = Store(tmp_path / "router.db")
    sessions = Sessions(store, guard=False)
    try:
        assert sessions.unique_turn_plans is True
        row = {
            "turn_id": "turn-1",
            "sequence": 1,
            "action": "change_effort",
            "status": "planned",
            "from_effort": "low",
            "to_effort": "medium",
        }
        first = sessions.record_plan("s1", {"plan_id": "p1", **row})
        second = sessions.record_plan("s1", {"plan_id": "p2", **row, "sequence": 2})
        assert first["plan_id"] == "p1"
        # The loser is handed the winner's row, not its own and not nothing.
        assert second["plan_id"] == "p1"
        assert [p["plan_id"] for p in sessions.plans("s1")] == ["p1"]
    finally:
        sessions.close()
        store.close()


def test_f07_an_older_database_with_duplicates_still_opens(tmp_path):
    """The index is additive. Rows written before it are left exactly as they are."""
    path = tmp_path / "router.db"
    store = Store(path)
    sessions = Sessions(store, guard=False)
    sessions.close()
    # Write the duplicate the way a release before the index could have.
    with store.lock:
        store.conn.execute("DROP INDEX turn_plans_one_per_turn")
        for plan_id, sequence in (("p1", 1), ("p2", 2)):
            store.conn.execute(
                "INSERT INTO turn_plans (session_id, plan_id, turn_id, sequence,"
                " status, created) VALUES (?,?,?,?,?,?)",
                ("s1", plan_id, "turn-1", sequence, "planned", 1.0),
            )
        store.conn.commit()
    store.close()

    reopened = Store(path)
    sessions = Sessions(reopened, guard=False)
    try:
        assert sessions.unique_turn_plans is False
        assert [p["plan_id"] for p in sessions.plans("s1")] == ["p1", "p2"]
        # And the index really is absent rather than quietly half applied.
        names = {
            row[0]
            for row in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "turn_plans_one_per_turn" not in names
    finally:
        sessions.close()
        reopened.close()


def test_f07_a_database_written_before_the_index_gains_it(tmp_path):
    path = tmp_path / "router.db"
    store = Store(path)
    sessions = Sessions(store, guard=False)
    sessions.close()
    with store.lock:
        store.conn.execute("DROP INDEX turn_plans_one_per_turn")
        store.conn.commit()
    store.close()

    reopened = Store(path)
    sessions = Sessions(reopened, guard=False)
    try:
        assert sessions.unique_turn_plans is True
        with pytest.raises(sqlite3.IntegrityError):
            reopened.conn.execute(
                "INSERT INTO turn_plans (session_id, plan_id, turn_id, sequence,"
                " status, created) VALUES (?,?,?,?,?,?),(?,?,?,?,?,?)",
                ("s1", "p1", "t1", 1, "planned", 1.0)
                + ("s1", "p2", "t1", 2, "planned", 1.0),
            )
    finally:
        reopened.conn.rollback()
        sessions.close()
        reopened.close()
