"""Pre-screen: decide from DISCOVERY data alone whether a candidate deserves paid scraping.

Runs after discovery and BEFORE website intelligence, so Firecrawl credits are only spent on
candidates that could still become a lead. It sees nothing but what discovery already stored
(company name, headline, snippet, funding mention) and costs nothing:

  1. name rule     - the stored name is not a plausible company name (headline fragments)
  2. cap rule      - the headline states a round above the funding cap (same parser and cap as
                     the qualification hard rule; skipped for ICP_OVER_CAP_FUNDING_POLICY=ignore)
  3. stage rule    - the headline names a late round (Series B+), an IPO or a unicorn
     investor rule - the name or headline is about a VC firm / fund, not a startup
  4. Laya veto     - the local Laya model clearly says "not a prospect" (optional, free)

It can only say SKIP earlier; it never qualifies anyone. Every company that passes still goes
through website verification, signal research and the full qualification gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.core.config import OverCapFundingPolicy
from app.domain.enums import LayaDecisionKind, QualificationDimension
from app.domain.qualification.funding import extract_funding, format_inr
from app.domain.qualification.icp import IcpConfig
from app.providers.contracts import LayaQuestion, QualificationRequest, QualificationResult
from app.utils.funding_headlines import plausible_company_name

PRESCREEN_POLICY_VERSION = "ps-2026.10.06"
_INVESTOR_NAME = re.compile(
    r"\b(capital|ventures?|partners|vc|fund|funds|investments?|angels|holdings)\b|\bxv\b", re.I
)
_INVESTOR_NEWS = re.compile(
    r"\b(investment ceiling|new fund|closes? (?:its )?(?:\w+ )?fund|cohort|accelerator|"
    r"portfolio companies|limited partners)\b",
    re.I,
)
_LATE_STAGE = re.compile(
    r"\b(series\s+[b-h]|pre-ipo|ipo|unicorn|decacorn|listed\s+company|nasdaq|nyse)\b", re.I
)


class PrescreenDecision(StrEnum):
    SCRAPE = "scrape"  # green signal: website intelligence may spend credits
    SKIP = "skip"  # not worth paid scraping (reasons recorded)


@dataclass(frozen=True)
class DiscoveryFacts:
    """What discovery stored about a candidate (nothing fetched for the pre-screen)."""

    company_id: UUID
    name: str
    title: str = ""
    description: str = ""
    source: str = ""  # discovery source id, e.g. funding_news / linkedin_posts
    funding_mention: str | None = None

    @property
    def text(self) -> str:
        return f"{self.title} {self.description}".strip()


@dataclass(frozen=True)
class PrescreenOutcome:
    decision: PrescreenDecision
    reasons: tuple[str, ...]
    laya_fit: float | None = None


def rule_reasons(facts: DiscoveryFacts, icp: IcpConfig) -> list[str]:
    """Deterministic reasons to SKIP (empty = the rules have no objection)."""
    reasons: list[str] = []
    if not plausible_company_name(facts.name):
        reasons.append(f"name rule: {facts.name!r} is not a plausible company name")
    mention = extract_funding(facts.text)
    amount = mention.amount_inr(icp.usd_to_inr_rate) if mention and not mention.cumulative else None
    if (
        amount is not None
        and amount > icp.funding_cap_inr
        and icp.over_cap_funding_policy is not OverCapFundingPolicy.IGNORE
    ):
        reasons.append(
            f"cap rule: headline round {format_inr(amount)} exceeds cap "
            f"{format_inr(icp.funding_cap_inr)}"
        )
    if _INVESTOR_NAME.search(facts.name) or _INVESTOR_NEWS.search(facts.text):
        reasons.append("investor rule: the headline is about an investor / fund, not a startup")
    late = _LATE_STAGE.search(facts.text)
    if late is not None:
        reasons.append(f"stage rule: headline mentions '{late.group(0).lower()}'")
    return reasons


_QUESTION = LayaQuestion(
    dimension=QualificationDimension.COMPANY_FIT,
    kind=LayaDecisionKind.PROBABILITY,
    instructions=(
        "Based only on this news headline, could this be an early-stage Indian startup worth "
        "researching as a prospect for AI and automation services?"
    ),
    criteria={
        "false": "a large or long-established company, a government body, an investor, a "
        "person, a list article, or not a company at all",
        "true": "a small or early-stage company with concrete recent news such as a small "
        "funding round, a launch, an expansion or hiring",
    },
)


def laya_request(facts: DiscoveryFacts) -> QualificationRequest:
    """One cheap typed question, shaped like the qualification bundle (one event)."""
    kind = "funding" if (facts.funding_mention or "fund" in facts.source) else "expansion"
    return QualificationRequest(
        subject_id=facts.company_id,
        state={
            "company": facts.name,
            "stage": "prescreen (discovery data only)",
            "events": [
                {
                    "headline": facts.title[:200],
                    "summary": facts.description[:400],
                    "types": [kind],
                    "attributed": True,
                }
            ],
        },
        questions=[_QUESTION],
    )


def laya_fit(result: QualificationResult) -> float | None:
    out = result.output(QualificationDimension.COMPANY_FIT)
    if out is None or isinstance(out.value, str | bool):
        return None
    return float(out.value)


def decide(facts: DiscoveryFacts, icp: IcpConfig, fit: float | None = None) -> PrescreenOutcome:
    """Rules first; then Laya may only VETO (fit at or below the reject threshold)."""
    reasons = rule_reasons(facts, icp)
    if reasons:
        return PrescreenOutcome(PrescreenDecision.SKIP, tuple(reasons), fit)
    if fit is not None and fit <= icp.laya_reject_threshold:
        why = f"laya veto: P(prospect)={fit:.2f} <= {icp.laya_reject_threshold:.2f}"
        return PrescreenOutcome(PrescreenDecision.SKIP, (why,), fit)
    note = "rules passed" + (f"; laya P(prospect)={fit:.2f}" if fit is not None else "")
    return PrescreenOutcome(PrescreenDecision.SCRAPE, (note,), fit)
