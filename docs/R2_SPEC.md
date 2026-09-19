# jev-router: semantic, quota-aware session routing

**Specification:** R2 — core product and implementation specification  
**Date:** 2026-09-19  
**Repository baseline:** `hyspacex/jev-router@a248b889b4511c154c2bb6d7410223c76a0fb8f7`  
**Status:** Proposed; not an implementation or a claim that the new tests have run.  
**Supersedes:** `JEV_ROUTER_PI_SESSION_SPEC.md` as the product specification. Client-specific examples may be written separately, but do not define this product.

MUST/MUST NOT are acceptance requirements. SHOULD describes the intended default. Illustrative thresholds and budgets are engineering starting points, not measured findings. All new endpoints, configuration fields, and commands below are proposed additions unless explicitly described as existing.

## 1. Product purpose and architectural decision

Build a tool that helps one developer complete more satisfactory coding work with the subscriptions and models already available to them. The primary goals are preserving quality, improving responsiveness, and preserving scarce provider capacity for work that benefits from it. Maximizing utilization for its own sake is not a goal.

**jev-router owns semantic interpretation, routing policy, quota-aware selection, durable routing bindings, optional effort decisions, and the evidence needed to evaluate them.** It remains a small Python service and CLI in front of existing execution paths. Pi, Codex, or another harness is a client, not the architectural center.

The central distinction is:

- **Model/provider identity is sticky for an execution session.** Automatic changes happen when admitting new, independently executable work, not inside an existing session.
- **Reasoning effort is a separate control.** It is fixed by default. Experimental, supported, between-turn changes may occur without replacing the model or provider.
- **Task interpretation is not resource arithmetic.** Jev describes what the work requires. Code handles capacity, reset times, context limits, permissions, and allowed actions.
- **A stronger classifier is not automatically a better router.** Features and policies earn promotion through downstream outcome evaluations.

A useful day looks like this: an existing Codex session continues undisturbed; newly admitted mechanical jobs use Flash; harder work retains a qualified strong model; Grok is used only in lanes where its observed adequacy permits it. A supported strong-model session may experimentally use less effort on a routine new user turn and more on a demanding one. None of these decisions requires hiding a cross-model migration from the harness.

## 2. Scope: keep the moving parts small

### 2.1 Ship in the stable core

Reuse the current HTTP service, CLI, configuration, SQLite store, feature extraction, Jev validation, policy functions, provider health, quota sources, and evaluation scripts. Add:

1. A strict durable session contract, alongside the explicitly retained legacy HTTP-alias behavior.
2. A harness-neutral resolution endpoint that returns the selected execution profile and accurate limits before the client constructs its first execution request.
3. A small semantic feature packet with active and shadow questions, plus distribution-aware policy experiments.
4. Session-level execution and outcome evaluation, separate from existing request-classification scores.

### 2.2 Ship behind experimental gates

One between-turn effort controller with `off`, `shadow`, and `active` modes. First active native transport: a verified GPT-6 Astra HTTP Responses path that preserves configuration updates correctly. A parameter-changing transport may be evaluated separately, but does not inherit a cache-preservation claim.

### 2.3 Explicit non-goals for this revision

Do not build a new agent runtime, Pi fork, credential broker, universal API translator, custom compactor, automatic handoff generator, parallel-worker scheduler, learned quota-demand forecaster, vector database, dashboard, online reinforcement learner, or automatic prompt-optimization loop.

The router may later admit an independent worker session created by a harness. This revision does not create that worker or decide its filesystem permissions. Failure diagnosis and delegation suitability may be shadow features, not authority to execute actions.

**Deployment scope:** one local owner, one service process, one SQLite database. Multi-tenant hosting and multi-process coordination are out of scope. No Redis, queue, or distributed lock service is required.

## 3. What the baseline already fixes—and what changes deliberately

The baseline is the September 19 hardening commit. Preserve its raw encoding behavior, bounded capture, Jev response validation, hard eligibility, accepted-versus-completed distinction, actual served metadata, local endpoint security, ingress limits, retention tooling, and offline CI. Source inspection, not a new test run, established this baseline. [S1–S4]

The patched legacy route still revalidates pins and can replace an incompatible model; it uses TTL-based conversation identity and creates pins after accepted upstream headers. Those remain legacy behavior. [S2]

**Strict-session routing introduces a different contract:** no automatic replacement on overflow, changed capabilities, a timeout, or classifier recovery. A strict binding is not a TTL cache entry. Existing legacy tests remain valid; add strict-mode tests rather than weakening those fixes.

For compatibility, an omitted new `session_mode` field means `legacy` for existing configurations. Ship an opt-in strict example alias such as `auto-session`. A user may later make `auto` strict deliberately. Do not silently migrate an old conversation to the new semantics during upgrade.

The current model IDs and context limits in `router.yaml` are deployment configuration, not independently verified capabilities of every endpoint exposing the same model name. In particular, a suffix-style proxy path is not evidence of native Responses configuration-update support. [S5]

## 4. Concepts and invariants

### 4.1 Terms

**Execution profile:** one configured provider/account, upstream model identity, wire protocol, context/output limits, effort encoding, and compatibility declaration. Existing provider/model records are extended; do not create a second independent model catalogue.

**Session:** one continuous execution history. It is not equivalent to a process, HTTP connection, first-user-message hash, or one user turn.

**User turn:** a user instruction plus all assistant/tool continuations needed to handle it until the harness reports settled. Here, a tool-result follow-up is a continuation, not a new effort-selection opportunity.

**Binding:** the router's durable association between a strict session and an execution profile. Model/provider identity is immutable. Effective effort may change under the separate experiment.

**Quality lane:** a class of work for which specific model/effort pairs have qualification evidence. Hardware capabilities and quality adequacy are different filters.

### 4.2 Required invariants

| ID | Invariant |
|---|---|
| I01 | Hard permission, protocol, modality, output, and context constraints are never relaxed to recover from failure. |
| I02 | A strict session's model and provider/account do not change automatically after resolution. |
| I03 | Quota pressure never changes the semantic answers to an otherwise identical task. |
| I04 | Every permitted effort change keeps the same execution identity and occurs only at a verified new-user-turn boundary. |
| I05 | Effort changes are off by default; unsupported or unverified deployments cannot activate them. |
| I06 | Client-advertised limits and effective execution limits agree. A metadata mismatch is an error, not a hidden substitution. |
| I07 | Jev failure at admission chooses an explicitly permitted conservative fallback or returns an error. After binding, it never creates a new model choice. |
| I08 | Resolve success, execution activation, accepted headers, transport completion, protocol completion, and task success are distinct facts. |
| I09 | Routing metadata, quota readings, timestamps, and explanations do not modify the historical prompt prefix. |
| I10 | No mid-stream cross-model retry; no automatic replay of an operation with an ambiguous execution outcome. |
| I11 | Opaque provider items and tool-result relationships are not translated, dropped, or summarized by the generic router. |
| I12 | Provider capacity, usage coverage, and cache measurements may be unknown; unknown is not zero. |
| I13 | Every active decision is reproducible from versioned configuration, semantic output, request facts, and quota snapshot, without requiring the original prompt in persistent logs. |
| I14 | Eval outcomes are not relabeled merely to make a newly admitted model or policy score better. |

## 5. Minimal architecture

```text
Client / harness / script
      |
      | resolve a fresh session; obtain concrete execution metadata
      v
jev-router
  features/state -> Jev semantic packet -> deterministic policy
                                    + model qualification
                                    + quota/health snapshot
      |
      | durable execution binding in the existing SQLite store
      v
Existing forwarding path -> configured upstream proxy/provider
      |
      +-- established session: same model; no admission classifier
      +-- experimental new user turn: effort-only plan, when supported

Existing evals -> compare decisions AND completed task/session outcomes
```

