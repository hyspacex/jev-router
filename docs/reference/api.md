# HTTP and CLI reference

[Documentation index](../README.md) · [Project home](../../README.md)

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
uv run jev-router config show --window 7d      # the rules, their lines and hits
uv run jev-router config preview draft.yaml    # replay recent admissions under a draft
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

### Endpoints

The `/dashboard` and `/router/audit` entries describe the separate dashboard
implementation; they may not be available on a revision containing only this
documentation update. Use `decisions` and `decisions replay` from the CLI there.

| Path | Method | Behaviour |
| --- | --- | --- |
| `/v1/chat/completions` | POST | aliases are routed; a strict session executes here; everything else is forwarded unchanged |
| `/v1/responses` | POST | a strict session whose profile speaks the Responses protocol executes here; everything else is forwarded unchanged |
| `/v1/responses/compact` | POST | a standalone compaction, forwarded as maintenance for the bound session |
| `/v1/models` | GET | the upstream list plus one entry per alias |
| `/router/health` | GET | mode, upstream, aliases, models, providers, routes, unhealthy providers, decider, whether a Jev key is set, uptime |
| `/router/providers` | GET | circuit breaker state and the pressure per provider |
| `/router/quota` | GET | quota snapshots, pressures, staleness and the last error per provider |
| `/dashboard` | GET | interactive read-only audit UI; data requires router authentication |
| `/router/audit?limit=N` | GET | prompt-free audit snapshot, 1–500 recent events (default 300) |
| `/router/decisions?limit=N` | GET | recent decisions as JSON |
| `/router/feedback` | POST | record what you thought of a decision |
| `/router/feedback?limit=N` | GET | recent feedback, joined to its decision |
| `/router/resolve` | POST | resolve or resume a strict session binding |
| `/router/sessions?limit=N` | GET | strict session bindings, newest first |
| `/router/sessions/{id}` | GET | one binding: identity, limits, efforts and state |
| `/router/sessions/{id}/close` | POST | end a session deliberately |
| `/router/turn-plan` | POST | an effort-only decision for a new user turn (experimental) |
| `/router/turn-plan/{id}/reconcile` | POST | say what happened to an ambiguous effort update |
| `/router/audit/overview?window=1h\|24h\|7d\|30d` | GET | router health from the tables: counts, rates, percentiles and buckets; see below |
| `/router/config?window=` | GET | the running config: its text, rules with their lines and hits, routes, lanes, models, thresholds |
| `/router/config/preview` | POST | validate a draft and replay recent admissions under it; no Jev call, no write |
| `/router/config/apply` | POST | validate a draft, back up router.yaml, write the draft, swap it in |
| `/router/sandbox/jev` | POST | ask Jev once about a stored decision with an edited state or wording; writes nothing |
| `/router/eval-cases` | POST | append one admission to `golden_cases.jsonl` beside the database |
| anything else | any | streamed through to the upstream, method and path unchanged |

An alias name on any other POST endpoint returns 400 with a message saying so.

Every `/router/*` path needs `X-Router-Admin-Token` once the environment
variable named by `settings.admin_token_env` is set; without one, only direct
loopback callers are accepted. The strict-session paths need that credential
always, loopback included, and there is no setting that turns it off:
`resolve`, `turn-plan`, `sessions`, and any managed execution on `/v1/*`.

### The overview

`GET /router/audit/overview` writes nothing and calls nothing. Every rate is
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
| `efficiency.quota_saved` | `saved_rate` = 1 - mean `quota_cost` over executions of models router.yaml prices; `quota_cost_sum`, `priced`, and `unpriced` (a model with no `quota_cost` written down) |
| `efficiency.cache` | cached over input tokens, from rows that reported both: `rows`, `cached_tokens`, `input_tokens`, `rate`, and `by_model[]` |
| `efficiency.usage_reported` | `executions`, `reported`, and how many carried `input`, `output`, `cached` counts |
| `matrix` | admissions by the first choice question's label (`question`, `tasks`) and the chosen pair: `pairs[]`, `cells[]` each with `n` and a `corrective` rate from its sessions' plans |
| `reliability` | `fallback` and `low_confidence` admission rates, and `breakers` per provider: `state`, `healthy` and the breaker's counters, never its error text |

