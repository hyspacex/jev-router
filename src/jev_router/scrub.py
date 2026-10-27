"""What text the router may keep, and how it is cleaned first.

Message text reaches the database only through this module. Every function
here is pure and has no dependencies: text in, scrubbed and capped text out.

The scrubber removes what should never be written down even when text may
be: API keys and bearer tokens, private keys, `KEY=value` style secrets,
inline images and long encoded blobs. It is a filter, not a guarantee. A
secret with no recognisable shape gets through, which is why the stored text
is also capped and why `settings.log_state: false` stores none at all.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .state import squeeze

TITLE_MAX_CHARS = 120
PREVIEW_MAX_CHARS = 400
WORKSPACE_MAX_CHARS = 120
SNIPPET_MAX_CHARS = 600
TOOL_NAME_MAX_CHARS = 64
MAX_TOOL_NAMES = 30
MAX_FILES = 20
FILE_MAX_CHARS = 200
DEFAULT_TEXT_MAX_CHARS = 2048

SECRET = "[REDACTED_SECRET]"
PRIVATE_KEY = "[REDACTED_PRIVATE_KEY]"
REDACTED = "[REDACTED]"
IMAGE = "[IMAGE_DATA_URI]"
BLOB = "[BINARY_BLOB_OMITTED]"

# Nothing alphanumeric directly before a token, so "task-..." is not "sk-...".
_EDGE = r"(?<![A-Za-z0-9])"

# Order matters: whole private keys and data URIs go first, so the later
# token patterns never see half of one.
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_DATA_URI = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
_TOKENS = [
    re.compile(_EDGE + r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(_EDGE + r"rk-[A-Za-z0-9_-]{20,}"),
    re.compile(_EDGE + r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(_EDGE + r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(_EDGE + r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(_EDGE + r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(_EDGE + r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
# `OPENAI_API_KEY=...`, `"password": "..."`, `Authorization: ...`. The value
# must look like a value: six characters or more, not a plain number (so
# `max_tokens: 4096` survives) and not something already redacted.
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|password|passwd|authorization)"
    r"[A-Za-z0-9_]*)"
    r"([\"']?\s*[:=]\s*)"
    r"([\"']?)"
    r"(?!\[REDACTED)(?!\d+(?:[\s\"',;}]|$))(?!(?:bearer|basic)\b)"
    r"([^\s\"',;}]{6,})"
)
# One run of encoded characters longer than any word: a file, a hash dump, an
# image someone pasted without the data: prefix.
_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{257,}")


def scrub_text(text: Any) -> str:
    """The same text with every recognisable secret and blob replaced."""
    if text is None:
        return ""
    out = str(text)
    out = _PEM.sub(PRIVATE_KEY, out)
    out = _DATA_URI.sub(IMAGE, out)
    for pattern in _TOKENS:
        out = pattern.sub(SECRET, out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {SECRET}", out)
    out = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", out)
    out = _BLOB.sub(BLOB, out)
    return out


def cap_text(text: Any, limit: int = DEFAULT_TEXT_MAX_CHARS) -> str:
    """At most `limit` characters, keeping the head and the tail.

    `state.squeeze` does the cutting, so the stored text is cut the way the
    text Jev was shown is cut. Its marker is counted inside the limit.
    """
    value = "" if text is None else str(text)
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    out, _ = squeeze(value, limit)
    budget = limit
    while len(out) > limit and budget > 0:
        budget -= len(out) - limit
        out, _ = squeeze(value, max(budget, 0))
    return out[:limit]


def clean(text: Any, limit: int = DEFAULT_TEXT_MAX_CHARS) -> str:
    """Scrubbed, then capped. What every stored text column goes through."""
    return cap_text(scrub_text(text), limit)


def scrub_state(state: Any, limit: int = DEFAULT_TEXT_MAX_CHARS) -> Any:
    """A copy of a Jev state with every string leaf scrubbed and capped.

    Keys, numbers, flags and the structure are kept as they are. Anything
    that is not JSON-shaped becomes its string form, scrubbed.
    """
    if state is None or isinstance(state, (bool, int, float)):
        return state
    if isinstance(state, str):
        return clean(state, limit)
    if isinstance(state, dict):
        return {str(key): scrub_state(value, limit) for key, value in state.items()}
    if isinstance(state, (list, tuple)):
        return [scrub_state(value, limit) for value in state]
    return clean(str(state), limit)


# --- titles and previews ---------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADER = re.compile(r"^\s*#{1,6}\s*")
_LEAD = re.compile(r"^\s*(?:>\s*|[-*+]\s+|\d+[.)]\s+)+")
_TAG_LINE = re.compile(r"^\s*</?[A-Za-z_][A-Za-z0-9_.:-]*(?:\s[^>]*)?/?>\s*$")
_SPACE = re.compile(r"\s+")


def _one_line(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def extract_session_title(prompt: Any) -> str:
    """The first line that says something, at most 120 characters.

    Code fences and what is inside them are skipped, markdown headers,
    bullets and quote markers are stripped, and a line that is only a markup
    tag (an `<environment_context>` opener, say) does not count. Empty when
    there is nothing.
    """
    inside = False
    for raw in scrub_text(prompt).splitlines():
        if _FENCE.match(raw):
            inside = not inside
            continue
        if inside or _TAG_LINE.match(raw):
            continue
        line = _one_line(_LEAD.sub("", _HEADER.sub("", raw)))
        if line.strip("#*_-=`~ ") == "":
            continue
        return _cut(line, TITLE_MAX_CHARS)
    return ""


def prompt_preview(prompt: Any) -> str:
    """The opening of a prompt on one line, scrubbed, at most 400 characters."""
    return _cut(_one_line(scrub_text(prompt)), PREVIEW_MAX_CHARS)


# --- where the work is -----------------------------------------------------

_CWD_PATTERNS = [
    re.compile(r"<cwd>\s*([^<\n]+?)\s*</cwd>", re.IGNORECASE),
    re.compile(
        r"(?im)^\s*[-*]?\s*(?:current\s+)?(?:working\s+directory|cwd|workspace|"
        r"project\s+root|repository\s+root)\s*[:=]\s*`?([^\s`]+)`?"
    ),
    re.compile(r"(?im)^\s*[-*]?\s*(?:repo|repository|project)\s*[:=]\s*`?([A-Za-z0-9._/-]+)`?"),
]
_HOME_ONLY = re.compile(r"^(?:~|/Users/[^/]+|/home/[^/]+|/root|[A-Za-z]:\\Users\\[^\\]+)$")
_WORKSPACE_NAME = re.compile(r"[A-Za-z0-9._@+-]{1,120}")
# A bounded look at each text, so a huge system prompt costs nothing.
_SCAN_CHARS = 20000


def _texts(system_prompt: Any, body: Any) -> list[str]:
    out = [str(system_prompt or "")[:_SCAN_CHARS]]
    if not isinstance(body, dict):
        return out
    if isinstance(body.get("instructions"), str):
        out.append(body["instructions"][:_SCAN_CHARS])
    items = body.get("messages")
    if not isinstance(items, list):
        items = body.get("input") if isinstance(body.get("input"), list) else []
    for item in items[:50]:
        if isinstance(item, dict):
            out.append(_content_text(item.get("content"))[:_SCAN_CHARS])
    return out


def workspace_hint(system_prompt: Any, body: Any = None) -> str | None:
    """The repository or working directory a harness says it is in.

    Only the last path component is kept, so a full path with a user name in
    it is never stored. None when nothing recognisable is there, or when the
    only thing named is a home directory.
    """
    for text in _texts(system_prompt, body):
        for pattern in _CWD_PATTERNS:
            for match in pattern.finditer(text):
                raw = match.group(1).strip().strip("'\"`").rstrip("/\\")
                if not raw or _HOME_ONLY.match(raw):
                    continue
                name = re.split(r"[/\\]", raw)[-1]
                name = scrub_text(name)
                if name in ("", ".", "..", "~") or not _WORKSPACE_NAME.fullmatch(name):
                    continue
                return name[:WORKSPACE_MAX_CHARS]
    return None


# --- what happened during a turn ---------------------------------------------

_PATH_KEYS = ("path", "file_path", "filepath", "filename", "file", "target_file", "target")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_HOME_PREFIX = re.compile(r"^(?:/Users/[^/]+|/home/[^/]+|[A-Za-z]:\\Users\\[^\\]+)(?=[/\\])")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif (
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and part.get("type") in (None, "text", "input_text", "output_text")
            ):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def _is_user(item: dict[str, Any]) -> bool:
    return item.get("role") == "user" and item.get("type") in (None, "message")


def user_turn_count(messages: Any) -> int:
    """How many user messages a chat or Responses message list holds."""
    if not isinstance(messages, list):
        return 0
    return sum(1 for m in messages if isinstance(m, dict) and _is_user(m))


def _calls(item: dict[str, Any]) -> list[tuple[str, Any]]:
    """(name, arguments) for every tool call this item makes."""
    out: list[tuple[str, Any]] = []
    if item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list):
        for call in item["tool_calls"]:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                out.append((str(fn.get("name") or ""), fn.get("arguments")))
    kind = item.get("type")
    if kind in ("function_call", "custom_tool_call"):
        out.append((str(item.get("name") or ""), item.get("arguments", item.get("input"))))
    elif kind == "local_shell_call":
        out.append(("shell", item.get("action")))
    return out


def _assistant_text(item: dict[str, Any]) -> str:
    if item.get("role") != "assistant":
        return ""
    if item.get("type") not in (None, "message"):
        return ""
    return _content_text(item.get("content")).strip()


def _paths(arguments: Any) -> list[str]:
    if isinstance(arguments, str):
        found = _PATCH_FILE.findall(arguments)
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return found
        if isinstance(arguments, str):
            return found + _PATCH_FILE.findall(arguments)
    out: list[str] = []
    if isinstance(arguments, dict):
        for key in _PATH_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
        paths = arguments.get("paths")
        if isinstance(paths, list):
            out.extend(p.strip() for p in paths if isinstance(p, str) and p.strip())
        for value in arguments.values():
            if isinstance(value, str) and "*** " in value:
                out.extend(_PATCH_FILE.findall(value))
    return out


def _file_label(path: str) -> str | None:
    path = _HOME_PREFIX.sub("~", path.strip())
    if not path or "\n" in path or len(path) > FILE_MAX_CHARS:
        return None
    path = scrub_text(path)
    return path if REDACTED not in path and SECRET not in path else None


def summarize_transcript_turn(
    messages: Any, *, segment: int = -1, limit: int = SNIPPET_MAX_CHARS
) -> dict[str, Any]:
    """What the assistant did in one user turn, from the client's transcript.

    `messages` is a chat-completions `messages` list or a Responses `input`
    list. `segment=-1` reads everything after the last user message, which is
    the turn still in progress; `segment=-2` reads the turn before it, which
    a request that opens a new turn carries in full.

    Returns `tool_calls` as `[{name, count}]` in first-use order, the last
    assistant text as `assistant_snippet` (scrubbed and capped, or None), and
    `files_touched` from tool arguments that carry a recognisable path. All
    scrubbed. Nothing here reads a reply stream: only what the client sent.
    """
    items = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
    starts = [i for i, m in enumerate(items) if _is_user(m)]
    if segment == -1:
        span = items[starts[-1] + 1:] if starts else items
    else:
        if len(starts) < 2:
            span = []
        else:
            span = items[starts[-2] + 1: starts[-1]]
    counts: dict[str, int] = {}
    files: list[str] = []
    snippet = ""
    for item in span:
        for name, arguments in _calls(item):
            label = name if _TOOL_NAME.fullmatch(name or "") else "other"
            if label not in counts and len(counts) >= MAX_TOOL_NAMES:
                label = "other"
            counts[label] = counts.get(label, 0) + 1
            for path in _paths(arguments):
                label_path = _file_label(path)
                if label_path and label_path not in files and len(files) < MAX_FILES:
                    files.append(label_path)
        text = _assistant_text(item)
        if text:
            snippet = text
    return {
        "tool_calls": [{"name": name, "count": n} for name, n in counts.items()],
        "assistant_snippet": clean(snippet, limit) if snippet else None,
        "files_touched": files,
    }
