#!/usr/bin/env python
"""Qualify one exact deployment for the between-turn effort experiment.

This is the only script here that talks to a real provider, and it refuses to
do so unless every one of these is true:

  - `--confirm-live` was typed, and `JEV_ROUTER_QUALIFY_LIVE=1` is set;
  - `--profile` names a model in router.yaml whose protocol is
    `openai-responses` and whose `effort_control.between_turn` is not `fixed`;
  - that profile is in `QUALIFY_ALLOWLIST` below, or in
    `$JEV_ROUTER_QUALIFY_ALLOW`, so nobody can qualify an arbitrary model by
    passing a flag;
  - a router control credential and an upstream credential are both present;
  - `--max-calls` and `--max-seconds` are set and small.

    uv run python evals/qualify_effort.py --profile gpt-6-astra --plan
    JEV_ROUTER_QUALIFY_LIVE=1 uv run python evals/qualify_effort.py \\
        --profile gpt-6-astra --confirm-live --max-calls 12 \\
        --report docs/qualification/2026-01-01-gpt-6-astra.md

Without `--confirm-live` it plans: it prints the checks it would run, the
calls each one costs, and the profile facts it would be qualifying. That is
the mode this repository has ever run it in.

What it writes is a dated report from `docs/qualification/TEMPLATE.md` with
the observed results filled in and every check it could not run marked `not
run`. A report with blanks is not a pass, and `qualification: verified` in
router.yaml is refused unless the file it names exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(EVALS_DIR))

from jev_router.config import load_config  # noqa: E402
from manifest import build_manifest, profile_versions, write_manifest  # noqa: E402

TEMPLATE = ROOT / "docs" / "qualification" / "TEMPLATE.md"
LIVE_ENV = "JEV_ROUTER_QUALIFY_LIVE"
ALLOW_ENV = "JEV_ROUTER_QUALIFY_ALLOW"

# Profiles anybody may point this at. Empty on purpose: qualifying a
# deployment is a deliberate act, and adding a name here is part of it.
QUALIFY_ALLOWLIST: tuple[str, ...] = ()


@dataclass
class Check:
    """One thing the qualification has to establish, and what it costs."""

    id: str
    what: str
    calls: int
    result: str = "not run"
    note: str = ""


def checks() -> list[Check]:
    """The Q01-Q10 table of the report template, in the order it runs."""
    return [
        Check("Q01", "the base effort is accepted with no update item", 1),
        Check("Q02", "an update immediately before the new user message is accepted", 1),
        Check("Q03", "the reply reports the base effort and the ledger is not overwritten", 0),
        Check("Q04", "a full-history replay carrying every previous update is accepted", 1),
        Check("Q05", "a previous_response_id chain referencing the accepted parent works", 1),
        Check("Q06", "resending a persisted update, or adding an adjacent one, is refused", 2),
        Check("Q07", "the change is observable: more reasoning work at the higher rung", 2),
        Check("Q08", "a tool continuation keeps the effective effort with no new item", 1),
        Check("Q09", "truncation 'auto' and standalone compaction are refused", 2),
        Check("Q10", "the terminal status and usage, including cached tokens, are readable", 0),
    ]


@dataclass
class Plan:
    profile: str
    calls: int
    checks: list[Check] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


def build_plan(args: argparse.Namespace) -> Plan:
    """What this run would do, and every reason it may not do it."""
    config = load_config(args.config)
    rows = checks()
    plan = Plan(profile=args.profile, calls=sum(c.calls for c in rows), checks=rows)

    mcfg = config.models.get(args.profile)
    if mcfg is None:
        plan.problems.append(f"{args.profile!r} is not a model in {args.config}")
        return plan
    plan.facts = profile_versions(config, [args.profile])[args.profile]

    if mcfg.protocol != "openai-responses":
        plan.problems.append(
            f"{args.profile} speaks {mcfg.protocol}; the first active experiment "
            "is qualified for openai-responses only"
        )
    if mcfg.effort_control.between_turn == "fixed":
        plan.problems.append(
            f"{args.profile} declares no between-turn strategy, so there is "
            "nothing to qualify; set `effort_control.between_turn` to the "
            "strategy this deployment actually implements first"
        )
    if len(config.effort_ladder(args.profile)) < 2:
        plan.problems.append(
            f"{args.profile} has fewer than two rungs on its effort ladder"
        )

    allowed = set(QUALIFY_ALLOWLIST) | {
        name.strip() for name in os.environ.get(ALLOW_ENV, "").split(",") if name.strip()
    }
    if args.profile not in allowed:
        plan.problems.append(
            f"{args.profile} is not allowlisted; add it to QUALIFY_ALLOWLIST in "
            f"this file or to ${ALLOW_ENV}. A flag alone does not authorise "
            "spending a subscription against a model"
        )
    if plan.calls > args.max_calls:
        plan.problems.append(
            f"the full run costs {plan.calls} calls and --max-calls is "
            f"{args.max_calls}"
        )
    if not os.environ.get(config.settings.admin_token_env):
        plan.problems.append(
            f"no router control credential in ${config.settings.admin_token_env}"
        )
    if args.upstream_key_env and not os.environ.get(args.upstream_key_env):
        plan.problems.append(f"no upstream credential in ${args.upstream_key_env}")
    return plan


def print_plan(plan: Plan, args: argparse.Namespace) -> None:
    print(f"profile: {plan.profile}")
    for name, value in sorted(plan.facts.items()):
        print(f"  {name}: {value}")
    print(f"\nrouter: {args.router}")
    print(f"budget: {plan.calls} calls of {args.max_calls}, {args.max_seconds:.0f}s")
    print("\nchecks:")
    for check in plan.checks:
        print(f"  {check.id}  {check.calls:>2} call(s)  {check.what}")
    print("\nwhat this would NOT establish:")
    for line in (
        "long sessions, compaction, or anything past the agreed context limit",
        "cache behaviour beyond the rung sequence it happens to send",
        "any other account, region, or day",
        "that a `request_parameter` result is native cache preservation",
    ):
        print(f"  - {line}")
    if plan.problems:
        print("\nthis run is refused:")
        for problem in plan.problems:
            print(f"  - {problem}")
    else:
        print(
            f"\nno objection found. Set {LIVE_ENV}=1 and pass --confirm-live to run it."
        )


def render_report(plan: Plan, args: argparse.Namespace, ran: dict[str, Any]) -> str:
    """Fill the template's header and check table; leave the prose to a person."""
    text = TEMPLATE.read_text()
    facts = plan.facts
    header = {
        "**Date:**": date.today().isoformat(),
        "**Profile (model key in router.yaml):**": plan.profile,
        "**Upstream id:**": str(facts.get("upstream_id") or ""),
        "**Endpoint / proxy:**": args.router,
        "**Protocol:**": str(facts.get("protocol") or ""),
        "**Adapter revision (`compatibility_revision`):**": str(
            facts.get("compatibility_revision") or ""
        ),
        "**Router commit:**": ran.get("repo_sha", ""),
        "**Verdict:**": ran.get("verdict", "not verified"),
    }
    lines = []
    for line in text.splitlines():
        for prefix, value in header.items():
            if line.startswith(prefix) and value:
                line = f"{prefix} {value}"
                break
        if line.startswith("| Q"):
            check_id = line.split("|")[1].strip()
            match = next((c for c in plan.checks if c.id == check_id), None)
            if match is not None:
                parts = line.split("|")
                parts[3] = f" {match.result} "
                parts[4] = f" {match.note or ''} "
                line = "|".join(parts)
        lines.append(line)
    return "\n".join(lines) + "\n"


