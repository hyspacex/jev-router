"""What quota pressure does to a routing decision.

Everything here is `policy.evaluate`, which is pure: the pressures come in as
a mapping and nothing reads a clock or a socket.
"""

from __future__ import annotations

import pytest

from conftest import make_config
from jev_router.features import Features
from jev_router.policy import evaluate, order_entries, shift_amount

FEATURES = Features(model="auto", message_count=1, est_tokens=100)


def answers(difficulty: float = 2.0, harm: float = 0.1, task: str = "code"):
    return {
        "task": {"type": "choice", "choice": task, "confidence": 0.9},
        "difficulty": {"type": "score", "score": difficulty, "confidence": 0.9},
        "harm_if_wrong": {"type": "noul", "noul": harm},
    }


PROVIDERS = {
    "providers": {"cloud": {}, "local": {}},
    "models": {
        "small": {"provider": "local"},
        "big": {"provider": "cloud"},
        "paramy": {"provider": "cloud"},
    },
}


def use(name: str) -> dict:
    """`use: {route: name}`, spelled so it survives conftest's deep merge.

    The sample config's `default` names a model. Merging `{route: n}` over it
    would leave both keys, and a mapping with both means the model wins.
    """
    return {"route": name, "model": None, "effort": None}


def shifting_config(**extra):
    """A ladder whose one frontier cutoff moves with `cloud` pressure."""
    return make_config(
        **PROVIDERS,
        routes={
            "fast": {"primary": {"model": "small", "effort": "none"}},
            "frontier": {"primary": {"model": "big", "effort": "high"}},
        },
        policy={
            "low_confidence": {"min_confidence": 0.0, "questions": [], "use": None},
            "rules": [
                {
                    "name": "risky",
                    "protected": True,
                    "when": {"harm_if_wrong": {"gte": 0.8, "shift_with": "cloud",
                                               "max_shift": 0.5}},
                    "use": {"route": "frontier"},
                },
                {
                    "name": "hard",
                    "when": {"difficulty": {"gte": 1.5, "shift_with": "cloud",
                                            "max_shift": 1.0}},
                    "use": {"route": "frontier"},
                },
                {"name": "easy", "when": {}, "use": {"route": "fast"}},
            ],
            "default": use("frontier"),
        },
        **extra,
    )


def route(config, **kwargs):
    alias = config.aliases["auto"]
    result = evaluate(config, alias, kwargs.pop("answers", answers()), FEATURES,
                      kwargs.pop("pressures", None))
    return result


# --- the shift ----------------------------------------------------------


def test_with_no_pressure_the_cutoff_is_the_number_in_the_config():
    cfg = shifting_config()
    assert route(cfg, answers=answers(difficulty=1.6)).model == "big"
    assert route(cfg, answers=answers(difficulty=1.4)).model == "small"


def test_pressure_raises_the_cutoff_and_the_request_falls_through():
    cfg = shifting_config()
    calm = route(cfg, answers=answers(difficulty=1.6))
    pressed = route(cfg, answers=answers(difficulty=1.6), pressures={"cloud": 1.0})
    assert calm.model == "big"
    assert pressed.model == "small"
    assert pressed.rule == "easy"
    assert pressed.pressure_changed_the_outcome is True


def test_the_shift_is_bounded_by_max_shift():
    cfg = shifting_config()
    # max_shift is 1.0, so at full pressure the cutoff is 2.5 and no higher.
    assert route(cfg, answers=answers(difficulty=2.6), pressures={"cloud": 1.0}).model == "big"
    assert route(cfg, answers=answers(difficulty=2.4), pressures={"cloud": 1.0}).model == "small"


def test_the_shift_is_proportional_to_pressure():
    cfg = shifting_config()
    at = lambda p: route(cfg, answers=answers(difficulty=2.0), pressures={"cloud": p}).model
    assert at(0.0) == "big"
    assert at(0.4) == "big"    # cutoff 1.9
    assert at(0.6) == "small"  # cutoff 2.1


