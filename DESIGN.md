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

1. Compute a versioned conversation key from hashed `Authorization`, alias,
   client policy identity, system prompt and first user message. Framed JSON
   avoids delimiter ambiguity. Legacy unscoped keys deliberately do not match.
2. If that key has a pin younger than the TTL, revalidate it against all current
   hard constraints, then skip Jev. A pin is a preference, not a policy exemption.
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
   safe default. The gate may be told to stand down on a named score question
   when its top rubric level holds almost no mass; the shipped config does
   that for `difficulty`, and every other low-confidence answer still
   escalates.
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
degrades routing quality but does not itself block an otherwise eligible request.
Malformed or incomplete HTTP-200 answers are provider failures too. Validation
checks requested IDs, types, vocabulary and finite numeric bounds before policy.
Unsatisfiable hard constraints instead produce a structured 422 routing error.

The fallback decider adds a safety margin to its guessed difficulty score,
because it reads counts rather than meaning and so misses hard work that looks
small. Under-routing is the expensive error: the user gets a bad answer and has
to redo it. Over-routing only costs quota and time.

## Pinning

A valid pinned conversation keeps its model, effort, decision id and original TTL.
Only hard infeasibility permits replacement: changed permissions, tools, vision,
context or effort bounds. Search all eligible models, not just larger windows.
Each replacement gets a new decision id. Quota, 429s and changed task wording do
not trigger reassessment.

Selection is not success. Commit new/replacement pins only after upstream 2xx
headers, never from temporary decider fallback. Failed replacement attempts leave
the original pin in storage; the next request revalidates it again, never blindly
executes it. Stream completion/failure is separate from acceptance. No retry or
pin rewrite follows a mid-stream failure.

Allowed-model intersections, capabilities, context and explicit effort caps/floors
are hard. Conflicting bounds or an empty eligible set are errors. Quota ordering
and stickiness are preferences. A pure eligibility gate checks every execution
entry, including local fallback, shadow defaults and optional drafts.

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

A non-temporary decision accepted with 2xx from a fallback is pinned to that
model, not to the primary. Rejected attempts and network errors create no pin. The alternative is to leave the pin on the
primary and hope, which would send every turn to a model that is failing and
would throw away the serving provider's prompt cache on every turn. The
decision row keeps both: `model` is what served, `intended_model` is what the
policy chose, and `fallback_reason` says why the earlier entries did not.

A pinned conversation has no fallback plan at all. The pin exists to keep one
conversation on one model; moving it on a transient 429 gives up the thing the
pin was for. Only hard incompatibility permits changing it.

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

Pressure belongs to a provider, so two models on the same subscription used to
be equally pressured and could never relieve each other. Each model now has a
`quota_cost`, its share of the provider's allowance against the most expensive
model there, and an entry's pressure is the provider's times that share. On a
busy OpenAI subscription astra is over the threshold while sol, at a fifth of
the price, is not, so an `equivalent` sol entry can take astra's place. The
share can only lower a model's pressure, and it comes from published prices,
never from what would make a route win. The shipped `auto` uses both kinds of
move: cheaper OpenAI models when Ollama is busy, glm-5.3 and sol when OpenAI
is, each only where the admission test found the pair equally good.

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

## A binding is a contract, not a cache entry

A pin is a preference. It says "this conversation went to that model last
time, and reusing the answer is cheaper than asking again". Everything about it
follows from that: it expires, it is revalidated on every turn, and hard
incompatibility replaces it.

A coding session needs something different, and the difference is not a matter
of degree. The harness has already sized its history against one model's
context window. It holds a prompt cache at one provider. It has been told, in
the shape of a profile it configured itself from, what it may send. A pin that
quietly becomes a different model has broken three promises the harness made to
itself on the router's word.

So a strict binding is a contract. The router says what the profile is, the
client acknowledges it by revision, and from then on neither side may change it
alone. That one decision settles most of the behaviour without further argument:

- **No TTL.** A contract does not expire because time passed. It ends when the
  client closes it, when it is pruned, or when a contract that was offered and
  never used goes stale.
- **No repin.** Overflow, a lost capability, a reduced configured limit: every
  one of these reports a machine-readable code and keeps the binding. The
  alternative is to move the work silently, which is the thing the contract
  exists to stop.
- **No cross-model fallback.** One entry in the execution plan. A 429 is
  reported to the caller on the bound profile. A route's fallback list is a
  legacy-path idea: it exists because a per-conversation pin can afford to land
  somewhere else, and a contract cannot.
- **No quota influence after admission.** Pressure chose which profile to
  admit. After that it has no say at all.

The two live in separate tables and neither reads the other, so none of this
leaks into the legacy path. An alias with no `session_mode` behaves byte for
byte as it did.

What a contract cannot do is police the other side of itself. The router is an
HTTP shim: it cannot tell genuinely new work from a fork of an old thread
beyond the history evidence in front of it, it cannot see a tool loop, and it
does not own the transcript. Those are named as trusted-client assertions
rather than pretended away, and the ones the router *can* check — the binding
revision, the request id, the context budget, its own in-flight state — it
checks on every request.

## Capability is not adequacy

