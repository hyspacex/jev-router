# Deploying it

This is how the router is actually run: one macOS launchd service, one git
checkout, one sqlite file. It is a single-owner local tool, so there is no
container, no process manager beyond launchd, and no clustering.

Every value below is a placeholder. Put no real token, host or key in a file
you commit.

## The shape of it

```
~/src/jev-router          a git checkout, on a tag or a commit you chose
~/.config/jev-router.yaml your config, outside the checkout
~/.jev-router/router.db   pins, decisions, feedback, sessions, the ledger
~/Library/LaunchAgents/…  the launchd plist
```

The config lives outside the checkout on purpose. `router.yaml` in the
repository is the shipped file the evals measure: leave it alone, and start
your own from a copy.

```sh
git clone <your-remote> ~/src/jev-router
cd ~/src/jev-router
uv sync
cp router.yaml ~/.config/jev-router.yaml
```

## `run.sh`

launchd gives a job almost no environment, so the secrets and the config path
are set in a wrapper rather than in the plist.

```sh
#!/bin/sh
# ~/src/jev-router/run.sh   (chmod +x)
set -eu

export JEV_ROUTER_ADMIN_TOKEN="<router control credential>"
export JEV_ROUTER_UPSTREAM="http://<your-proxy-host>:8317"
export JEV_ROUTER_CONFIG="$HOME/.config/jev-router.yaml"
export TYPESAFE_API_KEY="<your TypeSafe key>"

cd "$HOME/src/jev-router"
exec uv run --no-dev jev-router serve
```

What each one does:

| Variable | Effect |
| --- | --- |
| `JEV_ROUTER_ADMIN_TOKEN` | The credential for `/router/*`. Strict sessions require it on every control and execution request, loopback included, and there is no setting that turns that off. Named by `settings.admin_token_env`. |
| `JEV_ROUTER_UPSTREAM` | Overrides `settings.upstream_base_url` whenever it is set and non-empty. The environment wins over the config file. |
| `JEV_ROUTER_CONFIG` | The default for `-c/--config` on every subcommand, so `check-config` and `sessions` read the same file the service does. |
| `TYPESAFE_API_KEY` | The Jev key, named by `settings.jev_api_key_env`. Without it the router still serves every request, on the `rules` decider, and marks each decision as a fallback. |

The router holds no upstream key of its own. It forwards the client's
`Authorization` header, so the proxy keeps doing authentication.

`--no-dev` skips the test and lint dependencies. `serve` takes `--host`,
`--port`, `--mode shadow|active` and `--log-level`; leaving them off uses
`settings.host`, `settings.port` and `settings.mode` from the config.

Generate the credential once and keep it out of the repository:

```sh
openssl rand -hex 32
```

## The launchd job

```xml
<!-- ~/Library/LaunchAgents/local.jev-router.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>local.jev-router</string>
  <key>ProgramArguments</key>   <array><string>/Users/<you>/src/jev-router/run.sh</string></array>
  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <key>StandardOutPath</key>    <string>/Users/<you>/Library/Logs/jev-router.log</string>
  <key>StandardErrorPath</key>  <string>/Users/<you>/Library/Logs/jev-router.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.jev-router.plist
launchctl kickstart -k gui/$(id -u)/local.jev-router     # restart
launchctl print gui/$(id -u)/local.jev-router            # state and exit code
```

`chmod 600` the plist and `run.sh`: both hold a credential.

**Run exactly one instance.** The router refuses a second strict session
service on the same database inside one process, but that guard is in memory
and does not see another process. Two services sharing `router.db` would each
hold their own in-flight claims, and "one request in flight per session" would
stop meaning anything. One launchd job, one database.

## Checking it

```sh
curl -s localhost:8318/router/health | python3 -m json.tool
```

It reports the mode, the upstream, the aliases, the models, the decider,
whether a Jev key is set, and the uptime. `/router/*` needs
`X-Router-Admin-Token` once a token is set.

## Upgrading

The order matters. Validate against your own config **before** the restart, so
a config the new version refuses is a failed command rather than a service
that will not come back.

```sh
# 1. Back the database up. It is the only thing here that cannot be rebuilt.
cp ~/.jev-router/router.db ~/.jev-router/router.db.$(date +%Y%m%d-%H%M%S)

# 2. Fetch and check out the version you want. Note the commit you are on
#    first: it is what you roll back to.
cd ~/src/jev-router
git rev-parse --short HEAD
git fetch --all --tags
git checkout <tag-or-commit>

# 3. Install exactly what uv.lock says.
uv sync --no-dev

# 4. Validate the new code against YOUR config, before anything restarts.
uv run --no-dev jev-router -c ~/.config/jev-router.yaml check-config

# 5. Only now restart.
launchctl kickstart -k gui/$(id -u)/local.jev-router
curl -s localhost:8318/router/health | python3 -m json.tool
```

`check-config` prints what it loaded: the mode, the upstream, the providers,
models, routes, questions and aliases, the semantic packet, the quality lanes
with their qualification references, and the effort experiment's mode and
ladder. Read that summary, not just the exit code. It is also the command that
refuses `experiments.adaptive_effort.mode: active` when a profile is not
qualified, and refuses a lane entry with no `qualification_ref`.

### What the first start does to the database