The service remains in the execution path for managed sessions, including requests whose `model` is already concrete. Recognize strict-session headers before the legacy "non-alias means passthrough" branch. Otherwise strict sessions would bypass policy and observability.

Clients own the canonical transcript, actual tools, context compaction, and display. Their generic responsibilities are to supply a stable session ID, acknowledge resolved metadata, and, only for the effort experiment, supply reliable boundaries and preserve supported protocol updates. They do not duplicate Jev questions, quota calculations, or model-selection policy.

## 6. Execution profiles and context contracts

### 6.1 Extend the existing model configuration

Add optional fields to `ModelCfg` sufficient to identify the deployed protocol and output ceiling. Keep existing `provider`, `upstream_id`, `context_window`, `supports_tools`, `supports_vision`, and effort definitions. A suggested shape is:

```yaml
models:
  gpt-6-astra:
    # Existing provider, upstream_id, context_window and effort fields remain.
    protocol: openai-responses
    max_output_tokens: 16384
    compatibility_revision: responses-astra-deployment-v1
    effort_control:
      between_turn: native_configuration_update
      qualification: unverified
      cache_behavior: unverified
      qualification_ref: null
```

This is a schema example, **not** an instruction to relabel the current suffix proxy as Responses. If its protocol remains Chat Completions, configure that honestly. Output ceilings are operational caps, not assertions about a model family's absolute maximum.

Allowed protocols for this revision are `openai-chat` and `openai-responses`. The stable first implementation may support strict forwarding only for `openai-chat`; non-session Responses passthrough remains unchanged until the experimental native path passes qualification. Return `UNSUPPORTED_PROFILE` rather than converting formats.

`qualification` is `unverified` or `verified`. Verification references a checked-in, dated deployment test report. Qualification must be for the model + endpoint/proxy behavior + adapter revision, not model name alone. A provider login refresh must not change the account/profile identity; choosing a different account does.

### 6.2 Preferred context contract: resolved model metadata

Before the first execution request, the client calls `POST /router/resolve`. The response returns one fixed profile, concrete wire identity, protocol, context window, maximum output, modalities, initial effort, and a `binding_revision` digest.

The client configures its actual model object from that contract, then sends execution requests through jev-router with the session and binding revision. A routing response header alone is not sufficient if the client already constructed a request using different metadata.

A client may constrain the candidate set and request lower limits. The router intersects those constraints with configured permissions. The **negotiated** limit is returned and acknowledged before execution; the router never lowers its internal limit while leaving the client believing a larger value.

Example: Flash resolves to the configured 128,000-token profile, while a frontier profile resolves to 400,000. Those clients do not need to pretend both models have the same window. These are current repository examples, not externally verified product specifications. [S5]

### 6.3 Limited static `auto` compatibility

A client that cannot resolve dynamic metadata can use a separately qualified homogeneous pool. Its advertised envelope is the intersection of all possible profile capabilities and the minimum supported context/output ceilings. Register those values explicitly in that client.

Do not advertise the maximum pool window. Do not pool different wire protocols under one static model descriptor. No candidate or first-request fallback may fall outside the envelope. The effort experiment is disabled for this mode unless its client meets the full experimental contract independently.

This sacrifices large-window capacity and can cause earlier compaction. It is an intentional compatibility mode, not the recommended way to conceal heterogeneous models. Stable session IDs remain required for long-lived strict sessions. Existing hash-based legacy aliases are not advertised as equivalent safety.

### 6.4 Token accounting

Define `C` as negotiated context capacity, `O` as the execution output allowance including any reasoning budget sharing that ceiling, `I` as estimated full serialized input, and `S` as an uncertainty reserve. Admission requires `I + O + S <= C` for shared-window profiles.

An adapter with different input/output semantics must explicitly provide its own tested budget check. Do not blindly apply a shared-window equation to an incompatible API.

Include instructions, tool schemas, assistant/tool content, provider framing, and known multimodal overhead. Prefer tokenizer/provider counts when available without an extra execution call. Reuse the existing estimator initially with explicit uncertainty; do not market characters/4 as an exact guarantee. Unknown image or opaque-state size excludes automatic admission near the capacity boundary.

A provisional reserve is `max(4096, ceil(0.10 * estimated_input))`, with an explicit client-requested larger reserve allowed. This is an engineering heuristic requiring multilingual and tool-schema tests, not a validated error bound. Record estimate method and reserve.

Full history may be absent with `previous_response_id`. In that case require a trustworthy reported accumulated-usage estimate plus new-input allowance; a short delta is not the entire context. Without sufficient information, use the client's agreed conservative context contract or reject the request as unverifiable near the boundary.

### 6.5 Context growth and limits changing

The harness compacts using the resolved model's limits. The router does not compact, increase the window, or pick a larger model automatically. Overflow produces `CONTEXT_BUDGET_EXCEEDED`, with the existing binding preserved.

If the configured permitted capacity later decreases below a binding's negotiated contract, or the profile is revoked, stop with `PROFILE_CHANGED`/`PROFILE_REVOKED`. Do not continue using an invalid limit, silently clamp it, or migrate the session. The caller must deliberately reconfigure, compact, or create a separate session.

Higher newly available capacity does not silently enlarge an established session. Tool/vision changes are revalidated against the same profile. Unsupported input is reported, never silently removed.

## 7. Jev's job: a reusable semantic packet

TypeSafe documents typed Choice/Score/Noul outputs and independent parallel evaluation of questions against one state. That makes a small packet of atomic judgments worth investigating, without requiring a generative explanation. It is a vendor-described capability, not proof of superior routing on this workload. [S6]

The design hypothesis is **fast, editable semantic features plus deterministic policy**, rather than Jev knowing the best current model from its internal knowledge. No claim of unique functionality or universal superiority is required.

### 7.1 Versioned state, assembled in code

Build one bounded state packet at admission; optionally another at an experimental new-turn boundary:

```json
{
  "schema_version": "coding-state-v1",
  "event": "admission",
  "task_request": "Implement quoted CSV fields and add regression tests.",
  "current_user_request": "Implement quoted CSV fields and add regression tests.",
  "user_constraints": [],
  "quoted_material": [],
  "observations": [],
  "facts": {
    "has_tools": true,
    "has_images": false,
    "estimated_input_tokens": 4200,
    "turn_history_available": false
  }
}
```

The literal user request and material can be processed transiently; they are not persistently stored by default. Truncation is deterministic, disclosed in facts, and never replaces a capacity count with the truncated Jev-state size. No extra summarizer call is required.

Separate task instructions, quoted material, and trusted execution observations. A statement inside a pasted file that "this is easy" is not a routing instruction. An assistant's claim that tests pass is not a verified observation. Preserve provenance (`harness`, `user_report`, `assistant_claim`) in observations.

Keep provider names, prices, live quotas, and candidate rankings out of the semantic questions. Numeric counts remain in code. The Jev limitations documentation specifically cautions about counting, arithmetic, indirect tasks, and contextual sensitivity. [S7]

### 7.2 Initial question set

Retain the existing three active question definitions as a reproducible baseline. Do not rewrite them while attributing gains to a new policy feature. Add three narrowly scoped admission questions in shadow mode:

