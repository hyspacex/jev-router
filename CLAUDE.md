# CLAUDE.md

jev-router is an HTTP shim in front of an OpenAI-compatible proxy. It handles
`model: "auto"` by asking TypeSafe's Jev decision model what kind of request it
is, then choosing a model and a reasoning effort from rules in `router.yaml`.

## Commands

```sh
uv sync
uv run pytest -q                              # offline tests, no provider calls
uv run pytest tests/test_policy.py::test_name -x
uv run jev-router check-config                # validate router.yaml
uv run jev-router serve --mode shadow
uv run jev-router explain request.json        # calls Jev, never forwards
uv run jev-router explain request.json --pressure openai=0.8
uv run jev-router quota --poll                # runs each enabled quota source once
uv run jev-router sessions list               # strict session bindings
uv run jev-router sessions show <id>
uv run jev-router sessions close <id>
uv run jev-router sessions reconcile <id> --plan <plan-id> --outcome applied
uv run jev-router decisions -n 20             # active vs shadow answers, lane, quota
uv run jev-router decisions replay <id>       # re-run the selection from the row
uv run jev-router feedback last too_weak --model gpt-6-astra --effort high
uv run jev-router prune --older-than-days 30  # irreversible
uv run jev-router adapter --alias <strict-alias> --port 18320   # Codex client shim
```

Every subcommand takes `-c/--config`, which also reads `$JEV_ROUTER_CONFIG` and
defaults to `router.yaml` in the working directory.

Eval scripts all spend real API quota. `run_eval.py` and `tune.py` need
`TYPESAFE_API_KEY`; `run_outcomes.py` also needs `UPSTREAM_API_KEY` and
`JEV_ROUTER_UPSTREAM` because it calls the candidate models for real.
`run_eval.py --draft` is the one classification run that touches the upstream.

```sh
uv run python evals/run_eval.py --group final --repeats 3
uv run python evals/run_eval.py --variants router_yaml --stability
uv run python evals/run_eval.py --variants router_yaml --control shuffled-labels
uv run python evals/run_eval.py --variants router_yaml --control constant-state
uv run python evals/run_eval.py --variants router_yaml --control constant-policy
uv run python evals/run_eval.py --variants router_yaml --control length-only
uv run python evals/run_eval.py --variants router_yaml --slice-holdout agentic
uv run python evals/run_eval.py --packet --repeats 3   # the four arms of spec 13.4
uv run python evals/run_outcomes.py --samples 3 --max-calls 400
uv run python evals/tune.py --variant router_yaml --samples 5000
uv run python evals/tune.py --variant router_yaml --slice-holdout chat

uv sync --group evals                         # datasets, for the importer only
uv run python evals/import_public.py --limit 25
uv run python evals/run_eval.py --variants router_yaml --public evals/cases_public.yaml
```

These two do no live work of their own. The session runner needs a router and
an upstream to do anything; `--list` and `--dry-run` need neither.

```sh
uv run python evals/quota_sim.py --demo        # simulated, never an observed delta
uv run python evals/run_sessions.py --list
uv run python evals/run_sessions.py --dry-run
uv run python evals/run_sessions.py --arms fixed_strong,jev_packet --repeats 2
uv run python evals/run_sessions.py --arms fixed_effort_vs_adaptive \
    --allow-adaptive-effort                    # needs a qualified profile
uv run python evals/effort_replay.py           # offline, synthetic fixtures
uv run python evals/qualify_effort.py --profile <key> --plan
```

## Architecture

`src/jev_router/`

- `config.py` — pydantic models for `router.yaml`, cross-field validation, the
  `JEV_ROUTER_UPSTREAM` override, the config hash stored with every decision.
- `features.py` — turns a chat-completions body into counts and flags. Pure.
- `state.py` — state builders, the registry, the paste/continuation split, and
  `turn_state_v1`, which bounds a client-supplied turn packet.
- `policy.py` — confidence gate, rule matching, route resolution, quota
  threshold shifts, route reordering, caps, floors, capability filters, effort
  clamping. Also `select`, the one admission function of spec 8.1, the quality
  lane guard, the distribution helpers (`p_hard`, `p_nontrivial`) and the
  action-equivalence gate. Pure: no network, no clock, no database.
- `semantic.py` — packet versions, the active/shadow split, `SemanticResult`,
  and `replay_decision`, which re-runs a stored decision from its own row.
