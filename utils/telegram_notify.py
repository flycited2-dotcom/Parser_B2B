"""Отправка отчёта в Telegram после прогона парсера.

TG_BOT_TOKEN и TG_CHAT_ID — из переменных окружения (.env).
"""
import csv
import html
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# DNS/network может временно лежать (как было 07:30 07.05.2026).
# Делаем 5 попыток с экспоненциальной паузой: 30, 60, 120, 240, 480 сек ≈ 16 мин.
RETRIES = 5
BACKOFF = (30, 60, 120, 240, 480)


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _http_post(url: str, data: bytes, headers: dict, timeout: int = 60) -> tuple[int, bytes]:
    """POST с ретраями на DNS/сетевых сбоях. HTTP-ошибки 4xx не ретраит."""
    last_code, last_body = 0, b""
    for attempt in range(RETRIES):
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            # 4xx — это «нельзя», не ретраим
            if 400 <= e.code < 500:
                return e.code, body
            last_code, last_body = e.code, body
        except URLError as e:
            last_code, last_body = 0, str(e).encode()
        except Exception as e:
            last_code, last_body = 0, str(e).encode()

        if attempt < RETRIES - 1:
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            print(f"[telegram] retry {attempt+1}/{RETRIES} через {wait}с: {last_body[:100]!r}")
            time.sleep(wait)

    return last_code, last_body


def send_message(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    payload = urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }).encode()
    code, body = _http_post(
        _api(token, "sendMessage"),
        payload,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if code != 200:
        print(f"[telegram] sendMessage HTTP {code}: {body[:300]!r}")
    return code == 200


def send_document(token: str, chat_id: str, file_path: str, caption: str = "",
                  parse_mode: str = "HTML") -> bool:
    if not os.path.exists(file_path):
        print(f"[telegram] file not found: {file_path}")
        return False

    boundary = "----B2BParserBoundary7c3"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
    ]
    if caption:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
        )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\n{parse_mode}\r\n"
        )
    head = "".join(parts).encode("utf-8")

    file_name = os.path.basename(file_path)
    # Telegram сам определит тип по расширению — отдаём octet-stream.
    file_head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"document\"; filename=\"{file_name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")

    with open(file_path, "rb") as f:
        file_body = f.read()
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    code, body = _http_post(
        _api(token, "sendDocument"),
        head + file_head + file_body + tail,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=120,
    )
    if code != 200:
        print(f"[telegram] sendDocument HTTP {code}: {body[:300]!r}")
    return code == 200


def _read_csv(path: str) -> list[dict]:
    """Поддержка обоих разделителей."""
    for delim in (";", ","):
        try:
            with open(path, encoding="utf-8-sig") as f:
                sample = f.read(2048)
                f.seek(0)
                if delim in sample:
                    rows = list(csv.DictReader(f, delimiter=delim))
                    if rows and "name" in rows[0]:
                        return rows
        except Exception:
            continue
    return []


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_summary(csv_path: str, source_label: str = "") -> str:
    """HTML для Telegram (parse_mode=HTML).
    Используем только разрешённые теги: <b>, <i>, <code>, <pre>, <a>.
    """
    if not os.path.exists(csv_path):
        return "<b>CSV не найден</b>"

    rows = _read_csv(csv_path)
    total = len(rows)
    if total == 0:
        return "<b>CSV пуст</b>"

    with_phone = sum(1 for r in rows if (r.get("phone") or "").strip())
    with_email = sum(1 for r in rows if (r.get("email") or "").strip())
    with_addr = sum(1 for r in rows if (r.get("address") or "").strip())
    with_site = sum(1 for r in rows if (r.get("website") or "").strip())

    by_source: dict[str, int] = {}
    by_city: dict[str, int] = {}
    for r in rows:
        s = r.get("source", "?") or "?"
        c = r.get("city", "?") or "?"
        by_source[s] = by_source.get(s, 0) + 1
        by_city[c] = by_city.get(c, 0) + 1

    def pct(n: int) -> str:
        return f"<b>{n}</b> ({100 * n // max(1, total)}%)"

    src_lines = [f"  • {html.escape(k)}: <b>{v}</b>"
                 for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])]
    city_lines = [f"  • {html.escape(k)}: <b>{v}</b>"
                  for k, v in sorted(by_city.items(), key=lambda kv: -kv[1])][:10]

    title = "B2B Crimea Parser — отчёт"
    if source_label:
        title += f" · {html.escape(source_label)}"

    out = [
        f"<b>{title}</b>",
        f"Файл: <code>{html.escape(os.path.basename(csv_path))}</code>",
        "",
        f"📊 Всего записей: <b>{total}</b>",
        f"📞 С телефоном:  {pct(with_phone)}",
        f"✉️ С email:      {pct(with_email)}",
        f"🏠 С адресом:    {pct(with_addr)}",
        f"🌐 С сайтом:     {pct(with_site)}",
        "",
        "<b>По источникам:</b>",
        *src_lines,
        "",
        "<b>Топ городов:</b>",
        *city_lines,
    ]
    return "\n".join(out)


