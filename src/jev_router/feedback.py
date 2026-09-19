"""Validation for routing feedback, shared by the HTTP endpoint and the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import RouterConfig
from .pins import VERDICTS

NOTE_MAX_CHARS = 2000


class FeedbackError(ValueError):
    """Raised with a message the caller can act on."""


@dataclass
class Feedback:
    decision_id: str
    verdict: str
    better_model: str | None = None
    better_effort: str | None = None
    note: str | None = None


def validate(config: RouterConfig, payload: dict[str, Any]) -> Feedback:
    """Check one feedback submission against the configured models and efforts."""
    decision_id = payload.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise FeedbackError("decision_id is required (an id, or \"last\")")

    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise FeedbackError(f"verdict must be one of {', '.join(VERDICTS)}")

    better_model = payload.get("better_model") or None
    if better_model is not None:
        if better_model not in config.models:
            raise FeedbackError(
                f"better_model {better_model!r} is not configured "
                f"(known: {', '.join(config.models)})"
            )

    better_effort = payload.get("better_effort") or None
    if better_effort is not None:
        if better_effort not in config.settings.effort_order:
            raise FeedbackError(
                f"better_effort {better_effort!r} is not in settings.effort_order "
                f"({', '.join(config.settings.effort_order)})"
            )
        if better_model is not None:
            allowed = config.models[better_model].efforts
            if allowed and better_effort not in allowed:
                raise FeedbackError(
                    f"model {better_model!r} does not allow effort {better_effort!r} "
                    f"(allowed: {', '.join(allowed)})"
                )

    note = payload.get("note") or None
    if note is not None:
        if not isinstance(note, str):
            raise FeedbackError("note must be text")
        note = note[:NOTE_MAX_CHARS]

    return Feedback(
        decision_id=decision_id.strip(),
        verdict=verdict,
        better_model=better_model,
        better_effort=better_effort,
        note=note,
    )
