"""The Jev decider: ask TypeSafe, then run the answers through the policy."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..config import AliasCfg, RouterConfig
from ..features import Features
from ..policy import (
    RoutingError,
    apply_effort,
    constrained_effort,
    eligible_models,
    evaluate,
    finalize,
)
from ..semantic import (
    SemanticResult,
    active_questions,
    packet_version,
    shadow_questions,
)
from ..state import build_state
from .base import Decider, Decision, build_decider, pressure_source, register_decider
from .validation import validate_response, validate_shadow

log = logging.getLogger("jev_router.jev")


class JevDecider:
    name = "jev"

    def __init__(
        self,
        config: RouterConfig,
        client: httpx.AsyncClient,
        fallback: Decider | None = None,
        pressures: Any = None,
    ) -> None:
        self.config = config
        self.client = client
        self.fallback = fallback
        self.pressures = pressure_source({"pressures": pressures})

    @property
    def api_key(self) -> str:
        return os.environ.get(self.config.settings.jev_api_key_env, "")

    async def draft(self, features: Features, alias_cfg: AliasCfg) -> str:
        """A short answer from a cheap model, for Jev to judge.

        Fails open: on any error, or any missing piece of config, this returns
        an empty string and the router decides without it. It is only reached
        when the alias turns it on, and a pinned conversation never reaches the
        decider at all, so a pinned turn never pays for this.
        """
        cfg = alias_cfg.draft
        if cfg is None or not cfg.enabled or not cfg.model:
            return ""
        # A draft is optional, but it is still an execution attempt. Unknown
        # or forbidden models must not bypass the alias/client boundary.
        try:
            if cfg.model not in eligible_models(self.config, alias_cfg, features):
                return ""
            effort = constrained_effort(
                self.config, alias_cfg, features, cfg.model, cfg.effort
            )
            upstream_id, extra = apply_effort(self.config, cfg.model, effort)
        except RoutingError:
            return ""
        request = (features.last_user_message or "").strip()
        if not request:
            return ""
        headers = {}
        if cfg.api_key_env:
            key = os.environ.get(cfg.api_key_env, "")
            if key:
                headers["Authorization"] = f"Bearer {key}"
        try:
            resp = await self.client.post(
                f"{self.config.settings.upstream_base_url.rstrip('/')}/v1/chat/completions",
                json={
                    "model": upstream_id,
                    "messages": [{"role": "user", "content": request[:6000]}],
                    "max_tokens": cfg.max_tokens,
                    **extra,
                },
                headers=headers,
                timeout=cfg.timeout_ms / 1000,
            )
            resp.raise_for_status()
            choice = (resp.json().get("choices") or [{}])[0]
            return ((choice.get("message") or {}).get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001 - the draft is never required
            log.info("draft step skipped: %s", _short_error(exc))
            return ""

    async def classify(
        self, features: Features, alias_cfg: AliasCfg
    ) -> SemanticResult:
        """Ask Jev what this turn is. No model is chosen here.

        This is the boundary that keeps turn assessment from reselecting a
        model by accident: it builds the packet, makes one call, validates the
        active answers exactly as it always has, and validates the shadow
        answers separately. A malformed shadow answer is dropped and counted;
        it can neither weaken the active validation nor reach the policy.
        """
        settings = self.config.settings
        state: Any = None
        started = time.perf_counter()
        active_ids = active_questions(self.config, alias_cfg)
        shadow_ids = shadow_questions(self.config, alias_cfg)

        def versions(builder: str) -> tuple[str, str]:
            return (
                packet_version(self.config, active_ids, builder),
                packet_version(self.config, shadow_ids, builder) if shadow_ids else "",
            )

        active_version, shadow_version = versions(alias_cfg.state_builder)
        try:
            state = build_state(alias_cfg.state_builder, features, self.config)
            draft = await self.draft(features, alias_cfg)
            if draft and isinstance(state, dict) and alias_cfg.draft is not None:
                state[alias_cfg.draft.state_field] = draft
            questions = {qid: self.config.question(qid) for qid in active_ids}
            shadow = {qid: self.config.question(qid) for qid in shadow_ids}
            blank = SemanticResult(
                state=state,
                state_builder=alias_cfg.state_builder,
                packet_version=active_version,
                shadow_packet_version=shadow_version,
            )
            if not self.api_key:
                blank.error = "no Jev API key set"
                return blank
            if not questions:
                blank.error = "alias asks no questions"
                return blank
            resp = await self.client.post(
                settings.jev_url,
                json={
                    "model": settings.jev_model,
                    "state": state,
                    # One batched call. The shadow questions ride along so the
                    # experiment costs one round trip, not two.
                    "questions": {**questions, **shadow},
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=settings.jev_timeout_ms / 1000,
            )
            resp.raise_for_status()
            data = resp.json()
            answers, input_tokens = validate_response(data, questions)
            shadow_answers, invalid = validate_shadow(data, shadow)
            if invalid:
                log.info("dropped %d malformed shadow answer(s)", len(invalid))
            return SemanticResult(
                answers=answers,
                shadow=shadow_answers,
                invalid_shadow=invalid,
                state=state,
                state_builder=alias_cfg.state_builder,
                packet_version=active_version,
                shadow_packet_version=shadow_version,
                jev_ms=(time.perf_counter() - started) * 1000,
                jev_input_tokens=input_tokens,
            )
        except RoutingError:
            raise
        except Exception as exc:  # timeout, 429, 529, bad JSON, anything
            elapsed = (time.perf_counter() - started) * 1000
            reason = _short_error(exc)
            log.warning("jev call failed after %.0f ms: %s", elapsed, reason)
            return SemanticResult(
                state=state,
                state_builder=alias_cfg.state_builder,
                packet_version=active_version,
                shadow_packet_version=shadow_version,
                jev_ms=elapsed,
                error=reason,
            )

    async def decide(self, features: Features, alias_cfg: AliasCfg) -> Decision:
        """Classify the turn, then run the cross-model policy over the answers."""
        def refused(message: str) -> Decision:
            return Decision(
                model="",
                effort=None,
                rule="routing_error",
                reason=message,
                fallback=True,
                routing_error=message,
                decider=self.name,
            )

        try:
            semantics = await self.classify(features, alias_cfg)
        except RoutingError as exc:
            return refused(str(exc))
        if not semantics.ok:
            decision = await self._fallback(
                features,
                alias_cfg,
                semantics.state,
                semantics.error,
                jev_ms=semantics.jev_ms,
            )
            decision.packet_version = semantics.packet_version
            return decision
        try:
            pressures = self.pressures()
            result = evaluate(
                self.config, alias_cfg, semantics.answers, features, pressures
            )
        except RoutingError as exc:
            return refused(str(exc))
        except Exception as exc:  # the policy itself broke; never fail the request
            reason = f"policy error: {type(exc).__name__}"
            log.warning("policy evaluation failed: %s", reason)
            return await self._fallback(
                features, alias_cfg, semantics.state, reason, jev_ms=semantics.jev_ms
            )
        return Decision(
            model=result.model,
            effort=result.effort,
            rule=result.rule,
            reason=result.reason,
            answers=semantics.answers,
            shadow_answers=semantics.shadow,
            invalid_shadow=semantics.invalid_shadow,
            packet_version=semantics.packet_version,
            shadow_packet_version=semantics.shadow_packet_version,
            jev_ms=semantics.jev_ms,
            jev_input_tokens=semantics.jev_input_tokens,
            fallback=False,
            state=semantics.state,
            state_builder=semantics.state_builder,
            notes=result.notes,
            decider=self.name,
            route=result.route,
            plan=result.plan,
            pressures=dict(pressures),
            shifts=result.shifts,
            reordered=result.reordered,
            pressure_changed_the_outcome=result.pressure_changed_the_outcome,
            experiments=result.experiments,
        )

    async def _fallback(
        self,
        features: Features,
        alias_cfg: AliasCfg,
        state: Any,
        reason: str,
        jev_ms: float | None = None,
    ) -> Decision:
        if self.fallback is not None:
            try:
                decision = await self.fallback.decide(features, alias_cfg)
                decision.fallback = True
                decision.answers = {}  # heuristic answers are not Jev replay evidence
                decision.jev_ms = jev_ms
                decision.state = state
                decision.state_builder = alias_cfg.state_builder
                decision.reason = f"{reason}; {decision.reason}"
                return decision
            except Exception as exc:
                log.warning("fallback decider failed: %s", type(exc).__name__)

        route = self.config.settings.default_route
        try:
            result = finalize(
                self.config, alias_cfg, route, "fallback", reason, features
            )
        except Exception as exc:
            # Still return a Decision; the HTTP boundary reports infeasibility
            # rather than executing a candidate that bypasses hard constraints.
            message = (
                str(exc) if isinstance(exc, RoutingError) else "local routing failed"
            )
            return Decision(
                model="",
                effort=None,
                rule="routing_error",
                reason=reason,
                fallback=True,
                routing_error=message,
                jev_ms=jev_ms,
                state_builder=alias_cfg.state_builder,
                decider=self.name,
            )
        return Decision(
            model=result.model,
            effort=result.effort,
            rule="fallback",
            reason=reason,
            jev_ms=jev_ms,
            fallback=True,
            state=state,
            state_builder=alias_cfg.state_builder,
            notes=result.notes,
            decider=self.name,
            route=result.route,
            plan=result.plan,
        )


def _short_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"jev returned {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "jev timed out"
    return f"jev error: {type(exc).__name__}"


@register_decider("jev")
def _make_jev(config: RouterConfig, deps: dict[str, Any]) -> Decider:
    fallback_name = config.settings.fallback_decider
    fallback = None
    if fallback_name and fallback_name != "jev":
        fallback = build_decider(fallback_name, config, deps)
    return JevDecider(config, deps["jev_client"], fallback, pressure_source(deps))
