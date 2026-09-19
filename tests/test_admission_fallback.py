"""What a classifier outage binds, and what it is recorded as (spec 8.4).

When Jev is unreachable the alias's own `admission_fallback` binds. The
heuristic decider still ran, so that its answers could be discarded; nothing
it guessed is what happened, and nothing it guessed is recorded as though it
were.
"""

from __future__ import annotations

import httpx
import pytest
import respx
import session_harness as H
from session_harness import ADMIN, Client, Service
from test_app import JEV_URL

from jev_router.cli import main
from jev_router.semantic import replay_decision

# Two named routes so "which route bound" is a visible fact rather than an
# empty string. `quick` is where the heuristic decider lands a short, plain
# request; `careful` is what the alias falls back to when Jev is down.
FALLBACK_CONFIG = {
    "routes": {
        "quick": {"primary": {"model": "small", "effort": "none"}},
        "careful": {"primary": {"model": "big", "effort": "medium"}},
    },
    "quality_lanes": {
        "quick-work": {
            "qualification_ref": "test fixture",
            "qualified": [
                {
                    "model": "small",
                    "effort": "none",
                    "qualification_ref": "test fixture",
                }
            ],
        }
    },
    "policy": {
        "rules": [
            {
                "name": "easy",
                "when": {"difficulty": {"lte": 2.0}},
                "use": {"route": "quick"},
                "lane": "quick-work",
            }
        ],
        "default": {"route": "quick"},
    },
    "aliases": {
        "auto-session": {
            "description": "Strict session binding.",
            "session_mode": "strict",
            "state_builder": "summary_v1",
            "questions": ["task", "difficulty", "harm_if_wrong"],
            "rules": "policy",
            "allowed_models": ["small", "big"],
            "admission_fallback": {"route": "careful"},
        }
    },
}


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", ADMIN)
    svc = Service(tmp_path, config=H.session_config(**FALLBACK_CONFIG))
    yield svc
    await svc.close()


async def outage(service) -> Client:
    respx.post(JEV_URL).mock(return_value=httpx.Response(503))
    caller = Client(service)
    response = await caller.resolve()
    assert response.status_code == 200, response.text
    return caller


@respx.mock
async def test_the_heuristic_guess_is_not_what_binds(service):
    """The guess and the fallback really do differ, or this file proves nothing."""
    respx.post(JEV_URL).mock(return_value=httpx.Response(503))
    caller = Client(service)
    body = (await caller.resolve()).json()
    assert body["execution"]["model_key"] == "big"
    assert body["decision"]["source"] == "admission_fallback"
    assert body["decision"]["rule"] == "admission_fallback"

    # The heuristic decider really does land somewhere else, or the rest of
    # this file would prove nothing.
    from jev_router.deciders.rules import RulesDecider
    from jev_router.features import extract_features

    guess = await RulesDecider(service.config).decide(
        extract_features(H.user_request(), {}),
        service.config.aliases["auto-session"],
    )
    assert (guess.model, guess.route) == ("small", "quick")


@respx.mock
async def test_the_bound_route_is_the_lane_that_is_reported(service):
    caller = await outage(service)
    contract = (await caller.resume()).json()["decision"]
    assert contract["quality_lane"] == "careful"
    assert contract["quota_changed_choice"] is False

    row = service.sessions.get(caller.session_id)
    assert row["quality_lane"] == "careful"
    assert row["quota_changed_choice"] == 0
    assert row["decision_source"] == "admission_fallback"


@respx.mock
async def test_the_decision_row_carries_the_fallback_and_no_counterfactual(service):
    await outage(service)
    row = service.store.recent_decisions(1)[0]

    assert row["rule"] == "admission_fallback"
    assert row["fallback"] == 1
    # Absent, not a synthetic Jev measurement.
    assert row["answers"] == {}
    assert row["route"] == "careful"
    assert row["quality_lane"] == "careful"
    assert row["model"] == "big" and row["intended_model"] == "big"
    # Nothing was compared at two pressures, so there is nothing to claim.
    assert row["counterfactual"] is None
    assert row["evidence"] == "none"


@respx.mock
async def test_replay_reruns_the_fallback_rather_than_the_ruleset(service):
    caller = await outage(service)
    row = service.store.get_decision(
        service.sessions.get(caller.session_id)["decision_id"]
    )
    result = replay_decision(service.config, row)

    assert result.stored == ("big", "medium")
    assert result.replayed == ("big", "medium")
    assert result.replayed_rule == "admission_fallback"
    assert result.reproduced is True
    assert any("admission_fallback" in note for note in result.notes)
    assert "reproduced" in result.verdict()


@respx.mock
async def test_replay_says_so_when_the_fallback_has_been_taken_away(service):
    caller = await outage(service)
    row = service.store.get_decision(
        service.sessions.get(caller.session_id)["decision_id"]
    )
    service.config.aliases["auto-session"].admission_fallback = None

    result = replay_decision(service.config, row)
    assert result.replayed is None
    assert "no admission_fallback" in result.verdict()


@respx.mock
async def test_the_cli_replays_a_fallback_row(service, tmp_path, capsys):
    import yaml

    from conftest import _merge

    caller = await outage(service)
    decision_id = service.sessions.get(caller.session_id)["decision_id"]
    # The same config on disk, pointed at the same database, for the CLI.
    raw = _merge(H.session_raw(), FALLBACK_CONFIG)
    raw["settings"]["sqlite_path"] = str(service.path)
    path = tmp_path / "router.yaml"
    path.write_text(yaml.safe_dump(raw))

    main(["-c", str(path), "decisions", "replay", decision_id])
    out = capsys.readouterr().out
    assert "stored:   big(medium) rule=admission_fallback" in out
    assert "replayed: big(medium) rule=admission_fallback" in out
    assert "admission_fallback" in out
    assert "without pressure" not in out
