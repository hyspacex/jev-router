# Effort qualification report: gpt-6-astra on CLIProxyAPI (Responses)

**Date:** 2026-09-19
**Profile (model key):** `gpt-6-astra-responses`, in the test config built by
`examples/make-adaptive-astra.py`. `router.yaml` itself declares nothing
verified and keeps the experiment off.
**Upstream id:** `gpt-6-astra`
**Endpoint / proxy:** CLIProxyAPI on the owner's Mac mini,
`http://100.82.7.89:8317`, `POST /v1/responses`
**Protocol:** `openai-responses`
**Adapter revision (`compatibility_revision`):** `responses-astra-cliproxyapi-v1`
**Router commit:** the `r2-session-routing` branch, at the commit that adds
`src/jev_router/adapter.py`
**Client:** `codex-cli 0.155.1`, through `src/jev_router/adapter.py`
**Strategy under test:** `native_configuration_update`
**Verdict:** **provisional.** Owner-authorised for short personal test
sessions on 2026-09-19. This is **not** the promotion gate of spec 13.7. It
does not authorise the shipped configuration, another account, another client,
another day, or a session long enough to need compaction.

A model name is not a qualification. This report is about one model behind one
endpoint through one adapter revision, driven by one client.

## What was run

`evals/qualify_effort.py` was **not** run: its allowlist is empty and running
it live needs `--confirm-live` and `JEV_ROUTER_QUALIFY_LIVE=1`, neither of
which this test had. This is a hand test written up against the same template,
in two parts.

**1. Direct probes at the proxy**, with no router in the path, to establish
what the endpoint itself does.

**2. A real five-turn Codex conversation** through a local router instance and
the new client adapter:

    uv run python examples/make-adaptive-astra.py --out <cfg> --sqlite <db> --port 18318
    JEV_ROUTER_UPSTREAM=http://100.82.7.89:8317 \
      JEV_ROUTER_ADMIN_TOKEN=<throwaway> uv run jev-router -c <cfg> serve --port 18318
    JEV_ROUTER_ADMIN_TOKEN=<throwaway> uv run jev-router adapter \
      --router http://127.0.0.1:18318 --alias astra-adaptive --port 18320 \
      --upstream-key-file <key file>
    codex exec        ... "<turn 1>"
    codex exec resume --last ... "<turn 2>"   # and so on

The full commands are in `docs/ADAPTIVE_EFFORT.md`, "Trying it with Codex".

**Credential used:** the proxy key, read at run time from a file outside the
repository and given to the adapter with `--upstream-key-file`. Never printed,
logged, stored or committed. `TYPESAFE_API_KEY` from the environment, for the
classifier calls, which are not model calls and are not in the budget below.

**Calls made:** 14 upstream model calls against a budget of 40 — 6 through the
adapter (five turns, one of which had a tool continuation) and 8 direct probes.
**Wall clock:** about 20 minutes of running. **Spend:** subscription quota.

## Checks

`pass`, `fail` or `not run`. A blank is not a pass.

| ID | Check | Result | Notes |
|---|---|---|---|
| Q01 | The endpoint accepts the request at the base effort with no update item. | pass | 200, `status: completed`, on every direct probe and on every turn of the live run. |
| Q02 | A `configuration_update` item placed immediately before the new user message is accepted. | pass | 200 directly and through the router. The control matters and was run: an item of an invented type is rejected with 400, and the error names the item types the endpoint knows, so this proxy forwards items to the provider rather than dropping what it does not recognise. A 200 on Q02 therefore means the provider saw the item. |
| Q03 | The reply's `reasoning.effort` still reports the base effort, and the ledger is not overwritten by it. | pass | With base `low` and an update to `high`, the reply reports `low`. The router reads its effective effort from the ledger; `sessions show` reported `effective=medium` while every reply said `low`. |
| Q04 | A full-history replay carrying every previous update at its original position is accepted. | pass | Turn 3 replayed one update at index 8; turn 5 replayed two, at indices 8 and 14. Both accepted by the router's own history check (prefix hashes matched) and by the provider. |
| Q05 | A `previous_response_id` chain that references the accepted parent and does not resend a persisted update is accepted. | not run | Codex 0.155.1 sends `store: false` and the whole input every time, so no chain was ever produced. Untested on this path. |
| Q06 | Resending an already-persisted update, or adding a second adjacent one, is rejected by the provider. | partial pass | Two adjacent updates are rejected: HTTP 400, `"Consecutive 'configuration_update' items are not allowed."` The resend half needs a chain and so was **not run**. |
| Q07 | The change is observable in the output: the higher effort produces measurably more reasoning work than the lower one on the same input. | **weak pass, n=2 per arm, twice** | See below. Suggestive, not proof. |
| Q08 | A tool continuation inside the same turn keeps the effective effort with no new item. | pass | Turn 5 ran a shell tool. The continuation reused `turn-0005`, carried no plan header and no fresh item, replayed both earlier updates, and was accepted. The ledger did not move. Observed once. |
| Q09 | `truncation: auto` and the standalone compaction endpoint are refused for this history, as documented. | pass, router side only | The router refuses both. Codex sends neither field, so the provider's own behaviour was never exercised. Codex does send an `x-codex-beta-features: remote_compaction_v2` header, which the adapter drops; that is a precaution, not a measurement. |
| Q10 | The terminal SSE status and usage are readable, including cached input tokens. | pass | `response.completed` with `usage.output_tokens_details.reasoning_tokens` and `usage.input_tokens_details.cached_tokens` present and parsed by the bounded observer on all 6 live calls. |

