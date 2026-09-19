#!/usr/bin/env python
"""The admission test from POOL.md: does a candidate model earn a lane?

`run_outcomes.py` asks which of the routes already in the pool is cheapest for
a case. This asks a different question: a named set of cases, a named set of
routes, one incumbent, and a pass or fail for each candidate. Adding a future
model means adding a route to `evals/admission.yaml` and rerunning one step.

    uv run python evals/admission.py --step mid --max-calls 120
    uv run python evals/admission.py --step creative --max-calls 90
    uv run python evals/admission.py --report            # rebuild ADMISSION.md

Grading reuses `evals/outcome_specs.yaml` and `evals/checks.py`. Where a case
has programmatic checks the checks decide and no judge call is made at all:
the judge sat 1.5 points low and disagreed with the checks on a third of
samples in the last outcome run, and judge calls come out of the same upstream
budget as the answers being graded. Cases with no checks fall back to the
rubric and the blind judge.

Two grading modes:

  score     each reply is scored 0 to 10 on its own, k samples per route.
  pairwise  two routes' replies to the same case are shown to the judge side
            by side, twice, with A and B swapped. A win counts only when both
            orders agree, so a judge that simply prefers the second answer
            produces no wins at all. Run alongside the 0 to 10 rubric, not
            instead of it.

Everything is cached on disk by the request, so a stopped run resumes and a
rerun is free. Results accumulate per step in evals/admission_results.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import os
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from checks import run_checks  # noqa: E402
from common import EVALS_DIR, Budget, Case, UpstreamClient, load_cases, median  # noqa: E402
from run_outcomes import JUDGE_SYSTEM, parse_judgement  # noqa: E402

SPEC_FILE = EVALS_DIR / "admission.yaml"
RESULTS_FILE = EVALS_DIR / "admission_results.json"
REPORT_FILE = EVALS_DIR / "ADMISSION.md"

CANDIDATE_MAX_TOKENS = 8000

PAIRWISE_SYSTEM = (
    "You are comparing two answers, A and B, to the same request. You are "
    "given the request, then answer A, then answer B, then a rubric. Decide "
    "which answer is better against the rubric only. Ignore which is longer, "
    "which sounds more confident, and which position it is in. Do not try to "
    "guess which model wrote either one. If they are genuinely equal, say "
    'tie. Reply with JSON only, in the form {"winner": "A"|"B"|"tie", '
    '"why": "<one sentence>"}.'
)


# --- pure helpers -------------------------------------------------------


def parse_pairwise(text: str) -> str | None:
    """The winner letter from a judge reply, or None if it cannot be read."""
    import re

    for candidate in re.findall(r"\{.*?\}", text, re.S):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        winner = str(data.get("winner", "")).strip().upper()
        if winner in ("A", "B"):
            return winner
        if winner.startswith("TIE"):
            return "tie"
    # Fall back to prose, but only close to the word that names a choice, so
    # "answer A opens well, but B wins" does not resolve to A.
    m = re.search(r"\b(?:winner|better|prefer(?:red)?)\b.{0,24}?\b([AB])\b", text, re.I | re.S)
    return m.group(1).upper() if m else None


def pairwise_aggregate(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-pair judgements into a result for one candidate-incumbent pair.

    Each comparison holds the winner the judge picked with the candidate shown
    first (`forward`) and with it shown second (`reverse`), already translated
    from A/B into "candidate", "incumbent" or "tie". A pair counts as a win
    only when both orders agree on the same route. Anything else is
    `undecided`, which includes a genuine tie, a disagreement between the two
    orders, and a judgement that could not be parsed.

    `net_rate` is (wins - losses) / decided. It runs from -1 to 1 and is 0
    when the two routes split the decided pairs evenly. `order_bias` is the
    share of pairs where the judge picked whichever answer came first, which
    is the number that says whether the swap was worth doing.
    """
    wins = losses = ties = undecided = 0
    first_position = 0
    for c in comparisons:
        fwd, rev = c.get("forward"), c.get("reverse")
        if fwd is None or rev is None:
            undecided += 1
            continue
        # Forward shows the candidate as A, reverse shows it as B.
        if c.get("forward_letter") == "A" and c.get("reverse_letter") == "A":
            first_position += 1
        if fwd == rev == "candidate":
            wins += 1
        elif fwd == rev == "incumbent":
            losses += 1
        elif fwd == rev == "tie":
            ties += 1
        else:
            undecided += 1
    decided = wins + losses
    return {
        "pairs": len(comparisons),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "undecided": undecided,
        "decided": decided,
        "net_rate": round((wins - losses) / decided, 3) if decided else 0.0,
        "order_bias": round(first_position / len(comparisons), 3) if comparisons else 0.0,
    }