- `providers.py` — one circuit breaker per provider, in memory, plus the
  merge of quota pressure and health pressure.
- `quota.py` — the quota source registry (`command`, `static`, `none`), the
  background poller, coherent overlapping windows, `snapshot_status`, and
  `compute_pressure`, which is pure.
- `deciders/base.py` — the `Decision` dataclass and the decider registry.
- `deciders/jev.py` — `classify` makes one batched call and returns the
  answers; `decide` runs the cross-model policy over them. Falls back on any
  error.
- `deciders/rules.py` — no-network decider that guesses from counts.
- `pins.py` — sqlite: pins, decision log, feedback, conversation key, and the
  forward-only column migration.
- `sessions.py` — strict session bindings and the bounded request-ID history,
  on the connection `pins.py` owns. Conditional version writes, the in-process
  execution claim, the startup guard, the binding digest, the resolve request
  schema and the 14 error codes. Legacy pins are never read or written here.
  Also the decision-log columns the semantic packet and the quality guard add,
  in a map of their own, and the `turn_plans` ledger with the session columns
  the effort experiment adds, in another.
- `effort.py` — the between-turn turn-plan policy and the ledger's status
  transitions. Pure: a ladder in, a rung out. It never chooses a model.
- `protocols.py` — the experimental Responses contract: the
  `configuration_update` item, full-replay and chain validation against the
  ledger, what a compaction request may be, and the bounded read-only SSE
  observer. Not a converter.
- `adapter.py` — the Codex client shim behind `jev-router adapter`. A client,
  not policy: it resolves a binding, asks for turn plans, inserts the items
  the plans hand it, and chooses nothing. In memory only.
- `feedback.py` — validation shared by the CLI and the HTTP endpoint.
- `app.py` — Starlette routes, forwarding, upstream fallbacks, streaming,
  shadow mode.
- `security.py` — router-endpoint authentication, request-size and concurrency limits.
- `deciders/validation.py` — validates provider answer shape and numeric bounds.
- `cli.py` — serve, check-config, explain, quota, sessions, decisions,
  `decisions replay`, feedback, prune.

Request flow: `app.chat_completions` → `features.extract_features` →
`pins.conversation_key` and a pin lookup → `deciders.jev` → `state.build_state`
→ Jev → `policy.select` (with the pressures) → `app.forward_alias`, which
walks the route's entries and applies `policy.apply_effort` to each.

Strict sessions (`session_mode: strict`, `docs/R2_SPEC.md`, `docs/SESSION_ROUTING.md`)
take a second path: `app.resolve` → `features.extract_features` →
`deciders.jev` → `policy.select` → `policy.context_budget` → a `prepared`
row in `sessions.py`. Execution goes through `app.managed_execution`, which is
recognised before the "a concrete model means passthrough" branch, on
`/v1/chat/completions` or `/v1/responses` according to the bound protocol.

The between-turn effort experiment (`docs/ADAPTIVE_EFFORT.md`) adds a third:
`app.turn_plan` → `state.turn_state_v1` → `deciders.jev.classify` →
`effort.plan_turn` → a `turn_plans` row. The client records the item the plan
gives it, `protocols.py` checks the history against the ledger before
forwarding, and `protocols.Observer` taps the reply to confirm it.

- `deciders/jev.py` also holds the optional AutoMix-style draft step. It is off
  unless an alias sets `draft.enabled`, it fails open, and a pinned turn never
  reaches the decider so it never pays for it.

`evals/`

- `common.py` — cases, slices, splits, atomic `labels`, disk cache, API
  clients, the upstream `Budget`, `ROUTE_COST`, and the two case-level control
  transforms.
- `manifest.py` — the run manifest of spec 13.1, written beside every run. A
  cache-only run is labelled `policy_replay`.
- `metrics.py` — pure. Wilson intervals, ECE, the cost-matched random baseline,
  Gap@Oracle and Gain@BestSingle, the seeded perturbations, the paired
  bootstrap, the chance and majority-class rates.
- `cases.yaml` — 171 labelled requests. Every case has a `slice`; adversarial
  cases also have `attack` and `attack_vector`. A long-context case carries a
  `generated:` block instead of a `request:` block.
- `generators.py` — seeded synthetic material for the long-context cases.
- `variants.yaml` — overlays on `router.yaml`, one per experiment.
- `run_eval.py` — classification and routing scores, slices, controls,
  stability, calibration, per-question agreement, and `--packet` for the four
  arms of spec 13.4.
