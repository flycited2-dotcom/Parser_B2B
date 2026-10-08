"""Память добора контактов между прогонами (по домену сайта).

Без неё каждый недельный прогон заново обходил одни и те же первые сайты: результат
прошлого раза терялся, а бюджет ENRICH_MAX_SITES уходил на уже проверенное.

Запись: {"checked_at", "status": found|none|dead, "via": static|browser, "contacts": {...}}.
found хранит найденные контакты — их можно применить к свежим строкам без сети.
Срок жизни зависит от статуса: найденное — долго, «ничего не нашли» — две недели,
недоступный сайт — неделя (сбой сети не должен клеймить сайт надолго).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTACT_KEYS = ("email", "all_emails", "phone", "all_phones", "address", "social", "all_socials")
STATUSES = ("found", "none", "dead")


class EnrichCache:
    def __init__(
        self, path, *, now=None, found_days: int = 60, none_days: int = 14, dead_days: int = 7,
        entries: dict | None = None,
    ) -> None:
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._days = {"found": found_days, "none": none_days, "dead": dead_days}
        self._entries: dict[str, dict] = entries if entries is not None else {}

    @classmethod
    def load(cls, path, **kwargs) -> "EnrichCache":
        entries: dict[str, dict] = {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            entries = {
                host: entry for host, entry in data.items()
                if isinstance(entry, dict) and entry.get("status") in STATUSES
            }
        return cls(path, entries=entries, **kwargs)

    def lookup(self, host: str) -> dict | None:
        """Свежая запись по домену или None."""
        entry = self._entries.get(host)
        if not entry:
            return None
        try:
            checked = datetime.fromisoformat(str(entry.get("checked_at")))
        except (TypeError, ValueError):
            return None
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if self._now() - checked >= timedelta(days=self._days[entry["status"]]):
            return None
        return entry

    def record(self, host: str, *, status: str, via: str, contacts: dict | None = None,
               checked_at: str | None = None) -> None:
        if not host or status not in STATUSES:
            return
        self._entries[host] = {
            "checked_at": checked_at or self._now().isoformat(timespec="seconds"),
            "status": status,
            "via": via,
            "contacts": {key: str(value) for key, value in (contacts or {}).items()
                         if key in CONTACT_KEYS and value},
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._entries, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
