"""The semantic packet: which questions are asked, and under which version.

Jev answers questions about the work. This module says which questions go into
one call, keeps the active set and the shadow set apart, and stamps every
answer with a version string so a decision can be replayed later against the
wording that produced it.

Nothing here talks to the network. The decider does the call; this module
decides what goes in it and what comes out counts as what.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .config import AliasCfg, RouterConfig

# Bumped when the shape of a packet version changes, so two strings from
# different releases can never be compared as if they meant the same thing.
PACKET_SCHEMA = "pkt1"


def _digest(payload: Any) -> str:
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def question_hash(config: RouterConfig, question_ids: list[str]) -> str:
    """A hash of the exact wording of these questions."""
    return _digest({qid: config.questions.get(qid) for qid in sorted(question_ids)})


def packet_version(
    config: RouterConfig, question_ids: list[str], state_builder: str
) -> str:
    """Question wording + state builder + Jev model, as one string.

    Three things decide what an answer means, and all three have to move
    together: what was asked, what it was asked about, and which model read it.
    A builder is versioned by its registered name, so changing what a builder
    produces means registering a new name rather than editing one in place.
    """
    if not question_ids:
        return ""
    return "{}:{}".format(
        PACKET_SCHEMA,
        _digest(
            {
                "questions": {
                    qid: config.questions.get(qid) for qid in sorted(question_ids)
                },
                "state_builder": state_builder,
                "jev_model": config.settings.jev_model,
            }
        ),
    )


def active_questions(config: RouterConfig, alias_cfg: AliasCfg) -> list[str]:
    """What this alias routes on, in the order it wrote them down."""
    return [qid for qid in alias_cfg.questions if qid in config.questions]


def shadow_questions(config: RouterConfig, alias_cfg: AliasCfg) -> list[str]:
    """What rides along in the same call and may not touch the route."""
    return config.shadow_questions(alias_cfg)


@dataclass
class SemanticResult:
    """One classification of one turn. No model, no effort, no policy.

    This is the boundary Milestone C needs: a turn can be classified without
    anything here reselecting a model. `answers` is what the policy may read;
    `shadow` is recorded and never read by the routing path.
    """

    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    shadow: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Shadow questions whose answer came back unusable. Dropped, counted, and
    # never allowed to weaken the active validation.
    invalid_shadow: list[str] = field(default_factory=list)
    state: Any = None
    state_builder: str = ""
    packet_version: str = ""
    shadow_packet_version: str = ""
    jev_ms: float | None = None
    jev_input_tokens: int | None = None
    # Set when the call failed. The answers are then empty and the caller
    # takes its configured fallback.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def shadow_values(self) -> dict[str, Any]:
        """Shadow answers as bare values, which is all the log ever keeps."""
        out: dict[str, Any] = {}
        for qid, answer in self.shadow.items():
            kind = answer.get("type")
            value = answer.get(kind) if isinstance(kind, str) else None
            row: dict[str, Any] = {"type": kind, "value": value}
            if answer.get("confidence") is not None:
                row["confidence"] = answer["confidence"]
            if answer.get("probabilities"):
                row["probabilities"] = answer["probabilities"]
            out[qid] = row
        return out
