"""Deterministic loading of split dotenv files.

Precedence, from lowest to highest:

1. ``.env``
2. ``.env.tg``
3. ``.env.vk``
4. ``.env.gdrive``
5. any other ``.env.*`` file in lexical order
6. ``.env.local``
7. variables already present in the process environment

The last rule is important for systemd ``Environment=`` entries and shell
commands such as ``DRY_RUN=1 python main.py``: an on-disk file must never
silently undo an explicit runtime override.
"""
from __future__ import annotations

import glob
import os

from dotenv import dotenv_values

_BASE_FILES = (".env", ".env.tg", ".env.vk", ".env.gdrive")
_LOCAL_FILE = ".env.local"


def _ordered_env_files(base_dir: str) -> list[str]:
    """Return existing dotenv files in their merge order."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        normalized = os.path.abspath(path)
        if os.path.isfile(normalized) and normalized not in seen:
            ordered.append(normalized)
            seen.add(normalized)

    for name in _BASE_FILES:
        add(os.path.join(base_dir, name))

    for path in sorted(glob.glob(os.path.join(base_dir, ".env.*"))):
        name = os.path.basename(path)
        if name == _LOCAL_FILE or name.endswith(".example"):
            continue
        add(path)

    # Local overrides are intentionally last among files.
    add(os.path.join(base_dir, _LOCAL_FILE))
    return ordered


def load_all_env(base_dir: str | None = None) -> list[str]:
    """Load dotenv files without overwriting the process environment.

    Later files override earlier files. Values that were present in
    ``os.environ`` before this function was called win over every file.
    Returns the loaded file names for non-secret diagnostics.
    """
    cwd = os.path.abspath(base_dir or os.getcwd())
    file_values: dict[str, str] = {}
    loaded: list[str] = []

    for path in _ordered_env_files(cwd):
        values = dotenv_values(path)
        for key, value in values.items():
            if key and value is not None:
                file_values[key] = value
        loaded.append(os.path.basename(path))

    # setdefault preserves explicit OS/systemd/shell overrides.
    for key, value in file_values.items():
        os.environ.setdefault(key, value)

    return loaded
