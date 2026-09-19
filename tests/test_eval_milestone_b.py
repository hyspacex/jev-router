"""The Milestone B eval machinery, offline: manifest, controls, sim, sessions.

Nothing here makes a call. The session runner is driven with a fake client,
and the quota simulator replays numbers written into the test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
sys.path.insert(0, str(EVALS))

import manifest as manifest_mod  # noqa: E402
import quota_sim  # noqa: E402
import run_eval  # noqa: E402
import run_sessions  # noqa: E402
from common import config_with_overlay, load_cases  # noqa: E402

# --- the manifest --------------------------------------------------------


def test_a_cache_only_run_is_labelled_a_policy_replay():
    assert manifest_mod.run_kind(0, 100) == "policy_replay"
    assert manifest_mod.run_kind(100, 100) == "live"
    assert manifest_mod.run_kind(7, 100) == "mixed"


def test_the_manifest_carries_what_a_number_needs_to_be_believed():
    config = config_with_overlay()
    record = manifest_mod.build_manifest(
        script="evals/run_eval.py",
        kind=manifest_mod.POLICY_REPLAY,
        jev_model="jev-1.13.0",
        variants={
            "router_yaml": manifest_mod.variant_versions(
                config, "auto", ["task", "difficulty"], "continuation_aware_v1"
            )
        },
        candidates=manifest_mod.profile_versions(config, ["gpt-6-astra"]),
        cases=manifest_mod.case_manifest(load_cases()[:5]),
        seeds={"split": 20260918},
        repeats=3,
        cache={"jev": "enabled"},
        caps={"concurrency": 8},
        graders={"programmatic": "evals/checks.py"},
        unfinished=[{"case": "x", "error": "timeout"}],
    )
    for key in (
        "repo_sha",
        "jev_model",
        "variants",
        "candidates",
        "cases",
        "seeds",
        "repeats",
        "cache",
        "caps",
        "quota",
        "graders",
        "exclusions",
        "unfinished",
    ):
        assert key in record
    variant = record["variants"]["router_yaml"]
    assert variant["question_hash"] and variant["policy_hash"]
    assert variant["packet_version"].startswith("pkt1:")
    assert record["candidates"]["gpt-6-astra"]["protocol"] == "openai-chat"
    assert record["cases"]["count"] == 5
    assert record["unfinished"] == [{"case": "x", "error": "timeout"}]


def test_the_policy_hash_moves_when_the_ladder_moves():
    base = manifest_mod.policy_hash(config_with_overlay())
    moved = manifest_mod.policy_hash(
        config_with_overlay({"policy": {"low_confidence": {"min_confidence": 0.9}}})
    )
    assert base != moved
    assert base == manifest_mod.policy_hash(config_with_overlay())


def test_the_readable_summary_says_when_a_run_was_a_replay(tmp_path):
    record = manifest_mod.build_manifest(
        script="evals/run_eval.py", kind=manifest_mod.POLICY_REPLAY
    )
    manifest_mod.write_manifest(tmp_path, record)
    text = (tmp_path / "manifest.md").read_text()
    assert "policy_replay" in text
    assert "not a live classifier" in text
    assert "Every unit of work in this run finished." in text
    assert json.loads((tmp_path / "manifest.json").read_text())["kind"] == (
        "policy_replay"
    )


def test_the_summary_names_every_unfinished_unit(tmp_path):
    record = manifest_mod.build_manifest(
        script="evals/run_sessions.py",
        kind="live",
        unfinished=[{"task": "implementation-csv", "capped": "call cap"}],
    )
    manifest_mod.write_manifest(tmp_path, record)
    text = (tmp_path / "manifest.md").read_text()
    assert "implementation-csv" in text
    assert "call cap" in text


# --- the negative controls -----------------------------------------------


def test_the_two_policy_controls_cannot_read_a_jev_answer():
    variants, _ = run_eval.load_variants()
    for control in ("constant-policy", "length-only"):
        variant = run_eval.apply_policy_control(variants["router_yaml"], control)
        config = variant.config()
        ruleset = config.ruleset_for(config.aliases[variant.alias])
        assert ruleset.low_confidence is None
        for rule in ruleset.rules:
            for key in rule.when:
                assert key not in config.questions, (
                    f"{control} rule {rule.name} reads {key}, so it is not a control"
                )


def test_the_constant_policy_control_sends_everything_to_one_route():
    from jev_router.features import Features
    from jev_router.policy import evaluate

    variants, _ = run_eval.load_variants()
    variant = run_eval.apply_policy_control(variants["router_yaml"], "constant-policy")
    config = variant.config()
    alias = config.aliases[variant.alias]
    seen = set()
    for difficulty in (0.0, 1.5, 3.0):
        answers = {
            "task": {"type": "choice", "choice": "code-edit", "confidence": 0.9},
            "difficulty": {"type": "score", "score": difficulty, "confidence": 0.9},
            "harm_if_wrong": {"type": "noul", "noul": 0.9},
        }
        result = evaluate(
            config, alias, answers, Features(model="auto", message_count=1, est_tokens=10)
        )
        seen.add((result.model, result.effort))
    assert len(seen) == 1


def test_the_length_only_control_reads_nothing_but_the_token_count():
    from jev_router.features import Features
    from jev_router.policy import evaluate

    variants, _ = run_eval.load_variants()
    variant = run_eval.apply_policy_control(variants["router_yaml"], "length-only")
    config = variant.config()
    alias = config.aliases[variant.alias]
    hard = {
        "task": {"type": "choice", "choice": "code-edit", "confidence": 0.9},
        "difficulty": {"type": "score", "score": 3.0, "confidence": 0.9},
        "harm_if_wrong": {"type": "noul", "noul": 0.95},
    }
    short = evaluate(
        config, alias, hard, Features(model="auto", message_count=1, est_tokens=10)
    )
    long = evaluate(
        config, alias, {}, Features(model="auto", message_count=1, est_tokens=9000)
    )
    # A hard, harmful, short request goes to the cheap route and a long empty
    # one goes to the top. That is the control working.
    assert short.rule == "short"
    assert long.rule == "very_long"


def test_every_control_name_is_offered_on_the_command_line():
    assert set(run_eval.CONTROLS) == {
        "none",
        "shuffled-labels",
        "constant-state",
        "constant-policy",
        "length-only",
    }


# --- case labels ---------------------------------------------------------


def test_a_representative_subset_carries_atomic_labels():
    cases = load_cases()
    labelled = [c for c in cases if c.labels]
    assert 20 <= len(labelled) <= 40
    assert {k for c in labelled for k in c.labels} == {
        "mechanical_transform",
        "interacting_constraints",
        "requirements_missing",
    }
    # The categories spec 13.4 asks for are all represented.
    assert {c.slice for c in labelled} >= {
        "agentic",
        "chat",
        "adversarial",
        "longcontext",
        "multilingual",
        "pipeline",
    }


def test_adding_a_label_restates_nothing_from_expected():
    """Invariant I14: an annotation cannot move an existing number."""
    raw = yaml.safe_load((EVALS / "cases.yaml").read_text())
    for item in raw["cases"]:
        labels = item.get("labels") or {}
        assert not (set(labels) & set(item.get("expected") or {}))


def test_a_case_with_no_label_is_skipped_not_scored_as_wrong():
    from run_eval import Record, score_question

    cases = load_cases()
    unlabelled = next(c for c in cases if not c.labels)
    labelled = next(c for c in cases if c.labels.get("mechanical_transform") is True)
    answers = {"mechanical_transform": {"type": "noul", "noul": 0.9}}

    def record(case):
        return Record(
            variant="v", case=case, state=None, answers=answers, repeats=[],
            latencies=[], input_tokens=0, model="m", effort=None, rule="r", reason="",
        )

    assert score_question(record(unlabelled), "mechanical_transform") is None
    assert score_question(record(labelled), "mechanical_transform") is True


def test_a_question_table_counts_only_the_labelled_cases():
    from run_eval import Record, Variant, question_table

    cases = [c for c in load_cases() if c.labelled][:20]
    records = [
        Record(
            variant="v", case=c, state=None,
            answers={"mechanical_transform": {"type": "noul", "noul": 0.9}},
            repeats=[], latencies=[], input_tokens=0, model="m", effort=None,
            rule="r", reason="",
        )
        for c in cases
    ]
    rows = question_table(records, Variant(name="v", questions=["mechanical_transform"]))
    assert len(rows) == 1
    row = rows[0]
    assert row["answered"] == len(records)
    assert row["labelled"] <= len(records)
    assert row["agreement"].n == row["labelled"]


# --- the quota simulator -------------------------------------------------


def test_the_simulator_replays_in_time_order_and_labels_itself():
    result = quota_sim.run(quota_sim.demo_scenario())
    assert result.provenance == "simulated"
    assert [row["id"] for row in result.rows] == ["s1", "s2", "s3"]
    text = quota_sim.report(result)
    assert "Provenance: simulated" in text
    assert "ROUTE_COST" in text and "deliberately not used" in text


def test_the_simulator_never_reads_route_cost():
    source = (EVALS / "quota_sim.py").read_text()
    assert "route_cost(" not in source
    assert "from common import config_with_overlay" in source


def test_a_window_running_ahead_of_pace_moves_a_new_admission():
    result = quota_sim.run(quota_sim.demo_scenario())
    calm, pressed = result.rows[0], result.rows[1]
    assert calm["model"] == "gpt-6-astra"
    assert pressed["quota_changed_choice"] is True
    assert pressed["model"] != "gpt-6-astra"


def test_a_failed_poll_is_neutral_and_stays_visible_as_an_error():
    result = quota_sim.run(quota_sim.demo_scenario())
    after_error = result.rows[2]
    assert after_error["quota_status"]["openai"] == "error"
    assert after_error["model"] == "gpt-6-astra"
    assert result.unknown_at_admission == 3


def test_a_scenario_loads_from_a_file(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "tiny",
                "readings": [
                    {
                        "at": 0,
                        "provider": "openai",
                        "windows": [{"name": "weekly", "used_percent": 95.0}],
                    }
                ],
                "admissions": [
                    {
                        "at": 10,
                        "id": "a1",
                        "answers": {
                            "task": {"type": "choice", "choice": "code-edit",
                                     "confidence": 0.9},
                            "difficulty": {"type": "score", "score": 0.2,
                                           "confidence": 0.9},
                            "harm_if_wrong": {"type": "noul", "noul": 0.1},
                        },
                    }
                ],
            }
        )
    )
    scenario = quota_sim.load_scenario(path)
    assert scenario.name == "tiny"
    result = quota_sim.run(scenario)
    assert result.rows[0]["quota_status"]["openai"] == "fresh"
    assert result.summary()["admissions"] == 1


def test_an_outage_reads_as_full_pressure_but_never_as_usage():
    scenario = quota_sim.demo_scenario()
    scenario.outages = [{"provider": "openai", "from": 0, "until": 100000}]
    result = quota_sim.run(scenario)
    assert all(row["pressure"].get("openai") == 1.0 for row in result.rows)
    # Health is availability evidence. It never claims a usage figure.
    assert all("used" not in json.dumps(row["quota_status"]) for row in result.rows)


# --- the session runner --------------------------------------------------


def test_the_pilot_covers_the_six_families():
    tasks = run_sessions.load_tasks()
    assert run_sessions.validate(tasks) == []
    assert len(tasks) == 8
    assert {t.family for t in tasks} == set(run_sessions.FAMILIES)


def test_a_broken_task_spec_is_reported():
    bad = run_sessions.TaskSpec(
        id="x", family="nonsense", description="", prompt="",
        check=run_sessions.Check(command=[]),
    )
    problems = run_sessions.validate([bad, bad])
    assert any("unknown family" in p for p in problems)
    assert any("needs a command" in p for p in problems)
    assert any("duplicate task id" in p for p in problems)
    assert any("needs a prompt" in p for p in problems)


def test_the_environment_a_session_runs_in_carries_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-one")
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-two")
    monkeypatch.setenv("JEV_ROUTER_ADMIN_TOKEN", "secret-three")
    env = run_sessions.scrubbed_env(tmp_path)
    assert "secret-one" not in json.dumps(env)
    assert set(env) == {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH", "PYTHONHASHSEED", "JEV_SESSION_SANDBOX",
    }
    assert env["HOME"] == str(tmp_path)


def test_a_workspace_refuses_a_path_that_escapes_it(tmp_path):
    task = run_sessions.load_tasks()[0]
    workspace = run_sessions.Workspace(task, tmp_path / "ws")
    workspace.materialise()
    assert "src/billing.py" in workspace.list_files()
    with pytest.raises(ValueError):
        workspace.read_file("../../etc/passwd")
    with pytest.raises(ValueError):
        workspace.write_file("../escaped.txt", "no")


def test_the_hidden_check_files_do_not_exist_during_the_session(tmp_path):
    task = next(t for t in run_sessions.load_tasks() if t.id == "mechanical-rename")
    workspace = run_sessions.Workspace(task, tmp_path / "ws")
    workspace.materialise()
    listing = workspace.list_files()
    for name in task.check.hidden_files:
        assert name not in listing
    workspace.run_check()
    for name in task.check.hidden_files:
        assert not (tmp_path / "ws" / name).exists()


# --- fake clients --------------------------------------------------------


class FakeClient:
    """Plays a scripted session. No network, no model."""

    def __init__(self, script, model="gpt-6-astra", effort="medium"):
        self.script = list(script)
        self.model = model
        self.effort = effort
        self.resolved = 0
        self.completions = 0

    async def resolve(self, task, arm):
        self.resolved += 1
        return {
            "mode": "fake",
            "model": arm.get("model") or self.model,
            "effort": arm.get("effort", self.effort),
            "wire_model": "fake",
        }

    async def complete(self, binding, messages, tools):
        self.completions += 1
        if self.script:
            return self.script.pop(0)
        return {"content": "done", "tool_calls": []}


def tool_call(name, **args):
    return {
        "tool_calls": [
            {
                "id": f"call-{name}",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
        "content": "",
    }


async def test_a_session_that_does_the_work_is_recorded_as_completed(tmp_path):
    task = next(t for t in run_sessions.load_tasks() if t.id == "mechanical-transform")
    client = FakeClient(
        [
            tool_call("read_file", path="config/settings.env"),
            tool_call(
                "write_file",
                path="config/settings.json",
                content='{"host": "127.0.0.1", "port": "8318", "log_level": "info"}',
            ),
            {"content": "written", "tool_calls": []},
        ]
    )
    result = await run_sessions.run_task(
        task, "fixed_strong", client,
        caps=run_sessions.Caps(max_calls=8, wall_clock_seconds=120),
        workspace_root=tmp_path / "ws",
    )
    assert result.error == ""
    assert result.completed is True
    assert result.tool_calls == 2
    assert result.model == "gpt-6-astra"
    assert client.resolved == 1


async def test_a_session_that_does_nothing_is_recorded_as_not_completed(tmp_path):
    task = next(t for t in run_sessions.load_tasks() if t.id == "mechanical-transform")
    result = await run_sessions.run_task(
        task, "fixed_strong", FakeClient([{"content": "I'd rather not", "tool_calls": []}]),
        caps=run_sessions.Caps(max_calls=4, wall_clock_seconds=60),
        workspace_root=tmp_path / "ws",
    )
    assert result.completed is False
    assert result.capped == ""


async def test_a_capped_session_is_reported_and_not_dropped(tmp_path):
    task = next(t for t in run_sessions.load_tasks() if t.id == "mechanical-transform")
    looping = FakeClient([tool_call("list_files") for _ in range(50)])
    result = await run_sessions.run_task(
        task, "fixed_strong", looping,
        caps=run_sessions.Caps(max_calls=3, wall_clock_seconds=60),
        workspace_root=tmp_path / "ws",
    )
    assert result.calls == 3
    assert "call cap" in result.capped
    assert result.completed is False
    assert result.unfinished is True
    text = run_sessions.report([result], ["fixed_strong"])
    assert "Capped, unfinished and errored" in text
    assert "call cap" in text


async def test_a_wall_clock_cap_stops_a_session(tmp_path):
    task = next(t for t in run_sessions.load_tasks() if t.id == "mechanical-transform")
    result = await run_sessions.run_task(
        task, "fixed_strong", FakeClient([tool_call("list_files") for _ in range(50)]),
        caps=run_sessions.Caps(max_calls=99, wall_clock_seconds=0.0),
        workspace_root=tmp_path / "ws",
    )
    assert "wall clock" in result.capped


async def test_a_tool_the_task_does_not_allow_is_refused(tmp_path):
    task = next(t for t in run_sessions.load_tasks() if t.id == "mechanical-transform")
    workspace = run_sessions.Workspace(task, tmp_path / "ws")
    workspace.materialise()
    answer = run_sessions.run_tool(
        workspace,
        task,
        {"function": {"name": "delete_everything", "arguments": "{}"}},
    )
    assert "not an allowed tool" in answer


async def test_a_check_that_already_passes_is_reported_as_broken(tmp_path):
    task = run_sessions.TaskSpec(
        id="already-passing",
        family="mechanical",
        description="",
        prompt="do nothing",
        allowed_tools=[],
        repo={"files": {"tests/test_ok.py": "def test_ok():\n    assert True\n"}},
        check=run_sessions.Check(
            command=["python", "-m", "pytest", "-q", "tests/"],
            expect_fail_before=True,
        ),
    )
    result = await run_sessions.run_task(
        task, "fixed_strong", FakeClient([]),
        caps=run_sessions.Caps(max_calls=2, wall_clock_seconds=60),
        workspace_root=tmp_path / "ws",
    )
    assert "already passes" in result.error
    assert result.completed is False


async def test_a_multi_turn_task_walks_its_turns(tmp_path):
    task = next(t for t in run_sessions.load_tasks() if t.id == "multi-turn-ladder")
    assert len(task.user_turns()) == 3
    client = FakeClient([{"content": "ok", "tool_calls": []} for _ in range(3)])
    result = await run_sessions.run_task(
        task, "fixed_strong", client,
        caps=run_sessions.Caps(max_calls=9, wall_clock_seconds=120),
        workspace_root=tmp_path / "ws",
    )
    assert result.turns_total == 3
    assert result.turns_done == 3
    assert client.completions == 3


def test_the_report_pairs_by_task_and_says_what_a_pilot_cannot_show():
    results = [
        run_sessions.SessionResult(
            task=f"t{i}", family="mechanical", arm=arm, repeat=0,
            completed=(arm == "jev_packet" and i < 3),
        )
        for i in range(4)
        for arm in ("fixed_strong", "jev_packet")
    ]
    text = run_sessions.report(results, ["fixed_strong", "jev_packet"])
    assert "paired bootstrap p" in text
    assert "could not tell them apart" in text
    assert "## By task" in text


def test_the_arms_table_matches_the_spec_and_names_what_is_missing():
    assert set(run_sessions.POLICY_ARMS) == {
        "fixed_strong",
        "simple_rules",
        "jev_mean",
        "jev_packet",
        "fixed_effort_vs_adaptive",
    }
    blocked = run_sessions.POLICY_ARMS["fixed_effort_vs_adaptive"]
    assert "not implemented" in blocked["requires"]


def test_the_generated_fixture_is_deterministic():
    spec = {"kind": "handlers", "count": 5, "broken": 3}
    assert run_sessions.generated_repo(spec) == run_sessions.generated_repo(spec)
    files = run_sessions.generated_repo(spec)
    assert '"200"' in files["src/handlers/h03.py"]
    assert '"200"' not in files["src/handlers/h02.py"]
