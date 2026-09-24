# Quota-aware routing

[Documentation index](../README.md) · [Project home](../../README.md)

Quota affects new admissions, never an existing strict binding. For overlapping windows, use the [coherent-window configuration](../SEMANTIC_POLICY.md#quota-windows).

## Quota-aware routing

Several providers are flat-rate subscriptions with a usage window rather than
a metered bill. When one of them is being spent faster than its window, new
conversations should lean on the others, and the expensive model should be
saved for the work that needs it.

The router measures that as one number per provider, called pressure, in the
range 0 to 1. Pressure is computed from pace, not from raw percent: a provider
20 points ahead of where the window says it should be is under pressure at 40%
spent, and a provider on pace is neutral at 80%.

Everything about it fails neutral. A provider with no quota source, a source
that is switched off, a command that fails, a field that is missing and a
snapshot that has aged out all mean pressure 0, which routes exactly as the
router routed before quota existed. Every source ships disabled, so a fresh
install runs no subprocess.

### A quota source

Sources are registered by name, like deciders. Three ship: `command`, `static`
and `none`.

```yaml
providers:
  frontier-vendor:
    quota:
      source: command
      enabled: true
      poll_seconds: 300           # a background task, never the request path
      stale_after_seconds: 1800   # older than this means pressure 0
      config:
        argv: [usage-cli, usage, --provider, codex, --json-only]
        timeout_ms: 5000
        used_percent:
          - "0.usage.secondary.usedPercent"
          - "0.usage.primary.usedPercent"
        expected_used_percent:
          - "0.pace.secondary.expectedUsedPercent"
        window_minutes:
          - "0.usage.secondary.windowMinutes"
        resets_at:
          - "0.usage.secondary.resetsAt"
          - "0.usage.primary.resetsAt"
```

`argv` is a list, never a shell string, so nothing is interpolated and no
shell is involved. The program must print JSON on stdout. Each field is a list
of dotted paths and the first one that yields a value wins: a numeric step
indexes a list, so `0.usage.primary.usedPercent` means "the first element,
then `usage`, then `primary`, then `usedPercent`".

The path lists exist because providers fill in different windows. This is a
worked example against the open-source CodexBar CLI, which prints

```
$ codexbar usage --provider codex --json-only
[{"provider":"codex",
  "usage":{"secondary":{"usedPercent":20,"windowMinutes":10080,
                        "resetsAt":"2026-09-24T15:42:05Z"},
           "primary":null},
  "pace":{"secondary":{"expectedUsedPercent":29,"willLastToReset":true}}}]

$ codexbar usage --provider grok --json-only
[{"provider":"grok",
  "usage":{"primary":{"usedPercent":6,"resetsAt":"2026-09-20T21:28:25Z"}}}]
```

One fills in `secondary` and reports a pace; the other fills in `primary` and
does not. The same config reads both. A third provider may report no usage at
all, and then it has no quota source: `source: none`, and its pressure comes
from the circuit breaker alone.

The config for the second one would be

```yaml
providers:
  other-vendor:
    quota:
      source: command
      enabled: true
      config:
        argv: [codexbar, usage, --provider, grok, --json-only]
        used_percent: ["0.usage.primary.usedPercent", "0.usage.secondary.usedPercent"]
        resets_at: ["0.usage.primary.resetsAt", "0.usage.secondary.resetsAt"]
```

`static` takes the numbers straight from `config` and is for tests and demos.
`none` reports nothing.

To add a source, write a class with `async def fetch() -> QuotaSnapshot` in
`src/jev_router/quota.py` and register a factory with
`@register_quota_source("name")`. A source may never raise: return a snapshot
carrying an `error` instead.

### How pressure is computed

```yaml
quota_policy:
  enabled: true
  pressure_starts_at: 0        # points ahead of pace before pressure begins
  pressure_full_at: 25         # points ahead of pace that means 1.0
  full_used_percent: 90        # used at or above this is 1.0 whatever the pace
  raw_starts_at: 60            # only when there is no pace to compare with
  raw_full_at: 95
  hysteresis: 0.05             # a smaller move than this is ignored
  max_change_per_poll: 0.25    # one poll may not swing routing
  demote_above: 0.6            # a route entry over this drops behind healthy ones
  exhausted_at: 100            # a fresh window at this refuses new strict bindings
```

The expected percent comes from the source when it reports one, otherwise from
`window_minutes` and `resets_at`: a window that is 40% elapsed should be about
40% spent. With neither, the raw percent is used with its own knees.

Hysteresis holds a value that moved only a little, so pressure does not flap
around a knee, and `max_change_per_poll` stops one bad reading from swinging
routing in a single step.

A provider with no quota source takes its pressure from the circuit breaker
instead: 1.0 while the breaker is open, then decaying to 0 over
`recovery_seconds` after it closes. A provider with both takes the higher of
the two.

### What pressure does

Three things, all bounded.

**It shifts a threshold, on a legacy alias only.** Strict admission, which
is what the shipped `auto` uses, matches every cutoff at zero pressure, so
there pressure can only choose between the pairs a rule's quality lane
qualifies. A legacy rule condition may say which provider it is sensitive to
and by how much:

```yaml
- name: hard
  when:
    difficulty: {gte: 2.4, shift_with: frontier-vendor, max_shift: 0.4}
  use: {route: frontier_high}
```

The effective cutoff is `2.4 + pressure × 0.4`. It is bounded by `max_shift`,
monotone in pressure, and only ever rises, so pressure can only make the
pressured provider harder to reach, never easier.

**It reorders a route.** Entries whose provider is under more pressure than
`demote_above` move behind the healthy entries of the same route, keeping
their order among themselves. An entry may only move ahead of the primary if
it is marked `equivalent: true`.

**It leaves some rules alone.** A rule with `protected: true` ignores every
shift. Those are the moves that exist because the request needs them: vision,
harm if wrong, a context-overflow move. A protected rule still uses its
route's fallbacks when the provider is actually failing.

At full pressure on a healthy provider, the frontier route still serves the
protected rules and the top difficulty band. Everything else moves to the next
entry in its route, or falls through to a cheaper rule.

Pressure never touches a pinned conversation. It affects new decisions only.

### When a window is spent

Pressure is about pace. Whether a provider can serve at all is a separate
fact: a fresh window used at or above `exhausted_at` means that provider is
spent until the window resets. A strict binding never moves, so binding one to
a spent provider would promise a session that can only fail. Strict admission
refuses it instead with `PROVIDER_UNAVAILABLE` (503), naming the window, its
`resets_at` and `retry_after_seconds`. The caller decides what next: wait,
resolve again with `candidate_models` naming another model, or use a concrete
model. Stale, errored and unknown readings never count as spent, and an
existing binding is not touched. `quota --poll` prints `EXHAUSTED` beside a
spent provider.

`exhausted_at: 100` refuses only a fully spent window. Lower it to keep a
reserve for sessions already running.

### Saving a provider on purpose

Pressure never lowers the quality bar for `auto`. When you would rather keep
working on a weaker model than stop, say so by resolving `auto-conserve`
instead. It asks Jev the same questions and routes the same way except for
the work `auto` sends to astra:

| work | `auto` | `auto-conserve` |
|---|---|---|
| very hard (difficulty 3.0 and up) | astra xhigh | astra xhigh |
| high harm if wrong | astra medium | astra medium |
| hard | astra high | grok high |
| hard image, careful moderate | astra | grok low |
| Jev unsure, Jev down, no rule matched | astra medium | grok low |

grok is not qualified for that work, so those decisions record
`evidence: weak`. The choice is yours and is made per session: no quota
reading switches it on, and an open session keeps what it was bound to. For
the Codex adapter, restart it with `--alias auto-conserve`.

### Seeing it

```sh
uv run jev-router quota --poll            # snapshots, pressures, staleness, errors
uv run jev-router quota --poll --json
uv run jev-router explain request.json --pressure frontier-vendor=0.8
```

`GET /router/quota` returns the same report, and `GET /router/providers`
returns the breaker state. Every decision is logged with the pressure per
provider, any threshold that was shifted and by how much, and whether the
route order changed. A response carries `X-Router-Pressure` only when a shift
or a reorder actually changed the outcome.

Set `quota_policy.enabled: false` to turn the whole mechanism off. Pressures
are then empty and no rule is ever shifted.
