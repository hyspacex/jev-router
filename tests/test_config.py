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


# --- providers, routes, default effort ----------------------------------


def with_providers(raw):
    raw["providers"] = {"cloud": {"description": "A"}, "local": {"description": "B"}}
    raw["models"]["small"]["provider"] = "local"
    raw["models"]["big"]["provider"] = "cloud"
    raw["models"]["paramy"]["provider"] = "cloud"


def test_a_provider_is_required_once_providers_are_declared(tmp_path):
    def mutate(raw):
        with_providers(raw)
        del raw["models"]["big"]["provider"]

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, mutate)
    assert "'provider' is required" in str(exc.value)


def test_an_unknown_provider_on_a_model_is_reported(tmp_path):
    def mutate(raw):
        with_providers(raw)
        raw["models"]["big"]["provider"] = "ghost"

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, mutate)
    assert "unknown provider 'ghost'" in str(exc.value)


def test_naming_a_provider_with_no_providers_block_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["models"]["big"].update({"provider": "cloud"}))
    assert "declares no `providers:` block" in str(exc.value)


def test_a_default_effort_must_be_one_the_model_allows(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["models"]["small"].update({"default_effort": "max"}))
    assert "default_effort" in str(exc.value)
    assert "is not in models.small.efforts" in str(exc.value)


def test_a_default_effort_outside_the_effort_order_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["models"]["small"].update({"default_effort": "turbo"}))
    assert "settings.effort_order" in str(exc.value)


def test_a_good_default_effort_is_accepted(tmp_path):
    cfg = load_raw(tmp_path, lambda r: r["models"]["small"].update({"default_effort": "low"}))
    assert cfg.models["small"].default_effort == "low"


def test_an_unknown_route_in_a_rule_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            {"name": "bad", "when": {}, "use": {"route": "ghost"}}))
    assert "unknown route 'ghost'" in str(exc.value)


def test_a_route_entry_is_checked_like_any_other_model_and_effort(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r.update({"routes": {
            "lane": {"primary": {"model": "small", "effort": "xhigh"}}}}))
    assert "does not allow effort 'xhigh'" in str(exc.value)


def test_a_use_block_must_name_something(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            {"name": "bad", "when": {}, "use": {}}))
    assert "give a 'model' or a 'route'" in str(exc.value)


def test_an_effort_beside_a_route_name_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            {"name": "bad", "when": {}, "use": {"route": "lane", "effort": "high"}}))
    assert "belongs to the route's entries" in str(exc.value)


def test_a_route_and_a_model_together_means_the_model_wins(tmp_path):
    """An overlay that names a model is overriding whatever sat underneath."""
    cfg = load_raw(tmp_path, lambda r: r.update({
        "routes": {"lane": {"primary": {"model": "small", "effort": "low"}}},
        "policy": {**r["policy"], "default": {"route": "lane", "model": "big",
                                              "effort": "medium"}},
    }))
    assert cfg.policy.default.model == "big"
    assert cfg.policy.default.route is None


def test_equivalent_on_a_primary_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r.update({"routes": {
            "lane": {"primary": {"model": "small", "effort": "low", "equivalent": True}}}}))
    assert "'equivalent' belongs on a fallback" in str(exc.value)


# --- pressure-sensitive conditions --------------------------------------


def shift_rule(**cond):
    return {"name": "shifty", "when": {"difficulty": cond}, "use": {"model": "big"}}


def test_a_shift_needs_a_known_provider(tmp_path):
    def mutate(raw):
        with_providers(raw)
        raw["policy"]["rules"].append(shift_rule(gte=2.0, shift_with="ghost", max_shift=0.4))

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, mutate)
    assert "unknown provider 'ghost'" in str(exc.value)


def test_a_shift_needs_both_halves(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            shift_rule(gte=2.0, max_shift=0.4)))
    assert "needs a 'shift_with'" in str(exc.value)

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            shift_rule(gte=2.0, shift_with="default")))
    assert "needs a 'max_shift'" in str(exc.value)


def test_a_shift_needs_a_comparison_to_move(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            shift_rule(eq=2.0, shift_with="default", max_shift=0.4)))
    assert "needs a 'gte', 'lte' or 'conf_gte'" in str(exc.value)


def test_a_negative_max_shift_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r["policy"]["rules"].append(
            shift_rule(gte=2.0, shift_with="default", max_shift=-1)))
    assert "zero or more" in str(exc.value)


def test_an_unknown_quota_source_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: r.update({
            "providers": {"cloud": {"quota": {"source": "telepathy"}}},
            "models": {**r["models"],
                       "small": {**r["models"]["small"], "provider": "cloud"},
                       "big": {**r["models"]["big"], "provider": "cloud"},
                       "paramy": {**r["models"]["paramy"], "provider": "cloud"}},
        }))
    assert "unknown source 'telepathy'" in str(exc.value)


# --- the between-turn effort experiment (spec 12, 10.3) ------------------


