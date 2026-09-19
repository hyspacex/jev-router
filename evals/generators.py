"""Seeded generators for the long-context cases.

A 40,000-character request is not something to type into `cases.yaml`. So a
case may carry a `generated:` block instead of a `request:` block, and the
loader in `common.py` expands it by calling `build()` here.

    - id: longctx-incident-log-162
      slice: longcontext
      generated:
        generator: incident_log
        seed: 3
        chars: 38000
        envelope: chat
        ask: |
          Which request id appears in both the 500 block and the retry block?

The material is synthetic and original: every generator assembles text from
fixed vocabularies with a seeded `random.Random`, so the same spec always gives
the same bytes and nothing is copied from anywhere. The `ask` is hand-written
per case and is the part the label is about.

Add a generator with @register("name"). It takes (rng, chars) and returns a
string of roughly `chars` characters.
"""

from __future__ import annotations

import random
from typing import Any, Callable

Generator = Callable[[random.Random, int], str]

GENERATORS: dict[str, Generator] = {}


def register(name: str) -> Callable[[Generator], Generator]:
    def deco(fn: Generator) -> Generator:
        if name in GENERATORS:
            raise ValueError(f"generator {name!r} is already registered")
        GENERATORS[name] = fn
        return fn

    return deco


SYS_CHAT = (
    "You are a helpful assistant. Answer clearly and concisely. Use Markdown "
    "when it helps. Today's date is 2026-09-18.\n"
)

SYS_PIPELINE = (
    "Follow the instruction exactly. Output only the requested result, with no "
    "preamble, no restatement of the task, and no closing remarks.\n"
)

SYS_AGENT = (
    "You are Sable, a terminal coding agent working inside the user's repository.\n"
    "\n"
    "Environment\n"
    "- Working directory: /home/dev/src/ledger-api\n"
    "- Platform: linux, shell: bash, git branch: main\n"
    "- Package manager: uv (Python 3.13)\n"
    "\n"
    "Guidelines\n"
    "- Inspect files with read_file before editing them. Never guess at file contents.\n"
    "- Prefer small, surgical edits. Do not reformat code you were not asked to touch.\n"
    "- Run the project's test command (`uv run pytest -q`) before declaring work finished.\n"
    "\n"
    "Answer concisely. No emojis.\n"
)


def build(spec: dict[str, Any]) -> dict[str, Any]:
    """Turn a `generated:` block into a chat-completions body."""
    name = spec["generator"]
    try:
        gen = GENERATORS[name]
    except KeyError:
        raise KeyError(
            f"unknown generator {name!r} (known: {', '.join(sorted(GENERATORS))})"
        ) from None
    rng = random.Random(f"{name}:{spec.get('seed', 0)}")
    material = gen(rng, int(spec.get("chars", 30000)))
    ask = str(spec.get("ask", "")).rstrip()
    placement = spec.get("placement", "after")
    if placement == "before":
        content = f"{material}\n\n{ask}"
    else:
        content = f"{ask}\n\n{material}"

    envelope = spec.get("envelope", "chat")
    messages: list[dict[str, Any]] = []
    if envelope == "chat":
        messages.append({"role": "system", "content": SYS_CHAT})
    elif envelope == "pipeline":
        messages.append({"role": "system", "content": SYS_PIPELINE})
    elif envelope == "agent":
        messages.append({"role": "system", "content": SYS_AGENT})
    elif envelope != "bare":
        raise ValueError(f"unknown envelope {envelope!r}")
    messages.append({"role": "user", "content": content})

    body: dict[str, Any] = {"messages": messages, "stream": bool(spec.get("stream", True))}
    if envelope == "agent":
        body["tools"] = _agent_tools()
    return body


