"""Read-only audit UI. No transcripts, debug state, credentials or feedback notes."""

from __future__ import annotations

import logging
import math
import re
import time
from importlib.resources import files
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import RouterConfig
from .policy import trace
from .quota import STEERING_LABELS, TOKEN_FIELDS, foreign_label
from .semantic import ADMISSION_FALLBACK, features_from_facts, replay_decision
from .sessions import (
    AUDIT_PROBLEMS,
    EVENT_ADMISSION,
    EVENT_EFFORT_PLAN,
    EVENT_EXECUTION,
    REQUEST_PROBLEMS,
    STATES,
)

# Explicit projection: future decision columns are private until reviewed here.
AUDIT_FIELDS = frozenset([
    "id", "ts", "decision_id", "alias", "client", "conversation_key", "config_hash",
    "model", "effort", "rule", "mode", "fallback", "pinned", "jev_ms", "jev_tokens",
    "est_tokens", "message_count", "upstream_status", "route", "intended_model",
    "intended_effort", "fallback_index", "fallback_reason", "pressures", "shifted",
    "reordered", "accepted", "stream_state", "first_byte_ms", "response_ms",
    "response_bytes", "session_id", "request_id", "turn_id", "event_type",
    "quality_lane", "answers", "shadow_answers", "counterfactual", "exclusions",
    "evidence", "qualification_ref", "action", "experiment_routes", "quota_status",
    "packet_version", "shadow_packet_version",
])

HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}


async def dashboard(request: Request) -> Response:
    """Public, data-free shell: a browser cannot set an admin header on navigation."""
    return HTMLResponse(
        files("jev_router").joinpath("static/dashboard.html").read_text(),
        headers=HEADERS,
    )


# Public asset name -> (file under static/, media type). An explicit map, so a
# request can never name a path: nothing outside it is served.
ASSETS = {
    "dashboard.js": ("dashboard.js", "text/javascript"),
    "dashboard.css": ("dashboard.css", "text/css"),
    "Geist-Variable.woff2": ("fonts/Geist-Variable.woff2", "font/woff2"),
    "GeistMono-Variable.woff2": ("fonts/GeistMono-Variable.woff2", "font/woff2"),
    "OFL.txt": ("fonts/OFL.txt", "text/plain; charset=utf-8"),
}


async def asset(request: Request) -> Response:
    entry = ASSETS.get(request.path_params["name"])
    if entry is None:
        return Response(status_code=404, headers=HEADERS)
    path, media = entry
    resource = files("jev_router").joinpath("static").joinpath(*path.split("/"))
    try:
        body = resource.read_bytes()
    except OSError:
        # A font missing from a build falls back to system-ui in the CSS.
        return Response(status_code=404, headers=HEADERS)
    return Response(body, media_type=media, headers=HEADERS)


def project_row(row: dict[str, Any], config: RouterConfig) -> dict[str, Any]:
    item = {key: value for key, value in row.items() if key in AUDIT_FIELDS}
    # Never present today's thresholds as the cause of a historical choice.
    alias = config.aliases.get(row.get("alias") or "")
    if alias and row.get("config_hash") == config.config_hash:
        ruleset = config.ruleset_for(alias)
        name = row.get("rule")
        if name == "low_confidence" and ruleset.low_confidence:
            item["rule_definition"] = ruleset.low_confidence.model_dump(mode="json")
        elif name == "default" and ruleset.default:
            item["rule_definition"] = {"default": ruleset.default.model_dump(mode="json")}
        else:
            match = next((rule for rule in ruleset.rules if rule.name == name), None)
            if match:
                item["rule_definition"] = match.model_dump(mode="json")
    return item


async def audit(request: Request) -> Response:
    """Bounded snapshot, including mutations to older rows after stream completion.

    Protected by the existing /router/* ingress credential. This is a global
    admin view, not a session-owner endpoint. No provider calls or quota polls.
    """
    router = request.app.state.router
    try:
        limit = int(request.query_params.get("limit", "300"))
        if not 1 <= limit <= 500:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "limit must be between 1 and 500"}, status_code=400, headers=HEADERS)
    rows = router.store.recent_decisions(limit, with_feedback=False)
    return JSONResponse({
        "decisions": [project_row(row, router.config) for row in rows],
        "limit": limit,
        "config_hash": router.config.config_hash,
        "mode": router.config.settings.mode,
    }, headers=HEADERS)


