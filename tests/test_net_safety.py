import socket

import pytest

from utils.net_safety import UnsafeURLError, resolve_public_host, validate_public_url
from parsers.site_finder import _SafeRedirectHandler, _safe_candidate


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.1/admin",
    "http://2130706433/admin",
    "http://0x7f000001/admin",
    "http://0177.0.0.1/admin",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[2002:7f00:1::]/",
    "http://[fc00::1]/",
    "http://[fe80::1]/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",
    "http://0.0.0.0/",
    "http://224.0.0.1/",
    "http://192.0.2.1/",
    "http://localhost/",
    "file:///etc/passwd",
    "ftp://example.com/file",
    "http://user@example.com/",
])
def test_blocks_non_public_targets_without_dns(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url, resolve_dns=False)


def test_allows_public_ip_literal():
    assert validate_public_url("https://93.184.216.34/path") == (
        "https://93.184.216.34/path"
    )


def test_dns_private_answer_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
    ])
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_public_url("http://attacker.example/")


def test_mixed_public_private_dns_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", 80)),
    ])
    with pytest.raises(UnsafeURLError, match="non-public"):
        resolve_public_host("mixed.example", 80)


def test_public_dns_answer_is_returned(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
    ])
    assert resolve_public_host("example.com", 443) == (
        (socket.AF_INET, "93.184.216.34"),
    )


def test_site_finder_rejects_private_result_url():
    assert _safe_candidate("http://127.0.0.1/admin") is None


def test_site_finder_rejects_private_redirect():
    handler = _SafeRedirectHandler()
    with pytest.raises(UnsafeURLError):
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "http://169.254.169.254/latest/meta-data/",
        )
