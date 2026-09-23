# B2B Crimea Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Форк `horeca_parser` в `Parser_B2B`, который собирает контактную базу коммерческих компаний Крыма по ~20 сегментам и выдаёт `outreach_ready.xlsx` с email, городом, сегментом и «поводом для КП».

**Architecture:** Ядро HoReCa-парсера (оркестратор `main.py`, storage/entity resolution/merger, enrichment, handoff, TG/Drive, hardened deploy) переносится без изменения логики. Вся таксономия переезжает в один модуль `config/segments.py`, который читают все источники. Добавляются три новых модуля: `utils/web_signals.py` (сигналы по сайту), `utils/cross_base.py` (исключение компаний из баз HoReCa/отелей) и новая схема `utils/outreach_export.py`.

**Tech Stack:** Python 3.13, aiohttp, Playwright (Chromium), openpyxl, pytest 9, systemd (VPS).

**Spec:** `docs/superpowers/specs/2026-09-24-b2b-crimea-parser-design.md`

## Global Constraints

- Исходник форка: `C:\Users\TLT-1\Documents\GitHub\horeca_parser` (HEAD `89ced6b`). Рабочий репозиторий: `C:\Users\TLT-1\Documents\GitHub\Parser_B2B`. В horeca_parser ничего не менять.
- Все команды выполняются в Git Bash из корня `Parser_B2B`. Python — `.venv/Scripts/python`.
- Тесты: `.venv/Scripts/python -m pytest -p no:cacheprovider -q`. В конце каждой задачи весь набор зелёный.
- Сегмент хранится в существующем поле `client_type` как ключ (латиница, например `avto`). Значение «не определён» — `прочее`.
- Ровно 20 сегментов; у каждого ≥ 1 OSM-тег, ≥ 1 Yandex-запрос, ≥ 1 VK-ключ, ≥ 1 alias. Ключи, aliases, VK-ключи и OSM-теги уникальны между сегментами.
- Флаги исключения: `excluded_chain`, `excluded_gov`, `excluded_other_base_type`, `already_in_other_base` (последний — только причина в outreach_review).
- Сигналы и порядок приоритета: `no_website` > `site_dead` > `no_https` > `no_mobile` > `no_online_booking` > `site_builder` > `outdated`; без сигналов → «Автоматизация/боты/CRM»; не проверен → `not_checked`.
- `approved_for_send=false` и `auto_send_allowed=false` во всех артефактах. Никакой отправки писем.
- Яндекс только в пакетном режиме (`YANDEX_CITY_OFFSET`/`YANDEX_QUERY_OFFSET`, `YANDEX_MAX_DETAIL_REQUESTS=600`).
- Деплой: `/home/b2b_parser`, пользователь `b2b-parser`, таймер пятница 03:00 MSK, disabled до приёмки. `MemoryMax=3G`, `RuntimeMaxSec=12h`.
- Окончание commit-сообщений: пустая строка + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Название компании содержит и сегментное слово, и бренд сети («Магнит Косметик», «Салон МТС») → `excluded_chain`, в outreach не попадает. Тесты: Task 3, Task 10.
2. В поле «сайт» лежит соцсеть или агрегатор (`vk.com/...`, `taplink.cc/...`) → сигнал `no_website`, сетевой запрос к нему не делается. Тесты: Task 8.
3. Слова, похожие на HoReCa («Барбершоп», «Магнитные доски», «Столовые приборы оптом»), не должны исключаться как общепит или сеть. Тест: Task 3.
4. В чужой базе есть `hotel@mail.ru` и сайт `vk.com/hotel` → это не должно исключать все компании на mail.ru и VK; исключаются только точные совпадения email или корпоративного домена. Тест: Task 9.
5. HTTPS не работает, а HTTP отвечает → сигнал `no_https`, а не `site_dead`. Тест: Task 8.

---

### Task 1: Форк и зелёный baseline

**Files:**
- Create: всё дерево из `git archive` horeca_parser в корень `Parser_B2B`
- Create: `.venv/` (не коммитится, уже в `.gitignore`)

**Interfaces:**
- Consumes: —
- Produces: рабочая копия HoReCa-кода в `Parser_B2B`, из которой исходят все следующие задачи.

- [ ] **Step 1: Экспортировать отслеживаемые файлы horeca_parser (без .git, секретов и output)**

```bash
cd /c/Users/TLT-1/Documents/GitHub/Parser_B2B
git -C ../horeca_parser status --short   # Expected: пусто (чистое дерево)
git -C ../horeca_parser archive --format=tar HEAD | tar -xf - -C .
ls
```
Expected: `main.py parsers utils tests deploy docs README.md requirements*.txt .env.example .gitignore ...` и наш `docs/superpowers/`.

- [ ] **Step 2: Создать venv и поставить зависимости**

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -q -r requirements-dev.txt
```
Expected: установка без ошибок.

- [ ] **Step 3: Прогнать baseline**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q`
Expected: все тесты PASS. Если какие-то тесты падают только из-за Windows (пути, права), выпишите их имена. Это baseline-исключения: они должны остаться тем же списком до конца плана, новые падения недопустимы.

- [ ] **Step 4: Commit**

```bash
git add -A
git status --short | grep -E "\.env$|token\.json|output/" && echo "STOP: secrets staged" || true
git commit -q -m "chore: fork horeca_parser 89ced6b as B2B parser baseline

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Таксономия `config/segments.py` и `utils/categories.py`

**Files:**
- Create: `config/__init__.py` (пустой)
- Create: `config/segments.py`
- Modify: `utils/categories.py` (полная замена)
- Test: `tests/test_segments.py` (новый), `tests/test_categories.py` (полная замена)

**Interfaces:**
- Consumes: —
- Produces (используются задачами 3–11):
  - `Segment` (frozen dataclass: `key, title, osm_tags, yandex_queries, vk_keywords, aliases, booking_relevant, competitor`)
  - `SEGMENTS: tuple[Segment, ...]`, `SEGMENT_BY_KEY: dict[str, Segment]`, `DEFAULT_SEGMENT = "прочее"`
  - `fold(value: object) -> str`
  - `normalize_segment(text: object) -> str`
  - `segment_title(key: object) -> str`
  - `osm_segment(tags: dict) -> tuple[str, str]` → `(segment_key, "k=v")`
  - `osm_tag_groups() -> dict[str, tuple[str, ...]]`
  - `yandex_queries() -> list[tuple[str, str]]` → `[("<запрос> {city}", key), ...]`
  - `vk_queries() -> list[tuple[str, str]]` → `[(keyword, key), ...]`
  - `crawler_triggers() -> tuple[str, ...]`
  - `utils.categories.normalize(category) -> str`, `CANONICAL: list[str]`, `DEFAULT: str`

- [ ] **Step 1: Write the failing tests**

`tests/test_segments.py`:

```python
import pytest

from config import segments as seg


def test_twenty_unique_segments_with_all_search_inputs():
    keys = [s.key for s in seg.SEGMENTS]
    assert len(keys) == len(set(keys)) == 20
    for s in seg.SEGMENTS:
        assert s.osm_tags and s.yandex_queries and s.vk_keywords and s.aliases, s.key


@pytest.mark.parametrize("attr", ["aliases", "vk_keywords", "osm_tags"])
def test_search_inputs_are_unique_across_segments(attr):
    owner = {}
    for s in seg.SEGMENTS:
        for value in getattr(s, attr):
            identity = value if attr == "osm_tags" else seg.fold(value)
            assert owner.setdefault(identity, s.key) == s.key, (value, owner[identity], s.key)


def test_every_alias_query_and_keyword_normalizes_to_its_own_segment():
    for s in seg.SEGMENTS:
        for text in (*s.aliases, *s.yandex_queries, *s.vk_keywords, s.key, s.title):
            assert seg.normalize_segment(text) == s.key, (text, s.key)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Автошкола Старт", "obrazovanie"),
        ("Прокат автомобилей", "avto"),
        ("Прокат катамаранов", "turizm"),
        ("Ветеринарная клиника Айболит", "vet"),
        ("Стоматологическая клиника", "medicina"),
        ("Химчистка одежды", "klining_uslugi"),
        ("Винзавод Солнечная долина", "agro_vino"),
        ("Ведущий застройщик Крыма", "nedvizhimost"),
        ("Изготовление мебели", "mebel"),
        ("Пятёрочка", "прочее"),
        ("", "прочее"),
        (None, "прочее"),
    ],
)
def test_normalize_segment_prefers_longest_alias(text, expected):
    assert seg.normalize_segment(text) == expected


def test_osm_segment_and_tag_groups():
    assert seg.osm_segment({"shop": "car_repair"}) == ("avto", "shop=car_repair")
    assert seg.osm_segment({"amenity": "restaurant"}) == ("прочее", "")
    groups = seg.osm_tag_groups()
    assert "car_repair" in groups["shop"]
    assert "estate_agent" in groups["office"]


def test_query_builders_and_titles():
    yandex = seg.yandex_queries()
    assert len(yandex) == 60
    assert ("автосервис {city}", "avto") in yandex
    assert ("грузоперевозки", "logistika") in seg.vk_queries()
    assert seg.segment_title("stroitelstvo") == "Строительство и ремонт"
    assert seg.segment_title("unknown") == "Прочее"
    assert "натяжн" in seg.crawler_triggers()


def test_booking_and_competitor_flags():
    assert seg.SEGMENT_BY_KEY["krasota"].booking_relevant is True
    assert seg.SEGMENT_BY_KEY["torgovlya"].booking_relevant is False
    assert [s.key for s in seg.SEGMENTS if s.competitor] == ["it_svyaz"]
```

`tests/test_categories.py` (полная замена):

```python
"""utils.categories — тонкая обёртка над config.segments."""
from config.segments import SEGMENTS
from utils.categories import CANONICAL, DEFAULT, normalize


def test_normalize_delegates_to_segments():
    assert normalize("Автосервис") == "avto"
    assert normalize("stroitelstvo") == "stroitelstvo"


def test_empty_and_unknown_return_default():
    assert DEFAULT == "прочее"
    assert normalize("") == "прочее"
    assert normalize(None) == "прочее"
    assert normalize("ресторан") == "прочее"


