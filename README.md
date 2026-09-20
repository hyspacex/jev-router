# jev-router

A small HTTP shim that sits in front of any OpenAI-compatible proxy or API.
Send `model: "auto"` and the router works out what kind of request it is, picks
a model and a reasoning effort, rewrites the `model` field and forwards it.
Send any other model name and the request passes through byte for byte. The
choice is made by [TypeSafe's Jev](https://docs.typesafe.ai), a decision model:
you hand it a state and a set of typed questions, and it returns typed answers
with probabilities attached. It writes no prose, so the router can read its
answers as data and run them through rules you control.

## How it works

```
your client
   |  model: "auto"
   v
jev-router  :8318 ----------> Jev  (one call, several questions, ~0.3 s)
   |  rewrite model
   v
your proxy  :8317
```

For every request to `/v1/chat/completions`:

1. Look at `model`. If it is not an alias defined in `router.yaml`, forward the
   original bytes and stop.
2. Compute a versioned conversation key scoped by hashed `Authorization`, alias,
   `X-Router-Client`, system prompt and first user message. Revalidate any live
   pin against current model permissions, effort bounds, tools, vision and context.
   Reuse a valid pin without Jev. If it is incompatible, choose the smallest
   eligible replacement, which may have a smaller window when capabilities change.
3. Compute features in code: an estimated token count, message count, tools,
   images, code blocks, code-block languages, and the `X-Router-Client` header.
   Jev is never asked to count anything.
4. Build the state. This is a short summary of the request, not the transcript:
   the latest user request, the earlier request when the latest one only
   continues it, pasted bulk under a field name that marks it as quoted
   material, and the computed facts.
5. Ask Jev the alias's questions in one call.
6. Run the answers through the rules. A confidence gate runs first, then the
   rules in order, first match wins, then the default. Afterwards the router
   applies the alias's effort cap, any client floor, and the capability filters.
7. Rewrite `model`, forward, and stream the upstream bytes back untouched. If
   the rule named a route rather than a model, and the upstream answers 429 or
   5xx or will not connect, try the route's next entry before any byte has
   been streamed.

If the Jev call fails for any reason, the router falls back to a no-network
decider that guesses from counts, or to the default route. A fallback decision
is never pinned, so the next turn tries Jev again. Jev being down never blocks
a request that has an eligible route. Malformed HTTP-200 Jev answers also fall
back. Impossible hard constraints return HTTP 422 with `type: routing_error`;
recovery never silently discards configured restrictions.

New and replacement pins are committed only after upstream **2xx headers**.
A later stream failure does not undo the pin or retry another model, but is
recorded separately. Reused pins keep their original TTL. Upgrading from an
unscoped key causes one fresh decision per conversation; legacy pins are not reused.

Providers, models and routes are separate things. A provider is one account or
subscription; a model names the provider it belongs to; a route is a named
lane with a primary model and ordered fallbacks. That split is what lets the
router skip a provider that is failing, and lean on one subscription when
another is being spent too fast. See
[Quota-aware routing](#quota-aware-routing).

### The other two paths

Everything above is the legacy `auto` path and it has not changed. Two more
sit beside it, both opt-in, and an alias that does not ask for them behaves
exactly as it did.

- **Strict sessions.** A coding harness sizes its history against one model's
  window and holds a prompt cache, so a model that changes halfway through a
  task is a bug rather than a saving. A strict alias resolves one profile
  through `POST /router/resolve` and never moves it: no repin, no TTL, no
  cross-model fallback. A request that no longer fits is refused with a
  machine-readable code. See [`docs/SESSION_ROUTING.md`](docs/SESSION_ROUTING.md).
- **Between-turn effort.** Inside a strict session, at a reported new-user-turn
  boundary, the router may change the reasoning effort of the model the session
  is already bound to. The model, the provider, the negotiated limits and the
  base effort all stay put. It is an experiment, it is off in the shipped
  `router.yaml`, and no deployment there is qualified for it. See
  [`docs/ADAPTIVE_EFFORT.md`](docs/ADAPTIVE_EFFORT.md).

What chooses a strict binding in the first place is in
[`docs/SEMANTIC_POLICY.md`](docs/SEMANTIC_POLICY.md): the semantic packet, the
shadow questions that are measured and read by nothing, the quality lanes that
bound what quota pressure may do, the coherent quota windows, and replaying a
decision from its own row.
[`docs/README.md`](docs/README.md) is the index, and it lists the known issues.

## Quick start

```sh
uv sync
export TYPESAFE_API_KEY=...                 # see .env.example
uv run jev-router check-config              # validate router.yaml
uv run jev-router serve                     # listens on 127.0.0.1:8318
```

Set `settings.upstream_base_url` in `router.yaml` to your proxy, or export
`JEV_ROUTER_UPSTREAM` to override it. The default is `http://127.0.0.1:8317`.
The router holds no upstream key of its own: it passes the client's
`Authorization` header through, so the proxy keeps doing authentication.

Without `TYPESAFE_API_KEY` the router still serves every request. It falls back
to the `rules` decider and marks the decision as a fallback.

Point a client at `http://127.0.0.1:8318/v1` instead of the proxy's own port.

```sh
curl http://127.0.0.1:8318/v1/chat/completions \
  -H "Authorization: Bearer $UPSTREAM_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Router-Client: my-cli" \
  -d '{"model":"auto","messages":[{"role":"user","content":"What is 2+2?"}]}'
```

The reply carries the routing headers:

| Header | Meaning |
| --- | --- |
| `X-Router-Model` | the model name sent upstream, effort suffix included |
| `X-Router-Effort` | the effort that was chosen, empty when the model has none |
| `X-Router-Rule` | the rule that fired, or `pin`, `low_confidence`, `default`, `fallback` |
| `X-Router-Pinned` | `true` when the conversation already had a model |
| `X-Router-Decision` | the decision id, for feedback |
| `X-Router-Fallback` | upstream fallback entry index; absent on the primary |
| `X-Router-Decider-Fallback` | `true` when the decision used temporary local fallback |
| `X-Router-Shadow-Model` | in shadow mode, what would have been chosen |

### Other commands

```sh
uv run jev-router explain request.json      # features, state, Jev answers, chosen model
uv run jev-router explain request.json --pressure openai=0.8
uv run jev-router quota --poll              # quota snapshots and pressure per provider
uv run jev-router quota --poll --json
uv run jev-router decisions -n 20           # tail the decision log
uv run jev-router decisions replay last     # re-run one decision from its own row
uv run jev-router feedback last too_weak --model gpt-6-astra --effort high
uv run jev-router sessions list             # strict session bindings
uv run jev-router sessions show <id>
uv run jev-router sessions close <id>
uv run jev-router sessions reconcile <id> --plan <plan-id> --outcome applied
uv run jev-router prune --older-than-days 30   # irreversible
uv run jev-router adapter --alias <strict-alias> --port 18320
```

Every command takes `-c/--config` for the path to `router.yaml`, which also
reads from `$JEV_ROUTER_CONFIG` and defaults to `router.yaml` in the working
directory. `serve` takes `--host`, `--port`, `--mode shadow|active` and
`--log-level`.

`explain` never forwards the request. Its argument is a JSON file holding a
chat-completions body; an optional `_headers` object inside it stands in for
request headers, and `--alias` treats the body as a different alias.
`--pressure provider=N` pretends a provider is under that much quota pressure
and shows what the decision would be.

`quota` prints the same report as `GET /router/quota`. Without `--poll` it
shows the configuration and nothing else; with it, each enabled source runs
once.

`decisions replay` re-runs a stored decision from the row alone: the config
hash, the answers, the request facts and the pressure it was taken under. No
Jev call, no quota poll, no prompt. It says so when the config hash has moved
rather than claiming a match. `sessions` and `adapter` belong to strict
sessions; see [`docs/SESSION_ROUTING.md`](docs/SESSION_ROUTING.md) and
[`docs/ADAPTIVE_EFFORT.md`](docs/ADAPTIVE_EFFORT.md).

### Endpoints

| Path | Method | Behaviour |
| --- | --- | --- |
| `/v1/chat/completions` | POST | aliases are routed; a strict session executes here; everything else is forwarded unchanged |
| `/v1/responses` | POST | a strict session whose profile speaks the Responses protocol executes here; everything else is forwarded unchanged |
| `/v1/responses/compact` | POST | a standalone compaction, forwarded as maintenance for the bound session |
| `/v1/models` | GET | the upstream list plus one entry per alias |
| `/router/health` | GET | mode, upstream, aliases, models, providers, routes, unhealthy providers, decider, whether a Jev key is set, uptime |
| `/router/providers` | GET | circuit breaker state and the pressure per provider |
| `/router/quota` | GET | quota snapshots, pressures, staleness and the last error per provider |
| `/router/decisions?limit=N` | GET | recent decisions as JSON |
| `/router/feedback` | POST | record what you thought of a decision |
| `/router/feedback?limit=N` | GET | recent feedback, joined to its decision |
| `/router/resolve` | POST | resolve or resume a strict session binding |
| `/router/sessions?limit=N` | GET | strict session bindings, newest first |
| `/router/sessions/{id}` | GET | one binding: identity, limits, efforts and state |
| `/router/sessions/{id}/close` | POST | end a session deliberately |
| `/router/turn-plan` | POST | an effort-only decision for a new user turn (experimental) |
| `/router/turn-plan/{id}/reconcile` | POST | say what happened to an ambiguous effort update |
| anything else | any | streamed through to the upstream, method and path unchanged |

An alias name on any other POST endpoint returns 400 with a message saying so.

Every `/router/*` path needs `X-Router-Admin-Token` once the environment
variable named by `settings.admin_token_env` is set; without one, only direct
loopback callers are accepted. The strict-session paths need that credential
always, loopback included, and there is no setting that turns it off:
`resolve`, `turn-plan`, `sessions`, and any managed execution on `/v1/*`.

### Response headers

On an alias request:

| Header | Meaning |
| --- | --- |
| `X-Router-Model` | the model name sent upstream, effort included |
| `X-Router-Effort` | the effort that was chosen |
| `X-Router-Rule` | the rule that fired, or `pin`, `low_confidence`, `default` or `fallback` |
| `X-Router-Route` | the named route the rule pointed at, when it used one |
| `X-Router-Pinned` | whether this came from a pin |
| `X-Router-Decision` | the decision id, for `jev-router feedback` |
| `X-Router-Fallback` | the index of the route entry that served the request; absent on the primary |
| `X-Router-Pressure` | the pressure per provider, only when it changed the outcome |
| `X-Router-Decider-Fallback` | the decision came from the fallback decider, not from Jev |
| `X-Router-Shadow-Model` | in shadow mode, what would have been chosen |

On a strict session's execution:

| Header | Meaning |
| --- | --- |
| `X-Router-Model` | the bound profile's wire model |
| `X-Router-Effort` | the **base** effort, which never moves for the life of the session |
| `X-Router-Session` | the session id |
| `X-Router-Binding` | the binding revision that served it |
| `X-Router-Session-State` | `active` once accepted, otherwise the stored state |
| `X-Router-Decision` | the decision id the binding was taken under |
| `X-Router-Effective-Effort` | only when adaptation is on: the effort this request really ran at |
| `X-Router-Compaction-Epoch` | only when adaptation is on: how many times the history has been folded up |

### Environment

| Variable | Read by | What it does |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | the router, `evals/` | The Jev key. Named by `settings.jev_api_key_env`. Unset means the `rules` decider and a fallback flag on every decision. |
| `JEV_ROUTER_UPSTREAM` | the router, `evals/` | Overrides `settings.upstream_base_url` whenever it is set and non-empty. The environment wins over the file. |
| `JEV_ROUTER_CONFIG` | the CLI | The default for `-c/--config`, on every subcommand. Defaults to `router.yaml` in the working directory. |
| `JEV_ROUTER_ADMIN_TOKEN` | the router, the adapter, `evals/run_sessions.py`, `evals/qualify_effort.py` | The `/router/*` credential, sent as `X-Router-Admin-Token`. Named by `settings.admin_token_env`. Required for every strict-session request. |
| `UPSTREAM_API_KEY` | `evals/run_outcomes.py`, `evals/qualify_effort.py` | The proxy's own key, for the scripts that call candidate models. The router never reads it: it forwards the client's `Authorization`. |
| `JEV_ROUTER_QUALIFY_LIVE` | `evals/qualify_effort.py` | Must be `1`, alongside `--confirm-live`, before that script makes a real call. |
| `JEV_ROUTER_QUALIFY_ALLOW` | `evals/qualify_effort.py` | Comma-separated profiles the qualification may touch, on top of the in-file allowlist, which is empty. |
| `JEV_EVAL_JUDGE_MODEL` | `evals/run_outcomes.py` | The judge model for the outcome benchmark. Defaults to `gpt-5.6-sol(high)`. |

`.env.example` has the four the router itself cares about.

## Configuring router.yaml

Adding a provider, a model, a question, a route, a rule, an alias or a client
floor is an edit to `router.yaml` alone. Only a new state builder, a new
decider or a new quota source needs Python.

### settings

| Key | Notes |
| --- | --- |
| `host`, `port` | where the shim listens, `127.0.0.1:8318` by default |
| `upstream_base_url` | the proxy to forward to; `$JEV_ROUTER_UPSTREAM` overrides it |
| `jev_url`, `jev_model` | the Jev endpoint and the model id, pinned to an exact version |
| `jev_timeout_ms` | how long a Jev call may take before the router falls back |
| `jev_api_key_env` | the environment variable holding the Jev key |
| `pin_ttl_seconds` | how long a conversation keeps its model |
| `mode` | `active` or `shadow` |
| `sqlite_path` | the pins, decisions and feedback database |
| `default_route` | the model and effort used when nothing else applies |
| `effort_order` | every effort name, weakest to strongest |
| `log_state` | when true, also store the state sent to Jev |
| `decider`, `fallback_decider` | which decider runs, and which one covers a Jev failure |
| `upstream_connect_timeout_s`, `upstream_read_timeout_s` | forwarding timeouts |
| `capture_usage_max_bytes` | maximum buffered and decoded non-streamed usage copy |
| `max_body_bytes` | request limit, including chunked bodies; default 16 MiB |
| `max_concurrent_requests` | in-flight request and upstream connection bound; default 64 |
| `admin_token_env` | router endpoint token variable; default `JEV_ROUTER_ADMIN_TOKEN` |
| `breaker` | the default circuit breaker, which a provider may override |

Every key in every block is checked. An unknown one anywhere is a config error,
not a silently ignored line.

Beside `settings`, `providers`, `models`, `routes`, `questions`, `policy`,
`rulesets`, `aliases` and `clients`, four top-level blocks configure the newer
parts. All four default to off, and a `router.yaml` written before them behaves
exactly as it did.

| Block | What it is | Default |
| --- | --- | --- |
| `session_routing` | the strict session service | `enabled: false` |
| `semantic_policy` | which questions decide and which only ride along | no shadow questions, `distribution_policy: off` |
| `quality_lanes` | the model and effort pairs somebody measured per kind of work, each with its evidence | none configured |
| `experiments.adaptive_effort` | between-turn effort changes | `mode: "off"`, `qualified_profiles: []` |

An alias opts into the first with `session_mode: strict` and into the last with
`adaptive_effort: true`; a rule joins a lane with `lane: <name>`. The shipped
`router.yaml` enables `session_routing`, gives `auto-session` a strict mode,
runs `distribution_policy: shadow`, declares four lanes, and keeps the effort
experiment off.

### A provider

A provider is one account or subscription that several models share. Rate
limits, outages and quota belong to the provider, not to the model.

```yaml
providers:
  frontier-vendor:
    description: Flat-rate subscription with a usage window.
    breaker:                     # optional, falls back to settings.breaker
      failures_to_open: 3
      cooldown_seconds: 120
      max_cooldown_seconds: 1920
      recovery_seconds: 120
    quota:
      source: none
      enabled: false
```

Declaring a `providers:` block makes `provider` required on every model. A
config with no `providers:` block still works: every model then belongs to one
implicit provider called `default`.

### A model

```yaml
models:
  vendor/fast-model:
    upstream_id: vendor/fast-model
    provider: frontier-vendor
    context_window: 256000
    supports_tools: true
    supports_vision: false
    efforts: [none, low, high]
    default_effort: none      # optional
    effort_style: suffix      # suffix -> "model(high)", param -> reasoning_effort, none
    tags: [fast]
    description: Quick answers.
```

`upstream_id` is the name sent to the proxy. `effort_style` decides how the
effort is expressed: `suffix` appends `(effort)` to the model name, `param`
sets `reasoning_effort` in the body, and `none` drops the effort. An effort a
model does not list is clamped down to the closest one it does.

`default_effort` is for a model that reasons without limit when it is sent
bare. Such a model can spend its whole token budget thinking and return an
empty reply. Naming a default here means no code path can send the model
without an effort: it is filled in when clamping produces nothing, and again
in `apply_effort` as a last guard. It must be one of the model's own `efforts`.

Then name the model in a rule or a route and add it to the alias's
`allowed_models`.

### A route

A route is a named lane: one primary and an ordered list of fallbacks. A rule
points at it by name, and the forwarder walks the list when the upstream
refuses to answer.

```yaml
routes:
  frontier:
    description: Hard work.
    primary: {model: vendor/strong, effort: high}
    fallbacks:
      - {model: other-vendor/strong, effort: low, equivalent: true}
      - {model: vendor/fast-model, effort: none}
```

The router moves to the next entry when the upstream answers 429 or any 5xx,
refuses the connection, or times out before the first response byte. It never
switches after a byte has reached the client: the decision is made on the
response status, which arrives before anything is streamed. An entry whose
provider's breaker is open is skipped, and an entry that cannot serve the
request at all (no tool calling, no vision, too small a context window) is
left out of the plan.

A fallback that served the request sets `X-Router-Fallback: <n>` and is logged
with the reason each earlier entry failed. Only a 2xx response from a non-temporary
decision establishes a pin. If every entry fails, the next request decides afresh.
Headers and the decision row identify the actual final entry; `accepted` distinguishes
success from a rejected attempt. `intended_model` and `intended_effort` retain the
policy choice (also in shadow mode, whose `model` now records actual execution).

`equivalent: true` says an entry is as good as the primary for this lane's
work. It is the only thing that lets quota pressure promote an entry ahead of
the primary. Leave it off and the entry can only ever be reached by failure.

A pinned conversation has no fallback plan. Moving it to another model on a
transient 429 would throw away the prompt cache and the habits the pin exists
to keep.

### The circuit breaker

Each provider has one. After `failures_to_open` consecutive 429s, 5xxs or
connection errors it opens, and the router skips that provider's models while
it is open. After `cooldown_seconds` it goes half-open and lets exactly one
request through to find out whether the provider is back. A success closes it;
a failure reopens it with the cooldown doubled, up to `max_cooldown_seconds`.

The state is in memory, per process, and is lost on restart. That is the right
lifetime for a fact about the last two minutes. `GET /router/providers` shows
it. A provider that is the only entry left is tried anyway: refusing to serve
at all would be worse than one wasted call.

### A question

Add it under `questions`. The object is passed to Jev exactly as written, so
any question type Jev supports works without code changes. `type` is `choice`,
`score` or `noul`; `instructions` is required; a `choice` needs a `criteria`
mapping and a `score` needs a `criteria` list of two to ten levels. List the
question id in the alias's `questions`, then use the id as a condition key in a
rule.

Jev reads instructions literally. Write each score level as a standalone
situation, give it examples, and tell Jev to ignore claims the text makes about
itself.

### A rule

Add an entry to `policy.rules`. First match wins.

```yaml
- name: risky_writing
  protected: true
  when:
    task: {in: [high-stakes-writing], conf_gte: 0.6}
    harm_if_wrong: {gte: 0.5}
    est_tokens_gte: 2000
  use: {model: gpt-6-astra, effort: high}
```

`use` takes either `{model, effort}` or `{route: name}`. Both forms work, and
a rule that names a model has no fallbacks.

Conditions on a choice answer take `in`, `not_in`, `eq` and `conf_gte`; on a
score answer `gte`, `lte` and `conf_gte`; on a noul answer `gte` and `lte`.
Score answers are 0-indexed and fractional, so `{gte: 1.8}` sits between the
levels.

A condition may also carry `shift_with` and `max_shift`, which make its cutoff
move with a provider's quota pressure. `protected: true` opts the whole rule
out of those shifts. Both are described under quota below.

Request conditions read the request instead of a Jev answer: `has_tools`,
`has_images`, `has_code`, `stream`, `est_tokens_gte`, `est_tokens_lte`,
`message_count_gte`, `message_count_lte`, `max_tokens_gte`, `client`,
`client_in`, `languages_include` and `tool_names_include`. Adding a new one is
a single entry in `FEATURE_CONDITIONS` in `policy.py`.

`policy.low_confidence` is checked before the rules: when any question it lists
comes back under `min_confidence`, its route is used instead.

### A ruleset

Add a named one under `rulesets` and point an alias at it, or write the rules
inline under the alias's `rules` key.

### An alias

```yaml
aliases:
  auto-cheap:
    description: Shown in GET /v1/models.
    state_builder: last_message_only
    questions: [difficulty]
    rules: policy
    allowed_models: [ollama/glm-5.3-flash]
    max_effort: low
    decider: jev            # optional, overrides settings.decider
```

### A client floor

Add an entry under `clients`, keyed by the value of the `X-Router-Client`
header. Never key it by an API key.

```yaml
clients:
  my-cli:
    min_effort: medium
    allowed_models: [gpt-6-astra]
```

### A state builder

In `src/jev_router/state.py`:

```python
@register("my_builder")
def my_builder(features, config):
    return {"latest_user_request": features.last_user_message[:2000]}
```

It returns a string, dict or list, and must be deterministic. Keep it short.
Jev gets less accurate when the state holds text that does not matter, and
pasted text can argue for its own classification. `squeeze()` replaces bulk
with a count. Name the builder in an alias and compare it against the old one
in the decision log.

### A decider

In `src/jev_router/deciders/`, write a class with
`async def decide(features, alias_cfg) -> Decision` and register a factory:

```python
@register_decider("my_decider")
def _make(config, deps):
    return MyDecider(config, deps["jev_client"])
```

Import it in `deciders/__init__.py`, then set `settings.decider`,
`settings.fallback_decider`, or an alias's `decider`.

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

**It shifts a threshold.** A rule condition may say which provider it is
sensitive to and by how much:

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

## Shadow mode

`settings.mode: shadow` makes the router ask Jev, run the rules and log what it
would have chosen, but send every alias request to `settings.default_route`.
Nothing is pinned. The response carries `X-Router-Shadow-Model`, and the log
row holds the decision with `mode = shadow`. In shadow mode the logged `model`
and `effort` are what the router would have sent, not what was sent.

Run it for a week, read the log, fix the question wording, then switch to
`active`.

## Strict sessions

A legacy alias decides per conversation, pins for six hours, and may replace
the model when the conversation stops fitting it. That is right for a chat
window. A coding session wants the opposite, so a strict alias resolves one
execution profile once and keeps it.

```sh
curl -s localhost:8318/router/resolve \
  -H "X-Router-Admin-Token: $JEV_ROUTER_ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"schema_version":"1","intent":"new","session_id":"session-2f766788",
       "request_id":"resolve-0001","alias":"auto-session","client":"my-cli",
       "request":{"messages":[{"role":"user","content":"Add quoted CSV fields."}]}}'
```

The answer is one fixed profile: the model key, the provider, the wire model,
the protocol, the negotiated context window and output ceiling, the initial
effort, and a `binding_revision` to acknowledge. Size your history against that
window from then on. Every execution request carries four headers:
`X-Router-Admin-Token`, `X-Router-Session`, `X-Router-Binding` and
`X-Router-Request-Id`. All four are stripped before forwarding.

What that buys, and what it costs:

- The model and the provider never change. Not on an overflow, not on a lost
  capability, not on a 429, not on a timeout, not on a classifier outage.
- The binding does not expire on `pin_ttl_seconds`. It ends when you close it,
  when you prune it, or when a resolved but unused contract runs past
  `prepared_ttl_seconds`.
- A request that no longer fits is refused with a machine-readable code and the
  binding is kept. The router does not compact, widen the window or reach for a
  bigger model. Growth is the client's side of the contract.
- There is one entry in the execution plan, so a 429 or a 5xx is reported to
  you on the bound profile. Configured fallback lists do not apply.
- One request per session is in flight. A second concurrent one is a conflict,
  not a queue.

Errors are a structured body with a stable code:

```json
{"error": {"type": "session_error", "code": "CONTEXT_BUDGET_EXCEEDED", "message": "..."}}
```

There are thirteen: `INVALID_ROUTER_INPUT` (400), `ROUTER_UNAUTHORIZED` (401),
`SESSION_CONFLICT`, `SESSION_UNKNOWN`, `PROFILE_CHANGED`,
`REQUEST_ALREADY_COMPLETED`, `EXECUTION_OUTCOME_UNKNOWN`, `TURN_NOT_SETTLED`
and `EFFORT_HISTORY_MISMATCH` (409), `SESSION_CLOSED` (410), and
`NO_SAFE_ADMISSION`, `CONTEXT_BUDGET_EXCEEDED` and `UNSUPPORTED_PROFILE` (422).
None of them is ever turned into a cheaper request.

```sh
uv run jev-router sessions list
uv run jev-router sessions show session-2f766788
uv run jev-router sessions close session-2f766788
```

The whole contract is in
[`docs/SESSION_ROUTING.md`](docs/SESSION_ROUTING.md): the resolve schema,
retries and resume, the context budget, request ids, and what the router cannot
check on a client's behalf.

## The packet, the lanes and the windows

Three things decide a binding, and they are deliberately separate.

**The packet** is what Jev is asked. Some questions decide and the rest are
measured. `semantic_policy.shadow_questions` ride along in the same batched
call, are validated one at a time, are recorded as bare values, and cannot
reach the routing path. A malformed shadow answer is dropped and counted; it
can neither rescue a bad packet nor weaken a good one. The two sets may not
overlap, and an alias that routes on a question may not also be measuring it.
Every packet carries a version string hashing the question wording, the state
builder and the Jev model together, because all three have to move for an
answer to keep meaning what it meant.

**Quality lanes** are the model and effort pairs somebody measured for one kind
of work, each with a reference to where the evidence is. Capability and
adequacy are different filters: a model that supports tools is not thereby
qualified to finish a tool-driven coding task. So a rule may name a lane, and
then quota pressure may move the request **only between pairs in that lane**. A
threshold shift or an `equivalent` promotion that lands outside it is refused,
the adequate provider is kept, and the decision records the deferral. A
capability replacement is drawn from the lane, or the request is refused rather
than served by a model nobody measured. A lane entry with no
`qualification_ref` is a guess, and config load refuses it. A route's failure
fallbacks are not lane members: reaching one after a 429 is not a claim that it
was good enough.

**Quota windows.** A provider that publishes overlapping limits, a five-hour
one and a weekly one say, gets one record per window under `quota.config`'s
`windows:` list. Pressure is computed per window from that window's own
numbers, and the highest wins. One window's usage is never read against
another's reset time. A source that reports a single limit writes the same
fields one level up and is read exactly as it always was.

Every snapshot reports one word: `fresh`, `stale`, `unknown` or `error`. Only
`fresh` contributes a number; the other three contribute 0 and stay visible as
themselves in `explain`, in `quota`, on the resolve response and on the
decision row. **Unknown is never reported as unused capacity.**

```sh
uv run jev-router decisions replay 760392c45528
```

A decision is replayable from its own row and nothing else: the config hash,
the stored answers, the request facts and the pressure it was taken under. No
prompt is kept, and none is needed. Replaying at the recorded pressure is the
point — a deliberate quota saving replayed at pressure zero would look like a
routing bug.

[`docs/SEMANTIC_POLICY.md`](docs/SEMANTIC_POLICY.md) has the configuration, the
distribution-aware rules, the action-equivalence gate and the full replay
output.

## Between-turn effort

Off by default, and an experiment. Inside a strict session, at a reported
new-user-turn boundary, the router may change the reasoning effort of the model
the session is already bound to. Nothing else moves: not the model, not the
provider, not the account, not the negotiated limits, not the request-level
base effort.

Three modes. `off` is the default and an omitted `experiments:` block means
`off`.

| Mode | Jev call per turn | What the client sends | What moves |
| --- | --- | --- | --- |
| `off` | none | the fixed-effort request | nothing |
| `shadow` | one | the fixed-effort request, unchanged | nothing |
| `active` | one | the planned update item as well | the effective effort |

`shadow` needs three opt-ins to agree: `experiments.adaptive_effort.mode`, the
alias's `adaptive_effort: true` on a strict alias, and the client's
`turn_boundary_reporting: true` at resolve. Any one missing means `off`, the
mode is agreed once at resolve and stored on the session row, and a session
never gains it later.

`active` needs all of that plus a deployment somebody has qualified:
`effort_control.between_turn` naming a tested strategy, `qualification:
verified`, a `qualification_ref` that resolves to a file that exists, at least
two rungs on the ladder, and, for the native strategy, the base effort
carried as a request field rather than as a suffix on the model name.
`check-config` refuses it otherwise. No model in the shipped `router.yaml`
claims any of that.

The policy is `effort.plan_turn`, which is pure: a ladder and some evidence in,
one rung out. It never chooses a model.

- **Upward acts at once and jumps.** `thresholds` decide whether a turn wants
  more; `targets` decides how far, and the answer goes straight there — `low`
  to `high` in one move when the turn deserves it. `upward: step` restores the
  older single rung.
- **Downward moves one rung and needs confirming.** A drop needs low-risk
  evidence and `downgrade_confirmations` consecutive recommendations for the
  same rung, and a turn that recommends something else breaks the run. The
  asymmetry is deliberate: underserving a turn costs one turn's quality and is
  corrected at the next boundary, while a drop that is too deep is only noticed
  after the weak answer has been given.
- At most one change per user turn. A tool continuation is part of the same
  turn and never gets a decision of its own. A short "continue" is not evidence
  for anything. A classifier timeout keeps the current effort rather than
  guessing a cheaper one. Floors hold, and quota may only resolve an undecided
  turn downward, above every floor.

The ledger is one row per plan, holding hashes and identifiers and no
transcript: `planned` (decided, nothing sent), `accepted` (2xx headers, and
nothing more than that), `confirmed` (a valid provider completion carried it),
`rejected` (refused before acceptance, and retryable) and `outcome_unknown`.
Only a `confirmed` change moves the session's effective effort. An
`outcome_unknown` plan blocks the next change until somebody who can check the
provider reconciles it.

**Compaction.** A compaction the client asks for is passed through and opens a
compaction epoch. The router never compacts, summarises or rewrites a provider
item, and never changes model. In the new epoch, updates from earlier epochs
stop being position-validated and stop being required, because the positions
they were anchored at no longer name anything; the expected and confirmed
effort go back to the base effort; and the next eligible turn may plan again. `truncation:
auto` and the automatic-management fields are still refused.

**The adapter.** `jev-router adapter` is a small client shim that speaks the
session contract on behalf of a client that cannot, so Codex can drive the
experiment. It is a client, not policy: every item it inserts came out of a
turn plan, every refusal goes straight back, and it never retries on another
model.

```
codex exec  ->  adapter :18320  ->  jev-router :18318  ->  the proxy
```

[`docs/ADAPTIVE_EFFORT.md`](docs/ADAPTIVE_EFFORT.md) has the settings, the
protocol contract, what Codex actually puts on the wire, the reconciliation
rules and the rollback story. Read it before turning anything on.

## Feedback and what is stored

The sqlite file (`settings.sqlite_path`, WAL mode) holds six tables. Three are
the legacy path and three belong to strict sessions.

`pins` — `conversation_key`, `model`, `effort`, `created`, `decision_id`.

`decisions` — one row per alias request, and one per strict admission and
execution:

| Column | Notes |
| --- | --- |
| `id`, `ts` | row id and unix time |
| `decision_id` | short id, repeated on every turn of one pinned conversation |
| `alias`, `client` | the alias asked for, the `X-Router-Client` header |
| `conversation_key` | sha256, not reversible |
| `state_builder` | which builder produced the state |
| `answers` | the Jev answers as JSON, with confidence and probabilities |
| `features` | the computed counts and flags, as JSON |
| `config_hash` | sha256 prefix of the router.yaml that produced this row |
| `model`, `effort`, `rule` | what served the request, and what fired |
| `mode`, `fallback`, `pinned` | active or shadow, Jev failure, pin reuse |
| `jev_ms`, `jev_tokens` | Jev latency and input tokens |
| `est_tokens`, `message_count` | request size |
| `upstream_status`, `upstream_usage` | status, and usage for non-streamed replies |
| `state` | the state sent to Jev, only when `settings.log_state` is true |
| `route` | the named route the rule pointed at, if any |
| `intended_model`, `intended_effort` | the primary, when a fallback served instead |
| `fallback_index`, `fallback_reason` | which entry served, and why the earlier ones did not |
| `pressures` | the quota pressure per provider at the moment of the decision |
| `shifted` | every threshold quota pressure moved, with the numbers |
| `reordered` | whether quota pressure changed the route's order |

Later releases added more: `event_type` (`admission`, `execution` or
`effort_plan`), `session_id`, `turn_id`, `request_id` and `quality_lane` for
sessions; and `packet_version`, `shadow_packet_version`, `shadow_answers`,
`action`, `experiment_routes`, `quota_snapshot`, `quota_status`,
`counterfactual`, `exclusions`, `evidence` and `qualification_ref` for the
semantic packet and the lane guard.

Every added column is nullable, defaults to NULL, and an existing database
gains it the next time the router opens the file. Rows written before then read
back NULL, which is what "this decision predates the feature" means. Nothing is
dropped, rewritten or retyped, ever.

`feedback` — many rows per decision: `decision_id`, `ts`, `verdict` (one of
`right`, `too_weak`, `too_strong`, `too_slow`), `better_model`,
`better_effort`, `note`, `source` (`api` or `cli`).

`sessions` — one row per strict binding: the profile identity, the negotiated
limits, the binding revision, the base, effective, expected and confirmed
effort, the adaptation mode, the state (`prepared`, `active`, `blocked`,
`closed`), the quality lane, the versions a replay would need, and the last
reply's input and output token counts. `sha256` hashes for the owner and the
request fingerprint, and nothing recoverable.

`session_requests` — one row per execution attempt: `in_flight`, `accepted`,
`completed`, `rejected`, `stream_failed` or `unknown`. The history is bounded
per session, but a row whose outcome nobody can state is never dropped, so a
forgotten request id is never read as proof it did not execute.

`turn_plans` — the effort ledger, one row per turn plan: the ids, the sequence,
from and to effort, the history anchor or parent response id, expected and
confirmed effort, the status, the compaction epoch it was made in, and the
recommendation. No transcript, no message text, no reasoning text, no keys.

Record a verdict from the command line:

```sh
uv run jev-router feedback last too_weak --model gpt-6-astra --effort high \
  --note "missed the race condition"
```

Or over HTTP:

```sh
curl http://127.0.0.1:8318/router/feedback -H "X-Router-Client: my-cli" \
  -d '{"decision_id":"last","verdict":"too_weak","better_model":"gpt-6-astra","better_effort":"high"}'
```

`"last"` means the newest decision for that client header, or the newest of all
when the header is absent. `better_model` and `better_effort` are checked
against the configured models and efforts, so a typo is a 400 rather than a bad
row. Notes are cut to 2,000 characters and kept as written.

No message text, no prompts and no API keys are ever stored. The conversation
key hashes the `Authorization` header, and the features row keeps lengths, not
content. Turn on `settings.log_state` only while debugging question wording.

## Evaluating and tuning

Everything under `evals/` measures the router against 171 labelled requests in
`evals/cases.yaml`. Every case carries a `slice` saying what shape of request
it is:

| slice | n | slice | n |
| --- | --- | --- | --- |
| agentic | 46 | multilingual | 16 |
| chat | 45 | longcontext | 11 |
| adversarial | 27 | pipeline | 10 |
| multiturn | 16 | | |

The cases are split 70/30 by a fixed seed, stratified by task label: choices
are made on the 120 tuning cases and reported on the 51 held-out ones. A slice
can also be held out whole, which is a harder test than a row split, because a
row split leaves near-duplicates of a held-out case in the tuning half.

The scripts that cost quota need `TYPESAFE_API_KEY`; `run_outcomes.py` also
needs `UPSTREAM_API_KEY` and `JEV_ROUTER_UPSTREAM`, because it calls the
candidate models for real.

```sh
# Does Jev classify the request, and does the policy route it?
uv run python evals/run_eval.py --group final --repeats 3

# The same, plus a re-decide under four seeded rewordings of each request.
uv run python evals/run_eval.py --variants router_yaml --stability

# Two runs that are supposed to score badly. If they do not, the harness is broken.
uv run python evals/run_eval.py --variants router_yaml --control shuffled-labels
uv run python evals/run_eval.py --variants router_yaml --control constant-state

# Run the gradeable cases on every candidate route, three times each, and grade them.
uv run python evals/run_outcomes.py --samples 3 --max-calls 400

# Search the policy's numbers and print a diff. Optionally hold a whole slice out.
uv run python evals/tune.py --variant router_yaml --samples 5000
uv run python evals/tune.py --variant router_yaml --slice-holdout agentic

# Sample public datasets into a git-ignored out-of-distribution set.
uv sync --group evals
uv run python evals/import_public.py --limit 25
uv run python evals/run_eval.py --variants router_yaml --public evals/cases_public.yaml
```

Two more controls, and the four packet arms:

```sh
uv run python evals/run_eval.py --variants router_yaml --control constant-policy
uv run python evals/run_eval.py --variants router_yaml --control length-only
uv run python evals/run_eval.py --packet --repeats 3
```

Four scripts do no live work of their own, or none by default:

```sh
uv run python evals/quota_sim.py --demo         # simulated; never an observed delta
uv run python evals/effort_replay.py            # offline, synthetic turn fixtures
uv run python evals/qualify_effort.py --profile <key> --plan   # prints the plan and stops
uv run python evals/run_sessions.py --list
uv run python evals/run_sessions.py --dry-run
uv run python evals/run_sessions.py --arms fixed_strong,jev_packet --repeats 2
```

`quota_sim.py` replays a clock, a set of quota windows, their resets and
whatever telemetry failures you give it through the router's own pure
functions. Everything it prints is labelled `simulated`, and it never claims a
quota saving from `ROUTE_COST`: those weights order routes, they do not measure
a subscription.

`run_sessions.py` runs the eight pilot coding tasks in `evals/session_tasks/`,
each in a disposable directory with a scrubbed environment and hard call and
wall-clock caps. `--list` and `--dry-run` need no credentials; anything else
needs a router and an upstream. Eight tasks cannot separate two close policies
and the report says so.

`effort_replay.py` compares the between-turn effort policies over synthetic
turn sequences and makes no calls. `qualify_effort.py` plans by default;
running it live needs `--confirm-live`, `JEV_ROUTER_QUALIFY_LIVE=1`, an
allowlisted profile, credentials and a call budget. The allowlist is empty.

Every run writes `manifest.json` and `manifest.md` beside it: the repo SHA, the
question and policy hashes, the packet version, the candidate profiles, the
case ids and splits, the seeds, the cache condition, the caps and everything
that did not finish. A run that answered entirely out of the cache is labelled
`policy_replay`, which says what the policy does with answers somebody else
paid for. It is not a live classifier measurement and not a latency one.

`run_eval.py` scores Jev's answers against the labels and then feeds those
answers through `policy.py` to score the route. It writes
`evals/results/<timestamp>/summary.md` and `per_case.jsonl`, and points
`evals/results/latest` at the newest run. Answers are cached on disk by a hash
of the request, so a rerun is free; the repeats that measure stability always
go to the network. A variant is one state builder plus one set of question
wordings, defined in `evals/variants.yaml` as an overlay on `router.yaml`.
`router_yaml` is this file as it stands and `--group` runs a named set.

`evals/metrics.py` holds the definitions the scripts share: Wilson intervals,
expected calibration error, the cost-matched random baseline, Gap@Oracle and
Gain@BestSingle, and the seeded perturbations behind the stability number.

`run_outcomes.py` runs each gradeable case on all seven candidate routes, k
times per route, and grades the replies with a programmatic check from
`evals/checks.py`, a blind judge against a rubric, or both. Where a check
exists it decides the score and the judge is scored against it. It needs
`UPSTREAM_API_KEY` and `JEV_ROUTER_UPSTREAM` as well, because it calls the
models for real. Cases run one at a time in priority order under `--max-calls`,
so a run that hits its budget leaves whole cases finished and resumes from the
cache. It also stops itself if a route's own median latency triples or replies
start coming back empty. It writes `evals/outcomes.json` and
`evals/outcomes.md`; `--apply-labels` writes the clear-cut label changes back
into `cases.yaml`, and never writes one from a case whose winner is `unstable`.

`import_public.py` samples WildChat (ODC-BY), BFCL, LongBench v2 and MGSM into
`evals/cases_public.yaml`. That file is git-ignored on purpose: real user
conversations can hold personal data and must not be republished from here. The
script drops anything the dataset flags as toxic and anything matching an email
address, a phone number or a card-shaped number, and it never writes a label of
its own. Labels live beside it in `cases_public_labels.yaml`.

`tune.py` reads the cached answers, `evals/outcomes.json`, and the `decisions`
and `feedback` tables in the sqlite log. Feedback rows count three times. It
searches for the cheapest set of thresholds that keeps under-routing at or
below 5% on the tuning split and prints a unified diff. It never edits
`router.yaml`: read the diff, apply it yourself, and the `config_hash` in the
log marks the boundary between policy versions.

`evals/EXPERIMENTS.md` records every change that was tried, with the numbers
before and after, including the ones that did not help.

### Where it stands

Over all 171 cases, with Wilson 95% intervals. On this many cases one case is
0.6 points and two independent runs cannot separate a difference smaller than
about 11 points, so read the intervals before reading the ranking.

| | task acc | difficulty | tier correct | under-routed | over-routed | cost |
| --- | --- | --- | --- | --- | --- | --- |
| this config | 73.7 [66.6, 79.7] | 74.9 [67.9, 80.8] | **92.4 [87.4, 95.5]** | 1.2 [0.3, 4.2] | 6.4 [3.6, 11.2] | 10.8 |
| before the confidence-gate change | 73.7 | 74.9 | 88.3 [82.6, 92.3] | 1.2 | 10.5 | 10.7 |
| cost-matched random (Zero Router) | - | - | 57.3 ± 3.4 | 24.3 | 18.4 | 10.9 |
| always gpt-6-astra(medium) | - | - | 63.2 [55.7, 70.0] | 0.0 | 36.8 | 14.0 |
| always ollama/glm-5.3-flash(none) | - | - | 46.2 [38.9, 53.7] | 53.8 | 0.0 | 1.0 |
| the no-network `rules` decider | 30.4 [24.0, 37.7] | 31.6 [25.1, 38.9] | 60.2 [52.8, 67.3] | 23.4 | 16.4 | 7.4 |
| majority class (ignore the request) | - | - | 63.2 | - | - | - |

The row that matters is the cost-matched random one, which is RouterBench's
Zero Router. It sends a request to the frontier tier with the router's own
frontier rate and draws the effort from the router's own mix, so it spends
10.9 quota units against the router's 10.8 and differs only in choosing at
random. The router is 35 points better at the same spend, so it is choosing
rather than spending.

Cost is relative, not currency. `ROUTE_COST` in `evals/common.py` puts
`gpt-6-astra(medium)` at 14 units and `ollama/glm-5.3-flash(none)` at 1.
Nothing measured those ratios.

By slice:

| slice | n | task acc | difficulty | tier correct | under | cost |
| --- | --- | --- | --- | --- | --- | --- |
| chat | 45 | 77.8 [63.7, 87.5] | 91.1 [79.3, 96.5] | 93.3 [82.1, 97.7] | 2.2 | 8.7 |
| agentic | 46 | 54.3 [40.2, 67.8] | 76.1 [62.1, 86.1] | 89.1 [77.0, 95.3] | 0.0 | 13.5 |
| adversarial | 27 | 74.1 [55.3, 86.8] | 51.9 [34.0, 69.3] | 96.3 [81.7, 99.3] | 0.0 | 11.3 |
| multiturn | 16 | 87.5 [64.0, 96.5] | 68.8 [44.4, 85.8] | 87.5 [64.0, 96.5] | 0.0 | 11.9 |
| multilingual | 16 | 87.5 [64.0, 96.5] | 81.2 [57.0, 93.4] | 87.5 [64.0, 96.5] | 6.2 | 9.1 |
| longcontext | 11 | 90.9 [62.3, 98.4] | 63.6 [35.4, 84.8] | 100.0 [74.1, 100.0] | 0.0 | 10.0 |
| pipeline | 10 | 80.0 [49.0, 94.3] | 70.0 [39.7, 89.2] | 100.0 [72.2, 100.0] | 0.0 | 8.6 |

Slice intervals are wide enough that this table is for finding where the router
fails, not for deciding that one config beats another.

The 27 adversarial cases cover three attack goals, delivered six ways. All 27
hold their label: the route stays inside `acceptable_tiers` and the effort
stays within one level.

| attack goal | n | tier held | effort held |
| --- | --- | --- | --- |
| cost escalation | 8 | 100.0 [67.6, 100.0] | 100.0 [67.6, 100.0] |
| quality hijacking | 9 | 100.0 [70.1, 100.0] | 100.0 [70.1, 100.0] |
| harmful downgrade | 8 | 100.0 [67.6, 100.0] | 100.0 [67.6, 100.0] |

With 8 cases per goal the lower bound of the interval is 68% even at 100%
observed, so this says "no attack in this set works", not "no attack works".

Three targets in TESTPLAN.md are missed, all on classification:

- Task label accuracy is 73.7% against 85%. Most of the shortfall is the
  agentic slice, where Jev answers `code-edit` on turns the labels call
  `agentic-tool-task`. The policy treats those categories identically, so the
  tier is right 89% of the time on that slice.
- Difficulty within tolerance is 74.9% against 80%, concentrated on the
  adversarial slice, which exists to push that answer around.
- The faithfulness flag is 70.8% against 85%. It uses `harm_if_wrong` as a
  proxy; a dedicated `needs_faithfulness` question predicts the label better
  but made routing worse, so no alias asks it.

Two more measured properties:

- **Stability.** Reword a request four ways with a fixed seed and the route is
  unchanged 95.0% [93.1, 96.4] of the time, with 85.4% of requests never moving
  at all. The ones that move sit on a difficulty cutoff.
- **Calibration.** ECE is 0.129 on `task`, where Jev is overconfident, and
  0.084 on `difficulty`, where it is slightly underconfident. That is why
  `low_confidence.min_confidence` is 0.40 rather than 0.55.

The fast model is always used at `effort: none`. The outcome benchmark found
that raising its effort made answers slightly worse on average and cost a third
more latency. A rerun with three samples per route and the token cap raised
from 3,000 to 8,000 found the same signs, no truncation at all on any route,
and a blind judge that scores about 1.5 points below the programmatic check it
was measured against. `evals/outcomes.md` has the tables.

Caveats. The cases are written rather than sampled, and labelled by one person,
so task accuracy is measured against one opinion. A sample of 100 public
requests (WildChat, BFCL, LongBench v2, MGSM) routes at 86.0% [77.9, 91.5]
tier-correct with 5.0% under-routing, against 92.4% and 1.2% on the written
set, so the written set is somewhat easier than real traffic. The outcome
benchmark is partial.

Rerun the classification scores after any change to a question or a state
builder, and the tuner after that. The cutoffs only mean anything against one
wording. `settings.jev_model` is pinned to an exact version so a model update
on TypeSafe's side cannot move routing without a rerun.

The newer machinery has been measured and none of it was promoted. Over all 171
cases and all four policies, this branch and the release before it produced
identical states, identical Jev answers and identical routes: everything new is
opt-in or shadow. The four packet arms of spec 13.4 came out a tie —
`router_yaml` 92.4 [87.4, 95.5], `b1_coding_state` 90.6 [85.3, 94.2],
`b2_expanded_packet` 90.6 [85.3, 94.2], `b3_distribution` 91.8 [86.7, 95.1] —
with under-routing at 1.2% for the shipped packet against 4.1% for the
alternative, so the shipped one stayed. Asking three more questions over the
same state costs about 900 input tokens and 2 ms of median latency. Experiments
21 to 25 in `evals/EXPERIMENTS.md` have the rest, including the four negative
controls, the router's own added latency and the limits on every number.

## Limitations

- Only `/v1/chat/completions` understands an alias. Every other path is
  forwarded unchanged, and an alias name sent to another POST endpoint returns
  400.
- Upstream token usage is recorded only for non-streamed replies. A streamed
  response is passed through without being parsed, so its usage block is never
  captured.
- The questions and their criteria are written in English. Non-English requests
  are classified less reliably.
- A valid pin stays sticky, even during 429s or quota pressure. Hard permission,
  capability, context or effort incompatibility permits a replacement with a new
  decision id. New instructions alone do not trigger reassessment.
- Jev is a closed, paid API from TypeSafe. Without a key the router falls back
  to the `rules` decider, which reads counts rather than meaning and
  under-routes about a quarter of the case set.
- Reword a request and about one time in twenty the route changes. The pin
  contains this within a conversation but not across conversations.
- The evaluation is a benchmark of one deployment, not of models. Two specific
  models, one specific proxy, and a cost model in `evals/common.py` that nobody
  measured. Change `ROUTE_COST` first before reusing any of this.
- One service process per database. The router refuses a second strict session
  service on the same file inside one process, but that guard is in memory and
  cannot see another process. Two services sharing `router.db` would each hold
  their own in-flight claims.
- Between-turn effort is an experiment and nobody has shown it is worth doing.
  One deployment has a provisional protocol test; there is no quality
  comparison against a fixed effort, at any rung.
- Open bugs are listed under "Known issues" in
  [`docs/README.md`](docs/README.md#known-issues).

## Transport, privacy and deployment

Forwarded replies use raw streaming with their `Content-Encoding` preserved.
The client's `Accept-Encoding` is forwarded; absent means an explicit `identity`
request upstream. Only a bounded private copy is decoded for JSON usage capture
(gzip/deflate supported; unsupported encodings skip capture). SSE bytes are never
parsed or rewritten. Alias decisions record `accepted`, `stream_state`,
`first_byte_ms`, `response_ms`, and wire `response_bytes`. Timing starts at the
execution plan, not ingress or Jev classification. First byte is **not TTFT**;
`completed` means normal transport EOF, not a graded successful answer.

`log_state: false` controls local storage, not data sent to TypeSafe. State builders
send request excerpts, quoted material and tool names to Jev, a separate destination
from the selected model. For no external classification, set `settings.decider: rules`
and remove/replace any alias-level `decider: jev` overrides. This still forwards the
request to the selected upstream model; it is not an entirely local-processing mode.

`/router/*` endpoints require `X-Router-Admin-Token` when the environment variable
named by `admin_token_env` is set. Without a token, **only direct loopback callers**
are accepted; forwarded client-IP headers are not trusted. The admin header is
never sent upstream. `Authorization` remains the model API's credential and is
forwarded unchanged. Do not proxy remote traffic into loopback without setting an
admin token. Non-loopback serving also needs trusted ingress/Tailscale ACLs: upstream
authentication happens after classification, so it does not protect Jev quota.

Strict sessions are stricter. Every control and execution request needs that
credential, loopback included, and `session_routing.require_control_token:
false` is a config error rather than a way off: a session is scoped to the
owner of the credential, so there has to be one to scope it to. Everyone
holding it is the same owner. This is a single-owner local tool and it claims
no tenant isolation between people who share a token. Nothing recoverable is
stored: the owner and every request fingerprint are sha256 hashes.

[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) is how this is actually run: a
launchd job, a `run.sh` that exports the four environment variables, upgrading
with `check-config` before the restart, what the additive migration does on
first start, and rolling back.

Body/concurrency limits return 413/503. For chunked passthrough uploads, the size
limit is enforced as bytes arrive; an upstream may receive a prefix before rejection.
Keep `log_state` disabled. Feedback notes are stored verbatim. Retention is explicit:

```sh
uv run jev-router prune --older-than-days 30
```

This irreversibly deletes old decisions, feedback and pins, including tuning evidence.
Back up the database first. No automatic deletion runs at startup.

## Tests

```sh
uv run pytest -q
```

GitHub Actions installs from `uv.lock`, runs the offline tests on Python 3.12–3.14,
and validates the configuration. No provider evals run in CI. HTTP/session regressions
cover compressed representations, malformed Jev answers, pin success boundaries,
alias/capability changes, hard conflicts, stream failures, and ingress limits.

The unit tests mock Jev and the upstream with respx, so nothing leaves the
machine. No test runs a quota source against a real program: the `command`
source is exercised against a Python one-liner, and the rest use `static`.

Two tests guard upgrades. One runs the shipped `router.yaml` and a copy of the
configuration as it was before providers existed over a grid of synthetic
answers and asserts an identical model and effort for every one. The other
builds a database with the original schema and checks that it gains the new
columns, keeps its rows, and takes new ones.

The strict-session and effort contracts are numbered C01-C34 and F01-F20 in
[`docs/R2_SPEC.md`](docs/R2_SPEC.md), and every one has a test. `TESTPLAN.md`
maps each id to its file, and `uv run pytest -k c18` finds one by id. All of it
is offline: a fake client, mocked Jev, mocked upstream, synthetic fixtures. The
one deployment that has been exercised live is written up in
[`docs/qualification/`](docs/qualification/), and it calls itself provisional.

## License

MIT. See [LICENSE](LICENSE).
