# Development and tests

[Documentation index](../README.md) · [Project home](../../README.md)

## Tests

```sh
uv run pytest -q
```

GitHub Actions installs from `uv.lock`, runs the offline tests on Python 3.12–3.14,
and validates the configuration. No provider evals run in CI. HTTP/session regressions
cover compressed representations, malformed Jev answers, pin success boundaries,
alias/capability changes, hard conflicts, stream failures, and ingress limits.

The unit tests mock Jev and the upstream with respx, so nothing leaves the
machine. No test runs a quota source against a real program: the `command`
source is exercised against a Python one-liner, and the rest use `static`.

Two tests guard upgrades. One runs the shipped `router.yaml` and a copy of the
configuration as it was before providers existed over a grid of synthetic
answers and asserts an identical model and effort for every one. The other
builds a database with the original schema and checks that it gains the new
columns, keeps its rows, and takes new ones.

The strict-session and effort contracts are numbered C01-C34 and F01-F20 in
[`docs/R2_SPEC.md`](../../docs/R2_SPEC.md), and every one has a test. `TESTPLAN.md`
maps each id to its file, and `uv run pytest -k c18` finds one by id. All of it
is offline: a fake client, mocked Jev, mocked upstream, synthetic fixtures. The
one deployment that has been exercised live is written up in
[`docs/qualification/`](../../docs/qualification/), and it calls itself provisional.
