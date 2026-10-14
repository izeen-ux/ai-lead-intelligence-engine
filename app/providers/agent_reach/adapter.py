"""Live Agent-Reach adapter (Phase 4) - public, unauthenticated signal research.

Agent-Reach (Panniantong/Agent-Reach 1.5.0, commit a19a171) is not a library: it installs and
health-checks upstream tools and routes an agent to them per channel. This adapter implements
that routing for the channels our policy allows, calling the same upstream backends Agent-Reach
uses (verified with ``agent-reach doctor`` on 2026-09-29):

  news_rss  -> RSS channel (Agent-Reach: feedparser on RSS URLs) on the public Google News
               search feed ``https://news.google.com/rss/search?q=...`` - no key, no cookie.
  youtube   -> YouTube channel (Agent-Reach: yt-dlp), metadata-only search
               ``yt-dlp --flat-playlist --dump-json "ytsearchN:<query>"`` with cookies disabled.

Everything else (LinkedIn, Instagram, X, Reddit, Facebook, gh, Exa, ...) is disabled - see
``app.domain.signals.DISABLED_CHANNELS``. URLs on forbidden platforms are dropped even if a
feed returns them (``FORBIDDEN_RESEARCH_HOSTS``). Budgets: at most ``max_queries`` queries
and ``max_results`` processed items.
Per-query failures are logged and do not abort the run; the run fails only if every query did.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import ParseError

import httpx2
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pydantic import HttpUrl, ValidationError

from app.core.config import Settings
from app.core.errors import (
    ProviderChannelUnsupportedError,
    ProviderError,
    ProviderMalformedResponseError,
    ProviderPolicyBlockedError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.domain.enums import ProviderName, ResearchChannel
from app.domain.signals import DISABLED_CHANNELS, is_forbidden_research_url, score
from app.providers.base import LiveAdapterBase
from app.providers.contracts import (
    EvidenceItem,
    ProviderStatus,
    ResearchQueryLog,
    SignalResearchRequest,
    SignalResearchResult,
)

logger = get_logger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
AGENT_REACH_VERSION = "1.5.0 (a19a171)"

# (return code, stdout, stderr)
Runner = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]


async def _run_subprocess(args: list[str], limit_seconds: float) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limit_seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


@dataclass(frozen=True)
class _Finding:
    url: str
    title: str
    platform: str
    published_at: datetime | None


class AgentReachAdapter(LiveAdapterBase):
    name = ProviderName.AGENT_REACH
    implemented_in_phase = 4
    operational = True

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        runner: Runner | None = None,
    ) -> None:
        super().__init__(settings)
        self._transport = transport
        self._runner = runner or _run_subprocess

    # ------------------------------------------------------------------ status/policy
    def _ytdlp_available(self) -> bool:
        return self._runner is not _run_subprocess or bool(
            shutil.which(self._settings.agent_reach_ytdlp_path)
        )

    def available_channels(self) -> list[ResearchChannel]:
        channels = []
        for channel in self._settings.research_channels():
            if channel is ResearchChannel.YOUTUBE and not self._ytdlp_available():
                continue
            channels.append(channel)
        return channels

    def status(self) -> ProviderStatus:
        channels = self.available_channels()
        return ProviderStatus(
            name=self.name,
            mode="live",
            configured=bool(channels),
            operational=bool(channels),
            paid=False,
            detail="channels: " + (", ".join(c.value for c in channels) or "none available"),
        )

    def disabled_channels(self) -> dict[str, str]:
        disabled = dict(DISABLED_CHANNELS)
        for channel in ResearchChannel:
            if channel not in self._settings.research_channels():
                disabled[channel.value] = "not selected in AGENT_REACH_CHANNELS"
            elif channel is ResearchChannel.YOUTUBE and not self._ytdlp_available():
                disabled[channel.value] = "yt-dlp not found (install Agent-Reach as a uv tool)"
        return disabled

    # ------------------------------------------------------------------ planning
    @staticmethod
    def plan_queries(
        request: SignalResearchRequest, channels: list[ResearchChannel]
    ) -> list[tuple[ResearchChannel, str]]:
        name = request.company_name.strip()
        topics = request.questions or [
            "funding OR raises",
            "hiring OR jobs",
            "AI OR automation",
            "expansion OR launch OR partnership",
        ]
        news = [f'"{name}"'] + [f'"{name}" {topic}' for topic in topics]
        # D4 (Phase 5): never use a word of the company name as the disambiguating context
        # term - "Drivn drivn" returned only namesake/irrelevant videos.
        name_words = set(re.findall(r"[a-z0-9]+", name.lower()))
        context = next((t for t in request.context_terms if t.lower() not in name_words), "")
        youtube = [f"{name} {context}".strip()]
        per_channel = {ResearchChannel.NEWS_RSS: news, ResearchChannel.YOUTUBE: youtube}
        plan: list[tuple[ResearchChannel, str]] = []
        # interleave: first query of every channel, then the rest
        depth = max((len(per_channel[c]) for c in channels), default=0)
        for i in range(depth):
            for channel in channels:
                queries = per_channel[channel]
                if i < len(queries):
                    plan.append((channel, queries[i]))
        return plan[: request.max_queries]

    # ------------------------------------------------------------------ research
    async def research(self, request: SignalResearchRequest) -> SignalResearchResult:
        self._ensure_callable("research")
        channels = self.available_channels()
        result = SignalResearchResult(
            provider=self.name,
            channels_used=[c.value for c in channels],
            channels_disabled=self.disabled_channels(),
        )
        if not channels:
            raise ProviderChannelUnsupportedError(
                self.name.value, "no permitted Agent-Reach channel is available"
            )
        normalized = (request.normalized_name or request.company_name).lower()
        seen: set[tuple[str, str]] = set()
        errors: list[ProviderError] = []

        for channel, query in self.plan_queries(request, channels):
            remaining = request.max_results - result.items_processed
            if remaining <= 0:
                break
            log = ResearchQueryLog(channel=channel.value, query=query, status="ok")
            try:
                findings = await self._search(channel, query, request.country, remaining)
            except ProviderError as exc:
                errors.append(exc)
                log.status = (
                    "policy_blocked"
                    if isinstance(exc, ProviderPolicyBlockedError)
                    else (
                        "unsupported"
                        if isinstance(exc, ProviderChannelUnsupportedError)
                        else "error"
                    )
                )
                log.error_code, log.error_message = exc.code, exc.message
                result.queries.append(log)
                logger.warning(
                    "agent-reach query failed",
                    extra={"channel": channel.value, "error_code": exc.code},
                )
                continue
            log.items_returned = len(findings)
            if not findings:
                log.status = "empty"
            for finding in findings[:remaining]:
                log.items_processed += 1
                result.items_processed += 1
                if is_forbidden_research_url(finding.url):  # policy: LinkedIn/IG/X/... dropped
                    result.items_dropped += 1
                    continue
                scored = score(
                    finding.title,
                    channel=channel,
                    normalized_name=normalized,
                    domain=request.domain,
                    context_terms=request.context_terms,
                )
                if scored is None:
                    result.items_dropped += 1
                    continue
                for signal_type in scored.signal_types:
                    key = (finding.url, signal_type.value)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        result.items.append(
                            EvidenceItem(
                                signal_type=signal_type,
                                url=HttpUrl(finding.url),
                                snippet=finding.title,
                                title=finding.title,
                                platform=finding.platform,
                                channel=channel.value,
                                query=query,
                                published_at=finding.published_at,
                                confidence=scored.confidence,
                                evidence_notes=scored.evidence_notes,
                            )
                        )
                    except ValidationError:
                        result.items_dropped += 1
            result.queries.append(log)
            logger.info(
                "agent-reach query done",
                extra={
                    "channel": channel.value,
                    "returned": log.items_returned,
                    "processed": log.items_processed,
                },
            )

        if errors and len(errors) == len(result.queries):
            first = errors[0]
            raise type(first)(
                self.name.value,
                f"all {len(errors)} research queries failed: {first.message}",
                details={"queries": [q.model_dump() for q in result.queries]},
            )
        return result

    async def _search(
        self, channel: ResearchChannel, query: str, country: str | None, limit: int
    ) -> list[_Finding]:
        if channel is ResearchChannel.NEWS_RSS:
            return await self._news_rss(query, country, limit)
        if channel is ResearchChannel.YOUTUBE:
            return await self._youtube(query, limit)
        raise ProviderChannelUnsupportedError(self.name.value, f"channel {channel} not supported")

    # ------------------------------------------------------------------ news_rss backend
    async def _news_rss(self, query: str, country: str | None, limit: int) -> list[_Finding]:
        gl = country or "IN"
        params = {"q": query, "hl": f"en-{gl}", "gl": gl, "ceid": f"{gl}:en"}
        try:
            async with httpx2.AsyncClient(
                timeout=self._settings.agent_reach_timeout_seconds,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.get(GOOGLE_NEWS_RSS, params=params)
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(self.name.value, "news RSS request timed out") from None
        except httpx2.TransportError as exc:
            raise ProviderUnavailableError(
                self.name.value, f"news RSS unreachable ({type(exc).__name__})"
            ) from None
        if response.status_code == 429:
            raise ProviderRateLimitedError(self.name.value, "news RSS rate-limited (429)")
        if response.status_code in (401, 403):
            raise ProviderPolicyBlockedError(
                self.name.value, f"news RSS refused unauthenticated access ({response.status_code})"
            )
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                self.name.value, f"news RSS error ({response.status_code})"
            )
        return self._parse_rss(response.content, limit)

    def _parse_rss(self, payload: bytes, limit: int) -> list[_Finding]:
        try:
            root = SafeET.fromstring(payload)
        except (ParseError, DefusedXmlException) as exc:
            raise ProviderMalformedResponseError(
                self.name.value, f"news RSS payload is not valid/safe XML ({type(exc).__name__})"
            ) from None
        channel = root.find("channel")
        if channel is None:
            raise ProviderMalformedResponseError(self.name.value, "news RSS has no <channel>")
        findings: list[_Finding] = []
        for item in channel.findall("item")[:limit]:
            link = (item.findtext("link") or "").strip()
            raw_title = (item.findtext("title") or "").strip()
            source = item.find("source")
            publisher = (source.text or "").strip() if source is not None else ""
            if not link or not raw_title:
                continue
            title = raw_title
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3].strip()
            findings.append(
                _Finding(
                    url=link,
                    title=title,
                    platform=f"news:{publisher}" if publisher else "news",
                    published_at=_parse_rfc822(item.findtext("pubDate")),
                )
            )
        return findings

    # ------------------------------------------------------------------ youtube backend
    async def _youtube(self, query: str, limit: int) -> list[_Finding]:
        args = [
            self._settings.agent_reach_ytdlp_path,
            "--flat-playlist",
            "--dump-json",
            "--no-warnings",
            "--ignore-config",
            "--no-cookies",
            "--no-cookies-from-browser",
            "--no-cache-dir",
            f"ytsearch{min(limit, 10)}:{query}",
        ]
        try:
            code, out, err = await self._runner(
                args, float(self._settings.agent_reach_timeout_seconds)
            )
        except FileNotFoundError:
            raise ProviderChannelUnsupportedError(
                self.name.value, "yt-dlp not installed (Agent-Reach YouTube backend)"
            ) from None
        except TimeoutError:
            raise ProviderTimeoutError(self.name.value, "yt-dlp search timed out") from None
        if code != 0:
            lowered = err.lower()
            if "sign in" in lowered or "cookies" in lowered or "login" in lowered:
                raise ProviderPolicyBlockedError(
                    self.name.value, "YouTube demanded sign-in/cookies - refused by policy"
                )
            if "429" in lowered or "too many requests" in lowered:
                raise ProviderRateLimitedError(self.name.value, "YouTube rate-limited (429)")
            raise ProviderUnavailableError(self.name.value, f"yt-dlp failed (exit {code})")
        findings: list[_Finding] = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data: dict[str, Any] = json.loads(line)
            except ValueError:
                raise ProviderMalformedResponseError(
                    self.name.value, "yt-dlp returned invalid JSON"
                ) from None
            url, title = data.get("url") or data.get("webpage_url"), data.get("title")
            if not url or not title:
                continue
            channel_name = data.get("channel") or data.get("uploader") or ""
            findings.append(
                _Finding(
                    url=str(url),
                    title=str(title),
                    platform=f"youtube:{channel_name}" if channel_name else "youtube",
                    published_at=_parse_timestamp(data.get("timestamp"), data.get("upload_date")),
                )
            )
        return findings[:limit]


def _parse_rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_timestamp(timestamp: Any, upload_date: Any) -> datetime | None:
    if isinstance(timestamp, int | float):
        return datetime.fromtimestamp(timestamp, tz=UTC)
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
    return None


__all__ = ["AGENT_REACH_VERSION", "AgentReachAdapter", "Runner"]
