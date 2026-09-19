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
