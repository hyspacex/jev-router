#!/usr/bin/env python
"""Sample public datasets into evals/cases_public.yaml.

The shipped cases.yaml is hand written. This script adds unlabelled cases from
public datasets that allow redistribution, so the eval set covers shapes real
traffic has and synthetic cases miss. The output file is git-ignored: public
conversation dumps still carry personal data, so nothing sampled here is
republished from this repo.

    uv sync --group evals
    uv run python evals/import_public.py --limit 5
    uv run python evals/import_public.py --source wildchat --source mgsm
    uv run python evals/import_public.py --limit 100 --max-chars 40000

Sources, each optional and each skipped with one line if it is gated or the
download fails:

    wildchat   allenai/WildChat-1M (ODC-BY), WildChat, arXiv 2405.01470
    arena      lmsys/arena-human-preference-55k (Apache-2.0)
    bfcl       gorilla-llm/Berkeley-Function-Calling-Leaderboard (Apache-2.0)
    longbench  THUDM/LongBench-v2 (Apache-2.0)
    mgsm       juletxara/mgsm (CC-BY-4.0)

WildChat is stratified so the sample keeps the shape of the source. It has no
topic or category column, so the stratum is a computed proxy: first-turn length
bucket, whether the first turn holds code, and a turn-count bucket. If a topic
column ever appears the script prefers it, see TOPIC_FIELDS.

This script never writes an `expected:` block. It writes a companion stub file,
evals/cases_public_labels.yaml, with the label keys present and empty for a
human or a later script to fill in. Re-running merges stubs for new ids only.

Nothing from a sampled row is printed. The text may hold personal data, so the
output is counts and case ids.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import re
import sys
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

import yaml

EVALS_DIR = Path(__file__).resolve().parent

DEFAULT_OUT = EVALS_DIR / "cases_public.yaml"
DEFAULT_SEED = 20260919
DEFAULT_LIMIT = 40
DEFAULT_MAX_CHARS = 60000

# A request longer than this is a long-context case.
LONGCONTEXT_CHARS = 20000

# Four or more messages counts as a multi-turn case.
MULTITURN_MESSAGES = 4

# A moderation score at or above this drops the row. Datasets that publish
# scores instead of a flag, such as Detoxify, are read with this.
TOXICITY_THRESHOLD = 0.5

SOURCES = ["wildchat", "arena", "bfcl", "longbench", "mgsm"]

DATASETS = {
    "wildchat": "allenai/WildChat-1M",
    "arena": "lmsys/arena-human-preference-55k",
    "bfcl": "gorilla-llm/Berkeley-Function-Calling-Leaderboard",
    "longbench": "THUDM/LongBench-v2",
    "mgsm": "juletxara/mgsm",
}

CREDITS = {
    "wildchat": "WildChat-1M (ODC-BY; WildChat, arXiv 2405.01470)",
    "arena": "arena-human-preference-55k (Apache-2.0)",
    "bfcl": "Berkeley Function Calling Leaderboard (Apache-2.0)",
    "longbench": "LongBench-v2 (Apache-2.0)",
    "mgsm": "MGSM (CC-BY-4.0)",
}

# Column names that would carry a topic signal. WildChat-1M has none of them
# today, so the proxy stratum below is what actually runs.
TOPIC_FIELDS = ("topic", "topic_label", "category", "cluster", "intent")


# --- filters ------------------------------------------------------------

# Contact details and account numbers. Anything that matches is dropped, not
# masked: a false positive costs one row, a miss costs a person.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[ .\-]?)?(?:\(\d{2,4}\)[ .\-]?|\d{2,4}[ .\-])\d{3,4}[ .\-]\d{3,4}(?![\w.])"
)

IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")

# Candidate card numbers. The Luhn check below decides, which keeps version
# strings and long ids out of the count.
CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")

CODE_MARKERS = ("```", "def ", "class ", "import ", "function ", "SELECT ", "#include")

# Dataset language columns give names, not codes.
LANGUAGE_CODES = {
    "english": "en",
    "chinese": "zh",
    "mandarin": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "spanish": "es",
    "portuguese": "pt",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "dutch": "nl",
    "russian": "ru",
    "ukrainian": "uk",
    "polish": "pl",
    "czech": "cs",
    "turkish": "tr",
    "arabic": "ar",
    "persian": "fa",
    "hebrew": "he",
    "hindi": "hi",
    "bengali": "bn",
    "telugu": "te",
    "tamil": "ta",
    "urdu": "ur",
    "japanese": "ja",
    "korean": "ko",
    "vietnamese": "vi",
    "thai": "th",
    "indonesian": "id",
    "malay": "ms",
    "swahili": "sw",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "greek": "el",
    "romanian": "ro",
    "hungarian": "hu",
    "bulgarian": "bg",
    "serbian": "sr",
    "croatian": "hr",
    "catalan": "ca",
    "filipino": "tl",
    "tagalog": "tl",
    "estonian": "et",
    "latvian": "lv",
    "lithuanian": "lt",
    "slovak": "sk",
    "slovenian": "sl",
    "albanian": "sq",
    "macedonian": "mk",
    "bosnian": "bs",
    "belarusian": "be",
    "icelandic": "is",
    "irish": "ga",
    "welsh": "cy",
    "basque": "eu",
    "galician": "gl",
    "afrikaans": "af",
    "sotho": "st",
    "sesotho": "st",
    "tswana": "tn",
    "xhosa": "xh",
    "zulu": "zu",
    "shona": "sn",
    "somali": "so",
    "amharic": "am",
    "hausa": "ha",
    "igbo": "ig",
    "yoruba": "yo",
    "maori": "mi",
    "samoan": "sm",
    "javanese": "jv",
    "sundanese": "su",
    "cebuano": "ceb",
    "nepali": "ne",
    "sinhala": "si",
    "khmer": "km",
    "lao": "lo",
    "burmese": "my",
    "marathi": "mr",
    "gujarati": "gu",
    "kannada": "kn",
    "malayalam": "ml",
    "punjabi": "pa",
    "azerbaijani": "az",
    "kazakh": "kk",
    "uzbek": "uz",
    "mongolian": "mn",
    "georgian": "ka",
    "armenian": "hy",
    "pashto": "ps",
    "kurdish": "ku",
    "esperanto": "eo",
    "latin": "la",
    "maltese": "mt",
}

# What a dataset writes when it could not tell. These stay out of the
# multilingual slice instead of being guessed at.
UNKNOWN_LANGUAGES = {"nolang", "unknown", "und", "unk", "none", "n/a", "other"}


@dataclass
class Row:
    """One candidate case, before filtering and before it becomes YAML."""

    source: str
    messages: list[dict[str, Any]]
    language: str = "en"
    tools: list[dict[str, Any]] = field(default_factory=list)
    stratum: str = "all"
    flagged: bool = False
    detail: str = ""


@dataclass
class Counts:
    """What each filter removed, per source."""

    seen: int = 0
    toxic: int = 0
    pii: int = 0
    empty: int = 0
    kept: int = 0
    truncated: int = 0

    def line(self, source: str, sampled: int) -> str:
        return (
            f"{source:10s} scanned {self.seen:5d}  dropped toxic {self.toxic:4d}  "
            f"pii {self.pii:4d}  empty {self.empty:4d}  kept {self.kept:5d}  "
            f"sampled {sampled:4d}  truncated {self.truncated:3d}"
        )


class SourceUnavailable(RuntimeError):
    """The dataset is gated, needs a login, or would not download."""


# --- small helpers ------------------------------------------------------


def language_code(name: Any) -> str:
    """Map a dataset's language name to a short code. Unknown stays 'und'."""
    if not isinstance(name, str) or not name.strip():
        return "und"
    key = name.strip().lower()
    if key in UNKNOWN_LANGUAGES:
        return "und"
    if key in LANGUAGE_CODES:
        return LANGUAGE_CODES[key]
    if len(key) in (2, 3) and key.isalpha():
        return key
    return "und"


