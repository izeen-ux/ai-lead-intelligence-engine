from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    PAID_PROVIDERS,
    Environment,
    ProviderMode,
    Settings,
    get_settings,
)
from app.domain.enums import ProviderName


def test_defaults_load_without_any_secrets(settings: Settings) -> None:
    assert settings.provider_mode is ProviderMode.MOCK
    assert settings.allow_paid_provider_calls is False
    assert settings.database_url is None
    assert settings.funding_cap_inr == Decimal("50000000")
    assert settings.secret_values() == []


def test_env_vars_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("PROVIDER_MODE", "live")
    monkeypatch.setenv("HUNTER_API_KEY", "hunter-test-key-123")
    s = Settings(_env_file=None)
    assert s.app_env is Environment.STAGING
    assert s.provider_mode is ProviderMode.LIVE
    assert s.hunter_api_key is not None
    assert s.hunter_api_key.get_secret_value() == "hunter-test-key-123"


def test_env_file_is_read(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("APIFY_TOKEN=apify-from-file\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    s = Settings(_env_file=env)
    assert s.apify_token is not None
    assert s.apify_token.get_secret_value() == "apify-from-file"
    assert s.log_level == "DEBUG"


def test_secrets_never_appear_in_repr(make_settings) -> None:
    s = make_settings(apollo_api_key="super-secret-apollo", database_url="postgresql://u:pw@h/db")
    text = repr(s) + str(s) + s.model_dump_json()
    assert "super-secret-apollo" not in text
    assert "pw@h" not in text
    assert set(s.secret_values()) == {"super-secret-apollo", "postgresql://u:pw@h/db"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"log_level": "LOUD"}, "log_level"),
        ({"api_port": 70000}, "api_port"),
        ({"provider_mode": "yolo"}, "provider_mode"),
        ({"funding_cap_inr": 0}, "funding_cap_inr"),
        ({"apify_max_charge_usd_per_run": "0.05"}, "apify_max_charge_usd_per_run"),
        ({"monthly_lead_target_min": 40, "monthly_lead_target_max": 30}, "MONTHLY_LEAD_TARGET"),
        ({"app_env": "production"}, "DATABASE_URL is required"),
        (
            {"app_env": "production", "debug": True, "database_url": "postgresql://x/y"},
            "DEBUG must be false",
        ),
    ],
)
def test_invalid_configuration_is_rejected(make_settings, overrides, message) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**overrides)


def test_missing_credentials_report(make_settings) -> None:
    s = make_settings(apify_token="a" * 10, hunter_api_key="   ")
    missing = set(s.missing_provider_credentials())
    assert ProviderName.APIFY not in missing
    assert ProviderName.HUNTER in missing  # blank counts as missing
    assert {ProviderName.FIRECRAWL, ProviderName.APOLLO} <= missing
    # keyless providers are never reported missing
    assert ProviderName.CRAWL4AI not in missing
    assert ProviderName.LAYA not in missing


def test_enrichment_kit_is_treated_as_paid() -> None:
    assert ProviderName.ENRICHMENT_KIT in PAID_PROVIDERS
    assert ProviderName.CRAWL4AI not in PAID_PROVIDERS


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