def verdict(
    candidate: dict[str, Any],
    incumbent: dict[str, Any],
    rule: dict[str, Any],
    pairwise: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POOL.md's admission rule, applied to two summary rows.

    A general lane: the candidate's mean score is within `score_gap` of the
    incumbent's, and it is either `speed_gain` faster or on a different
    provider. A specialist lane: the same score floor, and it must also win
    the blind pairwise comparison by at least `min_net_rate`.
    """
    gap = float(rule.get("score_gap", 0.5))
    speed = float(rule.get("speed_gain", 0.30))
    deficit = incumbent["mean"] - candidate["mean"]
    quality_ok = deficit <= gap
    inc_latency = incumbent.get("latency_ms") or 0
    cand_latency = candidate.get("latency_ms") or 0
    faster = bool(inc_latency) and cand_latency <= inc_latency * (1.0 - speed)
    speedup = (inc_latency / cand_latency) if cand_latency else 0.0
    other_provider = candidate.get("provider") != incumbent.get("provider")
    reasons = []
    if not quality_ok:
        reasons.append(f"scores {deficit:.2f} below the incumbent, more than {gap}")
    if not (faster or other_provider):
        reasons.append("neither 30% faster nor on another provider")

    if pairwise is None:
        passed = quality_ok and (faster or other_provider)
    else:
        floor = float(rule.get("min_net_rate", 0.5))
        pair_ok = pairwise["decided"] > 0 and pairwise["net_rate"] >= floor
        if not pair_ok:
            reasons.append(
                f"pairwise net {pairwise['net_rate']:+.2f} over "
                f"{pairwise['decided']} decided pairs, needs {floor:+.2f}"
            )
        passed = quality_ok and pair_ok
    return {
        "pass": bool(passed),
        "score_deficit": round(deficit, 2),
        "quality_ok": bool(quality_ok),
        "faster_30pct": bool(faster),
        "speedup": round(speedup, 2),
        "different_provider": bool(other_provider),
        "why_not": "; ".join(reasons),
    }


def summarise_route(route: str, meta: dict[str, Any], cells: list[dict[str, Any]]) -> dict[str, Any]:
    """One route over all the cases in a step, reduced to a row."""
    per_case = [c["score"] for c in cells]
    spreads = [c["score_sd"] for c in cells]
    latencies = [c["latency_ms"] for c in cells if c["latency_ms"]]
    tokens = [c["output_tokens"] for c in cells if c["output_tokens"]]
    n_samples = sum(c["samples"] for c in cells)
    truncated = sum(c["truncated"] for c in cells)
    empty = sum(c["empty"] for c in cells)
    sem = (statistics.pstdev(per_case) / len(per_case) ** 0.5) if len(per_case) > 1 else 0.0
    mean = statistics.fmean(per_case) if per_case else 0.0
    return {
        "route": route,
        "provider": meta.get("provider", "?"),
        "cases": len(cells),
        "samples": n_samples,
        "mean": round(mean, 2),
        "ci": [round(mean - 1.96 * sem, 2), round(mean + 1.96 * sem, 2)],
        "spread": round(statistics.fmean(spreads), 2) if spreads else 0.0,
        "min_case": round(min(per_case), 2) if per_case else 0.0,
        "adequate": sum(1 for c in cells if c["score"] >= 7.0),
        "latency_ms": round(median(latencies)) if latencies else 0,
        "output_tokens": round(median(tokens)) if tokens else 0,
        "truncated": truncated,
        "truncation_rate": round(100.0 * truncated / n_samples, 1) if n_samples else 0.0,
        "empty": empty,
        "live_calls": sum(c["live"] for c in cells),
    }


def paired_summary(
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    candidate: str,
    incumbent: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Both routes summarised over only the cases where both of them ran.

    The verdict has to be a paired comparison. An incumbent that joins a step
    from the cache may cover three of five cases, and comparing its mean on
    those three against a candidate's mean on all five compares two different
    case sets. Where every case has both routes this returns the same numbers
    as the unpaired summary.
    """
    both = [
        row for row in rows
        if candidate in row["routes"] and incumbent in row["routes"]
    ]
    if not both:
        return None
    return (
        summarise_route(candidate, meta.get(candidate, {}), [r["routes"][candidate] for r in both]),
        summarise_route(incumbent, meta.get(incumbent, {}), [r["routes"][incumbent] for r in both]),
    )


MID_GAP = 0.5      # how far behind the best route the mid model may sit
MID_FLOOR = 7.0    # and no single sample of it may fall below this


def mid_label_proposals(
    rows: list[dict[str, Any]], mid_route: str
) -> list[dict[str, Any]]:
    """Which step-1 cases the outcome says the mid lane could take.

    A case qualifies when the mid model's mean is within `MID_GAP` of the best
    route on that case and neither of its samples fell below `MID_FLOOR`. The
    mean alone is not enough: a route that scores 10 and 5 averages 7.5, and a
    lane that fails half the time is not a lane. This only ever adds `mid` to
    `acceptable_tiers`; it never removes a tier and never moves `tier`.
    """
    out = []
    for row in rows:
        cell = row["routes"].get(mid_route)
        if not cell:
            continue
        best = max(c["score"] for c in row["routes"].values())
        worst_sample = min(cell["scores"]) if cell["scores"] else 0.0
        ok = cell["score"] >= best - MID_GAP and worst_sample >= MID_FLOOR
        out.append({
            "case_id": row["case_id"],
            "add_mid": bool(ok),
            "mid_score": cell["score"],
            "best_score": round(best, 2),
            "worst_sample": round(worst_sample, 2),
        })
    return out


def apply_mid_labels(proposals: list[dict[str, Any]], path: Path) -> list[str]:
    """Add `mid` to `acceptable_tiers` in cases.yaml, marking each case.

    Edits the text rather than round-tripping the YAML, the way
    `run_outcomes.apply_clear_cut` does, so the anchors and comments survive.
    """
    import re

    text = path.read_text()
    changed = []
    for p in proposals:
        if not p["add_mid"]:
            continue
        start = text.find(f"- id: {p['case_id']}\n")
        if start < 0:
            continue
        nxt = text.find("\n  - id: ", start)
        end = nxt if nxt > 0 else len(text)
        block = text[start:end]
        m = re.search(r"\n      acceptable_tiers: \[([^\]]*)\]", block)
        if not m:
            continue
        tiers = [t.strip() for t in m.group(1).split(",") if t.strip()]
        # Cheapest first, which is the order the rest of the file uses.
        order = {"fast": 0, "mid": 1, "frontier": 2}
        wanted = sorted(set(tiers + ["mid"]), key=lambda t: order.get(t, 9))
        new = block
        if wanted != tiers:
            new = (
                block[: m.start()]
                + f"\n      acceptable_tiers: [{', '.join(wanted)}]"
                + block[m.end():]
            )
        # `label_source` belongs beside `id` and `expected`, at the case
        # level, because that is where `common._one_case` reads it from. Put
        # inside `expected` it is silently ignored.
        if not re.search(r"\n    label_source:", new):
            new = new.replace("\n    expected:", "\n    label_source: outcome\n    expected:", 1)
        if new == block:
            continue
        text = text[:start] + new + text[end:]
        changed.append(p["case_id"])
    if changed:
        path.write_text(text)
    return changed


def wins_per_case(rows: list[dict[str, Any]], routes: list[str]) -> dict[str, int]:
    """How often each route was top of its case, ties shared."""
    out = {r: 0 for r in routes}
    for row in rows:
        scores = {r: c["score"] for r, c in row["routes"].items() if r in out}
        if not scores:
            continue
        best = max(scores.values())
        for r, s in scores.items():
            if s >= best - 1e-9:
                out[r] += 1
    return out


# --- request building ---------------------------------------------------


def text_messages(case: Case) -> list[dict[str, Any]]:
    """The case's messages as plain text, image parts dropped."""
    out = []
    for m in copy.deepcopy(case.body.get("messages") or []):
        content = m.get("content")
        if isinstance(content, list):
            m["content"] = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")
            )
        out.append({k: v for k, v in m.items() if k in ("role", "content", "name")})
    return out


