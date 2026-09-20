#!/usr/bin/env python
"""The executable coding-session suite of spec 13.5.

Each task in `evals/session_tasks/` is a small repository, a first user turn,
a set of tools, and a check that says whether the work is done. This runs a
session per task per policy arm, in a disposable temporary directory with a
scrubbed environment, under hard call, wall-clock and spend caps.

    uv run python evals/run_sessions.py --list
    uv run python evals/run_sessions.py --dry-run
    uv run python evals/run_sessions.py --container-image jev-eval:local --arms fixed_strong,jev_mean --repeats 2

It needs credentials to do real work: a router to resolve against and an
upstream that serves the candidate models. The offline tests drive it with
fake clients instead, so nothing in CI ever makes a call.

Isolation requirement: live runs require --container-image. Generated commands
and checks run in disposable Docker containers with only the workspace mounted
and networking disabled. The temporary directory and scrubbed environment used
by offline fixture tests are not an operating-system sandbox. See
`evals/ISOLATION.md` for setup and limits.

What it will not do:

- It will not drop a session that ran out of budget. A capped or unfinished
  session is reported as capped, with what it had done so far.
- It will not run against a repository you care about. Every workspace is a
  fresh temporary directory, the environment is rebuilt from nothing, and no
  API key is inherited by anything the session runs.
- It will not compare arms for you. It reports per-task outcomes and a paired
  resample by task; eight tasks cannot separate two close policies and the
  report says so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent
TASK_DIR = EVALS_DIR / "session_tasks"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(EVALS_DIR))

from result_io import allocate_results  # noqa: E402

from manifest import (  # noqa: E402
    build_manifest,
    run_kind,
    write_manifest,
)
from metrics import paired_bootstrap_p, rate as rate_of  # noqa: E402

FAMILIES = (
    "mechanical",
    "implementation",
    "diagnosis",
    "context-growth",
    "multi-turn",
    "environment",
)

# The policies of the 13.5 table. `alias` is what the arm resolves against and
# `model`/`effort` pin an arm that does not use the router's choice at all.
#
# Running all of them at once is not the plan: qualify the core path first,
# then compare admission policies, then hold the model fixed and vary effort.
POLICY_ARMS: dict[str, dict[str, Any]] = {
    "fixed_strong": {
        "purpose": "current-workflow reference: one strong model, one effort",
        "model": "gpt-6-astra",
        "effort": "medium",
    },
    "fixed_mid": {
        "purpose": "tool-driven coding qualification: fixed mid model, no routing",
        "model": "ollama/glm-5.3",
        "effort": "none",
    },
    "simple_rules": {
        "purpose": "what session routing is worth without Jev",
        "alias": "auto-session-rules",
        "decider": "rules",
    },
    "jev_mean": {
        "purpose": "the shipped Jev mean-and-confidence policy",
        "alias": "auto-session",
    },
    "jev_packet": {
        "purpose": "the expanded packet with the distribution policy promoted",
        "alias": "auto-session",
        "distribution_policy": "active",
    },
    "fixed_effort_vs_adaptive": {
        "purpose": "same model, fixed effort against adaptive effort. The "
        "model is held fixed; only the effort varies, so a result here is "
        "never a cross-model gain.",
        "alias": "auto-session",
        "model": "gpt-6-astra",
        "effort": "medium",
        "adaptive_effort": True,
        # The arm exists and the runner will run it, but only when somebody
        # says so twice: --allow-adaptive-effort on the command line, and a
        # deployment that has actually been qualified. Nothing in this
        # repository is qualified, so the live half of this arm is not
        # implemented and `evals/effort_replay.py` is where the comparison
        # happens offline in the meantime.
        "requires": "--allow-adaptive-effort and a qualified profile; no "
        "deployment is qualified here, so the live arm is not implemented",
        "opt_in_flag": "allow_adaptive_effort",
    },
}

DEFAULT_ARMS = ["fixed_strong", "simple_rules", "jev_mean"]


# --- task specs ----------------------------------------------------------


@dataclass
class Check:
    command: list[str]
    hidden_files: dict[str, str] = field(default_factory=dict)
    expect_fail_before: bool = True


@dataclass
class Caps:
    """Hard limits. Reaching one stops the session and is reported."""

    max_calls: int = 12
    wall_clock_seconds: float = 300.0
    max_spend_usd: float = 0.0  # 0 means "not tracked", not "unlimited"

    def merged(self, other: dict[str, Any] | None) -> Caps:
        if not other:
            return self
        return Caps(
            max_calls=min(self.max_calls, int(other.get("max_calls", self.max_calls))),
            wall_clock_seconds=min(
                self.wall_clock_seconds,
                float(other.get("wall_clock_seconds", self.wall_clock_seconds)),
            ),
            max_spend_usd=self.max_spend_usd,
        )


@dataclass
class TaskSpec:
    id: str
    family: str
    description: str
    prompt: str
    check: Check
    turns: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    repo: dict[str, Any] = field(default_factory=dict)
    caps: dict[str, Any] = field(default_factory=dict)
    path: str = ""

    def user_turns(self) -> list[str]:
        return [self.prompt, *self.turns]


def load_tasks(directory: Path | None = None) -> list[TaskSpec]:
    directory = directory or TASK_DIR
    tasks: list[TaskSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or {}
        check = raw.get("check") or {}
        tasks.append(
            TaskSpec(
                id=str(raw["id"]),
                family=str(raw["family"]),
                description=str(raw.get("description") or "").strip(),
                prompt=str(raw["prompt"]).strip(),
                turns=[str(t).strip() for t in raw.get("turns") or []],
                allowed_tools=list(raw.get("allowed_tools") or []),
                repo=dict(raw.get("repo") or {}),
                caps=dict(raw.get("caps") or {}),
                check=Check(
                    command=[str(c) for c in check.get("command") or []],
                    hidden_files=dict(check.get("hidden_files") or {}),
                    expect_fail_before=bool(check.get("expect_fail_before", True)),
                ),
                path=str(path),
            )
        )
    return tasks


def validate(tasks: list[TaskSpec]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        where = task.path or task.id
        if task.id in seen:
            problems.append(f"{where}: duplicate task id {task.id!r}")
        seen.add(task.id)
        if task.family not in FAMILIES:
            problems.append(
                f"{where}: unknown family {task.family!r} "
                f"(known: {', '.join(FAMILIES)})"
            )
        if not task.check.command:
            problems.append(f"{where}: the check needs a command")
        if not task.repo.get("files") and not task.repo.get("git") and not task.repo.get(
            "generated"
        ):
            problems.append(f"{where}: give `repo.files`, `repo.generated` or `repo.git`")
        if not task.prompt:
            problems.append(f"{where}: the task needs a prompt")
    return problems


# --- the workspace -------------------------------------------------------

# What a session's subprocesses are allowed to see. Built from nothing, so no
# API key, token or account reaches generated code.
def scrubbed_env(home: Path) -> dict[str, str]:
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(home),
        "PYTHONHASHSEED": "0",
        # Marks an evaluation workspace; this does not enforce isolation.
        "JEV_SESSION_SANDBOX": "1",
    }


def generated_repo(spec: dict[str, Any]) -> dict[str, str]:
    """Seeded fixture content for a task that wants more than a few files."""
    kind = str(spec.get("kind") or "")
    if kind != "handlers":
        raise ValueError(f"unknown generated repo kind {kind!r}")
    count = int(spec.get("count") or 12)
    broken = int(spec.get("broken") or 0)
    files = {
        "src/__init__.py": "",
        "src/handlers/__init__.py": "",
        "tests/test_handlers.py": (
            "import importlib\n\n\n"
            "def test_every_handler_answers():\n"
            "    for i in range(1, %d):\n" % (count + 1)
            + '        module = importlib.import_module(f"src.handlers.h{i:02d}")\n'
            "        status, body = module.handle({})\n"
            "        assert body\n"
        ),
    }
    for i in range(1, count + 1):
        status = f'"200"' if i == broken else "200"
        files[f"src/handlers/h{i:02d}.py"] = (
            f'"""Handler {i:02d}."""\n\n\n'
            "def handle(request):\n"
            f'    """Answer request {i:02d}."""\n'
            f"    return {status}, {{'handler': 'h{i:02d}'}}\n"
        )
    return files


class Workspace:
    """A disposable copy of a task's repository."""

    def __init__(self, task: TaskSpec, root: Path, container_image: str | None = None) -> None:
        self.task = task
        self.container_image = container_image
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def materialise(self) -> None:
        files = dict(self.task.repo.get("files") or {})
        if self.task.repo.get("generated"):
            files.update(generated_repo(self.task.repo["generated"]))
        if self.task.repo.get("git"):
            self._clone(self.task.repo["git"])
        for name, body in files.items():
            path = self._inside(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        # Packages, so `python -m pytest` can import `src.*` from the root.
        for package in ("src", "tests"):
            folder = self.root / package
            if folder.is_dir() and not (folder / "__init__.py").exists():
                (folder / "__init__.py").write_text("")

    def _clone(self, spec: dict[str, Any]) -> None:
        url, commit = str(spec.get("url") or ""), str(spec.get("commit") or "")
        if not url or not commit:
            raise ValueError("repo.git needs both a url and a fixed commit")
        subprocess.run(
            ["git", "clone", "--quiet", url, str(self.root)],
            check=True,
            timeout=300,
            env=scrubbed_env(self.root),
        )
        subprocess.run(
            ["git", "-C", str(self.root), "checkout", "--quiet", commit],
            check=True,
            timeout=120,
            env=scrubbed_env(self.root),
        )

    def _inside(self, name: str) -> Path:
        """Refuse a path that would escape the workspace."""
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"path escapes the workspace: {name}")
        return path

    # --- tools the session may use ------------------------------------

    def list_files(self) -> str:
        names = sorted(
            str(p.relative_to(self.root))
            for p in self.root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.name != ".sandbox"
        )
        return "\n".join(names)

    def read_file(self, path: str) -> str:
        target = self._inside(path)
        if not target.is_file():
            return f"error: no such file: {path}"
        return target.read_text()[:100_000]

    def write_file(self, path: str, content: str) -> str:
        target = self._inside(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path} ({len(content)} characters)"

    def run_command(self, command: list[str], timeout: float = 120.0) -> str:
        container = f"jev-eval-{uuid.uuid4().hex}" if self.container_image else None
        if container:
            command = [
                "docker", "run", "--rm", "--name", container,
                "--pull", "never", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "128", "--memory", "512m", "--cpus", "1",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
                "--mount", f"type=bind,src={self.root.resolve()},dst=/workspace",
                "--workdir", "/workspace", "--env", "HOME=/tmp",
                "--env", "PYTHONDONTWRITEBYTECODE=1",
                self.container_image, *command,
            ]
        try:
            done = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                # The trusted Docker CLI needs its host context. Container
                # environment is restricted to the explicit --env arguments.
                env=None if container else scrubbed_env(self.root),
            )
        except subprocess.TimeoutExpired:
            return "error: the command timed out"
        except (OSError, ValueError) as exc:
            return f"error: could not run it: {type(exc).__name__}"
        finally:
            if container:
                # Killing the CLI on timeout does not stop its container.
                subprocess.run(
                    ["docker", "rm", "--force", container],
                    capture_output=True, timeout=15,
                )
        return f"exit {done.returncode}\n{done.stdout[-8000:]}{done.stderr[-4000:]}"

    # --- the check ----------------------------------------------------

    def run_check(self) -> tuple[bool, str]:
        """Write the hidden files, run the check, then take them away again.

        The hidden files exist only while the check runs, so a session cannot
        read them, edit them, or make them pass by rewriting them.
        """
        written: list[Path] = []
        try:
            for name, body in self.task.check.hidden_files.items():
                path = self._inside(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                written.append(path)
            output = self.run_command(self.task.check.command)
        finally:
            for path in written:
                path.unlink(missing_ok=True)
        return output.startswith("exit 0"), output

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List every file in the repository.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write one file, replacing it if it exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "run_command": {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command in the repository root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["command"],
            },
        },
    },
}


