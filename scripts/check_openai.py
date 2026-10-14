"""Check the OpenAI reasoning provider step by step (Phase 10).

Default (no flags): configuration + model availability only - one FREE metadata call
(``models.retrieve``), no generation, no cost. Needs OPENAI_API_KEY in .env.
--chat               one tiny paid generation ("Reply with exactly: OK")
--analysis-sample    one structured lead analysis of a SYNTHETIC demo company (no database,
                     nothing stored) + full grounding/catalog validation
Both paid steps need PROVIDER_MODE=live and ALLOW_PAID_PROVIDER_CALLS=true for THIS process.
The key is never printed. Spend is bounded by OPENAI_MAX_TOKENS and the per-run AI budget.

Examples:
  uv run python scripts/check_openai.py
  uv run python scripts/check_openai.py --chat --analysis-sample
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.demo import build_demo_world
from app.domain.analysis.bundle import AnalysisInput, build_bundle
from app.domain.analysis.catalog import default_catalog
from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.output import (
    AnalysisOutputError,
    LeadAnalysisOutput,
    parse_output,
    validate_output,
)
from app.domain.analysis.prompt import build_messages
from app.domain.enums import ContactStatus
from app.providers.contracts import ChatMessage, ChatRequest, ChatResult
from app.providers.openai import OpenAIAdapter

DEMO_SAMPLE = (
    "## Ecoride Fleet\nElectric three-wheeler fleet for last-mile deliveries; operations team "
    "schedules drivers, charging and maintenance across cities.\n### Arjun Rao - Co-Founder & CEO"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--chat", action="store_true", help="one tiny paid generation")
    p.add_argument("--analysis-sample", action="store_true", help="one synthetic analysis (paid)")
    return p.parse_args(argv)


def record(step: str, result: ChatResult) -> None:
    print(
        f"  {step}: model={result.model} tokens={result.usage.total_tokens} "
        f"(in {result.usage.prompt_tokens} / out {result.usage.completion_tokens}) "
        f"latency={result.latency_ms}ms attempts={result.attempts} "
        f"estimated_cost=${result.cost_usd}"
    )


async def main_async(args: argparse.Namespace, adapter: OpenAIAdapter | None = None) -> int:
    settings = Settings()
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    openai = adapter or OpenAIAdapter(settings)
    status = openai.status()
    prices = openai.prices()
    print(f"1. provider: openai | key configured: {'yes' if status.configured else 'NO'}")
    print(
        f"2. model: {settings.openai_model} | price per 1M tokens: "
        + (f"${prices[0]} in / ${prices[1]} out" if prices else "UNKNOWN (set OPENAI_PRICE_*)")
    )
    print(
        f"3. budgets: max_output_tokens={settings.openai_max_tokens}, "
        f"${settings.openai_max_estimated_cost_usd_per_run}/run, "
        f"${settings.openai_max_estimated_cost_usd_per_month}/month"
    )
    if not status.configured:
        print("REFUSING: OPENAI_API_KEY is not set in .env (docs/OPENAI_PROVIDER_NOTES.md)")
        return 2
    info = await openai.check_model()
    print(f"4. models.retrieve({info['id']}) -> available (free metadata call, no generation)")
    if not (args.chat or args.analysis_sample):
        return 0
    problems = []
    if settings.provider_mode != ProviderMode.LIVE:
        problems.append("PROVIDER_MODE=live (this process only)")
    if not settings.allow_paid_provider_calls:
        problems.append("ALLOW_PAID_PROVIDER_CALLS=true (this process only)")
    if problems:
        print("REFUSING paid steps; missing: " + "; ".join(problems))
        return 2
    total = 0
    if args.chat:
        result = await openai.complete(
            ChatRequest(
                messages=[ChatMessage(role="user", content="Reply with exactly: OK")],
                purpose="smoke",
            )
        )
        print(f"5. responses.create -> {result.content.strip()[:80]!r}")
        record("   usage", result)
        total += result.usage.total_tokens
    if args.analysis_sample:
        world = await build_demo_world(settings, {"Q11": DEMO_SAMPLE})
        cid = world.company_ids[0]
        decision = world.qualifications.latest_decision(cid)
        contact = next(
            c for c in world.enrichment.contacts(cid) if c.status is ContactStatus.VERIFIED
        )
        ctx = world.enrichment.load_company(cid)
        if decision is None or ctx.domain is None:  # pragma: no cover - demo invariant
            raise RuntimeError("demo company was not qualified")
        bundle = build_bundle(
            AnalysisInput(
                cid, ctx.name, ctx.domain, decision, contact,
                world.qualifications.load(cid).signals, list(ctx.pages),
            ),
            AnalysisConfig.from_settings(settings),
        )  # fmt: skip
        catalog = default_catalog()
        result = await openai.complete(
            ChatRequest(
                messages=[ChatMessage(**m) for m in build_messages(bundle, catalog)],
                purpose="lead_analysis_sample",
                response_schema=LeadAnalysisOutput.model_json_schema(),
                response_schema_name="lead_analysis",
            )
        )
        record("6. structured analysis (SYNTHETIC company)", result)
        try:
            out = parse_output(result.content)
            validate_output(out, bundle, catalog)
        except AnalysisOutputError as exc:
            print(f"   OUTPUT REJECTED ({exc.kind}): {exc.problems}")
            return 3
        print(
            f"   VALID: offer={out.recommended_offer_id} trigger={out.primary_trigger} "
            f"confidence={out.confidence} facts={len(out.observed_facts)} "
            f"cited={sorted(out.cited_ids())}"
        )
        print(f"   hook: {out.outreach_hook}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except AppError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