| Question | Type | Purpose / wording target |
|---|---|---|
| `task` | Choice | Existing work-type question. Diagnostic and lane input. |
| `difficulty` | Score | Existing descriptive reasoning-demand levels. Retain its full distribution. |
| `harm_if_wrong` | Noul | Existing consequence signal; never the only safety boundary. |
| `mechanical_transform` | Noul | Is the requested edit specified as an explicit transformation, without choosing a new algorithm or architecture? |
| `interacting_constraints` | Noul | Does the supplied task require preserving a stated relationship across interacting components or requirements? |
| `requirements_missing` | Noul | Does the supplied request leave necessary desired behavior unspecified? Do not call ordinary repository inspection a missing requirement. |

For experimental turn assessment add `corrective_followup`: does the latest user message explicitly identify a substantive defect in the earlier result? When grounded failure observations exist, an optional shadow `failure_mode` Choice uses `reasoning_problem`, `missing_information`, `environment_problem`, `unclear`, and `no_failure_evidence`.

Do not ask every optional question on every event. One active+shadow batched call is preferred when testing speculative features; measure whether the bigger packet changes latency, error rate, and active-answer stability. If malformed experimental outputs would compromise active validation, fail the whole packet conservatively or isolate the shadow run. Never weaken the baseline validator to rescue an experiment.

### 7.3 Question-writing requirements

Each question names the fields it judges. Criteria describe observable situations, include boundary cases, and are meaningful independently. Questions do not refer to another answer that Jev has not seen. Multiple judgments are composed in code. `requirements_missing` is not the logical negation of a differently worded "requirements clear" question.

Retain `unknown/unclear` choices or explicit missing-evidence facts where appropriate. Absence of evidence is not automatically evidence that a job is safe or easy. Every question version is hashed with the state builder and Jev model version.

### 7.4 Distribution-aware policy experiment

A Score is a probability-weighted average over rubric levels; the same average can hide different distributions. [S8] Preserve the vector rather than just `score` and `confidence`.

For the existing four-level difficulty rubric, compute in Python:

```python
p_nontrivial = p["2"] + p["3"]
p_hard = p["3"]
```

Experiment with rejecting a cheap route when upper-level probability mass exceeds a tuned threshold, even if the mean looks moderate. Conversely, uncertainty between two labels that share the same action need not cause escalation.

An illustrative rule may require `mechanical_transform >= 0.8`, `interacting_constraints <= 0.2`, and `p_hard <= 0.1`. These are **candidate cutoffs for a tuning grid**, not shipped truth or a product guarantee. Compare them against the existing mean-score policy on frozen outcomes.

Do not multiply separate Noul answers into a pseudo-probability of successful code. Parallel independent evaluation is not evidence of statistical independence. Jev confidence describes its answer distribution; downstream model adequacy must be measured. [S9]

### 7.5 Active versus shadow

Each decision stores active answers and shadow answers distinctly, plus the selected route with and without each experimental policy. A shadow field cannot affect execution until explicitly enabled in a versioned ruleset after evaluation.

New features must demonstrate a downstream benefit at fixed model pool and workload. If they do not, remove them from the default packet. Low inference cost is not a reason to accumulate unused signals.

## 8. Deterministic selection and quota policy

### 8.1 Selection order

Implement one pure selection function used by HTTP, CLI, and eval replay:

1. Enforce configured client/provider permissions and protocol/capability/budget constraints.
2. Construct quality-adequate candidates using qualified lanes and explicit model/effort rules.
3. Apply relevant semantic uncertainty protections; missing evidence chooses a qualified conservative default, not an arbitrary cheap candidate.
4. Read a supplied snapshot of quota and health; prefer suitable capacity that is less scarce.
5. Clamp effort only within valid negotiated bounds. Empty intersections are errors.
6. Return the selected profile, initial effort, reasons, exclusions, and counterfactual choice without pressure.

Hard capability replacement must not accidentally substitute a weaker model for an adequate one. A model that supports tools is not thereby qualified to complete a tool-driven coding task.

Model rankings are per lane. Reuse admission benchmark artifacts and explicit qualification references. The current `equivalent` fallback concept does not establish adequacy for every task or effort level. Do not automatically promote all failure fallbacks into quota-saving primaries.

### 8.2 Make scarce capacity matter without changing task meaning

Reuse existing pace-based pressure and smoothing. Initially change only new-session admission. Pressure never changes the Jev answers or an active model binding.

Preserve bounded threshold shifting as a baseline experiment, but add a quality guard: a pressure-driven alternative must still belong to the task's qualified candidate set. A threshold shift is not permission to lower the quality floor. When no adequate alternative exists, keep the adequate constrained provider, defer visibly, or require a user decision.

Do not keep inflating thresholds until Flash handles everything. Protected/high-consequence work and uncertain hard cases retain their guards.

The useful counterfactual log is: "This task met the same adequacy requirement under both policies; pressure changed the choice between these qualified options." Explicitly record when evidence is too weak to make that claim.

### 8.3 Quota observations

A snapshot carries source, observation time, status (`fresh`, `stale`, `unknown`, `error`), used fraction, and reset/window information when available. The source runs outside the request path. Do not scrape or run account commands on each inference.

For providers exposing several overlapping limits, preserve each window as a coherent record. Compute pressure per fresh window and take the maximum relevant pressure; do not pair one window's usage with another's reset time. A normalized `windows` list is sufficient; no new scheduler is necessary.

Unknown/stale readings retain a visible unknown flag. Their numerical policy contribution may fall back to the existing neutral behavior, but never call them "unused capacity." Circuit-breaker health is availability evidence, not a quota meter. A 429 does not by itself distinguish a short rate limit from allowance exhaustion.

Initially use a configurable reserve through existing pressure settings rather than predicting each active session's future consumption. Record real windows and task chronology for later evaluation. No speculative token-to-subscription-quota conversion or automatic online demand forecasting.

### 8.4 Failure policy at admission

If Jev is down, use an explicitly configured conservative fallback that passes hard constraints and the selected lane's fallback policy, or return `NO_SAFE_ADMISSION`. Its semantic answers are absent, not synthetic Jev measurements.

In a strict session the resulting profile is still a fixed binding after resolution; recovery of Jev does not change it. This differs deliberately from legacy temporary fallback pins. A fallback classifier serving a request is not permission for a fallback execution model later.

## 9. Strict session lifecycle and HTTP contract

### 9.1 Identity, authentication, and concurrency

A strict session uses a caller-generated random session ID, scoped to the authenticated local router owner and configured client identity. It must not be reconstructed from message text, a system prompt, or a rotating upstream bearer token.

Use the existing router-control credential for strict control and execution requests; strip it before forwarding. Strict requests require that credential, even when legacy read-only endpoints permit direct loopback access. The current single-owner scope does not claim tenant isolation between people sharing that credential. Upstream credentials remain separate and are never placed in JSON, Jev state, fingerprints, or logs.

Reuse SQLite and an in-process per-session lock. Hold no database transaction across a network call. Conditional writes/version checks resolve concurrent admissions; losers reread the winning binding. A process-local startup guard prevents two workers from opening the strict service against the same database in this revision.

Only one execution request per strict session is in flight. Parallel independent sessions remain supported. Concurrent requests for the same session return a structured conflict rather than introducing serialization assumptions for competing tool continuations.

### 9.2 Minimal state machine

```text
absent --explicit fresh resolve--> prepared --first accepted response--> active
                                      |                                  |
                                      +--request rejection: stay---------+
                                      |                                  |
                                      +--cancel/expiry--> closed          +--explicit close--> closed

Any profile revocation/mismatch -> blocked, retaining the old binding
Unknown execution outcome -> retain identity; require reconciliation before adaptive effort
```

`prepared` is a fixed execution contract, not a claim that inference succeeded. Once disclosed to a client, it cannot be replaced invisibly even if the first inference fails. A first-request timeout does not permit submitting the same history to a different profile.

