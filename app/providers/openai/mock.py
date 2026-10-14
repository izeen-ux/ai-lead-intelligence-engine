"""Deterministic offline OpenAI stand-in (PROVIDER_MODE=mock, tests, dry runs, demo).

Same behaviour as the previous mock reasoning provider (a VALID grounded analysis built only
from the evidence bundle - it shows the pipeline, not real model quality), reported under the
OpenAI provider name so mock runs mirror the Phase 10 provider layout.
"""

from __future__ import annotations

from typing import ClassVar

from app.domain.enums import ProviderName
from app.providers.ai_router.mock import MockAiRouterProvider


class MockOpenAIProvider(MockAiRouterProvider):
    name: ClassVar[ProviderName] = ProviderName.OPENAI
