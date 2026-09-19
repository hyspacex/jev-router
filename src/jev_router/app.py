"""The HTTP shim.

Requests whose model is a router alias get a model chosen for them. Everything
else, on any path and any method, is forwarded as it arrived and streamed back
unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import sessions as S
from .config import AliasCfg, RouterConfig
from .deciders import build_decider
from .deciders.base import Decision
from .features import Features, estimate_input, extract_features
from .feedback import FeedbackError
from .feedback import validate as validate_feedback
from .pins import Store, conversation_key, new_decision_id
from .policy import (
    PlanEntry,
    RoutingError,
    apply_effort,
    constrained_effort,
    context_budget,
    eligible_models,
    guard_candidate,
    next_model_that_fits,
    validate_entry,
)
from .policy import finalize as policy_finalize
from .providers import ProviderBreakers, merge_pressures
from .quota import QuotaMonitor
from .semantic import SemanticResult
from .security import Ingress
from .sessions import ResolveRequest, SessionError, Sessions

log = logging.getLogger("jev_router")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# How a client acknowledges a strict binding. They are router control headers:
# they are read here and never reach the model API.
SESSION_HEADERS = (
    "x-router-session",
    "x-router-binding",
    "x-router-request-id",
    "x-router-turn",
    "x-router-turn-plan",
)
# Recomputed or owned by this hop.
DROP_REQUEST_HEADERS = (
    HOP_BY_HOP | {"host", "content-length", "x-router-admin-token"} | set(SESSION_HEADERS)
)
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}

# What one reply may use when neither the deployment nor the client declares a
# ceiling. An allowance for the budget check, not a limit sent upstream.
DEFAULT_OUTPUT_TOKENS = 4096

PEEK_LIMIT = 512 * 1024  # bodies larger than this are streamed without inspection


def _is_retryable_status(status: int) -> bool:
    """A status that says "try somewhere else", not "your request is wrong"."""
    return status == 429 or status >= 500


@dataclass(frozen=True)
class ServedOutcome:
    intended: PlanEntry
    entry: PlanEntry
    index: int
    reason: str
    status: int
    accepted: bool


class Router:
    def __init__(self, config: RouterConfig, store: Store | None = None) -> None:
        self.config = config
        self.store = store or Store(config.settings.db_path, config.settings.log_state)
        self.started = time.time()
        timeout = httpx.Timeout(
            config.settings.upstream_read_timeout_s,
            connect=config.settings.upstream_connect_timeout_s,
        )
        self.upstream = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=config.settings.max_concurrent_requests
            ),
        )
        self.jev_client = httpx.AsyncClient(
            timeout=config.settings.jev_timeout_ms / 1000,
            limits=httpx.Limits(max_keepalive_connections=4),
        )
        self.breakers = ProviderBreakers(config)
        self.quota = QuotaMonitor(config)
        self.sessions = Sessions(self.store, config.session_routing)
        deps: dict[str, Any] = {
            "jev_client": self.jev_client,
            "pressures": self.pressures,
        }
        self.decider = build_decider(config.settings.decider, config, deps)
        self._alias_deciders: dict[str, Any] = {}
        for alias, acfg in config.aliases.items():
            if acfg.decider:
                self._alias_deciders[alias] = build_decider(acfg.decider, config, deps)

    def pressures(self) -> dict[str, float]:
        """One number per provider. In memory, no clock work, no I/O."""
        try:
            self.quota.refresh_staleness()
            return merge_pressures(self.config, self.quota.pressures(), self.breakers)
        except Exception:  # noqa: BLE001 - quota may never fail a request
            log.exception("could not read quota pressure, routing without it")
            return {}

    def quota_status(self) -> dict[str, str]:
        """One word per provider. Unknown is reported, never read as zero."""
        status = {name: "unknown" for name in self.config.provider_names()}
        try:
            for name, row in self.quota.report()["providers"].items():
                status[name] = row.get("status") or "unknown"
        except Exception:  # noqa: BLE001 - quota may never fail a request
            log.exception("could not read quota status")
        return status

    async def aclose(self) -> None:
        await self.quota.stop()
        await self.upstream.aclose()
        await self.jev_client.aclose()
        self.sessions.close()
        self.store.close()

    def decider_for(self, alias: str):
        return self._alias_deciders.get(alias, self.decider)

    # --- forwarding ----------------------------------------------------

    def _upstream_url(self, request: Request) -> str:
        base = self.config.settings.upstream_base_url.rstrip("/")
        url = base + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        return url

    def _forward_headers(self, request: Request) -> list[tuple[bytes, bytes]]:
        connection = {
            v.strip().lower() for v in request.headers.get("connection", "").split(",")
        }
        headers = [
            (k, v)
            for k, v in request.headers.raw
            if k.decode("latin-1").lower() not in DROP_REQUEST_HEADERS | connection
        ]
        if not any(k.lower() == b"accept-encoding" for k, _ in headers):
            headers.append((b"accept-encoding", b"identity"))
        return headers

    async def forward(
        self,
        request: Request,
        content: bytes | AsyncIterator[bytes] | None,
        extra_response_headers: dict[str, str] | None = None,
        decision_row: int | None = None,
    ) -> Response:
        headers = self._forward_headers(request)
        if isinstance(content, bytes):
            headers.append((b"content-length", str(len(content)).encode()))

        upstream_request = self.upstream.build_request(
            request.method,
            self._upstream_url(request),
            headers=headers,
            content=content,
        )
        try:
            upstream_response = await self.upstream.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            if decision_row:
                self.store.update_decision(decision_row, upstream_status=502)
            return JSONResponse(
                {
                    "error": {
                        "message": f"upstream request failed: {exc}",
                        "type": "router",
                    }
                },
                status_code=502,
            )

        return self._respond(
            upstream_response, extra_response_headers or {}, 0, decision_row
        )

    async def _stream(
        self,
        upstream_response: httpx.Response,
        capture_row: int | None,
        *,
        started: float | None = None,
        on_finish: Callable[[str], None] | None = None,
    ) -> AsyncIterator[bytes]:
        capture = (
            capture_row
            and "text/event-stream"
            not in upstream_response.headers.get("content-type", "")
        )
        buffer = bytearray() if capture else None
        limit = self.config.settings.capture_usage_max_bytes
        started = started if started is not None else time.perf_counter()
        first_byte = None
        count = 0
        state = "failed"
        try:
            async for chunk in upstream_response.aiter_raw():
                if chunk and first_byte is None:
                    first_byte = (time.perf_counter() - started) * 1000
                count += len(chunk)
                if buffer is not None:
                    if len(buffer) + len(chunk) <= limit:
                        buffer.extend(chunk)
                    else:
                        buffer = (
                            None  # never keep a truncated body or an oversized chunk
                        )
                yield chunk
            state = "completed"
        except (asyncio.CancelledError, GeneratorExit):
            state = "disconnected"
            raise
        finally:
            if capture_row:
                self.store.update_decision(
                    capture_row,
                    stream_state=state,
                    first_byte_ms=first_byte,
                    response_ms=(time.perf_counter() - started) * 1000,
                    response_bytes=count,
                )
                if state == "completed" and buffer is not None:
                    self._record_usage(
                        capture_row,
                        bytes(buffer),
                        upstream_response.headers.get("content-encoding", ""),
                    )
            if on_finish is not None:
                # Transport completion is its own fact, separate from the 2xx
                # headers that accepted the request.
                on_finish(state)
            await upstream_response.aclose()

    # --- forwarding an alias, with fallbacks ---------------------------

    async def forward_alias(
        self,
        request: Request,
        body: dict[str, Any],
        plan: list[PlanEntry],
        headers: dict[str, str],
        decision_row: int | None = None,
        *,
        alias: str,
        features: Features,
    ) -> tuple[Response, ServedOutcome]:
        """Try the plan in order. Return the response and which entry served it.

        An entry is skipped when its provider's breaker is open. An entry is
        abandoned when the upstream answers 429 or 5xx, refuses the
        connection, or times out. All of that happens before a single byte has
        reached the client, because nothing is streamed until a response has
        been accepted. Once bytes are flowing the model cannot change.
        """
        base_headers = self._forward_headers(request)
        url = self._upstream_url(request)
        failures: list[str] = []
        started = time.perf_counter()
        for candidate in plan:
            validate_entry(self.config, self.config.aliases[alias], features, candidate)
        if not plan:
            raise RoutingError("no eligible execution entries")

        for index, entry in enumerate(plan):
            provider = entry.provider or self.config.provider_of(entry.model)
            last_rung = index == len(plan) - 1
            why = "; ".join(failures)

            if not self.breakers.acquire(provider):
                if not last_rung:
                    failures.append(f"{entry.model}: {provider} breaker open")
                    continue
                # Nothing else left. Serving through an open breaker beats
                # refusing to serve at all.
                log.info("no healthy provider left, trying %s anyway", entry.model)

            content = self._alias_body(body, entry)
            headers_out = [
                *base_headers,
                (b"content-length", str(len(content)).encode()),
            ]
            upstream_request = self.upstream.build_request(
                request.method, url, headers=headers_out, content=content
            )
            try:
                upstream_response = await self.upstream.send(
                    upstream_request, stream=True
                )
            except httpx.HTTPError as exc:
                self.breakers.record_failure(provider, type(exc).__name__)
                failures.append(f"{entry.model}: {type(exc).__name__}")
                if not last_rung:
                    continue
                if decision_row:
                    self.store.update_decision(decision_row, upstream_status=502)
                error = JSONResponse(
                    {
                        "error": {
                            "message": f"upstream request failed: {exc}",
                            "type": "router",
                        }
                    },
                    status_code=502,
                )
                return error, ServedOutcome(
                    plan[0], entry, index, "; ".join(failures), 502, False
                )

            if _is_retryable_status(upstream_response.status_code):
                self.breakers.record_failure(
                    provider, f"HTTP {upstream_response.status_code}"
                )
                failures.append(f"{entry.model}: HTTP {upstream_response.status_code}")
                if not last_rung:
                    await upstream_response.aclose()
                    continue
                # The last rung's answer is the answer, however bad.
                why = "; ".join(failures)
            else:
                self.breakers.record_success(provider)

            outcome = ServedOutcome(
                plan[0],
                entry,
                index,
                why,
                upstream_response.status_code,
                200 <= upstream_response.status_code < 300,
            )
            actual_headers = dict(headers)
            wire_model, _ = apply_effort(self.config, entry.model, entry.effort)
            actual_headers.update(
                {"X-Router-Model": wire_model, "X-Router-Effort": entry.effort or ""}
            )
            return self._respond(
                upstream_response, actual_headers, index, decision_row, started=started
            ), outcome

        raise RoutingError("no eligible execution entries")

    def _alias_body(self, body: dict[str, Any], entry: PlanEntry) -> bytes:
        upstream_model, extra = apply_effort(self.config, entry.model, entry.effort)
        payload = dict(body)
        payload["model"] = upstream_model
        payload.pop("reasoning_effort", None)  # ours wins for an alias request
        payload.update(extra)
        return json.dumps(payload).encode()

    def _respond(
        self,
        upstream_response: httpx.Response,
        headers: dict[str, str],
        index: int,
        decision_row: int | None,
        *,
        started: float | None = None,
        on_finish: Callable[[str], None] | None = None,
    ) -> Response:
        if decision_row:
            self.store.update_decision(
                decision_row, upstream_status=upstream_response.status_code
            )
        connection = {
            v.strip().lower()
            for v in upstream_response.headers.get("connection", "").split(",")
        }
        extra = {k.lower(): v for k, v in headers.items()}
        if index:
            extra["x-router-fallback"] = str(index)
        raw_headers = [
            (k.lower(), v)
            for k, v in upstream_response.headers.raw
            if k.decode("latin-1").lower()
            not in DROP_RESPONSE_HEADERS | connection | extra.keys()
        ]
        raw_headers.extend(
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in extra.items()
        )
        response = StreamingResponse(
            self._stream(
                upstream_response, decision_row, started=started, on_finish=on_finish
            ),
            status_code=upstream_response.status_code,
            background=BackgroundTask(upstream_response.aclose),
        )
        response.raw_headers = raw_headers
        return response

    def _record_usage(self, row_id: int, body: bytes, encoding: str = "") -> None:
        try:
            for coding in reversed(
                [e.strip().lower() for e in encoding.split(",") if e.strip()]
            ):
                if coding == "identity":
                    continue
                if coding not in ("gzip", "deflate"):
                    return
                decoder = zlib.decompressobj(31 if coding == "gzip" else 15)
                body = decoder.decompress(
                    body, self.config.settings.capture_usage_max_bytes + 1
                )
                if (
                    len(body) > self.config.settings.capture_usage_max_bytes
                    or not decoder.eof
                    or decoder.unused_data
                ):
                    return
            usage = json.loads(body).get("usage")
        except (ValueError, AttributeError, zlib.error):
            return
        if isinstance(usage, dict):
            self.store.update_decision(row_id, upstream_usage=usage)

    # --- routing an alias ----------------------------------------------

    async def route_alias(
        self, alias: str, body: dict[str, Any], features: Features
    ) -> tuple[Decision, bool, int, str, str]:
        """The decision, whether it came from a pin, the row id, the decision
        id and the conversation key."""
        settings = self.config.settings
        alias_cfg = self.config.aliases[alias]
        key = conversation_key(
            features.auth_header,
            features.system_prompt,
            features.first_user_message,
            alias=alias,
            client=features.client,
        )
        pin = self.store.get_pin(key, settings.pin_ttl_seconds)
        pinned = False
        decision_id = new_decision_id()

        eligible = eligible_models(self.config, alias_cfg, features)
        if pin:
            model, effort, rule = pin["model"], pin["effort"], "pin"
            reason = "pinned conversation"
            decision_id = pin["decision_id"] or decision_id
            if model not in eligible:
                replacement = next_model_that_fits(
                    self.config, model, features, eligible
                )
                if replacement is None:
                    raise RoutingError(
                        "pinned model is incompatible and no replacement is eligible"
                    )
                old = self.config.models.get(model)
                rule = (
                    "repin_context"
                    if old and features.budget_tokens > old.context_window
                    else "repin_constraints"
                )
                reason = f"{model} is no longer eligible, using {replacement}"
                model = replacement
            effort = constrained_effort(self.config, alias_cfg, features, model, effort)
            if (model, effort) != (pin["model"], pin["effort"]):
                decision_id = new_decision_id()
                if rule == "pin":
                    rule = "repin_constraints"
            decision = Decision(
                model=model,
                effort=effort,
                rule=rule,
                reason=reason,
                fallback=False,
                state_builder=alias_cfg.state_builder,
                decider="pin",
            )
            pinned = True
        else:
            decision = await self.decider_for(alias).decide(features, alias_cfg)
            if decision.routing_error:
                raise RoutingError(decision.routing_error)
            decision.model, decision.effort = guard_candidate(
                self.config, alias_cfg, features, decision.model, decision.effort
            )

        row = self.store.log_decision(
            decision_id=decision_id,
            alias=alias,
            client=features.client,
            conversation_key=key,
            state_builder=decision.state_builder,
            answers=decision.answers,
            features=features.to_facts() | {"alias": alias},
            config_hash=self.config.config_hash,
            model=decision.model,
            effort=decision.effort,
            rule=decision.rule,
            mode=settings.mode,
            fallback=decision.fallback,
            pinned=pinned,
            jev_ms=decision.jev_ms,
            jev_tokens=decision.jev_input_tokens,
            est_tokens=features.est_tokens,
            message_count=features.message_count,
            state=decision.state,
            route=decision.route,
            pressures={k: round(v, 4) for k, v in decision.pressures.items() if v},
            shifted=[s.to_dict() for s in decision.shifts],
            reordered=decision.reordered,
        )
        self.record_semantics(row, decision, action="pin" if pinned else "route")
        return decision, pinned, row, decision_id, key

    def record_semantics(self, row_id: int, decision: Decision, action: str) -> None:
        """Put the packet, the lane and the counterfactual on the decision row.

        Values only, and never anything that could fail a request: a problem
        writing diagnostics is a lost diagnostic, not a lost answer.
        """
        try:
            self.sessions.annotate_semantics(
                row_id,
                packet_version=decision.packet_version,
                shadow_packet_version=decision.shadow_packet_version,
                shadow_answers=_shadow_values(decision),
                action=action,
                experiment_routes=[e.to_dict() for e in decision.experiments],
                quota_snapshot=self.quota_snapshot(),
                quota_status=self.quota_status(),
                counterfactual=decision.counterfactual,
                exclusions=decision.exclusions,
                evidence=decision.evidence,
                qualification_ref=decision.qualification_ref,
            )
        except Exception:  # noqa: BLE001 - diagnostics may never fail a request
            log.exception("could not record the semantic facts for this decision")

    def quota_snapshot(self) -> dict[str, Any]:
        """The last reading per provider, windows and all. Never a payload."""
        try:
            return {
                name: {
                    "status": row.get("status"),
                    "pressure": row.get("pressure"),
                    "age_seconds": row.get("age_seconds"),
                    "windows": row.get("windows") or [],
                }
                for name, row in self.quota.report()["providers"].items()
            }
        except Exception:  # noqa: BLE001 - quota may never fail a request
            log.exception("could not read the quota snapshot")
            return {}

    def plan_for(
        self,
        decision: Decision,
        chosen_model: str,
        chosen_effort: str | None,
        pinned: bool,
    ) -> list[PlanEntry]:
        """The rungs the forwarder may try for this request.

        A pinned conversation gets exactly one. Moving it to another model on
        a transient 429 would throw away the prompt cache and the style the
        conversation was pinned for. Only hard infeasibility permits a
        replacement; quota and transient failures never do.
        """
        head = PlanEntry(
            model=chosen_model,
            effort=chosen_effort,
            provider=self.config.provider_of(chosen_model),
        )
        if pinned or not decision.plan:
            return [head]
        if decision.plan[0].as_pair() != head.as_pair():
            return [head]  # shadow mode, or something else overrode the route
        return list(decision.plan)

    def effective_route(
        self, decision: Decision, alias: str, features: Features
    ) -> tuple[str, str | None]:
        """In shadow mode the decision is logged but the default is sent."""
        if self.config.settings.mode != "shadow":
            return decision.model, decision.effort
        result = policy_finalize(
            self.config,
            self.config.aliases[alias],
            self.config.settings.default_route,
            "shadow",
            "shadow mode",
            features,
        )
        return result.model, result.effort

    # --- strict sessions -------------------------------------------------

    def session_owner(self, request: Request) -> str:
        """Who is asking. A strict request needs the control credential, always.

        The read-only router endpoints accept a loopback peer when no token is
        configured. Strict control and execution requests do not: a session is
        scoped to the owner of that credential, so there has to be one.
        """
        cfg = self.config.session_routing
        if not cfg.enabled:
            raise SessionError(
                S.UNSUPPORTED_PROFILE,
                "strict session routing is not enabled in router.yaml",
            )
        env = self.config.settings.admin_token_env
        token = os.environ.get(env, "")
        if not cfg.require_control_token:
            return S.owner_key(token or "local-owner")
        if not token:
            raise SessionError(
                S.ROUTER_UNAUTHORIZED,
                f"strict sessions need the router control credential; set ${env}",
            )
        supplied = request.headers.get("x-router-admin-token", "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise SessionError(
                S.ROUTER_UNAUTHORIZED, "router control credential required"
            )
        return S.owner_key(token)

    def live_session(self, session_id: str, owner: str) -> dict[str, Any]:
        """A session that may still be used, or the reason it may not."""
        row = self.sessions.get(session_id, owner)
        if row is None:
            raise SessionError(
                S.SESSION_UNKNOWN,
                "this session id is unknown; resolve a new session rather than "
                "continuing on an unbound one",
            )
        if row["tombstone"] or row["state"] == "closed":
            raise SessionError(
                S.SESSION_CLOSED,
                f"this session is closed ({row['blocked_reason'] or 'closed'})",
                detail={"session_id": session_id, "state": "closed"},
            )
        if self.sessions.expired(row):
            self.sessions.close_session(session_id, "prepared contract expired")
            raise SessionError(
                S.SESSION_CLOSED,
                "the prepared contract expired before it was used",
                detail={"session_id": session_id, "state": "closed"},
            )
        return row

    async def resolve(self, request: Request, payload: Any) -> dict[str, Any]:
        """Resolve a fresh binding, or hand back the one this id already has."""
        owner = self.session_owner(request)
        req = S.parse_resolve(payload)
        existing = self.sessions.get(req.session_id, owner)
        if existing is not None:
            return self.restore(req, existing)
        if req.intent == "resume":
            raise SessionError(
                S.SESSION_UNKNOWN,
                "this session id is unknown; it was never resolved here, or it "
                "belongs to another owner",
            )
        return await self.admit(req, request, owner)

    def restore(self, req: ResolveRequest, row: dict[str, Any]) -> dict[str, Any]:
        """The stored contract. No Jev call, no new choice."""
        if row["tombstone"] or row["state"] == "closed":
            raise SessionError(
                S.SESSION_CLOSED,
                f"this session is closed ({row['blocked_reason'] or 'closed'})",
                detail={"session_id": row["session_id"], "state": "closed"},
            )
        if self.sessions.expired(row):
            self.sessions.close_session(row["session_id"], "prepared contract expired")
            raise SessionError(
                S.SESSION_CLOSED,
                "the prepared contract expired before it was used",
                detail={"session_id": row["session_id"], "state": "closed"},
            )
        if req.intent == "new" and row["fingerprint"] != req.idempotency_fingerprint():
            raise SessionError(
                S.SESSION_CONFLICT,
                "this session id was already resolved with a different request",
                detail={"session_id": row["session_id"]},
            )
        self.sessions.touch(row["session_id"])
        return S.contract(row, self.quota_status())

    async def admit(
        self, req: ResolveRequest, request: Request, owner: str
    ) -> dict[str, Any]:
        """Choose one profile, once, and write it down as a contract."""
        config = self.config
        alias_cfg = config.aliases.get(req.alias)
        if alias_cfg is None:
            raise SessionError(
                S.INVALID_ROUTER_INPUT, f"unknown alias {req.alias!r}"
            )
        if alias_cfg.session_mode != "strict":
            raise SessionError(
                S.INVALID_ROUTER_INPUT,
                f"alias {req.alias!r} is a legacy alias; only a strict alias "
                "resolves a session binding",
            )
        body = dict(req.request or {})
        _check_freshness(body, req.client_contract)

        headers = dict(request.headers)
        headers["x-router-client"] = req.client
        features = extract_features(body, headers)
        pool = self.candidate_pool(req, alias_cfg)
        scoped = alias_cfg.model_copy(update={"allowed_models": pool, "draft": None})

        # Exactly one classifier call, and no draft call: admission decides,
        # it does not execute.
        try:
            decision = await self.decider_for(req.alias).decide(features, scoped)
        except RoutingError as exc:
            raise SessionError(S.NO_SAFE_ADMISSION, str(exc)) from None
        if decision.routing_error:
            raise SessionError(S.NO_SAFE_ADMISSION, decision.routing_error)

        answers = decision.answers
        source = decision.decider or "jev"
        rule, reason = decision.rule, decision.reason
        if decision.fallback:
            # The classifier could not answer. A strict binding may only fall
            # back onto a profile this alias named for it, and the result is
            # as fixed as any other binding: recovery does not revisit it.
            model, effort = self.conservative(scoped, features, decision.reason)
            answers, source, rule = {}, "admission_fallback", "admission_fallback"
            reason = f"classifier unavailable: {decision.reason}"
        else:
            try:
                model, effort = guard_candidate(
                    config, scoped, features, decision.model, decision.effort
                )
            except RoutingError as exc:
                raise SessionError(S.NO_SAFE_ADMISSION, str(exc)) from None

        mcfg = config.models[model]
        if mcfg.protocol not in set(req.client_contract.protocols):
            raise SessionError(
                S.UNSUPPORTED_PROFILE,
                f"the resolved profile speaks {mcfg.protocol} and the client "
                f"offered {', '.join(sorted(req.client_contract.protocols))}",
            )
        envelope = config.envelope_limits(req.alias, alias_cfg)
        window, output = _negotiate(mcfg, envelope, req.limits)
        check = _admission_budget(body, features, req.limits, window, output)
        if not check.fits:
            raise SessionError(
                S.CONTEXT_BUDGET_EXCEEDED, check.reason, detail={"budget": check.to_facts()}
            )

        wire_model, _extra = apply_effort(config, model, effort)
        decision_id = new_decision_id()
        binding: dict[str, Any] = {
            "model_key": model,
            "provider": config.provider_of(model),
            "upstream_id": mcfg.upstream_id,
            "protocol": mcfg.protocol,
            "context_window": window,
            "max_output_tokens": output,
            "supports_tools": mcfg.supports_tools,
            "supports_vision": mcfg.supports_vision,
            "compatibility_revision": mcfg.compatibility_revision,
        }
        binding["binding_revision"] = S.binding_digest(binding)
        binding.update(
            {
                "session_id": req.session_id,
                "owner": owner,
                "client": req.client,
                "alias": req.alias,
                "wire_model": wire_model,
                "base_effort": effort,
                "effective_effort": effort,
                "effort_mode": "fixed",
                "envelope_model": envelope["model_name"] if envelope else None,
                "decision_id": decision_id,
                "decision_rule": rule,
                "decision_reason": reason[:500],
                "decision_source": source,
                "quality_lane": decision.lane or decision.route,
                "quota_changed_choice": int(decision.pressure_changed_the_outcome),
                "config_hash": config.config_hash,
                "state_builder": alias_cfg.state_builder,
                # What this decision could be replayed against later.
                "question_hash": S.fingerprint(
                    {qid: config.questions.get(qid) for qid in alias_cfg.questions}
                )[:16],
                "jev_model": config.settings.jev_model,
                "fingerprint": req.idempotency_fingerprint(),
                "estimate_method": check.estimate_method,
                "estimated_input_tokens": check.input_tokens,
                "reserve_tokens": check.reserve_tokens,
                "quota_status": json.dumps(self.quota_status()),
            }
        )
        row, created = self.sessions.create(binding)
        if not created:
            # Another admission of the same id committed first. Its binding is
            # the one that counts; this one is discarded, never overwritten.
            return self.restore(req, row)

        row_id = self.store.log_decision(
            decision_id=decision_id,
            alias=req.alias,
            client=req.client,
            conversation_key="",
            state_builder=alias_cfg.state_builder,
            answers=answers,
            features=features.to_facts()
            | {"alias": req.alias, "session_mode": "strict"}
            | check.to_facts(),
            config_hash=config.config_hash,
            model=model,
            effort=effort,
            rule=rule,
            mode=config.settings.mode,
            fallback=decision.fallback,
            pinned=False,
            jev_ms=decision.jev_ms,
            jev_tokens=decision.jev_input_tokens,
            est_tokens=features.est_tokens,
            message_count=features.message_count,
            state=decision.state,
            route=decision.route,
            pressures={k: round(v, 4) for k, v in decision.pressures.items() if v},
            shifted=[s.to_dict() for s in decision.shifts],
            reordered=decision.reordered,
        )
        self.store.update_decision(
            row_id, intended_model=decision.model or model, intended_effort=effort or ""
        )
        self.sessions.annotate_decision(
            row_id,
            event_type=S.EVENT_ADMISSION,
            session_id=req.session_id,
            request_id=req.request_id,
            quality_lane=decision.lane or decision.route,
        )
        self.record_semantics(row_id, decision, action=f"admitted:{rule}")
        log.info(
            "admitted session alias=%s model=%s effort=%s rule=%s source=%s"
            " window=%s output=%s reserve=%s",
            req.alias,
            model,
            effort,
            rule,
            source,
            check.context_window,
            check.output_tokens,
            check.reserve_tokens,
        )
        return S.contract(row, self.quota_status())

    def candidate_pool(self, req: ResolveRequest, alias_cfg: AliasCfg) -> list[str]:
        """The alias, the client and the client's candidate list, intersected."""
        pool = self.config.alias_pool(alias_cfg)
        if req.candidate_models is not None:
            unknown = sorted(
                m for m in req.candidate_models if m not in self.config.models
            )
            if unknown:
                raise SessionError(
                    S.INVALID_ROUTER_INPUT,
                    f"unknown candidate model(s): {', '.join(unknown)}",
                )
            pool = [m for m in pool if m in req.candidate_models]
        client_cfg = self.config.clients.get(req.client) if req.client else None
        if client_cfg and client_cfg.allowed_models is not None:
            pool = [m for m in pool if m in client_cfg.allowed_models]
        if not pool:
            raise SessionError(
                S.NO_SAFE_ADMISSION,
                "the alias, the client and the candidate list permit no common model",
            )
        return pool

    def conservative(
        self, alias_cfg: AliasCfg, features: Features, why: str
    ) -> tuple[str, str | None]:
        """The explicitly configured fallback, or no admission at all."""
        route = alias_cfg.admission_fallback
        if route is None:
            raise SessionError(
                S.NO_SAFE_ADMISSION,
                f"the classifier is unavailable ({why}) and this alias declares "
                "no admission_fallback to bind instead",
            )
        try:
            result = policy_finalize(
                self.config, alias_cfg, route, "admission_fallback", why, features
            )
        except RoutingError as exc:
            raise SessionError(
                S.NO_SAFE_ADMISSION,
                f"the configured admission_fallback cannot serve this request: {exc}",
            ) from None
        return result.model, result.effort

    # --- managed execution -----------------------------------------------

    async def managed_execution(self, request: Request, raw: bytes) -> Response:
        """Execute one request against a binding. One entry, no substitution."""
        owner = self.session_owner(request)
        session_id = request.headers.get("x-router-session", "")
        revision = request.headers.get("x-router-binding", "")
        request_id = request.headers.get("x-router-request-id", "")
        missing = [
            name
            for name, value in (
                ("X-Router-Session", session_id),
                ("X-Router-Binding", revision),
                ("X-Router-Request-Id", request_id),
            )
            if not value
        ]
        if missing:
            raise SessionError(
                S.INVALID_ROUTER_INPUT,
                f"a managed request needs {', '.join(missing)}; a strict session "
                "is not executed on an unacknowledged binding",
            )
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise SessionError(
                S.INVALID_ROUTER_INPUT,
                "a managed request body must be JSON naming a concrete model",
            )

        row = self.live_session(session_id, owner)
        if row["state"] == "blocked":
            raise SessionError(
                S.PROFILE_CHANGED,
                row["blocked_reason"] or "this binding is blocked",
                detail={"session_id": session_id, "state": "blocked"},
            )
        if not hmac.compare_digest(revision, row["binding_revision"] or ""):
            raise SessionError(
                S.SESSION_CONFLICT,
                "the acknowledged binding is not the one this session holds",
                detail={"binding_revision": row["binding_revision"]},
            )
        if row["protocol"] != "openai-chat":
            raise SessionError(
                S.UNSUPPORTED_PROFILE,
                f"strict forwarding is qualified for openai-chat only, and this "
                f"binding speaks {row['protocol']}",
            )
        mcfg = self.revalidate_profile(row)
        _check_model_name(body["model"], row)
        features = extract_features(body, dict(request.headers))
        check = _execution_budget(row, mcfg, features, body)

        if not self.sessions.claim(session_id):
            raise SessionError(
                S.SESSION_CONFLICT,
                "another execution for this session is already in flight",
            )
        try:
            body_fingerprint = S.fingerprint(body)
            prior = self.sessions.start_request(
                session_id, request_id, body_fingerprint
            )
            if prior is not None:
                _check_prior_attempt(prior, body_fingerprint)
                self.sessions.finish_request(session_id, request_id, "in_flight")
            row_id = self._log_execution(row, request_id, features, check)
        except BaseException:
            self.sessions.release(session_id)
            raise

        done = False

        def finish(status: str, upstream_status: int | None = None) -> None:
            nonlocal done
            if done:
                return
            done = True
            self.sessions.finish_request(
                session_id,
                request_id,
                status,
                upstream_status=upstream_status,
                decision_id=row["decision_id"],
            )
            self.sessions.release(session_id)

        try:
            return await self.forward_session(request, body, row, row_id, finish)
        except BaseException:
            finish("unknown")
            raise

    def revalidate_profile(self, row: dict[str, Any]) -> Any:
        """The same profile, still configured the way it was negotiated.

        A profile that was revoked, or whose configured limits fell below the
        contract, blocks the session. The binding is kept: the caller
        reconfigures, compacts or starts a new session deliberately.
        """

        def stop(why: str) -> NoReturn:
            self.sessions.block(row["session_id"], row["version"], why)
            raise SessionError(
                S.PROFILE_CHANGED,
                why,
                detail={"session_id": row["session_id"], "state": "blocked"},
            )

        mcfg = self.config.models.get(row["model_key"] or "")
        alias_cfg = self.config.aliases.get(row["alias"] or "")
        if mcfg is None:
            stop(f"model {row['model_key']!r} is no longer configured")
        if alias_cfg is None or row["model_key"] not in self.config.alias_pool(alias_cfg):
            stop(f"alias {row['alias']!r} no longer permits {row['model_key']!r}")
        if (
            self.config.provider_of(row["model_key"]) != row["provider"]
            or mcfg.upstream_id != row["upstream_id"]
        ):
            stop("the profile now names a different upstream model or account")
        if mcfg.protocol != row["protocol"]:
            stop(f"the profile now speaks {mcfg.protocol}, not {row['protocol']}")
        if mcfg.context_window < (row["context_window"] or 0):
            stop(
                f"the configured context window fell to {mcfg.context_window}, below "
                f"the negotiated {row['context_window']}"
            )
        if (
            row["max_output_tokens"]
            and mcfg.max_output_tokens
            and mcfg.max_output_tokens < row["max_output_tokens"]
        ):
            stop(
                f"the configured output ceiling fell to {mcfg.max_output_tokens}, below "
                f"the negotiated {row['max_output_tokens']}"
            )
        if row["supports_tools"] and not mcfg.supports_tools:
            stop("the profile no longer supports tools")
        if row["supports_vision"] and not mcfg.supports_vision:
            stop("the profile no longer supports images")
        return mcfg

    def _log_execution(
        self,
        row: dict[str, Any],
        request_id: str,
        features: Features,
        check: Any,
    ) -> int:
        """A managed request is logged exactly like an alias request."""
        row_id = self.store.log_decision(
            decision_id=row["decision_id"] or new_decision_id(),
            alias=row["alias"] or "",
            client=row["client"] or "",
            conversation_key="",
            state_builder=row["state_builder"] or "",
            answers={},
            features=features.to_facts()
            | {"alias": row["alias"], "session_mode": "strict"}
            | check.to_facts(),
            config_hash=self.config.config_hash,
            model=row["model_key"] or "",
            effort=row["base_effort"],
            rule="session_binding",
            mode=self.config.settings.mode,
            fallback=False,
            pinned=False,
            jev_ms=None,
            jev_tokens=None,
            est_tokens=features.est_tokens,
            message_count=features.message_count,
        )
        self.store.update_decision(
            row_id,
            intended_model=row["model_key"] or "",
            intended_effort=row["base_effort"] or "",
        )
        self.sessions.annotate_decision(
            row_id,
            event_type=S.EVENT_EXECUTION,
            session_id=row["session_id"],
            request_id=request_id,
            quality_lane=row["quality_lane"],
        )
        return row_id

    async def forward_session(
        self,
        request: Request,
        body: dict[str, Any],
        row: dict[str, Any],
        decision_row: int,
        finish: Callable[..., None],
    ) -> Response:
        """The one-entry execution plan.

        No fallback list, no breaker skipping and no decider: a 429, a 5xx or
        a timeout is reported to the caller on the bound profile. Quota has no
        say here either; it only ever chose which profile to admit.
        """
        entry = PlanEntry(
            model=row["model_key"],
            effort=row["base_effort"],
            provider=row["provider"] or self.config.provider_of(row["model_key"]),
        )
        content = self._alias_body(body, entry)
        headers_out = [
            *self._forward_headers(request),
            (b"content-length", str(len(content)).encode()),
        ]
        started = time.perf_counter()
        upstream_request = self.upstream.build_request(
            request.method,
            self._upstream_url(request),
            headers=headers_out,
            content=content,
        )
        provider = entry.provider
        # The breaker still records what happened; it just has nowhere to send
        # the request instead, so it never skips this entry.
        self.breakers.acquire(provider)
        try:
            upstream_response = await self.upstream.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            self.breakers.record_failure(provider, type(exc).__name__)
            # A refused connection never reached the provider. Anything else
            # may have, and an ambiguous attempt is never replayed for free.
            settled = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
            self.store.update_decision(
                decision_row,
                upstream_status=502,
                accepted=False,
                stream_state="rejected" if settled else "unknown",
            )
            finish("rejected" if settled else "unknown", 502)
            return JSONResponse(
                {
                    "error": {
                        "message": f"upstream request failed: {exc}",
                        "type": "router",
                    }
                },
                status_code=502,
            )

        status = upstream_response.status_code
        accepted = 200 <= status < 300
        if accepted:
            self.breakers.record_success(provider)
        elif _is_retryable_status(status):
            self.breakers.record_failure(provider, f"HTTP {status}")
        self.store.update_decision(
            decision_row,
            model=entry.model,
            effort=entry.effort,
            upstream_status=status,
            accepted=accepted,
            fallback_index=0,
            stream_state="accepted" if accepted else "rejected",
        )
        if not accepted:
            # A rejected first inference leaves the prepared contract exactly
            # as it was disclosed. Nothing is re-chosen.
            finish("rejected", status)
        else:
            if row["state"] == "prepared":
                self.sessions.activate(row["session_id"], row["version"])
            self.sessions.finish_request(
                row["session_id"],
                request.headers.get("x-router-request-id", ""),
                "accepted",
                upstream_status=status,
            )

        def on_finish(state: str) -> None:
            if not accepted:
                return
            finish(
                {"completed": "completed", "failed": "stream_failed"}.get(
                    state, "unknown"
                ),
                status,
            )

        headers = {
            "X-Router-Model": row["wire_model"] or "",
            "X-Router-Effort": row["base_effort"] or "",
            "X-Router-Session": row["session_id"],
            "X-Router-Binding": row["binding_revision"] or "",
            "X-Router-Session-State": "active" if accepted else row["state"],
            "X-Router-Decision": row["decision_id"] or "",
        }
        return self._respond(
            upstream_response,
            headers,
            0,
            decision_row,
            started=started,
            on_finish=on_finish,
        )


