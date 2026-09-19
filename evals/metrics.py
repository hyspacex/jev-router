"""Metrics shared by the eval scripts.

Everything here is pure: no network, no clock, no files. The eval scripts pass
in lists of outcomes and get numbers back, so the same definitions serve
`run_eval.py`, `run_outcomes.py` and the unit tests.

Four groups:

  - interval arithmetic (Wilson) and what a given case count can detect
  - calibration: expected calibration error over Jev's confidences
  - baselines: the cost-matched random router from RouterBench
  - outcome scores: Gap@Oracle and Gain@BestSingle
  - decision stability: seeded perturbations of a user message

The perturbations are deliberately dumb string edits. They cost nothing, they
are reproducible from a seed, and they are the kind of noise a real user
produces. No model is asked to rewrite anything.
"""

from __future__ import annotations

import math
import random
import re
import statistics
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

Z95 = 1.959963984540054


# --- intervals ----------------------------------------------------------


@dataclass(frozen=True)
class Rate:
    """A proportion with a Wilson 95% interval, all three in percent."""

    k: int
    n: int

    @property
    def pct(self) -> float:
        return 100.0 * self.k / self.n if self.n else 0.0

    @property
    def lo(self) -> float:
        return wilson(self.k, self.n)[0] * 100.0

    @property
    def hi(self) -> float:
        return wilson(self.k, self.n)[1] * 100.0

    @property
    def half_width(self) -> float:
        return (self.hi - self.lo) / 2.0

    def __str__(self) -> str:
        if not self.n:
            return "-"
        return f"{self.pct:.1f} [{self.lo:.1f}, {self.hi:.1f}]"

    def cell(self) -> str:
        """A markdown table cell: the rate then the interval."""
        return str(self)


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials, as fractions.

    The Wilson interval, not the normal approximation, because the rates here
    sit near 0 and near 1 where the normal approximation runs off the end of
    the scale. See "Adding Error Bars to Evals" (arXiv 2411.00640).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def rate(values: Iterable[Any]) -> Rate:
    """A Rate over an iterable of truthy/falsy values. `None` is skipped."""
    vals = [v for v in values if v is not None]
    return Rate(sum(1 for v in vals if v), len(vals))


def min_detectable_difference(
    n: int, p: float = 0.85, power: float = 0.80, alpha: float = 0.05
) -> float:
    """Roughly the smallest true difference two runs of `n` cases can show.

    Two-proportion normal approximation, both arms the same size, baseline rate
    `p`. Returned in percentage points. It is a planning number, not a test:
    read it as "a change smaller than this will not separate from noise".
    """
    if n <= 0:
        return 100.0
    z_a = 1.959963984540054 if alpha == 0.05 else abs(_z_for(alpha / 2))
    z_b = 0.8416212335729143 if power == 0.80 else abs(_z_for(1 - power))
    return 100.0 * (z_a + z_b) * math.sqrt(2 * p * (1 - p) / n)


def _z_for(tail: float) -> float:
    """Inverse normal CDF, good to about 4 decimals. Only used off the
    default alpha and power, so speed does not matter."""
    if tail <= 0 or tail >= 1:
        raise ValueError("tail must be in (0, 1)")
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < tail:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def paired_bootstrap_p(
    a: Sequence[bool], b: Sequence[bool], draws: int = 2000, seed: int = 11
) -> float:
    """Two-sided p for "a and b differ", resampling the same cases.

    Both sequences are per-case outcomes for the same cases in the same order,
    which is how two variants are compared here. Paired, because the cases are
    the same and the case-to-case variance is the thing worth removing.
    """
    if len(a) != len(b) or not a:
        return 1.0
    diffs = [int(x) - int(y) for x, y in zip(a, b)]
    observed = statistics.fmean(diffs)
    centred = [d - observed for d in diffs]
    rng = random.Random(seed)
    n = len(centred)
    hits = 0
    for _ in range(draws):
        sample = statistics.fmean(centred[rng.randrange(n)] for _ in range(n))
        if abs(sample) >= abs(observed):
            hits += 1
    return (hits + 1) / (draws + 1)


# --- calibration --------------------------------------------------------

DEFAULT_BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 1.01))


@dataclass(frozen=True)
class Calibration:
    ece: float          # expected calibration error, 0 to 1
    mce: float          # the worst single bin
    mean_confidence: float
    accuracy: float
    n: int
    bins: tuple[dict[str, Any], ...] = ()

    @property
    def overconfident_by(self) -> float:
        """Positive when Jev claims more than it delivers."""
        return self.mean_confidence - self.accuracy


