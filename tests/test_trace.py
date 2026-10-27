"""`policy.trace` agrees with `select` because it is `select`, instrumented.

Every test here is offline: the policy is pure, so a trace is a function of
the config, the answers, the features and the pressures.
"""

from __future__ import annotations

import copy
import dataclasses
import random
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import make_config
from test_lanes import LANE_CONFIG
from test_low_confidence_tail import _two_scores
from test_quota_routing import shifting_config
from test_semantic_packet import experiment_config, gated_config

from jev_router.config import RouterConfig, load_config
from jev_router.features import Features
from jev_router.policy import RoutingError, evaluate, select, trace

ROOT = Path(__file__).resolve().parents[1]


def shipped() -> RouterConfig:
    return load_config(ROOT / "router.yaml")


def shipped_legacy() -> RouterConfig:
    """router.yaml with its aliases on the legacy path, where shifts apply."""
    raw = yaml.safe_load((ROOT / "router.yaml").read_text())
    for alias in raw["aliases"].values():
        alias["session_mode"] = "legacy"
        for key in ("admission_fallback", "adaptive_effort"):
            alias.pop(key, None)
    return RouterConfig.model_validate(raw)


def shipped_with_shifts() -> RouterConfig:
    """Legacy router.yaml with quota shifts on two rules, one protected."""
    raw = yaml.safe_load((ROOT / "router.yaml").read_text())
    for alias in raw["aliases"].values():
        alias["session_mode"] = "legacy"
        for key in ("admission_fallback", "adaptive_effort"):
            alias.pop(key, None)
    provider = min(raw["providers"])
    for rule in raw["policy"]["rules"]:
        if rule["name"] in ("hard", "high_harm"):
            for cond in rule["when"].values():
                if isinstance(cond, dict) and ("gte" in cond or "lte" in cond):
                    cond.update({"shift_with": provider, "max_shift": 0.8})
    return RouterConfig.model_validate(raw)


def choice(value, conf=0.9, probs=None):
    out = {"type": "choice", "choice": value, "confidence": conf}
    if probs is not None:
        out["probabilities"] = probs
    return out


def score(value, conf=0.9, probs=None):
    out = {"type": "score", "score": value, "confidence": conf}
    if probs is not None:
        out["probabilities"] = probs
    return out


def noul(value):
    return {"type": "noul", "noul": value}


# --- the invariants every trace must satisfy ------------------------------


def check(
    cfg: RouterConfig,
    alias: str,
    answers: dict[str, Any],
    features: Features,
    pressures: dict[str, float] | None = None,
) -> dict[str, Any]:
    alias_cfg = cfg.aliases[alias]
    before = copy.deepcopy(answers)
    t = trace(cfg, alias_cfg, answers, features, pressures)
    assert answers == before, "trace must not touch the answers"
    try:
        r = select(cfg, alias_cfg, answers, features, pressures)
    except RoutingError as exc:
        assert t["route"] is None and t["model"] is None
        assert t["error"] == f"routing_error: {exc}"
        r = None
    if r is not None:
        assert "error" not in t
        assert t["matched"] == r.rule
        assert t["route"] == r.route
        assert (t["model"], t["effort"]) == (r.model, r.effort)
        assert t["lane"] == (r.lane or None)

    rules = t["rules"]
    assert [x["name"] for x in rules] == [x.name for x in cfg.ruleset_for(alias_cfg).rules]
    hits = [x for x in rules if x["outcome"] == "hit"]
    if t["matched"] in ("default", "low_confidence"):
        assert hits == []
    else:
        assert [x["name"] for x in hits] == [t["matched"]]

    if t["matched"] == "low_confidence":
        assert t["gate"]["applies"] and t["gate"]["passed"] is False
        assert t["gate"]["fired_by"] in t["gate"]["questions"]
        assert {x["outcome"] for x in rules} <= {"not_reached"}
    elif t["gate"]["applies"]:
        assert t["gate"]["passed"] is True

    after_hit = False
    for x in rules:
        conds = x["conditions"]
        if after_hit or t["matched"] == "low_confidence":
            assert x["outcome"] == "not_reached"
            assert all(c["passed"] is None for c in conds)
            continue
        assert x["outcome"] in ("hit", "miss", "skipped_experiment")
        assert all(isinstance(c["passed"], bool) for c in conds)
        if x["outcome"] == "hit":
            assert all(c["passed"] for c in conds)
            after_hit = True
        elif x["outcome"] == "miss":
            assert any(c["passed"] is False for c in conds)
        for c in conds:
            assert c["source"] in ("feature", "answer")
            if c["key"] in ("tool_names_include", "languages_include"):
                assert c["value"] is None
    if t["matched"] == "default":
        assert all(x["outcome"] in ("miss", "skipped_experiment") for x in rules)
    return t