# --- session helpers ----------------------------------------------------


def _shadow_values(decision: Decision) -> dict[str, Any]:
    """Shadow answers as bare values, plus the ids that came back unusable."""
    out = SemanticResult(shadow=decision.shadow_answers).shadow_values()
    if decision.invalid_shadow:
        out["_invalid"] = list(decision.invalid_shadow)
    return out


def _check_freshness(body: dict[str, Any], contract: Any) -> None:
    """A fork carrying history is a continuation, not new independent work.

    Freshness is partly the client's word. What can be checked is checked:
    supplied assistant or tool items, and a previous-response reference, are
    evidence against it.
    """
    if body.get("previous_response_id"):
        raise SessionError(
            S.INVALID_ROUTER_INPUT,
            "a new session cannot carry a previous_response_id; that history "
            "belongs to the session it was created in",
        )
    roles = {
        str(m.get("role", ""))
        for m in body.get("messages") or []
        if isinstance(m, dict)
    }
    carried = sorted(roles & {"assistant", "tool", "function"})
    if carried:
        raise SessionError(
            S.INVALID_ROUTER_INPUT,
            f"a new session cannot carry {', '.join(carried)} history; resume the "
            "session it belongs to instead",
        )
    if not contract.fresh_execution_context:
        raise SessionError(
            S.INVALID_ROUTER_INPUT,
            "client_contract.fresh_execution_context must be true to admit new work",
        )


