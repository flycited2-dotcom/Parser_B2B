"""Conservative entity resolution and provenance for B2B observations.

The parsers emit *observations*.  Two observations are considered the same
venue only when they share strong evidence (source id, phone, domain,
address+name, or nearby coordinates+name).  A matching name/city by itself is
deliberately insufficient: generic names and multiple branches are common in
B2B companies.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable
from urllib.parse import urlparse


_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_PHONE_RE = re.compile(r"\d")
_HOUSE_RE = re.compile(r"(?:^|\s)(\d+[а-яa-z]?(?:[/\\-]\d+[а-яa-z]?)?)(?:\s|$)", re.I)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

CONTACT_FIELDS = ("phone", "email", "website", "address", "social")
MULTI_CONTACT_FIELDS = {
    "all_phones": "phone",
    "all_emails": "email",
    "all_websites": "website",
    "all_socials": "social",
}
PROVENANCE_FIELDS = (
    "name", "city", "client_type", "category", "address", "phone",
    "email", "website", "social", "comment", "latitude", "longitude",
    "raw_category", "matched_query", "confidence", "quality_flags",
    "all_phones", "all_emails", "all_websites", "all_socials",
)

SOURCE_PRIORITY = {
    "Я.Карты": 50,
    "Yandex": 50,
    "OSM": 40,
    "VK": 30,
    "Crawler": 20,
}

_GENERIC_NAMES = {
    "бар", "кафе", "клуб", "кофейня", "паб", "пиццерия", "ресторан",
    "столовая", "фастфуд", "фудкорт", "шашлычная", "закусочная",
}


def normalize_text(value: object) -> str:
    value = str(value or "").casefold().replace("ё", "е")
    value = _NON_WORD_RE.sub(" ", value)
    return _WS_RE.sub(" ", value).strip()


def normalize_name(value: object) -> str:
    return normalize_text(value)


def normalize_city(value: object) -> str:
    city = normalize_text(value)
    city = re.sub(r"^(?:г|город|пгт|поселок|село)\s+", "", city)
    return city


def normalize_address(value: object) -> str:
    address = normalize_text(value)
    replacements = {
        "улица": "ул", "проспект": "пр", "переулок": "пер",
        "набережная": "наб", "площадь": "пл", "шоссе": "ш",
    }
    tokens = [replacements.get(token, token) for token in address.split()]
    return " ".join(tokens)


def phone_digits(value: object) -> str:
    digits = "".join(_PHONE_RE.findall(str(value or "")))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def website_domain(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").casefold().rstrip(".")
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _source(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "").strip())


def _coord(row: dict, primary: str, legacy: str) -> float | None:
    value = row.get(primary, row.get(legacy, ""))
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def coordinates(row: dict) -> tuple[float, float] | None:
    lat = _coord(row, "latitude", "lat")
    lon = _coord(row, "longitude", "lon")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def observation_identity(row: dict) -> str:
    """Stable per-source observation id, using a conservative fallback."""
    existing = str(row.get("observation_id") or "").strip()
    if existing:
        return existing

    source = _source(row.get("source")).casefold() or "unknown"
    source_id = str(row.get("source_id") or "").strip()
    if source_id:
        material = f"source-id|{source}|{source_id}"
    else:
        coord = coordinates(row)
        coord_key = f"{coord[0]:.5f},{coord[1]:.5f}" if coord else ""
        source_url = str(row.get("source_url") or row.get("social") or "").strip().casefold()
        name = normalize_name(row.get("name"))
        city = normalize_city(row.get("city"))
        address = normalize_address(row.get("address"))
        domain = website_domain(row.get("website"))
        phone = phone_digits(row.get("phone"))
        # Prefer identifiers that remain stable when contacts are enriched.
        # The hierarchy also preserves same-name branches when address/coords
        # are available.
        if source_url:
            material = f"source-url|{source}|{source_url}"
        elif address:
            material = f"address|{source}|{name}|{city}|{address}"
        elif coord_key:
            material = f"coords|{source}|{name}|{city}|{coord_key}"
        elif domain:
            material = f"domain|{source}|{name}|{city}|{domain}"
        elif phone:
            material = f"phone|{source}|{name}|{city}|{phone}"
        else:
            category = normalize_text(row.get("category"))
            material = f"minimal|{source}|{name}|{city}|{category}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"obs_{digest}"


def _name_similarity(left: dict, right: dict) -> float:
    a = normalize_name(left.get("name"))
    b = normalize_name(right.get("name"))
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _address_similarity(left: dict, right: dict) -> float:
    a = normalize_address(left.get("address"))
    b = normalize_address(right.get("address"))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _distance_km(left: dict, right: dict) -> float | None:
    a = coordinates(left)
    b = coordinates(right)
    if not a or not b:
        return None
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _house_number(address: object) -> str:
    matches = _HOUSE_RE.findall(normalize_address(address))
    return matches[-1] if matches else ""


def _branch_conflict(left: dict, right: dict) -> bool:
    """Strong evidence that two records are separate physical branches."""
    distance = _distance_km(left, right)
    if distance is not None and distance > 1.0:
        return True
    a_addr = normalize_address(left.get("address"))
    b_addr = normalize_address(right.get("address"))
    if not a_addr or not b_addr or a_addr == b_addr:
        return False
    a_house = _house_number(a_addr)
    b_house = _house_number(b_addr)
    # Same brand/phone/domain at distinct house numbers is the normal shape of
    # two branches, not a formatting variation of one address.
    if a_house and b_house and a_house != b_house:
        return True
    return False


def same_entity(left: dict, right: dict) -> bool:
    """Return True only when two observations share strong identity evidence."""
    left_entity = str(left.get("entity_id") or "").strip()
    right_entity = str(right.get("entity_id") or "").strip()
    if left_entity and left_entity == right_entity:
        return True

    left_source = _source(left.get("source")).casefold()
    right_source = _source(right.get("source")).casefold()
    left_sid = str(left.get("source_id") or "").strip()
    right_sid = str(right.get("source_id") or "").strip()
    if left_source and left_source == right_source and left_sid and left_sid == right_sid:
        return True

    left_url = str(left.get("source_url") or "").strip().casefold()
    right_url = str(right.get("source_url") or "").strip().casefold()
    if left_source == right_source and left_url and left_url == right_url:
        return True

    name_similarity = _name_similarity(left, right)
    if name_similarity < 0.62:
        return False

    address_similarity = _address_similarity(left, right)
    distance = _distance_km(left, right)
    if address_similarity == 1.0 and name_similarity >= 0.72:
        return True
    if distance is not None and distance <= 0.35 and name_similarity >= 0.68:
        return True

    if _branch_conflict(left, right):
        return False

    if address_similarity >= 0.88 and name_similarity >= 0.72:
        return True

    left_name = normalize_name(left.get("name"))
    generic_name = left_name in _GENERIC_NAMES
    left_phone = phone_digits(left.get("phone"))
    right_phone = phone_digits(right.get("phone"))
    if left_phone and left_phone == right_phone and name_similarity >= (0.9 if generic_name else 0.72):
        return True

    left_domain = website_domain(left.get("website"))
    right_domain = website_domain(right.get("website"))
    city_left = normalize_city(left.get("city"))
    city_right = normalize_city(right.get("city"))
    city_compatible = not city_left or not city_right or "крым" in (city_left, city_right) or city_left == city_right
    if (left_domain and left_domain == right_domain and city_compatible
            and name_similarity >= (0.92 if generic_name else 0.82)):
        return True

    # Name/city alone is intentionally never sufficient.
    return False


def _blocking_keys(row: dict) -> set[str]:
    keys: set[str] = set()
    entity_id = str(row.get("entity_id") or "").strip()
    if entity_id:
        keys.add("entity:" + entity_id)
    source = _source(row.get("source")).casefold()
    source_id = str(row.get("source_id") or "").strip()
    if source and source_id:
        keys.add(f"source-id:{source}:{source_id}")
    source_url = str(row.get("source_url") or "").strip().casefold()
    if source and source_url:
        keys.add(f"source-url:{source}:{source_url}")
    phone = phone_digits(row.get("phone"))
    if phone:
        keys.add("phone:" + phone)
    domain = website_domain(row.get("website"))
    if domain:
        keys.add("domain:" + domain)
    name = normalize_name(row.get("name"))
    city = normalize_city(row.get("city"))
    address = normalize_address(row.get("address"))
    if name and city:
        keys.add(f"name-city:{name}:{city}")
    if address:
        keys.add("address:" + address)
    coord = coordinates(row)
    if coord:
        lat_cell = math.floor(coord[0] * 100)
        lon_cell = math.floor(coord[1] * 100)
        for lat_delta in (-1, 0, 1):
            for lon_delta in (-1, 0, 1):
                keys.add(f"geo:{lat_cell + lat_delta}:{lon_cell + lon_delta}")
    return keys or {"observation:" + observation_identity(row)}


def cluster_rows(rows: list[dict]) -> list[list[dict]]:
    """Cluster rows without an unsafe all-pairs name-only comparison."""
    count = len(rows)
    if not count:
        return []
    parent = list(range(count))
    rank = [0] * count
    members = [{index} for index in range(count)]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return True
        # Prevent a bridge observation with missing address/coordinates from
        # transitively collapsing two clearly different branches.
        if any(
            _branch_conflict(rows[left_member], rows[right_member])
            for left_member in members[left_root]
            for right_member in members[right_root]
        ):
            return False
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        members[left_root].update(members[right_root])
        members[right_root].clear()
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1
        return True

    index: dict[str, list[int]] = defaultdict(list)
    compared: set[tuple[int, int]] = set()
    for current, row in enumerate(rows):
        for key in _blocking_keys(row):
            for previous in index[key]:
                pair = (previous, current)
                if pair in compared:
                    continue
                compared.add(pair)
                if same_entity(rows[previous], row):
                    union(previous, current)
            index[key].append(current)

    groups: dict[int, list[dict]] = defaultdict(list)
    order: list[int] = []
    for idx, row in enumerate(rows):
        root = find(idx)
        if root not in groups:
            order.append(root)
        groups[root].append(row)
    return [groups[root] for root in order]


def _load_provenance(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {"observations": [], "fields": {}}
    try:
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else {"observations": [], "fields": {}}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"observations": [], "fields": {}}


def _append_unique(items: list, item: dict, identity_fields: Iterable[str]) -> None:
    key = tuple(str(item.get(field, "")) for field in identity_fields)
    for existing in items:
        if tuple(str(existing.get(field, "")) for field in identity_fields) == key:
            for field, value in item.items():
                if value in (None, ""):
                    continue
                if field == "last_seen":
                    existing[field] = max(str(existing.get(field) or ""), str(value))
                elif not existing.get(field):
                    existing[field] = value
            return
    items.append(item)


def provenance_for_rows(rows: Iterable[dict]) -> str:
    combined: dict = {"observations": [], "fields": {}}
    for row in rows:
        existing = _load_provenance(row.get("provenance"))
        for observation in existing.get("observations", []) or []:
            if isinstance(observation, dict):
                _append_unique(combined["observations"], observation, ("observation_id",))
        for field, evidence_list in (existing.get("fields", {}) or {}).items():
            if not isinstance(evidence_list, list):
                continue
            target = combined["fields"].setdefault(field, [])
            for evidence in evidence_list:
                if isinstance(evidence, dict):
                    _append_unique(target, evidence, ("observation_id", "value"))

        observation_id = observation_identity(row)
        observation = {
            "observation_id": observation_id,
            "source": _source(row.get("source")),
            "source_id": str(row.get("source_id") or ""),
            "source_url": str(row.get("source_url") or ""),
            "first_seen": str(row.get("first_seen") or ""),
            "last_seen": str(row.get("last_seen") or ""),
        }
        _append_unique(combined["observations"], observation, ("observation_id",))
        for field in PROVENANCE_FIELDS:
            value = row.get(field)
            if value in (None, ""):
                continue
            target = combined["fields"].setdefault(field, [])
            # A cross-source-enriched row may carry a value whose real donor
            # is already recorded in provenance.  Do not falsely attribute it
            # again to the recipient observation.
            prior_field_evidence = (existing.get("fields", {}) or {}).get(field, []) or []
            if any(
                isinstance(evidence, dict)
                and str(evidence.get("value", "")) == str(value)
                for evidence in prior_field_evidence
            ):
                continue
            evidence = {
                "value": str(value),
                "source": _source(row.get("source")),
                "source_id": str(row.get("source_id") or ""),
                "observation_id": observation_id,
            }
            _append_unique(target, evidence, ("observation_id", "value"))
    return json.dumps(combined, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(row: dict) -> str:
    return str(row.get("last_seen") or row.get("parsed_at") or "")


def _value_quality(field: str, value: object, row: dict) -> tuple[int, int, str, int]:
    text = str(value or "").strip()
    if not text:
        return (-1, -1, "", -1)
    quality = 10
    if field == "phone":
        quality += 80 if phone_digits(text) else -50
    elif field == "email":
        quality += 70 if _EMAIL_RE.match(text) else -50
        email_domain = text.casefold().partition("@")[2]
        if email_domain and email_domain == website_domain(row.get("website")):
            quality += 20
    elif field == "website":
        quality += 60 if website_domain(text) else -50
    elif field == "address":
        quality += min(len(normalize_address(text)), 50)
        if _house_number(text):
            quality += 20
    elif field == "city":
        quality += 0 if normalize_city(text) == "крым" else 30
    elif field in ("category", "client_type"):
        quality += 0 if normalize_text(text) == "прочее" else 30
    elif field == "name":
        quality += min(len(normalize_name(text)), 40)
    source_score = SOURCE_PRIORITY.get(_source(row.get("source")), 0)
    return quality, source_score, _timestamp(row), len(text)


def _best_value(field: str, rows: Iterable[dict]) -> str:
    candidates = [(row.get(field), row) for row in rows if row.get(field) not in (None, "")]
    if not candidates:
        return ""
    value, _ = max(candidates, key=lambda pair: _value_quality(field, pair[0], pair[1]))
    return str(value)


def _all_contact_values(rows: Iterable[dict], multi_field: str, primary_field: str) -> str:
    """Return a deterministic union of preferred and alternate contacts."""
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        candidates: list[str] = []
        primary = str(row.get(primary_field) or "").strip()
        if primary:
            candidates.append(primary)
        raw_multi = str(row.get(multi_field) or "")
        candidates.extend(part.strip() for part in re.split(r"\s+\|\s+", raw_multi))
        for value in candidates:
            if not value:
                continue
            if primary_field == "phone":
                identity = phone_digits(value) or normalize_text(value)
            elif primary_field == "email":
                identity = value.casefold()
            else:
                identity = value.casefold().rstrip("/")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            values.append(value)
    return " | ".join(values)


def canonical_entity_id(rows: list[dict]) -> str:
    existing = sorted({str(row.get("entity_id") or "").strip() for row in rows if row.get("entity_id")})
    if existing:
        return existing[0]
    materials: list[str] = []
    for row in rows:
        source = _source(row.get("source")).casefold()
        source_id = str(row.get("source_id") or "").strip()
        if source and source_id:
            materials.append(f"source-id|{source}|{source_id}")
    if not materials:
        materials = sorted(observation_identity(row) for row in rows)
    digest = hashlib.sha256("|".join(sorted(materials)).encode("utf-8")).hexdigest()[:24]
    return f"ent_{digest}"


def canonicalize_cluster(rows: list[dict], fieldnames: Iterable[str]) -> dict:
    """Create one canonical row while retaining every observation in provenance."""
    if not rows:
        return {}
    representative = max(
        rows,
        key=lambda row: (
            SOURCE_PRIORITY.get(_source(row.get("source")), 0),
            _timestamp(row),
            sum(bool(row.get(field)) for field in CONTACT_FIELDS),
        ),
    )
    result = {field: "" for field in fieldnames}
    for field in fieldnames:
        if field in {
            "entity_id", "observation_id", "source_id", "source_url",
            "sources", "provenance", "first_seen", "last_seen", "source",
            *MULTI_CONTACT_FIELDS,
        }:
            continue
        result[field] = _best_value(field, rows)

    for multi_field, primary_field in MULTI_CONTACT_FIELDS.items():
        if multi_field in result:
            result[multi_field] = _all_contact_values(rows, multi_field, primary_field)

    # Coordinates are an inseparable pair; never combine latitude from one
    # observation with longitude from another.
    coordinate_rows = [row for row in rows if coordinates(row)]
    if coordinate_rows:
        coordinate_donor = max(
            coordinate_rows,
            key=lambda row: (
                SOURCE_PRIORITY.get(_source(row.get("source")), 0),
                _timestamp(row),
            ),
        )
        result["latitude"] = str(coordinate_donor.get("latitude", coordinate_donor.get("lat", "")) or "")
        result["longitude"] = str(coordinate_donor.get("longitude", coordinate_donor.get("lon", "")) or "")

    matched_queries = sorted({
        query.strip()
        for row in rows
        for query in str(row.get("matched_query") or "").split("|")
        if query.strip()
    })
    if matched_queries:
        result["matched_query"] = " | ".join(matched_queries)
    quality_flags = sorted({
        flag.strip()
        for row in rows
        for flag in str(row.get("quality_flags") or "").split("|")
        if flag.strip()
    })
    result["quality_flags"] = "|".join(quality_flags)
    confidences: list[float] = []
    for row in rows:
        try:
            confidences.append(float(str(row.get("confidence") or "")))
        except ValueError:
            continue
    if confidences:
        result["confidence"] = f"{max(confidences):.2f}"

    result["entity_id"] = canonical_entity_id(rows)
    result["observation_id"] = observation_identity(representative)
    result["source"] = _source(representative.get("source"))
    result["source_id"] = str(representative.get("source_id") or "")
    result["source_url"] = str(representative.get("source_url") or "")
    sources = sorted({src for row in rows for src in str(row.get("sources") or row.get("source") or "").split("|") if src})
    result["sources"] = "|".join(sources)
    first_seen = sorted(str(row.get("first_seen") or "") for row in rows if row.get("first_seen"))
    last_seen = sorted(str(row.get("last_seen") or "") for row in rows if row.get("last_seen"))
    result["first_seen"] = first_seen[0] if first_seen else ""
    result["last_seen"] = last_seen[-1] if last_seen else ""
    result["provenance"] = provenance_for_rows(rows)
    return result


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
