# HTTP and CLI reference

[Documentation index](../README.md) · [Project home](../../README.md)

## Commands

```sh
uv run jev-router check-config              # validate router.yaml and print a summary
uv run jev-router serve --mode shadow
uv run jev-router explain request.json      # features, state, Jev answers, chosen model
uv run jev-router explain request.json --pressure openai=0.8
uv run jev-router quota --poll              # quota snapshots and pressure per provider
uv run jev-router quota --poll --json
uv run jev-router decisions -n 20           # tail the decision log
uv run jev-router decisions replay last     # re-run one decision from its own row
uv run jev-router decisions replay '#730'   # one exact row; also --row 730
uv run jev-router feedback last too_weak --model gpt-6-astra --effort high
uv run jev-router sessions list             # strict session bindings
uv run jev-router sessions show <id>
uv run jev-router sessions close <id>
uv run jev-router sessions reconcile <id> --plan <plan-id> --outcome applied
uv run jev-router prune --older-than-days 30   # irreversible
uv run jev-router adapter --alias <strict-alias> --port 18320
uv run jev-router config show --window 7d      # the rules, their lines and hits
uv run jev-router config preview draft.yaml    # replay recent admissions under a draft
uv run jev-router config-snapshots list        # stored configs, scrubbed, and whether each loads
uv run jev-router config-snapshots import old.yaml   # only a file whose hash a decision carries
uv run jev-router bookmarks add <session-id> --label "Debugging" [--slug debugging]
uv run jev-router bookmarks list
uv run jev-router bookmarks remove <slug>
```

`config preview` validates a draft exactly as startup validates a file, then
replays the last `--limit` admissions (default 200, at most 500) in
`--window` under the draft and under the running config, from their stored
answers, facts and pressure. No Jev call and no write. It exits 1 when the
draft does not load and prints each problem with its line. There is no
`config apply` on the command line: applying hot-swaps a running server, so it
is `POST /router/config/apply` against that server.

Put the global `-c/--config` option **before the subcommand**, for example
`uv run jev-router -c local.yaml serve`. It selects the path to `router.yaml`, which also
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
sessions; see [`docs/SESSION_ROUTING.md`](../../docs/SESSION_ROUTING.md) and
[`docs/ADAPTIVE_EFFORT.md`](../../docs/ADAPTIVE_EFFORT.md).

## Endpoints

| Path | Method | Behaviour | Writes |
| --- | --- | --- | --- |
| `/v1/chat/completions` | POST | aliases are routed; a strict session executes here; everything else is forwarded unchanged | a decision row per alias or session request, a pin after a legacy 2xx, the session's request ledger |
| `/v1/responses` | POST | a strict session whose profile speaks the Responses protocol executes here; everything else is forwarded unchanged | as above, for a session |
| `/v1/responses/compact` | POST | a standalone compaction, forwarded as maintenance for the bound session | as above, and the session's compaction epoch |
| `/v1/models` | GET | the upstream list plus one entry per alias | nothing |
| `/router/health` | GET | mode, upstream, aliases, models, providers, routes, unhealthy providers, decider, whether a Jev key is set, uptime | nothing |
| `/router/providers` | GET | circuit breaker state and the pressure per provider | nothing |
| `/router/quota` | GET | quota snapshots, pressures, staleness and the last error per provider, and `subscriptions`; see below | nothing |
| `/dashboard` | GET | the audit UI, a data-free shell; its data needs router authentication | nothing |
| `/router/audit?limit=N` | GET | prompt-free audit snapshot, 1–500 recent events (default 300) | nothing |
| `/router/audit/overview?window=1h\|24h\|7d\|30d` | GET | router health from the tables, and live quota; see below | nothing |
| `/router/audit/sessions` | GET | strict sessions, filtered, with facets and bookmarks; see below | nothing |
| `/router/audit/sessions/{id}` | GET | one session: binding, user turns, admission, requests, plans, trace; see below | nothing |
| `/router/decisions?limit=N` | GET | recent decisions as JSON | nothing |
| `/router/feedback` | POST | record what you thought of a decision | one feedback row |
| `/router/feedback?limit=N` | GET | recent feedback, joined to its decision | nothing |
| `/router/resolve` | POST | resolve or resume a strict session binding | the session and its admission row |
| `/router/sessions?limit=N` | GET | strict session bindings, newest first | nothing |
| `/router/sessions/{id}` | GET | one binding: identity, limits, efforts and state | nothing |
| `/router/sessions/{id}/close` | POST | end a session deliberately | the session row |
| `/router/sessions/{id}/feedback` | POST | a verdict on a session, and optionally a same-model effort step; see below | one feedback row, the session's verdict, and its effort when it steps |
| `/router/turn-plan` | POST | an effort-only decision for a new user turn (experimental) | a plan and its decision row |
| `/router/turn-plan/{id}/reconcile` | POST | say what happened to an ambiguous effort update | the plan |
| `/router/config?window=` | GET | the running config: its text, rules with their lines and hits, routes, lanes, models, thresholds, rated admissions | nothing |
| `/router/config/preview` | POST | validate a draft and replay recent admissions under it; no Jev call | nothing |
| `/router/config/apply` | POST | validate a draft, back up router.yaml, write the draft, swap it in | a backup, router.yaml, a config snapshot |
| `/router/sandbox/jev` | POST | ask Jev once about a stored decision with an edited state or wording | nothing (one Jev call) |
| `/router/eval-cases` | POST | append one decision to `golden_cases.jsonl` beside the database | one line in that file |
| anything else | any | streamed through to the upstream, method and path unchanged | nothing |

