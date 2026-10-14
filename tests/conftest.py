"""Shared fixtures.

* Outbound network is blocked for EVERY test (autouse) - a test that tries to reach a real
  provider fails loudly instead of silently spending credits.
* Provider/DB env vars from the developer's shell or ``.env`` are removed so tests are hermetic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import keyring
from app.core.config import Settings, get_settings
from app.db.session import dispose_engine
from app.main import create_app
from app.providers.registry import ProviderRegistry
from app.utils.network_guard import block_external_network


@pytest.fixture(autouse=True)
def network_attempts() -> Iterator[list[str]]:
    with block_external_network() as attempts:
        yield attempts


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    get_settings.cache_clear()
    dispose_engine()
    keyring._RINGS.clear()  # which backup key is used up must not leak between tests
    yield
    get_settings.cache_clear()
    dispose_engine()


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Build settings from explicit values only (never reads ``.env``)."""

    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {"app_env": "test", "log_format": "console"}
        base.update(overrides)
        return Settings(_env_file=None, **base)

    return _make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
def registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(settings)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c
