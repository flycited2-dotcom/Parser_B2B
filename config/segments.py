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


# --- Исключения -----------------------------------------------------------
# Сети и федеральные бренды: филиалы не покупают разработку локально.
EXCLUDE_BRANDS: tuple[str, ...] = (
    "магнит", "пятерочка", "перекресток", "ашан", "пуд", "fix price",
    "фикс прайс", "мтс", "мегафон", "билайн", "tele2", "теле2",
    "win mobile", "волна мобайл", "сбербанк", "сбер", "рнкб", "генбанк",
    "втб", "почта банк", "dns", "эльдорадо", "м.видео", "мвидео",
    "спортмастер", "wildberries", "вайлдберриз", "ozon", "озон",
    "яндекс маркет", "сдэк", "cdek", "boxberry", "деловые линии", "пэк",
    "gloria jeans", "playtoday", "street beat", "585 золотой", "технониколь",
    "этм", "главдоставка", "кий авиа", "самолет плюс", "дятьково", "аско",
)
# Бренды, чьи дочерние названия пишутся слитно («СберЛизинг», «СберЗдоровье»).
EXCLUDE_BRAND_PREFIXES: tuple[str, ...] = ("сбер",)
EXCLUDE_EMAIL_DOMAINS: tuple[str, ...] = (
    "magnit.ru", "x5.ru", "mts.ru", "megafon.ru", "beeline.ru", "tele2.ru",
    "sberbank.ru", "sber.ru", "rncb.ru", "genbank.ru", "vtb.ru",
    "pochtabank.ru", "dns-shop.ru", "eldorado.ru", "mvideo.ru",
    "sportmaster.ru", "fix-price.com", "wildberries.ru", "ozon.ru",
    "cdek.ru", "boxberry.ru", "dellin.ru", "pecom.ru",
)
EXCLUDED_FLAGS = frozenset({"excluded_chain", "excluded_gov", "excluded_other_base_type"})

_BRAND_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(fold(brand)) for brand in EXCLUDE_BRANDS)
    + r")(?!\w)|(?<!\w)(?:"
    + "|".join(re.escape(fold(prefix)) for prefix in EXCLUDE_BRAND_PREFIXES)
    + r")\w*"
)
_GOV_RE = re.compile(
    r"\b(?:администраци\w*|мфц|госуслуг\w*|прокуратур\w*|полици\w*|мвд|"
    r"министерств\w*|росреестр\w*|налогов\w+ инспекци\w*|пенсионн\w+ фонд\w*|"
    r"социальн\w+ фонд\w*|гбу\w*|мбу\w*|гбоу|мбоу|мбдоу|гбдоу|гауз|муп|гуп|"
    r"фгуп|фгбу\w*|суд|банкомат\w*|платежн\w+ терминал\w*|почта россии|"
    r"отделение почтов\w+ связи)\b"
    # Аббревиатуры учреждений (ГАУЗ, ГБПОУ, ФГАОУ, МКУ, МАДОУ…), в т.ч.
    # со слитным региональным суффиксом («ГАУЗРК», «ГБУЗС»).
    r"|\b(?:ф?г[абк]у[пз]?|гупс?|фгуп|ф?г[аб][пд]?оу|м[абк]у(?:до?)?|м[абк]д?оу|муп)"
    r"(?:рк|с)?\b"
    # Государственные школы, поликлиники, больницы (spec §3.2): только
    # с номером или квалификатором, чтобы не цеплять частные клиники.
    r"|\b(?:поликлиник\w*\s*№|больниц\w*\s*№|школ\w*\s*№|лице\w*\s*№|"
    r"(?:городск|районн|республиканск|центральн|детск)\w*\s+(?:больниц|поликлиник)\w*|"
    r"детск\w*\s+школ\w*\s+искусств|дши|дмш|дхш|фап|фельдшерск\w*|гимнази\w*|"
    r"(?:государственн|муниципальн)\w*\s+(?:бюджетн|автономн|казенн|унитарн)\w*|"
    r"дворец\w*\s+(?:культуры|пионеров|спорта|водных))"
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
