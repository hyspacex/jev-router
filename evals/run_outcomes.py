#!/usr/bin/env python
"""Layer 3 of TESTPLAN.md: the outcome benchmark.

Runs each gradeable case on all seven candidate routes, k times per route,
grades the replies, and works out the cheapest route that is still good
enough. That is the check on whether the hand labels in cases.yaml were right.

    uv run python evals/run_outcomes.py                 # every spec, k=3
    uv run python evals/run_outcomes.py --samples 1     # one sample per route
    uv run python evals/run_outcomes.py --case quick-percentage-74
    uv run python evals/run_outcomes.py --routes "ollama/glm-5.3-flash(none)"
    uv run python evals/run_outcomes.py --max-calls 400 # stop after 400 calls

Three things make the result trustworthy rather than merely available:

  - k samples per route, so a route's score comes with a spread, and the
    winner flip rate says how often the cheapest adequate route changes when
    the same run is resampled. A case whose winner flips is marked `unstable`
    and never overrides a hand label.
  - `finish_reason` is recorded, so a route that looks bad because its reply
    was cut off at the token cap is visible as truncation rather than quality.
  - a programmatic check overrides the judge wherever one exists, and the
    judge-versus-check discordance rate is reported on the cases that have
    both, so how far the judge can be trusted elsewhere is a measured number.

Every reply and every judgement is cached on disk by a hash of the request, so
a rerun costs nothing and a stopped run resumes. Cases are run one at a time in
priority order, so a run that stops on its budget leaves whole cases finished
rather than every case half done. Writes evals/outcomes.json and
evals/outcomes.md.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
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
    Budget,
    Case,
    UpstreamClient,
    load_cases,
    median,
    route_cost,
)
from metrics import rate as rate_of  # noqa: E402

# The blind judge. It has to be a strong model your proxy serves, and it should
# not be one of the candidate routes. Override with $JEV_EVAL_JUDGE_MODEL.
JUDGE_MODEL = os.environ.get("JEV_EVAL_JUDGE_MODEL", "").strip() or "gpt-5.6-sol(high)"
JUDGE_MAX_TOKENS = 800

# Raised from 3000. At 3000 a reasoning model at xhigh effort could run out of
# tokens mid-answer, which would show up as "more effort did not help" when
# what really happened is that the reply was cut off. `finish_reason` is now
# recorded per sample so the question is answered rather than assumed.
CANDIDATE_MAX_TOKENS = 8000

ADEQUATE_GAP = 0.5  # a route is adequate if it scores within this of the best

# How far a programmatic check and the judge may differ, on the 0-10 scale,
# before the pair counts as discordant.
DISCORDANCE_GAP = 2.0

# A case is `unstable` when the tier of its cheapest adequate route changes in
# more than this share of bootstrap resamples over the k samples. An unstable
# case is reported but never overrides a hand label.
UNSTABLE_TIER_FLIP = 0.20
BOOTSTRAP_DRAWS = 500

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
) -> dict[str, Any]:
    """Grade one reply. Where a programmatic check exists, it decides.

    The judge still runs when there is also a rubric, but only so the two can
    be compared. A check reads the answer; a judge reads the answer and its own
    priors. Where both exist the check is the ground truth and the judge is the
    thing being checked.
    """
    checks = spec.get("checks") or []
    rubric = spec.get("rubric")
    want_judge = bool(rubric) and float(
        spec.get("judge_weight", 1.0 if rubric and not checks else 0.0)
    ) > 0

    code_score, notes = (None, [])
    if checks:
        code_score, notes = run_checks(reply, checks)

    judge_score, why = None, ""
    if want_judge:
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

    if code_score is not None:
        score = code_score
    elif judge_score is not None:
        score = judge_score
    else:
        score = 0.0
    note = "; ".join(notes[:3])
    if why:
        note = f"{note} | judge: {why}" if note else f"judge: {why}"
    return {
        "score": score,
        "check_score": code_score,
        "judge_score": judge_score,
        "note": note,
    }


def _summarise_samples(
    model: str, effort: str, samples: list[dict[str, Any]]
) -> dict[str, Any]:
    """One route's k samples reduced to a row, keeping the spread."""
    scores = [s["score"] for s in samples]
    errors = [s["error"] for s in samples if s["error"]]
    finish = [s["finish_reason"] for s in samples]
    return {
        "route": route_name(model, effort),
        "model": model,
        "effort": effort,
        "cost": route_cost(model, effort),
        "score": round(statistics.fmean(scores), 2) if scores else 0.0,
        "scores": [round(s, 2) for s in scores],
        "score_sd": round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0,
        "samples": len(samples),
        "note": samples[0]["note"] if samples else "",
        "latency_ms": round(median(s["latency_ms"] for s in samples)) if samples else 0,
        "output_tokens": round(median(s["output_tokens"] for s in samples)) if samples else 0,
        "finish_reasons": finish,
        "truncated": sum(1 for f in finish if f == "length"),
        "check_scores": [s["check_score"] for s in samples],
        "judge_scores": [s["judge_score"] for s in samples],
        "error": errors[0] if len(errors) == len(samples) and errors else None,
        "cached": all(s["cached"] for s in samples),
    }


