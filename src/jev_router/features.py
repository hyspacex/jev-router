"""Turn a chat-completions request body into plain facts.

Jev cannot count or compare numbers, so every count lives here and the
questions only ever see short text plus these computed fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CODE_FENCE = re.compile(r"```([A-Za-z0-9_+#.\-]*)")
# A long run of base64-ish characters, the usual sign of an inlined file.
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/=]{512,}")


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
