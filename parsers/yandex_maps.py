"""Парсер Яндекс.Карт по Крыму. Работает с РФ-IP.

Стратегия:
1. Открываем yandex.ru/maps/?text=<запрос>
2. Ждём появления .search-snippet-view, скроллим левую панель — подгружаются все
3. До навигации фиксируем name, address и устойчивую ссылку /maps/org/<slug>/<id>
4. По ID открываем отдельную страницу организации — там телефон/сайт видны без WebGL
"""
import asyncio
import os
import random
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote_plus, urlparse

from utils.storage import save_item

# Typical URL: /maps/org/fabrikant/1324964934/. Requiring at least six
# digits prevents names such as "23 Cafe" from becoming the bogus id "23".
ORG_ID_RE = re.compile(r"/maps/org/(?:[^/?#]+/)?(\d{6,})(?:[/?#]|$)")

DETAIL_FIELDS = (
    "phone", "website", "email", "all_phones", "all_websites", "all_emails",
)
FAILURE_STATUSES = {
    "timeout", "navigation_error", "markup_error", "blocked", "captcha",
    "search_timeout", "search_navigation_error", "search_markup_error",
    "search_blocked", "search_captcha",
}
DETAIL_STATUSES = {"ok", "empty", "timeout", "navigation_error", "markup_error", "blocked", "captcha"}


@dataclass
class YandexRunState:
    """Bound expensive detail requests and stop politely when Yandex blocks us."""

    detail_limit: int
    max_consecutive_failures: int = 6
    max_captcha_hits: int = 1
    failure_window: int = 20
    failure_ratio: float = 0.50
    max_empty_ratio: float = 0.80
    details_cache: dict[str, dict] = field(default_factory=dict)
    detail_requests: int = 0
    cache_hits: int = 0
    consecutive_failures: int = 0
    captcha_hits: int = 0
    recent_statuses: deque[str] = field(default_factory=deque)
    statuses: Counter = field(default_factory=Counter)
    circuit_reason: str = ""

    @property
    def stopped(self) -> bool:
        return bool(self.circuit_reason)

    def record(self, status: str) -> None:
        self.statuses[status] += 1
        self.recent_statuses.append(status)
        while len(self.recent_statuses) > self.failure_window:
            self.recent_statuses.popleft()

        if status in {"ok", "empty", "search_no_results"}:
            self.consecutive_failures = 0
        elif status in FAILURE_STATUSES:
            self.consecutive_failures += 1
        if status in {"captcha", "blocked", "search_captcha", "search_blocked"}:
            self.captcha_hits += 1

        if not self.circuit_reason:
            if self.captcha_hits >= self.max_captcha_hits:
                self.circuit_reason = f"yandex_{status}"
            elif self.consecutive_failures >= self.max_consecutive_failures:
                self.circuit_reason = "consecutive_detail_failures"
            elif len(self.recent_statuses) >= min(10, self.failure_window):
                failures = sum(
                    s in FAILURE_STATUSES for s in self.recent_statuses
                )
                if failures / len(self.recent_statuses) >= self.failure_ratio:
                    self.circuit_reason = "high_detail_failure_ratio"
            if not self.circuit_reason:
                recent_details = [
                    status
                    for status in self.recent_statuses
                    if status in DETAIL_STATUSES
                ]
                if (
                    len(recent_details) >= min(10, self.failure_window)
                    and recent_details.count("empty") / len(recent_details)
                    >= self.max_empty_ratio
                ):
                    self.circuit_reason = "high_empty_detail_ratio"


CITIES = [
    "Симферополь", "Ялта", "Севастополь", "Евпатория", "Феодосия",
    "Керчь", "Алушта", "Судак", "Саки", "Бахчисарай",
    "Джанкой", "Красноперекопск", "Армянск", "Белогорск", "Старый Крым",
    "Щёлкино", "Черноморское", "Раздольное", "Кировское", "Нижнегорский",
    "Советский", "Первомайское", "Коктебель", "Партенит", "Гурзуф",
    "Алупка", "Симеиз", "Форос", "Ливадия", "Массандра", "Мисхор",
    "Новый Свет", "Орджоникидзе", "Морское", "Рыбачье", "Малореченское",
    "Солнечногорское", "Николаевка", "Балаклава",
]