Quota pressure was originally bounded by two things: a `max_shift` on a
threshold, and an `equivalent: true` flag on a route entry. Both bound *how
far* pressure may move a decision. Neither says anything about *where it may
land*.

That gap matters as soon as the pool has more than two models. A model that
declares `supports_tools: true` is capable of a tool-driven coding task in the
sense that the request will not be rejected. Whether it can finish one is a
different question, and the only honest answer comes from having run it.
Filtering on capability and then letting pressure pick among the survivors
quietly treats the first question as an answer to the second.

So a rule may name a **quality lane**: the set of model and effort pairs
somebody measured for that kind of work, each carrying a reference to where the
evidence is. Pressure may then move a request only between pairs inside the
lane. A threshold shift or an `equivalent` promotion that lands outside it is
refused, the adequate provider is kept, and the decision records the deferral
rather than taking it quietly. A capability replacement is drawn from the lane
too, and if the lane has nothing eligible the request is refused rather than
served by a model nobody measured — a hard constraint is never recovered from
by lowering the floor.

A lane entry with no `qualification_ref` is refused at config load. That is the
load-bearing part. Without it a lane is just another list of model names, and
the mechanism becomes a place to write down an opinion and have the router
treat it as a measurement.

A route's failure fallbacks are deliberately not lane members. Reaching one
after a 429 is not a claim that it was good enough; it is what happened when
the good one would not answer. In the shipped config that distinction is what
keeps `grok-4.6` a frontier fallback and never a quota saving.

The counterfactual is recorded with an honesty flag. The useful claim is "this
task met the same adequacy requirement under both policies, and pressure chose
between two qualified options". When there is no lane, or when one of the two
pairs is not in it, that claim cannot be made, and the row says `evidence:
weak` instead of implying it.

## Measuring is not deciding

The cheapest way to find out whether a new question helps is to ask it in the
call that is already being made and see what it says. The trap is that a
question you can see is a question you will be tempted to use, and once one
answer in the packet can reach the route, "we are only measuring it" stops
being true.

So shadow answers are separated four ways, not one. They live in a different
field on the result. They are validated one at a time by a different function,
so a malformed shadow answer is dropped and counted rather than failing the
packet, and — the direction that matters more — it can never satisfy or weaken
a requirement the active side has. They carry their own packet version. And
the routing and effort paths are handed the active answers only.

The fourth separation needed stating explicitly, because it is the one that
looks like an exception. The between-turn effort policy has a shadow question,
`failure_mode`, whose job would be to *hold an upgrade back* when the obstacle
is a missing credential rather than a hard problem. Suppressing a change is a
decision like any other, so a shadow answer may not do that either. What it
would have done is computed and stored on the plan as a counterfactual, with
the action, the rung and whether it differed. That is how the question earns a
promotion: move it from `shadow_questions` into `questions` and it arrives as
an active answer like any other.

Two config-load rules keep the arrangement from rotting. The active and shadow
sets may not overlap, and an alias that routes on a question may not also be
measuring it. A question cannot be both the answer and the experiment.

## Effort is not model identity

A strict binding says the model never changes. Between-turn effort changes the
reasoning effort of that model. Those two statements are only compatible
because effort and model identity are different things, and the design leans on
that rather than carving out an exception.

What the harness configured itself from is the profile: the model, the
provider, the context window, the output ceiling, what it supports. None of
that moves. The prompt cache is keyed on the history, which also does not move.
Effort is a setting on a request. Changing it costs nothing the binding
promised.

Three consequences follow. The turn boundary calls `classify`, which returns
answers, rather than `decide`, which runs the cross-model policy — there is no
model choice for a turn assessment to reach. `effort.plan_turn` is a pure
function from a ladder and some evidence to a rung, and it has no way to name a
model. And the ladder a turn may move on is narrowed first by the client's
floor, the alias's cap and the quality lane the admission rule named, so a
turn-level decision cannot escape a session-level one.

The asymmetry between up and down is about where the cost of being wrong lands.
An upward change goes straight to the rung the evidence asks for, because
underserving a turn costs that turn's quality and is corrected at the next
boundary. A downward change moves one rung and needs several consecutive
recommendations, because a drop that is too deep is only discovered after the
weak answer has been given. Confirmations are a way of paying for information
in the direction where the mistake is expensive.

The first implementation climbed one rung per turn in both directions, which
meant a turn nobody would call anything but hard was served at `medium` and
reached `high` only if the next turn was hard too. The owner overruled that on
2026-09-19. The old mechanic is kept as a replay arm rather than deleted, so
the two can be compared rather than asserted about.

## Planned, accepted, confirmed

The reply to a request that carried an effort update can tell you three
different things, and running them together produces a ledger that lies.

- **2xx headers** say the transport accepted the request. They say nothing
  about whether the provider read the item, honoured it, or ignored it.
- **A terminal provider completion** says the request ran to the end and what
  it did. This is the only thing that can confirm a transition.
- **A reply's own `reasoning.effort`** reports the *base* effort — the
  request-level field the client is required to hold still. Reading it as the
  effective effort would make the two impossible to tell apart, which is
  exactly why the client is required to hold it still.