def luhn_ok(digits: str) -> bool:
    """The card checksum. Used to keep long ids out of the card filter."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def has_pii(text: str) -> bool:
    if EMAIL_RE.search(text) or PHONE_RE.search(text) or IBAN_RE.search(text):
        return True
    for match in CARD_RE.finditer(text):
        digits = re.sub(r"[ \-]", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            return True
    return False


def text_of(content: Any) -> tuple[str, bool]:
    """Return a message's text and whether it carried a non-text part."""
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        parts: list[str] = []
        had_other = False
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") in ("text", "input_text") and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                else:
                    had_other = True
        return "\n".join(parts), had_other
    if content is None:
        return "", False
    return str(content), False


def normalise_messages(raw: Any) -> tuple[list[dict[str, Any]], bool]:
    """Flatten a dataset's turns into chat-completions messages."""
    out: list[dict[str, Any]] = []
    had_other = False
    for turn in raw or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role") or "user"
        text, other = text_of(turn.get("content"))
        had_other = had_other or other
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        msg: dict[str, Any] = {"role": role, "content": text}
        if turn.get("tool_calls"):
            msg["tool_calls"] = turn["tool_calls"]
        if turn.get("tool_call_id"):
            msg["tool_call_id"] = turn["tool_call_id"]
        out.append(msg)
    return out, had_other


