"""Public HTTP(S) URL validation and browser-context network guards.

Application-level DNS checks are defense in depth, not a substitute for
network-level egress controls (see the README for remaining limitations).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)
DNS_RESOLUTION_TIMEOUT_SECONDS = 3.0


class URLPolicyError(ValueError):
    """Raised when a URL is malformed or violates the outbound URL policy."""


_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_HEX_NUMBER = re.compile(r"^0x[0-9a-f]*$", re.IGNORECASE)


def _is_whatwg_ipv4_number(label: str) -> bool:
    """Recognize numeric forms browsers may interpret as legacy IPv4 parts."""

    if _HEX_NUMBER.fullmatch(label):
        return True
    if len(label) > 1 and label.startswith("0") and all(char in "01234567" for char in label[1:]):
        return True
    return label.isascii() and label.isdecimal()


def _is_ambiguous_legacy_ipv4(host: str) -> bool:
    # WHATWG URL parsing treats a hostname ending in a numeric component as an
    # IPv4 candidate, including hexadecimal and octal spellings.
    return bool(host) and _is_whatwg_ipv4_number(host.rsplit(".", 1)[-1])


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    if mapped is not None:
        return _is_public_address(mapped)
    return (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _normalize_and_validate_host(
    host: str,
) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    if not host or not host.isascii() or any(ord(character) < 33 or ord(character) == 127 for character in host):
        raise URLPolicyError("Hostname must be valid ASCII")
    if host.endswith("."):
        host = host[:-1]
    if not host or "%" in host:
        raise URLPolicyError("Invalid hostname")

    try:
        address = ipaddress.ip_address(host)
        return address.compressed.lower(), address
    except ValueError:
        pass

    ascii_host = host.lower()
    if len(ascii_host) > 253:
        raise URLPolicyError("Hostname is too long")
    labels = ascii_host.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise URLPolicyError("Malformed hostname")
    if ascii_host == "localhost" or ascii_host.endswith(".localhost"):
        raise URLPolicyError("Localhost destinations are not allowed")
    if _is_ambiguous_legacy_ipv4(ascii_host):
        raise URLPolicyError("Ambiguous numeric hostname formats are not allowed")
    return ascii_host, None


def _resolve_host(host: str) -> list[str]:
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise URLPolicyError(f"Could not resolve hostname: {host}") from exc
    addresses = list(dict.fromkeys(record[4][0].split("%", 1)[0] for record in records))
    if not addresses:
        raise URLPolicyError(f"Hostname resolved to no addresses: {host}")
    return addresses


def _canonical_url(parsed: Any, normalized_host: str) -> str:
    if ":" in normalized_host:
        netloc_host = f"[{normalized_host}]"
    else:
        netloc_host = normalized_host
    port = parsed.port
    netloc = f"{netloc_host}:{port}" if port is not None else netloc_host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment))


async def validate_target_url(
    raw_url: str,
    *,
    resolver: Callable[[str], list[str]] | None = None,
    dns_timeout_seconds: float = DNS_RESOLUTION_TIMEOUT_SECONDS,
) -> str:
    """Validate and canonicalize a public HTTP(S) URL.

    Hostnames must be ASCII for this milestone, avoiding differences between
    Python IDNA and browser hostname canonicalization. DNS resolution runs in a
    worker thread and fails closed after the configured timeout.
    """

    if not isinstance(raw_url, str) or not raw_url or any(ord(c) < 32 for c in raw_url):
        raise URLPolicyError("URL is empty or contains control characters")
    if any(character.isspace() for character in raw_url) or "\\" in raw_url:
        raise URLPolicyError("URL contains invalid whitespace or backslashes")
    if dns_timeout_seconds <= 0:
        raise ValueError("DNS timeout must be positive")
    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        _ = parsed.port  # Access validates malformed and out-of-range ports.
    except ValueError as exc:
        raise URLPolicyError("Malformed URL") from exc

    if scheme not in {"http", "https"}:
        raise URLPolicyError("Only HTTP and HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise URLPolicyError("URLs containing embedded credentials are not allowed")
    if not parsed.netloc or host is None:
        raise URLPolicyError("URL must include a hostname")

    normalized_host, address = _normalize_and_validate_host(host)
    if address is not None:
        if not _is_public_address(address):
            raise URLPolicyError("Non-public IP destinations are not allowed")
        return _canonical_url(parsed, normalized_host)

    resolve = resolver or _resolve_host
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(resolve, normalized_host), timeout=dns_timeout_seconds
        )
    except TimeoutError as exc:
        raise URLPolicyError("DNS resolution timed out") from exc
    except URLPolicyError:
        raise
    except Exception as exc:
        raise URLPolicyError(f"Could not resolve hostname: {normalized_host}") from exc
    if not addresses:
        raise URLPolicyError(f"Hostname resolved to no addresses: {normalized_host}")
    for value in addresses:
        try:
            resolved = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise URLPolicyError("Hostname returned an invalid IP address") from exc
        if not _is_public_address(resolved):
            raise URLPolicyError(
                f"Hostname resolves to a non-public IP address: {resolved.compressed}"
            )
    return _canonical_url(parsed, normalized_host)


async def install_context_url_guard(context: Any) -> None:
    """Guard HTTP requests and block all WebSockets for one browser context."""

    async def guard(route: Any) -> None:
        request_url = route.request.url
        try:
            parsed = urlsplit(request_url)
            if parsed.scheme.lower() not in {"http", "https"}:
                raise URLPolicyError("Only HTTP and HTTPS requests are allowed")
            safe_url = await validate_target_url(request_url)
        except Exception:
            # Do not log complete URLs, which can contain sensitive query data.
            logger.warning("Aborting browser request rejected by URL policy")
            await route.abort("blockedbyclient")
            return
        await route.continue_(url=safe_url)

    async def block_websocket(web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="WebSocket connections are disabled")

    await context.route("**/*", guard)
    # Playwright's context-level API intercepts sockets created by every page
    # after registration. Do not connect the routed socket to a server.
    await context.route_web_socket("**/*", block_websocket)
