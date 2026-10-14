"""Start the local dashboard (Phase 8) on http://127.0.0.1:<port>/dashboard.

Default: real data from PostgreSQL (read through the application services).
--demo: synthetic in-memory demo (no database needed); review actions change only memory.

The dashboard never calls Apify, Firecrawl, Crawl4AI, Agent-Reach, Laya, Hunter, Apollo, the
AI provider (OpenAI) or Google Sheets. It binds to 127.0.0.1 only. Stop it with Ctrl+C.

Examples:
  uv run python scripts/run_dashboard.py
  uv run python scripts/run_dashboard.py --demo --port 8010
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from app.core.config import Settings
from app.db.session import check_schema
from app.demo import build_demo_outputs, demo_pipeline
from app.main import create_app
from app.services.dashboard import DashboardBackend


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--demo", action="store_true", help="synthetic in-memory data, no database")
    p.add_argument("--port", type=int, default=8000, help="local port (default 8000)")
    return p.parse_args(argv)


def demo_backend(settings: Settings) -> DashboardBackend:
    demo = asyncio.run(build_demo_outputs(settings))
    return DashboardBackend(
        demo.outputs,
        demo.views,
        demo.world.qualifications,
        "demo (synthetic, in-memory)",
        pipeline=demo_pipeline(settings, demo),
        synthetic=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1024 <= args.port <= 65535:
        print("REFUSING: --port must be 1024..65535")
        return 2
    settings = Settings()
    backend = None
    if args.demo:
        backend = demo_backend(settings)
    else:
        state, detail = check_schema(settings)
        if state != "ok":
            print(f"REFUSING: database schema not ready ({state}: {detail}); try --demo")
            return 2
    app = create_app(settings, dashboard_backend=backend)
    print(f"dashboard: http://127.0.0.1:{args.port}/dashboard  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