# --- the worked example ---------------------------------------------------


def test_the_shipped_ladder_example():
    cfg = shipped()
    answers = {
        "task": choice("debugging", 0.98),
        "difficulty": score(2.33, 0.67),
        "harm_if_wrong": noul(0.27),
    }
    f = Features(model="auto", has_tools=True, message_count=2, est_tokens=26399)
    t = check(cfg, "auto", answers, f)
    assert (t["matched"], t["route"]) == ("moderate_tools", "mid_tools")
    assert t["gate"] == {
        "applies": True,
        "min_confidence": 0.4,
        "questions": {"task": 0.98, "difficulty": 0.67},
        "checks": [
            {"question": "task", "outcome": "confident"},
            {"question": "difficulty", "outcome": "confident"},
        ],
        "fired_by": None,
        "passed": True,
        "detail": "no gated answer below 0.4",
    }
    by_name = {x["name"]: x for x in t["rules"]}
    assert by_name["images_easy"]["conditions"][0] == {
        "key": "has_images",
        "source": "feature",
        "op": "eq",
        "threshold": True,
        "value": False,
        "passed": False,
    }
    hard = by_name["hard"]
    assert hard["outcome"] == "miss"
    assert hard["conditions"] == [
        {
            "key": "difficulty",
            "source": "answer",
            "op": "gte",
            "threshold": 2.4,
            "value": 2.33,
            "passed": False,
        }
    ]
    careful = by_name["moderate_careful"]["conditions"]
    assert [(c["key"], c["passed"]) for c in careful] == [
        ("task", True),
        ("difficulty", True),
        ("harm_if_wrong", False),
    ]
    hit = by_name["moderate_tools"]
    assert hit["outcome"] == "hit" and hit["lane"] == "moderate-tools"
    assert [(c["key"], c["source"], c["passed"]) for c in hit["conditions"]] == [
        ("has_tools", "feature", True),
        ("difficulty", "answer", True),
    ]
    assert by_name["moderate"]["outcome"] == "not_reached"
    assert by_name["moderate"]["conditions"][0]["passed"] is None
    assert by_name["easy"] == {**by_name["easy"], "outcome": "not_reached", "conditions": []}


def test_the_gate_firing_leaves_every_rule_unreached():
    cfg = shipped()
    answers = {
        "task": choice("debugging", 0.2),
        "difficulty": score(2.9, 0.9),
        "harm_if_wrong": noul(0.1),
    }
    t = check(cfg, "auto", answers, Features(model="auto"))
    assert t["matched"] == "low_confidence"
    assert t["gate"]["fired_by"] == "task"
    assert t["gate"]["detail"] == "task confidence 0.20 below 0.4"
    assert t["gate"]["checks"][-1]["outcome"] == "fired"


def test_a_noisy_top_level_stands_the_gate_down():
    cfg = shipped()
    answers = {
        "task": choice("agentic-tool-task", 0.95),
        "difficulty": score(0.68, 0.32, {"0": 0.5, "1": 0.33, "2": 0.16, "3": 0.01}),
        "harm_if_wrong": noul(0.37),
    }
    t = check(cfg, "auto", answers, Features(model="auto", has_tools=True))
    assert t["gate"]["passed"] is True
    noisy = t["gate"]["checks"][-1]
    assert noisy["question"] == "difficulty" and noisy["outcome"] == "top_is_noise"
    assert noisy["top_mass"] == pytest.approx(0.01)
    assert "stood down for difficulty" in t["gate"]["detail"]


def test_action_equivalence_is_recorded_on_the_gate():
    from test_semantic_packet import TORN_BETWEEN_EQUIVALENT

    t = check(gated_config(True), "auto", TORN_BETWEEN_EQUIVALENT, Features(model="auto"))
    assert t["gate"]["checks"][0]["outcome"] == "action_equivalent"
    assert t["matched"] == "codey"
    t = check(gated_config(False), "auto", TORN_BETWEEN_EQUIVALENT, Features(model="auto"))
    assert t["matched"] == "low_confidence"


