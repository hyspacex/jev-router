"""Exercise the benchmark's live HTTP contract without provider calls."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from run_sessions import POLICY_ARMS, load_tasks, tool_schemas
from session_client import RouterSessionClient, SessionClientError


@pytest.mark.parametrize("arm_name", ["fixed_strong", "fixed_mid", "simple_rules", "jev_mean"])
async def test_benchmark_sends_auth_tools_and_limits_for_each_arm(arm_name):
    task = load_tasks()[0]
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path == "/router/resolve":
            body = json.loads(request.content)
            assert body["alias"] == POLICY_ARMS[arm_name]["alias"]
            assert body["request"]["tools"] == tool_schemas(task)
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"binding_revision": "binding", "execution": {
                "model_key": "model", "wire_model": "model(low)",
                "initial_effort": "low", "context_window": 100000}})
        if request.method == "GET":
            return httpx.Response(200, json={"decision_source": "rules" if arm_name == "simple_rules" else "jev"})
        if request.url.path.endswith("/close"):
            return httpx.Response(200, json={"state": "closed"})
        assert request.headers["authorization"] == "Bearer upstream-test"
        body = json.loads(request.content)
        assert body["max_tokens"] == 4096
        assert body["tools"] == tool_schemas(task)
        if arm_name in {"fixed_strong", "fixed_mid"}:
            arm = POLICY_ARMS[arm_name]
            assert body["model"] == f"{arm['model']}({arm['effort']})"
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}],
                                        "usage": {"total_tokens": 42}})
    client = RouterSessionClient(admin_token="control-test", upstream_api_key="upstream-test")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        binding = await client.resolve(task, POLICY_ARMS[arm_name])
        reply = await client.complete(binding, [{"role": "user", "content": task.prompt}], tool_schemas(task))
        assert reply["usage"]["total_tokens"] == 42
        await client.close_binding(binding)
        execution = next(r for r in requests if r.url.path == "/v1/chat/completions")
        assert ("x-router-session" in execution.headers) == (arm_name not in {"fixed_strong", "fixed_mid"})
    finally:
        await client.aclose()


async def test_experimental_arm_overrides_cannot_silently_run_baseline():
    client = RouterSessionClient()
    try:
        with pytest.raises(SessionClientError, match="does not implement"):
            await client.resolve(load_tasks()[0], POLICY_ARMS["jev_packet"])
    finally:
        await client.aclose()
