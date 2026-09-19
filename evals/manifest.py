#!/usr/bin/env python
"""The run manifest of spec 13.1: everything a number needs to be believed.

Every eval run writes `manifest.json` beside its results, plus a readable
`manifest.md`. It records what was run, against which wording, with which
models, on which cases, under which caps, and what did not finish.

Two distinctions it exists to keep:

- A run that answered every question out of the disk cache is a `policy_replay`.
  It says what the policy does with answers somebody else's run paid for. It is
  not a live classifier measurement and it is not a latency measurement.
- An unfinished or capped unit of work is listed, not dropped. A run that
  silently skips the expensive half of its cases reports the cheap half's
  numbers as if they were the whole set.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(EVALS_DIR))

from jev_router.semantic import digest, packet_version, question_hash  # noqa: E402

# How the run got its answers.
LIVE = "live"
POLICY_REPLAY = "policy_replay"
MIXED = "mixed"

MANIFEST_SCHEMA = "1"


def repo_sha() -> str:
    """The commit this ran against, with a mark when the tree was dirty."""
    try:
        sha = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def run_kind(live_calls: int, total_units: int) -> str:
    """`policy_replay` when nothing was asked live, `live` when it all was."""
    if live_calls <= 0:
        return POLICY_REPLAY
    if total_units and live_calls >= total_units:
        return LIVE
    return MIXED


def policy_hash(config: Any) -> str:
    """Everything that turns answers into a route, as one hash."""
    return digest(
        {
            "policy": config.policy.model_dump(),
            "rulesets": {k: v.model_dump() for k, v in config.rulesets.items()},
            "routes": {k: v.model_dump() for k, v in config.routes.items()},
            "quality_lanes": {
                k: v.model_dump() for k, v in config.quality_lanes.items()
            },
            "quota_policy": config.quota_policy.model_dump(),
            "semantic_policy": config.semantic_policy.model_dump(),
            "default_route": config.settings.default_route.model_dump(),
            "effort_order": config.settings.effort_order,
        }
    )


def profile_versions(config: Any, models: list[str] | None = None) -> dict[str, Any]:
    """The deployment facts for each candidate profile, not the model's fame."""
    names = models if models is not None else list(config.models)
    out: dict[str, Any] = {}
    for name in names:
        mcfg = config.models.get(name)
        if mcfg is None:
            continue
        out[name] = {
            "upstream_id": mcfg.upstream_id,
            "provider": mcfg.provider,
            "protocol": mcfg.protocol,
            "context_window": mcfg.context_window,
            "max_output_tokens": mcfg.max_output_tokens,
            "compatibility_revision": mcfg.compatibility_revision,
            "efforts": list(mcfg.efforts),
            "effort_control": mcfg.effort_control.model_dump(),
        }
    return out


def variant_versions(config: Any, alias: str, questions: list[str],
                     state_builder: str) -> dict[str, Any]:
    return {
        "alias": alias,
        "questions": list(questions),
        "question_hash": question_hash(config, questions),
        "state_builder": state_builder,
        "packet_version": packet_version(config, questions, state_builder),
        "policy_hash": policy_hash(config),
        "config_hash": config.config_hash,
    }


def case_manifest(cases: list[Any]) -> dict[str, Any]:
    """Which cases ran and which split each was in. Ids, never requests."""
    by_split: dict[str, list[str]] = {}
    for case in cases:
        by_split.setdefault(case.split, []).append(case.id)
    return {
        "count": len(cases),
        "by_split": {k: sorted(v) for k, v in sorted(by_split.items())},
        "slices": _counts(getattr(c, "slice", "") for c in cases),
        "sources": _counts(getattr(c, "source", "") for c in cases),
    }


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items()))


