"""Analysis configuration: model policy + budgets. The only place Phase 7 reads these from."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import Field

from app.core.config import ReasoningProviderChoice, Settings
from app.domain.models import DomainModel

ANALYSIS_POLICY_VERSION = "p7-2026.10.01"  # eligibility, grounding, validation rules
PROMPT_VERSION = "lead-analysis-v1"  # bump on any prompt change


class ModelPolicy(DomainModel):
    gateway: str = "openrouter"
    primary_model: str | None = None  # None -> first enabled model within the cost ceiling
    fallback_models: tuple[str, ...] = ()  # "gateway/model" entries, router fallback array
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=1, ge=0, le=5)
    max_tokens: int = Field(default=1500, ge=64, le=16000)  # router field: maxTokens
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_price_per_1m_tokens_usd: Decimal = Field(default=Decimal(0), ge=0)  # 0 = free only


class AnalysisConfig(DomainModel):
    model: ModelPolicy = ModelPolicy()
    max_analyses_per_run: int = Field(default=5, ge=1)
    max_tokens_per_run: int = Field(default=30000, ge=1000)
    max_cost_usd_per_run: Decimal = Field(default=Decimal(0), ge=0)
    max_cost_usd_per_month: Decimal = Field(default=Decimal(0), ge=0)
    require_verified_contact: bool = True
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # below -> REJECTED_OUTPUT
    max_events: int = Field(default=8, ge=1, le=20)
    max_page_chars: int = Field(default=1200, ge=200, le=6000)

    @classmethod
    def from_settings(cls, s: Settings) -> AnalysisConfig:
        if s.reasoning_provider is ReasoningProviderChoice.ANTHROPIC:
            return cls(
                model=ModelPolicy(
                    gateway="anthropic",
                    primary_model=s.anthropic_model,
                    timeout_seconds=s.anthropic_timeout_seconds,
                    max_retries=s.anthropic_max_retries,
                    max_tokens=min(s.anthropic_max_tokens, 16000),
                ),
                max_analyses_per_run=s.analysis_max_per_run,
                max_tokens_per_run=s.anthropic_max_tokens_per_run,
                max_cost_usd_per_run=s.anthropic_max_estimated_cost_usd_per_run,
                max_cost_usd_per_month=s.anthropic_max_estimated_cost_usd_per_month,
                require_verified_contact=s.analysis_require_verified_contact,
                min_confidence=s.analysis_min_confidence,
            )
        if s.reasoning_provider is ReasoningProviderChoice.OPENAI:
            return cls(
                model=ModelPolicy(
                    gateway="openai",
                    primary_model=s.openai_model,
                    timeout_seconds=s.openai_timeout_seconds,
                    max_retries=s.openai_max_retries,
                    max_tokens=s.openai_max_tokens,
                ),
                max_analyses_per_run=s.analysis_max_per_run,
                max_tokens_per_run=s.openai_max_tokens_per_run,
                max_cost_usd_per_run=s.openai_max_estimated_cost_usd_per_run,
                max_cost_usd_per_month=s.openai_max_estimated_cost_usd_per_month,
                require_verified_contact=s.analysis_require_verified_contact,
                min_confidence=s.analysis_min_confidence,
            )
        return cls(  # deprecated Techno-AI-Router (explicit opt-in)
            model=ModelPolicy(
                gateway=s.ai_router_gateway,
                primary_model=s.ai_router_default_model or None,
                fallback_models=tuple(
                    m.strip() for m in s.ai_router_fallback_models.split(",") if m.strip()
                ),
                timeout_seconds=s.ai_router_timeout_seconds,
                max_retries=s.ai_router_max_retries,
                max_tokens=s.ai_router_max_tokens,
                temperature=s.ai_router_temperature,
                max_price_per_1m_tokens_usd=s.ai_router_max_price_per_1m_tokens_usd,
            ),
            max_analyses_per_run=s.analysis_max_per_run,
            max_tokens_per_run=s.analysis_max_tokens_per_run,
            max_cost_usd_per_run=s.analysis_max_cost_usd_per_run,
            max_cost_usd_per_month=s.analysis_max_cost_usd_per_month,
            require_verified_contact=s.analysis_require_verified_contact,
            min_confidence=s.analysis_min_confidence,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy_version": ANALYSIS_POLICY_VERSION,
            "prompt_version": PROMPT_VERSION,
            **self.model_dump(mode="json"),
        }