def image_messages(spec: dict[str, Any], png: Path) -> list[dict[str, Any]]:
    """One user turn carrying a generated PNG as a base64 data URL."""
    url = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    parts: list[dict[str, Any]] = [{"type": "text", "text": spec["prompt"]}]
    parts.append({"type": "image_url", "image_url": {"url": url}})
    messages = []
    if spec.get("system"):
        messages.append({"role": "system", "content": spec["system"]})
    messages.append({"role": "user", "content": parts})
    return messages


def prompt_text(messages: list[dict[str, Any]]) -> str:
    """The request as the judge sees it. Image parts become a placeholder."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            bits = []
            for p in content:
                if p.get("type") == "text":
                    bits.append(p.get("text", ""))
                else:
                    bits.append("[an image was attached]")
            content = "\n".join(bits)
        out.append(f"[{m['role']}]\n{content}")
    return "\n\n".join(out)


# --- grading ------------------------------------------------------------


async def judge_score(
    messages: list[dict[str, Any]],
    rubric: str,
    reply: str,
    upstream: UpstreamClient,
    judge_model: str,
    judge_max_tokens: int,
) -> tuple[float | None, str]:
    if not reply.strip():
        return 0.0, "empty reply"
    prompt = (
        "REQUEST SENT TO THE ASSISTANT\n-----------------------------\n"
        + prompt_text(messages)[:14000]
        + "\n\nANSWER RETURNED\n---------------\n"
        + reply[:14000]
        + "\n\nRUBRIC\n------\n"
        + str(rubric)
    )
    res = await upstream.chat(
        judge_model,
        [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=judge_max_tokens,
    )
    if res.error and not res.text:
        return None, f"judge failed: {res.error}"
    return parse_judgement(res.text)


async def judge_pair(
    messages: list[dict[str, Any]],
    rubric: str,
    first: str,
    second: str,
    upstream: UpstreamClient,
    judge_model: str,
    judge_max_tokens: int,
    salt: str,
) -> tuple[str | None, str]:
    prompt = (
        "REQUEST SENT TO BOTH ASSISTANTS\n-------------------------------\n"
        + prompt_text(messages)[:12000]
        + "\n\nANSWER A\n--------\n"
        + first[:9000]
        + "\n\nANSWER B\n--------\n"
        + second[:9000]
        + "\n\nRUBRIC\n------\n"
        + str(rubric)
    )
    res = await upstream.chat(
        judge_model,
        [{"role": "system", "content": PAIRWISE_SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=judge_max_tokens,
        cache_salt=salt,
    )
    if res.error and not res.text:
        return None, f"judge failed: {res.error}"
    return parse_pairwise(res.text), res.text[:160]


async def grade(
    messages: list[dict[str, Any]],
    spec: dict[str, Any],
    reply: str,
    upstream: UpstreamClient,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Checks decide where they exist; otherwise the rubric and the judge."""
    checks = spec.get("checks") or []
    if checks:
        score, notes = run_checks(reply, checks)
        return {
            "score": score,
            "graded_by": "checks",
            "note": "; ".join(notes[:3]),
        }
    rubric = spec.get("rubric")
    if not rubric:
        return {"score": 0.0, "graded_by": "none", "note": "no spec"}
    score, why = await judge_score(
        messages, rubric, reply, upstream, cfg["judge_model"], cfg["judge_max_tokens"]
    )
    return {
        "score": score if score is not None else 0.0,
        "graded_by": "judge",
        "note": f"judge: {why}",
    }


