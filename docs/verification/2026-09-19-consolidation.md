# Strict routing and evaluation safeguards

Verified implementation commits: `43e67e9` and `e350003`.

Strict admission now matches semantic rules without quota pressure, then
allocates within the resulting quality lane. Legacy aliases retain threshold
shifting. Production classification asks only task, difficulty and harm.
Execution rejects a changed compatibility revision, including after restart.

Evaluation file tools reject sibling-prefix and symlink escapes. Result
allocation uses atomic directory creation; completed routing runs publish
`latest` atomically. Session evaluations share the directory allocator and
require a Docker image for generated commands and hidden checks.

## Offline verification

On the deployment Mini, Python 3.14.7:

```sh
uv run --no-sync pytest --cov=jev_router --cov-branch --cov-report=term-missing --cov-report=xml -q
uv run --no-sync jev-router check-config
```

Result: **1,096 passed, 2 skipped**. Configuration validation passed for both
the shipped file and the deployment's local configuration.

Coverage: 5,685/6,213 lines (**91.5%**) and 1,905/2,246 branches (**84.8%**).
The combined coverage report rounds to **90%**. CI collects the same reports
on Python 3.13; no coverage gate has been introduced.

Regression cases include the difficulty-2.5 quota boundary, legacy routing
compatibility, profile revision changes across restart, workspace escapes,
12 simultaneous result writers, mandatory live-run isolation and timeout
cleanup. Existing session and broken-stream tests remain in the full suite.

## Docker verification

Built the image from `evals/Dockerfile`, then ran:

```sh
JEV_TEST_DOCKER_IMAGE=jev-eval:local uv run --no-sync pytest -q tests/test_eval_isolation_live.py
```

Result: **1 passed**. The container could write task files, but could not read
a host-only sentinel, inherit a host credential variable, write the root
filesystem, or connect to an external IP. A timed-out command was removed and
a subsequent command succeeded.

## Deployed service verification

Backed up SQLite using its backup API and saved the local configuration.
Updated the launchd checkout, validated configuration, and restarted it.
Preserved existing host settings, aliases, model profiles and quota commands;
added the shipped strict-session alias and quality lanes. Adaptive effort
remained off and extra semantic questions remained disabled.

A bounded smoke session used real HTTP sockets, Jev classification and the
configured upstream provider:

1. Health exposed `auto-session`.
2. Jev admitted the simple request to `simple-work`, binding
   `ollama/glm-5.3-flash(none)` with adaptation off.
3. Strict execution returned the requested `OK` answer.
4. Reusing its completed request ID returned `REQUEST_ALREADY_COMPLETED`.
5. Disconnecting during streaming released the session's execution claim.
   Reusing that request ID returned `EXECUTION_OUTCOME_UNKNOWN`.
6. A fresh request completed on the same binding.
7. After a second launchd restart, resume returned an identical binding and
   execution contract. Another real upstream request completed.
8. The smoke session was explicitly closed.

These checks establish behavior for this deployment and the exercised profile.
They do not establish coding-task quality, quota savings, every provider's
compatibility, or adaptive-effort benefit. The multi-arm executable benchmark
and the remaining consolidation milestones have not been run in this batch.
