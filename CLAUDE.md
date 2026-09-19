# CLAUDE.md

jev-router is an HTTP shim in front of an OpenAI-compatible proxy. It handles
`model: "auto"` by asking TypeSafe's Jev decision model what kind of request it
is, then choosing a model and a reasoning effort from rules in `router.yaml`.

## Commands

```sh
uv sync
uv run pytest -q                              # 152 tests, no network
uv run pytest tests/test_policy.py::test_name -x
uv run jev-router check-config                # validate router.yaml
uv run jev-router serve --mode shadow
uv run jev-router explain request.json        # calls Jev, never forwards
```

Eval scripts all spend real API quota. `run_eval.py` and `tune.py` need
`TYPESAFE_API_KEY`; `run_outcomes.py` also needs `UPSTREAM_API_KEY` and
`JEV_ROUTER_UPSTREAM` because it calls the candidate models for real.

```sh
uv run python evals/run_eval.py --group final --repeats 3
uv run python evals/run_outcomes.py
uv run python evals/tune.py --variant router_yaml --samples 5000
```

## Architecture

`src/jev_router/`

- `config.py` — pydantic models for `router.yaml`, cross-field validation, the
  `JEV_ROUTER_UPSTREAM` override, the config hash stored with every decision.
- `features.py` — turns a chat-completions body into counts and flags. Pure.
- `state.py` — state builders, the registry, the paste/continuation split.
- `policy.py` — confidence gate, rule matching, caps, floors, capability
  filters, effort clamping. Pure: no network, no clock, no database.
- `deciders/base.py` — the `Decision` dataclass and the decider registry.
- `deciders/jev.py` — calls Jev, evaluates the policy, falls back on any error.
- `deciders/rules.py` — no-network decider that guesses from counts.
- `pins.py` — sqlite: pins, decision log, feedback, conversation key.
- `feedback.py` — validation shared by the CLI and the HTTP endpoint.
- `app.py` — Starlette routes, forwarding, streaming, shadow mode.
- `cli.py` — serve, check-config, explain, decisions, feedback.

Request flow: `app.chat_completions` → `features.extract_features` →
`pins.conversation_key` and a pin lookup → `deciders.jev` → `state.build_state`
→ Jev → `policy.evaluate` → `policy.apply_effort` → `app.forward`.

`evals/` — `common.py` (cases, splits, disk cache, API clients, `ROUTE_COST`),
`cases.yaml` (86 labelled synthetic requests), `variants.yaml` (overlays on
`router.yaml`), `run_eval.py` (classification and routing scores),
`outcome_specs.yaml` + `checks.py` + `run_outcomes.py` (grade real replies),
`tune.py` (search the policy numbers), `EXPERIMENTS.md` (the log).

## Rules

- Extend through `router.yaml` and the registries. A model, question, rule,
  ruleset, alias or client floor is config. Only a new state builder or decider
  needs Python.
- `policy.py` and `features.py` stay pure. No I/O, no randomness, no clock.
- Never log or store message text, prompts or API keys. The conversation key
  hashes the `Authorization` header. `settings.log_state` is for debugging only.
- A Jev failure must never fail a request. Every path through `deciders/jev.py`
  returns a `Decision`.
- Never reroute a pinned conversation. The one exception is context overflow,
  which moves up once and takes a new decision id.
- Do not touch the streamed response bytes. `app._stream` may buffer for usage
  on non-streamed replies, and must yield chunks unchanged.
- `tune.py` proposes a diff and never writes `router.yaml`.
- Change one classifier variable at a time, and record it in
  `evals/EXPERIMENTS.md` with before/after numbers on both splits.
- Eval runs cost quota. Use the disk cache and keep concurrency low.

## Jev gotchas

- It reads instructions literally. Say exactly what you mean.
- Keep the state small. Irrelevant text lowers accuracy, and pasted text argues
  for its own classification, so label it as data and tell Jev to ignore
  instructions inside it.
- Describe each score level as a standalone situation with examples. That was
  worth 23 points of difficulty accuracy.
- Score answers are 0-indexed and fractional, so `{gte: 1.8}` sits between
  levels. A noul answer carries no confidence, so `conf_gte` never applies.
- Jev cannot count or compare numbers. Every count belongs in `features.py`.
