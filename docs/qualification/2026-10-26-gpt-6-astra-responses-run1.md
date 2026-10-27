# Effort qualification report: first sitting, superseded

Superseded the same day by `2026-10-26-gpt-6-astra-responses.md`. Q07 here used
four well-known puzzles and every arm, request-level `high` included, spent 7
to 32 reasoning tokens: answered from memory, so its pass is void. Kept as it
was written.

**Date:** 2026-10-26
**Profile (model key in router.yaml):** gpt-6-astra-responses
**Upstream id:** gpt-6-astra
**Endpoint / proxy:** http://127.0.0.1:18330
**Protocol:** openai-responses
**Adapter revision (`compatibility_revision`):** responses-astra-cliproxyapi-v1
**Router commit:**
**Strategy under test:** `native_configuration_update` | `request_parameter`
**Verdict:** not verified

A model name is not a qualification. This report is about one model behind one
endpoint through one adapter revision. Change any of the three and it has to be
run again. Copy this file to `docs/qualification/YYYY-MM-DD-<profile>.md`, fill
it in, check it in, and name it in `effort_control.qualification_ref`.

## What was run

    uv run python evals/qualify_effort.py \
      --profile <model key> --confirm-live --max-calls <n> --report <path>

**Credential used:** which env var, which account. Never the value.
**Calls made:** **Wall clock:** **Spend:**

## Checks

Fill in `pass`, `fail` or `not run`, and say what was observed. A blank is not
a pass.

| ID | Check | Result | Notes |
|---|---|---|---|
| Q01 | The endpoint accepts the request at the base effort with no update item. | pass | HTTP 200, terminal `response.completed`, reported effort `low` |
| Q02 | A `configuration_update` item placed immediately before the new user message is accepted. | pass | update: HTTP 200 `response.completed`; control with an invented item type: HTTP 400 {"code": "invalid_value", "message": "Invalid value: 'no_such_item_type'. Supported values are: 'additional_tools', 'agent_message', 'apply_patch_call', 'apply_ |
| Q03 | The reply's `reasoning.effort` still reports the base effort, and the ledger is not overwritten by it. | pass | plan low -> high; the reply reported `low`; ledger base `low`, confirmed `high`, effective `high` |
| Q04 | A full-history replay carrying every previous update at its original position is accepted. | pass | turns: HTTP 200/200/200; the update replayed at index [2] on turn 3 |
| Q05 | A `previous_response_id` chain that references the accepted parent and does not resend a persisted update is accepted. | pass | `store: true` parent HTTP 200; chained child HTTP 200  |
| Q06 | Resending an already-persisted update, or adding a second adjacent one, is rejected by the provider. | fail | adjacent updates: HTTP 200 ; resending the persisted update in the chain: HTTP 200  |
| Q07 | The change is observable in the output: the higher effort produces measurably more reasoning work than the lower one on the same input. | pass | update minus base: +2.8 reasoning tokens [+1.0, +4.8], n=12 pairs; update minus request-level high: +0.2 [-1.8, +2.5] |
| Q08 | A tool continuation inside the same turn keeps the effective effort with no new item. | pass | continuation HTTP 200; plans 3 -> 3; effective effort `high`; the two requests logged under ['turn-0003', 'turn-0003'] |
| Q09 | `truncation: auto` and the standalone compaction endpoint are refused for this history, as documented. | pass | `truncation`: HTTP 422 UNSUPPORTED_PROFILE; `context_management`: HTTP 422 UNSUPPORTED_PROFILE. The standalone compaction endpoint is forwarded as maintenance since the owner decision of 2026-09-19 and is not refused |
| Q10 | The terminal SSE status and usage are readable, including cached input tokens. | pass | 47 of 47 streamed replies carried a terminal status with reasoning and cached token counts |

## What "observable" means for Q07

State the evidence. Reasoning-token counts at each rung, with the denominators,
is the usual form. "The request succeeded" is not evidence that the effort
changed: an endpoint that silently drops an unknown item also returns 200.

## Cache behaviour

`cache_behavior` is a separate measurement and a separate claim.

| Rung sequence | Cached input tokens | Uncached input tokens | Notes |
|---|---:|---:|---|

**Verdict:** not verified

A `request_parameter` strategy is never reported as native cache preserving,
whatever this table shows. Record what it did and label it as the laboratory
baseline it is.

## Limitations

Say plainly what this run does not establish. At minimum:

- Sessions long enough to need compaction were not tested unless a row above
  says they were. Short-session qualification does not authorise long-session
  behaviour.
- One account, one region, one day. A provider may change the endpoint under
  the same model name.
- Sample size and repeats, with the interval. A no-failure pilot of a handful
  of calls does not establish noninferiority.

## Rollback

How the experiment was switched off again, and what was observed afterwards:
the effective effort and the ledger stay, prior update positions still
validate, and nothing resets.
