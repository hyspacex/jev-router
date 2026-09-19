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
from .features import Features
from .policy import RoutingError, finalize, select

# The rule name an admission that never reached the classifier is stored under.
ADMISSION_FALLBACK = "admission_fallback"

# Bumped when the shape of a packet version changes, so two strings from
# different releases can never be compared as if they meant the same thing.
PACKET_SCHEMA = "pkt1"


def digest(payload: Any) -> str:
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def question_hash(config: RouterConfig, question_ids: list[str]) -> str:
    """A hash of the exact wording of these questions."""
    return digest({qid: config.questions.get(qid) for qid in sorted(question_ids)})


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
        digest(
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


# --- replaying a decision from its own record ----------------------------


def features_from_facts(facts: dict[str, Any]) -> Features:
    """Rebuild what the policy needs from a logged row.

    The decision log keeps counts and flags and never message text, so this is
    everything a replay can have, and it is all a replay needs: invariant I13
    says an active decision is reproducible from versioned configuration, the
    semantic answers, the request facts and the quota snapshot, without the
    original prompt.
    """
    return Features(
        model=str(facts.get("requested_model") or "auto"),
        message_count=int(facts.get("message_count") or 0),
        est_tokens=int(facts.get("est_tokens") or 0),
        total_chars=int(facts.get("total_chars") or 0),
        has_tools=bool(facts.get("has_tools")),
        tool_names=tuple(facts.get("tool_names") or ()),
        has_images=bool(facts.get("has_images")),
        has_code=bool(facts.get("has_code")),
        languages=tuple(facts.get("languages") or ()),
        client=str(facts.get("client") or ""),
        stream=bool(facts.get("stream")),
        max_tokens=int(facts.get("max_tokens") or 0),
    )


@dataclass
class Replay:
    """What a stored decision does when the same selection is run again."""

    decision_id: str
    stored: tuple[str, str | None]
    stored_rule: str
    replayed: tuple[str, str | None] | None = None
    replayed_rule: str = ""
    lane: str = ""
    evidence: str = ""
    counterfactual: tuple[str, str | None] | None = None
    pressures: dict[str, float] = field(default_factory=dict)
    config_hash_stored: str = ""
    config_hash_now: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def same_config(self) -> bool:
        return bool(self.config_hash_stored) and (
            self.config_hash_stored == self.config_hash_now
        )

    @property
    def reproduced(self) -> bool:
        return self.replayed is not None and self.replayed == self.stored

    def verdict(self) -> str:
        if self.error:
            return f"cannot replay: {self.error}"
        if not self.same_config:
            # A different router.yaml is a different policy. Saying "matches"
            # here would claim something the run did not show.
            return (
                "config has changed since this decision "
                f"({self.config_hash_stored or 'unrecorded'} -> "
                f"{self.config_hash_now}); "
                + (
                    "the replay lands on the same route anyway"
                    if self.reproduced
                    else "the replay lands somewhere else"
                )
            )
        return (
            "reproduced: same config, same answers, same route"
            if self.reproduced
            else "NOT reproduced: the same inputs now route somewhere else"
        )


def replay_decision(config: RouterConfig, row: dict[str, Any]) -> Replay:
    """Run the pure selection again from a stored decision row.

    Pure with respect to the world: no Jev call, no quota poll, no upstream.
    The answers, the request facts and the pressure all come off the row.
    """
    stored = (str(row.get("model") or ""), row.get("effort"))
    out = Replay(
        decision_id=str(row.get("decision_id") or ""),
        stored=stored,
        stored_rule=str(row.get("rule") or ""),
        config_hash_stored=str(row.get("config_hash") or ""),
        config_hash_now=config.config_hash,
        pressures=_pressures(row.get("pressures")),
    )
    alias = str(row.get("alias") or "")
    alias_cfg = config.aliases.get(alias)
    if alias_cfg is None:
        out.error = f"alias {alias!r} is not in this router.yaml any more"
        return out
    answers = row.get("answers") or {}
    if not isinstance(answers, dict):
        out.error = "the stored answers are unreadable"
        return out
    if not answers:
        out.notes.append(
            "no Jev answers were stored; this decision came from a fallback, "
            "and a fallback is not replay evidence"
        )
    facts = row.get("features") or {}
    if not isinstance(facts, dict):
        out.error = "the stored request facts are unreadable"
        return out
    if out.stored_rule == ADMISSION_FALLBACK:
        # The classifier never answered, so the ruleset never ran. Replaying
        # it against no answers would compare this binding with a route it
        # was never offered. What is reproducible here is the alias's own
        # admission fallback, so that is what is run.
        return _replay_fallback(config, alias_cfg, facts, out)
    try:
        result = select(
            config, alias_cfg, answers, features_from_facts(facts), out.pressures
        )
    except RoutingError as exc:
        out.error = str(exc)
        return out
    out.replayed = (result.model, result.effort)
    out.replayed_rule = result.rule
    out.lane = result.lane
    out.evidence = result.evidence
    out.counterfactual = result.counterfactual
    out.notes.extend(result.notes)
    return out


def _replay_fallback(
    config: RouterConfig, alias_cfg: AliasCfg, facts: dict[str, Any], out: Replay
) -> Replay:
    """Re-resolve the alias's `admission_fallback`, which is what bound."""
    out.notes.append(
        "the classifier was unavailable, so this binding came from the alias's "
        "admission_fallback; there are no semantic answers to replay"
    )
    route = alias_cfg.admission_fallback
    if route is None:
        out.error = f"alias {alias_cfg.description!r} declares no admission_fallback any more"
        return out
    try:
        result = finalize(
            config,
            alias_cfg,
            route,
            ADMISSION_FALLBACK,
            "replayed admission fallback",
            features_from_facts(facts),
        )
    except RoutingError as exc:
        out.error = str(exc)
        return out
    out.replayed = (result.model, result.effort)
    out.replayed_rule = result.rule
    # No lane was matched and no counterfactual was drawn, because no rule ran
    # and pressure was never consulted. Both stay empty rather than borrowing
    # a value from a decision that did not happen.
    out.evidence = "none"
    out.notes.extend(result.notes)
    return out


def _pressures(raw: Any) -> dict[str, float]:
    """The pressure a decision was taken under, ignoring zeros and rubbish."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for name, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            out[str(name)] = number
    return out