An alias name on any other POST endpoint returns 400 with a message saying so.

Every `/router/*` path needs `X-Router-Admin-Token` once the environment
variable named by `settings.admin_token_env` is set; without one, only direct
loopback callers are accepted. The strict-session paths need that credential
always, loopback included, and there is no setting that turns it off:
`resolve`, `turn-plan`, `sessions` (session feedback included), and any
managed execution on `/v1/*`. The `/router/audit` and config workbench paths
need only the ingress credential.

### The overview

`GET /router/audit/overview?window=` (default `24h`; any other parameter is a
400) writes nothing and calls nothing. Every rate is
`{numerator, denominator, rate}`, every latency is `{n, p50, p95}`, and a value
no row recorded is `null`, never zero. Beside the keys the page has always
read (`sessions`, `executions`, `latency`, `series`, `decisions`, `quota`,
`effort`, `legacy`), it returns:

| Key | What it holds |
| --- | --- |
| `router_tax` | `admission_ms` (Jev at resolve, fallbacks left out), `plan_ms` and `plan_jev_ms` (the turn plan), `jev_ms_total` and `jev_calls_timed` (every Jev call, failed ones included), `execution_requests`, and `amortized_ms_per_request` = all Jev time over execution requests |
| `upstream.pairs[]` | per model and effort: `n`, `first_byte_ms`, `tps` (`{n, p50}`: output tokens over `max(response_ms - first_byte_ms, 1)` ms, rows with usage and both timings only) and `response_ms` (`{n, p50}`) |
| `quality.corrective_followup` | plans whose `corrective_followup` is at or above `threshold` (the running config's) over plans that recorded it, overall and `by_rule[]` of the admission |
| `quality.early_escalation` | sessions whose first plan moved effort up over sessions with a plan, overall and `by_rule[]` |
| `quality.knife_edge` | admissions within `margin` (0.10) of a numeric cutoff in the rule they matched or its two neighbours, under the running config only; `unassessed` and `unassessed_by_reason` (`config_changed`, `alias_not_in_config`, `no_ladder_rule`) are counted apart |
| `quality.feedback` | `admissions` and `counts` of the newest verdict per admission: `right`, `too_weak`, `too_strong`, `too_slow`, `unrated` |
| `quality.shadow` | per shadow choice question: `n`, `counts` by label, `by_pair[]` |
| `efficiency.quota_saved` | frontier quota saved. `baseline_model` is the priced model with the highest `quota_cost` (the first named on a tie) and `provider` its provider. An execution on that provider costs its model's `quota_cost` over the baseline's; one on another provider costs 0; one on that provider whose model has no `quota_cost` written down, or that the config does not know, is `unknown`. `saved_rate` = 1 - `quota_cost_sum` / `counted`, `null` when nothing is counted or nothing is priced |
| `efficiency.cache` | cached over input tokens, from rows that reported both: `rows`, `cached_tokens`, `input_tokens`, `rate`, and `by_model[]` |
| `efficiency.usage_reported` | `executions`, `reported`, and how many carried `input`, `output`, `cached` counts |
| `matrix` | admissions by the first choice question's label (`question`, `tasks`) and the chosen pair: `pairs[]`, `cells[]` each with `n` and a `corrective` rate from its sessions' plans |
| `reliability` | `fallback` and `low_confidence` admission rates, and `breakers` per provider: `state`, `healthy` and the breaker's counters, never its error text |

Each `series` bucket also has `first_byte_p50_ms` beside `response_p50_ms`.
The body also carries the running `config_hash`, and `subscriptions`: the one
live source, described next.

### Live quota: `subscriptions`

`GET /router/audit/overview` and `GET /router/quota` both return
`subscriptions`, the quota monitor's current readings read from memory. Nothing
is polled and no program is run to answer. It is `null` when the monitor cannot
be read.

| Key | What it holds |
| --- | --- |
| `as_of`, `quota_routing`, `demote_above`, `exhausted_at` | when it was read, whether `quota_policy` is on, and its two cutoffs |
| `routed[]` | every provider in the config: `provider`, `enabled`, `source`, `codexbar_provider`, `via` (`snapshot`, `cli` or `null`), `status` (`fresh`, `stale`, `unknown`, `error`), `pressure`, `age_seconds`, `fetched_at`, `used_percent`, `windows[]`, `exhausted` (the spent window or `null`), `steering`, `models[]` (`model`, `quota_cost`, `pressure`), `polls`, `errors` |
| `tracked[]` | every CodexBar subscription no enabled provider links to: `id`, `via`, `status`, `age_seconds`, `fetched_at`, `windows[]`, `plans[]` (`title`, `used_percent`), `tokens` (CodexBar's session and 30-day token and cost numbers), `currency`, `credits_remaining`, `polls`, `errors` |

Each window is `{name, used_percent, expected_percent, pace_delta, resets_at,
window_minutes, reset_description}`; `expected_percent` comes from the window's
own length and reset time, and `pace_delta` is `null` when either side is
unknown. `steering` is `calm` (pressure 0), `elevated` (above 0, not above
`demote_above`), `demoting` (above `demote_above`) or `exhausted` (a fresh
window at `exhausted_at`), and `null` whenever the reading is not fresh, the
provider is not polled, or quota routing is off. A tracked subscription never
produces pressure and has no `steering`. Error text is never projected here;
`GET /router/quota` reports each provider's `last_error` in its own
`providers` block.

### The session list

`GET /router/audit/sessions` writes nothing and calls nothing. It takes
`limit` (1–200, default 50) and these filters, all optional; any other
parameter is a 400.

| Parameter | Matches |
| --- | --- |
| `client`, `model`, `rule` | that exact label |
| `state` | `prepared`, `active`, `blocked` or `closed` |
| `adaptive` | the session's adaptation mode: `off`, `shadow` or `active` |
| `problem` | `any`, or one of `unknown`, `rejected`, `stream_failed`, `fallback`, `low_confidence` |
| `feedback` | the session's newest verdict: `right`, `too_weak`, `too_strong`, `too_slow`, or `unrated` for none |
| `pinned` | `1` for bookmarked sessions only |
| `q` | 1–80 letters, digits, spaces or ordinary punctuation, found anywhere in the session id, or in the title or prompt preview ignoring case |
| `window` | last seen within `1h`, `24h`, `7d` or `30d` |

It returns `config_hash`, `limit`, `matched` (the count beyond the limit),
`bookmarks[]`, `facets` (`client`, `model`, `rule` and `feedback`, each a list
of `{value, n}`; `feedback` counts unrated sessions as `unrated`) and
`sessions[]`. Each session has `session_id`, `alias`, `client`, `state`,
`created`, `last_seen`, `closed_at`, `model`, `effort` (the base effort),
`effective_effort`, `rule`, `quality_lane`, `config_hash`, `config_matches`,
`adaptation_mode`, `tombstone`, `title`, `prompt_preview`, `workspace_hint`,
`turn_count`, `feedback_verdict`, `requests` (a count per request status),
`problems[]`, `response_ms` (`{n, p50}`) and `pinned` (`{slug, label}` or
`null`). The text fields are what was stored, scrubbed; they are `null` for a
session that predates them or ran with `log_state: false`.

### One session

`GET /router/audit/sessions/{id}` writes nothing and calls nothing. An unknown
id is a 404. It returns:

- `session`: the binding's fields by name (identity, limits, the four efforts,
  adaptation, decision rule and source, lane, compaction, the last reply's
  token counts), the text fields `title`, `prompt_preview` and
  `workspace_hint`, the counters `turn_count`, `escalation_count` and
  `corrective_count`, `feedback_verdict`, and three computed ones:
  `request_count`, `cache_hit_ratio` (cached over input tokens from the calls
  that reported both, `null` when none did) and `feedback` (the newest row:
  `{verdict, note, source, ts, turn_index}`, or `null`).
- `turns[]`: one entry per user turn, oldest first, each with `turn_index`,
  `kind` (`admission`, `plan` or `user_turn`), `ts`, `decision_row`,
  `user_prompt`, `state` (the stored state's known text fields and its facts,
  or `null`), `state_shape`, `jev` (`latency_ms`, `tokens`, `answers`,
  `shadow_answers`, the packet versions; `null` for a `user_turn`, where Jev was
  not called), `decision` (`model`, `effort`, `rule`, `route`, `quality_lane`,
  `fallback`), `plan` (the turn plan, for a `plan` turn), `execution_rollup`,
  `requests` (the `seq` of each request in the turn) and `feedback[]` (the
  rows given during it).
- `turn_join`: `by_index` when every execution row names its turn, or
  `by_order` when some were written before that column and were matched to the
  newest turn logged before them.
- `admission` (the admission row, projected), `requests[]` (one per execution
  attempt, with its status, timings, token counts, `turn_index` and facts,
  tool names and languages included), `plans[]` (the effort ledger with the
  Jev answers of each plan), and `trace` (the admission's rule-by-rule replay
  under the config it was taken under, or `available: false` with the reason).

`execution_rollup` is `http_requests`, `usage_reported`, `input_tokens`,
`output_tokens`, `cached_input_tokens` (sums, `null` when no call reported the
number), `first_byte_ms_p50`, `tps_p50`, `tool_calls[]` (`{name, count}`),
`assistant_summary` and `files_touched[]`. The last three come from the
client's next request, never from the reply stream.

### Session feedback

`POST /router/sessions/{id}/feedback` needs the control credential, like every
strict-session request, and the session must belong to its owner. The body:

| Field | |
| --- | --- |
| `verdict` | required: `right`, `too_weak`, `too_strong` or `too_slow` |
| `adjust_effort` | `true` to step the effort; default `false` |
| `request_id` | the request the person was looking at; its turn is recorded, else the session's current turn |
| `note` | scrubbed and cut to 2,000 characters |
| `source` | 1–32 of `a-z`, `0-9`, `_`, `-`; default `api` |

Any other field, or a bad value, is `INVALID_ROUTER_INPUT` (400); an unknown
session, or one owned by another credential, is `SESSION_UNKNOWN` (409).

It writes one `feedback` row (under the session's admission decision id, with
`session_id`, `request_id` and `turn_index`) and sets the session's
`feedback_verdict`. With `adjust_effort: true`, `too_weak` steps one rung up
and `too_strong` one rung down along the bound model's configured efforts,
inside the alias cap and the client floor. The model never changes. The step
moves the base, effective, expected and confirmed effort and the wire model
together, as one conditional write on the session's version; the binding
revision does not change, because the binding digest does not cover effort.
The next execution is sent at the new effort.

The reply is `{ok, feedback_id, session_id, decision_id, verdict, turn_index,
model_key, previous_effort, current_effort, wire_model, effort_changed,
reason_code, reason}`. When a step was asked for and did not happen,
`reason_code` says why: `verdict_keeps_effort`, `session_closed`,
`session_blocked`, `adaptive_effort_active` (the experiment owns the effort),
`base_effort_checked` (a Responses session under adaptive effort, whose
history is checked against its base effort), `not_on_ladder`, `at_highest`,
`at_lowest` or `conflict` (the session changed during the write). The verdict
is recorded either way.

### The config workbench

These routes write files or spend a Jev call, so they sit outside
`/router/audit`, behind the same credential. None writes a decision row, a
session or a pin.

`GET /router/config?window=24h|7d|30d` (default `7d`; `1h` works too) returns
`config_hash`, `path`, `yaml` (the exact text this process loaded, or the file
when it still hashes the same; never a stored snapshot, so `null` when neither
is there), `yaml_source` (`loaded`, `file` or `null`), `window`, `since`,
`admissions`, and the shipped ruleset's `rules[]` (`index`, `name`, `lane`,
`protected`, `when`, `use`, `line`, `hits`, `share`), `low_confidence`,
`default` and `fallback` with their hits, every ruleset under `rulesets`,
`routes`, `quality_lanes`, `models` (`provider`, `quota_cost` or `null` when not
written down, `efforts`, `default_effort`, capabilities, `line`),
`quota_policy`, `adaptive_effort` (mode, ladder, thresholds, targets,
`downgrade_confirmations`, ...), `questions` (type, choices or levels and
`max`, `instructions` and `criteria` as the running config words them or
`null`, `line`), `thresholds` (every numeric `gte`/`lte` cutoff per question,
sorted), `effort_order`, `rated[]` (each admission in the window with numeric
answers, newest first, at most 500: `session_id`, `title`, `alias`, `rule`,
`verdict` (the newest one or `null`), `scores` (the score and noul answers as
numbers), and `knife_edge`, `true`/`false` under the running config or `null`
when it cannot be judged), `knife_edge` (the overview's `quality.knife_edge`
over those admissions) and `quota_sources` (the providers whose quota source
is on). An unknown parameter is a 400.

