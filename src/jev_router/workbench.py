"""The Rules & Config tab's backend.

Read the running config, replay a draft against recent admissions, write a
draft to router.yaml and swap it in, ask Jev about one stored decision again,
and save a decision as a golden eval case.

None of this lives under `/router/audit`, because three of these endpoints
write a file or spend a Jev call. All of them sit behind the same
`x-router-admin-token` ingress credential as every other `/router/*` route.
None of them writes a decision row, a session or a pin.

The preview is a pure replay: the stored answers, request facts and pressure
of each admission run through `policy.trace` under two configs. No Jev call,
no quota poll, no upstream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import (
    ConfigError,
    RouterConfig,
    check_registry_names,
    config_from_text,
    config_text_hash,
)
from .dashboard import (
    HEADERS,
    WINDOWS,
    _is_number,
    _label,
    knife_edge_of,
    knife_edge_rate,
    project_facts,
    quota_costs,
    quota_saved,
    verdicts_by_admission,
)
from .pins import VERDICTS
from .policy import FEATURE_CONDITIONS, trace
from .semantic import (
    ADMISSION_FALLBACK,
    _pressures,
    active_questions,
    features_from_facts,
    replay_decision,
)
from .sessions import EVENT_EFFORT_PLAN

try:  # The text-capture work adds this module; until then a local fallback.
    from .scrub import scrub_state as _scrub_state  # type: ignore[import-not-found]
    from .scrub import scrub_text as _scrub_text  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the merged tree
    _scrub_state = None
    _scrub_text = None

log = logging.getLogger("jev_router.workbench")

PREVIEW_LIMIT = 200
PREVIEW_MAX = 500
DRAFT_MAX_CHARS = 1_000_000
NOTE_MAX_CHARS = 500
DEFAULT_WINDOW = "7d"
BACKUP_DIR = "config-backups"
GOLDEN_FILE = "golden_cases.jsonl"


def _json(body: Any, status: int = 200) -> Response:
    return JSONResponse(body, status_code=status, headers=HEADERS)


def _error(message: str, status: int = 400, **extra: Any) -> Response:
    return _json({"error": message, **extra}, status)


async def _body(request: Request) -> dict[str, Any] | Response:
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return _error("body must be JSON")
    if not isinstance(payload, dict):
        return _error("body must be an object")
    return payload


def _since(window: str, now: float | None = None) -> float:
    return (time.time() if now is None else now) - WINDOWS[window][0]


# --- the YAML text, and where things are in it ------------------------------


def _compose(text: str) -> Any:
    try:
        return yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None


def _child(node: Any, key: Any) -> tuple[Any, Any] | None:
    """(key node, value node) of `key` under a mapping, or of an index."""
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == str(key):
                return k, v
    if isinstance(node, yaml.SequenceNode):
        try:
            index = int(key)
        except (TypeError, ValueError):
            # Problems name a rule by its name: `policy.rules[hard].use`.
            for item in node.value:
                named = _child(item, "name") if isinstance(item, yaml.MappingNode) else None
                if named is not None and getattr(named[1], "value", None) == str(key):
                    return item, item
            return None
        if 0 <= index < len(node.value):
            item = node.value[index]
            return item, item
    return None


def node_line(root: Any, path: list[Any], value: bool = False) -> int | None:
    """The 1-based line of the deepest part of `path` the YAML has.

    A part the YAML does not have is skipped, so a pydantic location that
    names a union member (`Ruleset`, `str`) still lands near its node.
    """
    line = None
    node = root
    for part in path:
        found = _child(node, part)
        if found is None:
            continue
        key, node = found
        line = (node if value else key).start_mark.line + 1
    return line


_WHERE = re.compile(r"[A-Za-z0-9_.\-/\[\]]+")


def _path_of(where: str) -> list[Any]:
    """`policy.rules[3].use` -> ["policy", "rules", 3, "use"]."""
    out: list[Any] = []
    for part in re.split(r"\.|\[|\]", where):
        if part:
            out.append(int(part) if part.isdigit() else part)
    return out


def _problem(root: Any, text: str) -> dict[str, Any]:
    """One `where: what` problem line as {message, path, line}."""
    text = text.strip().lstrip("-").strip()
    where, _, _what = text.partition(": ")
    if _what and _WHERE.fullmatch(where):
        path = _path_of(where)
        return {"message": text, "path": where, "line": node_line(root, path)}
    return {"message": text, "path": None, "line": None}


def draft_errors(text: str, path: str = "") -> list[dict[str, Any]]:
    """Why a draft does not load, one entry per problem, with a line where
    the YAML can give one. Empty when it loads."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        return [{
            "message": f"invalid YAML: {problem}",
            "path": None,
            "line": mark.line + 1 if mark is not None else None,
        }]
    if not isinstance(raw, dict):
        return [{"message": "top level must be a mapping", "path": None, "line": 1}]
    root = _compose(text)
    try:
        cfg = RouterConfig.model_validate(raw)
    except ValidationError as exc:
        out = []
        for err in exc.errors():
            loc = list(err.get("loc") or ())
            message = str(err.get("msg") or "")
            if not loc:
                # The cross-field check reports every problem in one message.
                for line in message.replace("Value error, ", "").splitlines():
                    if line.strip():
                        out.append(_problem(root, line))
                continue
            where = ".".join(str(p) for p in loc)
            out.append({
                "message": f"{where}: {message}",
                "path": where,
                "line": node_line(root, loc),
            })
        return out
    cfg._config_path = path
    return [_problem(root, problem) for problem in check_registry_names(cfg)]