# Категории берём шире — в выдачу попадает всё схожее
QUERIES = [
    ("ресторан {city}", "ресторан"),
    ("кафе {city}", "кафе"),
    ("бар {city}", "бар"),
    ("паб {city}", "паб"),
    ("ночной клуб {city}", "клуб"),
    ("кофейня {city}", "кофейня"),
    ("столовая {city}", "столовая"),
    ("фудкорт {city}", "фудкорт"),
    ("пиццерия {city}", "пиццерия"),
    ("кондитерская {city}", "кондитерская"),
    ("фастфуд {city}", "фастфуд"),
    ("пекарня {city}", "кондитерская"),
    ("бургерная {city}", "фастфуд"),
    ("шаурма {city}", "фастфуд"),
    ("суши {city}", "ресторан"),
    ("доставка еды {city}", "фастфуд"),
]

EXTRA_QUERIES_GLOBAL = [
    ("суши бар Крым", "ресторан", "Крым"),
    ("доставка еды Крым", "фастфуд", "Крым"),
]

# main._source_limits temporarily truncates public lists for legacy parsers.
# Keep immutable originals so Yandex offsets can select later production batches.
_ALL_CITIES = tuple(CITIES)
_ALL_QUERIES = tuple(QUERIES)


def _normalize_search_snapshot(raw_rows: list[dict], limit: int) -> list[dict]:
    """Validate one atomic browser snapshot and retain canonical org cards."""
    results: list[dict] = []
    seen_ids: set[str] = set()
    for raw in raw_rows or []:
        name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()
        address = re.sub(r"\s+", " ", str(raw.get("address") or "")).strip()
        if not name:
            continue
        source_url = ""
        org_id = ""
        for href in raw.get("hrefs") or []:
            candidate_id = _extract_org_id(href)
            if candidate_id:
                org_id = candidate_id
                source_url = str(href).split("?", 1)[0].split("#", 1)[0]
                break
        if not org_id or org_id in seen_ids:
            continue
        seen_ids.add(org_id)
        results.append({
            "name": name,
            "address": address,
            "org_id": org_id,
            "source_url": source_url,
        })
        if len(results) >= limit:
            break
    return results


async def _read_visible_search_results(page, limit: int) -> list[dict]:
    """Read cards atomically so a background DOM rerender cannot detach handles."""
    raw_rows = await page.eval_on_selector_all(
        ".search-snippet-view",
        """snippets => snippets.map(snippet => {
            const text = node => (node && node.textContent || '')
                .replace(/\\s+/g, ' ').trim();
            const firstText = selectors => {
                for (const selector of selectors) {
                    const value = text(snippet.querySelector(selector));
                    if (value) return value;
                }
                return '';
            };
            return {
                name: firstText([
                    '.search-business-snippet-view__title',
                    '.search-snippet-view__title',
                    '[class*="SnippetTitle"]', 'h3', '[class*="title"]'
                ]),
                address: firstText([
                    '.search-business-snippet-view__address',
                    '[class*="address"]'
                ]),
                hrefs: Array.from(snippet.querySelectorAll('a[href]'))
                    .map(anchor => anchor.href)
            };
        })""",
    )
    return _normalize_search_snapshot(raw_rows, limit)


async def _collect_search_results(page, n: int = 30, limit: int = 100) -> list[dict]:
    """Accumulate unique cards while scrolling, including virtualized lists."""
    collected: dict[str, dict] = {}
    stable_rounds = 0
    for iteration in range(n + 1):
        visible = await _read_visible_search_results(page, limit)
        before = len(collected)
        for item in visible:
            collected.setdefault(item["org_id"], item)
            if len(collected) >= limit:
                return list(collected.values())[:limit]
        stable_rounds = stable_rounds + 1 if len(collected) == before else 0
        if stable_rounds >= 3 or iteration >= n:
            break
        try:
            await page.evaluate(
                """() => {
                    const sels = ['.scroll__container', '.search-list-view__list', '[class*="scroll__container"]'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) { el.scrollTop = el.scrollHeight; return; }
                    }
                    window.scrollBy(0, 1500);
                }"""
            )
        except Exception:
            pass
        await page.wait_for_timeout(700)
    return list(collected.values())[:limit]


