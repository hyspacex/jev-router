"""The paired comparison. Separate file so the pairwise tests stay readable."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))

import admission  # noqa: E402

META = {"cand": {"provider": "local"}, "inc": {"provider": "openai"}}


def cell(score, latency=1000):
    return {
        "score": score, "scores": [score], "score_sd": 0.0, "samples": 1,
        "latency_ms": latency, "output_tokens": 100, "truncated": 0, "empty": 0,
        "live": 1, "graded_by": "checks", "note": "", "finish_reasons": ["stop"],
    }


def test_paired_summary_uses_only_cases_both_routes_ran():
    rows = [
        {"case_id": "a", "routes": {"cand": cell(10.0), "inc": cell(10.0)}},
        {"case_id": "b", "routes": {"cand": cell(4.0)}},              # no incumbent
        {"case_id": "c", "routes": {"cand": cell(8.0), "inc": cell(9.0)}},
    ]
    cand, inc = admission.paired_summary(rows, META, "cand", "inc")
    assert cand["cases"] == 2
    assert inc["cases"] == 2
    assert cand["mean"] == 9.0    # not (10 + 4 + 8) / 3
    assert inc["mean"] == 9.5


def test_paired_summary_is_none_when_they_never_overlap():
    rows = [
        {"case_id": "a", "routes": {"cand": cell(10.0)}},
        {"case_id": "b", "routes": {"inc": cell(10.0)}},
    ]
    assert admission.paired_summary(rows, META, "cand", "inc") is None


def test_paired_summary_matches_the_plain_summary_when_all_cases_overlap():
    rows = [
        {"case_id": "a", "routes": {"cand": cell(10.0), "inc": cell(9.0)}},
        {"case_id": "b", "routes": {"cand": cell(6.0), "inc": cell(7.0)}},
    ]
    cand, _ = admission.paired_summary(rows, META, "cand", "inc")
    plain = admission.summarise_route(
        "cand", META["cand"], [r["routes"]["cand"] for r in rows]
    )
    assert cand["mean"] == plain["mean"] == 8.0


def test_pairing_changes_the_verdict_when_the_incumbent_is_partly_cached():
    # The candidate looks 0.6 behind on the unpaired means and 0.1 behind on
    # the cases they actually share. Only the second one is a comparison.
    rows = [
        {"case_id": "a", "routes": {"cand": cell(10.0, 2000), "inc": cell(10.0, 9000)}},
        {"case_id": "b", "routes": {"cand": cell(8.2, 2000)}},
        {"case_id": "c", "routes": {"cand": cell(9.6, 2000), "inc": cell(9.8, 9000)}},
    ]
    rule = {"score_gap": 0.5, "speed_gain": 0.30}
    unpaired_cand = admission.summarise_route(
        "cand", META["cand"], [r["routes"]["cand"] for r in rows]
    )
    unpaired_inc = admission.summarise_route(
        "inc", META["inc"], [r["routes"]["inc"] for r in rows if "inc" in r["routes"]]
    )
    assert admission.verdict(unpaired_cand, unpaired_inc, rule)["pass"] is False
    cand, inc = admission.paired_summary(rows, META, "cand", "inc")
    assert admission.verdict(cand, inc, rule)["pass"] is True


def test_paired_summary_keeps_each_routes_own_latency():
    rows = [{"case_id": "a", "routes": {"cand": cell(9.0, 2000), "inc": cell(9.0, 9000)}}]
    cand, inc = admission.paired_summary(rows, META, "cand", "inc")
    assert cand["latency_ms"] == 2000
    assert inc["latency_ms"] == 9000
    assert cand["provider"] == "local"
    assert inc["provider"] == "openai"


def test_verdict_rejects_a_candidate_that_is_only_marginally_faster():
    rule = {"score_gap": 0.5, "speed_gain": 0.30}
    v = admission.verdict(
        {"mean": 9.0, "latency_ms": 7100, "provider": "local"},
        {"mean": 9.0, "latency_ms": 10000, "provider": "local"},
        rule,
    )
    assert v["faster_30pct"] is False
    assert v["pass"] is False


@pytest.mark.parametrize("gap,expected", [(0.5, True), (0.1, False)])
def test_score_gap_is_configurable(gap, expected):
    rule = {"score_gap": gap, "speed_gain": 0.30}
    v = admission.verdict(
        {"mean": 8.7, "latency_ms": 2000, "provider": "local"},
        {"mean": 9.0, "latency_ms": 10000, "provider": "openai"},
        rule,
    )
    assert v["pass"] is expected


# --- mid-tier labels ----------------------------------------------------


def row_with(case_id, mid, other, mid_samples=None):
    return {
        "case_id": case_id,
        "routes": {
            "glm-5.3": {**cell(mid), "scores": mid_samples or [mid, mid]},
            "astra": cell(other),
        },
    }


def test_mid_label_needs_both_the_mean_and_every_sample():
    rows = [
        row_with("a", 10.0, 10.0),                       # clear
        row_with("b", 9.6, 10.0),                        # within 0.5
        row_with("c", 9.4, 10.0),                        # 0.6 behind
        row_with("d", 8.5, 8.5, mid_samples=[10.0, 7.0]),  # top route, but
        row_with("e", 8.5, 8.5, mid_samples=[10.0, 6.0]),  # a failing sample
    ]
    got = {p["case_id"]: p["add_mid"] for p in admission.mid_label_proposals(rows, "glm-5.3")}
    assert got == {"a": True, "b": True, "c": False, "d": True, "e": False}


def test_mid_label_skips_a_case_the_mid_route_never_ran():
    rows = [{"case_id": "a", "routes": {"astra": cell(10.0)}}]
    assert admission.mid_label_proposals(rows, "glm-5.3") == []


def test_apply_mid_labels_adds_the_tier_and_marks_the_source(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        "cases:\n"
        "  - id: alpha\n"
        "    expected:\n"
        "      tier: frontier\n"
        "      acceptable_tiers: [frontier]\n"
        "\n"
        "  - id: beta\n"
        "    expected:\n"
        "      tier: fast\n"
        "      acceptable_tiers: [fast]\n"
    )
    changed = admission.apply_mid_labels(
        [{"case_id": "alpha", "add_mid": True}, {"case_id": "beta", "add_mid": False}],
        path,
    )
    text = path.read_text()
    assert changed == ["alpha"]
    assert "acceptable_tiers: [mid, frontier]" in text
    assert "acceptable_tiers: [fast]" in text          # beta untouched
    assert text.count("label_source: outcome") == 1
    assert "tier: frontier" in text                     # `tier` is never moved


def test_apply_mid_labels_is_idempotent(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        "cases:\n"
        "  - id: alpha\n"
        "    expected:\n"
        "      acceptable_tiers: [fast]\n"
    )
    proposals = [{"case_id": "alpha", "add_mid": True}]
    admission.apply_mid_labels(proposals, path)
    first = path.read_text()
    assert admission.apply_mid_labels(proposals, path) == []
    assert path.read_text() == first
