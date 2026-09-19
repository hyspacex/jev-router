"""Command line: serve, check-config, explain, decisions."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ConfigError, RouterConfig, load_config
from .pins import VERDICTS

DEFAULT_CONFIG = os.environ.get("JEV_ROUTER_CONFIG", "router.yaml")


def _load(path: str) -> RouterConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    config = _load(args.config)
    if args.mode:
        config.settings.mode = args.mode
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = args.host or config.settings.host
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local:
        logging.warning(
            "Non-loopback bind: protect the model API with trusted ingress/Tailscale ACLs; "
            "router endpoints require %s via X-Router-Admin-Token",
            config.settings.admin_token_env,
        )
    app = create_app(config)
    uvicorn.run(
        app,
        host=args.host or config.settings.host,
        port=args.port or config.settings.port,
        log_level=args.log_level,
        access_log=False,
        proxy_headers=False,  # loopback admin access uses the direct peer, not X-Forwarded-For
    )
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    config = _load(args.config)
    print(f"{args.config}: ok")
    print(f"  mode:      {config.settings.mode}")
    print(f"  upstream:  {config.settings.upstream_base_url}")
    print(f"  listen:    {config.settings.host}:{config.settings.port}")
    print(f"  database:  {config.settings.db_path}")
    print(f"  providers: {', '.join(config.provider_names())}")
    print(f"  models:    {', '.join(config.models)}")
    print(f"  routes:    {', '.join(config.routes) or '(none)'}")
    print(f"  questions: {', '.join(config.questions) or '(none)'}")
    print(f"  aliases:   {', '.join(config.aliases) or '(none)'}")
    sp = config.semantic_policy
    print(
        f"  semantic:  active=[{', '.join(sp.active_questions) or '-'}] "
        f"shadow=[{', '.join(sp.shadow_questions) or '-'}] "
        f"distribution={sp.distribution_policy}"
    )
    for name, lane in config.quality_lanes.items():
        pairs = ", ".join(f"{q.model}({q.effort or '-'})" for q in lane.qualified)
        print(f"  lane {name}: [{pairs}] ref={lane.qualification_ref or '-'}")
    for name in config.provider_names():
        quota = config.quota_cfg(name)
        source = "none"
        if quota is not None:
            source = f"{quota.source} ({'enabled' if quota.enabled else 'disabled'})"
        models = [m for m in config.models if config.provider_of(m) == name]
        print(f"  provider {name}: quota={source} models=[{', '.join(models)}]")
    for name, lane in config.routes.items():
        rungs = " -> ".join(
            f"{e.model}({e.effort or '-'})" + ("*" if e.equivalent else "")
            for e in lane.entries()
        )
        print(f"  route {name}: {rungs}")
    key = os.environ.get(config.settings.jev_api_key_env)
    print(
        f"  {config.settings.jev_api_key_env}: {'set' if key else 'NOT SET (will fail open)'}"
    )
    for alias, acfg in config.aliases.items():
        ruleset = config.ruleset_for(acfg)
        print(
            f"  alias {alias}: state={acfg.state_builder} "
            f"questions=[{', '.join(acfg.questions)}] rules={len(ruleset.rules)} "
            f"max_effort={acfg.max_effort or '-'}"
        )
    return 0


def parse_pressures(pairs: list[str] | None, config: RouterConfig) -> dict[str, float]:
    """`--pressure openai=0.8` a few times over, into a mapping."""
    out: dict[str, float] = {}
    known = set(config.provider_names())
    for pair in pairs or []:
        name, _, raw = pair.partition("=")
        name = name.strip()
        if not name or not raw.strip():
            raise ValueError(f"--pressure wants provider=number, got {pair!r}")
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"--pressure {name}: {raw!r} is not a number") from None
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"--pressure {name}: {value} is outside 0 to 1")
        if name not in known:
            raise ValueError(
                f"--pressure {name}: unknown provider (known: {', '.join(sorted(known))})"
            )
        out[name] = value
    return out


def cmd_quota(args: argparse.Namespace) -> int:
    """The same report as GET /router/quota, taken with one poll of each source."""
    from .providers import ProviderBreakers, merge_pressures
    from .quota import QuotaMonitor

    config = _load(args.config)
    monitor = QuotaMonitor(config)
    breakers = ProviderBreakers(config)

    async def run() -> None:
        for provider in list(monitor.sources):
            await monitor.poll_once(provider)

    if args.poll:
        asyncio.run(run())

    report = monitor.report()
    report["breaker_pressure"] = {
        name: round(breakers.pressure(name), 4) for name in config.provider_names()
    }
    report["effective_pressure"] = {
        k: round(v, 4)
        for k, v in merge_pressures(config, monitor.pressures(), breakers).items()
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"quota routing: {'on' if report['enabled'] else 'off'}")
    print(f"demote above:  {report['demote_above']}")
    if not args.poll:
        print("(no poll: pass --poll to run the sources once)")
    for name, row in report["providers"].items():
        snap = row["snapshot"] or {}
        used = snap.get("used_percent")
        expected = snap.get("expected_used_percent")
        print(
            f"\n  {name}: source={row['source']} "
            f"{'enabled' if row['enabled'] else 'disabled'}"
        )
        print(f"    pressure:  {row['pressure']:.2f}  ({row['reason']})")
        print(
            f"    status:    {row.get('status', 'unknown')}"
            + (
                "   (nobody measured this; it is not spare capacity)"
                if row.get("status") in ("unknown", "stale", "error")
                else ""
            )
        )
        print(
            f"    used:      {'-' if used is None else f'{used:.1f}%'}"
            f"   expected: {'-' if expected is None else f'{expected:.1f}%'}"
        )
        for window in row.get("windows") or []:
            pcnt = window.get("used_percent")
            resets = window.get("resets_at")
            print(
                f"    window {window.get('name') or '-'}: "
                f"used {'-' if pcnt is None else f'{pcnt:.1f}%'}"
                f"  limit={window.get('limit') or '-'}"
                f"  resets_at={'-' if resets is None else f'{resets:.0f}'}"
            )
        age = row["age_seconds"]
        print(
            f"    age:       {'-' if age is None else f'{age:.0f}s'}"
            f"{'  STALE' if row['stale'] else ''}"
            f"   polls={row['polls']} errors={row['errors']}"
        )
        if row["last_error"]:
            print(f"    error:     {row['last_error']}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    import httpx

    from .deciders import build_decider
    from .features import extract_features
    from .state import build_state

    config = _load(args.config)
    try:
        pressures = parse_pressures(args.pressure, config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        body = json.loads(Path(args.request).read_text())
    except (OSError, ValueError) as exc:
        print(f"cannot read request: {type(exc).__name__}", file=sys.stderr)
        return 2
    headers = body.pop("_headers", {}) if isinstance(body, dict) else {}
    features = extract_features(body, headers)

    alias = args.alias or features.model
    if alias not in config.aliases:
        print(
            f"model {alias!r} is not an alias; this request would be forwarded unchanged",
            file=sys.stderr,
        )
        return 1
    alias_cfg = config.aliases[alias]

    print("features:")
    for name in (
        "model",
        "message_count",
        "est_tokens",
        "total_chars",
        "has_tools",
        "tool_names",
        "has_images",
        "has_code",
        "languages",
        "client",
        "stream",
        "max_tokens",
    ):
        print(f"  {name}: {getattr(features, name)!r}")

    state = build_state(alias_cfg.state_builder, features, config)
    print(f"\nstate ({alias_cfg.state_builder}):")
    print(json.dumps(state, indent=2, default=str))

    async def run() -> Any:
        async with httpx.AsyncClient(
            timeout=config.settings.jev_timeout_ms / 1000
        ) as client:
            decider = build_decider(
                alias_cfg.decider or config.settings.decider,
                config,
                {"jev_client": client, "pressures": lambda: dict(pressures)},
            )
            return await decider.decide(features, alias_cfg)

    decision = asyncio.run(run())
    if decision.routing_error:
        print(f"routing error: {decision.routing_error}", file=sys.stderr)
        return 2
    print("\njev answers:")
    print(
        json.dumps(decision.answers, indent=2, default=str)
        if decision.answers
        else "  (none)"
    )
    if pressures:
        print("\npressure:")
        for name, value in sorted(pressures.items()):
            print(f"  {name}: {value:.2f}")
    print("\ndecision:")
    print(f"  model:    {decision.model}")
    print(f"  effort:   {decision.effort}")
    print(f"  rule:     {decision.rule}")
    print(f"  route:    {decision.route or '-'}")
    print(f"  reason:   {decision.reason}")
    print(f"  fallback: {decision.fallback}")
    if decision.plan:
        rungs = " -> ".join(f"{e.model}({e.effort or '-'})" for e in decision.plan)
        print(f"  plan:     {rungs}")
    for shift in decision.shifts:
        print(
            f"  shifted:  {shift.rule}.{shift.question} {shift.op} "
            f"{shift.base:g} -> {shift.effective:g} "
            f"(pressure {shift.pressure:.2f} on {shift.provider})"
        )
    if decision.reordered:
        print("  reordered: the route was reordered by quota pressure")
    if pressures and not decision.pressure_changed_the_outcome:
        print("  (pressure did not change this decision)")
    if decision.jev_ms is not None:
        print(f"  jev_ms:   {decision.jev_ms:.0f}")
    if decision.jev_input_tokens is not None:
        print(f"  jev_input_tokens: {decision.jev_input_tokens}")
    for note in decision.notes:
        print(f"  note:     {note}")
    if config.settings.mode == "shadow":
        print("  (shadow mode: the request would still go to the default route)")
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    from .pins import Store

    config = _load(args.config)
    store = Store(config.settings.db_path, config.settings.log_state)
    rows = store.recent_decisions(args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for row in reversed(rows):
        when = datetime.fromtimestamp(row["ts"]).strftime("%m-%d %H:%M:%S")
        marks = row.get("feedback") or []
        answers = row.get("answers") or {}
        bits = []
        for qid, ans in answers.items():
            if not isinstance(ans, dict):
                continue
            if ans.get("type") == "choice":
                bits.append(
                    f"{qid}={ans.get('choice')}({ans.get('confidence', 0):.2f})"
                )
            elif ans.get("type") == "score":
                bits.append(f"{qid}={ans.get('score', 0):.2f}")
            elif ans.get("type") == "noul":
                bits.append(f"{qid}={ans.get('noul', 0):.2f}")
        flags = []
        if row.get("pinned"):
            flags.append("pinned")
        if row.get("fallback"):
            flags.append("fallback")
        if row.get("mode") == "shadow":
            flags.append("shadow")
        print(
            f"{when}  {row['decision_id']}  {row['alias']:<10} {row['model']}"
            f"({row['effort'] or '-'})  rule={row['rule']}"
            f"  status={row.get('upstream_status')}"
            f"  jev={round(row['jev_ms']) if row.get('jev_ms') else '-'}ms"
            f"  {' '.join(bits)}"
            f"{'  [' + ', '.join(flags) + ']' if flags else ''}"
        )
        for mark in marks:
            better = ""
            if mark.get("better_model"):
                better = (
                    f" -> {mark['better_model']}({mark.get('better_effort') or '-'})"
                )
            note = f" {mark['note']!r}" if mark.get("note") else ""
            print(
                f"    feedback: {mark['verdict']}{better}{note} ({mark.get('source')})"
            )
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    from .feedback import FeedbackError, validate
    from .pins import Store

    config = _load(args.config)
    try:
        item = validate(
            config,
            {
                "decision_id": args.decision_id,
                "verdict": args.verdict,
                "better_model": args.model,
                "better_effort": args.effort,
                "note": args.note,
            },
        )
    except FeedbackError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    store = Store(config.settings.db_path, config.settings.log_state)
    decision_id = item.decision_id
    if decision_id == "last":
        decision_id = store.last_decision_id(args.client) or ""
        if not decision_id:
            print("no decisions have been logged yet", file=sys.stderr)
            return 1
    elif store.get_decision(decision_id) is None:
        print(f"unknown decision id {decision_id!r}", file=sys.stderr)
        return 1

    store.add_feedback(
        decision_id=decision_id,
        verdict=item.verdict,
        better_model=item.better_model,
        better_effort=item.better_effort,
        note=item.note,
        source="cli",
    )
    decision = store.get_decision(decision_id) or {}
    print(
        f"recorded {item.verdict} for {decision_id} "
        f"({decision.get('alias')} -> {decision.get('model')}"
        f"({decision.get('effort') or '-'}), rule={decision.get('rule')})"
    )
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from .pins import Store
    from .sessions import Sessions

    if args.older_than_days < 1:
        print("--older-than-days must be at least 1", file=sys.stderr)
        return 2
    config = _load(args.config)
    store = Store(config.settings.db_path)
    sessions = Sessions(store, config.session_routing, guard=False)
    try:
        counts = store.prune(args.older_than_days * 86400)
        # A pruned session keeps its id as a tombstone, so a client resuming a
        # removed session is told it is closed rather than given a new one.
        counts.update(sessions.prune(args.older_than_days * 86400))
        print(json.dumps(counts))
    finally:
        sessions.close()
        store.close()
    return 0


def _session_store(config: RouterConfig):
    from .pins import Store
    from .sessions import Sessions

    store = Store(config.settings.db_path, config.settings.log_state)
    return store, Sessions(store, config.session_routing, guard=False)


def _print_session(row: dict[str, Any], quota: dict[str, str]) -> None:
    created = datetime.fromtimestamp(row["created"] or 0).strftime("%m-%d %H:%M:%S")
    seen = datetime.fromtimestamp(row["last_seen"] or row["created"] or 0).strftime(
        "%m-%d %H:%M:%S"
    )
    print(f"{row['session_id']}  [{row['state']}{' tombstone' if row['tombstone'] else ''}]")
    print(f"  alias:    {row['alias'] or '-'}  client={row['client'] or '-'}")
    print(
        f"  model:    {row['model_key'] or '-'} -> {row['wire_model'] or '-'} "
        f"({row['provider'] or '-'}, {row['protocol'] or '-'})"
    )
    print(
        f"  window:   {row['context_window'] or '-'} negotiated, "
        f"max output {row['max_output_tokens'] or '-'}, "
        f"reserve {row['reserve_tokens'] or '-'}"
    )
    print(
        f"  effort:   base={row['base_effort'] or '-'} "
        f"effective={row['effective_effort'] or '-'} "
        f"mode={row['effort_mode'] or 'fixed'}  adaptation=off"
    )
    print(f"  binding:  {row['binding_revision'] or '-'}")
    print(
        f"  versions: config={row['config_hash'] or '-'} "
        f"questions={row['question_hash'] or '-'} jev={row['jev_model'] or '-'} "
        f"state_builder={row['state_builder'] or '-'}"
    )
    print(
        f"  decision: {row['decision_id'] or '-'} rule={row['decision_rule'] or '-'} "
        f"source={row['decision_source'] or '-'} lane={row['quality_lane'] or '-'}"
    )
    if row["decision_reason"]:
        print(f"  reason:   {row['decision_reason']}")
    if row["blocked_reason"]:
        print(f"  blocked:  {row['blocked_reason']}")
    admitted = row.get("quota_status")
    if isinstance(admitted, dict) and admitted:
        print(
            "  quota:    at admission "
            + ", ".join(f"{k}={v}" for k, v in sorted(admitted.items()))
        )
    if quota:
        print("            now " + ", ".join(f"{k}={v}" for k, v in sorted(quota.items())))
    unresolved = row.get("unresolved_requests") or 0
    if unresolved:
        print(f"  requests: {unresolved} with no settled outcome; reconcile before reuse")
    print(f"  created:  {created}  last seen {seen}")


def _quota_freshness(config: RouterConfig) -> dict[str, str]:
    """What the last stored snapshot says, without polling anything."""
    from .quota import QuotaMonitor

    try:
        monitor = QuotaMonitor(config)
        report = monitor.report()
    except Exception:  # noqa: BLE001 - diagnostics may never fail on quota
        return {}
    return {
        name: row.get("status") or "unknown"
        for name, row in report["providers"].items()
    }


def cmd_sessions(args: argparse.Namespace) -> int:
    """Inspect and close strict session bindings."""
    config = _load(args.config)
    store, sessions = _session_store(config)
    quota = _quota_freshness(config)
    try:
        if args.action == "list":
            rows = [
                row | {"unresolved_requests": sessions.unresolved(row["session_id"])}
                for row in sessions.recent(args.limit)
            ]
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
                return 0
            if not rows:
                print("no sessions")
                return 0
            for row in rows:
                _print_session(row, quota)
                print()
            return 0

        row = sessions.get(args.session_id)
        if row is None:
            print(f"unknown session {args.session_id!r}", file=sys.stderr)
            return 1
        row["unresolved_requests"] = sessions.unresolved(args.session_id)
        if args.action == "close":
            if row["state"] == "closed":
                print(f"{args.session_id} was already closed")
                return 0
            sessions.close_session(args.session_id, "closed from the command line")
            row = sessions.get(args.session_id) or row
            print(f"closed {args.session_id}")
        if args.json:
            print(json.dumps(row, indent=2, default=str))
        else:
            _print_session(row, quota)
        return 0
    finally:
        sessions.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-router", description=__doc__)
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG, help="path to router.yaml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the HTTP shim")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--mode", choices=["shadow", "active"])
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=cmd_serve)

    p_prune = sub.add_parser(
        "prune", help="delete old decisions, feedback and pins (irreversible)"
    )
    p_prune.add_argument("--older-than-days", type=int, required=True)
    p_prune.set_defaults(func=cmd_prune)

    p_check = sub.add_parser(
        "check-config", help="validate router.yaml and print a summary"
    )
    p_check.set_defaults(func=cmd_check_config)

    p_explain = sub.add_parser(
        "explain", help="show features, state, Jev answers and the chosen model"
    )
    p_explain.add_argument(
        "request", help="a JSON file holding a chat-completions body"
    )
    p_explain.add_argument("--alias", help="treat the request as this alias")
    p_explain.add_argument(
        "--pressure",
        action="append",
        metavar="PROVIDER=N",
        help="pretend a provider is under this much quota pressure, 0 to 1",
    )
    p_explain.set_defaults(func=cmd_explain)

    p_quota = sub.add_parser(
        "quota", help="show quota snapshots and pressure per provider"
    )
    p_quota.add_argument(
        "--poll", action="store_true", help="run each enabled source once first"
    )
    p_quota.add_argument("--json", action="store_true")
    p_quota.set_defaults(func=cmd_quota)

    p_sess = sub.add_parser("sessions", help="inspect or close strict session bindings")
    p_sess.add_argument("action", choices=["list", "show", "close"])
    p_sess.add_argument("session_id", nargs="?", help="a session id, for show and close")
    p_sess.add_argument("-n", "--limit", type=int, default=20)
    p_sess.add_argument("--json", action="store_true")
    p_sess.set_defaults(func=cmd_sessions)

    p_dec = sub.add_parser("decisions", help="tail the decision log")
    p_dec.add_argument("-n", "--limit", type=int, default=20)
    p_dec.add_argument("--json", action="store_true")
    p_dec.set_defaults(func=cmd_decisions)

    p_fb = sub.add_parser("feedback", help="say whether a routing decision was right")
    p_fb.add_argument("decision_id", help="a decision id from `decisions`, or 'last'")
    p_fb.add_argument("verdict", choices=list(VERDICTS))
    p_fb.add_argument("--model", help="the model that would have been better")
    p_fb.add_argument("--effort", help="the effort that would have been better")
    p_fb.add_argument("--note", help="free text, kept as written")
    p_fb.add_argument("--client", help="with 'last', the client header to look under")
    p_fb.set_defaults(func=cmd_feedback)

    args = parser.parse_args(argv)
    if args.command == "sessions" and args.action != "list" and not args.session_id:
        parser.error(f"sessions {args.action} needs a session id")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
