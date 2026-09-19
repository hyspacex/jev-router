"""Quota sources, the snapshot poller and the pure pressure function."""

from __future__ import annotations

import json
import sys
import time

import pytest

from conftest import make_config
from jev_router.config import QuotaPolicyCfg, QuotaSourceCfg
from jev_router.quota import (
    QUOTA_SOURCES,
    QuotaMonitor,
    QuotaSnapshot,
    build_quota_source,
    compute_pressure,
    expected_percent,
    extract,
    first_non_null,
    parse_timestamp,
    snapshot_from_payload,
)

KNOBS = QuotaPolicyCfg()

# The shape the open-source CodexBar CLI prints. One provider fills in the
# secondary window and reports a pace, another fills in the primary and does
# not, which is why every path in the config is a list.
CODEXBAR_WEEKLY = [
    {
        "provider": "codex",
        "usage": {
            "secondary": {
                "usedPercent": 20,
                "windowMinutes": 10080,
                "resetsAt": "2026-09-24T15:42:05Z",
            },
            "primary": None,
        },
        "pace": {"secondary": {"expectedUsedPercent": 29, "willLastToReset": True}},
    }
]

CODEXBAR_PRIMARY_ONLY = [
    {
        "provider": "grok",
        "usage": {"primary": {"usedPercent": 6, "resetsAt": "2026-09-20T21:28:25Z"}},
    }
]

PATHS = {
    "used_percent": ["0.usage.secondary.usedPercent", "0.usage.primary.usedPercent"],
    "expected_used_percent": [
        "0.pace.secondary.expectedUsedPercent",
        "0.pace.primary.expectedUsedPercent",
    ],
    "window_minutes": ["0.usage.secondary.windowMinutes", "0.usage.primary.windowMinutes"],
    "resets_at": ["0.usage.secondary.resetsAt", "0.usage.primary.resetsAt"],
}


# --- reading fields out of a payload ------------------------------------


def test_a_dotted_path_walks_lists_and_maps():
    assert extract(CODEXBAR_WEEKLY, "0.usage.secondary.usedPercent") == 20
    assert extract(CODEXBAR_WEEKLY, "0.provider") == "codex"
    assert extract(CODEXBAR_WEEKLY, "9.usage") is None
    assert extract(CODEXBAR_WEEKLY, "0.nope.deeper") is None
    assert extract(CODEXBAR_WEEKLY, "0.provider.nope") is None


def test_the_first_path_that_has_a_value_wins():
    assert first_non_null(CODEXBAR_WEEKLY, PATHS["used_percent"]) == 20
    assert first_non_null(CODEXBAR_PRIMARY_ONLY, PATHS["used_percent"]) == 6
    assert first_non_null(CODEXBAR_PRIMARY_ONLY, PATHS["expected_used_percent"]) is None
    assert first_non_null(CODEXBAR_WEEKLY, None) is None
    assert first_non_null(CODEXBAR_WEEKLY, "0.provider") == "codex"


def test_timestamps_parse_from_iso_or_a_number():
    assert parse_timestamp("2026-09-24T15:42:05Z") == pytest.approx(1790264525, abs=2)
    assert parse_timestamp("2026-09-24T15:42:05+00:00") == parse_timestamp(
        "2026-09-24T15:42:05Z"
    )
    assert parse_timestamp(1790264525) == 1790264525
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None


def test_a_snapshot_reads_every_field_it_can_find():
    snap = snapshot_from_payload("openai", CODEXBAR_WEEKLY, PATHS, now=100.0)
    assert snap.ok
    assert snap.used_percent == 20
    assert snap.expected_used_percent == 29
    assert snap.window_minutes == 10080
    assert snap.resets_at == pytest.approx(1790264525, abs=2)


def test_a_payload_with_no_used_percent_is_an_error_not_a_crash():
    snap = snapshot_from_payload("openai", [{"usage": {}}], PATHS, now=100.0)
    assert not snap.ok
    assert "used_percent" in snap.error


# --- pressure -----------------------------------------------------------


def snap(**kwargs) -> QuotaSnapshot:
    base = {"provider": "openai", "fetched_at": 1000.0}
    return QuotaSnapshot(**{**base, **kwargs})


def test_a_provider_on_pace_is_neutral_even_when_mostly_spent():
    on_pace = snap(used_percent=80, expected_used_percent=80)
    assert compute_pressure(on_pace, KNOBS, now=1000.0)[0] == 0.0


def test_a_provider_ahead_of_pace_is_under_pressure_even_when_barely_spent():
    ahead = snap(used_percent=30, expected_used_percent=5)
    value, why = compute_pressure(ahead, KNOBS, now=1000.0)
    assert value == 1.0
    assert why == "pace"


