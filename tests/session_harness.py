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
                # The effort is a request field, not part of the model name.
                # A native update moves the effective effort while the base
                # field stays put, so the two cannot share one encoding.
                "effort_style": "param",
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


# --- a fake Responses upstream -------------------------------------------

RESPONSES = f"{UPSTREAM}/v1/responses"
COMPACT = f"{UPSTREAM}/v1/responses/compact"


# Codex's own metadata header. Only two of its fields matter here: it says
# whether a request is a `turn` or Codex folding its own history up. The
# shapes below were read off codex-cli 0.155.1 on 2026-09-19 with a logging
# pass-through, and scrubbed of everything but identifiers and field names.
CODEX_TURN_METADATA = "x-codex-turn-metadata"


def codex_metadata(kind: str = "turn", window: int = 0) -> str:
    """What Codex sends about one request. Identifiers and flags only."""
    meta: dict[str, Any] = {
        "session_id": "01a0bbf2-31ed-7b32-803b-27a5f3f07d51",
        "thread_id": "01a0bbf2-31ed-7b32-803b-27a5f3f07d51",
        "turn_id": "01a0bbf2-6523-7300-b279-d44a53ec7879",
        "window_id": f"01a0bbf2-31ed-7b32-803b-27a5f3f07d51:{window}",
        "window_number": window,
        "request_kind": kind,
    }
    if kind == "compaction":
        meta["compaction"] = {
            "trigger": "auto",
            "reason": "context_limit",
            "implementation": "responses",
            "phase": "pre_turn",
            "strategy": "memento",
        }
    return json.dumps(meta)


def trigger_item() -> dict[str, Any]:
    """The provider's own explicit compaction request. Always the last item."""
    return {"type": "compaction_trigger"}


def compaction_item(item_id: str = "cmp_0001") -> dict[str, Any]:
    """A folded-up history, as the provider returns it.

    `encrypted_content` is opaque, about 1.8 kB on the deployment this was
    measured against. Nothing in the router ever looks inside it, so a short
    stand-in is enough and keeps no transcript in a fixture.
    """
    return {
        "type": "compaction",
        "id": item_id,
        "encrypted_content": "OPAQUE-" + "x" * 48,
    }


def response_payload(
    response_id: str,
    status: str = "completed",
    *,
    base_effort: str = "low",
    tool_calls: bool = False,
    compaction: bool = False,
    text: str = "Done.",
) -> dict[str, Any]:
    """One terminal response object.

    `reasoning.effort` reports the request's base effort, which is what the
    real API does. Nothing may read it as the effective effort.
    """
    output: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    ]
    if tool_calls:
        output.append(
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"csv.py"}',
            }
        )
    if compaction:
        # The provider answers an explicit trigger with one opaque item and
        # nothing else.
        output = [compaction_item(f"cmp_{response_id[-4:]}")]
    return {
        "id": response_id,
        "object": "response",
        "status": status,
        "model": "vendor/astra",
        "reasoning": {"effort": base_effort},
        "output": output,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 64,
            "total_tokens": 1264,
            "input_tokens_details": {"cached_tokens": 900},
            "output_tokens_details": {"reasoning_tokens": 40},
        },
    }


def sse_bytes(payload: dict[str, Any]) -> bytes:
    """A realistic event stream for one response, terminal event and all."""
    created = {
        "type": "response.created",
        "response": {"id": payload["id"], "status": "in_progress"},
    }
    delta = {"type": "response.output_text.delta", "delta": "Do"}
    delta2 = {"type": "response.output_text.delta", "delta": "ne."}
    terminal = {"type": f"response.{payload['status']}", "response": payload}
    blocks = []
    for event, body in (
        ("response.created", created),
        ("response.output_text.delta", delta),
        ("response.output_text.delta", delta2),
        (terminal["type"], terminal),
    ):
        blocks.append(f"event: {event}\ndata: {json.dumps(body)}\n\n")
    blocks.append("data: [DONE]\n\n")
    return "".join(blocks).encode()


class Seen(list):
    """The bodies the upstream received, plus the raw bytes it sent back."""

    raw: list[bytes]

    def __init__(self) -> None:
        super().__init__()
        self.raw = []


