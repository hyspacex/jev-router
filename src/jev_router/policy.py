"""Turn Jev answers plus request features into a model and an effort.

Pure functions only: no network, no clock, no database. The order is

    low confidence gate -> first matching rule -> default
    -> alias caps -> client floors -> capability filter -> effort clamp

Quota pressure enters here as data: a mapping of provider name to a number in
[0, 1] that somebody else measured. Given the same answers, the same features
and the same pressures, this module always returns the same route. That is
what lets `evals/tune.py` replay a decision taken under pressure honestly, and
what keeps the clock and the subprocess out of the routing code.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import AliasCfg, Lane, Route, RouteEntry, RouterConfig, Ruleset
from .features import Features

# Condition keys that read the request instead of a Jev answer.
# Adding one is a single entry here; rules can use it straight away.
FEATURE_CONDITIONS: dict[str, Callable[[Features, Any], bool]] = {
    "has_tools": lambda f, v: f.has_tools is bool(v),
    "has_images": lambda f, v: f.has_images is bool(v),
    "has_code": lambda f, v: f.has_code is bool(v),
    "stream": lambda f, v: f.stream is bool(v),
    "est_tokens_gte": lambda f, v: f.est_tokens >= v,
    "est_tokens_lte": lambda f, v: f.est_tokens <= v,
    "message_count_gte": lambda f, v: f.message_count >= v,
    "message_count_lte": lambda f, v: f.message_count <= v,
    "max_tokens_gte": lambda f, v: f.max_tokens >= v,
    "client": lambda f, v: f.client == v,
    "client_in": lambda f, v: f.client in v,
    "languages_include": lambda f, v: bool(set(f.languages) & set(v)),
    "tool_names_include": lambda f, v: bool(set(f.tool_names) & set(v)),
}


class RoutingError(Exception):
    """The current request has no route satisfying its hard constraints."""


@dataclass(frozen=True)
class PlanEntry:
    """One rung the forwarder may try, after caps, floors and clamping."""

    model: str
    effort: str | None
    provider: str
    equivalent: bool = False

    def as_pair(self) -> tuple[str, str | None]:
        return (self.model, self.effort)


@dataclass(frozen=True)
class Shift:
    """A threshold that quota pressure moved, and by how much."""

    rule: str
    question: str
    op: str
    base: float
    effective: float
    provider: str
    pressure: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "question": self.question,
            "op": self.op,
            "base": round(self.base, 4),
            "effective": round(self.effective, 4),
            "provider": self.provider,
            "pressure": round(self.pressure, 4),
        }


@dataclass
class PolicyResult:
    model: str
    effort: str | None
    rule: str
    reason: str
    notes: list[str] = field(default_factory=list)
    route: str | None = None
    # The primary first, then the fallbacks that can serve this request.
    plan: list[PlanEntry] = field(default_factory=list)
    shifts: list[Shift] = field(default_factory=list)
    reordered: bool = False
    protected: bool = False
    pressure_changed_the_outcome: bool = False


def evaluate(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    answers: dict[str, dict[str, Any]],
    features: Features,
    pressures: dict[str, float] | None = None,
) -> PolicyResult:
    """Choose a route. `pressures` is quota pressure per provider, or nothing."""
    ruleset = config.ruleset_for(alias_cfg)
    live = _live_pressures(config, pressures)
    shifts: list[Shift] = []
    use, rule_name, reason, protected = _pick_route(
        config, ruleset, answers, features, live, shifts
    )
    result = finalize(
        config,
        alias_cfg,
        use,
        rule_name,
        reason,
        features,
        pressures=live,
        protected=protected,
    )
    result.shifts = shifts
    if live:
        result.pressure_changed_the_outcome = _differs_without_pressure(
            config, alias_cfg, ruleset, answers, features, result
        )
    return result


def _live_pressures(
    config: RouterConfig, pressures: dict[str, float] | None
) -> dict[str, float]:
    """Drop the zeros, and drop everything when quota routing is switched off."""
    if not pressures or not config.quota_policy.enabled:
        return {}
    live = {}
    for key, value in pressures.items():
        try:
            pressure = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 < pressure <= 1:
            live[key] = pressure
    return live


def _differs_without_pressure(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    ruleset: Ruleset,
    answers: dict[str, dict[str, Any]],
    features: Features,
    result: PolicyResult,
) -> bool:
    """Would the same request have gone somewhere else at pressure zero?"""
    use, rule_name, reason, protected = _pick_route(
        config, ruleset, answers, features, {}, []
    )
    calm = finalize(
        config,
        alias_cfg,
        use,
        rule_name,
        reason,
        features,
        pressures={},
        protected=protected,
    )
    return (calm.model, calm.effort, [e.as_pair() for e in calm.plan]) != (
        result.model,
        result.effort,
        [e.as_pair() for e in result.plan],
    )


def finalize(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    route: Route,
    rule_name: str,
    reason: str,
    features: Features,
    pressures: dict[str, float] | None = None,
    protected: bool = False,
) -> PolicyResult:
    """Apply the parts a rule may not override: caps, floors, capabilities."""
    live = _live_pressures(config, pressures)
    entries, route_name = resolve_use(config, route)
    reordered = False
    if route_name and live:
        entries, reordered = order_entries(config, entries, live)

    notes: list[str] = []
    allowed = eligible_models(config, alias_cfg, features)
    model, effort = _apply_guards(
        config, alias_cfg, entries[0].model, entries[0].effort, features, allowed, notes
    )

    plan = [
        PlanEntry(
            model=model,
            effort=effort,
            provider=config.provider_of(model),
            equivalent=entries[0].equivalent,
        )
    ]
    seen = {(model, effort)}
    for entry in entries[1:]:
        if entry.model not in allowed or not fits(config, entry.model, features):
            continue
        alt_model, alt_effort = _apply_guards(
            config, alias_cfg, entry.model, entry.effort, features, allowed, []
        )
        if (alt_model, alt_effort) in seen:
            continue
        seen.add((alt_model, alt_effort))
        plan.append(
            PlanEntry(
                model=alt_model,
                effort=alt_effort,
                provider=config.provider_of(alt_model),
                equivalent=entry.equivalent,
            )
        )

    return PolicyResult(
        model=model,
        effort=effort,
        rule=rule_name,
        reason=reason,
        notes=notes,
        route=route_name,
        plan=plan,
        reordered=reordered,
        protected=protected,
    )


def _apply_guards(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    model: str,
    effort: str | None,
    features: Features,
    allowed: list[str],
    notes: list[str],
) -> tuple[str, str | None]:
    eligible = eligible_models(config, alias_cfg, features)
    if model not in eligible:
        replacement = next((m for m in allowed if m in eligible), None)
        if replacement is None:
            raise RoutingError("no permitted model can serve this request")
        notes.append(
            f"{model} cannot serve this request or is not allowed, using {replacement}"
        )
        model = replacement
    adjusted = constrained_effort(config, alias_cfg, features, model, effort)
    if adjusted != effort:
        notes.append("effort capped or floored onto the permitted model ladder")
    return model, adjusted


def constrained_effort(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    features: Features,
    model: str,
    effort: str | None,
) -> str | None:
    """Intersect the ladder with hard caps/floors before choosing a level."""
    mcfg = config.models[model]
    client = config.clients.get(features.client)
    floor = client.min_effort if client else None
    cap = alias_cfg.max_effort
    order = config.settings.effort_order
    if floor and cap and _rank(order, floor) > _rank(order, cap):
        raise RoutingError("client minimum effort exceeds alias maximum effort")
    if not mcfg.efforts or mcfg.effort_style == "none":
        if floor and _rank(order, floor) > _rank(order, "none"):
            raise RoutingError("model cannot satisfy the required effort floor")
        return None
    levels = [
        e
        for e in mcfg.efforts
        if (not floor or _rank(order, e) >= _rank(order, floor))
        and (not cap or _rank(order, e) <= _rank(order, cap))
    ]
    if not levels:
        raise RoutingError("model has no effort satisfying the alias/client bounds")
    want = effort or mcfg.default_effort or floor
    if want is None and cap is None:
        return None
    # With a cap, send an explicit effort rather than an uncontrolled default.
    want = want or cap or levels[0]
    lower = [e for e in levels if _rank(order, e) <= _rank(order, want)]
    return (
        max(lower, key=lambda e: _rank(order, e))
        if lower
        else min(levels, key=lambda e: _rank(order, e))
    )


def eligible_models(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    features: Features,
) -> list[str]:
    """Mandatory pure eligibility, deliberately independent of quota/health."""
    allowed = _allowed_models(config, alias_cfg, features, [])
    eligible = []
    for model in allowed:
        if not fits(config, model, features):
            continue
        try:
            constrained_effort(config, alias_cfg, features, model, None)
        except RoutingError:
            continue
        eligible.append(model)
    if not eligible:
        raise RoutingError(
            "no model satisfies permitted models, capabilities, context and effort bounds"
        )
    return eligible


def guard_candidate(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    features: Features,
    model: str,
    effort: str | None,
) -> tuple[str, str | None]:
    return _apply_guards(
        config,
        alias_cfg,
        model,
        effort,
        features,
        eligible_models(config, alias_cfg, features),
        [],
    )


def validate_entry(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    features: Features,
    entry: PlanEntry,
) -> None:
    """Last check before I/O. Never silently execute a different entry."""
    if entry.model not in eligible_models(config, alias_cfg, features):
        raise RoutingError("execution entry is not eligible for this request")
    if entry.effort != constrained_effort(
        config, alias_cfg, features, entry.model, entry.effort
    ):
        raise RoutingError("execution effort violates the current constraints")


# --- routes -------------------------------------------------------------


def resolve_use(
    config: RouterConfig, route: Route
) -> tuple[list[RouteEntry], str | None]:
    """A rule's `use:` as an ordered list of entries, plus the route's name.

    `{model, effort}` is one entry and no name, which is what every rule was
    before routes existed. `{route: n}` is that lane's primary and fallbacks.
    """
    if route.route is not None:
        lane: Lane | None = config.routes.get(route.route)
        if lane is not None:
            return lane.entries(), route.route
        # An unknown route cannot happen in a validated config. Fail neutral.
        return [RouteEntry(model=config.settings.default_route.model or "")], None
    return [RouteEntry(model=route.model or "", effort=route.effort)], None


def order_entries(
    config: RouterConfig, entries: list[RouteEntry], pressures: dict[str, float]
) -> tuple[list[RouteEntry], bool]:
    """Move entries under quota pressure behind the healthy ones.

    Two guards. An entry only moves ahead of the primary when it is marked
    `equivalent: true`, so a weaker fallback is never promoted to save quota.
    And entries keep their order among themselves, so the list only ever
    splits into "not pressured" then "pressured".
    """
    if len(entries) < 2:
        return entries, False
    limit = config.quota_policy.demote_above

    def hot(entry: RouteEntry) -> bool:
        return pressures.get(config.provider_of(entry.model), 0.0) > limit

    order = [e for e in entries if not hot(e)] + [e for e in entries if hot(e)]
    primary = entries[0]
    if hot(primary):
        at = order.index(primary)
        promoted = order[:at]
        may_pass = [e for e in promoted if e.equivalent]
        pushed_back = [e for e in promoted if not e.equivalent]
        order = may_pass + [primary] + pushed_back + order[at + 1 :]
    return order, order != entries


# --- rule matching ------------------------------------------------------


def _pick_route(
    config: RouterConfig,
    ruleset: Ruleset,
    answers: dict[str, dict[str, Any]],
    features: Features,
    pressures: dict[str, float],
    shifts: list[Shift],
) -> tuple[Route, str, str, bool]:
    lc = ruleset.low_confidence
    if lc and lc.use and lc.questions:
        for qid in lc.questions:
            ans = answers.get(qid)
            if ans is None:
                continue
            conf = ans.get("confidence")
            if conf is not None and conf < lc.min_confidence:
                return (
                    lc.use,
                    "low_confidence",
                    f"{qid} confidence {conf:.2f} below {lc.min_confidence}",
                    True,
                )

    for rule in ruleset.rules:
        # A protected rule exists because the request needs it, so quota never
        # makes it harder to reach.
        seen: list[Shift] = []
        applied = {} if rule.protected else pressures
        if matches(rule.when, answers, features, applied, seen, rule.name):
            shifts.extend(seen)
            return rule.use, rule.name, _describe(rule.when, answers), rule.protected
        if seen and matches(rule.when, answers, features, {}, None, rule.name):
            # The shift is the only reason this rule did not fire. That is the
            # one worth logging, so record it and carry on down the list.
            shifts.extend(seen)

    default = ruleset.default or config.settings.default_route
    return default, "default", "no rule matched", True


def matches(
    when: dict[str, Any],
    answers: dict[str, dict[str, Any]],
    features: Features,
    pressures: dict[str, float] | None = None,
    shifts: list[Shift] | None = None,
    rule_name: str = "",
) -> bool:
    for key, cond in when.items():
        feature_check = FEATURE_CONDITIONS.get(key)
        if feature_check is not None:
            if not feature_check(features, cond):
                return False
            continue
        answer = answers.get(key)
        if answer is None or not _answer_matches(
            cond, answer, pressures or {}, shifts, rule_name, key
        ):
            return False
    return True


def shift_amount(cond: Any, pressures: dict[str, float]) -> tuple[float, str, float]:
    """How far pressure moves this condition's threshold.

    Returns (points, provider, pressure). Bounded by `max_shift`, monotone in
    pressure, and zero whenever the condition says nothing about pressure.
    """
    if not isinstance(cond, dict):
        return 0.0, "", 0.0
    provider = cond.get("shift_with")
    max_shift = cond.get("max_shift")
    if not isinstance(provider, str) or not isinstance(max_shift, (int, float)):
        return 0.0, "", 0.0
    if isinstance(max_shift, bool):
        return 0.0, "", 0.0
    try:
        pressure = max(0.0, min(1.0, float(pressures.get(provider, 0.0) or 0.0)))
    except (TypeError, ValueError, OverflowError):
        pressure = 0.0
    return max(0.0, max_shift) * pressure, provider, pressure


def _answer_matches(
    cond: dict[str, Any],
    answer: dict[str, Any],
    pressures: dict[str, float] | None = None,
    shifts: list[Shift] | None = None,
    rule_name: str = "",
    question: str = "",
) -> bool:
    atype = answer.get("type")
    if atype == "choice":
        value: Any = answer.get("choice")
    elif atype == "score":
        value = answer.get("score")
    elif atype == "noul":
        value = answer.get("noul")
    else:
        return False

    points, provider, pressure = shift_amount(cond, pressures or {})

    def moved(op: str, operand: Any) -> Any:
        """A shift only ever makes the rule harder to reach.

        `gte` and `conf_gte` fire on high values, so the cutoff goes up. `lte`
        fires on low values, so the cutoff comes down.
        """
        if (
            not points
            or not isinstance(operand, (int, float))
            or isinstance(operand, bool)
        ):
            return operand
        effective = operand + points if op in ("gte", "conf_gte") else operand - points
        if shifts is not None:
            shifts.append(
                Shift(
                    rule=rule_name,
                    question=question,
                    op=op,
                    base=operand,
                    effective=effective,
                    provider=provider,
                    pressure=pressure,
                )
            )
        return effective

    conf = answer.get("confidence")
    for op, operand in cond.items():
        if op == "in" and value not in operand:
            return False
        if op == "not_in" and value in operand:
            return False
        if op == "eq" and value != operand:
            return False
        if op == "gte" and not (value is not None and value >= moved(op, operand)):
            return False
        if op == "lte" and not (value is not None and value <= moved(op, operand)):
            return False
        if op == "conf_gte" and not (conf is not None and conf >= moved(op, operand)):
            return False
    return True


def _describe(when: dict[str, Any], answers: dict[str, dict[str, Any]]) -> str:
    bits = []
    for key in when:
        ans = answers.get(key)
        if not ans:
            bits.append(key)
        elif ans.get("type") == "choice":
            bits.append(f"{key}={ans.get('choice')}@{ans.get('confidence', 0):.2f}")
        elif ans.get("type") == "score":
            bits.append(
                f"{key}={ans.get('score', 0):.2f}@{ans.get('confidence', 0):.2f}"
            )
        elif ans.get("type") == "noul":
            bits.append(f"{key}={ans.get('noul', 0):.2f}")
    return ", ".join(bits)


# --- capabilities and efforts -------------------------------------------


def _allowed_models(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    features: Features,
    notes: list[str],
) -> list[str]:
    allowed = list(
        config.models if alias_cfg.allowed_models is None else alias_cfg.allowed_models
    )
    client_cfg = config.clients.get(features.client) if features.client else None
    if client_cfg and client_cfg.allowed_models is not None:
        allowed = [m for m in allowed if m in client_cfg.allowed_models]
    if not allowed:
        raise RoutingError("alias and client permit no common model")
    return allowed


def fits(config: RouterConfig, model_id: str, features: Features) -> bool:
    mcfg = config.models.get(model_id)
    if mcfg is None:
        return False
    if features.has_tools and not mcfg.supports_tools:
        return False
    if features.has_images and not mcfg.supports_vision:
        return False
    return features.budget_tokens <= mcfg.context_window


def capable_models(
    config: RouterConfig, allowed: list[str], features: Features
) -> list[str]:
    return [m for m in allowed if fits(config, m, features)]


def next_model_that_fits(
    config: RouterConfig,
    current: str,
    features: Features,
    allowed: list[str] | None = None,
) -> str | None:
    """The smallest eligible replacement for an incompatible current model.

    Capability changes may require a smaller window, unlike context overflow.
    """
    pool = list(config.models) if allowed is None else allowed
    if current in pool and fits(config, current, features):
        return None
    candidates = [m for m in pool if m != current and fits(config, m, features)]
    if not candidates:
        return None
    return min(candidates, key=lambda m: config.models[m].context_window)


def _rank(order: list[str], effort: str) -> int:
    try:
        return order.index(effort)
    except ValueError:
        return len(order)


def clamp_effort(config: RouterConfig, model_id: str, effort: str | None) -> str | None:
    """Move an effort onto the ladder the model actually supports.

    A model with a `default_effort` never comes out of here bare. Some models
    think without limit when they are sent with no effort at all, spend their
    whole token budget on it and return nothing, so the config can say what
    "no opinion" means for that model.
    """
    mcfg = config.models.get(model_id)
    if mcfg is None or mcfg.effort_style == "none" or not mcfg.efforts:
        return None
    if effort is None:
        return mcfg.default_effort
    if effort in mcfg.efforts:
        return effort
    order = config.settings.effort_order
    want = _rank(order, effort)
    lower = [e for e in mcfg.efforts if _rank(order, e) <= want]
    if lower:
        return max(lower, key=lambda e: _rank(order, e))
    return min(mcfg.efforts, key=lambda e: _rank(order, e))


# --- the context budget --------------------------------------------------

# The floor under the uncertainty reserve, and its share of the estimated
# input above that floor. Engineering heuristics, not a measured error bound.
MIN_RESERVE_TOKENS = 4096
RESERVE_SHARE = 0.10
# How far from the ceiling an estimate with unknown parts has to stay. An
# image or a provider-held history we could not measure may not be admitted
# right on the boundary.
UNKNOWN_MARGIN = 0.05


@dataclass(frozen=True)
class BudgetCheck:
    """Does `I + O + S` fit inside `C` for a shared-window profile?

    Pure arithmetic over numbers somebody else measured. Quota never appears
    here: capacity pressure may change which profile is chosen, never whether
    a request fits the one that was.
    """

    fits: bool
    reason: str
    context_window: int
    input_tokens: int
    output_tokens: int
    reserve_tokens: int
    headroom: int
    estimate_method: str = ""
    unknown: tuple[str, ...] = ()

    def to_facts(self) -> dict[str, Any]:
        return {
            "context_window": self.context_window,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reserve_tokens": self.reserve_tokens,
            "headroom": self.headroom,
            "estimate_method": self.estimate_method,
            "unknown_parts": list(self.unknown),
        }


def _whole(value: Any) -> int | None:
    """A count, or nothing. Rejects NaN, infinities, booleans and text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    number = int(value)
    if number != value or number < 0:
        return None
    return number


