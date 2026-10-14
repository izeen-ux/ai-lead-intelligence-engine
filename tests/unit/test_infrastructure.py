"""Errors, repositories, database check, startup checks, network guard."""

from __future__ import annotations

import socket
import urllib.request
from uuid import uuid4

import pytest

from app.core.errors import (
    AppError,
    NotFoundError,
    ProviderError,
    ProviderUnavailableError,
)
from app.db.session import check_database
from app.domain.models import Company
from app.repositories.base import InMemoryRepository, Repository
from app.services.startup import run_startup_checks
from app.utils.network_guard import ExternalNetworkBlockedError


def test_error_hierarchy_and_serialization() -> None:
    err = ProviderUnavailableError("firecrawl", "timeout", details={"attempt": 2})
    assert isinstance(err, ProviderError) and isinstance(err, AppError)
    assert err.retryable is True
    assert err.to_dict() == {
        "code": "provider_unavailable",
        "message": "timeout",
        "details": {"provider": "firecrawl", "attempt": 2},
    }
    assert NotFoundError("x").retryable is False


def test_in_memory_repository_satisfies_protocol() -> None:
    repo: Repository[Company] = InMemoryRepository[Company]()
    company = repo.add(Company(name="Acme"))
    assert repo.get(company.id) is company
    assert repo.list() == [company]
    with pytest.raises(NotFoundError):
        repo.get(uuid4())


def test_database_not_configured(settings) -> None:
    assert check_database(settings) == ("not_configured", "DATABASE_URL not set")


def test_database_unreachable_reports_error_without_leaking_password(make_settings) -> None:
    # Port 9 on loopback: nothing listens, connection is refused immediately.
    s = make_settings(
        database_url="postgresql+psycopg://user:hunter2-secret@127.0.0.1:9/db",
        database_connect_timeout_seconds=1,
    )
    state, detail = check_database(s)
    assert state == "error"
    assert detail is not None and "hunter2-secret" not in detail


def test_startup_checks_warn_on_risky_switches(make_settings) -> None:
    assert run_startup_checks(make_settings()) == [
        "DATABASE_URL not set: running without persistence"
    ]
    warnings = run_startup_checks(
        make_settings(provider_mode="live", allow_paid_provider_calls=True)
    )
    joined = " | ".join(warnings)
    assert "PROVIDER_MODE=live" in joined
    assert "missing provider credentials" in joined
    assert "ALLOW_PAID_PROVIDER_CALLS=true" in joined


def test_network_guard_blocks_external_but_allows_loopback(network_attempts) -> None:
    with pytest.raises(ExternalNetworkBlockedError):
        socket.create_connection(("api.firecrawl.dev", 443), timeout=1)
    with pytest.raises(ExternalNetworkBlockedError):
        urllib.request.urlopen("https://api.hunter.io/v2/account", timeout=1)
    with pytest.raises(ExternalNetworkBlockedError):
        socket.socket().connect(("8.8.8.8", 53))
    assert len(network_attempts) == 3
    network_attempts.clear()  # expected attempts; keep the fixture clean

    # loopback must still work (asyncio / local services)
    a, b = socket.socketpair()
    a.close()
    b.close()
