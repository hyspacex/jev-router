#!/usr/bin/env python
"""The live client `evals/run_sessions.py` uses to execute a coding session.

It talks to a running jev-router over HTTP, exactly as any other harness
would: resolve a strict session, then send chat-completions requests with the
session, binding and request-id headers. Nothing here is Pi-specific and
nothing here duplicates the router's policy.

An arm that pins a model has no session to resolve. It sends the concrete
model straight through, which is the unmanaged passthrough contract, and the
report says which arm did that.

Offline tests use scripted HTTP transports; no test needs provider access.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx


class SessionClientError(RuntimeError):
    pass


class RouterSessionClient:
    """Resolve once per session, then execute on the binding it returned."""

    def __init__(
        self,
        router_url: str = "http://127.0.0.1:8318",
        admin_token: str = "",
        timeout_s: float = 300.0,
        client_name: str = "session-eval",
        upstream_api_key: str = "",
    ) -> None:
        self.base = router_url.rstrip("/")
        self.token = admin_token
        self.client_name = client_name
        self.upstream_api_key = upstream_api_key
        self._http = httpx.AsyncClient(timeout=timeout_s)
        self._counter = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    def _control(self) -> dict[str, str]:
        if not self.token:
            raise SessionClientError(
                "a strict session needs the router control credential; set "
                "JEV_ROUTER_ADMIN_TOKEN"
            )
        return {"X-Router-Admin-Token": self.token}

    async def resolve(self, task: Any, arm: dict[str, Any]) -> dict[str, Any]:
        """One binding for this session, or a pinned model for a fixed arm."""
        if arm.get("distribution_policy") or arm.get("adaptive_effort"):
            raise SessionClientError("this live client does not implement experimental arm overrides")
        if not arm.get("alias"):
            model = str(arm.get("model") or "")
            if not model:
                raise SessionClientError("a policy arm needs an alias or a model")
            return {
                "mode": "pinned",
                "model": model,
                "effort": arm.get("effort"),
                "wire_model": (
                    f"{model}({arm['effort']})" if arm.get("effort") else model
                ),
            }

        from run_sessions import tool_schemas

        session_id = "session-" + secrets.token_hex(8)
        payload = {
            "schema_version": "1",
            "intent": "new",
            "session_id": session_id,
            "request_id": "resolve-0001",
            "alias": arm["alias"],
            "client": self.client_name,
            "request": {
                "messages": [{"role": "user", "content": task.prompt}],
                "tools": tool_schemas(task),
            },
            "limits": {"max_output_tokens": 4096},
        }
        response = await self._http.post(
            f"{self.base}/router/resolve", headers=self._control(), json=payload
        )
        if response.status_code != 200:
            raise SessionClientError(
                f"resolve failed with {response.status_code}: {response.text[:300]}"
            )
        contract = response.json()
        execution = contract["execution"]
        report_response = await self._http.get(
            f"{self.base}/router/sessions/{session_id}", headers=self._control()
        )
        report_response.raise_for_status()
        report = report_response.json()
        expected = arm.get("decider")
        if expected and report.get("decision_source") != expected:
            await self.close_binding({"session_id": session_id})
            raise SessionClientError(f"expected {expected} admission, got {report.get('decision_source')}")
        return {
            "mode": "session",
            "session_id": session_id,
            "binding_revision": contract["binding_revision"],
            "model": execution["model_key"],
            "effort": execution["initial_effort"],
            "wire_model": execution["wire_model"],
            "context_window": execution["context_window"],
            "decision_source": report.get("decision_source"),
            "decision_rule": report.get("decision_rule"),
            "quality_lane": report.get("quality_lane"),
            "quota_at_admission": report.get("quota_at_admission"),
        }

    async def close_binding(self, binding: dict[str, Any]) -> None:
        if binding.get("session_id"):
            response = await self._http.post(
                f"{self.base}/router/sessions/{binding['session_id']}/close",
                headers=self._control(),
            )
            response.raise_for_status()

    async def complete(
        self,
        binding: dict[str, Any],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._counter += 1
        headers: dict[str, str] = {}
        if binding.get("mode") == "session":
            headers = {
                **self._control(),
                "X-Router-Session": binding["session_id"],
                "X-Router-Binding": binding["binding_revision"],
                "X-Router-Request-Id": f"execution-{self._counter:04d}",
            }
        if self.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.upstream_api_key}"
        body: dict[str, Any] = {
            "model": binding["wire_model"],
            "messages": messages,
            "max_tokens": 4096,
        }
        if tools:
            body["tools"] = tools
        response = await self._http.post(
            f"{self.base}/v1/chat/completions", headers=headers, json=body
        )
        if response.status_code != 200:
            raise SessionClientError(
                f"execution failed with {response.status_code}: "
                f"{response.text[:300]}"
            )
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return {
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "finish_reason": choice.get("finish_reason") or "",
            "usage": data.get("usage") or {},
        }
