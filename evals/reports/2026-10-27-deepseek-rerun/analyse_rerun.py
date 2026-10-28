"""Offline analysis of the rerun, by the rules in plan.json. No provider calls."""

import json
import random
import statistics
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "evals"), str(ROOT / "src"), str(HERE.parent / "2026-10-27-model-refresh")]
import admission as A  # noqa: E402
from analyse import DRAWS, bootstrap, compare  # noqa: E402

PLAN = json.loads((HERE / "plan.json").read_text())
CACHE = ROOT / "evals/.cache/rerun-2026-10-27"


def score(text: str, checks: list[dict], drop_length: bool) -> float:
    if drop_length:
        checks = [c for c in checks if c["kind"] != "max_words"] or checks
    return A.run_checks(text, checks)[0]


def ratio_ci(cand: list[float], ref: list[float]) -> dict:
    ratios = [c / r for c, r in zip(cand, ref, strict=True) if r]
    rng = random.Random(11)
    draws = sorted(statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(DRAWS))
    return {"median_ratio": round(statistics.median(ratios), 3), "ci95": [round(draws[250], 3), round(draws[9750], 3)]}


def main() -> None:
    specs = yaml.safe_load((ROOT / "evals/outcome_specs.yaml").read_text())["cases"]
    report: dict = {"steps": {}}
    lines = ["# DeepSeek 1017 vs GLM rerun: measurements", "",
             ("95% case-bootstrap intervals, 10,000 draws, seed 11. Each arm's per-case value is the mean "
              "(scores) or median (seconds) of its 3 samples. Differences are DeepSeek minus the named GLM arm. "
              "Latency is streamed end to end through the deployed CLIProxyAPI proxy, one request at a time, arms interleaved."), ""]
    for step, s in PLAN["steps"].items():
        recs: dict[str, dict[str, list[dict]]] = {}
        for f in CACHE.glob(f"{step}__*.json"):
            r = json.loads(f.read_text())
            recs.setdefault(r["arm"], {}).setdefault(r["case"], []).append(r)
        complete = [c for c in s["cases"] if all(len(recs.get(a, {}).get(c, [])) == PLAN["samples"] for a in s["arms"])]
        missing = sorted(set(s["cases"]) - set(complete))
        arms = {}
        for arm in s["arms"]:
            per = {c: recs[arm][c] for c in complete}
            arms[arm] = {
                "primary": {c: statistics.mean(score(r["text"], specs[c]["checks"], False) for r in rs) for c, rs in per.items()},
                "no_length_caps": {c: statistics.mean(score(r["text"], specs[c]["checks"], True) for r in rs) for c, rs in per.items()},
                "total_s": {c: statistics.median(r["total_s"] for r in rs) for c, rs in per.items()},
                "first_answer_s": {c: statistics.median(r["first_answer_token_s"] or r["total_s"] for r in rs) for c, rs in per.items()},
                "tokens": [r["completion_tokens"] or 0 for rs in per.values() for r in rs],
                "reasoning_chars": [r["reasoning_chars"] for rs in per.values() for r in rs],
                "empty": sum(1 for rs in per.values() for r in rs if r["error"]),
                "truncated": sum(1 for rs in per.values() for r in rs if r["finish"] == "length"),
                "models_returned": sorted({str(r["model_returned"]) for rs in per.values() for r in rs}),
            }
        out = {"complete_cases": len(complete), "missing_cases": missing, "arms": {}, "paired": {}}
        lines += [f"## {step} ({len(complete)} cases)", ""]
        if missing:
            lines += [f"Incomplete cases left out of every arm: {', '.join(missing)}", ""]
        lines += ["| arm | score, 95% CI | without word caps | median seconds, 95% CI | first answer token s | median completion tokens | empty | truncated | model returned |",
                  "|---|---|---|---|---|---:|---:|---:|---|"]
        for arm, a in arms.items():
            vals = list(a["primary"].values())
            nlc = list(a["no_length_caps"].values())
            tot = list(a["total_s"].values())
            fa = list(a["first_answer_s"].values())
            rec = {
                "score_mean": round(statistics.mean(vals), 3), "score_ci95": bootstrap(vals),
                "no_length_caps_mean": round(statistics.mean(nlc), 3), "no_length_caps_ci95": bootstrap(nlc),
                "median_total_s": round(statistics.median(tot), 3), "total_ci95": bootstrap(tot, statistics.median),
                "median_first_answer_s": round(statistics.median(fa), 3), "first_answer_ci95": bootstrap(fa, statistics.median),
                "median_completion_tokens": statistics.median(a["tokens"]),
                "median_reasoning_chars": statistics.median(a["reasoning_chars"]),
                "empty": a["empty"], "truncated": a["truncated"], "models_returned": a["models_returned"],
                "per_case": {"primary": a["primary"], "total_s": a["total_s"]},
            }
            out["arms"][arm] = rec
            lines.append(
                f"| {arm} | {rec['score_mean']:.2f} {rec['score_ci95']} | {rec['no_length_caps_mean']:.2f} {rec['no_length_caps_ci95']} "
                f"| {rec['median_total_s']:.2f} {rec['total_ci95']} | {rec['median_first_answer_s']:.2f} {rec['first_answer_ci95']} "
                f"| {rec['median_completion_tokens']:.0f} | {rec['empty']} | {rec['truncated']} | {', '.join(rec['models_returned'])} |")
        lines += ["", "| DeepSeek vs | score difference, 95% CI, p | without word caps | total-time ratio, 95% CI | first-answer ratio, 95% CI |",
                  "|---|---|---|---|---|"]
        for ref in s["arms"]:
            if ref == "deepseek":
                continue
            d, g = arms["deepseek"], arms[ref]
            q = compare([d["primary"][c] for c in complete], [g["primary"][c] for c in complete])
            q2 = compare([d["no_length_caps"][c] for c in complete], [g["no_length_caps"][c] for c in complete])
            t = ratio_ci([d["total_s"][c] for c in complete], [g["total_s"][c] for c in complete])
            f = ratio_ci([d["first_answer_s"][c] for c in complete], [g["first_answer_s"][c] for c in complete])
            out["paired"][ref] = {"quality": q, "quality_no_length_caps": q2, "total_time_ratio": t, "first_answer_ratio": f}
            lines.append(
                f"| {ref} | {q['difference']:+.2f} {q['ci95']}, p={q['paired_centred_bootstrap_p']:.3f} "
                f"| {q2['difference']:+.2f} {q2['ci95']} | {t['median_ratio']:.2f} {t['ci95']} | {f['median_ratio']:.2f} {f['ci95']} |")
        inc = s["production_incumbent"]
        p = out["paired"][inc]
        di, gi = out["arms"]["deepseek"], out["arms"][inc]
        quality_ok = gi["score_mean"] - di["score_mean"] <= 0.5
        faster = di["median_total_s"] <= gi["median_total_s"] * 0.7
        out["verdict"] = {
            "official_admission_rule": {"quality_ok": quality_ok, "30pct_faster": faster, "passed": quality_ok and faster},
            "non_inferior": p["quality"]["ci95"][0] > -0.5,
            "superior": p["quality"]["ci95"][0] > 0,
            "proposed_quality_path": p["quality"]["ci95"][0] > 0 and p["total_time_ratio"]["median_ratio"] <= 1.5,
        }
        v = out["verdict"]
        lines += ["", (f"Against production `{inc}`: official rule {'PASS' if v['official_admission_rule']['passed'] else 'FAIL'} "
                       f"(quality within 0.5: {quality_ok}; 30% faster: {faster}). Non-inferior: {v['non_inferior']}. "
                       f"Superior: {v['superior']}. Proposed quality path: {v['proposed_quality_path']}."), ""]
        report["steps"][step] = out
    (HERE / "analysis.json").write_text(json.dumps(report, indent=2))
    (HERE / "measurements.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