def tool_schemas(task: TaskSpec) -> list[dict[str, Any]]:
    return [TOOL_SCHEMAS[name] for name in task.allowed_tools if name in TOOL_SCHEMAS]


# --- the client ----------------------------------------------------------


class SessionClient(Protocol):
    """What the runner needs from whatever is executing the session.

    The real one resolves a strict session against a running jev-router and
    sends chat-completions requests through it. The tests supply a fake, which
    is why nothing offline ever makes a call.
    """

    async def resolve(self, task: TaskSpec, arm: dict[str, Any]) -> dict[str, Any]: ...

    async def complete(
        self, binding: dict[str, Any], messages: list[dict[str, Any]],
        tools: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


@dataclass
class SessionResult:
    task: str
    family: str
    arm: str
    repeat: int
    completed: bool = False
    capped: str = ""
    calls: int = 0
    wall_clock_s: float = 0.0
    tool_calls: int = 0
    model: str = ""
    effort: str | None = None
    check_output: str = ""
    error: str = ""
    turns_done: int = 0
    turns_total: int = 0
    admission: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    final_response: str = ""

    @property
    def unfinished(self) -> bool:
        return bool(self.capped or self.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "family": self.family,
            "arm": self.arm,
            "repeat": self.repeat,
            "completed": self.completed,
            "capped": self.capped,
            "calls": self.calls,
            "wall_clock_s": round(self.wall_clock_s, 2),
            "tool_calls": self.tool_calls,
            "model": self.model,
            "effort": self.effort,
            "turns": f"{self.turns_done}/{self.turns_total}",
            "error": self.error,
            "admission": self.admission,
            "usage": self.usage,
            "final_response": self.final_response[-4000:],
            # The tail only. A whole pytest run is not worth keeping.
            "check_output": self.check_output[-1500:],
        }


async def run_task(
    task: TaskSpec,
    arm_name: str,
    client: SessionClient,
    *,
    caps: Caps,
    repeat: int = 0,
    workspace_root: Path | None = None,
    container_image: str | None = None,
) -> SessionResult:
    """One session: resolve once, then work the turns until a cap stops it."""
    arm = POLICY_ARMS.get(arm_name) or {}
    limits = caps.merged(task.caps)
    result = SessionResult(
        task=task.id,
        family=task.family,
        arm=arm_name,
        repeat=repeat,
        turns_total=len(task.user_turns()),
    )
    root = Path(workspace_root or tempfile.mkdtemp(prefix=f"jev-session-{task.id}-"))
    workspace = Workspace(task, root, container_image)
    started = time.perf_counter()
    binding = None
    try:
        workspace.materialise()
        if task.check.expect_fail_before:
            passes, output = workspace.run_check()
            if passes:
                result.error = (
                    "the check already passes on the untouched repository, so it "
                    "cannot show that the session did anything"
                )
                result.check_output = output
                return result

        binding = await client.resolve(task, arm)
        result.admission = {key: binding.get(key) for key in (
            "decision_source", "decision_rule", "quality_lane", "quota_at_admission"
        ) if key in binding}
        result.model = str(binding.get("model") or "")
        result.effort = binding.get("effort")

        messages: list[dict[str, Any]] = []
        tools = tool_schemas(task)
        for turn_text in task.user_turns():
            messages.append({"role": "user", "content": turn_text})
            while True:
                if result.calls >= limits.max_calls:
                    result.capped = f"call cap: {result.calls} of {limits.max_calls}"
                    break
                if time.perf_counter() - started >= limits.wall_clock_seconds:
                    result.capped = (
                        f"wall clock cap: {limits.wall_clock_seconds:.0f}s"
                    )
                    break
                result.calls += 1
                remaining = limits.wall_clock_seconds - (time.perf_counter() - started)
                try:
                    reply = await asyncio.wait_for(client.complete(binding, messages, tools), timeout=remaining)
                except asyncio.TimeoutError:
                    result.capped = f"wall clock cap: {limits.wall_clock_seconds:.0f}s"
                    break
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = reply.get("usage", {}).get(key)
                    if isinstance(value, int):
                        result.usage[key] = result.usage.get(key, 0) + value
                result.final_response = reply.get("content") or ""
                calls = reply.get("tool_calls") or []
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.get("content") or "",
                        **({"tool_calls": calls} if calls else {}),
                    }
                )
                if not calls:
                    break
                for call in calls:
                    result.tool_calls += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": run_tool(workspace, task, call),
                        }
                    )
            if result.capped:
                break
            result.turns_done += 1

        passes, output = workspace.run_check()
        result.completed = passes and not result.capped
        result.check_output = output
    except Exception as exc:  # noqa: BLE001 - a broken session is a result
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.wall_clock_s = time.perf_counter() - started
        close_binding = getattr(client, "close_binding", None)
        if binding is not None and close_binding is not None:
            try:
                await close_binding(binding)
            except Exception as exc:
                result.error = result.error or f"session cleanup failed: {type(exc).__name__}"
        if workspace_root is None:
            workspace.cleanup()
    return result


