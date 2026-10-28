"""Regrade the 2026-10-27 refresh's easy and moderate replies from cache.

Offline: reads cached replies, never calls a provider. Every route is
regraded under the same checker (after the re.VERBOSE / triple-quote fix),
twice: as specified, and with unprompted `max_words` checks left out. The
second is a secondary reading, never the admission score.
"""

import json
import statistics
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "evals"), str(ROOT / "src"), str(HERE.parent / "2026-10-27-model-refresh")]
import admission as A  # noqa: E402
from analyse import bootstrap, compare  # noqa: E402
from common import DiskCache, load_cases  # noqa: E402

CONTROLS = {"glm-5.3-flash", "glm-5.3"}
STEPS = {"refresh_fast": "glm-5.3-flash", "refresh_mid": "glm-5.3"}


def score(text: str, checks: list[dict], drop_length: bool) -> float:
    if drop_length:
        checks = [c for c in checks if c["kind"] != "max_words"] or checks
    return A.run_checks(text, checks)[0]


def main() -> None:
    cfg = yaml.safe_load(A.SPEC_FILE.read_text())
    specs = yaml.safe_load((ROOT / "evals/outcome_specs.yaml").read_text())["cases"]
    cases = {c.id: c for c in load_cases()}
    cache = DiskCache("upstream")
    out: dict = {}
    for step, incumbent in STEPS.items():
        out[step] = {}
        for drop in (False, True):
            label = "no_length_caps" if drop else "as_specified"
            by_route: dict[str, dict[str, float]] = {}
            for route in cfg["steps"][step]["routes"]:
                salts = ["", "refresh-2026-10-27-control-1"] if route in CONTROLS else ["", "sample-1"]
                for cid in cfg["steps"][step]["cases"]:
                    msgs = A.text_messages(cases[cid])
                    hits = [cache.get(A._key(cfg["routes"][route]["upstream"], msgs, s)) for s in salts]
                    good = [h for h in hits if h is not None]
                    if len(good) < 2:
                        continue
                    by_route.setdefault(route, {})[cid] = statistics.mean(
                        score(h["text"], specs[cid]["checks"], drop) for h in good
                    )
            routes = {
                r: {"cases": len(v), "mean": round(statistics.mean(v.values()), 3), "ci95": bootstrap(list(v.values()))}
                for r, v in by_route.items()
            }
            paired = {}
            for r, v in by_route.items():
                if r == incumbent:
                    continue
                common = sorted(set(v) & set(by_route[incumbent]))
                paired[r] = compare([v[c] for c in common], [by_route[incumbent][c] for c in common])
            out[step][label] = {"routes": routes, "paired_vs_incumbent": paired, "per_case": by_route}
    (HERE / "rescore-old.json").write_text(json.dumps(out, indent=2))
    for step, readings in out.items():
        for label, r in readings.items():
            print(step, label)
            for route, s in r["routes"].items():
                p = r["paired_vs_incumbent"].get(route)
                extra = f"  diff {p['difference']:+.2f} {p['ci95']} p={p['paired_centred_bootstrap_p']:.3f}" if p else ""
                print(f"   {route:16} {s['mean']:.2f} {s['ci95']} n={s['cases']}{extra}")


if __name__ == "__main__":
    main()
