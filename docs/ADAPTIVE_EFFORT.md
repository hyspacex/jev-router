# Between-turn effort control

Inside a strict session the model does not change. This is the one exception,
and it is not an exception to that rule: at a reported new-user-turn boundary,
on a deployment somebody has qualified, the router may change the **reasoning
effort** of the model the session is already bound to. Nothing else moves. Not
the model, not the provider, not the account, not the negotiated limits, and
not the request-level base effort.

It is an experiment. It is off in the shipped `router.yaml` and no deployment
in that file is qualified for it. Nothing about whether it is *worth* doing
has been measured: no quality comparison against a fixed effort exists, at any
rung. `evals/EXPERIMENTS.md` says so in more detail.

One deployment has had a provisional protocol test —
[`docs/qualification/2026-09-19-gpt-6-astra-cliproxyapi-responses.md`](qualification/2026-09-19-gpt-6-astra-cliproxyapi-responses.md),
for short personal test sessions on one proxy on one day. It is used by the
test config that `examples/make-adaptive-astra.py` builds, and by nothing
else. See "Trying it with Codex" below.

## The supported deployment matrix

| Profile | Protocol | Strategy | Qualification | Cache behaviour |
| --- | --- | --- | --- | --- |
| *(none)* | | | | |

That table is empty on purpose. Qualification is for a model **plus** an
endpoint **plus** the proxy in front of it **plus** an adapter revision. A
model name is not a qualification, and a suffix-style proxy path that accepts
`(high)` is not evidence that the endpoint preserves native configuration
updates. Fill a row in only after `evals/qualify_effort.py` has run against
that exact path and its report is checked in.

The 2026-09-19 CLIProxyAPI report is deliberately **not** a row here.
`qualify_effort.py` did not run, its Q07 evidence is four samples of the
arm that matters, and its own verdict says provisional. A row in this table
is a standing claim about a shipped deployment; that report is permission for
one person to try something for an afternoon.

## Owner decisions, 2026-09-19

Two decisions override what was built before, and the second overrides the R2
spec as well. They are written down here so a reader who finds the code
disagreeing with `docs/R2_SPEC.md` can see that the difference is deliberate.

**1. An upward effort change may jump.** Spec 10.5 says an upward
recommendation "may act at the next eligible boundary" and the first
implementation read that as one rung per turn, so a demanding turn starting
from `low` landed on `medium` and needed a second demanding turn to reach
`high`. The owner decided an upward change goes straight to the rung the
evidence asks for: `low` to `high` in one move when the turn deserves it. The
mapping is `experiments.adaptive_effort.targets`, the direction switch is
`upward: jump|step`, and `jump` is the default. Everything else in 10.5 is
unchanged: one change per user turn, floors, a classifier failure keeps the
current effort, quota only above the floor, and a downward change still needs
low-risk evidence and its confirmations.

**2. Codex's own compaction keeps working.** Spec 10.3 and F17 say automatic
compaction and truncation are refused for the initial native experiment, and
the adapter used to strip Codex's `x-codex-beta-features` header for the same
reason. The owner decided compaction stays exactly what Codex does natively.
The router passes it through, records a compaction epoch, and still never
builds a compactor, never summarises, never rewrites a provider item and
never changes model. `truncation: auto` and router-side compaction are still
refused. See "Compaction" below for what was measured on the wire and what
the router does with it.

## The three modes

| Mode | Jev call per turn | What the client sends | What moves |
| --- | --- | --- | --- |
| `off` | none | the fixed-effort request | nothing |
| `shadow` | one | the fixed-effort request, unchanged | nothing |
| `active` | one | the planned update item as well | the effective effort |

`off` is the default, and an omitted `experiments:` block means `off`. A
router.yaml written before any of this behaves exactly as it did.

Start in `shadow`. It costs one classifier call per turn boundary, records
what it would have done, and changes nothing, so the overhead and the
recommendations can be looked at before anything is at stake.

## Turning it on

Three opt-ins have to agree, and any one of them missing means `off`:

1. `experiments.adaptive_effort.mode` is `shadow` or `active`.
2. The alias says `adaptive_effort: true`, and it is a strict alias.
3. The client declares `turn_boundary_reporting: true` in its resolve payload.

The mode a session gets is decided once, at resolve, and stored on the session
row. A session resolved before the experiment was switched on never gains it,
and one resolved under it keeps its ledger after it is switched off.

