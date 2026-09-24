# Runbook: безопасный запуск B2B Parser

Все команды выполнять из корня проекта. Shell environment имеет приоритет
над `.env`, поэтому одноразовые настройки можно передавать перед командой.
`DRY_RUN` всё равно делает запросы к выбранным источникам; «dry» означает
изоляцию файлов и запрет Telegram/Drive, а не отсутствие сети.

## 1. Перед первым запуском

1. Не включать timer до завершения всех canary-проверок.
2. Сделать резервную копию кода, `.env`/`token.json` в зашифрованное хранилище
   и существующего `output/`. Секреты не добавлять в Git.
3. Установить воспроизводимое окружение:

   ```bash
   python3 -m venv venv
   venv/bin/pip install -r requirements.lock
   # Системные пакеты устанавливаются администратором один раз.
   venv/bin/playwright install-deps chromium
   sudo -u b2b-parser env HOME=/home/b2b_parser \
     PLAYWRIGHT_BROWSERS_PATH=/home/b2b_parser/.cache/ms-playwright \
     venv/bin/playwright install chromium
   chown -R root:b2b-parser /home/b2b_parser/.cache
   chmod -R u=rwX,g=rX,o= /home/b2b_parser/.cache
   ```

4. Запустить unit-тесты без сетевых парсеров:

   ```bash
   venv/bin/python -m pytest -p no:cacheprovider
   ```

5. Создать системного пользователя `b2b-parser` без login shell. Проверить
   права: код не должен быть world-writable; `.env`, OAuth token и
   service-account JSON — `0600`, владелец — пользователь сервиса; `output/`
   и `.cache/ms-playwright/` доступны ему на запись.
6. На Ubuntu 23.10+ установить root-owned профиль
   `deploy/b2b-parser-chromium.apparmor` в `/etc/apparmor.d/` и выполнить
   `systemctl reload apparmor`. Не отключать
   `kernel.apparmor_restrict_unprivileged_userns` глобально и не добавлять
   Chromium флаг `--no-sandbox`. Основание: официальная инструкция Chromium
   `docs/security/apparmor-userns-restrictions.md`.

## 2. Preflight

Preflight выполняется перед браузером и сетью. Он проверяет имена источников,
числовые лимиты, обязательный `VK_TOKEN`, комплект Telegram credentials,
наличие Google credentials и свободное место. Значения секретов не попадают в
`run_summary.json`.

Коды завершения:

- `0` — проверенный успешный результат;
- `1` — источник/порог/artifact/delivery завершился ошибкой;
- `2` — неверная конфигурация или output path;
- `75` — уже существует недавно обновлённый активный запуск.

## 3. Canary по источникам

Начать с OSM. Команда создаст отдельный каталог, возьмёт максимум 25 записей и
ничего не отправит:

```bash
DRY_RUN=1 ONLY_SOURCE=osm venv/bin/python main.py
```

Проверить напечатанный output path:

- `run_summary.json`: `status=ok`, `exit_code=0`;
- `master_all.csv/.xlsx` существуют и содержат данные;
- `master_quarantine.csv` содержит широкий VK-шум отдельно от рабочего master;
- `outreach_ready.xlsx` и `outreach_review.csv` существуют (ready может быть
  пустым на маленьком OSM canary без email);
- `handoff/latest.json`: `state=dry_run_review`,
  `approved_for_send=false`, schema v3 и checksum неизменяемой копии
  quarantine;
- production `output/master_all.*` не изменился.

Затем выполнить VK:

```bash
DRY_RUN=1 ONLY_SOURCE=vk MAX_CITIES=1 MAX_QUERIES_PER_SOURCE=1 \
  MAX_ITEMS_PER_SOURCE=25 venv/bin/python main.py
```

Яндекс запускать только с жёсткими лимитами:

```bash
DRY_RUN=1 ONLY_SOURCE=yandex MAX_CITIES=1 MAX_QUERIES_PER_SOURCE=1 \
  MAX_ITEMS_PER_SOURCE=10 venv/bin/python main.py
```

У Яндекса есть второй, независимый предел дорогих открытий карточек:
`YANDEX_MAX_DETAIL_REQUESTS` (production default 600). Один и тот же `org_id`,
попавший в несколько запросов, открывается один раз за процесс. Первая
CAPTCHA/блокировка или серия ошибок открывает circuit breaker, источник
заканчивается с ненулевым кодом, а доставка подавляется.