def _smallest(*values: int | None) -> int | None:
    present = [v for v in values if v]
    return min(present) if present else None


def _negotiate(mcfg: Any, envelope: dict[str, Any] | None, limits: Any) -> tuple[int, int | None]:
    """The limits both sides agree on: always the smaller of the two."""
    window = mcfg.context_window
    output = mcfg.max_output_tokens
    if envelope:
        window = min(window, envelope["context_window"])
        output = _smallest(output, envelope["max_output_tokens"])
    if limits.context_window:
        window = min(window, limits.context_window)
    if limits.max_output_tokens:
        output = _smallest(output, limits.max_output_tokens)
    return window, output


def _admission_budget(
    body: dict[str, Any],
    features: Features,
    limits: Any,
    window: int,
    output: int | None,
) -> Any:
    """`I + O + S <= C` on the negotiated limits.

    A client-supplied count may raise the estimate and never lower it, so a
    client cannot talk its way past a server check.
    """
    estimate = estimate_input(
        body, accumulated_input_tokens=limits.accumulated_input_tokens
    )
    inputs, method = estimate.tokens, estimate.method
    if (limits.estimated_input_tokens or 0) > inputs:
        inputs = limits.estimated_input_tokens
        method = limits.estimate_method or "client-estimate"
    return context_budget(
        context_window=window,
        input_tokens=inputs,
        output_tokens=output or features.max_tokens or DEFAULT_OUTPUT_TOKENS,
        requested_reserve=limits.reserve_tokens or 0,
        estimate_method=method,
        unknown=estimate.unknown,
    )


