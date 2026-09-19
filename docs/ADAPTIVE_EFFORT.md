# Between-turn effort control

Inside a strict session the model does not change. This is the one exception,
and it is not an exception to that rule: at a reported new-user-turn boundary,
on a deployment somebody has qualified, the router may change the **reasoning
effort** of the model the session is already bound to. Nothing else moves. Not
the model, not the provider, not the account, not the negotiated limits, and
not the request-level base effort.

It is an experiment. It is off in the shipped `router.yaml`, no deployment in
this repository is qualified for it, and nothing about it has been measured.
`evals/EXPERIMENTS.md` says so in more detail.

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
    ladder: [low, medium, high]
    ladders: {gpt-6-astra: [low, medium, high]}
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

`uv run jev-router check-config` refuses `mode: active` unless every profile
in `qualified_profiles`:

- declares `effort_control.between_turn` of `native_configuration_update` or
  `request_parameter`;
- carries `effort_control.qualification: verified`;
- names a `qualification_ref` that resolves to a file that exists;
- carries its base effort as a request field (`effort_style: param`), because
  a model-name suffix would give the same setting two owners;
- has at least two rungs on its ladder.

## The policy

The decision is made by `effort.plan_turn`, which is pure: a ladder and some
evidence in, one rung out. It never chooses a model. The turn boundary calls
`deciders.jev.classify`, not `decide`, so the cross-model policy is not run
and there is nothing for a turn assessment to reselect.

The mechanics, all configurable and all recorded on the plan:

- **Upward** may act at the next eligible boundary, one rung, no waiting.
- **Downward** needs low-risk evidence and `downgrade_confirmations`
  consecutive eligible-turn recommendations for the lower rung. A turn that
  recommends something else breaks the run.
- **At most one change per user turn.** A tool continuation is part of the
  same turn and never gets a decision of its own.
- **A short "continue"** is not evidence for a downgrade. "yes, go on" says
  nothing about how hard the next step is.
- **A corrective follow-up can raise**, unless the optional shadow
  `failure_mode` answer says the obstacle is a missing credential or a missing
  fact. More reasoning is not the remedy for either.
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

## Compaction

Automatic compaction and truncation are refused for a session running the
experiment: `truncation: "auto"`, any `context_management`, `compaction`,
`auto_compaction`, `compaction_trigger` or `auto_truncation` field, and the
standalone `POST /v1/responses/compact` endpoint. All of them let the provider
rewrite a history the ledger has to be able to find again.

Use bounded sessions that do not need compaction. Adaptation also stops on its
own before `thresholds.context_safety_fraction` of the negotiated window: the
plan comes back `blocked`, the session carries on unchanged, and the ledger is
untouched. Supporting the provider's explicit compaction-trigger flow needs
its own fixtures and its own qualification; the router will not build a
compactor.

## The ledger

`turn_plans` is one row per turn plan. It holds hashes and identifiers: the
turn and plan ids, the sequence, from and to effort, the base effort, the
history anchor or parent response id, expected and confirmed effective effort,
the status, a request fingerprint and the recommendation. No transcript, no
message text, no reasoning text, no keys.

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
lineage flag is cleared and the session may plan again.

The same applies when the bounded SSE observer could not read a reply: lineage
is marked unknown, further transitions stop, and the reply itself is untouched
and never retried.

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
threshold rule and the policy with and without hysteresis, resampling by
session. Its fixtures are synthetic and labelled synthetic.

`qualify_effort.py` plans by default. Running it live needs `--confirm-live`,
`JEV_ROUTER_QUALIFY_LIVE=1`, an allowlisted profile, credentials and a call
budget. The allowlist is empty.

Compare against the fixed effort that achieves comparable quality, not only
the most expensive one. Counterbalance the order and keep the cache namespaces
apart. A cache hit is not guaranteed because the update syntax is correct, and
a valid protocol test is not a cache measurement.
