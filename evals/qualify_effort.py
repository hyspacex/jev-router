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
        Check("Q02", "an update immediately before the new user message is accepted", 2),
        Check("Q03", "the reply reports the base effort and the ledger is not overwritten", 2),
        Check("Q04", "a full-history replay carrying every previous update is accepted", 3),
        Check("Q05", "a previous_response_id chain referencing the accepted parent works", 2),
        Check("Q06", "resending a persisted update, or adding an adjacent one, is refused", 4),
        # Four puzzles, three arms, three counterbalanced repeats.
        Check("Q07", "the change is observable: more reasoning work at the higher rung", 36),
        Check("Q08", "a tool continuation keeps the effective effort with no new item", 2),
        Check("Q09", "truncation 'auto' and automatic context management are refused", 2),
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


# --- the live run ---------------------------------------------------------
#
# Two halves. The endpoint's own behaviour is probed straight at the proxy,
# with no router in the path, because that is the only way to know what the
# provider does with an item. The router's half goes through the real Codex
# adapter, run in this process, against a router that serves the profile:
# that is the client the deployment will be used with.

UPDATE = "configuration_update"
INSTRUCTIONS = "You are a careful assistant. Answer briefly."

# Short problems with one right answer that a model has to work through. Q07
# measures reasoning work on them, never the answers. The numbers are drawn
# from a seed for each run: a famous puzzle is answered from memory at every
# effort, which measures nothing (the first sitting of 2026-10-26 did exactly
# that and got 7 to 32 reasoning tokens in every arm).
def puzzles(seed: int) -> tuple[str, ...]:
    import random

    rng = random.Random(seed)
    n, a, b, c = rng.randint(5000, 9999), rng.choice((3, 4, 6)), rng.choice((5, 7, 9)), rng.choice((11, 13))
    x, e, m = rng.randint(101, 999), rng.randint(101, 999), rng.choice((37, 41, 43, 47))
    p, q, r = rng.choice((3, 4, 5)), rng.choice((7, 8, 11)), rng.randint(400, 900)
    u, v = rng.randint(1000, 9999), rng.randint(1000, 9999)
    return (
        (
            f"How many integers k with 1 <= k <= {n} are divisible by {a} or by {b} but "
            f"not by {c}? Reply with the number only."
        ),
        f"What is the remainder when {x}^{e} is divided by {m}? Reply with the number only.",
        (
            f"How many ordered pairs of positive integers (x, y) satisfy {p}x + {q}y = {r}? "
            "Reply with the number only."
        ),
        f"What is the sum of the digits of {u} * {v}? Reply with the number only.",
    )


ARMS = ("base", "update", "request_high")


class OverBudget(RuntimeError):
    pass


@dataclass
class Budget:
    max_calls: int
    deadline: float
    calls: int = 0

    def take(self) -> None:
        if self.calls >= self.max_calls:
            raise OverBudget(f"--max-calls {self.max_calls} reached")
        if time.time() > self.deadline:
            raise OverBudget("--max-seconds reached")
        self.calls += 1