`examples/adaptive-effort.yaml` is the overlay. The settings:

```yaml
experiments:
  adaptive_effort:
    mode: shadow                  # off | shadow | active
    qualified_profiles: [gpt-6-astra]
    evaluate_on: new_user_turn
    downgrade_confirmations: 2
    on_unknown: keep
    hysteresis: true
    upward: jump                  # jump | step
    downward: step                # step | jump
    ladder: [low, medium, high]
    ladders: {gpt-6-astra: [low, medium, high]}
    targets:
      difficulty: {medium: 1.8, high: 2.6}
      p_hard_top: 0.6
      corrective_rung: top
      protected_rung: top
    thresholds:
      upgrade_difficulty: 1.8
      upgrade_harm: 0.6
      upgrade_p_hard: 0.4
      corrective_followup: 0.6
      downgrade_difficulty: 0.8
      downgrade_harm: 0.2
      quota_pressure_at: 0.6
      context_safety_fraction: 0.8
    questions: [difficulty, harm_if_wrong, corrective_followup]
    shadow_questions: [failure_mode]
```

`questions` are the turn answers the policy reads. `shadow_questions` are
asked in the same call, recorded, and never read by it. The two sets may not
overlap. `failure_mode` is only asked at all when the turn carries a grounded
harness observation, in either list: without one Jev would be guessing why
work failed.

`uv run jev-router check-config` refuses `mode: active` unless every profile
in `qualified_profiles`:

- declares `effort_control.between_turn` of `native_configuration_update` or
  `request_parameter`;
- carries `effort_control.qualification: verified`;
- names a `qualification_ref` that resolves to a file that exists;
- carries its base effort as a request field (`effort_style: param`) when the
  strategy is `native_configuration_update`, because a model-name suffix would
  give the same setting two owners;
- has at least two rungs on its ladder, after the client floor, the alias cap
  and the lane have narrowed it.

## The policy

The decision is made by `effort.plan_turn`, which is pure: a ladder and some
evidence in, one rung out. It never chooses a model. The turn boundary calls
`deciders.jev.classify`, not `decide`, so the cross-model policy is not run
and there is nothing for a turn assessment to reselect.

The mechanics, all configurable and all recorded on the plan:

- **Upward** may act at the next eligible boundary, with no waiting, and it
  goes straight to the rung the evidence asks for. `thresholds` still decide
  *whether* a turn wants more; `targets` decides *how far*, and the answer is
  never less than one rung and never past the top of the permitted ladder.
  `upward: step` restores the old single rung.
- **Downward** needs low-risk evidence and `downgrade_confirmations`
  consecutive eligible-turn recommendations for the lower rung. A turn that
  recommends something else breaks the run. A confirmed downgrade moves
  **one rung**, which is the deliberate asymmetry: underserving a turn costs
  one turn's quality and is corrected on the next boundary, while a drop that
  is too deep is only noticed after the weak answer has been given. Each
  further rung down is therefore argued again on its own evidence and
  confirmed again. `downward: jump` drops straight to the floor instead.
- **At most one change per user turn.** A tool continuation is part of the
  same turn and never gets a decision of its own.
- **A short "continue"** is not evidence for a downgrade. "yes, go on" says
  nothing about how hard the next step is.
- **How far up is the strongest of four readings.** The difficulty entry for
  each rung (`targets.difficulty`, default `medium` at 1.8 and `high` at 2.6
  on the 0-3 score scale); `p_hard_top`, where the difficulty mass sitting on
  the top level asks for the top rung whatever the mean says; a corrective
  follow-up (`corrective_rung`, default the top rung); and a protected
  admission rule (`protected_rung`, default the top rung) on a turn that asks
  for more at all. Harm on its own asks for one rung: it says a mistake would
  be expensive, not that the work is hard. The rung the evidence reached and
  the rule that reached it are recorded on the plan as `target` and
  `target_reason`, and a plan that skipped a rung says which rule did it in
  its reason.
- **A corrective follow-up can raise**, unless `failure_mode` says the
  obstacle is a missing credential or a missing fact. More reasoning is not
  the remedy for either. `failure_mode` ships as a **shadow** question, and a
  shadow answer never reaches this policy (C25): holding an upgrade back is a
  decision like any other. What it would have done is recorded on the plan as
  `facts.shadow_counterfactual`, with the action, the rung and whether it
  differed, which is how the experiment earns a promotion. Move it from
  `shadow_questions` into `questions` to let it decide.
