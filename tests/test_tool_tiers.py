"""Shipped policy: tool availability is not a frontier requirement."""

import pytest

from jev_router.config import load_config
from jev_router.features import Features
from jev_router.policy import select


@pytest.mark.parametrize(
    "task,difficulty,harm,tools,model,effort,rule",
    [
        # Tool schemas at moderate difficulty reach the mid model at high
        # effort, which is what the bounded pilot qualified.
        ("debugging", 2.2, .37, True,
         "ollama/glm-5.3", "high", "moderate_tools"),
        ("agentic-tool-task", 1.78, .16, True,
         "ollama/glm-5.3", "high", "moderate_tools"),
        ("code-edit", 1.4, .5, True,
         "ollama/glm-5.3", "high", "moderate_tools"),
        # Faithfulness-sensitive work is still frontier, tools or not, because
        # moderate_careful is matched before moderate_tools.
        ("summarise-or-extract", 1.8, .5, True,
         "gpt-6-astra", "low", "moderate_careful"),
        ("debugging", 2.2, .5, True,
         "gpt-6-astra", "low", "moderate_careful"),
        # The same request without tools: the tool rule is what adds the
        # reasoning effort, not the difficulty.
        ("code-edit", 1.8, .2, False,
         "ollama/glm-5.3", "none", "moderate"),
    ],
)
def test_shipped_tool_tiers(task, difficulty, harm, tools, model, effort, rule):
    cfg = load_config("router.yaml")
    answers = {
        "task": {"type": "choice", "choice": task, "confidence": .9},
        "difficulty": {"type": "score", "score": difficulty, "confidence": .9},
        "harm_if_wrong": {"type": "noul", "noul": harm},
    }
    features = Features(model="auto", message_count=1, est_tokens=100,
                        has_tools=tools)
    chosen = select(cfg, cfg.aliases["auto"], answers, features)
    assert (chosen.model, chosen.effort, chosen.rule) == (model, effort, rule)


def test_pressure_lands_hard_tool_work_in_the_moderate_tools_lane():
    """A legacy alias still shifts the `hard` cutoff under quota pressure.

    The shipped `auto` is strict and ignores pressure entirely; that is
    `test_lanes.py::test_strict_quality_requirement_does_not_move_with_quota`.
    This is the legacy half: with the cutoff shifted out of reach, a
    tool-bearing request falls through to `moderate_tools` and lands on the
    pair that lane qualified, rather than anywhere the lane did not name.
    """
    cfg = load_config("router.yaml")
    legacy = cfg.aliases["auto"].model_copy(update={"session_mode": "legacy"})
    answers = {
        "task": {"type": "choice", "choice": "code-edit", "confidence": .9},
        "difficulty": {"type": "score", "score": 2.5, "confidence": .9},
        "harm_if_wrong": {"type": "noul", "noul": .1},
    }
    features = Features(model="auto", message_count=1, est_tokens=100,
                        has_tools=True)
    calm = select(cfg, legacy, answers, features)
    assert (calm.model, calm.effort, calm.lane) == (
        "gpt-6-astra", "high", "frontier-work"
    )
    pressed = select(cfg, legacy, answers, features, {"openai": 1.0})
    assert pressed.rule == "moderate_tools"
    assert (pressed.model, pressed.effort) == ("ollama/glm-5.3", "high")
    assert pressed.lane == "moderate-tools"
    assert pressed.pressure_changed_the_outcome is True