def calibration(
    pairs: Iterable[tuple[float | None, bool | None]],
    bins: Sequence[tuple[float, float]] = DEFAULT_BINS,
) -> Calibration:
    """Expected calibration error over (confidence, was_correct) pairs.

    ECE is the bin-count-weighted mean gap between the mean confidence in a bin
    and the accuracy in that bin. One number per question, which is what the
    confidence gate needs: the old per-bin table said where the miscalibration
    was but not how big it was.
    """
    clean = [(c, bool(ok)) for c, ok in pairs if c is not None and ok is not None]
    if not clean:
        return Calibration(0.0, 0.0, 0.0, 0.0, 0)
    total = len(clean)
    ece = 0.0
    mce = 0.0
    rows: list[dict[str, Any]] = []
    for lo, hi in bins:
        bucket = [(c, ok) for c, ok in clean if lo <= c < hi]
        if not bucket:
            continue
        conf = statistics.fmean(c for c, _ in bucket)
        acc = statistics.fmean(1.0 if ok else 0.0 for _, ok in bucket)
        gap = abs(conf - acc)
        ece += len(bucket) / total * gap
        mce = max(mce, gap)
        rows.append(
            {
                "bucket": f"{lo:.2f}-{hi:.2f}",
                "n": len(bucket),
                "mean_confidence": conf,
                "accuracy": 100.0 * acc,
                "gap": 100.0 * (conf - acc),
            }
        )
    return Calibration(
        ece=ece,
        mce=mce,
        mean_confidence=statistics.fmean(c for c, _ in clean),
        accuracy=statistics.fmean(1.0 if ok else 0.0 for _, ok in clean),
        n=total,
        bins=tuple(rows),
    )


def accuracy_below_confidence(
    pairs: Iterable[tuple[float | None, bool | None]], threshold: float
) -> tuple[Rate, Rate]:
    """Accuracy below and at-or-above a confidence threshold.

    A confidence gate only earns its place if the answers it rejects really are
    worse than the ones it keeps. This is the number that says so.
    """
    clean = [(c, bool(ok)) for c, ok in pairs if c is not None and ok is not None]
    below = [ok for c, ok in clean if c < threshold]
    above = [ok for c, ok in clean if c >= threshold]
    return rate(below), rate(above)


# --- baselines ----------------------------------------------------------


@dataclass(frozen=True)
class RandomBaseline:
    """RouterBench's Zero Router: the same spend, allocated at random."""

    frontier_rate: float
    tier_correct: float
    tier_correct_sd: float
    under_routed: float
    over_routed: float
    mean_cost: float
    draws: int


def cost_matched_random(
    acceptable: Sequence[Sequence[str]],
    router_routes: Sequence[tuple[str, str | None]],
    cost_of: Callable[[str, str | None], float],
    tier_of: Callable[[str], str],
    draws: int = 20,
    seed: int = 20260919,
) -> RandomBaseline:
    """Route to the frontier tier at the router's own frontier rate.

    RouterBench calls this the Zero Router: it spends what the router spends but
    chooses at random, so it separates "the router picked well" from "the router
    happened to spend more". The effort within each tier is drawn from the
    router's own empirical effort mix, which is what keeps the cost matched
    rather than merely the tier rate.
    """
    n = len(acceptable)
    if not n:
        return RandomBaseline(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, draws)

    frontier_routes = [r for r in router_routes if tier_of(r[0]) == "frontier"]
    fast_routes = [r for r in router_routes if tier_of(r[0]) == "fast"]
    p_frontier = len(frontier_routes) / n
    # A router that never used a tier still needs something to draw when the
    # coin lands there, so fall back to the cheapest route seen on that side.
    if not frontier_routes:
        frontier_routes = [("gpt-6-astra", "medium")]
    if not fast_routes:
        fast_routes = [("ollama/glm-5.3-flash", "none")]

    rng = random.Random(seed)
    correct_runs: list[float] = []
    under_runs: list[float] = []
    over_runs: list[float] = []
    cost_runs: list[float] = []
    for _ in range(draws):
        correct = under = over = 0
        cost = 0.0
        for tiers in acceptable:
            if rng.random() < p_frontier:
                model, effort = frontier_routes[rng.randrange(len(frontier_routes))]
            else:
                model, effort = fast_routes[rng.randrange(len(fast_routes))]
            t = tier_of(model)
            cost += cost_of(model, effort)
            if t in tiers:
                correct += 1
            elif t == "fast":
                under += 1
            else:
                over += 1
        correct_runs.append(100.0 * correct / n)
        under_runs.append(100.0 * under / n)
        over_runs.append(100.0 * over / n)
        cost_runs.append(cost / n)
    return RandomBaseline(
        frontier_rate=100.0 * p_frontier,
        tier_correct=statistics.fmean(correct_runs),
        tier_correct_sd=statistics.pstdev(correct_runs) if draws > 1 else 0.0,
        under_routed=statistics.fmean(under_runs),
        over_routed=statistics.fmean(over_runs),
        mean_cost=statistics.fmean(cost_runs),
        draws=draws,
    )


