"""Deterministic extraction of a funding ROUND amount from headline/snippet text.

Conservative by design - when in doubt the amount is ``None`` (unknown), which can never pass
the funding path:
* an amount needs an explicit unit (crore / lakh / million / billion / k); bare numbers such as
  "Seed Shares Priced at Rs 132.40 Apiece" are ignored;
* valuation / revenue / fund-size amounts are ignored;
* cumulative totals ("raised Rs 60 Cr so far", "total funding") are flagged and never treated
  as the latest round amount;
* non-INR amounts are converted only when an explicit FX rate is configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_MULTIPLIER: dict[str, Decimal] = {
    "crore": Decimal(10_000_000),
    "crores": Decimal(10_000_000),
    "cr": Decimal(10_000_000),
    "lakh": Decimal(100_000),
    "lakhs": Decimal(100_000),
    "lac": Decimal(100_000),
    "million": Decimal(1_000_000),
    "mn": Decimal(1_000_000),
    "m": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
    "bn": Decimal(1_000_000_000),
    "k": Decimal(1_000),
}
_INDIAN_UNITS = {"crore", "crores", "cr", "lakh", "lakhs", "lac"}

_AMOUNT_RE = re.compile(
    r"(?P<cur>₹|rs\.?|inr|usd|us\$|\$)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>crores?|cr|lakhs?|lac|million|mn|billion|bn|m|k)\b",
    re.I,
)
# words right before an amount that make it NOT a round amount
_NOT_ROUND_RE = re.compile(
    r"(valuation|valued|revenue|turnover|arr|sales|fund size|corpus|market cap|worth)\W+"
    r"(?:\w+\W+){0,3}$",
    re.I,
)
_CUMULATIVE_RE = re.compile(
    r"\b(so far|to date|till date|in total|total funding|total of|cumulative|overall funding|"
    r"taking (?:its|the) total|brings? (?:its|the) total)\b",
    re.I,
)


@dataclass(frozen=True)
class FundingMention:
    amount: Decimal  # in ``currency`` units
    currency: str  # "INR" | "USD"
    raw: str
    cumulative: bool

    def amount_inr(self, usd_to_inr: Decimal | None) -> Decimal | None:
        if self.currency == "INR":
            return self.amount
        if self.currency == "USD" and usd_to_inr is not None:
            return (self.amount * usd_to_inr).quantize(Decimal(1))
        return None


def _currency(cur: str | None, unit: str) -> str | None:
    c = (cur or "").lower().rstrip(".")
    if c in {"₹", "rs", "inr"}:
        return "INR"
    if c in {"$", "usd", "us$"}:
        return "USD"
    if unit.lower() in _INDIAN_UNITS:
        return "INR"  # crore/lakh are Indian units
    return None  # "5 million" with no currency: unknown


def extract_funding(text: str) -> FundingMention | None:
    """First round amount in ``text`` (or ``None``)."""
    cumulative = bool(_CUMULATIVE_RE.search(text))
    for m in _AMOUNT_RE.finditer(text):
        if _NOT_ROUND_RE.search(text[: m.start()]):
            continue
        unit = m.group("unit").lower()
        currency = _currency(m.group("cur"), unit)
        if currency is None:
            continue
        try:
            number = Decimal(m.group("num").replace(",", ""))
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            continue
        return FundingMention(
            amount=number * _MULTIPLIER[unit],
            currency=currency,
            raw=m.group(0).strip(),
            cumulative=cumulative,
        )
    return None


def format_inr(amount: Decimal | None) -> str:
    if amount is None:
        return "unknown"
    crores = amount / Decimal(10_000_000)
    return f"₹{crores.normalize():f} Cr"