# --- the session inspector ------------------------------------------------
#
# Read only, like everything above: no writes, no provider calls, no quota
# polls. Every field below is named on purpose. A stored row carries the
# conversation key, the full request facts (tool names, languages) and, with
# log_state, the state Jev was shown; none of that is copied wholesale.

# Request facts that may leave the router: counts, flags and router-made labels.
FACT_FIELDS = (
    "message_count", "est_tokens", "input_tokens", "output_tokens",
    "reserve_tokens", "headroom", "context_window", "max_tokens", "has_tools",
    "tool_count", "has_images", "has_code", "stream", "estimate_method",
    "session_mode",
)
# The two allowlisted facts that are strings. Both are written by the router.
FACT_LABELS = frozenset(["estimate_method", "session_mode"])
# A state-shape fact that is a string survives only under one of these keys.
SHAPE_LABELS = frozenset(["client"])

SESSION_FIELDS = (
    "session_id", "alias", "client", "state", "version", "created", "last_seen",
    "closed_at", "provider", "protocol", "context_window", "max_output_tokens",
    "supports_tools", "supports_vision", "base_effort", "effective_effort",
    "expected_effort", "confirmed_effort", "effort_mode", "adaptation_mode",
    "effort_lineage", "decision_id", "decision_rule", "decision_source",
    "quality_lane", "quota_changed_choice", "config_hash", "jev_model",
    "compaction_epoch", "compaction_state", "last_input_tokens",
    "last_output_tokens", "tombstone", "state_builder",
)

PLAN_FIELDS = (
    "plan_id", "turn_id", "sequence", "request_id", "mode", "action", "status",
    "from_effort", "to_effort", "base_effort", "recommendation",
    "expected_effort", "confirmed_effort", "reconciled", "compaction_epoch",
    "reason", "created", "updated",
)
# What Jev was asked at a turn boundary: the plan's own decision row. The same
# fields the admission shows, never the conversation key or the state text.
JUDGMENT_FIELDS = (
    "id", "ts", "answers", "shadow_answers", "packet_version",
    "shadow_packet_version", "jev_ms", "jev_tokens",
)
# The evidence the plan weighed and the thresholds it weighed it against.
EVIDENCE_NUMBERS = ("difficulty", "p_hard", "harm_if_wrong", "corrective_followup")
COUNTERFACTUAL_FIELDS = (
    "question", "answer", "action", "to_effort", "recommendation", "changed", "reason",
)

QUOTA_STATUSES = frozenset(["fresh", "stale", "unknown", "error"])
WINDOW_NUMBERS = (
    "used_percent", "used", "limit", "expected_used_percent", "window_minutes",
    "resets_at",
)

