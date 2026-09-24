import csv
import html
import os
import re
from datetime import datetime

from config.segments import exclusion_flags
from utils.categories import normalize as normalize_category
from utils import dedup, progress
from utils.csv_safety import neutralize_csv_formula
from utils.entity_resolution import (
    CONTACT_FIELDS,
    canonicalize_cluster,
    cluster_rows,
    normalize_name as _entity_normalize_name,
    observation_identity,
    provenance_for_rows,
)
from utils.quality import is_master_anchor

FIELDS = [
    "city", "name", "client_type", "category",
    "address", "phone", "email", "website", "social",
    "comment", "source", "parsed_at",
    # Stable entity/observation metadata.  Existing consumers can continue
    # reading the original first 12 columns unchanged.
    "entity_id", "observation_id", "source_id", "source_url",
    "latitude", "longitude", "raw_category", "matched_query",
    "confidence", "quality_flags", "sources", "provenance",
    "first_seen", "last_seen",
    # Flattened alternatives retained across sources/pages.  The singular
    # fields above remain the preferred values used by existing consumers.
    "all_phones", "all_emails", "all_websites", "all_socials",
]

OUTPUT_DIR = "output"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, f"result_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")

# Разделитель `;` — RU Excel читает столбцы из коробки.
CSV_DELIMITER = ";"

_seen = set()
_rows = []
# Header пишем один раз при первом append. Сбрасывается на _rewrite_all.
_header_written = False


def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return (raw or "").strip()
    return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: str) -> str:
    """Готовим значение к записи в CSV.

    - убираем HTML-теги (VK иногда отдаёт name с <a>/<br>; ломали TG parse_mode=HTML)
    - декодируем HTML entities (&amp; → &, &nbsp; → пробел)
    - схлопываем whitespace и переносы — иначе Excel ломает строку
    """
    if not value:
        return ""
    s = str(value)
    s = _HTML_TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ").replace(" ", " ")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def _csv_safe_row(row: dict) -> dict:
    """Neutralize spreadsheet formulas at the serialization boundary."""
    return {
        field: neutralize_csv_formula(str(row.get(field) or ""))
        for field in FIELDS
    }


def _key(item):
    """Per-source observation identity (never an entity-level name/city key)."""
    return observation_identity(item)


def _prepare_item(item: dict) -> dict:
    """Clean an observation and attach persistent/provenance metadata."""
    raw = dict(item)
    # Accept legacy parser keys while all sources migrate to the explicit
    # latitude/longitude schema.
    if raw.get("latitude") in (None, "") and raw.get("lat") not in (None, ""):
        raw["latitude"] = raw.get("lat")
    if raw.get("longitude") in (None, "") and raw.get("lon") not in (None, ""):
        raw["longitude"] = raw.get("lon")

    state = dedup.touch_observation(raw)
    raw["observation_id"] = state["observation_id"]
    raw["first_seen"] = raw.get("first_seen") or state["first_seen"]
    raw["last_seen"] = state["last_seen"]
    raw["sources"] = raw.get("sources") or raw.get("source", "")
    for multi_field, primary_field in {
        "all_phones": "phone",
        "all_emails": "email",
        "all_websites": "website",
        "all_socials": "social",
    }.items():
        if not raw.get(multi_field) and raw.get(primary_field):
            raw[multi_field] = raw[primary_field]

    cleaned = {field: _clean(raw.get(field, "")) for field in FIELDS}
    if cleaned.get("phone"):
        cleaned["phone"] = normalize_phone(cleaned["phone"])
    if not cleaned.get("client_type"):
        cleaned["client_type"] = normalize_category(cleaned.get("category", ""))
    extra_flags = exclusion_flags(
        cleaned.get("name", ""),
        f"{cleaned.get('category', '')} {cleaned.get('raw_category', '')}",
    )
    if extra_flags:
        existing_flags = [flag for flag in cleaned.get("quality_flags", "").split("|") if flag]
        cleaned["quality_flags"] = "|".join(dict.fromkeys(existing_flags + extra_flags))
    cleaned["provenance"] = provenance_for_rows([cleaned])
    return cleaned


