"""Offline paired analysis of the refresh; never makes provider calls."""

import json
import random
import statistics
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "evals"), str(ROOT / "src")]
import admission as A  # noqa: E402
from common import DiskCache, load_cases  # noqa: E402
from metrics import wilson  # noqa: E402

OUT = Path(__file__).resolve().parent
DRAWS = 10000


def bootstrap(values, statistic=statistics.mean):
    rng = random.Random(11)
    draws = sorted(statistic(rng.choices(values, k=len(values))) for _ in range(DRAWS))
    return [round(draws[250], 4), round(draws[9750], 4)]


def compare(left, right):
    diffs = [a - b for a, b in zip(left, right, strict=True)]
    observed = statistics.mean(diffs)
    centred = [d - observed for d in diffs]
    rng = random.Random(11)
    hits = sum(
        abs(statistics.mean(rng.choices(centred, k=len(centred)))) >= abs(observed)
        for _ in range(DRAWS)
    )
    return {
        "cases": len(diffs),
        "difference": round(observed, 4),
        "ci95": bootstrap(diffs),
        "paired_centred_bootstrap_p": (hits + 1) / (DRAWS + 1),
    }


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read evaluation artifact {path}") from exc


def main():
    cfg = yaml.safe_load(A.SPEC_FILE.read_text())
    specs = yaml.safe_load((ROOT / "evals/outcome_specs.yaml").read_text())["cases"]
    cases = {c.id: c for c in load_cases()}
    cache = DiskCache("upstream")
    results = read_json(OUT / "results.json")
    controls = {"glm-5.3-flash", "glm-5.3", "astra-medium", "astra-high", "sol6-xhigh"}
    analysis = {"draws": DRAWS, "seed": 11, "steps": {}}
    lines = [
        "# Model refresh: paired measurements",
        "",
        "95% case-bootstrap intervals, 10,000 draws, seed 11. Differences are candidate minus reference. Fresh latency uses only this run's dated control sample; candidates use their two fresh samples averaged within case. Neither latency nor output tokens measure subscription savings.",
        "",
    ]
    for name, step in results["steps"].items():
        report = {"routes": {}, "paired": {}}
        score_by_route = {}
        lines += [f"## {name}", ""]
        if name == "refresh_hard":
            lines += [
                "**Incomplete, no admission verdict.** Sol 6.1 xhigh repeatedly timed out; remaining cases were not run. Historical controls cover more cases than candidates. Scores below are available-case descriptions, not a completed comparison; failed attempts are retained in the wire ledger. Fresh latency intervals exclude unknown timeout durations and must not be used to excuse them.",
                "",
            ]
        lines += [
            "| route | cases | score, 95% CI | fresh median seconds, 95% CI (n) |",
            "|---|---:|---|---|",
        ]
        for route in step["summary"]:
            scores, latencies = {}, []
            for row in step["rows"]:
                if route not in row["routes"]:
                    continue
                cid = row["case_id"]
                cell = row["routes"][route]
                salts = (
                    ["", "refresh-2026-10-27-control-1"]
                    if route in controls
                    else ["", "sample-1"]
                )
                hits = [
                    cache.get(
                        A._key(
                            cfg["routes"][route]["upstream"],
                            A.text_messages(cases[cid]),
                            salt,
                        )
                    )
                    for salt in salts
                ]
                # Regrade every checked model after the punctuation correction.
                good = [h for h in hits if h is not None]
                checks = specs[cid].get("checks")
                scores[cid] = (
                    statistics.mean(A.run_checks(h["text"], checks)[0] for h in good)
                    if checks and good
                    else cell["score"]
                )
                fresh = hits[1:] if route in controls else hits
                if all(h is not None for h in fresh):
                    latencies.append(
                        statistics.mean(h["latency_ms"] for h in fresh if h is not None)
                        / 1000
                    )
            values = list(scores.values())
            record = {
                "cases": len(values),
                "score_mean": statistics.mean(values),
                "score_ci95": bootstrap(values),
                "fresh_median_seconds": statistics.median(latencies)
                if latencies
                else None,
                "latency_ci95": bootstrap(latencies, statistics.median)
                if latencies
                else None,
                "fresh_latency_cases": len(latencies),
                "scores": scores,
            }
            report["routes"][route] = record
            score_by_route[route] = scores
            lines.append(
                f"| {route} | {len(values)} | {record['score_mean']:.2f} {record['score_ci95']} | {record['fresh_median_seconds']:.2f} {record['latency_ci95']} (n={len(latencies)}) |"
            )
        refs = [step["incumbent"]]
        if name == "refresh_mid":
            refs += ["deepseek41-low"]
        if name == "refresh_hard":
            refs += ["sol6-xhigh"]
        lines += [
            "",
            "| candidate vs reference | paired cases | difference, 95% CI | paired p |",
            "|---|---:|---|---:|",
        ]
        for ref in refs:
            for route, candidate_scores in score_by_route.items():
                if route == ref:
                    continue
                common = sorted(candidate_scores.keys() & score_by_route[ref].keys())
                if not common:
                    continue
                key = f"{route} vs {ref}"
                value = compare(
                    [candidate_scores[c] for c in common],
                    [score_by_route[ref][c] for c in common],
                )
                report["paired"][key] = value
                lines.append(
                    f"| {key} | {len(common)} | {value['difference']:+.2f} {value['ci95']} | {value['paired_centred_bootstrap_p']:.4f} |"
                )
                if name == "refresh_hard":
                    for population in ["checks", "judge"]:
                        subset = [
                            c
                            for c in common
                            if bool(specs[c].get("checks")) == (population == "checks")
                        ]
                        if subset:
                            report["paired"][key + " / " + population] = compare(
                                [candidate_scores[c] for c in subset],
                                [score_by_route[ref][c] for c in subset],
                            )
        lines += [""]
        analysis["steps"][name] = report
    sessions_file = OUT / "sessions/sessions.json"
    if sessions_file.exists():
        rows = read_json(sessions_file)
        analysis["sessions"] = {}
        for arm in sorted({r["arm"] for r in rows}):
            subset = [r for r in rows if r["arm"] == arm]
            complete = sum(r["completed"] for r in subset)
            analysis["sessions"][arm] = {
                "completed": complete,
                "sessions": len(subset),
                "wilson95": wilson(complete, len(subset)),
                "calls": sum(r["calls"] for r in subset),
                "median_wall_seconds": statistics.median(
                    r["wall_clock_s"] for r in subset
                ),
                "wall_ci95": bootstrap(
                    [r["wall_clock_s"] for r in subset], statistics.median
                ),
            }
        base = {r["task"]: r for r in rows if r["arm"] == "refresh_glm_high"}
        analysis["session_pairs"] = {}
        for arm in ["refresh_deepseek_max", "refresh_sol61_low"]:
            cand = {r["task"]: r for r in rows if r["arm"] == arm}
            shared = sorted(base.keys() & cand.keys())
            analysis["session_pairs"][arm] = compare(
                [float(cand[t]["completed"]) for t in shared],
                [float(base[t]["completed"]) for t in shared],
            )
    (OUT / "analysis.json").write_text(json.dumps(analysis, indent=2))
    (OUT / "measurements.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
