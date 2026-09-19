"""Programmatic graders for the outcome benchmark.

Each check reads one model reply and returns a score from 0 to 1 plus a note.
Anything that executes model output does it in a fresh temporary directory,
with a timeout, with no network and with the environment stripped.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from re import error
from typing import Any, Callable

CHECKS: dict[str, Callable[..., tuple[float, str]]] = {}
RUN_TIMEOUT_S = 15


def check(name: str):
    def deco(fn):
        CHECKS[name] = fn
        return fn

    return deco


# --- helpers ------------------------------------------------------------

FENCE = re.compile(r"```(?:[A-Za-z0-9_+#.\-]*)\n(.*?)```", re.S)


def code_blocks(reply: str, want: str | None = None) -> list[str]:
    blocks = [m.group(1) for m in FENCE.finditer(reply)]
    if want:
        tagged = re.findall(
            rf"```{want}[^\n]*\n(.*?)```", reply, re.S | re.I
        )
        if tagged:
            return tagged
    return blocks


def first_json(reply: str) -> Any:
    for block in code_blocks(reply, "json") + [reply]:
        text = block.strip()
        start = min(
            [i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1]
        )
        if start < 0:
            continue
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end])
            except ValueError:
                continue
    return None


def numbers_in(text: str) -> list[float]:
    out = []
    for token in re.findall(r"-?\d[\d,_]*\.?\d*", text):
        try:
            out.append(float(token.replace(",", "").replace("_", "")))
        except ValueError:
            pass
    return out


# --- text checks --------------------------------------------------------


@check("contains_all")
def contains_all(reply: str, patterns: list[str], **_: Any) -> tuple[float, str]:
    hits = [p for p in patterns if re.search(p, reply, re.I | re.S)]
    missing = [p for p in patterns if p not in hits]
    return len(hits) / len(patterns), ("" if not missing else f"missing {missing}")


@check("contains_none")
def contains_none(reply: str, patterns: list[str], **_: Any) -> tuple[float, str]:
    hits = [p for p in patterns if re.search(p, reply, re.I | re.S)]
    return (0.0 if hits else 1.0), ("" if not hits else f"found {hits}")


@check("max_words")
def max_words(reply: str, limit: int, slack: float = 0.25, **_: Any) -> tuple[float, str]:
    n = len(reply.split())
    if n <= limit:
        return 1.0, ""
    if n <= limit * (1 + slack):
        return 0.5, f"{n} words, limit {limit}"
    return 0.0, f"{n} words, limit {limit}"


@check("numeric_answer")
def numeric_answer(reply: str, expected: float, tol: float = 0.01, **_: Any) -> tuple[float, str]:
    for value in numbers_in(reply):
        if abs(value - expected) <= tol:
            return 1.0, ""
    return 0.0, f"{expected} not found"


# --- executed checks ----------------------------------------------------


@check("regex_examples")
def regex_examples(
    reply: str,
    should_match: list[str],
    groups: dict[str, list[str]] | None = None,
    should_not_match: list[str] | None = None,
    **_: Any,
) -> tuple[float, str]:
    """Pull a Python regex out of the reply and run it against examples.

    `groups` maps a label to the value each example must yield. Any named group
    in the match may supply it, since the model chooses the group names.
    """
    candidates: list[str] = []
    for block in code_blocks(reply, "python") + code_blocks(reply) + [reply]:
        candidates.extend(_compile_call_patterns(block))
        for m in re.finditer(r"""r?(['"]{3})(?P<pat>.*?)\1""", block, re.S):
            candidates.append(m.group("pat"))
        for m in re.finditer(r"""r?(['"])(?P<pat>(?:\\.|(?!\1).){8,})\1""", block):
            candidates.append(m.group("pat"))
    best, best_note = 0.0, "no usable regex found"
    for pattern in candidates:
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        total, good = 0, 0
        notes = []
        for i, line in enumerate(should_match):
            total += 1
            m = rx.search(line)
            if not m:
                notes.append(f"no match on example {i + 1}")
                continue
            good += 1
            captured = set((m.groupdict() or {}).values())
            for label, wanted in (groups or {}).items():
                total += 1
                if wanted[i] in captured:
                    good += 1
                else:
                    notes.append(
                        f"no named group captured {wanted[i]!r} for {label} "
                        f"(got {sorted(x for x in captured if x)})"
                    )
        for line in should_not_match or []:
            total += 1
            if rx.search(line):
                notes.append("matched a line it should not")
            else:
                good += 1
        score = good / total if total else 0.0
        if score > best:
            best, best_note = score, "; ".join(notes[:3])
    return best, best_note


def _compile_call_patterns(block: str) -> list[str]:
    """Join the string literals inside each re.compile(...) call.

    Models write long patterns as adjacent literals with comments between, and
    each literal on its own is not the pattern they meant.
    """
    out: list[str] = []
    for m in re.finditer(r"re\.compile\s*\(", block):
        depth, i = 1, m.end()
        while i < len(block) and depth:
            if block[i] == "(":
                depth += 1
            elif block[i] == ")":
                depth -= 1
            i += 1
        inner = block[m.end() : i - 1]
        inner = re.sub(r"#[^\n]*", "", inner)
        parts = [
            lit.group("pat")
            for lit in re.finditer(r"""[rbfu]{0,2}(['"])(?P<pat>(?:\\.|(?!\1).)*)\1""", inner)
        ]
        if parts:
            out.append("".join(parts))
    return out


@check("json_fields")
def json_fields(
    reply: str, expected: dict[str, Any], tol: float = 0.005, **_: Any
) -> tuple[float, str]:
    data = first_json(reply)
    if not isinstance(data, dict):
        return 0.0, "no JSON object in the reply"
    good, notes = 0, []
    for key, want in expected.items():
        got = data.get(key)
        if isinstance(want, (int, float)) and isinstance(got, (int, float)):
            ok = abs(float(got) - float(want)) <= max(tol, abs(want) * tol)
        elif isinstance(want, str) and isinstance(got, str):
            ok = got.strip().lower() == want.strip().lower()
        else:
            ok = got == want
        if ok:
            good += 1
        else:
            notes.append(f"{key}={got!r} wanted {want!r}")
    return good / len(expected), "; ".join(notes[:4])


@check("json_list_len")
def json_list_len(reply: str, key: str, length: int, **_: Any) -> tuple[float, str]:
    data = first_json(reply)
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        return 0.0, f"no list at {key}"
    got = len(data[key])
    return (1.0 if got == length else 0.0), ("" if got == length else f"{got} items, wanted {length}")


@check("python_tests")
def python_tests(reply: str, setup: str = "", tests: str = "", **_: Any) -> tuple[float, str]:
    """Run the Python from the reply against assertions, in a temp dir."""
    blocks = code_blocks(reply, "python") or code_blocks(reply)
    if not blocks:
        return 0.0, "no python block in the reply"
    best, note = 0.0, "did not run"
    for block in blocks:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "run.py"
            script.write_text(f"{setup}\n{block}\n{tests}\n")
            try:
                proc = subprocess.run(
                    [sys.executable, "run.py"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    timeout=RUN_TIMEOUT_S,
                    env={"PATH": os.environ.get("PATH", ""), "HOME": tmp},
                )
            except subprocess.TimeoutExpired:
                note = "timed out"
                continue
            if proc.returncode == 0:
                return 1.0, ""
            note = (proc.stderr or proc.stdout).strip().splitlines()[-1:][0][:160] if (
                proc.stderr or proc.stdout
            ) else "non-zero exit"
    return best, note


@check("sqlite")
def sqlite_query(
    reply: str, setup: str = "", expected_rows: list[list[Any]] | None = None, **_: Any
) -> tuple[float, str]:
    """Run the SQL from the reply against an in-memory fixture."""
    blocks = code_blocks(reply, "sql") or code_blocks(reply)
    if not blocks:
        return 0.0, "no sql block in the reply"
    note = "did not run"
    for block in blocks:
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(setup)
            rows = [list(r) for r in conn.execute(block).fetchall()]
        except sqlite3.Error as exc:
            note = str(exc)[:160]
            continue
        finally:
            conn.close()
        if expected_rows is None:
            return 1.0, ""
        if rows == expected_rows:
            return 1.0, ""
        note = f"rows {rows[:3]} wanted {expected_rows[:3]}"
    return 0.0, note


def run_checks(reply: str, specs: list[dict[str, Any]]) -> tuple[float, list[str]]:
    """Weighted score from 0 to 10, plus one note per check that lost points."""
    if not reply.strip():
        return 0.0, ["empty reply"]
    total_weight = sum(float(s.get("weight", 1.0)) for s in specs) or 1.0
    score, notes = 0.0, []
    for spec in specs:
        kind = spec["kind"]
        args = {k: v for k, v in spec.items() if k not in ("kind", "weight")}
        fn = CHECKS.get(kind)
        if fn is None:
            raise KeyError(f"unknown check {kind!r} (known: {', '.join(sorted(CHECKS))})")
        value, note = fn(reply, **args)
        weight = float(spec.get("weight", 1.0))
        score += value * weight
        if value < 1.0:
            notes.append(f"{kind} {value:.2f}" + (f": {note}" if note else ""))
    return 10.0 * score / total_weight, notes
