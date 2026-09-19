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
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import effort as E
from . import protocols as P
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
    p_hard,
    validate_entry,
)
from .policy import finalize as policy_finalize
from .providers import ProviderBreakers, merge_pressures
from .quota import QuotaMonitor
from .semantic import SemanticResult
from .security import Ingress
from .sessions import ResolveRequest, SessionError, Sessions
from .state import is_short_continuation, turn_state_v1

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

# Turn questions that need a grounded harness observation behind them. Without
# one Jev would be asked to guess why work failed, so it is not asked at all,
# whether a config lists the question as active or as shadow.
GROUNDED_ONLY_QUESTIONS = ("failure_mode",)

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


@dataclass
class _TurnGuard:
    """One in-process waiting room per session and turn."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0


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
        # One waiting room per session and turn, so two concurrent plan
        # requests for one turn do not each buy a classifier call.
        self._turn_locks: dict[tuple[str, str], _TurnGuard] = {}
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
        observer: P.Observer | None = None,
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
                if observer is not None:
                    # Read-only. Whatever it makes of the chunk, the chunk
                    # below is the one the upstream sent, byte for byte.
                    observer.feed(chunk)
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
            if observer is not None:
                observer.finish()
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
        observer: P.Observer | None = None,
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
                upstream_response,
                decision_row,
                started=started,
                on_finish=on_finish,
                observer=observer,
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

        adaptation = config.adaptation_mode(
            alias_cfg, req.client_contract.turn_boundary_reporting
        )
        if adaptation != "off" and model not in config.adaptive_effort().qualified_profiles:
            # The experiment is on for this alias and this client, and the
            # model admission chose is not one anybody qualified. The session
            # is resolved with adaptation off rather than with a promise the
            # deployment cannot keep.
            adaptation = "off"
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
                # Agreed once, here, and stored. A session resolved before the
                # experiment was switched on never gains it, and one resolved
                # under it keeps its own ledger after it is switched off.
                "adaptation_mode": adaptation,
                "expected_effort": effort,
                "confirmed_effort": effort,
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

    # --- the between-turn effort experiment (spec 10) ---------------------

    async def turn_plan(self, request: Request, payload: Any) -> dict[str, Any]:
        """Prepare an effort-only decision for one new user turn.

        Nothing here executes anything. It decides among the efforts of the
        profile the session is already bound to, writes the decision down, and
        hands back either nothing to do or the exact protocol item the client
        must record. The model, the provider, the negotiated limits and the
        request-level base effort are not this endpoint's to change.
        """
        owner = self.session_owner(request)
        req = S.parse_turn_plan(payload)
        row = self.live_session(req.session_id, owner)
        if row["state"] == "blocked":
            raise SessionError(
                S.PROFILE_CHANGED,
                row["blocked_reason"] or "this binding is blocked",
                detail={"session_id": req.session_id, "state": "blocked"},
            )
        if not hmac.compare_digest(req.binding_revision, row["binding_revision"] or ""):
            raise SessionError(
                S.SESSION_CONFLICT,
                "the acknowledged binding is not the one this session holds",
                detail={"binding_revision": row["binding_revision"]},
            )

        mode = self.session_adaptation(row)
        evidence_fingerprint = req.evidence_fingerprint()

        stored = self.sessions.get_plan(req.session_id, turn_id=req.turn_id)
        if stored is not None:
            return _stored_plan(stored, row, mode, req, evidence_fingerprint)

        if mode == "off":
            # No per-turn classifier call at all. This is what a client that
            # asks anyway gets, and it is the same answer the router would
            # have given before the experiment existed.
            return _keep_response(
                req, row, mode, "the between-turn effort experiment is off for this session"
            )

        async with self._turn_guard(req.session_id, req.turn_id):
            # Whoever was ahead of us has finished by now, so ask again before
            # paying for a classifier call of our own.
            stored = self.sessions.get_plan(req.session_id, turn_id=req.turn_id)
            if stored is not None:
                row = self.live_session(req.session_id, owner)
                return _stored_plan(stored, row, mode, req, evidence_fingerprint)
            return await self._decide_turn(req, row, mode, evidence_fingerprint)

    @contextlib.asynccontextmanager
    async def _turn_guard(self, session_id: str, turn_id: str) -> AsyncIterator[None]:
        """One plan decision per session and turn at a time, in this process.

        Held across the Jev call on purpose, and holding no database
        transaction: the point is that a second caller waits for the first
        one's answer instead of buying a second classifier call for the same
        turn. It is not the correctness guarantee. The unique index on
        (session_id, turn_id) is, and it holds whether or not two callers ever
        met in one process.
        """
        key = (session_id, turn_id)
        entry = self._turn_locks.get(key)
        if entry is None:
            entry = self._turn_locks[key] = _TurnGuard()
        entry.waiting += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.waiting -= 1
            if entry.waiting <= 0:
                self._turn_locks.pop(key, None)

    async def _decide_turn(
        self,
        req: S.TurnPlanRequest,
        row: dict[str, Any],
        mode: str,
        evidence_fingerprint: str,
    ) -> dict[str, Any]:
        """One turn's effort decision, made once and written down once."""
        current = row["effective_effort"] or row["base_effort"]
        self._require_settled_turn(req)
        blocked_because = self._adaptation_blocked(req, row)
        if blocked_because:
            return _keep_response(req, row, mode, blocked_because, action="blocked")

        ladder, floor = self.turn_ladder(row)
        mcfg = self.config.models[row["model_key"]]
        strategy = mcfg.effort_control.between_turn
        started = time.perf_counter()
        semantics = await self.classify_turn(req, row)
        pressure = self.pressures().get(row["provider"] or "", 0.0)
        history = E.recommendation_history(self.sessions.plans(req.session_id))

        def decide(evidence: E.Evidence) -> E.TurnPlan:
            return E.plan_turn(
                cfg=self.config.adaptive_effort(),
                ladder=ladder,
                current=current,
                base=row["base_effort"],
                floor=floor,
                evidence=evidence,
                recommendations=history,
                pressure=pressure,
            )

        evidence = _turn_evidence(req, semantics)
        plan = decide(evidence)
        # What a shadow answer would have done, recorded and never applied.
        counterfactual = _shadow_counterfactual(decide, plan, evidence, semantics)
        hole = E.reconciliation_hole(self.sessions.plans(req.session_id))
        if hole is not None and plan.action == E.CHANGE:
            # Somebody confirmed an earlier update by hand and nobody could
            # record where it landed. Keeping the effort is still fine, and
            # the session executes as before; a further transition is not,
            # because its own anchor would be measured against a history the
            # ledger has a gap in.
            plan = E.blocked(
                plan,
                f"plan {hole['plan_id']} for turn {hole['turn_id']} was "
                "reconciled by hand and the router never saw where its update "
                "landed; a further effort change could not be checked against "
                "the history, so this session keeps the effort it has",
            )
        if mode == "shadow" and plan.action == E.CHANGE:
            # Shadow keeps the recommendation and sends nothing different.
            plan.action = E.KEEP
            plan.to_effort = plan.from_effort

        plan_id = new_decision_id()
        stored = self.sessions.record_plan(
            req.session_id,
            {
                "plan_id": plan_id,
                "turn_id": req.turn_id,
                "sequence": self.sessions.next_sequence(req.session_id),
                "mode": mode,
                "action": plan.action,
                "status": E.PLANNED,
                "from_effort": plan.from_effort,
                "to_effort": plan.to_effort,
                "base_effort": row["base_effort"],
                "recommendation": plan.recommendation,
                "expected_effort": plan.to_effort,
                "history_mode": req.context.history_mode,
                "request_fingerprint": evidence_fingerprint,
                "request_id": req.request_id,
                "reason": plan.reason[:500],
                "facts": {
                    **plan.to_facts(),
                    "strategy": strategy,
                    "protocol": row["protocol"],
                    "shadow_counterfactual": counterfactual,
                },
                "jev_ms": semantics.jev_ms,
                "plan_ms": (time.perf_counter() - started) * 1000,
            },
        )
        if stored.get("plan_id") != plan_id:
            # Another request planned this turn first. Its plan is the one
            # this turn has; ours is discarded, never written on top (F07).
            return _stored_plan(stored, row, mode, req, evidence_fingerprint)

        self._log_effort_plan(row, req, stored, plan, semantics)
        if plan.action == E.CHANGE:
            self.sessions.update(
                row["session_id"], row["version"], expected_effort=plan.to_effort
            )
        log.info(
            "turn plan session=%s turn=%s mode=%s action=%s %s -> %s reason=%s",
            req.session_id,
            req.turn_id,
            mode,
            plan.action,
            plan.from_effort,
            plan.to_effort,
            plan.reason,
        )
        return _plan_response(stored, row, mode)

    def _require_settled_turn(self, req: S.TurnPlanRequest) -> None:
        """No plan while anything about the last turn is still moving."""
        previous = req.previous_turn
        if self.sessions.inflight(req.session_id):
            raise SessionError(
                S.TURN_NOT_SETTLED,
                "an execution for this session is in flight; a turn boundary is "
                "not a boundary until the turn has finished",
            )
        if not previous.id:
            return  # the first turn of a session has no previous one
        if not previous.settled:
            raise SessionError(
                S.TURN_NOT_SETTLED,
                f"the client reports turn {previous.id!r} is not settled",
            )
        if previous.pending_tool_calls or previous.active_requests:
            raise SessionError(
                S.TURN_NOT_SETTLED,
                f"turn {previous.id!r} still has "
                f"{previous.pending_tool_calls} pending tool call(s) and "
                f"{previous.active_requests} active request(s)",
            )

    def _adaptation_blocked(
        self, req: S.TurnPlanRequest, row: dict[str, Any]
    ) -> str:
        """Why this turn may not adapt, or an empty string.

        Blocked means the model, the limits and the effective effort stay
        exactly where they are and the ledger keeps every update already
        applied. It is not an error and it is not a reset.
        """
        if row.get("effort_lineage") == "unknown":
            return (
                "the lineage or usage of an earlier response could not be read; "
                "reconcile the open plan before adapting again"
            )
        open_row = E.open_change(self.sessions.plans(req.session_id))
        if open_row is not None:
            if open_row["status"] == E.OUTCOME_UNKNOWN:
                raise SessionError(
                    S.EXECUTION_OUTCOME_UNKNOWN,
                    f"plan {open_row['plan_id']} was submitted and its outcome is "
                    "unknown; reconcile it before planning another change",
                    detail={"plan_id": open_row["plan_id"]},
                )
            return (
                f"plan {open_row['plan_id']} for turn {open_row['turn_id']} is "
                f"still {open_row['status']}; one change at a time"
            )
        mcfg = self.config.models.get(row["model_key"] or "")
        if mcfg is None:
            return f"model {row['model_key']!r} is no longer configured"
        if row["model_key"] not in self.config.adaptive_effort().qualified_profiles:
            return f"{row['model_key']} is not a qualified profile for this experiment"
        if mcfg.effort_control.between_turn == "fixed":
            return f"{row['model_key']} declares no between-turn effort strategy"
        budget = _context_headroom(req, row, self.config.adaptive_effort())
        if budget:
            return budget
        return ""

    def turn_ladder(self, row: dict[str, Any]) -> tuple[list[str], str | None]:
        """The rungs this session may move between, and the floor under them.

        The ladder is the profile's configured rungs, kept inside the alias
        cap, the client minimum and the quality lane the admission rule named.
        Capability and adequacy are different filters and both apply.
        """
        model = row["model_key"] or ""
        rungs = self.config.effort_ladder(model)
        alias_cfg = self.config.aliases.get(row["alias"] or "")
        client_cfg = self.config.clients.get(row["client"] or "")
        order = self.config.settings.effort_order
        floor = client_cfg.min_effort if client_cfg else None
        cap = alias_cfg.max_effort if alias_cfg else None

        if alias_cfg is not None:
            ruleset = self.config.ruleset_for(alias_cfg)
            for rule in ruleset.rules:
                if rule.name == row["decision_rule"] and rule.protected:
                    # A protected rule exists because the request needs it.
                    # Whatever it admitted is a floor, not a starting point.
                    floor = _stronger(order, floor, row["base_effort"])
            if row["decision_rule"] == "low_confidence":
                floor = _stronger(order, floor, row["base_effort"])

        lane = self.config.quality_lanes.get(row["quality_lane"] or "")
        keep = []
        for effort in rungs:
            if floor and _rank_of(order, effort) < _rank_of(order, floor):
                continue
            if cap and _rank_of(order, effort) > _rank_of(order, cap):
                continue
            if lane is not None and not lane.allows(model, effort):
                continue
            keep.append(effort)
        return keep, (floor if floor in keep else (keep[0] if keep else None))

    async def classify_turn(
        self, req: S.TurnPlanRequest, row: dict[str, Any]
    ) -> SemanticResult:
        """One Jev call about one turn. It chooses nothing.

        `classify`, never `decide`: the cross-model policy is not run here, so
        there is no selected model for a turn assessment to replace.
        """
        cfg = self.config.adaptive_effort()
        alias_cfg = self.config.aliases.get(row["alias"] or "") or AliasCfg()
        scoped = alias_cfg.model_copy(update={"draft": None})
        state = turn_state_v1(req.state.model_dump())
        # `failure_mode` is only meaningful with a grounded observation behind
        # it. Without one it would be asked to guess, so it is not asked,
        # whichever list a config has put it in.
        grounded = bool(state["facts"].get("failure_observations_available"))

        def askable(ids: list[str]) -> list[str]:
            return [
                qid
                for qid in ids
                if qid in self.config.questions
                and (grounded or qid not in GROUNDED_ONLY_QUESTIONS)
            ]

        features = extract_features(
            {"messages": [{"role": "user", "content": req.state.current_user_request}]},
            {"x-router-client": row["client"] or ""},
        )
        decider = self.decider_for(row["alias"] or "")
        classify = getattr(decider, "classify", None)
        if classify is None:
            return SemanticResult(error="this decider cannot classify a turn")
        try:
            return await classify(
                features,
                scoped,
                state=state,
                question_ids=askable(cfg.questions),
                shadow_ids=askable(cfg.shadow_questions),
            )
        except Exception as exc:  # noqa: BLE001 - a classifier failure keeps the effort
            log.warning("turn classification failed: %s", type(exc).__name__)
            return SemanticResult(error=f"classifier error: {type(exc).__name__}")

    def _log_effort_plan(
        self,
        row: dict[str, Any],
        req: S.TurnPlanRequest,
        stored: dict[str, Any],
        plan: Any,
        semantics: SemanticResult,
    ) -> None:
        """One `effort_plan` row in the same decision log as everything else."""
        try:
            row_id = self.store.log_decision(
                decision_id=stored["plan_id"],
                alias=row["alias"] or "",
                client=row["client"] or "",
                conversation_key="",
                state_builder="turn_state_v1",
                answers=semantics.answers,
                features={
                    "alias": row["alias"],
                    "session_mode": "strict",
                    "event": "new_user_turn",
                    "history_mode": req.context.history_mode,
                    **plan.to_facts(),
                },
                config_hash=self.config.config_hash,
                model=row["model_key"] or "",
                effort=stored["to_effort"],
                rule=f"effort_{stored['action']}",
                mode=self.config.settings.mode,
                fallback=bool(semantics.error),
                pinned=False,
                jev_ms=semantics.jev_ms,
                jev_tokens=semantics.jev_input_tokens,
                est_tokens=0,
                message_count=0,
                state=semantics.state,
            )
            self.store.update_decision(
                row_id,
                intended_model=row["model_key"] or "",
                intended_effort=stored["recommendation"] or "",
                response_ms=stored.get("plan_ms"),
            )
            self.sessions.annotate_decision(
                row_id,
                event_type=S.EVENT_EFFORT_PLAN,
                session_id=req.session_id,
                turn_id=req.turn_id,
                request_id=req.request_id,
                quality_lane=row["quality_lane"],
            )
            self.sessions.annotate_semantics(
                row_id,
                packet_version=semantics.packet_version,
                shadow_packet_version=semantics.shadow_packet_version,
                shadow_answers=SemanticResult(shadow=semantics.shadow).shadow_values(),
                action=f"effort_{stored['action']}",
            )
            self.sessions.update_plan(stored["plan_id"], decision_id=stored["plan_id"])
        except Exception:  # noqa: BLE001 - diagnostics may never fail a request
            log.exception("could not log the effort plan")

    # --- managed execution -----------------------------------------------

    def session_adaptation(self, row: dict[str, Any]) -> str:
        """The mode this session runs under now.

        The session's own mode was agreed at resolve and is never raised
        afterwards. Config can only lower it: switching the experiment off, or
        taking the opt-in off the alias, stops new decisions at once. It does
        not erase the ledger and it does not reset the effective effort, which
        is what makes a rollback safe.
        """
        stored = row.get("adaptation_mode") or "off"
        if stored == "off":
            return "off"
        alias_cfg = self.config.aliases.get(row["alias"] or "")
        if alias_cfg is None:
            return "off"
        live = self.config.adaptation_mode(alias_cfg, turn_boundaries=True)
        if live == "off":
            return "off"
        return "shadow" if live == "shadow" else stored

    async def managed_execution(
        self, request: Request, raw: bytes, protocol: str = P.CHAT
    ) -> Response:
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
        if row["protocol"] != protocol:
            raise SessionError(
                S.UNSUPPORTED_PROFILE,
                f"this endpoint executes {protocol} bindings and this binding "
                f"speaks {row['protocol']}; the router does not convert formats",
            )
        mcfg = self.revalidate_profile(row)
        _check_model_name(body["model"], row)
        features = extract_features(body, dict(request.headers))
        check = _execution_budget(row, mcfg, features, body)
        pending, anchor = self.check_effort_contract(request, body, row, mcfg)

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
            return await self.forward_session(
                request, body, row, row_id, finish, pending=pending, anchor=anchor
            )
        except BaseException:
            finish("unknown")
            if pending is not None:
                self.advance_plan(pending, row, "unknown")
            raise

    # --- the effort contract on an execution request ----------------------

    def check_effort_contract(
        self,
        request: Request,
        body: dict[str, Any],
        row: dict[str, Any],
        mcfg: Any,
    ) -> tuple[dict[str, Any] | None, Any]:
        """Validate the effort history before a byte is forwarded.

        Returns the plan this request carries out, if any, and where its
        update sits. The router never inserts the item: the client owns the
        transcript, the router owns the plan, and this is where the two are
        checked against each other.

        It runs for any session that was resolved under the experiment, even
        after the experiment has been switched off, because a full replay that
        drops an update already in the provider's history would corrupt it.
        """
        if (row.get("adaptation_mode") or "off") == "off":
            return None, None
        if row["protocol"] != P.RESPONSES:
            return None, None

        session_id = row["session_id"]
        strategy = mcfg.effort_control.between_turn
        turn_id = request.headers.get("x-router-turn", "")
        plan_id = request.headers.get("x-router-turn-plan", "")
        pending = None
        expected: str | None = None

        if plan_id:
            pending = self.sessions.plan_by_id(plan_id)
            if pending is None or pending["session_id"] != session_id:
                raise SessionError(
                    S.SESSION_CONFLICT,
                    f"X-Router-Turn-Plan {plan_id!r} is not a plan this session holds",
                )
            if turn_id and pending["turn_id"] != turn_id:
                raise SessionError(
                    S.SESSION_CONFLICT,
                    "X-Router-Turn and X-Router-Turn-Plan name different turns",
                )
            retryable = (E.PLANNED, E.REJECTED)
            if pending["status"] in retryable:
                # Planned, or definitively refused before acceptance and now
                # being sent again with the history restored. A `keep` plan is
                # still this turn's plan; it simply asks for no item.
                if pending["action"] == E.CHANGE:
                    expected = pending["to_effort"]
            else:
                # The plan for this turn was already carried out. This is a
                # continuation of the same turn: it keeps the effective
                # effort and adds no fresh item.
                pending = None

        problem = P.check_compaction(body)
        if problem:
            raise SessionError(S.UNSUPPORTED_PROFILE, problem)

        if strategy == "request_parameter":
            wanted = expected or row["effective_effort"] or row["base_effort"]
            reasoning = body.get("reasoning")
            sent = reasoning.get("effort") if isinstance(reasoning, dict) else None
            if sent != wanted:
                raise SessionError(
                    S.EFFORT_HISTORY_MISMATCH,
                    f"this strategy sets reasoning.effort to the planned effective "
                    f"effort {wanted!r}, and the request carries {sent!r}",
                )
            return pending, None

        problem = P.check_base_effort(body, row["base_effort"])
        if problem:
            raise SessionError(S.EFFORT_HISTORY_MISMATCH, problem)

        items = P.input_items(body)
        ledger = self.sessions.applied_updates(session_id)
        if body.get("previous_response_id"):
            lineage = {row.get("last_response_id") or ""} | {
                plan.get("response_id") or "" for plan in self.sessions.plans(session_id)
            }
            check = P.validate_chain(
                items,
                ledger,
                previous_response_id=str(body["previous_response_id"]),
                expected=expected,
                lineage={item for item in lineage if item},
            )
        else:
            check = P.validate_full_history(items, ledger, expected=expected)
        if not check.ok:
            raise SessionError(S.EFFORT_HISTORY_MISMATCH, check.reason)
        return pending, check

    def advance_plan(
        self,
        plan: dict[str, Any],
        row: dict[str, Any],
        event: str,
        observation: Any = None,
    ) -> None:
        """Move one ledger row, and the session's efforts, on one fact.

        Acceptance is transport acceptance. Only a valid successful provider
        completion confirms an effort transition, and only a confirmed
        transition moves the session's effective and confirmed effort.
        """
        try:
            fresh = self.sessions.plan_by_id(plan["plan_id"]) or plan
            status = E.advance(fresh["status"], event)
            fields: dict[str, Any] = {"status": status}
            if observation is not None:
                if observation.response_id:
                    fields["response_id"] = observation.response_id
                if observation.parse_failed:
                    # Lineage is unknown, so the transition cannot be called
                    # confirmed and nothing further may move until somebody
                    # reconciles it.
                    status = E.advance(fresh["status"], "unknown")
                    fields["status"] = status
            if status == E.CONFIRMED and fresh["action"] == E.CHANGE:
                fields["confirmed_effort"] = fresh["to_effort"]
            self.sessions.update_plan(plan["plan_id"], **fields)
            if status != E.CONFIRMED or fresh["action"] != E.CHANGE:
                return
            current = self.sessions.get(row["session_id"]) or row
            self.sessions.update(
                row["session_id"],
                current["version"],
                effective_effort=fresh["to_effort"],
                confirmed_effort=fresh["to_effort"],
                expected_effort=fresh["to_effort"],
            )
            log.info(
                "effort confirmed session=%s plan=%s %s -> %s",
                row["session_id"],
                plan["plan_id"],
                fresh["from_effort"],
                fresh["to_effort"],
            )
        except Exception:  # noqa: BLE001 - ledger bookkeeping may never fail a reply
            log.exception("could not advance the effort ledger")

    def reconcile(
        self, plan: dict[str, Any], row: dict[str, Any], outcome: str
    ) -> dict[str, Any]:
        """Settle an ambiguous update. The store owns the rules; this logs it."""
        result = self.sessions.reconcile(plan, row, outcome)
        log.info(
            "reconciled plan=%s outcome=%s effective=%s",
            plan["plan_id"],
            outcome,
            result["effective_effort"],
        )
        return result

    def record_observation(self, row: dict[str, Any], observation: Any) -> None:
        """Keep the lineage a chain request will be checked against."""
        try:
            current = self.sessions.get(row["session_id"]) or row
            fields: dict[str, Any] = {}
            if observation.response_id:
                fields["last_response_id"] = observation.response_id
            if observation.parse_failed:
                fields["effort_lineage"] = "unknown"
            if fields:
                self.sessions.update(row["session_id"], current["version"], **fields)
        except Exception:  # noqa: BLE001 - telemetry may never fail a reply
            log.exception("could not record the response lineage")

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
        *,
        pending: dict[str, Any] | None = None,
        anchor: Any = None,
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
        if row["protocol"] == P.RESPONSES:
            # The Responses body is forwarded as it arrived apart from the
            # model field. Its effort lives in the request's own `reasoning`
            # block and in the update items, both of which are the client's to
            # write and this hop's to check, never to edit.
            content = _responses_body(body, self.config, entry)
        else:
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
            if pending is not None:
                self.advance_plan(
                    pending, row, "rejected" if settled else "unknown"
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
            if pending is not None:
                # Definitively refused before acceptance. The previous
                # confirmed state stands and the same plan can be retried.
                self.advance_plan(pending, row, "rejected")
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
            if pending is not None:
                # 2xx headers are transport acceptance and nothing more. The
                # anchor is recorded now so a later full replay can be checked
                # against the history this update was accepted against.
                self.advance_plan(pending, row, "accepted")
                self.sessions.update_plan(
                    pending["plan_id"],
                    anchor_position=getattr(anchor, "position", None),
                    anchor_prefix_hash=getattr(anchor, "prefix", "") or None,
                    parent_response_id=body.get("previous_response_id"),
                )

        observer = (
            P.Observer(
                sse="text/event-stream"
                in upstream_response.headers.get("content-type", "")
            )
            if accepted and row["protocol"] == P.RESPONSES
            else None
        )

        def on_finish(state: str) -> None:
            if not accepted:
                return
            if observer is not None:
                seen = observer.observation
                self.record_observation(row, seen)
                if pending is not None:
                    # Only a terminal `response.completed` confirms it. A
                    # clean socket EOF is not a provider completion, and a
                    # completion carrying tool calls is still a completion.
                    self.advance_plan(
                        pending,
                        row,
                        "completed" if seen.completed and state == "completed" else "unknown",
                        observation=seen,
                    )
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
        effective = row["effective_effort"] or row["base_effort"]
        if (row.get("adaptation_mode") or "off") != "off":
            headers["X-Router-Effective-Effort"] = (
                pending["to_effort"] if pending else (effective or "")
            )
        return self._respond(
            upstream_response,
            headers,
            0,
            decision_row,
            started=started,
            on_finish=on_finish,
            observer=observer,
        )


# --- turn-plan helpers ---------------------------------------------------


def _rank_of(order: list[str], effort: str | None) -> int:
    try:
        return order.index(effort or "")
    except ValueError:
        return -1


def _stronger(order: list[str], left: str | None, right: str | None) -> str | None:
    """The higher of two floors, either of which may be absent."""
    if left is None:
        return right
    if right is None:
        return left
    return left if _rank_of(order, left) >= _rank_of(order, right) else right


def _context_headroom(
    req: S.TurnPlanRequest, row: dict[str, Any], cfg: Any
) -> str:
    """Stop adapting before the agreed context safety limit (F18).

    The session keeps running on the same model at the same effort. Only the
    experiment stops, and the ledger is untouched.
    """
    total = req.context.estimated_total_tokens
    window = row["context_window"] or 0
    if not total or not window:
        return ""
    limit = int(window * cfg.thresholds.context_safety_fraction)
    if total < limit:
        return ""
    return (
        f"{total} tokens of an agreed {window} token window is at or past the "
        f"{cfg.thresholds.context_safety_fraction:.0%} safety limit; the "
        "experiment stops here and the binding is unchanged"
    )


def _turn_evidence(req: S.TurnPlanRequest, semantics: SemanticResult) -> E.Evidence:
    """Jev's active answers about this turn, as numbers the pure policy compares.

    Active answers only (C25). A shadow answer is measured and never read: it
    may not decide, and it may not decide by refusing either, which is what
    suppressing an upgrade would be. A config that wants `failure_mode` to
    count moves it from `experiments.adaptive_effort.shadow_questions` into
    `questions`, and then it arrives here as an active answer like any other.
    """
    if semantics.error:
        return E.Evidence(error=semantics.error)
    answers = semantics.answers

    def value(qid: str, kind: str) -> float | None:
        answer = answers.get(qid)
        if not isinstance(answer, dict) or answer.get("type") != kind:
            return None
        number = answer.get(kind)
        return float(number) if isinstance(number, (int, float)) else None

    return E.Evidence(
        difficulty=value("difficulty", "score"),
        p_hard=p_hard(answers.get("difficulty")),
        harm_if_wrong=value("harm_if_wrong", "noul"),
        corrective_followup=value("corrective_followup", "noul"),
        failure_mode=_choice(answers.get("failure_mode")),
        short_continuation=is_short_continuation(req.state.current_user_request),
    )


def _choice(answer: Any) -> str | None:
    """The label a Choice answer carried, or nothing at all."""
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None
    choice = answer.get("choice")
    return choice if isinstance(choice, str) else None


def _shadow_counterfactual(
    decide: Callable[[E.Evidence], E.TurnPlan],
    plan: E.TurnPlan,
    evidence: E.Evidence,
    semantics: SemanticResult,
) -> dict[str, Any] | None:
    """What the shadow `failure_mode` answer would have done, had it counted.

    Measured on the plan row and never applied. This is how the experiment
    earns its promotion: the same turn, the same active answers, and the
    difference the shadow answer alone would have made.
    """
    choice = _choice(semantics.shadow.get("failure_mode"))
    if choice is None:
        return None
    hypothetical = decide(replace(evidence, failure_mode=choice))
    return {
        "question": "failure_mode",
        "answer": choice,
        "action": hypothetical.action,
        "to_effort": hypothetical.to_effort,
        "recommendation": hypothetical.recommendation,
        "reason": hypothetical.reason[:500],
        "changed": (hypothetical.action, hypothetical.to_effort)
        != (plan.action, plan.to_effort),
    }


def _stored_plan(
    stored: dict[str, Any],
    row: dict[str, Any],
    mode: str,
    req: S.TurnPlanRequest,
    evidence_fingerprint: str,
) -> dict[str, Any]:
    """The plan this turn already has (F07).

    Repeating a plan request returns the decision that was already made, with
    no second classifier call. The same turn id carrying different evidence is
    a conflict, not a second plan: one change per user turn.
    """
    if stored["request_fingerprint"] != evidence_fingerprint:
        raise SessionError(
            S.SESSION_CONFLICT,
            "this turn id was already planned with different evidence",
            detail={"turn_id": req.turn_id, "plan_id": stored["plan_id"]},
        )
    return _plan_response(stored, row, mode)


def _plan_response(
    stored: dict[str, Any], row: dict[str, Any], mode: str
) -> dict[str, Any]:
    """What the client is told. An executable item only for a real change."""
    facts = stored.get("facts") or {}
    out: dict[str, Any] = {
        "schema_version": "1",
        "session_id": stored["session_id"],
        "turn_id": stored["turn_id"],
        "plan_id": stored["plan_id"],
        "action": stored["action"],
        "adaptation": mode,
        "model_key": row["model_key"],
        "base_effort": stored["base_effort"],
        "previous_effective_effort": stored["from_effort"],
        "next_effective_effort": stored["to_effort"],
        "reason": stored["reason"],
        "status": stored["status"],
    }
    if mode == "shadow":
        # What it would have done. There is nothing to execute, and the
        # request the client sends next is the fixed-effort one it was
        # already going to send.
        out["hypothetical_effort"] = stored["recommendation"]
    if stored["action"] != E.CHANGE:
        return out
    strategy = facts.get("strategy") or ""
    if strategy == "request_parameter":
        out["update"] = {
            "parameter": "reasoning.effort",
            "value": stored["to_effort"],
            "rule": "set the request parameter on the next request of this turn",
        }
        return out
    out["update"] = {
        "item": P.configuration_update(stored["to_effort"] or ""),
        "rule": (
            "insert this item immediately before the designated new user "
            "message, keep every earlier update where it is, and leave the "
            "request-level reasoning.effort at the base effort"
        ),
    }
    out["headers"] = {
        "X-Router-Turn": stored["turn_id"],
        "X-Router-Turn-Plan": stored["plan_id"],
    }
    return out


def _keep_response(
    req: S.TurnPlanRequest,
    row: dict[str, Any],
    mode: str,
    reason: str,
    action: str = E.KEEP,
) -> dict[str, Any]:
    """An answer with no plan behind it: nothing was decided, nothing stored."""
    current = row["effective_effort"] or row["base_effort"]
    return {
        "schema_version": "1",
        "session_id": req.session_id,
        "turn_id": req.turn_id,
        "plan_id": "",
        "action": action,
        "adaptation": mode,
        "model_key": row["model_key"],
        "base_effort": row["base_effort"],
        "previous_effective_effort": current,
        "next_effective_effort": current,
        "reason": reason,
        "status": "",
    }


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


def _responses_body(
    body: dict[str, Any], config: RouterConfig, entry: PlanEntry
) -> bytes:
    """The Responses body, with only the model field normalised.

    `apply_effort` would add the base effort to the body or the model name.
    Here the request already carries its own `reasoning` block, written by the
    client and checked before this point, so the effort is left exactly as it
    arrived. Nothing else is read, moved or removed.
    """
    upstream_id, _extra = apply_effort(config, entry.model, entry.effort)
    payload = dict(body)
    payload["model"] = upstream_id
    return json.dumps(payload).encode()


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


async def responses(request: Request) -> Response:
    """The Responses endpoint.

    A managed request executes against a binding whose profile really speaks
    this protocol; anything else is refused rather than converted. An
    unmanaged request takes the passthrough path it always took.
    """
    router: Router = request.app.state.router
    raw = await request.body()
    if _managed(request):
        return await router.managed_execution(request, raw, protocol=P.RESPONSES)
    return await _forward_unmanaged(router, request, raw)


async def compact_responses(request: Request) -> Response:
    """Standalone compaction. Refused for a session running the experiment.

    The ledger has to be able to find every update again at the position it
    was accepted at. A compaction the router did not perform and cannot see
    makes that impossible, so this is refused rather than allowed to corrupt
    the history (spec 10.3).
    """
    router: Router = request.app.state.router
    raw = await request.body()
    if _managed(request):
        owner = router.session_owner(request)
        session_id = request.headers.get("x-router-session", "")
        row = router.live_session(session_id, owner) if session_id else None
        if row is not None and (row.get("adaptation_mode") or "off") != "off":
            raise SessionError(
                S.UNSUPPORTED_PROFILE,
                "standalone compaction is not supported while the between-turn "
                "effort experiment is on for this session; its history has to "
                "stay findable by the update ledger",
            )
        raise SessionError(
            S.UNSUPPORTED_PROFILE,
            "a managed session executes on /v1/chat/completions or /v1/responses",
        )
    return await _forward_unmanaged(router, request, raw)


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


async def turn_plan(request: Request) -> Response:
    """Prepare an effort-only decision for a new user turn (experimental)."""
    router: Router = request.app.state.router
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise SessionError(S.INVALID_ROUTER_INPUT, "body must be JSON") from None
    return JSONResponse(await router.turn_plan(request, payload))


async def reconcile_turn_plan(request: Request) -> Response:
    """Say what really happened to an update whose outcome was unknown.

    The router cannot find this out for itself: a disconnected response may
    have run. Somebody who can check the provider says `applied` or
    `not_applied`, and only then does the ledger move again.
    """
    router: Router = request.app.state.router
    owner = router.session_owner(request)
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise SessionError(S.INVALID_ROUTER_INPUT, "body must be JSON") from None
    item = S.parse_reconcile(payload)
    plan_id = request.path_params["plan_id"]
    plan = router.sessions.plan_by_id(plan_id)
    if plan is None:
        raise SessionError(S.SESSION_UNKNOWN, f"unknown plan id {plan_id!r}")
    row = router.sessions.get(plan["session_id"], owner)
    if row is None:
        raise SessionError(S.SESSION_UNKNOWN, "this plan belongs to another owner")
    return JSONResponse(router.reconcile(plan, row, item.outcome))


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
        # Expected is what the newest plan asked for; confirmed is what a
        # provider completion carried. They are reported apart because they
        # are apart.
        "expected_effort": row.get("expected_effort") or row["base_effort"],
        "confirmed_effort": row.get("confirmed_effort") or row["base_effort"],
        "adaptation": router.session_adaptation(row),
        "adaptation_agreed": row.get("adaptation_mode") or "off",
        "effort_lineage": row.get("effort_lineage") or "known",
        "turn_plans": router.sessions.plans(row["session_id"], limit=10),
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
            return await _forward_unmanaged(router, request, await request.body())
        return await router.forward(request, request.stream())
    return await router.forward(request, None)


async def _forward_unmanaged(
    router: Router, request: Request, raw: bytes
) -> Response:
    """Forward a body that has already been read, alias check and all."""
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
        Route("/v1/responses", responses, methods=["POST"]),
        Route("/v1/responses/compact", compact_responses, methods=["POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/router/health", health, methods=["GET"]),
        Route("/router/providers", providers, methods=["GET"]),
        Route("/router/quota", quota, methods=["GET"]),
        Route("/router/decisions", decisions, methods=["GET"]),
        Route("/router/feedback", post_feedback, methods=["POST"]),
        Route("/router/feedback", list_feedback, methods=["GET"]),
        Route("/router/resolve", resolve_session, methods=["POST"]),
        Route("/router/turn-plan", turn_plan, methods=["POST"]),
        Route(
            "/router/turn-plan/{plan_id}/reconcile",
            reconcile_turn_plan,
            methods=["POST"],
        ),
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
