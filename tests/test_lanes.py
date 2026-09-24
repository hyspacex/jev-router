"""Quality lanes, and the guard that keeps quota inside them.

Named cases from docs/R2_SPEC.md 13.2 covered here:

    C29  pressure never promotes an unqualified execution model or effort
         merely because it has capacity
"""

from __future__ import annotations

import pytest
from conftest import make_config
from pydantic import ValidationError

from jev_router.config import load_config
from jev_router.features import Features
from jev_router.policy import RoutingError, evaluate, excluded_models, select

# `cheap` is qualified for the lane, `weak` is not. `weak` is an `equivalent`
# fallback of the route, which is the only kind of entry quota may promote, so
# without the lane guard pressure would take it.
LANE_CONFIG = {
    "models": {
        # The sample config's three models stay, and gain a provider now that
        # this fixture declares some.
        "small": {"provider": "budget"},
        "big": {"provider": "premium"},
        "paramy": {"provider": "budget"},
        "cheap": {
            "upstream_id": "vendor/cheap",
            "provider": "budget",
            "context_window": 100000,
            "efforts": ["none", "low"],
            "effort_style": "suffix",
        },
        "weak": {
            "upstream_id": "vendor/weak",
            "provider": "budget",
            "context_window": 100000,
            "efforts": ["none"],
            "effort_style": "suffix",
        },
        "strong": {
            "upstream_id": "vendor/strong",
            "provider": "premium",
            "context_window": 400000,
            "supports_vision": True,
            "efforts": ["low", "medium", "high"],
            "effort_style": "suffix",
        },
    },
    "providers": {"budget": {}, "premium": {}},
    "routes": {
        "careful": {
            "primary": {"model": "strong", "effort": "medium"},
            "fallbacks": [{"model": "weak", "effort": "none", "equivalent": True}],
        }
    },
    "quality_lanes": {
        "careful-work": {
            "description": "Work somebody measured the strong model on.",
            "qualification_ref": "POOL.md: the test fixture",
            "qualified": [
                {
                    "model": "strong",
                    "effort": "medium",
                    "qualification_ref": "POOL.md: 9.3 on twelve cases",
                },
                {
                    "model": "cheap",
                    "effort": "low",
                    "qualification_ref": "POOL.md: 9.1 on the same twelve",
                },
            ],
            "conservative_default": {"model": "strong", "effort": "medium"},
        }
    },
    "rulesets": {
        "laned": {
            "rules": [
                {
                    "name": "careful",
                    "lane": "careful-work",
                    "when": {},
                    "use": {"route": "careful"},
                }
            ],
            "default": {"model": "strong", "effort": "medium"},
        }
    },
    "aliases": {
        "auto": {
            "rules": "laned",
            "allowed_models": ["cheap", "weak", "strong"],
            "questions": [],
        }
    },
    "settings": {"default_route": {"model": "strong", "effort": "medium"}},
}


@pytest.fixture
def laned():
    return make_config(**LANE_CONFIG)


FEATURES = Features(model="auto", message_count=1, est_tokens=100)


# --- C29 -----------------------------------------------------------------


def test_c29_pressure_does_not_promote_a_model_outside_the_lane(laned):
    alias = laned.aliases["auto"]
    # Without the lane, quota promotes the equivalent fallback.
    loose = evaluate(laned, alias, {}, FEATURES, {"premium": 0.9})
    assert loose.model == "weak"

    # With it, the adequate provider is kept and the deferral is on the record.
    result = select(laned, alias, {}, FEATURES, {"premium": 0.9})
    assert (result.model, result.effort) == ("strong", "medium")
    assert result.lane == "careful-work"
    assert result.pressure_changed_the_outcome is False
    assert any("deferred/kept under pressure" in n for n in result.notes)
    assert {"model": "weak", "effort": "none",
            "reason": "not qualified for lane 'careful-work' under pressure"} in (
        result.exclusions
    )


def test_c29_a_qualified_alternative_is_still_allowed_under_pressure(laned):
    raw = {
        **LANE_CONFIG,
        "routes": {
            "careful": {
                "primary": {"model": "strong", "effort": "medium"},
                "fallbacks": [
                    {"model": "cheap", "effort": "low", "equivalent": True}
                ],
            }
        },
    }
    config = make_config(**raw)
    result = select(config, config.aliases["auto"], {}, FEATURES, {"premium": 0.9})
    assert (result.model, result.effort) == ("cheap", "low")
    assert result.pressure_changed_the_outcome is True
    # Both the pressured and the calm choice are qualified, so the claim that
    # adequacy held under both policies can actually be made.
    assert result.evidence == "qualified"
    assert result.counterfactual == ("strong", "medium")


