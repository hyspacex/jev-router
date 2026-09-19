"""Shadow turn answers are measured and never applied (spec 7.5, C25).

`failure_mode` is the one that matters here: it is a shadow question, and a
shadow question may not decide. It may not decide by refusing either, which is
what suppressing an upgrade would be.
"""

from __future__ import annotations

import json

import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Service, upstream
from test_app import JEV_URL
from test_turn_plan import adaptive, turn_jev

from jev_router.config import AdaptiveEffortCfg

OBSERVATIONS = [
    {
        "source": "harness",
        "kind": "test_result",
        "status": "failed",
        "summary": "escaped-quote regression fails",
    }
]


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path, config=H.adaptive_config(tmp_path))
    yield svc
    await svc.close()


@pytest.fixture
async def active_failure_mode(tmp_path, monkeypatch):
    """The same deployment, with `failure_mode` promoted to a turn question."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    config = H.adaptive_config(
        tmp_path,
        experiments={
            "adaptive_effort": {
                "questions": [
                    "difficulty",
                    "harm_if_wrong",
                    "corrective_followup",
                    "failure_mode",
                ],
                "shadow_questions": [],
            }
        },
    )
    svc = Service(tmp_path, config=config)
    yield svc
    await svc.close()


def test_c25_failure_mode_is_a_shadow_turn_question_by_default():
    cfg = AdaptiveEffortCfg()
    assert "failure_mode" in cfg.shadow_questions
    assert "failure_mode" not in cfg.questions


@respx.mock
@pytest.mark.parametrize(
    "failure_mode", ["reasoning_problem", "environment_problem", "missing_information"]
)
async def test_c25_a_shadow_failure_mode_never_changes_the_plan(service, failure_mode):
    """Same active answers, different shadow answer, same executed decision."""
    turn_jev(difficulty=1.0, harm=0.1, corrective=0.9, failure_mode=failure_mode)
    upstream()
    caller = await adaptive(service)

    body = (await caller.turn_plan("turn-0002", observations=OBSERVATIONS)).json()
    assert body["action"] == "change_effort"
    assert body["next_effective_effort"] == "medium"
    assert "follow-up names a defect" in body["reason"]


@respx.mock
async def test_c25_the_shadow_answer_is_recorded_as_a_counterfactual(service):
    turn_jev(difficulty=1.0, harm=0.1, corrective=0.9, failure_mode="environment_problem")
    upstream()
    caller = await adaptive(service)
    await caller.turn_plan("turn-0002", observations=OBSERVATIONS)

    facts = service.sessions.plans(caller.session_id)[-1]["facts"]
    counterfactual = facts["shadow_counterfactual"]
    assert counterfactual["question"] == "failure_mode"
    assert counterfactual["answer"] == "environment_problem"
    # What the active policy did was raise. What it would have done with this
    # answer in hand was hold, and that difference is the measurement.
    assert counterfactual["action"] == "keep"
    assert counterfactual["changed"] is True
    assert facts["evidence"]["failure_mode"] is None


@respx.mock
async def test_c25_a_shadow_answer_that_changes_nothing_says_so(service):
    turn_jev(difficulty=1.0, harm=0.1, corrective=0.9, failure_mode="reasoning_problem")
    upstream()
    caller = await adaptive(service)
    await caller.turn_plan("turn-0002", observations=OBSERVATIONS)

    counterfactual = service.sessions.plans(caller.session_id)[-1]["facts"][
        "shadow_counterfactual"
    ]
    assert counterfactual["action"] == "change_effort"
    assert counterfactual["changed"] is False


@respx.mock
async def test_c25_config_may_promote_failure_mode_to_an_active_turn_question(
    active_failure_mode,
):
    """Only an explicit promotion lets it decide, and then it does."""
    jev = turn_jev(
        difficulty=1.0, harm=0.1, corrective=0.9, failure_mode="environment_problem"
    )
    upstream()
    caller = await adaptive(active_failure_mode)

    body = (await caller.turn_plan("turn-0002", observations=OBSERVATIONS)).json()
    asked = json.loads(jev.calls[-1].request.content)["questions"]
    assert "failure_mode" in asked
    assert body["action"] == "keep"

    plan = active_failure_mode.sessions.plans(caller.session_id)[-1]
    assert plan["facts"]["evidence"]["failure_mode"] == "environment_problem"
    # It is not a shadow answer any more, so there is nothing to counterfact.
    assert plan["facts"]["shadow_counterfactual"] is None


@respx.mock
async def test_c25_a_promoted_failure_mode_is_still_only_asked_when_grounded(
    active_failure_mode,
):
    jev = turn_jev(
        difficulty=1.0, harm=0.1, corrective=0.9, failure_mode="environment_problem"
    )
    upstream()
    caller = await adaptive(active_failure_mode)

    body = (await caller.turn_plan("turn-0002")).json()
    asked = json.loads(jev.calls[-1].request.content)["questions"]
    assert "failure_mode" not in asked
    # Nothing to suppress the raise with, so the active answers carry it.
    assert body["action"] == "change_effort"


@respx.mock
async def test_c25_a_shadow_answer_is_recorded_on_the_decision_row(service):
    turn_jev(difficulty=1.0, harm=0.1, corrective=0.9, failure_mode="environment_problem")
    upstream()
    caller = await adaptive(service)
    await caller.turn_plan("turn-0002", observations=OBSERVATIONS)

    row = service.store.recent_decisions(5)[0]
    assert row["shadow_answers"]["failure_mode"]["value"] == "environment_problem"
    assert "failure_mode" not in (row["answers"] or {})


@respx.mock
async def test_c25_the_counterfactual_stores_no_message_text(service):
    turn_jev(difficulty=1.0, harm=0.1, corrective=0.9, failure_mode="environment_problem")
    upstream()
    caller = await adaptive(service)
    await caller.turn_plan(
        "turn-0002",
        request="the secret passphrase is hunter2",
        observations=OBSERVATIONS,
    )
    blob = json.dumps(service.sessions.plans(caller.session_id), default=str)
    assert "hunter2" not in blob
