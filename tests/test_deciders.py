import json

import httpx
import pytest
import respx

from conftest import UPSTREAM, make_config
from jev_router.deciders import build_decider
from jev_router.deciders.rules import RulesDecider
from jev_router.features import extract_features

JEV_URL = "https://jev.test/v1/systemone"


def feats(text="hello", **body):
    body.setdefault("model", "auto")
    body.setdefault("messages", [{"role": "user", "content": text}])
    return extract_features(body, body.pop("_headers", {}))


def answer_payload(task="quick_question", difficulty=0.1, harm=0.1, conf=0.9):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "task": {"type": "choice", "choice": task, "confidence": conf, "probabilities": {}},
            "difficulty": {
                "type": "score",
                "score": difficulty,
                "confidence": conf,
                "probabilities": {},
            },
            "harm_if_wrong": {"type": "noul", "noul": harm},
        },
        "usage": {"input_tokens": 427, "output_tokens": 63},
    }


@pytest.fixture
def cfg():
    return make_config()


async def make_jev(cfg, client):
    return build_decider("jev", cfg, {"jev_client": client})


@respx.mock
async def test_jev_decider_uses_the_answers(cfg, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    route = respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=answer_payload()))
    async with httpx.AsyncClient() as client:
        decider = await make_jev(cfg, client)
        decision = await decider.decide(feats(), cfg.aliases["auto"])

    assert decision.model == "small"
    assert decision.rule == "easy"
    assert decision.fallback is False
    assert decision.jev_input_tokens == 427
    assert decision.jev_ms is not None
    assert decision.state_builder == "summary_v1"
    request_body = route.calls[0].request.content.decode()
    assert '"jev-latest"' in request_body
    assert "harm_if_wrong" in request_body


@respx.mock
async def test_jev_sends_only_the_questions_the_alias_asks(cfg, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    route = respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=answer_payload()))
    async with httpx.AsyncClient() as client:
        decider = await make_jev(cfg, client)
        await decider.decide(feats(), cfg.aliases["auto-fast"])
    body = route.calls[0].request.content.decode()
    assert "difficulty" in body
    assert "harm_if_wrong" not in body