def test_canonical_lists_segment_keys():
    assert CANONICAL == [s.key for s in SEGMENTS]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_segments.py tests/test_categories.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`.

- [ ] **Step 3: Write implementation**

`config/__init__.py` — пустой файл.

`config/segments.py`:

```python
"""Единственный источник B2B-таксономии.

Каждый сегмент описывает, как его искать во всех источниках (OSM, Яндекс,
VK) и как распознавать в свободном тексте (рубрика VK, название, заголовок
сайта). Добавить сегмент = добавить одну запись в SEGMENTS; код источников
не меняется.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_SEGMENT = "прочее"


@dataclass(frozen=True)
class Segment:
    key: str
    title: str
    osm_tags: tuple[tuple[str, str], ...]
    yandex_queries: tuple[str, ...]
    vk_keywords: tuple[str, ...]
    aliases: tuple[str, ...]
    booking_relevant: bool = False
    competitor: bool = False


SEGMENTS: tuple[Segment, ...] = (
    Segment(
        key="stroitelstvo",
        title="Строительство и ремонт",
        osm_tags=(
            ("craft", "builder"), ("craft", "window_construction"),
            ("craft", "plumber"), ("craft", "electrician"), ("craft", "roofer"),
            ("shop", "doityourself"), ("shop", "hardware"),
            ("office", "construction_company"),
        ),
        yandex_queries=("строительная компания", "ремонт квартир", "окна пвх"),
        vk_keywords=("строительство", "ремонт квартир", "натяжные потолки", "стройматериалы"),
        aliases=(
            "строител", "стройматериал", "ремонт квартир", "отделочн", "натяжн",
            "окна пвх", "кровл", "сантехник", "электромонтаж",
        ),
    ),
    Segment(
        key="nedvizhimost",
        title="Недвижимость, агентства",
        osm_tags=(("office", "estate_agent"),),
        yandex_queries=("агентство недвижимости", "застройщик", "новостройки"),
        vk_keywords=("недвижимость", "риелтор", "новостройки"),
        aliases=("недвижим", "риелтор", "риэлтор", "застройщик", "новостройк"),
    ),
    Segment(
        key="avto",
        title="Авто: СТО, автосалоны, запчасти",
        osm_tags=(
            ("shop", "car"), ("shop", "car_repair"), ("shop", "car_parts"),
            ("shop", "tyres"), ("amenity", "car_wash"), ("amenity", "car_rental"),
        ),
        yandex_queries=("автосервис", "автозапчасти", "шиномонтаж"),
        vk_keywords=("автосервис", "автозапчасти", "шиномонтаж", "прокат авто"),
        aliases=(
            "автосервис", "автосалон", "автозапчаст", "автомобил", "шиномонтаж",
            "автомойк", "автопрокат", "прокат авто", "детейлинг", "кузовн",
            "автоэлектрик",
        ),
        booking_relevant=True,
    ),
    Segment(
        key="medicina",
        title="Медицина, стоматология",
        osm_tags=(
            ("amenity", "dentist"), ("amenity", "clinic"), ("amenity", "doctors"),
            ("healthcare", "laboratory"), ("shop", "optician"),
        ),
        yandex_queries=("стоматология", "медицинский центр", "медицинская лаборатория"),
        vk_keywords=("стоматология", "медицинский центр", "клиника"),
        aliases=(
            "стоматолог", "клиник", "медицинск", "медцентр", "лаборатори",
            "оптик", "диагностическ",
        ),
        booking_relevant=True,
    ),
    Segment(
        key="krasota",
        title="Красота: салоны, барбершопы",
        osm_tags=(("shop", "hairdresser"), ("shop", "beauty"), ("shop", "massage")),
        yandex_queries=("салон красоты", "барбершоп", "косметология"),
        vk_keywords=("салон красоты", "барбершоп", "маникюр", "косметолог"),
        aliases=(
            "салон красоты", "барбер", "маникюр", "ногтев", "косметолог",
            "парикмахер", "ресниц", "эпиляц", "массаж",
        ),
        booking_relevant=True,
    ),
    Segment(
        key="fitnes",
        title="Фитнес и спорт",
        osm_tags=(
            ("leisure", "fitness_centre"), ("leisure", "sports_centre"),
            ("leisure", "dance"),
        ),
        yandex_queries=("фитнес клуб", "тренажерный зал", "школа танцев"),
        vk_keywords=("фитнес", "тренажерный зал", "йога", "школа танцев"),
        aliases=("фитнес", "тренажер", "йога", "танцев", "кроссфит", "единоборств", "бассейн"),
        booking_relevant=True,
    ),
    Segment(
        key="obrazovanie",
        title="Образование: курсы, автошколы, детские центры",
        osm_tags=(
            ("amenity", "driving_school"), ("amenity", "language_school"),
            ("amenity", "music_school"), ("amenity", "training"),
        ),
        yandex_queries=("автошкола", "детский развивающий центр", "курсы английского языка"),
        vk_keywords=("автошкола", "детский центр", "курсы", "репетитор"),
        aliases=(
            "автошкол", "курсы", "детский центр", "детский развивающ",
            "учебный центр", "репетитор", "обучени", "языковая школа",
            "подготовка к егэ",
        ),
        booking_relevant=True,
    ),
    Segment(
        key="yurist_buh",
        title="Юристы, бухгалтерия, консалтинг",
        osm_tags=(
            ("office", "lawyer"), ("office", "accountant"), ("office", "notary"),
            ("office", "consulting"), ("office", "tax_advisor"),
        ),
        yandex_queries=("юридические услуги", "бухгалтерские услуги", "адвокат"),
        vk_keywords=("юрист", "бухгалтерские услуги", "юридические услуги"),
        aliases=(
            "юридическ", "юрист", "адвокат", "бухгалтер", "нотариус",
            "консалтинг", "регистрация бизнеса",
        ),
    ),
    Segment(
        key="turizm",
        title="Туризм, экскурсии, прокат",
        osm_tags=(
            ("office", "travel_agent"), ("shop", "travel_agency"),
            ("amenity", "boat_rental"), ("amenity", "bicycle_rental"),
            ("office", "guide"),
        ),
        yandex_queries=("туристическое агентство", "экскурсии", "морские прогулки"),
        vk_keywords=("экскурсии", "турагентство", "морские прогулки", "прокат"),
        aliases=(
            "турагент", "туристическ", "турфирм", "экскурси", "прокат",
            "морские прогулки", "яхт", "дайвинг",
        ),
        booking_relevant=True,
    ),
    Segment(
        key="torgovlya",
        title="Торговля и опт",
        osm_tags=(
            ("shop", "wholesale"), ("shop", "trade"), ("shop", "clothes"),
            ("shop", "shoes"), ("shop", "florist"), ("shop", "gift"),
            ("shop", "jewelry"), ("shop", "mobile_phone"),
        ),
        yandex_queries=("оптовая база", "магазин одежды", "цветочный магазин"),
        vk_keywords=("оптом", "магазин одежды", "цветы", "сувениры"),
        aliases=(
            "оптов", "оптом", "магазин одежды", "одежд", "обув", "цветы",
            "цветочн", "ювелир", "подарк", "сувенир",
        ),
    ),
    Segment(
        key="proizvodstvo",
        title="Производство",
        osm_tags=(
            ("man_made", "works"), ("craft", "metal_construction"),
            ("craft", "stonemason"),
        ),
        yandex_queries=("производство", "металлоконструкции", "завод"),
        vk_keywords=("производство", "изготовление на заказ", "металлоконструкции"),
        aliases=("производств", "завод", "фабрик", "изготовлен", "металлоконструкц", "цех"),
    ),
    Segment(
        key="logistika",
        title="Логистика, грузоперевозки",
        osm_tags=(
            ("office", "logistics"), ("office", "moving_company"),
            ("shop", "storage_rental"),
        ),
        yandex_queries=("грузоперевозки", "транспортная компания", "эвакуатор"),
        vk_keywords=("грузоперевозки", "грузчики", "эвакуатор"),
        aliases=(
            "грузоперевоз", "транспортная компания", "логистик", "грузчик",
            "эвакуатор", "переезд", "доставка грузов",
        ),
    ),
    Segment(
        key="event",
        title="Event, свадьбы, фото/видео",
        osm_tags=(
            ("shop", "photo"), ("craft", "photographer"),
            ("amenity", "events_venue"), ("shop", "party"),
        ),
        yandex_queries=("организация праздников", "свадебное агентство", "фотостудия"),
        vk_keywords=("организация праздников", "свадьба", "фотограф", "ведущий"),
        aliases=(
            "праздник", "свадеб", "свадьб", "фотограф", "фотостуди",
            "видеограф", "ведущий", "аниматор",
        ),
        booking_relevant=True,
    ),
    Segment(
        key="mebel",
        title="Мебель и интерьер",
        osm_tags=(
            ("shop", "furniture"), ("shop", "interior_decoration"),
            ("shop", "kitchen"), ("craft", "carpenter"),
        ),
        yandex_queries=("мебель на заказ", "кухни на заказ", "дизайн интерьера"),
        vk_keywords=("мебель на заказ", "кухни на заказ", "дизайн интерьера"),
        aliases=(
            "мебел", "мебель на заказ", "изготовление мебели", "кухни на заказ",
            "шкафы-купе", "дизайн интерьер", "дизайнер интерьер",
        ),
    ),
    Segment(
        key="vet",
        title="Ветклиники, зоотовары",
        osm_tags=(("amenity", "veterinary"), ("shop", "pet"), ("shop", "pet_grooming")),
        yandex_queries=("ветеринарная клиника", "зоомагазин", "груминг"),
        vk_keywords=("ветеринарная клиника", "зоомагазин", "груминг"),
        aliases=("ветеринар", "ветклиник", "зоомагазин", "зоотовар", "груминг"),
        booking_relevant=True,
    ),
    Segment(
        key="klining_uslugi",
        title="Клининг, бытовые услуги",
        osm_tags=(
            ("shop", "dry_cleaning"), ("shop", "laundry"), ("craft", "tailor"),
            ("craft", "shoemaker"), ("shop", "repair"),
        ),
        yandex_queries=("клининговая компания", "химчистка", "ремонт бытовой техники"),
        vk_keywords=("клининг", "химчистка", "ателье", "мастер на час"),
        aliases=(
            "клининг", "уборк", "химчистк", "прачечн", "ателье", "ремонт техники",
            "ремонт бытовой техники", "ремонт обуви", "ремонт телефон", "мастер на час",
        ),
    ),
    Segment(
        key="agro_vino",
        title="Агро, виноделие",
        osm_tags=(
            ("craft", "winery"), ("shop", "farm"), ("shop", "agrarian"),
            ("shop", "garden_centre"),
        ),
        yandex_queries=("винодельня", "фермерское хозяйство", "питомник растений"),
        vk_keywords=("винодельня", "фермерские продукты", "саженцы"),
        aliases=("винодел", "винзавод", "фермер", "агро", "сельхоз", "саженц", "питомник растений"),
    ),
    Segment(
        key="it_svyaz",
        title="IT и связь",
        osm_tags=(("office", "it"), ("office", "telecommunication"), ("shop", "computer")),
        yandex_queries=("веб студия", "интернет провайдер", "ремонт компьютеров"),
        vk_keywords=("разработка сайтов", "веб студия", "интернет провайдер"),
        aliases=(
            "веб-студи", "веб студи", "разработка сайт", "создание сайт",
            "провайдер", "ремонт компьютер", "it-компани", "программн",
        ),
        competitor=True,
    ),
    Segment(
        key="finansy",
        title="Страхование, финансы",
        osm_tags=(
            ("office", "insurance"), ("office", "financial"),
            ("office", "financial_advisor"), ("shop", "pawnbroker"),
        ),
        yandex_queries=("страховая компания", "кредитный брокер", "лизинг"),
        vk_keywords=("страхование", "осаго", "кредитный брокер"),
        aliases=("страхов", "осаго", "кредитн", "лизинг", "ломбард", "микрозайм", "финансов"),
    ),
    Segment(
        key="reklama",
        title="Реклама, полиграфия",
        osm_tags=(
            ("office", "advertising_agency"), ("shop", "copyshop"),
            ("craft", "signmaker"), ("craft", "printer"),
        ),
        yandex_queries=("рекламное агентство", "полиграфия", "наружная реклама"),
        vk_keywords=("рекламное агентство", "полиграфия", "типография"),
        aliases=("реклам", "полиграф", "типограф", "вывеск", "баннер"),
    ),
)

SEGMENT_BY_KEY: dict[str, Segment] = {segment.key: segment for segment in SEGMENTS}


def fold(value: object) -> str:
    """Casefold + ё→е + схлопнутые пробелы: единая форма для сравнения."""
    text = str(value or "").casefold().replace("ё", "е")
    return " ".join(text.split())


_EXACT: dict[str, str] = {}
for _segment in SEGMENTS:
    for _text in (_segment.key, _segment.title, *_segment.aliases):
        _EXACT.setdefault(fold(_text), _segment.key)

_ALIASES_LONGEST_FIRST: tuple[tuple[str, str], ...] = tuple(sorted(
    ((fold(alias), segment.key) for segment in SEGMENTS for alias in segment.aliases),
    key=lambda pair: len(pair[0]),
    reverse=True,
))


def normalize_segment(text: object) -> str:
    """Ключ сегмента по тексту: точное совпадение, затем самый длинный alias."""
    value = fold(text)
    if not value:
        return DEFAULT_SEGMENT
    exact = _EXACT.get(value)
    if exact:
        return exact
    for alias, key in _ALIASES_LONGEST_FIRST:
        if alias in value:
            return key
    return DEFAULT_SEGMENT


def segment_title(key: object) -> str:
    segment = SEGMENT_BY_KEY.get(str(key or ""))
    return segment.title if segment else "Прочее"


def osm_segment(tags: dict) -> tuple[str, str]:
    """(segment_key, 'k=v') для первого совпавшего OSM-тега."""
    for segment in SEGMENTS:
        for key, value in segment.osm_tags:
            if str(tags.get(key) or "") == value:
                return segment.key, f"{key}={value}"
    return DEFAULT_SEGMENT, ""


def osm_tag_groups() -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for segment in SEGMENTS:
        for key, value in segment.osm_tags:
            values = groups.setdefault(key, [])
            if value not in values:
                values.append(value)
    return {key: tuple(values) for key, values in groups.items()}


def yandex_queries() -> list[tuple[str, str]]:
    return [
        (f"{query} {{city}}", segment.key)
        for segment in SEGMENTS
        for query in segment.yandex_queries
    ]


def vk_queries() -> list[tuple[str, str]]:
    return [(keyword, segment.key) for segment in SEGMENTS for keyword in segment.vk_keywords]


def crawler_triggers() -> tuple[str, ...]:
    return tuple(dict.fromkeys(fold(alias) for segment in SEGMENTS for alias in segment.aliases))
```

`utils/categories.py` (полная замена):

```python
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
```

- [ ] **Step 4: Проверить, что никто не импортирует удалённые имена**

Run: `grep -rn "from utils.categories import\|categories\.\(ALIASES\|CANONICAL\)" --include=*.py . | grep -v "^./tests/\|^./.venv/"`
Expected: только `import normalize` (или `normalize as ...`). Если где-то импортируется `ALIASES`, замените это место на `normalize()`.

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_segments.py tests/test_categories.py`
Expected: PASS.
Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q`
Expected: падают только тесты, которые правятся в задачах 4–7 и 10 (`test_osm_quality`, `test_vk_relevance`, `test_yandex_coverage`, `test_outreach_export`), а также baseline-исключения. Список запишите: к концу Task 10 он должен стать пустым.

- [ ] **Step 6: Commit**

```bash
git add config utils/categories.py tests/test_segments.py tests/test_categories.py
git commit -q -m "feat: single B2B segment taxonomy in config/segments

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Исключения (сети, госорганы, HoReCa/отели) и их применение в storage

**Files:**
- Modify: `config/segments.py` (добавить блок исключений в конец)
- Modify: `utils/storage.py` (`_prepare_item`)
- Test: `tests/test_exclusions.py` (новый)

**Interfaces:**
- Consumes: `fold` из Task 2.
- Produces:
  - `EXCLUDE_BRANDS: tuple[str, ...]`, `EXCLUDE_EMAIL_DOMAINS: tuple[str, ...]`
  - `EXCLUDED_FLAGS = frozenset({"excluded_chain", "excluded_gov", "excluded_other_base_type"})`
  - `exclusion_flags(name: object, category_text: object = "") -> list[str]` (порядок: chain, gov, other_base_type)
  - storage автоматически добавляет эти флаги в `quality_flags` каждого наблюдения.

- [ ] **Step 1: Write the failing tests**

`tests/test_exclusions.py`:

```python
import os
import tempfile

import pytest

from config.segments import exclusion_flags


@pytest.mark.parametrize(
    ("name", "category", "expected"),
    [
        ("Магнит Косметик", "krasota", ["excluded_chain"]),
        ("Салон МТС", "torgovlya", ["excluded_chain"]),
        ("Пятёрочка", "", ["excluded_chain"]),
        ("Администрация г. Ялта", "", ["excluded_gov"]),
        ("ГБУЗ РК Стоматологическая поликлиника", "medicina", ["excluded_gov"]),
        ("Кафе Ромашка", "", ["excluded_other_base_type"]),
        ("Гостевой дом У моря", "", ["excluded_other_base_type"]),
        ("Автосервис", "Ресторан", ["excluded_other_base_type"]),
        ("Барбершоп Борода", "krasota", []),
        ("Магнитные доски Крым", "reklama", []),
        ("Столовые приборы оптом", "torgovlya", []),
        ("Строительная компания Южный берег", "stroitelstvo", []),
        ("Пудра — студия макияжа", "krasota", []),
    ],
)
def test_exclusion_flags(name, category, expected):
    assert exclusion_flags(name, category) == expected


@pytest.fixture
def isolated_storage(monkeypatch):
    from utils import dedup, storage

    tmpdir = tempfile.mkdtemp(prefix="storage_excl_")
    monkeypatch.setattr(storage, "OUTPUT_DIR", tmpdir)
    monkeypatch.setattr(storage, "OUTPUT_FILE", os.path.join(tmpdir, "result_test.csv"))
    monkeypatch.setattr(storage, "_rows", [])
    monkeypatch.setattr(storage, "_seen", set())
    monkeypatch.setattr(storage, "_header_written", False)
    dedup.close()
    monkeypatch.setattr(dedup, "DEDUP_PATH", os.path.join(tmpdir, "dedup.db"))
    yield storage
    dedup.close()


def test_prepare_item_appends_exclusion_flags_once(isolated_storage):
    row = isolated_storage._prepare_item({
        "name": "Магнит Косметик", "city": "Ялта", "category": "krasota",
        "source": "OSM", "source_id": "node:1",
        "quality_flags": "manual_review|excluded_chain",
    })
    assert row["client_type"] == "krasota"
    assert row["quality_flags"].split("|") == ["manual_review", "excluded_chain"]


def test_prepare_item_keeps_clean_business_unflagged(isolated_storage):
    row = isolated_storage._prepare_item({
        "name": "Окна Юг", "city": "Ялта", "category": "stroitelstvo",
        "source": "OSM", "source_id": "node:2",
    })
    assert row["quality_flags"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_exclusions.py`
Expected: FAIL — `ImportError: cannot import name 'exclusion_flags'`.

- [ ] **Step 3: Write implementation**

Дописать в конец `config/segments.py`:

```python
# --- Исключения -----------------------------------------------------------
# Сети и федеральные бренды: филиалы не покупают разработку локально.
EXCLUDE_BRANDS: tuple[str, ...] = (
    "магнит", "пятерочка", "перекресток", "ашан", "пуд", "fix price",
    "фикс прайс", "мтс", "мегафон", "билайн", "tele2", "теле2",
    "win mobile", "волна мобайл", "сбербанк", "сбер", "рнкб", "генбанк",
    "втб", "почта банк", "dns", "эльдорадо", "м.видео", "мвидео",
    "спортмастер", "wildberries", "вайлдберриз", "ozon", "озон",
    "яндекс маркет", "сдэк", "cdek", "boxberry", "деловые линии", "пэк",
)
EXCLUDE_EMAIL_DOMAINS: tuple[str, ...] = (
    "magnit.ru", "x5.ru", "mts.ru", "megafon.ru", "beeline.ru", "tele2.ru",
    "sberbank.ru", "sber.ru", "rncb.ru", "genbank.ru", "vtb.ru",
    "pochtabank.ru", "dns-shop.ru", "eldorado.ru", "mvideo.ru",
    "sportmaster.ru", "fix-price.com", "wildberries.ru", "ozon.ru",
    "cdek.ru", "boxberry.ru", "dellin.ru", "pecom.ru",
)
EXCLUDED_FLAGS = frozenset({"excluded_chain", "excluded_gov", "excluded_other_base_type"})

_BRAND_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(fold(brand)) for brand in EXCLUDE_BRANDS) + r")(?!\w)"
)
_GOV_RE = re.compile(
    r"\b(?:администраци\w*|мфц|госуслуг\w*|прокуратур\w*|полици\w*|мвд|"
    r"министерств\w*|росреестр\w*|налогов\w+ инспекци\w*|пенсионн\w+ фонд\w*|"
    r"социальн\w+ фонд\w*|гбу\w*|мбу\w*|гбоу|мбоу|мбдоу|гбдоу|гауз|муп|гуп|"
    r"фгуп|фгбу\w*|суд|банкомат\w*|платежн\w+ терминал\w*|почта россии|"
    r"отделение почтов\w+ связи)\b"
)
# HoReCa и размещение уже покрыты двумя другими базами.
_OTHER_BASE_RE = re.compile(
    r"\b(?:ресторан\w*|кафе|бар|паб|кофейн\w*|столовая|пиццери\w*|бургерн\w*|"
    r"шаурм\w*|суши|фудкорт\w*|кондитерск\w*|пекарн\w*|отел\w*|гостиниц\w*|"
    r"мини-гостиниц\w*|гостев\w+\s+дом\w*|хостел\w*|пансионат\w*|санатори\w*|"
    r"база отдыха)\b"
)


def exclusion_flags(name: object, category_text: object = "") -> list[str]:
    """Флаги исключения по названию (+ рубрике/категории для gov/HoReCa)."""
    name_text = fold(name)
    full_text = f"{name_text} {fold(category_text)}".strip()
    flags: list[str] = []
    if _BRAND_RE.search(name_text):
        flags.append("excluded_chain")
    if _GOV_RE.search(full_text):
        flags.append("excluded_gov")
    if _OTHER_BASE_RE.search(full_text):
        flags.append("excluded_other_base_type")
    return flags
```

В `utils/storage.py` добавить импорт рядом с `from utils.categories import ...`:

```python
from config.segments import exclusion_flags
```

и в `_prepare_item` заменить блок

```python
    if not cleaned.get("client_type"):
        cleaned["client_type"] = normalize_category(cleaned.get("category", ""))
    cleaned["provenance"] = provenance_for_rows([cleaned])
    return cleaned
```

на

```python
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
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_exclusions.py tests/test_storage_append.py tests/test_segments.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config/segments.py utils/storage.py tests/test_exclusions.py
git commit -q -m "feat: flag chains, government bodies and HoReCa/hotels at storage

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: OSM-источник по тегам сегментов

**Files:**
- Modify: `parsers/osm.py`
- Test: `tests/test_osm_quality.py` (полная замена)

**Interfaces:**
- Consumes: `SEGMENTS`, `osm_segment`, `osm_tag_groups` из Task 2.
- Produces: `osm.QUERY: str`, `osm._category(tags) -> str` (ключ сегмента), `osm._raw_category(tags) -> str` ("k=v").

- [ ] **Step 1: Write the failing tests**

`tests/test_osm_quality.py` (полная замена):

```python
import pytest

from config.segments import SEGMENTS
from parsers import osm


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"shop": "car_repair"}, "avto"),
        ({"office": "estate_agent"}, "nedvizhimost"),
        ({"amenity": "dentist"}, "medicina"),
        ({"craft": "carpenter"}, "mebel"),
        ({"amenity": "restaurant"}, "прочее"),
    ],
)
def test_category_by_segment_tags(tags, expected):
    assert osm._category(tags) == expected


def test_raw_category_is_matched_tag():
    assert osm._raw_category({"shop": "car_repair", "name": "СТО"}) == "shop=car_repair"


def test_query_covers_every_segment_tag_and_no_food():
    for segment in SEGMENTS:
        for key, value in segment.osm_tags:
            assert f'nwr["{key}"' in osm.QUERY
            assert value in osm.QUERY
    assert "restaurant" not in osm.QUERY
    assert "fast_food" not in osm.QUERY


def test_all_endpoints_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(osm, "OVERPASS_ENDPOINTS", ["https://invalid.test"])
    monkeypatch.setattr(osm, "http_request", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("boom")))
    with pytest.raises(RuntimeError, match="Overpass"):
        osm._fetch_overpass()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_osm_quality.py`
Expected: FAIL — `_category({"shop": "car_repair"})` возвращает `прочее`, `QUERY` содержит `restaurant`.

- [ ] **Step 3: Write implementation**

В `parsers/osm.py`:

1. Docstring модуля заменить на:

```python
"""OpenStreetMap Overpass API: коммерческие организации Крыма по тегам сегментов.

