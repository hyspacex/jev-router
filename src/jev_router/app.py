"""The HTTP shim.

Requests whose model is a router alias get a model chosen for them. Everything
else, on any path and any method, is forwarded as it arrived and streamed back
unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import RouterConfig
from .deciders import build_decider
from .deciders.base import Decision
from .features import Features, extract_features
from .feedback import FeedbackError
from .feedback import validate as validate_feedback
from .pins import Store, conversation_key, new_decision_id
from .policy import (
    PlanEntry,
    RoutingError,
    apply_effort,
    constrained_effort,
    eligible_models,
    guard_candidate,
    next_model_that_fits,
    validate_entry,
)
from .policy import finalize as policy_finalize
from .providers import ProviderBreakers, merge_pressures
from .quota import QuotaMonitor
from .security import Ingress

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
# Recomputed or owned by this hop.
DROP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length", "x-router-admin-token"}
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}

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

    async def aclose(self) -> None:
        await self.quota.stop()
        await self.upstream.aclose()
        await self.jev_client.aclose()
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
            self._stream(upstream_response, decision_row, started=started),
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
        return decision, pinned, row, decision_id, key

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


# --- endpoints ----------------------------------------------------------


async def chat_completions(request: Request) -> Response:
    router: Router = request.app.state.router
    raw = await request.body()
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


async def passthrough(request: Request) -> Response:
    """Anything else: forward as it arrived, streaming both ways."""
    router: Router = request.app.state.router
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
        Route("/{path:path}", passthrough, methods=methods),
    ]

    async def routing_error(_request: Request, exc: Exception) -> Response:
        return JSONResponse(
            {"error": {"type": "routing_error", "message": str(exc)}}, status_code=422
        )

    app = Starlette(
        routes=routes,
        lifespan=_lifespan,
        exception_handlers={RoutingError: routing_error},
    )
    app.add_middleware(Ingress, settings=config.settings)
    app.state.router = Router(config, store)
    return app
