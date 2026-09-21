#!/usr/bin/env python
"""Chronological quota replay (spec 13.6). Everything it prints is simulated.

Feed it a scenario: a clock, a list of quota windows per provider with their
resets, a stream of admissions, and whatever telemetry failures you want to
see. It replays them in order through the router's own pure functions —
`quota.compute_pressure` and `policy.select` — and reports what was admitted
where, what was deferred, and how much strong-model capacity was still there
at the end.

Three things it will not do:

- It never claims a quota saving from `ROUTE_COST`. Those weights are relative
  numbers for ordering routes, not measured subscription consumption, and the
  report says so wherever cost appears.
- It never invents a reading. A window that is stale, missing or errored in
  the scenario stays unknown in the output, and unknown is not spare capacity.
- It never talks to a provider. Nothing here is evidence about a real account;
  every output is labelled `simulated`.

    uv run python evals/quota_sim.py --scenario evals/quota_scenarios/weekend.yaml
    uv run python evals/quota_sim.py --demo
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(EVALS_DIR))

from jev_router.config import RouterConfig  # noqa: E402
from jev_router.features import Features  # noqa: E402
from jev_router.policy import RoutingError, select  # noqa: E402
from jev_router.providers import merge_pressures  # noqa: E402
from jev_router.quota import (  # noqa: E402
    QuotaSnapshot,
    QuotaWindow,
    compute_pressure,
    literal_window,
    snapshot_from_windows,
    snapshot_status,
)

from common import config_with_overlay  # noqa: E402

# Stamped on every output so a simulator result is never read as an observed
# subscription delta.
PROVENANCE = "simulated"


@dataclass
class Reading:
    """One quota poll in the scenario, at a stated time."""

    at: float
    provider: str
    windows: list[dict[str, Any]] = field(default_factory=list)
    # Set to report a poll that failed, or one that nobody took.
    error: str = ""
    missing: bool = False

    def snapshot(self) -> QuotaSnapshot | None:
        if self.missing:
            return None
        if self.error:
            return QuotaSnapshot(
                provider=self.provider, fetched_at=self.at, error=self.error
            )
        windows = tuple(
            literal_window(spec, i) for i, spec in enumerate(self.windows)
        )
        return snapshot_from_windows(self.provider, windows, self.at)


@dataclass
class Admission:
    """One new session asking to be placed, at a stated time."""

    at: float
    id: str
    alias: str = "auto"
    answers: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    name: str
    description: str = ""
    stale_after_seconds: float = 1800.0
    readings: list[Reading] = field(default_factory=list)
    admissions: list[Admission] = field(default_factory=list)
    # Providers whose breaker is open from this time onwards, to stand in for
    # an outage. Health is availability evidence, not a quota meter.
    outages: list[dict[str, Any]] = field(default_factory=list)


def load_scenario(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text()) or {}
    return Scenario(
        name=str(raw.get("name") or path.stem),
        description=str(raw.get("description") or ""),
        stale_after_seconds=float(raw.get("stale_after_seconds") or 1800.0),
        readings=[Reading(**r) for r in raw.get("readings") or []],
        admissions=[Admission(**a) for a in raw.get("admissions") or []],
        outages=list(raw.get("outages") or []),
    )


def features_from(spec: dict[str, Any]) -> Features:
    return Features(
        model=str(spec.get("model") or "auto"),
        message_count=int(spec.get("message_count") or 1),
        est_tokens=int(spec.get("est_tokens") or 500),
        has_tools=bool(spec.get("has_tools")),
        tool_names=tuple(spec.get("tool_names") or ()),
        has_images=bool(spec.get("has_images")),
        has_code=bool(spec.get("has_code")),
        client=str(spec.get("client") or ""),
        max_tokens=int(spec.get("max_tokens") or 0),
    )


@dataclass
class SimResult:
    scenario: str
    description: str = ""
    provenance: str = PROVENANCE
    rows: list[dict[str, Any]] = field(default_factory=list)
    unknown_at_admission: int = 0

    def summary(self) -> dict[str, Any]:
        placed: dict[str, int] = {}
        deferred = 0
        blocked = 0
        for row in self.rows:
            if row.get("error"):
                blocked += 1
                continue
            key = f"{row['model']}({row['effort'] or '-'})"
            placed[key] = placed.get(key, 0) + 1
            if row.get("deferred"):
                deferred += 1
        return {
            "provenance": self.provenance,
            "admissions": len(self.rows),
            "placed": dict(sorted(placed.items())),
            "deferred_under_pressure": deferred,
            "blocked": blocked,
            "admissions_with_an_unknown_reading": self.unknown_at_admission,
        }


def run(scenario: Scenario, config: RouterConfig | None = None) -> SimResult:
    """Replay the scenario in time order. Pure: the clock comes from the file."""
    config = config or config_with_overlay()
    result = SimResult(scenario=scenario.name, description=scenario.description)

    # The last reading per provider, and the pressure it produced. Pressure is
    # carried forward exactly as the live poller carries it, hysteresis and
    # rate limit included.
    last: dict[str, QuotaSnapshot | None] = {}
    pressure: dict[str, float] = {}
    reasons: dict[str, str] = {}
    polls: dict[str, int] = {}

    events: list[tuple[float, int, Any]] = []
    for reading in scenario.readings:
        events.append((reading.at, 0, reading))
    for admission in scenario.admissions:
        events.append((admission.at, 1, admission))
    events.sort(key=lambda item: (item[0], item[1]))

    for at, _kind, event in events:
        if isinstance(event, Reading):
            snapshot = event.snapshot()
            last[event.provider] = snapshot
            count = polls.get(event.provider, 0)
            value, why = compute_pressure(
                snapshot,
                config.quota_policy,
                at,
                previous=pressure.get(event.provider) if count else None,
                stale_after_seconds=scenario.stale_after_seconds,
            )
            pressure[event.provider] = value
            reasons[event.provider] = why
            polls[event.provider] = count + 1
            continue

        status = {
            name: snapshot_status(last.get(name), at, scenario.stale_after_seconds)
            for name in config.provider_names()
        }
        if any(word != "fresh" for word in status.values()):
            result.unknown_at_admission += 1

        # A provider whose breaker is open during this window reads as fully
        # pressured, which is what the live merge does.
        live = dict(pressure)
        for outage in scenario.outages:
            if outage.get("from", 0) <= at <= outage.get("until", float("inf")):
                live[str(outage["provider"])] = 1.0
        live = merge_pressures(config, live, None) | {
            k: v for k, v in live.items() if v
        }

        alias_cfg = config.aliases.get(event.alias)
        row: dict[str, Any] = {
            "at": at,
            "id": event.id,
            "alias": event.alias,
            "quota_status": status,
            "pressure": {k: round(v, 4) for k, v in live.items() if v},
            "pressure_reason": {
                k: reasons.get(k, "no reading") for k in sorted(status)
            },
        }
        if alias_cfg is None:
            row["error"] = f"unknown alias {event.alias!r}"
            result.rows.append(row)
            continue
        try:
            chosen = select(
                config, alias_cfg, event.answers, features_from(event.features), live
            )
        except RoutingError as exc:
            row["error"] = str(exc)
            result.rows.append(row)
            continue
        row.update(
            {
                "model": chosen.model,
                "effort": chosen.effort,
                "rule": chosen.rule,
                "lane": chosen.lane,
                "evidence": chosen.evidence,
                "counterfactual": list(chosen.counterfactual or []),
                "quota_changed_choice": chosen.pressure_changed_the_outcome,
                "deferred": any("deferred/kept under pressure" in n for n in chosen.notes),
                "notes": chosen.notes,
            }
        )
        result.rows.append(row)
    return result


def report(result: SimResult) -> str:
    """A readable summary. Every line of it is labelled simulated."""
    summary = result.summary()
    lines = [
        f"# Quota simulation: {result.scenario}",
        "",
        f"**Provenance: {PROVENANCE}.** These are replayed readings against the "
        "router's own pure functions, not an observed subscription delta. No "
        "number here is a measured quota saving: `ROUTE_COST` is a relative "
        "weight for ordering routes and is deliberately not used.",
        "",
    ]
    if result.description:
        lines += [result.description, ""]
    lines += [
        f"- admissions: {summary['admissions']}",
        f"- blocked: {summary['blocked']}",
        f"- deferred under pressure (kept the adequate provider): "
        f"{summary['deferred_under_pressure']}",
        f"- admissions taken with at least one unknown or stale reading: "
        f"{summary['admissions_with_an_unknown_reading']}",
        "",
        "## Where the work went",
        "",
        "| profile | admissions |",
        "| --- | ---: |",
    ]
    for name, count in summary["placed"].items():
        lines.append(f"| {name} | {count} |")
    lines += ["", "## Chronology", "", "| at | id | placed | rule | quota status | changed by quota |", "| ---: | --- | --- | --- | --- | --- |"]
    for row in result.rows:
        status = ", ".join(f"{k}={v}" for k, v in sorted(row["quota_status"].items()))
        placed = (
            row.get("error")
            or f"{row.get('model')}({row.get('effort') or '-'})"
        )
        lines.append(
            f"| {row['at']:.0f} | {row['id']} | {placed} | {row.get('rule', '-')} | "
            f"{status} | {'yes' if row.get('quota_changed_choice') else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# The shipped `auto` is strict, and a strict binding is never moved by quota:
# admitting the demo against it would show a flat table and teach nothing. So
# the demo admits against a legacy copy of the same alias, which is how the
# router routed before strict sessions and how a legacy deployment still
# routes. A scenario loaded from a file runs against the shipped configuration
# unchanged, alias names and all.
DEMO_ALIAS = "auto-legacy-demo"


def demo_config() -> RouterConfig:
    """The shipped config plus one legacy copy of `auto`, for the demo only."""
    config = config_with_overlay()
    config.aliases[DEMO_ALIAS] = config.aliases["auto"].model_copy(
        update={"session_mode": "legacy", "admission_fallback": None}
    )
    return config


DEMO = {
    "name": "demo: a weekly window runs ahead and the telemetry goes stale",
    "description": (
        "Synthetic. No account was observed to produce it. Admissions go to "
        f"`{DEMO_ALIAS}`, a legacy copy of the shipped `auto`: the shipped "
        "alias is strict, and a strict binding ignores quota by design."
    ),
    "stale_after_seconds": 1800,
    "readings": [
        {
            "at": 0,
            "provider": "openai",
            "windows": [
                {"name": "five_hour", "used_percent": 10.0, "window_minutes": 300,
                 "resets_at": 18000},
                {"name": "weekly", "used_percent": 12.0, "window_minutes": 10080,
                 "resets_at": 604800},
            ],
        },
        {
            "at": 3600,
            "provider": "openai",
            "windows": [
                {"name": "five_hour", "used_percent": 40.0, "window_minutes": 300,
                 "resets_at": 18000},
                {"name": "weekly", "used_percent": 85.0, "window_minutes": 10080,
                 "resets_at": 604800},
            ],
        },
        {"at": 7200, "provider": "openai", "error": "the command timed out"},
    ],
    "admissions": [
        {"at": 60, "id": "s1", "alias": DEMO_ALIAS, "answers": {
            "task": {"type": "choice", "choice": "code-edit", "confidence": 0.9},
            "difficulty": {"type": "score", "score": 2.6, "confidence": 0.9},
            "harm_if_wrong": {"type": "noul", "noul": 0.2}}},
        {"at": 3660, "id": "s2", "alias": DEMO_ALIAS, "answers": {
            "task": {"type": "choice", "choice": "code-edit", "confidence": 0.9},
            "difficulty": {"type": "score", "score": 2.6, "confidence": 0.9},
            "harm_if_wrong": {"type": "noul", "noul": 0.2}}},
        {"at": 7260, "id": "s3", "alias": DEMO_ALIAS, "answers": {
            "task": {"type": "choice", "choice": "code-edit", "confidence": 0.9},
            "difficulty": {"type": "score", "score": 2.6, "confidence": 0.9},
            "harm_if_wrong": {"type": "noul", "noul": 0.2}}},
    ],
}


def demo_scenario() -> Scenario:
    return Scenario(
        name=DEMO["name"],
        description=DEMO["description"],
        stale_after_seconds=float(DEMO["stale_after_seconds"]),
        readings=[Reading(**r) for r in DEMO["readings"]],
        admissions=[Admission(**a) for a in DEMO["admissions"]],
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", help="a scenario YAML file")
    p.add_argument("--demo", action="store_true", help="run the built-in scenario")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="write the report to this file too")
    args = p.parse_args()

    config = None
    if args.scenario:
        scenario = load_scenario(Path(args.scenario))
    elif args.demo:
        scenario, config = demo_scenario(), demo_config()
    else:
        print("give --scenario FILE or --demo", file=sys.stderr)
        return 2

    result = run(scenario, config)
    if args.json:
        print(json.dumps(
            {"summary": result.summary(), "rows": result.rows}, indent=2, default=str
        ))
    else:
        print(report(result))
    if args.out:
        Path(args.out).write_text(report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
