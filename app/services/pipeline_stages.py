"""Stage runners for the pipeline orchestrator (Phase 9): thin wrappers that build the
existing stage services exactly like their CLIs do and translate the outcome into a
``StageOutcome``. No business rule lives here.

Read-only stages (qualification, enrichment, analysis, export) can run in a dry run
(``persist=False``). Discovery, website and signal research have no read-only mode (they exist
to write new data), so they are SKIPPED in a dry run.

Live pre-flight (fail closed): before a writing stage calls a local sidecar or the AI
provider, its free health / model check must pass - otherwise the stage FAILS instead of
writing degraded results (e.g. REVIEW decisions only because Laya was down).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Engine

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError, ProviderError
from app.core.logging import get_logger
from app.domain.analysis.records import AnalysisStatus
from app.domain.enums import DomainResolutionStatus
from app.domain.outputs.records import ExportMode
from app.domain.pipeline import Stage
from app.domain.prescreen import decide as decide_prescreen
from app.domain.prescreen import laya_fit, laya_request, rule_reasons
from app.domain.qualification.icp import IcpConfig
from app.domain.sources import SOURCES, SourceRegistry
from app.providers.base import WebSearchProvider
from app.providers.contracts import DiscoveryQuery
from app.providers.google_sheets import GoogleSheetsWriter, ServiceAccountTokenProvider
from app.providers.registry import ProviderRegistry
from app.repositories.analysis import AnalysisStore, SqlAnalysisStore
from app.repositories.discovery import SqlDiscoveryStore
from app.repositories.enrichment import EnrichmentStore, SqlEnrichmentStore
from app.repositories.outputs import OutputStore, SqlOutputStore
from app.repositories.pipeline import SqlPipelineStore
from app.repositories.prescreen import SqlPrescreenStore
from app.repositories.qualification import QualificationStore, SqlQualificationStore
from app.repositories.signals import SqlSignalStore
from app.repositories.website import SqlWebsiteStore
from app.services.discovery import DiscoveryService
from app.services.enrichment import EnrichmentService
from app.services.lead_analysis import LeadAnalysisService
from app.services.leads import LeadViewService
from app.services.pipeline import SECTORS, StageContext, StageOutcome, StageRunner, skipped
from app.services.qualification import QualificationService
from app.services.sheets_export import SheetsExportService, output_profile
from app.services.signal_research import SignalResearchService
from app.services.website_intelligence import CreditBudget, WebsiteIntelligenceService

Candidates = Callable[[int], list[UUID]]
Claim = Callable[[list[UUID], str, UUID], int]
logger = get_logger(__name__)
# Reasoning-provider errors that mean "the key / account cannot be used now".
AI_UNUSABLE = frozenset(
    {"provider_not_configured", "provider_quota_exhausted", "provider_model_unavailable"}
)
MAX_QUERIES_PER_REQUEST = 10  # DiscoveryQuery.keywords ceiling
GOOGLE_SEARCH_SOURCES = frozenset({"funding_news", "startup_news"})


def discovery_queries(settings: Settings, source: str) -> list[str]:
    """The configured ICP query set of a discovery source (";"-separated)."""
    if source == "funding_news" and settings.pipeline_discovery_keywords.strip():
        raw = settings.pipeline_discovery_keywords.replace(",", ";")  # deprecated override
    else:
        raw = {
            "funding_news": settings.discovery_queries,
            "startup_news": settings.discovery_news_queries,
            "linkedin_posts": settings.discovery_linkedin_queries,
            "linkedin_jobs": settings.discovery_linkedin_jobs_queries,
            "x_posts": settings.discovery_x_queries,
            "reddit": settings.discovery_reddit_queries,
        }.get(source, "")
    return list(dict.fromkeys(q.strip() for q in raw.split(";") if q.strip()))


def reddit_queries(configured: list[str], prompt: list[str]) -> list[str]:
    """Reddit is searched inside startup subreddits with short keywords: a news headline such
    as "startup raises Rs 5 crore" matches discussion posts there, not announcements (213
    posts gave 17 company names). So a prompt keeps the configured "<subreddit>:<keywords>"
    searches and only adds its sector words; a prompt line already written as
    "<subreddit>:<keywords>" is used as it is."""
    own = [q for q in prompt if ":" in q and " " not in q.partition(":")[0]]
    if own:
        return own
    words = [w for q in prompt for w in re.findall(r"[a-z0-9]+", q.lower()) if w in SECTORS]
    sector = " ".join(dict.fromkeys(words))[:40]
    if not sector:
        return configured
    return [
        f"{sub}:{sector} {keywords}".strip() if sep else f"{sector} {q}"
        for q in configured
        for sub, sep, keywords in [q.partition(":")]
    ]


def discovery_query(
    settings: Settings,
    source: str,
    *,
    limit: int,
    rotation: int = 0,
    prompt_queries: list[str] | None = None,
) -> DiscoveryQuery | None:
    """One budgeted discovery request for ``source``: as many queries (one results page
    each) as ``APIFY_MAX_ITEMS_PER_RUN`` allows, starting ``rotation`` queries into the set
    so consecutive runs do not repeat the same search. ``None`` = nothing configured."""
    # an operator prompt (one query per line) replaces the configured set for this run
    queries = list(prompt_queries) if prompt_queries else discovery_queries(settings, source)
    if source == "reddit" and prompt_queries:
        queries = reddit_queries(discovery_queries(settings, source), prompt_queries)
    if not queries:
        return None
    definition = SOURCES.get(source)
    search = source in GOOGLE_SEARCH_SOURCES
    take = min(len(queries), settings.apify_max_items_per_run, MAX_QUERIES_PER_REQUEST)
    start = (rotation * (take if search else 1)) % len(queries)
    chosen = (queries[start:] + queries[:start])[: take if search else 1]
    params = {}
    if search and settings.discovery_quick_date_range:
        params["quickDateRange"] = settings.discovery_quick_date_range
    return DiscoveryQuery(
        source=source,
        keywords=chosen,
        country_codes=["IN"],
        limit=limit,
        params=params,
        actor_id=definition.actor_id if definition else None,
        # search sources: items = result pages; social sources: items = posts / jobs
        max_items=None if search else settings.discovery_social_max_items_per_run,
    )


async def gather_limited[T, R](
    items: list[T], worker: Callable[[T], Awaitable[R]], limit: int
) -> list[R]:
    """Run ``worker`` for every item, at most ``limit`` at a time; results in input order."""
    gate = asyncio.Semaphore(max(limit, 1))

    async def one(item: T) -> R:
        async with gate:
            return await worker(item)

    return list(await asyncio.gather(*(one(item) for item in items)))


async def _preflight(provider: Any, method: str) -> None:
    """Call a provider's free health/model check when it has one (live adapters only)."""
    check: Callable[[], Awaitable[Any]] | None = getattr(provider, method, None)
    if check is not None:
        await check()


