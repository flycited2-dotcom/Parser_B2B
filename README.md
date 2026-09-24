# HORECA Crimea Parser

Сбор базы **объектов общественного питания** Крыма: рестораны, кафе,
фастфуд, бары, пабы, клубы, кофейни, столовые, фудкорты, пиццерии,
кондитерские и пекарни. Отели, базы отдыха и пансионаты не являются целевой
категорией этого проекта.

## Источники (v1)

| # | Источник | Тип | Файл | Комментарий |
|---|---|---|---|---|
| 1 | OSM Overpass | HTTP/JSON | `parsers/osm.py` | Amenity общепита плюс bakery/confectionery/pastry/coffee; результат проверяется локальной границей полуострова. Без токена. |
| 2 | VK Groups | HTTP/JSON | `parsers/vk_groups.py` | Поиск групп по городам × ключевым словам. Нужен `VK_TOKEN`; сомнительные совпадения сохраняются с confidence/quality flags для ручной проверки. |
| 3 | Я.Карты | Chromium (Playwright) | `parsers/yandex_maps.py` | Поиск по городам × категориям, парсинг сниппетов + карточки организации. |
| 4 | Crawler | aiohttp | `parsers/crawler.py` | Обходит сайты уже найденных заведений (sitemap + ссылки), ищет соседние объекты и добирает контакты. |

**Добор контактов** (после сбора, `parsers/email_finder.py`): обход сайта
(mailto/JSON-LD/контактные страницы/обфускация), `site_finder.py` (поиск
сайта через DuckDuckGo для записей без сайта), `vk_email.py` (email с
публичной VK-страницы).

Каждый источник сохраняется как отдельное наблюдение. Консервативный entity
resolution объединяет записи только по сильным признакам (source ID, телефон,
домен, адрес+название или близкие координаты); одного `название+город`
недостаточно. Предпочтительные и альтернативные телефоны, email, сайты и
соцсети сохраняются вместе с provenance.

### Источники hotels_sbor_baza, НЕ перенесённые в v1

| Источник | Почему не перенесён |
|---|---|
| Wikidata / Wikipedia | Слабое покрытие ресторанов/кафе — в основном только сетевые/исторические. Инфраструктура генерик, можно добавить при необходимости. |
| Госреестр Минэка | Реестр **средств размещения** — не применим к общепиту. |
| 2ГИС | В hotels_sbor_baza блокирует Крым для IP датацентра (403). Тот же баг ожидаем и здесь — не переносили первым, при необходимости портируется по образцу `twogis.py`. |
| Авито / Суточно.ру / Ostrovok | Площадки бронирования жилья — не про общепит. |

Добавить новый источник = один файл в `parsers/` с сигнатурой
`async def run(context)` + одна строка в `RUNNERS` в `main.py`.

## Категории (`utils/categories.py`)

`ресторан`, `кафе`, `фастфуд`, `бар`, `паб`, `клуб`, `кофейня`, `столовая`,
`фудкорт`, `пиццерия`, `кондитерская`, `прочее`.

## Установка

```bash
# Для разработки
pip install -r requirements-dev.txt

# Для воспроизводимого VPS-деплоя
pip install -r requirements.lock
playwright install chromium
cp .env.example .env   # заполнить VK_TOKEN / TG_BOT_TOKEN / TG_CHAT_ID / GDRIVE_*
```

## Запуск

```bash
# Безопасная первая проверка: только OSM, отдельный output, без TG/Drive,
# максимум 25 записей и без enrichment.
DRY_RUN=1 ONLY_SOURCE=osm python main.py

# Все источники по очереди + email_finder + XLSX + Telegram + Drive
python main.py

# Только один источник (без Chromium/токенов не всё сработает)
ONLY_SOURCE=osm python main.py

# Без добора email/сайтов (быстрее для проверки одного источника)
SKIP_ENRICHMENT=1 ONLY_SOURCE=osm python main.py
```

Shell/systemd environment имеет приоритет над dotenv-файлами. Среди файлов
поздний файл перекрывает ранний: `.env` → `.env.tg` → `.env.vk` →
`.env.gdrive` → прочие `.env.*` → `.env.local`.

`DRY_RUN=1` всё ещё обращается к выбранному источнику, но гарантированно
отключает Telegram/Drive и изолирует файлы в
`output/dry_runs/<run_id>/output`. Значения по умолчанию для dry-run:
`MAX_SOURCES=1`, `MAX_CITIES=1`, `MAX_QUERIES_PER_SOURCE=1`,
`MAX_ITEMS_PER_SOURCE=25`, `SKIP_ENRICHMENT=1`.

Оркестратор завершает процесс ненулевым кодом при ошибке источника,
нарушении `MIN_RECORDS_*`, пустом master или ошибке обязательного artifact.
Подробный машинно-читаемый итог — `output/run_summary.json`.

### Управление enrichment (важно для времени прогона)

Добор контактов по сайтам — самая долгая стадия (в hotels_sbor_baza полный
обход 12K сайтов занимал ~14 суток и его убивал systemd-таймаут). Поэтому:

- `ENRICH_MAX_SITES` (default **400**) — максимум сайтов за прогон;
  остальные дойдут в следующих прогонах (persistent-накопление в master).
- `SITE_FINDER` (default **выключен**) — поиск сайта через DuckDuckGo для
  записей без website. Включать точечно: тысячи DDG-запросов подряд
  приводят к rate-limit.