def _execution_budget(
    row: dict[str, Any], mcfg: Any, features: Features, body: dict[str, Any]
) -> Any:
    """The same hard constraints, revalidated against the same profile."""
    if features.has_tools and not mcfg.supports_tools:
        raise SessionError(
            S.UNSUPPORTED_PROFILE, "the bound profile does not support tools"
        )
    if features.has_images and not mcfg.supports_vision:
        raise SessionError(
            S.UNSUPPORTED_PROFILE, "the bound profile does not read images"
        )
    estimate = estimate_input(body)
    check = context_budget(
        context_window=row["context_window"],
        input_tokens=estimate.tokens,
        output_tokens=row["max_output_tokens"]
        or features.max_tokens
        or DEFAULT_OUTPUT_TOKENS,
        estimate_method=estimate.method,
        unknown=estimate.unknown,
    )
    if not check.fits:
        # The binding is preserved. The harness compacts against the window it
        # was given; the router does not grow it or pick a larger model.
        raise SessionError(
            S.CONTEXT_BUDGET_EXCEEDED, check.reason, detail={"budget": check.to_facts()}
        )
    return check


def _check_model_name(sent: str, row: dict[str, Any]) -> None:
    """The concrete model must be the one this binding resolved to."""
    names = {row["wire_model"], row["model_key"], row["upstream_id"]}
    if row["envelope_model"]:
        names.add(row["envelope_model"])
    if sent not in {n for n in names if n}:
        raise SessionError(
            S.SESSION_CONFLICT,
            f"model {sent!r} is not the bound profile ({row['wire_model']})",
            detail={"binding_revision": row["binding_revision"]},
        )