def validate_draft(text: Any, path: str = "") -> tuple[RouterConfig | None, list[dict[str, Any]]]:
    """Load a draft exactly as startup loads a file, registries included."""
    if not isinstance(text, str) or not text.strip():
        return None, [{"message": "yaml must be a non-empty string", "path": None, "line": None}]
    if len(text) > DRAFT_MAX_CHARS:
        return None, [{"message": "yaml is too long", "path": None, "line": None}]
    try:
        return config_from_text(text, "draft", check_registries=True, path=path), []
    except ConfigError as exc:
        errors = draft_errors(text, path)
        if not errors:
            errors = [{"message": line, "path": None, "line": None}
                      for line in str(exc).splitlines() if line.strip()]
        return None, errors


# --- GET /router/config -----------------------------------------------------


def running_text(config: RouterConfig) -> tuple[str | None, str | None]:
    """The exact text this process loaded, and where it came from.

    The in-memory text first, then the file when it still hashes the same.
    Never a stored snapshot: a snapshot is scrubbed and is never served.
    """
    text = getattr(config, "_config_text", "") or ""
    if text:
        return text, "loaded"
    if config.config_path:
        try:
            disk = Path(config.config_path).read_text()
        except OSError:
            return None, None
        if config_text_hash(disk) == config.config_hash:
            return disk, "file"
    return None, None


def _ruleset_name(alias_cfg: Any, alias: str) -> str:
    rules = alias_cfg.rules
    return rules if isinstance(rules, str) else f"alias:{alias}"


def _rule_entry(rule: Any, index: int, line: int | None) -> dict[str, Any]:
    return {
        "index": index,
        "name": rule.name,
        "lane": rule.lane or None,
        "protected": rule.protected,
        "when": rule.when,
        "use": rule.use.model_dump(mode="json", exclude_none=True),
        "line": line,
    }


def _share(hits: int, total: int) -> float | None:
    return hits / total if total else None


def describe_ruleset(
    config: RouterConfig,
    name: str,
    ruleset: Any,
    yaml_path: list[Any],
    root: Any,
    counts: dict[tuple[Any, Any], int],
) -> dict[str, Any]:
    """One ruleset, each rule with its line and its hits in the window."""
    aliases = [
        alias for alias, acfg in config.aliases.items() if _ruleset_name(acfg, alias) == name
    ]
    by_rule: dict[Any, int] = {}
    for (alias, rule), n in counts.items():
        if alias in aliases:
            by_rule[rule] = by_rule.get(rule, 0) + n
    total = sum(by_rule.values())

    def hits(rule: str) -> dict[str, Any]:
        n = by_rule.get(rule, 0)
        return {"hits": n, "share": _share(n, total)}

    rules = []
    for index, rule in enumerate(ruleset.rules, start=1):
        line = node_line(root, [*yaml_path, "rules", index - 1, "name"], value=True)
        rules.append({**_rule_entry(rule, index, line), **hits(rule.name)})
    gate = ruleset.low_confidence
    return {
        "name": name,
        "aliases": aliases,
        "admissions": total,
        "rules": rules,
        "low_confidence": (
            {**gate.model_dump(mode="json", exclude_none=True), **hits("low_confidence")}
            if gate is not None else None
        ),
        "default": (
            {"use": ruleset.default.model_dump(mode="json", exclude_none=True), **hits("default")}
            if ruleset.default is not None else None
        ),
        "fallback": hits(ADMISSION_FALLBACK),
    }


