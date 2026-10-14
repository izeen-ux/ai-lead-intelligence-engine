"""Unit-test fixtures for Phase 5 (kept apart from evaluation/ labeled cases and prod data)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums import DomainResolutionStatus, SignalType
from app.domain.qualification.clustering import SignalEvidence, cluster_signals
from app.domain.qualification.icp import IcpConfig
from app.domain.qualification.rules import CompanyFacts, HardRuleEvaluation, evaluate_hard_rules

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
ATTRIBUTED = ("news_rss item names the company", "matches verified-website context: leasing")
NAME_ONLY = ("news_rss item names the company",)


def facts(**kw: Any) -> CompanyFacts:
    base: dict[str, Any] = {
        "company_id": uuid.UUID(int=1),
        "name": "Acme Robotics",
        "domain": "acmerobotics.in",
        "domain_status": DomainResolutionStatus.VERIFIED,
        "country_code": None,
    }
    return CompanyFacts(**(base | kw))


def sig(
    title: str,
    signal_type: SignalType = SignalType.FUNDING,
    *,
    days_ago: float | None = 10,
    source: str | None = None,
    attributed: bool = True,
    confidence: float = 0.7,
    platform: str = "news:Inc42",
) -> SignalEvidence:
    src = source or title
    return SignalEvidence(
        signal_id=uuid.uuid4(),
        signal_type=signal_type,
        summary=title,
        evidence=title,
        source_id=uuid.uuid5(uuid.NAMESPACE_URL, src),
        url=f"https://news.example/{abs(hash(src))}",
        platform=platform,
        published_at=None if days_ago is None else NOW - timedelta(days=days_ago),
        confidence=confidence,
        notes=ATTRIBUTED if attributed else NAME_ONLY,
        channel="news_rss",
    )


def evaluate(
    signals: list[SignalEvidence], icp: IcpConfig | None = None, **fact_kw: Any
) -> HardRuleEvaluation:
    icp = icp or IcpConfig()
    f = facts(**fact_kw)
    clusters = cluster_signals(
        signals,
        company_name=f.name,
        title_similarity=icp.cluster_title_similarity,
        window_days=icp.cluster_window_days,
    )
    return evaluate_hard_rules(f, clusters, icp, NOW)