Один HTTP-запрос — JSON со всеми node/way/relation, чьи теги перечислены в
config/segments.py (shop/office/craft/amenity/...). Каждая точка проходит
локальную проверку границы полуострова.
"""
```

2. Добавить импорт: `from config.segments import osm_segment, osm_tag_groups`.

3. Удалить `AMENITY_RE`, `SHOP_RE`, `QUERY`, `CATEGORY_MAP`, `SHOP_CATEGORY_MAP`, `CUISINE_OVERRIDES` и вставить вместо них (после `BBOX`):

```python
def _build_query() -> str:
    lines = [
        f'  nwr["{key}"~"^({"|".join(values)})$"]({BBOX});'
        for key, values in sorted(osm_tag_groups().items())
    ]
    return "[out:json][timeout:180];\n(\n" + "\n".join(lines) + "\n);\nout center tags;\n"


QUERY = _build_query()
```

4. Заменить `_category` и `_raw_category` на:

```python
def _category(tags: dict) -> str:
    return osm_segment(tags)[0]


def _raw_category(tags: dict) -> str:
    return osm_segment(tags)[1]
```

5. В `_fetch_overpass`: `"User-Agent": "b2b_parser/1.0"` и `http_request(req, timeout=240)`.

6. В `run()` заменить строку confidence на `"confidence": "0.95",`.

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_osm_quality.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add parsers/osm.py tests/test_osm_quality.py
git commit -q -m "feat: OSM source queries segment tags

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: VK-источник, B2B quality gate и причина карантина

**Files:**
- Modify: `parsers/vk_groups.py`
- Modify: `utils/quality.py`
- Modify: `utils/merger.py` (строка причины карантина)
- Modify: `tests/test_orchestration.py` (строка данных с причиной)
- Test: `tests/test_vk_relevance.py` (полная замена)

**Interfaces:**
- Consumes: `normalize_segment`, `vk_queries`, `DEFAULT_SEGMENT` из Task 2.
- Produces:
  - `vk_groups.QUERIES: list[str]`, `vk_groups.QUERY_SEGMENT: dict[str, str]`
  - `_group_relevance(group, matched_queries) -> tuple[float, list[str]]`
  - `_category_for(group, matched_queries) -> str`
  - `utils.quality.VK_QUARANTINE_FLAGS = frozenset({"vk_no_primary_segment_signal", "vk_noise_primary"})` (используется merger, crawler, outreach)

- [ ] **Step 1: Write the failing tests**

`tests/test_vk_relevance.py` (полная замена):

```python
from config.segments import SEGMENTS
from parsers import vk_groups
from parsers.vk_groups import (
    QUERIES, QUERY_SEGMENT, _category_for, _group_relevance, _pick_address,
)
from utils.quality import VK_QUARANTINE_FLAGS


def test_queries_cover_every_segment_keyword():
    assert set(QUERIES) == {kw for s in SEGMENTS for kw in s.vk_keywords}
    assert QUERY_SEGMENT["автосервис"] == "avto"


def test_business_group_passes_gate():
    score, flags = _group_relevance(
        {"name": "Автосервис Мотор", "activity": "Автомобили", "description": "Ремонт и ТО"},
        ["автосервис"],
    )
    assert score >= 0.7
    assert not set(flags) & VK_QUARANTINE_FLAGS
    assert "manual_review" not in flags


def test_group_without_segment_signal_is_quarantined():
    score, flags = _group_relevance(
        {"name": "Мы из Ялты", "activity": "Сообщество", "description": ""},
        ["ремонт квартир"],
    )
    assert score < 0.5
    assert "vk_no_primary_segment_signal" in flags
    assert "manual_review" in flags


def test_noise_community_is_quarantined_even_with_segment_word():
    score, flags = _group_relevance(
        {"name": "Барахолка Симферополь одежда", "activity": "Сообщество"},
        ["магазин одежды"],
    )
    assert score < 0.5
    assert "vk_noise_primary" in flags


def test_category_prefers_activity_then_name_then_query():
    assert _category_for({"activity": "Салон красоты", "name": "Лилия"}, ["фитнес"]) == "krasota"
    assert _category_for({"activity": "Сообщество", "name": "Натяжные потолки Ялта"}, []) == "stroitelstvo"
    assert _category_for({"activity": "Сообщество", "name": "Мастерская Ивана"}, ["грузоперевозки"]) == "logistika"


def test_vk_address_is_extracted():
    group = {"addresses": [{"city": {"title": "Ялта"}, "address": "ул. Морская, 1"}]}
    assert _pick_address(group) == "Ялта, ул. Морская, 1"


def test_dry_run_never_emits_invalid_token_alert(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    vk_groups._token_alert_sent = False
    vk_groups._maybe_alert_token_dead({"error_code": 5, "error_msg": "invalid access token"})
    assert vk_groups._token_alert_sent is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_vk_relevance.py`
Expected: FAIL — `ImportError: cannot import name 'QUERY_SEGMENT'`.

- [ ] **Step 3: Write implementation**

`utils/quality.py` — заменить docstring, `VK_QUARANTINE_FLAGS` и `VK_RELEVANCE_FLAGS`; остальной модуль не трогать:

```python
"""Shared quality policy for broad-source observations.

