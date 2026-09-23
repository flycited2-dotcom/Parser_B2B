# B2B Crimea Parser — спецификация дизайна

Дата: 2026-09-24
Статус: утверждено в диалоге, ожидает ревью письменной спецификации
Основа: форк `horeca_parser` (remote `flycited2-dotcom/Horeca_parser`), идеи
предшественника `hotels_sbor_baza`.

## 1. Цель

Собрать контактную базу коммерческих компаний Крыма, которые публично
продвигают себя (карты, VK, сайты), чтобы отправлять им коммерческие
предложения на услуги разработки (сайты, боты, CRM, автоматизация).

Для каждой компании нужны:

- **email** (главное поле; без email запись не попадает в outreach);
- город / локация (Симферополь, Севастополь, Ялта… — по `geo_city`);
- направление деятельности (сегмент);
- телефон, сайт, соцсеть, адрес — как дополнительный контекст;
- «повод для КП» — эвристический сигнал, что компании может быть нужно.

### Критерии успеха v1

- `outreach_ready.xlsx` содержит только компании внутри границы Крыма, из
  целевых сегментов, с валидным email, без федеральных сетей, госучреждений,
  HoReCa и отелей.
- Каждая строка имеет сегмент, город и «повод для КП».
- Ручной аудит случайной выборки из 50 строк outreach: ≥ 90% — реальные
  коммерческие компании Крыма с правильным сегментом.
- Все тесты зелёные; dry-run и canary каждого источника проходят с exit 0.

### Вне рамок v1

2ГИС (блок IP датацентра), ЕГРЮЛ/Rusprofile (ИНН, выручка), интеграция с
рассыльщиком, любая реальная отправка писем.

## 2. Подход

Форк `horeca_parser` в `C:\Users\TLT-1\Documents\GitHub\Parser_B2B` как
отдельный git-репозиторий с чистой историей. Рабочий HoReCa-прод не
затрагивается. Ядро (оркестратор, storage, entity resolution, merger,
geo, safety, enrichment, handoff, TG/Drive) переносится без изменения логики;
заменяется таксономия, удаляется food-специфика.

## 3. Архитектура

```
main.py              оркестратор, RUNNERS: osm, vk, yandex, crawler
config/segments.py   единственный источник таксономии (новое)
parsers/osm.py       теги берутся из segments.py
parsers/vk_groups.py ключевые слова из segments.py; B2B quality gate
parsers/yandex_maps.py запросы из segments.py; пакетный режим обязателен
parsers/crawler.py   триггеры из segments.py вместо HORECA_TRIGGERS
parsers/email_finder.py, site_finder.py, vk_email.py — без изменений
utils/categories.py  тонкая обёртка: normalize() через aliases сегментов
utils/web_signals.py сигналы «повода для КП» (новое)
utils/cross_base.py  исключение компаний из других баз (новое)
utils/outreach_export.py новая схема колонок и правила отбора
utils/* остальное — без изменений логики
```

### 3.1 `config/segments.py`

```python
@dataclass(frozen=True)
class Segment:
    key: str                         # латиница, уникальный
    title: str                       # для людей и Excel
    osm_tags: list[tuple[str, str]]  # (key, value) для Overpass
    yandex_queries: list[str]        # без города; "{city}" подставляет парсер
    vk_keywords: list[str]
    aliases: list[str]               # для normalize() по рубрике/названию
    booking_relevant: bool = False   # проверять ли сигнал no_online_booking
    competitor: bool = False         # IT/связь — отдельный флаг, не в outreach по умолчанию

SEGMENTS: tuple[Segment, ...]
EXCLUDE_BRANDS: tuple[str, ...]      # федеральные сети и бренды
EXCLUDE_TERMS: tuple[str, ...]       # госучреждения, банкоматы, терминалы и т.п.
EXCLUDED_SEGMENTS_ALIASES: ...       # HoReCa, отели — маркер excluded_other_base_type
```

Добавление сегмента = одна запись; код не меняется.

Стартовые сегменты (v1):

| key | title | booking_relevant |
|---|---|---|
| stroitelstvo | Строительство и ремонт | нет |
| nedvizhimost | Недвижимость, агентства | нет |
| avto | Авто: СТО, автосалоны, запчасти | да |
| medicina | Медицина, стоматология | да |
| krasota | Красота: салоны, барбершопы | да |
| fitnes | Фитнес и спорт | да |
| obrazovanie | Образование: курсы, автошколы, детские центры | да |
| yurist_buh | Юристы, бухгалтерия, консалтинг | нет |
| turizm | Туризм, экскурсии, прокат | да |
| torgovlya | Торговля и опт | нет |
| proizvodstvo | Производство | нет |
| logistika | Логистика, грузоперевозки | нет |
| event | Event, свадьбы, фото/видео | да |
| mebel | Мебель и интерьер | нет |
| vet | Ветклиники, зоотовары | да |
| klining_uslugi | Клининг, бытовые услуги | нет |
| agro_vino | Агро, виноделие | нет |
| it_svyaz | IT и связь (competitor=True) | нет |
| finansy | Страхование, финансы | нет |
| reklama | Реклама, полиграфия | нет |