Existing accepted-after-2xx behavior still determines **activation** and execution statistics. It does not erase a previously disclosed strict contract. Track later stream failure separately.

Prepared records may expire before use, returning 410. Active bindings do not expire on the six-hour legacy pin TTL. Explicit retention pruning leaves enough tombstone information to reject resuming a removed ID; never silently treat it as new. An unknown resumed ID returns 409 and requires explicit reconciliation or a new genuinely fresh session.

A raw fork/import containing assistant/tool history is a continuation, not automatically new independent work. Freshness is partly a client assertion: reject obvious contrary evidence such as supplied historical assistant/tool items or a previous-response reference, and document the trusted-client boundary.

### 9.3 Proposed `POST /router/resolve`

This endpoint resolves or restores the router-owned execution binding. It is not Pi-specific. It performs no execution-model call and no draft call.

Illustrative new-session request:

```json
{
  "schema_version": "1",
  "intent": "new",
  "session_id": "session-2f766788",
  "request_id": "resolve-0001",
  "alias": "auto-session",
  "client": "coding-client",
  "candidate_models": ["ollama/glm-5.3-flash", "gpt-6-astra", "grok-4.6"],
  "client_contract": {
    "dynamic_metadata": true,
    "protocols": ["openai-chat"],
    "fresh_execution_context": true,
    "turn_boundary_reporting": false
  },
  "request": {
    "messages": [{"role": "user", "content": "Add quoted CSV fields and tests."}],
    "tools": []
  },
  "limits": {
    "max_output_tokens": 8192,
    "estimated_input_tokens": 4200,
    "estimate_method": "client-estimate-v1"
  }
}
```

This example contains empty tool schemas only to keep it readable. Real admission must supply complete capability/budget facts for tools that will actually be sent. Client-supplied counts do not override stricter server checks or deployment limits.

Illustrative response:

```json
{
  "schema_version": "1",
  "session_id": "session-2f766788",
  "state": "prepared",
  "binding_revision": "sha256:example-profile-digest",
  "decision_id": "example-decision",
  "execution": {
    "model_key": "ollama/glm-5.3-flash",
    "provider": "ollama",
    "wire_model": "ollama/glm-5.3-flash(none)",
    "protocol": "openai-chat",
    "context_window": 128000,
    "max_output_tokens": 8192,
    "supports_tools": true,
    "supports_vision": false,
    "initial_effort": "none",
    "effort_mode": "fixed"
  },
  "decision": {
    "rule": "qualified_simple_work",
    "source": "jev",
    "quality_lane": "simple-code",
    "quota_changed_choice": false,
    "quota_status": {"openai": "unknown", "ollama": "unknown", "xai": "unknown"}
  }
}
```

The actual outcome is policy-dependent; this is not a benchmark result. The binding digest covers immutable profile identity and negotiated limits, not changing quota, timestamps, or effective effort.

For resume use the same endpoint with `intent: resume`, session ID, client, and request ID; omit task content. It returns the stored contract and effective effort without Jev. A repeated `intent: new` with the same idempotency key and identical request fingerprint returns the same result. A conflicting fingerprint for the same session ID returns 409. Resolution retry does not spend another Jev call once a result has been committed.

### 9.4 Managed execution

The client acknowledges the binding by sending:

```text
X-Router-Admin-Token: <router control credential>
X-Router-Session: session-2f766788
X-Router-Binding: sha256:example-profile-digest
X-Router-Request-Id: execution-0001
```

The endpoint and concrete `model` must match the resolved contract. The request must also satisfy current hard constraints. An alias may be accepted only if the resolved/static mode explicitly declares it and the client has acknowledged the negotiated profile; it is not a way to skip resolution.

Managed concrete-model requests go through the same outcome/health/telemetry path as aliases. Unmanaged concrete-model requests retain passthrough behavior. Never use the passthrough branch as a way to evade strict binding validation.

A request ID provides correlation and prevents conflicting duplicate execution, not general exactly-once semantics. Keep a small execution status/fingerprint record. An already completed duplicate does not automatically invoke the provider again; return `REQUEST_ALREADY_COMPLETED` because the router does not store complete responses for replay. A request whose prior outcome is unknown returns `EXECUTION_OUTCOME_UNKNOWN`. The caller may recover through a qualified provider mechanism or explicitly start a new attempt; the router never invents an idempotency guarantee.

### 9.5 Fallback and configuration behavior

Pre-resolution policy may select a healthy adequate alternative using the health snapshot. After resolution, the disclosed profile is fixed. A strict request has a one-entry execution plan; configured cross-model fallback lists do not apply. Native bounded retry on the same profile is allowed only where outcome/retry semantics are known.

On hard incompatibility, preserve the binding and return an error with corrective options, not an automatic replacement. A new task, compaction request, router restart, auth refresh within the same account, or new wording does not create a fresh admission.

A user deliberately changing model must create/reconcile a distinct session binding. Automatic handoff construction is not part of R2.

### 9.6 Error vocabulary

Use existing structured router errors with stable machine codes:

| Code | HTTP | Meaning |
|---|---:|---|
| `INVALID_ROUTER_INPUT` | 400 | Invalid schema, unsupported event, or malformed request. |
| `ROUTER_UNAUTHORIZED` | 401/403 | Missing/invalid router credential or disallowed client/profile. |
| `SESSION_CONFLICT` | 409 | Conflicting session resolve, concurrent execution, or binding mismatch. |
| `SESSION_UNKNOWN` | 409 | Resume/execution references an unknown session. |
| `SESSION_CLOSED` | 410 | Expired prepared session or explicitly closed/pruned session. |
| `PROFILE_CHANGED` | 409 | Previously negotiated execution contract is no longer valid. |
| `NO_SAFE_ADMISSION` | 422 | No qualified candidate satisfies requirements. |
| `CONTEXT_BUDGET_EXCEEDED` | 422 | Input/output/reserve does not fit the bound profile. |
| `UNSUPPORTED_PROFILE` | 422 | Protocol or experimental feature is not qualified. |
| `REQUEST_ALREADY_COMPLETED` | 409 | Prevent accidental duplicate execution; no response replay store. |
| `EXECUTION_OUTCOME_UNKNOWN` | 409 | Prior ambiguous attempt must be reconciled, not rerouted. |
| `TURN_NOT_SETTLED` | 409 | Effort change requested during an unresolved/active turn. |
| `EFFORT_HISTORY_MISMATCH` | 409 | Client failed to preserve a required update or base setting. |

Do not convert an authorization, hard-context, or protocol error into a cheap-model request.

## 10. Experimental between-turn effort control

### 10.1 Purpose and scope

Test whether Jev can allocate reasoning effort within a fixed-model session more efficiently than one fixed effort level, without reducing completion quality or destroying useful cache reuse.

The experiment has three modes:

- `off`: no per-turn Jev calls; initial effort remains fixed.
- `shadow`: calculate/log a recommendation at eligible boundaries; send exactly the fixed-effort baseline.
- `active`: apply a qualified effort update, never a model/provider change.

The feature is explicitly opt-in at both alias and client contract. Existing sessions do not automatically enable it after a config change. Start with a short allowlist of exact deployment profiles and a small effort ladder, for example `low`, `medium`, `high` where supported. Effort labels are not assumed equivalent across models.

### 10.2 Verified GPT-6 Astra behavior

OpenAI currently documents `configuration_update` for GPT-6 Astra in standard single-agent mode. It is inserted before the next user message while request-level `reasoning.effort` remains unchanged. Updates must persist in original history order or through the previous-response chain; the response field still reports base request effort. Adjacent updates and automatic compaction/truncation combinations are disallowed; standalone `/responses/compact` rejects such histories. Explicit `compaction_trigger` has a separate documented flow. [S10]

