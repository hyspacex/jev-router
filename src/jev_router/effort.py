"""The between-turn effort policy, and the rules the update ledger follows.

Pure functions only: no network, no clock, no database. Quota pressure arrives
as an argument. Given the same evidence, the same ladder and the same prior
recommendations, `plan_turn` always returns the same plan, which is what lets
`evals/effort_replay.py` compare policies offline and what keeps this module
out of the request path's way.

Two things this module deliberately cannot do:

- It never chooses a model. It only ever picks among the efforts of the
  profile a session is already bound to, so a turn assessment cannot reselect
  a model by accident. The caller hands it a ladder; it hands back a rung.
- It never decides whether an update was applied. `plan_turn` says what should
  happen; `advance` says what a ledger row's status becomes when the world
  reports back. HTTP acceptance, provider completion and a confirmed effort
  are three separate facts and this file keeps them apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# --- the ledger's statuses (spec 10.6) ----------------------------------

PLANNED = "planned"        # decided, nothing sent
ACCEPTED = "accepted"      # upstream returned 2xx headers
CONFIRMED = "confirmed"    # a valid successful provider completion carried it
REJECTED = "rejected"      # a definitive refusal before acceptance; retryable
OUTCOME_UNKNOWN = "outcome_unknown"  # submitted, and nobody can say what ran

STATUSES = (PLANNED, ACCEPTED, CONFIRMED, REJECTED, OUTCOME_UNKNOWN)

# A row in one of these states has an effort change in the air. Another change
# on top of it would be a competing transition, so it is blocked until the
# first one resolves. A `keep` plan is never in the way: nothing was moving.
OPEN = (PLANNED, ACCEPTED, OUTCOME_UNKNOWN)

# What a plan asks for.
KEEP = "keep"
CHANGE = "change_effort"
BLOCKED = "blocked"


# --- evidence ------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """What one turn is known to be, as numbers the policy can compare.

    Everything is optional because everything can be missing: Jev can time
    out, an answer can come back unusable, and a question may not have been
    asked. Missing evidence keeps the current effort; it never guesses a
    cheaper one.
    """

    difficulty: float | None = None
    p_hard: float | None = None
    harm_if_wrong: float | None = None
    corrective_followup: float | None = None
    # Shadow only, and only when the turn carries a grounded harness
    # observation. Read to refuse a raise, never to cause one.
    failure_mode: str | None = None
    # The latest message only agrees with or asks to carry on with work
    # already described. Not evidence for a downgrade.
    short_continuation: bool = False
    # Set when the classifier failed outright.
    error: str = ""

    @property
    def missing(self) -> tuple[str, ...]:
        """The answers the policy needs and did not get."""
        absent = []
        if self.difficulty is None:
            absent.append("difficulty")
        if self.harm_if_wrong is None:
            absent.append("harm_if_wrong")
        return tuple(absent)

    def to_facts(self) -> dict[str, Any]:
        return {
            "difficulty": self.difficulty,
            "p_hard": self.p_hard,
            "harm_if_wrong": self.harm_if_wrong,
            "corrective_followup": self.corrective_followup,
            "failure_mode": self.failure_mode,
            "short_continuation": self.short_continuation,
            "missing": list(self.missing),
            "error": self.error,
        }


# A failure the work itself cannot reason its way out of. Spec 10.5: an
# environment failure or missing information is not, by itself, evidence that
# more reasoning would help.
NOT_A_REASONING_PROBLEM = ("environment_problem", "missing_information")


@dataclass
class TurnPlan:
    """One turn's effort decision, and everything it was decided under."""

    action: str = KEEP
    from_effort: str | None = None
    to_effort: str | None = None
    base_effort: str | None = None
    # What the policy wants, before hysteresis has its say. Shadow reports
    # this as the hypothetical effort, and the ledger counts it towards a
    # downgrade's confirmations.
    recommendation: str | None = None
    direction: str = "hold"  # up | down | hold
    reason: str = ""
    confirmations: int = 0
    needed_confirmations: int = 0
    floor: str | None = None
    ladder: list[str] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    quota_pressure: float = 0.0

    @property
    def changes(self) -> bool:
        return self.action == CHANGE

    def to_facts(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "from_effort": self.from_effort,
            "to_effort": self.to_effort,
            "base_effort": self.base_effort,
            "recommendation": self.recommendation,
            "direction": self.direction,
            "reason": self.reason,
            "confirmations": self.confirmations,
            "needed_confirmations": self.needed_confirmations,
            "floor": self.floor,
            "ladder": list(self.ladder),
            "thresholds": dict(self.thresholds),
            "evidence": dict(self.evidence),
            "quota_pressure": round(self.quota_pressure, 4),
        }


# --- the policy ----------------------------------------------------------


