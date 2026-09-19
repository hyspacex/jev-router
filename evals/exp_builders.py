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
