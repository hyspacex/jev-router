"""Optional real-browser test of the session inspector, with no providers.

Run with: uv run --with playwright pytest tests/test_dashboard_browser.py -q
Needs local Chrome (or Playwright's installed Chromium).

The two session endpoints are served here from fixture JSON shaped like the
contract (section D), mounted on the real app so the real ingress credential,
headers and asset route are what the browser talks to. The fixtures are test
data only. Set DASHBOARD_SHOTS=<dir> to also write screenshots.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import make_config
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from jev_router.app import create_app
from jev_router.dashboard import HEADERS

playwright = pytest.importorskip("playwright.sync_api")

RUNNING = "908c9c865852f977"
A_ID = "pi-32a7c1e04d5b4f0e9a1d6c2b7e8f9a01"
B_ID = "codex-bb93f2d6a0c14e7b8f3a5d9e1c2b4f60"
C_ID = "pi-0f3e9b2c7d8a4e61b5c0a9d8e7f6c5b4"
A_START = 1790271288.0
B_START = 1790318649.0

# t, messages, first byte ms, response ms, bytes, input tokens, headroom
A_ROWS = [
    [0.0, 2, 1659, 2252, 14681, 26453, 961259], [2.6, 5, 803, 2122, 33834, 29900, 957812],
    [5.9, 8, 899, 2165, 36682, 30365, 957347], [9.3, 11, 948, 997, 1088, 31016, 956696],
    [25.3, 13, 948, 1998, 23610, 31851, 955861], [29.4, 16, 999, 2267, 35823, 32768, 954944],
    [32.5, 18, 842, 2237, 34838, 33061, 954651], [35.5, 20, 1105, 1222, 1210, 33629, 954083],
    [37.2, 22, 944, 1053, 1123, 35047, 952665], [38.2, 24, 1028, 5366, 121152, 41162, 946529],
    [45.4, 27, 1007, 1103, 1075, 42152, 945440], [46.6, 29, 902, 2990, 57069, 42626, 944919],
    [51.1, 32, 1243, 4185, 58034, 44885, 942434], [57.9, 35, 1106, 2651, 23388, 45600, 941648],
    [61.8, 38, 1188, 6333, 162335, 46451, 940711], [68.3, 41, 1361, 1436, 1260, 49415, 937451],
    [71.4, 43, 1106, 3289, 39043, 49607, 937240], [77.8, 46, 949, 2683, 49640, 50255, 936527],
    [82.1, 48, 944, 6850, 172917, 50509, 936248], [91.0, 50, 1173, 5043, 114075, 51752, 934880],
    [96.7, 52, 1108, 4103, 80933, 52676, 933864], [101.3, 54, 952, 8678, 241023, 53420, 933046],
    [113.6, 56, 1249, 9546, 220334, 54698, 931640], [123.9, 58, 1193, 1270, 1078, 56126, 930069],
    [125.7, 60, 1235, 7157, 168141, 56571, 929579], [134.3, 62, 1245, 8477, 201270, 57752, 928280],
    [143.1, 64, 1159, 1490, 11428, 59043, 926860], [223.5, 66, 1699, 1797, 1104, 59119, 926777],
    [226.4, 68, 1074, 1519, 5558, 59157, 926735], [228.9, 70, 1706, 4860, 80527, 59538, 926316],
    [234.5, 72, 1005, 11050, 288746, 60052, 925750], [248.1, 74, 1284, 1890, 6098, 61508, 924149],
    [252.6, 76, 1306, 24121, 664189, 63716, 921720], [279.5, 78, 1681, 9607, 244722, 68982, 915927],
    [289.6, 80, 1141, 23964, 658823, 70320, 914456], [323.7, 82, 1694, 5401, 41417, 73775, 910655],
    [330.6, 85, 1026, 2856, 69001, 74585, 909764],
]
B_ROWS = [
    [0.0, 2, 2295, 3257, 8419, 26419, 361293], [3.3, 4, 1701, 2476, 6342, 26490, 361222],
    [6.5, 6, 1570, 2369, 6620, 26682, 361030], [8.9, 8, 1746, 4284, 23415, 29871, 357841],
    [14.0, 10, 5838, 6062, 4099, 30143, 357569], [20.1, 16, 3646, 7333, 35269, 37728, 349984],
    [28.3, 18, 7170, 7203, 4105, 42180, 345410], [35.7, 24, 3252, 56838, 466263, 46949, 340164],
    [203.9, 26, 1755, 6689, 45692, 49377, 337493], [211.2, 28, 5131, 9692, 42809, 52771, 333759],
    [221.7, 30, 2900, 29450, 235897, 54387, 331982],
]


def _facts(messages, tokens, headroom, window, *, tools=33, max_tokens=8192, reserve=None):
    reserve = reserve if reserve is not None else window - tokens - max_tokens - headroom
    return {
        "message_count": messages, "est_tokens": tokens, "input_tokens": tokens,
        "output_tokens": max_tokens, "reserve_tokens": reserve, "headroom": headroom, "context_window": window,
        "max_tokens": max_tokens, "has_tools": tools > 0, "tool_count": tools,
        "has_images": False, "has_code": True, "stream": True,
        "estimate_method": "chars_over_4", "session_mode": "strict",
    }


def _requests(prefix, start, rows, window, model, effort, first_row):
    out = []
    for seq, (t, messages, first_byte, total, size, tokens, headroom) in enumerate(rows, 1):
        out.append({
            "seq": seq, "request_id": f"{prefix}-request-{seq:03d}", "status": "completed",
            "created": start + t, "updated": start + t + total / 1000, "upstream_status": 200,
            "row": first_row + seq, "ts": start + t, "model": model, "effort": effort,
            "stream_state": "completed", "accepted": True, "first_byte_ms": float(first_byte),
            "response_ms": float(total), "response_bytes": size, "usage_reported": False,
            "turn_index": None, "turn_id": None, "event_type": "execution",
            "input_tokens": None, "output_tokens": None, "cached_input_tokens": None,
            "facts": _facts(messages, tokens, headroom, window),
        })
    return out


def _cond(key, source, op, threshold, value, passed):
    return {"key": key, "source": source, "op": op, "threshold": threshold, "value": value, "passed": passed}


def _trace_a():
    def rules(outcomes):
        spec = [
            ("images_easy", "simple-vision", True, [("has_images", "feature", "eq", True, False), ("difficulty", "answer", "lte", 1.4, 2.33)]),
            ("images_need_vision", "vision", True, [("has_images", "feature", "eq", True, False)]),
            ("very_hard", "hard-work", False, [("difficulty", "answer", "gte", 3.0, 2.33)]),
            ("hard", "hard-work", False, [("difficulty", "answer", "gte", 2.4, 2.33)]),
            ("high_harm", "hard-work", True, [("harm_if_wrong", "answer", "gte", 0.8, 0.27)]),
            ("moderate_careful", "moderate-careful", False, [("harm_if_wrong", "answer", "gte", 0.45, 0.27), ("difficulty", "answer", "gte", 1.4, 2.33)]),
            ("moderate_tools", "moderate-tools", False, [("has_tools", "feature", "eq", True, True), ("difficulty", "answer", "gte", 1.4, 2.33)]),
            ("moderate", "moderate", False, [("difficulty", "answer", "gte", 1.4, 2.33)]),
            ("easy", None, False, []),
        ]
        out = []
        for index, ((name, lane, protected, conds), outcome) in enumerate(zip(spec, outcomes), 1):
            conditions = []
            for key, source, op, threshold, value in conds:
                if outcome == "not_reached":
                    passed = None
                elif op == "eq":
                    passed = value == threshold
                elif op == "gte":
                    passed = value >= threshold
                else:
                    passed = value <= threshold
                conditions.append(_cond(key, source, op, threshold, value, passed))
            out.append({"index": index, "name": name, "lane": lane, "protected": protected,
                        "outcome": outcome, "conditions": conditions, "shift": None})
        return out

    outcomes = ["miss"] * 6 + ["hit", "not_reached", "not_reached"]
    return {
        "available": True, "source": "running", "config_hash": RUNNING, "reproduces": True,
        "stored": {"model": "glm-5.3", "effort": "high", "rule": "moderate_tools"},
        "replayed": {"model": "glm-5.3", "effort": "high", "rule": "moderate_tools", "lane": "moderate-tools"},
        "gate": {"applies": True, "min_confidence": 0.4, "questions": {"task": 0.98, "difficulty": 0.67},
                 "passed": True, "detail": "every gated question at or above 0.4"},
        "rules": rules(outcomes), "matched": "moderate_tools", "route": "mid_tools",
    }


def session_a():
    window = 1000000
    session = {
        "session_id": A_ID, "alias": "auto", "client": "pi", "state": "active", "version": 2,
        "created": A_START, "last_seen": A_START + 333.5, "closed_at": None,
        "model": "ollama/glm-5.3", "provider": "ollama", "protocol": "openai-chat",
        "context_window": window, "max_output_tokens": 8192, "supports_tools": True,
        "supports_vision": False, "base_effort": "high", "effective_effort": "high",
        "expected_effort": None, "confirmed_effort": None, "effort_mode": "fixed",
        "adaptation_mode": "off", "effort_lineage": "known", "decision_id": "25d58e7563e0",
        "decision_rule": "moderate_tools", "decision_source": "jev", "quality_lane": "moderate-tools",
        "quota_changed_choice": False, "config_hash": RUNNING, "jev_model": "jev-1.13.0",
        "compaction_epoch": 0, "compaction_state": None, "last_input_tokens": None, "last_output_tokens": None,
    }
    admission = {
        "id": 730, "ts": A_START - 0.4, "decision_id": "25d58e7563e0", "alias": "auto", "client": "pi",
        "config_hash": RUNNING, "model": "ollama/glm-5.3", "effort": "high", "rule": "moderate_tools",
        "mode": "active", "fallback": False, "pinned": False, "jev_ms": 183.0, "jev_tokens": 1651,
        "est_tokens": 26453, "message_count": 2, "route": "mid_tools", "event_type": "admission",
        "session_id": A_ID, "quality_lane": "moderate-tools",
        "answers": {
            "task": {"type": "choice", "choice": "debugging", "confidence": 0.98,
                     "probabilities": {"debugging": 0.99, "agentic-tool-task": 0.01, "code-edit": 0.0, "other": 0.0}},
            "difficulty": {"type": "score", "score": 2.33, "confidence": 0.67,
                           "probabilities": {"0": 0.0, "1": 0.0, "2": 0.67, "3": 0.33}},
            "harm_if_wrong": {"type": "noul", "noul": 0.27},
        },
        "shadow_answers": None, "pressures": {"openai": 0.0041, "xai": 0.0}, "shifted": None,
        # The zero-pressure choice is stored on every row; equal means no effect.
        "reordered": False, "counterfactual": "ollama/glm-5.3(high)", "exclusions": [
            {"model": "gpt-6-luna", "reason": "the client does not permit it"},
        ],
        "evidence": "qualified", "qualification_ref": "evals/reports/2026-09-20-glm-effort/summary.md",
        "packet_version": "routing_v3", "shadow_packet_version": None,
        "facts": _facts(2, 26453, 961259, window),
        "state_shape": {
            "builder": "continuation_aware_v1",
            "parts": {"latest_user_request": 127, "material_the_user_pasted": 0, "conversation_so_far": 412},
            "facts": {"message_count": 2, "estimated_prompt_tokens": 26399, "contains_code_blocks": True,
                      "request_has_tools": True, "tool_names": 20, "client": "pi"},
        },
        "quota_snapshot": {
            "openai": {"status": "fresh", "pressure": 0.0041, "age_seconds": 41.0},
            "xai": {"status": "fresh", "pressure": 0.0, "age_seconds": 41.0},
            "ollama": {"status": "error", "pressure": None, "age_seconds": None},
        },
    }
    return with_turns_a({
        "config_hash": RUNNING, "session": session, "admission": admission,
        "requests": _requests("a", A_START, A_ROWS, window, "glm-5.3", "high", 730),
        "plans": [], "trace": _trace_a(),
    })


def session_b():
    window = 400000
    session = {
        "session_id": B_ID, "alias": "auto-session-rules", "client": "codex", "state": "active", "version": 4,
        "created": B_START, "last_seen": B_START + 251.1, "closed_at": None,
        "model": "gpt-6-astra", "provider": "openai", "protocol": "openai-responses",
        "context_window": window, "max_output_tokens": 8192, "supports_tools": True,
        "supports_vision": True, "base_effort": "high", "effective_effort": "xhigh",
        "expected_effort": "high", "confirmed_effort": "xhigh", "effort_mode": "adaptive",
        "adaptation_mode": "active", "effort_lineage": "known", "decision_id": "f0cb2615c408",
        "decision_rule": "hard", "decision_source": "jev", "quality_lane": "hard-work",
        "quota_changed_choice": None, "config_hash": "1c44e0a9b7d2c315", "jev_model": "jev-1.13.0",
        "compaction_epoch": 0, "compaction_state": None, "last_input_tokens": 54387, "last_output_tokens": 2210,
    }
    admission = {
        "id": 902, "ts": B_START - 0.3, "decision_id": "f0cb2615c408", "alias": "auto-session-rules",
        "client": "codex", "config_hash": "1c44e0a9b7d2c315", "model": "gpt-6-astra", "effort": "high",
        "rule": "hard", "mode": "active", "fallback": False, "pinned": False, "jev_ms": 137.0,
        "jev_tokens": 1639, "est_tokens": 26419, "message_count": 2, "route": "frontier_high",
        "event_type": "admission", "session_id": B_ID, "quality_lane": "hard-work",
        "answers": {
            "task": {"type": "choice", "choice": "design-or-review", "confidence": 0.75,
                     "probabilities": {"design-or-review": 0.78, "agentic-tool-task": 0.22}},
            "difficulty": {"type": "score", "score": 2.5, "confidence": 0.5},
            "harm_if_wrong": {"type": "noul", "noul": 0.44},
        },
        "shadow_answers": {"failure_mode": {"type": "choice", "choice": "reasoning", "confidence": 0.61}},
        # Quota changed this one; NULL exclusions on an annotated row mean none.
        "pressures": None, "shifted": None, "reordered": False, "counterfactual": "gpt-6-luna(high)",
        "exclusions": None, "evidence": "qualified", "qualification_ref": None,
        "packet_version": "routing_v3", "shadow_packet_version": "turn_v1",
        "facts": {**_facts(2, 26419, 361293, window), "reserve_tokens": None},
        "state_shape": None,
        "quota_snapshot": {
            "openai": {"status": "fresh", "pressure": 0.18, "age_seconds": 12.0},
            "xai": {"status": "stale", "pressure": 0.4, "age_seconds": 5400.0},
        },
    }
    reqs = _requests("b", B_START, B_ROWS, window, "gpt-6-astra", "high", 902)
    unknown = reqs[9]
    unknown.update(status="unknown", upstream_status=200, stream_state="interrupted",
                   response_ms=None, response_bytes=None, usage_reported=None)
    reqs.append({
        "seq": 12, "request_id": "b-request-012", "status": "rejected", "created": B_START + 248.0,
        "updated": B_START + 248.0, "upstream_status": None, "row": None, "ts": None, "model": None,
        "effort": None, "stream_state": None, "accepted": None, "first_byte_ms": None,
        "response_ms": None, "response_bytes": None, "usage_reported": None,
        "turn_index": None, "turn_id": None, "event_type": None,
        "input_tokens": None, "output_tokens": None, "cached_input_tokens": None, "facts": {},
    })

    def plan(seq, request, action, status, from_, to, rec, expected, confirmed, created):
        return {
            "plan_id": f"plan-{seq}", "turn_id": f"turn-{seq}", "sequence": seq, "request_id": request,
            "mode": "active", "action": action, "status": status, "from_effort": from_, "to_effort": to,
            "base_effort": "high", "recommendation": rec, "expected_effort": expected,
            "confirmed_effort": confirmed, "reconciled": None, "compaction_epoch": 0,
            "reason": "upward_jump" if to != from_ else "steady", "created": created,
            "updated": created + 5, "shadow_counterfactual": None, "judgment": None,
            "evidence": {
                "difficulty": None, "p_hard": None, "harm_if_wrong": None, "corrective_followup": None,
                "failure_mode": None, "short_continuation": False, "missing": [], "error": False,
                "thresholds": dict(PLAN_THRESHOLDS), "ladder": ["medium", "high", "xhigh"],
                "floor": "medium", "target": to, "direction": "up" if to == "xhigh" and from_ != to else "down" if to != from_ else "hold",
                "confirmations": None, "needed_confirmations": 2,
            },
        }

    plans = [
        plan(1, "b-request-003", "change_effort", "confirmed", "high", "xhigh", "xhigh", "xhigh", "xhigh", B_START + 6.0),
        plan(2, "b-request-006", "keep", "confirmed", "xhigh", "xhigh", "xhigh", "xhigh", "xhigh", B_START + 19.5),
        plan(3, "b-request-009", "change_effort", "accepted", "xhigh", "high", "high", "high", None, B_START + 203.0),
        plan(4, None, "keep", "planned", "high", "high", "medium", None, None, B_START + 220.0),
    ]
    plans[3]["shadow_counterfactual"] = {"failure_mode": "reasoning", "would": "keep"}
    return with_turns_b({
        "config_hash": RUNNING, "session": session, "admission": admission, "requests": reqs,
        "plans": plans, "trace": {"available": False, "reason": "not_on_file"},
    })


def session_c():
    """A session where almost nothing was recorded: every gap must show as one."""
    session = {key: None for key in session_a()["session"]}
    session.update(session_id=C_ID, client="pi", state="closed", created=A_START - 86400, model="xai/grok-5",
                   tombstone=False, config_matches=False, turn_count=0, request_count=2)
    return {
        "config_hash": RUNNING, "session": session, "admission": None,
        "requests": [
            {"seq": 1, "request_id": "c-request-001", "status": "completed", "created": None, "updated": None,
             "upstream_status": None, "row": None, "ts": None, "model": None, "effort": None,
             "stream_state": None, "accepted": None, "first_byte_ms": None, "response_ms": None,
             "response_bytes": None, "usage_reported": None, "turn_index": None, "turn_id": None,
             "event_type": None, "input_tokens": None, "output_tokens": None,
             "cached_input_tokens": None, "facts": None},
            {"seq": 2, "request_id": "c-request-002", "status": "rejected", "created": None, "updated": None,
             "upstream_status": None, "row": None, "ts": None, "model": None, "effort": None,
             "stream_state": None, "accepted": None, "first_byte_ms": None, "response_ms": None,
             "response_bytes": None, "usage_reported": None, "turn_index": None, "turn_id": None,
             "event_type": None, "input_tokens": None, "output_tokens": None,
             "cached_input_tokens": None, "facts": {}},
        ],
        "plans": None, "trace": {"available": False, "reason": "no_admission_row"},
        # No turn rows at all: the page falls back to one admission turn.
        "turns": [], "turn_join": "by_index",
    }


F_ID = "pi-fa11bac0d4e24c6f8b1a0d9e8c7b6a50"


def session_fallback():
    """Session A as a fallback admission: Jev failed, no rule ran, no lane, no quota record."""
    item = copy.deepcopy(session_a())
    item["session"].update(session_id=F_ID, decision_rule="admission_fallback", quality_lane="frontier_medium",
                           feedback=None, feedback_verdict=None)
    adm = item["admission"]
    adm.update(fallback=True, rule="admission_fallback", route="frontier_medium", quality_lane="frontier_medium",
               answers={}, counterfactual=None, evidence=None, qualification_ref=None, jev_ms=None, jev_tokens=None)
    first = item["turns"][0]
    first["decision"].update(rule="admission_fallback", route="frontier_medium", quality_lane="frontier_medium",
                             fallback=True)
    first["jev"] = {**first["jev"], "answers": {}, "latency_ms": None, "tokens": None}
    item["trace"] = {"available": False, "reason": "fallback_admission"}
    return item


# What the list shows of each session, shaped from the seeded server's rows.
LABELS = {
    A_ID: {"title": "Getting this on startup since the pydantic upgrade, any idea?",
           "prompt_preview": "Getting this on startup since the pydantic upgrade, any idea? Traceback (most recent call last):",
           "workspace_hint": "inventory-service", "turn_count": 2, "feedback_verdict": "too_weak"},
    B_ID: {"title": "Design and implement an incremental re-index for the search service.",
           "prompt_preview": "Design and implement an incremental re-index for the search service.",
           "workspace_hint": "search-index", "turn_count": 5, "feedback_verdict": None},
    C_ID: {"title": None, "prompt_preview": None, "workspace_hint": None, "turn_count": None,
           "feedback_verdict": None},
}
PLAN_THRESHOLDS = {
    "upgrade_difficulty": 1.8, "upgrade_harm": 0.6, "upgrade_p_hard": 0.4, "corrective_followup": 0.6,
    "downgrade_difficulty": 0.8, "downgrade_harm": 0.2, "quota_pressure_at": 0.6, "context_safety_fraction": 0.8,
}
A_PROMPTS = [
    "The importer hangs after the first batch when the queue is full. Find out why and fix it.",
    "Good. Now add a test that fills the queue and checks the importer drains it.",
    "Run the whole suite and fix whatever lint complains about.",
]
B_PROMPTS = [
    "Review the plan for splitting the auth module into its own service.",
    "No, that misses the token refresh path entirely. Look again at refresh.py.",
    "Write the migration order down as a checklist.",
    "Rename the checklist headings to match the RFC.",
    "Thanks, that's all.",
]
FEEDBACK_FIELDS = ("verdict", "note", "source", "ts", "turn_index")


def _median(values):
    values = sorted(v for v in values if v is not None)
    return values[(len(values) + 1) // 2 - 1] if values else None


def _sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _rollup(calls, tools=(), summary=None):
    """execution_rollup's shape, from the fixture calls. Gaps stay None."""
    tps = [r["output_tokens"] * 1000 / max(r["response_ms"] - r["first_byte_ms"], 1)
           for r in calls if r["output_tokens"] is not None and r["response_ms"] and r["first_byte_ms"]]
    return {
        "http_requests": len(calls), "usage_reported": sum(1 for r in calls if r["usage_reported"]),
        "input_tokens": _sum(r["input_tokens"] for r in calls),
        "output_tokens": _sum(r["output_tokens"] for r in calls),
        "cached_input_tokens": _sum(r["cached_input_tokens"] for r in calls),
        "first_byte_ms_p50": _median(r["first_byte_ms"] for r in calls), "tps_p50": _median(tps),
        "tool_calls": [{"name": n, "count": c} for n, c in tools], "assistant_summary": summary,
        "files_touched": [],
    }