def test_the_codexbar_example_is_behind_pace_and_therefore_neutral():
    behind = snapshot_from_payload("openai", CODEXBAR_WEEKLY, PATHS, now=1000.0)
    assert compute_pressure(behind, KNOBS, now=1000.0)[0] == 0.0


def test_pressure_rises_smoothly_and_never_falls_as_a_provider_gets_further_ahead():
    seen = [
        compute_pressure(snap(used_percent=20 + n, expected_used_percent=20), KNOBS, 1000.0)[0]
        for n in range(0, 30)
    ]
    assert seen == sorted(seen)
    assert seen[0] == 0.0
    assert seen[-1] == 1.0
    assert 0.0 < seen[10] < 1.0


def test_a_nearly_spent_window_is_full_pressure_whatever_the_pace_says():
    nearly_gone = snap(used_percent=92, expected_used_percent=95)
    value, why = compute_pressure(nearly_gone, KNOBS, now=1000.0)
    assert value == 1.0
    assert "full mark" in why


def test_pace_is_derived_from_the_window_when_the_source_gives_none():
    # A 100 minute window with 40 minutes left is 60% elapsed.
    partway = snap(used_percent=60, window_minutes=100, resets_at=1000.0 + 40 * 60)
    assert expected_percent(partway, 1000.0) == pytest.approx(60.0)
    assert compute_pressure(partway, KNOBS, now=1000.0)[0] == 0.0

    spending_fast = snap(used_percent=85, window_minutes=100, resets_at=1000.0 + 40 * 60)
    assert compute_pressure(spending_fast, KNOBS, now=1000.0)[0] == 1.0


def test_with_no_pace_at_all_the_raw_percent_is_used_with_its_own_knees():
    assert compute_pressure(snap(used_percent=55), KNOBS, 1000.0) == (0.0, "raw percent, no pace")
    assert compute_pressure(snap(used_percent=77), KNOBS, 1000.0)[0] == pytest.approx(
        (77 - 60) / 35
    )
    assert compute_pressure(snap(used_percent=89), KNOBS, 1000.0)[0] == pytest.approx(
        (89 - 60) / 35
    )


def test_every_kind_of_failure_is_neutral():
    assert compute_pressure(None, KNOBS, 1000.0)[0] == 0.0
    assert compute_pressure(snap(error="the command timed out"), KNOBS, 1000.0)[0] == 0.0
    assert compute_pressure(snap(used_percent=None), KNOBS, 1000.0)[0] == 0.0
    stale = snap(used_percent=99, fetched_at=0.0)
    value, why = compute_pressure(stale, KNOBS, now=100_000.0, stale_after_seconds=1800)
    assert value == 0.0
    assert why == "the snapshot is stale"


def test_a_small_move_is_held_and_a_large_one_is_rate_limited():
    held, why = compute_pressure(
        snap(used_percent=61), KNOBS, 1000.0, previous=0.03
    )
    assert held == 0.03
    assert "hysteresis" in why

    jumped, why = compute_pressure(
        snap(used_percent=40, expected_used_percent=5), KNOBS, 1000.0, previous=0.0
    )
    assert jumped == pytest.approx(KNOBS.max_change_per_poll)
    assert "rate limited" in why


def test_pressure_stays_inside_zero_and_one():
    for used in (-50, 0, 50, 100, 500):
        value, _ = compute_pressure(snap(used_percent=used, expected_used_percent=0), KNOBS, 1000.0)
        assert 0.0 <= value <= 1.0


# --- the shipped sources ------------------------------------------------


def test_every_shipped_source_is_registered():
    assert set(QUOTA_SOURCES) == {"none", "static", "command"}


async def test_the_none_source_reports_nothing():
    source = build_quota_source("ollama", QuotaSourceCfg(source="none"))
    snapshot = await source.fetch()
    assert not snapshot.ok
    assert snapshot.error == "no quota source"


async def test_the_static_source_reads_numbers_straight_from_the_config():
    source = build_quota_source(
        "openai",
        QuotaSourceCfg(source="static", config={"used_percent": 70, "expected_used_percent": 40}),
    )
    snapshot = await source.fetch()
    assert snapshot.used_percent == 70
    assert compute_pressure(snapshot, KNOBS, time.time())[0] == pytest.approx(30 / 25, abs=1)


async def test_the_static_source_with_no_number_is_an_error():
    source = build_quota_source("openai", QuotaSourceCfg(source="static", config={}))
    assert not (await source.fetch()).ok


def command_source(script: str, **extra):
    return build_quota_source(
        "openai",
        QuotaSourceCfg(
            source="command",
            config={"argv": [sys.executable, "-c", script], "timeout_ms": 5000, **extra},
        ),
    )


def printer(payload) -> str:
    """A one-liner that prints this payload as JSON, for the command source."""
    return f"print({json.dumps(payload)!r})"


