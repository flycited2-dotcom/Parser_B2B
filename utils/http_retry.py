"""HTTP-запрос с экспоненциальным retry на сетевые ошибки и 5xx/429."""
from __future__ import annotations

import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def http_request(
    req: str | Request,
    *,
    timeout: float = 30,
    retries: int = 3,
    backoff: tuple[float, ...] = (1, 3, 8),
) -> bytes:
    """Сделать HTTP-запрос с retry. Возвращает body как bytes.

    Ретраит на:
      - URLError (timeout при коннекте, DNS, connection refused)
      - TimeoutError (timeout при чтении тела ответа — отдельный путь в
        urllib, не оборачивается в URLError)
      - HTTPError code 5xx и 429

    Не ретраит на:
      - HTTPError 4xx (кроме 429) — клиентская ошибка, retry не поможет

    Бросает последнюю ошибку если все retry исчерпаны.
    """
    last_err: Exception | None = None
    attempts = retries + 1
    for i in range(attempts):
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last_err = e
        except (URLError, TimeoutError) as e:
            last_err = e
        if i < retries:
            delay = backoff[min(i, len(backoff) - 1)]
            time.sleep(delay)
    assert last_err is not None
    raise last_err
