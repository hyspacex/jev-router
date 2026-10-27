# Dashboard v3 prototype: design notes

`dashboard-v3.html` is a static prototype of the audit UI, on sample data
shaped like the live `/router/audit` payloads. It is not wired to the router.
Open it in a browser; `#overview`, `#sessions`, `#session`, `#rules`.

## What is wrong with the current page

1. Missing data is the loudest thing on screen. Every "not recorded" is an
   amber bordered chip, and amber is also the colour of real problems. On the
   live Sessions list a whole column is amber chips; on a session page the
   calls footer is six of them in a row.
2. Everything has the same weight. Quota, four tiles, a heatmap, a latency
   table and a chart are all bordered panels of the same size, so nothing
   reads as the headline.
3. Nine columns and eight dropdowns, most carrying nothing. Client is `pi` on
   43 of 47 rows, Workspace and Feedback are empty, Turns is a gap.
4. Mono is used for everything (models, rules, numbers, ids, paths), so it no
   longer marks anything.
5. Mint means selected, good, up, filled and accent at once; amber means
   warning, gap, too weak and corrective.
6. The session page shows every column at once: request, scores and ladder
   side by side, plus facts, lane paths and quota pressures.
7. The Rules page is dominated by the full YAML editor.

## Principles applied (from the research pass)

- One element per view dominates; the chrome recedes (Linear, Few).
- Headline first, evidence one click away, never more than two levels
  (NN/g progressive disclosure, Stripe "current value above the chart").
- Colour only for status, always with a text label; one neutral accent for
  selection (Vercel guidelines, Grafana). Here: mint good, amber warning,
  red failure, blue "needed less"; selection is a lighter surface.
- Missing is a muted `–` with a tooltip, explained once per column, never
  blank and never zero (Cloudscape, Vercel AI Gateway logs).
- Structure by spacing and one surface step, borders only between surfaces
  (Refactoring UI, Linear's 2026 refresh).
- Four type sizes, tabular figures everywhere, mono only for identifiers
  (Geist typography).
- More than two filters becomes search plus removable chips plus saved views
  (Cloudscape, Linear, Stripe, Sentry).
- A list row is a summary; the first column is a human title (NN/g tables).
- A detail page is: header, one-line summary, primary pane, secondary pane,
  with raw data last (Honeycomb, Datadog, Braintrust, Langfuse).
- A trace is a vertical stepper with one line per step; only the deciding
  step is open (Carbon, Vercel routing cards).

## What changed per view

Overview: a four-stat headline band with one comparator each and a `?` for
the definition; a footnote for what the upstream does not report. Requests
and quota side by side; the latency table and the admissions count merge into
one "By model" table; the task heatmap is collapsed under it.

Sessions: saved views (All, Problems, Unrated, Pinned), one search box that
also takes `rule:hard` style filters, active filters as chips. Six columns:
Session (title, then client · workspace · turns), last seen, model · effort,
rule, verdict, problems count. Pre-logging sessions are explained once.

Session: title, one strip of facts, feedback at the right. A one-line
decision sentence ("Admitted by moderate_tools · difficulty 2.33 ≥ 1.4 with
tools"). Left: a vertical list of turns. Right: Why this rule (gate, missed
rules folded, the hit, quota and lane), Jev scores (one row per question),
Request, Calls (closed).

Rules: the ladder is the page; scores and settings in a side column; the YAML
editor opens on demand.

## Open questions for review

- Should a session open in a side panel over the list (Langfuse peek, j/k)
  instead of its own page?
- Keep Rule as a column, or move it into the row's second line?
- The quota meters are honest at 1 to 3% and nearly empty. The expected tick
  carries the pace; is the "−11 vs pace" number still wanted?
