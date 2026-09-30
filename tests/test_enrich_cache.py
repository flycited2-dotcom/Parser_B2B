"""Память добора контактов между прогонами."""
import json
from datetime import datetime, timedelta, timezone

from parsers.enrich_cache import EnrichCache

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make(tmp_path, **kwargs):
    return EnrichCache(tmp_path / "cache.json", now=lambda: NOW, **kwargs)


def aged(days):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


def test_freshness_depends_on_status(tmp_path):
    cache = make(tmp_path)
    for host, status in (("f.ru", "found"), ("n.ru", "none"), ("d.ru", "dead")):
        cache.record(host, status=status, via="browser", checked_at=aged(10))
    assert cache.lookup("f.ru") is not None  # найденное хранится 60 дней
    assert cache.lookup("n.ru") is not None  # «ничего» — 14 дней
    assert cache.lookup("d.ru") is None  # недоступный сайт перепроверяется через 7 дней


def test_entries_expire(tmp_path):
    cache = make(tmp_path)
    cache.record("n.ru", status="none", via="browser", checked_at=aged(20))
    cache.record("f.ru", status="found", via="static", checked_at=aged(70))
    assert cache.lookup("n.ru") is None and cache.lookup("f.ru") is None


def test_custom_none_days(tmp_path):
    cache = make(tmp_path, none_days=30)
    cache.record("n.ru", status="none", via="browser", checked_at=aged(20))
    assert cache.lookup("n.ru") is not None


def test_roundtrip_keeps_contacts_and_ignores_unknown_fields(tmp_path):
    cache = make(tmp_path)
    cache.record("a.ru", status="found", via="static",
                 contacts={"email": "info@a.ru", "all_emails": "info@a.ru | b@a.ru", "junk": "x", "phone": ""})
    cache.save()

    reloaded = EnrichCache.load(tmp_path / "cache.json", now=lambda: NOW)
    entry = reloaded.lookup("a.ru")
    assert entry["status"] == "found" and entry["via"] == "static"
    assert entry["contacts"] == {"email": "info@a.ru", "all_emails": "info@a.ru | b@a.ru"}


def test_corrupt_or_missing_file_gives_empty_cache(tmp_path):
    path = tmp_path / "cache.json"
    assert EnrichCache.load(path).lookup("a.ru") is None
    path.write_text("{not json", encoding="utf-8")
    assert EnrichCache.load(path).lookup("a.ru") is None
    path.write_text(json.dumps({"a.ru": {"status": "weird", "checked_at": aged(1)}, "b.ru": "oops"}), encoding="utf-8")
    cache = EnrichCache.load(path, now=lambda: NOW)
    assert cache.lookup("a.ru") is None and cache.lookup("b.ru") is None


def test_save_is_atomic_and_creates_parent_directory(tmp_path):
    cache = EnrichCache(tmp_path / "deep" / "dir" / "cache.json", now=lambda: NOW)
    cache.record("a.ru", status="none", via="browser")
    cache.save()
    assert (tmp_path / "deep" / "dir" / "cache.json").is_file()
    assert not list((tmp_path / "deep" / "dir").glob("*.tmp"))
