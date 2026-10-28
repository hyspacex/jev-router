# Inspecting and operating the router

[Documentation index](../README.md) · [Project home](../../README.md)

## Live audit dashboard

Open **`/dashboard` on the router's own address** (for a default local service,
`http://127.0.0.1:8318/dashboard`). It runs inside the router; no frontend build
or second service is needed. Restart the service after upgrading so it serves
the new page.

### Access

The page itself is a public, data-free shell. Its data comes from `/router/*`
endpoints behind the router admin token. Enter the token in the page. After
its first successful request the page keeps the token in this browser's local
storage, so a reload does not ask again; Disconnect removes it, and so does a
token the router refuses. It is never put in a URL. Use Disconnect on a
browser other people use. Without a configured token, only
direct loopback access can read (a Tailscale/LAN address is not loopback), and
feedback from the page does not work at all: like every strict-session request,
it needs the token, loopback included. Use HTTPS or a trusted encrypted tunnel
for remote access. This is a global view for one owner, not a per-session-owner
view.

The view on screen refreshes every five seconds; Pause stops it. The address
says where you are, so a link can be kept: `#overview?w=7d`,
`#sessions?model=gpt-6-sol&feedback=too_weak`, `#rules`, `#s=<session id>`, or
`#<slug>` for a session bookmarked with `jev-router bookmarks add`. Keys: `o`,
`s` and `r` switch views, `/` searches sessions, `j` and `k` move through the
list (on a session page, to the next or previous session), the arrow keys or
`[` and `]` move between turns, and `Escape` goes back.

A value no row recorded is shown as a gap, never as zero. Jev's answers are
structured judgments, **not a written reasoning trace**.

### Overview

How the router is doing over one window (1h, 24h, 7d or 30d). A `?` beside a
label gives its definition.

- **The headline band.** Four numbers, each with one comparator under it.
  *Frontier quota saved*: calls that did not spend the frontier model's quota,
  weighted by each model's `quota_cost` (a comparison against every call
  going to the priced model with the highest `quota_cost`, not a measured
  subscription delta). *Router overhead per admission*: Jev plus policy at
  admission, p50, with all Jev time spread over the execution calls beside
  it. *Clean turns*: turn plans whose `corrective_followup` answer is below
  the configured threshold. *Calls completed*: with the problem counts
  (unknown, rejected, stream failed), each linked to the matching sessions,
  and the fallback and low-confidence admission rates. A line under the band
  gives the cache hit (cached over input tokens, from calls that reported
  both) and the circuit breakers.
