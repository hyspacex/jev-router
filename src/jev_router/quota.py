"""Quota sources, the snapshot poller, and the pure pressure function.

A provider is a subscription with a usage window. A quota source reports how
much of that window has been spent. The router turns that into one number per
provider, called pressure, which is 0 when a provider is on pace and 1 when it
is far ahead of pace or nearly spent.

Three rules hold this together:

- Nothing here runs on the request path. A background task polls, the request
  path reads the last snapshot out of memory.
- Every failure is neutral. A missing source, a failed command, a missing
  field or a stale snapshot all mean pressure 0, which routes exactly as the
  router routed before quota existed.
- `compute_pressure` is pure. It takes a snapshot, the knobs, a clock reading
  and the previous value, and returns a number. That is what makes it
  testable and what lets `evals/tune.py` replay a recorded pressure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from .config import QuotaPolicyCfg, QuotaSourceCfg, RouterConfig

log = logging.getLogger("jev_router.quota")


# --- snapshots ----------------------------------------------------------


@dataclass(frozen=True)
class QuotaSnapshot:
    """What one poll of one provider found. Numbers only, never a payload."""

    provider: str
    fetched_at: float
    used_percent: float | None = None
    expected_used_percent: float | None = None
    window_minutes: float | None = None
    resets_at: float | None = None  # epoch seconds
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.used_percent is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "fetched_at": self.fetched_at,
            "used_percent": self.used_percent,
            "expected_used_percent": self.expected_used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "error": self.error,
        }


# --- the source registry ------------------------------------------------


@runtime_checkable
class QuotaSource(Protocol):
    name: str

    async def fetch(self) -> QuotaSnapshot: ...


QuotaSourceFactory = Callable[[str, QuotaSourceCfg], "QuotaSource"]

QUOTA_SOURCES: dict[str, QuotaSourceFactory] = {}


def register_quota_source(name: str) -> Callable[[QuotaSourceFactory], QuotaSourceFactory]:
    def deco(factory: QuotaSourceFactory) -> QuotaSourceFactory:
        if name in QUOTA_SOURCES:
            raise ValueError(f"quota source {name!r} is already registered")
        QUOTA_SOURCES[name] = factory
        return factory

    return deco


def build_quota_source(provider: str, cfg: QuotaSourceCfg) -> QuotaSource:
    try:
        factory = QUOTA_SOURCES[cfg.source]
    except KeyError:
        raise KeyError(
            f"unknown quota source {cfg.source!r} (known: {', '.join(sorted(QUOTA_SOURCES))})"
        ) from None
    return factory(provider, cfg)


# --- reading fields out of whatever the source returned -----------------


def extract(data: Any, path: str) -> Any:
    """Walk a dotted path. A numeric step indexes a list."""
    cursor = data
    for step in path.split("."):
        if cursor is None:
            return None
        if isinstance(cursor, list):
            try:
                cursor = cursor[int(step)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cursor, dict):
            cursor = cursor.get(step)
        else:
            return None
    return cursor


def first_non_null(data: Any, paths: Any) -> Any:
    """The first path that yields something.

    Providers fill in either a primary window or a secondary one, so a config
    lists both and the first one present wins.
    """
    if paths is None:
        return None
    if isinstance(paths, str):
        paths = [paths]
    for path in paths:
        value = extract(data, str(path))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_timestamp(value: Any) -> float | None:
    """ISO 8601 (with or without a trailing Z) or an epoch number."""
    number = _number(value)
    if number is not None:
        return number
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def snapshot_from_payload(
    provider: str, data: Any, cfg: dict[str, Any], now: float
) -> QuotaSnapshot:
    """Pull the four numbers out of a parsed payload using the configured paths."""
    used = _number(first_non_null(data, cfg.get("used_percent")))
    if used is None:
        return QuotaSnapshot(
            provider=provider, fetched_at=now, error="no used_percent at the configured paths"
        )
    return QuotaSnapshot(
        provider=provider,
        fetched_at=now,
        used_percent=used,
        expected_used_percent=_number(first_non_null(data, cfg.get("expected_used_percent"))),
        window_minutes=_number(first_non_null(data, cfg.get("window_minutes"))),
        resets_at=parse_timestamp(first_non_null(data, cfg.get("resets_at"))),
    )


# --- the shipped sources ------------------------------------------------


class NoneSource:
    """A provider with no usage numbers. Always neutral."""

    name = "none"

    def __init__(self, provider: str, cfg: QuotaSourceCfg) -> None:
        self.provider = provider

    async def fetch(self) -> QuotaSnapshot:
        return QuotaSnapshot(
            provider=self.provider, fetched_at=time.time(), error="no quota source"
        )


@register_quota_source("none")
def _make_none(provider: str, cfg: QuotaSourceCfg) -> QuotaSource:
    return NoneSource(provider, cfg)


class StaticSource:
    """Numbers written straight into router.yaml. For tests and for demos."""

    name = "static"

    def __init__(self, provider: str, cfg: QuotaSourceCfg) -> None:
        self.provider = provider
        self.cfg = dict(cfg.config)

    async def fetch(self) -> QuotaSnapshot:
        now = time.time()
        used = _number(self.cfg.get("used_percent"))
        if used is None:
            return QuotaSnapshot(
                provider=self.provider, fetched_at=now, error="static source has no used_percent"
            )
        return QuotaSnapshot(
            provider=self.provider,
            fetched_at=now,
            used_percent=used,
            expected_used_percent=_number(self.cfg.get("expected_used_percent")),
            window_minutes=_number(self.cfg.get("window_minutes")),
            resets_at=parse_timestamp(self.cfg.get("resets_at")),
        )


@register_quota_source("static")
def _make_static(provider: str, cfg: QuotaSourceCfg) -> QuotaSource:
    return StaticSource(provider, cfg)


class CommandSource:
    """Run a configured argv, read JSON from stdout, pick fields out by path.

    The argv is a list, never a shell string, so nothing is interpolated and
    no shell is involved. Output is capped, the process is killed on timeout,
    and every failure becomes an error on the snapshot rather than a raise.
    """

    name = "command"
    MAX_OUTPUT = 1_000_000

    def __init__(self, provider: str, cfg: QuotaSourceCfg) -> None:
        self.provider = provider
        self.cfg = dict(cfg.config)
        argv = self.cfg.get("argv") or []
        self.argv = [str(a) for a in argv] if isinstance(argv, list) else []
        self.timeout_s = (_number(self.cfg.get("timeout_ms")) or 5000.0) / 1000.0

    async def fetch(self) -> QuotaSnapshot:
        now = time.time()
        if not self.argv:
            return QuotaSnapshot(
                provider=self.provider, fetched_at=now, error="command source has no argv"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            return QuotaSnapshot(
                provider=self.provider,
                fetched_at=now,
                error=f"could not start the command: {type(exc).__name__}",
            )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            _kill(proc)
            return QuotaSnapshot(
                provider=self.provider, fetched_at=now, error="the command timed out"
            )
        except Exception as exc:  # noqa: BLE001 - a quota poll may never raise
            _kill(proc)
            return QuotaSnapshot(
                provider=self.provider,
                fetched_at=now,
                error=f"the command failed: {type(exc).__name__}",
            )
        if proc.returncode != 0:
            return QuotaSnapshot(
                provider=self.provider,
                fetched_at=now,
                error=f"the command exited {proc.returncode}",
            )
        if len(stdout) > self.MAX_OUTPUT:
            return QuotaSnapshot(
                provider=self.provider, fetched_at=now, error="the command printed too much"
            )
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
        except ValueError:
            return QuotaSnapshot(
                provider=self.provider, fetched_at=now, error="the command did not print JSON"
            )
        return snapshot_from_payload(self.provider, data, self.cfg, now)


def _kill(proc: Any) -> None:
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


@register_quota_source("command")
def _make_command(provider: str, cfg: QuotaSourceCfg) -> QuotaSource:
    return CommandSource(provider, cfg)


# --- pressure, a pure function ------------------------------------------


def expected_percent(snapshot: QuotaSnapshot, now: float) -> float | None:
    """Where usage should be, as a percentage of the window, right now.

    The source's own number wins when it has one. Otherwise it comes from the
    window length and the reset time: a window that is 40% elapsed should be
    about 40% spent. When neither is available this returns None and the
    caller falls back to the raw percent.
    """
    if snapshot.expected_used_percent is not None:
        return max(0.0, min(100.0, snapshot.expected_used_percent))
    if snapshot.window_minutes and snapshot.resets_at:
        window_s = snapshot.window_minutes * 60.0
        if window_s <= 0:
            return None
        elapsed = window_s - (snapshot.resets_at - now)
        return max(0.0, min(100.0, 100.0 * elapsed / window_s))
    return None


def _ramp(value: float, starts_at: float, full_at: float) -> float:
    if full_at <= starts_at:
        return 1.0 if value >= full_at else 0.0
    return max(0.0, min(1.0, (value - starts_at) / (full_at - starts_at)))


def raw_pressure(
    snapshot: QuotaSnapshot | None, knobs: QuotaPolicyCfg, now: float
) -> tuple[float, str]:
    """Pressure before hysteresis, with a one-word note on where it came from."""
    if snapshot is None:
        return 0.0, "no snapshot"
    if not snapshot.ok:
        return 0.0, snapshot.error or "no usage numbers"
    used = float(snapshot.used_percent or 0.0)
    if used >= knobs.full_used_percent:
        return 1.0, "used is at or above the full mark"
    expected = expected_percent(snapshot, now)
    if expected is None:
        return _ramp(used, knobs.raw_starts_at, knobs.raw_full_at), "raw percent, no pace"
    ahead = used - expected
    return _ramp(ahead, knobs.pressure_starts_at, knobs.pressure_full_at), "pace"


def compute_pressure(
    snapshot: QuotaSnapshot | None,
    knobs: QuotaPolicyCfg,
    now: float,
    previous: float | None = None,
    stale_after_seconds: float | None = None,
) -> tuple[float, str]:
    """Pressure in [0, 1] for one provider, with the reason for that number.

    Pure. Hysteresis keeps a value that moved only a little, and the change is
    clamped so one bad poll cannot swing routing.
    """
    stale = (
        snapshot is not None
        and stale_after_seconds is not None
        and now - snapshot.fetched_at > stale_after_seconds
    )
    if stale:
        return 0.0, "the snapshot is stale"
    value, why = raw_pressure(snapshot, knobs, now)
    if previous is not None:
        if abs(value - previous) < knobs.hysteresis:
            return previous, f"{why} (held, the move was under the hysteresis band)"
        span = max(0.0, knobs.max_change_per_poll)
        clamped = max(previous - span, min(previous + span, value))
        if clamped != value:
            why = f"{why} (rate limited)"
        value = clamped
    return max(0.0, min(1.0, value)), why


# --- the poller ---------------------------------------------------------


@dataclass
class ProviderQuota:
    provider: str
    enabled: bool = False
    source_name: str = "none"
    snapshot: QuotaSnapshot | None = None
    pressure: float = 0.0
    reason: str = "no quota source"
    polls: int = 0
    errors: int = 0
    last_error: str = ""
    history: list[float] = field(default_factory=list)


class QuotaMonitor:
    """Polls every enabled source and keeps the last snapshot of each.

    `pressures()` is an in-memory read. It is safe to call on a request path.
    """

    def __init__(self, config: RouterConfig, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.clock = clock
        self.knobs = config.quota_policy
        self.states: dict[str, ProviderQuota] = {}
        self.sources: dict[str, QuotaSource] = {}
        self._tasks: list[asyncio.Task[None]] = []
        for provider in config.provider_names():
            cfg = config.quota_cfg(provider)
            state = ProviderQuota(provider=provider)
            if cfg is not None:
                state.enabled = bool(cfg.enabled) and cfg.source != "none"
                state.source_name = cfg.source
                if state.enabled:
                    try:
                        self.sources[provider] = build_quota_source(provider, cfg)
                    except Exception as exc:  # noqa: BLE001 - never fail startup
                        state.enabled = False
                        state.last_error = f"could not build the source: {exc}"
            self.states[provider] = state

    # --- reading -------------------------------------------------------

    def pressures(self) -> dict[str, float]:
        """Only providers that actually have a source. A provider with none is
        left out, so the breaker is the only thing that can move it."""
        if not self.knobs.enabled:
            return {}
        return {
            name: state.pressure for name, state in self.states.items() if state.enabled
        }

    def report(self) -> dict[str, Any]:
        now = self.clock()
        providers = {}
        for name, state in self.states.items():
            cfg = self.config.quota_cfg(name)
            stale_after = cfg.stale_after_seconds if cfg else None
            snap = state.snapshot
            age = None if snap is None else round(now - snap.fetched_at, 1)
            providers[name] = {
                "enabled": state.enabled,
                "source": state.source_name,
                "pressure": round(state.pressure, 4),
                "reason": state.reason,
                "snapshot": snap.to_dict() if snap else None,
                "age_seconds": age,
                "stale": bool(
                    snap is not None and stale_after is not None and age is not None
                    and age > stale_after
                ),
                "polls": state.polls,
                "errors": state.errors,
                "last_error": state.last_error,
            }
        return {
            "enabled": self.knobs.enabled,
            "demote_above": self.knobs.demote_above,
            "providers": providers,
        }

    # --- polling -------------------------------------------------------

    async def poll_once(self, provider: str) -> ProviderQuota:
        """One poll of one provider. Never raises."""
        state = self.states[provider]
        source = self.sources.get(provider)
        cfg = self.config.quota_cfg(provider)
        if source is None:
            return state
        try:
            snapshot = await source.fetch()
        except Exception as exc:  # noqa: BLE001 - a poll may never take the router down
            snapshot = QuotaSnapshot(
                provider=provider,
                fetched_at=self.clock(),
                error=f"the source raised {type(exc).__name__}",
            )
        state.polls += 1
        state.snapshot = snapshot
        if snapshot.error:
            state.errors += 1
            state.last_error = snapshot.error
        pressure, why = compute_pressure(
            snapshot,
            self.knobs,
            self.clock(),
            previous=state.pressure if state.polls > 1 else None,
            stale_after_seconds=cfg.stale_after_seconds if cfg else None,
        )
        state.pressure = pressure
        state.reason = why
        state.history.append(pressure)
        del state.history[:-20]
        return state

    def refresh_staleness(self) -> None:
        """Drop pressure to zero for a provider whose snapshot has aged out."""
        now = self.clock()
        for name, state in self.states.items():
            cfg = self.config.quota_cfg(name)
            if not state.enabled or cfg is None or state.snapshot is None:
                continue
            if now - state.snapshot.fetched_at > cfg.stale_after_seconds and state.pressure:
                state.pressure = 0.0
                state.reason = "the snapshot is stale"

    async def _loop(self, provider: str) -> None:
        cfg = self.config.quota_cfg(provider)
        interval = max(1.0, cfg.poll_seconds if cfg else 300.0)
        while True:
            await self.poll_once(provider)
            state = self.states[provider]
            log.debug(
                "quota provider=%s pressure=%.2f reason=%s", provider, state.pressure, state.reason
            )
            await asyncio.sleep(interval)

    def start(self) -> None:
        if self._tasks or not self.sources:
            return
        for provider in self.sources:
            self._tasks.append(asyncio.create_task(self._loop(provider)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