def reserve_tokens(input_tokens: int, requested: int = 0) -> int:
    """`max(4096, ceil(0.10 * I))`, and never less than the client asked for."""
    return max(MIN_RESERVE_TOKENS, math.ceil(RESERVE_SHARE * input_tokens), requested)


def context_budget(
    *,
    context_window: Any,
    input_tokens: Any,
    output_tokens: Any,
    requested_reserve: Any = 0,
    estimate_method: str = "",
    unknown: tuple[str, ...] = (),
) -> BudgetCheck:
    """The admission check of spec 6.4, as one pure function."""
    numbers = {
        "context_window": _whole(context_window),
        "input_tokens": _whole(input_tokens),
        "output_tokens": _whole(output_tokens),
        "requested_reserve": _whole(requested_reserve),
    }
    bad = sorted(name for name, value in numbers.items() if value is None)
    if bad:
        return BudgetCheck(
            fits=False,
            reason=f"{', '.join(bad)} must be a whole number of zero or more",
            context_window=numbers["context_window"] or 0,
            input_tokens=numbers["input_tokens"] or 0,
            output_tokens=numbers["output_tokens"] or 0,
            reserve_tokens=0,
            headroom=0,
            estimate_method=estimate_method,
            unknown=tuple(unknown),
        )

    window = numbers["context_window"] or 0
    inputs = numbers["input_tokens"] or 0
    output = numbers["output_tokens"] or 0
    reserve = reserve_tokens(inputs, numbers["requested_reserve"] or 0)
    needed = inputs + output + reserve
    ceiling = window
    if unknown:
        ceiling = int(window * (1 - UNKNOWN_MARGIN))
    fits = needed <= ceiling
    if fits:
        reason = "input, output and reserve fit the negotiated window"
    elif unknown and needed <= window:
        reason = (
            f"{', '.join(unknown)} of unknown size, and {needed} tokens is within "
            f"{UNKNOWN_MARGIN:.0%} of the {window} token window"
        )
    else:
        reason = (
            f"{inputs} input + {output} output + {reserve} reserve = {needed} tokens "
            f"exceeds the negotiated window of {window}"
        )
    return BudgetCheck(
        fits=fits,
        reason=reason,
        context_window=window,
        input_tokens=inputs,
        output_tokens=output,
        reserve_tokens=reserve,
        headroom=ceiling - needed,
        estimate_method=estimate_method,
        unknown=tuple(unknown),
    )


def apply_effort(
    config: RouterConfig, model_id: str, effort: str | None
) -> tuple[str, dict[str, Any]]:
    """Return the upstream model name and any extra body fields."""
    mcfg = config.models[model_id]
    if effort is None:
        # The last guard: no path may send a model that declared a default
        # effort without one.
        effort = mcfg.default_effort
    if effort is None or mcfg.effort_style == "none":
        return mcfg.upstream_id, {}
    if mcfg.effort_style == "suffix":
        return f"{mcfg.upstream_id}({effort})", {}
    return mcfg.upstream_id, {"reasoning_effort": effort}