# --- outcome-backed scores ----------------------------------------------


@dataclass(frozen=True)
class OutcomeScores:
    n: int
    gap_at_oracle: float        # 0 is perfect, 1 is worthless
    gain_at_best_single: float  # positive means the router beat the best fixed route
    best_single_route: str
    mean_cost: float
    oracle_mean_cost: float
    best_single_mean_cost: float
    latency_p50: float
    latency_p95: float


def outcome_scores(
    per_case: Sequence[dict[str, Any]],
    chosen_route: Callable[[dict[str, Any]], str | None],
    cost_of_route: Callable[[str], float],
    floor: float = 1.0,
) -> OutcomeScores | None:
    """Gap@Oracle, Gain@BestSingle, cost and latency at the chosen route.

    `per_case` rows carry a `routes` list of {route, score, latency_ms, cost}.
    `chosen_route` says which route the router would have picked for that case.
    Rows where the router's route was not measured are skipped, and the count
    that survived is reported so a small n is visible.

    `floor` keeps the ratios finite. A case where the oracle itself scored 0 is
    a case where nothing worked, and dividing by it would make the router look
    arbitrarily bad or good depending on rounding.
    """
    gaps: list[float] = []
    gains: list[float] = []
    costs: list[float] = []
    oracle_costs: list[float] = []
    latencies: list[float] = []

    by_route_scores: dict[str, list[float]] = {}
    for row in per_case:
        for cell in row.get("routes", []):
            by_route_scores.setdefault(cell["route"], []).append(cell["score"])
    if not by_route_scores:
        return None
    complete = {r: s for r, s in by_route_scores.items() if len(s) == len(per_case)}
    pool = complete or by_route_scores
    best_single = max(pool, key=lambda r: statistics.fmean(pool[r]))

    best_single_costs: list[float] = []
    for row in per_case:
        cells = {c["route"]: c for c in row.get("routes", [])}
        want = chosen_route(row)
        if want is None or want not in cells:
            continue
        chosen = cells[want]
        oracle = max(cells.values(), key=lambda c: c["score"])
        gaps.append(1.0 - chosen["score"] / max(oracle["score"], floor))
        costs.append(chosen.get("cost", 0.0))
        oracle_costs.append(oracle.get("cost", 0.0))
        latencies.append(chosen.get("latency_ms", 0.0))
        if best_single in cells:
            gains.append(
                chosen["score"] / max(cells[best_single]["score"], floor) - 1.0
            )
            best_single_costs.append(cells[best_single].get("cost", 0.0))
    if not gaps:
        return None
    return OutcomeScores(
        n=len(gaps),
        gap_at_oracle=statistics.fmean(gaps),
        gain_at_best_single=statistics.fmean(gains) if gains else 0.0,
        best_single_route=best_single,
        mean_cost=statistics.fmean(costs),
        oracle_mean_cost=statistics.fmean(oracle_costs),
        best_single_mean_cost=(
            statistics.fmean(best_single_costs) if best_single_costs else 0.0
        ),
        latency_p50=_percentile(latencies, 0.5),
        latency_p95=_percentile(latencies, 0.95),
    )


def _percentile(values: Sequence[float], q: float) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] * (1 - (pos - lo)) + vals[hi] * (pos - lo)


# --- decision stability under perturbation ------------------------------

# A small fixed table. Swaps are one way only, so a perturbed message never
# turns back into the original, and every swap keeps the meaning.
SYNONYMS: tuple[tuple[str, str], ...] = (
    ("fix", "sort out"),
    ("broken", "not working"),
    ("quickly", "fast"),
    ("write", "put together"),
    ("check", "look at"),
    ("error", "failure"),
    ("issue", "problem"),
    ("change", "modify"),
    ("explain", "walk me through"),
    ("review", "go over"),
    ("help me", "give me a hand with"),
    ("figure out", "work out"),
    ("make sure", "confirm"),
    ("remove", "delete"),
    ("add", "put in"),
)

FILLER_PREFIXES = (
    "Hi, hope you're well. ",
    "Quick one if you have a moment. ",
    "Sorry to bother you. ",
)

FILLER_SUFFIXES = (
    "\n\nThanks so much, really appreciate it.",
    "\n\nNo rush at all.",
    "\n\nCheers.",
)

PERTURBATIONS = ("typos", "casing", "filler", "synonyms")


def perturb(text: str, kind: str, seed: int) -> str:
    """One deterministic variant of a user message.

    The same (text, kind, seed) always gives the same output, so a stability
    number is reproducible without storing the variants.
    """
    if not text.strip():
        return text
    rng = random.Random(f"{kind}:{seed}:{len(text)}")
    if kind == "typos":
        return _typos(text, rng)
    if kind == "casing":
        return _casing(text, rng)
    if kind == "filler":
        prefix = FILLER_PREFIXES[rng.randrange(len(FILLER_PREFIXES))]
        suffix = FILLER_SUFFIXES[rng.randrange(len(FILLER_SUFFIXES))]
        return prefix + text + suffix
    if kind == "synonyms":
        return _synonyms(text, rng)
    raise ValueError(f"unknown perturbation {kind!r} (known: {', '.join(PERTURBATIONS)})")


