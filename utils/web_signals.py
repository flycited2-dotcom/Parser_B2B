"""Эвристические сигналы «повода для КП» по сайту компании.

Сигналы — подсказка для текста письма, а не утверждение о компании.
Результаты хранятся в JSON-кэше по домену и пересчитываются не чаще раза
в max_age_days. Все сетевые запросы идут через safe_http (SSRF-защита,
лимит размера, проверка редиректов).
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from config.hosts import host_of as website_host
from config.hosts import is_non_company_url as is_social_url
from config.segments import SEGMENT_BY_KEY
from utils.safe_http import fetch_public_text

SIGNAL_ORDER = (
    "no_website", "site_dead", "no_https", "no_mobile",
    "no_online_booking", "site_builder", "outdated",
)
PITCH_BY_SIGNAL = {
    "no_website": "Сайт с нуля",
    "site_dead": "Сайт не работает",
    "no_https": "Модернизация сайта",
    "no_mobile": "Адаптив/редизайн",
    "no_online_booking": "Онлайн-запись / бот",
    "site_builder": "Собственная разработка",
    "outdated": "Редизайн",
}
DEFAULT_PITCH = "Автоматизация/боты/CRM"
NOT_CHECKED = "not_checked"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

VIEWPORT_RE = re.compile(r"<meta[^>]+name=[\"']?viewport", re.IGNORECASE)
BUILDER_RE = re.compile(
    r"tildacdn\.com|tilda\.ws|data-tilda|wixstatic\.com|wix\.com|\.ucoz\.|ucoz\.ru|"
    r"narod\.ru|nethouse\.ru|ukit\.com|flexbe\.|craftum\.",
    re.IGNORECASE,
)
BOOKING_RE = re.compile(
    r"yclients|dikidi|sonline|записаться|запись онлайн|онлайн[- ]запись|appointment",
    re.IGNORECASE,
)
COPYRIGHT_YEAR_RE = re.compile(
    r"(?:©|&copy;|copyright)[^<]{0,40}?((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?",
    re.IGNORECASE,
)
OLD_TECH_RE = re.compile(
    r"jquery[-.]?1\.\d|\.swf\b|shockwave-flash|<table[^>]+width=[\"']?\d{3,4}",
    re.IGNORECASE,
)
PARKING_RE = re.compile(
    r"домен\s+(?:продается|продаётся|припаркован|зарегистрирован)|"
    r"this domain (?:is for sale|may be for sale)|parked (?:free|domain)|"
    r"hosting account (?:has been )?suspended",
    re.IGNORECASE,
)

Fetcher = Callable[..., Awaitable[str]]


def order_signals(signals) -> list[str]:
    present = set(signals or [])
    return [signal for signal in SIGNAL_ORDER if signal in present]


def pick_pitch(signals) -> str:
    for signal in SIGNAL_ORDER:
        if signal in (signals or []):
            return PITCH_BY_SIGNAL[signal]
    return DEFAULT_PITCH


def detect_html_signals(html: str, *, booking_relevant: bool, now_year: int) -> list[str]:
    signals: list[str] = []
    if not VIEWPORT_RE.search(html):
        signals.append("no_mobile")
    if booking_relevant and not BOOKING_RE.search(html):
        signals.append("no_online_booking")
    if BUILDER_RE.search(html):
        signals.append("site_builder")
    years = [
        int(year)
        for match in COPYRIGHT_YEAR_RE.finditer(html)
        for year in match.groups()
        if year
    ]
    if (years and max(years) <= now_year - 3) or OLD_TECH_RE.search(html):
        signals.append("outdated")
    return signals


async def _try_fetch(fetch: Fetcher, url: str) -> str:
    try:
        return await fetch(
            url,
            timeout_seconds=15,
            max_response_bytes=2 * 1024 * 1024,
            headers={"User-Agent": UA},
            allowed_content_types=("text/html",),
        )
    except Exception:
        return ""


async def probe_site(
    website: str,
    *,
    booking_relevant: bool,
    fetch: Fetcher = fetch_public_text,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    checked_at = now.isoformat(timespec="seconds")
    host = website_host(website)
    signals: list[str] = []
    html = await _try_fetch(fetch, f"https://{host}/")
    if not html:
        html = await _try_fetch(fetch, f"http://{host}/")
        if html:
            signals.append("no_https")
    if not html or PARKING_RE.search(html):
        return {"signals": ["site_dead"], "checked_at": checked_at}
    signals.extend(detect_html_signals(html, booking_relevant=booking_relevant, now_year=now.year))
    return {"signals": order_signals(signals), "checked_at": checked_at}


def load_cache(path: str) -> dict[str, dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(path: str, cache: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def _is_fresh(entry: dict, now: datetime, max_age_days: int) -> bool:
    try:
        checked = datetime.fromisoformat(str(entry.get("checked_at")))
    except (TypeError, ValueError):
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return now - checked < timedelta(days=max_age_days)


def _site_targets(master_csv: str) -> dict[str, bool]:
    """host → booking_relevant (True, если хотя бы одна компания домена такая)."""
    with open(master_csv, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    targets: dict[str, bool] = {}
    for row in rows:
        website = str(row.get("website") or "").strip()
        host = website_host(website)
        if not host or is_social_url(website):
            continue
        segment = SEGMENT_BY_KEY.get(str(row.get("client_type") or ""))
        targets[host] = targets.get(host, False) or bool(segment and segment.booking_relevant)
    return targets


async def refresh_signals(
    master_csv: str,
    cache_path: str,
    *,
    max_sites: int = 400,
    max_age_days: int = 30,
    parallel: int = 8,
    fetch: Fetcher = fetch_public_text,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    cache = load_cache(cache_path)
    targets = _site_targets(master_csv)
    stale = [host for host in targets if not _is_fresh(cache.get(host) or {}, now, max_age_days)]
    batch = stale[:max_sites] if max_sites else stale
    semaphore = asyncio.Semaphore(max(1, parallel))

    async def _one(host: str) -> None:
        async with semaphore:
            cache[host] = await probe_site(
                host, booking_relevant=targets[host], fetch=fetch, now=now
            )

    await asyncio.gather(*(_one(host) for host in batch))
    _save_cache(cache_path, cache)
    return {
        "sites": len(targets),
        "fresh": len(targets) - len(stale),
        "checked": len(batch),
        "pending": len(stale) - len(batch),
    }


def row_signals(row: dict, cache: dict) -> tuple[list[str], str]:
    website = str(row.get("website") or "").strip()
    if not website or is_social_url(website):
        signals = ["no_website"]
    else:
        entry = cache.get(website_host(website))
        if not entry:
            return [NOT_CHECKED], DEFAULT_PITCH
        signals = order_signals(entry.get("signals") or [])
    return signals, pick_pitch(signals)