def user(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def update(effort: str) -> dict[str, Any]:
    return {"type": UPDATE, "reasoning": {"effort": effort}}


def replayable(output: list[Any]) -> list[dict[str, Any]]:
    """The reply's messages, as a stateless client sends them back."""
    out = []
    for item in output or []:
        if isinstance(item, dict) and item.get("type") == "message":
            out.append({k: v for k, v in item.items() if k != "id"})
    return out


def read_events(raw: bytes) -> list[dict[str, Any]]:
    events = []
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        data = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if not data or data[0] == "[DONE]":
            continue
        try:
            events.append(json.loads("\n".join(data)))
        except ValueError:
            continue
    return events


@dataclass
class Reply:
    status: int
    terminal: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.terminal == "response.completed"

    def usage(self, *path: str) -> Any:
        node: Any = self.response.get("usage")
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node

    @property
    def reasoning_tokens(self) -> int | None:
        return self.usage("output_tokens_details", "reasoning_tokens")

    @property
    def cached_tokens(self) -> int | None:
        return self.usage("input_tokens_details", "cached_tokens")

    @property
    def reported_effort(self) -> str:
        return str((self.response.get("reasoning") or {}).get("effort") or "")


async def post(
    http: Any, url: str, body: dict[str, Any], headers: dict[str, str], budget: Budget
) -> Reply:
    """One streamed Responses call. Error text is the provider's, never ours."""
    budget.take()
    response = await http.post(url, json=body, headers=headers)
    reply = Reply(status=response.status_code, headers=dict(response.headers))
    if response.status_code != 200:
        try:
            err = response.json().get("error") or response.json()
            reply.error = json.dumps(err)[:400]
        except ValueError:
            reply.error = response.text[:400]
        return reply
    for event in read_events(response.content):
        kind = str(event.get("type") or "")
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            reply.terminal = kind
            reply.response = event.get("response") or {}
    return reply


def direct_body(
    upstream_id: str, items: list[dict[str, Any]], effort: str = "low", **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": upstream_id,
        # A fresh prefix on every call, so no call can be served from another
        # call's cache. Q07 compares effort, not cache luck.
        "instructions": f"{INSTRUCTIONS} (probe {secrets.token_hex(6)})",
        "input": items,
        "reasoning": {"effort": effort},
        "stream": True,
        "store": False,
    }
    body.update(extra)
    return body


def mark(check: Check, ok: bool | None, note: str) -> None:
    check.result = "not run" if ok is None else ("pass" if ok else "fail")
    check.note = note


def bootstrap_mean_diff(diffs: list[float], draws: int = 4000, seed: int = 13) -> tuple[float, float, float]:
    """Mean of paired differences and a 95% percentile interval, resampled by pair."""
    import random

    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(draws))
    return sum(diffs) / n, means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