The API support does not establish that the current subscription proxy, Codex harness version, or another gateway implements the same behavior. Qualification of the exact path is mandatory. Do not merely change `(low)` to `(high)` on the existing suffix model and label the result cache-preserving.

Native update example:

```json
{
  "type": "configuration_update",
  "reasoning": {"effort": "high"}
}
```

The provider update is not a natural-language system prompt injection. No model migration occurs.

### 10.3 Capability declaration

A profile declares a tested strategy:

| Strategy | Meaning | R2 release status |
|---|---|---|
| `fixed` | No automatic effort changes. | Stable default. |
| `native_configuration_update` | Adapter preserves provider-supported update items and their history semantics. | First supported active experiment after qualification. |
| `request_parameter` | Adapter changes a request parameter on the same underlying model. | Separate lab baseline; cache behavior must be measured. Not automatically promoted. |

Record `cache_behavior: verified_preserving | verified_breaking | unverified`. This is a deployment qualification result, not an assurance that every subsequent request will hit cache. Normal caching requirements still apply. [S11]

The first active experiment supports HTTP Responses only. No WebSocket implementation, concurrent native multi-agent execution, automatic compaction/truncation, or standalone compaction is added incidentally. Clients unable to preserve updates and expose reliable boundaries remain fixed/shadow.

For early native experiments, use bounded sessions that do not require compaction. Stop further adaptive execution before the agreed context safety limit. Later support for the provider's explicit compaction-trigger flow requires separate fixtures for post-compaction reapplication; the router must not build its own compactor.

### 10.4 Proposed `POST /router/turn-plan`

This endpoint is experimental; stable clients never need it. It asks the router to prepare an effort-only decision for a new user turn. It does not execute tools or an inference request.

```json
{
  "schema_version": "1",
  "session_id": "session-2f766788",
  "binding_revision": "sha256:example-profile-digest",
  "turn_id": "turn-0002",
  "request_id": "plan-0002",
  "previous_turn": {
    "id": "turn-0001",
    "settled": true,
    "pending_tool_calls": 0,
    "active_requests": 0
  },
  "state": {
    "task_request": "Improve the CSV parser.",
    "current_user_request": "Investigate why escaped quotes fail across chunk boundaries.",
    "quoted_material": [],
    "observations": [
      {"source": "harness", "kind": "test_result", "status": "failed", "summary": "escaped-quote regression fails"}
    ]
  },
  "context": {
    "estimated_total_tokens": 12000,
    "estimate_method": "provider-usage-plus-new-input",
    "history_mode": "previous_response_id"
  }
}
```

The client reports lifecycle facts; Jev does not infer protocol safety. The router verifies its own in-flight status, binding, previous plan status, request sequencing, budget, and profile capabilities. The declared absence of external tools is a trusted-adapter assertion, not something an HTTP shim can independently prove. Qualification tests must exercise that assertion against the real harness.

Return one of `keep`, `change_effort`, or `blocked`, with plan ID, base effort, previous effective effort, next effective effort, reason, and (only for a permitted native change) the exact protocol item and insertion rule. Shadow responses additionally expose the hypothetical effort but have `action: keep` and contain no executable change.

Repeating an identical plan request returns the same decision. Same turn ID with different evidence while a plan is pending returns 409. A tool continuation reuses that turn's effective effort without another Jev call.

### 10.5 Provisional effort policy

Derive an effort floor from existing rules and experimental semantic signals. Choose among efforts of the bound model only. Do not call the full cross-model policy and then replace its selected model afterward.

Use current-turn demand plus task constraints and any relevant prior unresolved issue. A short "continue" is not evidence for a downgrade. A concise corrective message may require substantial effort. An environment failure or missing credentials is not, by itself, evidence that more reasoning would help.

Start with these conservative mechanics, all configurable and recorded:

- Upward recommendation may act at the next eligible boundary when sufficiently supported.
- Downward changes require low-risk evidence and two consecutive eligible-turn recommendations for the lower effort.
- Never change effort more than once per new user turn; never alternate within a tool loop.
- Keep explicit user/client minimum effort and protected-work floors.
- If Jev times out, returns malformed data, or relevant evidence is missing, keep current effort.
- Quota may influence choices only above the measured adequacy floor. It cannot make a failing hard task "easy."

These mechanics are a testable starting policy, not a statistical guarantee. Compare them with a simpler no-hysteresis policy in replay before adding further tuning knobs. Do not add cooldown daemons or a reinforcement learner.

### 10.6 Applying updates without corrupting history

The router owns the plan and validates execution. The generic client adapter owns mechanically recording the permitted item in its canonical transcript. This is not a client-side routing policy.

For `native_configuration_update`, the client must keep the initial request-level effort fixed and insert the planned item exactly before the designated new user message. For full-history replay it must retain every previous update at its original position; for a previous-response chain it must reference the accepted parent and not resend an already-persisted item. There is exactly one update owner. Reject client-authored conflicting updates while router-managed adaptation is enabled.

The request carries `X-Router-Turn` and `X-Router-Turn-Plan`. The Responses adapter validates the prescribed base setting and update against the pending plan before forwarding. It must not also insert a second copy. Continuations use the same effective setting; no fresh item is added simply because another HTTP request occurred.

Maintain a small update ledger with turn ID, sequence, from/to effort, base effort, plan ID, history anchor/parent reference, expected effective effort, confirmed effective effort when known, and status. Store hashes/identifiers, not whole transcripts. Full-replay validation uses original item positions and prefix hashes; repeated identical user text is not a unique anchor by itself. Chain mode validates known parent-response lineage when observable. Missing lineage makes further transitions unsupported until reconciled.

A plan alone does not prove application. Record `planned`, `accepted`, `confirmed`, `rejected`, or `outcome_unknown`. HTTP 2xx establishes transport acceptance only. Confirm the expected effort transition only after a valid successful provider completion that includes the submitted update in its accepted history, or another explicitly qualified reconciliation mechanism; the base-effort response field is not that evidence. On definitive pre-acceptance rejection, keep previous confirmed state and retry the same plan after the client restores the matching provisional history. On ambiguous failure after submission, retain expected and confirmed values separately and block a competing transition until reconciled. Do not assume a disconnected response never ran. A successful assistant response containing tool calls can confirm the update while the larger user turn is still active; subsequent tool continuations retain it.

Disabling the feature stops new decisions but does not erase previously applied update items or reset effort silently. The current effective setting remains until an explicit supported update changes it. This is essential for rollback and session resume.

### 10.7 Protocol observation

The native experimental adapter may require response IDs, terminal status, and usage to validate lineage and evaluate cache behavior. Add a bounded read-only SSE observer only for that adapter; raw downstream bytes remain unchanged. Parse only required event fields. Never assemble or log full reasoning/output text for telemetry.

Metric parsing failure must not corrupt or retry the response. Mark required lineage/usage unknown and disable further transitions until safe reconciliation. HTTP 200 and clean socket EOF do not prove provider-level completion. Expected and confirmed effective effort are tracked from the update ledger and qualified protocol outcomes, not inferred from a response field known to report base effort.

This observer is not required for the stable Chat Completions core. Existing non-streamed usage capture and externally supplied, provenance-labeled eval telemetry remain available.

## 11. Storage, diagnostics, and privacy

### 11.1 Store changes

Reuse the existing SQLite database. Add an additive `sessions` table and, only when implementing effort control, `turn_plans`. Extend existing decision rows rather than creating a second analytics database.

