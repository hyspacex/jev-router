# Running coding evaluations safely

Build the disposable command image once:

```sh
docker build -t jev-eval:local -f evals/Dockerfile .
uv run python evals/run_sessions.py --container-image jev-eval:local --arms fixed_strong,simple_rules,jev_mean --repeats 2
```

The harness talks to the router on the host. Generated commands and hidden
checks run in a new Docker container for each invocation. Only the task
workspace is mounted. Networking is disabled, the root filesystem is read-only,
capabilities are dropped, and CPU, memory, process count and temporary storage
are limited. No router or provider credentials are passed to the container.
Containers are removed after completion or timeout, including background work.
Docker must use a local daemon so the workspace mount names the intended files.

Use an image you trust. Install task dependencies at image build time; runtime
package downloads cannot work without networking. The harness and task specs
are trusted inputs. This is a Docker boundary, not a defense against container
runtime vulnerabilities. For hostile workloads, use a disposable VM as well.

The Python Workspace API still supports local execution for trusted offline
fixtures. A temporary working directory and a scrubbed environment alone do
not prevent access to host files or the network. The live CLI requires an image
and does not fall back to host execution when Docker fails.

File-tool containment rejects resolved paths outside the workspace, including
sibling names with a shared prefix and symlink escapes. That check is separate
from command isolation. Results retain the selected image name in the manifest.

Verify the Docker boundary without any model calls:

```sh
JEV_TEST_DOCKER_IMAGE=jev-eval:local uv run pytest -q tests/test_eval_isolation_live.py
```

This checks host-file and environment isolation, disabled networking, a
read-only root filesystem, writable task files, and recovery after a timeout.

The three baseline arms need the upstream client credential in
`UPSTREAM_API_KEY` and the router control credential in
`JEV_ROUTER_ADMIN_TOKEN`. Neither is passed into task containers. Use a
separate router configuration and SQLite file for benchmark runs. That
configuration has to define two strict aliases: `auto`, which the shipped
`router.yaml` already has, and `auto-session-rules`, a copy of it with
`decider: rules` for the deterministic arm. The live client verifies the rules
decision source. The Jev arm uses `auto` and records any admission fallback.

All arms receive the same tools and a 4,096-token output limit. Admission sees
the actual tool schemas. Task-specific call and time caps still apply; CLI
caps may tighten them. The runner rotates arm order by task, closes strict
sessions, and saves a checkpoint after each session. Provider token usage is
recorded when returned; it is not a measurement of subscription quota savings.