def core_stages(
    settings: Settings,
    registry: ProviderRegistry,
    *,
    qualifications: QualificationStore,
    enrichment: EnrichmentStore,
    analyses: AnalysisStore,
    outputs: OutputStore,
    qualification_candidates: Candidates,
    enrichment_candidates: Candidates,
    analysis_candidates: Candidates,
    web_search: WebSearchProvider | None = None,
    claim: Claim | None = None,
) -> dict[Stage, StageRunner]:
    """Qualification -> enrichment -> analysis -> export over ANY store implementation
    (PostgreSQL in production, in-memory in the demo)."""
    live = settings.provider_mode is ProviderMode.LIVE

    async def qualification(ctx: StageContext) -> StageOutcome:
        ids = await asyncio.to_thread(qualification_candidates, ctx.limit)
        if not ids:
            return skipped("no company needs a (new) qualification decision")
        if live and not ctx.dry_run:
            await _preflight(registry.qualification, "health")
        service = QualificationService(
            qualifications, registry.qualification, settings,
            reviewer=registry.llm_review, persist=not ctx.dry_run,
        )  # fmt: skip
        run, _ = await service.run(ids, limit=ctx.limit)
        owner = ctx.run.config.get("owner")
        if claim is not None and owner and not ctx.dry_run:
            # leads decided by this run belong to the person who started it (first owner kept)
            await asyncio.to_thread(claim, ids, str(owner), ctx.run.id)
        return StageOutcome(
            counts={
                "companies": run.companies_evaluated,
                "qualified": run.qualified_count,
                "review": run.review_count,
                "not_qualified": run.not_qualified_count,
            },
            stage_run_id=str(run.id),
        )

    async def enrich(ctx: StageContext) -> StageOutcome:
        ids = await asyncio.to_thread(enrichment_candidates, ctx.limit)
        if not ids:
            return skipped("no QUALIFIED company is waiting for a verified contact")
        if live and not ctx.dry_run:
            await _preflight(registry.enrichment, "health")
        service = EnrichmentService(
            enrichment, qualifications, registry.enrichment, registry.email_verifier, settings,
            persist=not ctx.dry_run, web_search=None if ctx.dry_run else web_search,
        )  # fmt: skip
        run, _ = await service.run(ids, limit=ctx.limit)
        return StageOutcome(
            counts={
                "companies": run.companies_evaluated,
                "verified": run.verified_count,
                "cached": run.cached_count,
                "blocked": run.blocked_count,
                "failed": run.failed_count,
            },
            estimated_cost_usd=run.estimated_cost_usd,
            stage_run_id=str(run.id),
            detail=run.error_code,
        )

    async def analysis(ctx: StageContext) -> StageOutcome:
        ids = await asyncio.to_thread(analysis_candidates, ctx.limit)
        if not ids:
            return skipped("no eligible lead is waiting for an analysis")
        service = LeadAnalysisService(
            analyses, qualifications, enrichment, registry.reasoning, settings,
            persist=not ctx.dry_run,
        )  # fmt: skip
        try:
            if live and not ctx.dry_run:
                await _preflight(registry.reasoning, "check_model")
            run, records = await service.run(ids, limit=ctx.limit)
        except ProviderError as exc:
            if exc.code not in AI_UNUSABLE:
                raise
            # The AI key is rejected / has no credits / the model is not available. That must
            # not stop the run before the sheet is written: the leads are exported without an
            # analysis and get one on a later run, once the key works.
            logger.warning("ai analysis skipped: provider unusable", extra={"error": exc.code})
            return skipped(
                f"AI analysis not available ({exc.code}) - check the AI key; "
                f"{len(ids)} lead(s) are exported without analysis for now"
            )
        return StageOutcome(
            counts={
                "leads": run.analyses_created,
                "succeeded": run.succeeded_count,
                "rejected": run.rejected_count,
                "failed": run.failed_count,
                "blocked": run.blocked_count,
                "tokens": run.total_tokens,
            },
            estimated_cost_usd=sum(
                (
                    r.cost_usd or Decimal(0)
                    for r in records
                    if r.status is not AnalysisStatus.BLOCKED
                ),
                Decimal(0),
            ),
            stage_run_id=str(run.id),
            detail=run.error_code,
        )

    async def export(ctx: StageContext) -> StageOutcome:
        views = LeadViewService(qualifications, enrichment, analyses)
        write = (
            live
            and not ctx.dry_run
            and settings.allow_sheets_writes
            and bool(settings.google_sheets_spreadsheet_id)
        )
        writer = None
        if write:
            writer = GoogleSheetsWriter(
                settings.google_sheets_spreadsheet_id or "",
                ServiceAccountTokenProvider(settings.google_service_account_json),
                timeout=settings.google_sheets_timeout_seconds,
                max_retries=settings.google_sheets_max_retries,
            )
        export_settings, fixture = output_profile(settings)
        if fixture:  # synthetic data (demo database) is never written to Google Sheets
            write, writer = False, None
        service = SheetsExportService(
            outputs,
            views,
            export_settings,
            writer,
            mode=ExportMode.LIVE if write else ExportMode.DRY_RUN,
            persist=write,
            fixture=fixture,
        )
        # not ctx.limit: a small "companies per step" would hide older leads from the sheet
        report = await service.run()
        run = report.run
        outcome = StageOutcome(
            counts={
                "leads": run.leads_considered,
                "inserted": run.inserted_count,
                "updated": run.updated_count,
                "unchanged": run.unchanged_count,
                "withdrawn": run.withdrawn_count,
                "failed": run.failed_count,
            },
            stage_run_id=str(run.id) if write else None,
            detail=None
            if write
            else "plan only (Sheet not written: dry run or ALLOW_SHEETS_WRITES=false)",
        )
        if run.status.value != "succeeded":
            raise AppError(f"sheets export {run.status.value}: {run.error_code}")
        return outcome

    return {
        Stage.QUALIFICATION: qualification,
        Stage.ENRICHMENT: enrich,
        Stage.ANALYSIS: analysis,
        Stage.EXPORT: export,
    }


