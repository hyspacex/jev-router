"""Atomic result allocation and publication for concurrent evaluation runs."""

import os
import tempfile
from pathlib import Path

def allocate_results(parent: Path, stamp: str) -> Path:
    """Reserve a distinct directory even when writers start together."""
    parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        candidate = parent / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            n += 1


def publish_latest(out_dir: Path) -> None:
    """Publish a completed run without exposing a missing or partial link."""
    with tempfile.TemporaryDirectory(prefix=".latest-", dir=out_dir.parent) as tmp:
        link = Path(tmp) / "latest"
        link.symlink_to(out_dir.name)
        os.replace(link, out_dir.parent / "latest")

