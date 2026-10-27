"""Read-only audit UI.

Every field is projected by name. No conversation key, owner, fingerprint,
credential, raw request facts or usage contents ever leaves. Since turn
capture, the scrubbed and capped text the router chose to keep does: a
session's title, preview and workspace, each turn's prompt, the text fields
of the state Jev was shown, the turn's assistant summary and tool calls, and
notes left through session feedback (scrubbed when written).
"""

from __future__ import annotations

import math
import re
import time
from importlib.resources import files
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import RouterConfig
from .pins import VERDICTS
from .policy import trace
from .semantic import ADMISSION_FALLBACK, features_from_facts, replay_decision
from .sessions import (
    AUDIT_PROBLEMS,
    EVENT_ADMISSION,
    EVENT_EFFORT_PLAN,
    EVENT_EXECUTION,
    EVENT_USER_TURN,
    REQUEST_PROBLEMS,
    STATES,
    TURN_EVENTS,
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
    "session_mode", "user_turns", "tool_names", "languages",
)
# The two allowlisted facts that are strings. Both are written by the router.
FACT_LABELS = frozenset(["estimate_method", "session_mode"])
# The allowlisted facts that are lists of short labels, at most this many.
FACT_LISTS = frozenset(["tool_names", "languages"])
FACT_LIST_MAX = 20
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
    # Turn capture and session feedback.
    "title", "prompt_preview", "workspace_hint", "turn_count",
    "escalation_count", "corrective_count", "feedback_verdict",
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
        if key in FACT_LISTS:
            if isinstance(value, list):
                out[key] = [v for v in value[:FACT_LIST_MAX] if _label(v) is not None]
            continue
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
        # The user turn this request belongs to; NULL before turn capture.
        "turn_index": decision.get("turn_index"),
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
# Letters in any script, digits, spaces and ordinary punctuation. No `%`,
# backslash, angle brackets or control characters; the value only ever
# reaches the database as a bound parameter, and only through `instr`.
_SEARCH = re.compile(r"[\w .,:;!?'\"()\[\]/#@+=&*-]{1,80}")
FEEDBACK_FILTERS = (*VERDICTS, "unrated")
LIST_PARAMS = frozenset([
    "limit", "client", "model", "rule", "state", "adaptive", "problem", "pinned",
    "q", "window", "feedback",
])
LISTED_FIELDS = (
    "session_id", "alias", "client", "state", "created", "last_seen",
    "closed_at", "model", "base_effort", "effective_effort",
    "decision_rule", "quality_lane", "config_hash", "config_matches",
    "adaptation_mode", "tombstone", "title", "prompt_preview",
    "workspace_hint", "turn_count", "feedback_verdict",
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
        if not _SEARCH.fullmatch(search) or not search.strip():
            raise BadQuery("q must be 1 to 80 letters, digits, spaces or punctuation")
        filters["search"] = search.strip()
    feedback = params.get("feedback")
    if feedback is not None:
        if feedback not in FEEDBACK_FILTERS:
            raise BadQuery("feedback must be one of " + ", ".join(FEEDBACK_FILTERS))
        filters["feedback"] = feedback
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
    return JSONResponse(body, headers=HEADERS)


# --- user turns ----------------------------------------------------------------
#
# One entry per user turn: the admission, each turn plan, and each new user
# turn an execution request opened without a plan. Execution rows join their
# turn by `turn_index`; a row written before that column existed joins the
# newest turn row logged before it, and the response says `by_order`.

TURN_KINDS = {EVENT_ADMISSION: "admission", EVENT_EFFORT_PLAN: "plan", EVENT_USER_TURN: "user_turn"}
# The text fields of a stored state that may be shown, and nothing else.
STATE_TEXT_FIELDS = (
    "latest_user_request", "material_the_user_pasted",
    "earlier_request_in_this_conversation", "task_request",
    "current_user_request",
)
STATE_LIST_FIELDS = ("user_constraints", "quoted_material", "observations")
QUOTED_FIELDS = ("kind", "note", "content")
OBSERVATION_FIELDS = ("source", "kind", "summary", "status")
STATE_FACT_TEXT_MAX = 40
FEEDBACK_FIELDS = ("verdict", "note", "source", "ts", "turn_index")


def project_turn_state(raw: Any) -> dict[str, Any] | None:
    """The stored state's known text fields and its facts; nothing else.

    The text was scrubbed and capped when it was written. A fact survives
    when it is a flag, a number, a short label or a list of short labels.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in STATE_TEXT_FIELDS:
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    constraints = raw.get("user_constraints")
    if isinstance(constraints, list):
        out["user_constraints"] = [c for c in constraints if isinstance(c, str)]
    for key, fields in (("quoted_material", QUOTED_FIELDS), ("observations", OBSERVATION_FIELDS)):
        items = raw.get(key)
        if isinstance(items, list):
            out[key] = [
                {f: item[f] for f in fields if isinstance(item.get(f), str)}
                for item in items if isinstance(item, dict)
            ]
    facts: dict[str, Any] = {}
    raw_facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
    for key, value in raw_facts.items():
        if _label(key) is None:
            continue
        short = isinstance(value, str) and len(value) <= STATE_FACT_TEXT_MAX and _label(value)
        if value is None or isinstance(value, bool) or _is_number(value) or short:
            facts[key] = value
        elif isinstance(value, list):
            facts[key] = [v for v in value[:FACT_LIST_MAX] if _label(v) is not None]
    out["facts"] = facts
    return out


def _usage_numbers(usage: Any) -> tuple[Any, Any, Any]:
    """(input, output, cached input) from a chat or Responses usage block."""
    if not isinstance(usage, dict):
        return None, None, None

    def first(*keys: str) -> Any:
        for key in keys:
            if _is_number(usage.get(key)):
                return usage[key]
        return None

    cached = first("cached_input_tokens")
    for details in ("input_tokens_details", "prompt_tokens_details"):
        block = usage.get(details)
        if cached is None and isinstance(block, dict) and _is_number(block.get("cached_tokens")):
            cached = block["cached_tokens"]
    return (
        first("input_tokens", "prompt_tokens"),
        first("output_tokens", "completion_tokens"),
        cached,
    )


def _total(values: list[Any]) -> Any:
    """A sum, or None when no row reported the number. Never a made-up zero."""
    numbers = [v for v in values if _is_number(v)]
    return sum(numbers) if numbers else None


def _tokens_per_second(row: dict[str, Any]) -> float | None:
    output = _usage_numbers(row.get("upstream_usage"))[1]
    total, first = row.get("response_ms"), row.get("first_byte_ms")
    if not _is_number(output) or not _is_number(total) or not _is_number(first):
        return None
    return output * 1000.0 / max(total - first, 1.0)


def _turn_calls(raw: Any) -> tuple[list[dict[str, Any]] | None, list[str] | None]:
    """The tool calls and files a turn row recorded, checked for type."""
    if not isinstance(raw, dict):
        return None, None
    calls = [
        {"name": item["name"], "count": int(item["count"])}
        for item in raw.get("tool_calls") or []
        if isinstance(item, dict) and _label(item.get("name")) is not None
        and _is_number(item.get("count"))
    ]
    files = [f for f in raw.get("files_touched") or [] if isinstance(f, str)]
    return calls, files


def execution_rollup(rows: list[dict[str, Any]], turn_row: dict[str, Any]) -> dict[str, Any]:
    """What one turn's execution requests add up to. Gaps stay gaps."""
    usage = [_usage_numbers(row.get("upstream_usage")) for row in rows]
    calls, files = _turn_calls(turn_row.get("tool_calls_json"))
    summary = turn_row.get("assistant_summary")
    return {
        "http_requests": len(rows),
        "usage_reported": sum(1 for row in rows if row.get("upstream_usage")),
        "input_tokens": _total([u[0] for u in usage]),
        "output_tokens": _total([u[1] for u in usage]),
        "cached_input_tokens": _total([u[2] for u in usage]),
        "first_byte_ms_p50": percentiles([row.get("first_byte_ms") for row in rows])["p50"],
        "tps_p50": percentiles([_tokens_per_second(row) for row in rows])["p50"],
        "tool_calls": calls,
        "assistant_summary": summary if isinstance(summary, str) else None,
        "files_touched": files,
    }


def _jev(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("event_type") == EVENT_USER_TURN:
        return None  # Jev is not called for a user turn without a plan
    return {
        "latency_ms": row.get("jev_ms"),
        "tokens": row.get("jev_tokens"),
        "answers": row.get("answers") if isinstance(row.get("answers"), dict) else {},
        "shadow_answers": row.get("shadow_answers") if isinstance(row.get("shadow_answers"), dict) else None,
        "packet_version": row.get("packet_version"),
        "shadow_packet_version": row.get("shadow_packet_version"),
    }


def build_turns(
    decisions: list[dict[str, Any]],
    seqs: dict[int, int],
    plans: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Group a session's decision rows into user turns. Pure.

    `seqs` maps an execution row id to its request seq in `requests[]`.
    Returns the turns and how executions were joined: `by_index` when every
    execution row carries its turn, `by_order` when some predate the column.
    """
    openers = [d for d in decisions if d.get("event_type") in TURN_EVENTS]
    executions = [d for d in decisions if d.get("event_type") == EVENT_EXECUTION]
    indexed: list[tuple[int, dict[str, Any]]] = []
    for position, row in enumerate(openers, start=1):
        index = row.get("turn_index")
        indexed.append((index if isinstance(index, int) else position, row))
    indexes = [index for index, _row in indexed]

    members: dict[int, list[dict[str, Any]]] = {index: [] for index in indexes}
    by_order = False
    for row in executions:
        index = row.get("turn_index")
        if isinstance(index, int) and indexes:
            # The newest turn at or below the recorded one, so a turn row that
            # failed to write never strands its requests.
            target = max((i for i in indexes if i <= index), default=indexes[0])
        else:
            by_order = True
            earlier = [i for i, opener in indexed if opener["id"] < row["id"]]
            target = earlier[-1] if earlier else (indexes[0] if indexes else None)
        if target is not None:
            members.setdefault(target, []).append(row)

    plan_of = {p.get("plan_id"): p for p in plans}
    turns = []
    for index, row in indexed:
        kind = TURN_KINDS.get(row.get("event_type"), "admission")
        rows = members.get(index, [])
        entry: dict[str, Any] = {
            "turn_index": index,
            "kind": kind,
            "ts": row.get("ts"),
            "decision_row": row.get("id"),
            "user_prompt": row.get("user_prompt") if isinstance(row.get("user_prompt"), str) else None,
            "state": project_turn_state(row.get("state")),
            "state_shape": project_state_shape(row.get("state_shape")),
            "jev": _jev(row),
            "decision": {
                "model": row.get("model"),
                "effort": row.get("effort"),
                "rule": row.get("rule"),
                "route": row.get("route"),
                "quality_lane": row.get("quality_lane"),
                "fallback": bool(row.get("fallback")),
            },
            "plan": None,
            "execution_rollup": execution_rollup(rows, row),
            "requests": sorted(seqs[r["id"]] for r in rows if r["id"] in seqs),
            "feedback": [
                {key: item.get(key) for key in FEEDBACK_FIELDS}
                for item in feedback if item.get("turn_index") == index
            ],
        }
        if kind == "plan":
            plan = plan_of.get(row.get("decision_id"))
            entry["plan"] = _project_plan(plan) if plan else None
        turns.append(entry)
    return turns, ("by_order" if by_order else "by_index")


def _session_totals(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    executions = [d for d in decisions if d.get("event_type") == EVENT_EXECUTION]
    usage = [_usage_numbers(d.get("upstream_usage")) for d in executions]
    pairs = [(u[0], u[2]) for u in usage if _is_number(u[0]) and _is_number(u[2])]
    inputs = sum(p[0] for p in pairs)
    return {
        "request_count": len(executions),
        # Cached over input, from the rows that reported both. None, never
        # zero, when nothing was reported.
        "cache_hit_ratio": (sum(p[1] for p in pairs) / inputs) if pairs and inputs else None,
    }


async def audit_session(request: Request) -> Response:
    """One session: binding, turns, admission, requests, plans and the trace."""
    router = request.app.state.router
    session_id = request.path_params["session_id"]
    row = router.sessions.audit_session(session_id)
    if row is None:
        return JSONResponse({"error": "unknown session"}, status_code=404, headers=HEADERS)
    config = router.config
    admission = router.sessions.admission_row(session_id)
    decisions = router.sessions.decision_rows(session_id)
    judgments = {
        decision["decision_id"]: decision
        for decision in decisions
        if decision.get("event_type") == EVENT_EFFORT_PLAN
    }
    requests = router.sessions.request_rows(session_id)
    seqs = {
        item["decision"]["id"]: seq
        for seq, item in enumerate(requests, start=1)
        if item.get("decision")
    }
    plans = router.sessions.all_plans(session_id)
    feedback = router.sessions.feedback_rows(session_id)
    turns, joined = build_turns(decisions, seqs, plans, feedback)
    session = _project_session(row, config.config_hash)
    session.update(_session_totals(decisions))
    latest = feedback[-1] if feedback else None
    session["feedback"] = {key: latest.get(key) for key in FEEDBACK_FIELDS} if latest else None
    return JSONResponse({
        "config_hash": config.config_hash,
        "session": session,
        "turns": turns,
        "turn_join": joined,
        "admission": _project_admission(admission, config) if admission else None,
        "requests": [_project_request(seq, item) for seq, item in enumerate(requests, start=1)],
        "plans": [
            _project_plan(plan, judgments.get(plan.get("decision_id") or ""))
            for plan in plans
        ],
        "trace": explain_admission(admission, config, router.store),
    }, headers=HEADERS)