def test_a_shift_is_monotone_so_more_pressure_never_makes_the_frontier_easier():
    cfg = shifting_config()
    reached = []
    for step in range(11):
        result = route(cfg, answers=answers(difficulty=2.2), pressures={"cloud": step / 10})
        reached.append(result.model == "big")
    # Once it stops reaching the frontier it never starts again.
    assert reached == sorted(reached, reverse=True)


def test_a_protected_rule_ignores_the_shift():
    cfg = shifting_config()
    risky = answers(difficulty=0.1, harm=0.9)
    assert route(cfg, answers=risky).rule == "risky"
    pressed = route(cfg, answers=risky, pressures={"cloud": 1.0})
    assert pressed.rule == "risky"
    assert pressed.model == "big"
    assert pressed.protected is True


def test_pressure_on_another_provider_moves_nothing():
    cfg = shifting_config()
    assert route(cfg, answers=answers(difficulty=1.6), pressures={"local": 1.0}).model == "big"


def test_the_shift_is_recorded_with_its_numbers():
    cfg = shifting_config()
    result = route(cfg, answers=answers(difficulty=1.6), pressures={"cloud": 0.5})
    assert [s.to_dict() for s in result.shifts] == [
        {
            "rule": "hard",
            "question": "difficulty",
            "op": "gte",
            "base": 1.5,
            "effective": 2.0,
            "provider": "cloud",
            "pressure": 0.5,
        }
    ]


def test_turning_quota_routing_off_ignores_the_pressures_entirely():
    cfg = shifting_config(quota_policy={"enabled": False})
    result = route(cfg, answers=answers(difficulty=1.6), pressures={"cloud": 1.0})
    assert result.model == "big"
    assert result.shifts == []
    assert result.pressure_changed_the_outcome is False


def test_shift_amount_is_zero_for_a_condition_that_says_nothing_about_pressure():
    assert shift_amount({"gte": 2.0}, {"cloud": 1.0}) == (0.0, "", 0.0)
    assert shift_amount("not a mapping", {"cloud": 1.0}) == (0.0, "", 0.0)
    assert shift_amount({"gte": 2.0, "shift_with": "cloud", "max_shift": 0.4},
                        {"cloud": 0.5})[0] == pytest.approx(0.2)
    # An unknown provider is pressure zero, not a crash.
    assert shift_amount({"gte": 2.0, "shift_with": "ghost", "max_shift": 0.4}, {})[0] == 0.0


# --- reordering a route -------------------------------------------------


def laned_config(fallbacks):
    return make_config(
        **PROVIDERS,
        routes={"lane": {"primary": {"model": "big", "effort": "high"},
                         "fallbacks": fallbacks}},
        policy={"low_confidence": None, "rules": [],
                "default": use("lane")},
    )


def test_an_equivalent_fallback_may_be_promoted_ahead_of_a_pressured_primary():
    cfg = laned_config([{"model": "small", "effort": "low", "equivalent": True}])
    calm = route(cfg, pressures=None)
    assert [e.model for e in calm.plan] == ["big", "small"]

    pressed = route(cfg, pressures={"cloud": 0.9})
    assert [e.model for e in pressed.plan] == ["small", "big"]
    assert pressed.model == "small"
    assert pressed.reordered is True


def test_a_weaker_fallback_is_never_promoted_over_the_primary():
    cfg = laned_config([{"model": "small", "effort": "low"}])  # equivalent is false
    pressed = route(cfg, pressures={"cloud": 0.9})
    assert [e.model for e in pressed.plan] == ["big", "small"]
    assert pressed.model == "big"
    assert pressed.reordered is False


def test_a_pressured_fallback_drops_behind_a_healthy_one():
    """The primary is fine. A pressured fallback moves behind a healthy one
    without disturbing the primary."""
    cfg = make_config(
        **PROVIDERS,
        routes={
            "lane": {
                "primary": {"model": "small", "effort": "none"},
                "fallbacks": [
                    {"model": "big", "effort": "high"},
                    {"model": "small", "effort": "low"},
                ],
            }
        },
        policy={"low_confidence": None, "rules": [], "default": use("lane")},
    )
    assert [e.as_pair() for e in route(cfg).plan] == [
        ("small", "none"), ("big", "high"), ("small", "low")
    ]
    pressed = route(cfg, pressures={"cloud": 0.9})
    assert [e.as_pair() for e in pressed.plan] == [
        ("small", "none"), ("small", "low"), ("big", "high")
    ]
    assert pressed.reordered is True
    assert pressed.model == "small"   # the primary did not move


