"""Validation for routing feedback, shared by the HTTP endpoint and the CLI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import RouterConfig
from .pins import VERDICTS
from .scrub import clean

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


# --- session-native feedback (POST /router/sessions/{id}/feedback) ---------

SESSION_FEEDBACK_KEYS = frozenset(["verdict", "adjust_effort", "request_id", "note", "source"])
_SOURCE = re.compile(r"[a-z0-9_-]{1,32}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")


@dataclass
class SessionFeedback:
    verdict: str
    adjust_effort: bool = False
    request_id: str | None = None
    note: str | None = None
    source: str = "api"


def validate_session(payload: Any) -> SessionFeedback:
    """Check one session feedback body. Unknown keys are refused.

    The note is scrubbed and capped here, so a key pasted into a complaint
    is never written down.
    """
    if not isinstance(payload, dict):
        raise FeedbackError("body must be a JSON object")
    unknown = sorted(set(payload) - SESSION_FEEDBACK_KEYS)
    if unknown:
        raise FeedbackError(f"unknown field(s): {', '.join(unknown)}")
    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise FeedbackError(f"verdict must be one of {', '.join(VERDICTS)}")
    adjust = payload.get("adjust_effort", False)
    if not isinstance(adjust, bool):
        raise FeedbackError("adjust_effort must be true or false")
    request_id = payload.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
    ):
        raise FeedbackError("request_id must be 1 to 200 id characters")
    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise FeedbackError("note must be text")
        note = clean(note, NOTE_MAX_CHARS).strip() or None
    source = payload.get("source", "api")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise FeedbackError("source must be 1 to 32 of a-z, 0-9, '_' and '-'")
    return SessionFeedback(
        verdict=verdict,
        adjust_effort=adjust,
        request_id=request_id,
        note=note,
        source=source,
    )
