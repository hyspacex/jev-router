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
import dataclasses
import glob
import json
import logging
import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .config import (
    CODEXBAR_ID,
    CodexBarCfg,
    QuotaPolicyCfg,
    QuotaSourceCfg,
    RouterConfig,
)

log = logging.getLogger("jev_router.quota")


# --- snapshots ----------------------------------------------------------


@dataclass(frozen=True)
class QuotaWindow:
    """One limit a provider exposes, with its own usage and its own reset.

    Providers publish several overlapping windows: a five-hour one and a
    weekly one, say. Each is a coherent record, and the fields stay together:
    one window's usage is never read against another window's reset time.
    """

    name: str = ""
    used_percent: float | None = None
    used: float | None = None
    limit: float | None = None
    expected_used_percent: float | None = None
    window_minutes: float | None = None
    resets_at: float | None = None  # epoch seconds
    # A short reset phrase another program wrote, kept only when it passes
    # `foreign_label`. Shown on the live overview; never stored, so it is
    # left out of `to_dict`.
    reset_description: str = field(default="", compare=False)

    @property
    def percent(self) -> float | None:
        """The window's usage as a percentage, computed only from itself."""
        if self.used_percent is not None:
            return self.used_percent
        if self.used is not None and self.limit:
            return 100.0 * self.used / self.limit
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "used_percent": self.percent,
            "used": self.used,
            "limit": self.limit,
            "expected_used_percent": self.expected_used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
        }


# What a snapshot is worth, in one word. `unknown` is not zero: it says nobody
# measured, and it is reported that way everywhere it is shown.
FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"
ERROR = "error"


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
    # Present when the source reports several limits. A source that reports
    # one keeps using the fields above and behaves exactly as it did.
    windows: tuple[QuotaWindow, ...] = ()

    @property
    def ok(self) -> bool:
        if self.error:
            return False
        if self.used_percent is not None:
            return True
        return any(w.percent is not None for w in self.windows)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "fetched_at": self.fetched_at,
            "used_percent": self.used_percent,
            "expected_used_percent": self.expected_used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "error": self.error,
        }
        if self.windows:
            out["windows"] = [w.to_dict() for w in self.windows]
        return out


def snapshot_status(
    snapshot: QuotaSnapshot | None,
    now: float,
    stale_after_seconds: float | None = None,
) -> str:
    """One word for how much this reading is worth. Pure.

    A stale or unknown reading contributes nothing to pressure, which is the
    neutral behaviour the router had before quota existed. It is never
    reported as unused capacity.
    """
    if snapshot is None:
        return UNKNOWN
    if snapshot.error:
        return ERROR
    if not snapshot.ok:
        return UNKNOWN
    if (
        stale_after_seconds is not None
        and now - snapshot.fetched_at > stale_after_seconds
    ):
        return STALE
    return FRESH


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


def build_quota_source(
    provider: str, cfg: QuotaSourceCfg, root: RouterConfig | None = None
) -> QuotaSource:
    """Build one provider's source. A factory marked `takes_root` also gets
    the whole config, for settings that live outside the provider block."""
    try:
        factory = QUOTA_SOURCES[cfg.source]
    except KeyError:
        raise KeyError(
            f"unknown quota source {cfg.source!r} (known: {', '.join(sorted(QUOTA_SOURCES))})"
        ) from None
    if root is not None and getattr(factory, "takes_root", False):
        return factory(provider, cfg, root)  # type: ignore[call-arg]
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


def windows_from_payload(data: Any, specs: Any) -> tuple[QuotaWindow, ...]:
    """Build one record per configured window, each read only from itself."""
    if not isinstance(specs, list):
        return ()
    out: list[QuotaWindow] = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        window = QuotaWindow(
            name=str(spec.get("name") or f"window{i + 1}"),
            used_percent=_number(first_non_null(data, spec.get("used_percent"))),
            used=_number(first_non_null(data, spec.get("used"))),
            limit=_number(first_non_null(data, spec.get("limit"))),
            expected_used_percent=_number(
                first_non_null(data, spec.get("expected_used_percent"))
            ),
            window_minutes=_number(first_non_null(data, spec.get("window_minutes"))),
            resets_at=parse_timestamp(first_non_null(data, spec.get("resets_at"))),
        )
        out.append(window)
    return tuple(out)


