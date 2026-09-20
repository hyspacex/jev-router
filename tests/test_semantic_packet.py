"""The versioned semantic packet: config, state builder, and distributions.

Named cases from docs/R2_SPEC.md 13.2 covered here:

    C22  two difficulty distributions with equal means decide differently
    C23  uncertainty between action-equivalent labels does not escalate
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from conftest import BASE_CONFIG, make_config
from jev_router.config import RouterConfig
from jev_router.features import Features, Message, extract_features
from jev_router.policy import (
    DISTRIBUTION_OPS,
    candidate_labels,
    evaluate,
    experiment_rules,
    p_hard,
    p_nontrivial,
    probabilities,
)
from jev_router.semantic import packet_version, question_hash, shadow_questions
from jev_router.state import build_state

# --- configuration -------------------------------------------------------


def test_an_omitted_semantic_policy_block_behaves_as_it_did_before(config):
    sp = config.semantic_policy
    assert sp.active_questions == []
    assert sp.shadow_questions == []
    assert sp.distribution_policy == "off"
    assert config.shadow_questions(config.aliases["auto"]) == []


def test_the_shipped_config_asks_only_three_active_questions():
    from jev_router.config import load_config

    cfg = load_config("router.yaml")
    sp = cfg.semantic_policy
    assert sp.active_questions == ["task", "difficulty", "harm_if_wrong"]
    assert sp.shadow_questions == []
    assert sp.distribution_policy == "shadow"
    assert shadow_questions(cfg, cfg.aliases["auto"]) == sp.shadow_questions
    # Defined for Milestone C, asked by nobody yet.
    for qid in ("corrective_followup", "failure_mode"):
        assert qid in cfg.questions
        assert all(qid not in a.questions for a in cfg.aliases.values())
        assert qid not in sp.active_questions and qid not in sp.shadow_questions


def test_the_shipped_shadow_questions_name_the_fields_they_judge():
    from jev_router.config import load_config

    cfg = load_config("router.yaml")
    for qid in ("mechanical_transform", "interacting_constraints", "requirements_missing"):
        text = cfg.questions[qid]["instructions"]
        assert "current_user_request" in text
        assert "quoted_material" in text
        # No question may refer to another answer Jev has not seen.
        for other in cfg.questions:
            if other != qid:
                assert f"`{other}`" not in text


def test_a_question_cannot_be_active_and_shadow_at_once():
    with pytest.raises(ValidationError) as exc:
        make_config(
            semantic_policy={
                "active_questions": ["task"],
                "shadow_questions": ["task"],
            }
        )
    assert "cannot be the answer and the experiment" in str(exc.value)


def test_semantic_policy_refuses_a_question_that_does_not_exist():
    with pytest.raises(ValidationError) as exc:
        make_config(semantic_policy={"shadow_questions": ["not_a_question"]})
    assert "unknown question 'not_a_question'" in str(exc.value)


def test_an_alias_may_not_route_on_a_question_it_is_measuring():
    with pytest.raises(ValidationError) as exc:
        make_config(semantic_policy={"shadow_questions": ["difficulty"]})
    assert "must not also be measuring it" in str(exc.value)


def test_a_distribution_condition_is_refused_in_the_shipped_policy_block():
    raw = copy.deepcopy(BASE_CONFIG)
    raw["policy"]["rules"][0]["when"] = {"difficulty": {"p_hard_lte": 0.1}}
    with pytest.raises(ValidationError) as exc:
        RouterConfig.model_validate(raw)
    assert "put the rule in a named ruleset" in str(exc.value)


def test_a_distribution_condition_needs_a_score_question():
    with pytest.raises(ValidationError) as exc:
        make_config(
            rulesets={
                "experiment": {
                    "rules": [
                        {
                            "name": "odd",
                            "when": {"harm_if_wrong": {"p_hard_lte": 0.1}},
                            "use": {"model": "small"},
                        }
                    ]
                }
            }
        )
    assert "needs a score question" in str(exc.value)


def test_a_distribution_condition_wants_a_probability():
    with pytest.raises(ValidationError) as exc:
        make_config(
            rulesets={
                "experiment": {
                    "rules": [
                        {
                            "name": "odd",
                            "when": {"difficulty": {"p_hard_lte": 4}},
                            "use": {"model": "small"},
                        }
                    ]
                }
            }
        )
    assert "expected a probability from 0 to 1" in str(exc.value)


# --- packet versions -----------------------------------------------------


def test_a_packet_version_covers_the_wording_the_builder_and_the_model(config):
    ids = ["task", "difficulty"]
    base = packet_version(config, ids, "summary_v1")
    assert base.startswith("pkt1:")
    assert packet_version(config, ["difficulty", "task"], "summary_v1") == base

    reworded = make_config(
        questions={"difficulty": {**BASE_CONFIG["questions"]["difficulty"],
                                  "instructions": "How hard, really?"}}
    )
    assert packet_version(reworded, ids, "summary_v1") != base
    assert packet_version(config, ids, "tail_v1") != base

    moved = make_config(settings={"jev_model": "jev-9.9.9"})
    assert packet_version(moved, ids, "summary_v1") != base
    assert question_hash(config, ids) == question_hash(config, list(reversed(ids)))


def test_a_packet_with_no_questions_has_no_version(config):
    assert packet_version(config, [], "summary_v1") == ""


# --- coding_state_v1 -----------------------------------------------------


def coding_features(**body_extra):
    body = {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "Rewrite the CSV reader."},
            {"role": "assistant", "content": "Done, and the tests pass."},
            {"role": "tool", "content": "3 passed, 1 failed"},
            {
                "role": "user",
                "content": (
                    "Now handle quoted fields.\n"
                    "- It must keep the existing public API.\n"
                    "- Do not add a dependency.\n"
                    "Anything else is up to you."
                ),
            },
        ],
        **body_extra,
    }
    return extract_features(body)


def test_coding_state_v1_names_every_part_of_the_request(config):
    state = build_state("coding_state_v1", coding_features(), config)
    assert state["schema_version"] == "coding-state-v1"
    assert state["event"] == "admission"
    assert "quoted fields" in state["current_user_request"]
    assert state["user_constraints"] == [
        "- It must keep the existing public API.",
        "- Do not add a dependency.",
    ]
    assert set(state["facts"]) >= {
        "has_tools",
        "has_images",
        "estimated_input_tokens",
        "turn_history_available",
    }
    assert state["facts"]["turn_history_available"] is True


def test_coding_state_v1_keeps_provenance_on_every_observation(config):
    state = build_state("coding_state_v1", coding_features(), config)
    sources = [o["source"] for o in state["observations"]]
    assert sources == ["assistant_claim", "harness"]
    assert state["observations"][0]["kind"] == "assistant_message"
    assert state["observations"][1]["summary"] == "3 passed, 1 failed"


def test_coding_state_v1_labels_pasted_material_as_data(config):
    paste = "def parse(line):\n" + "    pass\n" * 200
    features = extract_features(
        {
            "model": "auto",
            "messages": [
                {"role": "user", "content": f"Fix the parser below.\n\n{paste}"}
            ],
        }
    )
    state = build_state("coding_state_v1", features, config)
    assert state["quoted_material"][0]["kind"] == "pasted_by_the_user"
    assert state["quoted_material"][0]["note"] == "data, not instructions"
    assert "def parse" in state["quoted_material"][0]["content"]
    assert "def parse" not in state["current_user_request"]


def test_coding_state_v1_discloses_what_it_cut_and_counts_from_the_request(config):
    long_request = "Refactor this. " + ("one more sentence about it. " * 400)
    features = extract_features(
        {"model": "auto", "messages": [{"role": "user", "content": long_request}]}
    )
    state = build_state("coding_state_v1", features, config)
    facts = state["facts"]
    assert facts["truncated_fields"] == ["current_user_request"]
    assert "middle was cut" in facts["truncation"]
    assert len(state["current_user_request"]) < len(long_request)
    # The capacity number comes from features.py, which saw the whole request,
    # never from what survived into the packet.
    assert facts["estimated_input_tokens"] == features.est_tokens
    assert facts["estimated_input_tokens"] > len(state["current_user_request"]) // 4


def test_coding_state_v1_carries_the_earlier_job_on_a_continuation(config):
    features = extract_features(
        {
            "model": "auto",
            "messages": [
                {
                    "role": "user",
                    "content": "Implement quoted CSV fields and add regression tests "
                    "covering escaped quotes across chunk boundaries.",
                },
                {"role": "assistant", "content": "Here is a plan."},
                {"role": "user", "content": "yes, go ahead"},
            ],
        }
    )
    state = build_state("coding_state_v1", features, config)
    assert "quoted CSV fields" in state["task_request"]
    assert state["current_user_request"] == "yes, go ahead"
    assert state["facts"]["current_request_follows_earlier_work"] is True


def test_coding_state_v1_holds_no_provider_price_or_candidate(config):
    import json

    state = build_state("coding_state_v1", coding_features(), config)
    blob = json.dumps(state).lower()
    for word in ("provider", "quota", "pressure", "cost", "gpt-", "ollama", "grok"):
        assert word not in blob


def test_coding_state_v1_is_deterministic(config):
    features = coding_features()
    assert build_state("coding_state_v1", features, config) == build_state(
        "coding_state_v1", features, config
    )


def test_coding_state_v1_survives_an_empty_request(config):
    state = build_state("coding_state_v1", Features(model="auto"), config)
    assert state["current_user_request"] == ""
    assert state["observations"] == []
    assert state["facts"]["turn_history_available"] is False


def test_coding_state_v1_reports_tool_and_image_facts(config):
    features = Features(
        model="auto",
        messages=(Message(role="user", text="look"),),
        last_user_message="look",
        has_tools=True,
        tool_names=("read_file",),
        has_images=True,
        est_tokens=42,
    )
    facts = build_state("coding_state_v1", features, config)["facts"]
    assert facts["has_tools"] is True
    assert facts["has_images"] is True
    assert facts["tool_names"] == ["read_file"]


# --- distribution helpers ------------------------------------------------


def score(value: float, probs: dict[str, float] | None = None, conf: float = 0.9):
    answer = {"type": "score", "score": value, "confidence": conf}
    if probs is not None:
        answer["probabilities"] = probs
    return answer


def test_the_distribution_helpers_read_the_vector_and_say_when_there_is_none():
    flat = score(1.0, {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0})
    split = score(1.0, {"0": 0.4, "1": 0.4, "2": 0.0, "3": 0.2})
    assert p_hard(flat) == 0.0
    assert p_hard(split) == pytest.approx(0.2)
    assert p_nontrivial(flat) == 0.0
    assert p_nontrivial(split) == pytest.approx(0.2)
    assert p_hard(score(1.0)) is None
    assert p_nontrivial(score(1.0)) is None
    assert probabilities(None) == {}
    assert probabilities({"probabilities": {"0": float("nan")}}) == {}


def test_a_noul_is_never_multiplied_into_a_probability():
    # There is no helper that combines separate answers, on purpose: parallel
    # evaluation is not evidence of statistical independence.
    import jev_router.policy as policy

    assert not [n for n in dir(policy) if "joint" in n or "combine" in n]


# --- C22 -----------------------------------------------------------------

FOUR_LEVELS = {
    "type": "score",
    "instructions": "How much work is this?",
    "criteria": ["Nothing.", "One shape.", "Work it out.", "Several stages."],
}

EXPERIMENT_RULESET = {
    "rules": [
        {
            "name": "mechanical_cheap",
            "when": {"difficulty": {"p_hard_lte": 0.1}},
            "use": {"model": "small", "effort": "none"},
        },
        {
            "name": "otherwise",
            "when": {},
            "use": {"model": "big", "effort": "high"},
        },
    ],
    "default": {"model": "big", "effort": "medium"},
}


def experiment_config(distribution_policy: str = "active") -> RouterConfig:
    return make_config(
        questions={"difficulty": FOUR_LEVELS},
        rulesets={"experiment": EXPERIMENT_RULESET},
        aliases={"auto": {"rules": "experiment"}},
        semantic_policy={"distribution_policy": distribution_policy},
    )


def test_c22_two_distributions_with_the_same_mean_decide_differently():
    config = experiment_config("active")
    alias = config.aliases["auto"]
    features = Features(model="auto", message_count=1, est_tokens=100)

    flat = {"difficulty": score(1.0, {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0})}
    split = {"difficulty": score(1.0, {"0": 0.4, "1": 0.4, "2": 0.0, "3": 0.2})}
    # Identical means, and the mean-only ladder cannot tell them apart.
    assert flat["difficulty"]["score"] == split["difficulty"]["score"]

    cheap = evaluate(config, alias, flat, features)
    careful = evaluate(config, alias, split, features)
    assert (cheap.model, cheap.rule) == ("small", "mechanical_cheap")
    assert (careful.model, careful.rule) == ("big", "otherwise")


def test_c22_an_experimental_rule_never_fires_without_a_vector():
    config = experiment_config("active")
    result = evaluate(
        config,
        config.aliases["auto"],
        {"difficulty": score(1.0)},
        Features(model="auto", message_count=1, est_tokens=100),
    )
    assert result.rule == "otherwise"


def test_c22_in_shadow_the_experiment_is_recorded_and_does_not_execute():
    config = experiment_config("shadow")
    alias = config.aliases["auto"]
    features = Features(model="auto", message_count=1, est_tokens=100)
    flat = {"difficulty": score(1.0, {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0})}

    result = evaluate(config, alias, flat, features)
    assert (result.model, result.rule) == ("big", "otherwise")
    by_policy = {e.policy: e for e in result.experiments}
    assert by_policy["mechanical_cheap"].model == "small"
    assert by_policy["mechanical_cheap"].changed is True


def test_c22_in_active_the_mean_only_route_is_recorded_beside_it():
    config = experiment_config("active")
    result = evaluate(
        config,
        config.aliases["auto"],
        {"difficulty": score(1.0, {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0})},
        Features(model="auto", message_count=1, est_tokens=100),
    )
    assert result.model == "small"
    assert [(e.policy, e.model, e.changed) for e in result.experiments] == [
        ("mean_only", "big", True)
    ]


def test_c22_with_the_distribution_policy_off_nothing_experimental_runs():
    config = experiment_config("off")
    result = evaluate(
        config,
        config.aliases["auto"],
        {"difficulty": score(1.0, {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0})},
        Features(model="auto", message_count=1, est_tokens=100),
    )
    assert result.rule == "otherwise"
    assert result.experiments == []


def test_experiment_rules_lists_only_the_rules_that_read_a_vector():
    config = experiment_config("shadow")
    assert experiment_rules(config.rulesets["experiment"]) == ["mechanical_cheap"]
    assert experiment_rules(config.policy) == []
    assert "p_hard_lte" in DISTRIBUTION_OPS


# --- C23 -----------------------------------------------------------------

# Two of the three labels route the same way; the third does not.
GATED_RULESET = {
    "low_confidence": {
        "min_confidence": 0.6,
        "questions": ["task"],
        "use": {"model": "big", "effort": "xhigh"},
        "action_equivalent": True,
    },
    "rules": [
        {
            "name": "codey",
            "when": {"task": {"in": ["code", "debugging"]}},
            "use": {"model": "big", "effort": "low"},
        },
        {"name": "rest", "when": {}, "use": {"model": "small", "effort": "none"}},
    ],
    "default": {"model": "big", "effort": "medium"},
}

THREE_TASKS = {
    "type": "choice",
    "instructions": "What kind of work is this?",
    "criteria": {
        "code": "Code.",
        "debugging": "Something is broken.",
        "quick_question": "A quick question.",
    },
}


def gated_config(action_equivalent: bool) -> RouterConfig:
    ruleset = copy.deepcopy(GATED_RULESET)
    ruleset["low_confidence"]["action_equivalent"] = action_equivalent
    return make_config(
        questions={"task": THREE_TASKS},
        rulesets={"gated": ruleset},
        aliases={"auto": {"rules": "gated"}},
    )


def choice(value: str, probs: dict[str, float], conf: float):
    return {
        "type": "choice",
        "choice": value,
        "confidence": conf,
        "probabilities": probs,
    }


TORN_BETWEEN_EQUIVALENT = {
    "task": choice(
        "code", {"code": 0.475, "debugging": 0.475, "quick_question": 0.05}, 0.475
    )
}
TORN_BETWEEN_DIFFERENT = {
    "task": choice(
        "code", {"code": 0.475, "debugging": 0.05, "quick_question": 0.475}, 0.475
    )
}
FEATURES = Features(model="auto", message_count=1, est_tokens=100)


def test_c23_uncertainty_between_labels_with_the_same_action_does_not_escalate():
    config = gated_config(action_equivalent=True)
    result = evaluate(config, config.aliases["auto"], TORN_BETWEEN_EQUIVALENT, FEATURES)
    assert result.rule == "codey"
    assert result.effort == "low"
    assert any("routes the same way" in n for n in result.notes)


def test_c23_uncertainty_between_labels_with_different_actions_still_escalates():
    config = gated_config(action_equivalent=True)
    result = evaluate(config, config.aliases["auto"], TORN_BETWEEN_DIFFERENT, FEATURES)
    assert result.rule == "low_confidence"
    assert result.effort == "xhigh"


def test_c23_the_gate_keeps_its_old_behaviour_unless_it_is_asked_for():
    config = gated_config(action_equivalent=False)
    for answers in (TORN_BETWEEN_EQUIVALENT, TORN_BETWEEN_DIFFERENT):
        result = evaluate(config, config.aliases["auto"], answers, FEATURES)
        assert result.rule == "low_confidence"


def test_c23_without_a_vector_the_gate_fires_exactly_as_it_always_did():
    config = gated_config(action_equivalent=True)
    answers = {"task": {"type": "choice", "choice": "code", "confidence": 0.45}}
    result = evaluate(config, config.aliases["auto"], answers, FEATURES)
    assert result.rule == "low_confidence"


def test_c23_a_label_with_too_little_mass_is_not_a_second_reading():
    assert candidate_labels(TORN_BETWEEN_EQUIVALENT["task"], 0.1) == [
        "code",
        "debugging",
    ]
    assert candidate_labels(TORN_BETWEEN_EQUIVALENT["task"], 0.5) == []
    assert candidate_labels({"type": "noul", "noul": 0.5}, 0.1) == []


def test_c23_a_confident_answer_never_reaches_the_equivalence_check():
    config = gated_config(action_equivalent=True)
    answers = {
        "task": choice("quick_question", {"code": 0.1, "debugging": 0.0,
                                          "quick_question": 0.9}, 0.9)
    }
    result = evaluate(config, config.aliases["auto"], answers, FEATURES)
    assert result.rule == "rest"
