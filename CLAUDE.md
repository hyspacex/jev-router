# CLAUDE.md

jev-router is an HTTP shim in front of an OpenAI-compatible proxy. It handles
`model: "auto"` by asking TypeSafe's Jev decision model what kind of request it
is, then choosing a model and a reasoning effort from rules in `router.yaml`.

## Commands

```sh
uv sync
uv run pytest -q                              # 222 tests, no network
uv run pytest tests/test_policy.py::test_name -x
uv run jev-router check-config                # validate router.yaml
uv run jev-router serve --mode shadow
uv run jev-router explain request.json        # calls Jev, never forwards
```

Eval scripts all spend real API quota. `run_eval.py` and `tune.py` need
`TYPESAFE_API_KEY`; `run_outcomes.py` also needs `UPSTREAM_API_KEY` and
`JEV_ROUTER_UPSTREAM` because it calls the candidate models for real.
`run_eval.py --draft` is the one classification run that touches the upstream.

```sh
uv run python evals/run_eval.py --group final --repeats 3
uv run python evals/run_eval.py --variants router_yaml --stability
uv run python evals/run_eval.py --variants router_yaml --control shuffled-labels
uv run python evals/run_eval.py --variants router_yaml --control constant-state
uv run python evals/run_eval.py --variants router_yaml --slice-holdout agentic
uv run python evals/run_outcomes.py --samples 3 --max-calls 400
uv run python evals/tune.py --variant router_yaml --samples 5000
uv run python evals/tune.py --variant router_yaml --slice-holdout chat

uv sync --group evals                         # datasets, for the importer only
uv run python evals/import_public.py --limit 25
uv run python evals/run_eval.py --variants router_yaml --public evals/cases_public.yaml
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

- `deciders/jev.py` also holds the optional AutoMix-style draft step. It is off
  unless an alias sets `draft.enabled`, it fails open, and a pinned turn never
  reaches the decider so it never pays for it.

`evals/`

- `common.py` — cases, slices, splits, disk cache, API clients, the upstream
  `Budget`, `ROUTE_COST`, and the two control transforms.
- `metrics.py` — pure. Wilson intervals, ECE, the cost-matched random baseline,
  Gap@Oracle and Gain@BestSingle, the seeded perturbations, the paired
  bootstrap, the chance and majority-class rates.
- `cases.yaml` — 171 labelled requests. Every case has a `slice`; adversarial
  cases also have `attack` and `attack_vector`. A long-context case carries a
  `generated:` block instead of a `request:` block.
- `generators.py` — seeded synthetic material for the long-context cases.
- `variants.yaml` — overlays on `router.yaml`, one per experiment.
- `run_eval.py` — classification and routing scores, slices, controls,
  stability, calibration.
- `outcome_specs.yaml` + `checks.py` + `run_outcomes.py` — grade real replies,
  k samples per route, winner flip rate, truncation, judge-against-check.
- `tune.py` — search the policy numbers. Also `--slice-holdout`.
- `import_public.py` — sample public datasets into the git-ignored
  `cases_public.yaml`. Needs `uv sync --group evals`.
- `exp_builders.py` — state builders that exist only to be measured.
- `EXPERIMENTS.md` — the log, one change per experiment.

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
  `evals/EXPERIMENTS.md` with before/after numbers on both splits, Wilson
  intervals, and a paired bootstrap p-value. On 171 cases two independent runs
  cannot separate a difference under about 11 points, so the paired test is the
  one that decides.
- Never report a number without its interval, and never draw a conclusion from
  a slice table: the largest slice is 46 cases and the smallest is 10.
- Rerun both controls after any change to the harness. If a shuffled-label run
  scores above chance or a constant-state run beats the majority class, the
  harness is broken and every other number is void.
- Eval runs cost quota. Use the disk cache, keep concurrency low, and give
  `run_outcomes.py` a `--max-calls` budget. It stops itself if a route's own
  median latency triples or replies come back empty, and resumes from the cache.
- `evals/cases_public.yaml` is git-ignored and stays that way. It holds real
  user conversations that may contain personal data.

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
