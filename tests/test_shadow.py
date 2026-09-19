"""Shadow questions: asked, recorded, and never allowed to decide.

Named cases from docs/R2_SPEC.md 13.2 covered here:

    C24  malformed, partial, NaN and unsolicited Jev data hits the validated
         fallback boundary, including with the expanded packet
    C25  shadow questions never change execution, and an invalid shadow answer
         cannot weaken the validation of the active answers
    C26  the same packet is reused across a pressure sweep: one Jev call, and
         the answers do not move
"""

from __future__ import annotations

import copy

import httpx
import pytest
import respx

from conftest import make_config
from jev_router.deciders import build_decider
from jev_router.deciders.validation import validate_response, validate_shadow
from jev_router.features import extract_features

JEV_URL = "https://jev.test/v1/systemone"

SHADOW_QUESTIONS = {
    "mechanical_transform": {
        "type": "noul",
        "instructions": "Is the change stated rather than chosen?",
    },
    "interacting_constraints": {
        "type": "noul",
        "instructions": "Must two things keep agreeing?",
    },
}


def shadow_config(**overrides):
    return make_config(
        questions=SHADOW_QUESTIONS,
        semantic_policy={
            "active_questions": ["task", "difficulty", "harm_if_wrong"],
            "shadow_questions": list(SHADOW_QUESTIONS),
        },
        **overrides,
    )


def feats(text="hello"):
    return extract_features({"model": "auto", "messages": [{"role": "user", "content": text}]})


def payload(*, shadow=True, difficulty=0.1, **shadow_overrides):
    answers = {
        "task": {"type": "choice", "choice": "quick_question", "confidence": 0.9},
        "difficulty": {"type": "score", "score": difficulty, "confidence": 0.9},
        "harm_if_wrong": {"type": "noul", "noul": 0.1},
    }
    if shadow:
        answers["mechanical_transform"] = {"type": "noul", "noul": 0.9}
        answers["interacting_constraints"] = {"type": "noul", "noul": 0.1}
    answers.update(shadow_overrides)
    return {
        "model": "jev-1.13.0",
        "answers": answers,
        "usage": {"input_tokens": 400, "output_tokens": 60},
    }


async def decide(cfg, response, alias="auto"):
    route = respx.post(JEV_URL).mock(return_value=response)
    async with httpx.AsyncClient() as client:
        decider = build_decider("jev", cfg, {"jev_client": client})
        decision = await decider.decide(feats(), cfg.aliases[alias])
    return decision, route


# --- one batched call ----------------------------------------------------


