#!/usr/bin/env python
"""Compare between-turn effort policies offline, on recorded turn sequences.

    uv run python evals/effort_replay.py
    uv run python evals/effort_replay.py --fixtures evals/effort_fixtures.yaml
    uv run python evals/effort_replay.py --json

Five arms over the same sessions:

| arm | what it is |
| --- | --- |
| `fixed_low` | the bottom rung for every turn |
| `fixed_high` | the top rung for every turn |
| `fixed_medium` | the matched-quality middle, where there is one |
| `simple_rule` | one threshold on difficulty, no hysteresis, no floors |
| `jev_hysteresis` | the shipped policy: `effort.plan_turn` with hysteresis |
| `jev_no_hysteresis` | the same policy with the confirmations turned off |
| `jev_upward_step` | the same policy climbing one rung at a time |

`jev_hysteresis` raises to the rung the evidence asks for, which may be two
rungs in one move (owner decision, 2026-09-19). `jev_upward_step` is the
older mechanic, kept as an arm so the two can be compared rather than
asserted about.

It makes no calls. It replays `effort.plan_turn`, which is pure, over turn
sequences and scores each arm against the effort each turn is labelled as
needing. Two things it deliberately will not do:

- It will not pretend its fixtures are measurements. The shipped fixtures are
  synthetic, they say so in every row and in the report, and no number from
  this script is evidence about a real deployment or a real model.
- It will not treat a turn as an independent trial. The resample is by
  session, because the turns inside one session are not independent of each
  other and a downgrade's confirmations span several of them.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(EVALS_DIR))

from jev_router.config import AdaptiveEffortCfg  # noqa: E402
from jev_router.effort import CHANGE, Evidence, plan_turn  # noqa: E402
from manifest import build_manifest, write_manifest  # noqa: E402
from metrics import paired_bootstrap_p, wilson  # noqa: E402

DEFAULT_FIXTURES = EVALS_DIR / "effort_fixtures.yaml"
LADDER = ["low", "medium", "high"]

ARMS = (
    "fixed_low",
    "fixed_medium",
    "fixed_high",
    "simple_rule",
    "jev_hysteresis",
    "jev_no_hysteresis",
    "jev_upward_step",
)

# Which arm runs the policy, and how it is configured. The upward mode is an
# arm of its own so the jump and the step can be measured against each other.
POLICY_ARMS = {
    "jev_hysteresis": {"hysteresis": True, "upward": "jump"},
    "jev_no_hysteresis": {"hysteresis": False, "upward": "jump"},
    "jev_upward_step": {"hysteresis": True, "upward": "step"},
}


# --- fixtures ------------------------------------------------------------


@dataclass
class Turn:
    """One eligible boundary, its evidence, and what it ought to have got."""

    id: str
    difficulty: float | None = None
    harm_if_wrong: float | None = None
    p_hard: float | None = None
    corrective_followup: float | None = None
    failure_mode: str | None = None
    short_continuation: bool = False
    error: str = ""
    pressure: float = 0.0
    # The rung this turn is labelled as needing. Labels are the fixture
    # author's, not a measurement of any model.
    wants: str = "low"

    def evidence(self) -> Evidence:
        return Evidence(
            difficulty=self.difficulty,
            harm_if_wrong=self.harm_if_wrong,
            p_hard=self.p_hard,
            corrective_followup=self.corrective_followup,
            failure_mode=self.failure_mode,
            short_continuation=self.short_continuation,
            error=self.error,
        )


@dataclass
class Session:
    id: str
    base: str = "low"
    floor: str | None = None
    ladder: list[str] = field(default_factory=lambda: list(LADDER))
    provenance: str = "synthetic"
    description: str = ""
    turns: list[Turn] = field(default_factory=list)


def load_sessions(path: Path) -> list[Session]:
    raw = yaml.safe_load(path.read_text()) or {}
    out: list[Session] = []
    for row in raw.get("sessions") or []:
        out.append(
            Session(
                id=str(row["id"]),
                base=str(row.get("base") or "low"),
                floor=row.get("floor"),
                ladder=list(row.get("ladder") or LADDER),
                provenance=str(row.get("provenance") or "synthetic"),
                description=" ".join(str(row.get("description") or "").split()),
                turns=[
                    Turn(id=str(t["id"]), **{k: v for k, v in t.items() if k != "id"})
                    for t in row.get("turns") or []
                ],
            )
        )
    return out


def validate(sessions: list[Session]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for session in sessions:
        if session.id in seen:
            problems.append(f"duplicate session id {session.id!r}")
        seen.add(session.id)
        if not session.turns:
            problems.append(f"{session.id}: a session needs at least one turn")
        if session.base not in session.ladder:
            problems.append(
                f"{session.id}: base {session.base!r} is not on its ladder"
            )
        for turn in session.turns:
            if turn.wants not in session.ladder:
                problems.append(
                    f"{session.id}/{turn.id}: wants {turn.wants!r}, which is not "
                    "on the ladder"
                )
        if session.provenance != "synthetic":
            problems.append(
                f"{session.id}: provenance {session.provenance!r} is not "
                "'synthetic'; a recorded session needs its own labelled fixture "
                "file and must not be checked in with real work in it"
            )
    return problems


# --- the arms ------------------------------------------------------------


def policy_cfg(arm: str) -> AdaptiveEffortCfg:
    return AdaptiveEffortCfg(mode="shadow", ladder=list(LADDER), **POLICY_ARMS[arm])


def simple_rule(turn: Turn, ladder: list[str], floor: str | None) -> str:
    """One threshold on difficulty and nothing else.

    No hysteresis, no floors beyond the explicit one, no corrective signal,
    no quota. This is the mechanism the Jev policy has to beat to be worth
    its extra moving parts.
    """
    bottom = ladder.index(floor) if floor in ladder else 0
    if turn.difficulty is None:
        return ladder[bottom]
    if turn.difficulty >= 1.8:
        return ladder[max(bottom, len(ladder) - 1)]
    if turn.difficulty >= 0.8:
        return ladder[max(bottom, min(1, len(ladder) - 1))]
    return ladder[bottom]


def run_arm(arm: str, session: Session) -> list[dict[str, Any]]:
    """What one arm sends for each turn of one session."""
    rows: list[dict[str, Any]] = []
    ladder = session.ladder
    bottom = ladder.index(session.floor) if session.floor in ladder else 0
    current = session.base
    recommendations: list[str | None] = []

    for turn in session.turns:
        changed = False
        if arm == "fixed_low":
            sent = ladder[bottom]
        elif arm == "fixed_high":
            sent = ladder[-1]
        elif arm == "fixed_medium":
            sent = ladder[max(bottom, min(1, len(ladder) - 1))]
        elif arm == "simple_rule":
            sent = simple_rule(turn, ladder, session.floor)
            changed = sent != current
            current = sent
        else:
            plan = plan_turn(
                cfg=policy_cfg(arm),
                ladder=ladder,
                current=current,
                base=session.base,
                floor=session.floor,
                evidence=turn.evidence(),
                recommendations=recommendations,
                pressure=turn.pressure,
            )
            recommendations.append(plan.recommendation)
            changed = plan.action == CHANGE
            current = plan.to_effort or current
            sent = current
        rows.append(
            {
                "session": session.id,
                "turn": turn.id,
                "wants": turn.wants,
                "sent": sent,
                "changed": changed,
                "adequate": rank(ladder, sent) >= rank(ladder, turn.wants),
                "exact": sent == turn.wants,
                "over": rank(ladder, sent) - rank(ladder, turn.wants),
            }
        )
    return rows


def rank(ladder: list[str], effort: str) -> int:
    try:
        return ladder.index(effort)
    except ValueError:
        return -1


# --- scoring -------------------------------------------------------------


@dataclass
class ArmScore:
    arm: str
    turns: int
    adequate: int
    exact: int
    over_rungs: int
    changes: int
    sessions: int

    @property
    def under_rate(self) -> float:
        return 0.0 if not self.turns else (self.turns - self.adequate) / self.turns

    @property
    def mean_over(self) -> float:
        return 0.0 if not self.turns else self.over_rungs / self.turns


def score(arm: str, rows: list[dict[str, Any]]) -> ArmScore:
    return ArmScore(
        arm=arm,
        turns=len(rows),
        adequate=sum(1 for r in rows if r["adequate"]),
        exact=sum(1 for r in rows if r["exact"]),
        over_rungs=sum(max(0, r["over"]) for r in rows),
        changes=sum(1 for r in rows if r["changed"]),
        sessions=len({r["session"] for r in rows}),
    )


def by_session(rows: list[dict[str, Any]], sessions: list[Session]) -> list[float]:
    """One number per session: the share of its turns that were adequate.

    The unit of resampling is the session, not the turn. Turns inside one
    session share a starting effort and a confirmation run, so treating each
    as an independent trial would report an interval far too narrow.
    """
    out: list[float] = []
    for session in sessions:
        mine = [r for r in rows if r["session"] == session.id]
        out.append(sum(1 for r in mine if r["adequate"]) / len(mine) if mine else 0.0)
    return out


def report(
    scores: dict[str, ArmScore],
    per_session: dict[str, list[float]],
    sessions: list[Session],
    arms: list[str],
) -> str:
    lines = [
        "# Between-turn effort policies, replayed",
        "",
        "**Nothing here is a measurement of a model or a deployment.** The "
        "fixtures are synthetic and labelled synthetic, the labels are the "
        "fixture author's opinion of what each turn needs, and no live call "
        "was made. This exists to compare the mechanics of the policies "
        "against each other and to show what hysteresis costs and buys.",
        "",
        f"{len(sessions)} synthetic sessions, "
        f"{sum(len(s.turns) for s in sessions)} turns.",
        "",
        "| arm | adequate | under-served | mean rungs over | effort changes |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for arm in arms:
        s = scores[arm]
        low, high = wilson(s.adequate, s.turns)
        lines.append(
            f"| `{arm}` | {s.adequate}/{s.turns} [{low:.2f}, {high:.2f}] | "
            f"{s.under_rate:.2f} | {s.mean_over:.2f} | {s.changes} |"
        )

    lines += [
        "",
        "`adequate` is the share of turns sent at or above the rung the turn "
        "is labelled as needing; the interval is a Wilson interval over turns "
        "and is therefore optimistic, because turns in one session are not "
        "independent. `mean rungs over` is what that adequacy costs.",
        "",
        "## Paired by session",
        "",
    ]
    base = "jev_hysteresis"
    if base in per_session:
        for arm in arms:
            if arm == base:
                continue
            left, right = per_session[base], per_session[arm]
            p = paired_bootstrap_p(left, right)
            lines.append(
                f"- `{base}` against `{arm}`: {len(left)} paired sessions, "
                f"paired bootstrap p = {p:.3f}. With this many synthetic "
                "sessions a p above 0.05 means the replay could not separate "
                "them, not that they behave the same."
            )

    lines += ["", "## Hysteresis", ""]
    with_h = scores.get("jev_hysteresis")
    without = scores.get("jev_no_hysteresis")
    if with_h and without:
        lines.append(
            f"Turning the confirmations off changes {without.changes - with_h.changes} "
            f"effort changes and {without.adequate - with_h.adequate} adequate "
            "turns. Whether that trade is worth taking is a question for a "
            "live comparison on a deployment somebody has qualified, not for "
            "this file."
        )

    lines += ["", "## Upward: jump against step", ""]
    jump, step = scores.get("jev_hysteresis"), scores.get("jev_upward_step")
    if jump and step:
        lines.append(
            f"Going straight to the rung the evidence asks for reaches "
            f"{jump.adequate} adequate turns of {jump.turns} in {jump.changes} "
            f"changes; climbing one rung at a time reaches {step.adequate} in "
            f"{step.changes}. It costs {jump.mean_over:.2f} rungs of overshoot "
            f"a turn against {step.mean_over:.2f}. These are synthetic turns "
            "with the fixture author's labels, so this says what the two "
            "mechanics do to each other and nothing about answer quality."
        )

    lines += ["", "## Sessions", "", "| session | turns | provenance | what it is |",
              "| --- | ---: | --- | --- |"]
    for session in sessions:
        lines.append(
            f"| `{session.id}` | {len(session.turns)} | {session.provenance} | "
            f"{session.description} |"
        )
    return "\n".join(lines) + "\n"


# --- main ----------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixtures", help="a turn-sequence fixture file")
    p.add_argument("--arms", help=f"comma separated (known: {', '.join(ARMS)})")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-write", action="store_true", help="print, write nothing")
    args = p.parse_args()

    path = Path(args.fixtures) if args.fixtures else DEFAULT_FIXTURES
    sessions = load_sessions(path)
    problems = validate(sessions)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2

    arms = [a.strip() for a in (args.arms or ",".join(ARMS)).split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        print(f"unknown arms: {', '.join(unknown)}", file=sys.stderr)
        return 2

    rows: dict[str, list[dict[str, Any]]] = {}
    for arm in arms:
        rows[arm] = [row for s in sessions for row in run_arm(arm, s)]
    scores = {arm: score(arm, rows[arm]) for arm in arms}
    per_session = {arm: by_session(rows[arm], sessions) for arm in arms}

    if args.json:
        print(
            json.dumps(
                {
                    "provenance": "synthetic",
                    "sessions": len(sessions),
                    "arms": {
                        arm: {
                            "turns": s.turns,
                            "adequate": s.adequate,
                            "exact": s.exact,
                            "under_rate": round(s.under_rate, 4),
                            "mean_rungs_over": round(s.mean_over, 4),
                            "changes": s.changes,
                        }
                        for arm, s in scores.items()
                    },
                },
                indent=2,
            )
        )
        return 0

    text = report(scores, per_session, sessions, arms)
    print(text)
    if args.no_write:
        return 0
    out_dir = EVALS_DIR / "results" / "effort-replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "effort_replay.md").write_text(text)
    (out_dir / "effort_replay.json").write_text(
        json.dumps({arm: rows[arm] for arm in arms}, indent=2)
    )
    write_manifest(
        out_dir,
        build_manifest(
            script="evals/effort_replay.py",
            kind="policy_replay",
            cases={
                "count": sum(len(s.turns) for s in sessions),
                "by_split": {"synthetic": sorted(s.id for s in sessions)},
                "provenance": "synthetic fixtures; no live call, no model measured",
            },
            graders={"labels": "the fixture author's per-turn effort labels"},
            extra={"arms": arms, "fixtures": str(path)},
        ),
    )
    print(f"wrote {out_dir}/effort_replay.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