def _check_prior_attempt(prior: dict[str, Any], body_fingerprint: str) -> None:
    """What a repeated request id means. Never another call to the provider."""
    if prior.get("fingerprint") != body_fingerprint:
        raise SessionError(
            S.SESSION_CONFLICT,
            "this request id was used for a different request",
        )
    status = prior.get("status")
    if status == "completed":
        raise SessionError(
            S.REQUEST_ALREADY_COMPLETED,
            "this request already completed; the router keeps no response to "
            "replay, so ask the provider or start a new attempt",
        )
    if status != "rejected":
        raise SessionError(
            S.EXECUTION_OUTCOME_UNKNOWN,
            f"the previous attempt at this request id ended {status!r}; reconcile "
            "it before retrying rather than calling the provider again",
        )


# --- endpoints ----------------------------------------------------------


def _managed(request: Request) -> bool:
    """A request that acknowledges a strict binding, however badly."""
    return any(request.headers.get(name) for name in SESSION_HEADERS[:3])


async def chat_completions(request: Request) -> Response:
    router: Router = request.app.state.router
    raw = await request.body()
    # Before anything else, including the "a concrete model means passthrough"
    # branch. A strict session that slipped past here would skip its binding,
    # its policy and its log.
    if _managed(request):
        return await router.managed_execution(request, raw)
    try:
        body = json.loads(raw)
    except ValueError:
        return await router.forward(request, raw)
    if not isinstance(body, dict):
        return await router.forward(request, raw)

    model = body.get("model")
    if not isinstance(model, str) or not router.config.is_alias(model):
        return await router.forward(request, raw)  # byte for byte

    alias = model
    features = extract_features(body, dict(request.headers))
    decision, pinned, row, decision_id, key = await router.route_alias(
        alias, body, features
    )
    chosen_model, chosen_effort = router.effective_route(decision, alias, features)

    plan = router.plan_for(decision, chosen_model, chosen_effort, pinned)
    upstream_model, _extra = apply_effort(router.config, chosen_model, chosen_effort)

    headers = {
        "X-Router-Model": upstream_model,
        "X-Router-Effort": chosen_effort or "",
        "X-Router-Rule": decision.rule,
        "X-Router-Pinned": "true" if pinned else "false",
        "X-Router-Decision": decision_id,
    }
    if decision.route:
        headers["X-Router-Route"] = decision.route
    if router.config.settings.mode == "shadow":
        headers["X-Router-Shadow-Model"] = decision.model
    if decision.fallback:
        headers["X-Router-Decider-Fallback"] = "true"
    if decision.pressure_changed_the_outcome:
        headers["X-Router-Pressure"] = _pressure_header(decision)

    log.info(
        "alias=%s model=%s effort=%s rule=%s route=%s pinned=%s decider_fallback=%s"
        " jev_ms=%s pressure=%s shifted=%s reordered=%s",
        alias,
        chosen_model,
        chosen_effort,
        decision.rule,
        decision.route or "-",
        pinned,
        decision.fallback,
        None if decision.jev_ms is None else round(decision.jev_ms),
        _pressure_header(decision) or "-",
        ",".join(
            f"{s.question}{s.op}{s.base:g}->{s.effective:g}" for s in decision.shifts
        )
        or "-",
        decision.reordered,
    )

    response, outcome = await router.forward_alias(
        request,
        body,
        plan,
        headers,
        decision_row=row,
        alias=alias,
        features=features,
    )
    router.store.update_decision(
        row,
        model=outcome.entry.model,
        effort=outcome.entry.effort,
        intended_model=decision.model,
        intended_effort=decision.effort or "",
        upstream_status=outcome.status,
        accepted=outcome.accepted,
        fallback_index=outcome.index,
        fallback_reason=outcome.reason,
        stream_state="accepted" if outcome.accepted else "rejected",
    )
    # A valid reused pin keeps its original expiry, even during transient failures.
    if (
        outcome.accepted
        and not decision.fallback
        and router.config.settings.mode == "active"
        and (not pinned or decision.rule != "pin")
    ):
        router.store.set_pin(
            key, outcome.entry.model, outcome.entry.effort, decision_id
        )
    return response