- **Requests beside quota.** Requests per bucket with the first-byte p50,
  and beside them one row per provider the router knows and one per CodexBar
  subscription it only tracks. Each usage window is a meter with a tick where
  pace says it should be, and when it resets. While quota routing is on, a
  routed provider also says what quota is doing to routing right now: `calm`
  (pressure 0), `elevated` (pressure, but not above
  `quota_policy.demote_above`), `demoting` (above it: route entries on this
  provider move back) or `exhausted` (a fresh window at `exhausted_at`). A
  stale or failed reading is marked as such and steers nothing. A tracked
  subscription never steers; it shows its 30-day cost when it reports one.
  The quota rows are the page's one live source, the quota
  monitor's last readings read from memory; nothing on the page polls a
  provider. See [the CodexBar source](quota.md#the-codexbar-source).
- **By model.** Per model and effort: admissions, calls, first-byte p50 and
  p95, and tokens per second while streaming (output tokens over the time
  after the first byte). Total response time is not the headline: it mostly
  measures how much the model wrote. Admissions by the first choice
  question's answer, as a heatmap, are folded under the table.

Legacy alias traffic, when there is any, has its own panel last and is never
mixed into the session numbers. `GET /router/audit/overview` also returns the
knife-edge share, early escalation, feedback counts and shadow answers; the
page does not draw them yet.

### Sessions

Which session is which. Each row is one strict session, in six columns: the
session (its title, the first line of its first prompt, scrubbed, then the
client, the workspace and the number of user turns), when it was last seen,
model and effort (with the move, when the effort changed), the admission
rule, the latest feedback verdict, and the number of problems. The workspace
is the last path component of the directory the harness reports.

Saved views across the top: All, Problems, Unrated and Pinned (bookmarked
sessions). One search box matches the session id, the title and the prompt
preview, and also takes `key:value` filters such as `rule:hard`: client,
model, rule, feedback, state, adaptive effort mode, problem and last seen.
Each active filter shows as a chip you can remove.

### A session

Why this route and effort, and how it went, one user turn at a time. The page
opens with the title and one strip of facts: the bound model and effort, the
client, the workspace, the start time, the turns and calls. The feedback
buttons sit at the right of the strip. Under it, one sentence says how the
session was admitted, for example the rule and the score that decided it, or
the fallback route when Jev failed.

**The turn list**, down the left. One entry per user turn, not per HTTP call:
an agent makes many calls for one prompt, and the router decides only at turn
boundaries. Turn 1 is the admission. A later turn opens with a turn plan when
the session uses adaptive effort; otherwise it opens when an execution request
carries more user messages than the one before it, and that turn records its
prompt without calling Jev. An entry shows the prompt's first line, what the
turn did to the route or effort, its calls, its problems, a `corrective` mark
when Jev scored the turn as a correction, and any feedback given during it.
Calls logged before turn tracking existed are matched to turns by log order,
and the page says so.

**The selected turn**, on the right, in four sections:

1. **Why this rule** (for the admission) or **Why this effort** (for a later
   turn). For the admission: the rule, model and effort, what the rule is for,
   the closest miss when a skipped rule came near its cutoff, and the ladder
   replayed from the row under the config it was taken under, one line per
   rule (checked, got, needs, short, sends to). Then what quota did and the
   quality lane with its evidence. A rule's name opens a card that says what
   it matches, why it exists and where it sends work. A fallback admission
   shows the alias's fallback, because no rule ran. For a turn plan: the
   effort move, its status, the ladder and the thresholds. For a turn without
   a plan: the effort it ran at and, when that changed, the feedback that
   changed it.
2. **Jev scores.** One row per question, with the thresholds the ladder
   compared it against, and the call's latency. Shadow answers are listed
   apart. A turn that opened without a plan says "Not asked".
3. **Request.** What the user asked and what Jev was shown: the request, any
   pasted material (folded), the earlier request, constraints and
   observations, and the facts. A row written before text capture holds only
   the state's shape, so it shows character counts.
4. **Calls**, closed by default: the turn's calls, tokens, first-byte p50 and
   tokens per second, the tool calls the assistant made, its last text, and
   the HTTP calls in a table.

The tool calls and the assistant's text come from the client's own transcript:
each request carries what the assistant did so far, and the router reads it
there, scrubbed and capped. The reply stream is never read for it, so a turn's
final answer appears once the client sends its next request.

### Feedback

A session page has three buttons at the right of its strip: good, too weak
and too strong. Each records `right`, `too_weak` or `too_strong` against the session through
`POST /router/sessions/{id}/feedback`, and never changes its effort. Every
click adds a row; the newest is the session's verdict.

In Pi, `/jev more`, `/jev less`, `/jev good` and `/jev feedback` (a picker)
record the same verdicts, with an optional note, against the request Pi last
sent. `more` and `less` also step the session's effort one rung up or down on
the same model, within the model's configured efforts, the alias cap and the
client floor. The model never changes. The step is refused, and the verdict
still recorded, on a closed or blocked session, on a session whose effort the
adaptive experiment owns (`adaptation_mode: active`), and on a Responses
session under adaptive effort, whose history is checked against its base
effort. See the [Pi integration](../../integrations/pi/README.md).

A session feedback note is scrubbed and cut to 2,000 characters before it is
stored. Verdicts show in the session list, on the turn they were given in, as
dots on the Rules view's rulers, and in its preview.

### Jev sandbox

Jev sandbox, in the page's `⋯` menu, opens a drawer for the selected turn (the admission,
when the selected turn made no Jev call). It holds the state Jev was shown, as
JSON, and the instructions of each question asked, from the running config.
Edit either and press Run: the router makes **one live Jev call**, which spends
Jev quota, and shows the original and new answers side by side. For an
admission it also shows the rule, model and effort each set of answers routes
to under the running config, at the turn's recorded facts and pressure.

It writes nothing: no decision, no session, no pin, no change to router.yaml.
An edited state is scrubbed before it is sent. Use it to tell a threshold
problem (fix it in Rules) from a state or a question wording that misled Jev.

### Saving an eval case

Save as eval case, in the same menu once the session has a verdict, appends the
session's admission to `golden_cases.jsonl` beside the database
(`POST /router/eval-cases`): the stored state, the allowlisted facts, Jev's
answers, the chosen rule, route, model, effort and lane, the config hash, the
pressure, and the verdict as the expectation. The file is created private
(0600). Nothing else is written.

### Rules

The running policy over a window (7d by default). The ladder is the page; the
score rulers and the settings sit in a side column, and router.yaml opens on
demand. With several rulesets, a switch picks one.

