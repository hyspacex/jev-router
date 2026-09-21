"""A low score confidence escalates only when the top rubric level is real.

The stored admissions are the six times the live gate fired on difficulty
before this change. Two of them had a genuine top-level share and stay on
the frontier route. The other four were splits across the lower levels.
"""

from jev_router.config import load_config
from jev_router.features import Features
from jev_router.policy import evaluate


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


# id, answers, has_tools, expected rule, expected model
STORED = [
    (
        461,
        _answers(
            "agentic-tool-task", 0.95, 0.68, 0.32,
            {"0": 0.5, "1": 0.33, "2": 0.16, "3": 0.01}, 0.37,
        ),
        True,
        "easy",
        "ollama/glm-5.3-flash",
    ),
    (
        576,
        _answers(
            "agentic-tool-task", 0.99, 1.46, 0.32,
            {"0": 0.13, "1": 0.35, "2": 0.45, "3": 0.07}, 0.24,
        ),
        True,
        "moderate_tools",
        "ollama/glm-5.3",
    ),
    (
        601,
        _answers(
            "quick-question", 0.65, 0.96, 0.04,
            {"0": 0.45, "1": 0.17, "2": 0.35, "3": 0.03}, 0.66,
        ),
        True,
        "easy",
        "ollama/glm-5.3-flash",
    ),
    (
        615,
        _answers(
            "agentic-tool-task", 0.79, 2.37, 0.37,
            {"0": 0.0, "1": 0.11, "2": 0.4, "3": 0.49}, 0.19,
        ),
        True,
        "low_confidence",
        "gpt-6-astra",
    ),
    (
        674,
        _answers(
            "agentic-tool-task", 0.73, 2.15, 0.15,
            {"0": 0.04, "1": 0.2, "2": 0.32, "3": 0.44}, 0.59,
        ),
        True,
        "low_confidence",
        "gpt-6-astra",
    ),
    (
        682,
        _answers(
            "agentic-tool-task", 0.89, 1.11, 0.08,
            {"0": 0.33, "1": 0.25, "2": 0.4, "3": 0.02}, 0.48,
        ),
        True,
        "easy",
        "ollama/glm-5.3-flash",
    ),
]


def _features(has_tools: bool) -> Features:
    return Features(model="auto", has_tools=has_tools, message_count=2, est_tokens=200)


def test_stored_admissions_follow_the_top_level_mass():
    cfg = load_config("router.yaml")
    alias = cfg.aliases["auto"]
    for decision_id, answers, has_tools, rule, model in STORED:
        result = evaluate(cfg, alias, answers, _features(has_tools))
        assert (result.rule, result.model) == (rule, model), decision_id
        if rule == "easy":
            assert any("top level" in note for note in result.notes), decision_id


def test_a_score_with_no_vector_still_escalates():
    cfg = load_config("router.yaml")
    answers = {
        "task": {
            "type": "choice",
            "choice": "agentic-tool-task",
            "confidence": 0.9,
        },
        "difficulty": {"type": "score", "score": 1.11, "confidence": 0.08},
        "harm_if_wrong": {"type": "noul", "noul": 0.48},
    }
    result = evaluate(cfg, cfg.aliases["auto"], answers, _features(True))
    assert result.rule == "low_confidence"
    assert result.model == "gpt-6-astra"


def test_top_level_mass_on_the_noise_line_still_escalates():
    cfg = load_config("router.yaml")
    answers = _answers(
        "agentic-tool-task", 0.9, 1.11, 0.08,
        {"0": 0.30, "1": 0.30, "2": 0.30, "3": 0.10}, 0.2,
    )
    result = evaluate(cfg, cfg.aliases["auto"], answers, _features(True))
    assert result.rule == "low_confidence"


def test_a_low_confidence_task_still_escalates_when_the_score_tail_is_noise():
    cfg = load_config("router.yaml")
    answers = _answers(
        "agentic-tool-task", 0.2, 1.11, 0.08,
        {"0": 0.33, "1": 0.25, "2": 0.4, "3": 0.02}, 0.48,
    )
    result = evaluate(cfg, cfg.aliases["auto"], answers, _features(True))
    assert result.rule == "low_confidence"
    assert "task confidence" in result.reason
