# B2B Crimea Parser

Сбор контактной базы коммерческих компаний Крыма для B2B-предложений
(разработка сайтов, ботов, CRM). Каждая компания получает сегмент, город и
эвристический «повод для КП». Форк horeca_parser: та же архитектура
«оркестратор + независимые источники + conservative entity resolution +
enrichment + approval-gated handoff».

## Источники (v1)

| # | Источник | Тип | Файл | Комментарий |
|---|---|---|---|---|
| 1 | OSM Overpass | HTTP/JSON | `parsers/osm.py` | Теги shop/office/craft/amenity из `config/segments.py`; результат проверяется локальной границей полуострова. Без токена. |
| 2 | VK Groups | HTTP/JSON | `parsers/vk_groups.py` | Поиск групп по городам × ключевым словам сегментов. Нужен `VK_TOKEN`; группы без сегментного сигнала и «шум» (барахолки, паблики) уходят в карантин. |
| 3 | Я.Карты | Chromium (Playwright) | `parsers/yandex_maps.py` | 39 городов × 60 шаблонов запросов, только пакетами; парсинг сниппетов + карточки организации. |
| 4 | Crawler | aiohttp | `parsers/crawler.py` | Обходит сайты уже найденных компаний (sitemap + ссылки), ищет соседние объекты и добирает контакты. |

**Добор контактов** (после сбора, `parsers/email_finder.py`): обход сайта
(mailto/JSON-LD/контактные страницы/обфускация), `site_finder.py` (поиск
сайта через DuckDuckGo для записей без сайта), `vk_email.py` (email с
публичной VK-страницы).

Каждый источник сохраняется как отдельное наблюдение. Консервативный entity
resolution объединяет записи только по сильным признакам (source ID, телефон,
домен, адрес+название или близкие координаты); одного `название+город`
недостаточно. Предпочтительные и альтернативные телефоны, email, сайты и
соцсети сохраняются вместе с provenance.

Добавить новый источник = один файл в `parsers/` с сигнатурой
`async def run(context)` + одна строка в `RUNNERS` в `main.py`.

## Сегменты

Единственный источник таксономии — `config/segments.py` (20 сегментов:
строительство, недвижимость, авто, медицина, красота, фитнес, образование,
юристы/бухгалтерия, туризм, торговля, производство, логистика, event,
мебель, ветеринария, клининг/бытовые услуги, агро/вино, IT и связь
(конкуренты, не в outreach по умолчанию), финансы, реклама).
Добавить сегмент = одна запись `Segment(...)`: OSM-теги, запросы Яндекса,
ключевые слова VK и синонимы для распознавания в тексте.

Сегмент хранится в поле `client_type` master как ключ (`avto`,
`stroitelstvo`, …); в Excel выводится его название.

## Исключения

- `excluded_chain` — федеральные сети/бренды (`EXCLUDE_BRANDS`, почтовые домены сетей);
- `excluded_gov` — госорганы, ГБУ/МБУ, банкоматы;
- `excluded_other_base_type` — HoReCa и размещение (есть в других базах);
- `already_in_other_base` — email/домен найден в `EXCLUDE_MASTERS`.

Исключённые компании остаются в master для аудита, но не попадают в
`outreach_ready`; причина видна в `outreach_review.csv`.

## Повод для КП

`no_website` > `site_dead` > `no_https` > `no_mobile` > `no_online_booking`
> `site_builder` > `outdated`; без сигналов — «Автоматизация/боты/CRM».
Кэш: `output/web_signals.json`, пересчёт не чаще раза в 30 дней, бюджет —
`ENRICH_MAX_SITES`. Соцсеть в поле «сайт» считается `no_website`.
Сигналы — эвристика: подсказка для текста письма, а не утверждение.

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

# Все источники по очереди + email_finder + сигналы + XLSX + Telegram + Drive
python main.py

# Только один источник (без Chromium/токенов не всё сработает)
ONLY_SOURCE=osm python main.py

# Без добора email/сайтов и сигналов (быстрее для проверки одного источника)
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
Подробный машинно-читаемый итог — `output/run_summary.json` (включая
разбивку outreach по сегментам и сигналам).

### Управление enrichment (важно для времени прогона)

Добор контактов по сайтам — самая долгая стадия. Поэтому:

- `ENRICH_MAX_SITES` (default **400**) — максимум сайтов за прогон для
  email_finder и отдельно для проверки сигналов; остальные дойдут в
  следующих прогонах.
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
  сегментного сигнала; сохранены для аудита, но не питают crawler, outreach
  и автоматизацию;
- `outreach_ready.xlsx/.csv` — один email на строку: Сегмент, Все сегменты,
  Город, контакты, Повод для КП, Сигналы;
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
`YANDEX_CITY_OFFSET` (0..38) и `YANDEX_QUERY_OFFSET` (0..59); порядок
пакетов приведён в [RUNBOOK](docs/RUNBOOK.md).

## Тесты

```bash
python -m pytest -p no:cacheprovider
```

## Эксплуатационные документы

- [Статус и приоритеты](docs/PROJECT_STATUS.md)
- [Runbook первого запуска и восстановления](docs/RUNBOOK.md)
- [Спецификация](docs/superpowers/specs/2026-09-24-b2b-crimea-parser-design.md)

## Деплой на сервер (заготовка)

`deploy/b2b_parser.service` + `deploy/b2b_parser.timer` — systemd-юниты
для еженедельного прогона (ПТ 03:00 MSK; на том же VPS HoReCa — СБ,
отели — ВС, чтобы Chromium-процессы не пересекались).

```bash
# На сервере (/home/b2b_parser): сначала создать системного пользователя
# b2b-parser и назначить ему output/cache/secrets.
python3 -m venv venv && venv/bin/pip install -r requirements.lock
sudo -u b2b-parser env HOME=/home/b2b_parser \
  PLAYWRIGHT_BROWSERS_PATH=/home/b2b_parser/.cache/ms-playwright \
  venv/bin/playwright install chromium
chown -R root:b2b-parser /home/b2b_parser/.cache
# Ubuntu 23.10+: установить deploy/b2b-parser-chromium.apparmor в
# /etc/apparmor.d/ и reload AppArmor (не использовать --no-sandbox).
cp deploy/b2b_parser.* /etc/systemd/system/
systemctl daemon-reload
# timer включать только после canary и полного ручного прогона:
# systemctl enable --now b2b_parser.timer
# Ручной запуск — ВСЕГДА с --no-block (oneshot блокируется на часы):
systemctl start --no-block b2b_parser.service
journalctl -u b2b_parser.service -f
```

## Рассылка (не в этом репозитории)

Автоматическая email-рассылка **запрещена до отдельного ручного
одобрения** процесса, шаблона, списка получателей и механизма отписки.
Manifest всегда создаётся с `approved_for_send=false`.
