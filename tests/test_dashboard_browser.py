"""Optional real-browser test of the session inspector, with no providers.

Run with: uv run --with playwright pytest tests/test_dashboard_browser.py -q
Needs local Chrome (or Playwright's installed Chromium).

The two session endpoints are served here from fixture JSON shaped like the
contract (section D), mounted on the real app so the real ingress credential,
headers and asset route are what the browser talks to. The fixtures are test
data only. Set DASHBOARD_SHOTS=<dir> to also write screenshots.
"""

from __future__ import annotations

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
        "reserve_tokens": reserve, "headroom": headroom, "context_window": window,
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
    return {
        "config_hash": RUNNING, "session": session, "admission": admission,
        "requests": _requests("a", A_START, A_ROWS, window, "glm-5.3", "high", 730),
        "plans": [], "trace": _trace_a(),
    }


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
        "response_ms": None, "response_bytes": None, "usage_reported": None, "facts": {},
    })

    def plan(seq, request, action, status, from_, to, rec, expected, confirmed, created):
        return {
            "plan_id": f"plan-{seq}", "turn_id": f"turn-{seq}", "sequence": seq, "request_id": request,
            "mode": "active", "action": action, "status": status, "from_effort": from_, "to_effort": to,
            "base_effort": "high", "recommendation": rec, "expected_effort": expected,
            "confirmed_effort": confirmed, "reconciled": None, "compaction_epoch": 0,
            "reason": "upward_jump" if to != from_ else "steady", "created": created,
            "updated": created + 5, "shadow_counterfactual": None,
        }

    plans = [
        plan(1, "b-request-003", "change_effort", "confirmed", "high", "xhigh", "xhigh", "xhigh", "xhigh", B_START + 6.0),
        plan(2, "b-request-006", "keep", "confirmed", "xhigh", "xhigh", "xhigh", "xhigh", "xhigh", B_START + 19.5),
        plan(3, "b-request-009", "change_effort", "accepted", "xhigh", "high", "high", "high", None, B_START + 203.0),
        plan(4, None, "keep", "planned", "high", "high", "medium", None, None, B_START + 220.0),
    ]
    plans[3]["shadow_counterfactual"] = {"failure_mode": "reasoning", "would": "keep"}
    return {
        "config_hash": RUNNING, "session": session, "admission": admission, "requests": reqs,
        "plans": plans, "trace": {"available": False, "reason": "not_on_file"},
    }


def session_c():
    """A session where almost nothing was recorded: every gap must show as one."""
    session = {key: None for key in session_a()["session"]}
    session.update(session_id=C_ID, client="pi", state="closed", created=A_START - 86400, model="xai/grok-5")
    return {
        "config_hash": RUNNING, "session": session, "admission": None,
        "requests": [
            {"seq": 1, "request_id": "c-request-001", "status": "completed", "created": None, "updated": None,
             "upstream_status": None, "row": None, "ts": None, "model": None, "effort": None,
             "stream_state": None, "accepted": None, "first_byte_ms": None, "response_ms": None,
             "response_bytes": None, "usage_reported": None, "facts": None},
            {"seq": 2, "request_id": "c-request-002", "status": "rejected", "created": None, "updated": None,
             "upstream_status": None, "row": None, "ts": None, "model": None, "effort": None,
             "stream_state": None, "accepted": None, "first_byte_ms": None, "response_ms": None,
             "response_bytes": None, "usage_reported": None, "facts": {}},
        ],
        "plans": None, "trace": {"available": False, "reason": "no_admission_row"},
    }


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


def fixture_routes(details):
    now = time.time()  # one clock, so an unchanged overview stays unchanged
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
    ]


def body_text(page):
    return page.evaluate("document.body.innerText")