def _jev(row):
    return {"latency_ms": row["jev_ms"], "tokens": row["jev_tokens"], "answers": row["answers"],
            "shadow_answers": row["shadow_answers"], "packet_version": row["packet_version"],
            "shadow_packet_version": row["shadow_packet_version"]}


def _turn(index, kind, ts, row, prompt, calls, decision, *, state=None, shape=None, jev=None, plan=None,
          rollup=None, feedback=()):
    for r in calls:
        r["turn_index"] = index
    return {
        "turn_index": index, "kind": kind, "ts": ts, "decision_row": row, "user_prompt": prompt,
        "state": state, "state_shape": shape, "jev": jev,
        "decision": {"model": None, "effort": None, "rule": None, "route": None, "quality_lane": None,
                     "fallback": False, **decision},
        "plan": plan, "execution_rollup": rollup or _rollup(calls), "requests": [r["seq"] for r in calls],
        "feedback": [dict(f) for f in feedback],
    }


def _session_v2(session, turns, title, workspace, feedback=None, escalations=0, corrective=0):
    """The fields the turn-capture branch added to the session, shaped like the seeded JSON."""
    session.update(
        tombstone=False, state_builder="continuation_aware_v1", title=title, prompt_preview=title,
        workspace_hint=workspace, turn_count=len(turns), escalation_count=escalations,
        corrective_count=corrective, feedback_verdict=feedback["verdict"] if feedback else None,
        config_matches=session["config_hash"] == RUNNING,
        request_count=sum(len(t["requests"]) for t in turns), cache_hit_ratio=None,
        feedback={k: feedback[k] for k in FEEDBACK_FIELDS} if feedback else None,
    )