def _pressure_header(decision: Decision) -> str:
    return ",".join(
        f"{name}={value:.2f}"
        for name, value in sorted(decision.pressures.items())
        if value
    )


async def list_models(request: Request) -> Response:
    router: Router = request.app.state.router
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in DROP_REQUEST_HEADERS | {"accept-encoding"}
    }
    base = router.config.settings.upstream_base_url.rstrip("/")
    try:
        upstream = await router.upstream.get(base + request.url.path, headers=headers)
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": {"message": f"upstream request failed: {exc}", "type": "router"}},
            status_code=502,
        )
    if upstream.status_code != 200:
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )
    try:
        payload = upstream.json()
    except ValueError:
        return Response(upstream.content, media_type="application/json")

    data = payload.get("data")
    if isinstance(data, list):
        known = {m.get("id") for m in data if isinstance(m, dict)}
        for alias, acfg in router.config.aliases.items():
            if alias in known:
                continue
            data.append(
                {
                    "id": alias,
                    "object": "model",
                    "owned_by": "jev-router",
                    "created": round(router.started),
                    "description": acfg.description,
                }
            )
    return JSONResponse(payload)


async def health(request: Request) -> Response:
    router: Router = request.app.state.router
    settings = router.config.settings
    return JSONResponse(
        {
            "status": "ok",
            "mode": settings.mode,
            "upstream": settings.upstream_base_url,
            "aliases": sorted(router.config.aliases),
            "models": sorted(router.config.models),
            "providers": sorted(router.config.provider_names()),
            "routes": sorted(router.config.routes),
            "unhealthy_providers": sorted(
                name
                for name in router.config.provider_names()
                if not router.breakers.healthy(name)
            ),
            "decider": settings.decider,
            "jev_key_present": bool(os.environ.get(settings.jev_api_key_env)),
            "uptime_s": round(time.time() - router.started, 1),
        }
    )


