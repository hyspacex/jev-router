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
         "gpt-6-astra", "medium", "moderate_careful"),
        ("debugging", 2.2, .5, True,
         "gpt-6-astra", "medium", "moderate_careful"),
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



@pytest.mark.parametrize(
    "busy,task,difficulty,harm,tools,images,model,effort",
    [
        # Ollama is busy: easy and moderate work moves to OpenAI's cheap
        # models, which the admission test found as good for that work.
        ("ollama", "quick-question", .5, .1, False, False, "gpt-6-luna", "low"),
        ("ollama", "code-edit", 1.8, .2, False, False, "gpt-6-sol", "low"),
        ("ollama", "code-edit", 1.8, .2, True, False, "gpt-6-sol", "low"),
        # Nothing there sends an image to a model that misread one.
        ("ollama", "quick-question", .5, .1, False, True, "ollama/gemma4-31b", None),
        # OpenAI is busy: careful work moves to glm-5.3, hard work from astra
        # to the cheaper sol.
        ("openai", "summarise-or-extract", 1.8, .5, False, False, "ollama/glm-5.3", "none"),
        ("openai", "debugging", 2.6, .1, False, False, "gpt-6-sol", "xhigh"),
        # The hardest band, high harm and images have no measured equivalent,
        # so they stay on astra however busy OpenAI is.
        ("openai", "debugging", 3.2, .1, False, False, "gpt-6-astra", "xhigh"),
        ("openai", "debugging", 2.0, .9, False, False, "gpt-6-astra", "medium"),
        ("openai", "debugging", 2.0, .1, False, True, "gpt-6-astra", "medium"),
    ],
)
def test_auto_balances_either_way_inside_measured_lanes(
    busy, task, difficulty, harm, tools, images, model, effort
):
    cfg = load_config("router.yaml")
    answers = {
        "task": {"type": "choice", "choice": task, "confidence": .9},
        "difficulty": {"type": "score", "score": difficulty, "confidence": .9},
        "harm_if_wrong": {"type": "noul", "noul": harm},
    }
    features = Features(model="auto", message_count=1, est_tokens=100,
                        has_tools=tools, has_images=images)
    chosen = select(cfg, cfg.aliases["auto"], answers, features, {busy: 0.9})
    assert (chosen.model, chosen.effort) == (model, effort)
    assert chosen.evidence == "qualified"


def test_the_retired_alias_routes_exactly_as_auto():
    cfg = load_config("router.yaml")
    retired, auto = cfg.aliases["auto-conserve"], cfg.aliases["auto"]
    assert retired.rules == auto.rules
    assert retired.allowed_models == auto.allowed_models
    assert retired.admission_fallback == auto.admission_fallback