- `quota_sim.py` — chronological replay of quota windows, resets, stale
  telemetry and outages. Output is labelled `simulated`.
- `session_tasks/` + `run_sessions.py` + `session_client.py` — eight pilot
  coding sessions across the six families of spec 13.5, in disposable
  directories with a scrubbed environment and hard caps.
- `effort_replay.py` + `effort_fixtures.yaml` — the between-turn policies
  compared offline over synthetic turn sequences, resampled by session.
- `qualify_effort.py` — the live qualification of one exact deployment. It
  plans by default; its allowlist is empty and it has never been run.
- `REPORT_TEMPLATE.md` — keeps policy replay, live qualification, coding
  outcomes and subscription observations apart.
- `outcome_specs.yaml` + `checks.py` + `run_outcomes.py` — grade real replies,
  k samples per route, winner flip rate, truncation, judge-against-check.
- `tune.py` — search the policy numbers. Also `--slice-holdout`.
- `import_public.py` — sample public datasets into the git-ignored
  `cases_public.yaml`. Needs `uv sync --group evals`.
- `exp_builders.py` — state builders that exist only to be measured.
- `EXPERIMENTS.md` — the log, one change per experiment.

## Rules

- A model joins the pool through the admission test (`evals/admission.py`,
  POOL.md), not on vendor numbers. Record the verdict in POOL.md.

- Extend through `router.yaml` and the registries. A provider, model, question,
  route, rule, ruleset, alias or client floor is config. Only a new state
  builder, decider or quota source needs Python.
- `policy.py`, `features.py` and `effort.py` stay pure. No I/O, no randomness,
  no clock. Quota pressure arrives as an argument, never as a lookup.
- Never log or store message text, prompts or API keys. The conversation key
  hashes the `Authorization` header. `settings.log_state` is for debugging only.
- A Jev failure must never fail a request. Every path through `deciders/jev.py`
  returns a `Decision`.
- A quota failure must never fail a request either, and must never change one.
  A missing source, a failed command, a missing field or a stale snapshot all
  mean pressure 0, which routes as the router routed before quota existed.
  Never poll on the request path.
- Quota pressure may only make a provider harder to reach. Shifts are bounded
  by `max_shift`, monotone in pressure, and one-directional. A `protected`
  rule ignores them. Only an `equivalent: true` entry may be promoted ahead of
  a route's primary. Strict admission ignores shifts altogether.
- A spent provider is an availability fact, not pressure. A fresh window at
  `exhausted_at` refuses a new strict binding to it with
  `PROVIDER_UNAVAILABLE` and the reset time. A stale, errored or unknown
  reading never counts as spent, and an existing binding is never touched.
- Revalidate every pin against current alias/client permitted models, effort
  bounds, tools, vision and context. Only hard infeasibility permits replacing
  a pin, with a new decision id; search all eligible models, not just larger
  windows. Quota and transient failures never move a valid pin, a pinned turn
  has no fallback plan, and a reused pin keeps its TTL. Keys are scoped by
  authorization, alias and client. Commit a new or replacement pin only after
  upstream 2xx headers, never at candidate selection and never from a
  temporary decider fallback; a later stream failure is recorded separately
  and never switches models.
- An empty allowed intersection, or an impossible effort, capability or
  context constraint, is a routing error. Recovery never silently discards a
  configured restriction.

- A strict binding is a contract, not a cache entry. It never repins, never
  expires on `pin_ttl_seconds`, and gets a one-entry execution plan: no
  cross-model fallback on 429/5xx/timeout, no decider call, no quota
  influence. Overflow, a lost capability or a reduced configured limit report
  a machine code and keep the binding; only the caller replaces it. An
  omitted `session_mode` means legacy, and legacy behaviour stays byte for
  byte what it was.
- Strict control and execution requests always need the router control
  credential, even on loopback: a session is scoped to its owner.
  `require_control_token: false` is a config error, not a way off.
- Admission runs the decider exactly once and never executes anything. Jev
  failure uses the alias's explicit `admission_fallback` or returns
  `NO_SAFE_ADMISSION`, its answers are stored as absent, and recovery does not
  revisit the binding. A resolve retry must not spend another Jev call.
  Record the fallback that bound, never the discarded heuristic guess: its
  route and lane, `quota_changed_choice: false`, and no counterfactual.
  `decisions replay` re-runs the fallback route for such a row.
