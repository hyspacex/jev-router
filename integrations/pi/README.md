# Pi strict auto integration

Requires Pi / pi-ai 0.87.x and Node 22.19+ (tests use Node 26 TypeScript support).
One picker entry, `jev-router/auto`, resolves fresh work once under the strict
`auto` alias and keeps the model and base effort. Router policy chooses,
including which subscription takes the work when one is busy
(docs/guides/quota.md); the extension only names the alias. A session bound
under the retired `auto-conserve` carries on from the `auto` entry.

## Install

1. Install this directory as a Pi package, or point a global extension loader
   at `index.ts`. Keep package dependencies resolvable in the deployment.
2. Keep the existing `jev-router` upstream credential in Pi's `models.json` or
   auth store. Remove its old `models` and `modelOverrides` entries; the extension
   owns the auto models. Do not override the custom transport with another API.
3. Create `~/.pi/agent/jev-router.json`:

```json
{
  "origin": "http://127.0.0.1:8318",
  "adminTokenFile": "/absolute/path/to/private-router-token",
  "maxOutputTokens": 8192
}
```

`origin` must be a bare HTTP(S) origin with no credentials, path or query. It
defaults to `http://127.0.0.1:8318`.

`maxOutputTokens` is the output allowance admission asks the router for. It
defaults to 8192. The router negotiates it down against the deployment and the
alias; execution always uses the negotiated ceiling, never this number.

`JEV_ROUTER_PI_CONFIG` names a different config file. Set it to point Pi, or a
test run, at a configuration other than the one in `$HOME`.

The admin credential comes from `JEV_ROUTER_ADMIN_TOKEN` (or the variable
`adminTokenEnv` names). Only if that is empty is `adminTokenFile` read, so an
environment credential always wins over the deployment's file. The token file
must be private (0600). Never put credentials in the repository. For remote
connections use HTTPS or a trusted encrypted tunnel.

4. Reload Pi, start `/new`, and select `jev-router/auto`. Do not switch an
   old unbound transcript into it: strict admission requires genuinely fresh
   work.

## Behavior and limits

- Chat Completions, text/images subject to the negotiated profile, tools and
  streaming. The model picker metadata before admission is a placeholder;
  execution uses the bound context and output ceilings.
- Fixed effort only. Adaptive effort and extra semantic questions are off. A
  binding that is not fixed-effort `openai-chat` is refused, and the refusal
  names the field that was wrong.
- Binding identity and pending execution IDs are stored as Pi custom entries,
  outside LLM context. Credentials and extra prompt copies are not stored.
- A resume must find the same binding. A fork never inherits its parent's binding.
- Successful provider completion clears the pending execution before Pi can
  start its next tool-loop request.
- A refusal the router names with a code it only raises before forwarding
  (`INVALID_ROUTER_INPUT`, `ROUTER_UNAUTHORIZED`, `SESSION_UNKNOWN`,
  `SESSION_CLOSED`, `PROFILE_CHANGED`, `NO_SAFE_ADMISSION`,
  `CONTEXT_BUDGET_EXCEEDED`, `UNSUPPORTED_PROFILE`, `PROVIDER_UNAVAILABLE`)
  frees the request id: the provider never ran it, so the next request goes
  ahead on its own. The code is shown; the router's message text is not.
- `PROVIDER_UNAVAILABLE` at admission means the chosen model's quota is spent.
  No binding is made, and the message says roughly when the quota resets,
  from the router's `retry_after_seconds`.
- Everything else keeps the block. A transport failure, an upstream status the
  router passed through, a stream that broke after acceptance,
  `EXECUTION_OUTCOME_UNKNOWN`, `SESSION_CONFLICT` and
  `REQUEST_ALREADY_COMPLETED` leave an outcome only a person can settle.
- `/jev status` shows the contract and pending ID. `/jev retry` requires explicit
  confirmation to permit a new attempt that may duplicate work; it does not
  claim to reconcile the previous provider outcome. `/jev close` closes the
  contract. `/jev audit` prints the dashboard URL.
- Client compaction, tree rewinds, imported histories and adaptive effort are
  **not supported in this first version**. They are refused rather than silently
  re-admitting work or changing models. Other providers retain their behavior.
- Billing rates are unknown for the routed alias; displayed zero rates are not
  evidence of free inference or quota savings.

## Verification

```sh
cd integrations/pi
npm install   # the Pi packages are devDependencies as well as peers
npm test
```

The tests never make a real request. `test/env.mjs` points the extension at
`test/fixtures/jev-router.json` through `JEV_ROUTER_PI_CONFIG` before the
extension is imported, so `$HOME` is never read, and it replaces
`globalThis.fetch` with a stub that throws. The fixture origin is a `.invalid`
name that cannot resolve. `npm test` therefore gives the same result whether or
not a real `~/.pi/agent/jev-router.json` exists.

`test/transport.test.mjs` uses the actual Pi serializer and stream parser with
a fake HTTP router: the admission payload, the bound wire model, the negotiated
limits, the configured origin, credential precedence and both error branches.
`test/client.test.mjs` covers the session contract itself against the response
shape `sessions.contract()` produces: resume, fork refusal, the retry block,
request-id rotation, malformed bindings and the pre-acceptance code list.

Typecheck against the installed Pi package before deployment. Python session
contract tests remain in `tests/`.