def literal_window(spec: dict[str, Any], index: int = 0) -> QuotaWindow:
    """A window whose numbers are written out rather than looked up by path."""
    return QuotaWindow(
        name=str(spec.get("name") or f"window{index + 1}"),
        used_percent=_number(spec.get("used_percent")),
        used=_number(spec.get("used")),
        limit=_number(spec.get("limit")),
        expected_used_percent=_number(spec.get("expected_used_percent")),
        window_minutes=_number(spec.get("window_minutes")),
        resets_at=parse_timestamp(spec.get("resets_at")),
    )


def snapshot_from_windows(
    provider: str, windows: tuple[QuotaWindow, ...], now: float
) -> QuotaSnapshot:
    if not any(w.percent is not None for w in windows):
        return QuotaSnapshot(
            provider=provider,
            fetched_at=now,
            error="no window reported a usage figure",
        )
    # A summary for display only. Pressure comes from the windows, so no
    # window's usage is ever read against another window's reset.
    percents = [w.percent for w in windows if w.percent is not None]
    return QuotaSnapshot(
        provider=provider,
        fetched_at=now,
        used_percent=max(percents),
        windows=windows,
    )


def snapshot_from_payload(
    provider: str, data: Any, cfg: dict[str, Any], now: float
) -> QuotaSnapshot:
    """Pull the numbers out of a parsed payload using the configured paths.

    A config with a `windows:` list gets one record per window. A config
    without one is read exactly as it always was.
    """
    windows = windows_from_payload(data, cfg.get("windows"))
    if windows:
        return snapshot_from_windows(provider, windows, now)
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
        if isinstance(self.cfg.get("windows"), list):
            # Written straight into the config, so the values are the values.
            windows = tuple(
                literal_window(spec, i)
                for i, spec in enumerate(self.cfg["windows"])
                if isinstance(spec, dict)
            )
            return snapshot_from_windows(self.provider, windows, now)
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


# --- CodexBar -----------------------------------------------------------
#
# CodexBar.app watches coding subscriptions and writes all of them to one
# small JSON file about once a minute. Reading that file is tier 1. When the
# file is missing or old, the CodexBar CLI is tier 2, run through
# `CommandSource` exactly as a `command` source would run it.
#
# Only numbers and a few short labels come out of either. CodexBar's files
# also hold account emails, API keys and long free text; none of that is
# read into anything the router keeps.

CODEXBAR_SLOTS = ("primary", "secondary", "tertiary")
DEFAULT_SNAPSHOT_PATH = (
    "~/Library/Group Containers/Y5PE65HELJ.com.steipete.codexbar/widget-snapshot.json"
)
SNAPSHOT_GLOB = "~/Library/Group Containers/*.com.steipete.codexbar/widget-snapshot.json"
MAX_SNAPSHOT_BYTES = 1_000_000
MAX_ROWS = 8

# A string from another program passes only when it reads like a short
# label: letters, digits, spaces and a little punctuation. Anything longer or
# stranger is dropped, not trimmed.
_FOREIGN_LABEL = re.compile(r"[A-Za-z0-9 ,.:()/_+-]{1,48}")
_CURRENCY = re.compile(r"[A-Z]{3}")

TOKEN_FIELDS = (
    ("session_tokens", "sessionTokens"),
    ("session_cost_usd", "sessionCostUSD"),
    ("last_30_days_tokens", "last30DaysTokens"),
    ("last_30_days_cost_usd", "last30DaysCostUSD"),
)


def foreign_label(value: Any) -> str:
    """The value when it is a short plain label, else an empty string."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace(" ", " ").replace(" ", " ").split())
    return text if _FOREIGN_LABEL.fullmatch(text) else ""


def _percent(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else max(0.0, min(100.0, number))


@dataclass(frozen=True)
class CodexBarEntry:
    """One subscription out of CodexBar's snapshot. Numbers and labels only."""

    provider: str
    updated_at: float | None = None
    windows: tuple[QuotaWindow, ...] = ()
    # (title, used percent) per usage row: the plan's windows, or its pools.
    rows: tuple[tuple[str, float | None], ...] = ()
    # (name, number) for each of TOKEN_FIELDS, empty when CodexBar has none.
    tokens: tuple[tuple[str, float | None], ...] = ()
    currency: str = ""
    credits_remaining: float | None = None


