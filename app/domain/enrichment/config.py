"""Enrichment configuration - the only place Phase 6 code reads thresholds/budgets from."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import Field

from app.core.config import Settings
from app.domain.models import DomainModel

# Bump when identity, merge, verification or budget semantics change.
ENRICHMENT_POLICY_VERSION = "p6-2026.10.05"


class EnrichmentConfig(DomainModel):
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    early_exit_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    accept_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    cache_ttl_days: int = Field(default=30, ge=1)
    verification_ttl_days: int = Field(default=30, ge=1)
    role_priority: tuple[str, ...] = (
        "founder",
        "co-founder",
        "ceo",
        "chief executive",
        "managing director",
        "cto",
        "owner",
        "coo",
        "president",
    )
    max_persons_per_company: int = Field(default=1, ge=1)
    people_search: str = "none"  # none | hunter
    max_attempts_per_lead: int = Field(default=4, ge=1)
    max_sidecar_calls_per_run: int = Field(default=10, ge=1)
    web_search: bool = True
    max_web_searches_per_run: int = Field(default=5, ge=0)
    phone_enabled: bool = False
    max_phone_lookups_per_run: int = Field(default=5, ge=0)
    max_hunter_credits_per_run: int = Field(default=5, ge=0)
    max_apollo_credits_per_run: int = Field(default=3, ge=0)
    max_hunter_credits_per_month: int = Field(default=50, ge=0)
    max_apollo_credits_per_month: int = Field(default=20, ge=0)
    hunter_credit_usd: Decimal = Field(default=Decimal("0.03"), ge=0)
    apollo_credit_usd: Decimal = Field(default=Decimal("0.05"), ge=0)
    max_cost_usd_per_run: Decimal = Field(default=Decimal("0.50"), ge=0)
    max_cost_usd_per_month: Decimal = Field(default=Decimal("5.00"), ge=0)

    @classmethod
    def from_settings(cls, s: Settings) -> EnrichmentConfig:
        return cls(
            min_confidence=s.enrichment_min_confidence,
            early_exit_confidence=s.enrichment_early_exit_confidence,
            accept_confidence=s.enrichment_accept_confidence,
            cache_ttl_days=s.enrichment_cache_ttl_days,
            verification_ttl_days=s.enrichment_verification_ttl_days,
            role_priority=tuple(
                r.strip().lower() for r in s.enrichment_role_priority.split(",") if r.strip()
            ),
            max_persons_per_company=s.enrichment_max_persons_per_company,
            people_search=s.enrichment_people_search,
            max_attempts_per_lead=s.enrichment_max_attempts_per_lead,
            max_sidecar_calls_per_run=s.enrichment_max_sidecar_calls_per_run,
            web_search=s.enrichment_web_search,
            max_web_searches_per_run=s.enrichment_max_web_searches_per_run,
            phone_enabled=s.enrichment_phone_enabled,
            max_phone_lookups_per_run=s.enrichment_max_phone_lookups_per_run,
            max_hunter_credits_per_run=s.enrichment_max_hunter_credits_per_run,
            max_apollo_credits_per_run=s.enrichment_max_apollo_credits_per_run,
            max_hunter_credits_per_month=s.enrichment_max_hunter_credits_per_month,
            max_apollo_credits_per_month=s.enrichment_max_apollo_credits_per_month,
            hunter_credit_usd=s.enrichment_hunter_credit_usd,
            apollo_credit_usd=s.enrichment_apollo_credit_usd,
            max_cost_usd_per_run=s.enrichment_max_cost_usd_per_run,
            max_cost_usd_per_month=s.enrichment_max_cost_usd_per_month,
        )

    def snapshot(self) -> dict[str, Any]:
        return {"policy_version": ENRICHMENT_POLICY_VERSION, **self.model_dump(mode="json")}
