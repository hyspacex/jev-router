"""Unit tests for the admission harness. No network, no model calls.

What is worth testing here is the arithmetic that turns replies into a verdict:
the pairwise fold (which is the part that is easy to get subtly wrong, because
a win has to survive both orderings) and POOL.md's admission rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))

import admission  # noqa: E402
from checks import run_checks  # noqa: E402


# --- pairwise -----------------------------------------------------------


def pair(forward, reverse, forward_letter="A", reverse_letter="B"):
    return {
        "forward": forward,
        "reverse": reverse,
        "forward_letter": forward_letter,
        "reverse_letter": reverse_letter,
    }


def test_pairwise_counts_a_win_only_when_both_orders_agree():
    got = admission.pairwise_aggregate([
        pair("candidate", "candidate"),   # a real win
        pair("candidate", "incumbent"),   # the judge followed the position
        pair("incumbent", "incumbent"),   # a real loss
        pair("tie", "tie"),
    ])
    assert got["wins"] == 1
    assert got["losses"] == 1
    assert got["ties"] == 1
    assert got["undecided"] == 1
    assert got["decided"] == 2
    assert got["net_rate"] == 0.0


def test_pairwise_net_rate_runs_from_minus_one_to_one():
    all_wins = [pair("candidate", "candidate") for _ in range(4)]
    all_losses = [pair("incumbent", "incumbent") for _ in range(4)]
    assert admission.pairwise_aggregate(all_wins)["net_rate"] == 1.0
    assert admission.pairwise_aggregate(all_losses)["net_rate"] == -1.0


def test_pairwise_unparseable_judgement_is_undecided_not_a_loss():
    got = admission.pairwise_aggregate([pair(None, "candidate"), pair("candidate", None)])
    assert got["undecided"] == 2
    assert got["decided"] == 0
    assert got["net_rate"] == 0.0


def test_pairwise_order_bias_counts_pairs_where_the_judge_picked_a_twice():
    # Picking "A" in both orderings means the judge picked whichever answer
    # came first, which is the failure mode swapping exists to expose.
    got = admission.pairwise_aggregate([
        pair("candidate", "incumbent", forward_letter="A", reverse_letter="A"),
        pair("candidate", "candidate", forward_letter="A", reverse_letter="B"),
    ])
    assert got["order_bias"] == 0.5
    assert got["wins"] == 1


def test_pairwise_empty_input_is_all_zeros():
    got = admission.pairwise_aggregate([])
    assert got == {
        "pairs": 0, "wins": 0, "losses": 0, "ties": 0, "undecided": 0,
        "decided": 0, "net_rate": 0.0, "order_bias": 0.0,
    }


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"winner": "A", "why": "clearer"}', "A"),
        ('nonsense {"winner":"B"} trailing', "B"),
        ('{"winner": "tie"}', "tie"),
        ("The better answer is B.", "B"),
        ("I cannot decide.", None),
    ],
)
def test_parse_pairwise(text, expected):
    assert admission.parse_pairwise(text) == expected


def test_side_translates_letters_into_routes_both_ways():
    assert admission._side("A", candidate_is_a=True) == "candidate"
    assert admission._side("B", candidate_is_a=True) == "incumbent"
    assert admission._side("A", candidate_is_a=False) == "incumbent"
    assert admission._side("B", candidate_is_a=False) == "candidate"
    assert admission._side("tie", candidate_is_a=True) == "tie"
    assert admission._side(None, candidate_is_a=True) is None


# --- the admission rule -------------------------------------------------


RULE = {"score_gap": 0.5, "speed_gain": 0.30, "min_net_rate": 0.5}


def row(mean, latency_ms, provider="local"):
    return {"mean": mean, "latency_ms": latency_ms, "provider": provider}


def test_close_enough_and_fast_enough_passes():
    v = admission.verdict(row(8.6, 3000), row(9.0, 10000, "openai"), RULE)
    assert v["pass"] is True
    assert v["faster_30pct"] is True
    assert v["speedup"] == pytest.approx(3.33, abs=0.01)


def test_half_a_point_behind_is_still_within_the_gap():
    v = admission.verdict(row(8.5, 3000), row(9.0, 10000, "openai"), RULE)
    assert v["quality_ok"] is True
    v = admission.verdict(row(8.49, 3000), row(9.0, 10000, "openai"), RULE)
    assert v["quality_ok"] is False
    assert v["pass"] is False


def test_a_slower_model_on_another_provider_still_passes():
    # This is the grok-4.6 case: an overflow lane buys capacity, not speed.
    v = admission.verdict(row(8.8, 14000, "xai"), row(9.0, 10000, "openai"), RULE)
    assert v["faster_30pct"] is False
    assert v["different_provider"] is True
    assert v["pass"] is True


def test_same_provider_and_not_faster_fails():
    v = admission.verdict(row(9.0, 9000, "openai"), row(9.0, 10000, "openai"), RULE)
    assert v["pass"] is False
    assert "neither 30% faster" in v["why_not"]


def test_exactly_thirty_percent_faster_counts_as_faster():
    v = admission.verdict(row(9.0, 7000), row(9.0, 10000, "openai"), RULE)
    assert v["faster_30pct"] is True


def test_a_better_score_never_fails_on_quality():
    v = admission.verdict(row(9.9, 3000), row(9.0, 10000, "openai"), RULE)
    assert v["score_deficit"] < 0
    assert v["quality_ok"] is True


def test_specialist_lane_needs_the_pairwise_margin_as_well():
    close = row(8.9, 9000)
    inc = row(9.0, 10000, "openai")
    weak = {"decided": 6, "net_rate": 0.33}
    strong = {"decided": 6, "net_rate": 0.67}
    assert admission.verdict(close, inc, RULE, weak)["pass"] is False
    assert admission.verdict(close, inc, RULE, strong)["pass"] is True


def test_specialist_lane_with_no_decided_pairs_fails_rather_than_passes():
    v = admission.verdict(row(9.0, 9000), row(9.0, 10000, "openai"), RULE,
                          {"decided": 0, "net_rate": 0.0})
    assert v["pass"] is False


def test_specialist_lane_ignores_speed():
    # A specialist lane is bought with quality, so being slower is no bar.
    v = admission.verdict(row(9.0, 30000), row(9.0, 10000, "openai"), RULE,
                          {"decided": 4, "net_rate": 1.0})
    assert v["pass"] is True


# --- summaries ----------------------------------------------------------


def cell(score, scores, latency=1000, tokens=100, truncated=0, empty=0, live=2):
    return {
        "score": score, "scores": scores,
        "score_sd": round((max(scores) - min(scores)) / 2, 2),
        "samples": len(scores), "latency_ms": latency, "output_tokens": tokens,
        "truncated": truncated, "empty": empty, "live": live,
        "graded_by": "checks", "note": "", "finish_reasons": ["stop"] * len(scores),
    }


def test_summarise_route_keeps_the_spread_and_the_worst_case():
    cells = [cell(10.0, [10.0, 10.0]), cell(4.0, [2.0, 6.0]), cell(8.0, [8.0, 8.0])]
    got = admission.summarise_route("r", {"provider": "local"}, cells)
    assert got["mean"] == pytest.approx(7.33, abs=0.01)
    assert got["min_case"] == 4.0
    assert got["adequate"] == 2          # 10.0 and 8.0 clear 7
    assert got["spread"] == pytest.approx(0.67, abs=0.01)
    assert got["samples"] == 6


def test_summarise_route_reports_truncation_as_a_rate_over_samples():
    got = admission.summarise_route(
        "r", {"provider": "local"}, [cell(9.0, [9.0, 9.0], truncated=1)]
    )
    assert got["truncation_rate"] == 50.0


def test_wins_per_case_shares_a_tie():
    rows = [
        {"case_id": "a", "routes": {"x": cell(10.0, [10.0]), "y": cell(10.0, [10.0])}},
        {"case_id": "b", "routes": {"x": cell(9.0, [9.0]), "y": cell(4.0, [4.0])}},
    ]
    assert admission.wins_per_case(rows, ["x", "y"]) == {"x": 2, "y": 1}


# --- request building ---------------------------------------------------


def test_prompt_text_replaces_an_image_part_rather_than_dropping_the_turn():
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "read this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}
    ]
    text = admission.prompt_text(messages)
    assert "read this" in text
    assert "an image was attached" in text
    assert "base64" not in text


def test_image_messages_inline_a_data_url(tmp_path):
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    spec = {"prompt": "what is this", "system": "be exact"}
    messages = admission.image_messages(spec, png)
    assert messages[0]["role"] == "system"
    parts = messages[1]["content"]
    assert parts[0]["text"] == "what is this"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


# --- new checks ---------------------------------------------------------


def test_bullet_count_is_exact_and_near_misses_score_low():
    three = "- one\n- two\n- three\n"
    four = three + "- four\n"
    assert run_checks(three, [{"kind": "bullet_count", "expected": 3}])[0] == 10.0
    assert run_checks(four, [{"kind": "bullet_count", "expected": 3}])[0] == pytest.approx(3.0)
    assert run_checks("a paragraph", [{"kind": "bullet_count", "expected": 3}])[0] == 0.0


def test_bullet_count_ignores_nested_bullets():
    nested = "- one\n    - sub\n- two\n- three\n"
    assert run_checks(nested, [{"kind": "bullet_count", "expected": 3}])[0] == 10.0


def test_bullet_count_accepts_numbered_lists():
    assert run_checks("1. a\n2. b\n", [{"kind": "bullet_count", "expected": 2}])[0] == 10.0


def test_word_range_fails_both_ends():
    spec = [{"kind": "word_range", "low": 5, "high": 10}]
    assert run_checks("one two three four five six", spec)[0] == 10.0
    assert run_checks("hi", spec)[0] == 0.0
    assert run_checks(" ".join(["w"] * 40), spec)[0] == 0.0


# --- the step file ------------------------------------------------------


def test_admission_yaml_routes_and_cases_all_resolve():
    cfg = yaml.safe_load((EVALS / "admission.yaml").read_text())
    specs = yaml.safe_load((EVALS / "outcome_specs.yaml").read_text())["cases"]
    case_ids = {c["id"] for c in yaml.safe_load((EVALS / "cases.yaml").read_text())["cases"]}
    images = cfg["image_cases"]
    for name, step in cfg["steps"].items():
        assert step["incumbent"] in step["routes"], name
        for route in step["routes"]:
            assert route in cfg["routes"], (name, route)
        for case in step["cases"]:
            assert case in images or (case in case_ids and case in specs), (name, case)


def test_every_route_declares_an_effort_suffix_unless_it_has_no_reasoning_mode():
    # glm-5.3 and kimi-k3 sent bare think until the budget is gone and can come
    # back empty, so a missing suffix is a real bug rather than a style point.
    cfg = yaml.safe_load((EVALS / "admission.yaml").read_text())
    for name, meta in cfg["routes"].items():
        if name == "gemma4-31b":
            assert "(" not in meta["upstream"]
        else:
            assert meta["upstream"].endswith(")"), name


def test_image_case_checks_match_what_the_generator_draws():
    cfg = yaml.safe_load((EVALS / "admission.yaml").read_text())
    pytest.importorskip("PIL")
    import make_images

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        built = make_images.build_all(Path(tmp))
    truths = {name: info["truth"] for name, info in built.items()}
    chart = cfg["image_cases"]["img-bar-chart"]["checks"][0]["expected"]
    assert chart["highest"] == truths["bar-chart"]["highest"]
    assert chart["highest_value"] == truths["bar-chart"]["highest_value"]
    assert chart["sum_two_smallest"] == truths["bar-chart"]["sum_two_smallest"]
    table = cfg["image_cases"]["img-invoice-table"]["checks"][0]["expected"]
    assert table["overdue_count"] == truths["table-screenshot"]["overdue_count"]
    assert table["overdue_total"] == truths["table-screenshot"]["overdue_total"]
    digits = truths["handwritten-digits"]["digits"]
    assert run_checks(digits, cfg["image_cases"]["img-handwritten-digits"]["checks"])[0] == 10.0
