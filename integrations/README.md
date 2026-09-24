# Client integrations

Production exposes one alias: **auto**, a strict fixed-model session. Direct
OpenAI requests with `model: auto` are not enough: clients must resolve a
binding, then acknowledge it on execution. Existing generic concrete-model
passthrough remains available. The legacy pin implementation remains for
explicit custom configurations and historical records, not shipped aliases.

| Client | Location | Scope |
| --- | --- | --- |
| Pi | `integrations/pi/` | Native extension, fixed-effort Chat Completions |
| Codex | `src/jev_router/integrations/codex/` | Experimental Responses adapter |
| Benchmarks | `evals/session_client.py` | Disposable evaluation sessions only |

The Python integration lives inside the installable package so the wheel and
`jev-router adapter` CLI continue to work. `jev_router.adapter` is an import
compatibility shim, not an independent implementation. Pi is a separate package
because the core Python service must not depend on Node or Pi.

## Core versus experimental

Core service: admission, binding enforcement, request identity, audit and quota
telemetry. Production `auto` uses the existing three-question policy, quality
lanes and explicit frontier admission fallback. This change does not retune the
classifier or claim a new quality result.

Experimental: adaptive effort, extra semantic questions and distribution-based
routing. Their code and tests remain available, but production enables none of
them. Use separate configs/databases for qualification and evaluation. The
Codex adapter remains experimental and is never started by the service.

## Breaking migration

`auto-fast`, `auto-code` and `auto-session` are removed from the shipped config.
`auto` now requires strict admission. Existing pins are not converted. Existing
bindings under a removed alias must not be relabelled: finish/close them before
migration or keep a temporary local compatibility alias until they are retired.

Pi users must start fresh work with `/new` after loading the extension. Existing
unbound transcripts and forks cannot truthfully be admitted as new independent
work. See [Pi setup](pi/README.md) and [Codex notes](codex/README.md).
