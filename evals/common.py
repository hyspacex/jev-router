"""Shared pieces for the eval scripts: cases, labels, caching, API clients.

Nothing here talks to the router's HTTP server. The scripts build features and
state with the router's own code, so what is measured is what ships.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent
CACHE_DIR = EVALS_DIR / ".cache"

sys.path.insert(0, str(ROOT / "src"))

from jev_router.config import RouterConfig  # noqa: E402
from jev_router.features import Features, extract_features  # noqa: E402

# --- labels -------------------------------------------------------------

# The ten labels used in cases.yaml.
CASE_TASKS = [
    "code-edit",
    "debugging",
    "agentic-tool-task",
    "design-or-review",
    "quick-question",
    "summarise-or-extract",
    "high-stakes-writing",
    "creative-prose",
    "everyday-polish",
    "other",
]

# cases.yaml label -> the seven categories router.yaml ships with.
TEN_TO_SEVEN = {
    "code-edit": "code",
    "debugging": "debugging",
    "agentic-tool-task": "agentic_tool_task",
    "design-or-review": "analysis",
    "quick-question": "quick_question",
    "summarise-or-extract": "writing",
    "high-stakes-writing": "writing",
    "creative-prose": "writing",
    "everyday-polish": "writing",
    "other": "other",
}

# A ten-category question answers in cases.yaml's own names, so mapping down to
# the seven is the same table.
TEN_CAT_TO_SEVEN = dict(TEN_TO_SEVEN)

# `mid` was added on 2026-09-19 after the admission test. Tiers are ordered:
# a route below every acceptable tier is under-routed and one above every
# acceptable tier is over-routed.
TIERS = ("fast", "mid", "frontier")

# Every case carries one of these. They are shapes of request, not task labels:
# a case has exactly one shape and any of the ten task labels.
SLICES = (
    "chat",          # one person typing into a chat box
    "agentic",       # tool schemas, tool calls, tool results
    "multiturn",     # four or more turns of back and forth
    "longcontext",   # 20k characters or more
    "multilingual",  # the request is not in English
    "adversarial",   # something in the request argues for its own route
    "pipeline",      # a program calling the API with a fixed instruction
)

# Relative cost per route, cheapest first. Used to order routes and to score
# "cheapest adequate". These are quota units, not currency: see EXPERIMENTS.md.
ROUTE_COST = {
    ("ollama/glm-5.3-flash", "none"): 1.0,
    ("ollama/glm-5.3-flash", "low"): 1.4,
    ("ollama/glm-5.3-flash", "high"): 2.2,
    ("ollama/gemma4-31b", "none"): 1.0,
    ("ollama/glm-5.3", "none"): 2.5,
    ("gpt-5.6-luna", "low"): 4.0,
    ("grok-4.6", "low"): 10.0,
    ("gpt-6-astra", "low"): 10.0,
    ("gpt-6-astra", "medium"): 14.0,
    ("gpt-6-astra", "high"): 20.0,
    ("gpt-6-astra", "xhigh"): 30.0,
    ("gpt-6-astra", "max"): 45.0,
}

ROUTE_ORDER = [
    ("ollama/glm-5.3-flash", "none"),
    ("ollama/glm-5.3-flash", "low"),
    ("ollama/glm-5.3-flash", "high"),
    ("gpt-6-astra", "low"),
    ("gpt-6-astra", "medium"),
    ("gpt-6-astra", "high"),
    ("gpt-6-astra", "xhigh"),
]

EFFORT_ORDER = ["none", "low", "medium", "high", "xhigh", "max"]


def route_cost(model: str, effort: str | None) -> float:
    return ROUTE_COST.get((model, effort or "none"), 14.0)


# Which tier a routed model belongs to. Anything not listed is frontier.
MODEL_TIERS = {
    "ollama/glm-5.3-flash": "fast",
    "ollama/gemma4-31b": "fast",
    "ollama/glm-5.3": "mid",
    "gpt-5.6-luna": "mid",
}


def tier_of(model: str) -> str:
    return MODEL_TIERS.get(model.split("(")[0], "frontier")


def tier_rank(tier: str) -> int:
    return TIERS.index(tier)


def widen_for_mid(acceptable: list[str], *, difficulty: float, needs_faithfulness: bool,
                  slice_name: str, label_source: str | None) -> list[str]:
    """Where a hand label may also accept the mid tier.

    Only the admission cases carry a measured `mid` label. For the rest, mid is
    accepted when both neighbours are, or when the case looks like the ones the
    mid model was measured on: frontier-labelled, difficulty 2 or lower, no
    faithfulness requirement, and no tool loop. Outcome labels are left alone.
    """
    if "mid" in acceptable or label_source == "outcome":
        return list(acceptable)
    if "fast" in acceptable and "frontier" in acceptable:
        return [*acceptable, "mid"]
    if ("frontier" in acceptable and difficulty <= 2 and not needs_faithfulness
            and slice_name != "agentic"):
        return [*acceptable, "mid"]
    return list(acceptable)


def is_under_routed(model: str, acceptable: list[str]) -> bool:
    return tier_rank(tier_of(model)) < min(tier_rank(t) for t in acceptable)


def is_over_routed(model: str, acceptable: list[str]) -> bool:
    return tier_rank(tier_of(model)) > max(tier_rank(t) for t in acceptable)


def effort_rank(effort: str | None) -> int:
    if effort is None:
        return 0
    try:
        return EFFORT_ORDER.index(effort)
    except ValueError:
        return 0


# --- cases --------------------------------------------------------------

LANGUAGE_MARKERS = {
    "chinese": "zh",
    "spanish": "es",
    "german": "de",
    "microcuento": "es",
}


@dataclass
class Case:
    id: str
    notes: str
    body: dict[str, Any]
    expected: dict[str, Any]
    shape: str
    language: str
    multi_turn: bool
    adversarial: bool
    trap: bool
    slice: str = "chat"
    attack: str = ""          # adversarial cases only: the attacker's goal
    attack_vector: str = ""   # adversarial cases only: how it was delivered
    source: str = "hand"      # hand, or the public dataset the case came from
    label_source: str = "hand"
    split: str = "tune"
    # Labels for the atomic shadow questions, on the subset that carries them.
    # A case without them is skipped by the per-question scorers; nothing in
    # `expected` is ever restated here, so adding one cannot move an existing
    # number (invariant I14).
    labels: dict[str, Any] = field(default_factory=dict)

    @property
    def labelled(self) -> bool:
        """Public cases arrive without labels, so every scorer skips them."""
        return bool(self.expected)

    @property
    def task(self) -> str:
        return self.expected["task"]

    @property
    def task7(self) -> str:
        return TEN_TO_SEVEN[self.expected["task"]]

    @property
    def difficulty(self) -> int:
        return int(self.expected["difficulty"])

    @property
    def tolerance(self) -> int:
        return int(self.expected["difficulty_tolerance"])

    @property
    def needs_faithfulness(self) -> bool:
        return bool(self.expected["needs_faithfulness"])

    @property
    def tier(self) -> str:
        return self.expected["tier"]

    @property
    def effort(self) -> str:
        return self.expected["effort"]

    @property
    def labelled_tiers(self) -> list[str]:
        """`acceptable_tiers` exactly as written in cases.yaml."""
        return list(self.expected["acceptable_tiers"])

    @property
    def acceptable_tiers(self) -> list[str]:
        return widen_for_mid(
            self.labelled_tiers,
            difficulty=self.difficulty,
            needs_faithfulness=self.needs_faithfulness,
            slice_name=self.slice,
            label_source=self.label_source,
        )

    def features(self) -> Features:
        body = copy.deepcopy(self.body)
        body.setdefault("model", "auto")
        return extract_features(body)


def _shape_of(body: dict[str, Any]) -> str:
    """Which client envelope this request came in: the four shapes in the set."""
    system = ""
    for m in body.get("messages") or []:
        if m.get("role") in ("system", "developer"):
            c = m.get("content")
            if isinstance(c, str):
                system = c
            break
    if not system:
        return "bare"
    if "terminal coding agent" in system:
        return "terminal-agent"
    if "Follow the instruction exactly" in system:
        return "pipeline"
    return "chat-ui"


def _language_of(case_id: str, body: dict[str, Any]) -> str:
    low = case_id.lower()
    for marker, lang in LANGUAGE_MARKERS.items():
        if marker in low:
            return lang
    return "en"


def _multi_turn(body: dict[str, Any]) -> bool:
    roles = [m.get("role") for m in body.get("messages") or []]
    return any(r in ("assistant", "tool") for r in roles)


LONG_CONTEXT_CHARS = 20000


def total_chars(body: dict[str, Any]) -> int:
    blob = json.dumps(body.get("messages") or [], ensure_ascii=False)
    return len(blob)


def infer_slice(case_id: str, body: dict[str, Any], notes: str) -> str:
    """The shape a case would get if nobody wrote one down.

    Used to backfill and to check what is in the file. Precedence runs from the
    most specific shape to the least: a Chinese adversarial paste is filed under
    `adversarial`, because that is what the case is testing.
    """
    if "ADVERSARIAL" in notes.upper():
        return "adversarial"
    if total_chars(body) >= LONG_CONTEXT_CHARS:
        return "longcontext"
    if _language_of(case_id, body) != "en":
        return "multilingual"
    shape = _shape_of(body)
    if shape == "pipeline":
        return "pipeline"
    messages = body.get("messages") or []
    roles = [m.get("role") for m in messages]
    if body.get("tools") or "tool" in roles or "function" in roles:
        return "agentic"
    if shape == "terminal-agent":
        return "agentic"
    # Three non-system messages is the shortest conversation that has a turn
    # before the latest one, which is the thing this slice tests.
    if len([r for r in roles if r != "system"]) >= 3 and "assistant" in roles:
        return "multiturn"
    return "chat"


def load_cases(
    path: Path | None = None, extra: list[Path] | None = None
) -> list[Case]:
    """Every case, with splits assigned.

    `extra` names further YAML files in the same schema, for example the
    git-ignored `cases_public.yaml`. Their cases keep their own `source`, and
    an unlabelled case is carried through with an empty `expected` so the
    scorers can skip it while the router still runs on it.
    """
    path = path or EVALS_DIR / "cases.yaml"
    cases: list[Case] = []
    for file in [path] + list(extra or []):
        raw = yaml.safe_load(file.read_text()) or {}
        labels = _labels_beside(file)
        for item in raw.get("cases") or []:
            if not item.get("expected") and item["id"] in labels:
                item = {**item, **labels[item["id"]]}
            cases.append(_one_case(item))
    assign_splits(cases)
    return cases


def _labels_beside(file: Path) -> dict[str, dict[str, Any]]:
    """Labels for a case file that ships without them.

    `import_public.py` writes the requests and the label stubs separately, so
    the requests can be regenerated from the datasets without losing the
    labelling work. A missing labels file is normal: the cases then load
    unlabelled, the router still runs on them, and every scorer skips them.
    """
    sidecar = file.with_name(file.stem + "_labels" + file.suffix)
    if not sidecar.exists():
        return {}
    raw = yaml.safe_load(sidecar.read_text()) or {}
    out: dict[str, dict[str, Any]] = {}
    for item in raw.get("cases") or []:
        expected = item.get("expected") or {}
        if expected and all(v is not None for v in expected.values()):
            out[item["id"]] = {
                "expected": expected,
                "label_source": item.get("label_source", "llm-assisted"),
            }
    return out


def _one_case(item: dict[str, Any]) -> Case:
    if "generated" in item:
        import generators  # local import: only the long-context cases need it

        body = generators.build(item["generated"])
    else:
        body = item["request"]
    notes = " ".join(str(item.get("notes", "")).split())
    upper = notes.upper()
    case_id = item["id"]
    return Case(
        id=case_id,
        notes=notes,
        body=body,
        expected=item.get("expected") or {},
        shape=_shape_of(body),
        language=item.get("language") or _language_of(case_id, body),
        multi_turn=_multi_turn(body),
        adversarial="ADVERSARIAL" in upper or item.get("slice") == "adversarial",
        trap=any(w in upper for w in ("ADVERSARIAL", "MISLEADING", "HARD CASE")),
        slice=item.get("slice") or infer_slice(case_id, body, notes),
        attack=item.get("attack", ""),
        attack_vector=item.get("attack_vector", ""),
        source=item.get("source", "hand"),
        label_source=item.get("label_source", "hand"),
        labels=dict(item.get("labels") or {}),
    )


SPLIT_SEED = 20260918
TUNE_FRACTION = 0.70


def assign_splits(cases: list[Case], seed: int = SPLIT_SEED) -> None:
    """70/30 tune/held-out, stratified by task label so both halves cover all ten.

    The seed and the fraction are fixed so the split survives new cases being
    added: a case's split can still move when the group it sits in grows, but
    the procedure does not change, and the numbers stay comparable run to run.
    Unlabelled cases have no task to stratify on and are never tuned against.
    """
    by_task: dict[str, list[Case]] = {}
    for c in cases:
        if not c.labelled:
            c.split = "unlabelled"
            continue
        if c.source != "hand":
            # Sampled public traffic is the out-of-distribution slice. It is
            # reported on its own and never tuned against: its labels are
            # llm-assisted, and mixing them into the tuning split would fit the
            # policy to another model's opinion.
            c.split = "ood"
            continue
        by_task.setdefault(c.task, []).append(c)
    rng = random.Random(seed)
    for task in sorted(by_task):
        group = sorted(by_task[task], key=lambda c: c.id)
        rng.shuffle(group)
        n_tune = max(1, round(len(group) * TUNE_FRACTION))
        for i, case in enumerate(group):
            case.split = "tune" if i < n_tune else "held_out"


def assign_slice_holdout(cases: list[Case], held: str) -> int:
    """Hold out one whole slice instead of 30% of the rows.

    A row split inside a slice leaves near-duplicates of a held-out case in the
    tuning half, which flatters the held-out number. Removing the slice whole
    answers a different and harder question: does a policy tuned without this
    shape of request work on it? Returns the size of the held-out slice.
    """
    n = 0
    for c in cases:
        if not c.labelled:
            c.split = "unlabelled"
        elif c.slice == held:
            c.split = "held_out"
            n += 1
        else:
            c.split = "tune"
    return n


# --- controls -----------------------------------------------------------
#
# Two runs that must fail. If either one scores well, the evaluation is
# measuring something other than what it claims to.


def shuffle_labels(cases: list[Case], seed: int = 4242) -> list[Case]:
    """Every case keeps its request and gets another case's labels.

    Use a uniform permutation, matching the report's permutation null.
    Freeze derived acceptable tiers on the donor: recipient request metadata
    must not change the meaning of the shuffled label.
    """
    labelled = [c for c in cases if c.labelled]
    rng = random.Random(seed)
    order = list(range(len(labelled)))
    rng.shuffle(order)
    expected = []
    for c in labelled:
        label = copy.deepcopy(c.expected)
        label["acceptable_tiers"] = c.acceptable_tiers
        expected.append(label)
    out = []
    for c in cases:
        clone = copy.copy(c)
        if c.labelled:
            clone.expected = expected[order[labelled.index(c)]]
            # Prevent re-widening the donor's already resolved tier set.
            clone.label_source = "outcome"
        out.append(clone)
    return out


CONSTANT_STATE_TEXT = (
    "Please take a look at this and let me know what you think when you get a "
    "chance. I would like to move forward with it."
)


def constant_state_body() -> dict[str, Any]:
    """One request every case is replaced by, for the constant-state control.

    Deliberately bland: no code, no tools, no claim about difficulty. With the
    same state for every case the router cannot do better than always picking
    the commoner tier, so routing accuracy must fall to the majority-class rate.
    """
    return {
        "messages": [{"role": "user", "content": CONSTANT_STATE_TEXT}],
        "stream": False,
    }


# --- config variants ----------------------------------------------------


def deep_merge(base: Any, overlay: Any) -> Any:
    """Dict-into-dict merge. Lists and scalars in the overlay replace."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(overlay)


