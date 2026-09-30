"""Общие хосты: соцсети, мессенджеры, агрегаторы, площадки и бесплатная почта.

Такой хост никогда не является «сайтом компании»: ссылка на него в поле
website означает отсутствие собственного сайта, а совпадение по нему не
доказывает, что две записи — одна компания. Единый список используют
web_signals (no_website), cross_base (исключение из других баз) и VK-гейт.
"""
from __future__ import annotations

from urllib.parse import urlparse

SOCIAL_HOSTS = frozenset({
    "vk.com", "vk.ru", "m.vk.com", "instagram.com", "t.me", "telegram.me",
    "ok.ru", "facebook.com", "youtube.com", "youtu.be", "wa.me", "whatsapp.com",
    "taplink.cc", "taplink.ru", "linktr.ee", "dzen.ru", "rutube.ru",
})
PLATFORM_HOSTS = frozenset({
    "yandex.ru", "yandex.com", "2gis.ru", "avito.ru", "youla.ru",
    "booking.com", "tvil.ru", "sutochno.ru", "ostrovok.ru", "tripadvisor.ru",
    "tripadvisor.com", "zoon.ru", "flamp.ru", "yell.ru", "hh.ru",
    "pulscen.ru", "tiu.ru", "satu.kz", "ozon.ru", "wildberries.ru",
})
FREE_MAIL_HOSTS = frozenset({
    "mail.ru", "inbox.ru", "list.ru", "bk.ru", "internet.ru", "ya.ru",
    "yandex.ru", "yandex.com", "gmail.com", "googlemail.com", "rambler.ru",
    "outlook.com", "hotmail.com", "icloud.com", "me.com", "yahoo.com",
    "ukr.net", "i.ua", "yandex.ua", "yandex.by", "yandex.kz", "yandex.kg", "tut.by", "bigmir.net",
    "meta.ua", "mail.ua", "rambler.ua", "live.com", "msn.com", "aol.com", "proton.me",
    "protonmail.com", "pm.me", "gmx.com", "gmx.net", "zoho.com", "fastmail.com",
})
SHARED_HOSTS = SOCIAL_HOSTS | PLATFORM_HOSTS | FREE_MAIL_HOSTS


def host_of(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        host = urlparse(raw if "://" in raw else "https://" + raw).hostname or ""
    except ValueError:
        return ""
    host = host.casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, hosts: frozenset[str]) -> bool:
    return any(host == known or host.endswith("." + known) for known in hosts)


def is_shared_host(host: str) -> bool:
    return _matches(host, SHARED_HOSTS)


def is_non_company_url(url: object) -> bool:
    """Соцсеть/агрегатор/площадка в поле сайта — это не собственный сайт."""
    host = host_of(url)
    return bool(host) and _matches(host, SOCIAL_HOSTS | PLATFORM_HOSTS)
