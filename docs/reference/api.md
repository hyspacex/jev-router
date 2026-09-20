# HTTP and CLI reference

[Documentation index](../README.md) · [Project home](../../README.md)

### Other commands

```sh
uv run jev-router explain request.json      # features, state, Jev answers, chosen model
uv run jev-router explain request.json --pressure openai=0.8
uv run jev-router quota --poll              # quota snapshots and pressure per provider
uv run jev-router quota --poll --json
uv run jev-router decisions -n 20           # tail the decision log
uv run jev-router decisions replay last     # re-run one decision from its own row
uv run jev-router feedback last too_weak --model gpt-6-astra --effort high
uv run jev-router sessions list             # strict session bindings
uv run jev-router sessions show <id>
uv run jev-router sessions close <id>
uv run jev-router sessions reconcile <id> --plan <plan-id> --outcome applied
uv run jev-router prune --older-than-days 30   # irreversible
uv run jev-router adapter --alias <strict-alias> --port 18320
```

Put the global `-c/--config` option **before the subcommand**, for example
`uv run jev-router -c local.yaml serve`. It selects the path to `router.yaml`, which also
reads from `$JEV_ROUTER_CONFIG` and defaults to `router.yaml` in the working
directory. `serve` takes `--host`, `--port`, `--mode shadow|active` and
`--log-level`.

`explain` never forwards the request. Its argument is a JSON file holding a
chat-completions body; an optional `_headers` object inside it stands in for
request headers, and `--alias` treats the body as a different alias.
`--pressure provider=N` pretends a provider is under that much quota pressure
and shows what the decision would be.

`quota` prints the same report as `GET /router/quota`. Without `--poll` it
shows the configuration and nothing else; with it, each enabled source runs
once.

`decisions replay` re-runs a stored decision from the row alone: the config
hash, the answers, the request facts and the pressure it was taken under. No
Jev call, no quota poll, no prompt. It says so when the config hash has moved
rather than claiming a match. `sessions` and `adapter` belong to strict
sessions; see [`docs/SESSION_ROUTING.md`](../../docs/SESSION_ROUTING.md) and
[`docs/ADAPTIVE_EFFORT.md`](../../docs/ADAPTIVE_EFFORT.md).

### Endpoints

The `/dashboard` and `/router/audit` entries describe the separate dashboard
implementation; they may not be available on a revision containing only this
documentation update. Use `decisions` and `decisions replay` from the CLI there.

| Path | Method | Behaviour |
| --- | --- | --- |
| `/v1/chat/completions` | POST | aliases are routed; a strict session executes here; everything else is forwarded unchanged |
| `/v1/responses` | POST | a strict session whose profile speaks the Responses protocol executes here; everything else is forwarded unchanged |
| `/v1/responses/compact` | POST | a standalone compaction, forwarded as maintenance for the bound session |
| `/v1/models` | GET | the upstream list plus one entry per alias |
| `/router/health` | GET | mode, upstream, aliases, models, providers, routes, unhealthy providers, decider, whether a Jev key is set, uptime |
| `/router/providers` | GET | circuit breaker state and the pressure per provider |
| `/router/quota` | GET | quota snapshots, pressures, staleness and the last error per provider |
| `/dashboard` | GET | interactive read-only audit UI; data requires router authentication |
| `/router/audit?limit=N` | GET | prompt-free audit snapshot, 1–500 recent events (default 300) |
| `/router/decisions?limit=N` | GET | recent decisions as JSON |
| `/router/feedback` | POST | record what you thought of a decision |
| `/router/feedback?limit=N` | GET | recent feedback, joined to its decision |
| `/router/resolve` | POST | resolve or resume a strict session binding |
| `/router/sessions?limit=N` | GET | strict session bindings, newest first |
| `/router/sessions/{id}` | GET | one binding: identity, limits, efforts and state |
| `/router/sessions/{id}/close` | POST | end a session deliberately |
| `/router/turn-plan` | POST | an effort-only decision for a new user turn (experimental) |
| `/router/turn-plan/{id}/reconcile` | POST | say what happened to an ambiguous effort update |
| anything else | any | streamed through to the upstream, method and path unchanged |

An alias name on any other POST endpoint returns 400 with a message saying so.

Every `/router/*` path needs `X-Router-Admin-Token` once the environment
variable named by `settings.admin_token_env` is set; without one, only direct
loopback callers are accepted. The strict-session paths need that credential
always, loopback included, and there is no setting that turns it off:
`resolve`, `turn-plan`, `sessions`, and any managed execution on `/v1/*`.

### Response headers

On an alias request:

| Header | Meaning |
| --- | --- |
| `X-Router-Model` | the model name sent upstream, effort included |
| `X-Router-Effort` | the effort that was chosen |
| `X-Router-Rule` | the rule that fired, or `pin`, `low_confidence`, `default` or `fallback` |
| `X-Router-Route` | the named route the rule pointed at, when it used one |
| `X-Router-Pinned` | whether this came from a pin |
| `X-Router-Decision` | the decision id, for `jev-router feedback` |
| `X-Router-Fallback` | the index of the route entry that served the request; absent on the primary |
| `X-Router-Pressure` | the pressure per provider, only when it changed the outcome |
| `X-Router-Decider-Fallback` | the decision came from the fallback decider, not from Jev |
| `X-Router-Shadow-Model` | in shadow mode, what would have been chosen |

On a strict session's execution:

| Header | Meaning |
| --- | --- |
| `X-Router-Model` | the bound profile's wire model |
| `X-Router-Effort` | the **base** effort, which never moves for the life of the session |
| `X-Router-Session` | the session id |
| `X-Router-Binding` | the binding revision that served it |
| `X-Router-Session-State` | `active` once accepted, otherwise the stored state |
| `X-Router-Decision` | the decision id the binding was taken under |
| `X-Router-Effective-Effort` | only when adaptation is on: the effort this request really ran at |
| `X-Router-Compaction-Epoch` | only when adaptation is on: how many times the history has been folded up |

### Environment

| Variable | Read by | What it does |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | the router, `evals/` | The Jev key. Named by `settings.jev_api_key_env`. Unset means classifier fallback; strict admission requires an explicit `admission_fallback` or returns `NO_SAFE_ADMISSION`. |
| `JEV_ROUTER_UPSTREAM` | the router, `evals/` | Overrides `settings.upstream_base_url` whenever it is set and non-empty. The environment wins over the file. |
| `JEV_ROUTER_CONFIG` | the CLI | The default for `-c/--config`, on every subcommand. Defaults to `router.yaml` in the working directory. |
| `JEV_ROUTER_ADMIN_TOKEN` | the router, the adapter, `evals/run_sessions.py`, `evals/qualify_effort.py` | The `/router/*` credential, sent as `X-Router-Admin-Token`. Named by `settings.admin_token_env`. Required for every strict-session request. |
| `UPSTREAM_API_KEY` | `evals/run_outcomes.py`, `evals/qualify_effort.py` | The proxy's own key, for the scripts that call candidate models. The router never reads it: it forwards the client's `Authorization`. |
| `JEV_ROUTER_QUALIFY_LIVE` | `evals/qualify_effort.py` | Must be `1`, alongside `--confirm-live`, before that script makes a real call. |
| `JEV_ROUTER_QUALIFY_ALLOW` | `evals/qualify_effort.py` | Comma-separated profiles the qualification may touch, on top of the in-file allowlist, which is empty. |
| `JEV_EVAL_JUDGE_MODEL` | `evals/run_outcomes.py` | The judge model for the outcome benchmark. Defaults to `gpt-5.6-sol(high)`. |

`.env.example` has the four the router itself cares about.