def questions_to_replace(overlay: dict[str, Any] | None) -> list[str]:
    """Question ids in an overlay that define their own `criteria`.

    Those replace the shipped question outright. Merging two `criteria` maps
    would leave both sets of category names in the question.
    """
    return [
        qid
        for qid, q in ((overlay or {}).get("questions") or {}).items()
        if isinstance(q, dict) and "criteria" in q
    ]


def config_with_overlay(
    overlay: dict[str, Any] | None = None,
    path: Path | None = None,
    replace_questions: list[str] | None = None,
) -> RouterConfig:
    """router.yaml with an overlay merged in.

    Question ids named in `replace_questions` are taken from the overlay
    whole, instead of being merged field by field. Merging a `criteria` map
    would leave the old category names in place beside the new ones.
    """
    path = path or ROOT / "router.yaml"
    raw = yaml.safe_load(path.read_text())
    for qid in replace_questions or []:
        (raw.get("questions") or {}).pop(qid, None)
    merged = deep_merge(raw, overlay or {})
    cfg = RouterConfig.model_validate(merged)
    cfg._config_hash = hashlib.sha256(
        json.dumps(merged, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    cfg._config_path = str(path)
    return cfg


# --- disk cache ---------------------------------------------------------


class DiskCache:
    def __init__(self, name: str, enabled: bool = True) -> None:
        self.dir = CACHE_DIR / name
        self.enabled = enabled
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(payload: Any) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        f = self.dir / f"{key}.json"
        if not f.exists():
            return None
        try:
            self.hits += 1
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None

    def put(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        tmp = self.dir / f"{key}.json.tmp"
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        tmp.replace(self.dir / f"{key}.json")


# --- Jev client ---------------------------------------------------------


class JevError(RuntimeError):
    pass


@dataclass
class JevResult:
    answers: dict[str, Any]
    latency_ms: float
    input_tokens: int
    cached: bool


class JevClient:
    """Calls the real API. Backs off on 429 and 529, keeps concurrency low."""

    URL = "https://api.typesafe.ai/v1/systemone"

    def __init__(
        self,
        model: str = "jev-1.13.0",
        concurrency: int = 8,
        cache: bool = True,
        timeout_s: float = 30.0,
    ) -> None:
        key = os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            raise JevError(
                "TYPESAFE_API_KEY is not set. Export it, or put it in a .env file "
                "and load that, before running the eval scripts. See .env.example."
            )
        self._key = key
        self.model = model
        self.cache = DiskCache("jev", enabled=cache)
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self.calls = 0
        self.input_tokens = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ask(
        self, state: Any, questions: dict[str, Any], use_cache: bool = True
    ) -> JevResult:
        payload = {"model": self.model, "state": state, "questions": questions}
        ckey = DiskCache.key(payload)
        if use_cache:
            hit = self.cache.get(ckey)
            if hit is not None:
                return JevResult(
                    answers=hit["answers"],
                    latency_ms=hit.get("latency_ms", 0.0),
                    input_tokens=hit.get("input_tokens", 0),
                    cached=True,
                )

        delay = 1.0
        last: Exception | None = None
        for attempt in range(6):
            async with self._sem:
                started = time.perf_counter()
                try:
                    resp = await self._client.post(
                        self.URL,
                        json=payload,
                        headers={"Authorization": f"Bearer {self._key}"},
                    )
                except httpx.HTTPError as exc:  # network wobble
                    last = exc
                    resp = None
            if resp is not None:
                if resp.status_code in (429, 529, 500, 502, 503):
                    last = JevError(f"jev returned {resp.status_code}")
                elif resp.status_code >= 400:
                    raise JevError(f"jev returned {resp.status_code}: {resp.text[:300]}")
                else:
                    elapsed = (time.perf_counter() - started) * 1000
                    data = resp.json()
                    usage = data.get("usage") or {}
                    self.calls += 1
                    self.input_tokens += int(usage.get("input_tokens") or 0)
                    record = {
                        "answers": data.get("answers") or {},
                        "latency_ms": elapsed,
                        "input_tokens": int(usage.get("input_tokens") or 0),
                    }
                    if use_cache:
                        self.cache.put(ckey, record)
                    return JevResult(
                        answers=record["answers"],
                        latency_ms=elapsed,
                        input_tokens=record["input_tokens"],
                        cached=False,
                    )
            await asyncio.sleep(delay + random.random() * 0.4)
            delay = min(delay * 2, 20.0)
        raise JevError(f"jev failed after retries: {last}")


# --- upstream (proxy) client -------------------------------------------


DEFAULT_UPSTREAM = "http://127.0.0.1:8317"


def upstream_base_url() -> str:
    """Where the proxy that serves the candidate models is listening."""
    return os.environ.get("JEV_ROUTER_UPSTREAM", "").strip() or DEFAULT_UPSTREAM


def upstream_key() -> str:
    """The bearer token the proxy expects. Only the outcome benchmark needs it."""
    key = os.environ.get("UPSTREAM_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "UPSTREAM_API_KEY is not set. It is the key your OpenAI-compatible "
            "proxy expects, and only evals/run_outcomes.py needs it. Set "
            "JEV_ROUTER_UPSTREAM too if the proxy is not at "
            f"{DEFAULT_UPSTREAM}. See .env.example."
        )
    return key


@dataclass
class ChatResult:
    text: str
    latency_ms: float
    output_tokens: int
    prompt_tokens: int
    cached: bool
    error: str | None = None
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        """The reply stopped at the token cap instead of ending.

        This is the number that says whether a "more effort did not help"
        finding was about effort or about a cap cutting the reasoning off.
        """
        return self.finish_reason == "length"


class Budget:
    """A spending limit and a health check on a shared upstream.

    The proxy this runs against serves subscription quota. It degrades under
    sustained load rather than refusing: latency climbs and replies come back
    empty. So the run watches for both and stops itself, leaving the disk cache
    in place so the next run picks up where this one left off.
    """

    def __init__(
        self,
        max_calls: int,
        latency_multiple: float = 3.0,
        warmup: int = 12,
        max_empty: int = 2,
    ) -> None:
        self.max_calls = max_calls
        self.latency_multiple = latency_multiple
        self.warmup = warmup
        self.max_empty = max_empty
        self.spent = 0
        self.empty = 0
        self._latencies: list[float] = []
        self._by_route: dict[str, list[float]] = {}
        self.stop_reason: str | None = None

    def may_spend(self) -> bool:
        if self.stop_reason:
            return False
        if self.spent >= self.max_calls:
            self.stop_reason = f"budget spent: {self.spent} of {self.max_calls} calls"
            return False
        return True

    def note(self, latency_ms: float, text: str, route: str = "") -> None:
        """Record one live call.

        Latency is tracked per route, not over the whole run. A pooled median
        rises whenever the run moves from a cheap route to an expensive one or
        from a short prompt to a long one, which is a change in the work, not
        in the upstream. Comparing a route against its own earlier self is the
        only comparison that means the upstream got slower.
        """
        self.spent += 1
        if not text.strip():
            self.empty += 1
            if self.empty >= self.max_empty:
                self.stop_reason = (
                    f"{self.empty} empty replies: the upstream is unwell, stopping"
                )
            return
        self._latencies.append(latency_ms)
        seen = self._by_route.setdefault(route, [])
        seen.append(latency_ms)
        if len(seen) <= 2 * self.warmup:
            return
        early = sorted(seen[: self.warmup])
        recent = sorted(seen[-self.warmup :])
        baseline = early[len(early) // 2]
        now = recent[len(recent) // 2]
        if baseline > 0 and now > self.latency_multiple * baseline:
            self.stop_reason = (
                f"{route or 'upstream'} median latency went from "
                f"{baseline / 1000:.1f}s to {now / 1000:.1f}s, more than "
                f"{self.latency_multiple:g}x, stopping"
            )

    def line(self) -> str:
        med = median(self._latencies) / 1000 if self._latencies else 0.0
        return (
            f"upstream calls {self.spent}/{self.max_calls}, empty {self.empty}, "
            f"median latency {med:.1f}s"
            + (f", STOPPED: {self.stop_reason}" if self.stop_reason else "")
        )


class UpstreamClient:
    """Chat completions through the proxy. Everything is cached on disk."""

    def __init__(
        self,
        base_url: str | None = None,
        concurrency: int = 4,
        cache: bool = True,
        timeout_s: float = 900.0,
        budget: Budget | None = None,
    ) -> None:
        base_url = base_url or upstream_base_url()
        self._key = upstream_key()
        self.url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self.cache = DiskCache("upstream", enabled=cache)
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self.calls = 0
        self.budget = budget
        self.skipped = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 8000,
        temperature: float | None = None,
        cache_salt: str = "",
    ) -> ChatResult:
        """One completion, cached on disk by the request.

        `cache_salt` goes into the cache key and not into the request, so k
        samples of the same request are k separate entries holding k different
        replies to the same bytes on the wire. Without it the second sample
        would read the first one back off disk and every sample would agree
        with itself, which would make the resample spread look like zero.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        ckey = DiskCache.key({**body, "_sample": cache_salt} if cache_salt else body)
        hit = self.cache.get(ckey)
        if hit is not None:
            return ChatResult(
                text=hit["text"],
                latency_ms=hit.get("latency_ms", 0.0),
                output_tokens=hit.get("output_tokens", 0),
                prompt_tokens=hit.get("prompt_tokens", 0),
                cached=True,
                error=hit.get("error"),
                finish_reason=hit.get("finish_reason", ""),
            )
        if self.budget is not None and not self.budget.may_spend():
            self.skipped += 1
            return ChatResult("", 0.0, 0, 0, False, f"not run: {self.budget.stop_reason}")

        delay = 2.0
        last = ""
        for attempt in range(4):
            async with self._sem:
                started = time.perf_counter()
                try:
                    resp = await self._client.post(
                        self.url,
                        json=body,
                        headers={"Authorization": f"Bearer {self._key}"},
                    )
                except httpx.HTTPError as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    resp = None
            if resp is not None:
                if resp.status_code in (429, 500, 502, 503, 529):
                    last = f"status {resp.status_code}"
                elif resp.status_code >= 400:
                    return ChatResult("", 0.0, 0, 0, False, f"status {resp.status_code}: {resp.text[:200]}")
                else:
                    elapsed = (time.perf_counter() - started) * 1000
                    data = resp.json()
                    choice = (data.get("choices") or [{}])[0]
                    text = (choice.get("message") or {}).get("content") or ""
                    usage = data.get("usage") or {}
                    self.calls += 1
                    if self.budget is not None:
                        self.budget.note(elapsed, text, route=model)
                    record = {
                        "text": text,
                        "latency_ms": elapsed,
                        "output_tokens": int(usage.get("completion_tokens") or 0),
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "finish_reason": str(choice.get("finish_reason") or ""),
                        "error": None if text.strip() else "empty reply",
                    }
                    self.cache.put(ckey, record)
                    return ChatResult(
                        text=record["text"],
                        latency_ms=elapsed,
                        output_tokens=record["output_tokens"],
                        prompt_tokens=record["prompt_tokens"],
                        cached=False,
                        error=record["error"],
                        finish_reason=record["finish_reason"],
                    )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        return ChatResult("", 0.0, 0, 0, False, f"gave up: {last}")


# --- small stats --------------------------------------------------------


def pct(numer: float, denom: float) -> float:
    return 100.0 * numer / denom if denom else 0.0


def percentile(values: Iterable[float], q: float) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def median(values: Iterable[float]) -> float:
    return percentile(values, 0.5)


def estimate_tokens(state: Any) -> int:
    blob = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    return len(blob) // 4
