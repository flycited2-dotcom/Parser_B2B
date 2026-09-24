"""Список доменов-агрегаторов — используется crawler.py, чтобы не брать
в seed-домены геосервисы, соцсети, доски отзывов и площадки доставки еды
(это не сайты самих заведений).
"""
from urllib.parse import urlparse

AGGREGATOR_DOMAINS = {
    # Поисковики и соцсети
    "yandex.ru", "ya.ru", "yandex.com", "yandex.eu", "yastatic.net",
    "google.com", "google.ru", "bing.com", "duckduckgo.com",
    "wikipedia.org", "youtube.com", "vk.com", "ok.ru",
    "instagram.com", "facebook.com", "t.me", "telegram.org",
    "rutube.ru", "dzen.ru", "rambler.ru", "mail.ru",
    # Геокаталоги и отзывы
    "2gis.ru", "2gis.com", "zoon.ru", "yell.ru", "flamp.ru",
    "irecommend.ru", "otzyv.ru", "otzovik.com", "tripadvisor.ru",
    "tripadvisor.com", "restoran.ru", "tomesto.ru", "afisha.ru",
    "kudago.com", "tonkosti.ru",
    # Доставка еды / бронирование столиков
    "eda.yandex.ru", "delivery-club.ru", "restoclub.ru", "menu.ru",
    # Классифайды
    "avito.ru", "youla.ru",
    # Прочее
    "drom.ru", "domclick.ru", "cian.ru",
}


def is_aggregator(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        for agg in AGGREGATOR_DOMAINS:
            if host == agg or host.endswith("." + agg):
                return True
        return False
    except Exception:
        return True