- **Ladder.** Each rule in order: its conditions, where it sends work, its
  lane, and how many admissions in the window it took, linked to those
  sessions. The confidence gate comes first and the default last; Jev-failure
  fallbacks show when there were any. The ladder is read-only. A rule's name
  opens its card (what it matches, why it exists, where it sends work), with a
  link to its line in router.yaml.
- **Where admissions landed.** One ruler per score a rule compares, with each
  threshold marked and named, and one dot per admission in the window at its
  score, coloured by its verdict. A dot links to its session. A cluster of
  `↑ too weak` dots just below a cutoff is what to look for.
- **Settings.** The quota policy and the adaptive-effort settings, one line
  each, linked to their lines.
- **router.yaml.** Edit router.yaml opens the text the router loaded, in an
  editor with line numbers. It stays open while there is a draft or a
  preview. The page edits the text and never regenerates the file from parsed
  data, so comments and the literal question wording stay as written.

The editor has three actions.

- **Check** validates the draft exactly as startup validates a file,
  registries and lane qualification included, and lists each problem with a
  link to its line.
- **Preview** validates, then replays the newest admissions in the window (up
  to 200) under the running config and under the draft, from their stored
  answers, facts and pressure. No Jev call, no write. It shows how many would
  change, each model's share before and after, quota saved before and after,
  what happens to sessions somebody rated (resolved, a conflict, a `right`
  session that would change, or unranked when the two choices have no order),
  and each changed session.
