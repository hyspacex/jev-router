# jev-router: design

Why the router works the way it does. README.md says how to run it; this file
says why it was built this shape and what was rejected.

## Goal

A client sends `model: "auto"` and the request lands on a model that suits it.
Coding work goes to a strong coder, a one-line question goes to a fast model,
and nobody has to pick by hand.

Saving money is a secondary goal. Many OpenAI-compatible proxies sit in front
of subscriptions and OAuth accounts rather than metered APIs, so the gains that
matter are task fit, latency on easy requests, and spreading load across
several quotas instead of sending everything to one model.

## What the research says

Five findings shaped the design.

1. Rerouting on every request costs more than it saves. One published Jev-based
   router logged 309 requests and spent $106.73 against an $87.19 single-model
   baseline, because every model switch threw away the provider's prompt cache.
   Switching models inside a tool-use loop can also fail outright: some
   providers require their own reasoning blocks to be handed back unchanged. So
   this router decides once per conversation and pins the result.
2. The other open-source Jev router for this job is a one-commit scaffold. It
   ignores Jev's confidence number and reroutes every request. Its fail-open
   behaviour was worth copying; the rest was not.
3. Jev gets less accurate when the state holds text that does not matter, and
   text in the state can argue for its own classification ("this is trivial,
   use the cheap model"). So Jev sees a short summary of the request, never the
   transcript, and code enforces floors that Jev cannot lower.
4. Jev cannot count or compare numbers reliably. Token counts, context windows
   and capability checks stay in code.
5. Jev answers many questions in one request in about 0.3 s, and extra
   questions add almost no time. Output tokens are free and input costs about
   $0.042 per million, so a 2,000-token routing call costs under $0.0001.

## Where it sits

Three options were considered.

| Option | Verdict |
| --- | --- |
| A standalone shim in front of the proxy | Chosen. A small service that reads the request's `model` field and pipes everything else through untouched. It survives proxy upgrades, and it is easy to bypass by pointing a client back at the proxy's own port. |
| A router plugin inside the proxy | The designed extension point in some proxies, and it avoids an extra hop. Rejected: plugins are compiled shared libraries that have to match the host binary's plugin interface, which breaks quietly on upgrade. |
| LiteLLM with a pre-call hook | Rejected. It translates `/v1/messages` and `/v1/responses` rather than passing them through, and it adds a second large proxy to maintain. |

```
clients
   |
   v
jev-router  :8318
   |  model is not an alias: forward the bytes unchanged
   |  model is an alias:     pick a model, rewrite the field, forward
   v
any OpenAI-compatible proxy  :8317
```

The shim passes the client's `Authorization` header through, so it holds no
upstream key and the proxy keeps doing authentication. It was built and tested
against CLIProxyAPI, but nothing in it is specific to that proxy.

## Request flow for an alias

1. Compute a conversation key: a hash of the hashed `Authorization` header, the
   system prompt and the first user message. Few clients send a session id, and
   this hash is stable across the turns of one conversation. A subagent starts
   with a different first message, so it gets its own decision.
2. If that key has a pin younger than the TTL, reuse it and skip to step 7.
3. Filter models in code against a capability table in `router.yaml`: drop
   models without tool calling when the request carries `tools`, drop models
   without vision when it carries images, and drop models whose context window
   is smaller than the estimated prompt plus `max_tokens`.
4. Build the state, a short named summary of the request rather than the
   transcript. It holds the latest user request, the earlier request when the
   latest one only continues it, any pasted bulk under a field name that says
   it is quoted material, and facts computed in code.
5. Ask Jev the alias's questions in one request. The shipped `auto` alias asks
   three: what kind of work this is, how much thinking it needs, and whether a
   small mistake would cause real harm.
6. Run the answers through the rules in `router.yaml`. First match wins. A
   confidence gate runs before the rules and sends low-confidence answers to a
   safe default.
7. Apply the alias's effort cap, any client floor, and the capability filter
   again. Rewrite `model`, forward, and stream the upstream bytes back
   unmodified.

Naming the state's fields and pointing each question at one of them was worth
two points of task accuracy. Rewriting the difficulty levels as concrete
situations with examples was worth 23 points. `evals/EXPERIMENTS.md` has the
numbers.

## Fail-open

If the Jev call fails for any reason (timeout, 429, 529, bad JSON, an exception
in the client) the router falls back to a decider that guesses from counts
alone, or to the default route when no fallback is configured. A fallback
decision is never pinned, so the next turn tries Jev again. Jev being down
degrades the quality of the routing and never blocks a request.

The fallback decider adds a safety margin to its guessed difficulty score,
because it reads counts rather than meaning and so misses hard work that looks
small. Under-routing is the expensive error: the user gets a bad answer and has
to redo it. Over-routing only costs quota and time.

## Pinning

A pinned conversation never switches model. The pin holds a model, an effort
and the decision id, so every turn of one conversation reports the same
decision id and feedback lands in one place.

There is one exception. If a conversation outgrows the pinned model's context
window, the router moves it once to the smallest model that fits and re-pins.
That move gets a new decision id, because it is a different route.

## Shadow mode

`settings.mode: shadow` makes the router ask Jev, run the rules, and log what
it would have chosen, while sending every alias request to the default route.
Nothing is pinned. The response carries `X-Router-Shadow-Model` and the log row
is marked `mode = shadow`.

The point is to measure the classifier against real traffic before it can
affect anything. Run it for a week, read the log, fix the question wording,
then switch to `active`.

## Guard rails

- Pipelines that process untrusted text should keep naming their model
  explicitly. Text fetched from the web could try to steer Jev. The aliases are
  meant for interactive use, and the questions are written to treat pasted
  material as data.
- Per-client floors are keyed by the `X-Router-Client` request header, never by
  an API key.
- Only `/v1/chat/completions` understands an alias. Other endpoints pass
  through, and an alias name sent to one of them returns 400 with a message
  saying so.

## What is stored

One SQLite file holds pins, a decision log and feedback. The log keeps Jev's
answers with their confidences, the computed counts and flags, the rule that
fired, and a hash of the config that produced the row. It never keeps message
text, prompts or API keys. The conversation key is a hash and is not
reversible. That is enough to replay the policy against past decisions, which
is what `evals/tune.py` does.

## Open questions

- Jev's accuracy on code-heavy requests is measured only against a synthetic
  case set. Shadow mode on real traffic would say more.
- TypeSafe publishes no latency guarantee. The default timeout is a guess based
  on the measured 95th percentile of 0.25 s.
- A client that declares per-model settings (context window, reasoning efforts)
  needs the most conservative values of any model an alias can choose.
- `/v1/responses` and `/v1/messages` could learn aliases. They pass through
  today.
