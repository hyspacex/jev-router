"""A low score confidence escalates only when the top rubric level is real.

The check is scoped: `low_confidence.skip_when_top_is_noise` names the gated
score questions it applies to, and `score_top_min_mass` is the line. An empty
list is the old gate.

The stored admissions are three of the six times the live gate fired on
difficulty before this change. One had a genuine top-level share and stays on
the frontier route. The other two were splits across the lower levels.
"""

from pathlib import Path
from typing import Any

import yaml

from conftest import make_config
from jev_router.config import RouterConfig
from jev_router.features import Features
from jev_router.policy import evaluate

ROOT = Path(__file__).resolve().parents[1]


def _shipped(**gate: Any) -> RouterConfig:
    """router.yaml, optionally with the gate block overridden."""
    raw = yaml.safe_load((ROOT / "router.yaml").read_text())
    raw["policy"]["low_confidence"].update(gate)
    return RouterConfig.model_validate(raw)


def _answers(task, task_conf, score, score_conf, probs, harm):
    return {
        "task": {
            "type": "choice",
            "choice": task,
            "confidence": task_conf,
            "probabilities": {task: task_conf},
        },
        "difficulty": {
            "type": "score",
            "score": score,
            "confidence": score_conf,
            "probabilities": probs,
        },
        "harm_if_wrong": {"type": "noul", "noul": harm},
    }


def _features(has_tools: bool = True) -> Features:
    return Features(model="auto", has_tools=has_tools, message_count=2, est_tokens=200)


# The mass split across the lower levels. The gate skips these.
NOISY_TOP = _answers(
    "agentic-tool-task", 0.95, 0.68, 0.32,
    {"0": 0.5, "1": 0.33, "2": 0.16, "3": 0.01}, 0.37,
)
# Same shape, but the mean lands on the tool tier rather than the fast one.
NOISY_TOP_MODERATE = _answers(
    "agentic-tool-task", 0.99, 1.46, 0.32,
    {"0": 0.13, "1": 0.35, "2": 0.45, "3": 0.07}, 0.24,
)
# A real reading on the top level. The gate still escalates this.
REAL_TOP = _answers(
    "agentic-tool-task", 0.79, 2.37, 0.37,
    {"0": 0.0, "1": 0.11, "2": 0.4, "3": 0.49}, 0.19,
)

# decision id, answers, expected rule, expected model
STORED = [
    (461, NOISY_TOP, "easy", "ollama/glm-5.3-flash"),
    (576, NOISY_TOP_MODERATE, "moderate_tools", "ollama/glm-5.3"),
    (615, REAL_TOP, "low_confidence", "gpt-6-astra"),
]


def test_stored_admissions_follow_the_top_level_mass():
    cfg = _shipped()
    alias = cfg.aliases["auto"]
    for decision_id, answers, rule, model in STORED:
        result = evaluate(cfg, alias, answers, _features())
        assert (result.rule, result.model) == (rule, model), decision_id
        skipped = rule != "low_confidence"
        said_so = any("top level" in note for note in result.notes)
        assert said_so is skipped, decision_id


def test_a_score_with_no_vector_still_escalates():
    cfg = _shipped()
    answers = {
        "task": {
            "type": "choice",
            "choice": "agentic-tool-task",
            "confidence": 0.9,
        },
        "difficulty": {"type": "score", "score": 1.11, "confidence": 0.08},
        "harm_if_wrong": {"type": "noul", "noul": 0.48},
    }
    result = evaluate(cfg, cfg.aliases["auto"], answers, _features())
    assert result.rule == "low_confidence"
    assert result.model == "gpt-6-astra"


def test_top_level_mass_on_the_noise_line_still_escalates():
    cfg = _shipped()
    answers = _answers(
        "agentic-tool-task", 0.9, 1.11, 0.08,
        {"0": 0.30, "1": 0.30, "2": 0.30, "3": 0.10}, 0.2,
    )
    result = evaluate(cfg, cfg.aliases["auto"], answers, _features())
    assert result.rule == "low_confidence"


def test_a_low_confidence_choice_still_escalates_when_the_score_top_is_noise():
    """`task` is a choice. The skip cannot reach it, whatever difficulty did."""
    cfg = _shipped()
    answers = _answers(
        "agentic-tool-task", 0.2, 1.11, 0.08,
        {"0": 0.33, "1": 0.25, "2": 0.4, "3": 0.02}, 0.48,
    )
    result = evaluate(cfg, cfg.aliases["auto"], answers, _features())
    assert result.rule == "low_confidence"


def test_an_empty_scope_is_the_gate_as_it_was():
    """The default. Every row above escalates again, notes and all."""
    cfg = _shipped(skip_when_top_is_noise=[])
    alias = cfg.aliases["auto"]
    for answers in (NOISY_TOP, NOISY_TOP_MODERATE, REAL_TOP):
        result = evaluate(cfg, alias, answers, _features())
        assert result.rule == "low_confidence"
        assert result.model == "gpt-6-astra"
        assert not any("top level" in note for note in result.notes)


def test_equivalence_min_mass_no_longer_moves_the_skip():
    """The two masses are separate numbers now.

    `equivalence_min_mass` is the floor for "this label is a candidate". Moving
    it across the whole range leaves the top-level check where the config put
    it, so the noisy row still falls through and the on-the-line row does not.
    """
    on_the_line = _answers(
        "agentic-tool-task", 0.9, 1.11, 0.08,
        {"0": 0.30, "1": 0.30, "2": 0.30, "3": 0.10}, 0.2,
    )
    for mass in (0.0, 0.05, 0.5, 0.9):
        cfg = _shipped(equivalence_min_mass=mass)
        alias = cfg.aliases["auto"]
        assert evaluate(cfg, alias, NOISY_TOP, _features()).rule == "easy", mass
        assert (
            evaluate(cfg, alias, on_the_line, _features()).rule == "low_confidence"
        ), mass


def _two_scores() -> RouterConfig:
    """A gate over two score questions that only lists one of them."""
    return make_config(
        questions={
            "risk": {
                "type": "score",
                "instructions": "How much could go wrong?",
                "criteria": ["None.", "Some.", "A lot."],
            }
        },
        policy={
            "low_confidence": {
                "min_confidence": 0.45,
                "questions": ["difficulty", "risk"],
                "skip_when_top_is_noise": ["difficulty"],
                "score_top_min_mass": 0.1,
                "use": {"model": "big", "effort": "medium"},
            }
        },
    )


def _score(value, conf, probs):
    return {
        "type": "score",
        "score": value,
        "confidence": conf,
        "probabilities": probs,
    }


def test_a_score_outside_the_scope_still_escalates():
    cfg = _two_scores()
    alias = cfg.aliases["auto"]
    noisy = {"0": 0.6, "1": 0.38, "2": 0.02}
    answers = {
        "difficulty": _score(0.42, 0.1, noisy),
        "risk": _score(0.42, 0.1, noisy),
        "harm_if_wrong": {"type": "noul", "noul": 0.1},
    }
    # `risk` has the same noisy top level and is not in the scope, so it fires.
    result = evaluate(cfg, alias, answers, Features(model="auto", est_tokens=100))
    assert result.rule == "low_confidence"
    assert result.model == "big"

    # With `risk` confident, only the scoped question is left and it skips.
    answers["risk"] = _score(0.42, 0.9, {"0": 0.9, "1": 0.05, "2": 0.05})
    result = evaluate(cfg, alias, answers, Features(model="auto", est_tokens=100))
    assert result.rule == "easy"
    assert any("top level" in note for note in result.notes)