def _winner(results: list[dict[str, Any]], pick) -> dict[str, Any] | None:
    """The cheapest route within ADEQUATE_GAP of the best, on `pick(route)`."""
    scored = [r for r in results if r["error"] is None or pick(r) > 0]
    if not scored:
        return None
    best = max(pick(r) for r in scored)
    adequate = [r for r in scored if pick(r) >= best - ADEQUATE_GAP]
    return min(adequate, key=lambda r: r["cost"]) if adequate else None


def flip_rates(
    results: list[dict[str, Any]], draws: int = BOOTSTRAP_DRAWS, seed: int = 31
) -> dict[str, Any]:
    """How often the cheapest adequate route changes when the k samples are
    resampled.

    Each draw takes one sample per route, with replacement, and recomputes the
    winner. If the winner moves in most draws, the winner was a coin toss and
    the case says nothing about the label. The tier flip rate is the one that
    matters, since the tier is what a label records.
    """
    ks = [len(r["scores"]) for r in results if r["scores"]]
    if not results or not ks or max(ks) < 2:
        return {"route_flip_rate": 0.0, "tier_flip_rate": 0.0, "draws": 0}
    base = _winner(results, lambda r: r["score"])
    base_route = base["route"] if base else None
    base_tier = _tier(base["model"]) if base else None
    rng = random.Random(f"{results[0]['route']}:{seed}")
    route_moves = tier_moves = 0
    for _ in range(draws):
        drawn = []
        for r in results:
            scores = r["scores"] or [0.0]
            drawn.append({**r, "score": scores[rng.randrange(len(scores))]})
        w = _winner(drawn, lambda r: r["score"])
        if (w["route"] if w else None) != base_route:
            route_moves += 1
        if (_tier(w["model"]) if w else None) != base_tier:
            tier_moves += 1
    return {
        "route_flip_rate": route_moves / draws,
        "tier_flip_rate": tier_moves / draws,
        "draws": draws,
    }


def _tier(model: str) -> str:
    return "fast" if model.startswith("ollama/") else "frontier"


async def run_case(
    case: Case,
    spec: dict[str, Any],
    routes: list[tuple[str, str]],
    upstream: UpstreamClient,
    samples: int = 3,
) -> dict[str, Any]:
    messages = as_messages(case)

    async def one_sample(model: str, effort: str, index: int) -> dict[str, Any]:
        res = await upstream.chat(
            route_name(model, effort),
            messages,
            max_tokens=CANDIDATE_MAX_TOKENS,
            # Sample 0 keeps the plain key so an existing cache entry at this
            # cap is still a hit. Later samples get their own keys.
            cache_salt="" if index == 0 else f"sample-{index}",
        )
        graded = await grade_one(case, spec, res.text, upstream)
        return {
            **graded,
            "latency_ms": round(res.latency_ms),
            "output_tokens": res.output_tokens,
            "finish_reason": res.finish_reason,
            "error": res.error,
            "cached": res.cached,
        }

    async def one_route(model: str, effort: str) -> dict[str, Any]:
        got = []
        for i in range(samples):
            got.append(await one_sample(model, effort, i))
        return _summarise_samples(model, effort, got)

    results = list(await asyncio.gather(*(one_route(m, e) for m, e in routes)))
    cheapest = _winner(results, lambda r: r["score"])
    scored = [r for r in results if r["error"] is None or r["score"] > 0]
    best = max((r["score"] for r in scored), default=0.0)
    flips = flip_rates(results)

    return {
        "case_id": case.id,
        "slice": case.slice,
        "split": case.split,
        "samples": samples,
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
        "cheapest_adequate_tier": _tier(cheapest["model"]) if cheapest else None,
        "cheapest_adequate_effort": cheapest["effort"] if cheapest else None,
        "route_flip_rate": round(flips["route_flip_rate"], 3),
        "tier_flip_rate": round(flips["tier_flip_rate"], 3),
        "outcome_label": (
            "unstable"
            if flips["tier_flip_rate"] > UNSTABLE_TIER_FLIP
            else ("incomplete" if any(r["error"] for r in results) else "stable")
        ),
    }


# --- report -------------------------------------------------------------


