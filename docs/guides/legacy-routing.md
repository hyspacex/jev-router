# Legacy conversation routing

[Documentation index](../README.md) · [Project home](../../README.md)

This guide describes custom aliases with `session_mode: legacy` (also the schema default when omitted). **The shipped `auto` alias is strict**, so these pin and fallback rules do not describe its execution. See [strict sessions](../SESSION_ROUTING.md).

## How it works

```
your client
   |  model: "auto"
   v
jev-router  :8318 ----------> Jev  (one call, several questions, ~0.3 s)
   |  rewrite model
   v
your proxy  :8317
```

For every request to `/v1/chat/completions`:

1. Look at `model`. If it is not an alias defined in `router.yaml`, forward the
   original bytes and stop.
2. Compute a versioned conversation key scoped by hashed `Authorization`, alias,
   `X-Router-Client`, system prompt and first user message. Revalidate any live
   pin against current model permissions, effort bounds, tools, vision and context.
   Reuse a valid pin without Jev. If it is incompatible, choose the smallest
   eligible replacement, which may have a smaller window when capabilities change.
3. Compute features in code: an estimated token count, message count, tools,
   images, code blocks, code-block languages, and the `X-Router-Client` header.
   Jev is never asked to count anything.
4. Build the state. This is a short summary of the request, not the transcript:
   the latest user request, the earlier request when the latest one only
   continues it, pasted bulk under a field name that marks it as quoted
   material, and the computed facts.
5. Ask Jev the alias's questions in one call.
6. Run the answers through the rules. A confidence gate runs first, then the
   rules in order, first match wins, then the default. Afterwards the router
   applies the alias's effort cap, any client floor, and the capability filters.
7. Rewrite `model`, forward, and stream the upstream bytes back untouched. If
   the rule named a route rather than a model, and the upstream answers 429 or
   5xx or will not connect, try the route's next entry before any byte has
   been streamed.

If the Jev call fails for any reason, the router falls back to a no-network
decider that guesses from counts, or to the default route. A fallback decision
is never pinned, so the next turn tries Jev again. Jev being down never blocks
a request that has an eligible route. Malformed HTTP-200 Jev answers also fall
back. Impossible hard constraints return HTTP 422 with `type: routing_error`;
recovery never silently discards configured restrictions.

New and replacement pins are committed only after upstream **2xx headers**.
A later stream failure does not undo the pin or retry another model, but is
recorded separately. Reused pins keep their original TTL. Upgrading from an
unscoped key causes one fresh decision per conversation; legacy pins are not reused.

Providers, models and routes are separate things. A provider is one account or
subscription; a model names the provider it belongs to; a route is a named
lane with a primary model and ordered fallbacks. That split is what lets the
router skip a provider that is failing, and lean on one subscription when
another is being spent too fast. See
[Quota-aware routing](quota.md).

## Shadow mode

`settings.mode: shadow` makes the router ask Jev, run the rules and log what it
would have chosen, but send every alias request to `settings.default_route`.
Nothing is pinned. The response carries `X-Router-Shadow-Model`, and the log
row holds the decision with `mode = shadow`. The logged `model` and `effort`
record actual execution; `intended_model` and `intended_effort` retain the
policy's proposed choice.

Run it for a week, read the log, fix the question wording, then switch to
`active`.
