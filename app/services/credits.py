"""Credit counter: what each paid provider has used and what is left.

Two kinds of numbers, always labelled:
  * ``provider``  - read live from the provider's own FREE account endpoint (Apify monthly
                    usage, Firecrawl remaining credits, Hunter searches / verifications). Only
                    in live provider mode; these calls cost nothing and change nothing.
  * ``app``       - this app's own ledger compared with the configured monthly budget
                    (Apollo and OpenAI expose no balance endpoint to these keys).
A provider that cannot be read is reported with the reason - never with a guessed number.
No secret is returned: only counts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.domain.models import utcnow
from app.repositories.outputs import OutputStore, UsageLine

APIFY_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"
FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v2"
TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CreditLine:
    provider: str
    unit: str  # "USD" | "credits" | "searches" | "verifications"
    used: str | None = None  # this month / billing period
    remaining: str | None = None
    limit: str | None = None
    source: str = "app"  # "provider" (live account endpoint) | "app" (ledger vs budget)
    note: str = ""
    # Date (YYYY-MM-DD) the credits come back: the earliest reset of all keys of the provider.
    resets_on: str | None = None
    keys: tuple[dict[str, Any], ...] = ()  # one row per key: position, status, used, limit, reset


@dataclass(frozen=True)
class KeyReading:
    """What one key's account reports. ``resets_on`` = first day of the next billing cycle."""

    used: Decimal
    limit: Decimal
    resets_on: str | None = None


