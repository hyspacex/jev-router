# Pi strict auto integration

Requires Pi / pi-ai 0.86.x and Node 22.19+ (tests use Node 26 TypeScript support).
One picker entry, `jev-router/auto`, resolves fresh work once and keeps the
model and base effort. Router policy chooses; the extension does not.

## Install

1. Install this directory as a Pi package, or point a global extension loader
   at `index.ts`. Keep package dependencies resolvable in the deployment.
2. Keep the existing `jev-router` upstream credential in Pi's `models.json` or
   auth store. Remove its old `models` and `modelOverrides` entries; the extension
   owns the one auto model. Do not override the custom transport with another API.
3. Create `~/.pi/agent/jev-router.json`:

```json
{
  "origin": "http://127.0.0.1:8318",
  "adminTokenFile": "/absolute/path/to/private-router-token"
}
```

The token file must be private (0600). Alternatively set `adminTokenEnv` to the
name of an environment variable. Never put credentials in the repository.
For remote connections use HTTPS or a trusted encrypted tunnel.

4. Reload Pi, start `/new`, and select `jev-router/auto`. Do not switch an old
   unbound transcript into auto: strict admission requires genuinely fresh work.

## Behavior and limits

- Chat Completions, text/images subject to the negotiated profile, tools and
  streaming. The model picker metadata before admission is a placeholder;
  execution uses the bound context and output ceilings.
- Fixed effort only. Adaptive effort and extra semantic questions are off.
- Binding identity and pending execution IDs are stored as Pi custom entries,
  outside LLM context. Credentials and extra prompt copies are not stored.
- A resume must find the same binding. A fork never inherits its parent's binding.
- Successful provider completion clears the pending execution before Pi can
  start its next tool-loop request. Abort, broken streams and errors keep an
  unresolved marker and block automatic resubmission.
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
npm test
```

Tests use the actual Pi serializer/stream parser with a fake HTTP provider,
not paid inference. Cover tools, fixed wire identity, resume, fork rejection,
pending-outcome guards, persistence and redirects. Typecheck against the installed
Pi package before deployment. Python session contract tests remain in `tests/`.
