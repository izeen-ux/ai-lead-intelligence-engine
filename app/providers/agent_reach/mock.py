from __future__ import annotations

from datetime import UTC, datetime

from pydantic import HttpUrl

from app.domain.enums import ProviderName, ResearchChannel, SignalType
from app.domain.signals import DISABLED_CHANNELS
from app.providers.base import MockProviderBase
from app.providers.contracts import (
    EvidenceItem,
    ResearchQueryLog,
    SignalResearchRequest,
    SignalResearchResult,
)


class MockAgentReachProvider(MockProviderBase):
    """Deterministic offline research: two news signals + one video signal, within budget."""

    name = ProviderName.AGENT_REACH

    async def research(self, request: SignalResearchRequest) -> SignalResearchResult:
        self.calls.append("research")
        slug = request.company_name.lower().replace(" ", "-")
        name = request.company_name
        when = datetime(2026, 9, 21, tzinfo=UTC)
        items = [
            EvidenceItem(
                signal_type=SignalType.FUNDING,
                url=HttpUrl(f"https://news.example/{slug}/funding"),
                snippet=f"{name} raises Rs 45 Cr in seed funding",
                title=f"{name} raises Rs 45 Cr in seed funding",
                platform="news:News Example",
                channel=ResearchChannel.NEWS_RSS.value,
                query=f'"{name}"',
                published_at=when,
                confidence=0.7,
            ),
            EvidenceItem(
                signal_type=SignalType.HIRING,
                url=HttpUrl(f"https://news.example/{slug}/hiring"),
                snippet=f"{name} is hiring fleet operations managers",
                title=f"{name} is hiring fleet operations managers",
                platform="news:News Example",
                channel=ResearchChannel.NEWS_RSS.value,
                query=f'"{name}" hiring OR jobs',
                published_at=when,
                confidence=0.7,
            ),
            EvidenceItem(
                signal_type=SignalType.AI_INTEREST,
                url=HttpUrl(f"https://video.example/{slug}"),
                snippet=f"How {name} uses automation to scale",
                title=f"How {name} uses automation to scale",
                platform="youtube:Mock Channel",
                channel=ResearchChannel.YOUTUBE.value,
                query=name,
                confidence=0.45,
            ),
        ][: request.max_results]
        queries = [
            ResearchQueryLog(
                channel="news_rss",
                query=f'"{name}"',
                status="ok",
                items_returned=2,
                items_processed=2,
            ),
            ResearchQueryLog(
                channel="youtube", query=name, status="ok", items_returned=1, items_processed=1
            ),
        ][: request.max_queries]
        return SignalResearchResult(
            provider=self.name,
            items=items,
            queries=queries,
            items_processed=len(items),
            channels_used=[c.value for c in ResearchChannel],
            channels_disabled=dict(DISABLED_CHANNELS),
        )
