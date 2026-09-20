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
import copy
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

from result_io import allocate_results, publish_latest

from manifest import (  # noqa: E402
    build_manifest,
    case_manifest,
    profile_versions,
    run_kind,
    variant_versions,
    write_manifest,
)
from common import (  # noqa: E402
    CASE_TASKS,
    EVALS_DIR,
    SLICES,
    SPLIT_SEED,
    TEN_CAT_TO_SEVEN,
    Case,
    JevClient,
    assign_slice_holdout,
    config_with_overlay,
    constant_state_body,
    effort_rank,
    estimate_tokens,
    load_cases,
    median,
    pct,
    percentile,
    route_cost,
    shuffle_labels,
    is_over_routed,
    is_under_routed,
    tier_of,
)
from metrics import (  # noqa: E402
    PERTURBATIONS,
    Rate,
    calibration as calibration_metric,
    accuracy_below_confidence,
    permutation_membership_null,
    cost_matched_random,
    majority_class_rate,
    min_detectable_difference,
    perturb,
    rate as rate_of,
    stability as stability_metric,
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


# --- negative controls ---------------------------------------------------
#
# Four runs that have to come out badly. `shuffled-labels` and `constant-state`
# check the harness. These two check the policy: if a rule set that never looks
# at an answer scores as well as the real one, the answers are not what is
# doing the work.

CONTROL_CONSTANT_POLICY = {
    "rulesets": {
        "control_constant": {
            "rules": [
                {"name": "constant", "when": {}, "use": {"route": "frontier_medium"}}
            ],
            "default": {"route": "frontier_medium"},
        }
    }
}

CONTROL_LENGTH_ONLY = {
    "rulesets": {
        "control_length": {
            "rules": [
                {
                    "name": "very_long",
                    "when": {"est_tokens_gte": 8000},
                    "use": {"route": "frontier_xhigh"},
                },
                {
                    "name": "long",
                    "when": {"est_tokens_gte": 2000},
                    "use": {"route": "frontier_high"},
                },
                {
                    "name": "medium",
                    "when": {"est_tokens_gte": 500},
                    "use": {"route": "mid"},
                },
                {"name": "short", "when": {}, "use": {"route": "fast"}},
            ],
            "default": {"route": "fast"},
        }
    }
}

POLICY_CONTROLS = {
    "constant-policy": ("control_constant", CONTROL_CONSTANT_POLICY),
    "length-only": ("control_length", CONTROL_LENGTH_ONLY),
}

CONTROLS = ("none", "shuffled-labels", "constant-state", *POLICY_CONTROLS)


def apply_policy_control(variant: Variant, control: str) -> Variant:
    """Swap a variant's ruleset for one that cannot read a Jev answer."""
    from common import deep_merge

    ruleset, overlay = POLICY_CONTROLS[control]
    clone = copy.copy(variant)
    clone.overlay = deep_merge(
        deep_merge(variant.overlay, overlay),
        {"aliases": {variant.alias: {"rules": ruleset}}},
    )
    return clone


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
                "slice": c.slice,
                "attack": c.attack,
                "attack_vector": c.attack_vector,
                "shape": c.shape,
                "language": c.language,
                "multi_turn": c.multi_turn,
                "adversarial": c.adversarial,
                "trap": c.trap,
                "source": c.source,
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


def stratified_subset(cases: list[Case], size: int, seed: int = 20260919) -> list[Case]:
    """A sample of `size` cases that keeps the slice mix of the whole set.

    Used where a step costs a real upstream call per case and the whole set is
    out of budget. Proportional by slice, seeded, and it keeps at least one
    case from every slice so no shape disappears from the comparison.
    """
    import random as _random

    by_slice: dict[str, list[Case]] = collections.defaultdict(list)
    for c in cases:
        by_slice[c.slice].append(c)
    rng = _random.Random(seed)
    out: list[Case] = []
    total = len(cases)
    for name in sorted(by_slice):
        group = sorted(by_slice[name], key=lambda c: c.id)
        rng.shuffle(group)
        want = max(1, round(size * len(group) / total))
        out.extend(group[:want])
    out.sort(key=lambda c: c.id)
    return out[:size] if len(out) > size else out


async def fetch_drafts(
    cases: list[Case], config: Any, alias: str, budget_calls: int
) -> dict[str, str]:
    """One short draft per case from the fast model, for the AutoMix question.

    This is the only step in `run_eval.py` that touches the upstream, so it
    carries its own budget and it is only reached when `--draft` is passed.
    """
    from common import Budget, UpstreamClient

    draft_cfg = config.aliases[alias].draft
    if draft_cfg is None or not draft_cfg.enabled:
        return {}
    from jev_router.policy import apply_effort

    if draft_cfg.model in config.models:
        upstream_id, _ = apply_effort(config, draft_cfg.model, draft_cfg.effort)
    else:
        upstream_id = draft_cfg.model
    # An empty draft is an ordinary outcome here, not a sign of a sick
    # upstream: a reasoning model with a small token budget sometimes spends
    # it all thinking. So the empty-reply guard is loose and the step fails
    # open per case, the way the router's own draft step does.
    budget = Budget(max_calls=budget_calls, max_empty=budget_calls)
    client = UpstreamClient(concurrency=2, budget=budget)
    out: dict[str, str] = {}
    try:
        for case in cases:
            request = (case.features().last_user_message or "").strip()
            if not request:
                continue
            res = await client.chat(
                upstream_id,
                [{"role": "user", "content": request[:6000]}],
                max_tokens=draft_cfg.max_tokens,
            )
            if res.text.strip():
                out[case.id] = res.text.strip()
    finally:
        await client.aclose()
    print(f"drafts: {len(out)}/{len(cases)} cases, {budget.line()}")
    return out


async def run_variant(
    variant: Variant,
    cases: list[Case],
    jev: JevClient,
    repeats: int,
    cache: bool,
    drafts: dict[str, str] | None = None,
) -> list[Record]:
    config = variant.config()
    alias_cfg = config.aliases[variant.alias]
    questions = {qid: config.question(qid) for qid in alias_cfg.questions}
    draft_field = (
        alias_cfg.draft.state_field
        if (alias_cfg.draft and alias_cfg.draft.enabled)
        else ""
    )

    async def one(case: Case) -> Record:
        features = case.features()
        state = build_state(alias_cfg.state_builder, features, config)
        if draft_field and drafts and isinstance(state, dict):
            draft = drafts.get(case.id)
            if draft:
                state[draft_field] = draft
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


def metrics(
    records: list[Record], variant: Variant, split: str | tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Every reported number, with a Wilson interval beside each rate.

    Rates live twice in the returned dict: `key` is the percentage, for sorting
    and for the terminal line, and `ci[key]` is the `Rate` that knows the
    interval. The interval is the point of the exercise: on 96 cases one case is
    about one point, and a 95% interval on 90% over 96 cases is roughly six
    points wide, so a three-point "improvement" is not one.
    """
    wanted = (split,) if isinstance(split, str) else split
    recs = [r for r in records if wanted is None or r.case.split in wanted]
    recs = [r for r in recs if r.case.labelled]
    n = len(recs)
    if not n:
        return {}

    ci: dict[str, Rate] = {}

    def rate(key: str, fn) -> float:
        r = rate_of(fn(rec) for rec in recs)
        ci[key] = r
        return r.pct

    has_answers = any(r.answers for r in recs)
    out: dict[str, Any] = {"n": n, "ci": ci}

    if has_answers:
        out["task_acc"] = rate("task_acc", lambda r: score_task(r, variant.task_space))
        out["task_acc_7cat"] = rate("task_acc_7cat", lambda r: score_task7(r, variant.task_space))
        out["difficulty_ok"] = rate(
            "difficulty_ok", lambda r: score_difficulty(r, variant.difficulty_levels)
        )
        out["faith_acc"] = rate("faith_acc", lambda r: score_faith(r, variant))
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
        out["stability"] = rate("stability", stable) if stab else None
        for qid in ("task", "difficulty"):
            cal = calibration_metric(confidence_pairs(recs, variant, qid))
            out[f"ece_{qid}"] = cal.ece
            out[f"overconfident_{qid}"] = cal.overconfident_by
        lat = [ms for r in recs for ms in r.latencies]
        out["jev_p50_ms"] = percentile(lat, 0.5) if lat else None
        out["jev_p95_ms"] = percentile(lat, 0.95) if lat else None
        out["state_tokens_median"] = median([estimate_tokens(r.state) for r in recs])
        out["jev_tokens_median"] = median([r.input_tokens for r in recs if r.input_tokens])

    # routing
    out["tier_correct"] = rate("tier_correct", lambda r: tier_of(r.model) in r.case.acceptable_tiers)
    out["under_routed"] = rate(
        "under_routed",
        lambda r: is_under_routed(r.model, r.case.acceptable_tiers),
    )
    out["over_routed"] = rate(
        "over_routed",
        lambda r: is_over_routed(r.model, r.case.acceptable_tiers),
    )
    out["effort_within_1_all"] = rate(
        "effort_within_1_all",
        lambda r: abs(effort_rank(r.effort) - effort_rank(r.case.effort)) <= 1,
    )
    frontier = [r for r in recs if r.case.tier == "frontier"]
    ci["effort_within_1_frontier"] = rate_of(
        abs(effort_rank(r.effort) - effort_rank(r.case.effort)) <= 1 for r in frontier
    )
    out["effort_within_1_frontier"] = ci["effort_within_1_frontier"].pct
    out["mean_cost"] = statistics.fmean(route_cost(r.model, r.effort) for r in recs)
    out["frontier_rate"] = pct(sum(1 for r in recs if tier_of(r.model) == "frontier"), n)

    # What this many cases can and cannot show.
    out["min_detectable_pp"] = min_detectable_difference(n, p=0.85)
    out["majority_class"] = majority_class_rate([r.case.acceptable_tiers for r in recs])

    # The Zero Router from RouterBench: the router's own spend, allocated at
    # random. Beating it is the bar a router has to clear to be worth its hop.
    random_baseline = cost_matched_random(
        [r.case.acceptable_tiers for r in recs],
        [(r.model, r.effort) for r in recs],
        route_cost,
        tier_of,
    )
    out["random_tier_correct"] = random_baseline.tier_correct
    out["random_mean_cost"] = random_baseline.mean_cost
    out["random_under_routed"] = random_baseline.under_routed
    out["random_sd"] = random_baseline.tier_correct_sd

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


def confidence_pairs(
    recs: list[Record], variant: Variant, qid: str
) -> list[tuple[float | None, bool | None]]:
    """(confidence, was the answer right) for one question, for calibration."""
    scorer = (
        (lambda r: score_task(r, variant.task_space))
        if qid == "task"
        else (lambda r: score_difficulty(r, variant.difficulty_levels))
    )
    return [(confidence(r.answers, qid), scorer(r)) for r in recs]


def slice_table(records: list[Record], variant: Variant, key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Record]] = collections.defaultdict(list)
    for r in records:
        if r.case.labelled:
            groups[str(key(r.case))].append(r)
    out = {}
    for name in sorted(groups):
        recs = groups[name]
        out[name] = {
            "n": len(recs),
            "task_acc": rate_of(score_task(r, variant.task_space) for r in recs),
            "difficulty_ok": rate_of(
                score_difficulty(r, variant.difficulty_levels) for r in recs
            ),
            "tier_correct": rate_of(
                tier_of(r.model) in r.case.acceptable_tiers for r in recs
            ),
            "under_routed": rate_of(
                is_under_routed(r.model, r.case.acceptable_tiers)
                for r in recs
            ),
            "mean_cost": statistics.fmean(route_cost(r.model, r.effort) for r in recs),
        }
    return out


# --- decision stability under perturbation ------------------------------


def perturbed_body(case: Case, kind: str, seed: int) -> dict[str, Any] | None:
    """The case's request with only the latest user message reworded."""
    body = copy.deepcopy(case.body)
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = perturb(content, kind, seed)
            return body
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = perturb(part["text"], kind, seed)
                    return body
        return None
    return None


async def run_stability(
    variant: Variant, cases: list[Case], jev: JevClient, kinds: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Re-decide every case under each seeded rewording of its latest message.

    Answers are cached like any other Jev call, so a rerun is free. The
    perturbations are string edits with a fixed seed, so the variants do not
    have to be stored anywhere.
    """
    config = variant.config()
    alias_cfg = config.aliases[variant.alias]
    questions = {qid: config.question(qid) for qid in alias_cfg.questions}

    async def decide(body: dict[str, Any]) -> tuple[str, str | None]:
        from jev_router.features import extract_features

        body = dict(body)
        body.setdefault("model", "auto")
        features = extract_features(body)
        state = build_state(alias_cfg.state_builder, features, config)
        res = await jev.ask(state, questions)
        policy = evaluate(config, alias_cfg, res.answers, features)
        return policy.model, policy.effort

    async def one(case: Case) -> dict[str, Any] | None:
        try:
            base = await decide(case.body)
        except Exception:  # noqa: BLE001 - a case that cannot be decided is skipped
            return None
        variants: dict[str, tuple[str, str | None]] = {}
        for i, kind in enumerate(kinds):
            body = perturbed_body(case, kind, seed=i + 1)
            if body is None:
                continue
            try:
                variants[kind] = await decide(body)
            except Exception:  # noqa: BLE001
                continue
        return {"case_id": case.id, "slice": case.slice, "base": base, "variants": variants}

    rows = await asyncio.gather(*(one(c) for c in cases))
    return [r for r in rows if r]


# --- per-question agreement ----------------------------------------------

# The label in a case's `labels:` block each question is scored against, and
# how a raw answer becomes a comparable value. A case with no label for a
# question is skipped, so a partially annotated set is honest about its
# denominator rather than scoring the unlabelled cases as wrong.
NOUL_THRESHOLD = 0.5

ATOMIC_QUESTIONS = (
    "mechanical_transform",
    "interacting_constraints",
    "requirements_missing",
    "corrective_followup",
    "failure_mode",
)


def score_question(rec: Record, qid: str) -> bool | None:
    """Does this answer agree with the case's label for that question?

    Returns None when the case carries no label for it, which keeps the
    interval honest about how many cases actually said anything.
    """
    if qid not in rec.case.labels:
        return None
    answer = rec.answers.get(qid)
    if not isinstance(answer, dict):
        return None
    want = rec.case.labels[qid]
    kind = answer.get("type")
    if kind == "noul":
        value = answer.get("noul")
        if value is None:
            return None
        return bool(value >= NOUL_THRESHOLD) == bool(want)
    if kind == "choice":
        return answer.get("choice") == want
    if kind == "score":
        value = answer.get("score")
        return value is not None and abs(value - float(want)) <= 0.5
    return None


def question_table(records: list[Record], variant: Variant) -> list[dict[str, Any]]:
    """One row per question asked: agreement, abstention, state size, latency."""
    out: list[dict[str, Any]] = []
    for qid in variant.questions:
        if qid == "task":
            agreement = rate_of(score_task(r, variant.task_space) for r in records)
            against = "task label"
        elif qid == "difficulty":
            agreement = rate_of(
                score_difficulty(r, variant.difficulty_levels) for r in records
            )
            against = "difficulty within tolerance"
        elif qid in ATOMIC_QUESTIONS:
            agreement = rate_of(score_question(r, qid) for r in records)
            against = f"labels.{qid}"
        elif qid == variant.faith_question:
            agreement = rate_of(score_faith(r, variant) for r in records)
            against = "needs_faithfulness"
        else:
            agreement = rate_of(score_question(r, qid) for r in records)
            against = f"labels.{qid}"
        answered = sum(1 for r in records if isinstance(r.answers.get(qid), dict))
        out.append(
            {
                "question": qid,
                "against": against,
                "agreement": agreement,
                "answered": answered,
                "abstained": len(records) - answered,
                "labelled": sum(1 for r in records if qid in r.case.labels),
            }
        )
    return out


def confusion(records: list[Record], variant: Variant) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = collections.defaultdict(lambda: collections.defaultdict(int))
    for r in records:
        if not r.case.labelled:
            continue
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
                    c for r in bucket if (c := confidence(r.answers, qid)) is not None
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


def cell(m: dict[str, Any], key: str) -> str:
    """A rate with its Wilson interval, or a plain number when it is not a rate."""
    r = (m.get("ci") or {}).get(key)
    if r is not None and r.n:
        return r.cell()
    return fmt(m.get(key))


def write_summary(
    out_dir: Path,
    variants: dict[str, Variant],
    results: dict[str, list[Record]],
    repeats: int,
    jev_calls: int,
    jev_model: str,
    stability_rows: list[dict[str, Any]] | None = None,
    control: str = "",
    packet: bool = False,
) -> str:
    lines: list[str] = []
    w = lines.append
    first = next(iter(results.values()))
    n_all = sum(1 for r in first if r.case.labelled)
    w("# Eval run")
    w("")
    w(f"- when: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    w(f"- jev model: {jev_model}")
    w(f"- repeats per case: {repeats}")
    w(f"- live Jev calls this run: {jev_calls}")
    w(f"- cases: {n_all} labelled "
      f"(tune {sum(1 for r in first if r.case.split == 'tune')}, "
      f"held-out {sum(1 for r in first if r.case.split == 'held_out')})"
      + (f", plus {sum(1 for r in first if not r.case.labelled)} unlabelled"
         if any(not r.case.labelled for r in first) else ""))
    w(f"- slices: " + ", ".join(
        f"{name} {count}"
        for name, count in sorted(
            collections.Counter(r.case.slice for r in first if r.case.labelled).items()
        )
    ))
    if control:
        w(f"- **control run: `{control}`.** These numbers are supposed to be bad.")
    w("")
    w("Every rate is followed by its Wilson 95% interval. On "
      f"{n_all} cases a rate near 85% carries an interval about "
      f"{2 * min_detectable_difference(n_all, 0.85) / (1.96 + 0.84) * 1.96:.0f} "
      "points wide, and the smallest difference two runs of this size can "
      f"separate from noise is about {min_detectable_difference(n_all, 0.85):.0f} "
      "points. Read anything smaller as a tie.")
    w("")

    # The out-of-distribution sample is reported on its own and never folded
    # into the headline: its labels are llm-assisted, not hand-written.
    splits = [
        ("all written cases", ("tune", "held_out")),
        ("tuning split", ("tune",)),
        ("held-out split", ("held_out",)),
    ]
    if any(r.case.split == "ood" for r in first):
        splits.append(("out-of-distribution: sampled public traffic", ("ood",)))
    for split_name, split in splits:
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
            row = [name, str(m["n"])] + [
                (cell(m, k) if k not in ("mean_cost",) else fmt(m.get(k)))
                for k, _, _ in HEADLINE
            ]
            w("| " + " | ".join(row) + " |")
        w("")

    # --- controls and cost-matched baselines ----------------------------
    w("## Baselines and controls")
    w("")
    w("`cost-matched random` is RouterBench's Zero Router: it sends a request to "
      "the frontier tier with the same probability the router does, and picks the "
      "effort from the router's own mix, so it spends what the router spends and "
      "chooses at random. A router that cannot beat it is not choosing, it is "
      "spending. `majority class` is the best a router that ignores the request "
      "can do. Both are computed per variant, over all labelled cases.")
    w("")
    w("| variant | tier ok % | cost-matched random % | majority class % | cost | random cost |")
    w("|---|---|---|---|---|---|")
    for name, recs in results.items():
        v = variants.get(name) or Variant(name=name)
        m = metrics(recs, v, ("tune", "held_out"))
        if not m:
            continue
        w(f"| {name} | {cell(m, 'tier_correct')} | "
          f"{fmt(m.get('random_tier_correct'))} ± {fmt(m.get('random_sd'))} | "
          f"{fmt(m.get('majority_class'))} | {fmt(m.get('mean_cost'))} | "
          f"{fmt(m.get('random_mean_cost'))} |")
    w("")
    if control == "shuffled-labels":
        w("Shuffled-label control: fixed predictions against uniformly shuffled "
          "label sets, using the same membership scorer as accuracy. Bounds "
          "are 95% permutation-null intervals, not quality confidence intervals.")
        for name, records in results.items():
            labelled = [r for r in records if r.case.labelled]
            if not labelled:
                continue
            variant = variants.get(name) or Variant(name=name)
            for metric, predictions, accepted in (
                ("tier", [tier_of(r.model) for r in labelled],
                 [r.case.acceptable_tiers for r in labelled]),
                ("task", [str(answer_value(r.answers, "task")) for r in labelled],
                 [[r.case.task7 if variant.task_space == "seven" else r.case.task]
                  for r in labelled]),
            ):
                null = permutation_membership_null(predictions, accepted)
                w(f"- {name} {metric}: observed {null['observed']:.1f}%; "
                  f"chance {null['expected']:.1f}% "
                  f"[{null['low']:.1f}, {null['high']:.1f}]; "
                  f"one-sided permutation p = {null['p_upper']:.4f}.")
        w("")
    if control == "constant-state":
        w("Constant-state control. Every case was replaced by the same bland "
          "request, so routing accuracy has to fall to the majority-class rate "
          "in the table above. Anything higher means the routing score is "
          "reading something other than the state.")
        w("")

    # --- stability under perturbation -----------------------------------
    if stability_rows:
        st = stability_metric(stability_rows)
        w("## Decision stability under perturbation")
        w("")
        w(f"Each case's latest user message was reworded {st.n_variants} ways "
          f"({', '.join(PERTURBATIONS)}), with a fixed seed, and the router asked "
          "again. A route that moves when the wording moves is a route the user "
          "cannot predict.")
        w("")
        w(f"- unchanged (model, effort), over case x rewording: {st.unchanged}")
        w(f"- cases where every rewording kept the route: {st.unchanged_all}")
        w("")
        if st.detail:
            w("| case | base route | what moved |")
            w("|---|---|---|")
            for row in st.detail:
                moves = ", ".join(f"{k} -> {v}" for k, v in row["moves"].items())
                w(f"| {row['case_id']} | {row['base']} | {moves} |")
            w("")
        by_slice: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for row in stability_rows:
            by_slice[row.get("slice", "chat")].append(row)
        w("| slice | n | unchanged % |")
        w("|---|---|---|")
        for name in sorted(by_slice):
            s = stability_metric(by_slice[name])
            w(f"| {name} | {s.n_cases} | {s.unchanged} |")
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
            ("by slice", lambda c: c.slice),
            ("by client shape", lambda c: c.shape),
            ("by language", lambda c: c.language),
            ("multi-turn", lambda c: "multi-turn" if c.multi_turn else "single-turn"),
        ):
            w(f"### {name}: {title}")
            w("")
            w("| group | n | task acc % | difficulty % | tier ok % | under % | cost |")
            w("|---|---|---|---|---|---|---|")
            for gname, row in slice_table(recs, v, key).items():
                w(f"| {gname} | {row['n']} | {row['task_acc']} | "
                  f"{row['difficulty_ok']} | {row['tier_correct']} | "
                  f"{row['under_routed']} | {row['mean_cost']:.1f} |")
            w("")

        # --- the adversarial slice, by what the attacker was after -------
        traps = [r for r in recs if r.case.labelled and r.case.attack]
        if traps:
            w(f"### {name}: adversarial slice by attack goal")
            w("")
            w("An attack succeeded when the route left `acceptable_tiers`, or when "
              "the effort was pushed more than one level above the label. The "
              "expected labels ignore the attack, so holding the label is the "
              "whole of the test.")
            w("")
            for group_name, keyfn in (
                ("goal", lambda c: c.attack),
                ("delivery", lambda c: c.attack_vector),
            ):
                groups: dict[str, list[Record]] = collections.defaultdict(list)
                for r in traps:
                    groups[keyfn(r.case) or "unlabelled"].append(r)
                w(f"| {group_name} | n | tier held % | effort held % | cost |")
                w("|---|---|---|---|---|")
                for gname in sorted(groups):
                    g = groups[gname]
                    tier_held = rate_of(
                        tier_of(r.model) in r.case.acceptable_tiers for r in g
                    )
                    effort_held = rate_of(
                        effort_rank(r.effort) - effort_rank(r.case.effort) <= 1 for r in g
                    )
                    cost_mean = statistics.fmean(route_cost(r.model, r.effort) for r in g)
                    w(f"| {gname} | {len(g)} | {tier_held} | {effort_held} | {cost_mean:.1f} |")
                w("")
            broken = [
                r
                for r in traps
                if tier_of(r.model) not in r.case.acceptable_tiers
                or effort_rank(r.effort) - effort_rank(r.case.effort) > 1
            ]
            if broken:
                w("Attacks that worked:")
                w("")
                w("| case | goal | delivery | label | route |")
                w("|---|---|---|---|---|")
                for r in sorted(broken, key=lambda r: r.case.id):
                    c = r.case
                    w(f"| {c.id} | {c.attack} | {c.attack_vector} | "
                      f"{c.tier}/{c.effort} | {r.model.split('/')[-1]}({r.effort}) |")
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

        labelled = [r for r in recs if r.case.labelled]
        w(f"### {name}: confidence calibration")
        w("")
        w("ECE is the expected calibration error: the bin-count-weighted mean gap "
          "between what Jev claimed and what it delivered, on a 0 to 1 scale. "
          "`overconfident by` is mean confidence minus accuracy, so a positive "
          "number means Jev claims more than it knows. The rows below the table "
          "are what the confidence gate actually buys: if accuracy below the "
          "cutoff is no worse than above it, the gate is spending money for "
          "nothing.")
        w("")
        w("| question | n | ECE | worst bin | mean confidence | accuracy | overconfident by |")
        w("|---|---|---|---|---|---|---|")
        for qid in ("task", "difficulty"):
            cal = calibration_metric(confidence_pairs(labelled, v, qid))
            if not cal.n:
                continue
            w(f"| {qid} | {cal.n} | {cal.ece:.3f} | {cal.mce:.3f} | "
              f"{cal.mean_confidence:.3f} | {cal.accuracy:.3f} | "
              f"{cal.overconfident_by:+.3f} |")
        w("")
        gate = 0.55
        try:
            gate_config = v.config().policy.low_confidence
            if gate_config is not None:
                gate = gate_config.min_confidence
        except Exception:  # noqa: BLE001 - a variant with no gate keeps the default
            pass
        w(f"| question | cutoff | accuracy below | accuracy at or above |")
        w("|---|---|---|---|")
        for qid in ("task", "difficulty"):
            below, above = accuracy_below_confidence(
                confidence_pairs(labelled, v, qid), gate
            )
            w(f"| {qid} | {gate:g} | {below} | {above} |")
        w("")
        for qid in ("task", "difficulty"):
            rows = calibration(recs, v, qid)
            if not rows:
                continue
            w(f"#### {name}: {qid}, per bin")
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
            if r.case.labelled
            and (
                tier_of(r.model) not in r.case.acceptable_tiers
                or not score_task(r, v.task_space)
            )
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
                    why.append("under-routed" if is_under_routed(r.model, c.acceptable_tiers) else "over-routed")
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

    # --- per question ----------------------------------------------------
    w("## Per question")
    w("")
    w("Agreement is against the label named in the `against` column. A case "
      "that carries no label for a question is not counted, so `labelled` is "
      "the real denominator; `abstained` counts answers that never came back. "
      "Confidence is never read as the probability that the downstream model "
      "will succeed: it describes Jev's own answer distribution.")
    w("")
    w("| variant | question | against | agreement | labelled | answered | abstained |")
    w("|---|---|---|---|---:|---:|---:|")
    for name, recs in results.items():
        if name.startswith("baseline:"):
            continue
        v = variants.get(name) or Variant(name=name)
        scored = [r for r in recs if r.case.labelled]
        for row in question_table(scored, v):
            w(
                f"| {name} | {row['question']} | {row['against']} | "
                f"{row['agreement']} | {row['labelled']} | {row['answered']} | "
                f"{row['abstained']} |"
            )
    w("")

    if packet:
        w("## Packet comparison (spec 13.4)")
        w("")
        w("Four arms, one variable at a time: the baseline packet, the same "
          "policy on the new bounded packet, the expanded packet with the "
          "extra questions asked, and the distribution-aware policy against "
          "the mean-only one. The last pair asks identical questions over an "
          "identical state, so both arms read the same cached Jev answers and "
          "the only thing that differs is the rule set.")
        w("")
        w("| arm | state builder | questions | tier correct | under | over | cost | state tokens |")
        w("|---|---|---|---|---|---|---:|---:|")
        for name, recs in results.items():
            if name.startswith("baseline:"):
                continue
            v = variants.get(name) or Variant(name=name)
            m = metrics(recs, v)
            if not m:
                continue
            tokens = median([estimate_tokens(r.state) for r in recs if r.state])
            w(
                f"| {name} | {v.state_builder} | {len(v.questions)} | "
                f"{cell(m, 'tier_correct')} | {cell(m, 'under_routed')} | "
                f"{cell(m, 'over_routed')} | {fmt(m.get('mean_cost'))} | "
                f"{tokens:.0f} |"
            )
        w("")
        w("No conclusion is drawn here by the script. Two arms that differ by "
          "less than the minimum detectable difference above are a tie, and "
          "the paired bootstrap is what decides a real one.")
        w("")

    text = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(text)
    return text


# --- main ---------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    all_variants, groups = load_variants()
    names: list[str] = []
    if args.packet and not args.variants and not args.group:
        names = list(groups.get("packet", []))
    elif args.variants:
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

    extra = [Path(p.strip()) for p in (args.public or "").split(",") if p.strip()]
    missing_files = [p for p in extra if not p.exists()]
    if missing_files:
        print(f"no such case file: {missing_files}", file=sys.stderr)
        return 2
    cases = load_cases(extra=extra)
    if args.slice_holdout:
        if args.slice_holdout not in SLICES:
            print(f"unknown slice {args.slice_holdout!r} (known: {', '.join(SLICES)})",
                  file=sys.stderr)
            return 2
        n_held = assign_slice_holdout(cases, args.slice_holdout)
        print(f"slice hold-out: {args.slice_holdout}, {n_held} cases held out")
    if args.slice:
        wanted_slices = {s.strip() for s in args.slice.split(",")}
        cases = [c for c in cases if c.slice in wanted_slices]
    if args.limit:
        cases = cases[: args.limit]
    if args.case:
        wanted = {c.strip() for c in args.case.split(",")}
        cases = [c for c in cases if c.id in wanted]

    # Controls. Both are runs that have to come out badly: if a shuffled-label
    # run still scores well, the metric is not reading the labels, and if a
    # constant-state run still routes well, the router is not reading the state.
    if args.control in POLICY_CONTROLS:
        all_variants = {
            name: (apply_policy_control(v, args.control) if name in names else v)
            for name, v in all_variants.items()
        }
    if args.control == "shuffled-labels":
        cases = shuffle_labels(cases)
    elif args.control == "constant-state":
        body = constant_state_body()
        clones = []
        for c in cases:
            clone = copy.copy(c)
            clone.body = copy.deepcopy(body)
            clones.append(clone)
        cases = clones

    drafts: dict[str, str] = {}
    if args.draft:
        cases = stratified_subset(cases, args.draft_subset)
        print(f"draft run: {len(cases)} cases, stratified by slice")
        for name in names:
            cfg = all_variants[name].config()
            alias_cfg = cfg.aliases[all_variants[name].alias]
            if alias_cfg.draft and alias_cfg.draft.enabled:
                drafts = await fetch_drafts(
                    cases, cfg, all_variants[name].alias, args.draft_max_calls
                )
                break

    jev = JevClient(model=args.jev_model, concurrency=args.concurrency, cache=not args.no_cache)
    results: dict[str, list[Record]] = {}
    stability_rows: list[dict[str, Any]] = []
    try:
        for name in names:
            started = time.time()
            variant = all_variants[name]
            recs = await run_variant(
                variant, cases, jev, args.repeats, not args.no_cache, drafts=drafts
            )
            results[name] = recs
            m = metrics(recs, variant)
            print(
                f"{name:26s} task {m.get('task_acc', 0):5.1f}%  diff {m.get('difficulty_ok', 0):5.1f}%  "
                f"tier {m.get('tier_correct', 0):5.1f}%  under {m.get('under_routed', 0):4.1f}%  "
                f"over {m.get('over_routed', 0):4.1f}%  cost {m.get('mean_cost', 0):5.1f}  "
                f"({time.time() - started:.0f}s)"
            )
        if args.stability and names:
            started = time.time()
            stability_rows = await run_stability(
                all_variants[names[0]], cases, jev, PERTURBATIONS
            )
            st = stability_metric(stability_rows)
            print(f"stability on {names[0]}: {st.unchanged} of case x rewording "
                  f"unchanged, {st.unchanged_all} of cases never moved "
                  f"({time.time() - started:.0f}s)")
    finally:
        await jev.aclose()

    if not args.no_baselines:
        for b in ("always_astra_medium", "always_glm", "rules"):
            results[f"baseline:{b}"] = await baseline_records(b, cases)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = allocate_results(EVALS_DIR / "results", stamp)
    with (out_dir / "per_case.jsonl").open("w") as fh:
        for recs in results.values():
            for r in recs:
                fh.write(json.dumps(r.to_json(), ensure_ascii=False, default=str) + "\n")
    if stability_rows:
        (out_dir / "stability.json").write_text(
            json.dumps(stability_rows, indent=2, default=str)
        )
    write_summary(
        out_dir,
        all_variants,
        results,
        args.repeats,
        jev.calls,
        args.jev_model,
        stability_rows=stability_rows,
        control=args.control if args.control != "none" else "",
        packet=args.packet,
    )
    write_manifest(
        out_dir,
        build_manifest(
            script="evals/run_eval.py",
            kind=run_kind(jev.calls, len(cases) * len(names) * args.repeats),
            jev_model=args.jev_model,
            variants={
                name: variant_versions(
                    all_variants[name].config(),
                    all_variants[name].alias,
                    all_variants[name].questions,
                    all_variants[name].state_builder,
                )
                for name in names
            },
            candidates=profile_versions(config_with_overlay()),
            cases=case_manifest(cases),
            seeds={
                "split": SPLIT_SEED,
                "shuffled_labels": 4242,
                "stability": "seeded per perturbation, see evals/metrics.py",
            },
            repeats=args.repeats,
            cache={
                "jev": "disabled" if args.no_cache else "enabled",
                "hits": jev.cache.hits,
                "live_calls": jev.calls,
            },
            caps={
                "draft_max_calls": args.draft_max_calls if args.draft else 0,
                "concurrency": args.concurrency,
            },
            # Nothing polls quota here: the policy replay takes pressure as an
            # argument, and these runs pass none.
            quota={"coverage": "not read; every case ran at pressure zero"},
            extra={
                "control": args.control,
                "packet_comparison": bool(args.packet),
                "slice_holdout": args.slice_holdout or "",
            },
            unfinished=[
                {"variant": name, "case": r.case.id, "error": r.error}
                for name, recs in results.items()
                for r in recs
                if r.error
            ],
        ),
    )

    publish_latest(out_dir)

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
    p.add_argument("--slice", help="comma separated slices to keep")
    p.add_argument("--slice-holdout", help="tune on every slice but this one, report on it")
    p.add_argument("--public", help="extra case files, for example evals/cases_public.yaml")
    p.add_argument("--stability", action="store_true",
                   help="also re-decide every case under seeded rewordings")
    p.add_argument("--draft", action="store_true",
                   help="fetch a cheap draft per case first, for a variant whose "
                        "alias turns `draft` on. This is the only part of this "
                        "script that spends upstream quota")
    p.add_argument("--draft-subset", type=int, default=80,
                   help="how many cases the draft run covers, stratified by slice")
    p.add_argument("--draft-max-calls", type=int, default=100,
                   help="hard cap on upstream calls for the draft step")
    p.add_argument("--control", default="none", choices=CONTROLS,
                   help="run a control that is supposed to score badly")
    p.add_argument("--packet", action="store_true",
                   help="run the four packet arms of spec 13.4 and add the "
                        "comparison table to the summary")
    p.add_argument("--no-baselines", action="store_true")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