Minimum session fields: owner/client, session ID, alias, state, model/provider key, protocol, negotiated context/output limits, immutable binding digest, base/effective effort, decision/config/question/model versions, created/last-seen timestamps, and pending/unknown execution state.

Minimum decision additions: event type (`admission`, `effort_plan`, `execution`), session/turn/request IDs, active and shadow feature packet versions, action taken, intended/executed model and effort, quality lane, quota snapshot/status, counterfactual action without pressure, relevant capability exclusions, and evidence provenance.

Reuse existing feedback verdicts; optional additions identify session/turn and checked outcome provenance. A user saying "too weak" is feedback, not automatically a verified task failure or a training label for a different model.

Record execution attempts without complete response retention. Use one configurable bounded history for request-ID fingerprints/status; a pruned ID must not be treated as proof it never executed.

### 11.2 Human-readable diagnostics

Extend existing `explain`, `decisions`, and `quota`; add a small `sessions show`/`sessions close` CLI surface backed by the same service/store. Display model identity, negotiated window, base/effective effort, binding state, last decision reason, quota freshness, and whether adaptation is off/shadow/active.

Good output distinguishes: "model unchanged; next user turn requests high effort under the native update experiment" from "model changed." Do not inject these explanations into the model's prompt.

Report end-to-end latency, Jev latency, first response byte, first user-visible text when measurable, response completion, and completion quality separately. A first SSE heartbeat is not first-token latency.

### 11.3 Privacy and security

Keep request text and Jev state transient by default. `log_state: false` concerns local persistence; it does not prevent the selected excerpts from being sent to TypeSafe. Offer `decider: rules` or a permitted no-external-classification profile without disguising it as entirely local inference.

Never send auth headers, credentials, secret-source output, account tokens, or complete environment/configuration dumps to Jev. Redaction is a best-effort aid, not a universal sensitive-data detector. Local opt-in eval fixtures containing real work stay git-ignored and are reviewed before publication.

Profile endpoints come from trusted configuration, not request-supplied URLs. Requests cannot create arbitrary outbound destinations. Existing body/concurrency limits, authentication, raw-header handling, and bounded decompression capture remain in force.

## 12. Configuration proposal and defaults

This overlay illustrates the new core settings without duplicating the whole existing file. All fields here are proposed; validate unknown keys rather than silently ignoring them.

```yaml
session_routing:
  enabled: true
  prepared_ttl_seconds: 600
  max_inflight_per_session: 1
  require_control_token: true

semantic_policy:
  active_questions: [task, difficulty, harm_if_wrong]
  shadow_questions: [mechanical_transform, interacting_constraints, requirements_missing]
  distribution_policy: shadow

experiments:
  adaptive_effort:
    mode: off
    qualified_profiles: []
    evaluate_on: new_user_turn
    downgrade_confirmations: 2
    on_unknown: keep

aliases:
  auto-session:
    session_mode: strict
    state_builder: continuation_aware_v1
    questions: [task, difficulty, harm_if_wrong]
    rules: policy
    allowed_models: [ollama/glm-5.3-flash, gpt-6-astra, grok-4.6]
```

The initial examples use current baseline model IDs for continuity. Eligibility and lane qualification may leave some listed models unused for particular work. Do not change published context sizes or protocols solely to make the example pass.

Do not add a dashboard configuration layer, remote dynamic catalogue, speculative reservation worker, or unbounded automatic threshold search. All decision policy stays in this file and pure Python functions. External model credentials remain in the existing provider path.

## 13. Evaluation strategy: separate five questions

The evaluation must answer these independently:

1. Does the implementation honor transport, session, context, and effort contracts?
2. Does Jev measure useful properties of the supplied work?
3. Do those properties improve model/effort placement beyond simpler mechanisms?
4. Does the complete system improve satisfactory coding work under the same capacity constraints?
5. Does the effort experiment preserve quality and acceptable cache behavior on the exact deployed path?

Do not use a high routing-label score as the answer to all five.

### 13.1 Evidence manifest

Each run writes a machine-readable manifest and a readable summary. Include repository SHA, Jev model/version, question hash, state-builder hash, policy hash, execution-profile/adapter versions, candidate set, case IDs and split, randomization seeds, repeats, cache conditions, quota snapshots/coverage, call/time/spend caps, grader versions, and exclusions/unfinished sessions.

Hash each canonical input state, but keep raw prompts only in explicit local eval fixtures. A cache-only replay is labeled `policy_replay`, not a live classifier or latency test. An externally observed subscription delta is labeled accordingly and is not attributed to a session when competing traffic makes that attribution impossible.

### 13.2 Offline contractual coverage

Keep the baseline regression suite, including compressed JSON/SSE, headers, malformed Jev data, strict eligibility, first acceptance, failed streams, security, and migration. Add the following named cases. These IDs are required coverage, not claims that test files already exist.

| ID | Required case / assertion |
|---|---|
| C01 | Strict execution without stable session ID or binding acknowledgement is rejected. |
| C02 | Fresh resolve selects once; identical retry returns the stored result. |
| C03 | Concurrent fresh resolves commit one binding; conflicting payload returns 409. |
| C04 | A prepared profile does not change after its first rejected inference. |
| C05 | An active session survives restart and legacy TTL expiry unchanged. |
| C06 | A resumed unknown/pruned/closed ID never silently becomes a new session. |
| C07 | Model/account/protocol changes are rejected for existing bindings. |
| C08 | Upstream token refresh within the same configured account does not reclassify. |
| C09 | Quota changes affect new sessions only; active identity remains unchanged. |
| C10 | A 429/5xx/timeout in strict execution never falls through to another model. |
| C11 | Temporary Jev failure at admission remains the same resolved model after recovery. |
| C12 | Unmanaged concrete requests still follow the legacy passthrough contract. |
| C13 | Managed concrete requests cannot bypass binding, eligibility, or logging. |
| C14 | Static envelope admits only a homogeneous compatible pool and its minimum limits. |
| C15 | Resolved limits disagreeing with acknowledged client limits are rejected. |
| C16 | Boundary budget checks cover exactly fit, one over, output reserve, and invalid numbers. |
| C17 | Tool schemas, long code, CJK text, images, and previous-response deltas are not omitted from budget policy. |
| C18 | Overflow/capability change blocks the same profile rather than repinning. |
| C19 | Compaction/maintenance requests preserve session identity and do not trigger admission. |
| C20 | Profile revocation or reduced limits block; harmless quota/config changes do not repin. |
| C21 | Routing metadata never changes system text, tools, or historic message contents. |
| C22 | Two difficulty distributions with equal means can produce the intended different experimental decisions. |
| C23 | Uncertainty between action-equivalent labels does not force unnecessary escalation. |
| C24 | Malformed/partial/NaN/unsolicited Jev data uses the validated fallback boundary. |
| C25 | Shadow questions/policies never change actual execution; invalid shadow handling cannot weaken validation. |
| C26 | The same semantic packet is reused across pressure sweeps; quota cannot change semantic answers. |
| C27 | Missing/quota-error/stale snapshots remain visible as unknown, not unused capacity. |
| C28 | Multiple quota windows preserve matching usage/reset fields and use the appropriate maximum pressure. |
| C29 | Pressure never promotes an unqualified execution model/effort merely because it has capacity. |
| C30 | HTTP acceptance, transport EOF, provider completion, and graded task success remain separate. |
| C31 | Repeated/ambiguous execution IDs do not cause automatic duplicate upstream calls. |
| C32 | Router control tokens are stripped; unauthorized resolve/session access is denied. |
| C33 | Additive schema migration preserves existing pins, decisions, feedback, and baseline tests. |
| C34 | Bound sessions continue on the same model even when the selector is unavailable after resolution. |

