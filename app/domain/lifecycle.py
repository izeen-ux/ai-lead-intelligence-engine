"""Lead status state machine.

Encodes the core business invariant: nothing reaches enrichment (paid credits) without passing
qualification, and REJECTED is terminal unless a human re-opens it via REVIEW.
"""

from __future__ import annotations

from app.domain.enums import LeadStatus

S = LeadStatus

ALLOWED_TRANSITIONS: dict[LeadStatus, frozenset[LeadStatus]] = {
    S.DISCOVERED: frozenset({S.RESEARCHED, S.REJECTED}),
    S.RESEARCHED: frozenset({S.QUALIFIED, S.REVIEW, S.REJECTED}),
    S.REVIEW: frozenset({S.QUALIFIED, S.REJECTED}),
    S.QUALIFIED: frozenset({S.ENRICHMENT_PENDING, S.REJECTED}),
    S.ENRICHMENT_PENDING: frozenset({S.ENRICHED, S.REVIEW, S.REJECTED}),
    S.ENRICHED: frozenset({S.READY, S.REVIEW, S.REJECTED}),
    S.READY: frozenset({S.REVIEW}),
    S.REJECTED: frozenset({S.REVIEW}),
}


def can_transition(current: LeadStatus, target: LeadStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
