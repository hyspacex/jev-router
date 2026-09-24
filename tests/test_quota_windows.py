"""Overlapping quota windows, and what an unknown reading is worth.

Named cases from docs/R2_SPEC.md 13.2 covered here:

    C27  a missing, errored or stale snapshot stays visible as unknown and is
         never reported as unused capacity
    C28  several windows keep their own usage and reset together, and the
         highest relevant pressure wins
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import make_config
from jev_router.config import QuotaPolicyCfg, QuotaSourceCfg
from jev_router.quota import (
    QuotaMonitor,
    QuotaSnapshot,
    QuotaWindow,
    build_quota_source,
    compute_pressure,
    exhausted_window,
    raw_pressure,
    snapshot_from_payload,
    snapshot_status,
    window_pressure,
)

KNOBS = QuotaPolicyCfg()
NOW = 1_000_000.0
HOUR = 3600.0


def window(name: str, **fields) -> QuotaWindow:
    return QuotaWindow(name=name, **fields)


# --- C28 -----------------------------------------------------------------


def test_c28_pressure_is_the_highest_of_the_fresh_windows():
    snapshot = QuotaSnapshot(
        provider="openai",
        fetched_at=NOW,
        windows=(
            # On pace: five hours in, five hours left, half spent.
            window("five_hour", used_percent=50.0, window_minutes=600,
                   resets_at=NOW + 5 * HOUR),
            # Far ahead of pace: a whole week left and most of it spent.
            window("weekly", used_percent=80.0, window_minutes=10080,
                   resets_at=NOW + 168 * HOUR),
        ),
    )
    value, why = raw_pressure(snapshot, KNOBS, NOW)
    assert value == 1.0
    assert "weekly" in why


def test_c28_one_windows_usage_is_never_read_against_anothers_reset():
    """The weekly reset must not make the five-hour window look on pace."""
    crossed = QuotaSnapshot(
        provider="openai",
        fetched_at=NOW,
        windows=(
            window("five_hour", used_percent=90.0, window_minutes=600,
                   resets_at=NOW + 5 * HOUR),
            window("weekly", used_percent=1.0, window_minutes=10080,
                   resets_at=NOW + 167 * HOUR),
        ),
    )
    five, _ = window_pressure(crossed.windows[0], KNOBS, NOW)
    weekly, _ = window_pressure(crossed.windows[1], KNOBS, NOW)
    assert five == 1.0  # at or above the full mark on its own numbers
    assert weekly < 0.05  # an hour into a week, one per cent spent
    assert raw_pressure(crossed, KNOBS, NOW)[0] == 1.0


def test_c28_a_window_with_used_and_limit_computes_its_own_percent():
    w = window("tokens", used=800.0, limit=1000.0)
    assert w.percent == pytest.approx(80.0)
    assert window("tokens", used=800.0, limit=0.0).percent is None
    assert window("tokens").percent is None


def test_c28_a_window_with_no_usage_figure_is_skipped_not_scored_as_zero():
    snapshot = QuotaSnapshot(
        provider="openai",
        fetched_at=NOW,
        windows=(
            window("five_hour"),
            window("weekly", used_percent=95.0),
        ),
    )
    assert raw_pressure(snapshot, KNOBS, NOW)[0] == 1.0


def test_c28_a_snapshot_whose_windows_all_report_nothing_is_an_error():
    snapshot = snapshot_from_payload(
        "openai",
        {"usage": {}},
        {"windows": [{"name": "five_hour", "used_percent": "usage.five.pct"}]},
        NOW,
    )
    assert snapshot.error
    assert snapshot_status(snapshot, NOW) == "error"


def test_c28_windows_are_read_out_of_a_payload_by_path():
    payload = {
        "usage": {
            "primary": {"pct": 40.0, "resetsAt": "2026-09-19T12:00:00Z", "windowMinutes": 300},
            "secondary": {"pct": 91.0},
        }
    }
    snapshot = snapshot_from_payload(
        "openai",
        payload,
        {
            "windows": [
                {
                    "name": "five_hour",
                    "used_percent": "usage.primary.pct",
                    "resets_at": "usage.primary.resetsAt",
                    "window_minutes": "usage.primary.windowMinutes",
                },
                {"name": "weekly", "used_percent": "usage.secondary.pct"},
            ]
        },
        NOW,
    )
    assert [w.name for w in snapshot.windows] == ["five_hour", "weekly"]
    assert snapshot.windows[0].used_percent == 40.0
    assert snapshot.windows[0].resets_at is not None
    assert snapshot.windows[1].resets_at is None
    # The summary is the worst window, and it is for display only.
    assert snapshot.used_percent == 91.0
    assert snapshot_status(snapshot, NOW) == "fresh"


def test_c28_a_single_window_source_is_read_exactly_as_it_always_was():
    payload = {"usage": {"pct": 70.0}}
    snapshot = snapshot_from_payload("openai", payload, {"used_percent": "usage.pct"}, NOW)
    assert snapshot.windows == ()
    assert snapshot.used_percent == 70.0
    assert raw_pressure(snapshot, KNOBS, NOW)[1] == "raw percent, no pace"


def test_c28_a_static_source_can_declare_several_windows():
    cfg = QuotaSourceCfg(
        source="static",
        enabled=True,
        config={
            "windows": [
                {"name": "five_hour", "used_percent": 10.0},
                {"name": "weekly", "used": 92.0, "limit": 100.0},
            ]
        },
    )
    snapshot = asyncio.run(build_quota_source("openai", cfg).fetch())
    assert [w.name for w in snapshot.windows] == ["five_hour", "weekly"]
    assert snapshot.windows[1].percent == pytest.approx(92.0)
    assert raw_pressure(snapshot, KNOBS, snapshot.fetched_at)[0] == 1.0


# --- C27 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot,expected",
    [
        (None, "unknown"),
        (QuotaSnapshot(provider="p", fetched_at=NOW), "unknown"),
        (QuotaSnapshot(provider="p", fetched_at=NOW, error="the command timed out"), "error"),
        (QuotaSnapshot(provider="p", fetched_at=NOW, used_percent=10.0), "fresh"),
    ],
)
def test_c27_a_reading_reports_what_it_is_worth(snapshot, expected):
    assert snapshot_status(snapshot, NOW, 1800) == expected


def test_c27_an_aged_reading_reads_stale_not_fresh():
    snapshot = QuotaSnapshot(provider="p", fetched_at=NOW - 3600, used_percent=99.0)
    assert snapshot_status(snapshot, NOW, 1800) == "stale"
    # Numerically neutral, which routes as the router routed before quota...
    assert compute_pressure(snapshot, KNOBS, NOW, stale_after_seconds=1800) == (
        0.0,
        "the snapshot is stale",
    )
    # ...and still visible as stale rather than as spare capacity.
    assert snapshot_status(snapshot, NOW, 1800) != "fresh"


def test_c27_unknown_and_error_contribute_nothing_and_say_so():
    for snapshot in (
        None,
        QuotaSnapshot(provider="p", fetched_at=NOW),
        QuotaSnapshot(provider="p", fetched_at=NOW, error="the command exited 1"),
    ):
        value, why = compute_pressure(snapshot, KNOBS, NOW)
        assert value == 0.0
        assert "unused" not in why and "spare" not in why


def test_c27_the_report_carries_the_status_and_the_windows():
    config = make_config(
        providers={
            "budget": {
                "quota": {
                    "source": "static",
                    "enabled": True,
                    "config": {
                        "windows": [
                            {"name": "five_hour", "used_percent": 20.0},
                            {"name": "weekly", "used_percent": 30.0},
                        ]
                    },
                }
            },
            "premium": {},
        },
        models={
            "small": {"provider": "budget"},
            "big": {"provider": "premium"},
            "paramy": {"provider": "budget"},
        },
    )
    monitor = QuotaMonitor(config)
    asyncio.run(monitor.poll_once("budget"))
    report = monitor.report()

    budget = report["providers"]["budget"]
    assert budget["status"] == "fresh"
    assert [w["name"] for w in budget["windows"]] == ["five_hour", "weekly"]
    # A provider with no source reports unknown, never zero-used.
    premium = report["providers"]["premium"]
    assert premium["status"] == "unknown"
    assert premium["snapshot"] is None
    assert premium["windows"] == []


def test_c27_a_provider_with_no_source_is_unknown_in_the_resolve_response():
    from jev_router.app import Router

    config = make_config()
    router = Router(config)
    try:
        assert set(router.quota_status().values()) == {"unknown"}
    finally:
        router.sessions.close()
        router.store.close()


def test_c27_compute_pressure_is_still_pure():
    """No clock, no I/O: the caller supplies the time."""
    snapshot = QuotaSnapshot(
        provider="p",
        fetched_at=NOW,
        windows=(window("w", used_percent=50.0, window_minutes=600,
                        resets_at=NOW + 5 * HOUR),),
    )
    first = compute_pressure(snapshot, KNOBS, NOW)
    second = compute_pressure(snapshot, KNOBS, NOW)
    assert first == second


def test_only_a_fresh_spent_window_is_exhausted():
    def snap(**fields):
        return QuotaSnapshot(provider="p", fetched_at=NOW, windows=(
            window("five_hour", used_percent=40.0, resets_at=NOW + HOUR),
            window("weekly", resets_at=NOW + 24 * HOUR, **fields),
        ))

    spent = exhausted_window(snap(used_percent=100.0), KNOBS, NOW)
    assert (spent.name, spent.resets_at) == ("weekly", NOW + 24 * HOUR)
    assert exhausted_window(snap(used_percent=99.0), KNOBS, NOW) is None
    reserve = QuotaPolicyCfg(exhausted_at=98.0)
    assert exhausted_window(snap(used_percent=99.0), reserve, NOW).name == "weekly"
    # A stale reading says nothing, however spent it looked.
    later = NOW + 2 * HOUR
    assert exhausted_window(snap(used_percent=100.0), KNOBS, later, HOUR) is None
