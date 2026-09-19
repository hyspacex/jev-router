#!/usr/bin/env python
"""Layer 3 of TESTPLAN.md: the outcome benchmark.

Runs each gradeable case on all seven candidate routes, grades the replies,
and works out the cheapest route that is still good enough. That is the check
on whether the hand labels in cases.yaml were right.

    uv run python evals/run_outcomes.py                 # every spec
    uv run python evals/run_outcomes.py --case quick-percentage-74
    uv run python evals/run_outcomes.py --routes "ollama/glm-5.3-flash(none)"

Every reply and every judgement is cached on disk by a hash of the request, so
a rerun costs nothing. Writes evals/outcomes.json and evals/outcomes.md.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from checks import run_checks  # noqa: E402
from common import (  # noqa: E402
    EVALS_DIR,
    ROUTE_ORDER,
    Case,
    UpstreamClient,
    load_cases,
    median,
    route_cost,
)

# The blind judge. It has to be a strong model your proxy serves, and it should
# not be one of the candidate routes. Override with $JEV_EVAL_JUDGE_MODEL.
JUDGE_MODEL = os.environ.get("JEV_EVAL_JUDGE_MODEL", "").strip() or "gpt-5.6-sol(high)"
JUDGE_MAX_TOKENS = 800
CANDIDATE_MAX_TOKENS = 3000
ADEQUATE_GAP = 0.5  # a route is adequate if it scores within this of the best

JUDGE_SYSTEM = (
    "You are grading one answer produced by an unnamed assistant. You are given "
    "the request the assistant was sent and the answer it returned, then a "
    "rubric. Score the answer from 0 to 10 against the rubric only. Do not "
    "reward length, confidence or formatting beyond what the rubric asks for. "
    "Do not try to guess which model wrote the answer. Reply with JSON only, "
    'in the form {"score": <number 0-10>, "why": "<one sentence>"}.'
)


def route_name(model: str, effort: str) -> str:
    return f"{model}({effort})"


def as_messages(case: Case) -> list[dict[str, Any]]:
    """The case body's messages, with image parts dropped.

    The proxy route used here is text only, so an image case cannot be graded
    fairly. Those cases are left out of outcome_specs.yaml instead.
    """
    out = []
    for m in copy.deepcopy(case.body.get("messages") or []):
        content = m.get("content")
        if isinstance(content, list):
            m["content"] = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")
            )
        out.append({k: v for k, v in m.items() if k in ("role", "content", "name")})
    return out


def parse_judgement(text: str) -> tuple[float | None, str]:
    for candidate in re.findall(r"\{.*?\}", text, re.S):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if "score" in data:
            try:
                return max(0.0, min(10.0, float(data["score"]))), str(data.get("why", ""))
            except (TypeError, ValueError):
                continue
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*/\s*10", text)
    if m:
        return max(0.0, min(10.0, float(m.group(1)))), "parsed from text"
    return None, f"unparseable judgement: {text[:120]}"


async def grade_one(
    case: Case,
    spec: dict[str, Any],
    reply: str,
    upstream: UpstreamClient,
) -> tuple[float, str]:
    checks = spec.get("checks") or []
    rubric = spec.get("rubric")
    judge_weight = float(spec.get("judge_weight", 1.0 if rubric and not checks else 0.0))

    code_score, notes = (None, [])
    if checks:
        code_score, notes = run_checks(reply, checks)

    judge_score, why = None, ""
    if rubric and judge_weight > 0:
        if not reply.strip():
            judge_score, why = 0.0, "empty reply"
        else:
            prompt = (
                "REQUEST SENT TO THE ASSISTANT\n"
                "-----------------------------\n"
                + "\n\n".join(
                    f"[{m['role']}]\n{m['content']}" for m in as_messages(case)
                )[:14000]
                + "\n\nANSWER RETURNED\n---------------\n"
                + reply[:14000]
                + "\n\nRUBRIC\n------\n"
                + str(rubric)
            )
            res = await upstream.chat(
                JUDGE_MODEL,
                [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=JUDGE_MAX_TOKENS,
            )
            if res.error and not res.text:
                judge_score, why = None, f"judge failed: {res.error}"
            else:
                judge_score, why = parse_judgement(res.text)

    if code_score is not None and judge_score is not None:
        score = code_score * (1 - judge_weight) + judge_score * judge_weight
    elif code_score is not None:
        score = code_score
    elif judge_score is not None:
        score = judge_score
    else:
        score = 0.0
    note = "; ".join(notes[:3])
    if why:
        note = f"{note} | judge: {why}" if note else f"judge: {why}"
    return score, note


async def run_case(
    case: Case,
    spec: dict[str, Any],
    routes: list[tuple[str, str]],
    upstream: UpstreamClient,
) -> dict[str, Any]:
    messages = as_messages(case)

    async def one(model: str, effort: str) -> dict[str, Any]:
        res = await upstream.chat(
            route_name(model, effort), messages, max_tokens=CANDIDATE_MAX_TOKENS
        )
        score, note = await grade_one(case, spec, res.text, upstream)
        return {
            "route": route_name(model, effort),
            "model": model,
            "effort": effort,
            "cost": route_cost(model, effort),
            "score": round(score, 2),
            "note": note,
            "latency_ms": round(res.latency_ms),
            "output_tokens": res.output_tokens,
            "error": res.error,
            "cached": res.cached,
        }

    results = list(await asyncio.gather(*(one(m, e) for m, e in routes)))
    scored = [r for r in results if r["error"] is None or r["score"] > 0]
    best = max((r["score"] for r in scored), default=0.0)
    adequate = [r for r in scored if r["score"] >= best - ADEQUATE_GAP]
    cheapest = min(adequate, key=lambda r: r["cost"]) if adequate else None

    return {
        "case_id": case.id,
        "split": case.split,
        "label": {
            "task": case.task,
            "difficulty": case.difficulty,
            "tier": case.tier,
            "effort": case.effort,
            "acceptable_tiers": case.acceptable_tiers,
            "needs_faithfulness": case.needs_faithfulness,
        },
        "graded_by": "checks+judge"
        if (spec.get("checks") and spec.get("rubric"))
        else ("checks" if spec.get("checks") else "judge"),
        "routes": results,
        "best_score": round(best, 2),
        "cheapest_adequate": cheapest["route"] if cheapest else None,
        "cheapest_adequate_tier": (
            ("fast" if cheapest["model"].startswith("ollama/") else "frontier")
            if cheapest
            else None
        ),
        "cheapest_adequate_effort": cheapest["effort"] if cheapest else None,
    }


# --- report -------------------------------------------------------------


def build_report(rows: list[dict[str, Any]], routes: list[tuple[str, str]]) -> str:
    out: list[str] = []
    w = out.append
    w("# Outcome benchmark (layer 3)")
    w("")
    w(f"- when: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    w(f"- cases: {len(rows)}")
    w(f"- routes per case: {len(routes)}")
    w(f"- a route counts as adequate when it scores within {ADEQUATE_GAP} of the best route")
    w("")

    w("## Route averages")
    w("")
    w("| route | mean score | median score | wins outright | adequate on | median latency ms | median output tokens |")
    w("|---|---|---|---|---|---|---|")
    for model, effort in routes:
        name = route_name(model, effort)
        cells = [r for row in rows for r in row["routes"] if r["route"] == name]
        if not cells:
            continue
        scores = [c["score"] for c in cells]
        best_count = sum(
            1 for row in rows for c in row["routes"] if c["route"] == name and c["score"] >= row["best_score"] - 1e-9
        )
        adequate = sum(
            1
            for row in rows
            for c in row["routes"]
            if c["route"] == name and c["score"] >= row["best_score"] - ADEQUATE_GAP
        )
        w(
            f"| {name} | {statistics.fmean(scores):.2f} | {median(scores):.2f} | "
            f"{best_count}/{len(rows)} | {adequate}/{len(rows)} | "
            f"{median(c['latency_ms'] for c in cells):.0f} | "
            f"{median(c['output_tokens'] for c in cells):.0f} |"
        )
    w("")

    w("## Does effort change the answer?")
    w("")
    w("| model | effort pair | mean score difference | cases where the higher effort was better by 0.5+ |")
    w("|---|---|---|---|")
    for model in ("ollama/glm-5.3-flash", "gpt-6-astra"):
        efforts = [e for m, e in routes if m == model]
        for a, b in zip(efforts, efforts[1:]):
            diffs = []
            for row in rows:
                sa = next((c["score"] for c in row["routes"] if c["route"] == route_name(model, a)), None)
                sb = next((c["score"] for c in row["routes"] if c["route"] == route_name(model, b)), None)
                if sa is not None and sb is not None:
                    diffs.append(sb - sa)
            if diffs:
                w(
                    f"| {model.split('/')[-1]} | {a} -> {b} | {statistics.fmean(diffs):+.2f} | "
                    f"{sum(1 for d in diffs if d >= 0.5)}/{len(diffs)} |"
                )
    w("")

    w("## Cheapest adequate route per case, against the hand label")
    w("")
    w("| case | label tier/effort | cheapest adequate | best score | agrees |")
    w("|---|---|---|---|---|")
    for row in sorted(rows, key=lambda r: r["case_id"]):
        tier = row["cheapest_adequate_tier"]
        agrees = tier in row["label"]["acceptable_tiers"] if tier else False
        w(
            f"| {row['case_id']} | {row['label']['tier']}/{row['label']['effort']} | "
            f"{row['cheapest_adequate']} | {row['best_score']:.1f} | "
            f"{'yes' if agrees else 'NO'} |"
        )
    w("")

    proposals = label_proposals(rows)
    w("## Proposed label changes")
    w("")
    if not proposals:
        w("None. Every outcome agreed with its hand label.")
    else:
        w("| case | label | outcome says | confidence | applied |")
        w("|---|---|---|---|---|")
        for p in proposals:
            w(
                f"| {p['case_id']} | {p['from_tier']}/{p['from_effort']} | "
                f"{p['to_tier']}/{p['to_effort']} | {p['confidence']} | "
                f"{'yes' if p['clear_cut'] else 'no, left for a person'} |"
            )
        w("")
        for p in proposals:
            w(f"- **{p['case_id']}**: {p['reason']}")
    w("")
    return "\n".join(out) + "\n"


def label_proposals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where the outcome disagrees with the hand label, and how sure we are.

    Clear cut means: the disagreement is about the tier, every route of the
    cheaper tier scored within the adequacy gap of the best route, and the
    spread between routes is small enough that one judge call cannot explain
    it. Everything else is listed for a person to read.
    """
    out = []
    for row in rows:
        tier = row["cheapest_adequate_tier"]
        if tier is None:
            continue
        label_tiers = row["label"]["acceptable_tiers"]
        if tier in label_tiers:
            continue
        fast = [c for c in row["routes"] if c["model"].startswith("ollama/")]
        frontier = [c for c in row["routes"] if not c["model"].startswith("ollama/")]
        if tier == "fast":
            cheap_side, dear_side = fast, frontier
            reason_kind = "the fast model was good enough"
        else:
            cheap_side, dear_side = frontier, fast
            reason_kind = "the fast model was not good enough"
        cheap_min = min((c["score"] for c in cheap_side), default=0.0)
        cheap_max = max((c["score"] for c in cheap_side), default=0.0)
        dear_max = max((c["score"] for c in dear_side), default=0.0)
        clear = (
            cheap_min >= row["best_score"] - ADEQUATE_GAP
            if tier == "fast"
            else cheap_max - max((c["score"] for c in fast), default=0.0) >= 2.0
        )
        out.append(
            {
                "case_id": row["case_id"],
                "from_tier": row["label"]["tier"],
                "from_effort": row["label"]["effort"],
                "to_tier": tier,
                "to_effort": row["cheapest_adequate_effort"],
                "clear_cut": bool(clear),
                "confidence": "clear" if clear else "mixed",
                "reason": (
                    f"{reason_kind}: cheapest adequate route is "
                    f"{row['cheapest_adequate']}, best score {row['best_score']:.1f}, "
                    f"fast-tier scores {[c['score'] for c in fast]}, "
                    f"frontier-tier scores {[c['score'] for c in frontier]}."
                ),
            }
        )
    return out


