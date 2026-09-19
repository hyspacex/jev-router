"""The Jev decider: ask TypeSafe, then run the answers through the policy."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..config import AliasCfg, RouterConfig
from ..features import Features
from ..policy import apply_effort, evaluate, finalize
from ..state import build_state
from .base import Decider, Decision, build_decider, pressure_source, register_decider

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
        self.pressures = pressures if callable(pressures) else dict

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
        if cfg.model in self.config.models:
            upstream_id, extra = apply_effort(self.config, cfg.model, cfg.effort)
        else:
            upstream_id, extra = cfg.model, {}
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

    async def decide(self, features: Features, alias_cfg: AliasCfg) -> Decision:
        settings = self.config.settings
        state = build_state(alias_cfg.state_builder, features, self.config)
        draft = await self.draft(features, alias_cfg)
        if draft and isinstance(state, dict) and alias_cfg.draft is not None:
            state[alias_cfg.draft.state_field] = draft
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
        pressures = self.pressures()
        result = evaluate(self.config, alias_cfg, answers, features, pressures)
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
            route=result.route,
            plan=result.plan,
            pressures=dict(pressures),
            shifts=result.shifts,
            reordered=result.reordered,
            pressure_changed_the_outcome=result.pressure_changed_the_outcome,
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