async def providers(request: Request) -> Response:
    """Circuit breaker state per provider. In memory, lost on restart."""
    router: Router = request.app.state.router
    return JSONResponse(
        {
            "providers": router.breakers.report(),
            "pressures": {k: round(v, 4) for k, v in router.pressures().items()},
        }
    )


async def quota(request: Request) -> Response:
    """Quota snapshots, pressures, staleness and the last error per provider."""
    router: Router = request.app.state.router
    report = router.quota.report()
    report["breaker_pressure"] = {
        name: round(router.breakers.pressure(name), 4)
        for name in router.config.provider_names()
    }
    report["effective_pressure"] = {
        k: round(v, 4) for k, v in router.pressures().items()
    }
    return JSONResponse(report)


async def decisions(request: Request) -> Response:
    router: Router = request.app.state.router
    try:
        limit = min(int(request.query_params.get("limit", 20)), 500)
    except ValueError:
        limit = 20
    return JSONResponse({"decisions": router.store.recent_decisions(limit)})


async def post_feedback(request: Request) -> Response:
    """Record what the caller thought of a routing decision."""
    router: Router = request.app.state.router
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse(
            {"error": {"message": "body must be JSON"}}, status_code=400
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"message": "body must be an object"}}, status_code=400
        )

    try:
        item = validate_feedback(router.config, payload)
    except FeedbackError as exc:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=400)

    decision_id = item.decision_id
    if decision_id == "last":
        client = request.headers.get("x-router-client", "")
        decision_id = router.store.last_decision_id(client or None) or ""
        if not decision_id:
            return JSONResponse(
                {"error": {"message": "no decisions have been logged yet"}},
                status_code=404,
            )
    elif router.store.get_decision(decision_id) is None:
        return JSONResponse(
            {"error": {"message": f"unknown decision_id {decision_id!r}"}},
            status_code=404,
        )

    row_id = router.store.add_feedback(
        decision_id=decision_id,
        verdict=item.verdict,
        better_model=item.better_model,
        better_effort=item.better_effort,
        note=item.note,
        source="api",
    )
    decision = router.store.get_decision(decision_id)
    return JSONResponse(
        {
            "ok": True,
            "id": row_id,
            "decision_id": decision_id,
            "verdict": item.verdict,
            "decision": {
                "alias": decision.get("alias") if decision else None,
                "model": decision.get("model") if decision else None,
                "effort": decision.get("effort") if decision else None,
                "rule": decision.get("rule") if decision else None,
            },
        }
    )


