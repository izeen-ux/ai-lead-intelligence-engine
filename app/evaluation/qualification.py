"""Qualification evaluation harness (Phase 5).

Runs the REAL QualificationService (rules -> clustering -> bundle -> Laya -> policy) over a
labeled dataset held in memory (never the production database, never persisted) and reports:

* decision accuracy + confusion matrix (label x predicted);
* hard-path accuracy (deterministic stage) and event-count checks (duplicate coverage);
* safety: false QUALIFIED count (the costly error - it would unlock enrichment spend);
* per-category results and every mismatch with the decision's reasons.

Three data classes are kept apart on purpose: unit-test fixtures (``tests/``), labeled
evaluation cases (``evaluation/qualification/``) and production data (PostgreSQL).
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import Field

from app.core.config import Settings
from app.domain.enums import (
    DomainResolutionStatus,
    QualificationDecision,
    QualificationPath,
    SignalType,
)
from app.domain.models import DomainModel
from app.domain.qualification.clustering import SignalEvidence
from app.domain.qualification.rules import CompanyFacts
from app.providers.base import QualificationProvider
from app.repositories.qualification import InMemoryQualificationStore, QualificationInput
from app.services.qualification import QualificationService

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[2] / "evaluation" / "qualification" / "labeled_cases.json"
)
_NS = uuid.UUID("5f3a9d52-7f0e-4f59-9a51-000000000005")  # deterministic ids for cases


class CaseSignal(DomainModel):
    source: str
    type: SignalType
    title: str
    evidence: str | None = None
    platform: str | None = None
    days_ago: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    attributed: bool = False


class CaseCompany(DomainModel):
    name: str
    domain: str | None = None
    domain_status: DomainResolutionStatus
    country_code: str | None = None


class LabeledCase(DomainModel):
    id: str
    category: str
    label: QualificationDecision
    hard_path: QualificationPath
    rationale: str
    expected_events: int | None = None
    company: CaseCompany
    signals: list[CaseSignal] = Field(default_factory=list)


class LabeledDataset(DomainModel):
    dataset: str
    version: str
    reference_date: datetime
    provenance: str
    label_semantics: str
    cases: list[LabeledCase]


def load_dataset(path: Path = DEFAULT_DATASET) -> LabeledDataset:
    return LabeledDataset.model_validate_json(path.read_text(encoding="utf-8"))


def case_input(case: LabeledCase, reference: datetime) -> QualificationInput:
    company_id = uuid.uuid5(_NS, case.id)
    facts = CompanyFacts(
        company_id=company_id,
        name=case.company.name,
        domain=case.company.domain,
        domain_status=case.company.domain_status,
        country_code=case.company.country_code,
    )
    signals = []
    for i, s in enumerate(case.signals):
        notes = ["news_rss item names the company"]
        if s.attributed:
            notes.append("matches verified-website context: synthetic")
        signals.append(
            SignalEvidence(
                signal_id=uuid.uuid5(_NS, f"{case.id}:signal:{i}"),
                signal_type=s.type,
                summary=s.title,
                evidence=s.evidence or s.title,
                source_id=uuid.uuid5(_NS, f"{case.id}:source:{s.source}"),
                url=f"https://news.example/{case.id.lower()}/{s.source}",
                platform=s.platform,
                published_at=reference - timedelta(days=s.days_ago)
                if s.days_ago is not None
                else None,
                confidence=s.confidence,
                notes=tuple(notes),
                channel="news_rss",
            )
        )
    return QualificationInput(facts, signals)


@dataclass
class CaseResult:
    case: LabeledCase
    predicted: QualificationDecision
    path: QualificationPath
    events: int
    laya_status: str
    score: float | None
    reasons: list[str]

    @property
    def correct(self) -> bool:
        return self.predicted is self.case.label

    @property
    def path_correct(self) -> bool:
        return self.path is self.case.hard_path

    @property
    def events_correct(self) -> bool | None:
        return (
            None
            if self.case.expected_events is None
            else (self.events == self.case.expected_events)
        )


@dataclass
class EvaluationReport:
    dataset: str
    version: str
    laya_mode: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def path_accuracy(self) -> float:
        return (
            sum(r.path_correct for r in self.results) / len(self.results) if self.results else 0.0
        )

    @property
    def false_qualified(self) -> list[CaseResult]:
        return [
            r
            for r in self.results
            if r.predicted is QualificationDecision.QUALIFIED
            and r.case.label is not QualificationDecision.QUALIFIED
        ]

    def confusion(self) -> dict[str, dict[str, int]]:
        labels = [d.value for d in QualificationDecision]
        counts = Counter((r.case.label.value, r.predicted.value) for r in self.results)
        return {t: {p: counts[(t, p)] for p in labels} for t in labels}

    def by_category(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.results:
            c = out.setdefault(r.case.category, {"cases": 0, "correct": 0})
            c["cases"] += 1
            c["correct"] += int(r.correct)
        return out

    def as_dict(self) -> dict[str, Any]:
        events = [r.events_correct for r in self.results if r.events_correct is not None]
        return {
            "dataset": self.dataset,
            "version": self.version,
            "laya_mode": self.laya_mode,
            "cases": len(self.results),
            "decision_accuracy": round(self.accuracy, 4),
            "hard_path_accuracy": round(self.path_accuracy, 4),
            "event_count_checks": f"{sum(events)}/{len(events)}",
            "false_qualified": [r.case.id for r in self.false_qualified],
            "confusion_label_x_predicted": self.confusion(),
            "by_category": self.by_category(),
            "mismatches": [
                {
                    "id": r.case.id,
                    "category": r.case.category,
                    "label": r.case.label.value,
                    "predicted": r.predicted.value,
                    "laya": r.laya_status,
                    "score": r.score,
                    "reasons": r.reasons,
                }
                for r in self.results
                if not r.correct
            ],
        }


async def evaluate(
    dataset: LabeledDataset, laya: QualificationProvider, settings: Settings
) -> EvaluationReport:
    store = InMemoryQualificationStore()
    for case in dataset.cases:
        store.add(case_input(case, dataset.reference_date))
    service = QualificationService(
        store, laya, settings, persist=False, clock=lambda: dataset.reference_date
    )
    report = EvaluationReport(dataset.dataset, dataset.version, laya.status().mode)
    run_id = uuid.uuid4()
    for case in dataset.cases:
        record = await service.qualify(run_id, uuid.uuid5(_NS, case.id))
        report.results.append(
            CaseResult(
                case=case,
                predicted=record.decision,
                path=record.path,
                events=record.event_count,
                laya_status=record.laya_status.value,
                score=record.score,
                reasons=record.reasons,
            )
        )
    return report


def write_report(report: EvaluationReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
