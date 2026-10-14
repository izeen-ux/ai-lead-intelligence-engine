"""Pure website helpers (no I/O): URL policy, page classification, budgeted page selection.

Policy encoded here (Phase 3):
* Social-platform URLs are NEVER crawled; they are collected as metadata for later phases.
* Directory / news / app-store / data-broker hosts are never treated as a company's official site.
* A crawl only visits pages on the company's own site, at most ``budget`` pages, chosen by
  priority: home, about, products/services, careers, pricing, blog/news, contact, one extra.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from app.domain.enums import PageType
from app.domain.models import normalize_domain

SOCIAL_HOSTS = frozenset(
    {
        "linkedin.com",
        "instagram.com",
        "facebook.com",
        "fb.com",
        "x.com",
        "twitter.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "threads.net",
        "reddit.com",
        "medium.com",
        "t.me",
        "wa.me",
        "whatsapp.com",
        "pinterest.com",
        "github.com",
    }
)

# Hosts that can mention a company but are never its official website.
NON_OFFICIAL_HOSTS = SOCIAL_HOSTS | frozenset(
    {
        "wikipedia.org",
        "crunchbase.com",
        "tracxn.com",
        "pitchbook.com",
        "cbinsights.com",
        "zaubacorp.com",
        "tofler.in",
        "thecompanycheck.com",
        "ambitionbox.com",
        "glassdoor.com",
        "glassdoor.co.in",
        "indeed.com",
        "naukri.com",
        "justdial.com",
        "indiamart.com",
        "play.google.com",
        "apps.apple.com",
        "producthunt.com",
        "inc42.com",
        "yourstory.com",
        "entrackr.com",
        "economictimes.indiatimes.com",
        "indiatimes.com",
        "livemint.com",
        "moneycontrol.com",
        "business-standard.com",
        "thehindubusinessline.com",
        "vccircle.com",
        "techcrunch.com",
        "startupindia.gov.in",
        "google.com",
        "bing.com",
    }
)

# Suffixes we refuse to crawl (internal / reserved names) - SSRF hygiene.
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".test", ".invalid", ".lan")

_SKIP_EXTENSIONS = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|ico|zip|gz|mp4|mp3|avi|mov|xml|json|css|js|txt|docx?|xlsx?|pptx?)$",
    re.IGNORECASE,
)
_SKIP_PATHS = re.compile(
    r"(login|signin|sign-in|signup|sign-up|register|cart|checkout|account|privacy|terms|cookie|"
    r"legal|disclaimer|wp-admin|wp-json|feed|tag/|category/|author/|search)",
    re.IGNORECASE,
)
_PAGE_PATTERNS: tuple[tuple[PageType, re.Pattern[str]], ...] = (
    (PageType.ABOUT, re.compile(r"(about|company|who-we-are|our-story|team)", re.I)),
    (PageType.PRODUCTS, re.compile(r"(product|service|solution|platform|feature)", re.I)),
    (PageType.CAREERS, re.compile(r"(career|jobs|join-us|hiring|work-with-us)", re.I)),
    (PageType.PRICING, re.compile(r"(pricing|plans)", re.I)),
    (PageType.BLOG, re.compile(r"(blog|news|press|media|insight|article|updates)", re.I)),
    (PageType.CONTACT, re.compile(r"(contact|reach-us|get-in-touch)", re.I)),
)
PRIORITY: tuple[PageType, ...] = (
    PageType.HOME,
    PageType.ABOUT,
    PageType.PRODUCTS,
    PageType.CAREERS,
    PageType.PRICING,
    PageType.BLOG,
    PageType.CONTACT,
    PageType.OTHER,
)


def host_of(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")


def _host_matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_social_url(url: str) -> bool:
    return _host_matches(host_of(url), SOCIAL_HOSTS)


def is_non_official_host(host: str) -> bool:
    return _host_matches(host.lower().removeprefix("www."), NON_OFFICIAL_HOSTS)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def crawlable_domain(domain: str) -> str:
    """Canonical domain safe to crawl; raises ``ValueError`` for reserved/internal names."""
    canonical = normalize_domain(domain)
    if canonical.endswith(_BLOCKED_SUFFIXES) or is_non_official_host(canonical):
        raise ValueError(f"domain not allowed for crawling: {canonical!r}")
    # SSRF hardening (Phase 10): a company website is a public DNS name - never an IP literal
    # (10.x, 127.x, 169.254.169.254, [::1], decimal/hex forms...) and never a bare internal
    # host name such as "postgres" or "laya".
    host = canonical.split(":", 1)[0].strip("[]")
    if _is_ip_literal(host) or "." not in host or host.rsplit(".", 1)[-1].isdigit():
        raise ValueError(f"domain not allowed for crawling: {canonical!r}")
    return canonical


def belongs_to_site(url: str, domain: str) -> bool:
    host = host_of(url)
    return host == domain or host.endswith("." + domain)


def classify_page(url: str) -> PageType:
    path = urlsplit(url).path.rstrip("/").lower()
    if path in ("", "/index.html", "/index.php", "/home"):
        return PageType.HOME
    for page_type, pattern in _PAGE_PATTERNS:
        if pattern.search(path):
            return page_type
    return PageType.OTHER


def _clean(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    return urlunsplit(("https", (parts.hostname or "").lower(), path, "", ""))


@dataclass(frozen=True)
class PlannedPage:
    url: str
    page_type: PageType


def select_pages(domain: str, discovered_urls: list[str], budget: int) -> list[PlannedPage]:
    """Pick at most ``budget`` same-site pages by priority (one per type, shortest path wins).

    The homepage is always first. Social, off-site, file and utility URLs are dropped.
    """
    if budget < 1:
        return []
    by_type: dict[PageType, list[str]] = {t: [] for t in PRIORITY}
    seen: set[str] = set()
    for raw in discovered_urls:
        if not raw.lower().startswith(("http://", "https://")):
            continue
        if is_social_url(raw) or not belongs_to_site(raw, domain):
            continue
        path = urlsplit(raw).path
        if _SKIP_EXTENSIONS.search(path) or _SKIP_PATHS.search(path):
            continue
        url = _clean(raw)
        if url in seen:
            continue
        seen.add(url)
        by_type[classify_page(url)].append(url)

    home = f"https://{domain}/"
    plan = [PlannedPage(home, PageType.HOME)]
    for page_type in PRIORITY[1:]:
        if len(plan) >= budget:
            break
        options = sorted(by_type[page_type], key=lambda u: (len(u), u))
        if options:
            plan.append(PlannedPage(options[0], page_type))
    return plan[:budget]


def extract_social_links(urls: list[str]) -> dict[str, str]:
    """Map platform -> first profile URL seen (metadata only; never fetched in Phase 3)."""
    found: dict[str, str] = {}
    for url in urls:
        host = host_of(url)
        for platform in sorted(SOCIAL_HOSTS):
            if (host == platform or host.endswith("." + platform)) and platform not in found:
                found[platform] = url.strip()
    return found


# ---------------------------------------------------------------- extraction assessment
class PageLike(Protocol):
    page_type: PageType
    markdown: str
    title: str | None
    description: str | None


MIN_HOME_CHARS = 200
MIN_TOTAL_CHARS = 500
_AI_TERMS = re.compile(
    r"\b(ai|artificial intelligence|machine learning|ml|llm|genai|generative ai|automation|"
    r"automate|chatbot|agentic|ai agents?)\b",
    re.IGNORECASE,
)


def assess_sufficiency(pages: Sequence[PageLike]) -> tuple[bool, str | None]:
    """A crawl is sufficient when the homepage has real content and the site yields enough
    text overall. Otherwise the caller may try the fallback crawler."""
    if not pages:
        return False, "no pages extracted"
    home = next((p for p in pages if p.page_type is PageType.HOME), None)
    if home is None or len(home.markdown.strip()) < MIN_HOME_CHARS:
        return False, "homepage missing or nearly empty (possible JavaScript-rendered site)"
    if sum(len(p.markdown) for p in pages) < MIN_TOTAL_CHARS:
        return False, "too little text across extracted pages"
    return True, None


def build_site_facts(pages: Sequence[PageLike]) -> dict[str, Any]:
    """Deterministic, evidence-backed facts (no LLM). Each fact names the page type it came
    from; semantic analysis is a later phase."""
    home = next((p for p in pages if p.page_type is PageType.HOME), None)
    types = sorted({p.page_type.value for p in pages}, key=lambda v: PRIORITY.index(PageType(v)))
    ai_by_page = {
        p.page_type.value: len(_AI_TERMS.findall(p.markdown)) for p in pages if p.markdown
    }
    return {
        "home_title": home.title if home else None,
        "home_description": home.description if home else None,
        "page_types_found": types,
        "has_careers_page": PageType.CAREERS.value in types,
        "has_pricing_page": PageType.PRICING.value in types,
        "has_blog_page": PageType.BLOG.value in types,
        "ai_term_mentions": sum(ai_by_page.values()),
        "ai_term_mentions_by_page": {k: v for k, v in ai_by_page.items() if v},
        "total_text_chars": sum(len(p.markdown) for p in pages),
    }


def mentions_company(pages: Sequence[PageLike], normalized_name: str) -> bool:
    """Identity check: does the site's homepage (title/description/text) name the company?

    Compares on alphanumerics only so "Dextr AI" matches "DextrAI" and "dextr-ai".
    """
    target = re.sub(r"[^0-9a-z]", "", normalized_name.lower())
    if len(target) < 3:
        return False
    home = next((p for p in pages if p.page_type is PageType.HOME), None)
    if home is None:
        return False
    haystack = " ".join(filter(None, [home.title, home.description, home.markdown[:20_000]]))
    return target in re.sub(r"[^0-9a-z]", "", haystack.lower())
