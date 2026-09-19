"""Strict requests always need the router control credential (spec 9.1).

The read-only endpoints accept a loopback peer when no token is configured. A
strict session cannot: it is scoped to the owner of that credential, so there
has to be one to scope it to. There is no setting that turns this off.
"""

from __future__ import annotations

import copy

import httpx
import pytest
import respx
import session_harness as H
import yaml
from session_harness import ADMIN, Client, Service, upstream
from test_app import JEV_URL

from jev_router.config import ConfigError, SessionRoutingCfg, load_config


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


def test_require_control_token_false_is_a_config_error():
    with pytest.raises(ValueError) as raised:
        SessionRoutingCfg(require_control_token=False)
    assert "always need the router control credential" in str(raised.value)


def test_require_control_token_true_still_loads():
    assert SessionRoutingCfg(require_control_token=True).require_control_token is True
    assert SessionRoutingCfg().require_control_token is True


def test_a_router_yaml_that_turns_it_off_is_refused(tmp_path):
    raw = copy.deepcopy(H.session_raw())
    raw["session_routing"]["require_control_token"] = False
    raw["settings"]["sqlite_path"] = str(tmp_path / "router.db")
    path = tmp_path / "router.yaml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError) as raised:
        load_config(path)
    message = str(raised.value)
    assert "require_control_token" in message
    assert "session_routing.enabled: false" in message


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path)
    yield svc
    await svc.close()


@respx.mock
async def test_a_strict_request_with_no_credential_is_refused_on_loopback(service):
    jev_ok()
    upstream()
    caller = Client(service, token="")
    response = await caller.resolve()
    assert response.status_code == 401
    # The ingress guard gets there first for a /router path; either way the
    # request never reaches a binding.
    assert response.json()["error"]["type"] in ("router_auth", "session_error")
    assert service.sessions.get(caller.session_id) is None


@respx.mock
async def test_a_strict_execution_with_no_credential_is_refused(service):
    jev_ok()
    upstream()
    caller = Client(service)
    assert (await caller.resolve()).status_code == 200

    headers = caller.execution_headers()
    headers.pop("x-router-admin-token")
    response = await service.client.post(
        "/v1/chat/completions",
        json={
            "model": caller.wire_model(),
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=headers,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ROUTER_UNAUTHORIZED"


@respx.mock
async def test_the_service_refuses_strict_work_when_no_credential_is_set(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.delenv("JEV_ROUTER_ADMIN_TOKEN", raising=False)
    svc = Service(tmp_path)
    try:
        jev_ok()
        caller = Client(svc, token="")
        response = await caller.resolve()
        assert response.status_code == 401
        assert "set $JEV_ROUTER_ADMIN_TOKEN" in response.json()["error"]["message"]
    finally:
        await svc.close()


def test_the_shipped_config_requires_the_credential():
    assert load_config("router.yaml").session_routing.require_control_token is True