- The binding digest covers profile identity and negotiated limits only.
  Never quota, timestamps or effort.
- `session_requests` distinguishes accepted, completed, rejected, stream
  failure and unknown. A repeated id never calls the provider again, and a
  record whose outcome is unknown is never pruned.
- One request in flight per session, claimed in memory. A second is a
  conflict, never a queue, and a second strict service on the same database in
  one process is refused. The guard cannot see another process: one service
  per database.
- An execution is held to the limits it negotiated. An output ask above the
  ceiling is `CONTEXT_BUDGET_EXCEEDED` with the binding kept; the body is
  never rewritten to fit. With `previous_response_id` the budget uses the
  input+output counts the last reply reported for that session, stored as two
  numbers and nothing else. Without one the size is unknown, never zero, and
  the reserve is taken against the whole window rather than the delta.

- The between-turn effort experiment is off unless three opt-ins agree: the
  mode, `adaptive_effort: true` on a strict alias, and the client's
  `turn_boundary_reporting`. The mode is agreed at resolve and stored on the
  session row; a session never gains it later, and switching it off stops new
  decisions without erasing the effort or the ledger. `active` needs a
  profile with a tested strategy, `qualification: verified` and a
  `qualification_ref` naming a file that exists. Never mark a real model in
  `router.yaml` verified.
- An effort change keeps the model, the provider, the negotiated limits and
  the base effort. It happens at most once per user turn, at a reported
  boundary, and never inside a tool loop. Upward acts at once and goes straight
  to the rung `targets` asks for, which may be two rungs (`upward: jump`, the
  default; `step` is the old single rung). Downward moves one rung and needs
  `downgrade_confirmations` consecutive recommendations. A short "continue" is
  not downgrade evidence, an environment or missing-information failure is not
  by itself a reason to raise, and a classifier failure keeps the current
  effort rather than guessing a cheaper one. Quota may only resolve an
  undecided turn downward, above every floor.
- The router owns the plan; the client owns the transcript. The router never
  inserts an update item and a client-authored one is refused. Every applied
  update has to come back at its recorded position with a matching prefix
  hash, and the base setting never moves.
- A compaction the client asks for is passed through and opens a compaction
  epoch (owner decision, 2026-09-19). The router never compacts, summarises or
  rewrites a provider item, and never changes model. In a new epoch, updates
  from earlier epochs stop being position-validated and stop being required,
  the expected and confirmed effort go back to the base effort, and the next
  eligible turn may plan again. `truncation: auto` and the automatic-management
  fields (`context_management`, `compaction`, `auto_compaction`,
  `auto_truncation`) are still refused.
- 2xx headers are transport acceptance. Only a valid provider completion
  confirms an effort transition, and only a confirmed one moves the effective
  effort; expected and confirmed are separate columns. A reply's
  `reasoning.effort` reports the base effort and is never read as the
  effective one. An ambiguous outcome blocks a competing transition until an
  explicit reconcile.
- Reconciliation supplies a fact only a person can get, so it is legal only
  from `accepted` or `outcome_unknown`: the provider had the request either
  way. Any other status is a structured error that writes nothing. An
  `applied` reconciliation with no recorded anchor is a hole: execution and
  `keep` plans carry on, a replay is still checked for what it honestly can
  be, a chain must not resend the update, and the next effort change is
  blocked with the plan named.
- The Responses observer is read-only and bounded. It yields every byte
  unchanged, never assembles output or reasoning text, and a parse failure
  marks lineage unknown rather than retrying or altering the reply.
- A `request_parameter` result is labelled with its configured
  `cache_behavior` and is never reported as native cache preservation.
- A shadow question is measured and never read. It rides in the same batched
  call, is validated on its own, and a malformed one is dropped and counted.
  It may not weaken the validation of the active answers, and it may not reach
  `policy.evaluate` or `effort.Evidence`. Holding a change back is deciding
  too. `failure_mode` is shadow by default; promoting it into
  `experiments.adaptive_effort.questions` is what lets it count, and until
  then what it would have done is recorded on the plan as
  `facts.shadow_counterfactual`. Active and shadow sets stay disjoint, and an
  alias that routes on a question may not also be measuring it.
- `classify` returns answers and chooses nothing. Turn assessment must never
  be able to reselect a model.
