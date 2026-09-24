"""Состояние парсера в output/progress.json. Бот читает, парсер пишет.

Делаем атомарную запись: tmp + rename. Любые запросы читают не разорванный JSON.
"""
import json
import os
from datetime import datetime

PROGRESS_PATH = "output/progress.json"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write(state: dict) -> None:
    """state — произвольный словарь. Обогащаем last_update и пишем атомарно."""
    os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    state = dict(state)
    state["last_update"] = _now_iso()
    tmp = PROGRESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS_PATH)


def read() -> dict:
    try:
        with open(PROGRESS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {"_read_error": str(e)}


def update(**fields) -> dict:
    cur = read()
    cur.update(fields)
    write(cur)
    return cur


def mark_started(pid: int | None = None, **metadata) -> None:
    state = {
        "status": "running",
        "started_at": _now_iso(),
        "pid": pid or os.getpid(),
        "stage": "init",
        "current_query": "",
        "current_count": 0,
        "completed_sources": [],
        "source_results": [],
        "errors_last_5min": 0,
    }
    state.update(metadata)
    write(state)


def mark_stage(stage: str, **fields) -> None:
    fields.update(stage=stage, current_query="", current_count=0)
    update(**fields)


def mark_query(query: str) -> None:
    update(current_query=query)


def mark_count(count: int) -> None:
    update(current_count=count)


def mark_completed_source(source: str) -> None:
    cur = read()
    sources = cur.get("completed_sources", [])
    if source not in sources:
        sources.append(source)
    update(completed_sources=sources)


def mark_source_result(result: dict) -> None:
    cur = read()
    results = list(cur.get("source_results", []))
    results.append(dict(result))
    update(source_results=results)


def mark_finished(result: str = "ok", **fields) -> None:
    fields.update(status=result, finished_at=_now_iso(), stage="done")
    update(**fields)


def mark_failed(err: str, **fields) -> None:
    fields.update(
        status="failed",
        finished_at=_now_iso(),
        stage="failed",
        error=str(err)[:500],
    )
    update(**fields)
