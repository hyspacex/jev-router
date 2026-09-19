# Evaluation report: <what changed>, <date>

Copy this file, fill it in, and delete the guidance in italics. Four kinds of
evidence go in four sections, and they do not substitute for each other. A
strong classification score is not an answer about completed work, and a
simulator result is not an observed subscription delta.

Attach the `manifest.json` of every run named below. A number without its
manifest cannot be reproduced and should not be quoted.

---

## 0. What this is asking

*One sentence. What changed, and what would have to be true for it to be worth
keeping.*

**Change under test:**
**Compared against:**
**Decision this report is meant to support:**

---

## 1. Policy replay

*Cache-only runs. They say what the policy does with answers somebody else's
run already paid for. They are not a live classifier measurement and they are
not a latency measurement. Manifests for these read `kind: policy_replay`.*

| run | manifest | cases | split | kind |
| --- | --- | ---: | --- | --- |
|  |  |  |  |  |

### Classification, per question

| question | against | agreement | labelled | abstained |
| --- | --- | --- | ---: | ---: |
|  |  |  |  |  |

### Routing

| arm | tier correct | under-routed | over-routed | mean cost |
| --- | --- | --- | --- | ---: |
|  |  |  |  |  |

**Paired bootstrap against the baseline:** p = , on n = paired cases.

**Controls.** All four must fail. If any of them scores well the harness is
broken and every other number here is void.

| control | expected | observed |
| --- | --- | --- |
| shuffled labels | at chance |  |
| constant state | at the majority-class rate |  |
| constant policy | at the fixed-route baseline |  |
| length-only rules | no better than chance on tier |  |

*Every rate carries its Wilson interval. On this case set two independent runs
cannot separate a difference under about 11 points, so a paired test is what
decides. No conclusion is drawn from a slice table: the largest slice is 46
cases and the smallest is 10.*

---

## 2. Live qualification

*Runs that actually called Jev or a candidate model. Latency, token usage,
error rate, batching overhead and answer stability belong here and nowhere
else. Manifests read `kind: live` or `kind: mixed`.*

| measurement | value | n | notes |
| --- | --- | ---: | --- |
| Jev latency, median |  |  |  |
| Jev latency, p95 |  |  |  |
| state size, median tokens |  |  |  |
| batching overhead, active against active plus shadow |  |  | measured, not inferred |
| answers that came back unusable |  |  |  |
| shadow answers dropped as invalid |  |  |  |

**Admission verdicts recorded in POOL.md:**

---

## 3. Coding outcomes

*Executable sessions from `evals/run_sessions.py`. Completed work, not routing
labels. A capped or unfinished session is listed, never dropped.*

| arm | completed | capped | errored | calls | median wall clock |
| --- | --- | ---: | ---: | ---: | ---: |
|  |  |  |  |  |  |

| task | family | arm A | arm B |
| --- | --- | --- | --- |
|  |  |  |  |

**Paired by task:** p = , on n = paired tasks.

**Capped and unfinished sessions:**

*A pilot of eight tasks is for finding failures and measuring cost. It cannot
establish noninferiority, and a run with no failures does not either. Declare
the tolerable quality loss before a larger trial; an interval too wide to
assess that margin is inconclusive, not a pass.*

---

## 4. Subscription and cache observations

*Two separate things, and mixing them is the mistake this section exists to
prevent.*

### 4a. Simulated

*From `evals/quota_sim.py`. Replayed readings through the router's own pure
functions. Label every number here `simulated`. Never quote a quota saving
from `ROUTE_COST`: those weights order routes, they do not measure a
subscription.*

| scenario | admissions | placed | deferred under pressure | unknown readings |
| --- | ---: | --- | ---: | ---: |
|  |  |  |  |  |

### 4b. Observed

*Real allowance deltas, with their coverage limitation stated. If the provider
reports only coarse account-level percentages, give the interval and say so
rather than inventing per-request precision. An observed delta is not
attributed to a session when competing traffic makes that attribution
impossible.*

| provider | window | before | after | coverage | can this be attributed? |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

**API tokens and cost, recorded separately from subscription consumption:**

---

## 5. Verdict

| gate | met? | evidence |
| --- | --- | --- |
| no unexplained contract regression |  |  |
| benefit on a frozen set, or a clear error reduction |  |  |
| overhead inside the latency and call budget |  |  |
| not merely reshaping labels |  |  |

**Decision:** promote / keep in shadow / remove.

**What would change this verdict:**

**Limitations, stated plainly:**
