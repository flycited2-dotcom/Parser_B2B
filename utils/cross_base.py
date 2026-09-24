"""Исключение компаний, которые уже есть в других базах (HoReCa, отели).

Совпадение — только по точному email или корпоративному домену. Общие
почтовые домены и соцсети никогда не считаются доменом компании, иначе
одна запись hotel@mail.ru исключила бы всех клиентов на mail.ru.
"""
from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path

from utils.entity_resolution import website_domain

SHARED_HOSTS = frozenset({
    "mail.ru", "inbox.ru", "list.ru", "bk.ru", "internet.ru", "yandex.ru",
    "ya.ru", "yandex.com", "gmail.com", "googlemail.com", "rambler.ru",
    "outlook.com", "hotmail.com", "icloud.com", "me.com", "yahoo.com",
    "vk.com", "vk.ru", "instagram.com", "t.me", "ok.ru", "facebook.com",
    "taplink.cc", "2gis.ru", "avito.ru", "sutochno.ru", "ostrovok.ru",
})
EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
EMAIL_COLUMNS = frozenset({"email", "all_emails", "все email"})
WEBSITE_COLUMNS = frozenset({"website", "all_websites", "сайт"})
_SPLIT_RE = re.compile(r"[|,\s]+")


def env_paths(value: str | None = None) -> list[str]:
    raw = os.getenv("EXCLUDE_MASTERS", "") if value is None else value
    return [part.strip() for part in raw.split(";") if part.strip()]


def _read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    header = text.split("\n", 1)[0]
    delimiter = ";" if header.count(";") >= header.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def _domains(value: object) -> set[str]:
    return {
        domain
        for part in _SPLIT_RE.split(str(value or ""))
        if (domain := website_domain(part)) and domain not in SHARED_HOSTS
    }


def load_exclusions(paths) -> tuple[set[str], set[str], list[str]]:
    emails: set[str] = set()
    domains: set[str] = set()
    warnings: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            warnings.append(f"EXCLUDE_MASTERS: file not found: {raw_path}")
            continue
        try:
            rows = _read_rows(path)
        except (OSError, csv.Error) as exc:
            warnings.append(f"EXCLUDE_MASTERS: cannot read {raw_path}: {exc}")
            continue
        for row in rows:
            for column, value in row.items():
                if column is None:
                    continue
                name = column.strip().casefold()
                if name in EMAIL_COLUMNS:
                    emails.update(match.casefold() for match in EMAIL_RE.findall(str(value or "")))
                elif name in WEBSITE_COLUMNS:
                    domains.update(_domains(value))
    return emails, domains, warnings


def is_in_other_base(row: dict, emails: set[str], domains: set[str]) -> bool:
    own_emails = {
        match.casefold()
        for match in EMAIL_RE.findall(f"{row.get('email') or ''} {row.get('all_emails') or ''}")
    }
    if own_emails & emails:
        return True
    row_domains = _domains(f"{row.get('website') or ''} {row.get('all_websites') or ''}")
    row_domains |= {
        email.partition("@")[2] for email in own_emails
    } - SHARED_HOSTS
    return bool(row_domains & domains)
