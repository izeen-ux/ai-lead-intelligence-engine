"""ICP configuration - the ONLY place qualification code reads business thresholds from.

Values come from ``Settings`` (environment / ``.env``); nothing is hard-coded in the service
or the rules. A snapshot of the config is persisted with every decision (reproducibility).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from app.core.config import LayaAmbiguousPolicy, OverCapFundingPolicy, Settings
from app.domain.models import DomainModel

# Bump when rules / clustering / policy semantics change (stored on every decision).
POLICY_VERSION = "p5-2026.10.06"


class IcpConfig(DomainModel):
    country_code: str = Field(default="IN", pattern=r"^[A-Z]{2}$")
    country_tlds: tuple[str, ...] = ("in",)
    funding_cap_inr: Decimal = Field(default=Decimal("50000000"), gt=0)  # ₹5 Cr per round
    usd_to_inr_rate: Decimal | None = Field(default=None, gt=0)  # None = no FX conversion
    funding_window_days: int = Field(default=90, ge=1)
    intent_window_days: int = Field(default=90, ge=1)
    over_cap_funding_policy: OverCapFundingPolicy = OverCapFundingPolicy.REVIEW
    min_signal_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    cluster_title_similarity: float = Field(default=0.5, gt=0.0, le=1.0)
    cluster_window_days: int = Field(default=7, ge=0)
    laya_qualify_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    laya_reject_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    laya_ambiguous_policy: LayaAmbiguousPolicy = LayaAmbiguousPolicy.QUALIFY
    llm_review_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)

    @field_validator("country_tlds")
    @classmethod
    def _tlds(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(t.strip().lower().lstrip(".") for t in v if t.strip())

    @classmethod
    def from_settings(cls, s: Settings) -> IcpConfig:
        return cls(
            country_code=s.icp_country_code,
            country_tlds=tuple(s.icp_country_tlds.split(",")),
            funding_cap_inr=s.funding_cap_inr,
            usd_to_inr_rate=s.icp_usd_to_inr_rate,
            funding_window_days=s.icp_funding_window_days,
            intent_window_days=s.icp_intent_window_days,
            over_cap_funding_policy=s.icp_over_cap_funding_policy,
            min_signal_confidence=s.icp_min_signal_confidence,
            cluster_title_similarity=s.cluster_title_similarity,
            cluster_window_days=s.cluster_window_days,
            laya_qualify_threshold=s.laya_qualify_threshold,
            laya_reject_threshold=s.laya_reject_threshold,
            laya_ambiguous_policy=s.icp_laya_ambiguous_policy,
            llm_review_min_confidence=s.llm_review_min_confidence,
        )

    def snapshot(self) -> dict[str, Any]:
        return {"policy_version": POLICY_VERSION, **self.model_dump(mode="json")}
