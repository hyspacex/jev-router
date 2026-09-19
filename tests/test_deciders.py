import httpx
import pytest
import respx

from conftest import make_config
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