- **Floors hold.** An explicit client minimum and the floor a protected
  admission rule implies are never crossed, and neither is the quality lane
  the admission rule named.
- **A Jev timeout, malformed output or missing evidence keeps the current
  effort.** Never a cheaper heuristic one.
- **Quota may only resolve an undecided turn downward**, at or above
  `quota_pressure_at`, never on a turn the evidence calls demanding and never
  across a floor.

`hysteresis: false` turns the confirmations off. It exists so replay can
compare the two; see `evals/effort_replay.py`.

## `POST /router/turn-plan`

Experimental. A stable client never needs it.

```
POST /router/turn-plan
X-Router-Admin-Token: <router control credential>
```

```json
{
  "schema_version": "1",
  "session_id": "session-2f766788",
  "binding_revision": "sha256:...",
  "turn_id": "turn-0002",
  "request_id": "plan-0002",
  "previous_turn": {
    "id": "turn-0001",
    "settled": true,
    "pending_tool_calls": 0,
    "active_requests": 0
  },
  "state": {
    "task_request": "Improve the CSV parser.",
    "current_user_request": "Investigate why escaped quotes fail across chunks.",
    "user_constraints": [],
    "quoted_material": [],
    "observations": [
      {"source": "harness", "kind": "test_result", "status": "failed",
       "summary": "escaped-quote regression fails"}
    ]
  },
  "context": {
    "estimated_total_tokens": 12000,
    "estimate_method": "provider-usage-plus-new-input",
    "history_mode": "full_history"
  }
}
```

The `previous_turn` block is the client's word: an HTTP shim cannot see a tool
loop. The router checks what it can — its own in-flight state, the binding,
the previous plan's status, the context budget, the profile — and takes the
rest on trust. Qualification has to exercise that assertion against the real
harness.

`state` is processed and dropped. It is cut to the same bounds as the
admission packet, quoted material keeps its "data, not instructions" label,
and an `observations` source that is not `harness`, `user_report` or
`assistant_claim` becomes `user_report`, so a client cannot promote its own
guess to a harness result.

The answer is one of three actions:

```json
{
  "action": "change_effort",
  "plan_id": "a1b2c3d4e5f6",
  "turn_id": "turn-0002",
  "adaptation": "active",
  "model_key": "gpt-6-astra",
  "base_effort": "low",
  "previous_effective_effort": "low",
  "next_effective_effort": "medium",
  "reason": "low -> medium: difficulty 1.90 is at or above 1.8",
  "status": "planned",
  "update": {
    "item": {"type": "configuration_update", "reasoning": {"effort": "medium"}},
    "rule": "insert this item immediately before the designated new user message, keep every earlier update where it is, and leave the request-level reasoning.effort at the base effort"
  },
  "headers": {"X-Router-Turn": "turn-0002", "X-Router-Turn-Plan": "a1b2c3d4e5f6"}
}
```

- `keep`: nothing to do. In `shadow` this also carries `hypothetical_effort`,
  which is what `active` would have done.
- `change_effort`: the exact item and where it goes. For a
  `request_parameter` profile it is `update.parameter` and `update.value`
  instead, and there is no item.
- `blocked`: the experiment stops for this turn and everything stays exactly
  where it is. The reason says why: a change still in the air, a context
  budget near its safety limit, an unqualified profile, unknown lineage.

Repeating an identical plan request returns the stored plan and spends no
second Jev call. The same turn id with different evidence while a plan is
pending is `SESSION_CONFLICT` (409). An unsettled previous turn is
`TURN_NOT_SETTLED` (409). An earlier update whose outcome is unknown is
`EXECUTION_OUTCOME_UNKNOWN` (409) until it is reconciled.

That holds for two requests arriving at once as well as one after the other
(F07). A unique index on `(session_id, turn_id)` means the database, not the
arrival order, decides which plan a turn has; the loser rereads the winner's
plan and is given that, or `SESSION_CONFLICT` if its evidence differed. An
in-process waiting room per session and turn means the loser usually never
reaches Jev at all. It holds no database transaction across that call: it is
there to save a classifier call, not to be the guarantee.

A database written before the index may already hold two plans for one turn.
Those rows are left exactly as they are, the index is not created, and the
router logs what it found and carries on.

