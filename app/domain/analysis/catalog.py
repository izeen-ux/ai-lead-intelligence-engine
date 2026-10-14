"""Versioned service catalog (``app/knowledge/service_catalog.json``).

The catalog is the ONLY list of offers the analysis may recommend. It is copied from the
TechnoTribes approved services file (nine services); structured matching fields restate the
approved wording and add no prices, durations, case studies or free offers.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from app.domain.models import DomainModel

CATALOG_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "service_catalog.json"


class Offer(DomainModel):
    offer_id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    name: str = Field(min_length=3)
    description: str = Field(min_length=10)
    target_buyer: str = Field(min_length=3)
    target_company_characteristics: list[str] = Field(min_length=1)
    trigger_patterns: list[str] = Field(min_length=1)
    problem_patterns: list[str] = Field(min_length=1)
    constraints: list[str] = Field(min_length=1)
    evidence_requirements: list[str] = Field(min_length=1)
    hook_style: str = Field(min_length=3)


class ServiceCatalog(DomainModel):
    catalog_id: str
    version: str
    source: str
    derivation_note: str
    company: dict[str, str]
    global_constraints: list[str] = Field(min_length=1)
    offers: list[Offer] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> ServiceCatalog:
        ids = [o.offer_id for o in self.offers]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate offer_id in service catalog")
        return self

    @property
    def offer_ids(self) -> frozenset[str]:
        return frozenset(o.offer_id for o in self.offers)

    def get(self, offer_id: str) -> Offer | None:
        return next((o for o in self.offers if o.offer_id == offer_id), None)

    def for_prompt(self) -> list[dict[str, Any]]:
        """Compact form the model sees (ids + matching fields)."""
        return [
            {
                "offer_id": o.offer_id,
                "name": o.name,
                "description": o.description,
                "target_buyer": o.target_buyer,
                "fits": o.target_company_characteristics,
                "triggers": o.trigger_patterns,
                "problems": o.problem_patterns,
                "constraints": o.constraints,
                "requires_evidence_of": o.evidence_requirements,
                "hook_style": o.hook_style,
            }
            for o in self.offers
        ]


def load_catalog(path: Path = CATALOG_PATH) -> ServiceCatalog:
    return ServiceCatalog.model_validate(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def default_catalog() -> ServiceCatalog:
    return load_catalog()
