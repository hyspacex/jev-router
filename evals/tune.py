#!/usr/bin/env python
"""Layer 5 of TESTPLAN.md: search the policy's numbers.

Reads three sources and looks for the cheapest set of thresholds that keeps
under-routing at or below the limit on the tuning split:

  1. cached Jev answers from an evals/results/<run> directory
  2. evals/outcomes.json, where the outcome benchmark revised a label
  3. the `decisions` and `feedback` tables in the router's sqlite log

Feedback rows count three times, since they come from real work.

    uv run python evals/tune.py
    uv run python evals/tune.py --results evals/results/latest --variant winner
    uv run python evals/tune.py --db ~/.jev-router/router.db --samples 4000

It prints the metrics before and after and a unified diff for router.yaml. It
never writes to router.yaml.
"""

from __future__ import annotations

import argparse
import difflib
import json
import random
import re
import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    EVALS_DIR,
    ROOT,
    config_with_overlay,
    effort_rank,
    questions_to_replace,
    route_cost,
    tier_of,
)
from jev_router.config import AliasCfg, RouterConfig  # noqa: E402
from jev_router.features import Features  # noqa: E402
from jev_router.policy import evaluate  # noqa: E402

FEEDBACK_WEIGHT = 3.0
UNDER_ROUTE_LIMIT = 5.0

# What one under-route costs, in the same quota units as a route.
# A request that went to the fast model and came back unusable is redone on the
# frontier model at high effort (20 units), and the person's time is worth more
# than the quota, so it is counted three times. TESTPLAN.md puts under-routing
# ahead of over-routing, and this is the number that says by how much. Raise it
# to make the search more cautious; a search that only minimised quota would
# take every under-route that saved a few units.
UNDER_ROUTE_COST = 60.0

CODEY = ["code-edit", "debugging", "agentic-tool-task"]
OFF = 1.01  # a threshold a probability can never reach, so the rule never fires


# --- the tunable ladder -------------------------------------------------


@dataclass(frozen=True)
class Ladder:
    """Every number in the routing policy that a search may move."""

    conf_floor: float = 0.45        # low_confidence.min_confidence
    model_cutoff: float = 1.0       # difficulty at or above this -> frontier
    hard_min: float = 1.8           # difficulty at or above this -> high
    very_hard_min: float = 2.6      # difficulty at or above this -> xhigh
    harm_threshold: float = 0.6     # harm_if_wrong at or above this -> frontier
    faith_threshold: float = OFF    # needs_faithfulness at or above this -> frontier

    GRID = {
        "conf_floor": [0.0, 0.25, 0.35, 0.4, 0.45, 0.55],
        "model_cutoff": [1.0, 1.2, 1.4, 1.6, 1.8, 2.0],
        "hard_min": [1.6, 1.8, 2.0, 2.2, 2.4],
        "very_hard_min": [2.4, 2.6, 2.8, 3.0],
        "harm_threshold": [0.5, 0.6, 0.7, 0.8, 0.9, OFF],
        "faith_threshold": [0.5, 0.6, 0.7, 0.8, 0.9, OFF],
    }

    def sane(self) -> bool:
        return self.model_cutoff <= self.hard_min < self.very_hard_min

    def rules(self) -> list[dict[str, Any]]:
        """The ruleset this ladder means, in router.yaml's shape."""
        astra = "gpt-6-astra"
        glm = "ollama/glm-5.3-flash"
        out: list[dict[str, Any]] = [
            {
                "name": "images_need_vision",
                "when": {"has_images": True},
                "use": {"model": astra, "effort": "medium"},
            },
            {
                "name": "very_hard",
                "when": {"difficulty": {"gte": self.very_hard_min}},
                "use": {"model": astra, "effort": "xhigh"},
            },
            {
                "name": "hard",
                "when": {"difficulty": {"gte": self.hard_min}},
                "use": {"model": astra, "effort": "high"},
            },
        ]
        if self.faith_threshold <= 1.0:
            out.append(
                {
                    "name": "needs_care",
                    "when": {"needs_faithfulness": {"gte": self.faith_threshold}},
                    "use": {"model": astra, "effort": "medium"},
                }
            )
        if self.harm_threshold <= 1.0:
            out.append(
                {
                    "name": "high_harm",
                    "when": {"harm_if_wrong": {"gte": self.harm_threshold}},
                    "use": {"model": astra, "effort": "medium"},
                }
            )
        out += [
            {
                "name": "coding_middle",
                "when": {
                    "task": {"in": list(CODEY)},
                    "difficulty": {"gte": self.model_cutoff},
                },
                "use": {"model": astra, "effort": "medium"},
            },
            {
                "name": "tool_loop",
                "when": {"has_tools": True, "difficulty": {"gte": self.model_cutoff}},
                "use": {"model": astra, "effort": "medium"},
            },
            {
                "name": "moderate",
                "when": {"difficulty": {"gte": self.model_cutoff}},
                "use": {"model": astra, "effort": "low"},
            },
            # The fast tier has no effort ladder: the outcome benchmark found
            # that raising this model's effort made answers slightly worse.
            {
                "name": "easy",
                "when": {},
                "use": {"model": glm, "effort": "none"},
            },
        ]
        return out

    def policy(self) -> dict[str, Any]:
        block: dict[str, Any] = {
            "rules": self.rules(),
            "default": {"model": "gpt-6-astra", "effort": "medium"},
        }
        if self.conf_floor > 0:
            block["low_confidence"] = {
                "min_confidence": self.conf_floor,
                "questions": ["task", "difficulty"],
                "use": {"model": "gpt-6-astra", "effort": "medium"},
            }
        return block