VK is intentionally collected broadly, but rows without a primary business
segment signal (or that look like community noise) must not independently
enter the working master, seed the crawler, or become eligible for outreach.
Keeping this policy in one module prevents the pipeline stages from drifting
apart.
"""
from __future__ import annotations


VK_QUARANTINE_FLAGS = frozenset({
    "vk_no_primary_segment_signal",
    "vk_noise_primary",
})

VK_RELEVANCE_FLAGS = frozenset({
    *VK_QUARANTINE_FLAGS,
    "manual_review",
})
```

Также в `is_weak_vk_candidate` исправить docstring: `"""Whether a row is a broad VK candidate lacking a primary segment signal."""`.

`parsers/vk_groups.py`:

1. Docstring: заменить «общепита/отдыха (рестораны, кафе, бары, клубы)» на «коммерческих компаний по B2B-сегментам», а «без первичного food-сигнала» — на «без сегментного сигнала».
2. Импорты: удалить `from utils.categories import normalize as normalize_category`, добавить `from config.segments import DEFAULT_SEGMENT, normalize_segment, vk_queries`.
3. Удалить `QUERIES`, `POSITIVE_TERMS`, `NEGATIVE_TERMS`, `PRIMARY_FOOD_RE`, `ACCOMMODATION_PRIMARY_RE`, `NON_HORECA_PRIMARY_RE`, `SUPPLIER_PRIMARY_RE`, `INACTIVE_PRIMARY_RE`, `AGGREGATOR_PRIMARY_RE`, `CONSULT_PRIMARY_RE`, `NON_FOOD_ACTIVITY_RE`, `OFF_PREMISE_RE`. На место `QUERIES` вставить:

```python
QUERY_SEGMENT: dict[str, str] = dict(vk_queries())
QUERIES: list[str] = list(QUERY_SEGMENT)

# Сообщества-«шум»: барахолки, новости, паблики — не компании.
NOISE_PRIMARY_RE = re.compile(
    r"\b(?:подслушано|барахолк\w*|объявлени\w*|вакансии|работа в|новости|"
    r"сплетни|знакомств\w*|выпускник\w*|волонт\w*|благотворительн\w*|"
    r"приют\w*|клуб любителей|фан-?клуб\w*|типичн\w*|мемы|отдам даром|помогите)\b",
    re.IGNORECASE,
)
```

4. Заменить `_group_relevance` целиком:

```python
def _group_relevance(group: dict, matched_queries: list[str]) -> tuple[float, list[str]]:
    """Score broad VK candidates; master later quarantines weak standalone rows."""
    name = str(group.get("name") or "")
    activity = str(group.get("activity") or "")
    description = str(group.get("description") or "")
    has_primary = (
        normalize_segment(name) != DEFAULT_SEGMENT
        or normalize_segment(activity) != DEFAULT_SEGMENT
    )
    corpus = " ".join((name, activity, description, str(group.get("status") or ""))).casefold()
    query_hits = [q for q in matched_queries if q.casefold() in corpus]

    score = 0.35
    if has_primary:
        score += 0.35
    if normalize_segment(description) != DEFAULT_SEGMENT:
        score += 0.10
    score += min(0.15, 0.05 * len(query_hits))

    flags: list[str] = []
    if not has_primary:
        flags.append("vk_no_primary_segment_signal")
        score = min(score, 0.60)
    if NOISE_PRIMARY_RE.search(f"{name} {activity}"):
        flags.append("vk_noise_primary")
        score = min(score, 0.25)
    score = max(0.0, min(1.0, score))
    if score < 0.5:
        flags.append("manual_review")
    return score, flags
```

5. Заменить `_category_for` целиком:

```python
def _category_for(group: dict, matched_queries: list[str]) -> str:
    for text in (group.get("activity"), group.get("name")):
        segment = normalize_segment(text)
        if segment != DEFAULT_SEGMENT:
            return segment
    for query in matched_queries:
        segment = QUERY_SEGMENT.get(query, DEFAULT_SEGMENT)
        if segment != DEFAULT_SEGMENT:
            return segment
    return DEFAULT_SEGMENT
```

6. В `run()` заменить `activity = g.get("activity") or "общепит"` на `activity = g.get("activity") or ""`.

`utils/merger.py` и `tests/test_orchestration.py`: заменить строку `vk_missing_primary_food_signal` на `vk_missing_primary_segment_signal`:

```bash
sed -i 's/vk_missing_primary_food_signal/vk_missing_primary_segment_signal/g' utils/merger.py tests/test_orchestration.py
grep -rn "food" --include=*.py utils parsers main.py | grep -v "^parsers/email_finder"
```
Expected от grep: пусто (или только комментарии; комментарии про food поправьте на «сегмент»).

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_vk_relevance.py tests/test_crawler_quality.py tests/test_orchestration.py tests/test_entity_resolution.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add parsers/vk_groups.py utils/quality.py utils/merger.py tests/test_vk_relevance.py tests/test_orchestration.py
git commit -q -m "feat: VK source searches segment keywords with B2B quality gate

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Яндекс.Карты — матрица запросов по сегментам

**Files:**
- Modify: `parsers/yandex_maps.py`
- Test: `tests/test_yandex_coverage.py`

**Interfaces:**
- Consumes: `yandex_queries` из Task 2.
- Produces: `yandex_maps.QUERIES: list[tuple[str, str]]` (60 шт.), `EXTRA_QUERIES_GLOBAL = []`.

- [ ] **Step 1: Write the failing test**

В `tests/test_yandex_coverage.py` удалить `test_queries_cover_additional_food_segments`, добавить импорт `from config.segments import SEGMENT_BY_KEY, SEGMENTS` и тест:

```python
def test_queries_cover_every_segment_without_food():
    templates = {query for query, _ in yandex_maps.QUERIES}
    for segment in SEGMENTS:
        for query in segment.yandex_queries:
            assert f"{query} {{city}}" in templates
    assert len(yandex_maps.QUERIES) == 60
    assert all(key in SEGMENT_BY_KEY for _, key in yandex_maps.QUERIES)
    assert yandex_maps.EXTRA_QUERIES_GLOBAL == []
    assert not any("ресторан" in query for query, _ in yandex_maps.QUERIES)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_yandex_coverage.py`
Expected: FAIL — `len == 16`.

- [ ] **Step 3: Write implementation**

В `parsers/yandex_maps.py` добавить импорт `from config.segments import yandex_queries` и заменить определения `QUERIES` и `EXTRA_QUERIES_GLOBAL` (вместе с комментарием над ними) на:

```python
# 20 сегментов × 3 запроса = 60 шаблонов; сегмент берётся из запроса.
QUERIES = yandex_queries()

EXTRA_QUERIES_GLOBAL: list[tuple[str, str, str]] = []
```

Строки `_ALL_CITIES = tuple(CITIES)` и `_ALL_QUERIES = tuple(QUERIES)` остаются как есть.

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_yandex_coverage.py tests/test_yandex_runtime.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add parsers/yandex_maps.py tests/test_yandex_coverage.py
git commit -q -m "feat: Yandex Maps query matrix from segments (60 templates)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Crawler — сегментные триггеры

**Files:**
- Modify: `parsers/crawler.py`
- Test: `tests/test_crawler_quality.py` (добавить тест)

**Interfaces:**
- Consumes: `crawler_triggers`, `fold`, `normalize_segment` из Task 2.
- Produces: `crawler.SEGMENT_TRIGGERS`, `crawler._has_segment_trigger(text) -> bool`.

- [ ] **Step 1: Write the failing test**

Добавить в конец `tests/test_crawler_quality.py`:

```python
def test_segment_trigger_detects_business_and_ignores_food():
    assert crawler._has_segment_trigger("<title>Натяжные потолки в Симферополе</title>")
    assert crawler._has_segment_trigger("<h1>АВТОСЕРВИС на Киевской</h1>")
    assert not crawler._has_segment_trigger("<title>Меню ресторана и бронирование столика</title>")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_crawler_quality.py`
Expected: FAIL — `AttributeError: ... has no attribute '_has_segment_trigger'`.

- [ ] **Step 3: Write implementation**

В `parsers/crawler.py`:

1. Импорт: `from config.segments import DEFAULT_SEGMENT, crawler_triggers, fold, normalize_segment`.
2. Заменить блок `HORECA_TRIGGERS = (...)` на `SEGMENT_TRIGGERS = crawler_triggers()`.
3. Заменить `INTERESTING_PATHS_RE` на:

```python
INTERESTING_PATHS_RE = re.compile(
    r"/(contacts?|about|partner|filial|location|services?|uslugi|price|objects?|"
    r"услуги|прайс|контакт|о-нас|о_нас|о-компании|объект|филиал)",
    re.IGNORECASE,
)
```

4. Заменить `_has_horeca_trigger` на:

```python
def _has_segment_trigger(text: str) -> bool:
    low = fold(text)
    return any(trigger in low for trigger in SEGMENT_TRIGGERS)
```

5. В `_crawl_domain`: переменную `has_horeca_trigger` переименовать в `has_segment_trigger` (3 места), вызов `_has_horeca_trigger(html[:8000])` заменить на `_has_segment_trigger(html[:8000])`, комментарий «Триггер «общепита»» заменить на «Сегментный триггер».
6. Блок выбора категории (от `from utils.categories import normalize as normalize_category` до конца цикла `for trig in HORECA_TRIGGERS:`) заменить на:

```python
    cat = normalize_segment(main_name)
```

Строка `"category": cat,` остаётся. Если `DEFAULT_SEGMENT` после этого не используется, уберите его из импорта.
7. Docstring модуля: строку про HoReCa-триггеры заменить на «есть сегментные триггеры из config/segments.py».

Проверка: `grep -n "horeca\|HORECA" -i parsers/crawler.py` → пусто.

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_crawler_quality.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add parsers/crawler.py tests/test_crawler_quality.py
git commit -q -m "feat: crawler gates domains by segment triggers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Сигналы «повода для КП» — `utils/web_signals.py`

**Files:**
- Create: `utils/web_signals.py`
- Test: `tests/test_web_signals.py` (новый)

**Interfaces:**
- Consumes: `SEGMENT_BY_KEY` из Task 2; `utils.safe_http.fetch_public_text(url, *, timeout_seconds, max_response_bytes, headers, allowed_content_types) -> str` (существующий).
- Produces (используются Task 10, 11):
  - `SIGNAL_ORDER`, `PITCH_BY_SIGNAL`, `DEFAULT_PITCH = "Автоматизация/боты/CRM"`, `NOT_CHECKED = "not_checked"`
  - `is_social_url(url) -> bool`, `website_host(url) -> str`
  - `detect_html_signals(html, *, booking_relevant: bool, now_year: int) -> list[str]`
  - `pick_pitch(signals) -> str`
  - `async probe_site(website, *, booking_relevant, fetch=fetch_public_text, now=None) -> dict` → `{"signals": [...], "checked_at": iso}`
  - `load_cache(path) -> dict[str, dict]`
  - `async refresh_signals(master_csv, cache_path, *, max_sites=400, max_age_days=30, parallel=8, fetch=fetch_public_text, now=None) -> dict` → `{"sites", "fresh", "checked", "pending"}`
  - `row_signals(row, cache) -> tuple[list[str], str]`

- [ ] **Step 1: Write the failing tests**

`tests/test_web_signals.py`:

```python
import asyncio
import csv
import json
from datetime import datetime, timezone

from utils import web_signals as ws
from utils.storage import FIELDS

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
VIEWPORT = '<meta name="viewport" content="width=device-width">'
MODERN = f"<html><head>{VIEWPORT}</head><body>© 2026 Компания</body></html>"
TILDA = (
    f'<html><head>{VIEWPORT}<link href="https://static.tildacdn.com/css/tilda-grid.css">'
    "</head><body>© 2025</body></html>"
)
NO_VIEWPORT = "<html><head><title>Окна</title></head><body>© 2019 Окна</body></html>"
PARKED = "<html><body>Этот домен припаркован в REG.RU</body></html>"
YCLIENTS = (
    f"<html><head>{VIEWPORT}</head><body>"
    '<script src="https://w123.yclients.com/widgetJS"></script>© 2026</body></html>'
)


def _fake_fetch(pages, calls=None):
    async def fetch(url, **kwargs):
        if calls is not None:
            calls.append(url)
        value = pages.get(url, "")
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


def test_modern_site_has_no_signals():
    assert ws.detect_html_signals(MODERN, booking_relevant=False, now_year=2026) == []


def test_builder_and_outdated_and_mobile():
    assert ws.detect_html_signals(TILDA, booking_relevant=False, now_year=2026) == ["site_builder"]
    assert ws.detect_html_signals(NO_VIEWPORT, booking_relevant=False, now_year=2026) == [
        "no_mobile", "outdated",
    ]


def test_booking_signal_only_for_booking_segments():
    assert "no_online_booking" in ws.detect_html_signals(MODERN, booking_relevant=True, now_year=2026)
    assert "no_online_booking" not in ws.detect_html_signals(MODERN, booking_relevant=False, now_year=2026)
    assert "no_online_booking" not in ws.detect_html_signals(YCLIENTS, booking_relevant=True, now_year=2026)


def test_pitch_follows_priority():
    assert ws.pick_pitch(["site_builder", "no_https"]) == "Модернизация сайта"
    assert ws.pick_pitch(["no_website"]) == "Сайт с нуля"
    assert ws.pick_pitch([]) == "Автоматизация/боты/CRM"


def test_http_only_site_is_no_https_not_dead():
    fetch = _fake_fetch({"https://okna.ru/": OSError("ssl"), "http://okna.ru/": MODERN})
    result = asyncio.run(ws.probe_site("okna.ru", booking_relevant=False, fetch=fetch, now=NOW))
    assert result == {"signals": ["no_https"], "checked_at": "2026-09-24T00:00:00+00:00"}


def test_dead_and_parked_sites():
    dead = asyncio.run(ws.probe_site("https://dead.ru/about", booking_relevant=False, fetch=_fake_fetch({}), now=NOW))
    parked = asyncio.run(ws.probe_site(
        "parked.ru", booking_relevant=False,
        fetch=_fake_fetch({"https://parked.ru/": PARKED}), now=NOW,
    ))
    assert dead["signals"] == ["site_dead"]
    assert parked["signals"] == ["site_dead"]


def test_social_link_in_website_is_no_website():
    for website in ("https://vk.com/okna_crimea", "https://taplink.cc/okna", ""):
        signals, pitch = ws.row_signals({"website": website}, {})
        assert signals == ["no_website"]
        assert pitch == "Сайт с нуля"


def test_unchecked_and_cached_sites():
    assert ws.row_signals({"website": "https://okna.ru"}, {}) == (["not_checked"], "Автоматизация/боты/CRM")
    cache = {"okna.ru": {"signals": ["site_builder", "no_https"], "checked_at": "x"}}
    assert ws.row_signals({"website": "https://www.okna.ru/"}, cache) == (
        ["no_https", "site_builder"], "Модернизация сайта",
    )


def test_refresh_respects_budget_freshness_and_social_links(tmp_path):
    master = tmp_path / "master_all.csv"
    rows = [
        {"name": "Fresh", "website": "https://fresh.ru", "client_type": "torgovlya"},
        {"name": "Stale", "website": "https://stale.ru", "client_type": "krasota"},
        {"name": "New1", "website": "https://new1.ru", "client_type": "torgovlya"},
        {"name": "New2", "website": "https://new2.ru", "client_type": "torgovlya"},
        {"name": "Social", "website": "https://vk.com/x", "client_type": "torgovlya"},
    ]
    with master.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    cache_path = tmp_path / "web_signals.json"
    cache_path.write_text(json.dumps({
        "fresh.ru": {"signals": [], "checked_at": "2026-09-20T00:00:00+00:00"},
        "stale.ru": {"signals": ["no_mobile"], "checked_at": "2026-07-01T00:00:00+00:00"},
    }), encoding="utf-8")
    calls = []

    stats = asyncio.run(ws.refresh_signals(
        str(master), str(cache_path), max_sites=2,
        fetch=_fake_fetch({"https://stale.ru/": MODERN, "https://new1.ru/": MODERN}, calls),
        now=NOW,
    ))

    assert stats == {"sites": 4, "fresh": 1, "checked": 2, "pending": 1}
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["fresh.ru"]["checked_at"] == "2026-09-20T00:00:00+00:00"
    assert saved["stale.ru"]["signals"] == ["no_online_booking"]
    assert saved["new1.ru"]["signals"] == []
    assert "new2.ru" not in saved
    assert calls == ["https://stale.ru/", "https://new1.ru/"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_web_signals.py`
Expected: FAIL — `ImportError: cannot import name 'web_signals'`.

- [ ] **Step 3: Write implementation**

`utils/web_signals.py`:

```python
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
from urllib.parse import urlparse

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

SOCIAL_HOSTS = (
    "vk.com", "vk.ru", "instagram.com", "t.me", "telegram.me", "ok.ru",
    "facebook.com", "youtube.com", "taplink.cc", "taplink.ru", "wa.me",
    "yandex.ru", "yandex.com", "2gis.ru", "avito.ru",
)
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


def website_host(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        host = urlparse(raw if "://" in raw else "https://" + raw).hostname or ""
    except ValueError:
        return ""
    host = host.casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_social_url(url: object) -> bool:
    host = website_host(url)
    return any(host == social or host.endswith("." + social) for social in SOCIAL_HOSTS)


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
```

Замечание по порядку `calls` в последнем тесте: `asyncio.gather` запускает корутины в порядке списка, и фейковый fetch не уступает управление, поэтому порядок вызовов детерминирован.

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_web_signals.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/web_signals.py tests/test_web_signals.py
git commit -q -m "feat: web signals for outreach pitch with per-domain cache

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Исключение компаний из других баз — `utils/cross_base.py`

**Files:**
- Create: `utils/cross_base.py`
- Test: `tests/test_cross_base.py` (новый)

**Interfaces:**
- Consumes: `utils.entity_resolution.website_domain(value) -> str` (существующий; срезает `www.`).
- Produces (используются Task 10, 11):
  - `env_paths(value: str | None = None) -> list[str]` (из `EXCLUDE_MASTERS`, разделитель `;`)
  - `load_exclusions(paths) -> tuple[set[str], set[str], list[str]]` → `(emails, domains, warnings)`
  - `is_in_other_base(row, emails, domains) -> bool`

- [ ] **Step 1: Write the failing tests**

`tests/test_cross_base.py`:

```python
from utils.cross_base import env_paths, is_in_other_base, load_exclusions


def test_loads_semicolon_and_comma_masters_and_warns_on_missing(tmp_path):
    horeca = tmp_path / "horeca.csv"
    horeca.write_text(
        'name;email;all_emails;website\n'
        '"Кафе";info@cafe.ru;"info@cafe.ru | book@cafe.ru";https://www.cafe.ru\n',
        encoding="utf-8-sig",
    )
    hotels = tmp_path / "hotels.csv"
    hotels.write_text(
        "Название,Email,Сайт\nОтель,hotel@mail.ru,https://vk.com/hotel\n",
        encoding="utf-8-sig",
    )

    emails, domains, warnings = load_exclusions(
        [str(horeca), str(hotels), str(tmp_path / "missing.csv")]
    )

    assert emails == {"info@cafe.ru", "book@cafe.ru", "hotel@mail.ru"}
    assert domains == {"cafe.ru"}
    assert len(warnings) == 1 and "missing.csv" in warnings[0]


def test_free_mail_and_social_hosts_never_exclude_by_domain():
    emails, domains = {"hotel@mail.ru"}, {"cafe.ru"}
    assert not is_in_other_base({"email": "stroy@mail.ru", "website": "https://vk.com/stroy"}, emails, domains)
    assert is_in_other_base({"email": "HOTEL@mail.ru"}, emails, domains)
    assert is_in_other_base({"email": "sales@cafe.ru"}, emails, domains)
    assert is_in_other_base({"all_websites": "https://cafe.ru/catering | https://x.ru"}, emails, domains)


def test_env_paths_splits_semicolons(monkeypatch):
    assert env_paths("a.csv; ;b.csv") == ["a.csv", "b.csv"]
    monkeypatch.setenv("EXCLUDE_MASTERS", "")
    assert env_paths() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_cross_base.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.cross_base'`.

- [ ] **Step 3: Write implementation**

`utils/cross_base.py`:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_cross_base.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/cross_base.py tests/test_cross_base.py
git commit -q -m "feat: exclude companies already present in HoReCa/hotel bases

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Новая схема `outreach_ready` — `utils/outreach_export.py`

**Files:**
- Modify: `utils/outreach_export.py` (полная замена)
- Test: `tests/test_outreach_export.py` (полная замена)

**Interfaces:**
- Consumes: `SEGMENT_BY_KEY`, `segment_title`, `EXCLUDED_FLAGS`, `EXCLUDE_EMAIL_DOMAINS` (Task 2–3); `row_signals` (Task 8); `is_in_other_base` (Task 9); `VK_QUARANTINE_FLAGS` (Task 5).
- Produces: `OUTREACH_HEADERS` (17 колонок), `build_outreach_exports(master_csv, output_dir=None, *, run_id="", min_confidence=None, signals_cache=None, other_base=None, include_competitors=None) -> dict` с ключами `ready_csv, ready_xlsx, review_csv, ready_rows, review_rows, min_confidence, approved_for_send, by_segment, by_signal`.

- [ ] **Step 1: Write the failing tests**

`tests/test_outreach_export.py` (полная замена):

```python
import csv
import json

from openpyxl import load_workbook

from utils.outreach_export import OUTREACH_HEADERS, build_outreach_exports
from utils.storage import FIELDS


def _write_master(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def _provenance(*segments):
    return json.dumps({"observations": [], "fields": {
        "client_type": [{"value": key, "source": "OSM", "observation_id": f"o{i}"}
                        for i, key in enumerate(segments)],
    }}, ensure_ascii=False)


ROWS = [
    {"entity_id": "ok", "name": "Окна Юг", "city": "Ялта", "client_type": "stroitelstvo",
     "email": "info@okna-yug.ru", "website": "https://okna-yug.ru", "confidence": "0.95",
     "sources": "OSM|Я.Карты", "provenance": _provenance("stroitelstvo", "mebel")},
    {"entity_id": "nosite", "name": "Салон Лилия", "city": "Симферополь", "client_type": "krasota",
     "email": "lilia@mail.ru", "social": "https://vk.com/lilia", "confidence": "0.90"},
    {"entity_id": "chain", "name": "Магнит Косметик", "city": "Ялта", "client_type": "krasota",
     "email": "info@beauty.ru", "confidence": "0.99", "quality_flags": "excluded_chain"},
    {"entity_id": "chainmail", "name": "Салон связи", "city": "Ялта", "client_type": "torgovlya",
     "email": "shop@mts.ru", "confidence": "0.99"},
    {"entity_id": "competitor", "name": "Веб-студия Код", "city": "Ялта", "client_type": "it_svyaz",
     "email": "hi@kod.ru", "confidence": "0.99"},
    {"entity_id": "nosegment", "name": "ООО Ромашка", "city": "Ялта", "client_type": "прочее",
     "email": "info@romashka.ru", "confidence": "0.99"},
    {"entity_id": "otherbase", "name": "Кейтеринг Юг", "city": "Ялта", "client_type": "event",
     "email": "info@cafe.ru", "confidence": "0.90"},
    {"entity_id": "dup", "name": "Окна Юг филиал", "city": "Ялта", "client_type": "stroitelstvo",
     "email": "info@okna-yug.ru", "confidence": "0.80"},
    {"entity_id": "low", "name": "Сомнительно", "city": "Керчь", "client_type": "avto",
     "email": "a@maybe.ru", "confidence": "0.40"},
    {"entity_id": "noise", "name": "Барахолка", "city": "Ялта", "client_type": "torgovlya",
     "email": "b@bar.ru", "confidence": "0.90", "quality_flags": "vk_noise_primary"},
]
CACHE = {"okna-yug.ru": {"signals": ["site_builder", "no_https"], "checked_at": "2026-09-24T00:00:00+00:00"}}


def _read(path):
    with open(path, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def test_outreach_rows_segments_signals_and_review_reasons(tmp_path):
    master = tmp_path / "master_all.csv"
    _write_master(master, ROWS)

    result = build_outreach_exports(
        str(master), str(tmp_path), run_id="t", min_confidence=0.7,
        signals_cache=CACHE, other_base=({"info@cafe.ru"}, set()),
        include_competitors=False,
    )

    assert result["ready_rows"] == 2
    assert result["review_rows"] == 8
    assert result["approved_for_send"] is False
    assert result["by_segment"] == {"stroitelstvo": 1, "krasota": 1}
    assert result["by_signal"] == {"no_https": 1, "site_builder": 1, "no_website": 1}

    ready = {row["Email"]: row for row in _read(result["ready_csv"])}
    assert list(next(iter(ready.values()))) == OUTREACH_HEADERS
    okna = ready["info@okna-yug.ru"]
    assert okna["Сегмент"] == "Строительство и ремонт"
    assert okna["Все сегменты"] == "Строительство и ремонт; Мебель и интерьер"
    assert okna["Сигналы"] == "no_https; site_builder"
    assert okna["Повод для КП"] == "Модернизация сайта"
    lilia = ready["lilia@mail.ru"]
    assert lilia["Сигналы"] == "no_website"
    assert lilia["Повод для КП"] == "Сайт с нуля"

    reasons = {row["entity_id"]: row["review_reason"] for row in _read(result["review_csv"])}
    assert reasons == {
        "chain": "excluded_chain",
        "chainmail": "missing_or_invalid_email",
        "competitor": "competitor_segment",
        "nosegment": "no_segment",
        "otherbase": "already_in_other_base",
        "dup": "duplicate_email",
        "low": "low_confidence",
        "noise": "hard_quality_flag",
    }

    workbook = load_workbook(result["ready_xlsx"], read_only=True)
    assert workbook.sheetnames == ["Контакты", "Метаданные"]
    assert [cell.value for cell in next(workbook["Контакты"].iter_rows())] == OUTREACH_HEADERS
    metadata = dict(workbook["Метаданные"].iter_rows(values_only=True))
    assert metadata["approved_for_send"] is False
    assert metadata["consumer"] == "manual_review_only"


def test_competitors_can_be_included_explicitly(tmp_path):
    master = tmp_path / "master_all.csv"
    _write_master(master, [ROWS[4]])
    result = build_outreach_exports(
        str(master), str(tmp_path), min_confidence=0.7, include_competitors=True,
    )
    assert result["ready_rows"] == 1
    row = _read(result["ready_csv"])[0]
    assert row["Сигналы"] == "no_website"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_outreach_export.py`
Expected: FAIL — `TypeError: build_outreach_exports() got an unexpected keyword argument 'signals_cache'`.

- [ ] **Step 3: Write implementation**

`utils/outreach_export.py` (полная замена):

```python
"""Build an approval-gated B2B outreach workbook.

