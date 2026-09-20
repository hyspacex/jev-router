# Executable coding-session tasks

Spec 13.5. Each file here is one task: a starting repository, a first user
turn, the tools the session may use, and a check that says whether the work is
actually done. `evals/run_sessions.py` runs them.

These eight are a **pilot**. They exist to find failures and to measure what a
session costs, not to prove a small improvement. Eight tasks and two repeats
cannot separate policies that are close, and the report says so rather than
leaving it to be inferred.

```sh
uv run python evals/run_sessions.py --list      # no credentials needed
uv run python evals/run_sessions.py --dry-run   # shows what would run, no calls
uv run python evals/run_sessions.py --arms fixed_strong,jev_packet --repeats 2
```

Five arms: `fixed_strong`, `simple_rules`, `jev_mean`, `jev_packet`, and
`fixed_effort_vs_adaptive`, which needs `--allow-adaptive-effort` and a profile
that has passed `evals/qualify_effort.py`. Without one the router resolves its
sessions with adaptation off and that arm measures nothing.

Anything past `--list` and `--dry-run` needs a router to resolve against and an
upstream that serves the candidate models, and spends real quota. Nothing in
CI makes a call: the offline tests drive the runner with fake clients.

## The format

```yaml
id: csv-quoted-fields          # unique, and the directory name in the run
family: implementation         # one of the six families below
description: one line
prompt: the first user turn
turns: [...]                   # optional later user turns, in order
allowed_tools: [read_file, write_file, list_files, run_command]
repo:
  files:                       # an inline fixture repository
    src/thing.py: |
      ...
  # or, instead of `files`:
  git: {url: ..., commit: <sha>}   # a fixed start commit
check:
  command: [python, -m, pytest, -q, -x]
  hidden_files:                # written only when the check runs
    tests/test_hidden.py: |
      ...
  expect_fail_before: true     # the check must fail on the untouched repo
caps:
  max_calls: 12
  wall_clock_seconds: 300
```

Two rules the format exists to keep:

- **The hidden check is not authored by the thing being evaluated.** Files
  under `check.hidden_files` are written into the workspace immediately before
  the check runs and deleted after, so a session cannot read them, edit them
  or make them pass by rewriting them.
- **`expect_fail_before` is verified.** Before the session starts, the runner
  runs the check on the untouched repository. A check that already passes is
  reported as broken rather than counted as a success.

## Families

| family | what it is for |
| --- | --- |
| `mechanical` | bounded edits and repetitive transformations |
| `implementation` | normal implementation with meaningful edge cases |
| `diagnosis` | ambiguous or interacting-component diagnosis |
| `context-growth` | sessions whose history grows and has to be managed |
| `multi-turn` | a sequence that goes routine, demanding, routine again |
| `environment` | missing information or a broken environment, where more reasoning is not the remedy |

## Safety

Every session runs in a disposable temporary directory with a scrubbed
environment: no inherited API keys, `HOME` inside the workspace, and no
network credentials. Nothing here may be pointed at a real working repository.