def apply_clear_cut(proposals: list[dict[str, Any]], path: Path) -> list[str]:
    """Rewrite the clear-cut labels in cases.yaml, marking each one.

    Edits the text rather than round-tripping the YAML, so the anchors,
    comments and formatting survive.
    """
    text = path.read_text()
    changed = []
    for p in proposals:
        if not p["clear_cut"]:
            continue
        start = text.find(f"- id: {p['case_id']}\n")
        if start < 0:
            continue
        nxt = text.find("\n  - id: ", start)
        end = nxt if nxt > 0 else len(text)
        block = text[start:end]
        if "label_source: outcome" in block:
            continue
        new = re.sub(r"(\n      tier: )\w+", rf"\g<1>{p['to_tier']}", block, count=1)
        new = re.sub(r"(\n      effort: )\w+", rf"\g<1>{p['to_effort']}", new, count=1)
        new = re.sub(
            r"(\n      acceptable_tiers: )\[[^\]]*\]",
            rf"\g<1>[{p['to_tier']}]",
            new,
            count=1,
        )
        new = new.rstrip("\n") + "\n      label_source: outcome\n"
        text = text[:start] + new + text[end:]
        changed.append(p["case_id"])
    if changed:
        path.write_text(text)
    return changed


# --- main ---------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    specs = (yaml.safe_load((EVALS_DIR / "outcome_specs.yaml").read_text()) or {})["cases"]
    cases = {c.id: c for c in load_cases()}

    unknown = [cid for cid in specs if cid not in cases]
    if unknown:
        print(f"outcome_specs.yaml names cases that do not exist: {unknown}", file=sys.stderr)
        return 2

    wanted = specs
    if args.case:
        ids = {c.strip() for c in args.case.split(",")}
        wanted = {k: v for k, v in specs.items() if k in ids}

    routes = list(ROUTE_ORDER)
    if args.routes:
        names = [r.strip() for r in args.routes.split(",")]
        routes = [r for r in routes if route_name(*r) in names]

    upstream = UpstreamClient(concurrency=args.concurrency, cache=not args.no_cache)
    rows: list[dict[str, Any]] = []
    done = 0
    # A few cases run at once so the one call that is slow does not leave the
    # other three concurrency slots idle. `UpstreamClient` still holds the cap.
    cases_at_once = asyncio.Semaphore(args.cases_at_once)
    lock = asyncio.Lock()

    async def one_case(cid: str, spec: dict[str, Any]) -> None:
        nonlocal done
        async with cases_at_once:
            row = await run_case(cases[cid], spec, routes, upstream)
        async with lock:
            rows.append(row)
            done += 1
            scores = " ".join(f"{c['score']:4.1f}" for c in row["routes"])
            print(
                f"[{done:2d}/{len(wanted)}] {cid:36s} {scores}  -> "
                f"{row['cheapest_adequate']}",
                flush=True,
            )

    try:
        await asyncio.gather(*(one_case(cid, spec) for cid, spec in wanted.items()))
    finally:
        await upstream.aclose()
    rows.sort(key=lambda r: list(wanted).index(r["case_id"]))

    proposals = label_proposals(rows)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "adequate_gap": ADEQUATE_GAP,
        "routes": [route_name(*r) for r in routes],
        "cases": rows,
        "proposed_label_changes": proposals,
    }
    (EVALS_DIR / "outcomes.json").write_text(json.dumps(payload, indent=2))
    (EVALS_DIR / "outcomes.md").write_text(build_report(rows, routes))

    if args.apply_labels:
        changed = apply_clear_cut(proposals, EVALS_DIR / "cases.yaml")
        print(f"\napplied clear-cut label changes to {len(changed)} cases: {changed}")
    else:
        clear = [p["case_id"] for p in proposals if p["clear_cut"]]
        print(f"\n{len(proposals)} proposed label changes ({len(clear)} clear cut): {clear}")
        print("rerun with --apply-labels to write the clear-cut ones into cases.yaml")

    print(f"live upstream calls: {upstream.calls}")
    print(f"wrote {EVALS_DIR / 'outcomes.json'} and {EVALS_DIR / 'outcomes.md'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", help="comma separated case ids")
    p.add_argument("--routes", help="comma separated route names to run")
    p.add_argument("--concurrency", type=int, default=4,
                   help="upstream calls in flight at once")
    p.add_argument("--cases-at-once", type=int, default=3,
                   help="cases in flight at once; the concurrency cap still applies")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--apply-labels", action="store_true", help="write the clear-cut changes")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