@dataclass(frozen=True)
class WidgetSnapshot:
    """What one read of `widget-snapshot.json` found."""

    generated_at: float | None
    enabled: tuple[str, ...]
    entries: dict[str, CodexBarEntry]


def parse_codexbar_entry(raw: Any) -> CodexBarEntry | None:
    if not isinstance(raw, dict):
        return None
    provider = raw.get("provider")
    if not isinstance(provider, str) or not CODEXBAR_ID.fullmatch(provider):
        return None
    windows = []
    for slot in CODEXBAR_SLOTS:
        spec = raw.get(slot)
        if not isinstance(spec, dict):
            continue
        used = _percent(spec.get("usedPercent"))
        if used is None:
            continue
        windows.append(
            QuotaWindow(
                name=slot,
                used_percent=used,
                window_minutes=_number(spec.get("windowMinutes")),
                resets_at=parse_timestamp(spec.get("resetsAt")),
                reset_description=foreign_label(spec.get("resetDescription")),
            )
        )
    rows: list[tuple[str, float | None]] = []
    for row in raw.get("usageRows") or []:
        if not isinstance(row, dict) or len(rows) >= MAX_ROWS:
            continue
        left = _percent(row.get("percentLeft"))
        window = row.get("window") if isinstance(row.get("window"), dict) else {}
        used = 100.0 - left if left is not None else _percent(window.get("usedPercent"))
        title = foreign_label(row.get("title"))
        if title or used is not None:
            rows.append((title, used))
    usage = raw.get("tokenUsage") if isinstance(raw.get("tokenUsage"), dict) else {}
    tokens = tuple((name, _number(usage.get(key))) for name, key in TOKEN_FIELDS)
    currency = usage.get("currencyCode")
    return CodexBarEntry(
        provider=provider,
        updated_at=parse_timestamp(raw.get("updatedAt")),
        windows=tuple(windows),
        rows=tuple(rows),
        tokens=tokens if usage else (),
        currency=currency if isinstance(currency, str) and _CURRENCY.fullmatch(currency) else "",
        credits_remaining=_number(raw.get("creditsRemaining")),
    )


def parse_widget_snapshot(data: Any, mtime: float | None = None) -> WidgetSnapshot | None:
    """The snapshot's subscriptions by id, or None when it is not that file."""
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    entries: dict[str, CodexBarEntry] = {}
    for raw in data["entries"]:
        entry = parse_codexbar_entry(raw)
        if entry is not None and entry.provider not in entries:
            entries[entry.provider] = entry
    enabled = tuple(
        dict.fromkeys(
            p for p in data.get("enabledProviders") or []
            if isinstance(p, str) and CODEXBAR_ID.fullmatch(p)
        )
    )
    generated = parse_timestamp(data.get("generatedAt"))
    return WidgetSnapshot(
        generated_at=generated if generated is not None else mtime,
        enabled=enabled,
        entries=entries,
    )


def snapshot_candidates(cfg: CodexBarCfg) -> list[Path]:
    """Where to look for the snapshot. A configured path is the only place.
    Without one: CodexBar's default container, then any other CodexBar
    group container that has the file."""
    if cfg.snapshot_path:
        return [Path(os.path.expanduser(cfg.snapshot_path))]
    first = Path(os.path.expanduser(DEFAULT_SNAPSHOT_PATH))
    found = [Path(p) for p in sorted(glob.glob(os.path.expanduser(SNAPSHOT_GLOB)))]
    return [first, *(p for p in found if p != first)]