Nothing you have to do. The router opens `settings.sqlite_path` and adds any
column this version knows about and the file does not, with
`ALTER TABLE … ADD COLUMN`. Every added column is nullable and defaults to
NULL, nothing is ever dropped, rewritten or retyped, and the tables themselves
are created with `CREATE TABLE IF NOT EXISTS`. Rows written before a feature
existed read back NULL in its columns, which is exactly what "this row
predates the feature" means.

This release adds the `sessions`, `turn_plans` and `session_requests` tables,
five columns to `decisions` for the session and lane bookkeeping, eleven more
for the semantic packet, and the session columns the effort experiment,
reconciliation and compaction need. `check-config` and the log say which
columns were added.

One thing to know: the unique index `turn_plans_one_per_turn` is created
outside the schema script. If the file already holds two plans for one turn —
possible only on a database written by a build from before that index existed —
the index is not created, the rows are left exactly as they are, and the router
logs what it found and carries on.

## Rolling back

Check out the commit you noted, `uv sync --no-dev`, `check-config`, restart.
You do not need to restore the database.

The added columns are harmless to older code, and this is true of the code as
written rather than by convention. Reads are `SELECT *` into a `sqlite3.Row`
and then `dict(row)`, so a column an older build has never heard of is an extra
key nobody looks at; nothing reads a column by position and nothing asserts a
column count. Writes name their columns, so an older build's `INSERT` simply
omits the newer ones and they take NULL. Its own `CREATE TABLE IF NOT EXISTS`
is a no-op against the newer file, and its own migration finds every column it
knows about already present and adds nothing. Its updates are per field and
`COALESCE`-guarded, so rolling back stops new columns being written rather than
clobbering what the newer build wrote. `tests/test_migration.py` and the C33
cases in `tests/test_sessions.py` hold this.

One asymmetry worth knowing. `turn_plans_one_per_turn` is an index, not a
column, and nothing drops it. Roll back past the build that introduced it and
the index stays on the file. That older code inserts a plan with
`INSERT OR IGNORE` and then reads it back by plan id, so a second plan for the
same turn is silently ignored and the caller gets an empty plan rather than an
exception. Nothing is corrupted and no data is lost, but a duplicate-turn race
behaves differently there. It only matters if you were running the effort
experiment, which is off unless you turned it on.

Restore the backup only if you want the decision log back to where it was.
Nothing in the upgrade needs it.

Take the backup anyway. `jev-router prune --older-than-days N` deletes old
decisions, feedback and pins irreversibly, and those rows are the evidence
`evals/tune.py` replays.

## Turning strict sessions on

Strict sessions are opt-in twice: the service has to be enabled, and an alias
has to ask for them. Nothing else in the config changes, and an alias with no
`session_mode` keeps behaving exactly as it did.

In your own `~/.config/jev-router.yaml`:

```yaml
session_routing:
  enabled: true
  prepared_ttl_seconds: 600     # a resolved but never used contract expires
  max_inflight_per_session: 1   # a second concurrent request is a conflict
  require_control_token: true   # the only value there is; false is an error
  max_request_history: 200      # finished request ids kept per session

aliases:
  auto-session:
    description: Pick a model once and keep it for the whole session.
    session_mode: strict
    state_builder: continuation_aware_v1
    questions: [task, difficulty, harm_if_wrong]
    rules: policy
    allowed_models: [ollama/glm-5.3-flash, gpt-6-astra]
    admission_fallback: {route: frontier_medium}
```

Then:

```sh
uv run --no-dev jev-router -c ~/.config/jev-router.yaml check-config
launchctl kickstart -k gui/$(id -u)/local.jev-router
uv run --no-dev jev-router -c ~/.config/jev-router.yaml sessions list
```

`JEV_ROUTER_ADMIN_TOKEN` has to be set before any of this is usable: a strict
session is scoped to the owner of that credential, so there has to be one to
scope it to. `require_control_token: false` is a config error with a message
saying so, not a way around it.

`admission_fallback` is what a session binds when Jev is unreachable. Without
one, an admission with no classifier answer returns `NO_SAFE_ADMISSION` instead
of guessing. Whatever it binds is as fixed as any other binding.

The full contract, the headers a client sends and the fourteen error codes are
in [`SESSION_ROUTING.md`](SESSION_ROUTING.md).

The between-turn effort experiment is a third opt-in on top of this and is off
in the shipped file. Do not turn it on without reading
[`ADAPTIVE_EFFORT.md`](ADAPTIVE_EFFORT.md): `active` needs a deployment
somebody has qualified, and `check-config` refuses it otherwise.

## Beyond loopback

The default is `127.0.0.1`. Serving anywhere else needs the admin token set and
trusted ingress in front of it — a Tailscale ACL or equivalent. Upstream
authentication happens after classification, so it does not protect the Jev
quota: anyone who can reach the port can spend it. Do not proxy remote traffic
into loopback without setting the token, because the loopback exemption on the
read-only `/router/*` endpoints trusts the peer address and nothing else.

## Retention

```sh
uv run --no-dev jev-router -c ~/.config/jev-router.yaml prune --older-than-days 30
```

Irreversible, and nothing runs it automatically. It deletes old decisions,
feedback and pins, and old session request and plan rows. A pruned session
leaves a tombstone, so resuming its id answers `SESSION_CLOSED` and never
silently starts a new session.
