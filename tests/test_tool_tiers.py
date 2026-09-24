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