def run_tool(workspace: Workspace, task: TaskSpec, call: dict[str, Any]) -> str:
    """Run one tool call inside the workspace, or say why it was refused."""
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    if name not in task.allowed_tools:
        return f"error: {name!r} is not an allowed tool for this task"
    try:
        args = function.get("arguments")
        args = json.loads(args) if isinstance(args, str) else dict(args or {})
    except (TypeError, ValueError):
        return "error: the arguments were not valid JSON"
    try:
        if name == "list_files":
            return workspace.list_files()
        if name == "read_file":
            return workspace.read_file(str(args.get("path", "")))
        if name == "write_file":
            return workspace.write_file(
                str(args.get("path", "")), str(args.get("content", ""))
            )
        if name == "run_command":
            command = args.get("command")
            if not isinstance(command, list) or not command:
                return "error: `command` must be a non-empty list"
            return workspace.run_command([str(c) for c in command])
    except ValueError as exc:
        return f"error: {exc}"
    return f"error: unknown tool {name!r}"


# --- the report ----------------------------------------------------------


def report(results: list[SessionResult], arms: list[str]) -> str:
    lines = [
        "# Coding-session pilot",
        "",
        "Spec 13.5. This is a pilot: it is for finding failures and measuring "
        "what a session costs, not for proving a small improvement. Eight "
        "tasks cannot separate two policies that are close, and a capped or "
        "unfinished session is reported below rather than dropped.",
        "",
        "| arm | completed | capped | errored | calls | tool calls | median wall clock |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in arms:
        rows = [r for r in results if r.arm == arm]
        if not rows:
            continue
        times = sorted(r.wall_clock_s for r in rows)
        median = statistics.median(times) if times else 0.0
        lines.append(
            f"| {arm} | {rate_of(r.completed for r in rows)} | "
            f"{sum(1 for r in rows if r.capped)} | "
            f"{sum(1 for r in rows if r.error)} | "
            f"{sum(r.calls for r in rows)} | {sum(r.tool_calls for r in rows)} | "
            f"{median:.0f}s |"
        )

    lines += ["", "## By task", "", "| task | family | " + " | ".join(arms) + " |",
              "| --- | --- |" + "---|" * len(arms)]
    for task_id in sorted({r.task for r in results}):
        rows = [r for r in results if r.task == task_id]
        family = rows[0].family
        cells = []
        for arm in arms:
            hits = [r for r in rows if r.arm == arm]
            if not hits:
                cells.append("-")
                continue
            done = sum(1 for r in hits if r.completed)
            note = ""
            if any(r.capped for r in hits):
                note = " (capped)"
            elif any(r.error for r in hits):
                note = " (error)"
            cells.append(f"{done}/{len(hits)}{note}")
        lines.append(f"| {task_id} | {family} | " + " | ".join(cells) + " |")

    # Paired by task: the same task under two policies is the comparison that
    # means anything, and every turn of one session is not an independent trial.
    if len(arms) >= 2:
        lines += ["", "## Paired comparison", ""]
        base = arms[0]
        for arm in arms[1:]:
            pairs = []
            for task_id in sorted({r.task for r in results}):
                left = [r.completed for r in results if r.task == task_id and r.arm == base]
                right = [r.completed for r in results if r.task == task_id and r.arm == arm]
                if left and right:
                    pairs.append((sum(left) / len(left), sum(right) / len(right)))
            if len(pairs) < 2:
                lines.append(f"- {base} against {arm}: not enough paired tasks")
                continue
            p = paired_bootstrap_p([a for a, _ in pairs], [b for _, b in pairs])
            lines.append(
                f"- {base} against {arm}: {len(pairs)} paired tasks, "
                f"paired bootstrap p = {p:.3f}. With this few tasks a p above "
                "0.05 means the pilot could not tell them apart, not that they "
                "are the same."
            )

    unfinished = [r for r in results if r.unfinished]
    lines += ["", "## Capped, unfinished and errored", ""]
    if not unfinished:
        lines.append("None. Every session finished inside its caps.")
    for r in unfinished:
        lines.append(
            f"- `{r.task}` / `{r.arm}` repeat {r.repeat}: "
            f"{r.capped or r.error} (turns {r.turns_done}/{r.turns_total})"
        )
    return "\n".join(lines) + "\n"


# --- main ----------------------------------------------------------------


def build_client(args: argparse.Namespace) -> SessionClient:
    """The live client. Imported late so `--list` and `--dry-run` need nothing."""
    from session_client import RouterSessionClient  # noqa: PLC0415

    return RouterSessionClient(
        router_url=args.router,
        admin_token=os.environ.get("JEV_ROUTER_ADMIN_TOKEN", ""),
        timeout_s=args.timeout,
        upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
    )


async def main_async(args: argparse.Namespace) -> int:
    tasks = load_tasks(Path(args.tasks) if args.tasks else None)
    problems = validate(tasks)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    if args.task:
        wanted = {t.strip() for t in args.task.split(",")}
        tasks = [t for t in tasks if t.id in wanted]
    if args.family:
        families = {f.strip() for f in args.family.split(",")}
        tasks = [t for t in tasks if t.family in families]

    if args.list:
        for task in tasks:
            print(f"{task.id:26s} {task.family:16s} {task.description.splitlines()[0]}")
        return 0

    arms = [a.strip() for a in (args.arms or ",".join(DEFAULT_ARMS)).split(",") if a.strip()]
    unknown = [a for a in arms if a not in POLICY_ARMS]
    if unknown:
        print(f"unknown arms: {', '.join(unknown)}", file=sys.stderr)
        print(f"known: {', '.join(POLICY_ARMS)}", file=sys.stderr)
        return 2
    blocked = [
        a
        for a in arms
        if POLICY_ARMS[a].get("requires")
        and not getattr(args, POLICY_ARMS[a].get("opt_in_flag") or "", False)
    ]
    if blocked:
        for arm in blocked:
            print(
                f"arm {arm!r} needs {POLICY_ARMS[arm]['requires']}", file=sys.stderr
            )
        return 2

    if args.dry_run:
        print(f"{len(tasks)} tasks x {len(arms)} arms x {args.repeats} repeats = "
              f"{len(tasks) * len(arms) * args.repeats} sessions")
        print(f"hard caps: {args.max_calls} calls and {args.wall_clock:.0f}s each")
        for task in tasks:
            print(f"  {task.id:26s} {task.family:16s} check: "
                  f"{' '.join(task.check.command)}")
        return 0

    if not args.container_image:
        print("live evaluations require --container-image; see evals/ISOLATION.md", file=sys.stderr)
        return 2
    # Check the runtime before spending any model calls.
    probe = subprocess.run(
        ["docker", "run", "--rm", "--pull", "never", "--network", "none",
         args.container_image, "python", "-c", "import pytest"],
        capture_output=True, timeout=30,
    )
    if probe.returncode:
        print("evaluation container unavailable; see evals/ISOLATION.md", file=sys.stderr)
        return 2

    caps = Caps(max_calls=args.max_calls, wall_clock_seconds=args.wall_clock)
    out_dir = allocate_results(EVALS_DIR / "results", f"sessions-{int(time.time())}")
    (out_dir / "plan.json").write_text(json.dumps({
        "tasks": [t.id for t in tasks], "arms": arms, "repeats": args.repeats,
        "order": "task order; arm order rotates by task and repeat",
    }, indent=2))
    print(f"saving results to {out_dir}", flush=True)
    client = build_client(args)
    results: list[SessionResult] = []
    try:
        for repeat in range(args.repeats):
            for task_index, task in enumerate(tasks):
                offset = (task_index + repeat) % len(arms)
                for arm in arms[offset:] + arms[:offset]:
                    result = await run_task(
                        task, arm, client, caps=caps, repeat=repeat,
                        container_image=args.container_image
                    )
                    results.append(result)
                    checkpoint = out_dir / "sessions.tmp"
                    checkpoint.write_text(json.dumps([r.to_dict() for r in results], indent=2))
                    checkpoint.replace(out_dir / "sessions.json")
                    print(
                        f"[{arm:12s}] {task.id:26s} r{repeat} "
                        f"{'done' if result.completed else 'NOT done'} "
                        f"calls={result.calls} {result.capped or result.error}",
                        flush=True,
                    )
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()

    (out_dir / "sessions.json").write_text(
        json.dumps([r.to_dict() for r in results], indent=2)
    )
    (out_dir / "sessions.md").write_text(report(results, arms))
    write_manifest(
        out_dir,
        build_manifest(
            script="evals/run_sessions.py",
            kind=run_kind(sum(r.calls for r in results), len(results)),
            cases={
                "count": len(tasks),
                "by_split": {"pilot": sorted(t.id for t in tasks)},
                "families": sorted({t.family for t in tasks}),
            },
            repeats=args.repeats,
            caps={
                "max_calls": args.max_calls,
                "wall_clock_seconds": args.wall_clock,
                "per_task": {t.id: t.caps for t in tasks},
            },
            graders={"programmatic": "the task's own check command"},
            extra={"arms": {a: POLICY_ARMS[a] for a in arms},
                   "container_image": args.container_image},
            unfinished=[r.to_dict() for r in results if r.unfinished],
        ),
    )
    print(f"\nwrote {out_dir}/sessions.md")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tasks", help="a directory of task specs")
    p.add_argument("--task", help="comma separated task ids")
    p.add_argument("--family", help="comma separated families")
    p.add_argument("--arms", help=f"comma separated policy arms "
                                  f"(known: {', '.join(POLICY_ARMS)})")
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--max-calls", type=int, default=12,
                   help="hard cap on model calls per session")
    p.add_argument("--wall-clock", type=float, default=300.0,
                   help="hard cap in seconds per session")
    p.add_argument("--container-image", help="local Docker image for isolated commands and checks")
    p.add_argument("--router", default="http://127.0.0.1:8318")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument(
        "--allow-adaptive-effort",
        action="store_true",
        help="run the fixed_effort_vs_adaptive arm. It needs a profile that has "
             "passed evals/qualify_effort.py; without one the router resolves "
             "its sessions with adaptation off and the arm measures nothing.",
    )
    p.add_argument("--list", action="store_true", help="list the tasks and stop")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would run without a single call")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
