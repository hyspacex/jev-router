"""Tests for the programmatic graders in evals/checks.py.

They run no model: each one hands a grader a reply written by hand. The two
graders that execute model output (`python_tests` and `sqlite`) are covered
here even though no case in cases.yaml uses them, since they are the ones with
teeth.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import checks  # noqa: E402


def test_contains_all_scores_the_fraction_found():
    assert checks.contains_all("the port is 5432", ["5432"]) == (1.0, "")
    value, note = checks.contains_all("postgres, but no number", ["5432", "postgres"])
    assert value == 0.5
    assert "5432" in note


def test_max_words_allows_a_little_slack_then_stops():
    assert checks.max_words(" ".join(["w"] * 100), limit=120)[0] == 1.0
    assert checks.max_words(" ".join(["w"] * 130), limit=120)[0] == 0.5
    assert checks.max_words(" ".join(["w"] * 300), limit=120)[0] == 0.0


def test_numeric_answer_finds_the_number_anywhere():
    assert checks.numeric_answer("15% of 3,480 is 522.", expected=522)[0] == 1.0
    assert checks.numeric_answer("I think it is 520", expected=522)[0] == 0.0


def test_regex_examples_joins_a_split_pattern_and_ignores_group_names():
    reply = """
```python
import re
LOG = re.compile(
    r'^(?P<client_ip>\\S+)\\s+'     # the address
    r'\\S+\\s+\\S+\\s+'
    r'\\[[^\\]]+\\]\\s+'
    r'"[^"]*"\\s+'
    r'(?P<code>\\d{3})'
)
```
"""
    value, note = checks.regex_examples(
        reply,
        should_match=[
            '10.4.19.7 - - [18/Sep/2026:09:14:02 +0000] "GET /v1 HTTP/1.1" 200 4821',
            '2001:db8::5b - alice [18/Sep/2026:09:14:03 +0000] "POST /v1 HTTP/1.1" 422 190',
        ],
        groups={"ip": ["10.4.19.7", "2001:db8::5b"], "status": ["200", "422"]},
    )
    assert value == 1.0, note


def test_regex_examples_fails_a_pattern_that_misses_ipv6():
    reply = "```python\nrx = re.compile(r'^(?P<ip>\\\\d+\\\\.\\\\d+\\\\.\\\\d+\\\\.\\\\d+)')\n```"
    value, _ = checks.regex_examples(
        reply,
        should_match=["10.4.19.7 - - x", "2001:db8::5b - alice x"],
        groups={"ip": ["10.4.19.7", "2001:db8::5b"]},
    )
    assert value < 1.0


def test_json_fields_compares_numbers_loosely_and_strings_exactly():
    reply = 'Here:\n```json\n{"net": 7367.0, "currency": "dkk", "vat": 1841.75}\n```'
    value, _ = checks.json_fields(
        reply, expected={"net": 7367.00, "currency": "DKK", "vat": 1841.75}
    )
    assert value == 1.0
    value, note = checks.json_fields(reply, expected={"net": 1.0})
    assert value == 0.0
    assert "net" in note


def test_json_fields_with_no_json_scores_zero():
    assert checks.json_fields("no json here", expected={"a": 1}) == (
        0.0,
        "no JSON object in the reply",
    )


def test_python_tests_runs_the_reply_in_a_sandbox():
    good = "```python\ndef add(a, b):\n    return a + b\n```"
    assert checks.python_tests(good, tests="assert add(2, 3) == 5") == (1.0, "")
    bad = "```python\ndef add(a, b):\n    return a * b\n```"
    value, note = checks.python_tests(bad, tests="assert add(2, 3) == 5")
    assert value == 0.0
    assert "AssertionError" in note


def test_python_tests_gives_up_on_a_loop():
    checks.RUN_TIMEOUT_S = 3
    value, note = checks.python_tests("```python\nwhile True:\n    pass\n```")
    assert value == 0.0
    assert note == "timed out"


def test_sqlite_runs_the_query_and_compares_rows():
    setup = (
        "CREATE TABLE events (user_id INT, kind TEXT);"
        "INSERT INTO events VALUES (1,'view'),(1,'checkout'),(2,'view');"
    )
    good = "```sql\nSELECT user_id, COUNT(*) FROM events GROUP BY user_id ORDER BY user_id\n```"
    assert checks.sqlite_query(good, setup=setup, expected_rows=[[1, 2], [2, 1]]) == (1.0, "")
    wrong = "```sql\nSELECT user_id FROM events ORDER BY user_id\n```"
    value, note = checks.sqlite_query(wrong, setup=setup, expected_rows=[[1, 2], [2, 1]])
    assert value == 0.0
    assert "wanted" in note


def test_sqlite_reports_a_syntax_error_rather_than_raising():
    value, note = checks.sqlite_query("```sql\nSELEKT nope\n```", setup="")
    assert value == 0.0
    assert note


def test_run_checks_weights_and_returns_notes():
    specs = [
        {"kind": "contains_all", "patterns": ["yes"], "weight": 3},
        {"kind": "contains_all", "patterns": ["no"], "weight": 1},
    ]
    score, notes = checks.run_checks("yes", specs)
    assert score == 7.5
    assert len(notes) == 1


def test_an_empty_reply_scores_zero():
    assert checks.run_checks("   ", [{"kind": "contains_all", "patterns": ["x"]}]) == (
        0.0,
        ["empty reply"],
    )


def test_an_unknown_check_is_an_error_naming_the_known_ones():
    try:
        checks.run_checks("x", [{"kind": "nope"}])
    except KeyError as exc:
        assert "contains_all" in str(exc)
    else:
        raise AssertionError("expected KeyError")