def adaptive(raw, **fields):
    """Point the shared test config at the experiment, questions and all."""
    raw["questions"]["corrective_followup"] = {
        "type": "noul",
        "instructions": "Does the latest message name a defect in the earlier result?",
    }
    raw["questions"]["failure_mode"] = {
        "type": "choice",
        "instructions": "Why has the work not succeeded yet?",
        "criteria": {"reasoning_problem": "The logic is wrong.", "unclear": "Cannot say."},
    }
    raw["session_routing"] = {"enabled": True}
    raw["aliases"]["auto-session"] = {
        "session_mode": "strict",
        "adaptive_effort": True,
        "state_builder": "summary_v1",
        "questions": ["task", "difficulty", "harm_if_wrong"],
        "rules": "policy",
        "allowed_models": ["big"],
    }
    raw["experiments"] = {
        "adaptive_effort": {
            "mode": "shadow",
            "qualified_profiles": ["big"],
            "ladder": ["low", "medium", "high"],
            **fields,
        }
    }


def test_an_omitted_experiments_block_means_off():
    cfg = load_config("router.yaml")
    assert cfg.adaptive_effort().mode == "off"
    assert cfg.adaptive_effort().qualified_profiles == []
    # And no shipped model claims a between-turn strategy.
    for mid, mcfg in cfg.models.items():
        assert mcfg.effort_control.between_turn == "fixed", mid
        assert mcfg.effort_control.qualification == "unverified", mid


def test_shadow_mode_needs_nothing_verified(tmp_path):
    cfg = load_raw(tmp_path, adaptive)
    assert cfg.adaptive_effort().mode == "shadow"
    assert cfg.effort_ladder("big") == ["low", "medium", "high"]


def test_active_mode_is_refused_on_an_unverified_profile(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: adaptive(r, mode="active"))
    text = str(exc.value)
    assert "declares no between-turn strategy" in text
    assert "still 'unverified'" in text


def test_active_mode_is_refused_with_no_qualified_profile(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: adaptive(r, mode="active", qualified_profiles=[]))
    assert "needs at least one profile in qualified_profiles" in str(exc.value)


def test_active_mode_needs_a_qualification_report_that_exists(tmp_path):
    def mutate(raw):
        adaptive(raw, mode="active")
        raw["models"]["big"]["effort_style"] = "param"
        raw["models"]["big"]["effort_control"] = {
            "between_turn": "native_configuration_update",
            "qualification": "verified",
            "qualification_ref": "docs/qualification/no-such-report.md",
        }

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, mutate)
    assert "does not name a file that exists" in str(exc.value)


def test_active_mode_loads_with_a_real_report_beside_the_config(tmp_path):
    (tmp_path / "report.md").write_text("# a dated deployment report\n")

    def mutate(raw):
        adaptive(raw, mode="active")
        raw["models"]["big"]["effort_style"] = "param"
        raw["models"]["big"]["effort_control"] = {
            "between_turn": "native_configuration_update",
            "qualification": "verified",
            "qualification_ref": "report.md: verdict verified",
        }

    cfg = load_raw(tmp_path, mutate)
    assert cfg.adaptive_effort().mode == "active"
    assert cfg.models["big"].effort_control.between_turn == "native_configuration_update"


def test_verified_without_a_reference_is_refused(tmp_path):
    def mutate(raw):
        adaptive(raw)
        raw["models"]["big"]["effort_control"] = {"qualification": "verified"}

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, mutate)
    assert "qualification_ref" in str(exc.value)


def test_a_legacy_alias_cannot_opt_into_the_experiment(tmp_path):
    def mutate(raw):
        adaptive(raw)
        raw["aliases"]["auto"]["adaptive_effort"] = True

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, mutate)
    assert "needs a strict session" in str(exc.value)


def test_a_native_profile_cannot_encode_its_effort_in_the_model_name(tmp_path):
    def mutate(raw):
        adaptive(raw, mode="active")
        raw["models"]["big"]["effort_control"] = {
            "between_turn": "native_configuration_update",
            "qualification": "verified",
            "qualification_ref": "router.yaml",
        }

    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, mutate)
    assert "encodes the effort in the model name" in str(exc.value)


def test_a_ladder_rung_the_model_does_not_offer_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_raw(tmp_path, lambda r: adaptive(r, ladders={"big": ["low", "none"]}))
    assert "does not offer effort 'none'" in str(exc.value)


def test_the_three_opt_ins_all_have_to_agree(tmp_path):
    cfg = load_raw(tmp_path, adaptive)
    strict = cfg.aliases["auto-session"]
    legacy = cfg.aliases["auto"]
    assert cfg.adaptation_mode(strict, turn_boundaries=True) == "shadow"
    assert cfg.adaptation_mode(strict, turn_boundaries=False) == "off"
    assert cfg.adaptation_mode(legacy, turn_boundaries=True) == "off"
    cfg.experiments.adaptive_effort.mode = "off"
    assert cfg.adaptation_mode(strict, turn_boundaries=True) == "off"


def test_the_example_overlay_is_valid_yaml_and_stays_out_of_active():
    raw = yaml.safe_load(open("examples/adaptive-effort.yaml").read())
    assert raw["experiments"]["adaptive_effort"]["mode"] == "shadow"
    assert raw["aliases"]["auto-session-adaptive"]["adaptive_effort"] is True