def test_c29_the_shipped_frontier_lane_does_not_qualify_the_overflow_model():
    cfg = load_config("router.yaml")
    lane = cfg.quality_lanes["hard-work"]
    assert "grok-4.6" not in lane.models()
    assert lane.allows("gpt-6-astra", "high")
    assert not lane.allows("grok-4.6", "low")
    # It is still reachable as a failure fallback, which is a different claim.
    assert any(
        e.model == "grok-4.6" for e in cfg.routes["frontier_high"].entries()
    )


def test_c29_a_protected_rule_is_untouched_by_all_of_this(laned):
    result = select(laned, laned.aliases["auto"], {}, FEATURES, {"budget": 1.0})
    assert (result.model, result.effort) == ("strong", "medium")


def test_c29_pressure_on_a_lane_with_no_alternative_keeps_the_primary(laned):
    result = select(laned, laned.aliases["auto"], {}, FEATURES, {"premium": 1.0})
    assert result.model == "strong"


# --- capability replacement ----------------------------------------------


def test_a_capability_replacement_stays_inside_the_lane(laned):
    """An image forces a new model. It must not be one nobody measured."""
    raw = {
        **LANE_CONFIG,
        "routes": {
            "careful": {
                "primary": {"model": "cheap", "effort": "low"},
                "fallbacks": [],
            }
        },
    }
    config = make_config(**raw)
    with_image = Features(
        model="auto", message_count=1, est_tokens=100, has_images=True
    )
    # `weak` cannot read images either, so the only eligible model is `strong`,
    # which the lane does qualify.
    result = select(config, config.aliases["auto"], {}, with_image)
    assert (result.model, result.effort) == ("strong", "medium")


def test_an_unqualified_replacement_is_refused_rather_than_taken():
    raw = {
        **LANE_CONFIG,
        "quality_lanes": {
            "careful-work": {
                "qualification_ref": "POOL.md: the test fixture",
                "qualified": [
                    {
                        "model": "cheap",
                        "effort": "low",
                        "qualification_ref": "POOL.md: 9.1",
                    }
                ],
            }
        },
        "routes": {
            "careful": {"primary": {"model": "cheap", "effort": "low"}, "fallbacks": []}
        },
    }
    config = make_config(**raw)
    with_image = Features(
        model="auto", message_count=1, est_tokens=100, has_images=True
    )
    with pytest.raises(RoutingError) as exc:
        select(config, config.aliases["auto"], {}, with_image)
    assert "no model qualified for lane 'careful-work'" in str(exc.value)


# --- with no lanes configured --------------------------------------------


def test_without_lanes_select_is_evaluate_plus_a_counterfactual(config):
    alias = config.aliases["auto"]
    answers = {
        "task": {"type": "choice", "choice": "code", "confidence": 0.9},
        "difficulty": {"type": "score", "score": 1.9, "confidence": 0.9},
        "harm_if_wrong": {"type": "noul", "noul": 0.1},
    }
    plain = evaluate(config, alias, answers, FEATURES)
    chosen = select(config, alias, answers, FEATURES)
    assert (chosen.model, chosen.effort, chosen.rule) == (
        plain.model,
        plain.effort,
        plain.rule,
    )
    assert chosen.lane == ""
    assert chosen.evidence == "weak"
    assert chosen.counterfactual == (plain.model, plain.effort)


def test_the_shipped_ladder_routes_inside_its_lanes():
    cfg = load_config("router.yaml")
    alias = cfg.aliases["auto"]
    answers = {
        "task": {"type": "choice", "choice": "code-edit", "confidence": 0.9},
        "difficulty": {"type": "score", "score": 2.9, "confidence": 0.9},
        "harm_if_wrong": {"type": "noul", "noul": 0.1},
    }
    result = select(cfg, alias, answers, FEATURES)
    assert result.lane == "hard-work"
    assert result.evidence == "qualified"
    assert result.qualification_ref.startswith("POOL.md")


# --- exclusions ----------------------------------------------------------


def test_exclusions_say_which_hard_constraint_ruled_a_model_out(config):
    alias = config.aliases["auto"]
    reasons = {
        row["model"]: row["reason"]
        for row in excluded_models(
            config,
            alias,
            Features(model="auto", message_count=1, est_tokens=200, has_images=True),
        )
    }
    assert reasons["small"] == "the request carries an image"
    assert "big" not in reasons


