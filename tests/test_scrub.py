"""scrub.py: what is removed before any text is stored, and the helpers built on it."""

from __future__ import annotations

import json

import pytest

from jev_router.scrub import (
    BLOB,
    IMAGE,
    PRIVATE_KEY,
    REDACTED,
    SECRET,
    cap_text,
    extract_session_title,
    prompt_preview,
    scrub_state,
    scrub_text,
    summarize_transcript_turn,
    user_turn_count,
    workspace_hint,
)

# Built from parts so no scanner mistakes this file for a leak.
SK = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
RK = "rk-" + "live_" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
GHP = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123"
GHO = "gho_" + "abcdefghijklmnopqrstuvwxyz0123"
PAT = "github_pat_" + "11ABCDEFG0123456789_abcdefghijk"
SLACK = "xoxb-" + "1234567890-0987654321-abcDEF"
GOOGLE = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUifQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


@pytest.mark.parametrize("token", [SK, RK, GHP, GHO, PAT, SLACK, GOOGLE, JWT])
def test_every_token_shape_is_redacted(token):
    out = scrub_text(f"use {token} for the call")
    assert token not in out
    assert out == f"use {SECRET} for the call"


def test_a_token_inside_a_word_is_not_a_token():
    # "task-..." contains "sk-" but is an identifier, not a key.
    text = "rename task-abcdefghijklmnopqrstuvwxyz and work-abcdefghijklmnopqrstuvwxyz"
    assert scrub_text(text) == text


def test_bearer_and_basic_credentials():
    out = scrub_text("curl -H 'Authorization: Bearer abc.def-ghi_jkl012345' and Basic dXNlcjpwYXNzd29yZA==")
    assert "abc.def-ghi_jkl012345" not in out and "dXNlcjpwYXNzd29yZA==" not in out
    assert f"Bearer {SECRET}" in out and f"Basic {SECRET}" in out


def test_pem_private_keys_whole_and_cut_off():
    whole = "key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----\nafter"
    assert scrub_text(whole) == f"key:\n{PRIVATE_KEY}\nafter"
    # A paste cut off before the END line still loses everything after BEGIN.
    cut = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA"
    assert scrub_text(cut) == PRIVATE_KEY
    # A public key or certificate is not a secret.
    public = "-----BEGIN PUBLIC KEY-----\nMFkwEwYH\n-----END PUBLIC KEY-----"
    assert scrub_text(public) == public


@pytest.mark.parametrize("line, kept", [
    ("OPENAI_API_KEY=abcdef123456", "OPENAI_API_KEY="),
    ("export GITHUB_TOKEN='abc123def456'", "export GITHUB_TOKEN='"),
    ('{"password": "hunter2hunter2"}', '{"password": "'),
    ("DB_PASSWORD: correct-horse-battery", "DB_PASSWORD: "),
    ("client_secret = s3cr3tvalue", "client_secret = "),
    ("api-key: zzzzzzzz", "api-key: "),
])
def test_env_style_assignments_keep_the_name_and_lose_the_value(line, kept):
    out = scrub_text(line)
    assert out.startswith(kept + REDACTED), out


@pytest.mark.parametrize("line", [
    "max_tokens: 4096",
    "token_count = 12",
    "the token budget is small",
    "password reset flow",
    "secret: abc",  # too short to be a value worth hiding
])
def test_ordinary_words_and_numbers_survive(line):
    assert scrub_text(line) == line


def test_data_uris_and_long_blobs():
    uri = "data:image/png;base64," + "iVBORw0KGgoAAAANSUhEUgAA" * 20
    out = scrub_text(f"look {uri} here")
    assert out == f"look {IMAGE} here"
    blob = "a1B2" * 70  # 280 characters, one token
    assert scrub_text(f"x {blob} y") == f"x {BLOB} y"
    assert scrub_text("a" * 256) == "a" * 256  # at the limit, kept
    assert scrub_text(None) == ""


def test_cap_text_keeps_head_and_tail_within_the_limit():
    text = " ".join(f"w{i}" for i in range(3000))
    out = cap_text(text, 500)
    assert len(out) <= 500
    assert out.startswith("w0 w1") and out.endswith("w2999")
    assert "characters removed from the middle" in out
    assert cap_text("short", 500) == "short"
    assert cap_text(None, 10) == ""


def test_scrub_state_cleans_every_string_leaf_and_keeps_the_rest():
    state = {
        "latest_user_request": f"deploy with {SK}",
        "quoted_material": [{"kind": "pasted_by_the_user", "content": "x " * 3000}],
        "facts": {"has_tools": True, "estimated_input_tokens": 1200, "tool_names": ["bash", GHP]},
        "nothing": None,
    }
    out = scrub_state(state, 300)
    assert out["latest_user_request"] == f"deploy with {SECRET}"
    assert len(out["quoted_material"][0]["content"]) <= 300
    assert out["facts"] == {"has_tools": True, "estimated_input_tokens": 1200, "tool_names": ["bash", SECRET]}
    assert out["nothing"] is None
    assert SK not in json.dumps(out) and GHP not in json.dumps(out)


