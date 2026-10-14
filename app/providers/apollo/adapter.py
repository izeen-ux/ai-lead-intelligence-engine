"""Live Apollo adapter (Phase 6) - routed through the enrichment sidecar.

Verified 2026-10-01 (docs.apollo.io): People Enrichment ``POST /api/v1/people/match`` consumes
credits only on a match (email reveal 1 credit; personal emails +1; phone +8). The sidecar calls
it with ``reveal_personal_emails=false`` and ``reveal_phone_number=false`` (work email only) and
treats ``match_confidence: none`` as a miss. Not used (documented): ``mixed_people/api_search``
(free, but needs a master key and returns obfuscated last names) and
``organizations/enrich`` (1 credit; the domain is already verified in Phase 3).
Apollo is a fallback/complement: its data never overwrites a higher-confidence Hunter email.
"""

from __future__ import annotations

from app.domain.enums import ProviderName
from app.providers.hunter.adapter import SidecarVendorAdapter


class ApolloAdapter(SidecarVendorAdapter):
    name = ProviderName.APOLLO
