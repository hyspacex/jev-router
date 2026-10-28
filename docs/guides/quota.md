# Quota-aware routing

[Documentation index](../README.md) · [Project home](../../README.md)

Quota affects new admissions, never an existing strict binding. For overlapping windows, use the [coherent-window configuration](../SEMANTIC_POLICY.md#quota-windows).

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

## A quota source

Sources are registered by name, like deciders. Four ship: `codexbar`,
`command`, `static` and `none`.

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
does not. The same config reads both. A provider that reports no usage at all
has no quota source: `source: none`, and its pressure comes from the circuit
breaker alone.

Ollama publishes its usage on the ollama.com settings page, not in its API.
The shipped `ollama` provider reads it through the [CodexBar
source](#the-codexbar-source): CodexBar.app reads the page with its own
ollama.com session and writes the numbers to its snapshot. To read it with a
`command` source instead, run `codexbar usage --provider ollama --source web`,
which needs the browser's ollama.com session cookie. Sign in to ollama.com in
Chrome on the machine that runs the router, then import the cookie once from a
terminal on that machine, where macOS can show the Keychain prompt:

```sh
codexbar cookie refresh --provider ollama --allow-keychain-prompt
```

The CLI will not import a browser cookie in the background without that
prompt, even when the CodexBar app on the same machine already shows Ollama's
usage. Until it has a cookie the source reports an error, which is pressure 0.

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

## The CodexBar source

CodexBar.app writes every subscription it watches to
`~/Library/Group Containers/<team>.com.steipete.codexbar/widget-snapshot.json`
about once a minute, web-cookie providers such as Ollama included. The
`codexbar` source reads one subscription out of that file. It takes two
switches: the top-level `codexbar` block, and the provider's own quota block.

```yaml
codexbar:
  enabled: true                     # off in the shipped router.yaml
  cli_binary: codexbar              # "" turns the CLI fallback off
  cli_fallback_after_seconds: 300
  include_external_subscriptions: true
  poll_interval_seconds: 60         # tracked subscriptions only
  stale_after_seconds: 1800         # tracked subscriptions only

providers:
  openai:
    quota:
      source: codexbar
      codexbar_provider: codex      # CodexBar's id for the subscription
      enabled: true
      poll_seconds: 60
      stale_after_seconds: 900
```

An enabled `codexbar` source needs `codexbar.enabled: true`; with the block
off, `check-config` refuses the file. A CodexBar id may be linked to one
provider only, so one allowance is never counted twice. The shipped
router.yaml links `openai` to `codex` and `ollama` to `ollama`, with the block
and both sources off. `snapshot_path` is optional: unset, the router looks in
CodexBar's default container, then in any other CodexBar container that has
the file.

Each window CodexBar reports (`primary`, `secondary`, `tertiary`) becomes a
window record of its own, so pressure, exhaustion and pace work exactly as
they do for a `command` source. A reading is dated by when CodexBar measured
it, not when the file was read, so a snapshot CodexBar has stopped refreshing
goes stale like any other reading.

When the file is missing, older than `cli_fallback_after_seconds`, or has no
entry for the subscription, the source runs `codexbar usage --provider <id>
--json-only` through the `command` machinery, at most once per that many
seconds. A missing file with no CLI installed is `unknown`; a CLI that fails
is an `error`. Both are pressure 0. The CLI has no browser session, so for a
web-cookie provider such as Ollama it usually finds nothing; the snapshot is
the source that works there.

### Tracked subscriptions

CodexBar may watch subscriptions the router does not route to, such as a
Claude or Antigravity plan. With `codexbar.enabled` and
`include_external_subscriptions` both on, every subscription CodexBar watches
that no enabled provider links to is read too, every `poll_interval_seconds`,
through the same snapshot-then-CLI path. The list comes from the snapshot and
from CodexBar's own config; when both can be read, a subscription has to be
enabled in both.

A tracked subscription is for looking at. It is shown on the dashboard
overview and returned under `subscriptions.tracked` by
`GET /router/audit/overview` and `GET /router/quota`: its windows with pace,
plan rows, CodexBar's token and cost rollups, and whether the reading is fresh
(older than `codexbar.stale_after_seconds` is stale). It is never pressure,
never counts as spent, never refuses a binding and never changes a route. A
subscription becomes routed only by linking it to a provider with
`codexbar_provider`.

CodexBar's own `config_path` is read for provider ids and `enabled` flags
only; it can hold API keys, and nothing else in it is kept. Only numbers and
short labels come out of the snapshot.

To add a source, write a class with `async def fetch() -> QuotaSnapshot` in
`src/jev_router/quota.py` and register a factory with
`@register_quota_source("name")`. A source may never raise: return a snapshot
carrying an `error` instead.

## How pressure is computed

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

## What pressure does

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

**It reorders a route.** Entries whose pressure is over `demote_above` move
behind the healthy entries of the same route, keeping their order among
themselves. An entry's pressure is its provider's times its model's
`quota_cost`, so a model that costs a fifth as much is a fifth as pressured.
An entry may only move ahead of the primary if it is marked
`equivalent: true`.

**It leaves some rules alone.** A rule with `protected: true` ignores every
shift. Those are the moves that exist because the request needs them: vision,
harm if wrong, a context-overflow move. A protected rule still uses its
route's fallbacks when the provider is actually failing.

At full pressure on a healthy provider, the frontier route still serves the
protected rules and the top difficulty band. Everything else moves to the next
entry in its route, or falls through to a cheaper rule.

Pressure never touches a pinned conversation. It affects new decisions only.

## When a window is spent

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

## Balancing in both directions

`auto` moves work away from whichever subscription is busy, but only between
pairs the admission test measured as equally good for that work
(`evals/ADMISSION.md`, steps `mid6`, `faithful6`, `fast6` and `hard6`):

| work | primary | when its provider is over `demote_above` |
|---|---|---|
| easy | glm-5.3-flash | Ollama busy: gpt-6-luna low |
| moderate, with or without tools | glm-5.3 | Ollama busy: gpt-6-sol low |
| careful moderate | astra medium | OpenAI busy: glm-5.3 |
| hard | astra high | OpenAI busy: gpt-6-sol xhigh, a fifth of astra's cost |
| hardest, high harm, images | astra | stays on astra |

The last row has no measured equivalent, so no pressure moves it. Easy image
work stays on gemma4-31b: gpt-6-luna misread an image in the test. Every
decision is `evidence: qualified`, because every move lands on a measured
pair. A session keeps what it was bound to; balancing only affects new
admissions.

`auto-conserve`, which sent astra's work to grok by hand, was retired on
2026-09-24. It now routes exactly as `auto` and is kept only so sessions bound
under it still run.

## Seeing it

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
