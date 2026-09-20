# Documentation

`README.md` at the top of the repository is the overview and the quick start.
This directory holds the parts that need more room.

| File | What it covers |
| --- | --- |
| [`SESSION_ROUTING.md`](SESSION_ROUTING.md) | Strict sessions: `POST /router/resolve`, the managed execution headers, the context budget, and all thirteen error codes. |
| [`SEMANTIC_POLICY.md`](SEMANTIC_POLICY.md) | What decides a binding: the semantic packet, shadow questions, quality lanes, quota windows, and replaying a decision from its own row. |
| [`ADAPTIVE_EFFORT.md`](ADAPTIVE_EFFORT.md) | The between-turn effort experiment: the three modes, the policy, `POST /router/turn-plan`, the ledger, compaction epochs, the Codex adapter. Off by default. |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Running it as a launchd service: upgrading, the migration, rollback, and turning strict sessions on. |
| [`R2_SPEC.md`](R2_SPEC.md) | The specification the three above were built from, including the C01-C34 and F01-F20 acceptance cases. Two owner decisions of 2026-09-19 override parts of it; both are marked in place. |
| [`qualification/`](qualification/) | One dated report per deployment that has been tested for between-turn effort, plus `TEMPLATE.md`. |

Related, elsewhere in the repository:

- [`../DESIGN.md`](../DESIGN.md) — why the router is shaped this way, and what was rejected.
- [`../TESTPLAN.md`](../TESTPLAN.md) — what is measured, how, and what the numbers cannot show. The C and F ids map to test files there.
- [`../POOL.md`](../POOL.md) — which models are in the pool and what admitted them.
- [`../evals/EXPERIMENTS.md`](../evals/EXPERIMENTS.md) — one entry per change tried, with before and after numbers.

## Where to start

- Routing `model: "auto"` and nothing else: the top-level `README.md` is all of
  it. Nothing in this directory is on that path.
- Building a coding harness that wants one model for a whole session:
  `SESSION_ROUTING.md`, then `SEMANTIC_POLICY.md` for what chose the model.
- Deploying it: `DEPLOYMENT.md`.
- Changing what Jev is asked: `SEMANTIC_POLICY.md`, then `../TESTPLAN.md`.

## Known issues

Open as of 2026-09-19. None of them lose data.

- **Fixed: two eval runs in the same second collided.** `evals/run_eval.py` now
  adds a numeric suffix when the result directory already exists.
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
  flow does work; see `ADAPTIVE_EFFORT.md`, "Compaction".
## Fixed

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
