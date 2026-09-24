"""Small, bounded HTTP client for URLs supplied by parser sources.

Unlike a validate-then-fetch wrapper, this module pins aiohttp connections to
the public addresses returned by :mod:`utils.net_safety`.  Every redirect is
validated again and response bodies are read through a hard byte limit.
"""
from __future__ import annotations

import asyncio
import socket
from collections.abc import Collection, Mapping
from urllib.parse import urljoin, urlsplit

import aiohttp

from utils.net_safety import UnsafeURLError, resolve_public_host, validate_public_url


DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Resolve hostnames to an entirely public, connection-pinned answer set."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict]:
        answers = await asyncio.to_thread(resolve_public_host, host, port, family)
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": answer_family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
            for answer_family, address in answers
        ]

    async def close(self) -> None:
        return None


def _host_allowed(url: str, allowed_hosts: Collection[str] | None) -> bool:
    if allowed_hosts is None:
        return True
    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    allowed = {host.casefold().rstrip(".") for host in allowed_hosts}
    return hostname in allowed


async def fetch_public_text(
    url: str,
    *,
    timeout_seconds: float = 15,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    headers: Mapping[str, str] | None = None,
    allowed_content_types: Collection[str] = ("text/html", "xml", "text/plain"),
    allowed_hosts: Collection[str] | None = None,
) -> str:
    """Fetch public HTTP(S) text with pinned DNS and bounded redirects/body.

    ``allowed_hosts`` is an exact hostname allow-list applied to the initial
    URL and every redirect.  It is useful for provider-specific fallbacks such
    as VK where leaving the provider domain is never required.
    """
    if max_response_bytes < 1:
        raise ValueError("max_response_bytes must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects must be non-negative")

    connector = aiohttp.TCPConnector(
        resolver=PublicOnlyResolver(),
        use_dns_cache=False,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    current = url
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        headers=dict(headers or {}),
    ) as session:
        for _ in range(max_redirects + 1):
            # Hostnames are resolved and pinned by PublicOnlyResolver.  Literal
            # addresses and URL syntax are still rejected before the request.
            current = validate_public_url(current, resolve_dns=False)
            if not _host_allowed(current, allowed_hosts):
                raise UnsafeURLError(
                    f"redirect hostname is outside the allow-list: {urlsplit(current).hostname}"
                )

            async with session.get(current, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        return ""
                    current = urljoin(current, location)
                    continue
                if response.status >= 400:
                    return ""

                content_type = response.headers.get("Content-Type", "").casefold()
                if allowed_content_types and not any(
                    marker.casefold() in content_type for marker in allowed_content_types
                ):
                    return ""
                try:
                    declared_size = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    declared_size = 0
                if declared_size > max_response_bytes:
                    return ""

                body = await response.content.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    return ""
                encoding = response.charset or "utf-8"
                return body.decode(encoding, errors="replace")

    return ""
