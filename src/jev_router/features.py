"""Turn a chat-completions request body into plain facts.

Jev cannot count or compare numbers, so every count lives here and the
questions only ever see short text plus these computed fields.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

CODE_FENCE = re.compile(r"```([A-Za-z0-9_+#.\-]*)")
# A long run of base64-ish characters, the usual sign of an inlined file.
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/=]{512,}")

# Characters that carry about one token each instead of about a quarter of
# one: Han, Kana, Hangul and the full-width forms. Four characters per token
# is a Latin-text rule and it undercounts CJK by roughly four to one.
CJK = re.compile(
    r"[ᄀ-ᇿ⺀-꓏ꥠ-꥿가-퟿豈-﫿"
    r"︰-﹏＀-｠￠-￦]"
    r"|[\U00020000-\U0003ffff]"
)

# What one image is charged when nobody has told us its real cost. It is an
# allowance, not a measurement, which is why an image also marks the estimate
# uncertain and keeps it away from the capacity boundary.
IMAGE_TOKEN_ALLOWANCE = 1200


@dataclass(frozen=True)
class Message:
    role: str
    text: str
    has_image: bool = False


@dataclass(frozen=True)
class Features:
    model: str
    messages: tuple[Message, ...] = ()
    system_prompt: str = ""
    first_user_message: str = ""
    last_user_message: str = ""
    message_count: int = 0
    est_tokens: int = 0
    total_chars: int = 0
    has_tools: bool = False
    tool_names: tuple[str, ...] = ()
    has_images: bool = False
    has_code: bool = False
    languages: tuple[str, ...] = ()
    client: str = ""
    stream: bool = False
    max_tokens: int = 0
    auth_header: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def budget_tokens(self) -> int:
        """Prompt plus the room the answer may need."""
        return self.est_tokens + self.max_tokens

    def to_facts(self) -> dict[str, Any]:
        """Counts and flags only: what the decision log may keep forever."""
        return {
            "message_count": self.message_count,
            "est_tokens": self.est_tokens,
            "total_chars": self.total_chars,
            "system_prompt_chars": len(self.system_prompt),
            "last_user_message_chars": len(self.last_user_message),
            "has_tools": self.has_tools,
            "tool_count": len(self.tool_names),
            "tool_names": list(self.tool_names[:20]),
            "has_images": self.has_images,
            "has_code": self.has_code,
            "languages": list(self.languages),
            "client": self.client,
            "stream": self.stream,
            "max_tokens": self.max_tokens,
            "requested_model": self.model,
        }


def _content_text(content: Any) -> tuple[str, bool]:
    """Return (text, has_image) for either a string or a content-part list."""
    if content is None:
        return "", False
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        parts: list[str] = []
        has_image = False
        for part in content:
            if not isinstance(part, dict):
                parts.append(str(part))
                continue
            ptype = part.get("type")
            if ptype in ("image_url", "image", "input_image"):
                has_image = True
            elif ptype in ("text", "input_text") and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts), has_image
    return str(content), False


def _tool_names(body: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.append(fn["name"])
        elif isinstance(tool.get("name"), str):
            names.append(tool["name"])
    for fn in body.get("functions") or []:  # the older shape, still seen
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.append(fn["name"])
    return names


def extract_features(
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Features:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    messages_in = body.get("messages") or []

    messages: list[Message] = []
    system_chunks: list[str] = []
    user_texts: list[str] = []
    total_chars = 0
    has_images = False

    for raw in messages_in:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", ""))
        text, img = _content_text(raw.get("content"))
        # Tool calls carry their arguments as text the model must read.
        for call in raw.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                if isinstance(fn, dict):
                    text += "\n" + str(fn.get("arguments", ""))
        has_images = has_images or img
        total_chars += len(text)
        messages.append(Message(role=role, text=text, has_image=img))
        if role == "system" or role == "developer":
            system_chunks.append(text)
        elif role == "user":
            user_texts.append(text)

    if isinstance(body.get("system"), str):  # some clients send it beside messages
        system_chunks.insert(0, body["system"])
        total_chars += len(body["system"])

    joined = "\n".join(m.text for m in messages)
    languages = sorted({m.group(1).lower() for m in CODE_FENCE.finditer(joined) if m.group(1)})
    has_code = "```" in joined

    tool_names = _tool_names(body)
    # Rough but stable: four characters per token, plus the tool schemas.
    tool_chars = sum(len(str(t)) for t in (body.get("tools") or []))
    est_tokens = (total_chars + tool_chars) // 4

    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens") or 0
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        max_tokens = 0

    return Features(
        model=str(body.get("model", "")),
        messages=tuple(messages),
        system_prompt="\n".join(system_chunks),
        first_user_message=user_texts[0] if user_texts else "",
        last_user_message=user_texts[-1] if user_texts else "",
        message_count=len(messages),
        est_tokens=est_tokens,
        total_chars=total_chars,
        has_tools=bool(tool_names),
        tool_names=tuple(tool_names),
        has_images=has_images,
        has_code=has_code,
        languages=tuple(languages),
        client=headers.get("x-router-client", ""),
        stream=bool(body.get("stream")),
        max_tokens=max_tokens,
        auth_header=headers.get("authorization", ""),
    )


# --- serialized input size, for the strict session budget ----------------


@dataclass(frozen=True)
class InputEstimate:
    """How large the serialized input looks, and what we could not see.

    `est_tokens` on Features is the number the legacy ladder has always used
    and it stays exactly as it was. This is a second, more careful count for
    admission, where undercounting means a session that overflows on its
    third turn. It adds tool schemas, charges CJK properly and names the
    parts whose real size is unknown.
    """

    tokens: int
    method: str
    unknown: tuple[str, ...] = ()

    def to_facts(self) -> dict[str, Any]:
        return {
            "estimated_input_tokens": self.tokens,
            "estimate_method": self.method,
            "unknown_parts": list(self.unknown),
        }


def text_tokens(text: str) -> int:
    """Four characters per token, except CJK characters, which cost one each."""
    if not text:
        return 0
    cjk = len(CJK.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def _image_parts(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for part in content
        if isinstance(part, dict)
        and part.get("type") in ("image_url", "image", "input_image")
    )


def estimate_input(
    body: dict[str, Any],
    *,
    accumulated_input_tokens: int | None = None,
) -> InputEstimate:
    """Estimate the whole serialized input of one chat-completions body.

    Pure. Counts instructions, message text, tool-call arguments, tool
    schemas and provider framing, charges an allowance per image, and adds a
    client-reported accumulated usage figure when the history itself is not
    present. Anything it had to guess at is named in `unknown`, and the
    caller refuses admission near the capacity boundary when that list is not
    empty.
    """
    tokens = 0
    unknown: list[str] = []

    if isinstance(body.get("system"), str):
        tokens += text_tokens(body["system"])
    if isinstance(body.get("instructions"), str):
        tokens += text_tokens(body["instructions"])

    for raw in body.get("messages") or []:
        if not isinstance(raw, dict):
            tokens += text_tokens(str(raw))
            continue
        text, _ = _content_text(raw.get("content"))
        tokens += text_tokens(text)
        tokens += _image_parts(raw.get("content")) * IMAGE_TOKEN_ALLOWANCE
        if _image_parts(raw.get("content")):
            unknown.append("image")
        for call in raw.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                if isinstance(fn, dict):
                    tokens += text_tokens(str(fn.get("name", "")))
                    tokens += text_tokens(str(fn.get("arguments", "")))
        # Role, name and the separators every provider wraps a message in.
        tokens += 4

    for schema in list(body.get("tools") or []) + list(body.get("functions") or []):
        tokens += text_tokens(json.dumps(schema, ensure_ascii=False, default=str))

    if body.get("previous_response_id"):
        # The history is on the provider's side. A short delta is not the
        # context; without a reported total there is nothing to add up.
        if accumulated_input_tokens is None:
            unknown.append("previous_response")
        else:
            tokens += max(0, int(accumulated_input_tokens))

    method = "router-serialized-v1"
    if accumulated_input_tokens is not None:
        method = "router-serialized-v1+reported-usage"
    # Stable order, one entry per kind.
    ordered = tuple(k for k in ("image", "previous_response") if k in unknown)
    return InputEstimate(tokens=tokens, method=method, unknown=ordered)