## What the client must do

The router owns the plan. The client owns the transcript. There is exactly one
owner of the update items, and it is the router: a client-authored update is
rejected while adaptation is on, and the router never inserts one itself.

For `native_configuration_update`:

1. Keep the request-level `reasoning.effort` at the base effort, for the whole
   life of the session. The response reports that field back, so moving it
   would make the base and the effective setting impossible to tell apart.
2. Insert the planned item **immediately before** the designated new user
   message, and nowhere else. Never next to another update.
3. Send `X-Router-Turn` and `X-Router-Turn-Plan` with the request that carries
   it.
4. For a full-history replay, keep every previous update at its original
   position. The router recorded a prefix hash for each, because repeated
   identical user text is not a unique anchor.
5. For a `previous_response_id` chain, reference the parent the session is
   known to have accepted and do not resend an update the provider already
   holds.
6. Continue a turn's tool calls with no new item and no new plan. The same
   turn id, the same effective effort.

Anything else is `EFFORT_HISTORY_MISMATCH` (409), before a byte is forwarded.

For `request_parameter` the request carries the planned effective effort in
`reasoning.effort` and the history carries no item. Results from that strategy
are labelled with the profile's configured `cache_behavior` and are **never**
reported as native cache preservation, whatever a report says.

## Trying it with Codex

Codex cannot speak any of the above. It does not resolve a binding, it does
not report a turn boundary, and it has never heard of a plan. So there is a
small client adapter in front of the router that does all of that on its
behalf:

```
codex exec  ->  adapter :18320  ->  jev-router :18318  ->  the proxy
```

`src/jev_router/adapter.py` is a **client**, not routing policy. It chooses
nothing: every item it inserts came out of a `/router/turn-plan` answer, every
refusal the router gives it goes straight back to Codex, and it never retries
on another model. It identifies the conversation from Codex's own
`session-id` header (or `prompt_cache_key`), never from message text, and it
refuses a request that carries no such identifier. It keeps its state in
memory and writes nothing to disk.

It is experimental, and so is everything it points at. Read the qualification
report for the deployment before doing this.

**1. Build a test config.** `router.yaml` stays as it ships, with the
experiment off and nothing verified.

```sh
uv run python examples/make-adaptive-astra.py \
    --out /tmp/astra.yaml --sqlite /tmp/astra.db --port 18318
uv run jev-router -c /tmp/astra.yaml check-config
```

**2. Start the router**, against the proxy, with a throwaway credential.

```sh
export JEV_ROUTER_ADMIN_TOKEN=$(openssl rand -hex 16)
JEV_ROUTER_UPSTREAM=http://127.0.0.1:8317 \
    uv run jev-router -c /tmp/astra.yaml serve --port 18318
```

**3. Start the adapter.** `--upstream-key-file` is optional: the adapter
forwards whatever `Authorization` the client sent, and only puts the key from
that file in its place when the client sent the placeholder token. The value
is never logged or stored.

```sh
uv run jev-router adapter \
    --router http://127.0.0.1:18318 --alias astra-adaptive --port 18320 \
    --upstream-key-file ~/.config/proxy.key
```

**4. Point Codex at it**, with `-c` overrides rather than an edit to
`~/.codex/config.toml`:

```sh
export JEV_CODEX_KEY=placeholder
codex exec \
  -c 'model_providers.jev={name="jev",base_url="http://127.0.0.1:18320/v1",wire_api="responses",env_key="JEV_CODEX_KEY",requires_openai_auth=false}' \
  -c 'model_provider="jev"' -c 'model="gpt-6-astra"' \
  -c 'model_reasoning_effort="low"' -c 'sandbox_mode="read-only"' \
  --skip-git-repo-check \
  "In one sentence: what is the difference between a tuple and a list?"
```

Carry the same conversation on with `resume`, which keeps Codex's session id
and so keeps the router binding:

```sh
codex exec resume --last \
  -c 'model_providers.jev={name="jev",base_url="http://127.0.0.1:18320/v1",wire_api="responses",env_key="JEV_CODEX_KEY",requires_openai_auth=false}' \
  -c 'model_provider="jev"' -c 'model="gpt-6-astra"' \
  -c 'model_reasoning_effort="low"' -c 'sandbox_mode="read-only"' \
  --skip-git-repo-check \
  "Now derive the recurrence rigorously and compute f(12)."
```