def sql_stages(
    settings: Settings, engine: Engine, registry: ProviderRegistry
) -> dict[Stage, StageRunner]:
    """All eight stages over PostgreSQL."""
    pipeline_store = SqlPipelineStore(engine)
    pipeline_store.ai_review = settings.llm_review_enabled
    live = settings.provider_mode is ProviderMode.LIVE

    async def discovery(ctx: StageContext) -> StageOutcome:
        if ctx.dry_run:
            return skipped("dry run: discovery only exists to write new companies")
        sources = SourceRegistry.from_setting(settings.discovery_sources_enabled)
        store = SqlDiscoveryStore(engine)
        service = DiscoveryService(registry.discovery, store, sources)
        keys = ("sources", "queries", "candidates", "new_companies", "duplicates", "skipped")
        counts = dict.fromkeys((*keys, "failed"), 0)
        cost, run_ids, errors = Decimal(0), [], []
        prompt = [str(q) for q in ctx.run.config.get("discovery_queries") or []]
        owner = ctx.run.config.get("owner")
        planned = []
        for source in sources.runnable():
            rotation = await asyncio.to_thread(pipeline_store.discovery_run_count, source)
            query = discovery_query(
                settings, source, limit=ctx.limit, rotation=rotation, prompt_queries=prompt
            )
            if query is not None:
                planned.append((source, query))

        async def search(item: tuple[str, DiscoveryQuery]) -> Any:
            try:  # one source failing must not hide the others
                return await service.run(item[1])
            except AppError as exc:
                return exc

        # The sources are searched at the same time (the slow part is waiting for each
        # provider); what they find is stored and counted exactly as before.
        found = await gather_limited(planned, search, settings.pipeline_parallel_sources)
        for (source, query), run in zip(planned, found, strict=True):
            counts["sources"] += 1
            counts["queries"] += len(query.keywords)
            if isinstance(run, AppError):
                counts["failed"] += 1
                errors.append(f"{source}: {run.code}")
                continue
            counts["candidates"] += run.candidate_count
            counts["new_companies"] += run.new_company_count
            counts["duplicates"] += run.duplicate_count
            counts["skipped"] += run.skipped_count
            cost += run.cost_usd or run.estimated_cost_usd or Decimal(0)
            run_ids.append(str(run.id))
            if owner:
                await asyncio.to_thread(
                    pipeline_store.claim_new_companies, run.id, str(owner), ctx.run.id
                )
        if not counts["sources"]:
            return skipped("no runnable discovery source has queries configured")
        if counts["failed"] == counts["sources"]:
            raise AppError(f"every discovery source failed ({'; '.join(errors)})")
        return StageOutcome(
            counts=counts,
            estimated_cost_usd=cost,
            stage_run_id=run_ids[0] if run_ids else None,
            detail="; ".join(errors) or None,
        )

    prescreen_store = SqlPrescreenStore(engine)

    async def prescreen(ctx: StageContext) -> StageOutcome:
        if ctx.dry_run:
            return skipped("dry run: the pre-screen writes scrape / skip verdicts")
        fresh = Stage.DISCOVERY in ctx.run.stages_requested  # this run's finds go first
        candidates = await asyncio.to_thread(
            lambda: prescreen_store.candidates(ctx.limit, newest_first=fresh)
        )
        if not candidates:
            return skipped("no new company is waiting for a pre-screen")
        icp = IcpConfig.from_settings(settings)
        use_laya = settings.prescreen_use_laya
        if use_laya and live:
            await _preflight(registry.qualification, "health")
        counts = {"companies": 0, "scrape": 0, "skip": 0, "deferred": 0}
        for facts in candidates:
            counts["companies"] += 1
            fit, laya_status = None, "skipped"
            if use_laya and not rule_reasons(facts, icp):  # rules are free: ask Laya last
                try:
                    result = await registry.qualification.evaluate(laya_request(facts))
                except ProviderError:
                    # no verdict without the classifier: stay unscreened, retried next run
                    counts["deferred"] += 1
                    continue
                fit, laya_status = laya_fit(result), "ok"
            outcome = decide_prescreen(facts, icp, fit)
            await asyncio.to_thread(prescreen_store.insert, facts, outcome, laya_status)
            counts[outcome.decision.value] += 1
        return StageOutcome(counts=counts)

    async def website(ctx: StageContext) -> StageOutcome:
        if ctx.dry_run:
            return skipped("dry run: website intelligence writes pages and domain status")
        store = SqlWebsiteStore(engine)
        ids = await asyncio.to_thread(
            lambda: prescreen_store.scrape_candidates(
                ctx.limit,
                required=settings.prescreen_required,
                newest_first=Stage.DISCOVERY in ctx.run.stages_requested,
            )
        )
        if not ids:
            return skipped("no pre-screened company with an unresolved domain")
        service = WebsiteIntelligenceService(
            search=registry.web_search, primary=registry.website,
            fallback=registry.website_fallback, store=store, settings=settings,
        )  # fmt: skip
        budget = CreditBudget(total=settings.firecrawl_max_credits_per_run)
        counts = {"companies": 0, "verified": 0, "pages": 0, "credits": 0, "failed": 0}

        async def scrape(company_id: UUID) -> Any:
            try:
                return await service.process(company_id, budget)
            except AppError:
                return None
            except Exception:  # e.g. two companies claiming one domain at the same moment
                logger.exception("website step failed for one company; it stays waiting")
                return None

        # Several companies at a time: each one is searched, scraped and verified on its own,
        # so the order they are worked in does not change any result.
        for out in await gather_limited(ids, scrape, settings.pipeline_parallel_companies):
            counts["companies"] += 1
            if out is None:
                counts["failed"] += 1
                continue
            counts["verified"] += int(out.domain_status is DomainResolutionStatus.VERIFIED)
            counts["pages"] += out.pages_stored
            counts["credits"] += out.credits_used
        return StageOutcome(counts=counts)

    async def signals(ctx: StageContext) -> StageOutcome:
        if ctx.dry_run:
            return skipped("dry run: signal research writes signals")
        ids = await asyncio.to_thread(pipeline_store.signal_candidates, ctx.limit)
        if not ids:
            return skipped("no verified company is waiting for signal research")
        service = SignalResearchService(registry.signal_research, SqlSignalStore(engine), settings)
        counts = {"companies": 0, "signals_created": 0, "failed": 0}
        for company_id in ids:
            counts["companies"] += 1
            try:
                run = await service.run(company_id)
            except (ProviderError, AppError):
                counts["failed"] += 1
                continue
            counts["signals_created"] += run.signals_created
        return StageOutcome(counts=counts)

    stages: dict[Stage, StageRunner] = {
        Stage.DISCOVERY: discovery,
        Stage.PRESCREEN: prescreen,
        Stage.WEBSITE: website,
        Stage.SIGNALS: signals,
    }
    stages.update(
        core_stages(
            settings,
            registry,
            qualifications=SqlQualificationStore(engine),
            enrichment=SqlEnrichmentStore(engine),
            analyses=SqlAnalysisStore(engine),
            outputs=SqlOutputStore(engine),
            qualification_candidates=pipeline_store.qualification_candidates,
            enrichment_candidates=pipeline_store.enrichment_candidates,
            analysis_candidates=pipeline_store.analysis_candidates,
            web_search=registry.web_search if settings.enrichment_web_search else None,
            claim=pipeline_store.claim_unowned,
        )
    )
    return stages