def discordance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How often the judge and a programmatic check disagree.

    Only samples that have both are counted. The rate is the share where they
    differ by DISCORDANCE_GAP or more on the 0-10 scale, and the pass-level
    rate is the share where one would call the answer adequate and the other
    would not. Both are reported because they fail differently: a judge that is
    2 points optimistic everywhere is usable, and one that calls a failing
    answer a pass is not.
    """
    pairs: list[tuple[float, float]] = []
    for row in rows:
        for cell in row["routes"]:
            for check, judge in zip(cell.get("check_scores") or [], cell.get("judge_scores") or []):
                if check is not None and judge is not None:
                    pairs.append((float(check), float(judge)))
    if not pairs:
        return {"n": 0}
    gap = [abs(c - j) >= DISCORDANCE_GAP for c, j in pairs]
    pass_flip = [(c >= 7.0) != (j >= 7.0) for c, j in pairs]
    return {
        "n": len(pairs),
        "gap_rate": rate_of(gap),
        "pass_rate": rate_of(pass_flip),
        "mean_signed": statistics.fmean(j - c for c, j in pairs),
        "judge_higher": rate_of(j > c for c, j in pairs),
    }


def build_report(rows: list[dict[str, Any]], routes: list[tuple[str, str]]) -> str:
    out: list[str] = []
    w = out.append
    ks = {row.get("samples", 1) for row in rows}
    w("# Outcome benchmark (layer 3)")
    w("")
    w(f"- when: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    w(f"- cases: {len(rows)}")
    w(f"- routes per case: {len(routes)}")
    w(f"- samples per route: {', '.join(str(k) for k in sorted(ks))}")
    w(f"- candidate token cap: {CANDIDATE_MAX_TOKENS}")
    w(f"- a route counts as adequate when it scores within {ADEQUATE_GAP} of the best route")
    w("- where a programmatic check exists it decides the score; the judge still "
      "runs so the two can be compared")
    w("")

    w("## Route averages")
    w("")
    w("A route's score is the mean of its k samples. `spread` is the mean "
      "within-route standard deviation across samples: it is the noise floor, "
      "and a difference between two routes smaller than this is not a "
      "difference. `truncated` counts samples that stopped at the token cap "
      f"of {CANDIDATE_MAX_TOKENS} rather than finishing.")
    w("")
    w("| route | mean score | 95% CI | spread | wins outright | adequate on | truncated % | median latency ms | median output tokens |")
    w("|---|---|---|---|---|---|---|---|---|")
    for model, effort in routes:
        name = route_name(model, effort)
        cells = [r for row in rows for r in row["routes"] if r["route"] == name]
        if not cells:
            continue
        scores = [c["score"] for c in cells]
        mean = statistics.fmean(scores)
        # Normal interval on the mean of a 0-10 score, which is not a rate, so
        # Wilson does not apply.
        sem = (statistics.pstdev(scores) / (len(scores) ** 0.5)) if len(scores) > 1 else 0.0
        best_count = sum(
            1 for row in rows for c in row["routes"] if c["route"] == name and c["score"] >= row["best_score"] - 1e-9
        )
        adequate = sum(
            1
            for row in rows
            for c in row["routes"]
            if c["route"] == name and c["score"] >= row["best_score"] - ADEQUATE_GAP
        )
        n_samples = sum(c.get("samples", 1) for c in cells)
        truncated = sum(c.get("truncated", 0) for c in cells)
        w(
            f"| {name} | {mean:.2f} | [{mean - 1.96 * sem:.2f}, {mean + 1.96 * sem:.2f}] | "
            f"{statistics.fmean(c.get('score_sd', 0.0) for c in cells):.2f} | "
            f"{best_count}/{len(rows)} | {adequate}/{len(rows)} | "
            f"{100.0 * truncated / n_samples if n_samples else 0.0:.1f} | "
            f"{median(c['latency_ms'] for c in cells):.0f} | "
            f"{median(c['output_tokens'] for c in cells):.0f} |"
        )
    w("")

    w("## Winner stability")
    w("")
    w("The cheapest adequate route per case, recomputed on bootstrap resamples "
      "of the k samples. A case whose tier flips in more than "
      f"{UNSTABLE_TIER_FLIP:.0%} of draws is marked `unstable` and is not "
      "allowed to override a hand label.")
    w("")
    flips = [row.get("tier_flip_rate", 0.0) for row in rows]
    route_flips = [row.get("route_flip_rate", 0.0) for row in rows]
    unstable = [row["case_id"] for row in rows if row.get("outcome_label") == "unstable"]
    if flips:
        w(f"- mean tier flip rate: {statistics.fmean(flips):.3f}")
        w(f"- mean route flip rate: {statistics.fmean(route_flips):.3f}")
        w(f"- unstable cases: {len(unstable)}/{len(rows)}"
          + (f" ({', '.join(sorted(unstable))})" if unstable else ""))
    w("")

    disc = discordance(rows)
    w("## Judge against the programmatic check")
    w("")
    if not disc["n"]:
        w("No sample had both a check and a judge score, so there is nothing to compare.")
    else:
        w(f"- samples with both: {disc['n']}")
        w(f"- differ by {DISCORDANCE_GAP:g} points or more: {disc['gap_rate']}")
        w(f"- disagree on whether the answer passes (7 of 10): {disc['pass_rate']}")
        w(f"- the judge is higher on: {disc['judge_higher']}")
        w(f"- mean signed difference, judge minus check: {disc['mean_signed']:+.2f}")
        w("")
        w("Where the two disagree, the check is what the score used. The rate "
          "above is how much to discount the judge-only cases.")
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
    w("| case | label tier/effort | cheapest adequate | best score | tier flip | outcome | agrees |")
    w("|---|---|---|---|---|---|---|")
    for row in sorted(rows, key=lambda r: r["case_id"]):
        tier = row["cheapest_adequate_tier"]
        agrees = tier in row["label"]["acceptable_tiers"] if tier else False
        w(
            f"| {row['case_id']} | {row['label']['tier']}/{row['label']['effort']} | "
            f"{row['cheapest_adequate']} | {row['best_score']:.1f} | "
            f"{row.get('tier_flip_rate', 0.0):.2f} | {row.get('outcome_label', '')} | "
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
        # A case whose winner moves when the same run is resampled cannot
        # overturn anything. It is reported and then ignored.
        if row.get("outcome_label") == "unstable":
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

    order = list(wanted)
    if args.priority:
        # Cases that already have results come first, so the k samples land on
        # the cases with something to compare against before any new case is
        # started. Everything else keeps its file order behind them.
        previous = EVALS_DIR / "outcomes.json"
        known: list[str] = []
        if previous.exists():
            try:
                known = [
                    row["case_id"]
                    for row in json.loads(previous.read_text()).get("cases", [])
                ]
            except ValueError:
                known = []
        first = [cid for cid in known if cid in wanted]
        order = first + [cid for cid in wanted if cid not in first]

    budget = Budget(max_calls=args.max_calls) if args.max_calls else None
    upstream = UpstreamClient(
        concurrency=args.concurrency, cache=not args.no_cache, budget=budget
    )
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []

    # One case at a time, in priority order. A run that stops on its budget
    # then leaves whole cases finished instead of every case half sampled,
    # which is what makes the result usable rather than merely partial.
    try:
        for i, cid in enumerate(order, 1):
            if budget is not None and budget.stop_reason:
                skipped.append(cid)
                continue
            row = await run_case(
                cases[cid], wanted[cid], routes, upstream, samples=args.samples
            )
            # A case is only reported when every route ran. A partly
            # sampled case would otherwise enter the averages with zeros
            # for the routes the budget never reached.
            if any("not run" in str(c["error"] or "") for c in row["routes"]):
                skipped.append(cid)
                continue
            rows.append(row)
            scores = " ".join(f"{c['score']:4.1f}" for c in row["routes"])
            flip = row.get("tier_flip_rate", 0.0)
            print(
                f"[{i:2d}/{len(order)}] {cid:36s} {scores}  -> "
                f"{row['cheapest_adequate']}  flip {flip:.2f}"
                + (f"  [{budget.line()}]" if budget else ""),
                flush=True,
            )
    finally:
        await upstream.aclose()

    if skipped:
        print(f"\nnot run ({len(skipped)}): {', '.join(skipped)}")
        print("rerun the same command to pick up where this stopped; "
              "everything finished is cached")

    proposals = label_proposals(rows)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "adequate_gap": ADEQUATE_GAP,
        "samples": args.samples,
        "candidate_max_tokens": CANDIDATE_MAX_TOKENS,
        "routes": [route_name(*r) for r in routes],
        "budget": budget.line() if budget else "",
        "not_run": skipped,
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
    if budget is not None:
        print(budget.line())
    print(f"wrote {EVALS_DIR / 'outcomes.json'} and {EVALS_DIR / 'outcomes.md'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", help="comma separated case ids")
    p.add_argument("--routes", help="comma separated route names to run")
    p.add_argument("--samples", type=int, default=3,
                   help="replies per case per route; the score is their mean")
    p.add_argument("--concurrency", type=int, default=2,
                   help="upstream calls in flight at once")
    p.add_argument("--max-calls", type=int, default=0,
                   help="stop after this many live upstream calls, 0 for no limit. "
                        "The run also stops on its own if median latency triples "
                        "or replies come back empty, and is resumable either way")
    p.add_argument("--no-priority", dest="priority", action="store_false",
                   help="run cases in file order instead of resampling the "
                        "already-measured ones first")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--apply-labels", action="store_true", help="write the clear-cut changes")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
