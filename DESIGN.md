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

## Fallbacks and quota

Two problems sit beside routing and are not the classifier's business. A
provider can be down or rate limited. And a provider can be a flat-rate
subscription with a usage window, being spent faster than the window lasts.
The classifier should not know about either, so both are handled after the
decision, by rules in `router.yaml` and by code that never touches a request's
bytes.

### Fallbacks

A rule may point at a named route instead of a model. A route is a primary and
an ordered list of fallbacks. The forwarder moves to the next entry when the
upstream answers 429 or any 5xx, refuses the connection, or times out before
the first response byte.

Nothing is retried after a byte has reached the client. That is not a policy
so much as a fact about how the forwarder is written: the choice is made on
the response status, which arrives before anything is streamed, so once
streaming starts the model is fixed. It has to be. The client already holds
part of an answer, and two answers cannot be spliced together.

A 4xx is not retried either. A malformed request is malformed at every
provider, and retrying it spends a second provider's quota to get the same
error.

A conversation served by a fallback is pinned to the model that actually
answered, not to the primary. The alternative is to leave the pin on the
primary and hope, which would send every turn to a model that is failing and
would throw away the serving provider's prompt cache on every turn. The
decision row keeps both: `model` is what served, `intended_model` is what the
policy chose, and `fallback_reason` says why the earlier entries did not.

A pinned conversation has no fallback plan at all. The pin exists to keep one
conversation on one model; moving it on a transient 429 gives up the thing the
pin was for. The pin rule already has one exception, context overflow, and one
is enough.

### The circuit breaker

Sending a request to a provider that has failed three times in a row does not
produce an answer, it produces a delay and then a failure. So the breaker
opens per provider, the router skips that provider's models while it is open,
and after a cooldown it lets exactly one request through to find out whether
the provider is back. The cooldown doubles on each reopen so a long outage is
not probed every two minutes.

The state is in memory. It is a fact about the last few minutes, it is
per-process, and persisting it would mean a restart inherits a stale opinion
about a provider that has since recovered.

One exception: a provider that is the only entry left is tried anyway. A
breaker that is wrong should cost one wasted call, not a refused request.

### Pressure from pace, not from percent

Raw usage percent is the wrong number to route on. A subscription at 80% of a
weekly window on Friday is fine. The same 80% on Monday morning is not. So
pressure compares the percent spent against the percent the window says should
be spent by now: the provider's own pace figure when it reports one, otherwise
one derived from the window length and the reset time.

Pressure is a number in [0, 1] from a pure function. That matters for two
reasons. It can be tested without a clock or a subprocess. And `evals/tune.py`
can replay a decision taken under pressure at the pressure it was really taken
under, instead of scoring a deliberate saving as a classifier error.

Hysteresis and a per-poll clamp were added because a threshold that moves with
a measured number will otherwise chatter across a knee, and because one bad
reading should not swing routing in a single step.

### Bounded, monotone, one-directional

Pressure is allowed to move a rule's threshold, and nothing else about the
rule. The shift is `pressure × max_shift`, so it is bounded by a number in the
config, monotone in pressure, and always in the direction that makes the
pressured provider harder to reach. It can never route more work towards a
provider that is running out.

The alternative — letting pressure pick the model directly — was rejected. It
would put a second, invisible router behind the one in `router.yaml`, and the
reason a request went somewhere would stop being readable from the rules.

### Protected rules

Some rules exist because the request needs them, not because the work looked
hard: an image needs a model with vision, a request where a small mistake
causes real harm needs the careful model, a conversation that outgrew its
context window has to move. Those rules are marked `protected` and ignore
every shift. Quota is a budget. It is not a reason to answer a medical
question with the cheap model.

A protected rule still uses its route's fallbacks. Being protected from a
budget is not the same as being protected from an outage.

### Equivalent-only promotion

Reordering a route within itself is safe when the entries are interchangeable
and unsafe when they are not. So an entry may only be promoted ahead of the
primary when the config marks it `equivalent: true`, which is a claim about
quality that a person has to make deliberately. Without the flag a fallback is
still reachable, but only by failure. Saving quota is never a reason to answer
with a weaker model than the rules asked for.

### Pins are untouched

Quota affects new decisions only. A conversation that is already pinned keeps
its model whatever the numbers say. Rerouting mid-conversation is what the
whole design avoids, and doing it to save quota would be the same mistake for
a worse reason.

### Fail-neutral everywhere