### 13.3 Effort-specific offline and protocol coverage

| ID | Required case / assertion |
|---|---|
| F01 | `off` adds no per-turn Jev call and never changes effort. |
| F02 | `shadow` logs a recommendation but sends the exact fixed-effort request semantics. |
| F03 | Unverified model/endpoint/adapter or unsupported protocol cannot activate the experiment. |
| F04 | A qualified between-turn update changes effective effort but not model, account, limits, or base effort. |
| F05 | No update during active execution, unresolved tools, steering, queued continuation, or unknown boundary. |
| F06 | A tool-result continuation keeps the same turn and makes no fresh effort decision. |
| F07 | Duplicate turn-plan requests are idempotent; conflicting payloads cannot create two updates. |
| F08 | The native item occurs before the correct user message, never adjacent to another update. |
| F09 | Full replay preserves every prior update's original order and matching anchor. |
| F10 | Previous-response chaining neither drops an accepted update nor duplicates it in the next delta. |
| F11 | Response `reasoning.effort` reporting the base value does not overwrite the effective ledger. |
| F12 | Headers-only acceptance is not confirmed application; rejected/ambiguous updates cannot silently alter confirmed state or permit a competing transition. |
| F13 | Client and gateway do not both inject the same item; conflicting manual updates are rejected. |
| F14 | Classifier timeout/malformed output keeps current effort, not a lower heuristic effort. |
| F15 | Downshift confirmation and protected floors behave across resume and counterfactual pressure changes. |
| F16 | Disabling adaptation preserves already applied items and effective effort. |
| F17 | Automatic compaction/truncation and standalone compaction are rejected for the initial native experiment. |
| F18 | Approaching context limit stops the bounded experiment without changing model or erasing updates. |
| F19 | Bounded SSE observation preserves raw bytes; parser failure marks telemetry/lineage unknown. |
| F20 | A parameter-change laboratory result is not reported as native cache-preserving behavior. |

Test session contracts through the real jev-router HTTP app and store with fake upstreams. A small generic scripted harness should produce model requests, tool continuations, turn boundaries, restart/resume, and maintenance events. Do not make a Pi installation necessary to run the core test suite. A separate real-client smoke test can validate a particular adapter.

### 13.4 Semantic feature evaluation

Reuse the current written/public request corpus for regression, but annotate a representative subset for the new atomic questions. Include short difficult requests, long mechanical requests, ambiguous requirements, exactness-sensitive edits, interacting components, CJK/mixed language, pasted routing instructions, and different client envelopes.

Compare:

- Existing three-question packet and baseline mean/confidence policy.
- The expanded packet with the same policy (overhead/stability control).
- The expanded packet with each new feature activated individually.
- Distribution-aware policy versus mean-only policy, on identical cached answers.

For each question report agreement, confusion/error cases, confidence or probability reliability against the relevant label, abstention rate, repeats, state size, live latency, and token usage. Never evaluate `confidence` as downstream task-success probability. Measure batching overhead empirically; do not infer it from a vendor demonstration.

Group near-duplicates, perturbations, and tasks from the same project family in the same split. Once a holdout case influences wording or thresholds, it is no longer untouched holdout evidence. Keep a separate frozen final set and record all search iterations.

Add injection and negative controls: constant state, shuffled labels, constant policy, and length-only rules. Test whether quotes claiming a preferred model/effort actually alter the action. Numeric counters must be unit-tested independently from the classifier.

### 13.5 Complete coding-session evaluation

Create a small, local, executable task suite with clean disposable repositories/worktrees, fixed starting commits, predefined acceptance checks, and explicit allowed tools. Hidden/reference checks should not be authored solely by the candidate being evaluated.

Task families:

- Mechanical bounded edits and repetitive transformations.
- Normal implementation with meaningful edge cases.
- Ambiguous or interacting-component diagnosis.
- Context-growth sessions requiring normal compaction in fixed-effort mode.
- Multi-turn sequences that move from routine to demanding and back.
- Environment/missing-information failures where stronger reasoning is not the obvious remedy.

Evaluate at least these policies:

| Policy | Purpose |
|---|---|
| Fixed preferred strong model/effort | Current-workflow reference, using the same execution/account constraints. |
| Simple deterministic session routing | Measures benefit available without Jev. |
| Existing Jev mean/confidence routing | Baseline for the current implementation. |
| Jev packet + promoted distribution/features | Measures the actual contribution of the new semantic policy. |
| Same-model fixed effort vs adaptive effort | Separate experiment; do not conflate with cross-model gains. |

Do not run all combinations immediately. First qualify the core path, then compare admission policies, then the effort experiment with the model held fixed. Change one component per promoted experiment.

A budgeted pilot can begin with eight representative tasks and two repeats per selected policy. This is for identifying failures and operating cost, not proving a small improvement. Expand the frozen holdout and repetitions when results justify the spend. Configure hard execution-call, wall-clock, and spend limits; a capped/unfinished session is reported, not silently dropped.

Measure verified completion, critical regressions, manual intervention, retries, total execution calls, wall-clock time to a satisfactory result, model/effort usage, compaction events, context errors, and unknown outcomes. Treat subjective user satisfaction as an additional labeled outcome. A blind LLM judge can assist review but cannot override a reliable programmatic failure or serve as the sole grader of its own routed work.

### 13.6 Quota and cache evaluation

For subscription allocation, replay chronological sessions with recorded or explicitly synthetic quota windows, resets, stale telemetry, concurrent-session demand, and provider failures. Distinguish simulator results from live observed allowance deltas. Evaluate completed useful work, strong-model capacity preserved for later hard work, blocked/deferred sessions, idle expiring capacity, and corrections caused by under-allocation.

Do not claim real quota savings using the unmeasured `ROUTE_COST` ratios. Record API tokens/cost separately from subscription consumption. If the provider reports only coarse account-level percentages, report an interval/coverage limitation rather than false per-request precision.

For effort experiments compare fixed-low, fixed-medium/high, simple effort rules, and Jev effort control on the same target model and comparable task states. Include the fixed effort that achieves comparable quality, not only the most expensive baseline. Use counterbalanced/interleaved order and separated session/cache namespaces to avoid one arm benefiting from another arm's warm cache. Preserve realistic within-session reuse in each arm.

Report cached/uncached input, reasoning/output tokens where exposed, Jev overhead, first visible output, total completion time, task quality, and adapter errors. A cache hit is not guaranteed simply because native update syntax is correct. Separate a valid protocol-update test from a measured cache-preservation experiment.

Use paired task comparisons; resample by task/session rather than treating every turn as independent. Report denominators and uncertainty. A small no-failure pilot does not establish noninferiority. Predeclare a tolerable quality-loss margin before a larger trial; an interval too wide to assess that margin is inconclusive.

### 13.7 Promotion gates

**Stable strict core:** all contract tests pass; no observed silent model changes; failure paths are inspectable; generic harness plus at least one real deployment smoke test passes. This authorizes personal opt-in use, not a universal reliability claim.

**New Jev features:** no unexplained contract regression, benefit on a frozen task set or a clearly useful error reduction, measured overhead within the chosen latency/call budget, and no evidence that the feature merely reshapes labels. Non-beneficial features stay off or are removed.

**Active effort:** exact deployment qualification passes F01–F20, native history semantics verified, cache/quality comparison reported, and rollback/resume demonstrated. Short-session qualification does not authorize untested long-session compaction behavior. Remain `shadow` when evidence is inconclusive.

**Public claims:** publish the task/sample size, limitations, exact profiles, and unsupported configurations. Do not say "supports all OpenAI-compatible agents" merely because a transport smoke test passed.

