"""Enrichment budget ledger: per lead, per run, per month, sidecar calls - checked BEFORE every
paid operation with its worst-case credit cost; recorded AFTER with the actual cost."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.enrichment.config import EnrichmentConfig
from app.domain.enums import PhoneStatus, ProviderName

PAID_VENDORS = (ProviderName.HUNTER, ProviderName.APOLLO)


@dataclass
class BudgetLedger:
    config: EnrichmentConfig
    month_credits: dict[ProviderName, Decimal] = field(default_factory=dict)  # before this run
    run_credits: dict[ProviderName, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    lead_attempts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sidecar_calls: int = 0
    phone_lookups: int = 0  # contacts asked for a phone in this run

    def _price(self, provider: ProviderName) -> Decimal:
        return (
            self.config.hunter_credit_usd
            if provider is ProviderName.HUNTER
            else self.config.apollo_credit_usd
        )

    def _run_limit(self, provider: ProviderName) -> int:
        return (
            self.config.max_hunter_credits_per_run
            if provider is ProviderName.HUNTER
            else self.config.max_apollo_credits_per_run
        )

    def _month_limit(self, provider: ProviderName) -> int:
        return (
            self.config.max_hunter_credits_per_month
            if provider is ProviderName.HUNTER
            else self.config.max_apollo_credits_per_month
        )

    @property
    def run_usd(self) -> Decimal:
        return sum((self._price(p) * c for p, c in self.run_credits.items()), Decimal(0))

    @property
    def month_usd(self) -> Decimal:
        return sum((self._price(p) * c for p, c in self.month_credits.items()), Decimal(0))

    def blocked_reason(
        self, provider: ProviderName, person_key: str, credits: Decimal = Decimal(1)
    ) -> str | None:
        """Why ``provider`` may NOT be called now (None = allowed)."""
        if self.lead_attempts[person_key] >= self.config.max_attempts_per_lead:
            return "per-lead attempt limit reached"
        if self.sidecar_calls >= self.config.max_sidecar_calls_per_run:
            return "sidecar call limit (run) reached"
        run = self.run_credits[provider]
        if run + credits > self._run_limit(provider):
            return f"{provider.value} run credit limit reached"
        month = self.month_credits.get(provider, Decimal(0))
        if month + run + credits > self._month_limit(provider):
            return f"{provider.value} monthly credit limit reached"
        cost = self._price(provider) * credits
        if self.run_usd + cost > self.config.max_cost_usd_per_run:
            return "run cost budget reached"
        if self.month_usd + self.run_usd + cost > self.config.max_cost_usd_per_month:
            return "monthly cost budget reached"
        return None

    def allowed_waterfall(self, person_key: str) -> list[ProviderName]:
        """Providers the next waterfall call may use (each assumed to cost 1 credit)."""
        allowed = [p for p in PAID_VENDORS if self.blocked_reason(p, person_key) is None]
        room = self.config.max_attempts_per_lead - self.lead_attempts[person_key]
        return allowed[: max(room, 0)]

    def record(
        self,
        provider: ProviderName | None,
        person_key: str,
        credits: Decimal,
        *,
        attempts: int = 1,
        sidecar_call: bool = False,
    ) -> None:
        if provider is not None:
            self.run_credits[provider] += credits
        self.lead_attempts[person_key] += attempts
        if sidecar_call:
            self.sidecar_calls += 1

    def phone_status(self) -> PhoneStatus | None:
        """Why a phone may NOT be requested for the next contact (None = request it)."""
        if not self.config.phone_enabled:
            return PhoneStatus.NOT_REQUESTED
        if self.phone_lookups >= self.config.max_phone_lookups_per_run:
            return PhoneStatus.BUDGET_EXHAUSTED
        return None

    def exhausted(self) -> bool:
        """No paid vendor can be called at all any more in this run."""
        return all(self.blocked_reason(p, "__probe__") for p in PAID_VENDORS)

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_credits": {p.value: str(c) for p, c in self.run_credits.items()},
            "month_credits_before_run": {p.value: str(c) for p, c in self.month_credits.items()},
            "sidecar_calls": self.sidecar_calls,
            "phone_lookups": self.phone_lookups,
            "estimated_run_cost_usd": str(self.run_usd),
        }
