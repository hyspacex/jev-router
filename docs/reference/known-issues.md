# Known issues and fixes

[Documentation index](../README.md)

These are dated observations, not a fresh deployment qualification.

## Known issues

Open as of 2026-09-19. None of them lose data.

- **`sessions show` prints `mode=fixed` for an adaptive session.** The
  `effort_mode` column is written as `fixed` at resolve and never updated, so
  an adaptive session prints `mode=fixed  adaptation=active` on one line. The
  `adaptation` word on the same line is the one that is right; the efforts
  printed above it are right too.
- **Codex logs a model-list error against the adapter.** `codex` refreshes
  `GET /v1/models` and expects a Codex-shaped body; the router answers with an
  OpenAI-shaped one, so it logs `failed to refresh available models` once per
  session. It is noise. The conversation works.
- **`POST /v1/responses/compact` answers 404 on CLIProxyAPI.** The router
  forwards it as a maintenance request of the bound session, but the proxy
  tested on 2026-09-19 does not implement the endpoint. Codex never calls it,
  so nothing depends on it today. The provider's inline `compaction_trigger`
  flow does work; see [Adaptive effort](../ADAPTIVE_EFFORT.md), "Compaction".

## Fixed

- **Two eval runs in the same second collided.** `evals/run_eval.py` now
  adds a numeric suffix when the result directory already exists.
- **A held session claim after a mid-stream disconnect.** Fixed on
  2026-09-19. A client that walked away while a long reply was waiting for the
  socket left the body generator suspended at its `yield`, so nothing recorded
  the disconnect, closed the upstream response or released the session's
  in-flight claim. Every later request on that session was a
  `SESSION_CONFLICT` and every turn boundary a `TURN_NOT_SETTLED` until the
  garbage collector got round to it. A reply is now finalized on the way out
  of the ASGI call whatever ended it, with a backstop for a generator that
  never started. Two related things went with it: a stream the upstream closed
  cleanly without saying how the response ended now reads `unknown` rather than
  `completed`, so a repeat of that request id asks for a reconcile instead of
  claiming the work was done; and `TURN_NOT_SETTLED` and
  `EXECUTION_OUTCOME_UNKNOWN` now name the exact `jev-router sessions
  reconcile` command that would settle them. `tests/test_broken_stream.py`
  holds all of it.
