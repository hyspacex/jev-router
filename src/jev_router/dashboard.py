"""Read-only audit UI. No transcripts, debug state, credentials or feedback notes."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import RouterConfig

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
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}


async def dashboard(request: Request) -> Response:
    """Public, data-free shell: a browser cannot set an admin header on navigation."""
    return HTMLResponse(
        files("jev_router").joinpath("static/dashboard.html").read_text(),
        headers=HEADERS,
    )


async def asset(request: Request) -> Response:
    name = request.path_params["name"]
    media = {"dashboard.js": "text/javascript", "dashboard.css": "text/css"}
    if name not in media:
        return Response(status_code=404, headers=HEADERS)
    return Response(
        files("jev_router").joinpath(f"static/{name}").read_bytes(),
        media_type=media[name], headers=HEADERS,
    )


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
