"""Deterministic identity helpers used for deduplication across discovery runs.

* ``company_dedupe_key`` - stable key for a company: its canonical domain when known, otherwise
  its normalized name. Keys are prefixed (``domain:`` / ``name:``) so the two spaces never
  collide.
* ``canonicalize_url`` / ``url_hash`` - stable identity for a source URL (tracking parameters,
  fragments, case and trailing slashes do not create duplicates).

Known limitation (documented in ADR-021): a company first seen by name and later by domain gets
two keys; merging is left to the website-intelligence phase, which resolves domains.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain.models import normalize_domain

_LEGAL_SUFFIXES = (
    "private limited",
    "pvt ltd",
    "pvt",
    "limited",
    "ltd",
    "llp",
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "gmbh",
)
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|gclid|fbclid|mc_cid|mc_eid|ref|ref_src|igshid)$")


def normalize_company_name(name: str) -> str:
    """``"ACME Robotics Pvt. Ltd."`` -> ``"acme robotics"``. Raises ``ValueError`` if empty."""
    text = unicodedata.normalize("NFKC", name).lower().replace("&", " and ")
    text = _NON_ALNUM.sub(" ", text).strip()
    changed = True
    while changed and text:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if text == suffix:
                break
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].strip()
                changed = True
    if not text:
        raise ValueError(f"company name normalizes to nothing: {name!r}")
    return text


def company_dedupe_key(name: str, domain: str | None = None) -> str:
    if domain:
        return f"domain:{normalize_domain(domain)}"
    return f"name:{normalize_company_name(name)}"


def canonicalize_url(url: str) -> str:
    """Lowercase scheme/host, drop fragment, default ports, tracking params, trailing slash."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAMS.match(k.lower())
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()