async def run_live(plan: Plan, args: argparse.Namespace) -> dict[str, Any]:
    """Run the checks against the real deployment.

    Deliberately not implemented here. The checks above describe exactly what
    a run has to establish; writing the caller is the first half of qualifying
    a deployment, and doing it blind from a spec would produce a script that
    has never been run against anything. Whoever qualifies a profile writes
    this against the endpoint in front of them, and the report says what they
    did.
    """
    raise NotImplementedError(
        "the live caller is written against the deployment being qualified; "
        "see the Q01-Q10 checks above and docs/ADAPTIVE_EFFORT.md"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-c", "--config", default=str(ROOT / "router.yaml"))
    p.add_argument("--profile", required=True, help="the model key to qualify")
    p.add_argument("--router", default="http://127.0.0.1:8318")
    p.add_argument("--upstream-key-env", default="UPSTREAM_API_KEY")
    p.add_argument("--max-calls", type=int, default=12)
    p.add_argument("--max-seconds", type=float, default=600.0)
    p.add_argument("--plan", action="store_true", help="print the plan and stop")
    p.add_argument(
        "--confirm-live",
        action="store_true",
        help=f"actually call the provider; also needs {LIVE_ENV}=1",
    )
    p.add_argument("--report", help="where to write the dated report")
    args = p.parse_args()

    plan = build_plan(args)
    if args.plan or not args.confirm_live:
        print_plan(plan, args)
        return 1 if plan.problems else 0
    if os.environ.get(LIVE_ENV) != "1":
        print(
            f"--confirm-live also needs {LIVE_ENV}=1 in the environment; "
            "two switches, on purpose",
            file=sys.stderr,
        )
        return 2
    if plan.problems:
        print_plan(plan, args)
        return 2

    started = time.time()
    try:
        ran = asyncio.run(run_live(plan, args))
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    ran["seconds"] = time.time() - started

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report(plan, args, ran))
        write_manifest(
            out.parent,
            build_manifest(
                script="evals/qualify_effort.py",
                kind="live",
                candidates=profile_versions(load_config(args.config), [args.profile]),
                caps={"max_calls": args.max_calls, "max_seconds": args.max_seconds},
                extra={"checks": [c.__dict__ for c in plan.checks], "run": ran},
            ),
        )
        print(f"wrote {out}")
    else:
        print(json.dumps(ran, indent=2, default=str))
    return 0


def new_session_id() -> str:
    return "session-" + secrets.token_hex(4)


if __name__ == "__main__":
    raise SystemExit(main())