def with_turns_a(item):
    """A Pi session: an admission and two user turns with no plan; feedback stepped the effort."""
    reqs, adm = item["requests"], item["admission"]
    for r in reqs[:27]:  # the last turn's calls reported no usage: gaps
        r.update(usage_reported=True, input_tokens=r["facts"]["input_tokens"], output_tokens=900,
                 cached_input_tokens=int(r["facts"]["input_tokens"] * 0.8))
    for r in reqs[27:]:
        r["effort"] = "xhigh"
    weak = {"verdict": "too_weak", "note": "missed the second queue", "source": "pi",
            "ts": A_START + 140.0, "turn_index": 2}
    state = {
        "latest_user_request": A_PROMPTS[0],
        "material_the_user_pasted": "Traceback (most recent call last):\n  File \"importer.py\", line 88, in drain\nqueue.Full",
        "facts": {"message_count": 2, "estimated_prompt_tokens": 26399, "contains_code_blocks": True,
                  "request_has_tools": True, "client": "pi", "pasted_material_characters": 92},
    }
    base = {"model": "ollama/glm-5.3", "quality_lane": "moderate-tools"}
    turns = [
        _turn(1, "admission", adm["ts"], adm["id"], A_PROMPTS[0], reqs[:12],
              {**base, "effort": "high", "rule": "moderate_tools", "route": "mid_tools"},
              state=state, shape=adm["state_shape"], jev=_jev(adm),
              rollup=_rollup(reqs[:12], [("bash", 5), ("read_file", 3)], "The importer blocked on a full queue; it now drains before it refills.")),
        _turn(2, "user_turn", A_START + 25.0, 750, A_PROMPTS[1], reqs[12:27],
              {**base, "effort": "high", "rule": "user_turn"}, feedback=[weak]),
        _turn(3, "user_turn", A_START + 223.0, 770, A_PROMPTS[2], reqs[27:],
              {**base, "effort": "xhigh", "rule": "user_turn"}),
    ]
    _session_v2(item["session"], turns, A_PROMPTS[0], "importer", feedback=weak)
    item["session"]["effective_effort"] = "xhigh"
    item.update(turns=turns, turn_join="by_index")
    return item


def with_turns_b(item):
    """A Codex session on adaptive effort: an admission and four plan turns. Joined by log order."""
    reqs, adm, plans = item["requests"], item["admission"], item["plans"]
    answers = [  # difficulty, harm, corrective: one per plan turn
        (2.1, 0.4, 0.82), (1.9, 0.3, 0.1), (0.5, 0.1, 0.05), (0.4, 0.1, 0.02),
    ]
    plans[2]["evidence"].update(confirmations=2, direction="down")
    plans[3]["evidence"].update(confirmations=1, direction="down")
    groups = [reqs[0:2], reqs[2:5], reqs[5:8], reqs[8:11], []]
    base = {"model": "gpt-6-astra", "quality_lane": "hard-work"}
    turns = [_turn(1, "admission", adm["ts"], adm["id"], B_PROMPTS[0], groups[0],
                   {**base, "effort": "high", "rule": "hard", "route": "frontier_high"},
                   jev=_jev(adm))]
    for k, plan in enumerate(plans):
        d, h, c = answers[k]
        plan["evidence"].update(difficulty=d, harm_if_wrong=h, corrective_followup=c)
        jev = {"latency_ms": 90.0 + k, "tokens": 1200, "packet_version": "turn_v1", "shadow_packet_version": None,
               "shadow_answers": None, "answers": {
                   "difficulty": {"type": "score", "score": d, "confidence": 0.8},
                   "harm_if_wrong": {"type": "noul", "noul": h},
                   "corrective_followup": {"type": "noul", "noul": c}}}
        state = {"task_request": B_PROMPTS[0], "current_user_request": B_PROMPTS[k + 1],
                 "user_constraints": ["keep the public API"] if k == 0 else [], "quoted_material": [],
                 "observations": [], "facts": {"turn_history_available": True}}
        effort = plan["confirmed_effort"] or plan["expected_effort"] or plan["from_effort"]
        turns.append(_turn(k + 2, "plan", plan["created"], 903 + k, B_PROMPTS[k + 1], groups[k + 1],
                           {**base, "effort": effort, "rule": "effort_" + plan["action"]},
                           state=state, jev=jev, plan=plan))
    for r in reqs:  # rows from before the turn column: the turn is inferred
        r["turn_index"] = None
    _session_v2(item["session"], turns, B_PROMPTS[0], "auth-service", escalations=1, corrective=1)
    item.update(turns=turns, turn_join="by_order")
    return item


BOOKMARKS = [
    {"slug": "debugging", "label": "Debugging", "session_id": A_ID, "created": A_START},
    {"slug": "design-review", "label": "Design review", "session_id": B_ID, "created": B_START},
]