`model_reasoning_effort` is the base effort, and the adapter holds the request
field there whatever happens. The effective effort moves through the update
items and is read from the ledger, never from the reply.

**5. Watch it.** The adapter prints one line per request, identifiers and
counts only:

```
[adapter] conv=01a0bbde turn=turn-0002 action=change_effort base=low effective=medium plan=a2a452b6264d req=... items=10 updates=0
[adapter] conv=01a0bbde turn=turn-0002 status=completed response=resp_... reasoning_tokens=126 input_tokens=19351 cached_input_tokens=19072 output_tokens=518 ms=17190
```

```sh
uv run jev-router -c /tmp/astra.yaml sessions list
uv run jev-router -c /tmp/astra.yaml sessions show <session-id>
uv run jev-router -c /tmp/astra.yaml decisions -n 20
```

### What Codex's wire behaviour means for this

Measured against 0.155.1, and worth knowing before reading a result:

- It sends `store: false` and the **whole input** on every request, so this is
  always a full-history replay and never a `previous_response_id` chain. The
  chain path is untested on this route.
- Its transcript is its own. It has no idea the adapter inserted an item, so
  it never sends one back, and the adapter re-inserts every earlier update at
  its recorded index on every request. An adapter restart rebuilds those
  positions from the router's own ledger.
- It sends `session-id`, `thread-id` and `prompt_cache_key`, all the same
  value, and `codex exec resume` keeps it. That is the conversation identity.
- It sends an `x-codex-beta-features: remote_compaction_v2` header, which the
  adapter forwards. On 0.155.1 the header is vestigial: `codex features list`
  reports the feature as removed, and the client compacts locally whatever the
  header says. See "Compaction" below.
- It marks a compaction with `x-codex-turn-metadata`, whose `request_kind` is
  `"compaction"` rather than `"turn"`. That header is the only way to tell a
  compaction from a new user turn, so the adapter reads it.
- Its `GET /v1/models` expects a Codex-shaped body and logs
  `failed to refresh available models` against the router's OpenAI-shaped one.
  It is noise; the conversation works.
- A tool continuation arrives as another `POST /v1/responses` whose input ends
  in a tool result rather than a user message. That is how the adapter tells a
  continuation from a new turn.

## Compaction

Owner decision, 2026-09-19: a compaction the client asks for stays exactly
what the client does natively. The router passes it through, records that it
happened, and never compacts, summarises, translates or drops a provider item
itself, and never changes model (I11).

### What is actually on the wire

Measured on 2026-09-19 with a logging pass-through in front of codex-cli
0.155.1, and with direct probes at the CLIProxyAPI deployment. Both are
written up in the qualification report.

**Codex does not use the provider's compaction flow at all.** It sends
`x-codex-beta-features: remote_compaction_v2` on every request, but
`codex features list` reports `remote_compaction_v2` as **removed** on this
build, and enabling it changes nothing. What it really does when it runs out
of room is:

1. Send an ordinary `POST /v1/responses` carrying the whole history plus a
   trailing user message with no id, which asks for a handover note. Nothing
   in the body says what it is; the only marker is `x-codex-turn-metadata`,
   whose `request_kind` is `"compaction"` instead of `"turn"` and which
   carries `compaction: {trigger, reason, implementation: "responses", phase:
   "pre_turn", strategy: "memento"}`.
2. Open a new window - `x-codex-window-id` goes from `…:0` to `…:1` - and
   rebuild its history from scratch around the reply.

It never sends a `compaction_trigger` item, never sets `context_management`,
never calls `/v1/responses/compact`, and does not replay a provider
`compaction` item even when one is handed to it. Every effort update item is
simply gone from the new window.

**The provider does implement the explicit flow**, and the router supports it
for a client that speaks it: the whole history with `{"type":
"compaction_trigger"}` as the **final** item, answered by one opaque item,
`{"id": "cmp_…", "type": "compaction", "encrypted_content": …}` of about
1.8 kB. A `compaction_trigger` anywhere but last is a 400. A
`configuration_update` earlier in the history, or next to the trigger, is
accepted. A `configuration_update` **before** a replayed `compaction` item is
a 400; after it is fine.

**An effort update does not survive a compaction.** Replaying a compaction
built from a history that carried an update to `high` produced the same
reasoning work as the base arm (73, 71 tokens against 65, 94 at base and 121,
132 with the update); a fresh update placed after the compaction item restored
it (118, 112). Two samples an arm, no interval: it is consistent with the
protocol, and it is not a measurement anybody should lean on.

