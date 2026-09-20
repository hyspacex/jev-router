# Evaluating and tuning

[Documentation index](../README.md) · [Project home](../../README.md)

Live evaluations spend provider quota. Start with offline tests and dry runs; use a separate benchmark config and database.

## Evaluating and tuning

Everything under `evals/` measures the router against 171 labelled requests in
`evals/cases.yaml`. Every case carries a `slice` saying what shape of request
it is:

| slice | n | slice | n |
| --- | --- | --- | --- |
| agentic | 46 | multilingual | 16 |
| chat | 45 | longcontext | 11 |
| adversarial | 27 | pipeline | 10 |
| multiturn | 16 | | |

The cases are split 70/30 by a fixed seed, stratified by task label: choices
are made on the 120 tuning cases and reported on the 51 held-out ones. A slice
can also be held out whole, which is a harder test than a row split, because a
row split leaves near-duplicates of a held-out case in the tuning half.

The scripts that cost quota need `TYPESAFE_API_KEY`; `run_outcomes.py` also
needs `UPSTREAM_API_KEY` and `JEV_ROUTER_UPSTREAM`, because it calls the
candidate models for real.

```sh
# Does Jev classify the request, and does the policy route it?
uv run python evals/run_eval.py --group final --repeats 3

# The same, plus a re-decide under four seeded rewordings of each request.
uv run python evals/run_eval.py --variants router_yaml --stability

# Two runs that are supposed to score badly. If they do not, the harness is broken.
uv run python evals/run_eval.py --variants router_yaml --control shuffled-labels
uv run python evals/run_eval.py --variants router_yaml --control constant-state

# Run the gradeable cases on every candidate route, three times each, and grade them.
uv run python evals/run_outcomes.py --samples 3 --max-calls 400

# Search the policy's numbers and print a diff. Optionally hold a whole slice out.
uv run python evals/tune.py --variant router_yaml --samples 5000
uv run python evals/tune.py --variant router_yaml --slice-holdout agentic

# Sample public datasets into a git-ignored out-of-distribution set.
uv sync --group evals
uv run python evals/import_public.py --limit 25
uv run python evals/run_eval.py --variants router_yaml --public evals/cases_public.yaml
```

Two more controls, and the four packet arms:

```sh
uv run python evals/run_eval.py --variants router_yaml --control constant-policy
uv run python evals/run_eval.py --variants router_yaml --control length-only
uv run python evals/run_eval.py --packet --repeats 3
```

Four scripts do no live work of their own, or none by default:

```sh
uv run python evals/quota_sim.py --demo         # simulated; never an observed delta
uv run python evals/effort_replay.py            # offline, synthetic turn fixtures
uv run python evals/qualify_effort.py --profile <key> --plan   # prints the plan and stops
uv run python evals/run_sessions.py --list
uv run python evals/run_sessions.py --dry-run
```

`quota_sim.py` replays a clock, a set of quota windows, their resets and
whatever telemetry failures you give it through the router's own pure
functions. Everything it prints is labelled `simulated`, and it never claims a
quota saving from `ROUTE_COST`: those weights order routes, they do not measure
a subscription.

`run_sessions.py` runs the eight pilot coding tasks in `evals/session_tasks/`,
each in a disposable directory with a scrubbed environment and hard call and
wall-clock caps. `--list` and `--dry-run` need no credentials; anything else
needs a router, an upstream and a local Docker image for isolated commands and
hidden checks:

```sh
docker build -t jev-eval:local -f evals/Dockerfile .
uv run python evals/run_sessions.py --container-image jev-eval:local \
    --arms fixed_strong,simple_rules,jev_mean --repeats 2
```

See [`evals/ISOLATION.md`](../../evals/ISOLATION.md) for credentials, the required
`auto-session-rules` alias and the Docker boundary. Eight tasks cannot separate
two close policies and the report says so.

`effort_replay.py` compares the between-turn effort policies over synthetic
turn sequences and makes no calls. `qualify_effort.py` plans by default;
running it live needs `--confirm-live`, `JEV_ROUTER_QUALIFY_LIVE=1`, an
allowlisted profile, credentials and a call budget. The allowlist is empty.

Every run writes `manifest.json` and `manifest.md` beside it: the repo SHA, the
question and policy hashes, the packet version, the candidate profiles, the
case ids and splits, the seeds, the cache condition, the caps and everything
that did not finish. A run that answered entirely out of the cache is labelled
`policy_replay`, which says what the policy does with answers somebody else
paid for. It is not a live classifier measurement and not a latency one.

`run_eval.py` scores Jev's answers against the labels and then feeds those
answers through `policy.py` to score the route. It writes
`evals/results/<timestamp>/summary.md` and `per_case.jsonl`, and points
`evals/results/latest` at the newest run. Answers are cached on disk by a hash
of the request, so a rerun is free; the repeats that measure stability always
go to the network. A variant is one state builder plus one set of question
wordings, defined in `evals/variants.yaml` as an overlay on `router.yaml`.
`router_yaml` is this file as it stands and `--group` runs a named set.

`evals/metrics.py` holds the definitions the scripts share: Wilson intervals,
expected calibration error, the cost-matched random baseline, Gap@Oracle and
Gain@BestSingle, and the seeded perturbations behind the stability number.

`run_outcomes.py` runs each gradeable case on all seven candidate routes, k
times per route, and grades the replies with a programmatic check from
`evals/checks.py`, a blind judge against a rubric, or both. Where a check
exists it decides the score and the judge is scored against it. It needs
`UPSTREAM_API_KEY` and `JEV_ROUTER_UPSTREAM` as well, because it calls the
models for real. Cases run one at a time in priority order under `--max-calls`,
so a run that hits its budget leaves whole cases finished and resumes from the
cache. It also stops itself if a route's own median latency triples or replies
start coming back empty. It writes `evals/outcomes.json` and
`evals/outcomes.md`; `--apply-labels` writes the clear-cut label changes back
into `cases.yaml`, and never writes one from a case whose winner is `unstable`.

`import_public.py` samples WildChat (ODC-BY), BFCL, LongBench v2 and MGSM into
`evals/cases_public.yaml`. That file is git-ignored on purpose: real user
conversations can hold personal data and must not be republished from here. The
script drops anything the dataset flags as toxic and anything matching an email
address, a phone number or a card-shaped number, and it never writes a label of
its own. Labels live beside it in `cases_public_labels.yaml`.

`tune.py` reads the cached answers, `evals/outcomes.json`, and the `decisions`
and `feedback` tables in the sqlite log. Feedback rows count three times. It
searches for the cheapest set of thresholds that keeps under-routing at or
below 5% on the tuning split and prints a unified diff. It never edits
`router.yaml`: read the diff, apply it yourself, and the `config_hash` in the
log marks the boundary between policy versions.

`evals/EXPERIMENTS.md` records every change that was tried, with the numbers
before and after, including the ones that did not help.
