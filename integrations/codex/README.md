# Codex integration (experimental)

Implementation: `src/jev_router/integrations/codex/adapter.py`.
The existing `jev-router adapter` command and `jev_router.adapter` import remain
compatible. The core service never starts this client automatically.

This is a Responses adapter, not the Pi Chat Completions extension. It needs a
separate configuration with eligible Responses profiles. The production auto
pool currently declares Chat Completions, so do not point the Codex adapter at
that pool and assume a protocol conversion will occur.

See `docs/ADAPTIVE_EFFORT.md` for the qualification procedure, opt-ins and
protocol limitations. Use an isolated configuration/database for experiments.
