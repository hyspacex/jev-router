"""Opt-in Docker check: JEV_TEST_DOCKER_IMAGE=jev-eval:local uv run pytest here."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import run_sessions


@pytest.mark.skipif(not os.environ.get("JEV_TEST_DOCKER_IMAGE"), reason="requires a local evaluation image")
def test_generated_commands_are_isolated_and_recover_after_timeout(tmp_path, monkeypatch):
    sentinel = tmp_path / "host-only.txt"
    sentinel.write_text("host data")
    monkeypatch.setenv("JEV_ISOLATION_SECRET", "host-only-credential")
    workspace = run_sessions.Workspace(
        run_sessions.load_tasks()[0], tmp_path / "workspace",
        os.environ["JEV_TEST_DOCKER_IMAGE"],
    )
    script = f'''
import os, pathlib, socket
assert not pathlib.Path({str(sentinel)!r}).exists()
assert "JEV_ISOLATION_SECRET" not in os.environ
try:
    pathlib.Path("/host-write-test").write_text("no")
except OSError:
    pass
else:
    raise AssertionError("root filesystem is writable")
sock = socket.socket()
sock.settimeout(1)
try:
    sock.connect(("1.1.1.1", 443))
except OSError:
    pass
else:
    raise AssertionError("network is accessible")
finally:
    sock.close()
pathlib.Path("result.txt").write_text("workspace works")
print("isolation passed")
'''
    output = workspace.run_command(["python", "-c", script])
    assert output.startswith("exit 0"), output
    assert (workspace.root / "result.txt").read_text() == "workspace works"
    assert sentinel.read_text() == "host data"
    output = workspace.run_command(["python", "-c", "import time; time.sleep(60)"], timeout=2)
    assert output == "error: the command timed out"
    assert workspace.run_command(["python", "-c", "print('recovered')"]).startswith("exit 0")
