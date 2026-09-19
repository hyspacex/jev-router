"""Turn Jev answers plus request features into a model and an effort.

Pure functions only: no network, no clock, no database. The order is

    low confidence gate -> first matching rule -> default
    -> alias caps -> client floors -> capability filter -> effort clamp
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .config import AliasCfg, Route, RouterConfig, Ruleset
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


@dataclass
class PolicyResult:
    model: str
    effort: str | None
    rule: str
    reason: str
    notes: list[str] = field(default_factory=list)


def evaluate(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    answers: dict[str, dict[str, Any]],
    features: Features,
) -> PolicyResult:
    ruleset = config.ruleset_for(alias_cfg)
    route, rule_name, reason = _pick_route(config, ruleset, answers, features)
    return finalize(config, alias_cfg, route, rule_name, reason, features)


def finalize(
    config: RouterConfig,
    alias_cfg: AliasCfg,
    route: Route,
    rule_name: str,
    reason: str,
    features: Features,
) -> PolicyResult:
    """Apply the parts a rule may not override: caps, floors, capabilities."""
    notes: list[str] = []
    model = route.model
    effort = route.effort

    allowed = _allowed_models(config, alias_cfg, features, notes)
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

    effort = clamp_effort(config, model, effort)
    return PolicyResult(model=model, effort=effort, rule=rule_name, reason=reason, notes=notes)


# --- rule matching ------------------------------------------------------


def _pick_route(
    config: RouterConfig,
    ruleset: Ruleset,
    answers: dict[str, dict[str, Any]],
    features: Features,
) -> tuple[Route, str, str]:
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
                )

    for rule in ruleset.rules:
        if matches(rule.when, answers, features):
            return rule.use, rule.name, _describe(rule.when, answers)

    default = ruleset.default or config.settings.default_route
    return default, "default", "no rule matched"


def matches(
    when: dict[str, Any],
    answers: dict[str, dict[str, Any]],
    features: Features,
) -> bool:
    for key, cond in when.items():
        feature_check = FEATURE_CONDITIONS.get(key)
        if feature_check is not None:
            if not feature_check(features, cond):
                return False
            continue
        answer = answers.get(key)
        if answer is None or not _answer_matches(cond, answer):
            return False
    return True


def _answer_matches(cond: dict[str, Any], answer: dict[str, Any]) -> bool:
    atype = answer.get("type")
    if atype == "choice":
        value: Any = answer.get("choice")
    elif atype == "score":
        value = answer.get("score")
    elif atype == "noul":
        value = answer.get("noul")
    else:
        return False

    conf = answer.get("confidence")
    for op, operand in cond.items():
        if op == "in" and value not in operand:
            return False
        if op == "not_in" and value in operand:
            return False
        if op == "eq" and value != operand:
            return False
        if op == "gte" and not (value is not None and value >= operand):
            return False
        if op == "lte" and not (value is not None and value <= operand):
            return False
        if op == "conf_gte" and not (conf is not None and conf >= operand):
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
    """Move an effort onto the ladder the model actually supports."""
    mcfg = config.models.get(model_id)
    if mcfg is None or mcfg.effort_style == "none" or not mcfg.efforts:
        return None
    if effort is None:
        return None
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
    if effort is None or mcfg.effort_style == "none":
        return mcfg.upstream_id, {}
    if mcfg.effort_style == "suffix":
        return f"{mcfg.upstream_id}({effort})", {}
    return mcfg.upstream_id, {"reasoning_effort": effort}
