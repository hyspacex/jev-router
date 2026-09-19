"""Command line: serve, adapter, check-config, explain, decisions."""

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
    effort = config.adaptive_effort()
    print(
        f"  effort:    adaptive_effort={effort.mode} "
        f"profiles=[{', '.join(effort.qualified_profiles) or '-'}] "
        f"ladder=[{', '.join(effort.ladder)}] "
        f"hysteresis={'on' if effort.hysteresis else 'off'}"
    )
    for model in effort.qualified_profiles:
        mcfg = config.models.get(model)
        if mcfg is None:
            continue
        control = mcfg.effort_control
        print(
            f"    {model}: {control.between_turn} {control.qualification} "
            f"cache={control.cache_behavior} "
            f"rungs=[{', '.join(config.effort_ladder(model))}] "
            f"ref={control.qualification_ref or '-'}"
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


def cmd_adapter(args: argparse.Namespace) -> int:
    """Run the experimental Responses client adapter.

    It is a client, not a second router: it resolves one binding per
    conversation, acknowledges it, reports turn boundaries and records the
    update item the router hands back. See docs/ADAPTIVE_EFFORT.md.
    """
    import uvicorn

    from .adapter import Adapter, create_adapter_app, read_key_file

    token = os.environ.get(args.admin_token_env, "")
    if not token:
        print(
            f"{args.admin_token_env} is not set. Strict control and execution "
            "requests need the router control credential, even on loopback.",
            file=sys.stderr,
        )
        return 2
    key = ""
    if args.upstream_key_file:
        try:
            key = read_key_file(args.upstream_key_file)
        except (OSError, ValueError) as exc:
            print(f"--upstream-key-file: {exc}", file=sys.stderr)
            return 2
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    adapter = Adapter(
        router_url=args.router,
        alias=args.alias,
        client_name=args.client,
        admin_token=token,
        upstream_key=key,
        placeholder_token=args.placeholder_token,
    )
    print(
        f"adapter on http://{args.host}:{args.port} -> {args.router} "
        f"alias={args.alias} client={args.client} "
        f"upstream-key={'from file' if key else 'from the client'}"
    )
    print(
        "experimental: point a Responses client at this address. It never "
        "chooses a model or an effort; every item it inserts came from a "
        "/router/turn-plan answer."
    )
    uvicorn.run(
        create_adapter_app(adapter),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
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
    print("\njev answers (active, these decide):")
    print(
        json.dumps(decision.answers, indent=2, default=str)
        if decision.answers
        else "  (none)"
    )
    print("\njev answers (shadow, recorded and read by nothing):")
    if decision.shadow_answers:
        print(json.dumps(decision.shadow_answers, indent=2, default=str))
    else:
        print("  (none)")
    if decision.invalid_shadow:
        print(f"  dropped as invalid: {', '.join(decision.invalid_shadow)}")
    if decision.packet_version:
        print(f"\npacket version: {decision.packet_version}")
        if decision.shadow_packet_version:
            print(f"shadow packet:  {decision.shadow_packet_version}")

    exclusions = decision.exclusions
    print("\nhard constraints:")
    if exclusions:
        for row in exclusions:
            effort = f"({row['effort'] or '-'})" if "effort" in row else ""
            print(f"  ruled out {row['model']}{effort}: {row['reason']}")
    else:
        print("  every permitted model can serve this request")

    print("\nquota:")
    freshness = _quota_freshness(config)
    for name in sorted(set(freshness) | set(pressures)):
        word = freshness.get(name, "unknown")
        live = pressures.get(name)
        note = "" if word == "fresh" else "  (not measured; not spare capacity)"
        shown = f"{live:.2f}" if live is not None else "-"
        print(f"  {name}: {shown}  status={word}{note}")

    print("\ndecision:")
    print(f"  model:    {decision.model}")
    print(f"  effort:   {decision.effort}")
    print(f"  rule:     {decision.rule}")
    print(f"  route:    {decision.route or '-'}")
    print(f"  lane:     {decision.lane or '-'}")
    if decision.qualification_ref:
        print(f"  qualified by: {decision.qualification_ref}")
    print(f"  evidence: {decision.evidence}")
    if decision.counterfactual:
        model, effort = decision.counterfactual
        same = (model, effort) == (decision.model, decision.effort)
        print(
            f"  without pressure: {model}({effort or '-'})"
            + ("  (the same choice)" if same else "  (a different choice)")
        )
    for experiment in decision.experiments:
        print(
            f"  experiment {experiment.policy}: {experiment.model}"
            f"({experiment.effort or '-'}) via {experiment.rule}"
            f"{'  CHANGED' if experiment.changed else ''}"
        )
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


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-run the pure selection from a stored decision row.

    No Jev call, no quota poll, no prompt: the answers, the request facts and
    the pressure all come off the row, which is what invariant I13 asks for.
    """
    from .pins import Store
    from .semantic import replay_decision

    config = _load(args.config)
    store = Store(config.settings.db_path, config.settings.log_state)
    try:
        decision_id = args.decision_id
        if decision_id == "last":
            decision_id = store.last_decision_id() or ""
        row = store.get_decision(decision_id) if decision_id else None
        if row is None:
            print(f"unknown decision id {args.decision_id!r}", file=sys.stderr)
            return 1
        result = replay_decision(config, row)
    finally:
        store.close()

    if args.json:
        print(
            json.dumps(
                {
                    "decision_id": result.decision_id,
                    "stored": list(result.stored),
                    "stored_rule": result.stored_rule,
                    "replayed": list(result.replayed) if result.replayed else None,
                    "replayed_rule": result.replayed_rule,
                    "lane": result.lane,
                    "evidence": result.evidence,
                    "counterfactual": list(result.counterfactual)
                    if result.counterfactual
                    else None,
                    "pressures": result.pressures,
                    "config_hash_stored": result.config_hash_stored,
                    "config_hash_now": result.config_hash_now,
                    "same_config": result.same_config,
                    "reproduced": result.reproduced,
                    "verdict": result.verdict(),
                    "notes": result.notes,
                    "error": result.error,
                },
                indent=2,
                default=str,
            )
        )
        return 0 if result.reproduced and result.same_config else 1

    print(f"decision {result.decision_id}:")
    print(f"  stored:   {result.stored[0]}({result.stored[1] or '-'}) "
          f"rule={result.stored_rule}")
    if result.replayed:
        print(f"  replayed: {result.replayed[0]}({result.replayed[1] or '-'}) "
              f"rule={result.replayed_rule}")
    print(
        f"  config:   stored {result.config_hash_stored or '-'}, "
        f"now {result.config_hash_now}"
    )
    if result.pressures:
        print(
            "  pressure: "
            + ", ".join(f"{k}={v:.2f}" for k, v in sorted(result.pressures.items()))
        )
    else:
        print("  pressure: none recorded, so it was taken at pressure zero")
    if result.lane:
        print(f"  lane:     {result.lane} (evidence: {result.evidence})")
    if result.counterfactual:
        model, effort = result.counterfactual
        print(f"  without pressure: {model}({effort or '-'})")
    for note in result.notes:
        print(f"  note:     {note}")
    print(f"  {result.verdict()}")
    return 0 if result.reproduced and result.same_config else 1


def cmd_decisions(args: argparse.Namespace) -> int:
    from .pins import Store

    if getattr(args, "action", None) == "replay":
        if not args.decision_id:
            print("decisions replay needs a decision id", file=sys.stderr)
            return 2
        return cmd_replay(args)

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
        _print_decision_extras(row)
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


def _print_decision_extras(row: dict[str, Any]) -> None:
    """The packet, the lane and the counterfactual, when the row has them."""
    shadow = row.get("shadow_answers")
    if isinstance(shadow, dict) and shadow:
        bits = []
        for qid, answer in sorted(shadow.items()):
            if qid == "_invalid":
                bits.append(f"invalid={','.join(answer)}")
            elif isinstance(answer, dict):
                value = answer.get("value")
                bits.append(
                    f"{qid}={value:.2f}" if isinstance(value, float) else f"{qid}={value}"
                )
        print(f"    shadow: {' '.join(bits)}")
    if row.get("event_type") == "effort_plan":
        facts = row.get("features") or {}
        moved = (
            f"{facts.get('from_effort')} -> {facts.get('to_effort')}"
            if facts.get("action") == "change_effort"
            else f"kept {facts.get('from_effort')}"
        )
        print(
            f"    effort plan: {moved}  (model unchanged, base "
            f"{facts.get('base_effort')})  recommended="
            f"{facts.get('recommendation')}  turn={row.get('turn_id')}"
        )
        if facts.get("confirmations"):
            print(
                f"      downgrade confirmations: {facts['confirmations']} of "
                f"{facts.get('needed_confirmations')}"
            )
        if row.get("response_ms") is not None:
            print(
                f"      jev {round(row['jev_ms']) if row.get('jev_ms') else '-'}ms, "
                f"plan end to end {round(row['response_ms'])}ms"
            )
    laned = []
    if row.get("quality_lane"):
        laned.append(f"lane={row['quality_lane']}")
    if row.get("evidence"):
        laned.append(f"evidence={row['evidence']}")
    if row.get("action"):
        laned.append(f"action={row['action']}")
    if row.get("counterfactual"):
        laned.append(f"without pressure={row['counterfactual']}")
    if laned:
        print(f"    {'  '.join(laned)}")
    status = row.get("quota_status")
    if isinstance(status, dict) and status:
        print(
            "    quota: " + ", ".join(f"{k}={v}" for k, v in sorted(status.items()))
        )
    for item in row.get("experiment_routes") or []:
        if isinstance(item, dict):
            print(
                f"    experiment {item.get('policy')}: {item.get('model')}"
                f"({item.get('effort') or '-'})"
                f"{'  CHANGED' if item.get('changed') else ''}"
            )
    for item in row.get("exclusions") or []:
        if isinstance(item, dict):
            print(f"    ruled out {item.get('model')}: {item.get('reason')}")


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
    adaptation = row.get("adaptation") or row.get("adaptation_mode") or "off"
    print(
        f"  effort:   base={row['base_effort'] or '-'} "
        f"effective={row['effective_effort'] or '-'} "
        f"expected={row.get('expected_effort') or row['base_effort'] or '-'} "
        f"confirmed={row.get('confirmed_effort') or row['base_effort'] or '-'}"
    )
    print(
        f"            mode={row['effort_mode'] or 'fixed'}  adaptation={adaptation}"
        + (
            f"  lineage={row['effort_lineage']}"
            if row.get("effort_lineage") not in (None, "known")
            else ""
        )
    )
    epoch = int(row.get("compaction_epoch") or 0)
    if epoch:
        state = row.get("compaction_state") or "known"
        folded = (
            f"  compact:  the client has folded this history up {epoch} time(s); "
            f"the model did not change ({row['model_key'] or '-'}), and each "
            f"time the effective effort went back to the base effort "
            f"({row['base_effort'] or '-'}), because an update does not survive "
            "a compaction. A later turn may have asked for more since"
        )
        if state == "unknown":
            folded += (
                "\n            the last compaction was sent and nothing came "
                "back to say it finished; reconcile before adapting again"
            )
        print(folded)
    _print_ledger(row)
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


def _print_ledger(row: dict[str, Any]) -> None:
    """The recent turn plans, said in words that cannot be misread.

    "model unchanged" is the whole point of the experiment, so the line says
    it rather than leaving a reader to infer it from two identical columns.
    """
    plans = row.get("turn_plans") or []
    if not plans:
        return
    model = row.get("model_key") or "the bound model"
    print("  turns:")
    for plan in plans[-5:]:
        where = f"{plan.get('turn_id')} [{plan.get('status')}]"
        if plan.get("action") == "change_effort":
            what = (
                f"model unchanged ({model}); the next user turn requests "
                f"{plan.get('to_effort')} effort instead of "
                f"{plan.get('from_effort')} under the "
                f"{plan.get('mode') or 'active'} native update experiment"
            )
        elif plan.get("action") == "blocked":
            what = f"blocked, nothing changed: {plan.get('reason') or '-'}"
        else:
            what = (
                f"model and effort unchanged ({model} at "
                f"{plan.get('from_effort')}); recommended "
                f"{plan.get('recommendation') or '-'}"
            )
        print(f"    {where} {what}")


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


def _reconcile(sessions: Any, row: dict[str, Any], args: argparse.Namespace) -> int:
    """Say what really happened to an effort update nobody could call settled."""
    from .sessions import SessionError

    plan_id = args.plan
    if not plan_id:
        open_rows = [
            p for p in row["turn_plans"] if p["status"] in ("outcome_unknown", "accepted")
        ]
        if len(open_rows) != 1:
            print(
                "name the plan with --plan; this session has "
                f"{len(open_rows)} unsettled update(s)",
                file=sys.stderr,
            )
            return 2
        plan_id = open_rows[0]["plan_id"]
    plan = sessions.plan_by_id(plan_id)
    if plan is None or plan["session_id"] != row["session_id"]:
        print(f"unknown plan {plan_id!r} for this session", file=sys.stderr)
        return 1
    try:
        result = sessions.reconcile(plan, row, args.outcome)
    except SessionError as exc:
        # A plan that was never submitted, or one that already has its answer.
        # Nothing was written; say so rather than pretending it moved.
        print(exc.message, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(f"{plan_id}: {result['was']} -> {result['status']}")
    print(
        f"  effort: base={result['base_effort'] or '-'} "
        f"effective={result['effective_effort'] or '-'} "
        f"confirmed={result['confirmed_effort'] or '-'}  (model unchanged)"
    )
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """Inspect and close strict session bindings."""
    config = _load(args.config)
    store, sessions = _session_store(config)
    quota = _quota_freshness(config)
    try:
        if args.action == "list":
            rows = [
                row
                | {
                    "unresolved_requests": sessions.unresolved(row["session_id"]),
                    "turn_plans": sessions.plans(row["session_id"], limit=5),
                }
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
        row["turn_plans"] = sessions.plans(args.session_id, limit=10)
        if args.action == "reconcile":
            return _reconcile(sessions, row, args)
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
    # Imported here, as `serve` imports uvicorn here: the adapter is a client
    # and pulls in the HTTP stack, which `check-config` has no use for.
    from .adapter import DEFAULT_CLIENT as ADAPTER_CLIENT
    from .adapter import DEFAULT_PORT as ADAPTER_PORT
    from .adapter import DEFAULT_ROUTER as ADAPTER_ROUTER

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

    p_adapter = sub.add_parser(
        "adapter",
        help="run the experimental Responses client adapter in front of the router",
    )
    p_adapter.add_argument("--router", default=ADAPTER_ROUTER)
    p_adapter.add_argument("--alias", required=True, help="the strict alias to resolve")
    p_adapter.add_argument("--client", default=ADAPTER_CLIENT)
    p_adapter.add_argument("--host", default="127.0.0.1")
    p_adapter.add_argument("--port", type=int, default=ADAPTER_PORT)
    p_adapter.add_argument(
        "--upstream-key-file",
        help="a file holding the upstream credential, used only in place of "
        "the client's placeholder token",
    )
    p_adapter.add_argument(
        "--placeholder-token",
        default="placeholder",
        help="the bearer token a client sends when it has no real key",
    )
    p_adapter.add_argument("--admin-token-env", default="JEV_ROUTER_ADMIN_TOKEN")
    p_adapter.add_argument("--log-level", default="info")
    p_adapter.set_defaults(func=cmd_adapter)

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
    p_sess.add_argument("action", choices=["list", "show", "close", "reconcile"])
    p_sess.add_argument(
        "session_id", nargs="?", help="a session id, for show, close and reconcile"
    )
    p_sess.add_argument("-n", "--limit", type=int, default=20)
    p_sess.add_argument("--plan", help="the turn plan to reconcile")
    p_sess.add_argument(
        "--outcome",
        choices=["applied", "not_applied"],
        help="whether the provider really applied that effort update",
    )
    p_sess.add_argument("--json", action="store_true")
    p_sess.set_defaults(func=cmd_sessions)

    p_dec = sub.add_parser(
        "decisions", help="tail the decision log, or replay one decision"
    )
    p_dec.add_argument(
        "action",
        nargs="?",
        choices=["replay"],
        help="replay re-runs the selection from a stored row",
    )
    p_dec.add_argument(
        "decision_id", nargs="?", help="a decision id, or 'last', for replay"
    )
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
    if args.command == "sessions" and args.action == "reconcile" and not args.outcome:
        parser.error("sessions reconcile needs --outcome applied or not_applied")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