The canonical master remains complete.  This module creates a smaller,
auditable delivery candidate file with one row per email, the company
segment and a heuristic pitch reason.  It never sends mail and never marks
data as approved.
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from config.segments import (
    EXCLUDE_EMAIL_DOMAINS,
    EXCLUDED_FLAGS,
    SEGMENT_BY_KEY,
    segment_title,
)
from utils.cross_base import is_in_other_base
from utils.csv_safety import neutralize_csv_formula
from utils.quality import VK_QUARANTINE_FLAGS
from utils.web_signals import row_signals


OUTREACH_HEADERS = [
    "Email", "Название", "Сегмент", "Все сегменты", "Город", "Телефон", "Сайт",
    "Соцсеть", "Адрес", "Повод для КП", "Сигналы", "Источник", "ID объекта",
    "Доверие", "Флаги качества", "Все email", "Все телефоны",
]
COLUMN_WIDTHS = [30, 36, 26, 34, 16, 22, 34, 30, 40, 26, 34, 18, 30, 10, 30, 44, 44]

HARD_REJECT_FLAGS = {"outside_crimea", *VK_QUARANTINE_FLAGS}
EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
EMAIL_BLACKLIST = re.compile(
    r"(?:^|@)(?:example(?:\.|@)|test(?:\.|@))|"
    r"no-?reply|mailer-daemon|postmaster@|webmaster@|abuse@|"
    r"@.*\.gov\.ru$|@stacks\.vk-portal\.net$",
    re.I,
)
PREFERRED_PREFIXES = (
    "info", "office", "sales", "zakaz", "order", "manager", "contact",
    "director", "reception", "booking",
)
TRUE_VALUES = {"1", "true", "yes", "on"}


