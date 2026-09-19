"""State builders: what Jev is shown.

Jev gets less accurate when the state holds text that does not matter, and
pasted text can argue for its own classification. So builders stay small,
deterministic and short, and bulk is replaced by a count.

Add one with @register("name") and point an alias at it in router.yaml.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .features import BASE64_BLOB, Features

StateBuilder = Callable[[Features, Any], Any]

STATE_BUILDERS: dict[str, StateBuilder] = {}


def register(name: str) -> Callable[[StateBuilder], StateBuilder]:
    def deco(fn: StateBuilder) -> StateBuilder:
        if name in STATE_BUILDERS:
            raise ValueError(f"state builder {name!r} is already registered")
        STATE_BUILDERS[name] = fn
        return fn

    return deco


def build_state(name: str, features: Features, config: Any) -> Any:
    try:
        builder = STATE_BUILDERS[name]
    except KeyError:
        raise KeyError(
            f"unknown state builder {name!r} (known: {', '.join(sorted(STATE_BUILDERS))})"
        ) from None
    return builder(features, config)


def squeeze(text: str, limit: int) -> tuple[str, dict[str, int]]:
    """Cut text to `limit` characters, keeping the head and the tail.

    Returns the text and facts about what was removed, so the counting stays
    in code instead of being asked of Jev.
    """
    facts: dict[str, int] = {}
    blobs = 0

    def _mark(match: re.Match[str]) -> str:
        nonlocal blobs
        blobs += 1
        return f"[{len(match.group(0))} characters of encoded data removed]"

    text = BASE64_BLOB.sub(_mark, text)
    if blobs:
        facts["encoded_blobs_removed"] = blobs

    if len(text) <= limit:
        return text, facts

    facts["original_characters"] = len(text)
    facts["original_lines"] = text.count("\n") + 1
    head = limit * 2 // 3
    tail = limit - head
    return (
        text[:head]
        + f"\n[... {len(text) - limit} characters removed from the middle ...]\n"
        + text[-tail:],
        facts,
    )


def _common_facts(features: Features) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "message_count": features.message_count,
        "estimated_prompt_tokens": features.est_tokens,
        "contains_code_blocks": features.has_code,
        "request_has_tools": features.has_tools,
    }
    if features.tool_names:
        facts["tool_names"] = list(features.tool_names[:20])
    if features.languages:
        facts["code_block_languages"] = list(features.languages)
    if features.has_images:
        facts["contains_images"] = True
    if features.client:
        facts["client"] = features.client
    return facts


# A message that only agrees with, or asks to carry on with, work already
# described. Matched against the whole message, so a new instruction after the
# word "yes" stops it matching.
CONTINUATION = re.compile(
    r"^(?:"
    r"(?:ok(?:ay)?|yes|yep|yeah|sure|right|good|great|perfect|lgtm|sounds good|"
    r"please|thanks|thank you|cool|fine)[\s,.!]*"
    r")*"
    r"(?:(?:go ahead|go on|go|do it|do that|carry on|keep going|continue|proceed|"
    r"next|carry on with (?:it|that)|make it so|ship it|apply it|apply that|"
    r"implement it|implement that|write it|do the rest)[\s,.!]*)+$",
    re.IGNORECASE,
)

# The head of a pasted block: a code fence, a stack trace, a log line, or code.
PASTE_START = re.compile(
    r"^\s*(?:```|\{|\[|<\?|<[a-zA-Z]|#include|package\s|import\s|from\s+\w+\s+import|"
    r"def\s|class\s|func\s|fn\s|struct\s|type\s|SELECT\s|CREATE\s|Traceback|"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)

PASTE_MIN_CHARS = 400
PASTE_MIN_SHARE = 0.5
TRAILING_NOTE_MAX = 400


def _code_density(text: str) -> float:
    """Punctuation that shows up in code and almost never in a paragraph."""
    if not text:
        return 0.0
    return sum(text.count(ch) for ch in "{}()[];=<>|") / len(text)


def split_request_and_material(text: str) -> tuple[str, str]:
    """Separate what the user typed from the bulk they pasted below it.

    Returns (request, material). `material` is empty when the message is all
    one piece. The split is the first blank line, and a short prose paragraph
    at the very end is pulled back into the request, because people put the
    real constraint after the paste.
    """
    stripped = text.strip()
    if len(stripped) < PASTE_MIN_CHARS:
        return stripped, ""
    blocks = re.split(r"\n\s*\n", stripped)
    if len(blocks) < 2:
        return stripped, ""

    head = blocks[0].strip()
    rest_blocks = blocks[1:]
    if PASTE_START.match(head) or len(head) > 1200:
        return stripped, ""  # the message opens with the paste; no clean split

    tail_note = ""
    if len(rest_blocks) > 2:
        last = rest_blocks[-1].strip()
        before = rest_blocks[-2]
        # Only when the block above it is code or a log. After a pasted prose
        # document the last paragraph is part of the document, not a note.
        before_is_code = bool(PASTE_START.match(before)) or _code_density(before) > 0.02
        if (
            before_is_code
            and len(last) <= TRAILING_NOTE_MAX
            and not PASTE_START.match(last)
            and _code_density(last) < 0.01
            and sum(ch.isalpha() or ch.isspace() for ch in last) > 0.8 * len(last)
        ):
            tail_note = last
            rest_blocks = rest_blocks[:-1]

    material = "\n\n".join(b for b in rest_blocks).strip()
    if len(material) < PASTE_MIN_CHARS or len(material) < PASTE_MIN_SHARE * len(stripped):
        return stripped, ""

    request = head if not tail_note else f"{head}\n\n{tail_note}"
    return request, material


def _earlier_request(features: Features) -> str:
    """The last substantive user message before the latest one."""
    user_texts = [m.text for m in features.messages if m.role == "user"]
    if len(user_texts) < 2:
        return ""
    for text in reversed(user_texts[:-1]):
        if len(text.strip()) > 40:
            return text
    return user_texts[0]


def _last_turn_role(features: Features) -> str:
    for msg in reversed(features.messages):
        if msg.role in ("user", "assistant", "tool", "function"):
            return msg.role
    return ""


@register("summary_v1")
def summary_v1(features: Features, config: Any) -> dict[str, Any]:
    """Last user message, the head of the system prompt, and computed facts."""
    last, last_facts = squeeze(features.last_user_message, 4000)
    system, _ = squeeze(features.system_prompt[:500], 500)
    state: dict[str, Any] = {
        "latest_user_request": last,
        "facts": _common_facts(features) | last_facts,
    }
    if system.strip():
        state["start_of_system_prompt"] = system
    return state


@register("last_message_only")
def last_message_only(features: Features, config: Any) -> dict[str, Any]:
    """The smallest useful state: just what the user asked for now."""
    last, _ = squeeze(features.last_user_message, 2000)
    return {"latest_user_request": last}


@register("tail_v1")
def tail_v1(features: Features, config: Any) -> dict[str, Any]:
    """The last three turns, each cut short, plus computed facts."""
    turns = []
    for msg in features.messages[-3:]:
        if msg.role == "system":
            continue
        text, _ = squeeze(msg.text, 1500)
        turns.append({"role": msg.role, "text": text})
    state: dict[str, Any] = {
        "recent_turns": turns,
        "facts": _common_facts(features),
    }
    if features.system_prompt.strip():
        state["start_of_system_prompt"] = features.system_prompt[:500]
    return state


@register("continuation_aware_v1")
def continuation_aware_v1(features: Features, config: Any) -> dict[str, Any]:
    """Names each part of the request so the questions can point at one.

    Three things it does that summary_v1 does not:
      - when the latest message only approves or continues earlier work, the
        earlier request is carried along under its own name;
      - pasted bulk goes in a field whose name says it is quoted material, so
        instructions inside it read as data;
      - the facts say when the last turn was a tool result.

    The system prompt is left out on purpose: measured against this builder
    with it included, leaving it out was worth 2 to 3 points of task accuracy.
    The facts already carry the tool names, which is the part that mattered.
    """
    request, material = split_request_and_material(features.last_user_message)
    request, req_facts = squeeze(request, 3000)

    state: dict[str, Any] = {"latest_user_request": request}

    earlier = _earlier_request(features)
    short_now = len(features.last_user_message.strip())
    is_continuation = bool(
        earlier
        and (
            CONTINUATION.match(features.last_user_message.strip())
            or short_now < min(200, len(earlier.strip()))
        )
    )
    if is_continuation:
        state["earlier_request_in_this_conversation"] = squeeze(earlier, 2000)[0]

    pasted_chars = len(material)
    if material:
        material, mat_facts = squeeze(material, 2500)
        state["material_the_user_pasted"] = material
        pasted_chars = mat_facts.get("original_characters", pasted_chars)

    facts = _common_facts(features) | req_facts
    if pasted_chars:
        facts["pasted_material_characters"] = pasted_chars
    last_role = _last_turn_role(features)
    if last_role in ("tool", "function"):
        facts["last_turn_is_a_tool_result"] = True
    if is_continuation:
        facts["latest_message_follows_earlier_work"] = True
    state["facts"] = facts
    return state
