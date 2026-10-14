"""Pipeline scheduler (Phase 9): a small long-running process.

Every tick (60 s) it (1) closes stale RUNNING runs and (2) when
PIPELINE_SCHEDULE_INTERVAL_MINUTES > 0 and no run is in progress, starts one pipeline run:
a WRITING run when this process is in live provider mode, otherwise a DRY RUN.

It never changes the safety switches. Paid stages run only if ALLOW_PAID_PROVIDER_CALLS=true
was set for this process; the Sheet is written only with ALLOW_SHEETS_WRITES=true.
Ctrl+C / SIGTERM: the current run is recorded as CANCELLED and the process exits cleanly.

Examples:
  uv run python scripts/run_scheduler.py            # loop (interval from .env)
  uv run python scripts/run_scheduler.py --once     # one tick, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.db.session import check_schema, get_engine
from app.domain.pipeline import PipelineAlreadyRunningError, Trigger
from app.providers.registry import ProviderRegistry
from app.repositories.pipeline import SqlPipelineStore
from app.services.pipeline import PipelineService
from app.services.pipeline_stages import sql_stages

logger = get_logger("scheduler")
TICK_SECONDS = 60.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--once", action="store_true", help="one tick, then exit")
    return p.parse_args(argv)


async def tick(service: PipelineService, settings: Settings, *, due: bool) -> str:
    """One scheduler step. Returns what happened (for logs/tests)."""
    recovered = await service.recover_stale()
    if recovered:
        logger.warning("recovered stale run", extra={"pipeline_run_id": str(recovered[0].id)})
    if not due:
        return "idle"
    live = settings.provider_mode is ProviderMode.LIVE
    try:
        run = await service.run(
            trigger=Trigger.SCHEDULE, dry_run=not live, requested_by="scheduler"
        )
    except PipelineAlreadyRunningError:
        logger.info("scheduled run skipped: another run is in progress")
        return "skipped_running"
    return f"ran:{run.status.value}"


async def loop(
    service: PipelineService, settings: Settings, stop: asyncio.Event, once: bool
) -> None:
    interval = settings.pipeline_schedule_interval_minutes * 60
    last_start = 0.0
    logger.info(
        "scheduler started",
        extra={
            "interval_minutes": settings.pipeline_schedule_interval_minutes,
            "mode": service.mode,
        },
    )
    while not stop.is_set():
        due = interval > 0 and (time.monotonic() - last_start >= interval or last_start == 0.0)
        if due:
            last_start = time.monotonic()
        try:
            outcome = await tick(service, settings, due=due)
            logger.info("scheduler tick", extra={"outcome": outcome})
        except AppError as exc:
            logger.error("scheduler tick failed", extra={"error_code": exc.code})
        if once:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    logger.info("scheduler stopped")


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(
        level=settings.log_level, fmt=settings.log_format.value,
        secret_values=settings.secret_values(),
    )  # fmt: skip
    state, detail = check_schema(settings)
    if state != "ok":
        print(f"REFUSING: database schema not ready ({state}: {detail})")
        return 2
    engine = get_engine(settings)
    service = PipelineService(
        SqlPipelineStore(engine), sql_stages(settings, engine, ProviderRegistry(settings)), settings
    )
    stop = asyncio.Event()
    running = asyncio.get_running_loop()

    def request_stop(*_: object) -> None:
        running.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    task = asyncio.create_task(loop(service, settings, stop, args.once))
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task not in done:  # stop requested mid-run: cancel -> the run is recorded CANCELLED
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    waiter.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