Точные наборы тегов/запросов/ключевых слов — часть реализации (этап 1),
с условием: у каждого сегмента ≥ 1 yandex-запрос и ≥ 1 vk-ключ; ключи
уникальны; каждый alias однозначно указывает на один сегмент.

### 3.2 Исключения

- `EXCLUDE_TERMS`: администрация, МФЦ, школа №/гимназия (гос), поликлиника
  (гос), банкомат, терминал, почта России, суд, прокуратура и т.п. → флаг
  `excluded_gov`.
- `EXCLUDE_BRANDS`: федеральные сети (Магнит, Пятёрочка, ПУД, МТС, Мегафон,
  Сбер, РНКБ, DNS, Эльдорадо и т.п., расширяемый список) → `excluded_chain`.
- HoReCa и размещение (ресторан, кафе, отель, гостевой дом…) →
  `excluded_other_base_type`.
- Все исключённые остаются в master (аудит), но не в outreach.

## 4. Поток данных

```
источники (osm → vk → yandex → crawler)
 → наблюдения в storage (dedup.db) с provenance
 → entity resolution (только сильные признаки)
 → граница Крыма + geo_city
 → сегментация
 → enrichment (email_finder, vk_email, site_finder — выкл. по умолчанию)
 → web_signals
 → cross_base exclude
 → master_all.csv/xlsx, master_quarantine.csv,
   outreach_ready.xlsx/.csv, outreach_review.csv, handoff/latest.json
```

### 4.1 Сегментация

- `segment` хранится в существующем поле `client_type` (ключ сегмента).
  Источник кладёт в `category` ключ сегмента из своего запроса/тега; VK и
  crawler — по рубрике/названию через aliases. Выбор основного значения в
  кластере — существующий `SOURCE_PRIORITY`: Яндекс > OSM > VK > Crawler.
- `segments_all` не хранится отдельной колонкой master: outreach вычисляет
  его из provenance (`fields.client_type`) и выводит названия через `; `.
- Если сегмент не определён — `прочее`; в outreach не попадает, причина
  `no_segment`.

### 4.2 Источники

- **OSM**: Overpass по `osm_tags` всех сегментов внутри bbox Крыма +
  локальная проверка границы (как в HoReCa).
- **VK**: города × `vk_keywords`. B2B quality gate: VK-запись без
  подтверждения OSM/Яндекс и без собственного сайта → `master_quarantine`;
  не питает crawler и outreach. При сильном сопоставлении может заполнить
  только пустой контакт и ставит флаг ручной проверки (политика HoReCa).
- **Яндекс Карты**: 39 городов × все `yandex_queries` (~60). Пакетный режим
  обязателен: `YANDEX_CITY_OFFSET`, `YANDEX_QUERY_OFFSET`,
  `YANDEX_MAX_DETAIL_REQUESTS=600`, circuit breaker на CAPTCHA/ошибки.
  Сегмент берётся из запроса. Извлечение рубрики карточки в v1 не
  входит (нужна живая проверка селекторов); HoReCa/сети/госорганы в
  выдаче отсекаются по названию через исключения (§3.2).
- **Crawler**: обход сайтов найденных компаний, триггеры из aliases
  сегментов; слабые VK-сайты не являются seed.

### 4.3 Cross-base exclude (`utils/cross_base.py`)

- `EXCLUDE_MASTERS` — список путей к CSV других баз (HoReCa, отели), через
  `;`. Пусто — проверка выключена.
- Из них загружаются нормализованные email и домены сайтов (исключая
  общие почтовые домены: mail.ru, yandex.ru, gmail.com и т.п.).
- Совпадение по email или корпоративному домену → флаг
  `already_in_other_base`, запись не попадает в outreach.
- Отсутствующий файл → предупреждение в `run_summary`, не ошибка.

## 5. Сигналы «повода для КП» (`utils/web_signals.py`)

Вычисляются после сборки master отдельной стадией `web_signals`.
email_finder работает через Playwright и не хранит HTML, поэтому стадия
делает собственный GET главной: сначала `https://<домен>/`, при неудаче —
`http://<домен>/` (1–2 запроса на домен). Все запросы через
`safe_http.fetch_public_text`. Результат — JSON-кэш
`output/web_signals.json` по домену.