def build_preview(csv_path: str, limit: int = 10) -> str:
    """Превью первых N записей в виде <pre>-таблицы."""
    rows = _read_csv(csv_path)
    if not rows:
        return ""

    # отдаём приоритет строкам с заполненными контактами
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            -(1 if r.get("phone") else 0)
            - (1 if r.get("email") else 0)
            - (1 if r.get("address") else 0)
        ),
    )[:limit]

    lines = ["<b>Превью (первые с контактами):</b>", "<pre>"]
    for i, r in enumerate(rows_sorted, 1):
        lines.append(f"#{i} {_truncate(r.get('name',''), 60)}")
        if r.get("city"):
            lines.append(f"   город:    {_truncate(r['city'], 60)}")
        if r.get("address"):
            lines.append(f"   адрес:    {_truncate(r['address'], 70)}")
        if r.get("phone"):
            lines.append(f"   телефон:  {_truncate(r['phone'], 30)}")
        if r.get("email"):
            lines.append(f"   email:    {_truncate(r['email'], 60)}")
        if r.get("website"):
            lines.append(f"   сайт:     {_truncate(r['website'], 70)}")
        lines.append(f"   источник: {_truncate(r.get('source',''), 30)}")
        lines.append("")
    lines.append("</pre>")
    return "\n".join(lines)


def checkpoint(label: str, added: int, total: int, elapsed_sec: int,
                extra: str = "") -> None:
    """Короткое сообщение «✅ {label}: +{added}, всего {total} (за {elapsed})»."""
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    if elapsed_sec < 60:
        elapsed = f"{elapsed_sec}с"
    elif elapsed_sec < 3600:
        elapsed = f"{elapsed_sec // 60}м {elapsed_sec % 60}с"
    else:
        elapsed = f"{elapsed_sec // 3600}ч {(elapsed_sec % 3600) // 60}м"
    text = (
        f"✅ <b>{html.escape(label)}</b>: +{added} "
        f"(всего в прогоне: <b>{total}</b>, за {elapsed})"
    )
    if extra:
        text += f"\n{html.escape(extra)}"
    try:
        send_message(token, chat_id, text)
    except Exception as e:
        print(f"[telegram checkpoint] err: {e}")


def notify(csv_path: str, source_label: str = "", xlsx_path: str = "") -> None:
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[telegram] TG_BOT_TOKEN/TG_CHAT_ID не заданы, пропуск")
        return

    summary = build_summary(csv_path, source_label)
    preview = build_preview(csv_path, limit=10)

    print(f"[telegram] отправка отчёта в чат {chat_id}")

    # 1. Сводка
    send_message(token, chat_id, summary)
    # 2. Превью отдельным сообщением (его длина может перевалить лимит caption)
    if preview:
        send_message(token, chat_id, preview)
    # 3. CSV
    send_document(token, chat_id, csv_path,
                  caption=f"📁 {os.path.basename(csv_path)}", parse_mode="HTML")
    # 4. XLSX (если построен) — отдельным документом, удобнее для отдела продаж
    if xlsx_path and os.path.exists(xlsx_path):
        send_document(token, chat_id, xlsx_path,
                      caption=f"📊 {os.path.basename(xlsx_path)} — структура по городам",
                      parse_mode="HTML")
    print("[telegram] готово")