def test_exclusions_report_a_context_window_that_is_too_small(config):
    rows = excluded_models(
        config,
        config.aliases["auto"],
        Features(model="auto", message_count=1, est_tokens=300000, max_tokens=1000),
    )
    assert any("token window" in row["reason"] for row in rows)


def test_exclusions_report_a_client_that_forbids_a_model(config):
    rows = excluded_models(
        config,
        config.aliases["auto"],
        Features(model="auto", message_count=1, est_tokens=100, client="bigonly"),
    )
    assert {"model": "small", "reason": "the client does not permit it"} in rows


def test_exclusions_never_mention_quota(laned):
    result = select(laned, laned.aliases["auto"], {}, FEATURES)
    for row in excluded_models(laned, laned.aliases["auto"], FEATURES):
        assert "pressure" not in row["reason"] and "quota" not in row["reason"]
    assert result.exclusions == []


# --- configuration -------------------------------------------------------


def test_a_lane_must_name_where_its_evidence_is():
    with pytest.raises(ValidationError):
        make_config(
            quality_lanes={"x": {"qualified": [{"model": "small", "effort": "none"}]}}
        )


def test_a_rule_may_not_route_outside_the_lane_it_names():
    raw = dict(LANE_CONFIG)
    raw["rulesets"] = {
        "laned": {
            "rules": [
                {
                    "name": "careful",
                    "lane": "careful-work",
                    "when": {},
                    "use": {"model": "weak", "effort": "none"},
                }
            ]
        }
    }
    with pytest.raises(ValidationError) as exc:
        make_config(**raw)
    assert "not in the qualified set of lane 'careful-work'" in str(exc.value)


def test_a_rule_may_not_name_a_lane_that_does_not_exist():
    with pytest.raises(ValidationError) as exc:
        make_config(
            rulesets={
                "laned": {
                    "rules": [
                        {"name": "x", "lane": "nope", "when": {}, "use": {"model": "big"}}
                    ]
                }
            }
        )
    assert "unknown quality lane 'nope'" in str(exc.value)


def test_a_lane_needs_at_least_one_qualified_pair():
    with pytest.raises(ValidationError) as exc:
        make_config(quality_lanes={"x": {"qualification_ref": "somewhere"}})
    assert "a lane needs at least one pair" in str(exc.value)


def test_a_lane_refuses_a_model_or_effort_that_does_not_exist():
    with pytest.raises(ValidationError) as exc:
        make_config(
            quality_lanes={
                "x": {
                    "qualified": [
                        {"model": "small", "effort": "medium", "qualification_ref": "r"}
                    ]
                }
            }
        )
    assert "does not allow effort 'medium'" in str(exc.value)


def test_a_lane_entry_with_no_effort_means_any_effort_of_that_model():
    config = make_config(
        quality_lanes={
            "x": {"qualified": [{"model": "big", "qualification_ref": "r"}]}
        }
    )
    lane = config.quality_lanes["x"]
    assert lane.allows("big", "low") and lane.allows("big", "xhigh")
    assert not lane.allows("small", "none")
    assert lane.reference_for("big", "low") == "r"


@pytest.mark.parametrize("pressure", [0.0, 0.5, 1.0])
def test_strict_quality_requirement_does_not_move_with_quota(pressure):
    cfg = load_config("router.yaml")
    alias = cfg.aliases["auto"]
    answers = {
        "task": {"type": "choice", "choice": "code-edit", "confidence": 0.99},
        "difficulty": {"type": "score", "score": 2.5, "confidence": 0.99},
        "harm_if_wrong": {"type": "noul", "noul": 0.1},
    }
    result = select(cfg, alias, answers, FEATURES, {"openai": pressure})
    # The requirement never moves: the same rule and the same lane at any
    # pressure, and no threshold shifts.
    assert result.rule == "hard"
    assert result.lane == "hard-work"
    assert result.counterfactual == ("gpt-6-astra", "high")
    assert result.shifts == []
    # Only the pair inside the lane may. Over the demote threshold astra gives
    # way to sol xhigh, which the lane qualifies and which costs a fifth as
    # much of the same subscription.
    busy = pressure > cfg.quota_policy.demote_above
    expected = ("gpt-6-sol", "xhigh") if busy else ("gpt-6-astra", "high")
    assert (result.model, result.effort) == expected
    assert result.evidence == "qualified"
    assert result.pressure_changed_the_outcome is busy