def test_a_shift_that_decides_is_on_the_rule_and_the_condition():
    cfg = shifting_config()
    answers = {"task": choice("code"), "difficulty": score(1.6), "harm_if_wrong": noul(0.1)}
    t = check(cfg, "auto", answers, Features(model="auto"), {"cloud": 1.0})
    assert t["matched"] == "easy"
    hard = next(x for x in t["rules"] if x["name"] == "hard")
    assert hard["outcome"] == "miss"
    assert hard["shift"]["decisive"] is True
    assert hard["shift"]["shifts"][0]["effective"] == pytest.approx(2.5)
    cond = hard["conditions"][0]
    assert cond["passed"] is False and cond["threshold"] == 1.5
    assert cond["effective_threshold"] == pytest.approx(2.5)
    calm = check(cfg, "auto", answers, Features(model="auto"))
    assert calm["matched"] == "hard"


def test_a_protected_rule_under_pressure_is_judged_at_zero():
    cfg = shifting_config()
    answers = {"task": choice("code"), "difficulty": score(0.1), "harm_if_wrong": noul(0.85)}
    t = check(cfg, "auto", answers, Features(model="auto"), {"cloud": 1.0})
    risky = t["rules"][0]
    assert t["matched"] == "risky"
    assert risky["protected"] and risky["pressure_ignored"]
    assert risky["shift"] is None
    assert risky["conditions"][0]["passed"] is True
    assert "effective_threshold" not in risky["conditions"][0]


@pytest.mark.parametrize("mode", ["off", "shadow", "active"])
def test_an_experimental_rule_is_skipped_unless_active(mode):
    cfg = experiment_config(mode)
    answers = {"difficulty": score(1.5, probs={"0": 0.2, "1": 0.3, "2": 0.45, "3": 0.05})}
    t = check(cfg, "auto", answers, Features(model="auto"))
    first = t["rules"][0]
    assert first["experimental"] is True
    assert t["distribution_policy"] == mode
    if mode == "active":
        assert (first["outcome"], t["matched"]) == ("hit", "mechanical_cheap")
        assert first["conditions"][0]["value"] == pytest.approx(0.05)
    else:
        assert (first["outcome"], t["matched"]) == ("skipped_experiment", "otherwise")
    assert bool(t["experiments"]) == (mode != "off")


def test_a_routing_error_is_reported_not_raised(config):
    # Larger than every model's window: nothing can serve it.
    f = Features(model="auto", est_tokens=10_000_000)
    answers = {"task": choice("code"), "difficulty": score(2.0), "harm_if_wrong": noul(0.1)}
    with pytest.raises(RoutingError):
        select(config, config.aliases["auto"], answers, f)
    t = check(config, "auto", answers, f)
    assert t["matched"] == "hard" and t["route"] is None


# --- the collector changes nothing ---------------------------------------


def _fields(result) -> dict[str, Any]:
    return dataclasses.asdict(result)


def test_the_collector_leaves_every_result_field_unchanged():
    rng = random.Random(7)
    for cfg, alias in CONFIGS():
        alias_cfg = cfg.aliases[alias]
        for _ in range(40):
            answers = random_answers(rng, cfg)
            f = random_features(rng, cfg)
            p = random_pressures(rng, cfg)
            for frozen in (False, True):
                try:
                    plain = evaluate(cfg, alias_cfg, answers, f, p, freeze_rule_pressure=frozen)
                except RoutingError as exc:
                    with pytest.raises(RoutingError, match=str(exc)[:20]):
                        evaluate(cfg, alias_cfg, answers, f, p,
                                 freeze_rule_pressure=frozen, collector={})
                    continue
                seen: dict[str, Any] = {}
                traced = evaluate(
                    cfg, alias_cfg, answers, f, p,
                    freeze_rule_pressure=frozen, collector=seen,
                )
                assert _fields(plain) == _fields(traced)
                assert "rules" in seen or "gate_checks" in seen

            def collecting(*args, **kwargs):
                return evaluate(*args, collector={}, **kwargs)

            try:
                plain = select(cfg, alias_cfg, answers, f, p)
            except RoutingError:
                with pytest.raises(RoutingError):
                    select(cfg, alias_cfg, answers, f, p, evaluate_rules=collecting)
                continue
            traced = select(cfg, alias_cfg, answers, f, p, evaluate_rules=collecting)
            assert _fields(plain) == _fields(traced)