def _index(ladder: Sequence[str], effort: str | None) -> int | None:
    if effort is None:
        return None
    try:
        return list(ladder).index(effort)
    except ValueError:
        return None


def _wants_more(evidence: Evidence, t: Any) -> str:
    """Why this turn argues for more effort, or an empty string."""
    if evidence.difficulty is not None and evidence.difficulty >= t.upgrade_difficulty:
        return f"difficulty {evidence.difficulty:.2f} is at or above {t.upgrade_difficulty:g}"
    if evidence.p_hard is not None and evidence.p_hard >= t.upgrade_p_hard:
        return (
            f"{evidence.p_hard:.2f} of the difficulty mass is on the top level, "
            f"at or above {t.upgrade_p_hard:g}"
        )
    if evidence.harm_if_wrong is not None and evidence.harm_if_wrong >= t.upgrade_harm:
        return f"harm_if_wrong {evidence.harm_if_wrong:.2f} is at or above {t.upgrade_harm:g}"
    if (
        evidence.corrective_followup is not None
        and evidence.corrective_followup >= t.corrective_followup
    ):
        if evidence.failure_mode in NOT_A_REASONING_PROBLEM:
            # The follow-up is real, but the evidence says the obstacle is a
            # missing credential or a missing fact. More reasoning is not the
            # remedy, so this alone does not raise.
            return ""
        return (
            f"the follow-up names a defect in the earlier result "
            f"({evidence.corrective_followup:.2f})"
        )
    return ""


def _wants_less(evidence: Evidence, t: Any) -> str:
    """Why this turn is low risk enough to drop a rung, or an empty string."""
    if evidence.short_continuation:
        # "yes, go on" says nothing about how hard the next step is.
        return ""
    if evidence.difficulty is None or evidence.difficulty > t.downgrade_difficulty:
        return ""
    if evidence.harm_if_wrong is None or evidence.harm_if_wrong > t.downgrade_harm:
        return ""
    if (
        evidence.corrective_followup is not None
        and evidence.corrective_followup >= t.corrective_followup
    ):
        return ""
    return (
        f"difficulty {evidence.difficulty:.2f} and harm {evidence.harm_if_wrong:.2f} "
        f"are both low"
    )


def confirmations_for(recommendations: Sequence[str | None], effort: str) -> int:
    """How many eligible turns in a row have now asked for this effort.

    The list is oldest first and holds one entry per eligible turn boundary,
    including the turn being planned. A turn that recommended something else
    breaks the run.
    """
    count = 0
    for value in reversed(list(recommendations)):
        if value != effort:
            break
        count += 1
    return count


def plan_turn(
    *,
    cfg: Any,
    ladder: Sequence[str],
    current: str | None,
    base: str | None,
    floor: str | None,
    evidence: Evidence,
    recommendations: Sequence[str | None] = (),
    pressure: float = 0.0,
) -> TurnPlan:
    """Decide what this turn's effort should be. Pure.

    `cfg` is an `AdaptiveEffortCfg`; `ladder` is the rungs of the bound
    profile that the alias, the client and the quality lane all permit,
    weakest first. `floor` is the strongest of the explicit minimums, and
    nothing below it is ever recommended. `recommendations` is one entry per
    prior eligible turn, oldest first, used only for counting a downgrade's
    confirmations.

    The mechanics of spec 10.5, all configurable and all recorded:

    - Upward may act at the next eligible boundary.
    - Downward needs low-risk evidence and N consecutive recommendations.
    - A short "continue" is not downgrade evidence.
    - A corrective follow-up can raise, unless the failure looks like a
      missing credential or a missing fact.
    - A classifier timeout, malformed output or missing evidence keeps the
      current effort.
    - Quota may resolve an otherwise-undecided turn downward, never below a
      floor and never on a turn the evidence calls demanding.
    """
    t = cfg.thresholds
    rungs = list(ladder)
    plan = TurnPlan(
        action=KEEP,
        from_effort=current,
        to_effort=current,
        base_effort=base,
        recommendation=current,
        floor=floor,
        ladder=rungs,
        needed_confirmations=cfg.downgrade_confirmations if cfg.hysteresis else 1,
        thresholds=t.model_dump(),
        evidence=evidence.to_facts(),
        quota_pressure=max(0.0, min(1.0, pressure)),
    )
    if len(rungs) < 2:
        plan.reason = "this profile has no second rung to move to"
        return plan

    at = _index(rungs, current)
    if at is None:
        plan.reason = f"the current effort {current!r} is not on this profile's ladder"
        return plan

    if evidence.error:
        plan.reason = f"keeping {current}: {evidence.error}"
        return plan
    if evidence.missing:
        plan.reason = (
            f"keeping {current}: no usable answer for "
            f"{', '.join(evidence.missing)}"
        )
        return plan

    bottom = max(_index(rungs, floor) or 0, 0) if floor in rungs else 0

    up, down = _wants_more(evidence, t), _wants_less(evidence, t)
    if up:
        want, direction, why = min(at + 1, len(rungs) - 1), "up", up
    elif down:
        want, direction, why = max(at - 1, bottom), "down", down
    elif plan.quota_pressure >= t.quota_pressure_at and not up:
        # Nothing about the turn argues either way, and the provider is
        # scarce. Pressure resolves the hold downward and no further: it
        # cannot cross the floor, and it never reaches a turn `_wants_more`
        # already spoke for.
        want, direction = max(at - 1, bottom), "down"
        why = (
            f"nothing in this turn asks for more, and {plan.quota_pressure:.2f} "
            f"quota pressure is at or above {t.quota_pressure_at:g}"
        )
    else:
        plan.reason = f"keeping {current}: the evidence asks for no change"
        return plan

    want = max(want, bottom)
    plan.recommendation = rungs[want]
    plan.direction = direction if rungs[want] != current else "hold"

    if rungs[want] == current:
        floored = " (the floor is already here)" if want == bottom else ""
        plan.reason = f"keeping {current}: {why}{floored}"
        return plan

    if direction == "down" and cfg.hysteresis:
        seen = [*recommendations, plan.recommendation]
        plan.confirmations = confirmations_for(seen, plan.recommendation)
        if plan.confirmations < cfg.downgrade_confirmations:
            plan.reason = (
                f"keeping {current}: {why}, and {rungs[want]} has been asked for "
                f"{plan.confirmations} of {cfg.downgrade_confirmations} "
                "consecutive turns"
            )
            return plan
    elif direction == "down":
        plan.confirmations = 1

    plan.action = CHANGE
    plan.to_effort = rungs[want]
    plan.reason = f"{current} -> {rungs[want]}: {why}"
    return plan