def config_thresholds(config: RouterConfig) -> dict[str, list[float]]:
    """Every numeric `gte`/`lte` cutoff per question, over every ladder, sorted."""
    rulesets = [config.policy, *config.rulesets.values()]
    rulesets += [a.rules for a in config.aliases.values() if not isinstance(a.rules, str)]
    out: dict[str, set[float]] = {}
    for ruleset in rulesets:
        for rule in ruleset.rules:
            for key, cond in rule.when.items():
                if key in FEATURE_CONDITIONS or not isinstance(cond, dict):
                    continue
                for op in ("gte", "lte"):
                    if _is_number(cond.get(op)):
                        out.setdefault(key, set()).add(float(cond[op]))
    return {key: sorted(values) for key, values in sorted(out.items())}


def _question_shape(question: dict[str, Any]) -> dict[str, Any]:
    kind = question.get("type")
    criteria = question.get("criteria")
    item: dict[str, Any] = {"type": kind}
    if kind == "choice" and isinstance(criteria, dict):
        item["choices"] = list(criteria)
    if kind == "score" and isinstance(criteria, list):
        item["levels"] = len(criteria)
        item["max"] = max(0, len(criteria) - 1)
    if kind == "noul":
        item["max"] = 1
    return item


def describe_config(
    config: RouterConfig,
    counts: dict[tuple[Any, Any], int],
    window: str,
    since: float,
) -> dict[str, Any]:
    text, source = running_text(config)
    root = _compose(text) if text else None
    rulesets = {
        "policy": describe_ruleset(config, "policy", config.policy, ["policy"], root, counts),
    }
    for name, ruleset in config.rulesets.items():
        rulesets[name] = describe_ruleset(
            config, name, ruleset, ["rulesets", name], root, counts
        )
    for alias, acfg in config.aliases.items():
        if not isinstance(acfg.rules, str):
            name = f"alias:{alias}"
            rulesets[name] = describe_ruleset(
                config, name, acfg.rules, ["aliases", alias, "rules"], root, counts
            )
    effort = config.adaptive_effort()
    policy = rulesets["policy"]
    return {
        "config_hash": config.config_hash,
        "path": config.config_path or None,
        "yaml": text,
        "yaml_source": source,
        "window": window,
        "since": since,
        "admissions": sum(counts.values()),
        "rules": policy["rules"],
        "low_confidence": policy["low_confidence"],
        "default": policy["default"],
        "fallback": policy["fallback"],
        "rulesets": rulesets,
        "routes": {
            name: {
                "description": lane.description,
                "primary": lane.primary.model_dump(mode="json"),
                "fallbacks": [e.model_dump(mode="json") for e in lane.fallbacks],
                "line": node_line(root, ["routes", name]),
            }
            for name, lane in config.routes.items()
        },
        "quality_lanes": {
            name: {
                **lane.model_dump(mode="json", exclude_none=True),
                "line": node_line(root, ["quality_lanes", name]),
            }
            for name, lane in config.quality_lanes.items()
        },
        "models": {
            name: {
                "provider": config.provider_of(name),
                "quota_cost": model.quota_cost if "quota_cost" in model.model_fields_set else None,
                "efforts": list(model.efforts),
                "default_effort": model.default_effort,
                "supports_tools": model.supports_tools,
                "supports_vision": model.supports_vision,
                "context_window": model.context_window,
                "protocol": model.protocol,
                "tags": list(model.tags),
                "line": node_line(root, ["models", name]),
            }
            for name, model in config.models.items()
        },
        "quota_policy": config.quota_policy.model_dump(mode="json"),
        "adaptive_effort": {
            "mode": effort.mode,
            "qualified_profiles": list(effort.qualified_profiles),
            "ladder": list(effort.ladder),
            "ladders": {k: list(v) for k, v in effort.ladders.items()},
            "thresholds": effort.thresholds.model_dump(mode="json"),
            "targets": effort.targets.model_dump(mode="json"),
            "downgrade_confirmations": effort.downgrade_confirmations,
            "upward": effort.upward,
            "downward": effort.downward,
            "hysteresis": effort.hysteresis,
            "questions": list(effort.questions),
            "shadow_questions": list(effort.shadow_questions),
            "line": node_line(root, ["experiments", "adaptive_effort"]),
        },
        "questions": {
            qid: {
                **_question_shape(q),
                # The running wording, so the Jev sandbox can start from it.
                "instructions": q.get("instructions") if isinstance(q.get("instructions"), str) else None,
                "line": node_line(root, ["questions", qid]),
            }
            for qid, q in config.questions.items()
        },
        "thresholds": config_thresholds(config),
        "effort_order": list(config.settings.effort_order),
    }


def _window(params: Any, allowed: set[str]) -> str | Response:
    if set(params.keys()) - allowed:
        return _error("unknown parameter")
    window = params.get("window", DEFAULT_WINDOW)
    if window not in WINDOWS:
        return _error("window must be one of " + ", ".join(WINDOWS))
    return window


