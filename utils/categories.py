"""Нормализация category → client_type (ключ B2B-сегмента).

Таксономия живёт в config/segments.py; модуль сохранён, чтобы storage,
VK, crawler и email_finder продолжали вызывать `normalize()` как раньше.
"""
from config.segments import DEFAULT_SEGMENT, SEGMENTS, normalize_segment

CANONICAL = [segment.key for segment in SEGMENTS]
DEFAULT = DEFAULT_SEGMENT


def normalize(category: str) -> str:
    """Привести category к ключу сегмента. Пустое/неизвестное → 'прочее'."""
    return normalize_segment(category)
