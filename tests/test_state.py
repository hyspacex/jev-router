from jev_router.features import extract_features
from jev_router.state import STATE_BUILDERS, build_state, squeeze


def features_for(**body):
    body.setdefault("model", "auto")
    body.setdefault("messages", [])
    return extract_features(body, body.pop("_headers", {}))


def test_registry_has_the_shipped_builders():
    assert {
        "summary_v1",
        "last_message_only",
        "tail_v1",
        "continuation_aware_v1",
    } <= set(STATE_BUILDERS)


def test_unknown_builder_names_the_known_ones():
    try:
        build_state("nope", features_for(), None)
    except KeyError as exc:
        assert "summary_v1" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_squeeze_keeps_head_and_tail():
    text = "A" * 100 + "B" * 100 + "C" * 100
    out, facts = squeeze(text, 60)
    assert out.startswith("A")
    assert out.endswith("C")
    assert "removed from the middle" in out
    assert facts["original_characters"] == 300
    assert facts["original_lines"] == 1


def test_squeeze_leaves_short_text_alone():
    out, facts = squeeze("hello", 100)
    assert out == "hello"
    assert facts == {}


def test_squeeze_replaces_encoded_blobs():
    text = "before " + ("QUJDRA" * 100) + " after"
    out, facts = squeeze(text, 10_000)
    assert "encoded data removed" in out
    assert facts["encoded_blobs_removed"] == 1
    assert "before" in out and "after" in out


def test_summary_v1_shape_and_truncation():
    f = features_for(
        messages=[
            {"role": "system", "content": "S" * 900},
            {"role": "user", "content": "first"},
            {"role": "user", "content": "long request text. " * 500},
        ],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    state = build_state("summary_v1", f, None)
    assert len(state["latest_user_request"]) < 4200
    assert len(state["start_of_system_prompt"]) == 500
    assert state["facts"]["message_count"] == 3
    assert state["facts"]["request_has_tools"] is True
    assert state["facts"]["tool_names"] == ["read_file"]
    assert state["facts"]["original_characters"] == 9500


def test_summary_v1_omits_empty_system_prompt():
    state = build_state("summary_v1", features_for(messages=[{"role": "user", "content": "hi"}]), None)
    assert "start_of_system_prompt" not in state
    assert state["latest_user_request"] == "hi"


def test_last_message_only_is_just_the_request():
    f = features_for(
        messages=[
            {"role": "system", "content": "ignore me"},
            {"role": "user", "content": "older"},
            {"role": "user", "content": "newest"},
        ]
    )
    state = build_state("last_message_only", f, None)
    assert state == {"latest_user_request": "newest"}


def test_tail_v1_keeps_last_three_turns_in_order():
    f = features_for(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "assistant", "content": "four"},
            {"role": "user", "content": "five"},
        ]
    )
    state = build_state("tail_v1", f, None)
    assert [t["text"] for t in state["recent_turns"]] == ["three", "four", "five"]
    assert state["facts"]["message_count"] == 5


def test_builders_are_deterministic():
    f = features_for(messages=[{"role": "user", "content": "same"}])
    for name in ("summary_v1", "last_message_only", "tail_v1"):
        assert build_state(name, f, None) == build_state(name, f, None)


# --- continuation_aware_v1 and the splitting it does ---------------------


def test_split_leaves_a_short_message_alone():
    from jev_router.state import split_request_and_material

    assert split_request_and_material("why is this broken?") == (
        "why is this broken?",
        "",
    )


def test_split_separates_a_pasted_log_from_the_question():
    from jev_router.state import split_request_and_material

    text = "what's the error on the last line?\n\n" + "\n".join(
        f"2026-09-18T08:41:{i:02d}.000Z [runner] step {i} ok" for i in range(40)
    )
    request, material = split_request_and_material(text)
    assert request == "what's the error on the last line?"
    assert "step 39 ok" in material