Each `series` bucket also has `first_byte_p50_ms` beside `response_p50_ms`.

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
`downgrade_confirmations`, ...), `questions` (type, choices or levels, `line`),
`thresholds` (every numeric `gte`/`lte` cutoff per question, sorted) and
`effort_order`. An unknown parameter is a 400.

`POST /router/config/preview` takes `{yaml, window?, limit?}` (limit 1-500,
default 200). A draft that does not load returns 422 with
`{valid: false, errors: [{message, path, line}]}`; the registry and lane
qualification checks run as at startup. Otherwise it replays the newest
admissions in the window under the running config and the draft, at their
recorded pressure, and returns `evaluated`, `changed`, `unreplayable`,
`running_reproduces`, `tier_share_before`/`_after` (share by model),
`quota_saved_before`/`_after`, `feedback` (`rated`, `resolved_too_weak`,
`resolved_too_strong`, `conflicts`, `right_changed`, `unranked`) and `diffs[]`
(`row`, `decision_id`, `session_id`, `title`, `ts`, `answers` as numbers,
`before`/`after` as `{rule, route, model, effort, lane}`, `direction`,
`feedback_verdict`, `feedback_effect`). `direction` compares effort on one
model, and two models only by `quota_cost` on the same provider; anything
else is `unranked`.

`POST /router/config/apply` takes `{yaml, base_hash?, force?}`. It refuses with
409 when the running config came from no file, when `base_hash` is not the
running hash, or when the file on disk changed since it was loaded (unless
`force: true`). It validates as preview does (422), copies the file to
`config-backups/router.<UTC timestamp>.yaml` beside the database (mode 0700),
writes the draft atomically, swaps it in, snapshots it, and returns
`{applied, config_hash, previous_hash, backup_path, restart_required}`.
`restart_required` lists the settings this process read only at startup.
`JEV_ROUTER_UPSTREAM` still overrides the upstream on load.

`POST /router/sandbox/jev` takes `{decision_row | decision_id, state?,
questions?}`. `questions` maps a question id to a new `instructions` and/or
`criteria` for this call only. It runs one Jev call on the supplied state
(scrubbed) or the stored one, and returns `jev_ms`, `original_answers`,
`sandbox_answers`, and `original_route`/`sandbox_route` traced under the
running config at the row's facts and pressure (`null` for a turn plan). A
decision with no stored state and none supplied is a 422 with
`reason: no_stored_state`.

`POST /router/eval-cases` takes `{session_id | decision_row, verdict,
min_quality_lane?, max_quality_lane?, note?}` and appends one JSON line to
`golden_cases.jsonl` beside the database: the stored state if any, the
allowlisted facts, the answers, the chosen route, the config hash and the
expectation. The note is scrubbed and cut to 500 characters. It returns
`{saved, path, total_cases}`.

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
| `TYPESAFE_API_KEY` | the router, `evals/` | The Jev key. Named by `settings.jev_api_key_env`. Unset means classifier fallback; strict admission requires an explicit `admission_fallback` or returns `NO_SAFE_ADMISSION`. |
| `JEV_ROUTER_UPSTREAM` | the router, `evals/` | Overrides `settings.upstream_base_url` whenever it is set and non-empty. The environment wins over the file. |
| `JEV_ROUTER_CONFIG` | the CLI | The default for `-c/--config`, on every subcommand. Defaults to `router.yaml` in the working directory. |
| `JEV_ROUTER_ADMIN_TOKEN` | the router, the adapter, `evals/run_sessions.py`, `evals/qualify_effort.py` | The `/router/*` credential, sent as `X-Router-Admin-Token`. Named by `settings.admin_token_env`. Required for every strict-session request. |
| `UPSTREAM_API_KEY` | `evals/run_outcomes.py`, `evals/qualify_effort.py` | The proxy's own key, for the scripts that call candidate models. The router never reads it: it forwards the client's `Authorization`. |
| `JEV_ROUTER_QUALIFY_LIVE` | `evals/qualify_effort.py` | Must be `1`, alongside `--confirm-live`, before that script makes a real call. |
| `JEV_ROUTER_QUALIFY_ALLOW` | `evals/qualify_effort.py` | Comma-separated profiles the qualification may touch, on top of the in-file allowlist, which is empty. |
| `JEV_EVAL_JUDGE_MODEL` | `evals/run_outcomes.py` | The judge model for the outcome benchmark. Defaults to `gpt-5.6-sol(high)`. |

`.env.example` has the four the router itself cares about.