def test_browser_session_inspector(monkeypatch):
    credential = "browser-test-only"
    monkeypatch.setenv("AUDIT_BROWSER_TOKEN", credential)
    app = create_app(make_config(settings={"admin_token_env": "AUDIT_BROWSER_TOKEN"}))
    details = {A_ID: session_a(), B_ID: session_b(), C_ID: session_c()}
    for route in reversed(fixture_routes(details)):
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

                def stage(name):
                    page.locator(".card").filter(has_text=name).first.click()

                def open_book(slug):
                    page.evaluate(f"location.hash = '{slug}'")
                    page.wait_for_function("() => document.querySelector('.card')")
                    page.wait_for_timeout(250)

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
                page.wait_for_function("() => document.querySelector('.model') && document.querySelector('.model').textContent.includes('glm-5.3')")
                assert page.locator(".pager .note").inner_text() == "2 of 3"
                page.keyboard.press("k")
                page.wait_for_function("() => document.querySelector('.model') && document.querySelector('.model').textContent.includes('gpt-6-astra')")
                page.keyboard.press("Escape")
                page.wait_for_function("() => location.hash === '#sessions'")

                # A filtered list is kept on the way in and on the way back.
                page.evaluate("location.hash = 'sessions?client=pi'")
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 2")
                page.locator(".srow[href]").first.click()
                page.wait_for_function("() => document.querySelector('.model') && document.querySelector('.model').textContent.includes('glm-5.3')")
                assert page.locator(".pager .note").inner_text() == "1 of 2"
                assert page.locator(".back").get_attribute("href") == "#sessions?client=pi"
                assert page.locator(".fsum").inner_text() == "pi"
                page.locator(".back").click()
                page.wait_for_function("() => location.hash === '#sessions?client=pi' && document.querySelectorAll('.srow[href]').length === 2")
                page.locator("#clear").click()
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")

                # A pinned session's deep link: the admission-only session.
                page.evaluate("location.hash = 'debugging'")
                page.wait_for_function("() => document.querySelector('.model') && document.querySelector('.model').textContent.includes('glm-5.3')")
                assert page.url.endswith("#debugging")
                assert page.locator(".card").count() == 5
                assert page.locator(".tick").count() == 37
                assert page.locator(".mkr").count() == 0
                assert page.evaluate("getComputedStyle(document.querySelector('.card')).animationName") == "rise"
                text = body_text(page)
                assert "Shown to Jev" in text and "text not stored" in text and "names withheld" in text
                assert "Debugging" in text and "jev-1.13.0" in text and "none asked" in text
                assert "routing_v3" not in text
                assert page.locator(".answers .row").count() == 2  # 0% labels hidden
                assert "session 32a7…" in text
                shot("a-jev")
                page.keyboard.press("ArrowRight")
                assert "1,000,000 token window" in body_text(page)
                # Constraints headline eligibility; the lane belongs to the rule.
                assert page.locator(".card").nth(1).locator(".cv").inner_text() == "1 excluded"
                assert "Quality lane" not in body_text(page)
                shot("a-constraints")
                page.keyboard.press("ArrowRight")
                text = body_text(page)
                assert "Replayed under 908c9c86" in text and "reproduces moderate_tools" in text
                assert page.locator(".r.hit").inner_text().split("\n")[-1] == "tools yes · difficulty 2.33 ≥ 1.4"
                assert "difficulty 2.33 < 2.4" in page.locator(".r.miss").nth(4).inner_text()
                assert "images no" in page.locator(".r.miss").nth(1).inner_text()
                assert page.locator(".r.skip").count() == 2
                assert "unreadable · counted as 0" in text
                assert "0.07 below hard" in text
                assert "No threshold moved" in text and "· chosen" not in text
                assert "quota no effect" in page.locator(".card").nth(2).inner_text()
                assert "evals/reports/2026-09-20-glm-effort/summary.md" in text
                # Only thresholds that decided a rule, named after it.
                assert page.locator(".ticksx span").all_inner_texts() == ["1.4 moderate_tools", "2.4 hard", "3 very_hard"]
                tops = page.evaluate("[...document.querySelectorAll('.ticksx span')].map(e => e.getBoundingClientRect())"
                                     ".map(r => [r.left, r.right, r.top])")
                for a, b in zip(tops, tops[1:]):
                    assert a[1] <= b[0] or a[2] != b[2], tops  # no two labels collide on one row
                shot("a-rule")
                page.keyboard.press("ArrowRight")
                assert "adaptive effort off" in body_text(page)
                shot("a-binding")
                page.keyboard.press("ArrowRight")
                assert "usage not reported · 37 of 37" in body_text(page)
                shot("a-outcome")

                # A reused request: Jev and Rule are the admission's.
                page.keyboard.press("]")
                page.wait_for_function("() => document.querySelectorAll('.card.inh').length === 2")
                assert page.locator(".tick.on").count() == 1
                assert "Request 1 of 37" in body_text(page)
                assert page.evaluate("getComputedStyle(document.querySelector('.card')).animationName") == "none"
                page.keyboard.press("]")
                page.keyboard.press("ArrowLeft")
                page.keyboard.press("ArrowLeft")
                assert "No rule ran on this request" in body_text(page)
                assert "A strict binding skips the decider" in body_text(page)
                assert page.locator(".inhv").all_inner_texts() == ["Rule\nmoderate_tools", "Route\nmid_tools"]
                shot("a-request2-rule")
                page.keyboard.press("ArrowLeft")
                assert "Within budget" in body_text(page)
                page.keyboard.press("ArrowLeft")
                assert "Judged once, at admission." in body_text(page)
                assert page.locator(".inhv").all_inner_texts() == ["Task\nDebugging", "Difficulty\n2.33"]
                page.keyboard.press("ArrowRight")
                shot("a-request2-constraints")
                page.keyboard.press("Escape")
                assert page.locator(".card.inh").count() == 0

                # Bookmark 2: adaptive effort, gaps, unknown and rejected requests.
                open_book("design-review")
                page.wait_for_function("() => document.querySelector('.model') && document.querySelector('.model').textContent.includes('gpt-6-astra')")
                assert page.locator(".mkr").count() == 4
                assert page.locator(".mkr.g-confirmed").count() == 2
                assert page.locator(".mkr.g-expected").count() == 1
                assert page.locator(".mkr.g-recommended").count() == 1
                assert page.locator(".tick.unknown").count() == 1
                assert page.locator(".tick.rejected").count() == 1
                lane = page.evaluate("[...document.querySelectorAll('.slot')].map(s => Math.round(s.getBoundingClientRect().left))")
                ticks = page.evaluate("[...document.querySelectorAll('.tick')].map(s => Math.round(s.getBoundingClientRect().left))")
                assert lane == ticks
                stage("Jev")
                text = body_text(page)
                assert "not recorded for this decision" in text
                assert "Shadow" in text and "reasoning" in text
                assert "distribution not recorded" in text
                shot("b-jev")
                stage("Rule & quota")
                text = body_text(page)
                assert "config not on file" in text and "hard" in text and "frontier_high" in text
                assert "stale · unreadable · counted as 0" in text
                assert "without quota gpt-6-luna(high) · chosen gpt-6-astra(high)" in text
                assert "No threshold moved" not in text
                shot("b-rule")
                stage("Constraints")
                assert page.locator(".bl .gapchip").count() == 1  # reserve not recorded
                excluded = page.locator(".ck").filter(has_text="Excluded")
                assert excluded.locator(".v").inner_text() == "none"
                shot("b-constraints")
                stage("Binding")
                assert page.locator(".plan").count() == 4
                assert page.locator(".rung.base").inner_text() == "high"
                shot("b-binding")
                stage("Outcome")
                page.locator(".cb.unknown").click()
                assert page.locator(".tick.on.unknown").count() == 1
                shot("b-unknown-outcome")
                page.keyboard.press("ArrowLeft")
                # Every request belongs to the turn its plan opened.
                assert page.locator(".plan.on").count() == 1
                page.locator(".tick").nth(8).click()
                assert page.locator(".plan.on").count() == 1
                assert page.locator(".rung > i.g-expected").count() == 1
                shot("b-request9-binding")
                page.locator(".tick.rejected").click()
                page.keyboard.press("ArrowRight")
                assert "not recorded" in body_text(page)
                page.keyboard.press("Escape")

                # A session with almost nothing recorded never shows blanks.
                page.evaluate(f"location.hash = 's={C_ID}'")
                page.wait_for_function("() => document.querySelector('.model') && document.querySelector('.model').textContent.includes('grok-5')")
                for name in ("Jev", "Constraints", "Rule & quota", "Binding", "Outcome"):
                    stage(name)
                    assert page.locator(".gapchip, .gaprow").count() >= 1, name
                shot("c-outcome")

                # Unchanged data does not replay the entrance; changed data re-renders still.
                open_book("design-review")
                page.wait_for_function("() => document.querySelector('.model').textContent.includes('gpt-6-astra')")
                page.evaluate("document.querySelector('.pane').dataset.mark = 'kept'")
                page.wait_for_timeout(5600)
                assert page.evaluate("document.querySelector('.pane').dataset.mark") == "kept"
                details[B_ID]["session"]["last_seen"] += 10
                page.wait_for_function("() => !document.querySelector('.pane').dataset.mark", timeout=8000)
                assert page.locator(".pane.still").count() == 1
                assert page.locator(".spine.still").count() == 1

                # Pause holds the view; resume picks up the change.
                page.get_by_role("button", name="Pause", exact=True).click()
                details[B_ID]["requests"][0]["response_ms"] = 3999.0
                page.evaluate("document.querySelector('.pane').dataset.mark = 'paused'")
                page.wait_for_timeout(5600)
                assert page.evaluate("document.querySelector('.pane').dataset.mark") == "paused"
                page.get_by_role("button", name="Resume", exact=True).click()
                page.wait_for_function("() => !document.querySelector('.pane').dataset.mark", timeout=8000)

                # NULL exclusions on a row the packet code never annotated are a gap.
                details[B_ID]["admission"]["packet_version"] = None
                stage("Constraints")
                page.wait_for_function(
                    "() => [...document.querySelectorAll('.ck')].some(e => e.textContent === 'Excludednot recorded')",
                    timeout=8000)
                # With no state shape, the facts the replayed rules read are shown instead.
                details[A_ID]["admission"]["state_shape"] = None
                open_book("debugging")
                page.wait_for_function("() => document.querySelector('.model').textContent.includes('glm-5.3')")
                stage("Jev")
                page.wait_for_function("() => document.body.innerText.includes('Read by the rules')", timeout=8000)
                text = body_text(page)
                assert "not recorded for this decision" in text and "has_tools" in text and "has_images" in text

                # Phone width: no horizontal scroll on any stage of any session.
                page.set_viewport_size({"width": 390, "height": 844})
                for key in ("debugging", "design-review"):
                    open_book(key)
                    page.wait_for_timeout(300)
                    for name in ("Jev", "Constraints", "Rule & quota", "Binding", "Outcome"):
                        stage(name)
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (key, name)
                    page.keyboard.press("]")
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), key
                    page.keyboard.press("Escape")
                open_book("debugging")
                stage("Rule & quota")
                shot("a-rule", 390, 844)
                stage("Jev")
                shot("a-jev", 390, 844)
                open_book("design-review")
                stage("Binding")
                shot("b-binding", 390, 844)
                stage("Outcome")
                shot("b-outcome", 390, 844)
                page.keyboard.press("s")
                page.wait_for_function("() => document.querySelectorAll('.srow[href]').length === 3")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
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
                page.wait_for_function("() => document.querySelector('.card')")
                assert page.evaluate("getComputedStyle(document.querySelector('.card')).animationName") == "none"

                # Privacy: truncated session ids only, request ids never.
                for item in details.values():
                    page.evaluate(f"location.hash = 's={item['session']['session_id']}'")
                    page.wait_for_timeout(400)
                    for k in range(1, 6):
                        page.locator(".card").nth(k - 1).click()
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
    }
    plan_keys = set(session_b()["plans"][0])
    assert plan_keys == {
        "plan_id", "turn_id", "sequence", "request_id", "mode", "action", "status", "from_effort",
        "to_effort", "base_effort", "recommendation", "expected_effort", "confirmed_effort", "reconciled",
        "compaction_epoch", "reason", "created", "updated", "shadow_counterfactual",
    }
    request_keys = set(session_a()["requests"][0])
    assert request_keys == {
        "seq", "request_id", "status", "created", "updated", "upstream_status", "row", "ts", "model",
        "effort", "stream_state", "accepted", "first_byte_ms", "response_ms", "response_bytes",
        "usage_reported", "facts",
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
    for route in reversed(fixture_routes(details) + rules_routes(state)):
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