def test_split_pulls_a_trailing_note_back_out_of_code():
    from jev_router.state import split_request_and_material

    body = "\n\n".join(
        "func (w *Worker) step%d() {\n    w.mu.Lock()\n    w.results <- Result{}\n"
        "    w.mu.Unlock()\n}" % i
        for i in range(12)
    )
    text = (
        "why is this deadlocking?\n\n"
        + body
        + "\n\nIt runs fine with one worker and hangs under load with eight."
    )
    request, material = split_request_and_material(text)
    assert request.startswith("why is this deadlocking?")
    assert "hangs under load with eight" in request
    assert "w.mu.Lock()" in material


def test_split_keeps_a_prose_document_whole():
    # The last paragraph of a pasted policy is part of the policy, not a note.
    from jev_router.state import split_request_and_material

    text = "Summarise the policy below. Keep every exception.\n\n" + "\n\n".join(
        f"{i}. Clause {i} of the travel policy, which says something about "
        f"expenses and has an exception for unusual cases that matters here."
        for i in range(1, 8)
    )
    request, material = split_request_and_material(text)
    assert request == "Summarise the policy below. Keep every exception."
    assert material.startswith("1. Clause 1")
    assert material.rstrip().endswith("matters here.")


def test_continuation_carries_the_earlier_request():
    f = features_for(
        messages=[
            {"role": "user", "content": "Add keyset pagination to /v1/orders, " * 10},
            {"role": "assistant", "content": "I would encode the cursor as base64url."},
            {"role": "user", "content": "yes do it"},
        ]
    )
    state = build_state("continuation_aware_v1", f, None)
    assert state["latest_user_request"] == "yes do it"
    assert "keyset pagination" in state["earlier_request_in_this_conversation"]
    assert state["facts"]["latest_message_follows_earlier_work"] is True


def test_a_full_new_request_drops_the_earlier_one():
    f = features_for(
        messages=[
            {"role": "user", "content": "Review our sharding plan. " * 10},
            {"role": "assistant", "content": "The main risks are cross-shard transactions."},
            {"role": "user", "content": "Now write the migration for the lookup table. " * 12},
        ]
    )
    state = build_state("continuation_aware_v1", f, None)
    assert "earlier_request_in_this_conversation" not in state
    assert "latest_message_follows_earlier_work" not in state["facts"]


def test_continuation_words_alone_are_detected():
    from jev_router.state import CONTINUATION

    for text in ("yes do it", "continue", "ok, go ahead", "sure, proceed.", "LGTM ship it"):
        assert CONTINUATION.match(text), text
    for text in ("yes but use redis instead", "continue with the other file", "go to line 12"):
        assert not CONTINUATION.match(text), text


def test_pasted_material_gets_its_own_field():
    f = features_for(
        messages=[
            {
                "role": "user",
                "content": "Summarise this.\n\n" + "A policy clause about expenses. " * 40,
            }
        ]
    )
    state = build_state("continuation_aware_v1", f, None)
    assert state["latest_user_request"] == "Summarise this."
    assert "policy clause" in state["material_the_user_pasted"]
    assert state["facts"]["pasted_material_characters"] > 400


def test_a_tool_result_last_turn_is_a_fact():
    f = features_for(
        messages=[
            {"role": "user", "content": "Find every read of legacy_email and move them."},
            {"role": "assistant", "content": None},
            {"role": "tool", "content": "api/customers.py:88: row.legacy_email"},
        ]
    )
    state = build_state("continuation_aware_v1", f, None)
    assert state["facts"]["last_turn_is_a_tool_result"] is True
    assert state["latest_user_request"].startswith("Find every read")


def test_continuation_aware_leaves_out_the_system_prompt():
    f = features_for(
        messages=[
            {"role": "system", "content": "You are a terminal coding agent. " * 50},
            {"role": "user", "content": "rename amt to amount_cents"},
        ]
    )
    state = build_state("continuation_aware_v1", f, None)
    assert "start_of_system_prompt" not in state
    assert state["latest_user_request"] == "rename amt to amount_cents"


def test_continuation_aware_is_deterministic():
    f = features_for(
        messages=[
            {"role": "user", "content": "first request, long enough to be substantive"},
            {"role": "assistant", "content": "a plan"},
            {"role": "user", "content": "go"},
        ]
    )
    assert build_state("continuation_aware_v1", f, None) == build_state(
        "continuation_aware_v1", f, None
    )
