# Effort qualification report: gpt-6-astra-responses on CLIProxyAPI

**Date:** 2026-10-26
**Profile (model key):** `gpt-6-astra-responses`, as `examples/make-adaptive-astra.py`
declares it. `router.yaml` itself declares nothing verified and keeps the
experiment off; the profile is verified only in the deployment config that
names this file.
**Upstream id:** `gpt-6-astra`
**Endpoint / proxy:** CLIProxyAPI on the owner's Mac mini, `http://100.82.7.89:8317`,
`POST /v1/responses`, probed directly; the router half ran through a local
router on `http://127.0.0.1:18330` with that proxy as its upstream.
**Protocol:** `openai-responses`
**Adapter revision (`compatibility_revision`):** `responses-astra-cliproxyapi-v1`
**Client:** the Codex adapter (`src/jev_router/integrations/codex/adapter.py`),
run in-process by the script.
**Router commit:** `68384e2`, plus the uncommitted live caller in
`evals/qualify_effort.py` that this report was produced with and that is
committed alongside it.
**Strategy under test:** `native_configuration_update`
**Verdict:** **verified by owner decision, 2026-10-26, with one failed check.**
Q06 failed at the provider and holds only because the router refuses both
cases; Q07 passed on a small effect. Read "Verdict" below before relying on it.

A model name is not a qualification. This report is about one model behind one
endpoint through one adapter revision. Change any of the three and it has to be
run again.

## What was run

    JEV_ROUTER_QUALIFY_LIVE=1 JEV_ROUTER_QUALIFY_ALLOW=gpt-6-astra-responses \
      uv run python evals/qualify_effort.py -c <make-adaptive-astra config> \
        --profile gpt-6-astra-responses --router http://127.0.0.1:18330 \
        --upstream http://100.82.7.89:8317 --confirm-live \
        --max-calls 60 --max-seconds 1500 --report <this file>

This is the first time `qualify_effort.py` has run live; its live caller was
written for this sitting. The provider's own behaviour (Q01, Q02, Q04-Q07,
Q10) is probed straight at the proxy with no router in the path. The router's
half (Q03, Q08, Q09 and the router side of Q06) goes through the real Codex
adapter against a router serving the profile.

**Credential used:** the proxy key, read at run time from a private file into
`UPSTREAM_API_KEY`; a throwaway router control token; `TYPESAFE_API_KEY` for the
classifier calls, which are not model calls. Never printed, logged or stored.
**Calls made:** 54 against a cap of 60 (Q07 is 36 of them). **Wall clock:**
160 s. **Spend:** subscription quota on the owner's account.

**Two sittings.** A first sitting the same day (kept as
`2026-10-26-gpt-6-astra-responses-run1.md`) asked Q07 on four well-known
puzzles. Every arm, request-level `high` included, used 7 to 32 reasoning
tokens: the answers came from memory, so the test could not tell efforts apart
and its nominal pass meant nothing. Q07 was redesigned with problems whose
numbers are drawn from a seed (`20261026`) and the whole run was repeated. Its
other results agreed with this one on every check.

## Checks

