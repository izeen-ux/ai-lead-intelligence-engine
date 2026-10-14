"""Block outbound (non-loopback) network access within a scope.

Used by the test suite (autouse) and ``scripts/smoke_check.py`` to *prove* that no external
provider is contacted. Loopback stays allowed because the Windows asyncio event loop and local
test servers use ``127.0.0.1`` internally.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", ""})


class ExternalNetworkBlockedError(RuntimeError):
    """Raised when code attempts a non-loopback connection or DNS lookup."""


def _is_local_host(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode()
    if not isinstance(host, str):
        return False
    if host.lower() in _LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _is_local_address(address: Any) -> bool:
    if isinstance(address, tuple) and address:
        return _is_local_host(address[0])
    return True  # AF_UNIX path or other non-IP address family


@contextmanager
def block_external_network(attempts: list[str] | None = None) -> Iterator[list[str]]:
    """Within the block, external connects/DNS raise ``ExternalNetworkBlockedError``.

    Every blocked attempt is appended to ``attempts`` (returned) for assertions/reporting.
    """
    recorded = attempts if attempts is not None else []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _block(target: str) -> ExternalNetworkBlockedError:
        recorded.append(target)
        return ExternalNetworkBlockedError(f"external network access blocked: {target}")

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if not _is_local_address(address):
            raise _block(repr(address))
        real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_local_address(address):
            raise _block(repr(address))
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local_host(host):
            raise _block(f"dns:{host}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield recorded
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo
