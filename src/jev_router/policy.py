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

from dataclasses import dataclass, field
from typing import Any, Callable

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
    return {k: float(v) for k, v in pressures.items() if v}


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
        config, alias_cfg, use, rule_name, reason, features, pressures={}, protected=protected
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
    allowed = _allowed_models(config, alias_cfg, features, notes)
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
    if model not in allowed:
        replacement = _first_capable(config, allowed, features)
        if replacement:
            notes.append(f"{model} is not allowed here, using {replacement}")
            model = replacement
        else:
            notes.append(f"no allowed model fits, keeping {model}")

    capable = capable_models(config, allowed, features)
    if model not in capable and capable:
        notes.append(f"{model} cannot serve this request, using {capable[0]}")
        model = capable[0]

    order = config.settings.effort_order
    cap = alias_cfg.max_effort
    if cap and effort and _rank(order, effort) > _rank(order, cap):
        notes.append(f"effort capped at {cap} by the alias")
        effort = cap

    client_cfg = config.clients.get(features.client) if features.client else None
    if client_cfg and client_cfg.min_effort:
        floor = client_cfg.min_effort
        if effort is None or _rank(order, effort) < _rank(order, floor):
            notes.append(f"client {features.client} floors effort at {floor}")
            effort = floor

    return model, clamp_effort(config, model, effort)


# --- routes -------------------------------------------------------------


def resolve_use(config: RouterConfig, route: Route) -> tuple[list[RouteEntry], str | None]:
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
    pressure = max(0.0, min(1.0, float(pressures.get(provider, 0.0) or 0.0)))
    return max(0.0, float(max_shift)) * pressure, provider, pressure


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
        if not points or not isinstance(operand, (int, float)) or isinstance(operand, bool):
            return operand
        effective = operand + points if op in ("gte", "conf_gte") else operand - points
        if shifts is not None:
            shifts.append(
                Shift(
                    rule=rule_name,
                    question=question,
                    op=op,
                    base=float(operand),
                    effective=float(effective),
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
            bits.append(f"{key}={ans.get('score', 0):.2f}@{ans.get('confidence', 0):.2f}")
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
    allowed = list(alias_cfg.allowed_models or config.models.keys())
    client_cfg = config.clients.get(features.client) if features.client else None
    if client_cfg and client_cfg.allowed_models:
        narrowed = [m for m in allowed if m in client_cfg.allowed_models]
        if narrowed:
            allowed = narrowed
        else:
            notes.append(f"client {features.client} allows no model of this alias")
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


def _first_capable(config: RouterConfig, allowed: list[str], features: Features) -> str | None:
    capable = capable_models(config, allowed, features)
    if capable:
        return capable[0]
    return allowed[0] if allowed else None


def next_model_that_fits(
    config: RouterConfig,
    current: str,
    features: Features,
    allowed: list[str] | None = None,
) -> str | None:
    """The smallest model that is roomier than `current` and can serve this request."""
    pool = allowed or list(config.models)
    current_cfg = config.models.get(current)
    floor = current_cfg.context_window if current_cfg else 0
    candidates = [
        m
        for m in pool
        if m != current and config.models[m].context_window > floor and fits(config, m, features)
    ]
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
