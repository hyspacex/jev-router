# Setup choices and troubleshooting

[Documentation index](../README.md) · [Quick start](../../README.md#quick-start)

## Choose a starting point

| Configuration | Purpose | Classifier |
| --- | --- | --- |
| [`examples/quickstart.yaml`](../../examples/quickstart.yaml) | Verify connectivity and strict sessions with one model you supply. | Local rules; no Jev call. |
| [`router.yaml`](../../router.yaml) | Starting point for a multi-model policy with strict `auto`. Adapt it to your deployment. | Jev, with an explicit admission fallback. |
| A custom legacy alias | Support a chat client that cannot implement resolve. | Jev or local rules, as configured. |

The quickstart is intentionally not a model admission verdict. It cannot show
that semantic routing works: there is only one candidate. It uses a separate
`~/.jev-router/quickstart.db` so trying it does not reuse production bindings.

## Configure a real pool

Copy `router.yaml` rather than editing a running service's configuration in place.
Review these parts together:

1. **Upstream URL.** Use the origin without `/v1`, such as
   `http://127.0.0.1:8317`. `JEV_ROUTER_UPSTREAM` overrides the YAML value.
2. **Models.** Set each `upstream_id`, wire protocol, context window, output cap,
   tools/vision support, and effort encoding to what the deployment actually
   accepts. `effort_style: suffix` is proxy-specific; many direct APIs instead
   use a request parameter or support no effort setting.
3. **Selection.** Update routes, policy defaults, client restrictions, alias
   `allowed_models`, and `admission_fallback` consistently. A missing model or
   impossible intersection is a configuration/routing error, not permission
   to ignore restrictions.
4. **Evidence.** A quality lane claims adequacy for a model/effort pair on a kind
   of work. Follow the [pool admission process](../../evals/ADMISSION.md) before
   adding production candidates; do not copy an old model's qualification onto
   a different deployment.
5. **Credentials.** Export `TYPESAFE_API_KEY` for Jev and
   `JEV_ROUTER_ADMIN_TOKEN` for strict control/execution. Clients supply their
   upstream `Authorization` separately. See [environment reference](../reference/api.md#environment).
6. **Validation.** Run `uv run jev-router -c <your-config> check-config` before
   restarting. This checks configuration consistency, not live model availability.

The shipped `auto` uses the existing three-question policy. Extra shadow
questions, distribution routing, and adaptive effort are off. Quota commands
also ship disabled: enable only tools you have installed and reviewed.

## Preview a decision

Create a JSON Chat Completions request, then run:

```sh
uv run jev-router -c <your-config> explain request.json
uv run jev-router -c <your-config> explain request.json --pressure openai=0.8
```

`explain` does not forward to the selected model, but a Jev configuration makes
an external classification call and spends TypeSafe quota. Its output may
include the constructed request state; do not publish it if it contains private
input. Use synthetic text for setup.

For no external classification, set `settings.decider: rules` and remove or
replace alias-level `decider: jev` overrides. Strict admissions without semantic
answers use their explicit fallback; this is not equivalent to Jev selection.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `ROUTER_UNAUTHORIZED` | Server and client must use the same router control token, including on loopback. It is not the upstream API key. |
| A plain `model: auto` request is refused | The default alias is strict. Resolve first and send all four managed-execution headers, or use a session-aware integration. |
| `NO_SAFE_ADMISSION` | Check Jev access, explicit admission fallback, model permissions, tools/vision, and context constraints. Do not bypass a hard constraint to force admission. |
| Upstream 401/403 | Supply the upstream credential in `Authorization` on execution. The router does not read `UPSTREAM_API_KEY` for you. |
| Upstream 404 / unknown model | Verify the URL has no extra `/v1`, the wire model exists, and the effort encoding matches your proxy. |
| `CONTEXT_BUDGET_EXCEEDED` | Read the negotiated limits. Reduce the request or output ask within the contract; the router does not silently truncate or select a bigger model. |
| `SESSION_CONFLICT` | Check the binding revision, model identity, and concurrent requests. One execution may be in flight per session. |
| `REQUEST_ALREADY_COMPLETED` | The request ID was already used. Use a new ID only for a genuinely new attempt, not to evade duplicate protection. |
| `EXECUTION_OUTCOME_UNKNOWN` | Do not blindly retry: the provider may have executed. Inspect the session and follow its error guidance. |
| Configuration unexpectedly differs | Check `JEV_ROUTER_CONFIG`, `JEV_ROUTER_UPSTREAM`, and the server's `-c` argument. Use the same config for inspection commands. |

See the [full error contract](../SESSION_ROUTING.md) for response shapes and
retry semantics. Record the session/request IDs when investigating; never put
credentials or prompt text into a public issue.

## Next steps

- [Use Pi](../../integrations/pi/README.md) for fresh coding sessions.
- [Build a client](../SESSION_ROUTING.md) that consumes resolved metadata.
- [Inspect decisions](operations.md) and replay them before changing policy.
- [Deploy a service](../DEPLOYMENT.md) only after local execution works.
