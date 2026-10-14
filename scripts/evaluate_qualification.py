"""Evaluate the Phase 5 qualification gate on the LABELED (synthetic) dataset.

Default: mock Laya (offline, deterministic) - measures the deterministic pipeline only.
``--live``: the local Laya sidecar (LAYA_ENDPOINT_URL, loopback only; free, local compute).
Nothing is written to the database. Results on this synthetic set are NOT production accuracy.

Examples:
  uv run python scripts/evaluate_qualification.py
  uv run python scripts/evaluate_qualification.py --live --out <report.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.config import Settings
from app.core.errors import AppError
from app.evaluation.qualification import DEFAULT_DATASET, evaluate, load_dataset, write_report
from app.providers.base import QualificationProvider
from app.providers.laya import LayaAdapter, MockLayaProvider


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--live", action="store_true", help="use the local Laya sidecar")
    p.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    laya: QualificationProvider = LayaAdapter(settings) if args.live else MockLayaProvider()
    dataset = load_dataset(args.dataset)
    report = await evaluate(dataset, laya, settings)
    summary = report.as_dict()
    print(json.dumps({k: v for k, v in summary.items() if k != "mismatches"}, indent=2))
    for m in summary["mismatches"]:
        print(f"MISMATCH {m['id']} [{m['category']}] label={m['label']} predicted={m['predicted']}")
        for reason in m["reasons"]:
            print(f"    - {reason}")
    if args.out:
        write_report(report, args.out)
        print(f"report written to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except AppError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
