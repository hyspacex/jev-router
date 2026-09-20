# Documentation

Start with the [project overview and runnable quick start](../README.md).
The shipped entrypoint is **strict `auto`**: resolve a fresh session, then execute
on its fixed model. Legacy aliases and adaptive effort have separate guides.

## Get started

| Guide | What you will do |
| --- | --- |
| [Quick start](../README.md#quick-start) | Install, connect one upstream model, resolve a session, and get a reply. |
| [Setup choices and troubleshooting](guides/getting-started.md) | Move from the demo to Jev-based routing and diagnose common failures. |
| [Pi integration](../integrations/pi/README.md) | Use `jev-router/auto` for fresh coding sessions. |
| [Strict session contract](SESSION_ROUTING.md) | Build a client that negotiates model metadata before execution. |

## Use and operate

| Guide | What it covers |
| --- | --- |
| [Operations and privacy](guides/operations.md) | Dashboard, decision logs, feedback, stored data, transport, and retention. |
| [Deployment](DEPLOYMENT.md) | launchd example, upgrades, additive migrations, backups, and rollback. |
| [Quota-aware routing](guides/quota.md) | Sources, pressure, thresholds, and route ordering for new work. |
| [Legacy conversation routing](guides/legacy-routing.md) | Custom non-strict aliases, TTL pins, fallback plans, and shadow mode. Not the shipped default. |

## Reference

| Reference | What it covers |
| --- | --- |
| [Configuration](reference/configuration.md) | Settings, providers, models, routes, rules, aliases, and extension points. |
| [HTTP and CLI](reference/api.md) | Commands, endpoints, headers, and environment variables. |
| [Semantic policy](SEMANTIC_POLICY.md) | Active/shadow questions, quality lanes, coherent quota windows, and replay. |
| [Known issues and fixes](reference/known-issues.md) | Dated limitations and resolved regressions. |
| [Model pool](../POOL.md) | Admission verdicts and the evidence behind them. |

## Develop and evaluate

| Document | What it covers |
| --- | --- |
| [Development and tests](development/testing.md) | Offline checks and regression coverage. |
| [Evaluation guide](development/evaluation.md) | Classification, controls, live outcomes, isolation, and tuning. |
| [Design](../DESIGN.md) | Architecture, trade-offs, and rejected alternatives. |
| [Test plan](../TESTPLAN.md) | Measurement methodology and acceptance-case mapping. |
| [Session and effort specification](R2_SPEC.md) | C01–C34 and F01–F20 contracts, with later owner decisions marked in place. |
| [Experiment log](../evals/EXPERIMENTS.md) | Changes tried and their before/after evidence. |
| [Live evaluation isolation](../evals/ISOLATION.md) | Docker, scrubbed environments, benchmark aliases, and call budgets. |

## Experiments and evidence

These are not prerequisites for using the router. Do not treat historical
results as measurements of the current configuration.

- [Adaptive effort](ADAPTIVE_EFFORT.md): between-turn effort changes, the ledger,
  compaction epochs, and the experimental Codex adapter. Off by default.
- [Historical benchmark snapshot](development/historical-results.md): results
  moved out of the original README, with their original caveats.
- [Coding-session pilot](../evals/reports/2026-09-19-session-pilot/summary.md):
  the eight-task fixed/rules/Jev comparison; adaptive effort was off.
- [Deployment qualification reports](qualification/): provisional deployment
  evidence and a template, not blanket model-family qualification.
- [Consolidation verification](verification/2026-09-19-consolidation.md): dated
  offline/live checks and checks that were not run.

## Where documentation belongs

- The root `README.md` is the product overview and first successful request.
- `guides/` contains task-oriented instructions; `reference/` contains lookup material.
- `development/` contains contributor and evaluation guidance.
- Existing top-level contracts, `qualification/`, and `verification/` retain
  stable paths because code, configs, evidence, and external links cite them.
- Integration-specific instructions stay beside their client code in `integrations/`.
- Run artifacts and experiment history stay in `evals/`; new results need provenance
  and uncertainty, not an undated claim in the product README.
