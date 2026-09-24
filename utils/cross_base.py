"""Исключение компаний, которые уже есть в других базах (HoReCa, отели).

Совпадение — только по точному email или корпоративному домену. Общие
хосты (почта, соцсети, площадки бронирования) никогда не считаются доменом
компании, иначе одна запись hotel@mail.ru исключила бы всех клиентов на
mail.ru. Хост, который встречается у многих записей чужой базы, тоже
считается площадкой, даже если его нет в списке.
"""
from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
from pathlib import Path

from config.hosts import is_shared_host
from utils.entity_resolution import website_domain

EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
EMAIL_COLUMNS = frozenset({"email", "all_emails", "все email"})
WEBSITE_COLUMNS = frozenset({"website", "all_websites", "сайт"})
# Корпоративный домен сети отелей может стоять у нескольких филиалов;
# хост у большего числа записей — это площадка (tvil, booking и т.п.).
MAX_ROWS_PER_DOMAIN = 5
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
        if (domain := website_domain(part)) and not is_shared_host(domain)
    }


def load_exclusions(paths) -> tuple[set[str], set[str], list[str]]:
    emails: set[str] = set()
    domain_rows: Counter = Counter()
    warnings: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            if not path.is_file():
                warnings.append(f"EXCLUDE_MASTERS: file not found: {raw_path}")
                continue
            rows = _read_rows(path)
        except (OSError, csv.Error) as exc:
            warnings.append(f"EXCLUDE_MASTERS: cannot read {raw_path}: {exc}")
            continue
        known_columns = False
        for row in rows:
            row_domains: set[str] = set()
            for column, value in row.items():
                if column is None:
                    continue
                name = column.strip().casefold()
                if name in EMAIL_COLUMNS:
                    known_columns = True
                    emails.update(match.casefold() for match in EMAIL_RE.findall(str(value or "")))
                elif name in WEBSITE_COLUMNS:
                    known_columns = True
                    row_domains |= _domains(value)
            domain_rows.update(row_domains)
        if not known_columns:
            warnings.append(f"EXCLUDE_MASTERS: no email/website columns in {raw_path}")
    domains = {domain for domain, count in domain_rows.items() if count <= MAX_ROWS_PER_DOMAIN}
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
        domain
        for email in own_emails
        if not is_shared_host(domain := email.partition("@")[2])
    }
    return bool(row_domains & domains)