def _confidence(row: dict) -> float:
    try:
        value = float(str(row.get("confidence") or ""))
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, value))


def _flags(row: dict) -> set[str]:
    return {
        flag.strip().casefold()
        for flag in str(row.get("quality_flags") or "").split("|")
        if flag.strip()
    }


def _website_domain(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        host = (urlparse(raw if "://" in raw else "https://" + raw).hostname or "")
        return host.casefold().removeprefix("www.").rstrip(".")
    except ValueError:
        return ""


def _blocked_domain(domain: str) -> bool:
    return any(domain == blocked or domain.endswith("." + blocked) for blocked in EXCLUDE_EMAIL_DOMAINS)


def _email_candidates(row: dict) -> list[str]:
    website_domain = _website_domain(row.get("website", ""))
    candidates: dict[str, int] = {}
    material = " | ".join(str(row.get(field) or "") for field in ("email", "all_emails"))
    for match in EMAIL_RE.findall(material):
        email = match.strip().casefold()
        local, _separator, domain = email.partition("@")
        if EMAIL_BLACKLIST.search(email) or _blocked_domain(domain):
            continue
        score = 0
        if website_domain and (
            domain == website_domain
            or domain.endswith("." + website_domain)
            or website_domain.endswith("." + domain)
        ):
            score += 100
        for index, prefix in enumerate(PREFERRED_PREFIXES):
            if local == prefix or local.startswith(prefix + "."):
                score += 50 - index
                break
        candidates[email] = max(score, candidates.get(email, score))
    return [email for email, _score in sorted(candidates.items(), key=lambda item: (-item[1], item[0]))]


def _segments_all(row: dict) -> str:
    candidates = [str(row.get("client_type") or "")]
    try:
        provenance = json.loads(str(row.get("provenance") or "") or "{}")
    except ValueError:
        provenance = {}
    fields = provenance.get("fields") if isinstance(provenance, dict) else None
    if not isinstance(fields, dict):
        fields = {}
    for evidence in fields.get("client_type") or []:
        if isinstance(evidence, dict):
            candidates.append(str(evidence.get("value") or ""))
    keys = [key for key in dict.fromkeys(candidates) if key in SEGMENT_BY_KEY]
    return "; ".join(segment_title(key) for key in keys)


def _review_reason(
    row: dict,
    min_confidence: float,
    *,
    include_competitors: bool,
    other_base: tuple[set[str], set[str]] | None,
) -> str:
    if not str(row.get("name") or "").strip():
        return "missing_name"
    flags = _flags(row)
    excluded = sorted(flags & EXCLUDED_FLAGS)
    if excluded:
        return excluded[0]
    segment = SEGMENT_BY_KEY.get(str(row.get("client_type") or "").strip())
    if segment is None:
        return "no_segment"
    if segment.competitor and not include_competitors:
        return "competitor_segment"
    if not _email_candidates(row):
        return "missing_or_invalid_email"
    if flags & HARD_REJECT_FLAGS:
        return "hard_quality_flag"
    if "vk_weak_contact_donor" in flags:
        return "weak_vk_contact_donor"
    confidence = _confidence(row)
    if confidence < min_confidence:
        return "low_confidence"
    if "manual_review" in flags and confidence < max(min_confidence, 0.85):
        return "manual_review"
    if other_base and is_in_other_base(row, *other_base):
        return "already_in_other_base"
    return ""


def _outreach_row(row: dict, email: str, signals: list[str], pitch: str) -> dict:
    return {
        "Email": email,
        "Название": row.get("name", ""),
        "Сегмент": segment_title(row.get("client_type")),
        "Все сегменты": _segments_all(row),
        "Город": row.get("city", ""),
        "Телефон": row.get("phone", ""),
        "Сайт": row.get("website", ""),
        "Соцсеть": row.get("social", ""),
        "Адрес": row.get("address", ""),
        "Повод для КП": pitch,
        "Сигналы": "; ".join(signals),
        "Источник": row.get("sources") or row.get("source", ""),
        "ID объекта": row.get("entity_id", ""),
        "Доверие": f"{_confidence(row):.2f}",
        "Флаги качества": row.get("quality_flags", ""),
        "Все email": row.get("all_emails") or row.get("email", ""),
        "Все телефоны": row.get("all_phones") or row.get("phone", ""),
    }


def _safe_row(row: dict, headers: list[str]) -> dict:
    return {header: neutralize_csv_formula(str(row.get(header) or "")) for header in headers}


def _atomic_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=headers, delimiter=";",
            quoting=csv.QUOTE_ALL, extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(_safe_row(row, headers) for row in rows)
    os.replace(tmp, path)


def _atomic_xlsx(
    path: Path,
    rows: list[dict],
    *,
    generated_at: str,
    run_id: str,
    min_confidence: float,
    review_count: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.xlsx")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Контакты"
    header_fill = PatternFill("solid", fgColor="1F2937")
    for column, header in enumerate(OUTREACH_HEADERS, start=1):
        cell = sheet.cell(row=1, column=column, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for row_number, row in enumerate(rows, start=2):
        safe = _safe_row(row, OUTREACH_HEADERS)
        for column, header in enumerate(OUTREACH_HEADERS, start=1):
            sheet.cell(row=row_number, column=column, value=safe[header])
    sheet.freeze_panes = "A2"
    last_column = get_column_letter(len(OUTREACH_HEADERS))
    sheet.auto_filter.ref = f"A1:{last_column}{max(1, len(rows) + 1)}"
    for index, width in enumerate(COLUMN_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    metadata = workbook.create_sheet("Метаданные")
    metadata_rows = [
        ("schema_version", 1),
        ("state", "ready_for_review"),
        ("approved_for_send", False),
        ("run_id", run_id),
        ("generated_at", generated_at),
        ("ready_rows", len(rows)),
        ("review_rows", review_count),
        ("min_confidence", min_confidence),
        ("consumer", "manual_review_only"),
    ]
    for row_number, (key, value) in enumerate(metadata_rows, start=1):
        metadata.cell(row=row_number, column=1, value=key).font = Font(bold=True)
        metadata.cell(row=row_number, column=2, value=value)
    metadata.column_dimensions["A"].width = 24
    metadata.column_dimensions["B"].width = 38
    workbook.save(tmp)
    os.replace(tmp, path)


def build_outreach_exports(
    master_csv: str,
    output_dir: str | None = None,
    *,
    run_id: str = "",
    min_confidence: float | None = None,
    signals_cache: dict | None = None,
    other_base: tuple[set[str], set[str]] | None = None,
    include_competitors: bool | None = None,
) -> dict:
    """Create ready/review artifacts and return their paths and counts."""
    source = Path(master_csv)
    target_dir = Path(output_dir) if output_dir else source.parent
    if min_confidence is None:
        try:
            min_confidence = float(os.getenv("OUTREACH_MIN_CONFIDENCE", "0.70"))
        except ValueError:
            min_confidence = 0.70
    min_confidence = max(0.0, min(1.0, min_confidence))
    if include_competitors is None:
        include_competitors = (
            os.getenv("OUTREACH_INCLUDE_COMPETITORS", "").strip().casefold() in TRUE_VALUES
        )
    cache = signals_cache or {}

    with source.open(newline="", encoding="utf-8-sig") as handle:
        master_rows = list(csv.DictReader(handle, delimiter=";"))

    candidates: list[tuple[float, int, dict, str]] = []
    review_rows: list[dict] = []
    for row in master_rows:
        reason = _review_reason(
            row, min_confidence,
            include_competitors=include_competitors, other_base=other_base,
        )
        if reason:
            review_rows.append({**row, "review_reason": reason})
            continue
        email = _email_candidates(row)[0]
        completeness = sum(bool(row.get(field)) for field in ("phone", "website", "social", "address"))
        candidates.append((_confidence(row), completeness, row, email))

    ready_rows: list[dict] = []
    by_segment: Counter = Counter()
    by_signal: Counter = Counter()
    used_emails: set[str] = set()
    for _confidence_value, _completeness, row, email in sorted(
        candidates, key=lambda item: (-item[0], -item[1], item[3]),
    ):
        if email in used_emails:
            review_rows.append({**row, "review_reason": "duplicate_email"})
            continue
        used_emails.add(email)
        signals, pitch = row_signals(row, cache)
        ready_rows.append(_outreach_row(row, email, signals, pitch))
        by_segment[str(row.get("client_type") or "")] += 1
        by_signal.update(signals)
    ready_rows.sort(key=lambda row: (str(row["Город"]), str(row["Название"]), row["Email"]))

    ready_csv = target_dir / "outreach_ready.csv"
    ready_xlsx = target_dir / "outreach_ready.xlsx"
    review_csv = target_dir / "outreach_review.csv"
    review_headers = list(master_rows[0].keys()) + ["review_reason"] if master_rows else ["review_reason"]
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _atomic_csv(ready_csv, OUTREACH_HEADERS, ready_rows)
    _atomic_csv(review_csv, review_headers, review_rows)
    _atomic_xlsx(
        ready_xlsx, ready_rows,
        generated_at=generated_at, run_id=run_id,
        min_confidence=min_confidence, review_count=len(review_rows),
    )
    return {
        "ready_csv": str(ready_csv),
        "ready_xlsx": str(ready_xlsx),
        "review_csv": str(review_csv),
        "ready_rows": len(ready_rows),
        "review_rows": len(review_rows),
        "min_confidence": min_confidence,
        "approved_for_send": False,
        "by_segment": dict(by_segment),
        "by_signal": dict(by_signal),
    }
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_outreach_export.py`
Expected: PASS.
Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q`
Expected: все PASS, кроме baseline-исключений из Task 1 (список из Task 2 Step 5 теперь пуст).

- [ ] **Step 5: Commit**

```bash
git add utils/outreach_export.py tests/test_outreach_export.py
git commit -q -m "feat: B2B outreach schema with segment, pitch and exclusions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Интеграция в оркестратор, Excel и Telegram

**Files:**
- Modify: `main.py` (новая функция `_build_outreach`, вызов в `_pipeline`, брендинг, idempotency-префиксы)
- Modify: `utils/excel_export.py` (колонка «Сегмент», палитра по сегментам)
- Modify: `utils/telegram_notify.py` (заголовок)
- Test: `tests/test_orchestration.py` (добавить тест), `tests/test_excel_segments.py` (новый)

**Interfaces:**
- Consumes: `web_signals.refresh_signals/load_cache` (Task 8), `cross_base.env_paths/load_exclusions` (Task 9), `build_outreach_exports` (Task 10), `segment_title`/`SEGMENTS` (Task 2).
- Produces: `main._build_outreach(config, output_dir: Path, master_csv: str, summary: dict, failures: list[str]) -> dict` (async); в `run_summary.json` появляются `web_signals` и `segments`.

- [ ] **Step 1: Write the failing tests**

Добавить в конец `tests/test_orchestration.py`:

```python
def test_build_outreach_skips_signals_in_dry_run_and_reports_missing_exclusion_file(monkeypatch, tmp_path):
    _clean_config(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("EXCLUDE_MASTERS", str(tmp_path / "missing.csv"))
    config = main.RunConfig.from_env()
    master = tmp_path / "master_all.csv"
    with master.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=main.storage.FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerow({
            "entity_id": "e1", "name": "Окна Юг", "city": "Ялта",
            "client_type": "stroitelstvo", "email": "info@okna-yug.ru",
            "confidence": "0.95",
        })
    summary = {"warnings": []}
    failures = []

    outreach = asyncio.run(main._build_outreach(config, tmp_path, str(master), summary, failures))

    assert failures == []
    assert outreach["ready_rows"] == 1
    assert summary["web_signals"] == "skipped"
    assert summary["segments"]["ready_by_segment"] == {"stroitelstvo": 1}
    assert any("missing.csv" in warning for warning in summary["warnings"])
```

`tests/test_excel_segments.py`:

```python
import csv

from openpyxl import load_workbook

from utils import excel_export
from utils.storage import FIELDS


def test_master_xlsx_shows_segment_title(tmp_path):
    source = tmp_path / "master_all.csv"
    with source.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerow({
            "city": "Ялта", "name": "Окна Юг", "client_type": "stroitelstvo",
            "email": "info@okna-yug.ru", "phone": "+7 (978) 111-22-33",
        })

    path = excel_export.build_xlsx(str(source), str(tmp_path / "master_all.xlsx"))

    sheet = load_workbook(path)["Крым"]
    assert sheet["C1"].value == "Сегмент"
    assert sheet["C2"].value == "Строительство и ремонт"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_orchestration.py tests/test_excel_segments.py`
Expected: FAIL — `AttributeError: module 'main' has no attribute '_build_outreach'` и `'Тип клиента' != 'Сегмент'`.

- [ ] **Step 3: Write implementation**

`main.py` — добавить функцию перед `async def _pipeline(`:

```python
async def _build_outreach(
    config: RunConfig,
    output_dir: Path,
    master_csv: str,
    summary: dict[str, Any],
    failures: list[str],
) -> dict[str, Any]:
    """Web signals (bounded) → cross-base exclusions → outreach artifacts."""
    from utils import cross_base, web_signals
    from utils.outreach_export import build_outreach_exports

    signals_path = output_dir / "web_signals.json"
    if config.skip_enrichment:
        summary["web_signals"] = "skipped"
    else:
        progress.mark_stage("web_signals")
        try:
            summary["web_signals"] = await web_signals.refresh_signals(
                master_csv,
                str(signals_path),
                max_sites=_env_int("ENRICH_MAX_SITES", 400),
            )
        except Exception as exc:
            failures.append(f"web_signals: {_redact(str(exc))[:300]}")

    emails, domains, warnings = cross_base.load_exclusions(cross_base.env_paths())
    summary["warnings"].extend(warnings)
    outreach = build_outreach_exports(
        master_csv,
        str(output_dir),
        run_id=config.run_id,
        signals_cache=web_signals.load_cache(str(signals_path)),
        other_base=(emails, domains),
    )
    summary["segments"] = {
        "ready_by_segment": outreach["by_segment"],
        "ready_by_signal": outreach["by_signal"],
    }
    return outreach
```

В `_pipeline` заменить блок

```python
            from utils.outreach_export import build_outreach_exports

            outreach = build_outreach_exports(
                master_csv,
                str(output_dir),
                run_id=config.run_id,
            )
```

на

```python
            outreach = await _build_outreach(
                config, output_dir, master_csv, summary, failures
            )
```

Брендинг и idempotency-префиксы:

```bash
sed -i 's/HORECA CRIMEA PARSER/B2B CRIMEA PARSER/; s/horeca-outreach-v1/b2b-outreach-v1/; s/horeca-quarantine-v1/b2b-quarantine-v1/; s/horeca-master-v1/b2b-master-v1/' main.py
grep -n "horeca" -i main.py
```
Expected от grep: пусто.

`utils/excel_export.py`:
1. Импорты: `from itertools import cycle` и `from config.segments import SEGMENTS, segment_title`.
2. В `HEADERS` заменить `"Тип клиента"` на `"Сегмент"`.
3. Заменить весь словарь `FILL_BY_TYPE` на:

```python
# Заливка строки по сегменту (client_type)
_PALETTE = (
    "DCEEFB", "FEF3C7", "FFF7ED", "E2D9F3", "FFE4E6",
    "DCFCE7", "F3F4F6", "E0F2FE", "FCE7F3", "ECFCCB",
)
FILL_BY_TYPE = {segment.key: color for segment, color in zip(SEGMENTS, cycle(_PALETTE))}
FILL_BY_TYPE["прочее"] = "FFFFFF"
```

4. В `_write_sheet` сразу после `val = row.get(csv_field, "") or ""` добавить:

```python
            if csv_field == "client_type":
                val = segment_title(val)
```

5. В словаре `widths` заменить `"Тип клиента": 14` на `"Сегмент": 26`.
6. В `_write_summary` заменить `type_cnt = Counter(r.get("client_type", "—") for r in rows)` на `type_cnt = Counter(segment_title(r.get("client_type")) for r in rows)`.

`utils/telegram_notify.py`: `title = "HORECA Crimea Parser — отчёт"` → `title = "B2B Crimea Parser — отчёт"`; `"----HorecaParserBoundary7c3"` → `"----B2BParserBoundary7c3"`.

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q`
Expected: все PASS, кроме baseline-исключений из Task 1.

- [ ] **Step 5: Commit**

```bash
git add main.py utils/excel_export.py utils/telegram_notify.py tests/test_orchestration.py tests/test_excel_segments.py
git commit -q -m "feat: wire web signals, cross-base exclusion and segments into pipeline

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Deploy-юниты, `.env.example`, документация

**Files:**
- Rename + Modify: `deploy/horeca_parser.service` → `deploy/b2b_parser.service`, `deploy/horeca_parser.timer` → `deploy/b2b_parser.timer`, `deploy/horeca-parser-chromium.apparmor` → `deploy/b2b-parser-chromium.apparmor`
- Modify: `tests/test_deploy_hardening.py`, `tests/conftest.py`, `parsers/site_finder.py` (docstring/UA), `.env.example`
- Modify: `README.md` (полная переработка), `docs/RUNBOOK.md` (замена имён)
- Delete: `docs/AUTO_EMAIL_INTEGRATION.md`
- Replace: `docs/PROJECT_STATUS.md`

**Interfaces:**
- Consumes: всё выше.
- Produces: деплой-артефакты для `/home/b2b_parser`, документация.

- [ ] **Step 1: Обновить тест деплоя (failing)**

```bash
sed -i 's/horeca_parser/b2b_parser/g; s/horeca-parser/b2b-parser/g' tests/test_deploy_hardening.py
```
Добавить в `tests/test_deploy_hardening.py`:

```python
def test_timer_runs_friday_and_does_not_collide():
    timer = (ROOT / "deploy" / "b2b_parser.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Fri *-*-* 03:00:00" in timer
```

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q tests/test_deploy_hardening.py`
Expected: FAIL — `FileNotFoundError: deploy/b2b_parser.service`.

- [ ] **Step 2: Переименовать и переписать юниты**

```bash
git mv deploy/horeca_parser.service deploy/b2b_parser.service
git mv deploy/horeca_parser.timer deploy/b2b_parser.timer
git mv deploy/horeca-parser-chromium.apparmor deploy/b2b-parser-chromium.apparmor
sed -i 's/horeca_parser/b2b_parser/g; s/horeca-parser/b2b-parser/g; s/HORECA Crimea Parser/B2B Crimea Parser/g' deploy/*
```

`deploy/b2b_parser.timer` (полная замена):

```ini
# /etc/systemd/system/b2b_parser.timer
# Еженедельный запуск ПТ 03:00 MSK.
# На том же VPS работают hotels_sbor_baza (ВС 03:00) и horeca_parser
# (СБ 03:00): пятница выбрана, чтобы Chromium-процессы не пересекались (OOM).
[Unit]
Description=Weekly B2B parser run (Fri 03:00)

[Timer]
OnCalendar=Fri *-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Проверка: `grep -rn "horeca" -i deploy/` → пусто; в `b2b_parser.service` остаются `MemoryMax=3G` и `RuntimeMaxSec=12h` (или эквивалент из исходного юнита; если значения другие — привести к спецификации).

- [ ] **Step 3: Прочие упоминания HoReCa в коде**

```bash
sed -i 's/HORECA_parsing/Parser_B2B/' tests/conftest.py
sed -i 's/HoReCa venue/company/; s/HorecaSiteFinder/B2BSiteFinder/' parsers/site_finder.py
sed -i 's/HoReCa observations/B2B observations/; s/^HoReCa\.$/B2B companies./' utils/entity_resolution.py
grep -rn "horeca" -i --include=*.py . | grep -v "^./.venv/\|test_env_loader"
```
Expected: пусто. Имена переменных `HORECA_TEST_*` в `test_env_loader.py` оставить: это тестовые ключи окружения.

- [ ] **Step 4: `.env.example`**

Правки:
- VK_TOKEN: убрать «тот же токен, что для hotels_sbor_baza» → «можно тот же токен, что для HoReCa/hotels».
- TG: «заведи ОТДЕЛЬНОГО бота под B2B-проект».
- Комментарий смещений Яндекса: `39 городов × 60 шаблонов. Диапазоны: city 0..38, query 0..59.`
- Добавить в конец:

```bash
# Исключение компаний из других баз (HoReCa, отели): пути к их master CSV
# через ';'. Совпадение по email или корпоративному домену → в outreach
# не попадает (причина already_in_other_base). Пусто — проверка выключена.
EXCLUDE_MASTERS=

# 1 — включать сегмент «IT и связь» (конкуренты/партнёры) в outreach_ready.
OUTREACH_INCLUDE_COMPETITORS=0
```

- [ ] **Step 5: Документация**

```bash
git rm -q docs/AUTO_EMAIL_INTEGRATION.md
sed -i 's/HORECA Parser/B2B Parser/g; s/horeca_parser/b2b_parser/g; s/horeca-parser/b2b-parser/g' docs/RUNBOOK.md
```

`docs/PROJECT_STATUS.md` (полная замена):

```markdown
# B2B Parser — статус проекта

Актуально на 24 сентября 2026 года.

## Готово (код)

- Форк horeca_parser 89ced6b; ядро без изменения логики.
- 20 сегментов в `config/segments.py`; OSM, VK, Яндекс и crawler читают
  таксономию только оттуда.
- Исключения: сети, госорганы, HoReCa/отели (флаги `excluded_*`),
  пересечение с другими базами через `EXCLUDE_MASTERS`.
- Сигналы «повода для КП» с кэшем `output/web_signals.json`.
- `outreach_ready.xlsx` с колонками Сегмент/Все сегменты/Повод для КП/Сигналы;
  `approved_for_send=false` всегда.

## Осталось

1. Локальные DRY_RUN и canary OSM/VK/Яндекс, ручной аудит 50 строк outreach
   (критерий: ≥ 90% — реальные крымские компании с верным сегментом).
2. Деплой на VPS в `/home/b2b_parser`, таймер ПТ 03:00 — disabled до приёмки.
3. Пакетный прогон Яндекса (60 шаблонов × 39 городов) через смещения.
4. Рассылка — вне рамок; только после отдельного одобрения.
```

`README.md` — переписать шапку и разделы «Источники», «Категории», «Деплой» под B2B, сохранив разделы «Установка», «Запуск», «Управление enrichment», «Тесты» (с заменой `horeca_parser` → `b2b_parser`). Обязательные новые разделы:

```markdown
# B2B Crimea Parser

Сбор контактной базы коммерческих компаний Крыма для B2B-предложений
(разработка сайтов, ботов, CRM). Каждая компания получает сегмент, город и
эвристический «повод для КП». Форк horeca_parser: та же архитектура
«оркестратор + независимые источники + conservative entity resolution +
enrichment + approval-gated handoff».

## Сегменты

Единственный источник таксономии — `config/segments.py` (20 сегментов:
строительство, недвижимость, авто, медицина, красота, фитнес, образование,
юристы/бухгалтерия, туризм, торговля, производство, логистика, event,
мебель, ветеринария, клининг/бытовые услуги, агро/вино, IT и связь
(конкуренты, не в outreach по умолчанию), финансы, реклама).
Добавить сегмент = одна запись `Segment(...)`.

## Исключения

- `excluded_chain` — федеральные сети/бренды (`EXCLUDE_BRANDS`, почтовые домены сетей);
- `excluded_gov` — госорганы, ГБУ/МБУ, банкоматы;
- `excluded_other_base_type` — HoReCa и размещение (есть в других базах);
- `already_in_other_base` — email/домен найден в `EXCLUDE_MASTERS`.

## Повод для КП

`no_website` > `site_dead` > `no_https` > `no_mobile` > `no_online_booking`
> `site_builder` > `outdated`; без сигналов — «Автоматизация/боты/CRM».
Кэш: `output/web_signals.json`, пересчёт не чаще раза в 30 дней, бюджет —
`ENRICH_MAX_SITES`.
```

- [ ] **Step 6: Run tests**

Run: `.venv/Scripts/python -m pytest -p no:cacheprovider -q`
Expected: все PASS, кроме baseline-исключений.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -q -m "chore: B2B deploy units (Fri timer), env example and docs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Локальная приёмка (dry-run и canary)

Требует сети. Для VK нужен `VK_TOKEN` в `.env`: его вносит пользователь, агент токены не вводит.

**Files:**
- Create: `.env` (локально, не коммитится; заполняет пользователь)

**Interfaces:**
- Consumes: весь пайплайн.
- Produces: отчёт приёмки в `docs/PROJECT_STATUS.md`.

- [ ] **Step 1: Chromium для Playwright**

Run: `.venv/Scripts/python -m playwright install chromium`
Expected: установка завершена.

- [ ] **Step 2: DRY_RUN OSM**

Run: `DRY_RUN=1 ONLY_SOURCE=osm .venv/Scripts/python main.py; echo "exit=$?"`
Expected: `exit=0`; в `output/dry_runs/<run_id>/output/` есть `master_all.csv`, `outreach_ready.xlsx`, `run_summary.json` с блоком `segments`; `approved_for_send=false` в `handoff/latest.json`.

- [ ] **Step 3: Canary OSM без лимита записей, с сигналами**

Run: `DRY_RUN=1 DRY_RUN_ID=osm-full DRY_RUN_ENRICHMENT=1 MAX_ITEMS_PER_SOURCE=0 ENRICH_MAX_SITES=30 ONLY_SOURCE=osm .venv/Scripts/python main.py; echo "exit=$?"`
Expected: `exit=0`; в `run_summary.json` есть `web_signals.checked` ≤ 30; распределение `ready_by_segment` покрывает несколько сегментов. Если DRY_RUN не позволяет enrichment через `DRY_RUN_ENRICHMENT`, выполнить с `SKIP_ENRICHMENT=0` и зафиксировать фактический флаг в отчёте.

- [ ] **Step 4: Canary VK (1 город, 3 запроса)**

Run: `DRY_RUN=1 DRY_RUN_ID=vk-canary ONLY_SOURCE=vk MAX_CITIES=1 MAX_QUERIES_PER_SOURCE=3 MAX_ITEMS_PER_SOURCE=100 .venv/Scripts/python main.py; echo "exit=$?"`
Expected: `exit=0`; шумовые сообщества находятся в `master_quarantine.csv`, а не в outreach.

- [ ] **Step 5: Canary Яндекс (1 город, 2 шаблона)**

Run: `DRY_RUN=1 DRY_RUN_ID=ya-canary ONLY_SOURCE=yandex MAX_CITIES=1 MAX_QUERIES_PER_SOURCE=2 MAX_ITEMS_PER_SOURCE=10 .venv/Scripts/python main.py; echo "exit=$?"`
Expected: `exit=0`, 10 карточек с уникальными org ID; при CAPTCHA — ненулевой exit с `yandex_captcha` (это корректное поведение, не баг).

- [ ] **Step 6: Ручной аудит**

Взять 50 случайных строк из `outreach_ready.csv` всех canary и проверить: реальная коммерческая компания Крыма, верный сегмент, email похож на рабочий. Записать долю ≥ 90% (или список проблем) в `docs/PROJECT_STATUS.md`. Если доля < 90%, классифицируйте ошибки (сегмент, исключения, шум) и вернитесь к соответствующей задаче. До повторной приёмки дальше не идти.

- [ ] **Step 7: Commit**

```bash
git add docs/PROJECT_STATUS.md
git commit -q -m "docs: local canary acceptance results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Деплой на VPS (с участием пользователя)

Внешнее и труднообратимое действие. Каждый шаг на сервере выполняется только после явного «да» пользователя в чате. SSH-доступ, `.env` и `token.json` пользователь предоставляет сам. Агент не вводит пароли и токены.

- [ ] **Step 1: Создать удалённый репозиторий и запушить** — только по подтверждению пользователя (имя репозитория, приватность).
- [ ] **Step 2: На VPS** — по `docs/RUNBOOK.md`: пользователь `b2b-parser`, `/home/b2b_parser`, `python3 -m venv venv && venv/bin/pip install -r requirements.lock`, Chromium под `b2b-parser`, AppArmor-профиль `deploy/b2b-parser-chromium.apparmor`, юниты в `/etc/systemd/system/`, `systemctl daemon-reload`. Таймер **не включать**.
- [ ] **Step 3: Серверный canary** — `systemctl start --no-block b2b_parser.service` с `DRY_RUN=1 ONLY_SOURCE=osm` (через drop-in или override), проверить `journalctl -u b2b_parser.service`, exit 0, пик RAM.
- [ ] **Step 4: Пакетный Яндекс** — по пакетам `YANDEX_CITY_OFFSET`/`YANDEX_QUERY_OFFSET`, без unlimited-режима.
- [ ] **Step 5: Решение о таймере** — `systemctl enable --now b2b_parser.timer` только после письменного одобрения пользователя.
