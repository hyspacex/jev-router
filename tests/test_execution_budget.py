"""The output allowance on a managed execution (spec 6.4, I06).

The negotiated ceiling is a contract. A request that asks for more than it is
an error, not something to forward and hope the provider clamps, and not
something for the router to quietly rewrite.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Client, Service, upstream
from test_app import JEV_URL


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path)
    yield svc
    await svc.close()


def jev_ok():
    return respx.post(JEV_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    "task": {
                        "type": "choice",
                        "choice": "code",
                        "confidence": 0.9,
                        "probabilities": {},
                    },
                    "difficulty": {"type": "score", "score": 0.1, "confidence": 0.9},
                    "harm_if_wrong": {"type": "noul", "noul": 0.1},
                },
                "usage": {"input_tokens": 100},
            },
        )
    )


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def bound(service, **limits) -> Client:
    caller = Client(service)
    response = await caller.resolve(limits=limits or {"max_output_tokens": 2048})
    assert response.status_code == 200, response.text
    return caller


@respx.mock
@pytest.mark.parametrize(
    "field", ["max_tokens", "max_completion_tokens", "max_output_tokens"]
)
async def test_i06_an_ask_above_the_negotiated_ceiling_is_refused(service, field):
    jev_ok()
    seen = upstream()
    caller = await bound(service)
    ceiling = caller.execution["max_output_tokens"]
    assert ceiling == 2048

    response = await caller.execute(
        {
            "model": caller.wire_model(),
            "messages": [{"role": "user", "content": "hello"}],
            field: ceiling + 1,
        }
    )
    assert response.status_code == 422
    assert code(response) == "CONTEXT_BUDGET_EXCEEDED"
    error = response.json()["error"]
    assert error["requested_output_tokens"] == ceiling + 1
    assert error["max_output_tokens"] == ceiling
    assert seen == []  # nothing reached the provider


@respx.mock
async def test_i06_a_refused_ask_keeps_the_binding(service):
    jev_ok()
    upstream()
    caller = await bound(service)
    refused = await caller.execute(
        {
            "model": caller.wire_model(),
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100_000,
        }
    )
    assert refused.status_code == 422

    row = service.sessions.get(caller.session_id)
    assert row["state"] == "prepared" and row["blocked_reason"] is None
    # The same session executes as soon as the request asks for what it may.
    assert (await caller.execute()).status_code == 200


@respx.mock
async def test_i06_an_ask_at_or_below_the_ceiling_is_forwarded_unchanged(service):
    jev_ok()
    seen = upstream()
    caller = await bound(service)
    ceiling = caller.execution["max_output_tokens"]

    for ask in (ceiling, 16):
        response = await caller.execute(
            {
                "model": caller.wire_model(),
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": ask,
            }
        )
        assert response.status_code == 200, response.text
        # Not rewritten: the client asked for that, and it goes out as it came.
        assert seen[-1]["max_tokens"] == ask


@respx.mock
async def test_i06_a_modest_ask_is_still_budgeted_at_the_reserved_ceiling(service):
    """The ceiling is reserved for the session, used or not."""
    jev_ok()
    upstream()
    caller = Client(service)
    resolved = await caller.resolve(
        request={"messages": [{"role": "user", "content": "start"}]},
        limits={"max_output_tokens": 8000, "context_window": 14000},
        candidate_models=["big"],
    )
    assert resolved.status_code == 200, resolved.text
    assert caller.execution["max_output_tokens"] == 8000

    # 14000 window, 8000 reserved for output, 4096 minimum uncertainty
    # reserve: this input has nowhere to go even though it asks for 16 tokens.
    response = await caller.execute(
        {
            "model": caller.wire_model(),
            "messages": [{"role": "user", "content": "x" * 8000}],
            "max_tokens": 16,
        }
    )
    assert response.status_code == 422
    assert code(response) == "CONTEXT_BUDGET_EXCEEDED"
    assert "8000 output" in response.json()["error"]["message"]


@respx.mock
async def test_i06_a_responses_binding_refuses_an_oversized_output_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    service = Service(tmp_path, config=H.adaptive_config(tmp_path, mode="off"))
    try:
        jev_ok()
        seen = H.responses_upstream()
        caller = H.ResponsesClient(service)
        assert (await caller.start(limits={"max_output_tokens": 4096})).status_code == 200

        body = caller.request_body(input=[H.user_item("hello")], max_output_tokens=4097)
        response = await service.client.post(
            "/v1/responses", json=body, headers=caller.execution_headers()
        )
        assert response.status_code == 422
        assert code(response) == "CONTEXT_BUDGET_EXCEEDED"
        assert seen == []
    finally:
        await service.close()


def test_i06_a_body_with_no_output_ask_is_read_as_no_ask():
    from jev_router.app import _requested_output

    assert _requested_output({}) is None
    assert _requested_output({"max_tokens": None}) is None
    assert _requested_output({"max_tokens": 0}) is None
    assert _requested_output({"max_tokens": True}) is None
    assert _requested_output({"max_tokens": "lots"}) is None
    assert _requested_output({"max_tokens": 10, "max_output_tokens": 20}) == 20