def _day(value: Any, *, plus_one_ms: bool = False) -> str | None:
    """ISO timestamp / date -> YYYY-MM-DD. ``plus_one_ms``: the value is the END of a cycle
    (23:59:59.999), so the credits come back on the next day."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if plus_one_ms:
        moment += timedelta(milliseconds=1)
    return moment.date().isoformat()


def apify_reading(body: dict[str, Any]) -> KeyReading:
    data = body.get("data") or {}
    used = (data.get("current") or {}).get("monthlyUsageUsd")
    limit = (data.get("limits") or {}).get("maxMonthlyUsageUsd")
    cycle = data.get("monthlyUsageCycle") or {}
    return KeyReading(
        Decimal(str(used or 0)),
        Decimal(str(limit or 0)),
        _day(cycle.get("endAt"), plus_one_ms=True),
    )


def firecrawl_reading(body: dict[str, Any]) -> KeyReading:
    data = body.get("data") or {}
    remaining = Decimal(str(data.get("remainingCredits") or 0))
    plan = max(Decimal(str(data.get("planCredits") or 0)), remaining)  # bonus credits
    return KeyReading(plan - remaining, plan, _day(data.get("billingPeriodEnd")))


def hunter_reading(body: dict[str, Any]) -> KeyReading:
    """Hunter /v2/account (direct): searches used / available + the reset date."""
    data = body.get("data") or {}
    searches = (data.get("requests") or {}).get("searches") or {}
    return KeyReading(
        Decimal(str(searches.get("used") or 0)),
        Decimal(str(searches.get("available") or 0)),
        _day(data.get("reset_date")),
    )


def key_rows(results: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for position, result in enumerate(results, start=1):
        if isinstance(result, KeyReading):
            rows.append(
                {
                    "position": position,
                    "status": "ok" if result.used < result.limit or not result.limit else "used up",
                    "used": _money(result.used),
                    "limit": _money(result.limit),
                    "resets_on": result.resets_on,
                }
            )
        else:
            code = getattr(getattr(result, "response", None), "status_code", None)
            rows.append(
                {
                    "position": position,
                    "status": "refused" if code in (401, 403) else "not readable",
                    "used": None,
                    "limit": None,
                    "resets_on": None,
                }
            )
    return rows


def keys_note(keys: tuple[dict[str, Any], ...]) -> str:
    if len(keys) <= 1:
        return ""
    bad = sum(1 for k in keys if k["status"] in ("refused", "not readable"))
    note = f"{len(keys)} keys added together"
    return note + (f" ({bad} not accepted by the provider)" if bad else "")


def summed(
    provider: str, unit: str, rows: list[KeyReading], keys: tuple[dict[str, Any], ...]
) -> CreditLine:
    used = sum((r.used for r in rows), Decimal(0))
    limit = sum((r.limit for r in rows), Decimal(0))
    resets = sorted(r.resets_on for r in rows if r.resets_on)
    return CreditLine(
        provider, unit, _money(used), _left(limit, used), _money(limit), "provider",
        keys_note(keys), resets[0] if resets else None, keys,
    )  # fmt: skip


def first_of_next_month(now: datetime) -> str:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1).date().isoformat()


def _money(value: Any) -> str | None:
    try:
        return f"{Decimal(str(value)):.4f}".rstrip("0").rstrip(".") if value is not None else None
    except ArithmeticError:
        return None


def _left(limit: Any, used: Any) -> str | None:
    try:
        return _money(max(Decimal(str(limit)) - Decimal(str(used)), Decimal(0)))
    except (ArithmeticError, TypeError, ValueError):
        return None


def credit_delta(before: Any, after: Any) -> list[dict[str, Any]]:
    """What one pipeline run spent: the credit lines read just before and just after it.
    ``spent`` = growth of ``used`` (or drop of ``remaining``); None when a counter could not
    be read on either side - never a guessed number."""
    if not isinstance(before, list) or not isinstance(after, list):
        return []
    start = {(b.get("provider"), b.get("unit")): b for b in before if isinstance(b, dict)}
    rows = []
    for line in after:
        if not isinstance(line, dict):
            continue
        first = start.get((line.get("provider"), line.get("unit"))) or {}
        spent: Decimal | None = None
        try:
            if line.get("used") is not None and first.get("used") is not None:
                spent = Decimal(str(line["used"])) - Decimal(str(first["used"]))
            elif line.get("remaining") is not None and first.get("remaining") is not None:
                spent = Decimal(str(first["remaining"])) - Decimal(str(line["remaining"]))
        except ArithmeticError:
            spent = None
        rows.append(
            {
                "provider": line.get("provider"),
                "unit": line.get("unit"),
                "spent": _money(max(spent, Decimal(0))) if spent is not None else None,
                "remaining": line.get("remaining"),
                "limit": line.get("limit"),
                "source": line.get("source"),
            }
        )
    return rows


def run_credit_rows(runs: list[dict[str, Any]], average: Decimal) -> list[dict[str, Any]]:
    """Per pipeline run: how many companies the free pre-screen stopped, how many were
    scraped, and what the scraping cost. ``runs`` = ``run_summary`` dicts; ``average`` =
    website credits one scraped company costs overall (used when a run scraped nothing)."""
    rows = []
    for run in runs:
        stages = {s["stage"]: s.get("counts") or {} for s in run.get("stages") or []}
        screen, site = stages.get("prescreen") or {}, stages.get("website") or {}
        if not screen and not site:
            continue
        scraped, spent = int(site.get("companies") or 0), int(site.get("credits") or 0)
        skipped = int(screen.get("skip") or 0)
        per_company = (Decimal(spent) / scraped) if scraped else average
        rows.append(
            {
                "run_id": run.get("run_id"),
                "started_at": run.get("started_at"),
                "owner": run.get("owner") or run.get("requested_by"),
                "screened": int(screen.get("companies") or 0),
                "stopped_before_scraping": skipped,
                "allowed_to_scrape": int(screen.get("scrape") or 0),
                "scraped": scraped,
                "website_credits": spent,
                "credits_per_company": f"{per_company:.1f}",
                "website_verified": int(site.get("verified") or 0),
                "qualified": int((stages.get("qualification") or {}).get("qualified") or 0),
                "credits_saved_estimate": f"{per_company * skipped:.0f}",
                # provider counters before/after the run ("apify USD" -> spent), when recorded
                "spent": {
                    f"{c['provider']} {c['unit']}": c["spent"]
                    for c in run.get("credits") or []
                    if c.get("spent") is not None
                },
            }
        )
    return rows


class CreditService:
    def __init__(
        self,
        settings: Settings,
        outputs: OutputStore,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        hunter_usage: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._outputs = outputs
        self._transport = transport  # injected in tests
        self._hunter_usage = hunter_usage
        self._clock = clock

    @property
    def live(self) -> bool:
        return self._settings.provider_mode is ProviderMode.LIVE

    async def _get(self, url: str, key: str) -> dict[str, Any]:
        async with httpx2.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            transport=self._transport,
            headers={"Authorization": f"Bearer {key}"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else {}

    async def _per_key(
        self, field: str, read: Callable[[str], Awaitable[KeyReading]]
    ) -> tuple[list[KeyReading], tuple[dict[str, Any], ...]]:
        """Every key of a provider read from its own free account endpoint. Backup keys are
        separate accounts, so the dashboard adds them together; each key also gets its own row
        (position, accepted or not, used / limit, the date its credits come back)."""
        keys = self._settings.provider_keys(field)
        results = await asyncio.gather(*(read(k) for k in keys), return_exceptions=True)
        good = [r for r in results if isinstance(r, KeyReading)]
        if keys and not good:
            failed = [r for r in results if isinstance(r, BaseException)]
            if failed:
                raise failed[0]
        return good, tuple(key_rows(results))

    async def _apify(self) -> CreditLine:
        async def read(key: str) -> KeyReading:
            return apify_reading(await self._get(APIFY_LIMITS_URL, key))

        rows, keys = await self._per_key("apify_token", read)
        if not keys:
            return CreditLine("apify", "USD", note="APIFY_TOKEN not set")
        return summed("apify", "USD", rows, keys)

    async def _firecrawl(self) -> CreditLine:
        base = (self._settings.firecrawl_base_url or FIRECRAWL_BASE_URL).rstrip("/")

        async def read(key: str) -> KeyReading:
            return firecrawl_reading(await self._get(f"{base}/team/credit-usage", key))

        rows, keys = await self._per_key("firecrawl_api_key", read)
        if not keys:
            return CreditLine("firecrawl", "credits", note="FIRECRAWL_API_KEY not set")
        return summed("firecrawl", "credits", rows, keys)

    async def _hunter(self) -> list[CreditLine]:
        if self._hunter_usage is None:  # wired from the provider registry (live sidecar)
            raise ValueError("enrichment sidecar not available")
        body = await self._hunter_usage()
        if body.get("errorKind"):
            raise ValueError(str(body["errorKind"]))
        usage = (body.get("hunter") or body).get("requests") or body.get("requests") or {}
        detail = body.get("keysDetail") or []
        lines = []
        for kind in ("searches", "verifications"):
            row = usage.get(kind) or {}
            used, limit = row.get("used"), row.get("available")
            remaining = row.get("remaining")
            per_key = tuple(
                {
                    "position": d.get("position"),
                    "status": "ok" if d.get("ok") else "refused",
                    "used": _money(((d.get("requests") or {}).get(kind) or {}).get("used")),
                    "limit": _money(((d.get("requests") or {}).get(kind) or {}).get("available")),
                    "resets_on": d.get("resetDate"),
                }
                for d in detail
                if isinstance(d, dict)
            )
            resets = sorted(k["resets_on"] for k in per_key if k["resets_on"])
            lines.append(
                CreditLine(
                    "hunter",
                    kind,
                    _money(used),
                    _money(remaining) if remaining is not None else _left(limit, used),
                    _money(limit),
                    "provider",
                    keys_note(per_key),
                    resets[0] if resets else None,
                    per_key,
                )
            )
        return lines

    def _ledger(self, usage: list[UsageLine]) -> list[CreditLine]:
        s = self._settings
        month = {(u.provider, u.metric): u.this_month for u in usage}
        apollo = month.get(("apollo", "credits"), Decimal(0))
        claude = s.reasoning_provider.value == "anthropic"
        name = "anthropic" if claude else "openai"
        ai_cap = (
            s.anthropic_max_estimated_cost_usd_per_month
            if claude
            else s.openai_max_estimated_cost_usd_per_month
        )
        ai = sum((v for (p, m), v in month.items() if p == name and m == "usd"), Decimal(0))
        return [
            CreditLine(
                "apollo",
                "credits",
                _money(apollo),
                _left(s.enrichment_max_apollo_credits_per_month, apollo),
                _money(s.enrichment_max_apollo_credits_per_month),
                "app",
                "app ledger vs ENRICHMENT_MAX_APOLLO_CREDITS_PER_MONTH (Apollo has no balance API)",
                first_of_next_month(self._clock()),
            ),
            CreditLine(
                name,
                "USD",
                _money(ai),
                _left(ai_cap, ai),
                _money(ai_cap),
                "app",
                f"estimated spend vs {name.upper()}_MAX_ESTIMATED_COST_USD_PER_MONTH (the real "
                "balance is on the provider's billing page)",
                first_of_next_month(self._clock()),
            ),
        ]

    async def _safe(self, name: str, unit: str, call: Callable[[], Any]) -> list[CreditLine]:
        try:
            result = await call()
        except (httpx2.HTTPError, AppError, ValueError, KeyError, TypeError) as exc:
            reason = getattr(exc, "code", None) or type(exc).__name__
            return [CreditLine(name, unit, note=f"could not be read ({reason})")]
        return result if isinstance(result, list) else [result]

    async def lines(self) -> list[dict[str, Any]]:
        """Current credit lines only (the snapshot a pipeline run stores before and after)."""
        return list((await self.report())["lines"])

    async def report(self, runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        now = self._clock()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        usage = await asyncio.to_thread(self._outputs.usage_summary, month_start)
        screening = await asyncio.to_thread(self._outputs.screening_stats)
        lines: list[CreditLine] = []
        if self.live:
            groups = await asyncio.gather(
                self._safe("apify", "USD", self._apify),
                self._safe("firecrawl", "credits", self._firecrawl),
                self._safe("hunter", "searches", self._hunter),
            )
            lines = [line for group in groups for line in group]
        else:
            # safe mode makes no provider call: show what this app recorded this month
            month = {(u.provider, u.metric): u.this_month for u in usage}
            why = "app records this month; remaining needs live mode (provider's own counter)"
            lines = [
                CreditLine("apify", "USD", _money(month.get(("apify", "usd"))), note=why),
                CreditLine(
                    "firecrawl", "credits", _money(month.get(("firecrawl", "credits"))), note=why
                ),
                CreditLine("hunter", "credits", _money(month.get(("hunter", "credits"))), note=why),
            ]
        lines += self._ledger(usage)
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "live": self.live,
            "lines": [asdict(line) for line in lines],
            "screening": screening,
            "runs": run_credit_rows(
                runs or [], Decimal(str(screening.get("avg_credits_per_scraped_company") or 0))
            ),
        }
