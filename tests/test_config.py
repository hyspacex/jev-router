import copy

import pytest
import yaml

from conftest import BASE_CONFIG, write_config
from jev_router.config import UPSTREAM_ENV, ConfigError, load_config


def load_raw(tmp_path, mutate):
    raw = copy.deepcopy(BASE_CONFIG)
    mutate(raw)
    path = tmp_path / "router.yaml"
    raw["settings"]["sqlite_path"] = str(tmp_path / "router.db")
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


def test_the_shipped_config_is_valid():
    cfg = load_config("router.yaml")
    assert "auto" in cfg.aliases
    assert "gpt-6-astra" in cfg.models
    assert len(cfg.config_hash) == 16


def test_config_hash_changes_with_the_file(tmp_path):
    first = load_config(write_config(tmp_path))
    raw = copy.deepcopy(BASE_CONFIG)
    raw["settings"]["mode"] = "shadow"
    second = load_config(write_config(tmp_path, raw))
    assert first.config_hash != second.config_hash


def test_unknown_model_in_a_rule_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            {"name": "bad", "when": {}, "use": {"model": "nope"}}))
    assert "unknown model 'nope'" in str(exc.value)


def test_unknown_question_in_a_rule_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            {"name": "bad", "when": {"mystery": {"gte": 1}}, "use": {"model": "big"}}))
    message = str(exc.value)
    assert "mystery" in message
    assert "has_tools" in message  # the error lists what it could have been


def test_effort_a_model_does_not_support_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            {"name": "bad", "when": {}, "use": {"model": "small", "effort": "xhigh"}}))
    assert "does not allow effort 'xhigh'" in str(exc.value)


def test_unknown_alias_state_builder_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["aliases"]["auto"].update({"state_builder": "nope"}))
    assert "unknown builder 'nope'" in str(exc.value)


def test_unknown_ruleset_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["aliases"]["auto"].update({"rules": "missing"}))
    assert "unknown ruleset 'missing'" in str(exc.value)


def test_unknown_question_in_an_alias_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["aliases"]["auto"]["questions"].append("nope"))
    assert "unknown question 'nope'" in str(exc.value)


def test_bad_question_type_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["questions"].update({"q": {"type": "essay"}}))
    assert "questions.q" in str(exc.value)


def test_score_question_needs_two_to_ten_levels(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["questions"]["difficulty"].update({"criteria": ["only"]}))
    assert "2 to 10 levels" in str(exc.value)


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["settings"].update({"typo_here": 1}))
    assert "typo_here" in str(exc.value)


def test_missing_file_says_so(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nothing.yaml")
    assert "not found" in str(exc.value)


def test_broken_yaml_says_so(tmp_path):
    path = tmp_path / "router.yaml"
    path.write_text("settings: [unclosed\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "invalid YAML" in str(exc.value)


def test_upstream_env_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv(UPSTREAM_ENV, "http://elsewhere.test:9000")
    cfg = load_config(write_config(tmp_path))
    assert cfg.settings.upstream_base_url == "http://elsewhere.test:9000"


def test_upstream_env_is_ignored_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(UPSTREAM_ENV, "   ")
    cfg = load_config(write_config(tmp_path))
    assert cfg.settings.upstream_base_url == BASE_CONFIG["settings"]["upstream_base_url"]


def test_inline_ruleset_on_an_alias_is_allowed(tmp_path):
    cfg = load_raw(tmp_path, lambda r: r["aliases"]["auto"].update({
        "rules": {
            "rules": [{"name": "always", "when": {}, "use": {"model": "small", "effort": "low"}}],
            "default": {"model": "big", "effort": "medium"},
        }
    }))
    ruleset = cfg.ruleset_for(cfg.aliases["auto"])
    assert ruleset.rules[0].name == "always"