def _merge_same_observation(existing: dict, incoming: dict) -> bool:
    """Update an in-run duplicate instead of dropping richer values."""
    merged = canonicalize_cluster([existing, incoming], FIELDS)
    # It is still one source observation, not a canonical cross-source row.
    merged["entity_id"] = existing.get("entity_id", "")
    merged["observation_id"] = existing["observation_id"]
    merged["source"] = existing.get("source") or incoming.get("source", "")
    merged["source_id"] = existing.get("source_id") or incoming.get("source_id", "")
    merged["source_url"] = existing.get("source_url") or incoming.get("source_url", "")
    merged["sources"] = existing.get("sources") or incoming.get("sources", "")
    changed = any(existing.get(field, "") != merged.get(field, "") for field in FIELDS)
    if changed:
        existing.clear()
        existing.update(merged)
    return changed


def save_item(item):
    global _rows
    if not item.get("name"):
        return False
    cleaned = _prepare_item(item)
    k = cleaned["observation_id"]
    # A duplicate observation inside this process may be richer (for example,
    # the same Yandex card found by two queries).  Merge it in place instead of
    # discarding it.  Different sources always have different observation ids.
    if k in _seen:
        existing = next((row for row in _rows if row.get("observation_id") == k), None)
        if existing is not None and _merge_same_observation(existing, cleaned):
            _rewrite_all()
        return False
    _seen.add(k)

    _rows.append(cleaned)
    _append_last()
    # обновляем счётчик в progress.json раз в 10 записей (чтобы не перегружать диск)
    if len(_rows) % 10 == 0:
        try:
            progress.mark_count(len(_rows))
        except Exception:
            pass
    print(f"  ✓ [{cleaned['source']}] {cleaned['city']} | {cleaned['name']} | "
          f"{cleaned.get('phone') or '—'} | {cleaned.get('email') or '—'}")
    return True


def _append_last():
    """Дописать последнюю строку _rows в CSV (без rewrite всего файла).

    Header пишется один раз при первом вызове. Для больших прогонов это
    линейная сложность, в отличие от O(N²) полного rewrite на каждый save.
    """
    global _header_written
    if not _rows:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mode = "a" if _header_written else "w"
    with open(OUTPUT_FILE, mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=FIELDS,
            delimiter=CSV_DELIMITER, quoting=csv.QUOTE_ALL,
            extrasaction="ignore", restval="",
        )
        if not _header_written:
            writer.writeheader()
            _header_written = True
        writer.writerow(_csv_safe_row(_rows[-1]))


def _rewrite_all():
    """Полный rewrite файла из _rows. Используется после cross_source_merge,
    которая мутирует существующие строки in-place."""
    global _header_written
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=FIELDS,
            delimiter=CSV_DELIMITER, quoting=csv.QUOTE_ALL,
            extrasaction="ignore", restval="",
        )
        writer.writeheader()
        writer.writerows(_csv_safe_row(row) for row in _rows)
    _header_written = True


# Сохраняем _flush() как алиас на rewrite — для обратной совместимости
# с внешним кодом, который мог вызывать utils.storage._flush() напрямую.
_flush = _rewrite_all


def total():
    return len(_rows)


def get_output_file():
    return OUTPUT_FILE


def _normalize_name(name: str) -> str:
    """Backward-compatible alias for the shared entity normalizer."""
    return _entity_normalize_name(name)


_MERGE_FIELDS = CONTACT_FIELDS


def cross_source_merge() -> int:
    """Enrich observations only after conservative entity resolution.

    Every per-source row remains in the raw result.  Missing contacts are
    copied from the canonical cluster and all source/value provenance is kept.
    Name/city alone never forms a cluster.
    """
    enriched = 0
    metadata_changed = False
    for rows in cluster_rows(_rows):
        full_canonical = canonicalize_cluster(rows, FIELDS)
        anchors = [row for row in rows if is_master_anchor(row)]
        anchor_canonical = (
            canonicalize_cluster(anchors, FIELDS) if anchors else None
        )
        for row in rows:
            # A weak broad-source observation must never pre-poison a trusted
            # observation before the master-level donor gate runs.
            if is_master_anchor(row) and anchor_canonical:
                for field in _MERGE_FIELDS:
                    if row.get(field) or not anchor_canonical.get(field):
                        continue
                    row[field] = anchor_canonical[field]
                    enriched += 1
            for field in ("entity_id", "sources", "provenance", "first_seen", "last_seen"):
                value = full_canonical.get(field, "")
                if value and row.get(field) != value:
                    row[field] = value
                    metadata_changed = True
    if enriched or metadata_changed:
        _rewrite_all()
    return enriched
