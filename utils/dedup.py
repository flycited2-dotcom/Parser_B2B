"""Persistent observation state.

This module records when a source observation was first and last seen.  It is
not an entity-level exclusion list: seeing a venue in an earlier run must not
prevent a fresh observation (or richer data from another source) from entering
the current result set.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime

from utils.entity_resolution import (
    normalize_city,
    normalize_name,
    observation_identity,
)


DEDUP_PATH = "output/dedup.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    resolved = os.path.abspath(DEDUP_PATH)
    if _conn is not None and _conn_path == resolved:
        return _conn
    if _conn is not None:
        _conn.close()
    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _conn = sqlite3.connect(resolved, check_same_thread=False, timeout=10.0)
    _conn_path = resolved
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            observation_id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            name_norm TEXT NOT NULL DEFAULT '',
            city_norm TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1
        )
    """)
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_observations_name_city "
        "ON observations(name_norm, city_norm)"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_observations_source "
        "ON observations(source)"
    )
    _conn.commit()
    return _conn


def touch_observation(item: dict, observed_at: str | None = None) -> dict:
    """Upsert observation state and return its identity/timestamps.

    The returned ``is_new`` flag is informational only.  Callers must not use
    it to suppress a row from the current refresh.
    """
    observation_id = observation_identity(item)
    source = str(item.get("source") or "").strip()
    source_id = str(item.get("source_id") or "").strip()
    name_norm = normalize_name(item.get("name"))
    city_norm = normalize_city(item.get("city"))
    now = observed_at or _now()

    with _lock:
        conn = _connect()
        existing = conn.execute(
            "SELECT first_seen, seen_count FROM observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        if existing is None:
            first_seen = now
            conn.execute(
                """
                INSERT INTO observations(
                    observation_id, source, source_id, name_norm, city_norm,
                    first_seen, last_seen, seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (observation_id, source, source_id, name_norm, city_norm, now, now),
            )
            is_new = True
            seen_count = 1
        else:
            first_seen = existing[0]
            seen_count = int(existing[1] or 0) + 1
            conn.execute(
                """
                UPDATE observations
                   SET source = ?, source_id = ?, name_norm = ?, city_norm = ?,
                       last_seen = ?, seen_count = ?
                 WHERE observation_id = ?
                """,
                (source, source_id, name_norm, city_norm, now, seen_count, observation_id),
            )
            is_new = False
        conn.commit()
    return {
        "observation_id": observation_id,
        "is_new": is_new,
        "first_seen": first_seen,
        "last_seen": now,
        "seen_count": seen_count,
    }


def is_seen(name: str, city: str, source: str = "", source_id: str = "") -> bool:
    """Backward-compatible lookup; source/source_id make it observation-safe."""
    with _lock:
        conn = _connect()
        if source or source_id:
            probe = {
                "name": name,
                "city": city,
                "source": source,
                "source_id": source_id,
            }
            observation_id = observation_identity(probe)
            row = conn.execute(
                "SELECT 1 FROM observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM observations WHERE name_norm = ? AND city_norm = ? LIMIT 1",
                (normalize_name(name), normalize_city(city)),
            ).fetchone()
        return row is not None


def mark_seen(name: str, city: str, source: str = "", source_id: str = "") -> bool:
    """Backward-compatible wrapper: True only on the first observation.

    Storage intentionally ignores this boolean and always accepts a fresh-run
    observation.  Existing integrations that only need novelty information can
    continue using the return value.
    """
    state = touch_observation({
        "name": name,
        "city": city,
        "source": source,
        "source_id": source_id,
    })
    return bool(state["is_new"])


def total() -> int:
    with _lock:
        conn = _connect()
        return int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])


def stats_by_source() -> dict[str, int]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM observations GROUP BY source"
        ).fetchall()
        return {row[0] or "?": int(row[1]) for row in rows}


def reset() -> None:
    """Clear observation state for a deliberate clean run."""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM observations")
        # Old releases used an entity-blocking `seen` table.  If it exists,
        # clear it too, but do not depend on it for current behavior.
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='seen'"
        ).fetchone()
        if legacy:
            conn.execute("DELETE FROM seen")
        conn.commit()


def close() -> None:
    """Close the cached connection (primarily useful for tests/maintenance)."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _conn_path = None
