"""The Jev decider: ask TypeSafe, then run the answers through the policy."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..config import AliasCfg, RouterConfig
from ..features import Features
from ..policy import evaluate, finalize
from ..state import build_state
from .base import Decider, Decision, build_decider, register_decider

log = logging.getLogger("jev_router.jev")


class JevDecider:
    name = "jev"

    def __init__(
        self,
        config: RouterConfig,
        client: httpx.AsyncClient,
        fallback: Decider | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.fallback = fallback

    @property
    def api_key(self) -> str:
        return os.environ.get(self.config.settings.jev_api_key_env, "")

    async def decide(self, features: Features, alias_cfg: AliasCfg) -> Decision:
        settings = self.config.settings
        state = build_state(alias_cfg.state_builder, features, self.config)
        questions = {qid: self.config.question(qid) for qid in alias_cfg.questions}

        if not self.api_key:
            return await self._fallback(features, alias_cfg, state, "no Jev API key set")
        if not questions:
            return await self._fallback(
                features, alias_cfg, state, "alias asks no questions"
            )

        payload = {"model": settings.jev_model, "state": state, "questions": questions}
        started = time.perf_counter()
        try:
            resp = await self.client.post(
                settings.jev_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=settings.jev_timeout_ms / 1000,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # timeout, 429, 529, bad JSON, anything
            elapsed = (time.perf_counter() - started) * 1000
            reason = _short_error(exc)
            log.warning("jev call failed after %.0f ms: %s", elapsed, reason)
            return await self._fallback(features, alias_cfg, state, reason, jev_ms=elapsed)

        elapsed = (time.perf_counter() - started) * 1000
        answers = data.get("answers") or {}
        result = evaluate(self.config, alias_cfg, answers, features)
        return Decision(
            model=result.model,
            effort=result.effort,
            rule=result.rule,
            reason=result.reason,
            answers=answers,
            jev_ms=elapsed,
            jev_input_tokens=(data.get("usage") or {}).get("input_tokens"),
            fallback=False,
            state=state,
            state_builder=alias_cfg.state_builder,
            notes=result.notes,
            decider=self.name,
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
            decision = await self.fallback.decide(features, alias_cfg)
            decision.fallback = True
            decision.jev_ms = jev_ms
            decision.state = state
            decision.state_builder = alias_cfg.state_builder
            decision.reason = f"{reason}; {decision.reason}"
            return decision

        route = self.config.settings.default_route
        result = finalize(
            self.config, alias_cfg, route, "fallback", reason, features
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
    return JevDecider(config, deps["jev_client"], fallback)
