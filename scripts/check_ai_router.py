"""DEPRECATED (Phase 10): legacy check for the Techno-AI-Router, which is no longer the active AI
provider. Use scripts/check_openai.py. Kept for REASONING_PROVIDER=ai_router only.

Check the Techno-AI-Router step by step (Phase 7 router-first workflow).

Default (no flags): discovery only - NO key needed, no cost:
  GET /health, GET /v1/providers?enabled=true, GET /v1/models?provider=<gateway>&modality=llm,
  then shows which model the model policy would use (enabled + within the price ceiling).
--chat               one minimal authenticated chat ("Reply with exactly: OK") + schema check
--analysis-sample    one structured lead analysis of a SYNTHETIC demo company (no database,
                     nothing stored) + full grounding/catalog validation
Both need PROVIDER_MODE=live, ALLOW_PAID_PROVIDER_CALLS=true and AI_ROUTER_API_KEY in .env.
The key is never printed. Free models only by default (AI_ROUTER_MAX_PRICE_PER_1M_TOKENS_USD=0).

Examples:
  uv run python scripts/check_ai_router.py
  uv run python scripts/check_ai_router.py --chat --analysis-sample
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.demo import build_demo_world
from app.domain.analysis.bundle import AnalysisInput, build_bundle
from app.domain.analysis.catalog import default_catalog
from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.output import AnalysisOutputError, parse_output, validate_output
from app.domain.analysis.prompt import build_messages
from app.domain.enums import ContactStatus
from app.providers.ai_router import AiRouterAdapter
from app.providers.contracts import ChatMessage, ChatRequest, ChatResult

DEFAULT_URL = "https://techno-ai-router.vercel.app"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--chat", action="store_true", help="one minimal authenticated chat")
    p.add_argument(
        "--analysis-sample", action="store_true", help="one synthetic structured analysis"
    )
    return p.parse_args(argv)


def record(step: str, result: ChatResult) -> None:
    print(
        f"  {step}: model={result.model} gateway={result.gateway} "
        f"provider={result.upstream_provider} "
        f"finish={result.finish_reason} tokens={result.usage.total_tokens} "
        f"(in {result.usage.prompt_tokens} / out {result.usage.completion_tokens}) "
        f"latency={result.latency_ms}ms attempts={result.attempts} cost=${result.cost_usd}"
    )


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.ai_router_base_url:
        settings = settings.model_copy(update={"ai_router_base_url": DEFAULT_URL})
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    router = AiRouterAdapter(settings)
    print(f"router: {settings.ai_router_base_url}")
    health = await router.health()
    print(f"1. GET /health -> {json.dumps(health)}")
    llm = [p for p in await router.list_providers() if p.get("modality") == "llm"]
    print(f"2. GET /v1/providers?enabled=true -> llm providers: {[p.get('provider') for p in llm]}")
    models = await router.list_models()
    free = [m for m in models if m.enabled and m.is_free]
    print(
        f"3. GET /v1/models?provider={settings.ai_router_gateway}&modality=llm -> "
        f"{len(models)} models, "
        f"{len(free)} enabled free"
    )
    for m in free:
        print(f"     {m.model}  {m.price}  tags={m.tags}")
    chosen = await router.candidate_models()
    print(
        f"4. model policy -> primary {chosen[0].model}; fallbacks {[m.model for m in chosen[1:]]}"
    )
    print(f"   key configured: {'yes' if settings.provider_has_credentials(router.name) else 'NO'}")
    if not (args.chat or args.analysis_sample):
        return 0
    problems = []
    if settings.provider_mode != ProviderMode.LIVE:
        problems.append("PROVIDER_MODE=live (this process only)")
    if not settings.allow_paid_provider_calls:
        problems.append("ALLOW_PAID_PROVIDER_CALLS=true (this process only)")
    if not settings.provider_has_credentials(router.name):
        problems.append("AI_ROUTER_API_KEY in .env (see docs/TECHNO_AI_ROUTER_SETUP_GUIDE.md)")
    if problems:
        print("REFUSING authenticated steps; missing: " + "; ".join(problems))
        return 2
    if args.chat:
        result = await router.complete(
            ChatRequest(
                messages=[ChatMessage(role="user", content="Reply with exactly: OK")],
                purpose="smoke",
            )
        )
        print(f"5. POST /v1/chat/completions -> {result.content.strip()[:80]!r}")
        record("   usage", result)
        print(
            "6. response schema: choices[0].message.content, finish_reason, usage, x_router -> OK"
        )
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
                cid,
                ctx.name,
                ctx.domain,
                decision,
                contact,
                world.qualifications.load(cid).signals,
                list(ctx.pages),
            ),
            AnalysisConfig.from_settings(settings),
        )
        catalog = default_catalog()
        msgs = [ChatMessage(**m) for m in build_messages(bundle, catalog)]
        result = await router.complete(ChatRequest(messages=msgs, purpose="lead_analysis_sample"))
        record("7. structured analysis (synthetic company)", result)
        try:
            out = parse_output(result.content)
            validate_output(out, bundle, catalog)
        except AnalysisOutputError as exc:
            print(f"   OUTPUT REJECTED ({exc.kind}): {exc.problems}")
            print(f"   raw excerpt: {result.content[:300]!r}")
            return 3
        print(
            f"   VALID: offer={out.recommended_offer_id} trigger={out.primary_trigger} "
            f"confidence={out.confidence} facts={len(out.observed_facts)} "
            f"cited={sorted(out.cited_ids())}"
        )
        print(f"   hook: {out.outreach_hook}")
    return 0


DEMO_SAMPLE = (
    "## Ecoride Fleet\nElectric three-wheeler fleet for last-mile deliveries; operations team "
    "schedules drivers, charging and maintenance across cities.\n### Arjun Rao - Co-Founder & CEO"
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except AppError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