| ID | Check | Result | Notes |
|---|---|---|---|
| Q01 | The endpoint accepts the request at the base effort with no update item. | pass | HTTP 200, terminal `response.completed`, reported effort `low` |
| Q02 | A `configuration_update` item placed immediately before the new user message is accepted. | pass | update: HTTP 200 `response.completed`; control with an invented item type: HTTP 400 {"code": "invalid_value", "message": "Invalid value: 'no_such_item_type'. Supported values are: 'additional_tools', 'agent_message', 'apply_patch_call', 'apply_ |
| Q03 | The reply's `reasoning.effort` still reports the base effort, and the ledger is not overwritten by it. | pass | plan low -> high; the reply reported `low`; ledger base `low`, confirmed `high`, effective `high` |
| Q04 | A full-history replay carrying every previous update at its original position is accepted. | pass | turns: HTTP 200/200/200; the update replayed at index [2] on turn 3 |
| Q05 | A `previous_response_id` chain that references the accepted parent and does not resend a persisted update is accepted. | pass | `store: true` parent HTTP 200; chained child HTTP 200  |
| Q06 | Resending an already-persisted update, or adding a second adjacent one, is rejected by the provider. | fail | adjacent updates: HTTP 200 ; resending the persisted update in the chain: HTTP 200 ; through the router: an update no plan gave: HTTP 409 EFFORT_HISTORY_MISMATCH; a second copy beside a recorded update: HTTP 409 EFFORT_HISTORY_MISMATCH |
| Q07 | The change is observable in the output: the higher effort produces measurably more reasoning work than the lower one on the same input. | pass | update minus base: +5.4 reasoning tokens [+0.2, +10.6], n=12 pairs; update minus request-level high: -2.2 [-9.8, +3.3] |
| Q08 | A tool continuation inside the same turn keeps the effective effort with no new item. | pass | continuation HTTP 200; plans 3 -> 3; effective effort `high`; the two requests logged under ['turn-0003', 'turn-0003'] |
| Q09 | `truncation: auto` and the standalone compaction endpoint are refused for this history, as documented. | pass | `truncation`: HTTP 422 UNSUPPORTED_PROFILE; `context_management`: HTTP 422 UNSUPPORTED_PROFILE. The standalone compaction endpoint is forwarded as maintenance since the owner decision of 2026-09-19 and is not refused |
| Q10 | The terminal SSE status and usage are readable, including cached input tokens. | pass | 47 of 47 streamed replies carried a terminal status with reasoning and cached token counts |

## What "observable" means for Q07

Reasoning tokens, straight at the proxy, on four generated problems (inclusion
and exclusion count, modular power, a linear Diophantine count, a digit sum of
a product). Three arms on the same input: base `low` with no item; base `low`
with a `configuration_update` to `high` immediately before the user message;
and request-level `high`. Three repeats, arm order rotated per problem and
repeat, a fresh instruction prefix on every call so no call can reuse
another's cache. Pairs are (problem, repeat); intervals are 95% percentile
bootstrap over pairs.

| Problem | base `low` | `low` + update to `high` | request-level `high` |
|---|---|---|---|
| 1 | 28, 30, 25 | 37, 35, 40 | 41, 31, 36 |
| 2 | 30, 29, 30 | 31, 28, 33 | 31, 65, 29 |
| 3 | 58, 37, 56 | 56, 47, 41 | 50, 54, 47 |
| 4 | 52, 47, 56 | 65, 70, 60 | 60, 59, 67 |

- Update minus base: **+5.4** reasoning tokens, [+0.2, +10.6], n = 12 pairs.
- Update minus request-level `high`: **-2.2**, [-9.8, +3.3], n = 12 pairs.

Read it as: the update does what request-level `high` does, and the interval
says it is not nothing. It also says how little `high` changes on short
problems for this model: a handful of tokens. The interval only just excludes
zero, on one seed and one day. Whether a turn that needs more thought gets
better answers at `high` is a different question, and this report does not
answer it.

## Verdict

The owner decided on 2026-10-26 to run the experiment in `active` mode on the
mini on this report. What that decision accepts:

- **Q06 failed at the provider.** On 2026-09-19 the provider refused two
  adjacent updates with HTTP 400 ("Consecutive 'configuration_update' items are
  not allowed."). Today it accepts them, and it accepts resending a persisted
  update in a `previous_response_id` chain, both with HTTP 200. Which effort
  wins in either case is unknown. The router refuses both before forwarding
  (HTTP 409 `EFFORT_HISTORY_MISMATCH`, measured here through the deployed code),
  and it never forwards an update no plan gave. The protection is therefore
  the router's alone: a router defect in that check would reach the provider
  unrefused.
- **Q07 is a small effect** with an interval that only just excludes zero.

Nothing else is claimed. `cache_behavior` stays `unverified`.

## Cache behaviour

**Not measured.** No arm was designed to produce or detect a cache hit, and
every Q07 call carried a fresh prefix on purpose. `cache_behavior` stays
`unverified`.

## Limitations

- Short sessions only: no compaction, nothing near the context limit.
- One account, one proxy, one day. The provider changed its handling of
  adjacent updates between 2026-09-19 and today without any change on this
  side, which is itself a reason to rerun this before trusting it again.
- Q07 rests on 12 pairs from one seed. Q03 and Q08 were each observed once.
- The router half ran on a local router with the mini's proxy as upstream,
  not on the mini's own router process. Same commit, same profile, same
  upstream.

## Rollback

Set `experiments.adaptive_effort.mode` back to `off` (or `shadow`) in the
deployment config and restart. Existing sessions keep their effective effort
and their ledger; new turns stop planning.
