import time

from jev_router.pins import Store, conversation_key, new_decision_id


def log(store, **kwargs):
    base = dict(
        decision_id=new_decision_id(),
        alias="auto",
        client="",
        conversation_key="k",
        state_builder="summary_v1",
        answers={},
        features={"message_count": 1},
        config_hash="testhash",
        model="big",
        effort="medium",
        rule="default",
        mode="active",
        fallback=False,
        pinned=False,
        jev_ms=12.0,
        jev_tokens=400,
        est_tokens=10,
        message_count=1,
    )
    base.update(kwargs)
    return store.log_decision(**base), base["decision_id"]


def test_conversation_key_is_stable_and_specific():
    a = conversation_key("Bearer x", "system", "first")
    b = conversation_key("Bearer x", "system", "first")
    c = conversation_key("Bearer y", "system", "first")
    d = conversation_key("Bearer x", "system", "other first")
    assert a == b
    assert a != c and a != d
    assert len(a) == 64


def test_conversation_key_hides_the_key_and_the_text():
    key = conversation_key("Bearer secret-value", "system prompt", "hello there")
    assert "secret-value" not in key
    assert "hello" not in key


def test_pin_round_trip(store):
    store.set_pin("k1", "big", "high", "dec1")
    pin = store.get_pin("k1", 3600)
    assert pin["model"] == "big"
    assert pin["effort"] == "high"
    assert pin["decision_id"] == "dec1"


def test_pin_expires(store):
    store.set_pin("k1", "big", "high", "dec1")
    assert store.get_pin("k1", 0) is None
    assert store.get_pin("k1", 3600) is None  # the expired row is gone


def test_pin_overwrite(store):
    store.set_pin("k1", "big", "high", "dec1")
    store.set_pin("k1", "small", "low", "dec2")
    assert store.get_pin("k1", 3600)["model"] == "small"


def test_decision_log_keeps_answers_and_features(store):
    answers = {"task": {"type": "choice", "choice": "code", "confidence": 0.8,
                        "probabilities": {"code": 0.8}}}
    row, did = log(store, answers=answers, features={"has_code": True, "languages": ["python"]})
    assert row > 0
    stored = store.get_decision(did)
    assert stored["answers"]["task"]["probabilities"] == {"code": 0.8}
    assert stored["features"]["languages"] == ["python"]
    assert stored["config_hash"] == "testhash"


def test_state_is_not_stored_unless_asked(tmp_path):
    quiet = Store(tmp_path / "a.db", log_state=False)
    _, did = log(quiet, state={"latest_user_request": "secret"})
    assert quiet.get_decision(did)["state"] is None

    loud = Store(tmp_path / "b.db", log_state=True)
    _, did2 = log(loud, state={"latest_user_request": "secret"})
    assert loud.get_decision(did2)["state"] == {"latest_user_request": "secret"}
    quiet.close()
    loud.close()


def test_update_decision_adds_status_and_usage(store):
    row, did = log(store)
    store.update_decision(row, upstream_status=200, upstream_usage={"total_tokens": 99})
    stored = store.get_decision(did)
    assert stored["upstream_status"] == 200
    assert stored["upstream_usage"]["total_tokens"] == 99


def test_recent_decisions_is_newest_first(store):
    _, first = log(store, rule="one")
    time.sleep(0.01)
    _, second = log(store, rule="two")
    rows = store.recent_decisions(10)
    assert [r["decision_id"] for r in rows[:2]] == [second, first]


def test_last_decision_id_prefers_the_client(store):
    _, other = log(store, client="other")
    _, mine = log(store, client="mine")
    _, newest = log(store, client="other")
    assert store.last_decision_id("mine") == mine
    assert store.last_decision_id("nobody") == newest
    assert store.last_decision_id(None) == newest


def test_feedback_round_trip(store):
    _, did = log(store)
    store.add_feedback(decision_id=did, verdict="too_weak", better_model="big",
                       better_effort="high", note="needed more care", source="cli")
    store.add_feedback(decision_id=did, verdict="too_slow", source="api")
    rows = store.recent_feedback(10)
    assert len(rows) == 2
    assert rows[0]["verdict"] == "too_slow"
    assert rows[1]["better_model"] == "big"
    assert rows[1]["alias"] == "auto"  # joined to the decision
    decisions = store.recent_decisions(5)
    assert len(decisions[0]["feedback"]) == 2


def test_feedback_can_repeat_per_decision(store):
    _, did = log(store)
    for verdict in ("right", "too_strong"):
        store.add_feedback(decision_id=did, verdict=verdict)
    assert len(store.recent_feedback(10)) == 2