def read_widget_snapshot(cfg: CodexBarCfg) -> tuple[WidgetSnapshot | None, str]:
    """Read and parse the snapshot. Never raises: a problem comes back as a
    short reason, `missing` when there is no file to read."""
    for path in snapshot_candidates(cfg):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            if stat.st_size > MAX_SNAPSHOT_BYTES:
                return None, "the snapshot is too large"
            data = json.loads(path.read_bytes().decode("utf-8", "replace"))
        except (OSError, ValueError):
            return None, "the snapshot could not be read"
        parsed = parse_widget_snapshot(data, mtime=stat.st_mtime)
        if parsed is None:
            return None, "the snapshot is not a CodexBar widget snapshot"
        return parsed, ""
    return None, "missing"


def read_codexbar_enabled(cfg: CodexBarCfg) -> tuple[str, ...] | None:
    """The provider ids CodexBar's own config enables, or None when it cannot
    be read. Only `id` and `enabled` are looked at; the file can hold keys."""
    path = Path(os.path.expanduser(cfg.config_path))
    try:
        if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        data = json.loads(path.read_bytes().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, list):
        return None
    out = []
    for item in providers:
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        pid = item.get("id")
        if isinstance(pid, str) and CODEXBAR_ID.fullmatch(pid):
            out.append(pid)
    return tuple(dict.fromkeys(out))


def entry_snapshot(
    provider: str, entry: CodexBarEntry, widget: WidgetSnapshot, now: float
) -> QuotaSnapshot:
    """A CodexBar entry as the snapshot every other source produces.

    `fetched_at` is when CodexBar measured, not when the file was read, so a
    reading CodexBar has stopped refreshing ages out like any other. An entry
    with no usage figure is unknown, not an error: nobody measured.
    """
    measured = entry.updated_at if entry.updated_at is not None else widget.generated_at
    fetched_at = now if measured is None else min(measured, now)
    if not entry.windows:
        return QuotaSnapshot(provider=provider, fetched_at=fetched_at)
    return dataclasses.replace(
        snapshot_from_windows(provider, entry.windows, fetched_at), fetched_at=fetched_at
    )


def cli_windows() -> list[dict[str, str]]:
    """Where the CodexBar CLI puts each window, as `command` source paths."""
    return [
        {
            "name": slot,
            "used_percent": f"0.usage.{slot}.usedPercent",
            "expected_used_percent": f"0.pace.{slot}.expectedUsedPercent",
            "window_minutes": f"0.usage.{slot}.windowMinutes",
            "resets_at": f"0.usage.{slot}.resetsAt",
        }
        for slot in CODEXBAR_SLOTS
    ]


