"""State builders that only exist to be measured.

Importing this module registers them, so `evals/run_eval.py` can name them in
variants.yaml. A builder that wins moves into src/jev_router/state.py; the
rest stay here as the record of what was tried.
"""

from __future__ import annotations

import re
from typing import Any

from jev_router.features import Features
from jev_router.state import (
    PASTE_START,
    _common_facts,
    _earlier_request,
    CONTINUATION,
    continuation_aware_v1,
    register,
    split_request_and_material,
    squeeze,
)

# Lines worth keeping when a pasted block has to be cut down. Only
# code_aware_v1 below uses this, and code_aware_v1 lost.
SIGNAL_LINE = re.compile(
    r"(?:^\s*(?:def|class|func|fn|struct|impl|interface|type|public|private|async def)\s)"
    r"|(?:Traceback|Exception|panic:|FAIL|FAILED|ERROR|Error:|error:|assert|"
    r"deadlock|timeout|TimeoutError|WARN|fatal|SyntaxError|undefined|null pointer)",
)


# --- builders that lost, kept so the comparison can be re-run -------------
@register("request_thread_v1")
def request_thread_v1(features: Features, config: Any) -> dict[str, Any]:
    """The latest user message with the earlier one always beside it.

    The plain version of the continuation idea: no detection, both messages
    every time. Measured against continuation_aware_v1 to show whether the
    detection earns its keep.
    """
    last, last_facts = squeeze(features.last_user_message, 4000)
    state: dict[str, Any] = {"latest_user_message": last}
    earlier = _earlier_request(features)
    if earlier:
        state["earlier_user_message"] = squeeze(earlier, 1500)[0]
    state["facts"] = _common_facts(features) | last_facts
    return state



def keep_signal_lines(text: str, limit: int) -> tuple[str, dict[str, int]]:
    """Cut a pasted block to `limit` characters, keeping head, tail and the
    lines that carry the answer: definitions, errors, panics, failed asserts."""
    text, facts = squeeze(text, 10_000_000)  # only strips encoded blobs
    if len(text) <= limit:
        return text, facts

    lines = text.splitlines()
    facts["original_characters"] = len(text)
    facts["original_lines"] = len(lines)

    head_n, tail_n = 12, 12
    keep: set[int] = set(range(min(head_n, len(lines))))
    keep |= set(range(max(0, len(lines) - tail_n), len(lines)))
    budget = limit - sum(len(lines[i]) + 1 for i in keep)
    for i, line in enumerate(lines):
        if i in keep or not SIGNAL_LINE.search(line):
            continue
        if budget - len(line) - 1 < 0:
            break
        keep.add(i)
        budget -= len(line) + 1

    out: list[str] = []
    previous = -1
    for i in sorted(keep):
        if previous >= 0 and i > previous + 1:
            out.append(f"[... {i - previous - 1} lines not shown ...]")
        out.append(lines[i])
        previous = i
    if previous < len(lines) - 1:
        out.append(f"[... {len(lines) - 1 - previous} lines not shown ...]")
    return "\n".join(out)[: limit + 200], facts



@register("labelled_v1")
def labelled_v1(features: Features, config: Any) -> dict[str, Any]:
    """summary_v1, except pasted bulk is moved into a field named as quoted
    material. Isolates the naming effect from everything else."""
    request, material = split_request_and_material(features.last_user_message)
    request, req_facts = squeeze(request, 4000)
    state: dict[str, Any] = {"latest_user_request": request}
    if material:
        state["material_the_user_pasted"] = squeeze(material, 2500)[0]
    state["facts"] = _common_facts(features) | req_facts
    if features.system_prompt.strip():
        state["start_of_system_prompt"] = features.system_prompt[:400]
    return state


@register("code_aware_v1")
def code_aware_v1(features: Features, config: Any) -> dict[str, Any]:
    """continuation_aware_v1, with the paste cut down by keeping the lines that
    carry the answer instead of the head and the tail."""
    state = continuation_aware_v1(features, config)
    request, material = split_request_and_material(features.last_user_message)
    if material:
        kept, facts = keep_signal_lines(material, 2000)
        state["material_the_user_pasted"] = kept
        if facts.get("original_lines"):
            state["facts"]["pasted_material_lines"] = facts["original_lines"]
    return state



@register("x_facts_first")
def x_facts_first(features: Features, config: Any) -> dict[str, Any]:
    """The same fields as continuation_aware_v1, computed facts first."""
    base = continuation_aware_v1(features, config)
    out: dict[str, Any] = {"facts": base["facts"]}
    for key, value in base.items():
        if key != "facts":
            out[key] = value
    return out


@register("x_no_facts")
def x_no_facts(features: Features, config: Any) -> dict[str, Any]:
    """continuation_aware_v1 without the computed facts block."""
    base = continuation_aware_v1(features, config)
    return {k: v for k, v in base.items() if k != "facts"}


@register("x_no_system_prompt")
def x_no_system_prompt(features: Features, config: Any) -> dict[str, Any]:
    """continuation_aware_v1 without the head of the system prompt."""
    base = continuation_aware_v1(features, config)
    return {k: v for k, v in base.items() if k != "start_of_system_prompt"}


@register("x_one_string")
def x_one_string(features: Features, config: Any) -> str:
    """Every part of continuation_aware_v1 flattened into one labelled string."""
    base = continuation_aware_v1(features, config)
    parts: list[str] = []
    for key, value in base.items():
        if key == "facts":
            facts = ", ".join(f"{k}={v}" for k, v in value.items())
            parts.append(f"FACTS: {facts}")
        else:
            parts.append(f"{key.replace('_', ' ').upper()}:\n{value}")
    return "\n\n".join(parts)