So `turn_plans.status` has five values and the session row keeps expected and
confirmed effective effort in separate columns. Only `confirmed` moves the
effective effort. A plan that was accepted and never completed leaves the
confirmed value where it was, and it blocks the next change rather than being
guessed at.

`outcome_unknown` is the honest answer to a disconnected response, and it is
the one a system is tempted to round off. The request went out; nobody can say
what ran. Rounding it down loses an update the provider may hold. Rounding it
up claims a transition that may not have happened. So it blocks the next change
until a person who can check the provider says which it was, and
reconciliation is legal only from `accepted` or `outcome_unknown` — the two
states where the provider had the request either way. A `planned` plan was
never sent, so no hand-written `applied` could be true, and a `confirmed` or
`rejected` one already has its answer: a second would rewrite a fact rather
than supply a missing one.

An `applied` reconciliation leaves a hole, and saying so is better than
papering over it. The router never saw where the client put that item. So
execution carries on, a replay is still checked for everything that can
honestly be checked, a chain must still not resend it, and the *next* effort
transition is blocked, because a new update's anchor would be measured against
a history with a gap in it.

## Compaction epochs

The first design refused compaction outright, which is defensible for a
protocol experiment and wrong for a client. Codex compacts. It has always
compacted. Refusing it means the experiment can only run on short sessions,
which are the sessions where the experiment matters least.

The owner's decision on 2026-09-19 was that a compaction the client asks for
stays exactly what the client does. The router passes it through and records
that it happened. It still never compacts, summarises, translates or rewrites a
provider item, and never changes model. `truncation: auto` and the
automatic-management fields are still refused, because those rewrite a history
with no request of their own for the ledger to see.

What compaction breaks is anchoring. Every applied update is recorded with a
position and a prefix hash of the history in front of it, because repeated
identical user text is not a unique anchor. After the history is folded up
those positions name nothing. Codex also declares a different tool set on a
compaction request, so the very first item's id changes and every prefix hash
behind it changes with it.

An epoch is the smallest thing that fixes this. It is a counter on the session,
stamped on every plan. Crossing it relaxes what a later request is checked
against and nothing else:

- Updates from earlier epochs stop being position-validated and stop being
  required in a replay. They stay in the ledger; they are simply not asked for
  again.
- Everything since the last compaction is still checked exactly as before.
- Expected and confirmed effective effort go back to the base effort, because
  that is what the measurement says actually happens: replaying a compaction
  built from a history that carried an update to `high` produced the same
  reasoning work as the base arm.
- The next eligible turn may plan the effort again, which is the reapplication.

The epoch moves on an accepted compaction rather than on a confirmed one,
which looks like a violation of the previous section and is not. Moving it only
ever *relaxes* a check. If the compaction went out and nothing came back, the
session is marked `compaction_state: unknown` and further effort changes stop,
exactly as they do for a reply nobody could read. The router loosens what it
demands of the client and tightens what it will do itself, which is the safe
way round.

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

One SQLite file holds pins, a decision log and feedback, and, for strict
sessions, the bindings, the per-attempt request records and the effort ledger.
The log keeps Jev's answers with their confidences, the computed counts and
flags, the rule that fired, and a hash of the config that produced the row. It
never keeps message text, prompts or API keys. The conversation key, the
session owner and every request fingerprint are hashes and are not reversible.

That is enough to replay the policy against past decisions, which is what
`evals/tune.py` does — and, one row at a time, what `decisions replay` does.
Replay is why the pressure a decision was taken under is stored beside it: a
deliberate quota saving replayed at pressure zero would look like a routing
change. It is also the constraint that keeps the log honest. If a decision has
to be reproducible from its own row without the prompt, then everything the
policy read has to be in the row as counts and flags, and nothing the policy
read can be the prompt itself.

Migrations are forward-only and additive. A new column is nullable, defaults to
NULL, and is added the next time the router opens the file; nothing is dropped,
rewritten or retyped. Reads are `SELECT *` into a dict and writes name their
columns, so an older build against a newer file ignores what it does not know
and writes NULL where it has nothing to say. That is what makes a rollback a
checkout rather than a restore.

## Open questions

- Jev's accuracy on code-heavy requests is measured only against a synthetic
  case set. Shadow mode on real traffic would say more.
- TypeSafe publishes no latency guarantee. The default timeout is a guess based
  on the measured 95th percentile of 0.25 s.
- A client that declares per-model settings (context window, reasoning efforts)
  needs the most conservative values of any model an alias can choose. A
  strict alias can answer that with a static envelope; a legacy alias cannot.
- `/v1/responses` and `/v1/messages` could learn aliases. They pass through
  today, except that a strict session may execute on `/v1/responses` when its
  bound profile speaks that protocol. The router converts nothing either way.
- Whether changing effort between turns produces better work than leaving it
  alone is unmeasured, at every rung. The protocol has been tested on one
  deployment, provisionally. That is a different question and the reports say
  so.
- Multi-process deployment. The one-request-per-session claim is held in
  memory, so two services sharing a database would not see each other's
  claims. The router refuses the second one inside a process and cannot see
  another process.

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
