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
    assert set(QUOTA_SOURCES) == {"none", "static", "command", "codexbar"}


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


# --- CodexBar -----------------------------------------------------------
#
# The fixture is a sanitized copy of CodexBar.app's widget-snapshot.json with
# made-up numbers. Every window's reset is a round distance from
# `generatedAt`, so each pace below is exact.

from pathlib import Path  # noqa: E402

from jev_router.config import CodexBarCfg  # noqa: E402
from jev_router.quota import (  # noqa: E402
    CodexBarSource,
    foreign_label,
    parse_widget_snapshot,
    read_widget_snapshot,
    snapshot_candidates,
    snapshot_status,
    steering_label,
)

FIXTURE = Path(__file__).parent / "fixtures" / "codexbar_widget_snapshot.json"
GENERATED = parse_timestamp("2026-10-27T06:53:34Z")


def fixture_data() -> dict:
    return json.loads(FIXTURE.read_text())


def write_snapshot(tmp_path, data=None, name="widget-snapshot.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(fixture_data() if data is None else data))
    return path


def write_codexbar_config(tmp_path, enabled=("codex", "claude", "antigravity", "ollama", "grok")):
    path = tmp_path / "codexbar-config.json"
    providers = [{"id": pid, "enabled": True} for pid in enabled]
    providers.append({"id": "kimi", "enabled": False, "apiKey": "sk-never-read"})
    path.write_text(json.dumps({"version": 1, "providers": providers}))
    return path


def bar_settings(tmp_path, **extra) -> dict:
    out = {
        "enabled": True,
        "snapshot_path": str(tmp_path / "widget-snapshot.json"),
        "config_path": str(tmp_path / "codexbar-config.json"),
        "cli_binary": "",
    }
    out.update(extra)
    return out


def codexbar_config(tmp_path, bar=None, quota_policy=None, **providers):
    """openai reads `codex`, ollama reads `ollama`, unless told otherwise."""
    links = providers or {"openai": "codex", "ollama": "ollama"}
    raw = {
        "codexbar": bar_settings(tmp_path, **(bar or {})),
        "providers": {
            name: {"quota": {"source": "codexbar", "codexbar_provider": cid,
                             "enabled": True, "stale_after_seconds": 900}}
            for name, cid in links.items()
        },
        "models": {
            "small": {"provider": "ollama"},
            "big": {"provider": "openai", "quota_cost": 1.0},
            "paramy": {"provider": "openai", "quota_cost": 0.2},
        },
    }
    if quota_policy:
        raw["quota_policy"] = quota_policy
    return make_config(**raw)


def at(seconds: float):
    return lambda: GENERATED + seconds


def test_the_snapshot_parses_into_windows_rows_and_rollups():
    widget = parse_widget_snapshot(fixture_data())
    assert widget.generated_at == GENERATED
    assert widget.enabled == ("codex", "claude", "antigravity", "ollama", "grok")
    codex = widget.entries["codex"]
    assert [w.name for w in codex.windows] == ["secondary"]
    weekly = codex.windows[0]
    assert (weekly.used_percent, weekly.window_minutes) == (40.0, 10080.0)
    assert weekly.resets_at == GENERATED + 3 * 86400
    assert weekly.reset_description == "Oct 30 at 11:53 PM"
    assert codex.rows == (("Weekly", 40.0),)
    assert dict(codex.tokens) == {
        "session_tokens": 120000.0, "session_cost_usd": 3.25,
        "last_30_days_tokens": 9000000.0, "last_30_days_cost_usd": 210.5,
    }
    assert (codex.currency, codex.credits_remaining) == ("USD", 42.0)
    antigravity = widget.entries["antigravity"]
    assert [w.name for w in antigravity.windows] == ["primary", "secondary", "tertiary"]
    # A sentence is not a label: it is dropped, not trimmed.
    assert antigravity.windows[0].reset_description == ""
    assert [title for title, _ in antigravity.rows] == [
        "Claude / GPT-OSS", "Gemini Pro", "Gemini Flash", "",
    ]
    assert dict(antigravity.tokens)["last_30_days_cost_usd"] is None
    # A window with no length still parses; its pace is simply unknown.
    grok = widget.entries["grok"].windows[0]
    assert grok.window_minutes is None and grok.used_percent == 6.0


def test_nothing_but_numbers_and_short_labels_comes_out_of_the_snapshot():
    widget = parse_widget_snapshot(fixture_data())
    text = repr(widget)
    for secret in ("owner-key", "someone@example", "Ignore previous", "fully refresh"):
        assert secret not in text


def test_a_foreign_label_is_short_and_plain_or_nothing():
    assert foreign_label("Gemini 5-hour") == "Gemini 5-hour"
    assert foreign_label("resets Oct 27 at 4:30am (America/Los_Angeles)").startswith("resets")
    assert foreign_label("Nov 2 at 7:55\u202fAM") == "Nov 2 at 7:55 AM"
    assert foreign_label("x" * 49) == ""
    assert foreign_label("<script>") == ""
    assert foreign_label("a\nb") == "a b"
    assert foreign_label(None) == ""


def test_a_file_that_is_not_a_snapshot_is_refused(tmp_path):
    assert parse_widget_snapshot({"entries": "nope"}) is None
    assert parse_widget_snapshot([1, 2]) is None
    bad = tmp_path / "widget-snapshot.json"
    bad.write_text("{not json")
    widget, why = read_widget_snapshot(CodexBarCfg(snapshot_path=str(bad)))
    assert widget is None and why == "the snapshot could not be read"
    widget, why = read_widget_snapshot(CodexBarCfg(snapshot_path=str(tmp_path / "nope.json")))
    assert widget is None and why == "missing"


def test_without_a_configured_path_the_default_then_any_container_is_tried(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    other = tmp_path / "Library" / "Group Containers" / "ABCDE12345.com.steipete.codexbar"
    other.mkdir(parents=True)
    write_snapshot(other)
    candidates = snapshot_candidates(CodexBarCfg())
    assert candidates[0].parent.name == "Y5PE65HELJ.com.steipete.codexbar"
    assert candidates[1].parent == other
    widget, _ = read_widget_snapshot(CodexBarCfg())
    assert widget is not None and "codex" in widget.entries
    # A configured path is the only place looked.
    assert snapshot_candidates(CodexBarCfg(snapshot_path="~/x.json")) == [tmp_path / "x.json"]


async def test_a_fresh_snapshot_is_read_without_the_cli(tmp_path):
    write_snapshot(tmp_path)
    source = CodexBarSource("openai", "codex", CodexBarCfg(**bar_settings(tmp_path)), clock=at(30))
    snapshot = await source.fetch()
    assert source.via == "snapshot"
    # fetched_at is when CodexBar measured, not when the file was read.
    assert snapshot.fetched_at == GENERATED - 34
    assert [w.name for w in snapshot.windows] == ["secondary"]
    assert snapshot_status(snapshot, GENERATED + 30, 900) == "fresh"
    assert snapshot_status(snapshot, GENERATED + 1000, 900) == "stale"


async def test_a_missing_file_with_no_cli_is_unknown_and_pressure_zero(tmp_path):
    cfg = codexbar_config(tmp_path)
    monitor = QuotaMonitor(cfg, clock=at(0))
    await monitor.poll_once("openai")
    assert monitor.pressures()["openai"] == 0.0
    row = monitor.all_snapshots()["routed"][0]
    assert (row["provider"], row["status"], row["steering"], row["via"]) == (
        "openai", "unknown", None, None,
    )
    assert row["windows"] == []


async def test_a_cli_that_is_not_installed_is_the_same_as_none(tmp_path):
    settings = CodexBarCfg(**bar_settings(tmp_path, cli_binary="no-such-codexbar-binary"))
    snapshot = await CodexBarSource("openai", "codex", settings).fetch()
    assert snapshot_status(snapshot, time.time(), 900) == "unknown"


def fake_cli(tmp_path, payload, exit_code=0) -> Path:
    """A stand-in for the CodexBar CLI that logs its argv and prints JSON."""
    log_path = tmp_path / "cli-calls.log"
    script = tmp_path / "fake-codexbar"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log_path)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"print(json.dumps({payload!r}))\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def cli_calls(tmp_path) -> list[str]:
    path = tmp_path / "cli-calls.log"
    return path.read_text().splitlines() if path.exists() else []


CLI_CODEX = [{
    "provider": "codex",
    "usage": {
        "primary": None,
        "secondary": {"usedPercent": 41, "windowMinutes": 10080,
                      "resetsAt": "2026-10-30T06:53:34Z"},
        "accountEmail": "someone@example.invalid",
    },
    "pace": {"secondary": {"expectedUsedPercent": 50, "deltaPercent": -9}},
}]


async def test_an_old_snapshot_falls_back_to_the_cli_at_most_once_per_interval(tmp_path):
    write_snapshot(tmp_path)
    script = fake_cli(tmp_path, CLI_CODEX)
    settings = CodexBarCfg(**bar_settings(tmp_path, cli_binary=str(script)))
    now = [GENERATED + 400]   # past cli_fallback_after_seconds (300)
    source = CodexBarSource("openai", "codex", settings, clock=lambda: now[0])
    snapshot = await source.fetch()
    assert source.via == "cli"
    assert cli_calls(tmp_path) == ["usage --provider codex --json-only"]
    assert [(w.name, w.used_percent, w.expected_used_percent) for w in snapshot.windows] == [
        ("secondary", 41.0, 50.0),
    ]
    assert "someone@example" not in repr(snapshot)

    now[0] += 60
    await source.fetch()
    assert len(cli_calls(tmp_path)) == 1   # reused, not run again
    now[0] += 300
    await source.fetch()
    assert len(cli_calls(tmp_path)) == 2


async def test_a_missing_file_falls_back_to_the_cli(tmp_path):
    script = fake_cli(tmp_path, CLI_CODEX)
    settings = CodexBarCfg(**bar_settings(tmp_path, cli_binary=str(script)))
    source = CodexBarSource("openai", "codex", settings, clock=at(0))
    snapshot = await source.fetch()
    assert source.via == "cli" and snapshot.ok


async def test_a_failing_cli_keeps_the_old_snapshot_and_its_own_age_decides(tmp_path):
    write_snapshot(tmp_path)
    script = fake_cli(tmp_path, {"error": "no"}, exit_code=1)
    settings = CodexBarCfg(**bar_settings(tmp_path, cli_binary=str(script)))
    source = CodexBarSource("openai", "codex", settings, clock=at(400))
    snapshot = await source.fetch()
    assert source.via == "snapshot"
    assert snapshot.windows[0].used_percent == 40.0
    assert snapshot_status(snapshot, GENERATED + 400, 900) == "fresh"
    assert snapshot_status(snapshot, GENERATED + 400, 300) == "stale"


async def test_a_failing_cli_with_no_file_is_an_error_and_pressure_zero(tmp_path):
    script = fake_cli(tmp_path, {}, exit_code=3)
    cfg = codexbar_config(tmp_path, bar={"cli_binary": str(script)})
    monitor = QuotaMonitor(cfg, clock=at(0))
    await monitor.poll_once("openai")
    assert monitor.pressures()["openai"] == 0.0
    row = monitor.all_snapshots()["routed"][0]
    assert row["status"] == "error" and row["last_error"] == "the command exited 3"


async def test_codexbar_pressure_goes_through_the_same_pure_function(tmp_path):
    write_snapshot(tmp_path)
    cfg = codexbar_config(tmp_path)
    monitor = QuotaMonitor(cfg, clock=at(0))
    for provider in ("openai", "ollama"):
        await monitor.poll_once(provider)
    # codex is 17 points behind pace; ollama's weekly is 5.4 ahead.
    snap = monitor.states["ollama"].snapshot
    expected, _ = compute_pressure(snap, cfg.quota_policy, GENERATED, stale_after_seconds=900)
    assert monitor.pressures() == {"openai": 0.0, "ollama": pytest.approx(expected)}
    assert expected == pytest.approx((34 - 100 * 2 / 7) / 25, abs=1e-6)


async def test_all_snapshots_carries_each_windows_pace(tmp_path):
    write_snapshot(tmp_path)
    cfg = codexbar_config(tmp_path)
    monitor = QuotaMonitor(cfg, clock=at(0))
    await monitor.poll_once("ollama")
    live = monitor.all_snapshots()
    assert set(live) == {
        "as_of", "quota_routing", "demote_above", "exhausted_at", "routed", "tracked",
    }
    ollama = next(r for r in live["routed"] if r["provider"] == "ollama")
    assert ollama["source"] == "codexbar" and ollama["codexbar_provider"] == "ollama"
    assert ollama["via"] == "snapshot" and ollama["status"] == "fresh"
    assert ollama["age_seconds"] == 24.0
    primary, secondary = ollama["windows"]
    assert primary == {
        "name": "primary", "used_percent": 18.0, "expected_percent": 60.0,
        "pace_delta": -42.0, "resets_at": GENERATED + 2 * 3600, "window_minutes": 300.0,
        "reset_description": None,
    }
    assert secondary["expected_percent"] == pytest.approx(28.57)
    assert secondary["pace_delta"] == pytest.approx(5.43)
    # `small` has no quota_cost in the config: reported as unknown, never as
    # the schema's 1.0, while its pressure is still its provider's in full.
    assert ollama["models"] == [
        {"model": "small", "quota_cost": None, "pressure": ollama["pressure"]},
    ]
    assert (ollama["demote_above"], ollama["exhausted_at"]) == (0.6, 100.0)
    openai = next(r for r in live["routed"] if r["provider"] == "openai")
    assert openai["status"] == "unknown"   # not polled yet: a gap, not zero
    assert [m["model"] for m in openai["models"]] == ["big", "paramy"]
    assert [m["quota_cost"] for m in openai["models"]] == [1.0, 0.2]


async def test_pace_delta_is_none_when_either_side_is_unknown(tmp_path):
    write_snapshot(tmp_path)
    cfg = codexbar_config(tmp_path, openai="grok", ollama="ollama")
    monitor = QuotaMonitor(cfg, clock=at(0))
    await monitor.poll_once("openai")
    window = monitor.all_snapshots()["routed"][0]["windows"][0]
    assert window["used_percent"] == 6.0
    assert window["expected_percent"] is None and window["pace_delta"] is None


def test_steering_labels_sit_on_the_policy_thresholds():
    knobs = QuotaPolicyCfg()
    assert steering_label(0.0, "fresh", False, knobs) == "calm"
    assert steering_label(0.01, "fresh", False, knobs) == "elevated"
    assert steering_label(knobs.demote_above, "fresh", False, knobs) == "elevated"
    assert steering_label(knobs.demote_above + 0.01, "fresh", False, knobs) == "demoting"
    assert steering_label(0.0, "fresh", True, knobs) == "exhausted"
    # The numbers come from quota_policy, not from the design's sketch.
    low = QuotaPolicyCfg(demote_above=0.2)
    assert steering_label(0.3, "fresh", False, low) == "demoting"
    assert steering_label(0.3, "fresh", False, knobs) == "elevated"


@pytest.mark.parametrize("status", ["stale", "unknown", "error"])
def test_a_reading_that_is_not_fresh_steers_nothing(status):
    assert steering_label(0.9, status, False, QuotaPolicyCfg()) is None


def test_quota_routing_off_steers_nothing():
    assert steering_label(0.9, "fresh", False, QuotaPolicyCfg(enabled=False)) is None
    assert steering_label(0.9, "fresh", False, QuotaPolicyCfg(), enabled=False) is None


async def test_the_monitor_labels_steering_from_its_own_pressure(tmp_path):
    write_snapshot(tmp_path)
    cfg = codexbar_config(tmp_path, quota_policy={"pressure_full_at": 5.0})
    monitor = QuotaMonitor(cfg, clock=at(0))
    for provider in ("openai", "ollama"):
        await monitor.poll_once(provider)
    rows = {r["provider"]: r for r in monitor.all_snapshots()["routed"]}
    assert rows["openai"]["steering"] == "calm"
    assert rows["ollama"]["pressure"] == 1.0
    assert rows["ollama"]["steering"] == "demoting"
    assert rows["ollama"]["models"][0]["pressure"] == 1.0

    spent = codexbar_config(tmp_path, quota_policy={"exhausted_at": 30.0})
    monitor = QuotaMonitor(spent, clock=at(0))
    await monitor.poll_once("ollama")
    row = next(r for r in monitor.all_snapshots()["routed"] if r["provider"] == "ollama")
    assert row["steering"] == "exhausted"
    assert row["exhausted"]["name"] == "secondary"


async def test_unlinked_subscriptions_are_tracked_and_never_routed(tmp_path):
    write_snapshot(tmp_path)
    write_codexbar_config(tmp_path)
    cfg = codexbar_config(tmp_path)
    monitor = QuotaMonitor(cfg, clock=at(0))
    await monitor.poll_tracked()
    assert list(monitor.tracked) == ["claude", "antigravity", "grok"]
    assert set(monitor.pressures()) == {"openai", "ollama"}
    assert monitor.exhausted() == {}
    live = monitor.all_snapshots()
    claude = live["tracked"][0]
    assert (claude["id"], claude["status"], claude["via"]) == ("claude", "fresh", "snapshot")
    assert [w["name"] for w in claude["windows"]] == ["primary", "secondary"]
    assert claude["windows"][0]["expected_percent"] == 50.0
    assert claude["windows"][0]["pace_delta"] == -38.0
    assert claude["windows"][0]["reset_description"].startswith("resets Oct 27")
    assert claude["plans"] == [
        {"title": "Session", "used_percent": 12.0},
        {"title": "Weekly", "used_percent": 43.0},
    ]
    assert claude["tokens"]["last_30_days_cost_usd"] == 812.0
    assert claude["currency"] == "USD"
    assert "steering" not in claude and "pressure" not in claude
    antigravity = live["tracked"][1]
    assert [w["name"] for w in antigravity["windows"]] == ["primary", "secondary", "tertiary"]
    assert antigravity["plans"][-1] == {"title": None, "used_percent": 50.0}
    assert "owner-key" not in json.dumps(live)


async def test_tracked_follows_both_codexbar_lists_when_both_are_readable(tmp_path):
    write_snapshot(tmp_path)
    write_codexbar_config(tmp_path, enabled=("codex", "claude"))
    monitor = QuotaMonitor(codexbar_config(tmp_path), clock=at(0))
    await monitor.poll_tracked()
    assert list(monitor.tracked) == ["claude"]
    # Without CodexBar's config, the snapshot's own list is used.
    (tmp_path / "codexbar-config.json").unlink()
    await monitor.poll_tracked()
    assert list(monitor.tracked) == ["claude", "antigravity", "grok"]


async def test_a_disabled_link_leaves_its_subscription_tracked(tmp_path):
    write_snapshot(tmp_path)
    cfg = codexbar_config(tmp_path)
    cfg.providers["ollama"].quota.enabled = False
    monitor = QuotaMonitor(cfg, clock=at(0))
    await monitor.poll_tracked()
    assert "ollama" in monitor.tracked and "codex" not in monitor.tracked


async def test_tracking_is_off_unless_the_block_says_so(tmp_path):
    write_snapshot(tmp_path)
    off = make_config(codexbar=bar_settings(tmp_path, enabled=False))
    monitor = QuotaMonitor(off)
    await monitor.poll_tracked()
    assert monitor.tracked == {} and not monitor.tracking
    monitor.start()
    assert monitor._tasks == []
    no_external = make_config(
        codexbar=bar_settings(tmp_path, include_external_subscriptions=False)
    )
    assert not QuotaMonitor(no_external).tracking


async def test_tracking_alone_starts_a_poller(tmp_path):
    write_snapshot(tmp_path)
    monitor = QuotaMonitor(make_config(codexbar=bar_settings(tmp_path)), clock=at(0))
    assert monitor.sources == {}
    monitor.start()
    try:
        assert len(monitor._tasks) == 1
    finally:
        await monitor.stop()


async def test_a_tracked_subscription_ages_out_on_the_blocks_own_threshold(tmp_path):
    write_snapshot(tmp_path)
    monitor = QuotaMonitor(
        make_config(codexbar=bar_settings(tmp_path, stale_after_seconds=60)), clock=at(0)
    )
    await monitor.poll_tracked()
    statuses = {t["id"]: t["status"] for t in monitor.all_snapshots()["tracked"]}
    assert statuses["claude"] == "stale"      # measured 180 s before the file
    assert statuses["antigravity"] == "fresh"