A provider with no quota source, a source that is disabled, a command that
fails or times out, a field missing from the output, a snapshot older than
`stale_after_seconds`: every one of these means pressure 0 for that provider,
which routes exactly as the router routed before quota existed. Polling
happens in a background task and never on the request path, so a slow command
cannot add latency to a request. A source that raises is caught and recorded
as an error on the snapshot.

The shipped `router.yaml` has every source disabled. Somebody who clones this
repository and runs it gets no subprocesses and the same routing the tuner
measured.

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

## Related work

The router-evaluation literature was the source of most of what
`evals/metrics.py` measures. The references below are the well-known ones and
they are worth reading before changing the harness.

- **RouteLLM** (arXiv 2406.18665) frames the two-model routing problem and its
  cost-quality trade-off, and is where the habit of reporting a router against
  both single-model endpoints comes from.
- **RouterBench** (arXiv 2403.12031) contributes the Zero Router: a baseline
  that sends a request to the strong model with some fixed probability, so a
  router can be compared against random allocation at the same spend. That is
  the `cost-matched random` row in every results table here, and it is the
  baseline that most changes what the numbers look like.
- **FrugalGPT** (arXiv 2305.05176) is the cascade: try a cheap model, judge the
  answer, escalate if it will not do.
- **Hybrid LLM** (arXiv 2404.14618) routes on a predicted quality gap between
  the two models rather than on a property of the request.
- **AutoMix** (arXiv 2310.12963) self-verifies a cheap draft before deciding
  whether to escalate. `aliases.<name>.draft` is that idea with the verification
  done by the decision model instead of a prompted LLM. It is off by default.
- **Rerouting LLM Routers** (arXiv 2501.01818) shows that a short,
  query-independent token prefix can move a router's decision without changing
  the request. The `gibberish_prefix` attack vector in `evals/cases.yaml` is
  built from that finding.
- **Adding Error Bars to Evals** (arXiv 2411.00640) is why every rate here
  carries a Wilson interval and why experiments are tested paired on the same
  cases rather than by comparing two headline numbers.
- **WildChat** (arXiv 2405.01470) is the real-traffic sample, under ODC-BY.
- **"Do NOT Think That Much for 2+3=?"** (arXiv 2412.21187) documents
  overthinking on easy problems, which is the mechanism behind the
  cost-escalation attacks and behind the outcome benchmark's finding that
  raising effort makes some answers worse.

### Where this project differs

Three things this router does are not what the papers study, and each one
changes what an evaluation has to measure.

**It pins one route per conversation.** The papers route per request. This
router decides once and reuses that decision for the rest of the conversation,
because a chat that changes model mid-thread loses its style and its tool
habits, and because a per-turn decision costs a classifier call per turn. The
consequences are all on the evaluation side: accuracy has to be measured on
the turn that triggers the decision rather than on every turn, and decision
stability matters more than it does for a per-request router, because one wrong
decision is inherited by every later turn. It is also a defence: an injection
that arrives in turn four cannot move a conversation pinned at turn one.

**It routes reasoning effort through a proxy it does not control.** The
literature routes between models. Here the second axis is how much the chosen
model is told to think, expressed as a suffix on the model name or a
`reasoning_effort` field, depending on what the upstream accepts. Nothing
downstream reports how much reasoning actually happened, so effort can only be
evaluated end to end, by running the same request at several efforts and
grading the replies. That is layer 3, and it is why the outcome benchmark
records `finish_reason`: a reply cut off at the token cap looks exactly like a
route that did not help.

**Its classifier returns probabilities, not a route.** RouteLLM and Hybrid LLM
train a model to predict the routing decision or the quality gap. Here a
general decision model answers typed questions about the request, and rules in
`router.yaml` turn those answers into a route. The classifier never sees the
model list, the prices or the outcome labels, so it cannot be fitted to them,
and swapping a model is a config edit rather than a retraining run. The cost is
that everything the classifier knows has to be said in a question, and the
questions are the main lever on accuracy. It also puts a confidence on every
answer, which the papers mostly do not have, and that confidence turned out to
be both useful and dangerous: the largest single accuracy change in the second
round of experiments came from loosening a gate that was trusting a confidence
the calibration numbers said not to trust.

A fourth difference is smaller but worth stating. The evaluation here is not a
benchmark of models. It is a benchmark of one deployment: two specific models
behind one specific proxy, with a cost model that was made up. Numbers from it
do not transfer to another pair of models, and `ROUTE_COST` in
`evals/common.py` should be the first thing anyone changes before reusing this.
