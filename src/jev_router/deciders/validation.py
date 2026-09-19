"""Validate the Jev fields used by routing, without coercion or prompt logging."""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any, maximum: float) -> bool:
    return (
        type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)
    )


def validate_response(
    data: Any,
    questions: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int | None]:
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise ValueError("invalid Jev answer envelope")
    answers = {}
    for qid, question in questions.items():
        answer = data["answers"].get(qid)
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ValueError("missing or mistyped Jev answer")
        value = answer.get(kind)
        if kind == "choice":
            if not isinstance(value, str) or value not in question["criteria"]:
                raise ValueError("invalid Jev choice")
        elif not _number(
            value, len(question["criteria"]) - 1 if kind == "score" else 1
        ):
            raise ValueError("invalid Jev numeric answer")
        if kind != "noul" and not _number(answer.get("confidence"), 1):
            raise ValueError("invalid Jev confidence")
        # Distributions are not needed for routing. Accept older minimal
        # responses, but never retain malformed or off-vocabulary probabilities.
        if "probabilities" in answer:
            probabilities = answer["probabilities"]
            keys = (
                question.get("criteria", {})
                if kind == "choice"
                else {str(i) for i in range(len(question.get("criteria", [])))}
            )
            if not isinstance(probabilities, dict) or any(
                key not in keys or not _number(probability, 1)
                for key, probability in probabilities.items()
            ):
                raise ValueError("invalid Jev probabilities")
            if probabilities and (
                set(probabilities) != set(keys)
                or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.01)
            ):
                raise ValueError("incomplete Jev probability distribution")
        # Do not retain arbitrary provider fields or unsolicited answers.
        clean = {"type": kind, kind: value}
        if kind != "noul":
            clean["confidence"] = answer["confidence"]
        if "probabilities" in answer:
            clean["probabilities"] = answer["probabilities"]
        answers[qid] = clean
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("invalid Jev usage")
    tokens = usage.get("input_tokens")
    for key in ("input_tokens", "output_tokens"):
        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
            raise ValueError("invalid Jev token count")
    return answers, tokens
