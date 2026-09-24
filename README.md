# jev-router

**Choose a model for the task. Keep it for the session.**

jev-router is a self-hosted model router for coding agents and OpenAI-compatible
clients. It uses [TypeSafe’s Jev](https://docs.typesafe.ai) to assess a new task,
then applies your YAML rules to choose a model and reasoning effort from your
upstream’s model pool.

The default `auto` entrypoint binds each session to one model, provider, and set
of limits. It does not switch models halfway through a tool loop—or silently
move a session to a cheaper model when quota gets tight.

[Get started](#quick-start) · [Documentation](docs/README.md) ·
[Pi integration](integrations/pi/README.md) ·
[Configuration](docs/reference/configuration.md)

## Why use it?

- **One entrypoint for different tasks.** Let a policy choose between fast and
  stronger models rather than choosing manually for every new session.
- **Stable coding sessions.** Resolve the model before the client sizes its
  history. Context overflow and provider failures are explicit errors, not
  reasons to change the binding.
- **Rules you can inspect.** Providers, models, questions, routes, client floors,
  and effort limits live in YAML. Decisions can be replayed from their logs.
- **Quota-aware admission.** Optional quota sources can steer new work within
  configured quality lanes, in whichever direction relieves the busy
  subscription: to a model measured as equally good on another provider, or to
  a cheaper one on the same provider. Missing or stale telemetry contributes no
  pressure.
- **Your existing upstream.** The router forwards to an OpenAI-compatible API or
  proxy and preserves streamed response bytes. It does not host models.

```text
Client / coding agent
        │
        ├── resolve a fresh session ──► jev-router ──► Jev assessment
        │                                  │         + YAML policy
        │◄── model, effort, limits ────────┘
        │
        └── execute with binding ────► jev-router ──► your upstream
                                         same model for the session
```

**Status:** early-stage software (0.1.0). Strict `auto` and fixed effort are the
shipped path. Adaptive effort is experimental and off by default. The repository
contains deployment-specific model names and evaluation evidence, not a promise
of quality equivalence or cost savings for your own pool.

## Quick start

### 1. Install from source

You need **Python 3.12+**, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and an upstream that serves an OpenAI-compatible Chat Completions API.
The HTTP walkthrough also uses `curl` and `jq`.

```sh
git clone https://github.com/hyspacex/jev-router.git
cd jev-router
uv sync --locked
```

### 2. Test the connection with your own model

Start with the single-model demo. It needs **no Jev key** and makes no external
classification calls; generating a reply still uses your upstream’s quota.
This proves the session contract works, not that routing improves quality.

```sh
cp examples/quickstart.yaml quickstart.local.yaml
```

Edit `quickstart.local.yaml`: set `models.demo.upstream_id` to a model ID your
upstream serves, and check its context/output limits. The demo advertises neither
tools nor vision. Do not advertise capabilities your deployment has not tested.

In the terminal that will run the server:

```sh
export JEV_ROUTER_UPSTREAM="http://127.0.0.1:8317"  # your upstream, without /v1
export JEV_ROUTER_ADMIN_TOKEN="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run jev-router -c quickstart.local.yaml check-config
uv run jev-router -c quickstart.local.yaml serve
```

Keep the control token private. In a second terminal, set
`JEV_ROUTER_ADMIN_TOKEN` to the **same value** and set `UPSTREAM_API_KEY` to your
upstream’s credential. These are different credentials: the first authorizes
router sessions; the second is forwarded to the model API. The CLI reads the
process environment; it does not automatically load a `.env` file.

### 3. Resolve a session, then send a request

A strict alias is **not** a drop-in `model: "auto"` request. Resolve it first:

```sh
export SESSION_ID="demo-$(uv run python -c 'import uuid; print(uuid.uuid4())')"

BINDING=$(curl --fail-with-body -sS http://127.0.0.1:8318/router/resolve \
  -H "X-Router-Admin-Token: $JEV_ROUTER_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg id "$SESSION_ID" '{
    schema_version: "1", intent: "new", session_id: $id,
    request_id: "resolve-1", alias: "auto", client: "quickstart",
    client_contract: {
      dynamic_metadata: true, protocols: ["openai-chat"],
      fresh_execution_context: true
    },
    request: {messages: [{role: "user", content: "Say hello in one sentence."}]},
    limits: {max_output_tokens: 256}
  }')")

printf '%s\n' "$BINDING" | jq .
```

A successful response has `state: "prepared"`, a `binding_revision`, and an
`execution` object with the selected model and limits. If the command fails,
fix the reported error before continuing.

```sh
curl --fail-with-body -sS http://127.0.0.1:8318/v1/chat/completions \
  -H "Authorization: Bearer $UPSTREAM_API_KEY" \
  -H "X-Router-Admin-Token: $JEV_ROUTER_ADMIN_TOKEN" \
  -H "X-Router-Session: $SESSION_ID" \
  -H "X-Router-Binding: $(printf '%s' "$BINDING" | jq -r .binding_revision)" \
  -H 'X-Router-Request-Id: execution-1' \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg model "$(printf '%s' "$BINDING" | jq -r .execution.wire_model)" '{
    model: $model, max_tokens: 256,
    messages: [{role: "user", content: "Say hello in one sentence."}]
  }')"
```

You should receive your upstream’s Chat Completions response. Use a new execution
request ID for each subsequent request; resending a completed ID never calls the
provider again. Start a new session for independent work.

```sh
uv run jev-router -c quickstart.local.yaml sessions list
uv run jev-router -c quickstart.local.yaml decisions -n 5
uv run jev-router -c quickstart.local.yaml sessions close "$SESSION_ID"
```

See [troubleshooting](docs/guides/getting-started.md#troubleshooting) if admission
works but execution fails.

## Turn on task-based routing

The demo has only one model. For Jev-based selection:

1. Copy [`router.yaml`](router.yaml) to your own configuration. Its model IDs and
   effort suffixes describe a particular proxy; they are not universal API IDs.
2. Configure your providers, model capabilities, routes, and alias permissions.
   Review the quality-lane evidence before using or replacing its model pairs.
3. Set `TYPESAFE_API_KEY` to your [TypeSafe](https://docs.typesafe.ai) API key.
4. Validate with `jev-router -c <your-config> check-config`, then serve that file.

Jev receives a bounded state containing request excerpts and computed facts—not
just anonymous counts. If Jev fails, strict admission uses the alias’s explicit
`admission_fallback`, or refuses with `NO_SAFE_ADMISSION`. It never revisits an
existing binding when the classifier recovers.

[Setup and configuration choices](docs/guides/getting-started.md) ·
[How the policy selects a model](docs/SEMANTIC_POLICY.md) ·
[Model admission evidence](POOL.md)

## Connect a client

| Client | Path |
| --- | --- |
| **Pi** | The [Pi extension](integrations/pi/README.md) resolves fresh work and uses the negotiated metadata. Start `/new`, then select `jev-router/auto`. |
| **Your own client** | Implement [resolve and managed execution](docs/SESSION_ROUTING.md); the HTTP example above shows the minimum flow. |
| **Ordinary chat client** | Define a [legacy alias](docs/guides/legacy-routing.md) if the client cannot resolve sessions. It has different pin and fallback semantics. |
| **Codex** | The [experimental adapter](docs/ADAPTIVE_EFFORT.md#trying-it-with-codex) is for a separately configured deployment, not the default onboarding path. |

The shipped config exposes only strict `auto`. Older `auto-fast`, `auto-code`,
and `auto-session` examples require custom configurations.

## Inspect, deploy, and extend

- [Operations](docs/guides/operations.md): inspect decisions, record feedback,
  understand storage and privacy, and manage retention.
- [Deployment](docs/DEPLOYMENT.md): service setup, upgrades, backup, and rollback.
- [API and CLI reference](docs/reference/api.md): endpoints, headers, environment
  variables, and commands.
- [Configuration reference](docs/reference/configuration.md): extend the pool and
  policy through YAML; use Python only for new builders, deciders, or quota sources.
- [Evaluation guide](docs/development/evaluation.md): distinguish offline replay,
  live model results, and simulated quota behavior.

Run the offline test suite without spending provider quota:

```sh
uv run pytest -q
uv run jev-router check-config
```

## Boundaries to know

- Run **one service process per SQLite database**. Strict execution claims are
  in memory, not coordinated across processes.
- A strict session does not fail over to another model on 429, timeout, or context
  overflow. The client must handle the error and decide what to do next.
- Keep the router on loopback unless you have authenticated ingress and HTTPS or
  a trusted encrypted tunnel. Upstream authentication alone does not protect Jev quota.
- Keep `log_state: false`. Request excerpts go to Jev when classification is on;
  feedback notes are stored verbatim. This is a single-owner tool, not a
  multi-tenant authorization system.
- Live evaluations and model replies spend quota. Existing benchmarks do not
  establish savings or equivalent quality for another deployment.

[Known issues](docs/reference/known-issues.md) · [Full documentation](docs/README.md)

## License

[MIT](LICENSE).
