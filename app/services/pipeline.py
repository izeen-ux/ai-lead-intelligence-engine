"""Pipeline orchestrator (Phase 9, minimal).

``PipelineService.run()`` executes the requested stages in the fixed order
discovery -> website -> signals -> qualification -> enrichment -> analysis -> export.
Every stage is the EXISTING stage service with its own gate, budget and history; the
orchestrator adds only: one running pipeline at a time (duplicate triggers refused), a company
limit per stage, a total estimated-cost ceiling, a heartbeat, per-stage results, fail-closed
behaviour (a failed stage stops the run; later stages never run on a broken input) and
stale-run recovery. It never bypasses a gate and never retries a paid call on its own.

DRY RUN (default everywhere) = no stage writes anything. A writing run needs live provider
mode; paid stages additionally need ALLOW_PAID_PROVIDER_CALLS (enforced by the adapters).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError, ConfigurationError
from app.core.logging import get_logger, pipeline_run_var, pipeline_stage_var
from app.domain.models import utcnow
from app.domain.pipeline import (
    PIPELINE_POLICY_VERSION,
    STAGE_ORDER,
    PipelineRunRecord,
    PipelineStatus,
    Stage,
    StageResult,
    StageStatus,
    Trigger,
)
from app.repositories.pipeline import PipelineStore
from app.services.credits import credit_delta

logger = get_logger(__name__)


@dataclass(frozen=True)
class StageContext:
    run: PipelineRunRecord
    limit: int
    dry_run: bool


@dataclass
class StageOutcome:
    status: StageStatus = StageStatus.SUCCEEDED
    counts: dict[str, int] = field(default_factory=dict)
    estimated_cost_usd: Decimal = Decimal(0)
    stage_run_id: str | None = None
    detail: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def skipped(detail: str) -> StageOutcome:
    return StageOutcome(status=StageStatus.SKIPPED, detail=detail)


StageRunner = Callable[[StageContext], Awaitable[StageOutcome]]


def parse_stages(value: str) -> list[Stage]:
    """Comma list -> stages in the canonical order (unknown names are a configuration error)."""
    names = [v.strip().lower() for v in value.split(",") if v.strip()]
    if names == ["all"]:
        return list(STAGE_ORDER)
    try:
        wanted = {Stage(n) for n in names}
    except ValueError as exc:
        valid = ", ".join(s.value for s in STAGE_ORDER)
        raise ConfigurationError(f"unknown pipeline stage ({exc}); valid: {valid}") from None
    if not wanted:
        raise ConfigurationError("no pipeline stage selected")
    return [s for s in STAGE_ORDER if s in wanted]


PROMPT_MAX_CHARS = 2000
MAX_PROMPT_QUERIES = 10


def owner_names(setting: str) -> list[str]:
    return [n.strip() for n in setting.split(",") if n.strip()]


def resolve_owner(owner: str | None, setting: str) -> str | None:
    """Canonical owner name, or None when no owner was given. With a configured owner list
    (LEAD_OWNERS) any other name is refused."""
    name = (owner or "").strip()
    if not name:
        return None
    allowed = owner_names(setting)
    if not allowed:
        return name[:120]
    match = next((a for a in allowed if a.lower() == name.lower()), None)
    if match is None:
        raise ConfigurationError(f"unknown lead owner {name!r}; allowed: {', '.join(allowed)}")
    return match


# Words that show a line is a request in plain language, not search keywords.
_REQUEST_WORDS = frozenset(
    [
        "i",
        "we",
        "me",
        "my",
        "need",
        "want",
        "give",
        "show",
        "who",
        "whom",
        "which",
        "got",
        "get",
        "trying",
        "looking",
        "should",
        "please",
        "leads",
        "till",
        "upto",
        "recently",
        "find",
    ]
)
# Sector words kept when a request becomes search queries. A fixed list on purpose: an
# unknown or misspelt word in a search query ("expnad") finds nothing.
SECTORS = frozenset(
    [
        "ai",
        "saas",
        "d2c",
        "b2b",
        "fintech",
        "healthtech",
        "edtech",
        "agritech",
        "agtech",
        "foodtech",
        "cleantech",
        "climate",
        "deeptech",
        "proptech",
        "insurtech",
        "legaltech",
        "hrtech",
        "martech",
        "adtech",
        "logistics",
        "mobility",
        "ev",
        "electric",
        "robotics",
        "drone",
        "spacetech",
        "space",
        "cybersecurity",
        "gaming",
        "media",
        "creator",
        "fashion",
        "beauty",
        "skincare",
        "wellness",
        "fitness",
        "pet",
        "jewellery",
        "furniture",
        "retail",
        "ecommerce",
        "consumer",
        "pharma",
        "biotech",
        "medtech",
        "healthcare",
        "manufacturing",
        "energy",
        "solar",
        "travel",
        "food",
        "beverage",
        "education",
        "finance",
        "insurance",
        "marketplace",
        "apparel",
        "sustainability",
    ]
)
_AMOUNT_CR = re.compile(r"(\d+(?:\.\d+)?)\s*(?:cr|crore|crores)\b", re.I)
_HIRING = re.compile(r"\b(hir\w*|expand\w*|grow\w*|team)\b", re.I)


def is_request(line: str) -> bool:
    """True for a sentence ("I need startups that got funding") rather than search words."""
    words = re.findall(r"[a-z']+", line.lower())
    return len(words) > 9 or any(w in _REQUEST_WORDS for w in words)


def expand_request(line: str) -> list[str]:
    """A plain-language request -> news-style search queries for this product's lead type
    (young Indian companies with a small recent round). Sector words of the request are kept
    ("healthtech", "d2c skincare"); an amount in crore picks the round sizes searched."""
    sector_words = [w for w in re.findall(r"[a-z0-9]+", line.lower()) if w in SECTORS]
    sector = " ".join(dict.fromkeys(sector_words))[:60]
    lead = f"{sector} startup" if sector else "startup"
    cap = min((float(m) for m in _AMOUNT_CR.findall(line)), default=5.0)
    amounts = [a for a in (2, 3, 5, 1, 4) if a <= max(cap, 1)][:3]
    queries = [
        f"{lead} raises pre-seed funding India",
        f"{lead} raises seed round India",
        *[f"{lead} raises Rs {a} crore" for a in amounts],
        f"{lead} bags angel funding India",
        f"Indian {lead} secures seed funding led by",
    ]
    if _HIRING.search(line):
        queries.insert(2, f"{lead} raises funding to expand team India")
    return queries


def prompt_queries(prompt: str | None) -> list[str]:
    """The operator's prompt as search queries (max 10, 200 chars each). A line of search
    words is used as written; a line written as a sentence is turned into search queries
    (``expand_request``) because a sentence typed into a search engine finds no funding news."""
    out: list[str] = []
    for raw in (prompt or "").splitlines():
        line = " ".join(raw.split())[:200]
        if line:
            out += expand_request(line) if is_request(line) else [line]
    return list(dict.fromkeys(out))[:MAX_PROMPT_QUERIES]


# Stage counts that describe the whole sheet (not this round): the last round's value wins.
_LAST_VALUE_COUNTS = frozenset({"leads", "unchanged"})


def merge_stage(old: StageResult, new: StageResult) -> StageResult:
    """One result per stage for a run of several rounds: counts, cost and time add up."""
    counts = dict(old.counts)
    for key, value in new.counts.items():
        counts[key] = value if key in _LAST_VALUE_COUNTS else counts.get(key, 0) + value
    worked = StageStatus.SUCCEEDED in (old.status, new.status)
    status = (
        StageStatus.FAILED
        if new.status is StageStatus.FAILED
        else (StageStatus.SUCCEEDED if worked else new.status)
    )
    return StageResult(
        stage=old.stage,
        status=status,
        started_at=old.started_at,
        finished_at=new.finished_at,
        duration_ms=old.duration_ms + new.duration_ms,
        counts=counts,
        estimated_cost_usd=old.estimated_cost_usd + new.estimated_cost_usd,
        stage_run_id=new.stage_run_id or old.stage_run_id,
        detail=new.detail if status is not StageStatus.SUCCEEDED else None,
        error_code=new.error_code,
        error_message=new.error_message,
    )


class PipelineService:
    def __init__(
        self,
        store: PipelineStore,
        runners: dict[Stage, StageRunner],
        settings: Settings,
        *,
        allow_mock_writes: bool = False,
        clock: Callable[[], datetime] = utcnow,
        credit_snapshot: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self._store = store
        self._runners = runners
        self._settings = settings
        self._allow_mock_writes = allow_mock_writes  # in-memory demo / tests only
        self._clock = clock
        self._credit_snapshot = credit_snapshot  # provider counters, read before/after a run

    async def _credits(self, run: PipelineRunRecord, key: str) -> None:
        """Store the credit counters in the run (free account endpoints; never fails a run)."""
        if self._credit_snapshot is None or run.dry_run:
            return
        try:
            run.config[key] = await asyncio.wait_for(self._credit_snapshot(), timeout=30)
        except Exception:  # a counter that cannot be read must not stop lead finding
            logger.warning("credit snapshot failed", extra={"pipeline_run_id": str(run.id)})

    @property
    def mode(self) -> str:
        return "live" if self._settings.provider_mode is ProviderMode.LIVE else "mock"

    def writes_allowed(self) -> bool:
        return self.mode == "live" or self._allow_mock_writes

    # ------------------------------------------------------------------ read
    async def runs(self, limit: int = 20) -> list[PipelineRunRecord]:
        return await asyncio.to_thread(self._store.list_runs, limit)

    async def running(self) -> PipelineRunRecord | None:
        return await asyncio.to_thread(self._store.running)

    # ------------------------------------------------------------------ recovery
    async def recover_stale(self) -> list[PipelineRunRecord]:
        """Close RUNNING runs whose heartbeat is older than PIPELINE_STALE_AFTER_MINUTES (the
        process died). Stage histories are untouched; the run is marked ABANDONED and the
        running slot is freed. Safe to call at any time."""
        current = await asyncio.to_thread(self._store.running)
        if current is None:
            return []
        cutoff = self._clock() - timedelta(minutes=self._settings.pipeline_stale_after_minutes)
        if current.heartbeat_at > cutoff:
            return []
        died_in = current.current_stage  # read before the status changes
        current.status = PipelineStatus.ABANDONED
        current.error_code = "stale_run"
        current.error_message = (
            f"no heartbeat since {current.heartbeat_at.isoformat(timespec='seconds')}; "
            f"closed by recovery (stage: {died_in.value if died_in else 'unknown'})"
        )
        current.finished_at = self._clock()
        await asyncio.to_thread(self._store.save_run, current)
        logger.warning("stale pipeline run recovered", extra={"pipeline_run_id": str(current.id)})
        return [current]

    # ------------------------------------------------------------------ run
    def new_run(
        self,
        *,
        trigger: Trigger,
        dry_run: bool = True,
        stages: list[Stage] | None = None,
        limit: int | None = None,
        requested_by: str | None = None,
        owner: str | None = None,
        prompt: str | None = None,
        target_leads: int | None = None,
    ) -> PipelineRunRecord:
        if not dry_run and not self.writes_allowed():
            raise ConfigurationError(
                "a writing pipeline run needs PROVIDER_MODE=live for this process "
                "(use a dry run otherwise)"
            )
        wanted = stages or parse_stages(self._settings.pipeline_stages)
        # No limit given -> the conservative default. An explicit limit is honoured up to
        # PIPELINE_MAX_COMPANY_LIMIT and refused beyond it (never silently reduced: an
        # operator asking for 10 companies must not get 5 without being told).
        ceiling = self._settings.pipeline_max_company_limit
        if limit is not None and not 1 <= limit <= ceiling:
            raise ConfigurationError(
                f"company limit {limit} is outside 1..{ceiling} (PIPELINE_MAX_COMPANY_LIMIT)"
            )
        company_limit = limit or min(self._settings.pipeline_company_limit, ceiling)
        most = self._settings.pipeline_max_target_leads
        if target_leads is not None and not 1 <= target_leads <= most:
            raise ConfigurationError(
                f"leads wanted {target_leads} is outside 1..{most} (PIPELINE_MAX_TARGET_LEADS)"
            )
        lead_owner = resolve_owner(owner, self._settings.lead_owners)
        queries = prompt_queries(prompt)
        now = self._clock()
        return PipelineRunRecord(
            trigger=trigger,
            requested_by=requested_by,
            mode=self.mode,
            dry_run=dry_run,
            stages_requested=[s for s in STAGE_ORDER if s in wanted],
            company_limit=company_limit,
            config={
                "policy_version": PIPELINE_POLICY_VERSION,
                "max_estimated_cost_usd": str(
                    self._settings.pipeline_max_estimated_cost_usd_per_run
                ),
                "paid_calls_allowed": self._settings.allow_paid_provider_calls,
                "sheets_writes_allowed": self._settings.allow_sheets_writes,
                # who the leads of this run belong to, and the operator's search prompt (one
                # query per line) that replaces the configured discovery queries for this run
                "owner": lead_owner,
                "prompt": (prompt or "").strip()[:PROMPT_MAX_CHARS] or None,
                "discovery_queries": queries,
                # "leads wanted": repeat the stages until this many NEW sheet rows exist
                "target_leads": target_leads,
            },
            started_at=now,
            heartbeat_at=now,
        )

    async def start(self, run: PipelineRunRecord) -> None:
        """Claim the single running slot. Raises PipelineAlreadyRunningError if taken."""
        await asyncio.to_thread(self._store.create_run, run)

    async def run(self, **kw: object) -> PipelineRunRecord:
        """Convenience: new_run + start + execute."""
        record = self.new_run(**kw)  # type: ignore[arg-type]
        await self.start(record)
        return await self.execute(record)

    async def execute(self, run: PipelineRunRecord) -> PipelineRunRecord:
        token_run = pipeline_run_var.set(str(run.id))
        cap = self._settings.pipeline_max_estimated_cost_usd_per_run
        target = int(run.config.get("target_leads") or 0)
        rounds = self._settings.pipeline_max_rounds if target and not run.dry_run else 1
        run.config["max_rounds"] = rounds
        idle = 0  # rounds in a row that had no company to work on
        try:
            await self._credits(run, "credits_before")
            for round_no in range(1, rounds + 1):
                run.config["round"] = round_no
                spent_before, worked = run.estimated_cost_usd, 0
                for stage in run.stages_requested:
                    run.config["stage_now"] = stage.value
                    await asyncio.to_thread(self._store.save_run, run)
                    result = await self._stage(run, stage)
                    worked += sum(result.counts.get(k, 0) for k in ("new_companies", "companies"))
                    earlier = run.result(stage)
                    if earlier is None:
                        run.stages.append(result)
                    else:
                        run.stages[run.stages.index(earlier)] = merge_stage(earlier, result)
                    run.estimated_cost_usd += result.estimated_cost_usd
                    run.heartbeat_at = self._clock()
                    if result.status is StageStatus.FAILED:
                        run.status = PipelineStatus.FAILED
                        run.error_code = result.error_code or "stage_failed"
                        run.error_message = f"{stage.value}: {result.error_message or 'failed'}"
                        break
                    if run.estimated_cost_usd > cap:
                        run.status = PipelineStatus.FAILED
                        run.error_code = "pipeline_budget_exhausted"
                        run.error_message = (
                            f"estimated cost ${run.estimated_cost_usd} exceeded "
                            f"PIPELINE_MAX_ESTIMATED_COST_USD_PER_RUN (${cap}) after {stage.value}"
                        )
                        break
                    await asyncio.to_thread(self._store.save_run, run)
                if run.status is PipelineStatus.FAILED:
                    break
                export = run.result(Stage.EXPORT)
                found = export.counts.get("inserted", 0) if export else 0
                run.config["leads_found"] = found
                idle = 0 if worked else idle + 1
                round_cost = run.estimated_cost_usd - spent_before
                if not target:
                    break
                if found >= target:
                    run.config["stop_reason"] = "the wanted number of leads was found"
                    break
                if idle >= 2:
                    run.config["stop_reason"] = "the searches found no more new companies"
                    break
                if run.estimated_cost_usd + round_cost > cap:
                    run.config["stop_reason"] = "the cost limit of one run was reached"
                    break
                if round_no == rounds:
                    run.config["stop_reason"] = f"the limit of {rounds} search rounds was reached"
            if run.status is not PipelineStatus.FAILED:
                run.status = PipelineStatus.SUCCEEDED
        except BaseException as exc:  # shutdown / cancellation: record it, then re-raise
            run.status = PipelineStatus.CANCELLED
            run.error_code = "cancelled"
            run.error_message = type(exc).__name__
            run.finished_at = self._clock()
            self._store.save_run(run)  # sync: the event loop may be closing
            logger.warning("pipeline run cancelled", extra={"status": run.status.value})
            raise
        finally:
            pipeline_run_var.reset(token_run)
        await self._credits(run, "credits_after")
        run.finished_at = self._clock()
        await asyncio.to_thread(self._store.save_run, run)
        logger.info(
            "pipeline run finished",
            extra={
                "pipeline_run_id": str(run.id),
                "status": run.status.value,
                "error_code": run.error_code,
                "estimated_cost_usd": str(run.estimated_cost_usd),
                "duration_ms": int((run.finished_at - run.started_at).total_seconds() * 1000),
            },
        )
        return run

    async def _stage(self, run: PipelineRunRecord, stage: Stage) -> StageResult:
        started_at, started = self._clock(), time.monotonic()
        token = pipeline_stage_var.set(stage.value)
        try:
            runner = self._runners.get(stage)
            if runner is None:
                outcome = skipped("stage not available in this environment")
            else:
                outcome = await runner(StageContext(run, run.company_limit, run.dry_run))
        except AppError as exc:
            outcome = StageOutcome(
                status=StageStatus.FAILED, error_code=exc.code, error_message=exc.message[:500]
            )
        except Exception as exc:  # unexpected: fail the stage, never leak details/secrets
            logger.exception("pipeline stage crashed")
            outcome = StageOutcome(
                status=StageStatus.FAILED,
                error_code="internal_error",
                error_message=type(exc).__name__,
            )
        finally:
            pipeline_stage_var.reset(token)
        duration = int((time.monotonic() - started) * 1000)
        logger.info(
            "pipeline stage finished",
            extra={
                "pipeline_run_id": str(run.id),
                "stage": stage.value,
                "status": outcome.status.value,
                "error_code": outcome.error_code,
                "duration_ms": duration,
                "estimated_cost_usd": str(outcome.estimated_cost_usd),
            },
        )
        return StageResult(
            stage=stage,
            status=outcome.status,
            started_at=started_at,
            finished_at=self._clock(),
            duration_ms=duration,
            counts=outcome.counts,
            estimated_cost_usd=outcome.estimated_cost_usd,
            stage_run_id=outcome.stage_run_id,
            detail=outcome.detail,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
        )


def run_summary(run: PipelineRunRecord) -> dict[str, object]:
    """JSON-safe view of a run for the API / dashboard / CLI (no secrets, no lead payloads)."""
    return {
        "run_id": str(run.id),
        "status": run.status.value,
        "trigger": run.trigger.value,
        "requested_by": run.requested_by,
        "owner": run.config.get("owner"),
        "prompt": run.config.get("prompt"),
        # the searches really used (a sentence prompt is turned into search queries)
        "queries": run.config.get("discovery_queries") or [],
        "target_leads": run.config.get("target_leads"),
        "leads_found": run.config.get("leads_found"),
        "round": run.config.get("round"),
        "max_rounds": run.config.get("max_rounds"),
        "stop_reason": run.config.get("stop_reason"),
        "mode": run.mode,
        "dry_run": run.dry_run,
        "company_limit": run.company_limit,
        "current_stage": run.current_stage.value if run.current_stage else None,
        "started_at": run.started_at.isoformat(timespec="seconds"),
        "heartbeat_at": run.heartbeat_at.isoformat(timespec="seconds"),
        "finished_at": run.finished_at.isoformat(timespec="seconds") if run.finished_at else None,
        "estimated_cost_usd": str(run.estimated_cost_usd),
        "error_code": run.error_code,
        "error_message": run.error_message,
        # what this run spent per provider (counters before vs after); [] until it finished
        "credits": credit_delta(run.config.get("credits_before"), run.config.get("credits_after")),
        "stages": [
            {
                "stage": s.stage.value,
                "status": s.status.value,
                "duration_ms": s.duration_ms,
                "counts": s.counts,
                "estimated_cost_usd": str(s.estimated_cost_usd),
                "stage_run_id": s.stage_run_id,
                "detail": s.detail,
                "error_code": s.error_code,
                "error_message": s.error_message,
            }
            for s in run.stages
        ],
    }