Результат: `output/result_<timestamp>.csv`, после `cross_source_merge` и
email_finder — обогащённый result. Накопленные `master_all.csv/.xlsx`
строятся **всегда**, даже без Google Drive. При заданных
`TG_BOT_TOKEN`/`TG_CHAT_ID` master уходит в Telegram; при
`GDRIVE_FOLDER_ID` — также в Google Drive. Ошибочный частичный результат по
умолчанию никуда не доставляется.

Дополнительно строятся:

- `master_quarantine.csv` — широкие самостоятельные VK-кандидаты без
  первичного food-сигнала; они сохранены для аудита, но не питают crawler,
  outreach и автоматизацию. При сильном сопоставлении слабая VK-запись может
  заполнить только отсутствующий контакт, никогда не заменяет контакт
  OSM/Яндекс и всегда переводит объект в ручную проверку. Слабый VK-сайт не
  может стать seed для crawler;
- `outreach_ready.xlsx/.csv` — совместимый с `Email_horeca_send` список
  только целевых типов с валидным email и пройденным quality gate;
- `outreach_review.csv` — всё исключённое с причиной;
- `output/handoff/latest.json` — schema v3, checksums, idempotency keys и
  неизменяемые копии master, outreach и quarantine. Значение
  `approved_for_send` всегда `false`.

Общий контракт ограничений: `MAX_SOURCES`, `MAX_CITIES`,
`MAX_QUERIES_PER_SOURCE`, `MAX_ITEMS_PER_SOURCE`; `0` означает без лимита.
Пороги качества: `MIN_RECORDS_TOTAL` и `MIN_RECORDS_OSM/VK/YANDEX/CRAWLER`.
Критичные источники перечисляются через `CRITICAL_SOURCES`.

Яндекс дополнительно ограничен `YANDEX_MAX_DETAIL_REQUESTS=600` по умолчанию,
кэширует повторные `org_id` внутри запуска и прекращает источник при
CAPTCHA/серии ошибок. Для последовательного покрытия матрицы используйте
`YANDEX_CITY_OFFSET` и `YANDEX_QUERY_OFFSET`; безопасный пример и порядок
пакетов приведены в [RUNBOOK](docs/RUNBOOK.md).

## Тесты

```bash
python -m pytest -p no:cacheprovider
```

## Эксплуатационные документы

- [Статус и приоритеты](docs/PROJECT_STATUS.md)
- [Runbook первого запуска и восстановления](docs/RUNBOOK.md)
- [Контракт будущей email-интеграции](docs/AUTO_EMAIL_INTEGRATION.md)

## Деплой на сервер (заготовка)

`deploy/horeca_parser.service` + `deploy/horeca_parser.timer` — systemd-юниты
для еженедельного прогона (СБ 03:00 MSK; суббота — чтобы не пересекаться с
hotels-парсером, у которого ВС 03:00, если оба на одном VPS с 5.8 ГБ RAM).

```bash
# На сервере (пример для /home/horeca_parser): сначала создать системного
# пользователя horeca-parser и назначить ему output/cache/secrets.
python3 -m venv venv && venv/bin/pip install -r requirements.lock
sudo -u horeca-parser env HOME=/home/horeca_parser \
  PLAYWRIGHT_BROWSERS_PATH=/home/horeca_parser/.cache/ms-playwright \
  venv/bin/playwright install chromium
chown -R root:horeca-parser /home/horeca_parser/.cache
# Ubuntu 23.10+: установить deploy/horeca-parser-chromium.apparmor в
# /etc/apparmor.d/ и reload AppArmor (не использовать --no-sandbox).
cp deploy/horeca_parser.* /etc/systemd/system/
systemctl daemon-reload
# timer включать только после canary и полного ручного прогона:
# systemctl enable --now horeca_parser.timer
# Ручной запуск — ВСЕГДА с --no-block (oneshot блокируется на часы):
systemctl start --no-block horeca_parser.service
journalctl -u horeca_parser.service -f
```

## Расширение на другие категории объектов (на будущее)

Архитектура не завязана на HoReCa — паттерн «оркестратор (`main.py`
`RUNNERS`) + независимые модули-источники + общий storage с дедупом +
email/site-enrichment» переносится на любую категорию объектов (например,
коммерческие организации/магазины с доп. видами деятельности). Чтобы
добавить новую категорию:

1. Завести новый список тегов/ключевых слов (аналог `CATEGORY_MAP` в
   `parsers/osm.py`, `QUERIES` в `parsers/vk_groups.py`/`yandex_maps.py`,
   `HORECA_TRIGGERS` в `parsers/crawler.py`).
2. Обновить `utils/categories.py` (CANONICAL/ALIASES) под новую таксономию.
3. `utils/geo_city.py`, `utils/storage.py`, `utils/dedup.py`,
   `utils/telegram_notify.py`, `utils/gdrive.py`, `utils/merger.py`,
   `parsers/email_finder.py`, `site_finder.py`, `vk_email.py` — category-
   agnostic, менять не нужно.

Это отдельная будущая задача, не входит в v1.

## Следующий шаг пайплайна (не в этом репозитории)

Собранные данные (Telegram/Google Drive) — вход для следующего этапа:
агент может забрать атомарный manifest из `output/handoff/` и сформировать
черновик. Автоматическая email-рассылка **запрещена до отдельного ручного
одобрения**. Manifest всегда создаётся с `approved_for_send=false`; полный
контракт описан в `docs/AUTO_EMAIL_INTEGRATION.md`.