def _first_line_span(text: str) -> tuple[int, int]:
    """Where the prose is. Edits stay out of anything that looks pasted.

    Only the opening paragraph is touched, because a typo inserted into a stack
    trace or a code block is not a typo a user would make, and it would change
    what the request means rather than how it is worded.
    """
    end = text.find("\n\n")
    return (0, len(text) if end < 0 else end)


def _typos(text: str, rng: random.Random) -> str:
    start, end = _first_line_span(text)
    head, tail = text[start:end], text[end:]
    words = head.split(" ")
    targets = [i for i, w in enumerate(words) if len(w) >= 5 and w.isalpha()]
    if not targets:
        return text
    for i in rng.sample(targets, min(2, len(targets))):
        w = words[i]
        j = rng.randrange(1, len(w) - 1)
        # Swap two neighbouring letters: the commonest real typing slip.
        words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
    return " ".join(words) + tail


def _casing(text: str, rng: random.Random) -> str:
    start, end = _first_line_span(text)
    head, tail = text[start:end], text[end:]
    head = head.lower()
    head = head.replace(".", "").replace(",", "").replace("?", "")
    return head + tail


def _synonyms(text: str, rng: random.Random) -> str:
    start, end = _first_line_span(text)
    head, tail = text[start:end], text[end:]
    swapped = 0
    for old, new in SYNONYMS:
        if swapped >= 3:
            break
        pattern = re.compile(rf"\b{re.escape(old)}\b", re.IGNORECASE)
        if pattern.search(head):
            head = pattern.sub(new, head, count=1)
            swapped += 1
    return head + tail


@dataclass(frozen=True)
class Stability:
    n_cases: int
    n_variants: int
    unchanged: Rate           # over case x variant
    unchanged_all: Rate       # cases where every variant kept the route
    flipped: tuple[str, ...]  # case ids that moved, worst first
    detail: tuple[dict[str, Any], ...] = ()


def stability(
    rows: Sequence[dict[str, Any]],
) -> Stability:
    """How often a reworded request keeps its (model, effort).

    Each row is {"case_id", "base": (model, effort), "variants": {kind: (model,
    effort)}}. Both numbers are reported because they answer different
    questions: the first is how often a single reworded turn moves, the second
    is how many requests are fragile at all.
    """
    per_variant: list[bool] = []
    per_case: list[bool] = []
    flipped: list[tuple[int, str]] = []
    detail: list[dict[str, Any]] = []
    n_variants = 0
    for row in rows:
        base = tuple(row["base"])
        variants = row.get("variants") or {}
        n_variants = max(n_variants, len(variants))
        moves = {k: tuple(v) for k, v in variants.items() if tuple(v) != base}
        per_variant.extend(tuple(v) == base for v in variants.values())
        per_case.append(not moves)
        if moves:
            flipped.append((len(moves), row["case_id"]))
            detail.append(
                {
                    "case_id": row["case_id"],
                    "base": f"{base[0]}({base[1]})",
                    "moves": {
                        k: f"{v[0]}({v[1]})" for k, v in sorted(moves.items())
                    },
                }
            )
    flipped.sort(key=lambda t: (-t[0], t[1]))
    return Stability(
        n_cases=len(rows),
        n_variants=n_variants,
        unchanged=rate(per_variant),
        unchanged_all=rate(per_case),
        flipped=tuple(cid for _, cid in flipped),
        detail=tuple(detail),
    )


# --- controls -----------------------------------------------------------


def majority_class_rate(acceptable: Sequence[Sequence[str]]) -> float:
    """The best a constant router can do: always pick the commoner answer.

    The constant-state control has to fall to roughly this. If it does not, the
    routing score was reading something other than the state.
    """
    if not acceptable:
        return 0.0
    fast = sum(1 for tiers in acceptable if "fast" in tiers)
    frontier = sum(1 for tiers in acceptable if "frontier" in tiers)
    return 100.0 * max(fast, frontier) / len(acceptable)


def chance_rate(labels: Sequence[Any]) -> float:
    """Accuracy a shuffled-label run should land near.

    For a shuffled permutation of the same labels the expected match rate is
    the sum of squared label frequencies, not 1/k: the label mix is uneven and
    a common label matches itself by accident more often.
    """
    if not labels:
        return 0.0
    counts: dict[Any, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    n = len(labels)
    return 100.0 * sum((c / n) ** 2 for c in counts.values())
