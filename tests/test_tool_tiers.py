"""Shipped policy: tool availability is not a frontier requirement."""

import pytest

from jev_router.config import load_config
from jev_router.features import Features
from jev_router.policy import select


@pytest.mark.parametrize(
    "task,difficulty,harm,tools,images,confidence,model,effort,rule",
    [
        ("debugging", 2.2, .37, True, False, .9,
         "ollama/glm-5.3", "high", "moderate_tools"),
        ("agentic-tool-task", 1.78, .16, True, False, .9,
         "ollama/glm-5.3", "high", "moderate_tools"),
        ("code-edit", 1.4, .5, True, False, .9,
         "ollama/glm-5.3", "high", "moderate_tools"),
        ("summarise-or-extract", 1.8, .5, True, False, .9,
         "gpt-6-astra", "low", "moderate_careful"),
        ("debugging", 2.2, .5, True, False, .9,
         "gpt-6-astra", "low", "moderate_careful"),
        ("code-edit", 1.8, .2, False, False, .9,
         "ollama/glm-5.3", "none", "moderate"),
        ("code-edit", 1.39, .2, True, False, .9,
         "ollama/glm-5.3-flash", "none", "easy"),
        ("code-edit", 2.4, .2, True, False, .9,
         "gpt-6-astra", "high", "hard"),
        ("code-edit", 3.0, .2, True, False, .9,
         "gpt-6-astra", "xhigh", "very_hard"),
        ("code-edit", 1.8, .8, True, False, .9,
         "gpt-6-astra", "medium", "high_harm"),
        ("code-edit", 1.8, .2, True, True, .9,
         "gpt-6-astra", "medium", "images_need_vision"),
        ("code-edit", 1.8, .2, True, False, .3,
         "gpt-6-astra", "medium", "low_confidence"),
    ],
)
def test_shipped_tool_tiers(task, difficulty, harm, tools, images, confidence,
                            model, effort, rule):
    cfg = load_config("router.yaml")
    answers = {
        "task": {"type": "choice", "choice": task, "confidence": confidence},
        "difficulty": {"type": "score", "score": difficulty,
                       "confidence": confidence},
        "harm_if_wrong": {"type": "noul", "noul": harm},
    }
    features = Features(model="auto", message_count=1, est_tokens=100,
                        has_tools=tools, has_images=images)
    chosen = select(cfg, cfg.aliases["auto"], answers, features)
    assert (chosen.model, chosen.effort, chosen.rule) == (model, effort, rule)


def test_pressure_cannot_downgrade_hard_work_into_moderate_tools():
    cfg = load_config("router.yaml")
    answers = {
        "task": {"type": "choice", "choice": "code-edit", "confidence": .9},
        "difficulty": {"type": "score", "score": 2.5, "confidence": .9},
        "harm_if_wrong": {"type": "noul", "noul": .1},
    }
    chosen = select(cfg, cfg.aliases["auto"], answers,
                    Features(model="auto", message_count=1, est_tokens=100,
                             has_tools=True), {"openai": 1.0})
    assert (chosen.model, chosen.effort) == ("gpt-6-astra", "high")
    assert chosen.lane == "frontier-work"
