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
does not authorise the shipped configuration, another account, another client
or another day. A second sitting the same day added one compaction and the
measurements behind it; one compaction on a short session is not a long
session.

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

**3. A logging pass-through** in the second sitting, standing in for the proxy
so that Codex's own compaction could be read off the wire without spending
anything. It records identifiers, field names, item types and sizes, never
message text. Codex was pushed into compacting with
`-c model_auto_compact_token_limit=<small>`; against the real proxy the same
override is what made the live run compact.

Two shell details worth knowing before repeating any of this: `codex exec`
reads stdin, so it has to be given `< /dev/null` or it hangs before making a
request, and both the router and the adapter need the same
`JEV_ROUTER_ADMIN_TOKEN` in their environment or every execution comes back
401.

**Credential used:** the proxy key, read at run time from a file outside the
repository and given to the adapter with `--upstream-key-file`. Never printed,
logged, stored or committed. `TYPESAFE_API_KEY` from the environment, for the
classifier calls, which are not model calls and are not in the budget below.

**Calls made:** 14 upstream model calls against a budget of 40 — 6 through the
adapter (five turns, one of which had a tool continuation) and 8 direct probes.
**Wall clock:** about 20 minutes of running. **Spend:** subscription quota.

A second sitting the same day, after the owner's two decisions, added 31 more
against a budget of 60: 24 direct probes about compaction and about whether an
update survives one, and 7 through the adapter — one first attempt whose
stream broke, then a five-turn Codex conversation with one compaction in it. A
logging pass-through in front of Codex established what it sends and made no
upstream calls at all. Both sittings are written up below.

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
| Q09 | `truncation: auto` and the standalone compaction endpoint are refused for this history, as documented. | **superseded**, see "Compaction, measured" | The owner decided on 2026-09-19 that a compaction the client asks for is supported. `truncation: auto` is still refused by the router; the compaction endpoint is now forwarded as a maintenance request, and this proxy answers it with a 404. |
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

## Compaction, measured

Added later the same day, after the owner decided that a compaction the
client asks for stays exactly what the client does. Two parts: a logging
pass-through in front of codex-cli 0.155.1 with a low
`model_auto_compact_token_limit`, which made no upstream calls at all, and
direct probes at the proxy.

**What Codex sends.** Not the provider's flow. It sends
`x-codex-beta-features: remote_compaction_v2` on every request, but
`codex features list` on this build reports `remote_compaction_v2` as
**removed**, and `--enable remote_compaction_v2` changes nothing. When it runs
out of room it sends an ordinary `POST /v1/responses` carrying the whole
history plus a trailing user message with no id, marked only by
`x-codex-turn-metadata`: `request_kind` is `"compaction"` instead of
`"turn"`, with `compaction: {trigger: "auto", reason: "context_limit",
implementation: "responses", phase: "pre_turn", strategy: "memento"}`. It then
opens a new window (`x-codex-window-id` `…:0` to `…:1`) and rebuilds its
history around the reply. No `compaction_trigger` item, no
`context_management` field, no call to `/v1/responses/compact`, and it does
not replay a provider `compaction` item even when the endpoint returns one.

It also declares a **different tool set** for a compaction request, so the
first item of the history carries a different id and every prefix hash behind
it changes. That is why a compaction cannot be held to a turn's recorded
anchors, and it is what the first live attempt refused on.

**What the provider does.** It implements the explicit flow.

| Probe | Result |
|---|---|
| `compaction_trigger` as the final input item | 200, output is one item `{"id": "cmp_…", "type": "compaction", "encrypted_content": …}`, about 1.8 kB |
| `compaction_trigger` anywhere but last | 400 `"The 'compaction_trigger' item must be the final input item."` |
| `configuration_update` earlier in a triggered history, or adjacent to the trigger | 200 |
| replay `[compaction, user]` | 200 |
| replay `[compaction, configuration_update, user]` | 200 |
| replay `[configuration_update, compaction, user]` | 400 `"The 'configuration_update' item type is not supported with compaction without a subsequent configuration update."`, `param: input[0].type` |
| `truncation: "auto"` with an update in the history | 200 at the provider; the router refuses it |
| `context_management: {mode: "auto"}` with an update | 200 at the provider; the router refuses it |
| `POST /v1/responses/compact` | **404 Not Found** on this proxy, with and without an update in the history |