_LABEL = re.compile(r"[A-Za-z0-9_.:/()+-]{1,64}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else None


def project_facts(raw: Any) -> dict[str, Any]:
    """The allowlisted request facts, each checked for its type."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in FACT_FIELDS:
        value = raw.get(key)
        typed = isinstance(value, bool) or _is_number(value)
        if typed or (key in FACT_LABELS and _label(value) is not None):
            out[key] = value
    return out


def project_state_shape(raw: Any) -> dict[str, Any] | None:
    """Field names and character counts; flags and counts; never text."""
    if not isinstance(raw, dict):
        return None
    parts = raw.get("parts") if isinstance(raw.get("parts"), dict) else {}
    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
    out_facts: dict[str, Any] = {}
    for key, value in facts.items():
        if _label(key) is None:
            continue
        typed = value is None or isinstance(value, bool) or _is_number(value)
        if typed or (key in SHAPE_LABELS and _label(value) is not None):
            out_facts[key] = value
    return {
        "builder": _label(raw.get("builder")),
        "parts": {
            key: value for key, value in parts.items()
            if _label(key) is not None and _is_number(value) and not isinstance(value, float)
        },
        "facts": out_facts,
    }


def project_quota(raw: Any) -> dict[str, Any] | None:
    """Per provider: status, pressure, age, and each window's own numbers."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for provider, row in raw.items():
        if _label(provider) is None or not isinstance(row, dict):
            continue
        status = row.get("status")
        item: dict[str, Any] = {
            "status": status if status in QUOTA_STATUSES else None,
            "pressure": row.get("pressure") if _is_number(row.get("pressure")) else None,
            "age_seconds": row.get("age_seconds") if _is_number(row.get("age_seconds")) else None,
        }
        windows = []
        for window in row.get("windows") or []:
            if not isinstance(window, dict):
                continue
            # Each window stays one record: its usage beside its own reset.
            entry = {k: window.get(k) for k in WINDOW_NUMBERS if _is_number(window.get(k))}
            if _label(window.get("name")) is not None:
                entry["name"] = window["name"]
            windows.append(entry)
        item["windows"] = windows
        out[provider] = item
    return out


# --- the live quota strip ------------------------------------------------
#
# The one live source on the overview: the quota monitor's last readings,
# read out of memory. Nothing here polls, runs a program or writes.

log = logging.getLogger("jev_router.dashboard")

LIVE_VIA = frozenset(["snapshot", "cli"])
LIVE_WINDOW_NUMBERS = (
    "used_percent", "expected_percent", "pace_delta", "resets_at", "window_minutes",
)
LIVE_ROUTED_NUMBERS = (
    "pressure", "age_seconds", "fetched_at", "used_percent", "demote_above",
    "exhausted_at", "polls", "errors",
)
LIVE_TRACKED_NUMBERS = ("age_seconds", "fetched_at", "credits_remaining", "polls", "errors")
_CURRENCY = re.compile(r"[A-Z]{3}")


def _num(value: Any) -> Any:
    return value if _is_number(value) and math.isfinite(value) else None


def _live_windows(raw: Any) -> list[dict[str, Any]]:
    out = []
    for window in raw if isinstance(raw, list) else []:
        if not isinstance(window, dict):
            continue
        item = {key: _num(window.get(key)) for key in LIVE_WINDOW_NUMBERS}
        item["name"] = _label(window.get("name"))
        item["reset_description"] = foreign_label(window.get("reset_description")) or None
        out.append(item)
    return out


def _live_routed(row: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {key: _num(row.get(key)) for key in LIVE_ROUTED_NUMBERS}
    status = row.get("status")
    steering = row.get("steering")
    spent = row.get("exhausted") if isinstance(row.get("exhausted"), dict) else None
    item.update({
        "provider": _label(row.get("provider")),
        "enabled": row.get("enabled") is True,
        "source": _label(row.get("source")),
        "codexbar_provider": _label(row.get("codexbar_provider")),
        "via": row.get("via") if row.get("via") in LIVE_VIA else None,
        "status": status if status in QUOTA_STATUSES else None,
        "steering": steering if steering in STEERING_LABELS else None,
        "exhausted": None if spent is None else {
            "name": _label(spent.get("name")),
            "used_percent": _num(spent.get("used_percent")),
            "resets_at": _num(spent.get("resets_at")),
        },
        "windows": _live_windows(row.get("windows")),
        "models": [
            {
                "model": _label(m.get("model")),
                "quota_cost": _num(m.get("quota_cost")),
                "pressure": _num(m.get("pressure")),
            }
            for m in row.get("models") or []
            if isinstance(m, dict) and _label(m.get("model")) is not None
        ],
    })
    return item


def _live_tracked(row: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {key: _num(row.get(key)) for key in LIVE_TRACKED_NUMBERS}
    status = row.get("status")
    tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else None
    currency = row.get("currency")
    item.update({
        "id": _label(row.get("id")),
        "via": row.get("via") if row.get("via") in LIVE_VIA else None,
        "status": status if status in QUOTA_STATUSES else None,
        "windows": _live_windows(row.get("windows")),
        "plans": [
            {
                "title": foreign_label(plan.get("title")) or None,
                "used_percent": _num(plan.get("used_percent")),
            }
            for plan in row.get("plans") or []
            if isinstance(plan, dict)
        ],
        "tokens": None if tokens is None else {
            name: _num(tokens.get(name)) for name, _ in TOKEN_FIELDS
        },
        "currency": currency if isinstance(currency, str) and _CURRENCY.fullmatch(currency) else None,
    })
    return item


def live_quota(monitor: Any, now: float) -> dict[str, Any] | None:
    """The monitor's current readings, projected field by field.

    `routed` is every provider the router knows, with what quota is doing to
    its routing. `tracked` is every CodexBar subscription no provider links
    to: shown, never routed. Returns None when the monitor cannot be read,
    which the page shows as a gap.
    """
    try:
        raw = monitor.all_snapshots(now)
    except Exception:  # noqa: BLE001 - a read-only view may never fail on quota
        log.exception("could not read the live quota snapshots")
        return None
    if not isinstance(raw, dict):
        return None
    routed = [
        _live_routed(row) for row in raw.get("routed") or []
        if isinstance(row, dict) and _label(row.get("provider")) is not None
    ]
    tracked = [
        _live_tracked(row) for row in raw.get("tracked") or []
        if isinstance(row, dict) and _label(row.get("id")) is not None
    ]
    return {
        "as_of": _num(raw.get("as_of")),
        "quota_routing": raw.get("quota_routing") is True,
        "demote_above": _num(raw.get("demote_above")),
        "exhausted_at": _num(raw.get("exhausted_at")),
        "routed": routed,
        "tracked": tracked,
    }


def _project_session(row: dict[str, Any], running: str) -> dict[str, Any]:
    item = {key: row.get(key) for key in SESSION_FIELDS}
    item["model"] = row.get("model_key")
    if item["quota_changed_choice"] is not None:
        item["quota_changed_choice"] = bool(item["quota_changed_choice"])
    item["config_matches"] = bool(row.get("config_hash")) and row.get("config_hash") == running
    return item


def _project_evidence(facts: dict[str, Any]) -> dict[str, Any] | None:
    """The numbers the plan weighed, its ladder and thresholds; no text."""
    raw = facts.get("evidence")
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {
        key: raw.get(key) if _is_number(raw.get(key)) else None for key in EVIDENCE_NUMBERS
    }
    out["failure_mode"] = _label(raw.get("failure_mode"))
    out["short_continuation"] = raw.get("short_continuation") is True
    out["missing"] = [m for m in raw.get("missing") or [] if _label(m) is not None]
    # The classifier's error text can quote anything; only that there was one.
    out["error"] = bool(raw.get("error"))
    thresholds = facts.get("thresholds") if isinstance(facts.get("thresholds"), dict) else {}
    out["thresholds"] = {
        key: value for key, value in thresholds.items()
        if _label(key) is not None and _is_number(value)
    }
    out["ladder"] = [rung for rung in facts.get("ladder") or [] if _label(rung) is not None]
    out["floor"] = _label(facts.get("floor"))
    out["target"] = _label(facts.get("target"))
    out["direction"] = _label(facts.get("direction"))
    return out


def _project_judgment(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = {key: row.get(key) for key in JUDGMENT_FIELDS}
    item["state_shape"] = project_state_shape(row.get("state_shape"))
    return item


def _project_plan(
    row: dict[str, Any], judgment: dict[str, Any] | None = None
) -> dict[str, Any]:
    item = {key: row.get(key) for key in PLAN_FIELDS}
    facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
    item["evidence"] = _project_evidence(facts)
    # The plan's own decision row: Jev's answers for this turn, as recorded.
    item["judgment"] = _project_judgment(judgment)
    counterfactual = facts.get("shadow_counterfactual")
    item["shadow_counterfactual"] = (
        {key: counterfactual.get(key) for key in COUNTERFACTUAL_FIELDS}
        if isinstance(counterfactual, dict) else None
    )
    return item


def _project_request(seq: int, row: dict[str, Any]) -> dict[str, Any]:
    decision = row.get("decision") or {}
    usage = decision.get("upstream_usage")
    return {
        "seq": seq,
        "request_id": row.get("request_id"),
        "status": row.get("status"),
        "created": row.get("created"),
        "updated": row.get("updated"),
        "upstream_status": row.get("upstream_status")
        if row.get("upstream_status") is not None
        else decision.get("upstream_status"),
        "row": decision.get("id"),
        "ts": decision.get("ts"),
        # The turn the router logged this request under; NULL before it did.
        "turn_id": _label(decision.get("turn_id")),
        "event_type": decision.get("event_type"),
        "model": decision.get("model"),
        # The base effort the request was sent with. Not the effective one.
        "effort": decision.get("effort"),
        "stream_state": decision.get("stream_state"),
        "accepted": None if decision.get("accepted") is None else bool(decision["accepted"]),
        "first_byte_ms": decision.get("first_byte_ms"),
        "response_ms": decision.get("response_ms"),
        "response_bytes": decision.get("response_bytes"),
        "fallback_index": decision.get("fallback_index"),
        "usage_reported": bool(usage) if decision else None,
        "facts": project_facts(decision.get("features")) if decision else None,
    }


def _project_admission(row: dict[str, Any], config: RouterConfig) -> dict[str, Any]:
    item = project_row(row, config)
    item.pop("conversation_key", None)
    item["facts"] = project_facts(row.get("features"))
    item["state_shape"] = project_state_shape(row.get("state_shape"))
    item["quota_snapshot"] = project_quota(row.get("quota_snapshot"))
    return item


def explain_admission(
    row: dict[str, Any] | None, config: RouterConfig, store: Any
) -> dict[str, Any]:
    """The rule-by-rule trace of the admission, under the config it was taken under.

    Only ever that config: the running one when the hashes agree, else the
    stored snapshot. A changed router.yaml is a different policy, and a trace
    under it would explain a decision nobody took.
    """
    if row is None:
        return {"available": False, "reason": "no_admission_row"}
    recorded = str(row.get("config_hash") or "")
    stored = {"model": row.get("model"), "effort": row.get("effort"), "rule": row.get("rule")}
    head = {"config_hash": recorded, "stored": stored}
    if row.get("rule") == ADMISSION_FALLBACK:
        # The classifier never answered and no rule ran; there is nothing to trace.
        return {"available": False, "reason": "fallback_admission", **head}
    answers = row.get("answers")
    if not isinstance(answers, dict) or not answers:
        return {"available": False, "reason": "no_answers", **head}
    if recorded and recorded == config.config_hash:
        source, under = "running", config
    else:
        loaded = store.load_config_snapshot(recorded) if recorded else "not_on_file"
        if isinstance(loaded, str):
            return {"available": False, "reason": loaded, **head}
        source, under = "snapshot", loaded
    alias_cfg = under.aliases.get(str(row.get("alias") or ""))
    if alias_cfg is None:
        return {"available": False, "reason": "alias_not_in_config", **head}
    facts = row.get("features") if isinstance(row.get("features"), dict) else {}
    # The pressure it was taken under, read the way `decisions replay` reads it.
    pressures = replay_decision(under, row).pressures
    try:
        traced = trace(under, alias_cfg, answers, features_from_facts(facts), pressures)
    except Exception as exc:  # noqa: BLE001 - a diagnostic never fails the page
        return {"available": False, "reason": f"untraceable: {type(exc).__name__}", **head}
    replayed = {
        "model": traced.get("model"),
        "effort": traced.get("effort"),
        "rule": traced.get("matched"),
        "lane": traced.get("lane"),
    }
    return {
        "available": True,
        "source": source,
        **head,
        "reproduces": all(replayed[k] == stored[k] for k in ("model", "effort", "rule")),
        "replayed": replayed,
        "pressures": pressures,
        **{k: v for k, v in traced.items() if k not in ("model", "effort", "lane")},
    }


# --- the session list -------------------------------------------------------

# Overview and list windows: span and bucket width, in seconds.
WINDOWS = {
    "1h": (3600, 300),
    "24h": (86400, 3600),
    "7d": (7 * 86400, 6 * 3600),
    "30d": (30 * 86400, 86400),
}
ADAPTATION_MODES = ("off", "shadow", "active")
_SEARCH = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
LIST_PARAMS = frozenset([
    "limit", "client", "model", "rule", "state", "adaptive", "problem", "pinned",
    "q", "window",
])
LISTED_FIELDS = (
    "session_id", "alias", "client", "state", "created", "last_seen",
    "closed_at", "model", "base_effort", "effective_effort",
    "decision_rule", "quality_lane", "config_hash", "config_matches",
    "adaptation_mode", "tombstone",
)


class BadQuery(ValueError):
    """A query parameter outside its bounds; the message is safe to return."""


def _bad(message: str) -> Response:
    return JSONResponse({"error": message}, status_code=400, headers=HEADERS)


def percentiles(values: list[Any]) -> dict[str, Any]:
    """Nearest-rank p50 and p95. No values is a gap, never zero."""
    data = sorted(v for v in values if _is_number(v))
    if not data:
        return {"n": 0, "p50": None, "p95": None}

    def rank(q: float) -> float:
        return data[max(0, math.ceil(q * len(data)) - 1)]

    return {"n": len(data), "p50": rank(0.5), "p95": rank(0.95)}


def list_filters(params: Any, now: float) -> dict[str, Any]:
    """The validated filters of `/router/audit/sessions`. Raises BadQuery."""
    unknown = sorted(set(params.keys()) - LIST_PARAMS)
    if unknown:
        raise BadQuery("unknown parameter")
    filters: dict[str, Any] = {}
    for key in ("client", "model", "rule"):
        value = params.get(key)
        if value is not None:
            if _label(value) is None:
                raise BadQuery(f"{key} must be a label of at most 64 characters")
            filters[key] = value
    state = params.get("state")
    if state is not None:
        if state not in STATES:
            raise BadQuery("state must be one of " + ", ".join(STATES))
        filters["state"] = state
    adaptive = params.get("adaptive")
    if adaptive is not None:
        if adaptive not in ADAPTATION_MODES:
            raise BadQuery("adaptive must be one of " + ", ".join(ADAPTATION_MODES))
        filters["adaptation"] = adaptive
    problem = params.get("problem")
    if problem is not None:
        if problem != "any" and problem not in AUDIT_PROBLEMS:
            raise BadQuery("problem must be any or one of " + ", ".join(AUDIT_PROBLEMS))
        filters["problem"] = problem
    pinned = params.get("pinned")
    if pinned is not None:
        if pinned not in ("0", "1"):
            raise BadQuery("pinned must be 0 or 1")
        filters["pinned"] = pinned == "1"
    search = params.get("q")
    if search is not None:
        if not _SEARCH.fullmatch(search):
            raise BadQuery("q must be 1 to 64 id characters")
        filters["search"] = search
    window = params.get("window")
    if window is not None:
        if window not in WINDOWS:
            raise BadQuery("window must be one of " + ", ".join(WINDOWS))
        filters["since"] = now - WINDOWS[window][0]
    return filters


def session_problems(row: dict[str, Any]) -> list[str]:
    """What went wrong on this session, by kind, from its own rows."""
    counts = row.get("requests") or {}
    out = [kind for kind, statuses in REQUEST_PROBLEMS.items()
           if any(counts.get(s) for s in statuses)]
    if "admission_fallback" in (row.get("decision_source"), row.get("decision_rule")):
        out.append("fallback")
    if row.get("decision_rule") == "low_confidence":
        out.append("low_confidence")
    return out


async def audit_sessions(request: Request) -> Response:
    """Recent strict sessions, newest first, filtered. Protected like every /router/* route."""
    router = request.app.state.router
    try:
        limit = int(request.query_params.get("limit", "50"))
        if not 1 <= limit <= 200:
            raise ValueError
    except ValueError:
        return _bad("limit must be between 1 and 200")
    try:
        filters = list_filters(request.query_params, time.time())
    except BadQuery as exc:
        return _bad(str(exc))
    running = router.config.config_hash
    pins = {b["session_id"]: b for b in router.sessions.bookmarks()}
    sessions = []
    for row in router.sessions.audit_sessions(limit, filters):
        item = _project_session(row, running)
        listed = {key: item[key] for key in LISTED_FIELDS}
        listed["effort"] = listed.pop("base_effort")
        listed["rule"] = listed.pop("decision_rule")
        listed["requests"] = {
            str(k): int(v) for k, v in (row.get("requests") or {}).items()
        }
        listed["problems"] = session_problems(row)
        times = percentiles(row.get("response_ms") or [])
        listed["response_ms"] = {"n": times["n"], "p50": times["p50"]}
        pin = pins.get(row["session_id"])
        listed["pinned"] = {"slug": pin["slug"], "label": pin["label"]} if pin else None
        sessions.append(listed)
    bookmarks = [
        {key: b.get(key) for key in ("slug", "label", "session_id", "created")}
        for b in router.sessions.bookmarks()
    ]
    facets = {
        name: [{"value": value, "n": n} for value, n in values if _label(value) is not None]
        for name, values in router.sessions.audit_facets().items()
    }
    return JSONResponse({
        "config_hash": running,
        "limit": limit,
        "matched": router.sessions.audit_count(filters),
        "bookmarks": bookmarks,
        "facets": facets,
        "sessions": sessions,
    }, headers=HEADERS)


# --- the overview ------------------------------------------------------------

REQUEST_STATUSES = ("completed", "unknown", "rejected", "stream_failed", "accepted", "in_flight")
PLAN_STATUSES = ("planned", "accepted", "confirmed", "rejected", "outcome_unknown")
PLAN_ACTIONS = ("keep", "change_effort", "blocked")
TOP = 20


def _top(counter: dict[Any, int], *keys: str) -> list[dict[str, Any]]:
    """The most common values, each a router-made label or a visible null."""
    out = []
    for value, n in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[:TOP]:
        values = value if len(keys) > 1 else (value,)
        entry: dict[str, Any] = {k: _label(v) for k, v in zip(keys, values)}
        entry["n"] = n
        out.append(entry)
    return out


def _slot(series: list[dict[str, Any]], since: float, step: int, ts: Any) -> dict | None:
    if not _is_number(ts) or ts < since:
        return None
    index = int((ts - since) // step)
    return series[index] if 0 <= index < len(series) else None


def _is_fallback(row: dict[str, Any]) -> bool:
    return row.get("rule") == ADMISSION_FALLBACK or bool(row.get("fallback"))


def build_overview(raw: dict[str, list[dict[str, Any]]], window: str, since: float) -> dict[str, Any]:
    """Counts, rates, percentiles and buckets. Pure: rows in, numbers out.

    Strict-session metrics come from session rows only. Legacy alias rows
    (no event type) are counted on their own and never mixed in.
    """
    span, step = WINDOWS[window]
    series: list[dict[str, Any]] = [
        {"t": since + i * step, "requests": 0, "problems": 0, "_ms": []}
        for i in range(span // step)
    ]

    sessions = raw.get("sessions") or []
    adaptation = dict.fromkeys(ADAPTATION_MODES, 0)
    unrecorded = quota_changed = quota_recorded = 0
    for row in sessions:
        mode = row.get("adaptation_mode")
        if mode in adaptation:
            adaptation[mode] += 1
        else:
            unrecorded += 1
        if row.get("quota_changed_choice") is not None:
            quota_recorded += 1
            quota_changed += int(bool(row["quota_changed_choice"]))

    # The request ledger decides each execution's outcome.
    statuses = dict.fromkeys(REQUEST_STATUSES, 0)
    for row in raw.get("requests") or []:
        status = row.get("status")
        if status in statuses:
            statuses[status] += 1
        slot = _slot(series, since, step, row.get("created"))
        if slot is not None:
            slot["requests"] += 1
            if status in ("unknown", "rejected", "stream_failed"):
                slot["problems"] += 1
    total = sum(statuses.values())

    admissions: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    legacy: list[dict[str, Any]] = []
    for row in raw.get("decisions") or []:
        kind = row.get("event_type")
        if kind == EVENT_ADMISSION:
            admissions.append(row)
        elif kind == EVENT_EXECUTION:
            executions.append(row)
            slot = _slot(series, since, step, row.get("ts"))
            if slot is not None and _is_number(row.get("response_ms")):
                slot["_ms"].append(row["response_ms"])
        elif kind is None:
            legacy.append(row)

    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in executions:
        by_model.setdefault(_label(row.get("model")) or "", []).append(row)
    models = [
        {
            "model": model or None,
            "n": len(rows),
            "first_byte_ms": percentiles([r.get("first_byte_ms") for r in rows]),
            "response_ms": percentiles([r.get("response_ms") for r in rows]),
            "usage_reported": sum(1 for r in rows if r.get("usage_reported")),
        }
        for model, rows in sorted(by_model.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:TOP]
    ]

    rules: dict[Any, int] = {}
    choices: dict[Any, int] = {}
    for row in admissions:
        rules[row.get("rule")] = rules.get(row.get("rule"), 0) + 1
        pair = (row.get("model"), row.get("effort"))
        choices[pair] = choices.get(pair, 0) + 1

    plans = raw.get("plans") or []
    effort: dict[str, Any] = {}
    for mode in ("shadow", "active"):
        mine = [p for p in plans if p.get("mode") == mode]
        changes = [p for p in mine if p.get("action") == "change_effort"]
        effort[mode] = {
            "plans": len(mine),
            # A plan whose own recommendation differs from the effort it started at.
            "recommended": sum(
                1 for p in mine
                if p.get("recommendation") and p.get("recommendation") != p.get("from_effort")
            ),
            "applied": len(changes),
            "confirmed": sum(1 for p in changes if p.get("status") == "confirmed"),
            "by_action": {a: sum(1 for p in mine if p.get("action") == a) for a in PLAN_ACTIONS},
            "by_status": {s: sum(1 for p in mine if p.get("status") == s) for s in PLAN_STATUSES},
        }

    quota = None
    for reading in (raw.get("quota") or [])[:1]:
        quota = {
            "ts": reading.get("ts") if _is_number(reading.get("ts")) else None,
            "providers": project_quota(reading.get("snapshot")) or {},
        }

    for slot in series:
        times = slot.pop("_ms")
        slot["timed"] = len(times)
        slot["response_p50_ms"] = percentiles(times)["p50"]

    return {
        "window": window,
        "since": since,
        "bucket_seconds": step,
        "sessions": {
            "admitted": len(sessions),
            "adaptation": adaptation,
            "adaptation_unrecorded": unrecorded,
        },
        "executions": {
            "total": total,
            "by_status": statuses,
            "completion_rate": statuses["completed"] / total if total else None,
            "rows": len(executions),
            "usage_reported": sum(1 for r in executions if r.get("usage_reported")),
            "first_byte_recorded": sum(1 for r in executions if _is_number(r.get("first_byte_ms"))),
        },
        "latency": {
            # A fallback admission's time is a failed call's, not Jev's answer time.
            "jev_admission_ms": percentiles(
                [r.get("jev_ms") for r in admissions if not _is_fallback(r)]
            ),
            "jev_plan_ms": percentiles([p.get("jev_ms") for p in plans]),
            "models": models,
        },
        "series": series,
        "decisions": {
            "admissions": len(admissions),
            "rules": _top(rules, "rule"),
            "choices": _top(choices, "model", "effort"),
            "low_confidence": rules.get("low_confidence", 0),
            "fallback": sum(1 for r in admissions if _is_fallback(r)),
            "quota_changed": quota_changed,
            "quota_recorded": quota_recorded,
        },
        "quota": quota,
        "effort": effort,
        "legacy": {
            "decisions": len(legacy),
            "pinned": sum(1 for r in legacy if r.get("pinned")),
            "fallback": sum(1 for r in legacy if r.get("fallback")),
            "upstream_errors": sum(
                1 for r in legacy
                if _is_number(r.get("upstream_status")) and r["upstream_status"] >= 400
            ),
            "jev_ms": percentiles(
                [r.get("jev_ms") for r in legacy if not r.get("pinned") and not r.get("fallback")]
            ),
            "first_byte_ms": percentiles([r.get("first_byte_ms") for r in legacy]),
            "response_ms": percentiles([r.get("response_ms") for r in legacy]),
        },
    }


async def audit_overview(request: Request) -> Response:
    """Router health over a window, computed from the tables. Writes nothing."""
    router = request.app.state.router
    if set(request.query_params.keys()) - {"window"}:
        return _bad("unknown parameter")
    window = request.query_params.get("window", "24h")
    if window not in WINDOWS:
        return _bad("window must be one of " + ", ".join(WINDOWS))
    span, step = WINDOWS[window]
    # Buckets end at the next boundary, so the answer changes only with the data.
    since = (math.floor(time.time() / step) + 1) * step - span
    body = build_overview(router.sessions.audit_overview(since), window, since)
    body["config_hash"] = router.config.config_hash
    body["subscriptions"] = live_quota(router.quota, router.quota.clock())
    return JSONResponse(body, headers=HEADERS)


async def audit_session(request: Request) -> Response:
    """One session: binding, admission, requests, plans and the admission trace."""
    router = request.app.state.router
    session_id = request.path_params["session_id"]
    row = router.sessions.audit_session(session_id)
    if row is None:
        return JSONResponse({"error": "unknown session"}, status_code=404, headers=HEADERS)
    config = router.config
    admission = router.sessions.admission_row(session_id)
    judgments = {
        decision["decision_id"]: decision
        for decision in router.sessions.decision_rows(session_id)
        if decision.get("event_type") == EVENT_EFFORT_PLAN
    }
    return JSONResponse({
        "config_hash": config.config_hash,
        "session": _project_session(row, config.config_hash),
        "admission": _project_admission(admission, config) if admission else None,
        "requests": [
            _project_request(seq, item)
            for seq, item in enumerate(router.sessions.request_rows(session_id), start=1)
        ],
        "plans": [
            _project_plan(plan, judgments.get(plan.get("decision_id") or ""))
            for plan in router.sessions.all_plans(session_id)
        ],
        "trace": explain_admission(admission, config, router.store),
    }, headers=HEADERS)