def responses_upstream(
    *,
    status: str = "completed",
    tool_calls: bool = False,
    sse: bool = True,
    response: httpx.Response | Exception | None = None,
    base_effort: str = "low",
    body_override: bytes | None = None,
):
    """Record every forwarded body and answer with a realistic reply."""
    seen = Seen()
    raw = seen.raw
    counter = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        counter[0] += 1
        items = body.get("input") or []
        triggered = bool(items) and isinstance(items[-1], dict) and (
            items[-1].get("type") == "compaction_trigger"
        )
        payload = response_payload(
            f"resp_{counter[0]:04d}",
            status,
            base_effort=base_effort,
            tool_calls=tool_calls,
            compaction=triggered,
        )
        if body_override is not None:
            raw.append(body_override)
            return httpx.Response(
                200,
                content=body_override,
                headers={"content-type": "text/event-stream"},
            )
        if not sse:
            content = json.dumps(payload).encode()
            raw.append(content)
            return httpx.Response(
                200, content=content, headers={"content-type": "application/json"}
            )
        content = sse_bytes(payload)
        raw.append(content)
        return httpx.Response(
            200, content=content, headers={"content-type": "text/event-stream"}
        )

    respx.post(RESPONSES).mock(side_effect=handler)
    return seen


def user_item(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": text}


def read_sse(body: bytes) -> list[dict[str, Any]]:
    """Every `data:` object in an event stream, in order."""
    out: list[dict[str, Any]] = []
    for block in body.decode().split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            out.append(json.loads(payload))
    return out


class ResponsesClient(Client):
    """A scripted client that keeps its own transcript, as a harness does.

    It supports both histories the experiment has to work with: a full replay
    that resends every item, and a `previous_response_id` chain that sends
    only what is new. The router never writes to this transcript; the client
    records the item the plan hands it, in the place the plan says.
    """

    def __init__(
        self,
        service: Service,
        *,
        history_mode: str = "full_history",
        alias: str = "auto-adaptive",
        **kwargs: Any,
    ) -> None:
        super().__init__(service, alias=alias, **kwargs)
        self.items: list[dict[str, Any]] = []
        self.history_mode = history_mode
        self.sent_upto = 0
        self.parent = ""
        self.turns = 0
        self.last_response: dict[str, Any] = {}

    # --- setting up -----------------------------------------------------

    async def start(self, **overrides: Any) -> httpx.Response:
        contract = {
            "protocols": ["openai-responses"],
            "turn_boundary_reporting": True,
        }
        contract.update(overrides.pop("client_contract", {}))
        return await self.resolve(client_contract=contract, **overrides)

    @property
    def base_effort(self) -> str:
        return self.execution.get("base_effort") or ""

    @property
    def effective_effort(self) -> str:
        return self.execution.get("effective_effort") or self.base_effort

    # --- one turn -------------------------------------------------------

    def settled_previous(self) -> dict[str, Any]:
        return {
            "id": f"turn-{self.turns:04d}" if self.turns else "",
            "settled": True,
            "pending_tool_calls": 0,
            "active_requests": 0,
        }

    async def plan(self, text: str, **overrides: Any) -> dict[str, Any]:
        self.turns += 1
        turn_id = overrides.pop("turn_id", f"turn-{self.turns:04d}")
        overrides.setdefault("previous", self.settled_previous())
        overrides.setdefault("context", {"history_mode": self.history_mode})
        response = await self.turn_plan(turn_id, request=text, **overrides)
        plan = response.json()
        plan["_status"] = response.status_code
        plan.setdefault("turn_id", turn_id)
        return plan

    def record(self, plan: dict[str, Any], text: str) -> None:
        """Write the planned item down, then the new user message after it."""
        update = plan.get("update") or {}
        if plan.get("action") == "change_effort" and "item" in update:
            self.items.append(update["item"])
        self.items.append(user_item(text))

    def request_body(self, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.wire_model(),
            "reasoning": {"effort": self.base_effort},
        }
        if self.history_mode == "previous_response_id" and self.parent:
            body["previous_response_id"] = self.parent
            body["input"] = list(self.items[self.sent_upto :])
        else:
            body["input"] = list(self.items)
        body.update(extra)
        return body

    async def send(
        self,
        plan: dict[str, Any] | None = None,
        *,
        turn_id: str = "",
        request_id: str | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> httpx.Response:
        body = self.request_body(**extra)
        sent = self.execution_headers(request_id)
        turn = turn_id or (plan or {}).get("turn_id") or ""
        if turn:
            sent["x-router-turn"] = turn
        if plan and plan.get("plan_id"):
            sent["x-router-turn-plan"] = plan["plan_id"]
        sent.update(headers or {})
        response = await self.service.client.post(
            "/v1/responses", json=body, headers=sent
        )
        if response.status_code == 200:
            self._absorb(response)
        return response

    def _absorb(self, response: httpx.Response) -> None:
        """Take the reply into the transcript, as a real client would."""
        self.sent_upto = len(self.items)
        try:
            events = (
                read_sse(response.content)
                if "text/event-stream" in response.headers.get("content-type", "")
                else [json.loads(response.content or b"{}")]
            )
        except ValueError:
            # A reply this client cannot read is a reply it learns nothing
            # from. The router is in the same position, which is the point.
            return
        for event in events:
            payload = event.get("response") if isinstance(event, dict) else None
            payload = payload if isinstance(payload, dict) else event
            if not isinstance(payload, dict) or payload.get("object") != "response":
                if not (isinstance(payload, dict) and payload.get("id", "").startswith("resp_")):
                    continue
            if payload.get("status") in ("completed", "incomplete"):
                self.last_response = payload
                self.parent = str(payload.get("id") or self.parent)
                for item in payload.get("output") or []:
                    self.items.append(item)
                self.sent_upto = len(self.items)

    async def turn(self, text: str, **overrides: Any) -> tuple[dict, httpx.Response]:
        """Plan a turn, record what it says, and send it."""
        plan = await self.plan(text, **overrides)
        if plan["_status"] != 200:
            return plan, None  # type: ignore[return-value]
        self.record(plan, text)
        return plan, await self.send(plan)

    async def compact(
        self, *, trigger: bool = False, declare: bool = True, **extra: Any
    ) -> httpx.Response:
        """Fold this history up, the way a client does it.

        Two shapes, both read off the wire on 2026-09-19. `trigger` is the
        provider's explicit flow: the whole history with a `compaction_trigger`
        as the final item, answered with one opaque `compaction` item. The
        default is Codex's own: an ordinary request carrying a summarising
        message, which nothing in the body distinguishes from a turn, so the
        client says what it is in a header.

        The transcript afterwards is the client's business, and both shapes
        rebuild it: this stands in for that.
        """
        items = list(self.items)
        if items and isinstance(items[0], dict) and items[0].get("type") == "tools":
            # Measured on codex-cli 0.155.1: a compaction request declares a
            # different tool set from a turn, so the first item of the history
            # carries a different id and every prefix hash behind it changes.
            items[0] = {**items[0], "id": "at_compaction"}
        if trigger:
            items.append(trigger_item())
        else:
            items.append(user_item("Summarise the work so far. Do not continue it."))
        body = {
            "model": self.wire_model(),
            "reasoning": {"effort": self.base_effort},
            "input": items,
        }
        body.update(extra)
        sent = self.execution_headers()
        if declare:
            sent["x-router-request-kind"] = "compaction"
            sent[CODEX_TURN_METADATA] = codex_metadata("compaction")
        response = await self.service.client.post(
            "/v1/responses", json=body, headers=sent
        )
        if response.status_code == 200:
            self.rebuild(response, folded=trigger)
        return response

    def rebuild(self, response: httpx.Response, *, folded: bool = False) -> None:
        """Start the window the client keeps after a compaction.

        Codex 0.155.1 throws its transcript away and writes a new one from the
        summary. The provider's explicit flow replays the opaque item instead.
        Either way every earlier update item is gone, which is the whole point
        of the epoch.
        """
        self.items = []
        self.sent_upto = 0
        if folded:
            events = (
                read_sse(response.content)
                if "text/event-stream" in response.headers.get("content-type", "")
                else [json.loads(response.content or b"{}")]
            )
            for event in events:
                payload = event.get("response") if isinstance(event, dict) else None
                payload = payload if isinstance(payload, dict) else event
                for item in (payload or {}).get("output") or []:
                    if isinstance(item, dict) and item.get("type") == "compaction":
                        self.items = [item]
        self.parent = ""

    async def continuation(self, output: str = "def parse(line): ...") -> httpx.Response:
        """Carry the same turn on after a tool result. No new plan."""
        self.items.append(
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": output,
            }
        )
        return await self.send(turn_id=f"turn-{self.turns:04d}")
