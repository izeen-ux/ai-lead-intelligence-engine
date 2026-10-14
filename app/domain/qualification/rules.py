"""Hard ICP rules - deterministic, configuration-driven, never overridden downstream.

Every check yields PASS / FAIL / UNKNOWN / NOT_APPLICABLE with a human-readable reason and
the signal ids it relied on. UNKNOWN (missing or conflicting data) is never a pass.

Checks:
  identity   - company VERIFIED with a canonical domain (blocking).
  geography  - ICP country: explicit ``country_code``; else ccTLD of the verified domain;
               else attributed evidence naming the country / INR amounts. Otherwise UNKNOWN.
  funding    - path A: an attributed funding EVENT inside the window whose ROUND amount is
               known and <= cap (lifetime/cumulative totals do not count).
  intent     - path B: an attributed, confident intent EVENT inside the window. Hiring-only
               events are too weak on their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.core.config import OverCapFundingPolicy
from app.domain.enums import (
    DomainResolutionStatus,
    QualificationPath,
    RuleOutcome,
    SignalType,
)
from app.domain.qualification.clustering import EventCluster
from app.domain.qualification.funding import format_inr
from app.domain.qualification.icp import IcpConfig

INTENT_TYPES: frozenset[SignalType] = frozenset(
    {
        SignalType.AI_INTEREST,
        SignalType.EXPANSION,
        SignalType.PRODUCT_CUSTOMER,
        SignalType.TECHNOLOGY,
        SignalType.PAIN_POINT,
        SignalType.HIRING,
    }
)
# Weak alone: a job post does not show growth unless it comes with another intent cue.
WEAK_ALONE: frozenset[SignalType] = frozenset({SignalType.HIRING})
_CONFLICT_TOLERANCE = Decimal("0.10")  # round amounts within 10 % are the same figure
_COUNTRY_WORDS = {"IN": ("india", "indian", "bengaluru", "bangalore", "mumbai", "delhi", "pune")}


@dataclass(frozen=True)
class CompanyFacts:
    company_id: UUID
    name: str
    domain: str | None
    domain_status: DomainResolutionStatus
    country_code: str | None = None

    @property
    def verified(self) -> bool:
        return self.domain_status is DomainResolutionStatus.VERIFIED and bool(self.domain)


@dataclass(frozen=True)
class RuleCheck:
    rule: str
    outcome: RuleOutcome
    reason: str
    signal_ids: tuple[UUID, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "signal_ids": [str(i) for i in self.signal_ids],
        }


@dataclass
class HardRuleEvaluation:
    identity: RuleCheck
    geography: RuleCheck
    funding: RuleCheck
    intent: RuleCheck
    over_cap_recent: bool = False  # an attributed round above the cap inside the window
    qualifying_clusters: list[EventCluster] = field(default_factory=list)

    @property
    def checks(self) -> list[RuleCheck]:
        return [self.identity, self.geography, self.funding, self.intent]

    @property
    def path(self) -> QualificationPath:
        f = self.funding.outcome is RuleOutcome.PASS
        i = self.intent.outcome is RuleOutcome.PASS
        if f and i:
            return QualificationPath.BOTH
        if f:
            return QualificationPath.FUNDING
        if i:
            return QualificationPath.INTENT
        return QualificationPath.NONE

    @property
    def blocking_failure(self) -> RuleCheck | None:
        """A failure no later stage may override (identity / geography)."""
        for c in (self.identity, self.geography):
            if c.outcome is RuleOutcome.FAIL:
                return c
        return None

    @property
    def any_path_unknown(self) -> bool:
        return RuleOutcome.UNKNOWN in (self.funding.outcome, self.intent.outcome)

    @property
    def passed(self) -> bool:
        """Hard ICP passes: identity + geography PASS and at least one path PASS."""
        return (
            self.identity.outcome is RuleOutcome.PASS
            and self.geography.outcome is RuleOutcome.PASS
            and self.path is not QualificationPath.NONE
        )

    def qualified_allowed(self, policy: OverCapFundingPolicy) -> bool:
        """Can ANY downstream stage (Laya / LLM) produce QUALIFIED? Hard gate for overrides."""
        if not self.passed:
            return False
        return not (
            self.path is QualificationPath.INTENT
            and self.over_cap_recent
            and policy is not OverCapFundingPolicy.IGNORE
        )


def _in_window(when: datetime | None, now: datetime, days: int) -> bool | None:
    if when is None:
        return None
    return now - timedelta(days=days) <= when <= now + timedelta(days=1)


def _ids(clusters: list[EventCluster]) -> tuple[UUID, ...]:
    return tuple(i for c in clusters for i in c.signal_ids)


def check_identity(facts: CompanyFacts) -> RuleCheck:
    if facts.verified:
        return RuleCheck("identity", RuleOutcome.PASS, f"verified domain {facts.domain}")
    return RuleCheck(
        "identity",
        RuleOutcome.FAIL,
        f"company identity not verified (domain_status={facts.domain_status.value}); "
        "unverified companies can never be QUALIFIED",
    )


def check_geography(facts: CompanyFacts, clusters: list[EventCluster], icp: IcpConfig) -> RuleCheck:
    want = icp.country_code
    if facts.country_code:
        if facts.country_code.upper() == want:
            return RuleCheck("geography", RuleOutcome.PASS, f"country_code={want}")
        return RuleCheck(
            "geography",
            RuleOutcome.FAIL,
            f"country_code={facts.country_code.upper()} is outside the ICP ({want})",
        )
    if facts.domain and any(facts.domain.endswith("." + t) for t in icp.country_tlds):
        return RuleCheck(
            "geography", RuleOutcome.PASS, f"inferred {want} from ccTLD of {facts.domain}"
        )
    words = _COUNTRY_WORDS.get(want, ())
    supporting = [
        c
        for c in clusters
        if c.attributed
        and (
            any(w in s.text.lower() for s in c.signals for w in words)
            or (want == "IN" and any(m.currency == "INR" for m in c.funding))
        )
    ]
    if supporting:
        return RuleCheck(
            "geography",
            RuleOutcome.PASS,
            f"inferred {want} from attributed evidence ({len(supporting)} event(s))",
            _ids(supporting),
        )
    return RuleCheck(
        "geography", RuleOutcome.UNKNOWN, f"country not known and not inferable as {want}"
    )


def _round_amounts(cluster: EventCluster, icp: IcpConfig) -> list[Decimal]:
    return [
        a
        for m in cluster.funding
        if not m.cumulative and (a := m.amount_inr(icp.usd_to_inr_rate)) is not None
    ]


ATTRIBUTION_UNKNOWN = "funding evidence not attributable beyond a name match"


def _conflicting(amounts: list[Decimal]) -> bool:
    lo, hi = min(amounts), max(amounts)
    return hi > lo * (1 + _CONFLICT_TOLERANCE)


def check_funding(
    clusters: list[EventCluster], icp: IcpConfig, now: datetime
) -> tuple[RuleCheck, bool, list[EventCluster]]:
    """Returns (check, over_cap_recent, passing clusters)."""
    funding = [c for c in clusters if SignalType.FUNDING in c.types]
    if not funding:
        return RuleCheck("funding", RuleOutcome.NOT_APPLICABLE, "no funding evidence"), False, []
    passing: list[EventCluster] = []
    over_cap: list[EventCluster] = []
    unknown: list[tuple[EventCluster, str]] = []
    stale: list[EventCluster] = []
    cap = icp.funding_cap_inr
    for c in funding:
        inside = _in_window(c.event_date, now, icp.funding_window_days)
        if inside is None:
            unknown.append((c, "funding date missing"))
            continue
        if not inside:
            stale.append(c)
            continue
        if not c.attributed:
            unknown.append((c, ATTRIBUTION_UNKNOWN))
            continue
        amounts = _round_amounts(c, icp)
        if not amounts:
            why = (
                "only a cumulative/total amount reported"
                if any(m.cumulative for m in c.funding)
                else "round amount in a currency without a configured FX rate"
                if c.funding
                else "round amount missing"
            )
            unknown.append((c, why))
            continue
        if _conflicting(amounts):
            unknown.append((c, "publishers report conflicting round amounts"))
            continue
        (over_cap if max(amounts) > cap else passing).append(c)

    window = f"{icp.funding_window_days}d"
    cap_txt = format_inr(cap)
    if passing and over_cap:
        return (
            RuleCheck(
                "funding",
                RuleOutcome.UNKNOWN,
                f"conflicting recent rounds: one <= {cap_txt}, another above it",
                _ids(passing + over_cap),
            ),
            True,
            [],
        )
    if passing:
        best = passing[0]
        amt = max(_round_amounts(best, icp))
        return (
            RuleCheck(
                "funding",
                RuleOutcome.PASS,
                f"round {format_inr(amt)} <= cap {cap_txt} within {window} ({best.headline!r})",
                _ids(passing),
            ),
            False,
            passing,
        )
    if over_cap:
        amt = max(_round_amounts(over_cap[0], icp))
        return (
            RuleCheck(
                "funding",
                RuleOutcome.FAIL,
                f"recent round {format_inr(amt)} exceeds cap {cap_txt}",
                _ids(over_cap),
            ),
            True,
            [],
        )
    if unknown:
        reasons = sorted({why for _, why in unknown})
        return (
            RuleCheck(
                "funding",
                RuleOutcome.UNKNOWN,
                "funding evidence cannot be confirmed: " + "; ".join(reasons),
                _ids([c for c, _ in unknown]),
            ),
            False,
            [],
        )
    return (
        RuleCheck(
            "funding",
            RuleOutcome.FAIL,
            f"funding evidence older than {window}",
            _ids(stale),
        ),
        False,
        [],
    )


def check_intent(
    clusters: list[EventCluster], icp: IcpConfig, now: datetime
) -> tuple[RuleCheck, list[EventCluster]]:
    intent = [c for c in clusters if c.types & INTENT_TYPES]
    if not intent:
        return RuleCheck("intent", RuleOutcome.NOT_APPLICABLE, "no intent evidence"), []
    recent = [c for c in intent if _in_window(c.event_date, now, icp.intent_window_days)]
    if not recent:
        return (
            RuleCheck(
                "intent",
                RuleOutcome.FAIL,
                f"intent evidence missing a date or older than {icp.intent_window_days}d",
                _ids(intent),
            ),
            [],
        )
    strong = [
        c
        for c in recent
        if c.attributed
        and c.confidence >= icp.min_signal_confidence
        and (c.types & INTENT_TYPES) - WEAK_ALONE
    ]
    if strong:
        kinds = sorted({t.value for c in strong for t in c.types & INTENT_TYPES})
        return (
            RuleCheck(
                "intent",
                RuleOutcome.PASS,
                f"{len(strong)} attributed recent intent event(s): {', '.join(kinds)}",
                _ids(strong),
            ),
            strong,
        )
    return (
        RuleCheck(
            "intent",
            RuleOutcome.FAIL,
            "recent intent evidence is weak (name-only match, low confidence or hiring-only)",
            _ids(recent),
        ),
        [],
    )


def evaluate_hard_rules(
    facts: CompanyFacts, clusters: list[EventCluster], icp: IcpConfig, now: datetime
) -> HardRuleEvaluation:
    funding, over_cap, funding_clusters = check_funding(clusters, icp, now)
    intent, intent_clusters = check_intent(clusters, icp, now)
    qualifying = {c.cluster_id: c for c in funding_clusters + intent_clusters}
    return HardRuleEvaluation(
        identity=check_identity(facts),
        geography=check_geography(facts, clusters, icp),
        funding=funding,
        intent=intent,
        over_cap_recent=over_cap,
        qualifying_clusters=list(qualifying.values()),
    )