def rated_admissions(
    rows: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]] | None = None,
    config: RouterConfig | None = None,
) -> list[dict[str, Any]]:
    """Each admission's numeric scores beside the verdict about it, if any.

    Numbers and labels only: the session id, alias and rule, the score and
    noul answers, and the newest verdict (None when nobody rated it). An
    admission with no numeric answer, such as a fallback, is left out.
    Pure. The Rules tab plots these on its threshold rulers.

    `knife_edge` is True when a score sits within the overview's margin of a
    cutoff near the rule it matched, under `config` (the running one); None
    when the row cannot be judged, such as one taken under another config.
    """
    labels = labels or {}
    sessions = [{"session_id": sid, **item} for sid, item in labels.items()]
    verdicts = verdicts_by_admission(rows, feedback, sessions)
    out = []
    for row in rows:
        scores = _numbers(row.get("answers"))
        if not scores:
            continue
        title = (labels.get(row.get("session_id") or "") or {}).get("title")
        out.append({
            "session_id": _label(row.get("session_id")),
            "title": title if isinstance(title, str) else None,
            "alias": _label(row.get("alias")),
            "rule": _label(row.get("rule")),
            "verdict": verdicts.get(row.get("id")),
            "scores": scores,
            "knife_edge": _knife(row, scores, config),
        })
    return out


def _knife(row: dict[str, Any], scores: dict[str, float], config: RouterConfig | None) -> bool | None:
    judged = knife_edge_of(row, scores, config)
    return judged if isinstance(judged, bool) else None


def knife_edge(rows: list[dict[str, Any]], config: RouterConfig) -> dict[str, Any]:
    """The overview's knife-edge rate over these admission rows. Pure."""
    return knife_edge_rate([knife_edge_of(r, _numbers(r.get("answers")), config) for r in rows])


def quota_sources(config: RouterConfig) -> list[str]:
    """The providers whose quota source is switched on, in config order.

    On means `enabled: true` and a source other than `none`, as the quota
    monitor decides it. Read from the config only: nothing is polled.
    """
    out = []
    for name, provider in config.providers.items():
        quota = provider.quota
        if quota is not None and quota.enabled and quota.source != "none":
            out.append(name)
    return out


async def get_config(request: Request) -> Response:
    """The running config, its rules with their hits, and where each one is.

    Also `rated`: the admissions in the window with their scores and
    verdicts, newest first, at most PREVIEW_MAX. Reads only.
    """
    router = request.app.state.router
    window = _window(request.query_params, {"window"})
    if isinstance(window, Response):
        return window
    since = _since(window)
    counts = {
        (alias, rule): n for alias, rule, n in router.sessions.admission_rule_counts(since)
    }
    body = describe_config(router.config, counts, window, since)
    rows = router.sessions.admissions_since(since, PREVIEW_MAX)
    body["rated"] = rated_admissions(
        rows,
        router.sessions.admission_feedback(since),
        router.sessions.session_labels(since),
        router.config,
    )
    body["knife_edge"] = knife_edge(rows, router.config)
    body["quota_sources"] = quota_sources(router.config)
    return _json(body)


# --- POST /router/config/preview -------------------------------------------


def _route_under(config: RouterConfig, row: dict[str, Any]) -> dict[str, Any]:
    """Where `config` sends this stored admission, at its recorded pressure."""
    alias = str(row.get("alias") or "")
    alias_cfg = config.aliases.get(alias)
    if alias_cfg is None:
        return {"error": "alias_not_in_config"}
    if row.get("rule") == ADMISSION_FALLBACK:
        # No rule ran; what replays is the alias's own admission fallback.
        replay = replay_decision(config, row)
        if replay.error or replay.replayed is None:
            return {"error": "routing_error"}
        fallback = alias_cfg.admission_fallback
        return {
            "rule": ADMISSION_FALLBACK,
            "route": fallback.route if fallback is not None else None,
            "model": replay.replayed[0],
            "effort": replay.replayed[1],
            "lane": None,
        }
    answers = row.get("answers")
    facts = row.get("features")
    if not isinstance(answers, dict) or not answers or not isinstance(facts, dict):
        return {"error": "no_answers"}
    try:
        traced = trace(
            config, alias_cfg, answers, features_from_facts(facts), _pressures(row.get("pressures"))
        )
    except Exception as exc:  # noqa: BLE001 - one bad row never fails the preview
        return {"error": f"untraceable: {type(exc).__name__}"}
    if traced.get("error"):
        return {"error": "routing_error"}
    return {
        "rule": traced.get("matched"),
        "route": traced.get("route"),
        "model": traced.get("model"),
        "effort": traced.get("effort"),
        "lane": traced.get("lane"),
    }


def _rank(order: list[str], effort: Any) -> int | None:
    return order.index(effort) if effort in order else None