| Сигнал | Правило | Повод |
|---|---|---|
| `no_website` | нет сайта ни в одном источнике (соцсети/агрегаторы не считаются) | Сайт с нуля |
| `site_dead` | таймаут, 5xx, DNS-ошибка, parking-маркеры | Сайт не работает |
| `no_https` | https недоступен или сертификат невалиден, http работает | Модернизация сайта |
| `no_mobile` | нет `<meta name="viewport">` | Адаптив/редизайн |
| `no_online_booking` | сегмент `booking_relevant`, нет маркеров записи (YCLIENTS, DIKIDI, Sonline, формы «записаться») | Онлайн-запись / бот |
| `site_builder` | маркеры Tilda, Wix, uCoz, narod, Nethouse, Ukit, Flexbe, Craftum | Собственная разработка |
| `outdated` | копирайт-год ≤ текущий−3, jQuery 1.x, Flash, табличная вёрстка | Редизайн |

- «Повод для КП» — первый сигнал по порядку таблицы; если сигналов нет —
  «Автоматизация/боты/CRM».
- Кэш хранит по домену `signals` и `checked_at`; в outreach выводятся
  колонки «Сигналы» и «Повод для КП». Сайт, который ещё не проверен,
  получает сигнал `not_checked` и общий повод. Соцсети/агрегаторы в поле
  сайта (vk.com, instagram, taplink, t.me…) считаются `no_website` и не
  запрашиваются.
- Пересчёт не чаще раза в 30 дней на компанию.
- Бюджет — общий `ENRICH_MAX_SITES`.
- Сигналы — эвристика; в outreach это подсказка, а не утверждение.

## 6. Выходные файлы

### 6.1 `outreach_ready.xlsx/.csv` — колонки

Email, Название, Сегмент, Все сегменты, Город, Телефон, Сайт, Соцсеть,
Адрес, Повод для КП, Сигналы, Источник, ID объекта, Доверие,
Флаги качества, Все email, Все телефоны.

### 6.2 Правила отбора

В outreach попадает запись, если:

- есть валидный email (формат + `EMAIL_BLACKLIST` из HoReCa + email на
  доменах `EXCLUDE_BRANDS`);
- сегмент из целевого списка и не `competitor` (competitor включается
  `OUTREACH_INCLUDE_COMPETITORS=1`);
- нет флагов `outside_crimea`, `excluded_chain`, `excluded_gov`,
  `excluded_other_base_type`, `already_in_other_base` и флагов VK-карантина.

Один email — одна строка (дубли email схлопываются, остальные компании
перечисляются в `outreach_review.csv` с причиной `duplicate_email`).
Всё исключённое — в `outreach_review.csv` с причиной.

### 6.3 Handoff и доставка

`output/handoff/latest.json` — схема HoReCa, всегда
`approved_for_send=false`, `auto_send_allowed=false`. Отдельный Telegram-бот
и отдельная папка Google Drive.

## 7. Ошибки и наблюдаемость

Контракт HoReCa без изменений: ненулевой exit code при исключении
источника, нарушении `MIN_RECORDS_*`, пустом master, ошибке обязательного
artifact; частичный результат не доставляется. `run_summary.json`
дополнительно содержит разбивку по сегментам: записи, email, outreach,
счётчики каждого сигнала.

## 8. Тестирование

- Перенос всего pytest-набора HoReCa; адаптация `test_categories`,
  `test_vk_relevance`, `test_osm_quality`, `test_outreach_export`,
  `test_yandex_coverage`, `test_crawler_quality` под сегменты.
- Новые: `test_segments.py`, `test_web_signals.py` (HTML-фикстуры: Tilda,
  Wix, без viewport, parking, http-only, YCLIENTS), `test_cross_base.py`,
  `test_exclusions.py`.
- Приёмка: `DRY_RUN=1 ONLY_SOURCE=osm` → canary VK и Яндекс на одном
  городе → ручной аудит 50 строк outreach.

## 9. Деплой

- `/home/b2b_parser`, системный пользователь `b2b-parser`.
- `deploy/b2b_parser.service/.timer`, AppArmor-профиль Chromium — копии
  hardened-юнитов HoReCa; `MemoryMax=3G`, `RuntimeMaxSec=12h`.
- Таймер: пятница 03:00 MSK (HoReCa — СБ, отели — ВС). Disabled до
  завершения приёмки.

## 10. Этапы реализации

1. Форк, удаление food-специфики, `segments.py`, адаптация источников и
   тестов; все тесты зелёные.
2. `web_signals`, `cross_base`, исключения, новые колонки outreach.
3. Локальные dry-run и canary.
4. Деплой на VPS, пакетный прогон Яндекса, ручной аудит.
