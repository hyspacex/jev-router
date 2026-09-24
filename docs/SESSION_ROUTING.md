# Strict session routing

A legacy alias decides per request. It hashes the conversation, pins the model
for six hours, and may replace that model when the conversation stops fitting
it. That can suit a chat window, but it is not the shipped default:
`router.yaml` exposes one strict alias, `auto`.

A coding session wants the opposite. The harness has already sized its history
against one model's window, it holds a prompt cache, and a model that changes
halfway through a task is a bug rather than a saving. So a strict alias
resolves one execution profile once and keeps it:

- The model and the provider do not change after resolution. Nothing
  automatic replaces them: not an overflow, not a changed capability, not a
  429, not a timeout, not a classifier outage.
- The binding does not expire on the legacy pin TTL. It ends when you close
  it, when you prune it, or when a prepared contract is never used.
- A request that no longer fits is refused with a machine-readable reason.
  The router does not compact, widen the window or pick a bigger model.
  Compacting is the client's job, and a compaction the client asks for is
  passed through untouched; see
  [`ADAPTIVE_EFFORT.md`](ADAPTIVE_EFFORT.md), "Compaction".
- Effort is fixed by default. Changing it between turns is a separate,
  opt-in experiment that never changes the model; see
  [`ADAPTIVE_EFFORT.md`](ADAPTIVE_EFFORT.md). With it off, `effort_mode` is
  `fixed` and `adaptation` is `off`, which is what every session gets unless
  three separate opt-ins agree.

Legacy behaviour is untouched. An alias with no `session_mode` is legacy, and
legacy requests without `X-Router-Session` keep the legacy path. A strict
alias without a binding is refused; it never falls through to legacy routing.

This file is about the contract: one binding, resolved once, never moved.
What decides that binding — the semantic packet, the shadow questions, the
quality lanes that bound what quota may do, the coherent quota windows, and
replaying a decision from its own row — is in
[`SEMANTIC_POLICY.md`](SEMANTIC_POLICY.md).

## Turning it on