def direction(
    before: dict[str, Any], after: dict[str, Any], config: RouterConfig
) -> str:
    """`up`, `down`, `same` or `unranked`: did the change ask for more?

    The same model compares by effort. Two models compare by `quota_cost`
    only when both are priced and share a provider; anything else has no
    order in router.yaml and is `unranked`, never guessed.
    """
    if before.get("error") or after.get("error"):
        return "unranked"
    pair = (before.get("model"), before.get("effort"))
    other = (after.get("model"), after.get("effort"))
    if pair == other:
        return "same"
    order = list(config.settings.effort_order)

    def by_effort() -> str:
        a, b = _rank(order, pair[1]), _rank(order, other[1])
        if a is None or b is None or a == b:
            return "unranked"
        return "up" if b > a else "down"

    if pair[0] == other[0]:
        return by_effort()
    costs = quota_costs(config)
    if (
        pair[0] in costs
        and other[0] in costs
        and config.provider_of(str(pair[0])) == config.provider_of(str(other[0]))
    ):
        if costs[pair[0]] == costs[other[0]]:
            return by_effort()
        return "up" if costs[other[0]] > costs[pair[0]] else "down"
    return "unranked"


def _numbers(answers: Any) -> dict[str, float]:
    """The score and noul answers, as numbers. No choices, no text."""
    out: dict[str, float] = {}
    if not isinstance(answers, dict):
        return out
    for qid, answer in answers.items():
        if not isinstance(answer, dict) or _label(qid) is None:
            continue
        kind = answer.get("type")
        if kind in ("score", "noul") and _is_number(answer.get(kind)):
            out[qid] = float(answer[kind])
    return out


def _share_by_model(routes: list[dict[str, Any]]) -> dict[str, float]:
    models = [r.get("model") for r in routes if r.get("model")]
    counts: dict[str, int] = {}
    for model in models:
        counts[model] = counts.get(model, 0) + 1
    return {m: n / len(models) for m, n in sorted(counts.items(), key=lambda kv: -kv[1])}


def draft_ladders(config: RouterConfig) -> dict[str, list[dict[str, Any]]]:
    """Every ladder in `config`, each rule as its name, `when` and line.

    Named as `describe_config` names its rulesets: `policy`, each named
    ruleset, and `alias:<name>` for an alias with rules of its own. The line
    is the rule's `name:` line in the config's own text, or None.
    """
    text, _source = running_text(config)
    root = _compose(text) if text else None
    ladders: list[tuple[str, Any, list[Any]]] = [("policy", config.policy, ["policy"])]
    ladders += [
        (name, ruleset, ["rulesets", name]) for name, ruleset in config.rulesets.items()
    ]
    ladders += [
        (f"alias:{alias}", acfg.rules, ["aliases", alias, "rules"])
        for alias, acfg in config.aliases.items()
        if not isinstance(acfg.rules, str)
    ]
    return {
        name: [
            {
                "index": index,
                "name": rule.name,
                "when": rule.when,
                "line": node_line(root, [*path, "rules", index - 1, "name"], value=True),
            }
            for index, rule in enumerate(ruleset.rules, start=1)
        ]
        for name, ruleset, path in ladders
    }


