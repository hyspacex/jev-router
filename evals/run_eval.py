#!/usr/bin/env python
"""Layers 1 and 2 of TESTPLAN.md.

For every case in cases.yaml, build features and state with the router's own
code, ask the real Jev API, then run the real policy. Report how often the
classification and the route are right, per state builder.

    uv run python evals/run_eval.py --group shipped
    uv run python evals/run_eval.py --variants summary_v1,continuation_aware_v1
    uv run python evals/run_eval.py --repeats 3 --no-cache

Results go to evals/results/<timestamp>/{summary.md,per_case.jsonl}.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from common import (  # noqa: E402
    CASE_TASKS,
    EVALS_DIR,
    TEN_CAT_TO_SEVEN,
    Case,
    JevClient,
    config_with_overlay,
    effort_rank,
    estimate_tokens,
    load_cases,
    median,
    pct,
    percentile,
    route_cost,
    tier_of,
)
from jev_router.deciders.rules import RulesDecider  # noqa: E402
from jev_router.policy import evaluate  # noqa: E402
from jev_router.state import build_state  # noqa: E402

import exp_builders  # noqa: E402,F401 - registers the experimental builders

CONF_BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 1.01)]


# --- variants -----------------------------------------------------------


@dataclass
class Variant:
    name: str
    state_builder: str = "summary_v1"
    questions: list[str] = field(default_factory=lambda: ["task", "difficulty", "harm_if_wrong"])
    task_space: str = "seven"
    faith_question: str = "harm_if_wrong"
    faith_threshold: float = 0.6
    difficulty_levels: int = 4
    alias: str = "auto"
    group: str = ""
    overlay: dict[str, Any] = field(default_factory=dict)

    def config(self):
        from common import deep_merge

        overlay = deep_merge(
            {
                "aliases": {
                    self.alias: {
                        "state_builder": self.state_builder,
                        "questions": list(self.questions),
                    }
                }
            },
            self.overlay,
        )
        from common import questions_to_replace

        return config_with_overlay(
            overlay, replace_questions=questions_to_replace(overlay)
        )


def load_variants(path: Path | None = None) -> tuple[dict[str, Variant], dict[str, list[str]]]:
    path = path or EVALS_DIR / "variants.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    out: dict[str, Variant] = {}
    for name, spec in (raw.get("variants") or {}).items():
        out[name] = Variant(name=name, **(spec or {}))
    return out, raw.get("groups") or {}


# --- one measured case --------------------------------------------------


@dataclass
class Record:
    variant: str
    case: Case
    state: Any
    answers: dict[str, Any]
    repeats: list[dict[str, Any]]
    latencies: list[float]
    input_tokens: int
    model: str
    effort: str | None
    rule: str
    reason: str
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        c = self.case
        return {
            "variant": self.variant,
            "case_id": c.id,
            "split": c.split,
            "label": {
                "task": c.task,
                "task7": c.task7,
                "difficulty": c.difficulty,
                "difficulty_tolerance": c.tolerance,
                "needs_faithfulness": c.needs_faithfulness,
                "tier": c.tier,
                "effort": c.effort,
                "acceptable_tiers": c.acceptable_tiers,
            },
            "meta": {
                "shape": c.shape,
                "language": c.language,
                "multi_turn": c.multi_turn,
                "adversarial": c.adversarial,
                "trap": c.trap,
            },
            "state": self.state,
            "state_tokens": estimate_tokens(self.state),
            "jev_input_tokens": self.input_tokens,
            "jev_ms": self.latencies,
            "answers": self.answers,
            "repeat_answers": self.repeats,
            "route": {
                "model": self.model,
                "effort": self.effort,
                "rule": self.rule,
                "reason": self.reason,
                "tier": tier_of(self.model),
                "cost": route_cost(self.model, self.effort),
            },
            "error": self.error,
        }


def answer_value(answers: dict[str, Any], qid: str) -> Any:
    a = answers.get(qid) or {}
    return a.get("choice", a.get("score", a.get("noul")))


def confidence(answers: dict[str, Any], qid: str) -> float | None:
    a = answers.get(qid) or {}
    return a.get("confidence")


async def run_variant(
    variant: Variant, cases: list[Case], jev: JevClient, repeats: int, cache: bool
) -> list[Record]:
    config = variant.config()
    alias_cfg = config.aliases[variant.alias]
    questions = {qid: config.question(qid) for qid in alias_cfg.questions}

    async def one(case: Case) -> Record:
        features = case.features()
        state = build_state(alias_cfg.state_builder, features, config)
        results = []
        error = None
        for i in range(repeats):
            try:
                results.append(await jev.ask(state, questions, use_cache=(cache and i == 0)))
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                error = f"{type(exc).__name__}: {exc}"
                break
        if not results:
            return Record(
                variant.name, case, state, {}, [], [], 0, "gpt-6-astra", "medium",
                "error", error or "no answer", error=error,
            )
        primary = results[0]
        policy = evaluate(config, alias_cfg, primary.answers, features)
        return Record(
            variant=variant.name,
            case=case,
            state=state,
            answers=primary.answers,
            repeats=[r.answers for r in results],
            latencies=[r.latency_ms for r in results if not r.cached],
            input_tokens=primary.input_tokens,
            model=policy.model,
            effort=policy.effort,
            rule=policy.rule,
            reason=policy.reason,
            error=error,
        )

    return list(await asyncio.gather(*(one(c) for c in cases)))


# --- baselines ----------------------------------------------------------


async def baseline_records(name: str, cases: list[Case]) -> list[Record]:
    config = config_with_overlay()
    alias_cfg = config.aliases["auto"]
    out: list[Record] = []
    decider = RulesDecider(config)
    for case in cases:
        features = case.features()
        if name == "always_astra_medium":
            model, effort, rule = "gpt-6-astra", "medium", "baseline"
            answers: dict[str, Any] = {}
        elif name == "always_glm":
            model, effort, rule = "ollama/glm-5.3-flash", "none", "baseline"
            answers = {}
        elif name == "rules":
            d = await decider.decide(features, alias_cfg)
            model, effort, rule, answers = d.model, d.effort, d.rule, d.answers
        else:
            raise ValueError(name)
        out.append(
            Record(
                variant=f"baseline:{name}",
                case=case,
                state=None,
                answers=answers,
                repeats=[answers],
                latencies=[],
                input_tokens=0,
                model=model,
                effort=effort,
                rule=rule,
                reason="baseline",
            )
        )
    return out


# --- scoring ------------------------------------------------------------


def score_task(rec: Record, task_space: str = "") -> bool | None:
    """Compare in whichever space the answer came back in.

    A variant's question may offer cases.yaml's ten categories or the seven the
    router shipped with, and the baselines answer in whatever router.yaml
    currently defines. Reading the space off the answer keeps every row
    comparable without the caller having to know.
    """
    got = answer_value(rec.answers, "task")
    if got is None:
        return None
    if got in CASE_TASKS:
        return got == rec.case.task
    return got == rec.case.task7


def score_task7(rec: Record, task_space: str) -> bool | None:
    """Both spaces collapsed to the seven router categories, so the two
    question wordings can be compared on the same scale."""
    got = answer_value(rec.answers, "task")
    if got is None:
        return None
    if task_space == "ten":
        got = TEN_CAT_TO_SEVEN.get(got, got)
    return got == rec.case.task7


def score_difficulty(rec: Record, levels: int = 4) -> bool | None:
    """Labels run 0..3. A question with a different number of levels is
    rescaled onto that range before it is compared."""
    got = answer_value(rec.answers, "difficulty")
    if got is None:
        return None
    if levels != 4:
        got = got * 3.0 / (levels - 1)
    return abs(round(got) - rec.case.difficulty) <= rec.case.tolerance


def score_faith(rec: Record, variant: Variant) -> bool | None:
    got = answer_value(rec.answers, variant.faith_question)
    if got is None:
        return None
    return bool(got >= variant.faith_threshold) == rec.case.needs_faithfulness


def stable(rec: Record) -> bool | None:
    if len(rec.repeats) < 2:
        return None
    tasks = {answer_value(a, "task") for a in rec.repeats}
    diffs = [answer_value(a, "difficulty") for a in rec.repeats]
    diffs = [d for d in diffs if d is not None]
    same_task = len(tasks) <= 1
    same_diff = (max(diffs) - min(diffs) <= 0.25) if diffs else True
    return same_task and same_diff


def metrics(records: list[Record], variant: Variant, split: str | None = None) -> dict[str, Any]:
    recs = [r for r in records if split is None or r.case.split == split]
    n = len(recs)
    if not n:
        return {}

    def rate(fn) -> float:
        vals = [fn(r) for r in recs]
        vals = [v for v in vals if v is not None]
        return pct(sum(1 for v in vals if v), len(vals))

    has_answers = any(r.answers for r in recs)
    out: dict[str, Any] = {"n": n}

    if has_answers:
        out["task_acc"] = rate(lambda r: score_task(r, variant.task_space))
        out["task_acc_7cat"] = rate(lambda r: score_task7(r, variant.task_space))
        out["difficulty_ok"] = rate(lambda r: score_difficulty(r, variant.difficulty_levels))
        out["faith_acc"] = rate(lambda r: score_faith(r, variant))
        best_th, best_acc = variant.faith_threshold, out["faith_acc"]
        for th in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            acc = pct(
                sum(
                    1
                    for r in recs
                    if (v := answer_value(r.answers, variant.faith_question)) is not None
                    and (v >= th) == r.case.needs_faithfulness
                ),
                len(recs),
            )
            if acc > best_acc:
                best_th, best_acc = th, acc
        out["faith_best_threshold"] = best_th
        out["faith_acc_best"] = best_acc
        stab = [stable(r) for r in recs]
        stab = [s for s in stab if s is not None]
        out["stability"] = pct(sum(1 for s in stab if s), len(stab)) if stab else None
        lat = [ms for r in recs for ms in r.latencies]
        out["jev_p50_ms"] = percentile(lat, 0.5) if lat else None
        out["jev_p95_ms"] = percentile(lat, 0.95) if lat else None
        out["state_tokens_median"] = median([estimate_tokens(r.state) for r in recs])
        out["jev_tokens_median"] = median([r.input_tokens for r in recs if r.input_tokens])

    # routing
    tiers = [tier_of(r.model) for r in recs]
    out["tier_correct"] = pct(
        sum(1 for r, t in zip(recs, tiers) if t in r.case.acceptable_tiers), n
    )
    out["under_routed"] = pct(
        sum(1 for r, t in zip(recs, tiers) if t == "fast" and "fast" not in r.case.acceptable_tiers),
        n,
    )
    out["over_routed"] = pct(
        sum(
            1
            for r, t in zip(recs, tiers)
            if t == "frontier" and "frontier" not in r.case.acceptable_tiers
        ),
        n,
    )
    out["effort_within_1_all"] = pct(
        sum(1 for r in recs if abs(effort_rank(r.effort) - effort_rank(r.case.effort)) <= 1), n
    )
    frontier = [r for r in recs if r.case.tier == "frontier"]
    out["effort_within_1_frontier"] = pct(
        sum(1 for r in frontier if abs(effort_rank(r.effort) - effort_rank(r.case.effort)) <= 1),
        len(frontier),
    )
    out["mean_cost"] = statistics.fmean(route_cost(r.model, r.effort) for r in recs)

    # traps
    traps = [r for r in recs if r.case.adversarial]
    out["adversarial_n"] = len(traps)
    out["adversarial_held"] = sum(
        1 for r in traps if tier_of(r.model) in r.case.acceptable_tiers
    )
    all_traps = [r for r in recs if r.case.trap]
    out["trap_n"] = len(all_traps)
    out["trap_tier_correct"] = pct(
        sum(1 for r in all_traps if tier_of(r.model) in r.case.acceptable_tiers), len(all_traps)
    )
    return out


def slice_table(records: list[Record], variant: Variant, key) -> dict[str, dict[str, float]]:
    groups: dict[str, list[Record]] = collections.defaultdict(list)
    for r in records:
        groups[str(key(r.case))].append(r)
    out = {}
    for name in sorted(groups):
        recs = groups[name]
        out[name] = {
            "n": len(recs),
            "task_acc": pct(
                sum(1 for r in recs if score_task(r, variant.task_space)), len(recs)
            ),
            "difficulty_ok": pct(
                sum(1 for r in recs if score_difficulty(r, variant.difficulty_levels)), len(recs)
            ),
            "tier_correct": pct(
                sum(1 for r in recs if tier_of(r.model) in r.case.acceptable_tiers), len(recs)
            ),
        }
    return out


def confusion(records: list[Record], variant: Variant) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = collections.defaultdict(lambda: collections.defaultdict(int))
    for r in records:
        got = answer_value(r.answers, "task")
        want = r.case.task if variant.task_space == "ten" else r.case.task7
        table[str(want)][str(got)] += 1
    return {k: dict(v) for k, v in table.items()}


def calibration(records: list[Record], variant: Variant, qid: str) -> list[dict[str, Any]]:
    scorer = (
        (lambda r: score_task(r, variant.task_space))
        if qid == "task"
        else (lambda r: score_difficulty(r, variant.difficulty_levels))
    )
    rows = []
    for lo, hi in CONF_BUCKETS:
        bucket = [
            r
            for r in records
            if (c := confidence(r.answers, qid)) is not None and lo <= c < hi
        ]
        if not bucket:
            continue
        correct = [scorer(r) for r in bucket]
        rows.append(
            {
                "bucket": f"{lo:.2f}-{hi:.2f}",
                "n": len(bucket),
                "mean_confidence": statistics.fmean(
                    confidence(r.answers, qid) for r in bucket
                ),
                "accuracy": pct(sum(1 for c in correct if c), len(correct)),
            }
        )
    return rows


# --- report -------------------------------------------------------------

HEADLINE = [
    ("task_acc", "task acc %", 85.0),
    ("difficulty_ok", "difficulty %", 80.0),
    ("faith_acc", "faith %", 85.0),
    ("stability", "stable %", 95.0),
    ("tier_correct", "tier ok %", 90.0),
    ("under_routed", "under %", None),
    ("over_routed", "over %", None),
    ("effort_within_1_frontier", "effort±1 %", 80.0),
    ("mean_cost", "cost", None),
]


def fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def write_summary(
    out_dir: Path,
    variants: dict[str, Variant],
    results: dict[str, list[Record]],
    repeats: int,
    jev_calls: int,
    jev_model: str,
) -> str:
    lines: list[str] = []
    w = lines.append
    w("# Eval run")
    w("")
    w(f"- when: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    w(f"- jev model: {jev_model}")
    w(f"- repeats per case: {repeats}")
    w(f"- live Jev calls this run: {jev_calls}")
    w(f"- cases: {len(next(iter(results.values())))} "
      f"(tune {sum(1 for r in next(iter(results.values())) if r.case.split == 'tune')}, "
      f"held-out {sum(1 for r in next(iter(results.values())) if r.case.split == 'held_out')})")
    w("")

    for split_name, split in (("all cases", None), ("tuning split", "tune"), ("held-out split", "held_out")):
        w(f"## {split_name}")
        w("")
        head = "| variant | n | " + " | ".join(h[1] for h in HEADLINE) + " |"
        w(head)
        w("|" + "---|" * (len(HEADLINE) + 2))
        for name, recs in results.items():
            v = variants.get(name) or Variant(name=name)
            m = metrics(recs, v, split)
            if not m:
                continue
            row = [name, str(m["n"])] + [fmt(m.get(k)) for k, _, _ in HEADLINE]
            w("| " + " | ".join(row) + " |")
        w("")

    w("## Targets from TESTPLAN.md")
    w("")
    w("| metric | target | best variant | value | met |")
    w("|---|---|---|---|---|")
    for key, label, target in HEADLINE:
        if target is None:
            continue
        best_name, best_val = None, None
        for name, recs in results.items():
            if name.startswith("baseline:"):
                continue
            v = variants.get(name) or Variant(name=name)
            val = metrics(recs, v).get(key)
            if val is None:
                continue
            if best_val is None or val > best_val:
                best_name, best_val = name, val
        if best_val is None:
            continue
        w(f"| {label} | {target:.0f} | {best_name} | {best_val:.1f} | "
          f"{'yes' if best_val >= target else 'NO'} |")
    w("")

    for name, recs in results.items():
        if name.startswith("baseline:"):
            continue
        v = variants.get(name) or Variant(name=name)
        w(f"## {name}")
        w("")
        w(f"- state builder: `{v.state_builder}`, questions: {', '.join(v.questions)}, "
          f"task space: {v.task_space}")
        m = metrics(recs, v)
        w(f"- median state tokens: {fmt(m.get('state_tokens_median'))} "
          f"(Jev counted {fmt(m.get('jev_tokens_median'))})")
        w(f"- Jev latency p50 {fmt(m.get('jev_p50_ms'))} ms, p95 {fmt(m.get('jev_p95_ms'))} ms")
        w(f"- injection traps held: {m.get('adversarial_held')}/{m.get('adversarial_n')}; "
          f"all trap cases tier-correct: {fmt(m.get('trap_tier_correct'))}%")
        w("")

        for title, key in (
            ("by client shape", lambda c: c.shape),
            ("by language", lambda c: c.language),
            ("multi-turn", lambda c: "multi-turn" if c.multi_turn else "single-turn"),
        ):
            w(f"### {name}: {title}")
            w("")
            w("| group | n | task acc % | difficulty % | tier ok % |")
            w("|---|---|---|---|---|")
            for gname, row in slice_table(recs, v, key).items():
                w(f"| {gname} | {row['n']} | {row['task_acc']:.1f} | "
                  f"{row['difficulty_ok']:.1f} | {row['tier_correct']:.1f} |")
            w("")

        w(f"### {name}: task confusion (rows = label, columns = Jev)")
        w("")
        table = confusion(recs, v)
        cols = sorted({c for row in table.values() for c in row})
        w("| label | " + " | ".join(cols) + " |")
        w("|" + "---|" * (len(cols) + 1))
        for label in sorted(table):
            w(f"| {label} | " + " | ".join(str(table[label].get(c, "")) for c in cols) + " |")
        w("")

        for qid in ("task", "difficulty"):
            rows = calibration(recs, v, qid)
            if not rows:
                continue
            w(f"### {name}: {qid} confidence calibration")
            w("")
            w("| confidence bucket | n | mean confidence | accuracy % |")
            w("|---|---|---|---|")
            for row in rows:
                w(f"| {row['bucket']} | {row['n']} | {row['mean_confidence']:.2f} | "
                  f"{row['accuracy']:.1f} |")
            w("")

        wrong = [
            r
            for r in recs
            if tier_of(r.model) not in r.case.acceptable_tiers
            or not score_task(r, v.task_space)
        ]
        if wrong:
            w(f"### {name}: cases that failed")
            w("")
            w("| case | split | label task/diff/tier | Jev task/diff/harm | route | why |")
            w("|---|---|---|---|---|---|")
            for r in sorted(wrong, key=lambda r: r.case.id):
                c = r.case
                got_t = answer_value(r.answers, "task")
                got_d = answer_value(r.answers, "difficulty")
                got_h = answer_value(r.answers, v.faith_question)
                why = []
                if tier_of(r.model) not in c.acceptable_tiers:
                    why.append("under-routed" if tier_of(r.model) == "fast" else "over-routed")
                if not score_task(r, v.task_space):
                    why.append("task")
                if not score_difficulty(r, v.difficulty_levels):
                    why.append("difficulty")
                w(
                    f"| {c.id} | {c.split} | {c.task}/{c.difficulty}±{c.tolerance}/{c.tier} | "
                    f"{got_t}/{got_d if got_d is None else round(got_d, 2)}/"
                    f"{got_h if got_h is None else round(got_h, 2)} | "
                    f"{r.model.split('/')[-1]}({r.effort}) via {r.rule} | {', '.join(why)} |"
                )
            w("")

    text = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(text)
    return text


# --- main ---------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    all_variants, groups = load_variants()
    names: list[str] = []
    if args.variants:
        names = [n.strip() for n in args.variants.split(",") if n.strip()]
    elif args.group:
        for g in args.group.split(","):
            names.extend(groups.get(g.strip(), []))
    else:
        names = list(all_variants)
    missing = [n for n in names if n not in all_variants]
    if missing:
        print(f"unknown variants: {', '.join(missing)}", file=sys.stderr)
        print(f"known: {', '.join(all_variants)}", file=sys.stderr)
        return 2

    cases = load_cases()
    if args.limit:
        cases = cases[: args.limit]
    if args.case:
        wanted = {c.strip() for c in args.case.split(",")}
        cases = [c for c in cases if c.id in wanted]

    jev = JevClient(model=args.jev_model, concurrency=args.concurrency, cache=not args.no_cache)
    results: dict[str, list[Record]] = {}
    try:
        for name in names:
            started = time.time()
            variant = all_variants[name]
            recs = await run_variant(variant, cases, jev, args.repeats, not args.no_cache)
            results[name] = recs
            m = metrics(recs, variant)
            print(
                f"{name:26s} task {m.get('task_acc', 0):5.1f}%  diff {m.get('difficulty_ok', 0):5.1f}%  "
                f"tier {m.get('tier_correct', 0):5.1f}%  under {m.get('under_routed', 0):4.1f}%  "
                f"over {m.get('over_routed', 0):4.1f}%  cost {m.get('mean_cost', 0):5.1f}  "
                f"({time.time() - started:.0f}s)"
            )
    finally:
        await jev.aclose()

    if not args.no_baselines:
        for b in ("always_astra_medium", "always_glm", "rules"):
            results[f"baseline:{b}"] = await baseline_records(b, cases)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = EVALS_DIR / "results" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "per_case.jsonl").open("w") as fh:
        for recs in results.values():
            for r in recs:
                fh.write(json.dumps(r.to_json(), ensure_ascii=False, default=str) + "\n")
    write_summary(out_dir, all_variants, results, args.repeats, jev.calls, args.jev_model)

    latest = EVALS_DIR / "results" / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(stamp)

    print(f"\nlive Jev calls: {jev.calls}, input tokens: {jev.input_tokens}")
    print(f"wrote {out_dir}/summary.md")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variants", help="comma separated variant names")
    p.add_argument("--group", help="comma separated group names from variants.yaml")
    p.add_argument("--repeats", type=int, default=3, help="Jev calls per case (default 3)")
    p.add_argument("--no-cache", action="store_true", help="never read the Jev cache")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--jev-model", default="jev-1.13.0")
    p.add_argument("--limit", type=int, help="only the first N cases")
    p.add_argument("--case", help="comma separated case ids")
    p.add_argument("--no-baselines", action="store_true")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
