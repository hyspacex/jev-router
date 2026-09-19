from jev_router.features import extract_features


def test_plain_request():
    body = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "What is 2 + 2?"},
        ],
        "max_tokens": 64,
    }
    f = extract_features(body, {"X-Router-Client": "agent-cli", "Authorization": "Bearer abc"})
    assert f.model == "auto"
    assert f.system_prompt == "You are terse."
    assert f.first_user_message == "What is 2 + 2?"
    assert f.last_user_message == "What is 2 + 2?"
    assert f.message_count == 2
    assert f.has_tools is False
    assert f.has_images is False
    assert f.has_code is False
    assert f.client == "agent-cli"
    assert f.auth_header == "Bearer abc"
    assert f.max_tokens == 64
    assert f.stream is False


def test_token_estimate_is_chars_over_four():
    text = "x" * 400
    f = extract_features({"model": "auto", "messages": [{"role": "user", "content": text}]})
    assert f.est_tokens == 100
    assert f.total_chars == 400
    assert f.budget_tokens == 100


def test_budget_adds_max_tokens():
    f = extract_features(
        {"model": "auto", "messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 900}
    )
    assert f.budget_tokens == 1000


def test_tools_and_languages():
    body = {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "```python\nprint(1)\n```"},
            {"role": "user", "content": "now fix ```sql\nselect 1\n```"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "read_file", "parameters": {}}},
            {"type": "function", "function": {"name": "run", "parameters": {}}},
        ],
        "stream": True,
    }
    f = extract_features(body)
    assert f.has_tools is True
    assert f.tool_names == ("read_file", "run")
    assert f.has_code is True
    assert f.languages == ("python", "sql")
    assert f.first_user_message == "first"
    assert f.last_user_message.startswith("now fix")
    assert f.stream is True


def test_image_parts_and_multipart_text():
    body = {
        "model": "auto",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ],
    }
    f = extract_features(body)
    assert f.has_images is True
    assert f.last_user_message == "what is this"


def test_tool_call_arguments_count_towards_text():
    body = {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "run", "arguments": '{"cmd": "ls -la"}'}}
                ],
            },
        ],
    }
    f = extract_features(body)
    assert "ls -la" in f.messages[1].text
    assert f.message_count == 2


def test_max_completion_tokens_alias_and_bad_value():
    f = extract_features({"model": "auto", "messages": [], "max_completion_tokens": 100})
    assert f.max_tokens == 100
    f2 = extract_features({"model": "auto", "messages": [], "max_tokens": "lots"})
    assert f2.max_tokens == 0


def test_to_facts_holds_no_message_text():
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": "a secret sentence"}],
    }
    facts = extract_features(body).to_facts()
    blob = repr(facts)
    assert "secret" not in blob
    assert facts["message_count"] == 1
    assert facts["last_user_message_chars"] == len("a secret sentence")