def preview(
    running: RouterConfig,
    draft: RouterConfig,
    rows: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay `rows` under both configs and say what moves. Pure."""
    labels = labels or {}
    sessions = [{"session_id": sid, **item} for sid, item in labels.items()]
    verdicts = verdicts_by_admission(rows, feedback, sessions)
    befores, afters, diffs = [], [], []
    reproduces = unreplayable = 0
    summary = {
        "rated": 0, "resolved_too_weak": 0, "resolved_too_strong": 0,
        "conflicts": 0, "right_changed": 0, "unranked": 0,
    }
    for row in rows:
        before = _route_under(running, row)
        after = _route_under(draft, row)
        if before.get("error") and after.get("error"):
            unreplayable += 1
            continue
        befores.append(before)
        afters.append(after)
        stored = {"rule": row.get("rule"), "model": row.get("model"), "effort": row.get("effort")}
        if all(before.get(k) == stored[k] for k in stored):
            reproduces += 1
        keys = ("rule", "route", "model", "effort", "error")
        changed = any(before.get(k) != after.get(k) for k in keys)
        verdict = verdicts.get(row.get("id"))
        moved = direction(before, after, running) if changed else "same"
        effect = _feedback_effect(verdict, changed, moved)
        if verdict:
            summary["rated"] += 1
        if effect:
            summary[effect] += 1
        if not changed:
            continue
        session_id = row.get("session_id")
        title = (labels.get(session_id or "") or {}).get("title")
        diffs.append({
            "row": row.get("id"),
            "decision_id": row.get("decision_id"),
            "session_id": session_id,
            "title": title if isinstance(title, str) else None,
            "ts": row.get("ts"),
            "answers": _numbers(row.get("answers")),
            "before": before,
            "after": after,
            "direction": moved,
            "feedback_verdict": verdict,
            "feedback_effect": effect,
        })
    return {
        "valid": True,
        "errors": [],
        "config_hash_running": running.config_hash,
        "config_hash_draft": draft.config_hash,
        "evaluated": len(befores),
        "changed": len(diffs),
        "unreplayable": unreplayable,
        "running_reproduces": reproduces,
        "tier_share_before": _share_by_model(befores),
        "tier_share_after": _share_by_model(afters),
        "quota_saved_before": quota_saved([r.get("model") for r in befores], running),
        "quota_saved_after": quota_saved([r.get("model") for r in afters], draft),
        "feedback": summary,
        "diffs": diffs,
        "draft_thresholds": config_thresholds(draft),
        "draft_rules": draft_ladders(draft),
    }


def _feedback_effect(verdict: str | None, changed: bool, moved: str) -> str | None:
    """What a change does to a session somebody rated (design 7.4)."""
    if not verdict or not changed:
        return None
    if verdict == "right":
        return "right_changed"
    if verdict not in ("too_weak", "too_strong"):
        return None
    if moved == "unranked":
        return "unranked"
    wanted, unwanted = ("up", "down") if verdict == "too_weak" else ("down", "up")
    if moved == wanted:
        return f"resolved_{verdict}"
    if moved == unwanted:
        return "conflicts"
    return None


def _limit(value: Any) -> int | None:
    if value is None:
        return PREVIEW_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= PREVIEW_MAX:
        return None
    return value


def run_preview(
    router: Any, text: Any, window: str = DEFAULT_WINDOW, limit: int = PREVIEW_LIMIT,
    now: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Validate a draft and replay recent admissions under it. Writes nothing."""
    draft, errors = validate_draft(text, router.config.config_path)
    if draft is None:
        return 422, {"valid": False, "errors": errors}
    since = _since(window, now)
    rows = router.sessions.admissions_since(since, limit)
    body = preview(
        router.config, draft, rows,
        router.sessions.admission_feedback(since),
        router.sessions.session_labels(since),
    )
    body.update({"window": window, "since": since, "limit": limit})
    return 200, body


async def preview_config(request: Request) -> Response:
    router = request.app.state.router
    payload = await _body(request)
    if isinstance(payload, Response):
        return payload
    if set(payload) - {"yaml", "window", "limit"}:
        return _error("unknown field")
    window = payload.get("window", DEFAULT_WINDOW)
    if window not in WINDOWS:
        return _error("window must be one of " + ", ".join(WINDOWS))
    limit = _limit(payload.get("limit"))
    if limit is None:
        return _error(f"limit must be an integer from 1 to {PREVIEW_MAX}")
    status, body = run_preview(router, payload.get("yaml"), window, limit)
    return _json(body, status)


# --- POST /router/config/apply ---------------------------------------------


def data_dir(router: Any) -> Path:
    """Where backups and golden cases go: beside the sqlite file."""
    path = str(getattr(router.store, "path", "") or "")
    if path and path != ":memory:":
        return Path(path).expanduser().parent
    return Path(os.path.expanduser("~/.jev-router"))


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_atomically(path: Path, text: str) -> None:
    """Write to a temporary file beside `path`, then os.replace over it."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup(path: Path, folder: Path, now: datetime | None = None) -> Path:
    """Copy the file as it is on disk into `folder`, under a UTC timestamp."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    target = _private_dir(folder) / f"{path.stem}.{stamp}{path.suffix or '.yaml'}"
    n = 1
    while target.exists():
        target = folder / f"{path.stem}.{stamp}-{n}{path.suffix or '.yaml'}"
        n += 1
    target.write_bytes(path.read_bytes())
    os.chmod(target, 0o600)
    return target


async def apply_config(request: Request) -> Response:
    """Validate a draft, back up router.yaml, write the draft, swap it in."""
    router = request.app.state.router
    payload = await _body(request)
    if isinstance(payload, Response):
        return payload
    if set(payload) - {"yaml", "base_hash", "force"}:
        return _error("unknown field")
    # One apply at a time per router. Made on first use, inside the loop.
    lock = getattr(router, "_apply_lock", None)
    if lock is None:
        lock = router._apply_lock = asyncio.Lock()
    async with lock:
        running = router.config
        if not running.config_path:
            return _error(
                "the running config was not loaded from a file, so there is nothing to write",
                409,
            )
        path = Path(running.config_path)
        base = payload.get("base_hash")
        if base is not None and base != running.config_hash:
            return _error(
                "the running config is not the one this draft was based on",
                409, config_hash=running.config_hash,
            )
        try:
            disk = path.read_text()
        except OSError:
            return _error("router.yaml cannot be read", 409)
        if config_text_hash(disk) != running.config_hash and payload.get("force") is not True:
            return _error(
                "router.yaml changed on disk since this process loaded it; "
                "send force: true to overwrite it",
                409, on_disk_hash=config_text_hash(disk), config_hash=running.config_hash,
            )
        draft, errors = validate_draft(payload.get("yaml"), str(path))
        if draft is None:
            return _json({"valid": False, "errors": errors}, 422)
        if draft.config_hash == running.config_hash and disk == payload["yaml"]:
            return _json({
                "applied": False, "unchanged": True, "config_hash": running.config_hash,
                "backup_path": None, "restart_required": [],
            })
        try:
            saved = backup(path, data_dir(router) / BACKUP_DIR)
            write_atomically(path, payload["yaml"])
        except OSError as exc:
            log.warning("could not write router.yaml: %s", type(exc).__name__)
            return _error(f"could not write router.yaml: {type(exc).__name__}", 500)
        restart = router.restart_required(draft)
        await router.replace_config(draft)
        log.info("config applied: %s -> %s", running.config_hash, draft.config_hash)
        return _json({
            "applied": True,
            "config_hash": draft.config_hash,
            "previous_hash": running.config_hash,
            "backup_path": str(saved),
            "restart_required": restart,
        })


# --- POST /router/sandbox/jev ----------------------------------------------

_SECRET = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:sk|pk|rk|ghp|gho|ghs|xox[abprs])[-_][A-Za-z0-9_-]{8,}"
    r"|\b[A-Za-z0-9+/_-]{40,}={0,2}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)"
)


