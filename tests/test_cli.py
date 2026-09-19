import json

import httpx
import pytest
import respx

from conftest import write_config
from jev_router.cli import main
from jev_router.config import load_config
from jev_router.pins import Store, new_decision_id

JEV_URL = "https://jev.test/v1/systemone"


def log_one(config_path, **kwargs):
    cfg = load_config(config_path)
    store = Store(cfg.settings.db_path)
    decision_id = kwargs.pop("decision_id", new_decision_id())
    row = dict(
        decision_id=decision_id,
        alias="auto",
        client="",
        conversation_key="k",
        state_builder="summary_v1",
        answers={"task": {"type": "choice", "choice": "code", "confidence": 0.9}},
        features={"message_count": 1},
        config_hash=cfg.config_hash,
        model="small",
        effort="none",
        rule="easy",
        mode="active",
        fallback=False,
        pinned=False,
        jev_ms=180.0,
        jev_tokens=400,
        est_tokens=10,
        message_count=1,
    )
    row.update(kwargs)
    store.log_decision(**row)
    store.close()
    return decision_id


def test_check_config_prints_a_summary(tmp_path, capsys):
    path = write_config(tmp_path)
    assert main(["-c", path, "check-config"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out
    assert "alias auto:" in out


def test_check_config_reports_a_broken_file(tmp_path, capsys):
    path = tmp_path / "router.yaml"
    path.write_text("models: {}\n")
    with pytest.raises(SystemExit) as exc:
        main(["-c", str(path), "check-config"])
    assert exc.value.code == 2
    assert "settings" in capsys.readouterr().err


def test_decisions_shows_ids_and_feedback(tmp_path, capsys):
    path = write_config(tmp_path)
    decision_id = log_one(path)
    cfg = load_config(path)
    store = Store(cfg.settings.db_path)
    store.add_feedback(
        decision_id=decision_id, verdict="too_weak", better_model="big", source="cli"
    )
    store.close()

    assert main(["-c", path, "decisions", "-n", "5"]) == 0
    out = capsys.readouterr().out
    assert decision_id in out
    assert "rule=easy" in out
    assert "feedback: too_weak -> big" in out


def test_decisions_json_output(tmp_path, capsys):
    path = write_config(tmp_path)
    decision_id = log_one(path)
    assert main(["-c", path, "decisions", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["decision_id"] == decision_id


def test_feedback_command_records_a_row(tmp_path, capsys):
    path = write_config(tmp_path)
    decision_id = log_one(path)
    code = main(
        ["-c", path, "feedback", decision_id, "too_strong", "--model", "small",
         "--effort", "low", "--note", "a smaller model was fine"]
    )
    assert code == 0
    assert "recorded too_strong" in capsys.readouterr().out

    cfg = load_config(path)
    store = Store(cfg.settings.db_path)
    rows = store.recent_feedback(5)
    store.close()
    assert rows[0]["verdict"] == "too_strong"
    assert rows[0]["better_model"] == "small"
    assert rows[0]["source"] == "cli"
    assert rows[0]["note"] == "a smaller model was fine"


def test_feedback_last_resolves_the_newest(tmp_path, capsys):
    path = write_config(tmp_path)
    log_one(path)
    newest = log_one(path)
    assert main(["-c", path, "feedback", "last", "right"]) == 0
    assert newest in capsys.readouterr().out


def test_feedback_last_with_a_client(tmp_path, capsys):
    path = write_config(tmp_path)
    mine = log_one(path, client="agent-cli")
    log_one(path, client="chat-ui")
    assert main(["-c", path, "feedback", "last", "right", "--client", "agent-cli"]) == 0
    assert mine in capsys.readouterr().out


def test_feedback_rejects_an_unknown_model(tmp_path, capsys):
    path = write_config(tmp_path)
    decision_id = log_one(path)
    assert main(["-c", path, "feedback", decision_id, "right", "--model", "ghost"]) == 2
    assert "not configured" in capsys.readouterr().err


def test_feedback_rejects_an_unknown_decision(tmp_path, capsys):
    path = write_config(tmp_path)
    log_one(path)
    assert main(["-c", path, "feedback", "0000deadbeef", "right"]) == 1
    assert "unknown decision id" in capsys.readouterr().err


def test_feedback_with_no_decisions_at_all(tmp_path, capsys):
    path = write_config(tmp_path)
    assert main(["-c", path, "feedback", "last", "right"]) == 1
    assert "no decisions" in capsys.readouterr().err


def test_feedback_rejects_a_bad_verdict(tmp_path):
    path = write_config(tmp_path)
    with pytest.raises(SystemExit):  # argparse rejects it before we look at the store
        main(["-c", path, "feedback", "last", "excellent"])


@respx.mock
def test_explain_prints_features_state_and_decision(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    path = write_config(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "write a merge sort"}],
                "_headers": {"X-Router-Client": "agent-cli"},
            }
        )
    )
    respx.post(JEV_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    "task": {"type": "choice", "choice": "code", "confidence": 0.95},
                    "difficulty": {"type": "score", "score": 1.6, "confidence": 0.8},
                    "harm_if_wrong": {"type": "noul", "noul": 0.2},
                },
                "usage": {"input_tokens": 300},
            },
        )
    )
    assert main(["-c", path, "explain", str(request)]) == 0
    out = capsys.readouterr().out
    assert "features:" in out
    assert "state (summary_v1)" in out
    assert "write a merge sort" in out
    assert "model:    big" in out
    assert "rule:     hard" in out
    assert "jev_input_tokens: 300" in out