def _entry_flagged(entry: Any) -> bool:
    """One moderation record: a flag, a category, or a score over the line."""
    if not isinstance(entry, dict):
        return False
    if entry.get("flagged") is True:
        return True
    cats = entry.get("categories")
    if isinstance(cats, dict) and any(v is True for v in cats.values()):
        return True
    # Detoxify records are bare score maps, so the threshold decides.
    scores = [
        v for v in entry.values() if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    return bool(scores) and max(scores) >= TOXICITY_THRESHOLD


def moderation_flagged(raw: dict[str, Any]) -> bool:
    """True if the dataset's own moderation fields flag this row."""
    if raw.get("toxic") is True or raw.get("redacted_toxic") is True:
        return True
    for key in ("openai_moderation", "detoxify_moderation", "moderation"):
        value = raw.get(key)
        entries = value if isinstance(value, list) else [value]
        if any(_entry_flagged(entry) for entry in entries):
            return True
    for turn in raw.get("conversation") or []:
        if isinstance(turn, dict) and turn.get("toxic") is True:
            return True
    return False


def request_chars(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    total = sum(len(m.get("content") or "") for m in messages)
    if tools:
        total += len(json.dumps(tools, ensure_ascii=False))
    return total


def slice_of(messages: list[dict[str, Any]], tools: list[dict[str, Any]], language: str) -> str:
    """Pick the slice from the row. First rule that fits wins."""
    if tools or any(m.get("role") == "tool" or m.get("tool_calls") for m in messages):
        return "agentic"
    if request_chars(messages, tools) > LONGCONTEXT_CHARS:
        return "longcontext"
    # "und" means the dataset could not tell, so it is not called multilingual.
    if language not in ("en", "und"):
        return "multilingual"
    if len(messages) >= MULTITURN_MESSAGES:
        return "multiturn"
    return "chat"


def length_bucket(n: int) -> str:
    if n < 400:
        return "s"
    if n < 2000:
        return "m"
    return "l"


def turn_bucket(n: int) -> str:
    if n <= 1:
        return "t1"
    if n <= 3:
        return "t3"
    return "t4"


def proxy_stratum(messages: list[dict[str, Any]]) -> str:
    """The stand-in for a topic column: length, code, turn count."""
    first = messages[0].get("content") if messages else ""
    first = first or ""
    code = "code" if any(marker in first for marker in CODE_MARKERS) else "prose"
    return f"{length_bucket(len(first))}-{code}-{turn_bucket(len(messages))}"


# --- datasets ----------------------------------------------------------


def import_datasets() -> Any:
    """Import `datasets` late, so --help works without it."""
    try:
        import datasets  # noqa: PLC0415
    except ImportError as exc:
        raise SourceUnavailable(
            "the datasets package is missing. Install it with: uv sync --group evals"
        ) from exc
    try:
        datasets.logging.set_verbosity_error()
        datasets.disable_progress_bars()
    except Exception:  # older versions, nothing important
        pass
    return datasets


def short_reason(exc: BaseException) -> str:
    """One short line about a failure. Never more than the first line."""
    text = " ".join(str(exc).split())
    if not text:
        text = type(exc).__name__
    return text[:200]


def skip_line(source: str, reason: str) -> str:
    """One line per skipped source. A login wall is reported, never worked around."""
    lowered = reason.lower()
    if "401" in lowered or "403" in lowered or "cannot be accessed" in lowered or "gated" in lowered:
        reason += " (it needs a Hub login or accepted terms, so it was left alone)"
    return f"skipped {source} ({DATASETS[source]}): {reason}"


def stream_rows(name: str, config: str | None, splits: tuple[str, ...], scan: int) -> list[dict]:
    """Read up to `scan` rows. Streaming keeps the download small."""
    datasets = import_datasets()
    last: Exception | None = None
    for split in splits:
        try:
            ds = datasets.load_dataset(name, config, split=split, streaming=True)
            return [dict(row) for row in islice(iter(ds), scan)]
        except Exception as exc:  # gated, missing split, network, parse
            last = exc
    raise SourceUnavailable(short_reason(last) if last else "no usable split")


def collect_wildchat(scan: int) -> list[Row]:
    rows: list[Row] = []
    for raw in stream_rows(DATASETS["wildchat"], None, ("train",), scan):
        messages, had_other = normalise_messages(raw.get("conversation"))
        if had_other and not any((m.get("content") or "").strip() for m in messages):
            messages = []
        language = language_code(raw.get("language"))
        topic = ""
        for key in TOPIC_FIELDS:
            if isinstance(raw.get(key), str) and raw[key].strip():
                topic = raw[key].strip().lower()
                break
        stratum = topic or proxy_stratum(messages)
        rows.append(
            Row(
                source="wildchat",
                messages=messages,
                language=language,
                stratum=stratum,
                flagged=moderation_flagged(raw),
            )
        )
    return rows


def collect_arena(scan: int) -> list[Row]:
    """Prompt and response turns, ending on the user so the case is answerable."""
    rows: list[Row] = []
    for raw in stream_rows(DATASETS["arena"], None, ("train", "test"), scan):
        prompts = _json_list(raw.get("prompt"))
        replies = _json_list(raw.get("response_a"))
        messages: list[dict[str, Any]] = []
        for i, prompt in enumerate(prompts):
            messages.append({"role": "user", "content": str(prompt or "")})
            if i < len(prompts) - 1 and i < len(replies) and replies[i]:
                messages.append({"role": "assistant", "content": str(replies[i])})
        rows.append(
            Row(
                source="arena",
                messages=messages,
                language="en",
                stratum=proxy_stratum(messages),
                flagged=moderation_flagged(raw),
            )
        )
    return rows


def _json_list(value: Any) -> list[Any]:
    """Arena stores turn lists as a JSON string."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return []


# The dataset publishes one file per category instead of named configs, so the
# files are named here and each one becomes a stratum.
BFCL_FILES = (
    "BFCL_v3_simple.json",
    "BFCL_v3_multiple.json",
    "BFCL_v3_parallel.json",
    "BFCL_v3_parallel_multiple.json",
    "BFCL_v3_live_simple.json",
    "BFCL_v3_live_multiple.json",
    "BFCL_v3_live_parallel_multiple.json",
)


def collect_bfcl(scan: int) -> list[Row]:
    """Tool-calling shapes: a user turn plus the function schemas it may call."""
    datasets = import_datasets()
    per_file = max(1, scan // len(BFCL_FILES))
    rows: list[Row] = []
    errors: list[str] = []
    for filename in BFCL_FILES:
        url = f"hf://datasets/{DATASETS['bfcl']}/{filename}"
        try:
            ds = datasets.load_dataset("json", data_files=url, split="train", streaming=True)
            raws = [dict(row) for row in islice(iter(ds), per_file)]
        except Exception as exc:  # gated, missing file, network, parse
            errors.append(short_reason(exc))
            continue
        category = filename.removeprefix("BFCL_v3_").removesuffix(".json")
        for raw in raws:
            tools = _bfcl_tools(raw.get("function"))
            if not tools:
                continue  # without schemas the row is not a tool-calling shape
            rows.append(
                Row(
                    source="bfcl",
                    messages=_bfcl_messages(raw.get("question")),
                    language="en",
                    tools=tools,
                    stratum=category,
                    detail=f"category {category}",
                )
            )
    if not rows:
        raise SourceUnavailable(errors[0] if errors else "no rows in any category file")
    return rows


def _bfcl_messages(question: Any) -> list[dict[str, Any]]:
    if isinstance(question, str):
        return [{"role": "user", "content": question}]
    if isinstance(question, list) and question:
        first = question[0]
        if isinstance(first, list):
            messages, _ = normalise_messages(first)
            return messages
        if isinstance(first, dict):
            messages, _ = normalise_messages(question)
            return messages
        return [{"role": "user", "content": "\n".join(str(q) for q in question)}]
    return []


def _bfcl_tools(functions: Any) -> list[dict[str, Any]]:
    if isinstance(functions, str):
        functions = _json_list(functions)
    tools: list[dict[str, Any]] = []
    for fn in functions or []:
        if isinstance(fn, str):
            try:
                fn = json.loads(fn)
            except ValueError:
                continue
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                params = None
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(fn["name"]),
                    "description": str(fn.get("description") or ""),
                    "parameters": params or {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def collect_longbench(scan: int) -> list[Row]:
    rows: list[Row] = []
    for raw in stream_rows(DATASETS["longbench"], None, ("train", "test", "validation"), scan):
        context = str(raw.get("context") or "")
        question = str(raw.get("question") or "")
        choices = [
            f"{letter}. {raw.get('choice_' + letter)}"
            for letter in ("A", "B", "C", "D")
            if raw.get("choice_" + letter)
        ]
        body = context
        if question:
            body = f"{body}\n\nQuestion: {question}" if body else question
        if choices:
            body = body + "\n\n" + "\n".join(choices)
        language = language_code(raw.get("language") or "english")
        domain = str(raw.get("domain") or "unknown")
        rows.append(
            Row(
                source="longbench",
                messages=[{"role": "user", "content": body}],
                language=language,
                stratum=domain,
                detail=f"domain {domain}",
            )
        )
    return rows


MGSM_LANGUAGES = ("en", "es", "fr", "de", "ru", "zh", "ja", "th", "sw", "bn", "te")


def collect_mgsm(scan: int) -> list[Row]:
    """One config per language, so the sample covers all of them."""
    datasets = import_datasets()
    try:
        available = list(datasets.get_dataset_config_names(DATASETS["mgsm"]))
    except Exception as exc:
        raise SourceUnavailable(short_reason(exc)) from exc

    wanted = [c for c in MGSM_LANGUAGES if c in available] or sorted(available)
    per_config = max(1, scan // max(1, len(wanted)))
    rows: list[Row] = []
    errors: list[str] = []
    for config in wanted:
        try:
            raws = stream_rows(DATASETS["mgsm"], config, ("test", "train"), per_config)
        except SourceUnavailable as exc:
            errors.append(str(exc))
            continue
        for raw in raws:
            question = str(raw.get("question") or "")
            rows.append(
                Row(
                    source="mgsm",
                    messages=[{"role": "user", "content": question}],
                    language=language_code(config),
                    stratum=config,
                )
            )
    if not rows:
        raise SourceUnavailable(errors[0] if errors else "no rows in any config")
    return rows


COLLECTORS = {
    "wildchat": collect_wildchat,
    "arena": collect_arena,
    "bfcl": collect_bfcl,
    "longbench": collect_longbench,
    "mgsm": collect_mgsm,
}


# --- filtering and sampling --------------------------------------------


def keep_row(row: Row, counts: Counts) -> bool:
    """Apply the drop rules and count what each one removed."""
    counts.seen += 1
    if row.flagged:
        counts.toxic += 1
        return False
    texts = [m.get("content") or "" for m in row.messages]
    if not any(t.strip() for t in texts):
        counts.empty += 1
        return False
    blob = "\n".join(texts)
    if row.tools:
        blob = blob + "\n" + json.dumps(row.tools, ensure_ascii=False)
    if has_pii(blob):
        counts.pii += 1
        return False
    counts.kept += 1
    return True


def proportional_sample(rows: list[Row], limit: int, rng: random.Random) -> list[Row]:
    """Sample `limit` rows, keeping each stratum's share of the pool."""
    if len(rows) <= limit:
        return list(rows)
    buckets: dict[str, list[Row]] = {}
    for row in rows:
        buckets.setdefault(row.stratum, []).append(row)
    keys = sorted(buckets)
    for key in keys:
        rng.shuffle(buckets[key])

    total = len(rows)
    quota: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for key in keys:
        exact = limit * len(buckets[key]) / total
        quota[key] = int(exact)
        remainders.append((exact - int(exact), key))
    # Largest remainder first, ties by name so the result does not move.
    remainders.sort(key=lambda pair: (-pair[0], pair[1]))
    short = limit - sum(quota.values())
    for _, key in remainders:
        if short <= 0:
            break
        if quota[key] < len(buckets[key]):
            quota[key] += 1
            short -= 1

    picked: list[Row] = []
    for key in keys:
        picked.extend(buckets[key][: quota[key]])
    # Any shortfall from small strata is filled from whatever is left.
    if len(picked) < limit:
        chosen = {id(r) for r in picked}
        rest = [r for r in rows if id(r) not in chosen]
        rng.shuffle(rest)
        picked.extend(rest[: limit - len(picked)])
    return picked[:limit]


def truncate(row: Row, max_chars: int) -> str:
    """Cap every message and report the original length of the longest cut."""
    longest = 0
    for msg in row.messages:
        content = msg.get("content") or ""
        if len(content) > max_chars:
            longest = max(longest, len(content))
            msg["content"] = content[:max_chars]
    if not longest:
        return ""
    return f"One message was {longest} characters and was truncated to {max_chars}."


def build_case(row: Row, index: int, max_chars: int) -> dict[str, Any]:
    truncated_note = truncate(row, max_chars)
    chars = request_chars(row.messages, row.tools)
    slice_name = slice_of(row.messages, row.tools, row.language)
    turns = len(row.messages)
    notes = (
        f"Imported from {CREDITS[row.source]}. {turns} turn{'' if turns == 1 else 's'}, "
        f"{chars} characters, {row.language}."
    )
    if row.detail:
        notes += f" {row.detail[0].upper()}{row.detail[1:]}."
    if truncated_note:
        notes += f" {truncated_note}"
    request: dict[str, Any] = {"messages": row.messages}
    if row.tools:
        request["tools"] = row.tools
    request["stream"] = True
    return {
        "id": f"public-{row.source}-{index:04d}",
        "slice": slice_name,
        "source": row.source,
        "language": row.language,
        "label_source": "unlabelled",
        "notes": notes,
        "request": request,
    }


# --- writing ------------------------------------------------------------


class BlockDumper(yaml.SafeDumper):
    """Keeps long message text readable and indents lists like cases.yaml."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _str_block(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _empty_none(dumper: yaml.Dumper, _data: Any) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


BlockDumper.add_representer(str, _str_block)
BlockDumper.add_representer(type(None), _empty_none)


def dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.dump(
        payload,
        Dumper=BlockDumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )


HEADER = (
    "# Unlabelled cases sampled from public datasets by evals/import_public.py.\n"
    "# Git-ignored on purpose: public conversation dumps can hold personal data.\n"
    "# Labels live in cases_public_labels.yaml and are not written by the script.\n"
)

LABELS_HEADER = (
    "# Label stubs for cases_public.yaml, one per case, all keys empty.\n"
    "# Fill these in by hand or with a labelling script. Re-running\n"
    "# evals/import_public.py adds stubs for new ids and never overwrites.\n"
)


def label_stub(case_id: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "label_source": "llm-assisted",
        "expected": {
            "task": None,
            "difficulty": None,
            "difficulty_tolerance": None,
            "needs_faithfulness": None,
            "tier": None,
            "effort": None,
            "acceptable_tiers": [],
        },
    }


def merge_labels(path: Path, case_ids: list[str]) -> int:
    """Add a stub for every id that has none. Existing stubs are untouched."""
    existing: list[dict[str, Any]] = []
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
        existing = list(loaded.get("cases") or [])
    known = {str(item.get("id")) for item in existing if isinstance(item, dict)}
    added = [label_stub(cid) for cid in case_ids if cid not in known]
    if not added and path.exists():
        return 0
    path.write_text(LABELS_HEADER + dump_yaml({"cases": existing + added}))
    return len(added)


# --- main ---------------------------------------------------------------


# How many rows to read per case wanted, and a floor. LongBench rows are whole
# documents, so it reads far fewer of them.
SCAN_FACTOR = {"wildchat": 25, "arena": 25, "bfcl": 25, "longbench": 6, "mgsm": 25}
SCAN_FLOOR = {"wildchat": 400, "arena": 400, "bfcl": 400, "longbench": 100, "mgsm": 400}


def run_source(source: str, args: argparse.Namespace) -> tuple[list[Row], Counts]:
    scan = max(args.limit * SCAN_FACTOR[source], SCAN_FLOOR[source])
    rows = COLLECTORS[source](scan)
    counts = Counts()
    kept = [row for row in rows if keep_row(row, counts)]
    rng = random.Random(f"{args.seed}:{source}")
    sampled = proportional_sample(kept, args.limit, rng)
    sampled.sort(key=lambda r: (r.stratum, len(r.messages)))
    return sampled, counts


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--source",
        action="append",
        choices=SOURCES,
        help="dataset to sample; repeatable, defaults to all of them",
    )
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="cases per source")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where the cases go")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sampling seed")
    p.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="cap on one message; the original length goes in notes",
    )
    args = p.parse_args()

    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2

    # Checked before any download so the message is one line, not a traceback.
    if importlib.util.find_spec("datasets") is None:
        print(
            "the datasets package is missing. Install it with: uv sync --group evals",
            file=sys.stderr,
        )
        return 2

    wanted = args.source or SOURCES
    cases: list[dict[str, Any]] = []
    worked: list[str] = []
    skipped: list[str] = []

    for source in wanted:
        try:
            rows, counts = run_source(source, args)
        except SourceUnavailable as exc:
            print(skip_line(source, str(exc)))
            skipped.append(source)
            continue
        except Exception as exc:  # anything the library throws
            print(skip_line(source, short_reason(exc)))
            skipped.append(source)
            continue
        worked.append(source)
        for i, row in enumerate(rows, start=1):
            case = build_case(row, i, args.max_chars)
            if "truncated to" in case["notes"]:
                counts.truncated += 1
            cases.append(case)
        print(counts.line(source, len(rows)))

    if not worked:
        print(
            "no source was available, so nothing was written. Check the network, "
            "or accept the dataset terms on the Hub for the gated ones.",
            file=sys.stderr,
        )
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HEADER + dump_yaml({"cases": cases}))

    labels_path = out.with_name(out.stem + "_labels" + out.suffix)
    added = merge_labels(labels_path, [c["id"] for c in cases])

    by_slice: dict[str, int] = {}
    for case in cases:
        by_slice[case["slice"]] = by_slice.get(case["slice"], 0) + 1

    print(f"wrote {len(cases)} cases to {out.name} from {len(worked)} sources")
    print("by slice: " + ", ".join(f"{k} {v}" for k, v in sorted(by_slice.items())))
    print(f"label stubs added to {labels_path.name}: {added}")
    if skipped:
        print("skipped: " + ", ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