def test_pressure_at_or_below_the_demote_threshold_changes_nothing():
    cfg = laned_config([{"model": "small", "effort": "low", "equivalent": True}])
    assert [e.model for e in route(cfg, pressures={"cloud": 0.6}).plan] == ["big", "small"]
    assert [e.model for e in route(cfg, pressures={"cloud": 0.61}).plan] == ["small", "big"]


def test_order_entries_keeps_a_single_entry_route_alone():
    cfg = laned_config([])
    lane = cfg.routes["lane"]
    entries, reordered = order_entries(cfg, lane.entries(), {"cloud": 1.0})
    assert entries == lane.entries()
    assert reordered is False


def cost_config(cost: float, equivalent: bool = True):
    """`big` and a cheaper `paramy` on the same busy subscription."""
    return make_config(
        **{**PROVIDERS, "models": {**PROVIDERS["models"],
                                   "paramy": {"provider": "cloud", "quota_cost": cost}}},
        routes={"lane": {"primary": {"model": "big"},
                         "fallbacks": [{"model": "paramy", "equivalent": equivalent}]}},
        policy={"low_confidence": None, "rules": [], "default": use("lane")},
        aliases={"auto": {"allowed_models": ["small", "big", "paramy"]}},
    )


def test_a_cheaper_equivalent_on_the_same_provider_takes_over_under_pressure():
    cfg = cost_config(0.2)
    assert route(cfg, pressures={"cloud": 0.9}).model == "paramy"
    # At 0.9 the primary is over 0.6 and the cheap one is at 0.18.
    assert route(cfg, pressures={"cloud": 0.6}).model == "big"
    assert route(cfg).model == "big"


def test_quota_cost_never_promotes_a_weaker_fallback_or_one_that_costs_the_same():
    assert route(cost_config(0.2, equivalent=False), pressures={"cloud": 1.0}).model == "big"
    # Both are equally pressured, so neither is healthier than the other.
    assert route(cost_config(1.0), pressures={"cloud": 1.0}).model == "big"


def test_quota_cost_is_a_share_of_the_allowance():
    with pytest.raises(ValueError):
        make_config(models={"big": {"quota_cost": 1.5}})
    with pytest.raises(ValueError):
        make_config(models={"big": {"quota_cost": 0}})


def test_the_shipped_shape_at_full_pressure_keeps_the_top_band_and_the_protected_rules():
    """Requirement (d): at pressure 1.0 on a healthy provider the frontier
    route still serves protected rules and the hardest work, and everything
    else moves on."""
    cfg = make_config(
        **PROVIDERS,
        routes={
            "fast": {"primary": {"model": "small", "effort": "none"}},
            "frontier_high": {"primary": {"model": "big", "effort": "high"}},
            "frontier_xhigh": {"primary": {"model": "big", "effort": "xhigh"}},
        },
        policy={
            "low_confidence": None,
            "rules": [
                {"name": "images", "protected": True, "when": {"has_images": True},
                 "use": {"route": "frontier_high"}},
                {"name": "top_band", "when": {"difficulty": {"gte": 3.0}},
                 "use": {"route": "frontier_xhigh"}},
                {"name": "hard",
                 "when": {"difficulty": {"gte": 1.5, "shift_with": "cloud", "max_shift": 1.0}},
                 "use": {"route": "frontier_high"}},
                {"name": "easy", "when": {}, "use": {"route": "fast"}},
            ],
            "default": use("frontier_high"),
        },
    )
    full = {"cloud": 1.0}
    assert route(cfg, answers=answers(difficulty=3.0), pressures=full).model == "big"
    assert route(cfg, answers=answers(difficulty=2.0), pressures=full).model == "small"
    with_image = Features(model="auto", message_count=1, est_tokens=100, has_images=True)
    protected = evaluate(cfg, cfg.aliases["auto"], answers(difficulty=0.1), with_image, full)
    assert protected.model == "big"
    assert protected.rule == "images"