async def run_live(plan: Plan, args: argparse.Namespace) -> dict[str, Any]:
    """Run Q01-Q10 against the deployment in front of this script."""
    import httpx

    from jev_router.integrations.codex.adapter import Adapter, create_adapter_app

    config = load_config(args.config)
    mcfg = config.models[args.profile]
    upstream_id = mcfg.upstream_id
    key = os.environ[args.upstream_key_env]
    admin = os.environ[config.settings.admin_token_env]
    url = args.upstream.rstrip("/") + "/v1/responses"
    auth = {"authorization": f"Bearer {key}"}
    budget = Budget(args.max_calls, time.time() + args.max_seconds)
    by_id = {c.id: c for c in plan.checks}
    ran: dict[str, Any] = {"upstream": args.upstream, "router": args.router, "q07": [], "q10": []}
    replies: list[Reply] = []

    def keep(reply: Reply) -> Reply:
        replies.append(reply)
        return reply

    timeout = httpx.Timeout(300.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            # Q01: the base effort, no item.
            r1 = keep(await post(http, url, direct_body(upstream_id, [user("Reply with the word ok.")]), auth, budget))
            mark(by_id["Q01"], r1.ok, f"HTTP {r1.status}, terminal `{r1.terminal or '-'}`, reported effort `{r1.reported_effort or '-'}`")

            # Q02: an update right before the new user message, and a control:
            # an item type nobody knows has to be refused, or a 200 proves nothing.
            r2 = keep(await post(http, url, direct_body(upstream_id, [update("high"), user("Reply with the word ok.")]), auth, budget))
            rc = await post(http, url, direct_body(upstream_id, [{"type": "no_such_item_type"}, user("Reply with the word ok.")]), auth, budget)
            mark(by_id["Q02"], r2.ok and rc.status >= 400,
                 f"update: HTTP {r2.status} `{r2.terminal or '-'}`; control with an invented item type: HTTP {rc.status} {rc.error[:160]}")

            # Q04: a three-turn full replay, the update kept where it was accepted.
            t1 = [user("Name a prime number between 10 and 20. One word.")]
            a1 = keep(await post(http, url, direct_body(upstream_id, t1), auth, budget))
            t2 = t1 + replayable(a1.response.get("output")) + [update("high"), user("Name another one. One word.")]
            a2 = keep(await post(http, url, direct_body(upstream_id, t2), auth, budget))
            t3 = t2 + replayable(a2.response.get("output")) + [user("And a third. One word.")]
            a3 = keep(await post(http, url, direct_body(upstream_id, t3), auth, budget))
            pos = [i for i, item in enumerate(t3) if item.get("type") == UPDATE]
            mark(by_id["Q04"], a1.ok and a2.ok and a3.ok,
                 f"turns: HTTP {a1.status}/{a2.status}/{a3.status}; the update replayed at index {pos} on turn 3")

            # Q05: a previous_response_id chain. Needs the provider to store.
            c1 = keep(await post(http, url, direct_body(upstream_id, [user("Say ok.")], store=True), auth, budget))
            chain_id = str(c1.response.get("id") or "")
            if c1.ok and chain_id:
                c2 = keep(await post(http, url, direct_body(
                    upstream_id, [update("high"), user("Say ok again.")], store=True,
                    previous_response_id=chain_id), auth, budget))
                mark(by_id["Q05"], c2.ok, f"`store: true` parent HTTP {c1.status}; chained child HTTP {c2.status} {c2.error[:160]}")
                ran["chain_child"] = str(c2.response.get("id") or "")
            else:
                mark(by_id["Q05"], False, f"`store: true` refused: HTTP {c1.status} {c1.error[:200]}")
                ran["chain_child"] = ""

            # Q06: two adjacent updates; and, where a chain exists, resending the
            # update the parent already holds.
            adj = await post(http, url, direct_body(upstream_id, [update("high"), update("medium"), user("Say ok.")]), auth, budget)
            note = f"adjacent updates: HTTP {adj.status} {adj.error[:160]}"
            resend_ok: bool | None = None
            if ran["chain_child"]:
                rs = await post(http, url, direct_body(
                    upstream_id, [update("high"), user("Say ok once more.")], store=True,
                    previous_response_id=ran["chain_child"]), auth, budget)
                resend_ok = rs.status >= 400
                note += f"; resending the persisted update in the chain: HTTP {rs.status} {rs.error[:160]}"
            else:
                note += "; resend half not run: no chain on this endpoint"
            mark(by_id["Q06"], (adj.status >= 400) and (resend_ok is not False), note)
            if resend_ok is None and adj.status >= 400:
                by_id["Q06"].result = "partial pass"

            # Q07: reasoning work under each arm, same puzzle, counterbalanced.
            rows = []
            problems = puzzles(args.seed)
            ran["q07_seed"] = args.seed
            for rep in range(args.q07_repeats):
                for p_index, puzzle in enumerate(problems):
                    shift = (rep + p_index) % len(ARMS)
                    for arm in ARMS[shift:] + ARMS[:shift]:
                        if arm == "base":
                            body = direct_body(upstream_id, [user(puzzle)], "low")
                        elif arm == "update":
                            body = direct_body(upstream_id, [update("high"), user(puzzle)], "low")
                        else:
                            body = direct_body(upstream_id, [user(puzzle)], "high")
                        r = keep(await post(http, url, body, auth, budget))
                        rows.append({"rep": rep, "puzzle": p_index, "arm": arm, "status": r.status,
                                     "reasoning_tokens": r.reasoning_tokens, "cached_tokens": r.cached_tokens})
            ran["q07"] = rows

            def pairs(a: str, b: str) -> list[float]:
                cell = {(x["rep"], x["puzzle"], x["arm"]): x["reasoning_tokens"] for x in rows}
                out = []
                for rep in range(args.q07_repeats):
                    for p_index in range(len(problems)):
                        va, vb = cell.get((rep, p_index, a)), cell.get((rep, p_index, b))
                        if isinstance(va, int) and isinstance(vb, int):
                            out.append(float(vb - va))
                return out

            up = pairs("base", "update")
            vs_high = pairs("request_high", "update")
            if up:
                mean, lo, hi = bootstrap_mean_diff(up)
                ran["q07_update_minus_base"] = {"n": len(up), "mean": mean, "lo": lo, "hi": hi}
                m2, lo2, hi2 = bootstrap_mean_diff(vs_high) if vs_high else (0.0, 0.0, 0.0)
                ran["q07_update_minus_request_high"] = {"n": len(vs_high), "mean": m2, "lo": lo2, "hi": hi2}
                mark(by_id["Q07"], lo > 0,
                     f"update minus base: {mean:+.1f} reasoning tokens [{lo:+.1f}, {hi:+.1f}], n={len(up)} pairs; "
                     f"update minus request-level high: {m2:+.1f} [{lo2:+.1f}, {hi2:+.1f}]")
            else:
                mark(by_id["Q07"], False, "no reasoning-token counts came back")

            # Q03, Q08, Q09: through the adapter and the router.
            adapter = Adapter(router_url=args.router, alias=args.alias, admin_token=admin,
                              upstream_key=key, trace=lambda line: ran.setdefault("adapter", []).append(line))
            app = create_adapter_app(adapter)
            transport = httpx.ASGITransport(app=app)
            conv = f"qualify-{secrets.token_hex(6)}"
            async with httpx.AsyncClient(transport=transport, base_url="http://adapter", timeout=timeout) as via, \
                    httpx.AsyncClient(base_url=args.router, timeout=timeout) as router:
                head = {"session-id": conv, "authorization": "Bearer placeholder"}

                def adapter_body(items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
                    body = {"model": args.profile, "instructions": INSTRUCTIONS, "input": items,
                            "reasoning": {"effort": "low"}, "stream": True, "store": False}
                    body.update(extra)
                    return body

                async def inspect() -> dict[str, Any]:
                    sid = adapter.conversations[conv].session_id
                    got = await router.get(f"/router/audit/sessions/{sid}", headers={"x-router-admin-token": admin})
                    return got.json()

                history = [user("What is 7 times 8? Reply with the number only.")]
                h1 = keep(await post(via, "/v1/responses", adapter_body(history), head, budget))
                history += replayable(h1.response.get("output"))
                history.append(user(
                    "Prove that there are infinitely many primes of the form 4k+3, then "
                    "give the smallest such prime above 100. Be rigorous; this matters."))
                h2 = keep(await post(via, "/v1/responses", adapter_body(history), head, budget))
                state = await inspect()
                session = state.get("session") or {}
                plans = state.get("plans") or []
                last = plans[-1] if plans else {}
                ran["router_session"] = session.get("session_id")
                changed = last.get("action") == "change_effort"
                if not changed:
                    mark(by_id["Q03"], None, f"turn 2 planned `{last.get('action')}`, so there was no change to read back")
                else:
                    mark(by_id["Q03"], h2.ok and h2.reported_effort == session.get("base_effort")
                         and session.get("confirmed_effort") == last.get("to_effort"),
                         f"plan {last.get('from_effort')} -> {last.get('to_effort')}; the reply reported "
                         f"`{h2.reported_effort}`; ledger base `{session.get('base_effort')}`, confirmed "
                         f"`{session.get('confirmed_effort')}`, effective `{session.get('effective_effort')}`")

                # Q08: a tool call, then its continuation in the same turn.
                history += replayable(h2.response.get("output"))
                history.append(user("Use the lookup tool to fetch the record named 'alpha', then reply with its value."))
                tools = [{"type": "function", "name": "lookup", "description": "Fetch a record by name.",
                          "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                                         "required": ["name"]}}]
                h3 = keep(await post(via, "/v1/responses", adapter_body(history, tools=tools), head, budget))
                calls = [i for i in h3.response.get("output") or [] if isinstance(i, dict) and i.get("type") == "function_call"]
                before = await inspect()
                if not calls:
                    mark(by_id["Q08"], None, "the model answered without calling the tool")
                else:
                    call = {k: v for k, v in calls[0].items() if k != "id"}
                    history += [call, {"type": "function_call_output", "call_id": call.get("call_id"), "output": "42"}]
                    h4 = keep(await post(via, "/v1/responses", adapter_body(history, tools=tools), head, budget))
                    after = await inspect()
                    same_plans = len(after.get("plans") or []) == len(before.get("plans") or [])
                    same_effort = (after.get("session") or {}).get("effective_effort") == (before.get("session") or {}).get("effective_effort")
                    turns = [r.get("turn_id") for r in after.get("requests") or []][-2:]
                    mark(by_id["Q08"], h4.ok and same_plans and same_effort,
                         f"continuation HTTP {h4.status}; plans {len(before.get('plans') or [])} -> "
                         f"{len(after.get('plans') or [])}; effective effort "
                         f"`{(after.get('session') or {}).get('effective_effort')}`; the two requests logged under {turns}")

                # Q06, the router's half: whatever the provider accepts, the
                # router must not forward an update no plan gave, nor a second
                # copy next to one it recorded.
                state_conv = adapter.conversations[conv]
                forwarded, _ = adapter.compose(state_conv, adapter_body(history + [user("Say ok.")]), {})
                items = list(forwarded["input"])
                authored = items[:-1] + [update("medium"), items[-1]]
                doubled = list(items)
                if state_conv.updates:
                    first = state_conv.updates[0]
                    doubled.insert(first.position + 1, dict(first.item))
                router_head = {
                    "x-router-admin-token": admin, "x-router-session": state_conv.session_id,
                    "x-router-binding": state_conv.binding, "x-router-turn": state_conv.last_turn_id,
                    "authorization": f"Bearer {key}", "accept-encoding": "identity",
                }
                q6r = []
                for name, probe in (("an update no plan gave", authored), ("a second copy beside a recorded update", doubled)):
                    budget.take()  # refused before forwarding, but counted in case it is not
                    reply = await router.post("/v1/responses", json={**forwarded, "input": probe},
                                              headers={**router_head, "x-router-request-id": f"qualify-{secrets.token_hex(6)}"})
                    try:
                        code = (reply.json().get("error") or {}).get("code")
                    except ValueError:
                        code = None
                    q6r.append((name, reply.status_code, code))
                ran["q06_router"] = q6r
                router_refuses = all(status >= 400 for _, status, _ in q6r)
                check = by_id["Q06"]
                check.note += "; through the router: " + "; ".join(
                    f"{name}: HTTP {status} {code or ''}".strip() for name, status, code in q6r)
                ran["q06_router_refuses"] = router_refuses

                # Q09: automatic management is refused before anything is sent.
                q9 = []
                for extra in ({"truncation": "auto"}, {"context_management": {"mode": "auto"}}):
                    probe = history + [user("Say ok.")]
                    budget.take()  # refused before forwarding, but counted in case it is not
                    reply = await via.post("/v1/responses", json=adapter_body(probe, **extra), headers=head)
                    try:
                        code = (reply.json().get("error") or {}).get("code")
                    except ValueError:
                        code = None
                    q9.append((next(iter(extra)), reply.status_code, code))
                mark(by_id["Q09"], all(status >= 400 for _, status, _ in q9),
                     "; ".join(f"`{name}`: HTTP {status} {code or ''}".strip() for name, status, code in q9)
                     + ". The standalone compaction endpoint is forwarded as maintenance since the "
                     "owner decision of 2026-09-19 and is not refused")
            await adapter.aclose()
        except OverBudget as exc:
            ran["stopped"] = str(exc)

    # Q10: every completed stream ends with a readable status and usage.
    done = [r for r in replies if r.status == 200]
    readable = [r for r in done if r.terminal and r.reasoning_tokens is not None and r.cached_tokens is not None]
    mark(by_id["Q10"], bool(done) and len(readable) == len(done),
         f"{len(readable)} of {len(done)} streamed replies carried a terminal status with reasoning and cached token counts")
    ran["calls"] = budget.calls
    ran["verdict"] = "verified" if all(c.result in ("pass", "partial pass") for c in plan.checks) else "not verified"
    return ran


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-c", "--config", default=str(ROOT / "router.yaml"))
    p.add_argument("--profile", required=True, help="the model key to qualify")
    p.add_argument("--router", default="http://127.0.0.1:8318")
    p.add_argument("--upstream-key-env", default="UPSTREAM_API_KEY")
    p.add_argument("--upstream", default="http://127.0.0.1:8317", help="the proxy probed directly")
    p.add_argument("--alias", default="astra-adaptive", help="the strict adaptive alias the router serves")
    p.add_argument("--q07-repeats", type=int, default=3)
    p.add_argument("--seed", type=int, default=20261026, help="draws the Q07 problems")
    p.add_argument("--max-calls", type=int, default=60)
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
            out.parent / out.stem,
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