- A distribution condition (`p_hard_*`, `p_nontrivial_*`) lives in a named
  ruleset, never in the shipped `policy` block. `distribution_policy` decides
  what it may do: `off` never fires, `shadow` records the route with and
  without it, `active` lets it choose. No vector means the comparison cannot
  be made, so it reads as "no". Never multiply separate Nouls into a
  pseudo-probability.
- Capability and adequacy are different filters. A rule that names a lane may
  only be moved inside that lane: a threshold shift, an `equivalent` promotion
  and a capability replacement all have to land on a qualified pair, or the
  adequate provider is kept and the deferral is recorded. A route's failure
  fallbacks are not lane members. Record `evidence: weak` whenever "the same
  adequacy under both policies" cannot be claimed.
- Every qualified pair names where its evidence is. A lane entry with no
  `qualification_ref` is a guess and config load refuses it.
- A provider's overlapping windows stay separate records. Pressure is computed
  per fresh window and the highest wins; never pair one window's usage with
  another's reset. Stale, unknown and errored readings contribute 0 and stay
  visible as themselves. Unknown is never reported as unused capacity.
- A decision must be replayable from its own row: config hash, answers,
  request facts and the pressure it was taken under, with no prompt. Replay at
  the recorded pressure, and say so when the config hash has moved rather than
  claiming a match.
- Do not touch the streamed response bytes. `app._stream` may buffer for usage
  on non-streamed replies, and must yield chunks unchanged. An upstream
  fallback is chosen on the response status, before anything is streamed;
  never switch model after a byte has been sent.
- sqlite migrations are forward-only and additive. Add a nullable column to
  `ADDED_COLUMNS` in `pins.py`, or to one of the maps in `sessions.py` for a
  later column; never drop or rewrite one. Each release keeps its own map so
  its migration list says exactly what it added. A database from an older
  version must keep working. A new index is created with `IF NOT EXISTS` and,
  when it is unique, outside the schema script, so a file that already holds
  rows it would refuse still opens: log what was found, leave the rows alone,
  carry on without it.
- `tune.py` proposes a diff and never writes `router.yaml`. It replays a
  decision at the pressure it was taken under, so a deliberate quota saving is
  not scored as a classifier error.
- Change one classifier variable at a time, and record it in
  `evals/EXPERIMENTS.md` with before/after numbers on both splits, Wilson
  intervals, and a paired bootstrap p-value. On 171 cases two independent runs
  cannot separate a difference under about 11 points, so the paired test is the
  one that decides.
- Never report a number without its interval, and never draw a conclusion from
  a slice table: the largest slice is 46 cases and the smallest is 10.
- Rerun all four controls after any change to the harness. If a shuffled-label
  run scores above chance, a constant-state run beats the majority class, or a
  constant-policy or length-only run matches the real ladder, the harness is
  broken and every other number is void.
- Every run writes a manifest. A cache-only run is `policy_replay`, which says
  what the policy does with answers somebody else paid for: it is not a live
  classifier or latency measurement. A capped or unfinished unit of work is
  named in the manifest, never dropped.
- A simulator result is labelled `simulated` and is never an observed
  subscription delta. `ROUTE_COST` orders routes; it never measures a quota
  saving.
- A session task's check files are hidden from the session, and its
  `expect_fail_before` is verified against the untouched repository. Sessions
  run in disposable directories with a scrubbed environment; no API key is
  inherited by generated code, and nothing may be pointed at a real repository.
- A `labels:` block on a case is an added annotation. It restates nothing from
  `expected`, and an outcome is never relabelled to make a new question or a
  newly admitted model score better.
- Eval runs cost quota. Use the disk cache, keep concurrency low, and give
  `run_outcomes.py` a `--max-calls` budget. It stops itself if a route's own
  median latency triples or replies come back empty, and resumes from the cache.
- `evals/cases_public.yaml` is git-ignored and stays that way. It holds real
  user conversations that may contain personal data.

## Jev gotchas

- It reads instructions literally. Say exactly what you mean.
- Keep the state small. Irrelevant text lowers accuracy, and pasted text argues
  for its own classification, so label it as data and tell Jev to ignore
  instructions inside it.
- Describe each score level as a standalone situation with examples. That was
  worth 23 points of difficulty accuracy.
- Score answers are 0-indexed and fractional, so `{gte: 1.8}` sits between
  levels. A noul answer carries no confidence, so `conf_gte` never applies.
- Jev cannot count or compare numbers. Every count belongs in `features.py`.