### What the router does with it

- **The adapter** forwards `x-codex-beta-features` untouched, reads
  `request_kind` out of Codex's metadata header, and tells the router this
  request is maintenance with `X-Router-Request-Kind: compaction`. It asks for
  no plan, spends no classifier call and inserts no item. A compaction is not
  a turn boundary.
- **The router** treats a compaction as a managed maintenance request of the
  same session: same binding, no admission, no turn plan, no effort decision.
  It recognises one either by its shape - a trailing `compaction_trigger` - or
  by the client's word, which is the same kind of trusted-adapter assertion as
  a reported turn boundary, and which can only ever relax what a later request
  is checked against.
- **What is still refused**: `truncation: "auto"` and the automatic-management
  fields `context_management`, `compaction`, `auto_compaction` and
  `auto_truncation`. Those rewrite a history with no request of their own for
  the ledger to see.

### The compaction epoch

An accepted compaction opens a new epoch on the session, recorded in
`sessions.compaction_epoch` and stamped on every plan. In the new epoch:

- updates from earlier epochs are no longer position-validated and no longer
  required in a replay, because the positions they were anchored at no longer
  name anything. They stay in the ledger; they simply stop being asked for.
- whatever can still be honestly checked still is: every update made **since**
  the last compaction has to be in the history, at its recorded position, with
  its recorded prefix hash, and the router is still the only owner of these
  items. An update sitting before a compaction item is refused before it is
  forwarded, because the provider refuses it too.
- the expected and confirmed effective effort go back to the base effort,
  which is what the measurement above says is true. `sessions show` says so
  in words rather than leaving it to be inferred.
- the next eligible new user turn may plan the effort again, which is the
  post-compaction reapplication of spec 10.3.
- an adapter that restarts reads the epoch back from
  `GET /router/sessions/{id}` and restores only that epoch's update positions.

If a compaction is sent and nothing comes back to say it finished, the epoch
still moves - it only relaxes what a replay is checked against - and the
session is marked `compaction_state: unknown`. Further effort changes stop
until somebody reconciles, exactly as they do for an unreadable reply.

Adaptation still stops on its own before
`thresholds.context_safety_fraction` of the negotiated window (F18): the plan
comes back `blocked`, the session carries on unchanged, and the ledger is
untouched. What changed is that the client's way out of a full window - to
compact - is now allowed to proceed.

### What is still untested here

`POST /v1/responses/compact` is handled as a maintenance request, and the
CLIProxyAPI deployment of 2026-09-19 answers it with a **404**. Nothing has
exercised it against a provider that implements it. The provider's explicit
trigger flow has been exercised by direct probe and by offline fixtures, and
by no real client: Codex does not speak it.

## The ledger

`turn_plans` is one row per turn plan. It holds hashes and identifiers: the
turn and plan ids, the sequence, from and to effort, the base effort, the
history anchor or parent response id, expected and confirmed effective effort,
the status, the compaction epoch it was made in, a request fingerprint and the
recommendation. No transcript, no message text, no reasoning text, no keys.

| Status | What it means |
| --- | --- |
| `planned` | decided; nothing has been sent |
| `accepted` | upstream returned 2xx headers, and nothing more than that |
| `confirmed` | a valid successful provider completion carried it |
| `rejected` | definitively refused before acceptance; the same plan is retryable |
| `outcome_unknown` | submitted, and nobody can say what ran |

Only a `confirmed` change moves the session's effective and confirmed effort.
A completion that contains tool calls is still a completion and still
confirms; the tool continuations that follow keep the effort. The reply's own
`reasoning.effort` reports the **base** effort and is never read as the
effective one.

The session row keeps expected and confirmed effective effort in separate
columns, because they are separate facts. An accepted-but-never-completed plan
leaves the confirmed value where it was.

## Reconciling

A disconnected response may well have run. The router will not guess, so an
`outcome_unknown` plan blocks the next change until somebody who can check the
provider says what happened:

```sh
uv run jev-router sessions reconcile session-2f766788 \
  --plan a1b2c3d4e5f6 --outcome applied
```

```
POST /router/turn-plan/a1b2c3d4e5f6/reconcile
{"schema_version": "1", "outcome": "applied"}
```