def build_manifest(
    *,
    script: str,
    kind: str,
    variants: dict[str, Any] | None = None,
    cases: dict[str, Any] | None = None,
    seeds: dict[str, Any] | None = None,
    repeats: int = 1,
    cache: dict[str, Any] | None = None,
    caps: dict[str, Any] | None = None,
    quota: dict[str, Any] | None = None,
    graders: dict[str, Any] | None = None,
    candidates: dict[str, Any] | None = None,
    jev_model: str = "",
    exclusions: list[dict[str, Any]] | None = None,
    unfinished: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One machine-readable record of what this run was."""
    return {
        "schema_version": MANIFEST_SCHEMA,
        "script": script,
        "kind": kind,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_sha": repo_sha(),
        "python": platform.python_version(),
        "jev_model": jev_model,
        "variants": variants or {},
        "candidates": candidates or {},
        "cases": cases or {},
        "seeds": seeds or {},
        "repeats": repeats,
        "cache": cache or {},
        "caps": caps or {},
        # Recorded so a later reader can see how much of the run had any quota
        # reading at all. Unknown coverage is reported, never read as zero.
        "quota": quota or {"coverage": "none recorded"},
        "graders": graders or {},
        "exclusions": exclusions or [],
        "unfinished": unfinished or [],
        **(extra or {}),
    }


def summarise(manifest: dict[str, Any]) -> str:
    """The same facts, for a person."""
    lines = [
        f"# Run manifest: {manifest.get('script', '?')}",
        "",
        f"- **kind**: `{manifest.get('kind')}`"
        + (
            "  — answers came out of the disk cache, so this says what the "
            "policy does with them. It is not a live classifier or latency "
            "measurement."
            if manifest.get("kind") == POLICY_REPLAY
            else ""
        ),
        f"- **written**: {manifest.get('written_at')}",
        f"- **repo**: `{manifest.get('repo_sha')}`",
        f"- **jev model**: `{manifest.get('jev_model') or '-'}`",
        f"- **repeats**: {manifest.get('repeats')}",
    ]
    cases = manifest.get("cases") or {}
    if cases:
        splits = ", ".join(
            f"{name} {len(ids)}" for name, ids in (cases.get("by_split") or {}).items()
        )
        lines.append(f"- **cases**: {cases.get('count', 0)} ({splits or 'no splits'})")
    seeds = manifest.get("seeds") or {}
    if seeds:
        lines.append(
            "- **seeds**: " + ", ".join(f"{k}={v}" for k, v in sorted(seeds.items()))
        )
    cache = manifest.get("cache") or {}
    if cache:
        lines.append(
            "- **cache**: " + ", ".join(f"{k}={v}" for k, v in sorted(cache.items()))
        )
    caps = manifest.get("caps") or {}
    if caps:
        lines.append(
            "- **caps**: " + ", ".join(f"{k}={v}" for k, v in sorted(caps.items()))
        )
    quota = manifest.get("quota") or {}
    if quota:
        lines.append(f"- **quota coverage**: {quota.get('coverage', 'unknown')}")

    variants = manifest.get("variants") or {}
    if variants:
        lines += ["", "## Variants", "", "| variant | state builder | questions | question hash | policy hash |", "| --- | --- | --- | --- | --- |"]
        for name, row in sorted(variants.items()):
            lines.append(
                f"| {name} | {row.get('state_builder', '-')} | "
                f"{', '.join(row.get('questions') or []) or '-'} | "
                f"`{row.get('question_hash', '-')}` | `{row.get('policy_hash', '-')}` |"
            )

    candidates = manifest.get("candidates") or {}
    if candidates:
        lines += ["", "## Candidate profiles", "", "| model | protocol | window | max output | revision |", "| --- | --- | ---: | ---: | --- |"]
        for name, row in sorted(candidates.items()):
            lines.append(
                f"| {name} | {row.get('protocol')} | {row.get('context_window')} | "
                f"{row.get('max_output_tokens') or '-'} | "
                f"{row.get('compatibility_revision') or '-'} |"
            )

    graders = manifest.get("graders") or {}
    if graders:
        lines += ["", "## Graders", ""]
        for name, value in sorted(graders.items()):
            lines.append(f"- {name}: {value}")

    unfinished = manifest.get("unfinished") or []
    lines += ["", "## Unfinished and excluded", ""]
    if not unfinished and not manifest.get("exclusions"):
        lines.append("Nothing. Every unit of work in this run finished.")
    for row in unfinished:
        lines.append(f"- unfinished: {json.dumps(row, default=str)}")
    for row in manifest.get("exclusions") or []:
        lines.append(f"- excluded: {json.dumps(row, default=str)}")
    return "\n".join(lines) + "\n"


def write_manifest(out_dir: Path, manifest: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str, sort_keys=False))
    (out_dir / "manifest.md").write_text(summarise(manifest))
    return path


if __name__ == "__main__":  # a quick look at what this repo would stamp
    print(json.dumps({"repo_sha": repo_sha()}, indent=2))