# --- the sweep ------------------------------------------------------------


def CONFIGS() -> list[tuple[RouterConfig, str]]:
    base = make_config()
    laned = make_config(**LANE_CONFIG)
    out = [
        (base, "auto"),
        (base, "auto-fast"),
        (shifting_config(), "auto"),
        (laned, "auto"),
        (gated_config(True), "auto"),
        (gated_config(False), "auto"),
        (_two_scores(), "auto"),
        (experiment_config("off"), "auto"),
        (experiment_config("shadow"), "auto"),
        (experiment_config("active"), "auto"),
    ]
    for cfg in (shipped(), shipped_legacy(), shipped_with_shifts()):
        out.extend((cfg, alias) for alias in cfg.aliases)
    return out


def _vector(rng: random.Random, n: int) -> dict[str, float]:
    raw = [rng.random() ** 2 for _ in range(n)]
    total = sum(raw) or 1.0
    return {str(i): v / total for i, v in enumerate(raw)}


def random_answers(rng: random.Random, cfg: RouterConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, q in cfg.questions.items():
        if rng.random() < 0.08:
            continue  # an absent answer
        conf = rng.choice([0.1, 0.3, 0.39, 0.4, 0.45, 0.6, 0.8, 0.95, rng.random()])
        kind = q.get("type")
        if kind == "choice":
            labels = list(q.get("criteria") or {}) or ["x"]
            label = rng.choice(labels)
            probs = None
            if rng.random() < 0.5:
                weights = _vector(rng, len(labels))
                probs = {lab: weights[str(i)] for i, lab in enumerate(labels)}
            out[qid] = choice(label, conf, probs)
        elif kind == "score":
            top = len(q.get("criteria") or [0, 1]) - 1
            value = rng.choice([0.0, 1.399, 1.4, 1.5, 2.33, 2.4, 3.0, rng.uniform(0, top)])
            probs = _vector(rng, top + 1) if rng.random() < 0.6 else None
            out[qid] = score(min(value, top), conf, probs)
        elif kind == "noul":
            out[qid] = noul(rng.choice([0.1, 0.45, 0.6, 0.799, 0.8, 0.9, rng.random()]))
    return out


def random_features(rng: random.Random, cfg: RouterConfig) -> Features:
    clients = ["", *cfg.clients]
    return Features(
        model="auto",
        has_tools=rng.random() < 0.5,
        has_images=rng.random() < 0.3,
        has_code=rng.random() < 0.5,
        stream=rng.random() < 0.5,
        message_count=rng.randint(1, 40),
        est_tokens=rng.choice([100, 900, 5000, 60000, 300000, 2_000_000]),
        max_tokens=rng.choice([0, 0, 512, 4096]),
        client=rng.choice(clients),
        tool_names=("bash",) if rng.random() < 0.5 else (),
    )


def random_pressures(rng: random.Random, cfg: RouterConfig) -> dict[str, float] | None:
    if rng.random() < 0.25:
        return None
    names = cfg.provider_names() or ["cloud"]
    return {
        name: rng.choice([0.0, 0.3, 0.61, 0.9, 1.0, rng.random()])
        for name in names
        if rng.random() < 0.7
    }


def test_trace_agrees_with_select_over_a_seeded_sweep():
    seen: set[str] = set()
    for seed in range(6):
        rng = random.Random(seed)
        for cfg, alias in CONFIGS():
            for _ in range(60):
                t = check(
                    cfg,
                    alias,
                    random_answers(rng, cfg),
                    random_features(rng, cfg),
                    random_pressures(rng, cfg),
                )
                seen.add(t["matched"])
                seen.update(x["outcome"] for x in t["rules"])
                if "error" in t:
                    seen.add("routing_error")
                for x in t["rules"]:
                    if x["pressure_ignored"]:
                        seen.add("pressure_ignored")
                    if x["shift"] and x["shift"]["decisive"]:
                        seen.add("decisive_shift")
                if t["experiments"]:
                    seen.add("experiments")
    # The sweep reaches every branch it is meant to cover.
    assert {
        "low_confidence", "default", "hit", "miss", "not_reached",
        "skipped_experiment", "routing_error", "pressure_ignored",
        "decisive_shift", "experiments",
    } <= seen
