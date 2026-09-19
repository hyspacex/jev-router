"""A small scripted client for strict sessions, over the real app and store.

Nothing here knows about any particular harness product. It produces what any
coding client produces: a resolve, model requests, tool continuations, a
maintenance request, a restart and a resume. The upstreams are fakes; no test
in this suite talks to a network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from conftest import UPSTREAM, make_config

from jev_router.app import create_app
from jev_router.pins import Store

ADMIN = "test-admin"
CHAT = f"{UPSTREAM}/v1/chat/completions"

# Two strict aliases on top of the shared test config: one ordinary, one
# advertising a static envelope over a homogeneous pool.
SESSION_CONFIG: dict[str, Any] = {
    "session_routing": {
        "enabled": True,
        "prepared_ttl_seconds": 600,
        "max_inflight_per_session": 1,
        "require_control_token": True,
        "max_request_history": 50,
    },
    "models": {
        "big": {"max_output_tokens": 8192, "compatibility_revision": "chat-big-v1"},
        # Wider than the shared test config's 1000, which predates the output
        # reserve and could not hold one admission.
        "small": {
            "context_window": 32000,
            "max_output_tokens": 2048,
            "compatibility_revision": "chat-small-v1",
        },
        "responsey": {
            "upstream_id": "vendor/responsey",
            "context_window": 200000,
            "supports_tools": True,
            "supports_vision": False,
            "protocol": "openai-responses",
            "max_output_tokens": 4096,
        },
    },
    "aliases": {
        "auto-session": {
            "description": "Strict session binding.",
            "session_mode": "strict",
            "state_builder": "summary_v1",
            "questions": ["task", "difficulty", "harm_if_wrong"],
            "rules": "policy",
            "allowed_models": ["small", "big"],
            "admission_fallback": {"model": "big", "effort": "medium"},
        },
        "auto-session-bare": {
            "description": "Strict, with no configured admission fallback.",
            "session_mode": "strict",
            "state_builder": "summary_v1",
            "questions": ["task", "difficulty", "harm_if_wrong"],
            "rules": "policy",
            "allowed_models": ["small", "big"],
        },
    },
}


# --- the between-turn effort experiment ----------------------------------
#
# A verified profile, built here and never in router.yaml: no real deployment
# has been qualified, and marking one verified in the shipped file would be a
# claim nobody measured. The report the profile names is written into tmp_path
# by `adaptive_config`, so the file-existence check has something real to find.

TURN_QUESTIONS: dict[str, Any] = {
    "corrective_followup": {
        "type": "noul",
        "instructions": "Does the latest message name a defect in the earlier result?",
    },
    "failure_mode": {
        "type": "choice",
        "instructions": "Why has the work not succeeded yet?",
        "criteria": {
            "reasoning_problem": "The logic is wrong.",
            "missing_information": "A fact nobody supplied.",
            "environment_problem": "Something outside the work is in the way.",
            "unclear": "There is a failure and it does not say which.",
            "no_failure_evidence": "Nothing reports a failure.",
        },
    },
}


def adaptive_config(tmp_path, *, mode: str = "active", **overrides: Any):
    """A config with one qualified Responses profile and one alias using it."""
    report = tmp_path / "qualification.md"
    report.write_text("# a dated deployment report for the test profile\n")
    raw: dict[str, Any] = {
        "questions": TURN_QUESTIONS,
        "models": {
            "astra": {
                "upstream_id": "vendor/astra",
                "context_window": 200000,
                "supports_tools": True,
                "supports_vision": False,
                "efforts": ["low", "medium", "high", "xhigh"],
                "effort_style": "suffix",
                "protocol": "openai-responses",
                "max_output_tokens": 8192,
                "compatibility_revision": "responses-astra-test-v1",
                "effort_control": {
                    "between_turn": "native_configuration_update",
                    "qualification": "verified",
                    "cache_behavior": "unverified",
                    "qualification_ref": str(report),
                },
            },
            # The other tested strategy: the same model behind a parameter
            # change. Its cache behaviour is a separate question and it never
            # reports as native cache preserving.
            "paramy-native": {
                "upstream_id": "vendor/paramy-native",
                "context_window": 200000,
                "supports_tools": True,
                "supports_vision": False,
                "efforts": ["low", "medium", "high"],
                "effort_style": "param",
                "protocol": "openai-responses",
                "max_output_tokens": 8192,
                "compatibility_revision": "responses-paramy-test-v1",
                "effort_control": {
                    "between_turn": "request_parameter",
                    "qualification": "verified",
                    "cache_behavior": "verified_breaking",
                    "qualification_ref": str(report),
                },
            },
        },
        "aliases": {
            "auto-adaptive": {
                "description": "Strict session with the effort experiment on.",
                "session_mode": "strict",
                "adaptive_effort": True,
                "state_builder": "summary_v1",
                "questions": ["task", "difficulty", "harm_if_wrong"],
                "rules": "policy",
                "allowed_models": ["astra"],
            },
            "auto-adaptive-param": {
                "description": "The same, on the parameter-change strategy.",
                "session_mode": "strict",
                "adaptive_effort": True,
                "state_builder": "summary_v1",
                "questions": ["task", "difficulty", "harm_if_wrong"],
                "rules": "policy",
                "allowed_models": ["paramy-native"],
            },
        },
        "experiments": {
            "adaptive_effort": {
                "mode": mode,
                "qualified_profiles": ["astra", "paramy-native"],
                "ladder": ["low", "medium", "high"],
                "questions": ["difficulty", "harm_if_wrong", "corrective_followup"],
                "shadow_questions": ["failure_mode"],
            }
        },
    }
    for key, value in overrides.items():
        raw[key] = _deep(raw[key], value) if isinstance(raw.get(key), dict) else value
    return session_config(**raw)


def _deep(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        out[key] = _deep(out[key], value) if isinstance(value, dict) and isinstance(
            out.get(key), dict
        ) else value
    return out


def session_raw() -> dict[str, Any]:
    """The same config as a plain mapping, for writing a router.yaml."""
    import copy

    from conftest import BASE_CONFIG, _merge

    raw = copy.deepcopy(BASE_CONFIG)
    for key, value in SESSION_CONFIG.items():
        raw[key] = _merge(raw[key], value) if isinstance(raw.get(key), dict) else value
    return raw


def session_config(**overrides: Any):
    raw = dict(SESSION_CONFIG)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            merged = dict(raw[key])
            for k, v in value.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            raw[key] = merged
        else:
            raw[key] = value
    return make_config(**raw)


class Service:
    """One running router. `restart()` is a new process against the same file."""

    def __init__(self, tmp_path, config=None) -> None:
        self.path = tmp_path / "router.db"
        self.config = config or session_config()
        self.open()

    def open(self) -> None:
        self.store = Store(self.path)
        self.app = create_app(self.config, self.store)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://router.test",
        )

    @property
    def router(self):
        return self.app.state.router

    @property
    def sessions(self):
        return self.app.state.router.sessions

    async def close(self) -> None:
        await self.client.aclose()
        await self.router.aclose()

    async def restart(self) -> None:
        await self.close()
        self.open()


class Client:
    """The scripted caller. It holds a session id and the binding it was told."""

    def __init__(
        self,
        service: Service,
        session_id: str = "session-2f766788",
        alias: str = "auto-session",
        client: str = "coding-client",
        token: str = ADMIN,
    ) -> None:
        self.service = service
        self.session_id = session_id
        self.alias = alias
        self.client = client
        self.token = token
        self.binding: str | None = None
        self.execution: dict[str, Any] = {}
        self.counter = 0

    # --- control -------------------------------------------------------

    def control_headers(self) -> dict[str, str]:
        return {"x-router-admin-token": self.token} if self.token else {}

    async def resolve(self, **overrides: Any) -> httpx.Response:
        payload: dict[str, Any] = {
            "schema_version": "1",
            "intent": "new",
            "session_id": self.session_id,
            "request_id": "resolve-0001",
            "alias": self.alias,
            "client": self.client,
            "client_contract": {
                "dynamic_metadata": True,
                "protocols": ["openai-chat"],
                "fresh_execution_context": True,
                "turn_boundary_reporting": False,
            },
            "request": user_request(),
            "limits": {"max_output_tokens": 2048},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(payload.get(key), dict):
                payload[key] = {**payload[key], **value}
            elif value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        response = await self.service.client.post(
            "/router/resolve", json=payload, headers=self.control_headers()
        )
        if response.status_code == 200:
            self.binding = response.json()["binding_revision"]
            self.execution = response.json()["execution"]
        return response

    async def resume(self, **overrides: Any) -> httpx.Response:
        return await self.resolve(
            intent="resume",
            request=None,
            alias="",
            candidate_models=None,
            limits={},
            request_id="resume-0001",
            **overrides,
        )

    async def close_session(self) -> httpx.Response:
        return await self.service.client.post(
            f"/router/sessions/{self.session_id}/close", headers=self.control_headers()
        )

    async def show(self) -> httpx.Response:
        return await self.service.client.get(
            f"/router/sessions/{self.session_id}", headers=self.control_headers()
        )

    # --- turn boundaries -----------------------------------------------

    async def turn_plan(
        self,
        turn_id: str = "turn-0002",
        *,
        request: str = "Investigate why escaped quotes fail across chunks.",
        task: str = "Improve the CSV parser.",
        previous: dict[str, Any] | None = None,
        observations: list[dict[str, Any]] | None = None,
        binding: str | None = None,
        **overrides: Any,
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "schema_version": "1",
            "session_id": self.session_id,
            "binding_revision": binding or self.binding or "",
            "turn_id": turn_id,
            "request_id": f"plan-{turn_id}",
            "previous_turn": previous
            if previous is not None
            else {
                "id": "turn-0001",
                "settled": True,
                "pending_tool_calls": 0,
                "active_requests": 0,
            },
            "state": {
                "task_request": task,
                "current_user_request": request,
                "user_constraints": [],
                "quoted_material": [],
                "observations": observations or [],
            },
            "context": {
                "estimated_total_tokens": 12000,
                "estimate_method": "provider-usage-plus-new-input",
                "history_mode": "full_history",
            },
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(payload.get(key), dict):
                payload[key] = {**payload[key], **value}
            else:
                payload[key] = value
        return await self.service.client.post(
            "/router/turn-plan", json=payload, headers=self.control_headers()
        )

    async def reconcile(self, plan_id: str, outcome: str) -> httpx.Response:
        return await self.service.client.post(
            f"/router/turn-plan/{plan_id}/reconcile",
            json={"schema_version": "1", "outcome": outcome},
            headers=self.control_headers(),
        )

    # --- execution -----------------------------------------------------

    def next_request_id(self) -> str:
        self.counter += 1
        return f"execution-{self.counter:04d}"

    def execution_headers(
        self, request_id: str | None = None, binding: str | None = None
    ) -> dict[str, str]:
        headers = dict(self.control_headers())
        headers["x-router-session"] = self.session_id
        headers["x-router-binding"] = binding or self.binding or ""
        headers["x-router-request-id"] = request_id or self.next_request_id()
        return headers

    async def execute(
        self,
        body: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        binding: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        payload = body if body is not None else user_request(self.wire_model())
        payload = dict(payload)
        payload.setdefault("model", self.wire_model())
        sent = self.execution_headers(request_id, binding)
        sent.update(headers or {})
        return await self.service.client.post(
            "/v1/chat/completions", json=payload, headers=sent
        )

    def wire_model(self) -> str:
        return self.execution.get("wire_model", "")


# --- the bodies a coding client sends ------------------------------------


def user_request(model: str = "", text: str = "Add quoted CSV fields and tests.") -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 256,
    }
    if model:
        body = {"model": model, **body}
    return body


def tool_request(model: str, text: str = "Read the parser and fix it.") -> dict[str, Any]:
    """A first turn that offers tools."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read one file from the working tree.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }
        ],
        "max_tokens": 256,
    }


def tool_continuation(model: str, call_id: str = "call-1") -> dict[str, Any]:
    """The same turn carrying on after a tool result."""
    body = tool_request(model)
    body["messages"] = [
        *body["messages"],
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"csv.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "def parse(line): ..."},
    ]
    return body


def maintenance_request(model: str) -> dict[str, Any]:
    """A compaction-style request: the client asking for its own summary."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You summarise transcripts."},
            {"role": "user", "content": "Summarise the work so far in ten lines."},
        ],
        "max_tokens": 512,
    }


# --- fake upstreams ------------------------------------------------------


def upstream(response: httpx.Response | Exception | None = None):
    """Record every forwarded body, and answer with whatever was asked for."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if isinstance(response, Exception):
            raise response
        return response or httpx.Response(200, json={"id": "cmpl-1", "choices": []})

    respx.post(CHAT).mock(side_effect=handler)
    return seen


def models_sent(seen: list[dict[str, Any]]) -> list[str]:
    return [body.get("model") for body in seen]