## What "observable" means for Q07

Reasoning-token counts on one short reasoning puzzle, same input in all three
arms, straight at the proxy. Two independent runs on the same day, each n=2
per arm. The first was the owner's, on a different puzzle; the second was run
for this report.

| Arm | Owner's run (different puzzle) | This report's run |
|---|---|---|
| base `low`, no update | 78, 76 | 41, 40 |
| base `low` + update to `high` | 92, 99 | 60, 69 |
| request-level `high` | 112 (n=1) | 69, 62 |

In both runs the update arm is above the base arm on every sample and no
sample overlaps. That is the evidence, and it is thin: four samples of the
update arm in total, no variance estimate, no interval, no counterbalancing of
order, and two different prompts. It is consistent with the update taking
effect and also consistent with a large enough run-to-run spread. Whether the
update reaches the same effective effort as a request-level `high` is
genuinely unclear: the owner's run put it below (92–99 against 112), this one
put it level (60–69 against 62–69).

This is the weakest row in the table and it is the row the whole experiment
rests on. A promotion needs a real design: counterbalanced order, separate
cache namespaces, enough samples for an interval, and the comparison made
against the fixed effort that achieves comparable quality rather than against
the most expensive one.

## Cache behaviour

**Not measured, and `cache_behavior` stays `unverified`.** No arm of any run
was designed to produce or to detect a cache hit.

Cached input tokens were read off the live run, so they are recorded here as
observations. They are not a cache measurement and must not be cited as one.

| Turn | What it carried | Input tokens | Cached input tokens |
|---|---|---:|---:|
| 1 | no update | 19260 | 0 |
| 2 | new update at index 8 | 19351 | 19072 |
| 3 | one update replayed at index 8 | 19889 | 19200 |
| 4 | new update at index 14, one replayed | 19914 | 19712 |
| 5 | two updates replayed | 19940 | 19840 |
| 5 (tool continuation) | two updates replayed | 20045 | 19840 |

What this does not establish: every update in this run sat near the end of a
long, stable prefix, which is the easiest possible case. Nothing here says
what happens when an update lands early in a history, what it costs across a
compaction, or whether the hit rate differs from a fixed-effort session. That
needs its own design, its own cache namespaces and its own report.

**Verdict:** `unverified`. Correct update syntax is not a cache result.

## What the live run did

Five user turns and one tool continuation, one Codex conversation
(`codex exec` then `codex exec resume --last`), conversation id stable
throughout and one router session, `session-85b5c8f8…`.

| Turn | Demand | Plan action | Effective effort | Reasoning tokens | Plan status |
|---|---|---|---|---:|---|
| 1 | low (one-sentence question) | keep | low | 0 | confirmed |
| 2 | high (derive a recurrence, prove it) | change_effort low → medium | medium | 126 | confirmed |
| 3 | low ("what is 7 times 8") | keep, 1 of 2 downgrade confirmations | medium | 0 | confirmed |
| 4 | low ("capital of Portugal") | change_effort medium → low | low | 0 | confirmed |
| 5 | low, with a shell tool call | keep | low | 0 | confirmed |
| 5 cont. | tool continuation | none (same turn) | low | 0 | — |

The model did not change at any point: one decision id, one binding revision,
`gpt-6-astra-responses -> gpt-6-astra` on every request, and the request-level
`reasoning.effort` was `low` on all six calls.

## Scope

- One model (`gpt-6-astra`) behind one proxy (CLIProxyAPI on one Mac mini) on
  one day, over HTTP only.
- Short personal test sessions: five turns, about 20k tokens of context, well
  inside a 400k window. No compaction, no truncation.
- Full-history replay only. Codex 0.155.1 sends `store: false` and the whole
  input on every request, so that is the only history mode exercised.
- One client (`codex-cli 0.155.1`) through one adapter revision.

## Limitations

- **Sample size.** Q07 is n=2 per arm, twice, with no interval. Nothing here
  establishes that adaptation is better than a fixed effort on any measure of
  quality, latency or cost. No such comparison was attempted.
- **Quality was not assessed at all.** Nobody graded a single reply, at any
  rung. The turn-4 downgrade to `low` on a trivial question is what the policy
  is meant to do, but no evidence in this run says the answers stayed good.
- **`evals/qualify_effort.py` was not run**, so none of its fixtures ran.
- **No long sessions, no compaction, no truncation.** Short-session
  qualification does not authorise long-session behaviour, and the provider's
  compaction-trigger flow is untested and unsupported.
- **No previous-response chain** (Q05), and only half of Q06.
- **One account, one region, one day.** A provider may change the endpoint
  under the same model name, and a proxy may change what it forwards.
- **Only one update is in flight at a time** by construction, and the deepest
  history tested carried two updates. Nothing here says what a long ledger
  does.
- **WebSocket, native multi-agent and concurrent execution** were not involved
  and are not covered.

## Rollback

Serving `router.yaml` instead, or switching the test config's
`experiments.adaptive_effort.mode` back to `off`, stops new decisions
immediately. Nothing in the shipped configuration ever changed: `router.yaml`
keeps the experiment off, declares no verified profile and has no
`gpt-6-astra-responses` entry. The test instance keeps its own sqlite file, so
this run's sessions and ledger are not mixed into the deployment's database,
and a session that ran under the experiment keeps its recorded effective
effort and its ledger rows exactly as they were.
