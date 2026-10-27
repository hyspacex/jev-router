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

## The dashboard

The page has one real-browser test. It is skipped unless Playwright is
importable, and it uses local Chrome when it finds it, else Playwright's own
Chromium (`uv run --with playwright playwright install chromium` once):

```sh
uv run --with playwright pytest tests/test_dashboard_browser.py -q
DASHBOARD_SHOTS=/tmp/shots uv run --with playwright pytest tests/test_dashboard_browser.py -q
```

It serves fixture JSON on the real app, so the real credential check, headers
and asset route are what the browser talks to. `DASHBOARD_SHOTS` also writes
screenshots. The rest of the dashboard's tests (`test_dashboard*.py`,
`test_workbench.py`, `test_session_feedback.py`, `test_turn_capture.py`,
`test_scrub.py`) run in the normal suite.

To look at the page by hand, seed a database and serve it. Nothing reaches a
network: Jev and the upstream are mocked while seeding, and the generated
config points them at addresses that cannot resolve.

```sh
export JEV_ROUTER_ADMIN_TOKEN=seed-admin-token
uv run python tests/seed_dashboard.py --db /tmp/jev-seed/router.db --config /tmp/jev-seed/router.yaml
uv run jev-router -c /tmp/jev-seed/router.yaml serve   # http://127.0.0.1:18319/dashboard
```

Serve with the token the sessions were seeded under. See
[the operations guide](../guides/operations.md#trying-it-locally).
