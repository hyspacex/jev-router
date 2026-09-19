"""Per-provider circuit breakers.

A provider is one subscription. When it answers 429 or 5xx, or will not
connect, several times in a row, there is no point sending it the next
request: it will fail too, and the caller waits for the failure. So the
breaker opens, the router skips that provider's models while it is open, and
after a cooldown it lets exactly one request through to find out whether the
provider is back.

All of this lives in memory. It is per process and it is lost on restart,
which is the right lifetime for a fact about the last two minutes. Nothing
here blocks, and nothing here can fail a request: the worst a wrong answer
does is send a request to a provider that is still down, or skip one that has
recovered.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import BreakerCfg, RouterConfig

log = logging.getLogger("jev_router.providers")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


@dataclass
class BreakerState:
    provider: str
    state: str = CLOSED
    failures: int = 0
    opened_at: float | None = None
    closed_at: float | None = None
    cooldown: float = 0.0
    trial_in_flight: bool = False
    opens: int = 0
    successes: int = 0
    total_failures: int = 0
    last_error: str = ""


class ProviderBreakers:
    """The breaker for every provider, keyed by name."""

    def __init__(self, config: RouterConfig, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.clock = clock
        self.states: dict[str, BreakerState] = {
            name: BreakerState(provider=name) for name in config.provider_names()
        }

    def state_for(self, provider: str) -> BreakerState:
        state = self.states.get(provider)
        if state is None:
            state = BreakerState(provider=provider)
            self.states[provider] = state
        return state

    def cfg_for(self, provider: str) -> BreakerCfg:
        return self.config.breaker_cfg(provider)

    # --- what the forwarder asks ---------------------------------------

    def _refresh(self, state: BreakerState) -> None:
        """Move an open breaker to half-open once its cooldown has run out."""
        if state.state != OPEN or state.opened_at is None:
            return
        if self.clock() - state.opened_at >= state.cooldown:
            state.state = HALF_OPEN
            state.trial_in_flight = False

    def available(self, provider: str) -> bool:
        """True when a request may be sent to this provider right now."""
        state = self.state_for(provider)
        self._refresh(state)
        if state.state == CLOSED:
            return True
        if state.state == HALF_OPEN:
            return not state.trial_in_flight
        return False

    def acquire(self, provider: str) -> bool:
        """Take the right to send one request. Half-open hands out exactly one."""
        state = self.state_for(provider)
        self._refresh(state)
        if state.state == CLOSED:
            return True
        if state.state == HALF_OPEN and not state.trial_in_flight:
            state.trial_in_flight = True
            return True
        return False

    def release(self, provider: str) -> None:
        """Give back an unused half-open trial slot."""
        state = self.state_for(provider)
        state.trial_in_flight = False

    # --- what the forwarder reports ------------------------------------

    def record_success(self, provider: str) -> None:
        state = self.state_for(provider)
        was = state.state
        state.failures = 0
        state.successes += 1
        state.trial_in_flight = False
        if was != CLOSED:
            state.state = CLOSED
            state.opened_at = None
            state.closed_at = self.clock()
            state.cooldown = 0.0
            log.info("provider %s recovered", provider)

    def record_failure(self, provider: str, reason: str = "") -> None:
        cfg = self.cfg_for(provider)
        state = self.state_for(provider)
        state.total_failures += 1
        state.last_error = reason
        state.trial_in_flight = False
        if state.state == HALF_OPEN:
            # The trial failed. Back to open, waiting twice as long.
            state.cooldown = min(
                max(state.cooldown, cfg.cooldown_seconds) * 2.0, cfg.max_cooldown_seconds
            )
            state.state = OPEN
            state.opened_at = self.clock()
            state.opens += 1
            log.warning(
                "provider %s failed its trial, open for %.0f s (%s)",
                provider,
                state.cooldown,
                reason,
            )
            return
        state.failures += 1
        if state.state == CLOSED and state.failures >= max(1, cfg.failures_to_open):
            state.cooldown = cfg.cooldown_seconds
            state.state = OPEN
            state.opened_at = self.clock()
            state.opens += 1
            log.warning(
                "provider %s open for %.0f s after %d failures (%s)",
                provider,
                state.cooldown,
                state.failures,
                reason,
            )

    # --- what routing asks ---------------------------------------------

    def pressure(self, provider: str) -> float:
        """Pressure derived from health alone, for a provider with no quota source.

        An open breaker is 1.0. After it closes the number decays linearly to
        zero over `recovery_seconds`, so a provider that has just come back is
        not handed the whole load at once.
        """
        state = self.state_for(provider)
        self._refresh(state)
        if state.state in (OPEN, HALF_OPEN):
            return 1.0
        if state.closed_at is None:
            return 0.0
        span = max(0.0, self.cfg_for(provider).recovery_seconds)
        if span <= 0:
            return 0.0
        elapsed = self.clock() - state.closed_at
        if elapsed >= span:
            return 0.0
        return max(0.0, min(1.0, 1.0 - elapsed / span))

    def healthy(self, provider: str) -> bool:
        state = self.state_for(provider)
        self._refresh(state)
        return state.state == CLOSED

    def report(self) -> dict[str, Any]:
        now = self.clock()
        out: dict[str, Any] = {}
        for name, state in self.states.items():
            self._refresh(state)
            cfg = self.cfg_for(name)
            retry_in = None
            if state.state == OPEN and state.opened_at is not None:
                retry_in = round(max(0.0, state.opened_at + state.cooldown - now), 1)
            out[name] = {
                "state": state.state,
                "healthy": state.state == CLOSED,
                "consecutive_failures": state.failures,
                "cooldown_seconds": round(state.cooldown, 1),
                "retry_in_seconds": retry_in,
                "trial_in_flight": state.trial_in_flight,
                "opens": state.opens,
                "successes": state.successes,
                "failures": state.total_failures,
                "last_error": state.last_error,
                "breaker_pressure": round(self.pressure(name), 4),
                "failures_to_open": cfg.failures_to_open,
                "models": sorted(
                    m for m in self.config.models if self.config.provider_of(m) == name
                ),
            }
        return out


def merge_pressures(
    config: RouterConfig,
    quota_pressures: dict[str, float],
    breakers: ProviderBreakers | None,
) -> dict[str, float]:
    """One number per provider: the quota reading, or the breaker's when there
    is no quota source, and the higher of the two when there are both."""
    if not config.quota_policy.enabled:
        return {}
    out: dict[str, float] = {}
    for provider in config.provider_names():
        quota = quota_pressures.get(provider)
        health = breakers.pressure(provider) if breakers else 0.0
        if quota is None:
            out[provider] = health
        else:
            out[provider] = max(quota, health)
    return out