def scrub_text(text: str) -> str:
    if _scrub_text is not None:
        return _scrub_text(text)
    return _SECRET.sub("[redacted]", text)


def scrub_state(state: Any) -> Any:
    if _scrub_state is not None:
        return _scrub_state(state)
    if isinstance(state, str):
        return scrub_text(state)
    if isinstance(state, dict):
        return {k: scrub_state(v) for k, v in state.items()}
    if isinstance(state, list):
        return [scrub_state(v) for v in state]
    return state


def find_row(router: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The decision row a request names, by row number or decision id."""
    row_id = payload.get("decision_row")
    if row_id is not None:
        if isinstance(row_id, bool) or not isinstance(row_id, int):
            return None
        return router.store.decision_by_row(row_id)
    decision_id = payload.get("decision_id")
    if isinstance(decision_id, str) and _label(decision_id):
        return router.store.deciding_decision(decision_id)
    return None


def _overrides(config: RouterConfig, raw: Any) -> dict[str, dict[str, Any]] | str:
    """The question overrides, merged over the running wording, or why not."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return "questions must be an object"
    out: dict[str, dict[str, Any]] = {}
    for qid, change in raw.items():
        if qid not in config.questions:
            return f"unknown question {qid!r}"
        if not isinstance(change, dict) or set(change) - {"instructions", "criteria"}:
            return "a question override may set only instructions and criteria"
        out[qid] = {**config.questions[qid], **change}
    return out


def _route_view(config: RouterConfig, row: dict[str, Any], answers: Any) -> dict[str, Any] | None:
    if row.get("event_type") == EVENT_EFFORT_PLAN or not isinstance(answers, dict) or not answers:
        return None
    return _route_under(config, {**row, "answers": answers, "rule": None})


async def sandbox_jev(request: Request) -> Response:
    """Ask Jev once about a stored decision, with an edited state or wording.

    Spends one Jev call. Writes nothing: no decision row, no session, no pin.
    The routes beside the answers come from `policy.trace` under the running
    config, at the facts and pressure the row recorded.
    """
    from .deciders.jev import JevDecider

    router = request.app.state.router
    payload = await _body(request)
    if isinstance(payload, Response):
        return payload
    if set(payload) - {"decision_row", "decision_id", "state", "questions"}:
        return _error("unknown field")
    row = find_row(router, payload)
    if row is None:
        return _error("unknown decision", 404)
    config: RouterConfig = router.config
    alias_cfg = config.aliases.get(str(row.get("alias") or ""))
    if alias_cfg is None:
        return _error("this decision's alias is not in the running config", 422)
    state = payload.get("state")
    if state is not None:
        if not isinstance(state, dict):
            return _error("state must be an object")
        state = scrub_state(state)
    else:
        state = row.get("state")
        if not state:
            return _error(
                "this decision has no stored state; send one in `state`",
                422, reason="no_stored_state",
            )
    overrides = _overrides(config, payload.get("questions"))
    if isinstance(overrides, str):
        return _error(overrides)
    probe = config.model_copy(update={"questions": {**config.questions, **overrides}})
    if row.get("event_type") == EVENT_EFFORT_PLAN:
        asked = [q for q in config.adaptive_effort().questions if q in config.questions]
    else:
        asked = active_questions(config, alias_cfg)
    facts = row.get("features") if isinstance(row.get("features"), dict) else {}
    # No draft step: the sandbox spends one Jev call and never an upstream one.
    quiet = alias_cfg.model_copy(update={"draft": None})
    decider = JevDecider(probe, router.jev_client)
    result = await decider.classify(
        features_from_facts(facts), quiet, state=state, question_ids=asked, shadow_ids=[]
    )
    if result.error:
        return _error(f"jev call failed: {result.error}", 502, jev_ms=result.jev_ms)
    original = row.get("answers") if isinstance(row.get("answers"), dict) else {}
    return _json({
        "row": row.get("id"),
        "decision_id": row.get("decision_id"),
        "event_type": row.get("event_type"),
        "config_matches": row.get("config_hash") == config.config_hash,
        "questions": asked,
        "overridden": sorted(overrides),
        "jev_ms": result.jev_ms,
        "original_answers": original,
        "sandbox_answers": result.answers,
        "original_route": _route_view(config, row, original),
        "sandbox_route": _route_view(config, row, result.answers),
    })


# --- POST /router/eval-cases -----------------------------------------------


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def golden_case(
    row: dict[str, Any], verdict: str, note: str | None,
    min_lane: str | None, max_lane: str | None,
) -> dict[str, Any]:
    """One exported case: what Jev saw and said, what was chosen, what was wanted."""
    return {
        "exported_at": time.time(),
        "row": row.get("id"),
        "decision_id": row.get("decision_id"),
        "session_id": row.get("session_id"),
        "alias": row.get("alias"),
        "event_type": row.get("event_type"),
        "config_hash": row.get("config_hash"),
        "state_builder": row.get("state_builder"),
        "packet_version": row.get("packet_version"),
        "state": row.get("state") or None,
        "facts": project_facts(row.get("features")),
        "answers": row.get("answers") if isinstance(row.get("answers"), dict) else None,
        "chosen": {
            "rule": row.get("rule"),
            "route": row.get("route"),
            "model": row.get("model"),
            "effort": row.get("effort"),
            "lane": row.get("quality_lane"),
        },
        "pressures": _pressures(row.get("pressures")),
        "expected": {
            "verdict": verdict,
            "min_quality_lane": min_lane,
            "max_quality_lane": max_lane,
        },
        "note": note,
    }


async def eval_cases(request: Request) -> Response:
    """Append one admission to the golden eval file beside the database."""
    router = request.app.state.router
    payload = await _body(request)
    if isinstance(payload, Response):
        return payload
    allowed = {"session_id", "decision_row", "verdict", "min_quality_lane",
               "max_quality_lane", "note"}
    if set(payload) - allowed:
        return _error("unknown field")
    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        return _error("verdict must be one of " + ", ".join(VERDICTS))
    lanes = {}
    for key in ("min_quality_lane", "max_quality_lane"):
        lane = payload.get(key)
        if lane is not None and lane not in router.config.quality_lanes:
            return _error(f"{key} must name a quality lane in router.yaml")
        lanes[key] = lane
    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            return _error("note must be a string")
        note = scrub_text(note)[:NOTE_MAX_CHARS]
    if payload.get("session_id") is not None:
        session_id = payload["session_id"]
        if not isinstance(session_id, str) or _label(session_id) is None:
            return _error("session_id must be a session id")
        row = router.sessions.admission_row(session_id)
    elif payload.get("decision_row") is not None:
        row = find_row(router, {"decision_row": payload["decision_row"]})
    else:
        return _error("give a session_id or a decision_row")
    if row is None:
        return _error("unknown session or decision", 404)
    case = golden_case(row, verdict, note, lanes["min_quality_lane"], lanes["max_quality_lane"])
    path = _private_dir(data_dir(router)) / GOLDEN_FILE
    line = json.dumps(case, default=str, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    return _json({"saved": True, "path": str(path), "total_cases": _count_lines(path)})


ROUTES = (
    ("/router/config", get_config, ["GET"]),
    ("/router/config/preview", preview_config, ["POST"]),
    ("/router/config/apply", apply_config, ["POST"]),
    ("/router/sandbox/jev", sandbox_jev, ["POST"]),
    ("/router/eval-cases", eval_cases, ["POST"]),
)
