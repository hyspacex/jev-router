# Configuration reference

[Documentation index](../README.md) · [Project home](../../README.md)

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
`router.yaml` enables `session_routing` and exposes one strict alias, `auto`.
It configures quality lanes, keeps `distribution_policy: off`, asks no extra
shadow questions, and keeps the effort experiment off.

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

A route has one primary and an ordered list of fallbacks. A rule points at it
by name. Legacy unpinned execution can walk the list when the upstream refuses
to answer; strict execution always has one bound entry and never fails over.
A route is not the same thing as a qualified quality lane.

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
out of those shifts. See the [quota guide](../guides/quota.md).

Request conditions read the request instead of a Jev answer: `has_tools`,
`has_images`, `has_code`, `stream`, `est_tokens_gte`, `est_tokens_lte`,
`message_count_gte`, `message_count_lte`, `max_tokens_gte`, `client`,
`client_in`, `languages_include` and `tool_names_include`. Adding a new one is
a single entry in `FEATURE_CONDITIONS` in `policy.py`.

`policy.low_confidence` is checked before the rules: when any question it lists
comes back under `min_confidence`, its route is used instead.

`skip_when_top_is_noise` names the score questions that are an exception to
that. For a question it lists, if the probability vector puts less than
`score_top_min_mass` (default 0.1) on the highest rubric level, that question
does not escalate and the mean score is left to the rules. A score with no
vector still escalates. The default is an empty list, which is the gate
without this. A name in the list must be a score question and must also be in
`questions`, or config load refuses it.

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
