"""Read-only audit UI. No transcripts, debug state, credentials or feedback notes."""

from __future__ import annotations

import re
from importlib.resources import files
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import RouterConfig
from .policy import trace
from .semantic import ADMISSION_FALLBACK, features_from_facts, replay_decision
from .sessions import EVENT_EFFORT_PLAN

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


async def audit_sessions(request: Request) -> Response:
    """Recent strict sessions, newest first. Protected like every /router/* route."""
    router = request.app.state.router
    try:
        limit = int(request.query_params.get("limit", "50"))
        if not 1 <= limit <= 200:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "limit must be between 1 and 200"}, status_code=400, headers=HEADERS)
    running = router.config.config_hash
    sessions = []
    for row in router.sessions.audit_sessions(limit):
        item = _project_session(row, running)
        listed = {
            key: item[key] for key in (
                "session_id", "alias", "client", "state", "created", "last_seen",
                "closed_at", "model", "base_effort", "effective_effort",
                "decision_rule", "quality_lane", "config_hash", "config_matches",
                "adaptation_mode", "tombstone",
            )
        }
        listed["effort"] = listed.pop("base_effort")
        listed["rule"] = listed.pop("decision_rule")
        listed["requests"] = {
            str(k): int(v) for k, v in (row.get("requests") or {}).items()
        }
        sessions.append(listed)
    bookmarks = [
        {key: b.get(key) for key in ("slug", "label", "session_id", "created")}
        for b in router.sessions.bookmarks()
    ]
    return JSONResponse({
        "config_hash": running,
        "limit": limit,
        "bookmarks": bookmarks,
        "sessions": sessions,
    }, headers=HEADERS)


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
