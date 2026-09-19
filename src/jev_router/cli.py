"""Command line: serve, check-config, explain, decisions."""

from __future__ import annotations

import argparse
import asyncio
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
        raise SystemExit(2)


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
    app = create_app(config)
    uvicorn.run(
        app,
        host=args.host or config.settings.host,
        port=args.port or config.settings.port,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    config = _load(args.config)
    print(f"{args.config}: ok")
    print(f"  mode:      {config.settings.mode}")
    print(f"  upstream:  {config.settings.upstream_base_url}")
    print(f"  listen:    {config.settings.host}:{config.settings.port}")
    print(f"  database:  {config.settings.db_path}")
    print(f"  models:    {', '.join(config.models)}")
    print(f"  questions: {', '.join(config.questions) or '(none)'}")
    print(f"  aliases:   {', '.join(config.aliases) or '(none)'}")
    key = os.environ.get(config.settings.jev_api_key_env)
    print(f"  {config.settings.jev_api_key_env}: {'set' if key else 'NOT SET (will fail open)'}")
    for alias, acfg in config.aliases.items():
        ruleset = config.ruleset_for(acfg)
        print(
            f"  alias {alias}: state={acfg.state_builder} "
            f"questions=[{', '.join(acfg.questions)}] rules={len(ruleset.rules)} "
            f"max_effort={acfg.max_effort or '-'}"
        )
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    import httpx

    from .deciders import build_decider
    from .features import extract_features
    from .state import build_state

    config = _load(args.config)
    body = json.loads(Path(args.request).read_text())
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
        async with httpx.AsyncClient(timeout=config.settings.jev_timeout_ms / 1000) as client:
            decider = build_decider(
                alias_cfg.decider or config.settings.decider, config, {"jev_client": client}
            )
            return await decider.decide(features, alias_cfg)

    decision = asyncio.run(run())
    print("\njev answers:")
    print(json.dumps(decision.answers, indent=2, default=str) if decision.answers else "  (none)")
    print("\ndecision:")
    print(f"  model:    {decision.model}")
    print(f"  effort:   {decision.effort}")
    print(f"  rule:     {decision.rule}")
    print(f"  reason:   {decision.reason}")
    print(f"  fallback: {decision.fallback}")
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
                bits.append(f"{qid}={ans.get('choice')}({ans.get('confidence', 0):.2f})")
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
                better = f" -> {mark['better_model']}({mark.get('better_effort') or '-'})"
            note = f" {mark['note']!r}" if mark.get("note") else ""
            print(f"    feedback: {mark['verdict']}{better}{note} ({mark.get('source')})")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-router", description=__doc__)
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to router.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the HTTP shim")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--mode", choices=["shadow", "active"])
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=cmd_serve)

    p_check = sub.add_parser("check-config", help="validate router.yaml and print a summary")
    p_check.set_defaults(func=cmd_check_config)

    p_explain = sub.add_parser(
        "explain", help="show features, state, Jev answers and the chosen model"
    )
    p_explain.add_argument("request", help="a JSON file holding a chat-completions body")
    p_explain.add_argument("--alias", help="treat the request as this alias")
    p_explain.set_defaults(func=cmd_explain)

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
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
