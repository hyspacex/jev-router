import pytest

from conftest import make_config
from jev_router.features import extract_features
from jev_router.policy import (
    apply_effort,
    clamp_effort,
    evaluate,
    fits,
    next_model_that_fits,
)


def feats(**body):
    body.setdefault("model", "auto")
    body.setdefault("messages", [{"role": "user", "content": "hello"}])
    headers = body.pop("_headers", {})
    return extract_features(body, headers)


def choice(value, conf=0.9):
    return {"type": "choice", "choice": value, "confidence": conf}


def score(value, conf=0.9):
    return {"type": "score", "score": value, "confidence": conf}


def noul(value):
    return {"type": "noul", "noul": value}


@pytest.fixture
def cfg():
    return make_config()


def test_easy_request_goes_to_the_small_model(cfg):
    answers = {
        "task": choice("quick_question"),
        "difficulty": score(0.2),
        "harm_if_wrong": noul(0.1),
    }
    r = evaluate(cfg, cfg.aliases["auto"], answers, feats())
    assert (r.model, r.effort, r.rule) == ("small", "none", "easy")


def test_hard_request_goes_to_the_big_model(cfg):
    answers = {"task": choice("code"), "difficulty": score(2.0), "harm_if_wrong": noul(0.2)}
    r = evaluate(cfg, cfg.aliases["auto"], answers, feats())
    assert (r.model, r.effort, r.rule) == ("big", "high", "hard")


def test_rules_are_first_match_wins(cfg):
    # Harm fires before difficulty even though both match.
    answers = {"task": choice("code"), "difficulty": score(2.0), "harm_if_wrong": noul(0.9)}
    r = evaluate(cfg, cfg.aliases["auto"], answers, feats())
    assert r.rule == "harmful"


def test_confidence_gate_blocks_a_rule(cfg):
    answers = {
        "task": choice("code", conf=0.5),
        "difficulty": score(1.0),
        "harm_if_wrong": noul(0.1),
    }
    r = evaluate(cfg, cfg.aliases["auto"], answers, feats())
    # conf 0.5 fails confident_code's conf_gte 0.8, and nothing else matches.
    assert r.rule == "default"


def test_low_confidence_gate_fires_first(cfg):
    answers = {"task": choice("code", conf=0.2), "difficulty": score(0.1)}
    r = evaluate(cfg, cfg.aliases["auto"], answers, feats())
    assert (r.rule, r.model, r.effort) == ("low_confidence", "big", "medium")
    assert "0.20" in r.reason


def test_missing_answer_never_matches(cfg):
    r = evaluate(cfg, cfg.aliases["auto"], {}, feats())
    assert r.rule == "default"


def test_feature_condition_matches(cfg):
    answers = {"task": choice("quick_question"), "difficulty": score(1.0)}
    r = evaluate(cfg, cfg.aliases["auto"], answers, feats(tools=[{"function": {"name": "x"}}]))
    assert r.rule == "tools"


def test_alias_max_effort_caps(cfg):
    answers = {"difficulty": score(2.0)}
    r = evaluate(cfg, cfg.aliases["auto-fast"], answers, feats())
    assert r.model == "big"
    assert r.effort == "low"
    assert any("capped" in n for n in r.notes)


def test_client_floor_raises_effort(cfg):
    answers = {"difficulty": score(0.1)}
    r = evaluate(
        cfg, cfg.aliases["auto"], answers, feats(_headers={"X-Router-Client": "floored"})
    )
    assert r.model == "small"
    assert r.effort == "high"  # small allows none/low/high


def test_client_allowed_models_narrow_the_pool(cfg):
    answers = {"difficulty": score(0.1)}
    r = evaluate(
        cfg, cfg.aliases["auto"], answers, feats(_headers={"X-Router-Client": "bigonly"})
    )
    assert r.model == "big"


def test_context_window_filter_moves_off_the_small_model(cfg):
    answers = {"difficulty": score(0.1)}
    big_request = feats(messages=[{"role": "user", "content": "x" * 40000}])
    r = evaluate(cfg, cfg.aliases["auto"], answers, big_request)
    assert r.model == "big"
    assert any("cannot serve" in n for n in r.notes)


def test_vision_filter(cfg):
    answers = {"difficulty": score(0.1)}
    with_image = feats(
        messages=[
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
        ]
    )
    r = evaluate(cfg, cfg.aliases["auto"], answers, with_image)
    assert r.model == "big"


def test_tool_filter_skips_a_model_without_tools():
    cfg = make_config(
        aliases={"auto": {"allowed_models": ["paramy", "big"]}},
        policy={"default": {"model": "paramy", "effort": "low"}},
    )
    answers = {"difficulty": score(0.1)}
    r = evaluate(cfg, cfg.aliases["auto"], answers, feats(tools=[{"function": {"name": "t"}}]))
    assert r.model == "big"


def test_fits_checks_tools_vision_and_window(cfg):
    assert fits(cfg, "small", feats()) is True
    assert fits(cfg, "small", feats(messages=[{"role": "user", "content": "x" * 8000}])) is False
    assert fits(cfg, "paramy", feats(tools=[{"function": {"name": "t"}}])) is False


def test_next_model_that_fits_picks_the_smallest_that_works(cfg):
    big_request = feats(messages=[{"role": "user", "content": "x" * 40000}])
    assert next_model_that_fits(cfg, "small", big_request, ["small", "paramy", "big"]) == "paramy"
    assert next_model_that_fits(cfg, "big", big_request, ["small", "big"]) is None


def test_clamp_effort_moves_down_to_a_supported_level(cfg):
    assert clamp_effort(cfg, "small", "medium") == "low"
    assert clamp_effort(cfg, "small", "xhigh") == "high"
    assert clamp_effort(cfg, "big", "max") == "xhigh"
    assert clamp_effort(cfg, "big", None) is None


def test_apply_effort_suffix_and_param(cfg):
    assert apply_effort(cfg, "big", "high") == ("vendor/big(high)", {})
    assert apply_effort(cfg, "big", None) == ("vendor/big", {})
    name, extra = apply_effort(cfg, "paramy", "high")
    assert (name, extra) == ("vendor/paramy", {"reasoning_effort": "high"})


def test_apply_effort_none_style_drops_the_effort():
    cfg = make_config(
        models={"plain": {"upstream_id": "vendor/plain", "effort_style": "none"}},
        settings={"default_route": {"model": "plain"}},
        policy={"rules": [], "default": {"model": "plain"}},
        aliases={"auto": {"allowed_models": ["plain"], "questions": []}},
    )
    assert apply_effort(cfg, "plain", "high") == ("vendor/plain", {})
