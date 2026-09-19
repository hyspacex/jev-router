"""A no-network decider.

It guesses the same answers Jev would give, from counts alone, then runs them
through the ordinary policy. That keeps one code path for routing and means a
Jev outage changes the quality of the answers, not the shape of the system.
"""

from __future__ import annotations

from typing import Any

from ..config import AliasCfg, RouterConfig
from ..features import Features
from ..policy import evaluate
from .base import Decider, Decision, register_decider

# Added to every heuristic difficulty score. This decider runs when Jev is
# down, and it reads counts, not meaning, so it misses hard work that looks
# small. Under-routing is the expensive mistake, so it errs upward. Measured on
# evals/cases.yaml against the tuned ladder: without the margin it under-routes
# 41% of cases, with it 23%.
SAFETY_MARGIN = 0.5

HARD_WORDS = (
    "refactor",
    "debug",
    "architecture",
    "design",
    "migrate",
    "across",
    "step by step",
    "root cause",
)


class RulesDecider:
    name = "rules"

    def __init__(self, config: RouterConfig) -> None:
        self.config = config

    async def decide(self, features: Features, alias_cfg: AliasCfg) -> Decision:
        answers = self._guess(features)
        # Only answer the questions this alias actually asks about.
        answers = {k: v for k, v in answers.items() if k in alias_cfg.questions}
        result = evaluate(self.config, alias_cfg, answers, features)
        return Decision(
            model=result.model,
            effort=result.effort,
            rule=result.rule,
            reason=f"heuristic: {result.reason}",
            answers=answers,
            fallback=False,
            state_builder=alias_cfg.state_builder,
            notes=result.notes,
            decider=self.name,
        )

    def _guess(self, features: Features) -> dict[str, dict[str, Any]]:
        text = features.last_user_message.lower()
        long_request = features.est_tokens > 4000 or len(text) > 1500
        hard_word = any(w in text for w in HARD_WORDS)

        score = 0.0
        if features.has_code or features.has_tools:
            score += 1.0
        if long_request:
            score += 1.0
        if hard_word:
            score += 1.0
        if features.message_count > 6:
            score += 0.5
        score = min(score + SAFETY_MARGIN, 3.0)

        # These names have to match the categories in router.yaml's `task`
        # question, or no rule with a `task` condition can ever fire here.
        if features.has_tools:
            task = "agentic-tool-task"
        elif features.has_code:
            task = "code-edit"
        elif score >= 2:
            task = "design-or-review"
        else:
            task = "quick-question"

        # A confidence is given only where there is direct evidence: tools or
        # code blocks for the task, a score at either end of the range for the
        # difficulty. Otherwise the key is left out, and the policy's
        # low-confidence gate skips it. That gate is calibrated against Jev's
        # confidence, which this heuristic does not produce; a flat made-up
        # value below the gate would send every request to the strong model
        # for as long as Jev is down.
        task_answer: dict[str, Any] = {"type": "choice", "choice": task}
        if features.has_tools or features.has_code:
            task_answer["confidence"] = 0.7
        difficulty_answer: dict[str, Any] = {"type": "score", "score": score}
        if score in (0.0, 3.0):
            difficulty_answer["confidence"] = 0.7
        return {
            "task": task_answer,
            "difficulty": difficulty_answer,
            "harm_if_wrong": {"type": "noul", "noul": 0.5},
        }


@register_decider("rules")
def _make_rules(config: RouterConfig, deps: dict[str, Any]) -> Decider:
    return RulesDecider(config)