@respx.mock
async def test_timeout_falls_back(cfg, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    respx.post(JEV_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as client:
        decider = await make_jev(cfg, client)
        decision = await decider.decide(feats(), cfg.aliases["auto"])
    assert decision.fallback is True
    assert "timed out" in decision.reason
    assert decision.model in cfg.models


@respx.mock
async def test_529_falls_back(cfg, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    respx.post(JEV_URL).mock(return_value=httpx.Response(529, text="overloaded"))
    async with httpx.AsyncClient() as client:
        decider = await make_jev(cfg, client)
        decision = await decider.decide(feats(), cfg.aliases["auto"])
    assert decision.fallback is True
    assert "529" in decision.reason


@respx.mock
async def test_429_falls_back(cfg, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    respx.post(JEV_URL).mock(return_value=httpx.Response(429, text="slow down"))
    async with httpx.AsyncClient() as client:
        decider = await make_jev(cfg, client)
        decision = await decider.decide(feats(), cfg.aliases["auto"])
    assert decision.fallback is True


async def test_missing_key_falls_back_without_calling(cfg, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    async with httpx.AsyncClient() as client:
        decider = await make_jev(cfg, client)
        decision = await decider.decide(feats(), cfg.aliases["auto"])
    assert decision.fallback is True
    assert "no Jev API key" in decision.reason


@respx.mock
async def test_fallback_without_a_fallback_decider_uses_the_default_route(monkeypatch):
    cfg = make_config(settings={"fallback_decider": None})
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    respx.post(JEV_URL).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        decider = await make_jev(cfg, client)
        decision = await decider.decide(feats(), cfg.aliases["auto"])
    assert (decision.model, decision.effort, decision.rule) == ("big", "medium", "fallback")
    assert decision.fallback is True


async def test_rules_decider_reads_the_request(cfg):
    decider = RulesDecider(cfg)
    easy = await decider.decide(feats("what time is it"), cfg.aliases["auto"])
    assert easy.model == "small"
    hard = await decider.decide(
        feats("refactor this across the whole package ```python\nx=1\n```"),
        cfg.aliases["auto"],
    )
    assert hard.model == "big"
    assert hard.answers["difficulty"]["score"] >= 1.5


async def test_rules_decider_only_answers_asked_questions(cfg):
    decision = await RulesDecider(cfg).decide(feats(), cfg.aliases["auto-fast"])
    assert set(decision.answers) == {"difficulty"}


# --- the optional AutoMix-style draft step -------------------------------


DRAFT_CFG = {
    "enabled": True,
    "model": "small",
    "max_tokens": 60,
    "timeout_ms": 500,
}


def _draft_reply(text="A draft answer."):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@respx.mock
async def test_draft_is_off_unless_the_alias_turns_it_on(cfg, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    """No `draft` block means no upstream call and no extra state field."""
    jev = respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=answer_payload()))
    upstream = respx.post(f"{UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_draft_reply())
    )
    async with httpx.AsyncClient() as client:
        decider = build_decider("jev", cfg, {"jev_client": client})
        decision = await decider.decide(feats("what is 2+2"), cfg.aliases["auto"])
    assert jev.called
    assert not upstream.called
    assert "draft_answer_from_a_fast_model" not in (decision.state or {})


@respx.mock
async def test_draft_reaches_jev_when_the_alias_asks_for_it(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    config = make_config(aliases={"auto": {"draft": DRAFT_CFG}})
    jev = respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=answer_payload()))
    upstream = respx.post(f"{UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_draft_reply("4"))
    )
    async with httpx.AsyncClient() as client:
        decider = build_decider("jev", config, {"jev_client": client})
        decision = await decider.decide(feats("what is 2+2"), config.aliases["auto"])
    assert upstream.called
    # The draft goes upstream under the upstream_id with its effort applied,
    # the same way a forwarded request would. Without the effort a reasoning
    # model spends the whole token budget thinking and returns empty content.
    assert json.loads(upstream.calls[0].request.content)["model"] == "vendor/small(none)"
    assert decision.state["draft_answer_from_a_fast_model"] == "4"
    sent = json.loads(jev.calls[0].request.content)
    assert sent["state"]["draft_answer_from_a_fast_model"] == "4"


@respx.mock
async def test_draft_failing_does_not_fail_the_request(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    """Fails open. A dead upstream must cost the draft, not the decision."""
    config = make_config(aliases={"auto": {"draft": DRAFT_CFG}})
    respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=answer_payload()))
    respx.post(f"{UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="down")
    )
    async with httpx.AsyncClient() as client:
        decider = build_decider("jev", config, {"jev_client": client})
        decision = await decider.decide(feats("what is 2+2"), config.aliases["auto"])
    assert not decision.fallback
    assert "draft_answer_from_a_fast_model" not in decision.state


@respx.mock
async def test_draft_timing_out_does_not_fail_the_request(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    config = make_config(aliases={"auto": {"draft": DRAFT_CFG}})
    respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=answer_payload()))
    respx.post(f"{UPSTREAM}/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    async with httpx.AsyncClient() as client:
        decider = build_decider("jev", config, {"jev_client": client})
        decision = await decider.decide(feats("hello"), config.aliases["auto"])
    assert not decision.fallback
    assert decision.model


@respx.mock
async def test_an_empty_draft_is_left_out_of_the_state(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    config = make_config(aliases={"auto": {"draft": DRAFT_CFG}})
    respx.post(JEV_URL).mock(return_value=httpx.Response(200, json=answer_payload()))
    respx.post(f"{UPSTREAM}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_draft_reply("   "))
    )
    async with httpx.AsyncClient() as client:
        decider = build_decider("jev", config, {"jev_client": client})
        decision = await decider.decide(feats("hello"), config.aliases["auto"])
    assert "draft_answer_from_a_fast_model" not in decision.state