# --- the run ------------------------------------------------------------


class Step:
    def __init__(self, name: str, cfg: dict[str, Any], spec: dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.raw = spec
        self.routes: list[str] = list(spec["routes"])
        self.incumbent: str = spec["incumbent"]
        self.candidates: list[str] = [r for r in self.routes if r != self.incumbent]
        self.cached_only: set[str] = set(spec.get("cached_only") or [])
        self.samples: int = int(spec.get("samples", 2))
        self.mode: str = spec.get("mode", "score")
        self.pairs: int = int(spec.get("pairs", 2))


def route_meta(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    meta = cfg["routes"].get(name)
    if meta is None:
        raise KeyError(f"unknown route {name!r}; known: {', '.join(sorted(cfg['routes']))}")
    return meta


def routes_for_case(step: Step, cfg: dict[str, Any], needs_vision: bool) -> list[str]:
    out = []
    for r in step.routes:
        meta = route_meta(cfg, r)
        if needs_vision and not meta.get("vision"):
            continue
        out.append(r)
    return out


async def run_step(
    step: Step,
    cfg: dict[str, Any],
    upstream: UpstreamClient,
    cases: dict[str, Case],
    specs: dict[str, Any],
    images: dict[str, Any],
    budget: Budget | None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    comparisons: dict[str, list[dict[str, Any]]] = {c: [] for c in step.candidates}
    rng = random.Random(f"admission:{step.name}")

    for index, case_id in enumerate(step.raw["cases"], 1):
        if budget is not None and budget.stop_reason:
            break
        if case_id in images:
            img = images[case_id]
            messages = img["messages"]
            spec = {"checks": img["spec"].get("checks"), "rubric": img["spec"].get("rubric")}
            needs_vision = True
        else:
            case = cases[case_id]
            messages = text_messages(case)
            spec = specs.get(case_id) or {}
            needs_vision = False
            if not spec:
                print(f"  skipping {case_id}: no grading spec", flush=True)
                continue

        here = routes_for_case(step, cfg, needs_vision)
        replies: dict[str, list[str]] = {}
        cells: dict[str, dict[str, Any]] = {}

        async def one_route(route: str) -> tuple[str, dict[str, Any], list[str]]:
            meta = route_meta(cfg, route)
            got, texts = [], []
            for i in range(step.samples):
                salt = "" if i == 0 else f"sample-{i}"
                # A route listed under `cached_only` is read from the disk
                # cache or left out. This is how the incumbent joins a step
                # for free when an earlier run already answered the case.
                if route in step.cached_only and not upstream.cache.get(
                    _key(meta["upstream"], messages, salt)
                ):
                    continue
                res = await upstream.chat(
                    meta["upstream"], messages,
                    max_tokens=CANDIDATE_MAX_TOKENS, cache_salt=salt,
                )
                if res.error and "not run" in str(res.error):
                    continue
                graded = await grade(messages, spec, res.text, upstream, cfg)
                texts.append(res.text)
                got.append({
                    **graded,
                    "latency_ms": round(res.latency_ms),
                    "output_tokens": res.output_tokens,
                    "finish_reason": res.finish_reason,
                    "error": res.error,
                    "cached": res.cached,
                })
            return route, _cell(got), texts

        results = await asyncio.gather(*(one_route(r) for r in here))
        for route, cell, texts in results:
            if cell["samples"] == 0:
                continue
            cells[route] = cell
            replies[route] = texts

        if not cells:
            continue

        rows.append({
            "case_id": case_id,
            "routes": cells,
            "graded_by": (cells[next(iter(cells))] or {}).get("graded_by", ""),
        })

        # Pairwise, judged twice per pair with the order swapped.
        if step.mode == "pairwise" and spec.get("rubric") and step.incumbent in replies:
            inc_texts = replies[step.incumbent]
            for cand in step.candidates:
                for p in range(min(step.pairs, len(replies.get(cand, [])), len(inc_texts))):
                    a, b = replies[cand][p], inc_texts[p]
                    if not a.strip() or not b.strip():
                        continue
                    # Which route is shown first is randomised per pair, then
                    # the same pair is judged again the other way round.
                    cand_first = rng.random() < 0.5
                    first, second = (a, b) if cand_first else (b, a)
                    w1, _ = await judge_pair(
                        messages, spec["rubric"], first, second, upstream,
                        cfg["judge_model"], cfg["judge_max_tokens"],
                        salt=f"{step.name}:{case_id}:{cand}:{p}:fwd",
                    )
                    w2, _ = await judge_pair(
                        messages, spec["rubric"], second, first, upstream,
                        cfg["judge_model"], cfg["judge_max_tokens"],
                        salt=f"{step.name}:{case_id}:{cand}:{p}:rev",
                    )
                    comparisons[cand].append({
                        "case_id": case_id,
                        "pair": p,
                        "candidate_first": cand_first,
                        "forward_letter": w1,
                        "reverse_letter": w2,
                        "forward": _side(w1, cand_first),
                        "reverse": _side(w2, not cand_first),
                    })

        line = "  ".join(f"{r}={cells[r]['score']:4.1f}" for r in here if r in cells)
        print(
            f"[{step.name} {index:2d}/{len(step.raw['cases'])}] {case_id:42s} {line}"
            + (f"   [{budget.line()}]" if budget else ""),
            flush=True,
        )

    summary = {}
    for route in step.routes:
        cells = [row["routes"][route] for row in rows if route in row["routes"]]
        if cells:
            summary[route] = summarise_route(route, route_meta(cfg, route), cells)

    pair_results = {}
    verdicts = {}
    paired: dict[str, Any] = {}
    for cand in step.candidates:
        if cand not in summary or step.incumbent not in summary:
            continue
        pw = pairwise_aggregate(comparisons[cand]) if step.mode == "pairwise" else None
        if pw is not None:
            pair_results[cand] = pw
        both = paired_summary(rows, cfg["routes"], cand, step.incumbent)
        cand_row, inc_row = both if both else (summary[cand], summary[step.incumbent])
        paired[cand] = {
            "cases": cand_row["cases"],
            "candidate_mean": cand_row["mean"],
            "incumbent_mean": inc_row["mean"],
        }
        verdicts[cand] = verdict(cand_row, inc_row, cfg.get("rule") or {}, pw)

    return {
        "step": step.name,
        "question": step.raw.get("question", ""),
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": step.mode,
        "samples": step.samples,
        "incumbent": step.incumbent,
        "candidates": step.candidates,
        "cases_run": [r["case_id"] for r in rows],
        "cases_planned": list(step.raw["cases"]),
        "rows": rows,
        "summary": summary,
        "wins": wins_per_case(rows, step.routes),
        "paired": paired,
        "pairwise": pair_results,
        "pairwise_detail": {k: v for k, v in comparisons.items() if v},
        "verdicts": verdicts,
        "budget": budget.line() if budget else "",
    }


def _key(model: str, messages: list[dict[str, Any]], salt: str) -> str:
    from common import DiskCache

    body = {"model": model, "messages": messages, "max_tokens": CANDIDATE_MAX_TOKENS}
    return DiskCache.key({**body, "_sample": salt} if salt else body)


def _side(letter: str | None, candidate_is_a: bool) -> str | None:
    if letter is None:
        return None
    if letter == "tie":
        return "tie"
    if letter == "A":
        return "candidate" if candidate_is_a else "incumbent"
    return "incumbent" if candidate_is_a else "candidate"


def _cell(samples: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [s["score"] for s in samples]
    finish = [s["finish_reason"] for s in samples]
    return {
        "score": round(statistics.fmean(scores), 2) if scores else 0.0,
        "scores": [round(s, 2) for s in scores],
        "score_sd": round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0,
        "samples": len(samples),
        "graded_by": samples[0]["graded_by"] if samples else "",
        "note": next((s["note"] for s in samples if s["note"]), ""),
        "latency_ms": round(median(s["latency_ms"] for s in samples)) if samples else 0,
        "output_tokens": round(median(s["output_tokens"] for s in samples)) if samples else 0,
        "finish_reasons": finish,
        "truncated": sum(1 for f in finish if f == "length"),
        "empty": sum(1 for s in samples if s["error"] == "empty reply"),
        "live": sum(1 for s in samples if not s["cached"]),
    }


# --- image cases --------------------------------------------------------


def build_images(cfg: dict[str, Any], wanted: list[str], out_dir: Path) -> dict[str, Any]:
    """Generate the PNGs this step needs and turn each one into messages.

    Only called when a step names an image case, because the generator needs
    Pillow and the router does not depend on it. Run those steps with
    `uv run --with pillow`.
    """
    specs = {k: v for k, v in (cfg.get("image_cases") or {}).items() if k in wanted}
    if not specs:
        return {}
    import make_images

    built = make_images.build_all(out_dir)
    out = {}
    for case_id, spec in specs.items():
        info = built[spec["image"]]
        out[case_id] = {
            "spec": spec,
            "truth": info["truth"],
            "messages": image_messages(spec, Path(info["path"])),
        }
    return out


# --- report -------------------------------------------------------------


PREAMBLE = """\
Generated by `evals/admission.py` from `evals/admission.yaml`. Rerun any step
with `uv run python evals/admission.py --step <name>`, and rebuild this file
with `--report`. Everything is cached, so a rerun costs nothing.

The question each step asks, and the rule each verdict applies, come from
POOL.md. A candidate takes a general lane when its mean score is within 0.5
points of the incumbent's and it is either 30% faster or on a different
subscription. A specialist lane needs the score floor and a win in blind
pairwise judging as well.

## How the grading works

A programmatic check decides the score wherever a case has one. The judge runs
only on cases with no check. That is a change from `run_outcomes.py`, which
runs both so they can be compared, and it was made for two reasons: the last
outcome run measured the judge 1.5 points low with a 32.5% disagreement rate
against the checks, and a judge call spends the same upstream budget as the
answer it is grading. Specs live in `evals/outcome_specs.yaml`.

Several specs were written or tightened for this run. Three of them
(`writing-investor-update-numbers-44`, `summarise-trial-abstract-66`,
`debug-adversarial-injection-in-log-16`) previously gave every route 10.0,
which says nothing about the routes. The new checks ask for arithmetic on the
input rather than words lifted out of it.

Eight check patterns were corrected after reading replies that they had marked
wrong: they were grader bugs, not model errors. They are listed at the end.
Every score in this file is from the corrected specs, regraded from the cached
replies, so no model was scored under one spec and compared under another.

## The 2026-09-19 run

263 live upstream calls, including judge calls, against a budget of 300.
Concurrency 2, two samples per case per route, an 8,000 token cap. No reply was
truncated and no reply came back empty, on any route, in any step.

Latency here is the whole call, request to last byte, because nothing is
streamed. It is not time to first token, and it rises with how much a model
writes: the median output column sits beside it for that reason. Two models
that answer equally well but at different lengths will differ on this column
without differing in speed.

The proxy degraded during the run, as it is known to. Median latency per call
went from about 6 seconds in the first three steps to 25 seconds by the
frontier step, and two calls hung long enough to block the step behind them.
`design-auth-module-review-23` was dropped from the frontier step for that
reason, leaving 9 cases there rather than 10; the reason and the case are in
`admission.yaml`. Nothing else was cut. `evals/admission.py` now takes a
`--timeout`, defaulting to 300 seconds rather than the 900 that
`run_outcomes.py` uses, so one hung call cannot stall a whole step again.

## What this can and cannot show

Sample sizes are 5 to 12 cases per step and 2 samples per case per route. That
is small on purpose: the proxy degrades after roughly 300 calls in a session
and the whole test has to fit inside one.

What it can show: a model that is broken on a kind of work, a model that is
plainly faster, a difference of a point or more in mean score, a failure mode
that repeats (wrong language, a refusal, an empty reply, truncation).

What it cannot show: a difference of a few tenths of a point. With 12 cases
and a within-route spread near 0.3, the interval on a mean is roughly plus or
minus 0.5, so two routes inside half a point of each other are tied as far as
this test is concerned. That is exactly why POOL.md's rule is "within 0.5 and
measurably faster" rather than "scores higher": the speed difference is large
and easy to measure, and the quality difference is small and is not.

It also cannot show anything about traffic that is not in `cases.yaml`. These
cases were written, not sampled.
"""


def _failures(step: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Cells worth reading: a low score, an empty reply, or truncation."""
    out = []
    for row in step["rows"]:
        for route, cell in row["routes"].items():
            problems = []
            if cell["empty"]:
                problems.append(f"{cell['empty']} empty reply")
            if cell["truncated"]:
                problems.append(f"{cell['truncated']} truncated at the token cap")
            if cell["score"] < 7.0:
                problems.append(f"scored {cell['score']:.1f}")
            if problems:
                out.append((
                    row["case_id"], route,
                    "; ".join(problems) + (f" — {cell['note'][:180]}" if cell["note"] else ""),
                ))
    return out


def build_report(payload: dict[str, Any], cfg: dict[str, Any]) -> str:
    out: list[str] = []
    w = out.append
    w("# Admission test")
    w("")
    w(PREAMBLE)
    w("")
    w("## Routes under test")
    w("")
    w("| route | upstream model string | provider | reads images |")
    w("|---|---|---|---|")
    for name, meta in cfg["routes"].items():
        w(f"| `{name}` | `{meta['upstream']}` | {meta['provider']} | {meta['vision']} |")
    w("")
    w("`glm-5.3` and `kimi-k3` are never sent without an effort suffix. At "
      "their default effort both think until the token budget is gone and can "
      "return empty content. `gemma4-31b` has no reasoning mode and takes no "
      "suffix. `grok-4.6` without `(low)` takes about 11 seconds to first "
      "token.")
    w("")
    for name, step in payload.get("steps", {}).items():
        w(f"## {name} — {step.get('question', '')}")
        w("")
        w(f"- run: {step['when']}")
        w(f"- cases: {len(step['cases_run'])} of {len(step['cases_planned'])} planned, "
          f"{step['samples']} samples per case per route")
        w(f"- incumbent: `{step['incumbent']}`")
        w("")
        w("| route | mean | 95% CI | spread | worst case | adequate (>=7) | top of case | "
          "median latency s | median out tokens | truncated % | empty |")
        w("|---|---|---|---|---|---|---|---|---|---|---|")
        for route, row in step["summary"].items():
            w(
                f"| `{route}` | {row['mean']:.2f} | [{row['ci'][0]:.2f}, {row['ci'][1]:.2f}] | "
                f"{row['spread']:.2f} | {row['min_case']:.1f} | {row['adequate']}/{row['cases']} | "
                f"{step['wins'].get(route, 0)}/{row['cases']} | {row['latency_ms'] / 1000:.1f} | "
                f"{row['output_tokens']} | {row['truncation_rate']:.1f} | {row['empty']} |"
            )
        w("")
        if step.get("pairwise"):
            w("Blind pairwise, each pair judged twice with A and B swapped. A win "
              "counts only when both orders agree.")
            w("")
            w("| candidate vs incumbent | pairs | wins | losses | ties | undecided | net rate | "
              "judge picked whichever came first |")
            w("|---|---|---|---|---|---|---|---|")
            for cand, pw in step["pairwise"].items():
                w(
                    f"| `{cand}` vs `{step['incumbent']}` | {pw['pairs']} | {pw['wins']} | "
                    f"{pw['losses']} | {pw['ties']} | {pw['undecided']} | {pw['net_rate']:+.2f} | "
                    f"{pw['order_bias']:.0%} |"
                )
            w("")
        if step["verdicts"]:
            w("Verdicts are paired: each candidate is compared with the incumbent "
              "over only the cases where both of them ran.")
            w("")
            w("| candidate | verdict | paired cases | candidate | incumbent | score gap | "
              "30% faster | speed-up | other provider | why not |")
            w("|---|---|---|---|---|---|---|---|---|---|")
        for cand, v in step["verdicts"].items():
            pr = (step.get("paired") or {}).get(cand, {})
            w(
                f"| `{cand}` | {'PASS' if v['pass'] else 'fail'} | {pr.get('cases', '-')} | "
                f"{pr.get('candidate_mean', '-')} | {pr.get('incumbent_mean', '-')} | "
                f"{v['score_deficit']:+.2f} | "
                f"{'yes' if v['faster_30pct'] else 'no'} | {v['speedup']:.2f}x | "
                f"{'yes' if v['different_provider'] else 'no'} | {v['why_not'] or '-'} |"
            )
        w("")
        thin = {
            cand: pr["cases"]
            for cand, pr in (step.get("paired") or {}).items()
            if pr["cases"] < len(step["cases_run"])
        }
        if thin:
            w("**Read these verdicts with care.** The two routes did not both "
              "run every case here, either because the incumbent joined from "
              "the cache or because one of them cannot read images, so the "
              "paired comparison rests on "
              + ", ".join(f"{n} of {len(step['cases_run'])} cases for `{c}`"
                          for c, n in thin.items())
              + ". At that size the verdict is a direction, not a result. The "
                "per-route means over all the cases each route did run, in the "
                "table above, are the numbers to read.")
            w("")
        w("Per case, mean of the samples:")
        w("")
        routes = [r for r in step["summary"]]
        w("| case | " + " | ".join(f"`{r}`" for r in routes) + " |")
        w("|---" * (len(routes) + 1) + "|")
        for row in step["rows"]:
            cells = [
                f"{row['routes'][r]['score']:.1f}" if r in row["routes"] else "-"
                for r in routes
            ]
            w(f"| {row['case_id']} | " + " | ".join(cells) + " |")
        w("")
        fails = _failures(step)
        w("Worth reading:")
        w("")
        if not fails:
            w("- Nothing. No empty reply, no truncation, and no route scored "
              "below 7 on any case in this step.")
        else:
            for case_id, route, why in fails:
                w(f"- `{route}` on `{case_id}`: {why}")
        w("")
    w("## Grading-spec corrections made during the run")
    w("")
    w("Each of these marked a correct answer wrong. They were fixed and "
      "everything was regraded from cache, at no upstream cost.")
    w("")
    for line in SPEC_FIXES:
        w(f"- {line}")
    w("")
    return "\n".join(out) + "\n"


SPEC_FIXES = [
    "`code-sql-window-query-05`: the tiebreaker check demanded a tiebreaking "
    "column. Explaining why identical timestamps are already safe (a zero gap "
    "is under 30 minutes) is an equally correct answer and now counts.",
    "`adv-five-second-cron-tz-96`: the DST check wanted the word DST. "
    '"07:00 UTC in summer and 08:00 in winter" says the same thing and now '
    "counts.",
    "`debug-css-overflow-17`: the cause check wanted `min-width: auto`. "
    '"automatic minimum width" now counts.',
    "`design-index-strategy-26`: the composite-index check required the "
    "columns on one line and required `status` among them. A multi-line "
    "`CREATE INDEX`, and the partial form that moves `status` into the "
    "`WHERE`, now count.",
    "`adv-static-markup-rounding-99`: the injection check penalised any "
    "mention of the planted comment, which punished the correct behaviour of "
    "quoting it in order to flag it. It now penalises only obeying it, and a "
    "separate check rewards naming it.",
    "`polish-slack-grammar-54`: the check penalised the original typos "
    "appearing anywhere, which punished answers that list what they changed.",
    "`quick-timezone-abbrev-77`: the pattern could not match across markdown "
    'emphasis, so "does **not** observe" read as a wrong answer.',
    "`debug-nameerror-typo-14`: pointing at the call site by showing it "
    "corrected now counts, and the weight moved from that to brevity.",
]


# --- main ---------------------------------------------------------------


def load_results() -> dict[str, Any]:
    if RESULTS_FILE.exists():
        try:
            return json.loads(RESULTS_FILE.read_text())
        except ValueError:
            pass
    return {"steps": {}}


async def main_async(args: argparse.Namespace) -> int:
    cfg = yaml.safe_load(SPEC_FILE.read_text())
    payload = load_results()

    if args.report:
        REPORT_FILE.write_text(build_report(payload, cfg))
        print(f"wrote {REPORT_FILE}")
        return 0

    if args.label_mid:
        step_result = payload["steps"].get(args.label_mid)
        if not step_result:
            print(f"no results for step {args.label_mid!r}", file=sys.stderr)
            return 2
        proposals = mid_label_proposals(step_result["rows"], args.mid_route)
        for p in proposals:
            print(
                f"{p['case_id']:42s} mid={p['mid_score']:5.2f} best={p['best_score']:5.2f} "
                f"worst sample={p['worst_sample']:5.2f}  "
                f"{'add mid' if p['add_mid'] else 'no'}"
            )
        if args.apply:
            changed = apply_mid_labels(proposals, EVALS_DIR / "cases.yaml")
            print(f"\nadded `mid` to {len(changed)} cases: {changed}")
        else:
            print("\nrerun with --apply to write these into cases.yaml")
        return 0

    if args.step not in cfg["steps"]:
        print(f"unknown step {args.step!r}; known: {', '.join(cfg['steps'])}", file=sys.stderr)
        return 2

    specs = (yaml.safe_load((EVALS_DIR / "outcome_specs.yaml").read_text()) or {})["cases"]
    cases = {c.id: c for c in load_cases()}
    step = Step(args.step, cfg, cfg["steps"][args.step])
    if args.samples:
        step.samples = args.samples

    image_dir = Path(args.image_dir or os.environ.get("TMPDIR", "/tmp")) / "jev-admission-images"
    images = build_images(cfg, list(step.raw["cases"]), image_dir)

    missing = [
        c for c in step.raw["cases"]
        if c not in images and (c not in cases or c not in specs)
    ]
    if missing:
        print(f"cases with no case entry or no grading spec: {missing}", file=sys.stderr)
        return 2

    budget = Budget(max_calls=args.max_calls) if args.max_calls else None
    upstream = UpstreamClient(
        concurrency=args.concurrency, cache=True, budget=budget,
        timeout_s=args.timeout,
    )
    try:
        result = await run_step(step, cfg, upstream, cases, specs, images, budget)
    finally:
        await upstream.aclose()

    payload.setdefault("steps", {})[step.name] = result
    payload["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RESULTS_FILE.write_text(json.dumps(payload, indent=2))
    REPORT_FILE.write_text(build_report(payload, cfg))

    print("")
    for cand, v in result["verdicts"].items():
        print(f"{cand}: {'PASS' if v['pass'] else 'fail'}  {v['why_not'] or ''}")
    print(f"live upstream calls this run: {upstream.calls}")
    if budget is not None:
        print(budget.line())
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--step", help="a step name from evals/admission.yaml")
    p.add_argument("--report", action="store_true", help="rebuild ADMISSION.md and stop")
    p.add_argument("--samples", type=int, default=0, help="override samples per route")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--max-calls", type=int, default=0,
                   help="stop after this many live upstream calls, 0 for no limit")
    p.add_argument("--image-dir", help="where to write the generated PNGs")
    p.add_argument("--label-mid", metavar="STEP",
                   help="propose `mid` in acceptable_tiers from a finished step")
    p.add_argument("--mid-route", default="glm-5.3",
                   help="the route the mid lane would use")
    p.add_argument("--apply", action="store_true", help="write the label changes")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="seconds to wait for one upstream reply. The default "
                        "is lower than run_outcomes.py's because a single "
                        "hung call on a degrading proxy otherwise blocks the "
                        "whole step behind it")
    args = p.parse_args()
    if not args.report and not args.step and not args.label_mid:
        p.error("give --step, --label-mid or --report")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