def test_explain_on_a_non_alias_request(tmp_path, capsys):
    path = write_config(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"model": "vendor/small", "messages": []}))
    assert main(["-c", path, "explain", str(request)]) == 1
    assert "forwarded unchanged" in capsys.readouterr().err


# --- providers, routes and quota ----------------------------------------


def shifting_config(tmp_path):
    """A config with two providers, one route and one shifted cutoff."""
    import copy

    from conftest import BASE_CONFIG

    raw = copy.deepcopy(BASE_CONFIG)
    raw["providers"] = {
        "cloud": {"quota": {"source": "static", "enabled": True,
                            "config": {"used_percent": 95, "expected_used_percent": 10}}},
        "local": {},
    }
    raw["models"]["small"]["provider"] = "local"
    raw["models"]["big"]["provider"] = "cloud"
    raw["models"]["paramy"]["provider"] = "cloud"
    raw["routes"] = {
        "fast": {"primary": {"model": "small", "effort": "none"}},
        "frontier": {"primary": {"model": "big", "effort": "high"},
                     "fallbacks": [{"model": "small", "effort": "low"}]},
    }
    raw["policy"] = {
        "rules": [
            {"name": "hard",
             "when": {"difficulty": {"gte": 1.5, "shift_with": "cloud", "max_shift": 1.0}},
             "use": {"route": "frontier"}},
            {"name": "easy", "when": {}, "use": {"route": "fast"}},
        ],
        "default": {"route": "frontier"},
    }
    return write_config(tmp_path, raw)


def test_check_config_lists_providers_and_routes(tmp_path, capsys):
    path = shifting_config(tmp_path)
    assert main(["-c", path, "check-config"]) == 0
    out = capsys.readouterr().out
    assert "providers: cloud, local" in out
    assert "routes:    fast, frontier" in out
    assert "provider cloud: quota=static (enabled)" in out
    assert "route frontier: big(high) -> small(low)" in out


def test_quota_without_a_poll_reports_neutral(tmp_path, capsys):
    path = shifting_config(tmp_path)
    assert main(["-c", path, "quota"]) == 0
    out = capsys.readouterr().out
    assert "quota routing: on" in out
    assert "pressure:  0.00" in out
    assert "pass --poll" in out


def test_quota_with_a_poll_reads_the_source(tmp_path, capsys):
    path = shifting_config(tmp_path)
    assert main(["-c", path, "quota", "--poll"]) == 0
    out = capsys.readouterr().out
    assert "cloud: source=static enabled" in out
    assert "pressure:  1.00" in out
    assert "used:      95.0%" in out


def test_quota_json_output(tmp_path, capsys):
    path = shifting_config(tmp_path)
    assert main(["-c", path, "quota", "--poll", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["providers"]["cloud"]["snapshot"]["used_percent"] == 95
    assert body["effective_pressure"]["cloud"] == 1.0
    assert body["effective_pressure"]["local"] == 0.0


def explain_request(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"model": "auto", "messages": [{"role": "user", "content": "fix this"}]})
    )
    return str(request)


def answer_jev(difficulty=2.0):
    return respx.post(JEV_URL).mock(
        return_value=httpx.Response(
            200,
            json={"answers": {
                "task": {"type": "choice", "choice": "code", "confidence": 0.95},
                "difficulty": {"type": "score", "score": difficulty, "confidence": 0.9},
                "harm_if_wrong": {"type": "noul", "noul": 0.1},
            }},
        )
    )


@respx.mock
def test_explain_shows_what_pressure_would_do(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    path = shifting_config(tmp_path)
    request = explain_request(tmp_path)
    answer_jev(difficulty=2.0)

    assert main(["-c", path, "explain", request]) == 0
    calm = capsys.readouterr().out
    assert "model:    big" in calm
    assert "route:    frontier" in calm
    assert "plan:     big(high) -> small(low)" in calm

    assert main(["-c", path, "explain", request, "--pressure", "cloud=1.0"]) == 0
    pressed = capsys.readouterr().out
    assert "cloud: 1.00" in pressed
    assert "model:    small" in pressed
    assert "shifted:  hard.difficulty gte 1.5 -> 2.5" in pressed


@respx.mock
def test_explain_says_when_pressure_changed_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    path = shifting_config(tmp_path)
    answer_jev(difficulty=3.0)
    assert main(["-c", path, "explain", explain_request(tmp_path),
                 "--pressure", "cloud=0.2"]) == 0
    out = capsys.readouterr().out
    assert "pressure did not change this decision" in out


@pytest.mark.parametrize(
    "value, message",
    [
        ("ghost=0.5", "unknown provider"),
        ("cloud=2", "outside 0 to 1"),
        ("cloud=high", "is not a number"),
        ("cloud", "provider=number"),
    ],
)
def test_explain_rejects_a_bad_pressure(tmp_path, capsys, value, message):
    path = shifting_config(tmp_path)
    assert main(["-c", path, "explain", explain_request(tmp_path),
                 "--pressure", value]) == 2
    assert message in capsys.readouterr().err