**Does an effort update survive a compaction?** No. Same puzzle, base `low`,
two samples an arm, straight at the proxy:

| Arm | Reasoning tokens |
|---|---|
| base `low`, no compaction | 65, 94 |
| base `low` + update to `high` | 121, 132 |
| replay a compaction **made from a history that carried the update**, no new update | 73, 71 |
| replay a plain compaction **plus a fresh update to `high`** | 118, 112 |

The compacted arm sits with the base arm and the reapplied arm sits with the
update arm. Read it as consistent with the protocol and nothing more: two
samples an arm, one prompt, no interval, no counterbalancing. It is the same
weakness as Q07 and it earns the same caveat.

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

## What the first live run did

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

That run was made before the two owner decisions of 2026-09-19. It is left
here as it was; turn 2's `low -> medium` is what the one-rung climb did.

## The second live run: the jump and a compaction

One Codex conversation, session `session-9a7cb1c0…`, five user turns and one
Codex compaction. Ten upstream calls.

| Turn | Demand | Plan action | from → to | Reasoning tokens | Plan status | Compaction |
|---|---|---|---|---:|---|---|
| 1 | trivial ("7 times 8") | keep | low → low | 0 | confirmed | — |
| 2 | clearly hard, difficulty 2.99 | change_effort | low → **high** | 182 | confirmed | — |
| 3 | trivial ("colour of a ripe banana") | keep, 1 of 2 downgrade confirmations | high → high | 0 | confirmed | — |
| 4 | hard, difficulty 2.98 | keep (already at the top rung) | high → high | 151 | confirmed | — |
| — | Codex compacts, pre-turn, `reason: context_limit` | maintenance, no plan | effective high → low | 0 | — | epoch 0 → 1 |
| 5 | hard, difficulty 2.98 | change_effort | low → **high** | 0 | confirmed | in epoch 1 |

Turn 2 is the upward jump: one plan, `low` straight to `high`, with the reason
`low -> high: difficulty 2.99 is at or above 1.8; difficulty 2.99 is at or
above the 2.6 this ladder asks for high`. Turn 5 is the post-compaction
reapplication: the epoch put the session back at the base effort, the turn
asked for `high` again, and the request carried one update item and none of
the folded-up one. The model was `gpt-6-astra-responses -> gpt-6-astra` on
every call, one binding revision throughout, and the request-level
`reasoning.effort` was `low` on all ten.

Two things this run says that are worth writing down rather than smoothing
over:

- **The first compaction attempt was refused**, with
  `EFFORT_HISTORY_MISMATCH`: "the history before position 8 is not the history
  the update for turn 'turn-0002' was accepted against". That is the different
  tool declaration above. The router now checks a maintenance request for
  ownership rather than for position, and the retry went through.
- **Turn 5 reported 0 reasoning tokens** at `high`, where turns 2 and 4
  reported 182 and 151. Its response id also carries a different prefix from
  the earlier ones, which suggests the proxy served it from a different
  account or backend. Nothing here establishes whether the update took effect
  on that call, and this report does not claim it did.

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
- **One compaction, on a short session.** The second run compacted once, with
  Codex's auto-compact limit lowered by hand so it would. Nothing here says
  what a long session does, what several compactions in a row do, or what a
  compaction costs in cache. The provider's explicit trigger flow was probed
  directly and by fixture and by no real client: Codex does not speak it. The
  standalone compaction endpoint 404s on this proxy. `truncation: auto` was
  accepted by the provider and is still refused by the router.
- **The reasoning-token evidence about compaction is two samples an arm**, on
  one prompt, with no interval — the same weakness as Q07, and turn 5 of the
  live run disagreed with it by reporting none at all.
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
