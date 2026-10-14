"""Backup API keys per provider (owner decision 2026-10-14).

A provider can have several keys: the primary one (``APIFY_TOKEN``) plus backups
(``APIFY_TOKEN_BACKUPS``, comma-separated). ``KeyRing.current()`` is the first key that is not
"benched". A key is benched when the provider itself says it is unusable - rejected (401/403)
or out of credits (402 / usage limit) - and the call is repeated with the next key. A benched
key is tried again after a cool-down (credits reset monthly; a rejected key may be fixed).

State lives in the process only: after a restart the first call re-discovers a dead key (a
refused request costs nothing). Keys are never logged - only their position ("key 2 of 3").
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

REJECTED_COOLDOWN_SECONDS = 24 * 3600.0  # key not accepted (revoked / wrong)
EXHAUSTED_COOLDOWN_SECONDS = 6 * 3600.0  # out of credits / usage limit


class KeyRing:
    def __init__(
        self, provider: str, keys: list[str], *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.provider = provider
        self._keys = list(dict.fromkeys(k for k in keys if k))
        self._benched: dict[str, float] = {}  # key -> usable again at (clock time)
        self._clock = clock

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def usable(self) -> list[str]:
        now = self._clock()
        return [k for k in self._keys if self._benched.get(k, 0.0) <= now]

    def current(self) -> str | None:
        """The key to use now. With every key benched the first one is returned, so the
        provider's own error (not a silent skip) is what the caller sees."""
        usable = self.usable()
        return usable[0] if usable else (self._keys[0] if self._keys else None)

    def position(self, key: str | None) -> int:
        return self._keys.index(key) + 1 if key in self._keys else 0

    def bench(self, key: str, *, rejected: bool) -> bool:
        """Take ``key`` out of rotation. Returns True when another key can be tried now."""
        if key not in self._keys:
            return False
        cooldown = REJECTED_COOLDOWN_SECONDS if rejected else EXHAUSTED_COOLDOWN_SECONDS
        self._benched[key] = self._clock() + cooldown
        again = bool(self.usable())
        logger.warning(
            "provider key switched off",
            extra={
                "provider": self.provider,
                "key_position": self.position(key),
                "keys_total": len(self._keys),
                "reason": "rejected" if rejected else "credits exhausted",
                "next_key_available": again,
            },
        )
        return again


_RINGS: dict[tuple[str, tuple[str, ...]], KeyRing] = {}


def ring_for(provider: str, keys: list[str]) -> KeyRing:
    """The process-wide ring of a provider (adapters are rebuilt per request; the knowledge of
    which key is used up must survive that)."""
    ident = (provider, tuple(dict.fromkeys(k for k in keys if k)))
    ring = _RINGS.get(ident)
    if ring is None:
        ring = _RINGS[ident] = KeyRing(provider, list(ident[1]))
    return ring


def split_keys(*values: str | None) -> list[str]:
    """Primary key + comma-separated backups -> ordered, de-duplicated list."""
    out: list[str] = []
    for value in values:
        out += [part.strip() for part in (value or "").split(",") if part.strip()]
    return list(dict.fromkeys(out))