@respx.mock
async def test_active_and_shadow_go_out_in_one_call(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    decision, route = await decide(cfg, httpx.Response(200, json=payload()))

    assert route.call_count == 1
    asked = route.calls[0].request.content.decode()
    for qid in ("task", "difficulty", "harm_if_wrong", *SHADOW_QUESTIONS):
        assert f'"{qid}"' in asked
    assert set(decision.answers) == {"task", "difficulty", "harm_if_wrong"}
    assert set(decision.shadow_answers) == set(SHADOW_QUESTIONS)
    assert decision.invalid_shadow == []
    assert decision.packet_version.startswith("pkt1:")
    assert decision.shadow_packet_version.startswith("pkt1:")
    assert decision.packet_version != decision.shadow_packet_version


@respx.mock
async def test_with_no_shadow_questions_the_call_is_exactly_what_it_was(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = make_config()
    decision, route = await decide(cfg, httpx.Response(200, json=payload(shadow=False)))
    asked = route.calls[0].request.content.decode()
    assert "mechanical_transform" not in asked
    assert decision.shadow_answers == {}
    assert decision.shadow_packet_version == ""


# --- C25 -----------------------------------------------------------------


@respx.mock
async def test_c25_a_shadow_answer_never_changes_the_route(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    plain = make_config()

    with_shadow, _ = await decide(cfg, httpx.Response(200, json=payload()))
    respx.reset()
    without, _ = await decide(plain, httpx.Response(200, json=payload(shadow=False)))

    assert (with_shadow.model, with_shadow.effort, with_shadow.rule) == (
        without.model,
        without.effort,
        without.rule,
    )


@respx.mock
async def test_c25_an_extreme_shadow_answer_still_changes_nothing(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    body = payload()
    body["answers"]["mechanical_transform"] = {"type": "noul", "noul": 1.0}
    body["answers"]["interacting_constraints"] = {"type": "noul", "noul": 1.0}
    decision, _ = await decide(cfg, httpx.Response(200, json=body))
    assert decision.model == "small" and decision.rule == "easy"
    assert decision.shadow_answers["mechanical_transform"]["noul"] == 1.0


@respx.mock
@pytest.mark.parametrize(
    "broken",
    [
        {"type": "noul", "noul": "yes"},
        {"type": "score", "score": 1.0, "confidence": 0.5},
        {"type": "noul", "noul": 1.5},
        {"type": "noul"},
        None,
        "not a mapping",
    ],
)
async def test_c25_an_invalid_shadow_answer_is_dropped_and_counted(monkeypatch, broken):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    body = payload()
    body["answers"]["mechanical_transform"] = broken
    decision, _ = await decide(cfg, httpx.Response(200, json=body))

    # The active half is untouched and the request still routes.
    assert decision.fallback is False
    assert decision.model == "small" and decision.rule == "easy"
    assert decision.invalid_shadow == ["mechanical_transform"]
    assert "mechanical_transform" not in decision.shadow_answers
    assert decision.shadow_answers["interacting_constraints"]["noul"] == 0.1


@respx.mock
async def test_c25_a_non_finite_shadow_answer_is_dropped(monkeypatch):
    """An infinity does not survive `json.dumps`, so send the bytes directly."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    decision, _ = await decide(
        cfg,
        httpx.Response(
            200,
            content=b'{"answers": {'
            b'"task": {"type": "choice", "choice": "quick_question", "confidence": 0.9},'
            b'"difficulty": {"type": "score", "score": 0.1, "confidence": 0.9},'
            b'"harm_if_wrong": {"type": "noul", "noul": 0.1},'
            b'"mechanical_transform": {"type": "noul", "noul": Infinity},'
            b'"interacting_constraints": {"type": "noul", "noul": 0.1}},'
            b' "usage": {}}',
            headers={"content-type": "application/json"},
        ),
    )
    assert decision.fallback is False
    assert decision.invalid_shadow == ["mechanical_transform"]


def test_c25_invalid_shadow_handling_does_not_touch_the_active_validator():
    questions = {"harm_if_wrong": {"type": "noul", "instructions": "?"}}
    data = {
        "answers": {"harm_if_wrong": {"type": "noul", "noul": "nonsense"}},
        "usage": {},
    }
    # The same malformed shape is a refusal on the active side and a drop on
    # the shadow side. The two code paths agree about what is invalid.
    with pytest.raises(ValueError):
        validate_response(data, questions)
    answers, invalid = validate_shadow(data, questions)
    assert answers == {} and invalid == ["harm_if_wrong"]


def test_c25_a_broken_envelope_drops_every_shadow_answer_without_raising():
    questions = {"a": {"type": "noul", "instructions": "?"}}
    for data in (None, {}, {"answers": []}, {"answers": {}}, 7):
        answers, invalid = validate_shadow(data, questions)
        assert answers == {} and invalid == ["a"]
    assert validate_shadow({"answers": {}}, {}) == ({}, [])


# --- C24 -----------------------------------------------------------------

BAD_ACTIVE = {
    "missing_answer": {"task": None},
    "mistyped_answer": {"difficulty": {"type": "choice", "choice": "code", "confidence": 0.9}},
    "out_of_vocabulary": {"task": {"type": "choice", "choice": "nope", "confidence": 0.9}},
    "missing_confidence": {"task": {"type": "choice", "choice": "code"}},
    "partial_distribution": {
        "difficulty": {
            "type": "score",
            "score": 1.0,
            "confidence": 0.9,
            "probabilities": {"0": 0.5},
        }
    },
}


@respx.mock
@pytest.mark.parametrize("name", sorted(BAD_ACTIVE))
async def test_c24_bad_active_data_reaches_the_fallback_with_the_expanded_packet(
    monkeypatch, name
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    body = payload()
    body["answers"].update(copy.deepcopy(BAD_ACTIVE[name]))
    decision, _ = await decide(cfg, httpx.Response(200, json=body))

    assert decision.fallback is True
    assert decision.answers == {}
    # A fallback is not Jev evidence, so no shadow answer is kept either.
    assert decision.shadow_answers == {}
    assert decision.model  # the request is still served


@respx.mock
async def test_c24_a_nan_score_reaches_the_fallback(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    body = payload()
    body["answers"]["difficulty"] = {
        "type": "score",
        "score": float("nan"),
        "confidence": 0.9,
    }
    decision, _ = await decide(
        cfg,
        httpx.Response(
            200,
            content=b'{"answers": {"difficulty": {"type": "score", "score": NaN,'
            b' "confidence": 0.9}}, "usage": {}}',
            headers={"content-type": "application/json"},
        ),
    )
    assert decision.fallback is True and decision.answers == {}


@respx.mock
async def test_c24_an_unsolicited_answer_is_not_retained(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    body = payload()
    body["answers"]["something_nobody_asked"] = {"type": "noul", "noul": 1.0}
    decision, _ = await decide(cfg, httpx.Response(200, json=body))

    assert decision.fallback is False
    assert "something_nobody_asked" not in decision.answers
    assert "something_nobody_asked" not in decision.shadow_answers


@respx.mock
async def test_c24_a_shadow_only_reply_still_fails_the_packet(monkeypatch):
    """Shadow answers may not stand in for the answers that decide."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    decision, _ = await decide(
        cfg,
        httpx.Response(
            200,
            json={
                "answers": {
                    "mechanical_transform": {"type": "noul", "noul": 0.9},
                    "interacting_constraints": {"type": "noul", "noul": 0.1},
                },
                "usage": {},
            },
        ),
    )
    assert decision.fallback is True and decision.answers == {}


# --- C26 -----------------------------------------------------------------


@respx.mock
async def test_c26_a_pressure_sweep_reuses_one_packet(monkeypatch):
    """Quota changes the route it may choose, never what the work is."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    alias = cfg.aliases["auto"]
    route = respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=payload()))

    async with httpx.AsyncClient() as client:
        decider = build_decider("jev", cfg, {"jev_client": client})
        semantics = await decider.classify(feats(), alias)

    assert route.call_count == 1

    from jev_router.policy import evaluate

    seen = []
    for pressure in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        result = evaluate(
            cfg, alias, semantics.answers, feats(), {"default": pressure}
        )
        seen.append((result.model, result.effort))
    # One call, one packet, six routings.
    assert route.call_count == 1
    assert semantics.answers["difficulty"]["score"] == 0.1
    assert semantics.shadow["mechanical_transform"]["noul"] == 0.9
    assert len(seen) == 6


@respx.mock
async def test_c26_classify_chooses_no_model(monkeypatch):
    """The turn can be classified without anything reselecting a model."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    async with httpx.AsyncClient() as client:
        respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=payload()))
        decider = build_decider("jev", cfg, {"jev_client": client})
        semantics = await decider.classify(feats(), cfg.aliases["auto"])

    assert not hasattr(semantics, "model")
    assert not hasattr(semantics, "effort")
    assert semantics.ok
    assert semantics.state_builder == "summary_v1"


@respx.mock
async def test_classify_reports_a_failure_instead_of_choosing(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    cfg = shadow_config()
    async with httpx.AsyncClient() as client:
        respx.post(JEV_URL).mock(return_value=httpx.Response(529))
        decider = build_decider("jev", cfg, {"jev_client": client})
        semantics = await decider.classify(feats(), cfg.aliases["auto"])
    assert not semantics.ok
    assert "529" in semantics.error
    assert semantics.answers == {} and semantics.shadow == {}


def test_shadow_values_keep_numbers_and_never_text():
    from jev_router.semantic import SemanticResult

    result = SemanticResult(
        shadow={
            "mechanical_transform": {"type": "noul", "noul": 0.9},
            "task": {
                "type": "choice",
                "choice": "code",
                "confidence": 0.8,
                "probabilities": {"code": 1.0},
            },
        }
    )
    assert result.shadow_values() == {
        "mechanical_transform": {"type": "noul", "value": 0.9},
        "task": {
            "type": "choice",
            "value": "code",
            "confidence": 0.8,
            "probabilities": {"code": 1.0},
        },
    }