## 14. Implementation plan inside jev-router

Keep changes in this repository. Suggested file boundaries are guidance, not a demand for new abstractions when an existing file fits.

| Area | Change |
|---|---|
| `config.py` | Strict session mode, protocol/output metadata, active/shadow packet settings, experimental effort capability validation. |
| `features.py`, `state.py` | Versioned bounded semantic packet; budget facts/provenance; no model-generated summarizer dependency. |
| `deciders/jev.py` | Reuse validated Jev call; separate semantic classification from cross-model policy so turn assessment cannot accidentally reselect a model. |
| `policy.py` | Pure quality-guarded admission and distribution helpers; take quota snapshots as inputs. |
| `pins.py` or a small `sessions.py` | Additive durable bindings, request status, conditional updates; keep legacy pins distinct. |
| `app.py` | Resolve endpoint; strict execution before passthrough dispatch; one-entry strict execution plan. |
| `quota.py` | Preserve source freshness and coherent overlapping windows; reuse existing poller. |
| `effort.py` | Optional turn-plan policy/ledger, no cross-model selection and no continuous supervisor. |
| `protocols.py` or existing protocol helper | Experimental Responses validation/update contract and bounded observer, not universal conversion. |
| `cli.py` | Reuse explain/decisions/quota; session inspect/close; expose active versus shadow. |
| `evals/` and `tests/` | Reuse replay/common metrics; add generic session fixtures and explicit effort qualification reports. |

### Milestone A — stable session contracts

Implement execution-profile metadata, strict sessions, resolve/resume, context agreement, and managed concrete forwarding. Preserve the hardening tests. Keep all effort adaptation off. Use the current Jev packet and model policy as the first baseline.

**Acceptance:** through a generic client, resolve once, execute multiple tool turns, change quota, restart service/client, exceed the old TTL, and confirm the same profile remains. Overflow blocks instead of switching. Classifier outage after resolution is irrelevant to continued execution.

### Milestone B — Jev semantic experiments and quota validation

Add the three shadow questions, distribution features, coherent quota-window handling, counterfactual logging, and completed-task evals. Compare old policy, rules, and experimental policy. Promote only features with useful evidence.

**Acceptance:** one explain record makes clear which semantic judgments, hard constraints, quality qualification, and quota state caused admission. Replaying that record reproduces the decision. Under pressure, no unqualified model is substituted.

### Milestone C — effort shadow and native active experiment

Implement turn-plan in shadow, then the qualified native GPT-6 Astra path. Keep exact model identity fixed. Use a generic scripted client to validate protocol items and history before adding any optional harness adapter.

**Acceptance:** a low-demand / high-demand / low-demand turn sequence can change effective effort without changing model or base effort, without losing required history, and without violating floors. Repeat with retries, resume, ambiguous failures, disabling the experiment, and context-limit interruption. Compare against matched-quality fixed-effort baselines.

### Later work—not dependencies

An independently created worker session can use the same resolve contract. A harness adapter may expose automatic initial selection or effort-plan plumbing. A small learned adequacy predictor may eventually consume the Jev features, but only after enough outcome data and controlled exploration address selection bias. Automatic feature discovery and online policy updates are not required for the everyday tool.

## 15. Developer runbook and deliverables

A repository implementation should include:

1. `docs/SESSION_ROUTING.md`: strict/legacy behavior, resolve contract, context limits, failure semantics, and minimal client example.
2. `docs/ADAPTIVE_EFFORT.md`: supported deployment matrix, off/shadow/active behavior, native update semantics, compaction restriction, and rollback.
3. An opt-in example configuration that does not silently rewrite the user's existing aliases or provider paths.
4. Generic offline session fixtures and the named contract/effort coverage above.
5. An eval manifest and report template that separates replay, live qualification, coding outcomes, and subscription/cache observations.

Use the repository's existing `uv run pytest -q` and `uv run jev-router check-config` as baseline checks. Add new runnable commands only with their implementation; names such as `sessions show` and the new eval entry point are proposals, not commands already available at the baseline.

No live provider calls should occur in ordinary CI. Live qualification requires explicit opt-in, credentials, allowlisted profiles, and budgets. Tests must never use production repositories as writable fixtures or inherit unrestricted secrets into generated-code execution.

## 16. Open decisions with explicit defaults

| Decision | Default for implementation |
|---|---|
| Should richer Jev features affect production immediately? | No; baseline active, added packet fields shadow until outcome evidence. |
| Should quota select an otherwise unsuitable model? | No; capacity preferences operate within adequate candidates. |
| Should strict model bindings expire on legacy TTL? | No. |
| Should the router compact or hand off automatically? | No; return a visible constraint and retain identity. |
| Can reasoning effort vary within a session? | Yes, at verified new-user-turn boundaries on qualified deployments, experimentally. |
| Must native effort changes be configured in Pi specifically? | No; the core contract is harness-neutral. |
| Should an HTTP-successful update be called task success? | No; preserve all status distinctions. |
| Should current suffix-style effort support count as native cache preservation? | No. |
| Can missing provider usage be estimated as zero? | No; unknown coverage is retained. |
| Should unsuccessful experiments remain as permanent complexity? | No; keep them in eval variants or remove them from the default packet. |

## 17. Source notes

These are implementation references and evidence for existing behavior, not endorsements of unmeasured performance claims. Sources were checked for this specification on September 19, 2026. Repository links are pinned to the reviewed baseline where possible.

- **[S1] Baseline commit:** https://github.com/hyspacex/jev-router/commit/a248b889b4511c154c2bb6d7410223c76a0fb8f7
- **[S2] Current HTTP execution and pin behavior:** https://github.com/hyspacex/jev-router/blob/a248b889b4511c154c2bb6d7410223c76a0fb8f7/src/jev_router/app.py
- **[S3] Validated Jev/fallback implementation:** https://github.com/hyspacex/jev-router/blob/a248b889b4511c154c2bb6d7410223c76a0fb8f7/src/jev_router/deciders/jev.py
- **[S4] Hardening regression tests:** https://github.com/hyspacex/jev-router/blob/a248b889b4511c154c2bb6d7410223c76a0fb8f7/tests/test_hardening.py
- **[S5] Configured model limits and routes:** https://github.com/hyspacex/jev-router/blob/a248b889b4511c154c2bb6d7410223c76a0fb8f7/router.yaml
- **[S6] TypeSafe introduction / atomic parallel questions:** https://docs.typesafe.ai/introduction
- **[S7] Jev 1.13 limitations:** https://docs.typesafe.ai/model-jaggedness/jev-1.13
- **[S8] Score distribution semantics:** https://docs.typesafe.ai/primitives/score
- **[S9] Confidence interpretation:** https://docs.typesafe.ai/confidence
- **[S10] OpenAI reasoning guide, “Change reasoning mid-conversation”:** https://developers.openai.com/api/docs/guides/reasoning
- **[S11] OpenAI prompt-caching guide:** https://developers.openai.com/api/docs/guides/prompt-caching

## 18. Implementation brief

Implement R2 as an extension of jev-router, not a new agent or a Pi-first product. First deliver strict router-owned session resolution and forwarding with accurate context metadata. Next evaluate additional Jev semantic features and distribution-aware, quality-guarded quota placement. Finally add opt-in effort-only changes on qualified between-turn transports, beginning in shadow and preserving the exact model binding.

Keep the existing hardening. Do not conflate legacy pin expiry with strict session lifecycle, model changes with effort updates, valid protocol output with task success, or token-based cost weights with measured subscription savings. The required evidence is reliable execution plus more satisfactory completed work under the same constraints.