async def _has_captcha(page) -> bool:
    try:
        if "captcha" in page.url.lower() or "showcaptcha" in page.url.lower():
            return True
    except Exception:
        pass
    for selector in (
        "iframe[src*='captcha']",
        "form[action*='captcha']",
        ".CheckboxCaptcha",
        ".SmartCaptcha",
    ):
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            continue
    return False


async def _has_org_marker(page) -> bool:
    """Require a stable sign that the loaded page is an organization card."""
    for selector in (
        ".card-title-view__title",
        "[class*='orgpage-header-view__header']",
        "[class*='business-card-title-view']",
        "[itemprop='name']",
        "h1",
    ):
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            continue
    return False


async def _has_no_results_marker(page) -> bool:
    for selector in (
        ".search-list-view__empty",
        "[class*='search-list-view__empty']",
        "[class*='nothing-found']",
    ):
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            continue
    return False


async def _click_show_phone(page) -> None:
    """Я.Карты часто прячут телефон под кнопкой/спойлером. Пробуем раскрыть."""
    selectors = [
        "[class*='card-phones-view__phone-number']",
        "[class*='card-phones']  button",
        "[class*='card-phones-view'] [role='button']",
        "div[class*='phone'] button",
        "button:has-text('Показать')",
        "span:has-text('Показать телефон')",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            await el.scroll_into_view_if_needed(timeout=1500)
            await el.click(timeout=2000)
            await page.wait_for_timeout(700)
            return
        except Exception:
            continue


async def _scrape_org_page(context, org_id: str) -> dict:
    """Открыть /maps/org/<id>/ напрямую: телефон/сайт/email видны без WebGL.
    В headed/xvfb-режиме телефон может быть скрыт за «Показать» — раскрываем.
    """
    out = {
        "phone": "", "website": "", "email": "",
        "all_phones": "", "all_websites": "", "all_emails": "",
        "status": "navigation_error", "quality_flags": [],
    }
    phones: list[str] = []
    emails: list[str] = []
    websites: list[str] = []

    def add_unique(target: list[str], value: str) -> None:
        clean = (value or "").strip()
        if clean and clean.casefold().rstrip("/") not in {
            item.casefold().rstrip("/") for item in target
        }:
            target.append(clean)
    page = await context.new_page()
    try:
        url = f"https://yandex.ru/maps/org/{org_id}/"
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            if "timeout" in type(exc).__name__.casefold():
                out["status"] = "timeout"
            return out
        if await _has_captcha(page):
            out["status"] = "captcha"
            out["quality_flags"] = ["detail_blocked", "contact_unverified"]
            return out
        if response is not None and response.status in {403, 429}:
            out["status"] = "blocked"
            out["quality_flags"] = ["detail_blocked", "contact_unverified"]
            return out
        if response is not None and response.status >= 400:
            out["status"] = "navigation_error"
            out["quality_flags"] = ["detail_navigation_error", "contact_unverified"]
            return out

        # ждём появления контактного блока (любой из признаков)
        try:
            await page.wait_for_selector(
                "a[href^='tel:'], [class*='card-phones'], [itemprop='telephone'], "
                "[class*='_phone']",
                timeout=6000,
            )
        except Exception:
            await page.wait_for_timeout(2000)
        if await _has_captcha(page):
            out["status"] = "captcha"
            out["quality_flags"] = ["detail_blocked", "contact_unverified"]
            return out
        if _extract_org_id(getattr(page, "url", "")) != org_id:
            out["status"] = "markup_error"
            out["quality_flags"] = ["detail_markup_error", "manual_review"]
            return out
        if not await _has_org_marker(page):
            out["status"] = "markup_error"
            out["quality_flags"] = ["detail_markup_error", "manual_review"]
            return out

        # пробуем раскрыть телефон, если он скрыт
        await _click_show_phone(page)

        # phone
        for sel in ["a[href^='tel:']", "[itemprop='telephone']",
                    "[class*='card-phones-view__phone-number']",
                    "[class*='card-phones']", "[class*='phones-item__text']",
                    "[class*='_phone']"]:
            try:
                for el in await page.query_selector_all(sel):
                    if sel.startswith("a"):
                        href = await el.get_attribute("href") or ""
                        add_unique(phones, href.replace("tel:", "").strip())
                    else:
                        add_unique(phones, (await el.inner_text()).strip())
            except Exception:
                continue
        out["phone"] = phones[0] if phones else ""

        # email
        try:
            for em in await page.query_selector_all("a[href^='mailto:']"):
                href = await em.get_attribute("href") or ""
                add_unique(emails, href.replace("mailto:", "").split("?")[0].strip())
        except Exception:
            pass
        out["email"] = emails[0] if emails else ""

        # website (не yandex)
        try:
            candidates = await page.query_selector_all("a[href^='http']")
            for el in candidates:
                href = await el.get_attribute("href") or ""
                host = urlparse(href).netloc.lower()
                if not host:
                    continue
                if any(b in host for b in ("yandex.", "ya.ru", "yastatic.")):
                    continue
                from urllib.parse import urlsplit, urlunsplit
                sp = urlsplit(href)
                add_unique(
                    websites,
                    urlunsplit((sp.scheme, sp.netloc, sp.path, "", "")),
                )
        except Exception:
            pass
        out["website"] = websites[0] if websites else ""

        # fallback на innerText — иногда телефон есть только текстом
        if not out["phone"]:
            try:
                txt = await page.evaluate("document.body && document.body.innerText || ''")
                m = re.search(r"\+7[\s\-\(\)\d]{10,18}|8[\s\-\(\)\d]{10,18}", txt)
                if m:
                    out["phone"] = m.group(0)
                    add_unique(phones, out["phone"])
            except Exception:
                pass
        out["all_phones"] = " | ".join(phones)
        out["all_emails"] = " | ".join(emails)
        out["all_websites"] = " | ".join(websites)
        has_contacts = any(out.get(field) for field in DETAIL_FIELDS)
        out["status"] = "ok" if has_contacts else "empty"
        if not has_contacts:
            out["quality_flags"] = ["no_public_contacts"]
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return out


def _extract_org_id(value: str) -> str:
    match = ORG_ID_RE.search(str(value or ""))
    return match.group(1) if match else ""


DETAIL_QUALITY = {
    "ok": ("0.95", []),
    "empty": ("0.85", ["no_public_contacts"]),
    "timeout": ("0.65", ["detail_timeout", "contact_unverified"]),
    "navigation_error": (
        "0.60", ["detail_navigation_error", "contact_unverified"],
    ),
    "markup_error": ("0.55", ["detail_markup_error", "manual_review"]),
    "captcha": ("0.50", ["detail_blocked", "contact_unverified"]),
    "blocked": ("0.50", ["detail_blocked", "contact_unverified"]),
}


def _detail_result(status: str) -> dict:
    _confidence, flags = DETAIL_QUALITY.get(
        status, ("0.50", ["contact_unverified", "manual_review"])
    )
    return {
        **{field: "" for field in DETAIL_FIELDS},
        "status": status,
        "quality_flags": list(flags),
    }


async def _get_org_details(
    context, org_id: str, state: YandexRunState
) -> dict:
    """Return cached details or spend one explicitly bounded network request."""
    if org_id in state.details_cache:
        state.cache_hits += 1
        return state.details_cache[org_id]
    if state.stopped:
        return _detail_result("circuit_open")
    if state.detail_limit and state.detail_requests >= state.detail_limit:
        state.circuit_reason = "detail_budget_exhausted"
        return _detail_result("detail_budget_exhausted")

    state.detail_requests += 1
    try:
        details = await _scrape_org_page(context, org_id)
    except Exception as exc:
        print(f"  detail {org_id} exception: {type(exc).__name__}")
        details = _detail_result("navigation_error")
        details["quality_flags"].append("detail_exception")
    status = str(details.get("status") or "navigation_error")
    if status not in DETAIL_QUALITY:
        status = "navigation_error"
        details["status"] = status
    _confidence, default_flags = DETAIL_QUALITY[status]
    details["quality_flags"] = list(details.get("quality_flags") or default_flags)
    for field in DETAIL_FIELDS:
        details.setdefault(field, "")
    state.details_cache[org_id] = details
    state.record(status)
    return details


def _env_int(names: tuple[str, ...], default: int, minimum: int = 0) -> int:
    raw = next(
        (os.getenv(name) for name in names if os.getenv(name) not in (None, "")),
        None,
    )
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{names[0]} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{names[0]} must be >= {minimum}, got {value}")
    return value


def _env_ratio(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not 0 < value <= 1:
        raise ValueError(f"{name} must be > 0 and <= 1")
    return value


def _new_run_state(max_items: int) -> YandexRunState:
    detail_limit = _env_int(
        ("YANDEX_MAX_DETAIL_REQUESTS",),
        max_items or 600,
    )
    # The common item bound is authoritative, including when an expert has
    # set the Yandex-specific value to zero (unlimited). This guarantees that
    # DRY_RUN and MAX_ITEMS_PER_SOURCE cannot spend a larger network budget.
    if max_items:
        detail_limit = min(detail_limit, max_items) if detail_limit else max_items
    return YandexRunState(
        detail_limit=detail_limit,
        max_consecutive_failures=_env_int(
            ("YANDEX_MAX_CONSECUTIVE_FAILURES",), 6, minimum=1
        ),
        max_captcha_hits=_env_int(("YANDEX_MAX_CAPTCHA_HITS",), 1, minimum=1),
        failure_window=_env_int(("YANDEX_FAILURE_WINDOW",), 20, minimum=10),
        failure_ratio=_env_ratio("YANDEX_FAILURE_RATIO", 0.50),
        max_empty_ratio=_env_ratio("YANDEX_MAX_EMPTY_RATIO", 0.80),
    )


async def _process_query(
    context,
    query_text: str,
    category: str,
    city: str,
    remaining: int = 0,
    run_state: YandexRunState | None = None,
):
    state = run_state or YandexRunState(detail_limit=remaining or 100)
    if state.stopped:
        return 0
    try:
        page = await context.new_page()
    except Exception as exc:
        state.record("search_navigation_error")
        print(f"  search page exception: {type(exc).__name__}")
        return 0
    try:
        url = f"https://yandex.ru/maps/?text={quote_plus(query_text)}"
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=30000
            )
        except Exception as e:
            status = (
                "search_timeout"
                if "timeout" in type(e).__name__.casefold()
                else "search_navigation_error"
            )
            state.record(status)
            print(f"  goto fail: {type(e).__name__}")
            return 0
        if response is not None and response.status in {403, 429}:
            state.record("search_blocked")
            print(f"  search blocked: HTTP {response.status}")
            return 0
        if response is not None and response.status >= 400:
            state.record("search_navigation_error")
            print(f"  search error: HTTP {response.status}")
            return 0

        # ждём появления сниппетов
        found_snippets = False
        try:
            await page.wait_for_selector(".search-snippet-view", timeout=15000)
            found_snippets = True
        except Exception:
            await page.wait_for_timeout(4000)
            try:
                found_snippets = bool(
                    await page.query_selector(".search-snippet-view")
                )
            except Exception:
                found_snippets = False

        if await _has_captcha(page):
            state.record("search_captcha")
            print("  CAPTCHA, остановка источника")
            return 0

        per_query_limit = _env_int(
            ("YANDEX_RESULTS_PER_QUERY",), 100, minimum=1
        )
        max_scrolls = _env_int(("YANDEX_MAX_SCROLLS",), 30, minimum=1)
        if remaining:
            per_query_limit = min(per_query_limit, remaining)

        # Read each visible batch atomically before navigation. This survives
        # both stale ElementHandles and Yandex's virtualized result lists.
        try:
            candidates = await _collect_search_results(
                page, n=max_scrolls, limit=per_query_limit
            )
        except Exception as exc:
            state.record("search_markup_error")
            print(f"  search snapshot fail: {type(exc).__name__}")
            return 0
        print(f"  стабильных карточек: {len(candidates)}")
        if not candidates:
            if not found_snippets and await _has_no_results_marker(page):
                state.record("search_no_results")
                print("  результатов нет")
            else:
                state.record("search_markup_error")
                print("  карточки выдачи не распознаны")
            return 0

        added = 0
        for i, candidate in enumerate(candidates):
            if state.stopped:
                break
            try:
                name = candidate["name"]
                address = candidate["address"]
                org_id = candidate["org_id"]
                was_cached = org_id in state.details_cache
                details = await _get_org_details(context, org_id, state)
                status = str(details.get("status") or "navigation_error")
                if status in {"detail_budget_exhausted", "circuit_open"}:
                    break
                confidence, default_flags = DETAIL_QUALITY.get(
                    status, ("0.50", ["contact_unverified", "manual_review"])
                )
                flags = sorted(set(details.get("quality_flags") or default_flags))

                if save_item({
                    "city": city,
                    "name": name,
                    "address": address,
                    "phone": details["phone"],
                    "email": details["email"],
                    "website": details["website"],
                    "all_phones": details["all_phones"],
                    "all_emails": details["all_emails"],
                    "all_websites": details["all_websites"],
                    "category": category,
                    "raw_category": category,
                    "matched_query": query_text,
                    "source": "Я.Карты",
                    "source_id": org_id,
                    "source_url": candidate["source_url"] or url,
                    "confidence": confidence,
                    "quality_flags": "|".join(flags),
                    "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }):
                    added += 1

                if not was_cached:
                    await asyncio.sleep(random.uniform(0.6, 1.4))
            except Exception as e:
                print(f"  err snippet#{i}: {e}")
                continue

        return added
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def run(context):
    print("\n=== Я.Карты ===")
    total = 0

    max_cities = _env_int(("MAX_CITIES", "YANDEX_MAX_CITIES"), 0)
    max_queries = _env_int(
        ("MAX_QUERIES_PER_SOURCE", "YANDEX_MAX_QUERIES"), 0
    )
    max_items = _env_int(("MAX_ITEMS_PER_SOURCE", "YANDEX_MAX_ITEMS"), 0)
    city_offset = _env_int(("YANDEX_CITY_OFFSET",), 0)
    query_offset = _env_int(("YANDEX_QUERY_OFFSET",), 0)
    state = _new_run_state(max_items)

    city_end = city_offset + max_cities if max_cities else None
    query_end = query_offset + max_queries if max_queries else None
    cities = list(_ALL_CITIES[city_offset:city_end])
    queries = list(_ALL_QUERIES[query_offset:query_end])
    print(
        "[Я.Карты] batch "
        f"cities={city_offset}:{city_end or len(_ALL_CITIES)} "
        f"queries={query_offset}:{query_end or len(_ALL_QUERIES)} "
        f"detail_limit={state.detail_limit or 'unlimited'}"
    )
    for q_tmpl, category in queries:
        for city in cities:
            if state.stopped or (max_items and total >= max_items):
                break
            q = q_tmpl.format(city=city)
            print(f"\n[Я.Карты] {q}")
            try:
                remaining = max(0, max_items - total) if max_items else 0
                added = await _process_query(
                    context, q, category, city, remaining, state
                )
                total += added
                print(f"  + {added}")
            except Exception as e:
                state.record("search_navigation_error")
                print(f"  CRIT: {e}")
            await asyncio.sleep(random.uniform(2.5, 5.0))
        if state.stopped or (max_items and total >= max_items):
            break

    include_global = not max_queries and not query_offset and not city_offset
    for q, category, city in EXTRA_QUERIES_GLOBAL if include_global else []:
        if state.stopped or (max_items and total >= max_items):
            break
        print(f"\n[Я.Карты] {q}")
        try:
            remaining = max(0, max_items - total) if max_items else 0
            added = await _process_query(context, q, category, city, remaining, state)
            total += added
            print(f"  + {added}")
        except Exception as e:
            state.record("search_navigation_error")
            print(f"  CRIT: {e}")
        await asyncio.sleep(random.uniform(3.0, 5.0))

    print(
        f"\n[Я.Карты] всего новых: {total}; "
        f"detail_requests={state.detail_requests}; cache_hits={state.cache_hits}; "
        f"statuses={dict(state.statuses)}; "
        f"stop={state.circuit_reason or 'completed'}"
    )
    if state.circuit_reason and state.circuit_reason != "detail_budget_exhausted":
        raise RuntimeError(f"Yandex circuit open: {state.circuit_reason}")
    return total
