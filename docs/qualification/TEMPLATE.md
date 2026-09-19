# Effort qualification report: <model> on <endpoint>

**Date:** YYYY-MM-DD
**Profile (model key in router.yaml):**
**Upstream id:**
**Endpoint / proxy:**
**Protocol:**
**Adapter revision (`compatibility_revision`):**
**Router commit:**
**Strategy under test:** `native_configuration_update` | `request_parameter`
**Verdict:** verified | not verified

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
| Q01 | The endpoint accepts the request at the base effort with no update item. | | |
| Q02 | A `configuration_update` item placed immediately before the new user message is accepted. | | |
| Q03 | The reply's `reasoning.effort` still reports the base effort, and the ledger is not overwritten by it. | | |
| Q04 | A full-history replay carrying every previous update at its original position is accepted. | | |
| Q05 | A `previous_response_id` chain that references the accepted parent and does not resend a persisted update is accepted. | | |
| Q06 | Resending an already-persisted update, or adding a second adjacent one, is rejected by the provider. | | |
| Q07 | The change is observable in the output: the higher effort produces measurably more reasoning work than the lower one on the same input. | | |
| Q08 | A tool continuation inside the same turn keeps the effective effort with no new item. | | |
| Q09 | `truncation: auto` and the standalone compaction endpoint are refused for this history, as documented. | | |
| Q10 | The terminal SSE status and usage are readable, including cached input tokens. | | |

## What "observable" means for Q07

State the evidence. Reasoning-token counts at each rung, with the denominators,
is the usual form. "The request succeeded" is not evidence that the effort
changed: an endpoint that silently drops an unknown item also returns 200.

## Cache behaviour

`cache_behavior` is a separate measurement and a separate claim.

| Rung sequence | Cached input tokens | Uncached input tokens | Notes |
|---|---:|---:|---|

**Verdict:** `verified_preserving` | `verified_breaking` | `unverified`

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