class CodexBarSource:
    """One CodexBar subscription: the snapshot file first, the CLI second.

    The CLI runs only when the file is missing, older than
    `cli_fallback_after_seconds`, or has no entry for this subscription, and
    at most once per that many seconds. When neither tier has numbers the
    reading is unknown (nothing to read) or an error (something broke), and
    both mean pressure 0.
    """

    name = "codexbar"

    def __init__(
        self,
        provider: str,
        codexbar_provider: str,
        settings: CodexBarCfg | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.provider = provider
        self.codexbar_provider = codexbar_provider
        self.settings = settings or CodexBarCfg()
        self.clock = clock
        # Which tier the last reading came from: `snapshot`, `cli` or None.
        self.via: str | None = None
        # The last snapshot entry, for the rows and token rollups a tracked
        # subscription shows. The CLI does not report those.
        self.entry: CodexBarEntry | None = None
        self.cli: CommandSource | None = None
        binary = self.settings.cli_binary.strip()
        if binary:
            self.cli = CommandSource(
                provider,
                QuotaSourceCfg(
                    source="command",
                    enabled=True,
                    config={
                        "argv": [binary, "usage", "--provider", codexbar_provider, "--json-only"],
                        "timeout_ms": self.settings.cli_timeout_ms,
                        "windows": cli_windows(),
                    },
                ),
            )
        self._cli_at: float | None = None
        self._cli_last: QuotaSnapshot | None = None

    async def fetch(self) -> QuotaSnapshot:
        widget, why = read_widget_snapshot(self.settings)
        return await self.fetch_from(widget, why)

    async def fetch_from(self, widget: WidgetSnapshot | None, why: str = "") -> QuotaSnapshot:
        """One reading, from a snapshot the caller already read."""
        now = self.clock()
        entry = widget.entries.get(self.codexbar_provider) if widget else None
        self.entry = entry
        tier1 = entry_snapshot(self.provider, entry, widget, now) if entry and widget else None
        file_age = (
            None if widget is None or widget.generated_at is None
            else now - widget.generated_at
        )
        current = file_age is not None and file_age <= self.settings.cli_fallback_after_seconds
        if tier1 is not None and current:
            self.via = "snapshot"
            return tier1
        cli = await self._run_cli(now)
        if cli is not None and cli.ok:
            self.via = "cli"
            return cli
        if tier1 is not None:
            # Old, but what CodexBar last saw. Its own age decides whether it
            # is still fresh enough to count.
            self.via = "snapshot"
            return tier1
        self.via = None
        if cli is not None:
            return cli
        if why and why != "missing":
            return QuotaSnapshot(provider=self.provider, fetched_at=now, error=why)
        return QuotaSnapshot(provider=self.provider, fetched_at=now)

    async def _run_cli(self, now: float) -> QuotaSnapshot | None:
        """The CLI's reading, or None when there is no CLI to run.

        A CLI that is not installed is the same as no CLI: nothing measured,
        nothing broken. A recent result is reused rather than run again.
        """
        if self.cli is None or shutil.which(self.cli.argv[0]) is None:
            return None
        if (
            self._cli_at is not None
            and now - self._cli_at < self.settings.cli_fallback_after_seconds
        ):
            return self._cli_last
        self._cli_at = now
        snapshot = await self.cli.fetch()
        if snapshot.windows:
            # The CLI names every slot; keep only the windows it filled in.
            present = tuple(w for w in snapshot.windows if w.percent is not None)
            snapshot = dataclasses.replace(snapshot, windows=present)
        self._cli_last = snapshot
        return snapshot


def _make_codexbar(
    provider: str, cfg: QuotaSourceCfg, root: RouterConfig | None = None
) -> QuotaSource:
    return CodexBarSource(
        provider,
        cfg.codexbar_provider or "",
        root.codexbar if root is not None else None,
    )


_make_codexbar.takes_root = True  # type: ignore[attr-defined]
register_quota_source("codexbar")(_make_codexbar)


# --- what a reader is shown ---------------------------------------------

STEERING_LABELS = ("calm", "elevated", "demoting", "exhausted")


def steering_label(
    pressure: float,
    status: str,
    exhausted: bool,
    knobs: QuotaPolicyCfg,
    enabled: bool = True,
) -> str | None:
    """What quota is doing to routing for this provider right now. Pure.

    `exhausted` is a fresh window at or above `exhausted_at`. `demoting` is
    pressure above `demote_above`, where route entries on this provider move
    back. `elevated` is any pressure up to that: ahead of pace, not yet
    acting. `calm` is pressure 0 on a fresh reading. A reading that is not
    fresh, or quota routing that is off, steers nothing and gets no label:
    unknown is never shown as calm.
    """
    if not knobs.enabled or not enabled or status != FRESH:
        return None
    if exhausted:
        return "exhausted"
    if pressure > knobs.demote_above:
        return "demoting"
    if pressure > 0:
        return "elevated"
    return "calm"


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def window_view(window: QuotaWindow, now: float) -> dict[str, Any]:
    """One window with its pace: where it is, where it should be, and the gap."""
    used = window.percent
    expected = window_expected_percent(window, now)
    return {
        "name": window.name,
        "used_percent": _round(used),
        "expected_percent": _round(expected),
        "pace_delta": None if used is None or expected is None else round(used - expected, 2),
        "resets_at": window.resets_at,
        "window_minutes": window.window_minutes,
        "reset_description": window.reset_description or None,
    }


def snapshot_windows(snapshot: QuotaSnapshot | None) -> tuple[QuotaWindow, ...]:
    """The snapshot's windows, or its one summary window when it has no list."""
    if snapshot is None:
        return ()
    if snapshot.windows:
        return snapshot.windows
    if snapshot.used_percent is None:
        return ()
    return (
        QuotaWindow(
            name="usage",
            used_percent=snapshot.used_percent,
            expected_used_percent=snapshot.expected_used_percent,
            window_minutes=snapshot.window_minutes,
            resets_at=snapshot.resets_at,
        ),
    )


@dataclass
class TrackedSubscription:
    """A CodexBar subscription no provider links to. Shown, never routed."""

    id: str
    source: CodexBarSource
    snapshot: QuotaSnapshot | None = None
    polls: int = 0
    errors: int = 0
    last_error: str = ""


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


def window_expected_percent(window: QuotaWindow, now: float) -> float | None:
    """Where this one window should be, from its own length and its own reset."""
    if window.expected_used_percent is not None:
        return max(0.0, min(100.0, window.expected_used_percent))
    if window.window_minutes and window.resets_at:
        window_s = window.window_minutes * 60.0
        if window_s <= 0:
            return None
        elapsed = window_s - (window.resets_at - now)
        return max(0.0, min(100.0, 100.0 * elapsed / window_s))
    return None


def window_pressure(
    window: QuotaWindow, knobs: QuotaPolicyCfg, now: float
) -> tuple[float, str]:
    """Pressure from one window, using nothing but that window's own fields."""
    used = window.percent
    if used is None:
        return 0.0, "no usage numbers"
    label = window.name or "window"
    if used >= knobs.full_used_percent:
        return 1.0, f"{label} is at or above the full mark"
    expected = window_expected_percent(window, now)
    if expected is None:
        return (
            _ramp(used, knobs.raw_starts_at, knobs.raw_full_at),
            f"{label} raw percent, no pace",
        )
    return (
        _ramp(used - expected, knobs.pressure_starts_at, knobs.pressure_full_at),
        f"{label} pace",
    )


def raw_pressure(
    snapshot: QuotaSnapshot | None, knobs: QuotaPolicyCfg, now: float
) -> tuple[float, str]:
    """Pressure before hysteresis, with a one-word note on where it came from."""
    if snapshot is None:
        return 0.0, "no snapshot"
    if not snapshot.ok:
        return 0.0, snapshot.error or "no usage numbers"
    if snapshot.windows:
        # One pressure per window, and the highest wins. Nothing is averaged
        # and nothing is crossed over: the window nearest its limit is the one
        # that constrains the provider.
        scored = [window_pressure(w, knobs, now) for w in snapshot.windows]
        usable = [(value, why) for value, why in scored if why != "no usage numbers"]
        if not usable:
            return 0.0, "no usage numbers"
        return max(usable, key=lambda pair: pair[0])
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


def exhausted_window(
    snapshot: QuotaSnapshot | None,
    knobs: QuotaPolicyCfg,
    now: float,
    stale_after_seconds: float | None = None,
) -> QuotaWindow | None:
    """The window that has run out, or None. Pure.

    Only a fresh reading can say a provider is spent; stale, unknown and
    errored readings say nothing. Pace plays no part: this is whether the
    provider can serve, not whether it should. With several spent windows the
    one that resets last is returned, and an unknown reset counts as latest.
    """
    if snapshot is None or snapshot_status(snapshot, now, stale_after_seconds) != FRESH:
        return None
    windows = snapshot.windows or (
        QuotaWindow(
            used_percent=snapshot.used_percent,
            window_minutes=snapshot.window_minutes,
            resets_at=snapshot.resets_at,
        ),
    )
    spent = [
        w for w in windows if w.percent is not None and w.percent >= knobs.exhausted_at
    ]
    return max(
        spent,
        key=lambda w: w.resets_at if w.resets_at is not None else float("inf"),
        default=None,
    )


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
    So is `all_snapshots()`, which is what the overview shows.

    With `codexbar.enabled` and `include_external_subscriptions`, it also
    reads every CodexBar subscription no provider links to. Those are kept
    apart in `tracked` and never reach `pressures()` or `exhausted()`.
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
                        source = build_quota_source(provider, cfg, config)
                        if isinstance(source, CodexBarSource):
                            source.clock = clock
                        self.sources[provider] = source
                    except Exception as exc:  # noqa: BLE001 - never fail startup
                        state.enabled = False
                        state.last_error = f"could not build the source: {exc}"
            self.states[provider] = state
        self.codexbar = config.codexbar
        self.tracked: dict[str, TrackedSubscription] = {}

    # --- CodexBar subscriptions no provider links to -------------------

    @property
    def tracking(self) -> bool:
        return self.codexbar.enabled and self.codexbar.include_external_subscriptions

    def linked_codexbar_ids(self) -> set[str]:
        """CodexBar ids an enabled provider reads. These are routed, not tracked."""
        out = set()
        for name, state in self.states.items():
            cfg = self.config.quota_cfg(name)
            if state.enabled and cfg is not None and cfg.source == "codexbar":
                out.add(cfg.codexbar_provider or "")
        return out

    async def poll_tracked(self) -> dict[str, TrackedSubscription]:
        """One read of every tracked subscription. Never raises.

        The list is CodexBar's enabled providers, from the snapshot and, when
        it can be read, CodexBar's own config: both have to agree when both
        are there. A subscription CodexBar stops watching is dropped.
        """
        if not self.tracking:
            return self.tracked
        try:
            widget, why = read_widget_snapshot(self.codexbar)
            from_config = read_codexbar_enabled(self.codexbar)
            ids: list[str]
            if widget is not None and from_config is not None:
                ids = [i for i in widget.enabled if i in from_config]
            elif widget is not None:
                ids = list(widget.enabled)
            else:
                ids = list(from_config or ())
            linked = self.linked_codexbar_ids()
            wanted = [i for i in ids if i not in linked]
            self.tracked = {
                i: self.tracked.get(i)
                or TrackedSubscription(
                    id=i,
                    source=CodexBarSource(i, i, self.codexbar, clock=self.clock),
                )
                for i in wanted
            }
            for sub in self.tracked.values():
                try:
                    snapshot = await sub.source.fetch_from(widget, why)
                except Exception as exc:  # noqa: BLE001 - a poll may never raise
                    snapshot = QuotaSnapshot(
                        provider=sub.id,
                        fetched_at=self.clock(),
                        error=f"the source raised {type(exc).__name__}",
                    )
                sub.polls += 1
                sub.snapshot = snapshot
                if snapshot.error:
                    sub.errors += 1
                    sub.last_error = snapshot.error
        except Exception:  # noqa: BLE001 - observability may never take the router down
            log.exception("could not read the CodexBar subscriptions")
        return self.tracked

    # --- reading -------------------------------------------------------

    def pressures(self) -> dict[str, float]:
        """Only providers that actually have a source. A provider with none is
        left out, so the breaker is the only thing that can move it."""
        if not self.knobs.enabled:
            return {}
        return {
            name: state.pressure for name, state in self.states.items() if state.enabled
        }

    def exhausted(self) -> dict[str, QuotaWindow]:
        """The spent window of each provider a fresh reading says is spent."""
        if not self.knobs.enabled:
            return {}
        now = self.clock()
        out = {}
        for name, state in self.states.items():
            cfg = self.config.quota_cfg(name)
            if not state.enabled or cfg is None:
                continue
            window = exhausted_window(state.snapshot, self.knobs, now, cfg.stale_after_seconds)
            if window is not None:
                out[name] = window
        return out

    def report(self) -> dict[str, Any]:
        now = self.clock()
        providers = {}
        for name, state in self.states.items():
            cfg = self.config.quota_cfg(name)
            stale_after = cfg.stale_after_seconds if cfg else None
            snap = state.snapshot
            age = None if snap is None else round(now - snap.fetched_at, 1)
            status = snapshot_status(snap, now, stale_after)
            spent = (
                exhausted_window(snap, self.knobs, now, stale_after)
                if state.enabled
                else None
            )
            providers[name] = {
                "enabled": state.enabled,
                "source": state.source_name,
                "pressure": round(state.pressure, 4),
                "reason": state.reason,
                "snapshot": snap.to_dict() if snap else None,
                # `fresh`, `stale`, `unknown` or `error`. Anything but fresh
                # contributes nothing to pressure and is never reported as
                # unused capacity.
                "status": status,
                "windows": [w.to_dict() for w in snap.windows] if snap else [],
                "age_seconds": age,
                "stale": status == STALE,
                "exhausted": spent.to_dict() if spent else None,
                "polls": state.polls,
                "errors": state.errors,
                "last_error": state.last_error,
            }
        return {
            "enabled": self.knobs.enabled,
            "demote_above": self.knobs.demote_above,
            "providers": providers,
        }

    def all_snapshots(self, now: float | None = None) -> dict[str, Any]:
        """Every routed provider and every tracked subscription, as of now.

        An in-memory read, like `pressures()`: nothing is polled and nothing
        is run. Each window carries its own pace: used, expected from its own
        length and reset, and the difference, which is None when either side
        is unknown. `steering` says what quota is doing to routing, from the
        monitor's pressure against `quota_policy`; it is None whenever the
        reading is not fresh.
        """
        now = self.clock() if now is None else now
        knobs = self.knobs
        models_of: dict[str, list[dict[str, Any]]] = {}
        for model, mcfg in self.config.models.items():
            models_of.setdefault(self.config.provider_of(model), []).append(
                {"model": model, "quota_cost": mcfg.quota_cost}
            )
        routed = []
        for name, state in self.states.items():
            cfg = self.config.quota_cfg(name)
            stale_after = cfg.stale_after_seconds if cfg else None
            snap = state.snapshot
            status = snapshot_status(snap, now, stale_after)
            spent = exhausted_window(snap, knobs, now, stale_after) if state.enabled else None
            source = self.sources.get(name)
            routed.append({
                "provider": name,
                "enabled": state.enabled,
                "source": state.source_name,
                "codexbar_provider": cfg.codexbar_provider if cfg else None,
                "via": getattr(source, "via", None),
                "pressure": round(state.pressure, 4),
                "reason": state.reason,
                "status": status,
                "stale": status == STALE,
                "age_seconds": None if snap is None else round(now - snap.fetched_at, 1),
                "fetched_at": None if snap is None else snap.fetched_at,
                "used_percent": None if snap is None else snap.used_percent,
                "windows": [window_view(w, now) for w in snapshot_windows(snap)],
                "exhausted": spent.to_dict() if spent else None,
                "steering": steering_label(
                    state.pressure, status, spent is not None, knobs, state.enabled
                ),
                "demote_above": knobs.demote_above,
                "exhausted_at": knobs.exhausted_at,
                "models": [
                    {**m, "pressure": round(state.pressure * m["quota_cost"], 4)}
                    for m in models_of.get(name, [])
                ],
                "polls": state.polls,
                "errors": state.errors,
                "last_error": state.last_error,
            })
        tracked = []
        for sub in self.tracked.values():
            snap = sub.snapshot
            entry = sub.source.entry
            tracked.append({
                "id": sub.id,
                "via": sub.source.via,
                "status": snapshot_status(snap, now, self.codexbar.stale_after_seconds),
                "age_seconds": None if snap is None else round(now - snap.fetched_at, 1),
                "fetched_at": None if snap is None else snap.fetched_at,
                "windows": [window_view(w, now) for w in snapshot_windows(snap)],
                "plans": [
                    {"title": title or None, "used_percent": _round(used)}
                    for title, used in (entry.rows if entry else ())
                ],
                "tokens": dict(entry.tokens) if entry and entry.tokens else None,
                "currency": (entry.currency or None) if entry else None,
                "credits_remaining": entry.credits_remaining if entry else None,
                "polls": sub.polls,
                "errors": sub.errors,
                "last_error": sub.last_error,
            })
        return {
            "as_of": now,
            "quota_routing": knobs.enabled,
            "demote_above": knobs.demote_above,
            "exhausted_at": knobs.exhausted_at,
            "routed": routed,
            "tracked": tracked,
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

    async def _tracked_loop(self) -> None:
        interval = max(1.0, self.codexbar.poll_interval_seconds)
        while True:
            await self.poll_tracked()
            await asyncio.sleep(interval)

    def start(self) -> None:
        if self._tasks or not (self.sources or self.tracking):
            return
        for provider in self.sources:
            self._tasks.append(asyncio.create_task(self._loop(provider)))
        if self.tracking:
            self._tasks.append(asyncio.create_task(self._tracked_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
