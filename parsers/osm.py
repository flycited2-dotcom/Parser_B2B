"""OpenStreetMap Overpass API: коммерческие организации Крыма по тегам сегментов.

Один HTTP-запрос — JSON со всеми node/way/relation, чьи теги перечислены в
config/segments.py (shop/office/craft/amenity/...). Каждая точка проходит
локальную проверку границы полуострова.
"""
import json
import os
import re
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request

from utils.http_retry import http_request
from urllib.error import URLError, HTTPError

from config.segments import osm_segment, osm_tag_groups
from utils.storage import save_item
from utils.geo_city import detect_city_by_coords, normalize_city_name
from utils.crimea_boundary import is_in_crimea

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

# Overpass быстрее и надёжнее отдаёт bbox, чем тяжёлый area-query. После ответа
# каждая точка обязательно проходит локальный point-in-polygon по контуру OSM.
BBOX = "44.35,32.45,46.20,36.70"


def _build_query() -> str:
    lines = [
        f'  nwr["{key}"~"^({"|".join(values)})$"]({BBOX});'
        for key, values in sorted(osm_tag_groups().items())
    ]
    return "[out:json][timeout:180];\n(\n" + "\n".join(lines) + "\n);\nout center tags;\n"


QUERY = _build_query()

CITY_HINTS = (
    "Симферополь", "Ялта", "Севастополь", "Евпатория", "Феодосия",
    "Керчь", "Алушта", "Судак", "Саки", "Бахчисарай",
    "Коктебель", "Партенит", "Гурзуф", "Новый Свет", "Форос",
    "Симеиз", "Алупка", "Ливадия", "Массандра", "Мисхор",
    "Канака", "Орджоникидзе", "Щёлкино", "Морское", "Малореченское",
)

def _normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return raw.strip()
    return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _detect_city(tags: dict, lat: float | None = None, lon: float | None = None) -> str:
    # 1. addr:city / town / village / hamlet
    for k in ("addr:city", "addr:town", "addr:village", "addr:hamlet"):
        v = tags.get(k)
        if v:
            for c in CITY_HINTS:
                if c.lower() in v.lower():
                    return normalize_city_name(c)
            return normalize_city_name(v)
    # 2. полный адрес как строка
    addr_full = tags.get("addr:full") or tags.get("address") or ""
    for c in CITY_HINTS:
        if c in addr_full:
            return c
    # 3. фоллбек на координаты (bbox-таблица)
    by_coords = detect_city_by_coords(lat, lon)
    if by_coords:
        return by_coords
    # 4. ничего не сработало — общий регион
    return "Крым"


def _build_address(tags: dict) -> str:
    parts = []
    for k in ("addr:city", "addr:town", "addr:village"):
        if tags.get(k):
            parts.append(f"г. {tags[k]}")
            break
    if tags.get("addr:street"):
        s = "ул. " + tags["addr:street"]
        if tags.get("addr:housenumber"):
            s += f", {tags['addr:housenumber']}"
        parts.append(s)
    if tags.get("addr:postcode"):
        parts.append(tags["addr:postcode"])
    if not parts:
        return tags.get("addr:full") or tags.get("address") or ""
    return ", ".join(parts)


def _category(tags: dict) -> str:
    return osm_segment(tags)[0]


def _raw_category(tags: dict) -> str:
    return osm_segment(tags)[1]


def _tag_values(tags: dict, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for key in keys:
        for value in str(tags.get(key) or "").split(";"):
            clean = value.strip()
            identity = clean.casefold()
            if clean and identity not in seen:
                seen.add(identity)
                values.append(clean)
    return values


def _websites(tags: dict) -> list[str]:
    values = _tag_values(tags, ("website", "contact:website", "url"))
    return [value if value.startswith(("http://", "https://")) else "https://" + value
            for value in values]


def _website(tags: dict) -> str:
    values = _websites(tags)
    return values[0] if values else ""


def _email(tags: dict) -> str:
    values = _tag_values(tags, ("email", "contact:email"))
    return values[0] if values else ""


def _emails(tags: dict) -> list[str]:
    return _tag_values(tags, ("email", "contact:email"))


def _phone(tags: dict) -> str:
    values = _phones(tags)
    return values[0] if values else ""


def _phones(tags: dict) -> list[str]:
    values = _tag_values(
        tags,
        ("phone", "contact:phone", "phone:mobile", "contact:mobile"),
    )
    return [_normalize_phone(value) for value in values]


def _fetch_overpass() -> list:
    body = urlencode({"data": QUERY}).encode("utf-8")
    last_err = None
    for url in OVERPASS_ENDPOINTS:
        try:
            req = Request(
                url, data=body,
                headers={"User-Agent": "b2b_parser/1.0", "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            raw = http_request(req, timeout=240)
            data = json.loads(raw.decode("utf-8"))
            return data.get("elements", [])
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError) as e:
            print(f"  [OSM] {url} fail: {e}")
            last_err = e
            continue
    raise RuntimeError(f"OSM: все Overpass endpoints недоступны: {last_err}")


async def run(context):
    """context — Playwright BrowserContext, не используется. Сигнатура для совместимости с main.py."""
    print("\n=== OSM Overpass ===")
    elements = _fetch_overpass()
    print(f"  получено объектов: {len(elements)}")

    added = 0
    outside = 0
    missing_coords = 0
    try:
        max_items = int(os.getenv("OSM_MAX_ITEMS") or os.getenv("MAX_ITEMS_PER_SOURCE") or "0")
    except ValueError:
        max_items = 0
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:ru") or tags.get("operator") or ""
        if not name:
            continue
        # координаты: для node — lat/lon на верхнем уровне, для way/relation — center.lat/center.lon
        lat = el.get("lat")
        lon = el.get("lon")
        if lat is None and "center" in el:
            lat = el["center"].get("lat")
            lon = el["center"].get("lon")
        if lat is None or lon is None:
            missing_coords += 1
            continue
        if not is_in_crimea(lat, lon):
            outside += 1
            continue
        osm_type = str(el.get("type") or "node")
        osm_id = str(el.get("id") or "")
        raw_category = _raw_category(tags)
        item = {
            "city": _detect_city(tags, lat, lon),
            "name": name,
            "address": _build_address(tags),
            "phone": _phone(tags),
            "email": _email(tags),
            "website": _website(tags),
            "all_phones": " | ".join(_phones(tags)),
            "all_emails": " | ".join(_emails(tags)),
            "all_websites": " | ".join(_websites(tags)),
            "category": _category(tags),
            "raw_category": raw_category,
            "source": "OSM",
            "source_id": f"{osm_type}:{osm_id}" if osm_id else "",
            "source_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_id else "",
            "latitude": str(lat),
            "longitude": str(lon),
            "confidence": "0.95",
            "quality_flags": "",
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if save_item(item):
            added += 1
            if max_items and added >= max_items:
                print(f"  [OSM] достигнут лимит {max_items}")
                break

    print(f"\n[OSM] добавлено: {added}; вне полуострова: {outside}; без координат: {missing_coords}")
    return added