The shipped `auto` already enables this contract. The `auto-session` name in
this guide is an example for a custom configuration, not a shipped alias.
For a runnable example using `auto`, start with the [quick start](../README.md#quick-start).

```yaml
session_routing:
  enabled: true
  prepared_ttl_seconds: 600     # a resolved but unused contract expires
  max_inflight_per_session: 1   # a second concurrent request is a conflict
  require_control_token: true   # the only value there is; false is an error
  max_request_history: 200      # finished request ids kept per session

aliases:
  auto-session:
    session_mode: strict
    state_builder: continuation_aware_v1
    questions: [task, difficulty, harm_if_wrong]
    rules: policy
    allowed_models: [ollama/glm-5.3-flash, ollama/glm-5.3, gpt-6-astra, grok-4.6]
    admission_fallback: {route: frontier_medium}
```

`admission_fallback` is what admission binds when Jev is unreachable. Without
one, an admission with no classifier answer returns `NO_SAFE_ADMISSION` rather
than guessing. Whatever it binds is as fixed as any other binding: Jev coming
back does not revisit it.

What is recorded for such a binding is the fallback, not the heuristic guess
that was discarded on the way to it. `quality_lane` and the logged route are
the fallback's, `quota_changed_choice` is false because pressure was never
consulted, there is no counterfactual to draw, and the semantic answers are
stored as absent rather than synthetic. `decisions replay` re-runs the alias's
`admission_fallback` for such a row, which is the only thing about it that is
reproducible, and says so in a note.

A model may also describe its deployment:

```yaml
models:
  gpt-6-astra:
    protocol: openai-chat        # what this endpoint actually speaks
    max_output_tokens: 16384     # an operational cap, not the model's maximum
    compatibility_revision: chat-astra-proxy-v1
    effort_control:
      between_turn: fixed
      qualification: unverified
      cache_behavior: unverified
```

Configure what the deployment does, not what the model family supports
somewhere else. A suffix-style proxy path is not evidence of a native
Responses endpoint. `qualification: verified` is refused without a
`qualification_ref` naming a dated deployment test report.

## The resolve contract

Before the first execution request, the client asks for a profile.

```
POST /router/resolve
X-Router-Admin-Token: <router control credential>
```

```json
{
  "schema_version": "1",
  "intent": "new",
  "session_id": "session-2f766788",
  "request_id": "resolve-0001",
  "alias": "auto-session",
  "client": "coding-client",
  "candidate_models": ["ollama/glm-5.3-flash", "gpt-6-astra"],
  "client_contract": {
    "dynamic_metadata": true,
    "protocols": ["openai-chat"],
    "fresh_execution_context": true,
    "turn_boundary_reporting": false
  },
  "request": {
    "messages": [{"role": "user", "content": "Add quoted CSV fields and tests."}],
    "tools": []
  },
  "limits": {
    "max_output_tokens": 8192,
    "estimated_input_tokens": 4200,
    "estimate_method": "client-estimate-v1"
  }
}
```

The session id is yours to generate. Make it random and stable for the life of
the session. It must not be derived from message text, a system prompt or a
rotating upstream token.

The answer is one fixed profile:

```json
{
  "schema_version": "1",
  "session_id": "session-2f766788",
  "state": "prepared",
  "binding_revision": "sha256:...",
  "decision_id": "760392c45528",
  "execution": {
    "model_key": "ollama/glm-5.3-flash",
    "provider": "ollama",
    "wire_model": "ollama/glm-5.3-flash(none)",
    "protocol": "openai-chat",
    "context_window": 128000,
    "max_output_tokens": 8192,
    "supports_tools": true,
    "supports_vision": false,
    "initial_effort": "none",
    "effort_mode": "fixed"
  },
  "decision": {
    "rule": "easy",
    "source": "jev",
    "quality_lane": "fast",
    "quota_changed_choice": false,
    "quota_status": {"openai": "unknown", "ollama": "unknown", "xai": "unknown"}
  },
  "limits": {
    "estimated_input_tokens": 4200,
    "estimate_method": "router-serialized-v1",
    "reserve_tokens": 4096
  }
}
```

Configure your model object from `execution`, not from a response header. A
header on a request you have already built with different metadata is too
late.

`quota_status` reports one word per provider: `fresh`, `stale`, `unknown` or
`error`. Unknown means nobody measured it. It never means unused capacity.

`binding_revision` covers the profile identity and the negotiated limits:
model key, provider, upstream id, protocol, context window, output ceiling,
tool and vision support, and the compatibility revision. It deliberately
leaves out quota, timestamps and effort, so nothing that is allowed to change
can invalidate an acknowledgement.

### Retries, resume and conflicts

- The same `intent: new` payload under the same session id returns the stored
  contract and spends no second classifier call. The `request_id` is not part
  of that comparison, so a retry with a new id is still a retry.
- A different payload under the same session id is `SESSION_CONFLICT` (409).
- `intent: resume` returns the stored contract with no classifier call at all.
  Send the session id, the client and a request id; leave out the task.
- An unknown id is `SESSION_UNKNOWN` (409). A closed, expired or pruned id is
  `SESSION_CLOSED` (410). Neither ever silently becomes a new session.

### Freshness

`intent: new` means new independent work. The router rejects the evidence it
can see against that: supplied `assistant` or `tool` messages, a
`previous_response_id`, or `fresh_execution_context: false`. It cannot prove
the rest. A fork or an import that carries history is a continuation, and
sending it as new work is a client bug the router will not always catch.

## Executing

Send ordinary chat-completions requests with three extra headers:

```
POST /v1/chat/completions
X-Router-Admin-Token: <router control credential>
X-Router-Session: session-2f766788
X-Router-Binding: sha256:...
X-Router-Request-Id: execution-0001
```

The `model` field must be the profile the binding named: the `wire_model`, the
`model_key`, the bare upstream id, or the static envelope's descriptor when
the alias declares one. Anything else is a conflict.

All four headers are read by the router and stripped before forwarding. The
body is forwarded with the `model` field normalised onto the bound profile and
nothing else touched: system text, tools, historic messages and any other
field arrive exactly as you sent them. The response body is never modified.

What happens around the request:

- The first accepted 2xx headers move the session from `prepared` to `active`.
  A rejected first inference leaves the prepared contract exactly as it was
  disclosed.
- There is one entry in the execution plan. A 429, a 5xx, a refused connection
  or a timeout is reported to you on the bound profile. Configured fallback
  lists do not apply to a strict session.
- One request per session is in flight. A second concurrent one is
  `SESSION_CONFLICT`, not a queue.
- Acceptance, transport completion and a later stream failure are three
  separate facts, recorded separately. HTTP 200 is not task success.

### Request ids

A request id prevents an accidental duplicate. It is not exactly-once
delivery, and the router never calls the provider again on your behalf.

| Prior outcome | What a repeat gets |
|---|---|
| Completed | `REQUEST_ALREADY_COMPLETED`. No response is stored to replay. |
| Definitively rejected before acceptance | Retried normally. |
| Accepted, stream failed, in flight, or unknown | `EXECUTION_OUTCOME_UNKNOWN`. Reconcile it, or start a new attempt with a new id. The message names the reconcile command. |
| Same id, different body | `SESSION_CONFLICT`. |

Records whose outcome nobody can state are never dropped by the bounded
history, so a forgotten id is never read as proof that it did not execute.

## Context limits

Admission requires `I + O + S <= C`:

- `C` is the negotiated context window: the smallest of the deployment's
  window, the static envelope's, and any smaller window you asked for.
- `O` is the negotiated output allowance: the smallest of the deployment's
  ceiling, the envelope's and yours, falling back to your `max_tokens` and
  then to 4096.
- `I` is the estimated serialized input. The router counts instructions,
  message text, tool-call arguments, tool schemas and per-message framing,
  charges CJK characters at about one token each instead of a quarter, and
  allows for each image. Your `estimated_input_tokens` may raise that number
  and never lowers it.
- `S` is an uncertainty reserve, `max(4096, ceil(0.10 * I))`, or more if you
  ask for more.

These are engineering heuristics, not a measured error bound. `estimate_method`
in the response says which one produced the number.

When part of the input has no knowable size — an image, or a history the
provider holds behind a `previous_response_id` with no reported total — the
estimate is marked uncertain and admission also requires five per cent of
headroom. Send the accumulated usage figure if you have one; a short delta is
not the context.

On execution the router supplies that figure itself where it can. It keeps
the input and output token counts the last reply reported for the session —
two numbers, nothing else — from the Responses observer and from a
non-streamed chat reply's own `usage` block. A `previous_response_id` request
is budgeted against their sum plus the new delta.

With no such figure the size stays unknown rather than being read as zero.
The uncertainty reserve is then taken against the whole negotiated window
instead of against the delta, so the near-boundary rule has the window to work
against, and the estimate is still marked uncertain.

On execution, the same `O` is revalidated against what the request itself asks
for in `max_tokens`, `max_completion_tokens` or `max_output_tokens`. Above the
negotiated ceiling is `CONTEXT_BUDGET_EXCEEDED`, naming both numbers, and the
binding is kept: advertised limits and effective limits agree or it is an
error, never a hidden substitution (I06). The router does not rewrite your
body to make it fit. At or below the ceiling the request is forwarded exactly
as it arrived, and the budget still reserves the larger of the two figures,
because the negotiated ceiling is reserved for this session whether or not
this request uses all of it.

Growth is your side of the contract. The harness compacts against the window
it was given. When a request no longer fits, the router answers
`CONTEXT_BUDGET_EXCEEDED` and keeps the binding; it does not widen the window
or move to a larger model. A later config change that raises the window does
not enlarge an established session either.

## Failure semantics

Every error is a structured body with a stable machine code:

```json
{"error": {"type": "session_error", "code": "CONTEXT_BUDGET_EXCEEDED", "message": "..."}}
```

| Code | HTTP | When |
|---|---:|---|
| `INVALID_ROUTER_INPUT` | 400 | Bad schema, unknown alias, carried history, missing acknowledgement headers. |
| `ROUTER_UNAUTHORIZED` | 401 | Missing or wrong router control credential. |
| `SESSION_CONFLICT` | 409 | Conflicting resolve payload, wrong binding, wrong model, a concurrent execution, or a reused request id with a different body. |
| `SESSION_UNKNOWN` | 409 | The session id was never resolved here, or belongs to another owner. |
| `SESSION_CLOSED` | 410 | Closed, pruned, or a prepared contract that expired. |
| `PROFILE_CHANGED` | 409 | The profile was revoked or its configured limits fell below the contract. The session is `blocked` and the binding is kept. |
| `NO_SAFE_ADMISSION` | 422 | No qualified candidate, or no classifier answer and no configured fallback. |
| `PROVIDER_UNAVAILABLE` | 503 | A fresh quota reading says the chosen model's provider is spent. Carries `window`, `resets_at` and `retry_after_seconds`. Nothing is bound. |
| `CONTEXT_BUDGET_EXCEEDED` | 422 | `I + O + S` does not fit `C`, or the request asks for more output than the negotiated ceiling. |
| `UNSUPPORTED_PROFILE` | 422 | Unsupported protocol or endpoint, tools or images the profile cannot take, or the session service switched off. |
| `REQUEST_ALREADY_COMPLETED` | 409 | A completed request id was sent again. |
| `EXECUTION_OUTCOME_UNKNOWN` | 409 | The previous attempt at that id cannot be called settled. |
| `TURN_NOT_SETTLED` | 409 | An effort plan was asked for while the previous turn was still running. The message says what the router is holding and, when that is an update nobody can account for, the command that settles it. |
| `EFFORT_HISTORY_MISMATCH` | 409 | A required effort update was dropped, moved, duplicated, or the base setting was changed. |

None of these is ever converted into a cheaper request. An authorization, a
context or a protocol error is reported and stops there.

A `blocked` session keeps its binding so you can see what it was. Reconfigure
deliberately, compact, or resolve a new session.

## The trusted-client boundary

The router is an HTTP shim. There are assertions it cannot check, and it says
so rather than pretending:

- **Session identity.** You generate the id and decide which work belongs to
  it. The router cannot tell a genuinely new task from a fork of an old one
  beyond the history evidence described above.
- **Freshness.** `fresh_execution_context` is your word.
- **The transcript.** You own it. The router never rewrites, compacts,
  summarises or reorders it, and never injects routing explanations into a
  prompt.
- **Ownership.** A session is scoped to the owner of the router control
  credential and to the configured client identity. Everyone holding that
  credential is the same owner. This is a single-owner local tool; it does not
  claim tenant isolation between people who share the credential.
- **Upstream credentials.** They are yours, they are forwarded untouched, and
  they are never written to the database, a fingerprint, a log or the Jev
  state. Refreshing an upstream token within the same configured account
  changes nothing about a session.

Strict control and execution requests require the router credential even on
loopback, where the read-only router endpoints do not. Set
`JEV_ROUTER_ADMIN_TOKEN` before using any of this.

There is no setting that turns that off. `require_control_token` stays in the
file so a config that already says `true` keeps loading, and `false` is a
config error with a message saying so: a strict session is scoped to the owner
of that credential, so there has to be one to scope it to. A deployment that
does not want strict sessions sets `session_routing.enabled: false`.

Nothing recoverable is stored. Request fingerprints are sha256 hashes, the
owner is a sha256 hash, and no message text, prompt or key is written.

## A minimal client

```bash
export TOKEN=$JEV_ROUTER_ADMIN_TOKEN
SESSION=session-$(openssl rand -hex 4)

BINDING=$(curl -s localhost:8318/router/resolve \
  -H "X-Router-Admin-Token: $TOKEN" -H 'content-type: application/json' \
  -d "{\"schema_version\":\"1\",\"intent\":\"new\",\"session_id\":\"$SESSION\",
       \"request_id\":\"resolve-0001\",\"alias\":\"auto-session\",\"client\":\"cli\",
       \"request\":{\"messages\":[{\"role\":\"user\",\"content\":\"Add quoted CSV fields.\"}]}}" \
  | tee /dev/stderr | python3 -c 'import json,sys; print(json.load(sys.stdin)["binding_revision"])')

curl -s localhost:8318/v1/chat/completions \
  -H "X-Router-Admin-Token: $TOKEN" \
  -H "X-Router-Session: $SESSION" \
  -H "X-Router-Binding: $BINDING" \
  -H "X-Router-Request-Id: execution-0001" \
  -H 'content-type: application/json' \
  -d '{"model":"ollama/glm-5.3-flash(none)","messages":[{"role":"user","content":"Add quoted CSV fields."}]}'
```

The same thing in Python, keeping the whole contract the router returned:

```python
import os
import secrets

import httpx

ROUTER = "http://127.0.0.1:8318"
CONTROL = {"X-Router-Admin-Token": os.environ["JEV_ROUTER_ADMIN_TOKEN"]}
session_id = "session-" + secrets.token_hex(4)
first = {"messages": [{"role": "user", "content": "Add quoted CSV fields and tests."}]}

with httpx.Client(base_url=ROUTER, timeout=60) as http:
    contract = http.post(
        "/router/resolve",
        headers=CONTROL,
        json={
            "schema_version": "1",
            "intent": "new",
            "session_id": session_id,
            "request_id": "resolve-0001",
            "alias": "auto-session",
            "client": "my-client",
            "request": first,
            "limits": {"max_output_tokens": 4096},
        },
    ).raise_for_status().json()

    profile = contract["execution"]
    # Size your history against profile["context_window"] from here on.
    counter = 0

    def send(body):
        global counter
        counter += 1
        return http.post(
            "/v1/chat/completions",
            headers={
                **CONTROL,
                "X-Router-Session": session_id,
                "X-Router-Binding": contract["binding_revision"],
                "X-Router-Request-Id": f"execution-{counter:04d}",
            },
            json={**body, "model": profile["wire_model"]},
        )

    reply = send(first)
    if reply.status_code != 200:
        print(reply.json()["error"]["code"], reply.json()["error"]["message"])
```

After a restart, call `/router/resolve` again with `"intent": "resume"` and the
same session id. You get the same profile back and no classifier call.

## Diagnostics

```sh
uv run jev-router sessions list
uv run jev-router sessions show session-2f766788
uv run jev-router sessions close session-2f766788
uv run jev-router decisions -n 20            # admission and execution events
uv run jev-router decisions replay <id>      # re-run the selection from the row
uv run jev-router prune --older-than-days 30 # sessions leave tombstones
```

The same views are available over HTTP at `GET /router/sessions`,
`GET /router/sessions/{id}` and `POST /router/sessions/{id}/close`, all
requiring the control credential.

`sessions show` prints the model identity, the negotiated window and output
ceiling, base and effective effort, the binding state, the rule and reason
behind the choice, the quota freshness at admission and now, and the
adaptation mode. It distinguishes "model unchanged" from "model changed", and
none of it is ever injected into a prompt.

## Beside this contract

- **Between-turn effort changes.** `POST /router/turn-plan`, the `turn_plans`
  ledger and the `effort_control` block are an opt-in experiment that changes
  the effort of the bound model and nothing else. It is off in the shipped
  configuration, which declares no qualified profile, and whether it is worth
  doing has not been measured. One deployment has a provisional protocol test.
  See [`ADAPTIVE_EFFORT.md`](ADAPTIVE_EFFORT.md).
- **Compaction.** A compaction the client asks for is forwarded as a
  maintenance request of the same session: same binding, no admission, no turn
  plan. It opens a compaction epoch, which is what the effort ledger checks a
  later replay against. The router builds no compactor of its own.
- **Strict forwarding over the Responses protocol.** A managed request on
  `/v1/responses` executes against a binding whose profile really speaks that
  protocol. A mismatch either way is `UNSUPPORTED_PROFILE`; the router never
  converts one format into the other. Non-session passthrough is unchanged.

## Not in this release

- Format conversion of any kind, a compactor, a cooldown daemon, a learner, or
  a WebSocket transport.
- Multi-process deployment. One service process per database: a second strict
  service opened on the same file in one process is refused, because their
  in-flight claims would not see each other.