async def test_the_command_source_runs_a_program_and_parses_its_output():
    snapshot = await command_source(printer(CODEXBAR_WEEKLY), **PATHS).fetch()
    assert snapshot.ok
    assert snapshot.used_percent == 20
    assert snapshot.expected_used_percent == 29


async def test_the_command_source_handles_the_primary_only_shape():
    snapshot = await command_source(printer(CODEXBAR_PRIMARY_ONLY), **PATHS).fetch()
    assert snapshot.used_percent == 6
    assert snapshot.expected_used_percent is None
    assert snapshot.window_minutes is None


@pytest.mark.parametrize(
    "script, expected",
    [
        ("raise SystemExit(3)", "exited 3"),
        ("print('not json')", "did not print JSON"),
        ("print('{}')", "used_percent"),
    ],
)
async def test_a_failing_command_becomes_an_error_on_the_snapshot(script, expected):
    snapshot = await command_source(script, **PATHS).fetch()
    assert not snapshot.ok
    assert expected in snapshot.error


async def test_a_command_that_does_not_exist_is_an_error_not_a_crash():
    source = build_quota_source(
        "openai", QuotaSourceCfg(source="command", config={"argv": ["/nonexistent/binary"]})
    )
    snapshot = await source.fetch()
    assert not snapshot.ok
    assert "could not start" in snapshot.error


async def test_a_command_with_no_argv_is_an_error():
    source = build_quota_source("openai", QuotaSourceCfg(source="command", config={}))
    assert "no argv" in (await source.fetch()).error


async def test_a_slow_command_is_killed_and_reported():
    source = build_quota_source(
        "openai",
        QuotaSourceCfg(
            source="command",
            config={"argv": [sys.executable, "-c", "import time;time.sleep(30)"],
                    "timeout_ms": 200},
        ),
    )
    snapshot = await source.fetch()
    assert snapshot.error == "the command timed out"


# --- the monitor --------------------------------------------------------


def monitor_config(**quota):
    return make_config(
        providers={
            "cloud": {"quota": {"source": "static", "enabled": True, **quota}},
            "local": {"quota": {"source": "none", "enabled": False}},
        },
        models={"small": {"provider": "local"}, "big": {"provider": "cloud"},
                "paramy": {"provider": "cloud"}},
    )


async def test_the_monitor_polls_and_keeps_a_snapshot():
    cfg = monitor_config(config={"used_percent": 80, "expected_used_percent": 50})
    monitor = QuotaMonitor(cfg)
    assert monitor.pressures() == {"cloud": 0.0}   # nothing polled yet
    await monitor.poll_once("cloud")
    assert monitor.pressures()["cloud"] > 0
    report = monitor.report()
    assert report["providers"]["cloud"]["snapshot"]["used_percent"] == 80
    assert report["providers"]["local"]["enabled"] is False
    assert report["providers"]["local"]["pressure"] == 0.0


async def test_a_provider_with_no_source_is_left_out_of_the_pressures():
    cfg = monitor_config(config={"used_percent": 10})
    monitor = QuotaMonitor(cfg)
    assert set(monitor.pressures()) == {"cloud"}


async def test_the_monitor_never_raises_when_a_source_does():
    class Exploding:
        name = "boom"

        async def fetch(self):
            raise RuntimeError("no")

    cfg = monitor_config(config={"used_percent": 10})
    monitor = QuotaMonitor(cfg)
    monitor.sources["cloud"] = Exploding()
    state = await monitor.poll_once("cloud")
    assert state.pressure == 0.0
    assert "RuntimeError" in state.snapshot.error
    assert monitor.report()["providers"]["cloud"]["errors"] == 1


async def test_a_snapshot_that_ages_out_drops_back_to_neutral():
    cfg = monitor_config(
        stale_after_seconds=1, config={"used_percent": 99, "expected_used_percent": 10}
    )
    monitor = QuotaMonitor(cfg)
    await monitor.poll_once("cloud")
    assert monitor.pressures()["cloud"] > 0

    monitor.clock = lambda: time.time() + 3600
    monitor.refresh_staleness()
    assert monitor.pressures()["cloud"] == 0.0
    assert monitor.report()["providers"]["cloud"]["stale"] is True


async def test_a_disabled_source_is_never_built_and_never_polled():
    cfg = make_config(
        providers={"cloud": {"quota": {"source": "command", "enabled": False,
                                       "config": {"argv": ["/bin/false"]}}}},
        models={"small": {"provider": "cloud"}, "big": {"provider": "cloud"},
                "paramy": {"provider": "cloud"}},
    )
    monitor = QuotaMonitor(cfg)
    assert monitor.sources == {}
    monitor.start()
    assert monitor._tasks == []
    await monitor.stop()
