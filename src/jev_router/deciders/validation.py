"""Validate the Jev fields used by routing, without coercion or prompt logging."""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any, maximum: float) -> bool:
    return (
        type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)
    )


def validate_answer(answer: Any, question: dict[str, Any]) -> dict[str, Any]:
    """One answer, checked and stripped to the fields routing may read.

    Raises ValueError on anything it cannot vouch for. Nothing is coerced: a
    number where a choice belongs is a refusal, not a cast.
    """
    kind = question["type"]
    if not isinstance(answer, dict) or answer.get("type") != kind:
        raise ValueError("missing or mistyped Jev answer")
    value = answer.get(kind)
    if kind == "choice":
        if not isinstance(value, str) or value not in question["criteria"]:
            raise ValueError("invalid Jev choice")
    elif not _number(value, len(question["criteria"]) - 1 if kind == "score" else 1):
        raise ValueError("invalid Jev numeric answer")
    if kind != "noul" and not _number(answer.get("confidence"), 1):
        raise ValueError("invalid Jev confidence")
    # A Score is a probability-weighted average, so the vector says something
    # the mean cannot. Keep it when it is complete; accept older minimal
    # responses that have none; never retain a malformed or off-vocabulary one.
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
    return clean


def validate_response(
    data: Any,
    questions: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """The active answers. Any problem at all fails the whole packet.

    Only the questions passed in are looked at, so an extra key in the reply,
    including a shadow answer, can neither satisfy nor weaken this.
    """
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise ValueError("invalid Jev answer envelope")
    answers = {}
    for qid, question in questions.items():
        answers[qid] = validate_answer(data["answers"].get(qid), question)
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("invalid Jev usage")
    tokens = usage.get("input_tokens")
    for key in ("input_tokens", "output_tokens"):
        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
            raise ValueError("invalid Jev token count")
    return answers, tokens


def validate_shadow(
    data: Any,
    questions: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The shadow answers, each judged on its own. Never raises.

    A shadow answer is an experiment. One that comes back unusable is dropped
    and its question id is returned so the count can be recorded. It is not
    allowed to decide anything, so it is also not allowed to bring down a
    packet whose active answers are sound.
    """
    if not questions:
        return {}, []
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        return {}, sorted(questions)
    answers: dict[str, dict[str, Any]] = {}
    invalid: list[str] = []
    for qid, question in questions.items():
        try:
            answers[qid] = validate_answer(data["answers"].get(qid), question)
        except (ValueError, KeyError, TypeError):
            invalid.append(qid)
    return answers, invalid