def _agent_tools() -> list[dict[str, Any]]:
    def fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }

    return [
        fn("read_file", "Read a file from disk. Returns numbered lines.",
           {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
           ["path"]),
        fn("edit_file", "Replace an exact string in a file with new text.",
           {"path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"}}, ["path", "old_string", "new_string"]),
        fn("bash", "Run a shell command in the working directory.",
           {"command": {"type": "string"}, "timeout_ms": {"type": "integer"}}, ["command"]),
        fn("grep", "Search file contents with a regular expression.",
           {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    ]


# --- the generators -----------------------------------------------------

SERVICES = ("ledger-api", "settle-worker", "fx-quoter", "webhook-fanout", "ledger-read")
ROUTES = (
    "/v1/accounts/{id}/entries",
    "/v1/transfers",
    "/v1/transfers/{id}/reverse",
    "/v1/fx/quote",
    "/v1/batches/{id}/settle",
    "/internal/health",
)
LOG_LEVELS = ("INFO", "INFO", "INFO", "INFO", "WARN", "ERROR")
ERRORS = (
    "upstream connection reset by peer",
    "deadline exceeded after 2500ms",
    "duplicate idempotency key",
    "ledger entry would leave account negative",
    "fx rate older than max_age_ms=1500",
    "pool exhausted: 32/32 connections in use",
)


def _rid(rng: random.Random) -> str:
    return "req_" + "".join(rng.choice("0123456789abcdef") for _ in range(16))


@register("incident_log")
def incident_log(rng: random.Random, chars: int) -> str:
    """Service logs across one incident window, one line per event."""
    lines = [
        "# exported from the log store, window 2026-09-14T09:00:00Z to 2026-09-14T11:00:00Z",
        "# fields: ts level service route status latency_ms request_id detail",
    ]
    minute, second = 0, 0
    total = 0
    while total < chars:
        second += rng.randrange(1, 6)
        if second >= 60:
            second -= 60
            minute += 1
        level = rng.choice(LOG_LEVELS)
        service = rng.choice(SERVICES)
        route = rng.choice(ROUTES)
        rid = _rid(rng)
        if level == "ERROR":
            status = rng.choice((500, 502, 503, 409))
            latency = rng.randrange(1800, 9000)
            detail = rng.choice(ERRORS)
        elif level == "WARN":
            status = rng.choice((200, 429))
            latency = rng.randrange(600, 2500)
            detail = "retry scheduled in " + str(rng.randrange(50, 800)) + "ms"
        else:
            status = 200
            latency = rng.randrange(8, 400)
            detail = "ok"
        line = (
            f"2026-09-14T{9 + minute // 60:02d}:{minute % 60:02d}:{second:02d}.{rng.randrange(100, 999)}Z "
            f"{level:<5s} {service:<14s} {route:<32s} {status} {latency:>5d}ms {rid} {detail}"
        )
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


CLAUSE_SUBJECTS = (
    "expense reimbursement", "contractor onboarding", "data retention",
    "customer refunds", "incident escalation", "access review",
    "vendor security review", "travel booking", "equipment purchase",
    "record disposal", "background checks", "change approval",
)
CLAUSE_VERBS = (
    "must be approved in writing by", "may be delegated to", "is reviewed quarterly by",
    "requires a second signature from", "is recorded in the register maintained by",
)
CLAUSE_OWNERS = (
    "the finance controller", "the head of engineering", "the data protection lead",
    "the on-call incident manager", "the procurement team", "the office manager",
)


@register("policy_document")
def policy_document(rng: random.Random, chars: int) -> str:
    """A numbered internal policy with exceptions buried in the clauses."""
    out = [
        "OPERATIONS HANDBOOK, REVISION 14",
        "Effective 1 March 2026. Supersedes revision 13 in full.",
        "",
    ]
    total = sum(len(x) for x in out)
    section = 0
    while total < chars:
        section += 1
        title = rng.choice(CLAUSE_SUBJECTS).title()
        out.append(f"{section}. {title}")
        out.append("")
        for clause in range(1, rng.randrange(4, 9)):
            owner = rng.choice(CLAUSE_OWNERS)
            verb = rng.choice(CLAUSE_VERBS)
            amount = rng.randrange(1, 40) * 250
            body = (
                f"{section}.{clause} Any {rng.choice(CLAUSE_SUBJECTS)} above "
                f"{amount} euro {verb} {owner}. Where the amount is at or below "
                f"{amount} euro the requester may proceed and file the record "
                f"within {rng.randrange(2, 15)} working days."
            )
            if rng.random() < 0.18:
                body += (
                    " This clause does not apply during a declared incident, when "
                    f"{rng.choice(CLAUSE_OWNERS)} may authorise the spend and "
                    "reconcile it afterwards."
                )
            out.append(body)
            out.append("")
            total += len(body) + 2
    return "\n".join(out)


PY_NOUNS = ("ledger", "entry", "transfer", "batch", "quote", "account", "reversal", "hold")
PY_VERBS = ("validate", "normalise", "apply", "settle", "reconcile", "expand", "collapse")


@register("python_module")
def python_module(rng: random.Random, chars: int) -> str:
    """A long Python source file, many small functions, a few with real bugs."""
    out = [
        '"""Ledger posting helpers.',
        "",
        "Generated fixture. Every function here is deliberately small so the file",
        "is long rather than deep.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from decimal import Decimal",
        "",
    ]
    total = sum(len(x) for x in out)
    i = 0
    while total < chars:
        i += 1
        verb = rng.choice(PY_VERBS)
        noun = rng.choice(PY_NOUNS)
        name = f"{verb}_{noun}_{i}"
        limit = rng.randrange(2, 99) * 100
        block = [
            f"def {name}(rows: list[dict], *, limit: int = {limit}) -> list[dict]:",
            f'    """{verb.title()} the {noun} rows and drop anything over the limit."""',
            "    out = []",
            "    for row in rows:",
            f"        amount = Decimal(str(row.get('amount_cents', 0))) / 100",
            "        if amount > limit:",
            "            continue",
            f"        row = dict(row, {noun}_checked=True)",
            "        out.append(row)",
            "    return out",
            "",
            "",
        ]
        for line in block:
            out.append(line)
            total += len(line) + 1
    return "\n".join(out)


SPEAKERS = ("Priya", "Tomas", "Dana", "Karl", "Mei", "Ruth", "Owen")
MEETING_LINES = (
    "I think the number we quoted last week was the gross figure, not the net one.",
    "We can ship it behind a flag and turn it on for one tenant first.",
    "The migration has to run before the month-end close, not after.",
    "That assumes the fx rate is refreshed every second, and it is not.",
    "Nobody has owned that alert since the team split in June.",
    "Can we agree the rollback is a config change and not a redeploy?",
    "The contract says thirty days, but the renewal notice went out late.",
    "Two of the three regions are already on the new schema.",
    "If we do that we break the webhook ordering guarantee.",
    "Let us park that and come back to it once we have the numbers.",
)


@register("meeting_transcript")
def meeting_transcript(rng: random.Random, chars: int) -> str:
    """A multi-speaker transcript with timestamps and crosstalk."""
    out = [
        "Transcript, weekly platform sync, 2026-09-11, 45 minutes, 7 attendees.",
        "Automatic transcription. Speaker labels may be wrong where voices overlap.",
        "",
    ]
    total = sum(len(x) for x in out)
    minute, second = 0, 0
    while total < chars:
        second += rng.randrange(4, 40)
        if second >= 60:
            second -= 60
            minute += 1
        speaker = rng.choice(SPEAKERS)
        n = rng.randrange(1, 4)
        text = " ".join(rng.choice(MEETING_LINES) for _ in range(n))
        line = f"[00:{minute:02d}:{second:02d}] {speaker}: {text}"
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


MERCHANTS = (
    "Northgate Supplies", "Kelso Freight", "Halden Systems", "Brassmill Ltd",
    "Verda Energy", "Oakline Travel", "Pike & Cowan", "Ilmara Software",
)
CURRENCIES = ("EUR", "USD", "GBP", "SEK", "PLN")


@register("csv_ledger")
def csv_ledger(rng: random.Random, chars: int) -> str:
    """A wide transaction export, one row per line."""
    header = (
        "txn_id,posted_at,merchant,currency,amount_cents,fx_rate,category,"
        "cost_centre,approved_by,memo"
    )
    out = [header]
    total = len(header)
    i = 0
    while total < chars:
        i += 1
        row = ",".join(
            [
                f"T{100000 + i}",
                f"2026-{rng.randrange(1, 10):02d}-{rng.randrange(1, 29):02d}",
                rng.choice(MERCHANTS),
                rng.choice(CURRENCIES),
                str(rng.randrange(-400000, 900000)),
                f"{rng.uniform(0.8, 1.4):.5f}",
                rng.choice(("software", "travel", "freight", "energy", "legal", "hardware")),
                f"CC-{rng.randrange(100, 999)}",
                rng.choice(CLAUSE_OWNERS).replace("the ", "").replace(" ", "_"),
                rng.choice(
                    (
                        "quarterly licence",
                        "rebooked after cancellation",
                        "partial credit note applied",
                        "duplicate, pending reversal",
                        "",
                    )
                ),
            ]
        )
        out.append(row)
        total += len(row) + 1
    return "\n".join(out)


@register("api_changelog")
def api_changelog(rng: random.Random, chars: int) -> str:
    """A versioned changelog where breaking changes hide among the rest."""
    out = ["# Public API changelog", ""]
    total = 24
    major, minor, patch = 4, 0, 0
    while total < chars:
        patch += 1
        if patch > 9:
            patch, minor = 0, minor + 1
        if minor > 9:
            minor, major = 0, major + 1
        head = f"## {major}.{minor}.{patch} - 2026-{rng.randrange(1, 10):02d}-{rng.randrange(1, 29):02d}"
        out.append(head)
        out.append("")
        total += len(head) + 2
        for _ in range(rng.randrange(2, 6)):
            kind = rng.choices(
                ("Added", "Fixed", "Changed", "Deprecated", "BREAKING"),
                weights=(4, 5, 3, 2, 1),
            )[0]
            route = rng.choice(ROUTES)
            if kind == "BREAKING":
                body = (
                    f"- **BREAKING** `{route}` no longer accepts "
                    f"`{rng.choice(('amount', 'rate', 'ref', 'memo'))}` as a query "
                    "parameter. Send it in the body instead."
                )
            else:
                body = (
                    f"- {kind}: `{route}` now "
                    f"{rng.choice(('returns', 'accepts', 'validates', 'rejects'))} "
                    f"`{rng.choice(('settled_at', 'fx_age_ms', 'idempotency_key', 'reversal_of'))}`."
                )
            out.append(body)
            total += len(body) + 1
        out.append("")
        total += 1
    return "\n".join(out)