@register("x_squeeze_only")
def x_squeeze_only(features: Features, config: Any) -> dict[str, Any]:
    """continuation_aware_v1 with the paste folded back into the request, to
    show what the separate `material_the_user_pasted` field is worth."""
    request, material = split_request_and_material(features.last_user_message)
    whole = request if not material else f"{request}\n\n{material}"
    text, facts = squeeze(whole, 5000)
    state: dict[str, Any] = {"latest_user_request": text}
    earlier = _earlier_request(features)
    short_now = len(features.last_user_message.strip())
    if earlier and (
        CONTINUATION.match(features.last_user_message.strip())
        or short_now < min(200, len(earlier.strip()))
    ):
        state["earlier_request_in_this_conversation"] = squeeze(earlier, 2000)[0]
    state["facts"] = _common_facts(features) | facts
    if features.system_prompt.strip():
        state["start_of_system_prompt"] = features.system_prompt[:400]
    return state


# --- experiment 12: language and domain as computed facts ----------------
#
# Both are computed in code, with no model call. Jev cannot count and should
# not be asked to guess a language from a sample it can already see, but the
# questions and their criteria are written in English, and the eval set shows
# non-English requests scoring lower. Naming the language is one way to see
# whether that gap is Jev not recognising the language or the wording simply
# not carrying over.

# Script ranges that identify a language on their own.
SCRIPTS = (
    ("ja", ("぀", "ヿ")),   # hiragana and katakana
    ("ko", ("가", "힯")),   # hangul
    ("zh", ("一", "鿿")),   # han, after the kana check
    ("ru", ("Ѐ", "ӿ")),   # cyrillic
    ("ar", ("؀", "ۿ")),   # arabic
    ("el", ("Ͱ", "Ͽ")),   # greek
    ("he", ("֐", "׿")),   # hebrew
)

# Function words for the Latin-script languages in the case set. Function
# words, not content words, because they survive a technical topic.
STOPWORDS: dict[str, frozenset[str]] = {
    "es": frozenset("el la los las de que y en un una por con para pero como no se lo está cuando donde".split()),
    "pt": frozenset("o a os as de que e em um uma por com para mas como não se está quando onde muito".split()),
    "fr": frozenset("le la les de que et en un une par avec pour mais comme ne se est quand où très".split()),
    "de": frozenset("der die das und ist ich nicht mit für auf eine einen dem den aber wie wenn wo sehr".split()),
    "it": frozenset("il lo la i gli le di che e in un una per con ma come non si quando dove molto".split()),
    "nl": frozenset("de het een en is niet met voor op maar hoe als waar zeer dat die ik je".split()),
    "en": frozenset("the a an and is to of that it for with on but how if where very this i you".split()),
}

DOMAIN_WORDS: dict[str, tuple[str, ...]] = {
    "medical": ("patient", "dose", "dosage", "mg", "diagnosis", "discharge", "clinical",
                "symptom", "prescription", "contraindicated", "trial", "placebo"),
    "legal": ("clause", "indemnity", "liability", "contract", "counterparty", "jurisdiction",
              "regulator", "statute", "breach", "warranty", "gdpr", "compliance"),
    "finance": ("invoice", "ledger", "revenue", "tax", "vat", "payroll", "refund",
                "reconcile", "fx", "pension", "mortgage", "amortis"),
    "infrastructure": ("kubernetes", "terraform", "docker", "compose", "deploy", "nginx",
                       "systemd", "cron", "helm", "ingress", "tls", "dns"),
    "data": ("sql", "select ", "postgres", "query plan", "index", "dataframe", "etl",
             "schema", "partition", "warehouse", "csv"),
    "software": ("function", "class ", "def ", "import ", "refactor", "compile", "traceback",
                 "stack trace", "unit test", "pytest", "typescript", "rust", "golang"),
}


def detect_language(text: str) -> str:
    """A two-letter guess at the language of `text`, or "en".

    Script first, because a script settles it. Then function-word counts over
    the first few hundred words, because a technical request in any language is
    mostly English nouns and the function words are what is left.
    """
    sample = text[:4000]
    if not sample.strip():
        return "en"
    for code, (lo, hi) in SCRIPTS:
        hits = sum(1 for ch in sample if lo <= ch <= hi)
        if hits >= max(4, len(sample) * 0.02):
            return code
    words = [w.strip(".,;:!?()[]{}\"'`").lower() for w in sample.split()][:400]
    if not words:
        return "en"
    scores = {
        code: sum(1 for w in words if w in stops) for code, stops in STOPWORDS.items()
    }
    best = max(scores, key=lambda c: (scores[c], c == "en"))
    return best if scores[best] >= 3 else "en"


def detect_domain(text: str) -> str:
    """The subject area, from fixed keyword lists. "general" when unsure."""
    low = text[:6000].lower()
    scores = {
        name: sum(low.count(w) for w in words) for name, words in DOMAIN_WORDS.items()
    }
    best = max(scores, key=lambda n: (scores[n], n))
    return best if scores[best] >= 2 else "general"


@register("x_lang_domain")
def x_lang_domain(features: Features, config: Any) -> dict[str, Any]:
    """continuation_aware_v1 with two more computed facts: language and domain."""
    state = continuation_aware_v1(features, config)
    whole = features.last_user_message
    state["facts"]["request_language"] = detect_language(whole)
    state["facts"]["subject_area"] = detect_domain(whole)
    return state


@register("x_draft")
def x_draft(features: Features, config: Any) -> dict[str, Any]:
    """continuation_aware_v1. The draft field is added by the caller.

    Registered under its own name so a run using the draft is visible in the
    results as a different builder, even though the code is the same.
    """
    return continuation_aware_v1(features, config)