`POST /router/config/preview` takes `{yaml, window?, limit?}` (limit 1-500,
default 200); any other field is a 400. A draft that does not load returns 422
with `{valid: false, errors: [{message, path, line}]}`; the registry and lane
qualification checks run as at startup. Otherwise it replays the newest
admissions in the window under the running config and the draft, at their
recorded pressure, and returns `valid`, `errors`, `config_hash_running`,
`config_hash_draft`, `window`, `since`, `limit`, `evaluated`, `changed`,
`unreplayable`,
`running_reproduces`, `tier_share_before`/`_after` (share by model),
`quota_saved_before`/`_after`, `feedback` (`rated`, `resolved_too_weak`,
`resolved_too_strong`, `conflicts`, `right_changed`, `unranked`) and `diffs[]`
(`row`, `decision_id`, `session_id`, `title`, `ts`, `answers` as numbers,
`before`/`after` as `{rule, route, model, effort, lane}`, `direction`,
`feedback_verdict`, `feedback_effect`). `direction` compares effort on one
model, and two models only by `quota_cost` on the same provider; anything
else is `unranked`. `draft_thresholds` has the shape of `thresholds`, for the
draft; `draft_rules` maps each ladder, named as under `rulesets`, to its rules
as `{index, name, when, line}`, the line in the draft's own text.

