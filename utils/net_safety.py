"""Network target validation used by crawlers and browser automation.

The parser consumes URLs supplied by third-party datasets.  Treat every such
URL as untrusted: only public HTTP(S) endpoints are allowed, and DNS answers
containing a non-public address are rejected fail-closed.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURLError(ValueError):
    """Raised when a URL may reach a local or otherwise non-public target."""


_NETWORK_SCHEMES = {"http", "https"}
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}
_NAT64_NETWORKS = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def _parse_ip_literal(address: str):
    """Parse canonical and legacy IPv4 spellings used in SSRF bypasses."""
    raw = address.split("%", 1)[0]
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        pass
    # inet_aton additionally recognises forms such as 127.1, 2130706433 and
    # 0x7f000001 that URL clients commonly interpret as 127.0.0.1.
    try:
        return ipaddress.IPv4Address(socket.inet_aton(raw))
    except (OSError, ValueError):
        return None


def _public_ip(address: str) -> str:
    """Return a canonical IP string or reject non-global address space."""
    # getaddrinfo may include an IPv6 scope id (``fe80::1%eth0``).
    ip = _parse_ip_literal(address)
    if ip is None:
        raise UnsafeURLError(f"invalid IP address: {address!r}")
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UnsafeURLError(f"non-public IP address is blocked: {ip}")
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        for network in _NAT64_NETWORKS:
            if ip in network:
                embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
                break
        for inner in embedded:
            _public_ip(str(inner))
    return str(ip)


def _normalise_hostname(hostname: str) -> str:
    host = (hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL hostname is empty")
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        raise UnsafeURLError(f"local hostname is blocked: {host}")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeURLError(f"invalid hostname: {hostname!r}") from exc


def resolve_public_host(
    hostname: str,
    port: int,
    family: int = socket.AF_UNSPEC,
) -> tuple[tuple[int, str], ...]:
    """Resolve *hostname* and return only an entirely public DNS answer set.

    Rejecting a mixed public/private answer is deliberate: choosing only the
    public record would leave room for DNS rebinding and resolver-order tricks.
    """
    host = _normalise_hostname(hostname)
    literal = _parse_ip_literal(host)

    if literal is not None:
        public = _public_ip(str(literal))
        ip_family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        return ((ip_family, public),)

    try:
        answers = socket.getaddrinfo(
            host,
            port,
            family=family,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise UnsafeURLError(f"DNS resolution failed for {host}: {exc}") from exc

    resolved: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for answer_family, _socktype, _proto, _canonname, sockaddr in answers:
        public = _public_ip(sockaddr[0])
        item = (answer_family, public)
        if item not in seen:
            seen.add(item)
            resolved.append(item)
    if not resolved:
        raise UnsafeURLError(f"DNS returned no usable addresses for {host}")
    return tuple(resolved)


def validate_public_url(url: str, *, resolve_dns: bool = True) -> str:
    """Validate and return a public HTTP(S) URL.

    Literal IPs are always checked, even when ``resolve_dns`` is false.  The
    latter mode is intended for clients with a public-only resolver of their
    own (for example aiohttp's connector in ``parsers/crawler.py``).
    """
    if not isinstance(url, str):
        raise UnsafeURLError("URL must be a string")
    clean = url.strip()
    if not clean or any(ord(ch) < 32 for ch in clean):
        raise UnsafeURLError("URL is empty or contains control characters")
    try:
        parts = urlsplit(clean)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {url!r}") from exc
    scheme = parts.scheme.lower()
    if scheme not in _NETWORK_SCHEMES:
        raise UnsafeURLError(f"URL scheme is blocked: {scheme or '<empty>'}")
    if not parts.netloc or not parts.hostname:
        raise UnsafeURLError("URL must contain a hostname")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URL userinfo is not allowed")

    host = _normalise_hostname(parts.hostname)
    effective_port = port or (443 if scheme == "https" else 80)
    literal = _parse_ip_literal(host)
    if literal is not None:
        _public_ip(str(literal))
    elif resolve_dns:
        resolve_public_host(host, effective_port)
    return clean


async def validate_public_url_async(url: str, *, resolve_dns: bool = True) -> str:
    """Async wrapper that keeps blocking DNS resolution off the event loop."""
    return await asyncio.to_thread(validate_public_url, url, resolve_dns=resolve_dns)