def blocked(plan: TurnPlan, reason: str) -> TurnPlan:
    """Stop a plan without losing what it was going to say.

    A blocked turn keeps the model, the limits and the current effort exactly
    as they are, and the ledger keeps every update already applied.
    """
    plan.action = BLOCKED
    plan.to_effort = plan.from_effort
    plan.reason = reason
    return plan


# --- the ledger ----------------------------------------------------------


def open_change(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest effort change nobody can call settled, if there is one.

    Only a change blocks. A `keep` plan moved nothing, so whatever happened to
    the request that carried it, no transition is in the air.
    """
    for row in sorted(rows, key=lambda r: r.get("sequence") or 0, reverse=True):
        if row.get("action") != CHANGE:
            continue
        if row.get("status") in OPEN:
            return row
    return None


def advance(status: str, event: str) -> str:
    """What a ledger row's status becomes when something happens to it.

    The transitions of spec 10.6, and nothing else. A response field reporting
    the base effort is not an event here, because it is not evidence about the
    effective one.

    Events:
      `accepted`  upstream returned 2xx headers
      `completed` a valid successful provider completion carried the update
      `rejected`  a definitive refusal before acceptance
      `unknown`   submitted, and the outcome cannot be stated
    """
    if status not in STATUSES:
        return status
    if status in (CONFIRMED, REJECTED):
        # Terminal. A confirmed transition is not un-confirmed by a later
        # transport problem, and a rejected one is retried as a new attempt.
        return status
    if event == "accepted":
        return ACCEPTED if status == PLANNED else status
    if event == "completed":
        # Only an accepted submission can be confirmed. A plan that was never
        # sent cannot be completed by anything.
        return CONFIRMED if status == ACCEPTED else status
    if event == "rejected":
        # Before acceptance only. Once the provider has the request, a
        # refusal downstream is ambiguous, not definitive.
        return REJECTED if status == PLANNED else OUTCOME_UNKNOWN
    if event == "unknown":
        return OUTCOME_UNKNOWN
    return status


def effective_after(plan_row: dict[str, Any], confirmed: str | None) -> str | None:
    """The confirmed effective effort after this row, or what it already was.

    Expected and confirmed are tracked apart on purpose. A plan that was
    accepted but never completed leaves the confirmed value where it was, so
    an ambiguous attempt cannot quietly become a fact.
    """
    if plan_row.get("status") != CONFIRMED or plan_row.get("action") != CHANGE:
        return confirmed
    return plan_row.get("to_effort") or confirmed


def recommendation_history(rows: Sequence[dict[str, Any]]) -> list[str | None]:
    """The recommendation each eligible turn made, oldest first.

    Shadow rows count: the point of shadow is to measure the policy the active
    mode would run, and a downgrade's confirmations are part of that policy.
    """
    return [
        row.get("recommendation")
        for row in sorted(rows, key=lambda r: r.get("sequence") or 0)
    ]