# `gt` is not a comparison the shipped policy understands, so express it as the
# equivalent `gte` with a hair added. Keeping it here means router.yaml only
# ever uses the operators policy.py already supports.
def _normalise(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for rule in rules:
        when = {}
        for key, cond in rule["when"].items():
            if isinstance(cond, dict) and "gt" in cond:
                when[key] = {"gte": round(cond["gt"] + 0.001, 4)}
            elif isinstance(cond, dict):
                when[key] = {k: v for k, v in cond.items() if v is not None}
            else:
                when[key] = cond
        out.append({**rule, "when": when})
    return out


# --- cases to tune on ---------------------------------------------------


@dataclass
class TuneCase:
    case_id: str
    split: str
    answers: dict[str, Any]
    features: Features
    acceptable_tiers: list[str]
    effort: str | None
    weight: float = 1.0
    source: str = "eval"
    slice: str = "chat"

    def __post_init__(self) -> None:
        self.acceptable_tiers = list(self.acceptable_tiers)


def features_from_facts(facts: dict[str, Any]) -> Features:
    """Rebuild just enough of Features for the policy from a logged row.

    The decision log keeps counts and flags, never message text, so this is
    everything a replay can have.
    """
    return Features(
        model=str(facts.get("requested_model", "auto")),
        message_count=int(facts.get("message_count") or 0),
        est_tokens=int(facts.get("est_tokens") or 0),
        total_chars=int(facts.get("total_chars") or 0),
        has_tools=bool(facts.get("has_tools")),
        tool_names=tuple(facts.get("tool_names") or ()),
        has_images=bool(facts.get("has_images")),
        has_code=bool(facts.get("has_code")),
        languages=tuple(facts.get("languages") or ()),
        client=str(facts.get("client") or ""),
        stream=bool(facts.get("stream")),
        max_tokens=int(facts.get("max_tokens") or 0),
    )


def load_eval_cases(results_dir: Path, variant: str) -> list[TuneCase]:
    path = results_dir / "per_case.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r["variant"] == variant]
    if not rows:
        names = sorted({r["variant"] for r in rows} or {})
        raise SystemExit(
            f"no rows for variant {variant!r} in {path}. "
            f"Known variants: {', '.join(sorted({json.loads(l)['variant'] for l in path.read_text().splitlines() if l.strip()}))}"
        )
    out = []
    for r in rows:
        facts = {
            "message_count": (r["state"] or {}).get("facts", {}).get("message_count", 0)
            if isinstance(r["state"], dict)
            else 0,
            "est_tokens": (r["state"] or {}).get("facts", {}).get("estimated_prompt_tokens", 0)
            if isinstance(r["state"], dict)
            else 0,
            "has_tools": (r["state"] or {}).get("facts", {}).get("request_has_tools", False)
            if isinstance(r["state"], dict)
            else False,
            "tool_names": (r["state"] or {}).get("facts", {}).get("tool_names", [])
            if isinstance(r["state"], dict)
            else [],
            "has_images": (r["state"] or {}).get("facts", {}).get("contains_images", False)
            if isinstance(r["state"], dict)
            else False,
            "has_code": (r["state"] or {}).get("facts", {}).get("contains_code_blocks", False)
            if isinstance(r["state"], dict)
            else False,
        }
        out.append(
            TuneCase(
                case_id=r["case_id"],
                split=r["split"],
                answers=r["answers"],
                features=features_from_facts(facts),
                acceptable_tiers=r["label"]["acceptable_tiers"],
                effort=r["label"]["effort"],
                slice=(r.get("meta") or {}).get("slice", "chat"),
            )
        )
    return out


def resplit_by_slice(cases: list[TuneCase], held: str) -> int:
    """Hold out one whole slice instead of 30% of the rows.

    A row split puts near-duplicates of a held-out case in the tuning half,
    which flatters the held-out number. Holding out a whole shape asks the
    harder question: do numbers tuned without this kind of request work on it?
    """
    n = 0
    for case in cases:
        if case.slice == held:
            case.split = "held_out"
            n += 1
        else:
            case.split = "tune"
    return n


def apply_outcomes(cases: list[TuneCase], outcomes_path: Path) -> int:
    """Widen a case's acceptable tiers where the outcome benchmark showed the
    cheaper tier was good enough. Never narrows: a label saying frontier and an
    outcome saying frontier agree, and a label saying fast is left alone."""
    if not outcomes_path.exists():
        return 0
    data = json.loads(outcomes_path.read_text())
    by_id = {row["case_id"]: row for row in data.get("cases", [])}
    widened = 0
    for case in cases:
        row = by_id.get(case.case_id)
        if not row:
            continue
        tier = row.get("cheapest_adequate_tier")
        if tier and tier not in case.acceptable_tiers:
            case.acceptable_tiers.append(tier)
            case.source = "eval+outcome"
            widened += 1
    return widened


def load_feedback_cases(db_path: Path, config: RouterConfig) -> list[TuneCase]:
    """One replayable case per feedback row that carries a correction.

    `right` confirms what was chosen. `too_weak` and `too_strong` move the
    label to the model and effort the person named, or one step if they did not
    name one. Every row counts FEEDBACK_WEIGHT times.
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT f.verdict, f.better_model, f.better_effort, f.decision_id,"
            " d.answers, d.features, d.model, d.effort"
            " FROM feedback f JOIN decisions d"
            " ON d.id = (SELECT MAX(id) FROM decisions WHERE decision_id = f.decision_id)"
            " WHERE d.fallback = 0"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    order = config.settings.effort_order
    out: list[TuneCase] = []
    for row in rows:
        try:
            answers = json.loads(row["answers"] or "{}")
            facts = json.loads(row["features"] or "{}")
        except ValueError:
            continue
        if not answers:
            continue
        model = row["better_model"] or row["model"]
        effort = row["better_effort"] or row["effort"]
        if row["verdict"] == "too_weak" and not row["better_model"]:
            model = "gpt-6-astra"
            if not row["better_effort"]:
                effort = _step(order, effort, +1)
        if row["verdict"] == "too_strong" and not row["better_model"]:
            if not row["better_effort"]:
                effort = _step(order, effort, -1)
        if row["verdict"] == "too_slow" and not row["better_effort"]:
            effort = _step(order, effort, -1)
        out.append(
            TuneCase(
                case_id=f"feedback:{row['decision_id']}",
                split="tune",
                answers=answers,
                features=features_from_facts(facts),
                acceptable_tiers=[tier_of(model)],
                effort=effort,
                weight=FEEDBACK_WEIGHT,
                source="feedback",
            )
        )
    return out


def _step(order: list[str], effort: str | None, delta: int) -> str | None:
    if effort is None:
        return None
    try:
        i = order.index(effort)
    except ValueError:
        return effort
    return order[max(0, min(len(order) - 1, i + delta))]


# --- scoring ------------------------------------------------------------


def score(
    ladder: Ladder,
    cases: Iterable[TuneCase],
    base_overlay: dict[str, Any],
    alias: str = "auto",
) -> dict[str, Any]:
    overlay = dict(base_overlay)
    policy = ladder.policy()
    policy["rules"] = _normalise(policy["rules"])
    overlay = {**overlay, "policy": policy}
    try:
        config = config_with_overlay(
            overlay, replace_questions=questions_to_replace(overlay)
        )
    except Exception:
        # A ladder that turns on a rule about a question this alias does not
        # ask. Not a viable setting, so the search skips it.
        return {"n": 0}
    alias_cfg = config.aliases[alias]
    if any(
        q not in alias_cfg.questions
        for rule in config.ruleset_for(alias_cfg).rules
        for q in rule.when
        if q in config.questions
    ):
        return {"n": 0}
    return score_with_config(config, alias_cfg, cases)


def score_with_config(
    config: RouterConfig, alias_cfg: AliasCfg, cases: Iterable[TuneCase]
) -> dict[str, Any]:
    weight = under = over = correct = effort_ok = cost = 0.0
    misses: list[str] = []
    for case in cases:
        result = evaluate(config, alias_cfg, case.answers, case.features)
        tier = tier_of(result.model)
        w = case.weight
        weight += w
        cost += w * route_cost(result.model, result.effort)
        if tier in case.acceptable_tiers:
            correct += w
        elif tier == "fast":
            under += w
            misses.append(case.case_id)
        else:
            over += w
        if case.effort is not None and abs(
            effort_rank(result.effort) - effort_rank(case.effort)
        ) <= 1:
            effort_ok += w
    if not weight:
        return {"n": 0}
    return {
        "n": weight,
        "tier_correct": 100 * correct / weight,
        "under_routed": 100 * under / weight,
        "over_routed": 100 * over / weight,
        "effort_within_1": 100 * effort_ok / weight,
        "mean_cost": cost / weight,
        "under_routed_cases": misses,
    }


def search(
    cases: list[TuneCase],
    base_overlay: dict[str, Any],
    start: Ladder,
    samples: int = 3000,
    limit: float = UNDER_ROUTE_LIMIT,
    seed: int = 7,
    under_cost: float = UNDER_ROUTE_COST,
) -> tuple[Ladder, dict[str, Any]]:
    """Random sampling over the grid, then coordinate descent to polish.

    The objective is quota spent plus the cost of the under-routes, among
    settings that keep under-routing at or below `limit`. Ties go to less
    over-routing.
    """
    rng = random.Random(seed)
    fields = list(Ladder.GRID)

    def key(m: dict[str, Any]) -> tuple[float, float, float]:
        if not m or m["n"] == 0:
            return (1e9, 1e9, 1e9)
        over_limit = max(0.0, m["under_routed"] - limit) * 1000
        spent = m["mean_cost"] + under_cost * m["under_routed"] / 100.0
        return (over_limit + spent, m["over_routed"], -m["tier_correct"])

    cache: dict[Ladder, dict[str, Any]] = {}

    def measure(l: Ladder) -> dict[str, Any]:
        if l not in cache:
            cache[l] = score(l, cases, base_overlay) if l.sane() else {"n": 0}
        return cache[l]

    best, best_metrics = start, measure(start)
    for _ in range(samples):
        cand = Ladder(**{f: rng.choice(Ladder.GRID[f]) for f in fields})
        if not cand.sane():
            continue
        m = measure(cand)
        if key(m) < key(best_metrics):
            best, best_metrics = cand, m

    improved = True
    while improved:
        improved = False
        for f in fields:
            for value in Ladder.GRID[f]:
                cand = replace(best, **{f: value})
                if not cand.sane():
                    continue
                m = measure(cand)
                if key(m) < key(best_metrics):
                    best, best_metrics, improved = cand, m, True
    return best, best_metrics


# --- the proposed diff --------------------------------------------------


def render_policy_block(ladder: Ladder) -> str:
    import yaml

    policy = ladder.policy()
    policy["rules"] = _normalise(policy["rules"])
    body = yaml.safe_dump(
        {"policy": policy}, sort_keys=False, default_flow_style=False, width=100
    )
    return body


def propose_diff(ladder: Ladder, path: Path | None = None) -> str:
    path = path or ROOT / "router.yaml"
    old = path.read_text()
    match = re.search(r"^policy:\n(?:[ \t].*\n|\n)*", old, re.M)
    if not match:
        return "# could not find a top-level `policy:` block in router.yaml\n"
    new = old[: match.start()] + render_policy_block(ladder) + "\n" + old[match.end() :]
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (proposed)",
        )
    )


# --- main ---------------------------------------------------------------


def current_ladder_from_config(path: Path | None = None) -> Ladder:
    """Read the numbers already in router.yaml, so `before` is the real policy."""
    cfg = config_with_overlay(path=path)
    by_name = {r.name: r for r in cfg.policy.rules}

    def gte(name: str, default: float) -> float:
        rule = by_name.get(name)
        if rule is None:
            return default
        for cond in rule.when.values():
            if isinstance(cond, dict) and "gte" in cond:
                return float(cond["gte"])
        return default

    lc = cfg.policy.low_confidence
    return Ladder(
        conf_floor=lc.min_confidence if lc else 0.0,
        model_cutoff=gte("moderate", 1.0),
        hard_min=gte("hard", 1.8),
        very_hard_min=gte("very_hard", 2.6),
        harm_threshold=gte("high_harm", OFF),
        faith_threshold=gte("needs_care", OFF),
    )


def fmt(m: dict[str, Any]) -> str:
    if not m or not m.get("n"):
        return "no cases"
    return (
        f"n={m['n']:.0f} tier {m['tier_correct']:5.1f}%  under {m['under_routed']:4.1f}%  "
        f"over {m['over_routed']:5.1f}%  effort±1 {m['effort_within_1']:5.1f}%  "
        f"cost {m['mean_cost']:5.2f}  total {m['mean_cost'] + UNDER_ROUTE_COST * m['under_routed'] / 100:5.2f}"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", default=str(EVALS_DIR / "results" / "latest"))
    p.add_argument("--variant", default="winner")
    p.add_argument("--outcomes", default=str(EVALS_DIR / "outcomes.json"))
    p.add_argument("--db", default="~/.jev-router/router.db")
    p.add_argument("--samples", type=int, default=3000)
    p.add_argument("--limit", type=float, default=UNDER_ROUTE_LIMIT,
                   help="highest under-routing the search may accept, in percent")
    p.add_argument("--under-cost", type=float, default=UNDER_ROUTE_COST,
                   help="quota units one under-route costs (see the top of this file)")
    p.add_argument("--slice-holdout",
                   help="hold out one whole slice instead of 30%% of the rows")
    p.add_argument("--no-outcomes", action="store_true")
    p.add_argument("--no-feedback", action="store_true")
    args = p.parse_args()

    results = Path(args.results)
    cases = load_eval_cases(results, args.variant)

    # The overlay is everything about the variant except the policy numbers.
    overlay = variant_overlay(args.variant)
    config = config_with_overlay(
        overlay, replace_questions=questions_to_replace(overlay)
    )

    held_n = 0
    if args.slice_holdout:
        held_n = resplit_by_slice(cases, args.slice_holdout)
        if not held_n:
            print(f"no cases in slice {args.slice_holdout!r}", file=sys.stderr)
            return 2

    widened = 0
    if not args.no_outcomes:
        widened = apply_outcomes(cases, Path(args.outcomes))

    feedback: list[TuneCase] = []
    if not args.no_feedback:
        feedback = load_feedback_cases(Path(args.db).expanduser(), config)

    tune_cases = [c for c in cases if c.split == "tune"] + feedback
    held_out = [c for c in cases if c.split == "held_out"]

    print(f"tuning on {len(tune_cases)} cases "
          f"({len(feedback)} from feedback, weighted {FEEDBACK_WEIGHT:g}x), "
          f"holding out {len(held_out)}"
          + (f" (the whole `{args.slice_holdout}` slice)" if args.slice_holdout else ""))
    if widened:
        print(f"outcome benchmark widened the acceptable tiers of {widened} cases")

    start = current_ladder_from_config()
    print("\nbefore (the numbers in router.yaml now)")
    print(f"  tuning   {fmt(score(start, tune_cases, overlay))}")
    print(f"  held-out {fmt(score(start, held_out, overlay))}")

    best, best_metrics = search(
        tune_cases,
        overlay,
        start,
        samples=args.samples,
        limit=args.limit,
        under_cost=args.under_cost,
    )

    print("\nafter")
    print(f"  tuning   {fmt(best_metrics)}")
    ho = score(best, held_out, overlay)
    print(f"  held-out {fmt(ho)}")
    print("\nproposed numbers")
    for f in Ladder.GRID:
        before, after = getattr(start, f), getattr(best, f)
        mark = "   " if before == after else " ->"
        print(f"  {f:16s} {before:<6g}{mark} {after:g}")
    if best_metrics.get("under_routed_cases"):
        print("\nstill under-routed on the tuning split: "
              f"{sorted(set(best_metrics['under_routed_cases']))}")
    if ho.get("under_routed_cases"):
        print("still under-routed on the held-out split: "
              f"{sorted(set(ho['under_routed_cases']))}")

    print("\n--- proposed diff for router.yaml (not applied) ---")
    print(propose_diff(best))
    return 0


def variant_overlay(name: str) -> dict[str, Any]:
    """The questions and alias a variant uses, without its policy numbers."""
    from run_eval import load_variants

    variants, _ = load_variants()
    if name not in variants:
        return {}
    v = variants[name]
    overlay = dict(v.overlay)
    overlay.pop("policy", None)
    overlay.setdefault("aliases", {})
    overlay["aliases"] = {
        **overlay.get("aliases", {}),
        v.alias: {"state_builder": v.state_builder, "questions": list(v.questions)},
    }
    return overlay


if __name__ == "__main__":
    raise SystemExit(main())
