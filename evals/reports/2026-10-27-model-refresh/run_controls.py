import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "evals"), str(ROOT / "src")]
import run_eval as R  # noqa: E402
from common import DiskCache, JevClient, JevResult  # noqa: E402


class OfflineJev(JevClient):
    def __init__(self, *args, **kwargs):
        os.environ["TYPESAFE_API_KEY"] = "offline-cache-only-not-a-credential"
        super().__init__(*args, **kwargs)

    async def ask(self, state, questions, use_cache=True):
        hit = self.cache.get(
            DiskCache.key({"model": self.model, "state": state, "questions": questions})
        )
        if hit is None:
            raise RuntimeError("offline-only cache miss; no provider request made")
        return JevResult(
            hit["answers"], hit.get("latency_ms", 0), hit.get("input_tokens", 0), True
        )


R.JevClient = OfflineJev
DEST = Path(os.environ.get(
    "JEV_REFRESH_CONTROL_OUT",
    str(ROOT / "evals/reports/2026-10-27-model-refresh/controls"),
))
DEST.mkdir(exist_ok=True)
R.publish_latest = lambda path: None
current = ""


def allocate(*args):
    p = DEST / current
    p.mkdir(exist_ok=True)
    return p


R.allocate_results = allocate


async def main():
    global current
    for control in [
        "none",
        "shuffled-labels",
        "constant-state",
        "constant-policy",
        "length-only",
    ]:
        current = control
        args = argparse.Namespace(
            packet=False,
            variants="router_yaml",
            group=None,
            public=None,
            slice_holdout=None,
            slice=None,
            limit=None,
            case=None,
            control=control,
            draft=False,
            no_cache=False,
            jev_model="jev-1.13.0",
            concurrency=2,
            repeats=1,
            stability=False,
            no_baselines=False,
            draft_max_calls=0,
        )
        await R.main_async(args)


asyncio.run(main())