def test_session_title():
    prompt = "```python\nprint('x')\n```\n\n## Fix   the quota race in QuotaMonitor\nDetails follow."
    assert extract_session_title(prompt) == "Fix the quota race in QuotaMonitor"
    assert extract_session_title("<environment_context>\n</environment_context>\n- add retries") == "add retries"
    assert extract_session_title(f"Rotate {SK} please") == f"Rotate {SECRET} please"
    long = extract_session_title("word " * 100)
    assert len(long) == 120 and long.endswith("…")
    assert extract_session_title("") == "" and extract_session_title("```\nonly code\n```") == ""


def test_prompt_preview_is_one_scrubbed_line_of_400():
    preview = prompt_preview("first line\n\nsecond   line " + "z " * 400 + SK)
    assert preview.startswith("first line second line")
    assert len(preview) <= 400 and preview.endswith("…") and "\n" not in preview


@pytest.mark.parametrize("system, body, expected", [
    ("Current working directory: /Users/alice/Developer/jev-router\n", None, "jev-router"),
    ("cwd: /home/bob/src/app/", None, "app"),
    ("", {"input": [{"role": "user", "content": "<environment_context>\n  <cwd>/srv/work/billing</cwd>\n</environment_context>"}]}, "billing"),
    ("", {"instructions": "Repository: acme/widgets"}, "widgets"),
    ("cwd: /Users/alice", None, None),  # a home directory names a person, not a project
    ("You are a helpful assistant.", {"messages": [{"role": "user", "content": "hi"}]}, None),
])
def test_workspace_hint_is_a_basename_or_nothing(system, body, expected):
    assert workspace_hint(system, body) == expected


def chat_transcript():
    def call(name, args, cid):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the failing test."},
        {"role": "assistant", "content": None, "tool_calls": [
            call("read_file", {"path": "/Users/alice/src/app/quota.py"}, "c1"),
            call("bash", {"command": "pytest -q"}, "c2"),
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "..."},
        {"role": "tool", "tool_call_id": "c2", "content": "1 failed"},
        {"role": "assistant", "content": "Found it. Patching now.", "tool_calls": [
            call("edit_file", {"file_path": "src/app/quota.py", "patch": "..."}, "c3"),
        ]},
        {"role": "tool", "tool_call_id": "c3", "content": "ok"},
        {"role": "assistant", "content": f"Fixed the race. Token was {SK}."},
        {"role": "user", "content": "Now run the whole suite."},
        {"role": "assistant", "content": None, "tool_calls": [call("bash", {"command": "pytest"}, "c4")]},
        {"role": "tool", "tool_call_id": "c4", "content": "ok"},
    ]


def test_summary_of_the_turn_in_progress():
    out = summarize_transcript_turn(chat_transcript())
    assert out == {"tool_calls": [{"name": "bash", "count": 1}], "assistant_snippet": None, "files_touched": []}


def test_summary_of_the_previous_turn_has_its_final_answer_scrubbed():
    out = summarize_transcript_turn(chat_transcript(), segment=-2)
    assert out["tool_calls"] == [
        {"name": "read_file", "count": 1}, {"name": "bash", "count": 1}, {"name": "edit_file", "count": 1},
    ]
    assert out["assistant_snippet"] == f"Fixed the race. Token was {SECRET}."
    # The home directory, which names a person, is folded to ~.
    assert out["files_touched"] == ["~/src/app/quota.py", "src/app/quota.py"]
    assert user_turn_count(chat_transcript()) == 2


def test_summary_of_a_responses_transcript():
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Add a flag."}]},
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "name": "shell", "call_id": "1", "arguments": '{"cmd": ["ls"]}'},
        {"type": "function_call_output", "call_id": "1", "output": "a b"},
        {"type": "custom_tool_call", "name": "apply_patch", "call_id": "2",
         "input": "*** Begin Patch\n*** Update File: src/cli.py\n@@\n*** End Patch"},
        {"type": "custom_tool_call_output", "call_id": "2", "output": "done"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Added --flag."}]},
    ]
    out = summarize_transcript_turn(items)
    assert out["tool_calls"] == [{"name": "shell", "count": 1}, {"name": "apply_patch", "count": 1}]
    assert out["assistant_snippet"] == "Added --flag."
    assert out["files_touched"] == ["src/cli.py"]
    assert user_turn_count(items) == 1
    assert summarize_transcript_turn("not a list") == {
        "tool_calls": [], "assistant_snippet": None, "files_touched": [],
    }


def test_tool_names_that_are_not_labels_are_counted_as_other():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "has spaces <b>", "arguments": "{}"}},
            {"function": {"name": "ok_name", "arguments": "not json"}},
        ]},
    ]
    out = summarize_transcript_turn(messages)
    assert out["tool_calls"] == [{"name": "other", "count": 1}, {"name": "ok_name", "count": 1}]
