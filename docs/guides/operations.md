# Inspecting and operating the router

[Documentation index](../README.md) · [Project home](../../README.md)

## Live audit dashboard

**Availability:** this section documents the dashboard work present in the local
checkout. Its implementation is separate from this documentation change. If
`/dashboard` returns 404 on your revision, use the CLI commands below; the
quick start does not depend on the dashboard.

Open **`/dashboard` on the router's own address** (for a default local service,
`http://127.0.0.1:8318/dashboard`). It runs inside the router; no frontend build
or second service is needed. Restart the service after installing this version.

- A timeline refreshes every two seconds, including outcome updates to streamed
  requests. Pause it to inspect a busy feed.
- Filter by alias, model, event type, client or IDs. Select a strict session or
  hashed legacy conversation to group its events.
- Inspect Jev's recorded active answers, separately labelled shadow answers,
  the fired rule, quota pressure, fallbacks, exclusions and latency. These are
  structured judgments, **not a written reasoning trace**. A rule definition is
  shown only when the recorded config hash matches the running config.
- The latest 100, 300 or 500 events are loaded. This is a bounded audit window,
  not a complete historical search or a count of all provider traffic. Concrete
  model passthrough and requests rejected before logging are not included.

The page itself is a public, data-free shell. Its read-only `/router/audit` API
uses the existing router admin authentication. If configured, enter the token
in the page; it stays in memory, not browser storage or URLs. Without a token,
only direct loopback access works (a Tailscale/LAN address is not loopback).
Use HTTPS or a trusted encrypted tunnel for remote access. This is a global
admin view, not a per-session-owner view.

No prompt text, debug state, feedback notes or credentials are returned by the
audit API. A legacy conversation hash is not a client's native session ID, and
one decision ID may span multiple turns: the unique log-row ID identifies each
recorded event. For older events, use `jev-router decisions` and
`jev-router decisions replay <id>` against the service's config and database.

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
  -H "X-Router-Admin-Token: $JEV_ROUTER_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"decision_id":"last","verdict":"too_weak","better_model":"gpt-6-astra","better_effort":"high"}'
```

`"last"` means the newest decision for that client header, or the newest of all
when the header is absent. `better_model` and `better_effort` are checked
against the configured models and efforts, so a typo is a 400 rather than a bad
row. Notes are cut to 2,000 characters and kept as written.

With `settings.log_state: false`, normal decision logging does not retain prompt
or message text. The conversation key hashes the `Authorization` header, and
features keep lengths rather than content. Debug state can contain request
excerpts, and feedback notes are stored verbatim: keep secrets and private
prompts out of notes and leave state logging disabled in normal use.

## Transport, privacy and deployment

Forwarded replies use raw streaming with their `Content-Encoding` preserved.
The client's `Accept-Encoding` is forwarded; absent means an explicit `identity`
request upstream. Only a bounded private copy is decoded for JSON usage capture
(gzip/deflate supported; unsupported encodings skip capture). Legacy forwarding
does not parse SSE. Managed Responses execution observes a bounded copy for
completion and usage metadata; neither path rewrites the
streamed bytes. Alias decisions record `accepted`, `stream_state`,
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

[`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md) is how this is actually run: a
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
