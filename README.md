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
2. Compute the conversation key, a sha256 of the hashed `Authorization` header,
   the system prompt and the first user message. If that key has a pin younger
   than the TTL, reuse it and skip Jev. If the conversation has outgrown the
   pinned model's context window, move up once to the smallest model that fits
   and re-pin.
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
7. Rewrite `model`, forward, and stream the upstream bytes back untouched.

If the Jev call fails for any reason, the router falls back to a no-network
decider that guesses from counts, or to the default route. A fallback decision
is never pinned, so the next turn tries Jev again. Jev being down never blocks
a request.

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
| `X-Router-Fallback` | `true`, present only when Jev could not be used |
| `X-Router-Shadow-Model` | in shadow mode, what would have been chosen |

### Other commands

```sh
uv run jev-router explain request.json      # features, state, Jev answers, chosen model
uv run jev-router decisions -n 20           # tail the decision log
uv run jev-router decisions --json
uv run jev-router feedback last too_weak --model gpt-6-astra --effort high
```

Every command takes `-c/--config` for the path to `router.yaml`, which also
reads from `$JEV_ROUTER_CONFIG` and defaults to `router.yaml` in the working
directory. `serve` takes `--host`, `--port`, `--mode shadow|active` and
`--log-level`.

`explain` never forwards the request. Its argument is a JSON file holding a
chat-completions body; an optional `_headers` object inside it stands in for
request headers, and `--alias` treats the body as a different alias.

### Endpoints

| Path | Behaviour |
| --- | --- |
| `POST /v1/chat/completions` | aliases are routed, everything else is forwarded unchanged |
| `GET /v1/models` | the upstream list plus one entry per alias |
| `GET /router/health` | mode, upstream, aliases, models, decider, whether a Jev key is set, uptime |
| `GET /router/decisions?limit=N` | recent decisions as JSON |
| `POST /router/feedback` | record what you thought of a decision |
| `GET /router/feedback?limit=N` | recent feedback, joined to its decision |
| anything else | streamed through to the upstream, method and path unchanged |

An alias name on any other POST endpoint returns 400 with a message saying so.

## Configuring router.yaml

Adding a model, a question, a rule, an alias or a client floor is an edit to
`router.yaml` alone. Only a new state builder or a new decider needs Python.

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
| `capture_usage_max_bytes` | how much of a non-streamed reply to buffer for its usage block |

### A model

```yaml
models:
  vendor/fast-model:
    upstream_id: vendor/fast-model
    context_window: 256000
    supports_tools: true
    supports_vision: false
    efforts: [none, low, high]
    effort_style: suffix      # suffix -> "model(high)", param -> reasoning_effort, none
    tags: [fast]
    description: Quick answers.
```

`upstream_id` is the name sent to the proxy. `effort_style` decides how the
effort is expressed: `suffix` appends `(effort)` to the model name, `param`
sets `reasoning_effort` in the body, and `none` drops the effort. An effort a
model does not list is clamped down to the closest one it does.

Then name the model in a rule and add it to the alias's `allowed_models`.

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
  when:
    task: {in: [high-stakes-writing], conf_gte: 0.6}
    harm_if_wrong: {gte: 0.5}
    est_tokens_gte: 2000
  use: {model: gpt-6-astra, effort: high}
```

Conditions on a choice answer take `in`, `not_in`, `eq` and `conf_gte`; on a
score answer `gte`, `lte` and `conf_gte`; on a noul answer `gte` and `lte`.
Score answers are 0-indexed and fractional, so `{gte: 1.8}` sits between the
levels.

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

## Shadow mode

`settings.mode: shadow` makes the router ask Jev, run the rules and log what it
would have chosen, but send every alias request to `settings.default_route`.
Nothing is pinned. The response carries `X-Router-Shadow-Model`, and the log
row holds the decision with `mode = shadow`. In shadow mode the logged `model`
and `effort` are what the router would have sent, not what was sent.

Run it for a week, read the log, fix the question wording, then switch to
`active`.

## Feedback and what is stored

The sqlite file (`settings.sqlite_path`, WAL mode) holds three tables.

`pins` — `conversation_key`, `model`, `effort`, `created`, `decision_id`.

`decisions` — one row per alias request:

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
| `model`, `effort`, `rule` | what was chosen and what fired |
| `mode`, `fallback`, `pinned` | active or shadow, Jev failure, pin reuse |
| `jev_ms`, `jev_tokens` | Jev latency and input tokens |
| `est_tokens`, `message_count` | request size |
| `upstream_status`, `upstream_usage` | status, and usage for non-streamed replies |
| `state` | the state sent to Jev, only when `settings.log_state` is true |

`feedback` — many rows per decision: `decision_id`, `ts`, `verdict` (one of
`right`, `too_weak`, `too_strong`, `too_slow`), `better_model`,
`better_effort`, `note`, `source` (`api` or `cli`).

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

Four scripts. The first three need `TYPESAFE_API_KEY`:

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

## Limitations

- Only `/v1/chat/completions` understands an alias. Every other path is
  forwarded unchanged, and an alias name sent to another POST endpoint returns
  400.
- Upstream token usage is recorded only for non-streamed replies. A streamed
  response is passed through without being parsed, so its usage block is never
  captured.
- The questions and their criteria are written in English. Non-English requests
  are classified less reliably.
- A pinned conversation never changes model. The only exception is a
  conversation that outgrows the pinned model's context window, which moves up
  once.
- Jev is a closed, paid API from TypeSafe. Without a key the router falls back
  to the `rules` decider, which reads counts rather than meaning and
  under-routes about a quarter of the case set.
- Reword a request and about one time in twenty the route changes. The pin
  contains this within a conversation but not across conversations.
- The evaluation is a benchmark of one deployment, not of models. Two specific
  models, one specific proxy, and a cost model in `evals/common.py` that nobody
  measured. Change `ROUTE_COST` first before reusing any of this.

## Tests

```sh
uv run pytest -q
```

The unit tests mock Jev and the upstream with respx, so nothing leaves the
machine.

## License

MIT. See [LICENSE](LICENSE).