- **Apply** asks first. It refuses when the running config came from no file,
  when the running config changed since the draft was started, or when
  router.yaml changed on disk since it was loaded (the page then offers to
  overwrite). It validates again, copies the current file to
  `config-backups/router.<UTC timestamp>.yaml` beside the database (folder
  0700, file 0600), writes the draft to a temporary file beside router.yaml and
  renames it over (`os.replace`, keeping the file's permissions), and swaps the
  new config into the running process. Requests already under way finish
  under the old one. Breaker state carries over, and so do quota readings for
  providers whose source did not change. The new config is snapshotted so its
  decisions replay. The reply names the backup and lists `restart_required`:
  settings this process reads only at startup (`host`, `port`, `sqlite_path`,
  `admin_token_env`, `max_body_bytes`, `max_concurrent_requests`, the two
  upstream timeouts, and `session_routing.enabled`). Those need a restart.

`JEV_ROUTER_UPSTREAM` still overrides the upstream on load. There is no
`config apply` on the command line; `jev-router config preview draft.yaml`
runs the same replay.

### Trying it locally

A seeded server shows every view with no provider, no Jev key and no network:

```sh
export JEV_ROUTER_ADMIN_TOKEN=seed-admin-token
uv run python tests/seed_dashboard.py --db /tmp/jev-seed/router.db --config /tmp/jev-seed/router.yaml
uv run jev-router -c /tmp/jev-seed/router.yaml serve
```

Open `http://127.0.0.1:18319/dashboard` and enter the token. The script writes
a config built from router.yaml plus the adaptive-effort overlay, with every
quota source off and upstream and Jev URLs that cannot resolve, then drives
the real app in-process through about fifteen invented sessions over the last
30 hours, with Jev and the upstream mocked. The sessions belong to the token
they were seeded under, so serve with the same one. `--force` replaces an
existing database and `--seed` changes the timings. The Jev sandbox fails
there by design, and Apply writes only to the seeded config.

### What the API returns

Every `GET /router/audit/*` endpoint writes nothing and calls nothing, and
projects every field by name. It returns the stored, scrubbed text (titles,
prompts, the state Jev was shown, the assistant's last text, tool names) and
never the conversation key, the owner, a request fingerprint or raw usage. The
endpoints that write or call out (session feedback, eval cases, the Jev
sandbox, config preview and apply) live outside `/router/audit`, behind the
same token. They are listed in the [API reference](../reference/api.md).

A legacy conversation hash is not a client's native session ID, and one
decision ID may span multiple turns: the unique log-row ID identifies each
recorded event. For legacy alias traffic and older events, use
`jev-router decisions` and `jev-router decisions replay <id>` against the
service's config and database.

## Feedback and what is stored

The sqlite file (`settings.sqlite_path`, WAL mode) holds six main tables. Three
are the legacy path and three belong to strict sessions. Two small ones sit
beside them: `config_snapshots` (each config's text once per hash, quota
commands scrubbed) and `audit_bookmarks` (a slug and label per bookmarked
session).

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
| `state` | the state sent to Jev, scrubbed and capped, only while `settings.log_state` is true (the default) |
| `route` | the named route the rule pointed at, if any |
| `intended_model`, `intended_effort` | the primary, when a fallback served instead |
| `fallback_index`, `fallback_reason` | which entry served, and why the earlier ones did not |
| `pressures` | the quota pressure per provider at the moment of the decision |
| `shifted` | every threshold quota pressure moved, with the numbers |
| `reordered` | whether quota pressure changed the route's order |

Later releases added more: `event_type` (`admission`, `execution`,
`effort_plan` or `user_turn`), `session_id`, `turn_id`, `request_id` and
`quality_lane` for sessions; `packet_version`, `shadow_packet_version`,
`shadow_answers`, `action`, `experiment_routes`, `quota_snapshot`,
`quota_status`, `counterfactual`, `exclusions`, `evidence` and
`qualification_ref` for the semantic packet and the lane guard; `state_shape`
(field names and character counts, never text) for the inspector; and
`turn_index`, `user_prompt`, `assistant_summary` and `tool_calls_json` for turn
capture. A `user_turn` row marks a user turn that opened without a turn plan;
Jev is not called for it.

Every added column is nullable, defaults to NULL, and an existing database
gains it the next time the router opens the file. Rows written before then read
back NULL, which is what "this decision predates the feature" means. Nothing is
dropped, rewritten or retyped, ever.

`feedback` — many rows per decision: `decision_id`, `ts`, `verdict` (one of
`right`, `too_weak`, `too_strong`, `too_slow`), `better_model`,
`better_effort`, `note`, `source` (`api` or `cli`, or what a session feedback
request named, such as `pi` or `dashboard`). Session feedback also fills
`session_id`, `request_id` and `turn_index`, and puts the effort it stepped to,
if any, in `better_effort`.

`sessions` — one row per strict binding: the profile identity, the negotiated
limits, the binding revision, the base, effective, expected and confirmed
effort, the adaptation mode, the state (`prepared`, `active`, `blocked`,
`closed`), the quality lane, the versions a replay would need, and the last
reply's input and output token counts. `sha256` hashes for the owner and the
request fingerprint, and nothing recoverable. Turn capture added `title`,
`prompt_preview` and `workspace_hint` (text, under `log_state`),
`turn_count`, `escalation_count` and `corrective_count`; session feedback
added `feedback_verdict`, the newest verdict.

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

For a strict session, feedback goes to the session instead, and may step its
effort on the same model (see [Feedback](#feedback) above):

```sh
curl http://127.0.0.1:8318/router/sessions/$SESSION_ID/feedback \
  -H "X-Router-Admin-Token: $JEV_ROUTER_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"verdict":"too_weak","adjust_effort":true,"note":"missed the race condition"}'
```

### Stored text

With `settings.log_state: true`, the shipped default, the router keeps message
text, and only in named columns: the state Jev was shown (`state`), each user
turn's prompt (`user_prompt`), the assistant's last text and its tool calls in
a turn (`assistant_summary`, `tool_calls_json`), and the session's `title`,
`prompt_preview` and `workspace_hint`. Every one of them goes through
`scrub.py` first, which replaces:

- API keys and tokens with a known shape (`sk-`, `rk-`, GitHub, Slack, Google
  and JWT tokens), and the value after `Bearer` or `Basic`;
- PEM private keys;
- `KEY=value` and `"key": "value"` assignments whose name contains api key,
  secret, token, password or authorization;
- image data URIs, and any unbroken run of encoded characters longer than 256.

The text is then cut to `settings.turn_text_max_chars` (2,048 by default),
keeping the head and the tail. The scrubber is a filter, not a guarantee: a
secret with no recognisable shape gets through. `scrub_secrets: false` is a
config error. Set `log_state: false` to store no message text at all; the
state's shape (field names and character counts) is kept either way.

API keys and the `Authorization` header are never stored; the conversation key
hashes the header, and features keep counts and flags, plus tool names and
languages. Session feedback notes are scrubbed; notes sent to
`/router/feedback` or the CLI are stored as written, so keep secrets out of
them.

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
Set `log_state: false` if message text must not be kept at all. Retention is explicit:

```sh
uv run jev-router prune --older-than-days 30
```

This irreversibly deletes old decisions, feedback, pins, session requests and turn
plans, including tuning evidence and the text stored on those rows. An old session
becomes a tombstone: its binding details, title, preview and workspace are blanked,
and its id stays so a resume answers "closed". Back up the database first. No
automatic deletion runs at startup. `golden_cases.jsonl` and `config-backups/` beside the database are
yours to manage; prune does not touch them.