`applied` confirms the transition. `not_applied` puts the ledger back to the
previous confirmed state and leaves the same plan retryable. Either way the
lineage flag is cleared.

Only a plan the provider actually received can be spoken about this way:
`accepted`, where 2xx headers came back, and `outcome_unknown`, where the
request went out and nothing came back. Anything else is `SESSION_CONFLICT`
(409) and writes nothing at all. A `planned` plan was never sent, so there is
nothing at the provider to go and check and no hand-written `applied` could be
true. A `confirmed` or `rejected` plan already has its answer, and a second
one would rewrite a fact rather than supply a missing one.

An `applied` reconciliation of a request that went quiet leaves a **hole**:
the router never saw where the client put that update, or which response
holds it. The session is not bricked by it.

- Execution carries on. A full replay is still checked for everything that
  can honestly be checked: the update has to be in the history, asking for
  the recorded effort, after the update before it. Its exact position and the
  prefix hash of the history in front of it cannot be checked, so they are
  not.
- A `previous_response_id` delta must still not resend it. The provider holds
  it whether or not a response id was ever read for it.
- Further effort **transitions** stop. The next plan comes back `blocked`,
  naming the plan that left the hole, because a new update's own anchor would
  be measured against a history with a gap in it. `keep` plans carry on as
  usual, and the effort the reconciliation confirmed stays where it is.

The same applies when the bounded SSE observer could not read a reply: lineage
is marked unknown, further transitions stop, and the reply itself is untouched
and never retried. A stream the upstream closed cleanly without ever saying how
the response ended is ambiguous too, however tidily the transport finished: the
attempt reads `unknown`, not `completed`.

Every refusal that a reconcile would clear names the command. `TURN_NOT_SETTLED`
says which plan the router is holding and what state it is in;
`EXECUTION_OUTCOME_UNKNOWN` gives the `jev-router sessions reconcile` line and
the `POST /router/turn-plan/{plan}/reconcile` equivalent. A session report names
the open update as well, so a client can read what is blocking it rather than
working it out from the ledger.

## Rollback

Switching `mode` to `off`, or taking `adaptive_effort` off the alias, stops
new decisions immediately. It does not erase anything:

- the session's effective effort stays where the last confirmed change put it;
- the ledger stays;
- every update already in the history is still required in a full replay, so
  a rollback cannot corrupt a transcript.

Dropping from `active` to `shadow` keeps measuring and stops changing.
`/router/resolve` with `intent: resume` returns the base effort, the effective
effort and the adaptation mode, so a client that restarts picks up where it
was.

## Diagnostics

```sh
uv run jev-router check-config                  # mode, profiles, rungs, qualification
uv run jev-router sessions show <session-id>    # efforts, mode, recent ledger
uv run jev-router decisions -n 20               # effort_plan events
uv run jev-router sessions reconcile <id> --outcome applied
```

`sessions show` prints base, expected and confirmed effective effort and the
recent turn plans, and it says "model unchanged (gpt-6-astra); the next user
turn requests medium effort instead of low under the active native update
experiment" rather than leaving that to be inferred. None of it is ever
injected into a prompt.

Latency is reported in pieces: Jev latency for the plan, the plan end to end,
and the request's own first byte and completion. A first SSE heartbeat is not
first-token latency.

## Evaluating it

```sh
uv run python evals/effort_replay.py               # offline, synthetic, no calls
uv run python evals/qualify_effort.py --profile <key> --plan
uv run python evals/run_sessions.py --arms fixed_effort_vs_adaptive \
    --allow-adaptive-effort
```

`effort_replay.py` compares fixed-low, fixed-medium, fixed-high, a simple
threshold rule, and the policy in three configurations: with hysteresis,
without it, and climbing one rung at a time (`jev_upward_step`). It resamples
by session. Its fixtures are synthetic and labelled synthetic, so the two
upward modes can be compared there for mechanics and for nothing else.

`qualify_effort.py` plans by default. Running it live needs `--confirm-live`,
`JEV_ROUTER_QUALIFY_LIVE=1`, an allowlisted profile, credentials and a call
budget. The allowlist is empty.

Compare against the fixed effort that achieves comparable quality, not only
the most expensive one. Counterbalance the order and keep the cache namespaces
apart. A cache hit is not guaranteed because the update syntax is correct, and
a valid protocol test is not a cache measurement.