def listing(details, params=None):
    """The list contract, filtered the way the router filters it (a fixture, not the SQL)."""
    params = dict(params or {})
    params.pop("limit", None)
    pins = {b["session_id"]: b for b in BOOKMARKS}
    out = []
    for item in details.values():
        s = item["session"]
        counts: dict[str, int] = {}
        for r in item["requests"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        problems = [k for k in ("unknown", "rejected", "stream_failed") if counts.get(k)]
        if s["decision_rule"] == "low_confidence":
            problems.append("low_confidence")
        times = sorted(r["response_ms"] for r in item["requests"] if r["response_ms"] is not None)
        pin = pins.get(s["session_id"])
        out.append({
            "session_id": s["session_id"], "alias": s["alias"], "client": s["client"], "state": s["state"],
            "created": s["created"], "last_seen": s["last_seen"], "closed_at": s["closed_at"],
            "model": s["model"], "effective_effort": s["effective_effort"],
            "effort": s["base_effort"], "rule": s["decision_rule"], "quality_lane": s["quality_lane"],
            "config_hash": s["config_hash"], "config_matches": s["config_hash"] == RUNNING,
            "adaptation_mode": s["adaptation_mode"], "tombstone": False, "requests": counts,
            "problems": problems,
            "response_ms": {"n": len(times), "p50": times[(len(times) + 1) // 2 - 1] if times else None},
            "pinned": {"slug": pin["slug"], "label": pin["label"]} if pin else None,
            **LABELS[s["session_id"]],
        })
    out.sort(key=lambda row: -(row["last_seen"] or row["created"] or 0))
    facets = {
        name: [{"value": v, "n": sum(1 for r in out if r[key] == v)}
               for v in sorted({r[key] for r in out if r[key]})]
        for name, key in (("client", "client"), ("model", "model"), ("rule", "rule"))
    }
    facets["feedback"] = [
        {"value": v, "n": sum(1 for r in out if (r["feedback_verdict"] or "unrated") == v)}
        for v in sorted({r["feedback_verdict"] or "unrated" for r in out})
    ]
    keep = []
    for row in out:
        if params.get("client") and row["client"] != params["client"]:
            continue
        if params.get("model") and row["model"] != params["model"]:
            continue
        if params.get("state") and row["state"] != params["state"]:
            continue
        if params.get("adaptive") and row["adaptation_mode"] != params["adaptive"]:
            continue
        if params.get("pinned") == "1" and not row["pinned"]:
            continue
        if params.get("q") and not any(params["q"] in (row[k] or "") for k in ("session_id", "title", "prompt_preview")):
            continue
        if params.get("feedback") and (row["feedback_verdict"] or "unrated") != params["feedback"]:
            continue
        problem = params.get("problem")
        if problem and not (row["problems"] if problem == "any" else problem in row["problems"]):
            continue
        keep.append(row)
    return {
        "config_hash": RUNNING, "limit": 200, "matched": len(keep),
        "bookmarks": BOOKMARKS, "facets": facets, "sessions": keep,
    }


def overview(window, now):
    """The overview contract, computed by the real pure builder from fixture rows."""

    from jev_router.dashboard import WINDOWS, build_overview

    span, step = WINDOWS[window]

    since = (now // step + 1) * step - span
    raw = {
        "sessions": [
            {"created": now - 3000, "adaptation_mode": "off", "quota_changed_choice": 0},
            {"created": now - 2000, "adaptation_mode": "active", "quota_changed_choice": 1},
        ],
        "requests": [{"created": now - 2900 + i * 30, "status": "completed"} for i in range(20)]
        + [{"created": now - 1000, "status": "unknown"}, {"created": now - 900, "status": "rejected"}],
        "decisions": [
            {"ts": now - 3000, "event_type": "admission", "rule": "moderate_tools", "model": "ollama/glm-5.3",
             "effort": "high", "jev_ms": 183.0, "fallback": 0},
            {"ts": now - 2000, "event_type": "admission", "rule": "hard", "model": "gpt-6-astra",
             "effort": "high", "jev_ms": 137.0, "fallback": 0},
        ] + [
            {"ts": now - 2900 + i * 30, "event_type": "execution", "model": "gpt-6-astra" if i % 2 else "ollama/glm-5.3",
             "first_byte_ms": None if i % 5 == 0 else 800.0 + i * 10, "response_ms": 2000.0 + i * 50,
             "usage_reported": 0}
            for i in range(20)
        ] + [{"ts": now - 500, "event_type": None, "rule": "easy", "model": "m", "jev_ms": 120.0,
              "pinned": 0, "fallback": 0, "first_byte_ms": 700.0, "response_ms": 1500.0}],
        "plans": [
            {"created": now - 1900, "mode": "active", "action": "change_effort", "status": "confirmed",
             "from_effort": "high", "to_effort": "xhigh", "recommendation": "xhigh", "jev_ms": 90.0},
            {"created": now - 1500, "mode": "active", "action": "keep", "status": "outcome_unknown",
             "from_effort": "xhigh", "recommendation": "medium", "jev_ms": 85.0},
        ],
        "quota": [{"ts": now - 60, "snapshot": {
            "openai": {"status": "fresh", "pressure": 0.18, "age_seconds": 12.0, "windows": [
                {"name": "primary", "used_percent": 41.0, "window_minutes": 300.0, "resets_at": now + 3600},
                {"name": "secondary", "used_percent": 12.5, "window_minutes": 10080.0},
            ]},
            "xai": {"status": "stale", "pressure": 0.4, "age_seconds": 5400.0, "windows": []},
        }}],
    }
    body = build_overview(raw, window, since)
    body["config_hash"] = RUNNING
    body["subscriptions"] = subscriptions(now)
    return body


def subscriptions(now):
    """`live_quota` as the monitor reports it: one routed provider read from
    CodexBar, one not polled, one tracked subscription with plans and cost."""
    def window(name, minutes, used, expected, resets):
        return {"used_percent": used, "expected_percent": expected,
                "pace_delta": None if expected is None else round(used - expected, 2),
                "resets_at": now + resets, "window_minutes": minutes, "name": name,
                "reset_description": None}

    def routed(provider, models, **extra):
        return {"pressure": 0.0, "age_seconds": None, "fetched_at": None, "used_percent": None,
                "demote_above": 0.6, "exhausted_at": 100.0, "polls": 0, "errors": 0,
                "provider": provider, "enabled": False, "source": "codexbar",
                "codexbar_provider": None, "via": None, "status": "unknown", "steering": None,
                "exhausted": None, "windows": [],
                "models": [{"model": m, "quota_cost": 1.0, "pressure": 0.0} for m in models], **extra}

    return {
        "as_of": now, "quota_routing": True, "demote_above": 0.6, "exhausted_at": 100.0,
        "routed": [
            routed("openai", ["gpt-6-astra", "gpt-6-sol"], enabled=True, codexbar_provider="codex",
                   via="snapshot", status="fresh", steering="calm", age_seconds=34.0, polls=1,
                   used_percent=40.0, windows=[window("secondary", 10080.0, 40.0, 57.14, 259200)]),
            routed("ollama", ["ollama/glm-5.3"]),
        ],
        "tracked": [{
            "age_seconds": 180.0, "fetched_at": now - 180, "credits_remaining": None, "polls": 1,
            "errors": 0, "id": "claude", "via": "snapshot", "status": "fresh",
            "windows": [window("primary", 300.0, 12.0, 50.0, 9000),
                        window("secondary", 10080.0, 43.0, 71.43, 172800)],
            "plans": [{"title": "Session", "used_percent": 12.0}, {"title": "Weekly", "used_percent": 43.0}],
            "tokens": {"session_tokens": 5000.0, "session_cost_usd": 0.75,
                       "last_30_days_tokens": 70000000.0, "last_30_days_cost_usd": 812.0},
            "currency": "USD",
        }],
    }


SANDBOX_OK = {
    "row": 730, "decision_id": "25d58e7563e0", "event_type": "admission", "config_matches": True,
    "questions": ["task", "difficulty", "harm_if_wrong"], "overridden": [], "jev_ms": 241.0,
    "original_answers": {"task": {"type": "choice", "choice": "debugging", "confidence": 0.98},
                         "difficulty": {"type": "score", "score": 2.33, "confidence": 0.67},
                         "harm_if_wrong": {"type": "noul", "noul": 0.27}},
    "sandbox_answers": {"task": {"type": "choice", "choice": "debugging", "confidence": 0.97},
                        "difficulty": {"type": "score", "score": 2.98, "confidence": 0.7},
                        "harm_if_wrong": {"type": "noul", "noul": 0.27}},
    "original_route": {"rule": "moderate_tools", "route": "mid_tools", "model": "ollama/glm-5.3", "effort": "high", "lane": "moderate-tools"},
    "sandbox_route": {"rule": "very_hard", "route": "frontier_xhigh", "model": "gpt-6-astra", "effort": "xhigh", "lane": "hardest-work"},
}
CONFIG = {
    "config_hash": RUNNING,
    "questions": {
        "task": {"type": "choice", "choices": ["debugging", "code-edit"], "instructions": "Which kind of request is this?", "line": 10},
        "difficulty": {"type": "score", "levels": 4, "max": 3, "instructions": "How hard is this request?", "line": 20,
                       "criteria": [{"what": f"Level {n}.", "examples": [f"example {n}"]} for n in range(4)]},
        "harm_if_wrong": {"type": "noul", "max": 1, "instructions": "Would a mistake cause real harm?", "line": 30},
        "corrective_followup": {"type": "noul", "max": 1, "instructions": "Does the follow-up name a defect?", "line": 40},
    },
}


def fixture_routes(details, posted=None):
    now = time.time()  # one clock, so an unchanged overview stays unchanged
    posted = posted if posted is not None else []

    async def feedback(request: Request):
        body = json.loads(await request.body())
        sid = request.path_params["session_id"]
        posted.append(("feedback", sid, body))
        item = details[sid]
        mark = {"verdict": body["verdict"], "note": None, "source": body.get("source"),
                "ts": time.time(), "turn_index": item["turns"][-1]["turn_index"] if item["turns"] else None}
        item["session"]["feedback"] = mark
        item["session"]["feedback_verdict"] = body["verdict"]
        return JSONResponse({"ok": True, "feedback_id": len(posted), "session_id": sid, "verdict": body["verdict"],
                             "effort_changed": False, "reason_code": None, "reason": None})

    async def eval_case(request: Request):
        body = json.loads(await request.body())
        posted.append(("eval", None, body))
        return JSONResponse({"saved": True, "path": "/data/golden_cases.jsonl", "total_cases": 3}, headers=HEADERS)

    async def sandbox(request: Request):
        body = json.loads(await request.body())
        posted.append(("sandbox", None, body))
        await asyncio.sleep(0.4)
        if isinstance(body.get("state"), dict) and body["state"].get("fail"):
            return JSONResponse({"error": "jev call failed: jev error: ConnectError", "jev_ms": 31.0, "reason": "unreachable"},
                                status_code=502, headers=HEADERS)
        return JSONResponse(SANDBOX_OK, headers=HEADERS)

    async def config(_request: Request):
        return JSONResponse(CONFIG, headers=HEADERS)
    async def sessions(request: Request):
        return JSONResponse(listing(details, request.query_params), headers=HEADERS)

    async def overview_route(request: Request):
        return JSONResponse(overview(request.query_params.get("window", "24h"), now), headers=HEADERS)

    async def one(request: Request):
        item = details.get(request.path_params["session_id"])
        if item is None:
            return JSONResponse({"error": "unknown session"}, status_code=404, headers=HEADERS)
        return JSONResponse(item, headers=HEADERS)

    return [
        Route("/router/audit/overview", overview_route, methods=["GET"]),
        Route("/router/audit/sessions", sessions, methods=["GET"]),
        Route("/router/audit/sessions/{session_id}", one, methods=["GET"]),
        Route("/router/sessions/{session_id}/feedback", feedback, methods=["POST"]),
        Route("/router/eval-cases", eval_case, methods=["POST"]),
        Route("/router/sandbox/jev", sandbox, methods=["POST"]),
        Route("/router/config", config, methods=["GET"]),
    ]


def body_text(page):
    return page.evaluate("document.body.innerText")


def test_browser_session_inspector(monkeypatch):
    credential = "browser-test-only"
    monkeypatch.setenv("AUDIT_BROWSER_TOKEN", credential)
    app = create_app(make_config(settings={"admin_token_env": "AUDIT_BROWSER_TOKEN"}))
    details = {A_ID: session_a(), B_ID: session_b(), C_ID: session_c()}
    posted = []
    for route in reversed(fixture_routes(details, posted)):
        app.router.routes.insert(0, route)
    shots = os.environ.get("DASHBOARD_SHOTS")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                errors, console = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda msg: console.append(msg.text) if msg.type == "error" else None)

                def shot(name, width=1920, height=1080):
                    if not shots:
                        return
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(1300)
                    Path(shots).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(shots) / f"{name}-{width}.png"), full_page=True)

                def turn(k):
                    page.locator(".tcard").nth(k).click()
                    page.wait_for_function(f"() => document.querySelectorAll('.tcard')[{k}].classList.contains('sel')")

                def open_book(slug):
                    page.evaluate(f"location.hash = '{slug}'")
                    page.wait_for_function("() => document.querySelector('.tcard')")
                    page.wait_for_timeout(250)

                def heads():
                    return [h.lower() for h in page.locator(".tcolh h2").all_inner_texts()]

                def no_sideways():
                    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                page.goto(f"http://127.0.0.1:{port}/dashboard")
                page.wait_for_function("() => !document.getElementById('error').hidden")
                assert "Admin token required" in page.locator("#error").inner_text()
                page.locator("#token").fill(credential)
                page.get_by_role("button", name="Connect", exact=True).click()
                # The home is the overview.
                page.wait_for_function("() => document.querySelectorAll('.bigk').length === 4")
                assert page.locator("#token").input_value() == ""
                page.wait_for_function("() => document.fonts.ready.then(() => document.fonts.check('16px Geist'))")
                assert page.evaluate("document.fonts.check('16px \"Geist Mono\"')")
                assert page.evaluate("getComputedStyle(document.documentElement).fontSize") == "17px"
                assert page.locator("#v-overview").get_attribute("aria-current") == "page"
                text = body_text(page)
                assert "Legacy alias traffic" in text
                assert page.locator(".rf.warn").count() == 2  # one unknown, one rejected
                # First byte missing on 2 of 10 calls per model is a gap, not a faster number.
                assert "8 of 10" in page.locator(".lt").inner_text()
                assert page.locator(".sb .bad").count() == 24
                shot("overview")
                for window in ("1h", "7d", "30d"):
                    page.locator(".segb", has_text=window).click()
                    page.wait_for_function(f"() => location.hash === '#overview?w={window}' && document.querySelector('.segb.on').textContent === '{window}'")
                    assert page.locator(".bigk").count() == 4
                page.locator(".segb", has_text="24h").click()
                page.wait_for_function("() => location.hash === '#overview'")

                # A problem count opens the sessions behind it.
                page.locator(".rf.warn").first.click()
                page.wait_for_function("() => location.hash.startsWith('#sessions?problem=unknown')")
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 1")
                assert page.locator("#f-problem").input_value() == "unknown"
                assert page.locator(".srow[href] .prob").all_inner_texts() == ["1 unknown", "1 rejected"]
                assert "codex" in page.locator(".srow[href]").inner_text()

                # The sessions view: every session, newest first, then narrowed.
                page.keyboard.press("s")
                page.locator("#clear").click()
                page.wait_for_function("() => location.hash === '#sessions' && document.querySelectorAll('.srow[href]').length === 3")
                assert page.locator("#v-sessions").get_attribute("aria-current") == "page"
                assert page.locator(".srow[href] .pin").count() == 2
                assert page.locator(".srow[href]").first.locator(".plabel").inner_text() == "Design review"
                shot("sessions")
                page.locator("#f-client").select_option("pi")
                page.wait_for_function("() => location.hash === '#sessions?client=pi' && document.querySelectorAll('.srow[href]').length === 2")
                page.locator("#f-state").select_option("closed")
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 1")
                assert "closed" in page.locator(".srow[href]").inner_text()
                page.locator("#clear").click()
                page.locator("#pinned").click()
                page.wait_for_function("() => location.hash === '#sessions?pinned=1' && document.querySelectorAll('.srow[href]').length === 2")
                assert page.locator("#pinned").get_attribute("aria-pressed") == "true"
                page.locator("#clear").click()
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")
                page.keyboard.press("/")
                page.wait_for_function("() => document.activeElement && document.activeElement.id === 'q'")
                page.keyboard.type("pydantic upgrade")
                page.wait_for_function("() => location.hash === '#sessions?q=pydantic+upgrade' && document.querySelectorAll('.srow[href]').length === 1")
                assert page.locator("#q").input_value() == "pydantic upgrade"  # typing is never interrupted
                page.locator("#clear").click()
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")
                page.locator("body").click(position={"x": 5, "y": 400})
                # j and k walk the rows; Enter opens one.
                page.keyboard.press("j")
                page.keyboard.press("j")
                assert page.evaluate("document.activeElement.dataset.sid") == A_ID
                page.keyboard.press("Enter")
                page.wait_for_function("() => document.querySelector('.imodel') && document.querySelector('.imodel').textContent.includes('glm-5.3')")
                assert page.locator(".pager .note").inner_text() == "2 of 3"
                page.keyboard.press("k")
                page.wait_for_function("() => document.querySelector('.imodel') && document.querySelector('.imodel').textContent.includes('gpt-6-astra')")
                page.keyboard.press("Escape")
                page.wait_for_function("() => location.hash === '#sessions'")

                # A filtered list is kept on the way in and on the way back.
                page.evaluate("location.hash = 'sessions?client=pi'")
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 2")
                page.locator(".srow[href]").first.click()
                page.wait_for_function("() => document.querySelector('.imodel') && document.querySelector('.imodel').textContent.includes('glm-5.3')")
                assert page.locator(".pager .note").inner_text() == "1 of 2"
                assert page.locator(".back").get_attribute("href") == "#sessions?client=pi"
                assert page.locator(".fsum").inner_text() == "pi"
                page.locator(".back").click()
                page.wait_for_function("() => location.hash === '#sessions?client=pi' && document.querySelectorAll('.srow[href]').length === 2")
                page.locator("#clear").click()
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")

                # A pinned session's deep link: a Pi session, admission first.
                page.evaluate("location.hash = 'debugging'")
                page.wait_for_function("() => document.querySelector('.imodel') && document.querySelector('.imodel').textContent.includes('glm-5.3')")
                assert page.url.endswith("#debugging")
                assert page.locator(".tcard").count() == 3
                assert page.locator(".tcard.sel .tn").inner_text() == "Turn 1 · Admission"
                assert page.evaluate("getComputedStyle(document.querySelector('.tcard')).animationName") == "rise"
                cards = page.locator(".tcard").all_inner_texts()
                assert "moderate_tools → glm-5.3 · high" in cards[0] and "12 calls" in cards[0]
                assert "↑ too weak" in cards[1] and "15 calls" in cards[1]
                assert "high → xhigh ▲" in cards[2] and "tokens" in cards[2]  # no usage reported: a gap
                assert page.locator(".tcard").nth(2).locator(".gapchip").count() == 1
                assert page.locator(".tjoin").count() == 0
                head = page.locator(".ihead").inner_text()
                assert A_PROMPTS[0] in head and "importer" in head and "3 turns" in head and "37 calls" in head
                assert "missed the second queue" in head
                assert page.locator(".ifacts .vb.v-too_weak").count() == 1
                assert page.locator(".fbb.on").inner_text() == "↑ too weak"
                assert heads() == ["request", "jev", "rule"]
                text = body_text(page)
                assert "Traceback" not in text  # pasted material starts closed
                assert "routing_v3" not in text
                page.locator(".paste summary").click()
                assert "queue.Full" in body_text(page)
                # Hard facts, from the admission.
                facts = page.locator(".hfacts").inner_text()
                assert "33" in facts and "26,453 in" in facts and "961,259" in facts and "none" in facts
                # Tokens in, the reserve held back for the reply, and the headroom add up to the window.
                assert "Reserve\n12,288" in facts
                # Jev: each numeric answer on a ruler, thresholds named by their rule.
                assert page.locator(".tcolh .note").first.inner_text() == "183 ms · 1,651 tok"
                assert page.locator(".qb").count() == 3
                diff = page.locator(".qb").nth(1)
                assert diff.locator(".qval").inner_text() == "2.33"
                # Every rule under its tick, one per line: the decisive one bright and
                # first, a rule this request fails elsewhere (no images) dimmer and last.
                ticks = diff.locator(".rlabels span.rl")
                assert ticks.count() == 3
                assert ticks.nth(0).locator("em").inner_text() == "1.4"
                names = ticks.nth(0).locator("b").all_inner_texts()
                assert names[0] == "moderate_tools" and len(names) == 4
                assert ticks.nth(0).locator("b.on").all_inner_texts() == ["moderate_tools"]
                dim = ticks.nth(0).locator("b.dim").all_inner_texts()
                assert "images_easy" in dim and names[-len(dim):] == dim
                assert diff.locator(".rlabels b.on").all_inner_texts() == ["moderate_tools", "hard", "very_hard"]
                # The end label a tick sits on is not drawn twice.
                assert diff.locator(".rlabels span.end").all_inner_texts() == ["0"]
                assert diff.locator(".rdist i").count() == 4
                assert page.locator(".rmark").count() == 2
                assert page.locator(".cbar i").count() == 2
                for q in range(3):
                    rects = page.locator(".qb").nth(q).evaluate_all(
                        "els => els.length ? [...els[0].querySelectorAll('.rlabels > span')].map(e => e.getBoundingClientRect()).map(r => [r.left, r.right, r.top, r.bottom]) : []")
                    for i, a in enumerate(rects):
                        for b in rects[i + 1:]:
                            assert a[3] <= b[2] or b[3] <= a[2] or a[1] <= b[0] or b[1] <= a[0], rects
                # Rule: the ladder as replayed, then quota and lane.
                assert page.locator(".r.hit").inner_text().split("\n")[-1] == "tools yes · difficulty 2.33 ≥ 1.4"
                assert "difficulty 2.33 < 2.4" in page.locator(".r.miss").nth(4).inner_text()
                assert "images no" in page.locator(".r.miss").nth(1).inner_text()
                assert page.locator(".r.skip").count() == 2
                text = body_text(page)
                assert "mid_tools → ollama/glm-5.3 · high" in text
                assert "kept primary" in text and "openai <0.01" in text and "ollama error" in text
                assert "evals/reports/2026-09-20-glm-effort/summary.md" in text
                # Under the columns: how the turn ran; the calls start closed.
                roll = page.locator(".roll").inner_text()
                assert "12 calls" in roll and "first byte" in roll and "cache" in roll
                assert page.locator(".chip").all_inner_texts() == ["bash × 5", "read_file × 3"]
                assert "drains before it refills" in page.locator(".asum").inner_text()
                assert page.locator(".crow:not(.chead)").first.is_visible() is False
                assert page.locator(".calls summary").inner_text() == "Tool loop · 12 calls"
                page.locator(".calls summary").click()
                assert page.locator(".crow:not(.chead)").count() == 12
                shot("a-admission")

                # A user turn with no plan: Jev was not asked; the effort is the admission's.
                turn(1)
                assert heads() == ["request", "jev", "effort"]
                text = body_text(page)
                assert "Not asked" in text and "Fixed effort · no plan" in text
                assert "moderate_tools" in page.locator(".tcol").nth(2).inner_text()
                assert page.locator(".crow:not(.chead)").count() == 15  # the calls table stays open
                assert page.evaluate("getComputedStyle(document.querySelector('.tcard')).animationName") == "none"
                page.keyboard.press("ArrowRight")
                page.wait_for_function("() => document.querySelectorAll('.tcard')[2].classList.contains('sel')")
                why = page.locator(".tcol").nth(2).inner_text()
                assert "high → xhigh ▲" in page.locator(".effbig").inner_text()
                assert "Stepped by feedback" in why and "↑ too weak" in why and "Fixed effort" not in why
                assert "usage not reported" in page.locator(".roll").inner_text()
                assert page.locator(".roll .gapchip").count() == 1
                shot("a-turn3")
                page.keyboard.press("ArrowLeft")
                page.wait_for_function("() => document.querySelectorAll('.tcard')[1].classList.contains('sel')")
                page.keyboard.press("ArrowRight")

                # The selected turn survives the live refresh; unchanged data does not re-render.
                page.evaluate("document.querySelector('.pane').dataset.mark = 'kept'")
                page.wait_for_timeout(5600)
                assert page.evaluate("document.querySelector('.pane').dataset.mark") == "kept"
                details[A_ID]["session"]["last_seen"] += 10
                page.wait_for_function("() => !document.querySelector('.pane').dataset.mark", timeout=8000)
                assert page.locator(".tcard.sel .tn").inner_text() == "Turn 3"
                assert page.locator(".pane.still").count() == 1 and page.locator(".turns.still").count() == 1

                # Feedback posts the verdict, nothing else, and the page shows it.
                page.locator(".fbb.v-too_strong").click()
                page.wait_for_function("() => document.querySelector('.ifacts .vb.v-too_strong')", timeout=8000)
                assert posted[-1] == ("feedback", A_ID, {"verdict": "too_strong", "source": "dashboard"})
                assert page.locator(".fbb.on").inner_text() == "↓ too strong"
                # Save as eval case: the session and the verdict now on it.
                page.get_by_role("button", name="Save as eval case").click()
                page.wait_for_function("() => document.querySelector('.isaved')")
                assert posted[-1] == ("eval", None, {"session_id": A_ID, "verdict": "too_strong"})
                saved = page.locator(".isaved").inner_text()
                assert "/data/golden_cases.jsonl" in saved and "3 cases" in saved
                shot("a-saved")

                # Jev sandbox: the admission's stored state, the running wording, one probe per Run.
                page.get_by_role("button", name="Jev sandbox").click()
                assert page.locator(".drawer").is_visible()
                assert page.locator(".dtarget").inner_text() == "Turn 1 · Admission"
                assert page.locator(".dnote").inner_text() == "One live Jev call on the text below. Nothing is stored or rerouted."
                assert A_PROMPTS[0] in page.locator(".dstate").input_value()
                assert page.locator(".dq").count() == 3
                assert page.locator(".dq").nth(1).input_value() == "How hard is this request?"
                runs = sum(1 for p in posted if p[0] == "sandbox")
                page.wait_for_timeout(300)
                assert sum(1 for p in posted if p[0] == "sandbox") == runs  # never runs by itself
                page.locator(".drun").click()
                assert page.locator(".drun").is_disabled()
                page.wait_for_function("() => document.querySelector('.dgrid')")
                assert posted[-1] == ("sandbox", None, {"decision_row": 730})
                result = page.locator(".dres").inner_text()
                assert "+0.65" in result and "very_hard → gpt-6-astra · xhigh" in result
                assert page.locator(".dgrid .chg").count() == 2  # difficulty and the route
                assert page.locator(".dms").inner_text() == "Jev 241 ms"
                assert page.locator(".drun").is_enabled()
                shot("a-sandbox")
                # An edited state and wording are sent; a failed probe says so.
                page.locator(".dq").nth(1).fill("Rate it harder.")
                page.locator(".dstate").fill('{"latest_user_request": "x", "fail": true}')
                page.locator(".drun").click()
                page.wait_for_function("() => document.querySelector('.dres .gaprow')")
                assert posted[-1][2] == {"decision_row": 730, "state": {"latest_user_request": "x", "fail": True},
                                         "questions": {"difficulty": {"instructions": "Rate it harder."}}}
                assert page.locator(".dres").inner_text() == "Jev unreachable · check settings.jev_url"
                assert page.locator(".dms").inner_text() == ""  # nothing was measured
                # The criteria can be edited too; only a changed set is sent.
                page.locator('[data-fk="crit-difficulty"]').click()
                levels = json.loads(page.locator(".dc").first.input_value())
                assert levels[0] == {"what": "Level 0.", "examples": ["example 0"]}
                levels[3]["what"] = "Several connected stages."
                page.locator(".dc").first.fill(json.dumps(levels))
                page.locator(".dstate").fill('{"latest_user_request": "x"}')
                page.locator(".drun").click()
                page.wait_for_function("() => document.querySelector('.dgrid')")
                assert posted[-1][2]["questions"] == {"difficulty": {"instructions": "Rate it harder.", "criteria": levels}}
                page.locator(".dc").first.fill("[not json")
                page.locator(".drun").click()
                assert "Difficulty criteria are not JSON" in page.locator(".dres").inner_text()
                page.locator(".dstate").fill("not json")
                page.locator(".drun").click()
                assert "not a JSON object" in page.locator(".dres").inner_text()
                # The drawer and its edits outlive a refresh of the page under it.
                details[A_ID]["session"]["last_seen"] += 10
                page.wait_for_timeout(5600)
                assert page.locator(".dstate").input_value() == "not json"
                page.get_by_role("button", name="Close").click()
                assert page.locator(".drawer").is_hidden()
                # Opened again, it starts from the stored state and the running wording.
                page.get_by_role("button", name="Jev sandbox").click()
                assert page.locator(".dq").nth(1).input_value() == "How hard is this request?"
                assert A_PROMPTS[0] in page.locator(".dstate").input_value()
                page.get_by_role("button", name="Close").click()

                # Bookmark 2: adaptive effort, plan turns, an inferred join and a rejected call.
                open_book("design-review")
                page.wait_for_function("() => document.querySelector('.imodel').textContent.includes('gpt-6-astra')")
                assert page.locator(".tcard").count() == 5
                cards = page.locator(".tcard").all_inner_texts()
                assert "high → xhigh ▲" in cards[1] and "effort held" in cards[2] and "xhigh → high ▼" in cards[3]
                assert page.locator(".tcard.corr").count() == 1 and "corrective" in cards[1]
                assert "1 call" in cards[4] and "1 rejected" in cards[4]
                assert page.locator(".tjoin").inner_text() == "Calls matched to turns by log order"
                assert "1 escalation" in page.locator(".ihead").inner_text()
                assert page.locator(".ifacts .vb").count() == 0
                assert page.get_by_role("button", name="Save as eval case").is_disabled()
                assert page.get_by_role("button", name="Save as eval case").get_attribute("title") == "Needs a verdict first"
                # The admission: no replay, quota steered, shadow answers apart.
                text = body_text(page)
                assert "not replayed · config not on file" in text
                assert "steered" in text and "without quota gpt-6-luna(high)" in text
                assert "reasoning" in page.locator(".shadowq").inner_text()
                assert page.locator(".r.hit .nm").inner_text() == "hard"
                shot("b-admission")
                # The escalating plan turn.
                turn(1)
                assert heads() == ["request", "jev", "effort"]
                assert page.locator(".effbig").inner_text() == "high → xhigh ▲"
                assert "confirmed" in page.locator(".effst").inner_text()
                assert page.locator(".rung").count() == 3
                assert "keep the public API" in page.locator(".tcol").first.inner_text()
                assert "Earlier request" in page.locator(".tcol").first.inner_text()
                corr = page.locator(".qb").nth(2)
                assert corr.locator(".rlabels b.on").all_inner_texts() == ["raise"]
                assert "0.82 ≥ 0.60" in page.locator(".limits").inner_text()
                shot("b-escalation")
                # The step down, sent but not confirmed, with its confirmations.
                turn(3)
                assert page.locator(".effbig").inner_text() == "xhigh → high ▼"
                assert "sent, not confirmed" in page.locator(".effst").inner_text()
                assert "2 of 2" in page.locator(".tcol").nth(2).inner_text()
                # The call no turn claimed sits with the turn it followed.
                turn(4)
                page.locator(".calls summary").click()
                assert page.locator(".crow:not(.chead)").count() == 1
                assert "rejected" in page.locator(".crow:not(.chead)").inner_text()
                shot("b-last")

                # A fallback admission: no rule ran, no lane, and no quota finding.
                details[F_ID] = session_fallback()
                LABELS[F_ID] = LABELS[A_ID]
                page.evaluate(f"location.hash = 's={F_ID}'")
                page.wait_for_function("() => document.querySelector('.r.hit.fb')")
                why = page.locator(".tcol").nth(2).inner_text()
                assert page.locator(".tcol").nth(2).locator(".tcolh .note").inner_text() == "Jev failed · no rule ran"
                assert "Jev failed" not in page.locator(".r.hit.fb").inner_text()
                assert "not recorded" in why and "kept primary" not in why
                assert "no lane · fallback" in why and "frontier_medium\n" not in why.split("Lane")[-1]
                del details[F_ID], LABELS[F_ID]

                # A session with almost nothing recorded never shows blanks.
                page.evaluate(f"location.hash = 's={C_ID}'")
                page.wait_for_function("() => document.querySelector('.imodel') && document.querySelector('.imodel').textContent.includes('grok-5')")
                assert page.locator(".tcard").count() == 1
                assert page.locator(".gapchip, .gaprow").count() >= 4
                shot("c-gaps")

                # Phone width: no sideways scroll on any turn; the columns stack in order.
                page.set_viewport_size({"width": 390, "height": 844})
                for key in ("debugging", "design-review"):
                    open_book(key)
                    for k in range(page.locator(".tcard").count()):
                        turn(k)
                        assert no_sideways(), (key, k)
                        tops = page.evaluate("[...document.querySelectorAll('.tcol')].map(c => Math.round(c.getBoundingClientRect().top))")
                        assert tops == sorted(tops) and len(set(tops)) == 3, tops
                open_book("debugging")
                shot("a-admission", 390, 844)
                turn(2)
                shot("a-turn3", 390, 844)
                page.get_by_role("button", name="Jev sandbox").click()
                assert no_sideways()
                shot("a-sandbox", 390, 844)
                page.keyboard.press("Escape")
                open_book("design-review")
                turn(1)
                shot("b-escalation", 390, 844)
                page.keyboard.press("s")
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")
                assert no_sideways()
                shot("sessions", 390, 844)
                page.keyboard.press("o")
                page.wait_for_function("() => document.querySelectorAll('.bigk').length === 4")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                shot("overview", 390, 844)
                page.set_viewport_size({"width": 1440, "height": 900})
                assert page.evaluate("getComputedStyle(document.documentElement).fontSize") == "16px"
                shot("overview", 1440, 900)

                # Reduced motion turns every animation off.
                page.emulate_media(reduced_motion="reduce")
                open_book("debugging")
                assert page.evaluate("getComputedStyle(document.querySelector('.tcard')).animationName") == "none"

                # Privacy: truncated session ids only, request ids never.
                for item in details.values():
                    page.evaluate(f"location.hash = 's={item['session']['session_id']}'")
                    page.wait_for_timeout(400)
                    page.wait_for_function("() => document.querySelector('.tcard')")
                    for k in range(page.locator(".tcard").count()):
                        page.locator(".tcard").nth(k).click()
                        html = page.content()
                        text = body_text(page)
                        assert item["session"]["session_id"] not in text
                        assert all(r["request_id"] not in html for r in item["requests"])

                page.get_by_role("button", name="Disconnect", exact=True).click()
                assert page.locator("#main").inner_text() == ""
                assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
                assert credential not in page.url
                assert not errors, errors
                assert not [c for c in console if "Content Security Policy" in c or "Refused" in c], console
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_fixtures_match_the_contract_shape():
    """Keep the fixtures honest: the keys the contract names, and no others."""
    session_keys = set(json.loads(json.dumps(session_a()))["session"])
    assert session_keys == {
        "session_id", "alias", "client", "state", "version", "created", "last_seen", "closed_at",
        "model", "provider", "protocol", "context_window", "max_output_tokens", "supports_tools",
        "supports_vision", "base_effort", "effective_effort", "expected_effort", "confirmed_effort",
        "effort_mode", "adaptation_mode", "effort_lineage", "decision_id", "decision_rule",
        "decision_source", "quality_lane", "quota_changed_choice", "config_hash", "jev_model",
        "compaction_epoch", "compaction_state", "last_input_tokens", "last_output_tokens",
        "tombstone", "state_builder", "title", "prompt_preview", "workspace_hint", "turn_count",
        "escalation_count", "corrective_count", "feedback_verdict", "config_matches", "request_count",
        "cache_hit_ratio", "feedback",
    }
    plan_keys = set(session_b()["plans"][0])
    assert plan_keys == {
        "plan_id", "turn_id", "sequence", "request_id", "mode", "action", "status", "from_effort",
        "to_effort", "base_effort", "recommendation", "expected_effort", "confirmed_effort", "reconciled",
        "compaction_epoch", "reason", "created", "updated", "shadow_counterfactual", "evidence", "judgment",
    }
    assert set(session_b()["plans"][0]["evidence"]) == {
        "difficulty", "p_hard", "harm_if_wrong", "corrective_followup", "failure_mode", "short_continuation",
        "missing", "error", "thresholds", "ladder", "floor", "target", "direction", "confirmations",
        "needed_confirmations",
    }
    for item in (session_a(), session_b()):
        assert set(item) == {"config_hash", "session", "admission", "requests", "plans", "trace", "turns", "turn_join"}
        for t in item["turns"]:
            assert set(t) == {
                "turn_index", "kind", "ts", "decision_row", "user_prompt", "state", "state_shape", "jev",
                "decision", "plan", "execution_rollup", "requests", "feedback",
            }
            assert set(t["decision"]) == {"model", "effort", "rule", "route", "quality_lane", "fallback"}
            assert set(t["execution_rollup"]) == {
                "http_requests", "usage_reported", "input_tokens", "output_tokens", "cached_input_tokens",
                "first_byte_ms_p50", "tps_p50", "tool_calls", "assistant_summary", "files_touched",
            }
    request_keys = set(session_a()["requests"][0])
    assert request_keys == {
        "seq", "request_id", "status", "created", "updated", "upstream_status", "row", "ts", "model",
        "effort", "stream_state", "accepted", "first_byte_ms", "response_ms", "response_bytes",
        "usage_reported", "turn_index", "turn_id", "event_type", "input_tokens", "output_tokens",
        "cached_input_tokens", "facts",
    }
    allow = {"message_count", "est_tokens", "input_tokens", "output_tokens", "reserve_tokens", "headroom",
             "context_window", "max_tokens", "has_tools", "tool_count", "has_images", "has_code", "stream",
             "estimate_method", "session_mode"}
    assert set(session_a()["admission"]["facts"]) <= allow
    assert copy.deepcopy(session_b())["trace"] == {"available": False, "reason": "not_on_file"}


# --- the Rules view ------------------------------------------------------------

def rules_yaml():
    """A long router.yaml-like text, so the editor has to scroll to a rule."""
    head = [f"# filler line {i}" for i in range(1, 121)]
    body = """policy:
  low_confidence:
    min_confidence: 0.4
    questions: [task, difficulty]
    use:
      route: frontier_medium
  rules:
    - name: very_hard
      lane: hardest-work
      when:
        difficulty:
          gte: 3.0
      use:
        route: frontier_xhigh
    - name: hard
      lane: hard-work
      when:
        difficulty:
          gte: 2.4
      use:
        route: frontier_high
    - name: high_harm
      lane: high-consequence
      when:
        harm_if_wrong:
          gte: 0.8
      use:
        route: frontier_medium
    - name: moderate_tools
      lane: moderate-tools
      when:
        has_tools: true
        difficulty:
          gte: 1.4
      use:
        route: mid_tools
    - name: easy
      lane: simple-work
      when: {}
      use:
        route: fast
  default:
    use:
      route: frontier_medium
""".splitlines()
    tail = [f"# more filler {i}" for i in range(1, 81)]
    return "\n".join(head + body + tail) + "\n"


def line_of(text, needle):
    return text.splitlines().index(needle) + 1


def rules_config(window, yaml_text, config_hash=RUNNING):
    """`GET /router/config`, shaped from the seeded server's answer."""
    def rule(index, name, lane, when, route, hits):
        return {"index": index, "name": name, "lane": lane, "protected": False, "when": when,
                "use": {"route": route}, "line": line_of(yaml_text, f"    - name: {name}"),
                "hits": hits, "share": hits / 10}

    rules = [
        rule(1, "very_hard", "hardest-work", {"difficulty": {"gte": 3.0}}, "frontier_xhigh", 0),
        rule(2, "hard", "hard-work", {"difficulty": {"gte": 2.4}}, "frontier_high", 3),
        rule(3, "high_harm", "high-consequence", {"harm_if_wrong": {"gte": 0.8}}, "frontier_medium", 1),
        rule(4, "moderate_tools", "moderate-tools", {"has_tools": True, "difficulty": {"gte": 1.4}}, "mid_tools", 4),
        rule(5, "easy", "simple-work", {}, "fast", 1),
    ]
    gate = {"min_confidence": 0.4, "questions": ["task", "difficulty"], "use": {"route": "frontier_medium"},
            "hits": 1, "share": 0.1}
    default = {"use": {"route": "frontier_medium"}, "hits": 0, "share": 0.0}
    policy = {"name": "policy", "aliases": ["auto"], "admissions": 10, "rules": rules,
              "low_confidence": gate, "default": default, "fallback": {"hits": 0, "share": 0.0}}
    rated = [
        {"session_id": A_ID, "alias": "auto", "rule": "moderate_tools", "verdict": "too_weak",
         "scores": {"difficulty": 2.33, "harm_if_wrong": 0.27}},
        {"session_id": B_ID, "alias": "auto", "rule": "hard", "verdict": "too_strong",
         "scores": {"difficulty": 2.5, "harm_if_wrong": 0.2}},
        {"session_id": C_ID, "alias": "auto", "rule": "easy", "verdict": "right",
         "scores": {"difficulty": 0.4, "harm_if_wrong": 0.05}},
        {"session_id": "pi-unrated-0001", "alias": "auto", "rule": "moderate_tools", "verdict": None,
         "scores": {"difficulty": 1.6, "harm_if_wrong": 0.3}},
    ]
    return {
        "config_hash": config_hash, "path": "/srv/router.yaml", "yaml": yaml_text, "yaml_source": "loaded",
        "window": window, "since": 0, "admissions": 10, "rules": rules, "low_confidence": gate,
        "default": default, "fallback": {"hits": 0, "share": 0.0}, "rulesets": {"policy": policy},
        "routes": {}, "quality_lanes": {}, "models": {},
        "quota_policy": {"enabled": True, "demote_above": 0.6, "exhausted_at": 100.0},
        "adaptive_effort": {"mode": "off", "line": None, "ladder": [], "thresholds": {}},
        "questions": {"difficulty": {"type": "score", "levels": 4, "max": 3, "line": 2},
                      "harm_if_wrong": {"type": "noul", "max": 1, "line": 3}},
        "thresholds": {"difficulty": [1.4, 2.4, 3.0], "harm_if_wrong": [0.8]},
        "effort_order": ["none", "low", "medium", "high", "xhigh", "max"],
        "rated": rated,
    }


PREVIEW = {
    "valid": True, "errors": [], "config_hash_running": RUNNING, "config_hash_draft": "d4af7c0ffee01234",
    "evaluated": 10, "changed": 2, "unreplayable": 1, "running_reproduces": 10,
    "tier_share_before": {"gpt-6-astra": 0.4, "ollama/glm-5.3": 0.6},
    "tier_share_after": {"gpt-6-astra": 0.6, "ollama/glm-5.3": 0.4},
    "quota_saved_before": {"saved_rate": 0.42, "quota_cost_sum": 5.8, "priced": 10, "unpriced": 0},
    "quota_saved_after": {"saved_rate": 0.3, "quota_cost_sum": 7.0, "priced": 10, "unpriced": 0},
    "feedback": {"rated": 2, "resolved_too_weak": 1, "resolved_too_strong": 0, "conflicts": 0,
                 "right_changed": 0, "unranked": 0},
    "diffs": [
        {"row": 730, "decision_id": "25d58e7563e0", "session_id": A_ID,
         "title": "Getting this on startup since the pydantic upgrade, any idea?", "ts": A_START,
         "answers": {"difficulty": 2.33, "harm_if_wrong": 0.27},
         "before": {"rule": "moderate_tools", "route": "mid_tools", "model": "ollama/glm-5.3", "effort": "high", "lane": "moderate-tools"},
         "after": {"rule": "hard", "route": "frontier_high", "model": "gpt-6-astra", "effort": "high", "lane": "hard-work"},
         "direction": "up", "feedback_verdict": "too_weak", "feedback_effect": "resolved_too_weak"},
        {"row": 741, "decision_id": "aa11bb22cc33", "session_id": "pi-unrated-0001", "title": None, "ts": A_START,
         "answers": {"difficulty": 2.1, "harm_if_wrong": 0.3},
         "before": {"rule": "moderate_tools", "route": "mid_tools", "model": "ollama/glm-5.3", "effort": "high", "lane": "moderate-tools"},
         "after": {"rule": "hard", "route": "frontier_high", "model": "gpt-6-astra", "effort": "high", "lane": "hard-work"},
         "direction": "unranked", "feedback_verdict": None, "feedback_effect": None},
    ],
    "window": "7d", "since": 0, "limit": 200,
}


def rules_routes(state):
    """`/router/config` and its preview and apply, served from fixtures.

    The preview refuses a draft that holds `bad:` the way the router refuses
    an invalid one: 422 with lines. Apply records what it was sent."""
    async def config(request: Request):
        return JSONResponse(rules_config(request.query_params.get("window", "7d"), state["yaml"], state["hash"]),
                            headers=HEADERS)

    async def preview(request: Request):
        body = await request.json()
        state["previews"].append(body)
        if "bad:" in body["yaml"]:
            line = body["yaml"].splitlines().index("bad: [") + 1
            return JSONResponse({"valid": False, "errors": [{"message": "invalid YAML: unclosed list",
                                                             "path": None, "line": line}]},
                                status_code=422, headers=HEADERS)
        return JSONResponse(PREVIEW, headers=HEADERS)

    async def apply(request: Request):
        body = await request.json()
        state["applied"].append(body)
        return JSONResponse({"applied": True, "config_hash": "d4af7c0ffee01234", "previous_hash": RUNNING,
                             "backup_path": "/srv/config-backups/router.20261027T101500Z.yaml",
                             "restart_required": []}, headers=HEADERS)

    return [
        Route("/router/config", config, methods=["GET"]),
        Route("/router/config/preview", preview, methods=["POST"]),
        Route("/router/config/apply", apply, methods=["POST"]),
    ]


def test_browser_overview_sessions_and_rules(monkeypatch):
    credential = "browser-test-only"
    monkeypatch.setenv("AUDIT_BROWSER_TOKEN", credential)
    app = create_app(make_config(settings={"admin_token_env": "AUDIT_BROWSER_TOKEN"}))
    details = {A_ID: session_a(), B_ID: session_b(), C_ID: session_c()}
    state = {"yaml": rules_yaml(), "hash": RUNNING, "previews": [], "applied": []}
    # The rules view needs the ladder fixture, not the inspector's questions-only config.
    shared = [r for r in fixture_routes(details) if r.path != "/router/config"]
    for route in reversed(shared + rules_routes(state)):
        app.router.routes.insert(0, route)
    shots = os.environ.get("DASHBOARD_SHOTS")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def shot(name, width=1920, height=1080):
                    if not shots:
                        return
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(900)
                    Path(shots).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(shots) / f"{name}-{width}.png"), full_page=True)
                    page.set_viewport_size({"width": 1920, "height": 1080})

                def narrow_fits():
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.wait_for_timeout(300)
                    fits = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    page.set_viewport_size({"width": 1920, "height": 1080})
                    return fits

                page.goto(f"http://127.0.0.1:{port}/dashboard")
                page.locator("#token").fill(credential)
                page.get_by_role("button", name="Connect", exact=True).click()
                page.wait_for_function("() => document.querySelectorAll('.bigk').length === 4")

                # Overview: quota first, routed then tracked, gaps as chips.
                subs = page.locator(".sub")
                assert subs.count() == 3
                assert "calm" in subs.nth(0).inner_text() and "−17" in subs.nth(0).inner_text()
                assert subs.nth(0).locator(".qtrack b").count() == 1  # the pace tick
                assert subs.nth(1).locator(".gapchip").inner_text() == "not polled"
                assert subs.nth(1).locator(".qbar").count() == 0
                tracked = subs.nth(2)
                assert "tracked" in tracked.get_attribute("class")
                assert tracked.locator(".ql").all_inner_texts() == ["Session", "Weekly"]
                assert "$812" in tracked.inner_text()
                labels = page.locator(".bigk .kl").all_inner_texts()
                assert labels == ["Quota saved", "Router tax", "Clean turns", "Cache hit"]
                assert page.locator(".mxv .mxn").count() >= 1
                assert narrow_fits()
                shot("rules-overview")

                # Sessions: titles, the preview beyond them, the feedback filter.
                page.evaluate("location.hash = 'sessions'")
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")
                row_a = page.locator(f".srow[data-sid='{A_ID}']")
                assert row_a.locator(".stt").inner_text().startswith("Getting this on startup")
                assert row_a.locator(".sprev").inner_text() == "Traceback (most recent call last):"
                assert row_a.locator(".vbadge").inner_text() == "↑ more"
                assert row_a.locator(".wchip").inner_text() == "inventory-service"
                # A title that is the whole prompt shows no second line.
                assert page.locator(f".srow[data-sid='{B_ID}'] .sprev").count() == 0
                page.locator("#f-feedback").select_option("too_weak")
                page.wait_for_function("() => location.hash === '#sessions?feedback=too_weak' && document.querySelectorAll('.srow[href]').length === 1")
                assert narrow_fits()
                page.locator("#clear").click()
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")

                # Rules: the ladder between the gate and the default.
                page.keyboard.press("r")
                page.wait_for_function("() => document.querySelectorAll('.lad .lr').length === 7")
                assert page.locator("#v-rules").get_attribute("aria-current") == "page"
                names = page.locator(".lad .lnm").all_inner_texts()
                assert names == ["confidence gate", "very_hard", "hard", "high_harm", "moderate_tools", "easy", "default"]
                assert "difficulty ≥ 2.4" in page.locator(".lad .lr").nth(2).inner_text()
                assert page.locator(".rfield .rdot.v-too_weak").count() == 2  # one per ruler
                assert page.locator(".rfield .rdot.v-none").count() == 2
                assert page.locator(".rleg").inner_text().split() == ["✓", "right", "↑", "more", "↓", "less"]
                shot("rules-ladder")

                # A ladder row scrolls the editor to its line and marks it.
                hard = line_of(state["yaml"], "    - name: hard")
                assert page.evaluate("document.querySelector('.yta').scrollTop") == 0
                page.locator(".lad .lr").nth(2).click()
                page.wait_for_timeout(300)
                top = page.evaluate("document.querySelector('.yta').scrollTop")
                assert top > 1000
                band = page.evaluate("(() => { const b = document.querySelector('.hlb'); const t = document.querySelector('.yta');"
                                     " return {hidden: b.hidden, top: b.getBoundingClientRect().top - t.getBoundingClientRect().top,"
                                     " height: t.clientHeight}; })()")
                assert not band["hidden"] and 0 < band["top"] < band["height"]
                assert abs(band["top"] - (10 + (hard - 1) * 20 - top)) < 1
                assert page.evaluate("document.querySelector('.gut').scrollTop") == top
                assert page.locator(".lad .lr.on .lnm").inner_text() == "hard"

                # A draft survives the live refresh; a moved server hash is said quietly.
                draft = state["yaml"].replace("          gte: 2.4", "          gte: 2.0")
                page.evaluate("(text) => { const t = document.querySelector('.yta'); t.value = text; t.dispatchEvent(new Event('input')); }", draft)
                assert "edited" in page.locator(".edp .ph").inner_text()
                state["hash"] = "0123456789abcdef"
                page.wait_for_function("() => document.querySelector('.edmoved') && !document.querySelector('.edmoved').hidden", timeout=8000)
                assert page.locator(".yta").input_value() == draft
                assert "config changed on the server" in page.locator(".edmoved").inner_text()

                # Check: an invalid draft lists its line; the line button goes there.
                bad = draft + "bad: [\n"
                page.evaluate("(text) => { const t = document.querySelector('.yta'); t.value = text; t.dispatchEvent(new Event('input')); }", bad)
                page.get_by_role("button", name="Check", exact=True).click()
                page.wait_for_function("() => document.querySelector('.err')")
                assert state["previews"][-1]["limit"] == 1
                bad_line = bad.splitlines().index("bad: [") + 1
                assert page.locator(".err .eline").inner_text() == f"line {bad_line}"
                page.locator(".err .eline").click()
                assert page.evaluate("document.querySelector('.yta').scrollTop") > top

                # Preview: k of n, the share before and after, the rated rows.
                page.evaluate("(text) => { const t = document.querySelector('.yta'); t.value = text; t.dispatchEvent(new Event('input')); }", draft)
                page.get_by_role("button", name="Preview", exact=True).click()
                page.wait_for_function("() => document.querySelector('.prv')")
                assert state["previews"][-1] == {"yaml": draft, "window": "7d"}
                assert page.locator(".pvk").inner_text() == "2 of 10"
                assert "1 not replayable" in page.locator(".pvh").inner_text()
                assert page.locator(".shrow.moved").count() == 3  # two models and quota saved
                assert "1 resolved ↑ more" in page.locator(".fbm").inner_text()
                rows = page.locator(".dfr")
                assert rows.count() == 2
                assert "resolved" in rows.nth(0).locator(".dff").inner_text()
                assert rows.nth(0).locator(".arr.up").count() == 1
                assert rows.nth(0).locator("a.dft").get_attribute("href") == "#s=" + A_ID
                assert narrow_fits()
                shot("rules-preview")
                shot("rules-preview", 390, 844)

                # Apply asks once, in the page, then says where the old file went.
                page.get_by_role("button", name="Apply", exact=True).click()
                page.wait_for_function("() => document.querySelector('.confirm')")
                assert "Replace router.yaml and reload?" in page.locator(".confirm").inner_text()
                assert state["applied"] == []
                shot("rules-confirm")
                page.locator(".confirm").get_by_role("button", name="Apply", exact=True).click()
                page.wait_for_function("() => document.querySelector('.applied')")
                assert state["applied"] == [{"yaml": draft, "base_hash": RUNNING}]
                text = page.locator(".applied").inner_text()
                assert "Applied" in text and "d4af7c0f" in text
                assert "/srv/config-backups/router.20261027T101500Z.yaml" in text
                assert page.locator(".prv").count() == 0
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