async def list_feedback(request: Request) -> Response:
    router: Router = request.app.state.router
    try:
        limit = min(int(request.query_params.get("limit", 20)), 500)
    except ValueError:
        limit = 20
    return JSONResponse({"feedback": router.store.recent_feedback(limit)})


async def resolve_session(request: Request) -> Response:
    """Resolve or restore the router-owned execution binding for a session."""
    router: Router = request.app.state.router
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise SessionError(S.INVALID_ROUTER_INPUT, "body must be JSON") from None
    return JSONResponse(await router.resolve(request, payload))


async def close_session(request: Request) -> Response:
    """End a session deliberately. The row stays as a tombstone."""
    router: Router = request.app.state.router
    owner = router.session_owner(request)
    session_id = request.path_params["session_id"]
    row = router.sessions.get(session_id, owner)
    if row is None:
        raise SessionError(S.SESSION_UNKNOWN, "this session id is unknown")
    router.sessions.close_session(session_id, "closed by the client")
    after = router.sessions.get(session_id, owner) or row
    return JSONResponse(
        {
            "session_id": session_id,
            "state": after["state"],
            "was": row["state"],
            "model_key": row["model_key"],
            "binding_revision": row["binding_revision"],
        }
    )


async def show_session(request: Request) -> Response:
    router: Router = request.app.state.router
    owner = router.session_owner(request)
    row = router.sessions.get(request.path_params["session_id"], owner)
    if row is None:
        raise SessionError(S.SESSION_UNKNOWN, "this session id is unknown")
    return JSONResponse(session_report(router, row))


async def list_sessions(request: Request) -> Response:
    router: Router = request.app.state.router
    router.session_owner(request)
    try:
        limit = min(int(request.query_params.get("limit", 20)), 500)
    except ValueError:
        limit = 20
    return JSONResponse(
        {"sessions": [session_report(router, row) for row in router.sessions.recent(limit)]}
    )


def session_report(router: Router, row: dict[str, Any]) -> dict[str, Any]:
    """What a person needs to see: identity, limits, state and why."""
    quota = row.get("quota_status")
    return {
        "session_id": row["session_id"],
        "alias": row["alias"],
        "client": row["client"],
        "state": row["state"],
        "tombstone": row["tombstone"],
        "model_key": row["model_key"],
        "provider": row["provider"],
        "wire_model": row["wire_model"],
        "protocol": row["protocol"],
        "context_window": row["context_window"],
        "max_output_tokens": row["max_output_tokens"],
        "binding_revision": row["binding_revision"],
        "base_effort": row["base_effort"],
        "effective_effort": row["effective_effort"],
        "effort_mode": row["effort_mode"] or "fixed",
        "adaptation": "off",
        "decision_id": row["decision_id"],
        "decision_rule": row["decision_rule"],
        "decision_reason": row["decision_reason"],
        "decision_source": row["decision_source"],
        "quality_lane": row["quality_lane"],
        "config_hash": row["config_hash"],
        "question_hash": row["question_hash"],
        "jev_model": row["jev_model"],
        "quota_at_admission": quota if isinstance(quota, dict) else {},
        "quota_now": router.quota_status(),
        "blocked_reason": row["blocked_reason"],
        "created": row["created"],
        "last_seen": row["last_seen"],
        "in_flight": router.sessions.inflight(row["session_id"]),
        "unresolved_requests": router.sessions.unresolved(row["session_id"]),
    }


async def passthrough(request: Request) -> Response:
    """Anything else: forward as it arrived, streaming both ways."""
    router: Router = request.app.state.router
    if _managed(request):
        # A binding executes where it was resolved. Another endpoint is not a
        # way around strict validation.
        raise SessionError(
            S.UNSUPPORTED_PROFILE,
            "a managed session executes on /v1/chat/completions",
        )
    if request.method in ("POST", "PUT", "PATCH"):
        length = request.headers.get("content-length")
        try:
            small = length is not None and 0 <= int(length) <= PEEK_LIMIT
        except ValueError:
            small = False
        if small:
            raw = await request.body()
            alias = _alias_in_body(router.config, raw)
            if alias:
                return JSONResponse(
                    {
                        "error": {
                            "message": (
                                f"model {alias!r} is a jev-router alias and only works on "
                                f"/v1/chat/completions; name a real model on this endpoint"
                            ),
                            "type": "invalid_request_error",
                            "param": "model",
                        }
                    },
                    status_code=400,
                )
            return await router.forward(request, raw)
        return await router.forward(request, request.stream())
    return await router.forward(request, None)


def _alias_in_body(config: RouterConfig, raw: bytes) -> str | None:
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    model = body.get("model") if isinstance(body, dict) else None
    if isinstance(model, str) and config.is_alias(model):
        return model
    return None


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    router: Router = app.state.router
    try:
        router.quota.start()
    except Exception:  # noqa: BLE001 - quota may never stop the router starting
        log.exception("could not start the quota poller, routing without it")
    try:
        yield
    finally:
        await router.aclose()


def create_app(config: RouterConfig, store: Store | None = None) -> Starlette:
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    routes = [
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/router/health", health, methods=["GET"]),
        Route("/router/providers", providers, methods=["GET"]),
        Route("/router/quota", quota, methods=["GET"]),
        Route("/router/decisions", decisions, methods=["GET"]),
        Route("/router/feedback", post_feedback, methods=["POST"]),
        Route("/router/feedback", list_feedback, methods=["GET"]),
        Route("/router/resolve", resolve_session, methods=["POST"]),
        Route("/router/sessions", list_sessions, methods=["GET"]),
        Route("/router/sessions/{session_id}", show_session, methods=["GET"]),
        Route("/router/sessions/{session_id}/close", close_session, methods=["POST"]),
        Route("/{path:path}", passthrough, methods=methods),
    ]

    async def routing_error(_request: Request, exc: Exception) -> Response:
        return JSONResponse(
            {"error": {"type": "routing_error", "message": str(exc)}}, status_code=422
        )

    async def session_error(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, SessionError)
        return JSONResponse(exc.body(), status_code=exc.status)

    app = Starlette(
        routes=routes,
        lifespan=_lifespan,
        exception_handlers={
            RoutingError: routing_error,
            SessionError: session_error,
        },
    )
    app.add_middleware(Ingress, settings=config.settings)
    app.state.router = Router(config, store)
    return app