## 4. Пакетный проход Яндекс.Карт

Полная матрица содержит 39 городов × 16 запросов; один unlimited-запуск не
является безопасным. Использовать прямоугольные пакеты и явные смещения:

```bash
ONLY_SOURCE=yandex AUTO_NOTIFY=0 AUTO_UPLOAD=0 SKIP_ENRICHMENT=1 \
MAX_CITIES=2 MAX_QUERIES_PER_SOURCE=2 MAX_ITEMS_PER_SOURCE=40 \
YANDEX_RESULTS_PER_QUERY=10 YANDEX_MAX_DETAIL_REQUESTS=40 \
YANDEX_CITY_OFFSET=0 YANDEX_QUERY_OFFSET=0 \
MIN_RECORDS_YANDEX=10 CRITICAL_SOURCES=yandex venv/bin/python main.py
```

Следующий пакет городов: `YANDEX_CITY_OFFSET=2`; после 38 перейти к следующей
паре типов: `YANDEX_CITY_OFFSET=0 YANDEX_QUERY_OFFSET=2`. Допустимые начальные
смещения: города `0..38`, запросы `0..15`; preflight отклоняет выход за
диапазон до запуска Chromium. Повторы org ID не теряются и не создают лишний
detail request. После каждого пакета проверить `stop=completed` или ожидаемый
`detail_budget_exhausted`, exit code, master и журнал. При CAPTCHA пакет не
перезапускать немедленно.

Crawler и enrichment проверять на малом подготовленном input. Для разрешения
enrichment в dry-run нужны обе настройки:

```bash
DRY_RUN=1 DRY_RUN_ENRICHMENT=1 SKIP_ENRICHMENT=0 \
  ENRICH_MAX_SITES=10 MAX_SOURCES=1 venv/bin/python main.py
```

Перед crawler обязательно заново собрать master текущей версией. Loader
отбрасывает standalone VK-шум и сайт, подаренный слабым VK-наблюдением, до
DNS/HTTP; `master_quarantine.csv` никогда не использовать как seed-файл.

## 5. Полный ручной прогон

Первый production-прогон выполнить без доставки:

```bash
AUTO_NOTIFY=0 AUTO_UPLOAD=0 venv/bin/python main.py
```

Следить за process exit code, RAM/swap, свободным диском,
`journalctl -u b2b_parser.service -f`,
`output/progress.json` и `output/run_summary.json`. Не считать запуск успешным
только по наличию CSV: обязательны exit code `0`, непустой master и отсутствие
failures в summary.

После ручной проверки отдельно разрешить Telegram и Drive на тестовую папку.
Только затем можно включать timer.

## 6. Сбой и восстановление

- Не удалять `progress.json` вслепую. Сначала убедиться, что PID/процесс
  действительно отсутствует. Свежий `running` приводит к exit `75`; stale
  state включает resume.
- Сохранить `run_summary.json`, журнал и весь частичный `output/` до повторного
  запуска.
- `dedup.db` копировать согласованным SQLite backup или только при остановленном
  парсере. Простая копия во время записи может быть неконсистентной.
- Для повторной доставки использовать checksum/idempotency key из handoff, а
  не создавать новую рассылку для того же master.
- Для аудита quarantine использовать run-specific файл и SHA-256 из handoff,
  а не изменяемый `output/master_quarantine.csv`.
- Backup считается рабочим только после тестового восстановления в отдельный
  каталог.

## 7. Регулярная эксплуатация

- Настроить срок хранения journald; ограничить срок хранения raw
  result/enriched files.
- Передавать на рассылку только `outreach_ready.xlsx`, никогда не полный
  `master_all.xlsx`; каждый новый `run_id` требует отдельного approval.
- Хранить внешний версионный backup `output/`, включая `dedup.db`, manifests и
  raw CSV. Google Drive `master_all` не заменяет такой backup.
- Следить за `failure_count`, числом строк master и временем каждого источника.
- `Persistent=true` у timer может дать запуск сразу после reboot; тяжёлые
  парсеры на одном VPS должны использовать общий lock.
