"""Human REVIEW workflow (Phase 8).

Every action creates a NEW immutable qualification decision through the same domain rules as
Phase 5 (hard ICP rules are re-evaluated NOW, under the current policy + ICP configuration) and
an append-only ``human_reviews`` audit row (reviewer, reason, timestamp, prior/new decision).
Existing decisions are never edited.

  APPROVE  latest decision must be REVIEW and the hard ICP rules must pass now
           -> new QUALIFIED decision (downstream Phase 6/7 gates then admit it normally)
  REJECT   latest decision must be REVIEW -> new NOT_QUALIFIED decision
  REOPEN   latest decision must be QUALIFIED or NOT_QUALIFIED -> new REVIEW decision
           (a reopened QUALIFIED lead immediately loses enrichment/analysis eligibility)
A refused action (wrong state, stale view, hard rules failing) changes no decision and is
still audited. This service never calls Laya, Hunter, Apollo, the AI provider or any other provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.domain.enums import QualificationDecision, QualificationPath, RunStatus, StageStatus
from app.domain.models import utcnow
from app.domain.outputs.records import HumanReviewRecord, ReviewAction, ReviewOutcome
from app.domain.qualification.bundle import build_bundle
from app.domain.qualification.clustering import cluster_signals
from app.domain.qualification.icp import POLICY_VERSION, IcpConfig
from app.domain.qualification.records import QualificationDecisionRecord, QualificationRunRecord
from app.domain.qualification.rules import HardRuleEvaluation, evaluate_hard_rules
from app.repositories.outputs import OutputStore
from app.repositories.qualification import QualificationStore

logger = get_logger(__name__)

ALLOWED_FROM: dict[ReviewAction, frozenset[QualificationDecision]] = {
    ReviewAction.APPROVE: frozenset({QualificationDecision.REVIEW}),
    ReviewAction.REJECT: frozenset({QualificationDecision.REVIEW}),
    ReviewAction.REOPEN: frozenset(
        {QualificationDecision.QUALIFIED, QualificationDecision.NOT_QUALIFIED}
    ),
}
RESULT: dict[ReviewAction, QualificationDecision] = {
    ReviewAction.APPROVE: QualificationDecision.QUALIFIED,
    ReviewAction.REJECT: QualificationDecision.NOT_QUALIFIED,
    ReviewAction.REOPEN: QualificationDecision.REVIEW,
}


@dataclass(frozen=True)
class ReviewResult:
    audit: HumanReviewRecord
    decision: QualificationDecisionRecord | None  # the NEW decision when applied

    @property
    def applied(self) -> bool:
        return self.audit.outcome is ReviewOutcome.APPLIED


class ReviewService:
    def __init__(
        self,
        qualifications: QualificationStore,
        outputs: OutputStore,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._q = qualifications
        self._out = outputs
        self._icp = IcpConfig.from_settings(settings)
        self._clock = clock

    async def act(
        self,
        company_id: UUID,
        action: ReviewAction,
        *,
        reviewer: str,
        reason: str,
        expected_decision_id: UUID | None = None,
    ) -> ReviewResult:
        """``expected_decision_id`` = the decision the reviewer was looking at; if a newer
        decision exists the action is refused (``stale_decision``) instead of guessing."""
        now = self._clock()
        latest = await asyncio.to_thread(self._q.latest_decision, company_id)
        if latest is None:
            raise NotFoundError(
                "company has no qualification decision", details={"id": str(company_id)}
            )
        refusal = self._precheck(latest, action, expected_decision_id)
        hard: HardRuleEvaluation | None = None
        data = None
        if refusal is None:
            data = await asyncio.to_thread(self._q.load, company_id)
            clusters = cluster_signals(
                data.signals,
                company_name=data.facts.name,
                title_similarity=self._icp.cluster_title_similarity,
                window_days=self._icp.cluster_window_days,
            )
            hard = evaluate_hard_rules(data.facts, clusters, self._icp, now)
            if action is ReviewAction.APPROVE and (
                not hard.passed or hard.path is QualificationPath.NONE
            ):
                refusal = "hard_rules_failed"

        def audit(
            outcome: ReviewOutcome, *, code: str | None = None, new_id: UUID | None = None
        ) -> HumanReviewRecord:
            return HumanReviewRecord(
                company_id=company_id,
                action=action,
                outcome=outcome,
                reviewer=reviewer,
                reason=reason,
                prior_decision_id=latest.id,
                resulting_decision_id=new_id,
                hard_rules_passed=hard.passed if hard is not None else None,
                hard_rule_results=[c.as_dict() for c in hard.checks] if hard is not None else [],
                refusal_code=code,
                policy_version=POLICY_VERSION,
                reviewed_at=now,
            )

        if refusal is not None or hard is None or data is None:
            audit_row = audit(ReviewOutcome.REFUSED, code=refusal or "internal")
            await asyncio.to_thread(self._out.insert_human_review, audit_row)
            logger.info(
                "review action refused",
                extra={"company_id": str(company_id), "action": action.value, "code": refusal},
            )
            return ReviewResult(audit_row, None)

        clusters = cluster_signals(
            data.signals,
            company_name=data.facts.name,
            title_similarity=self._icp.cluster_title_similarity,
            window_days=self._icp.cluster_window_days,
        )
        bundle = build_bundle(data.facts, hard, clusters, self._icp, now)
        run = QualificationRunRecord(
            policy_version=POLICY_VERSION,
            laya_mode="human",
            llm_review_mode="human",
            icp_config=self._icp.snapshot(),
            started_at=now,
        )
        new_decision = RESULT[action]
        record = QualificationDecisionRecord(
            run_id=run.id,
            company_id=company_id,
            company_name=data.facts.name,
            decision=new_decision,
            path=hard.path,
            hard_rules_passed=hard.passed,
            hard_rule_results=[c.as_dict() for c in hard.checks],
            reasons=[
                f"human {action.value} by {reviewer}: {reason}",
                f"previous decision {latest.decision.value} ({latest.id})",
                *latest.reasons[:3],
            ],
            laya_status=StageStatus.SKIPPED,
            llm_review_status=StageStatus.SKIPPED,
            policy_version=POLICY_VERSION,
            icp_config=self._icp.snapshot(),
            evidence_bundle={
                **bundle.as_record(),
                "human_review": {
                    "action": action.value,
                    "reviewer": reviewer,
                    "reason": reason,
                    "prior_decision_id": str(latest.id),
                },
            },
            evidence_signal_ids=[UUID(i) for i in bundle.signal_ids],
            signal_count=len(data.signals),
            event_count=len(clusters),
            decided_at=now,
        )
        run.count(new_decision)
        run.status = RunStatus.SUCCEEDED
        run.finished_at = now
        audit_row = audit(ReviewOutcome.APPLIED, new_id=record.id)

        def write() -> None:
            self._q.create_run(run)
            self._q.insert_decision(record)
            self._q.finish_run(run)
            self._out.insert_human_review(audit_row)

        await asyncio.to_thread(write)
        logger.info(
            "review action applied",
            extra={
                "company_id": str(company_id),
                "action": action.value,
                "decision": new_decision.value,
            },
        )
        return ReviewResult(audit_row, record)

    @staticmethod
    def _precheck(
        latest: QualificationDecisionRecord, action: ReviewAction, expected: UUID | None
    ) -> str | None:
        if expected is not None and latest.id != expected:
            return "stale_decision"
        if latest.decision not in ALLOWED_FROM[action]:
            return f"not_allowed_from_{latest.decision.value}"
        return None