`POST /router/config/apply` takes `{yaml, base_hash?, force?}`, one apply at a
time. It refuses with 409 when the running config came from no file, when
`base_hash` is not the running hash, or when the file on disk changed since it
was loaded (unless `force: true`; the 409 then carries `on_disk_hash`). It
validates as preview does (422), copies the file to
`config-backups/router.<UTC timestamp>.yaml` beside the database (folder 0700,
file 0600), writes the draft to a temporary file beside router.yaml and
renames it over (`os.replace`, keeping the file's mode), swaps it into the
running process, snapshots it, and returns `{applied, config_hash,
previous_hash, backup_path, restart_required}`. A request already under way
keeps the config it started with. `restart_required` lists the settings this
process reads only at startup: `settings.host`, `port`, `sqlite_path`,
`admin_token_env`, `max_body_bytes`, `max_concurrent_requests`,
`upstream_connect_timeout_s`, `upstream_read_timeout_s`, and
`session_routing.enabled`. A draft identical to the running file returns
`{applied: false, unchanged: true, ...}` and writes nothing; a failed write is
a 500. `JEV_ROUTER_UPSTREAM` still overrides the upstream on load.

`POST /router/sandbox/jev` takes `{decision_row | decision_id, state?,
questions?}`. A `decision_id` names its deciding row. `questions` maps a
question id to a new `instructions` and/or `criteria` for this call only. It
runs one Jev call, with no draft step, on the supplied state (scrubbed) or the
stored one, asking the questions the running config asks for that kind of
decision (an admission's, or a turn plan's), and returns `row`,
`decision_id`, `event_type`, `config_matches`, `questions`, `overridden`,
`jev_ms`, `original_answers`, `sandbox_answers`, and
`original_route`/`sandbox_route` traced under the running config at the row's
facts and pressure (`null` for a turn plan). It writes nothing. An unknown
decision is a 404, an alias no longer in the config a 422, a decision with no
stored state and none supplied a 422 with `reason: no_stored_state`, and a
failed Jev call a 502 with its `jev_ms`.

`POST /router/eval-cases` takes `{session_id | decision_row, verdict,
min_quality_lane?, max_quality_lane?, note?}`. A `session_id` names its
admission; a `decision_row` may be any decision row. A lane must name a
quality lane in the config. It appends one JSON line to `golden_cases.jsonl`
beside the database (created 0600): `exported_at`, the row's ids, alias, event
type, config hash, state builder and packet version, the stored state if any,
the allowlisted facts, the answers, `chosen` (`rule`, `route`, `model`,
`effort`, `lane`), the pressures, and `expected` (`verdict` and the two lanes).
The note is scrubbed and cut to 500 characters. It returns `{saved, path,
total_cases}`. Nothing else is written.

## Response headers

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

## Environment

| Variable | Read by | What it does |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | the router, `evals/` | The Jev key. Named by `settings.jev_api_key_env`. Unset means classifier fallback; strict admission requires an explicit `admission_fallback` or returns `NO_SAFE_ADMISSION`. |
| `JEV_ROUTER_UPSTREAM` | the router, `evals/` | Overrides `settings.upstream_base_url` whenever it is set and non-empty. The environment wins over the file. |
| `JEV_ROUTER_CONFIG` | the CLI | The default for `-c/--config`, on every subcommand. Defaults to `router.yaml` in the working directory. |
| `JEV_ROUTER_ADMIN_TOKEN` | the router, the adapter, `evals/run_sessions.py`, `evals/qualify_effort.py` | The `/router/*` credential, sent as `X-Router-Admin-Token`. Named by `settings.admin_token_env`. Required for every strict-session request. |
| `UPSTREAM_API_KEY` | `evals/run_outcomes.py`, `evals/qualify_effort.py` | The proxy's own key, for the scripts that call candidate models. The router never reads it: it forwards the client's `Authorization`. |
| `JEV_ROUTER_QUALIFY_LIVE` | `evals/qualify_effort.py` | Must be `1`, alongside `--confirm-live`, before that script makes a real call. |
| `JEV_ROUTER_QUALIFY_ALLOW` | `evals/qualify_effort.py` | Comma-separated profiles the qualification may touch, on top of the in-file allowlist, which is empty. |
| `JEV_EVAL_JUDGE_MODEL` | `evals/run_outcomes.py` | The judge model for the outcome benchmark. Defaults to `gpt-5.6-sol(high)`. |

`.env.example` has the four the router itself cares about.
