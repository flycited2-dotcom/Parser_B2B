from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

if sys.platform == "win32":
    # Windows consoles are often cp1251 and fail on status emoji.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

# Вывод в файл/пайп по умолчанию блочный: лог долгого этапа (Overpass, enrichment) молчал бы
# минутами, и было бы не понять, что процесс работает.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(line_buffering=True)

PROJECT_ROOT = Path(__file__).resolve().parent

from utils.env_loader import load_all_env

# Runtime/systemd/shell variables win over all files; files are read from the
# project directory even if main.py was started from a different CWD.
LOADED_ENV_FILES = load_all_env(str(PROJECT_ROOT))

from playwright.async_api import async_playwright

from parsers import crawler, osm, vk_groups, yandex_maps
from parsers.email_finder import run_enrichment
from utils import progress, storage
from utils.browser import create_browser_context
from utils.telegram_notify import checkpoint as tg_checkpoint
from utils.telegram_notify import notify as tg_notify


Runner = Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True)
class SourceSpec:
    label: str
    key: str
    runner: Runner
    module: ModuleType


RUNNERS: tuple[SourceSpec, ...] = (
    SourceSpec("OSM", "osm", osm.run, osm),
    SourceSpec("VK Groups", "vk", vk_groups.run, vk_groups),
    SourceSpec("Я.Карты", "yandex", yandex_maps.run, yandex_maps),
    SourceSpec("Crawler", "crawler", crawler.run, crawler),
)

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
RUN_SUMMARY_NAME = "run_summary.json"
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_ALREADY_RUNNING = 75


class ConfigError(ValueError):
    pass


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def _exclusive_run_lock(path: Path):
    """Hold a non-blocking process lock for one output directory.

    The open descriptor owns the lock, so an unclean process exit releases it
    automatically.  The small metadata file is intentionally retained for
    diagnostics; deleting a live lock file would create an inode race.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AlreadyRunning(f"run lock is held: {path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AlreadyRunning(f"run lock is held: {path}") from exc
        locked = True
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {"pid": os.getpid(), "acquired_at": datetime.now().isoformat(timespec="seconds")}
            )
        )
        handle.flush()
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be one of 1/0, true/false, yes/no; got {raw!r}")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer; got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}; got {value}")
    return value


def _safe_run_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    if not cleaned:
        raise ConfigError("DRY_RUN_ID is empty after sanitization")
    return cleaned[:80]


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    dry_run: bool
    headless: bool
    only_source: str
    skip_enrichment: bool
    auto_notify: bool
    auto_upload: bool
    allow_partial_delivery: bool
    fail_fast: bool
    max_sources: int
    max_cities: int
    max_queries_per_source: int
    max_items_per_source: int
    min_records_total: int
    min_records: dict[str, int]
    critical_sources: frozenset[str]
    min_free_disk_mb: int

    @classmethod
    def from_env(cls) -> "RunConfig":
        dry_run = _env_bool("DRY_RUN", False)
        now_id = datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
        run_id = _safe_run_id(os.getenv("DRY_RUN_ID", "") or now_id)

        critical_raw = os.getenv("CRITICAL_SOURCES", "osm")
        critical = frozenset(
            part.strip().lower() for part in critical_raw.split(",") if part.strip()
        )
        known = {spec.key for spec in RUNNERS}
        unknown = critical - known
        if unknown:
            raise ConfigError(f"CRITICAL_SOURCES contains unknown keys: {sorted(unknown)}")

        only_source = (os.getenv("ONLY_SOURCE") or "").strip().lower()
        if only_source and only_source not in known:
            raise ConfigError(
                f"ONLY_SOURCE={only_source!r} is invalid; expected one of {sorted(known)}"
            )

        # A bare DRY_RUN is deliberately small and side-effect free. Explicit
        # process env values can raise the limits for a larger rehearsal.
        max_sources = _env_int("MAX_SOURCES", 1 if dry_run else 0)
        max_cities = _env_int("MAX_CITIES", 1 if dry_run else 0)
        max_queries = _env_int("MAX_QUERIES_PER_SOURCE", 1 if dry_run else 0)
        max_items = _env_int("MAX_ITEMS_PER_SOURCE", 25 if dry_run else 0)

        skip_enrichment = _env_bool("SKIP_ENRICHMENT", dry_run)
        if dry_run and not _env_bool("DRY_RUN_ENRICHMENT", False):
            skip_enrichment = True

        min_records: dict[str, int] = {}
        for spec in RUNNERS:
            default = 1 if spec.key in critical else 0
            # A specifically selected source is a canary and must not silently
            # return an empty result (crawler is allowed to find no neighbours).
            if only_source == spec.key and spec.key != "crawler":
                default = max(default, 1)
            min_records[spec.key] = _env_int(
                f"MIN_RECORDS_{spec.key.upper()}", default
            )

        drive_requested = bool((os.getenv("GDRIVE_FOLDER_ID") or "").strip())
        return cls(
            run_id=run_id,
            dry_run=dry_run,
            headless=_env_bool("HEADLESS", True),
            only_source=only_source,
            skip_enrichment=skip_enrichment,
            auto_notify=(not dry_run and _env_bool("AUTO_NOTIFY", True)),
            auto_upload=(
                not dry_run and drive_requested and _env_bool("AUTO_UPLOAD", True)
            ),
            allow_partial_delivery=_env_bool("ALLOW_PARTIAL_DELIVERY", False),
            fail_fast=_env_bool("FAIL_FAST", False),
            max_sources=max_sources,
            max_cities=max_cities,
            max_queries_per_source=max_queries,
            max_items_per_source=max_items,
            min_records_total=_env_int("MIN_RECORDS_TOTAL", 1),
            min_records=min_records,
            critical_sources=critical,
            min_free_disk_mb=_env_int("MIN_FREE_DISK_MB", 512),
        )

    def public_dict(self) -> dict[str, Any]:
        """Non-secret settings suitable for logs and run_summary.json."""
        return {
            "dry_run": self.dry_run,
            "headless": self.headless,
            "only_source": self.only_source or None,
            "skip_enrichment": self.skip_enrichment,
            "auto_notify": self.auto_notify,
            "auto_upload": self.auto_upload,
            "allow_partial_delivery": self.allow_partial_delivery,
            "fail_fast": self.fail_fast,
            "max_sources": self.max_sources,
            "max_cities": self.max_cities,
            "max_queries_per_source": self.max_queries_per_source,
            "max_items_per_source": self.max_items_per_source,
            "min_records_total": self.min_records_total,
            "min_records": dict(self.min_records),
            "critical_sources": sorted(self.critical_sources),
            "min_free_disk_mb": self.min_free_disk_mb,
        }


def _resolve_paths(config: RunConfig) -> tuple[Path, Path]:
    if not config.dry_run:
        return PROJECT_ROOT, PROJECT_ROOT / "output"

    configured_root = (os.getenv("DRY_RUN_ROOT") or "").strip()
    base = Path(configured_root).expanduser() if configured_root else PROJECT_ROOT / "output" / "dry_runs"
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    run_root = (base / config.run_id).resolve()
    if run_root == Path(run_root.anchor):
        raise ConfigError("DRY_RUN_ROOT cannot resolve to a filesystem root")
    return run_root, run_root / "output"


def _selected_sources(config: RunConfig) -> list[SourceSpec]:
    selected = [spec for spec in RUNNERS if not config.only_source or spec.key == config.only_source]
    if config.max_sources:
        selected = selected[: config.max_sources]
    return selected


def _resolve_config_path(raw: str, default: str = "") -> Path:
    value = raw.strip() or default
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def preflight(
    config: RunConfig, selected: list[SourceSpec], run_root: Path
) -> tuple[list[str], list[str]]:
    """Validate configuration without making network requests."""
    errors: list[str] = []
    warnings: list[str] = []

    if not selected:
        errors.append("No sources selected")

    keys = {spec.key for spec in selected}
    if "vk" in keys and not (os.getenv("VK_TOKEN") or "").strip():
        errors.append("VK_TOKEN is required when the VK source is selected")

    if "yandex" in keys:
        try:
            yandex_state = yandex_maps._new_run_state(config.max_items_per_source)
            city_offset = yandex_maps._env_int(("YANDEX_CITY_OFFSET",), 0)
            query_offset = yandex_maps._env_int(("YANDEX_QUERY_OFFSET",), 0)
            results_per_query = yandex_maps._env_int(
                ("YANDEX_RESULTS_PER_QUERY",), 100, minimum=1
            )
            max_scrolls = yandex_maps._env_int(
                ("YANDEX_MAX_SCROLLS",), 30, minimum=1
            )
            if city_offset >= len(yandex_maps._ALL_CITIES):
                errors.append(
                    f"YANDEX_CITY_OFFSET={city_offset} is outside "
                    f"0..{len(yandex_maps._ALL_CITIES) - 1}"
                )
            if query_offset >= len(yandex_maps._ALL_QUERIES):
                errors.append(
                    f"YANDEX_QUERY_OFFSET={query_offset} is outside "
                    f"0..{len(yandex_maps._ALL_QUERIES) - 1}"
                )
            warnings.append(
                "Yandex limits: "
                f"city_offset={city_offset}, query_offset={query_offset}, "
                f"results_per_query={results_per_query}, max_scrolls={max_scrolls}, "
                f"detail_requests={yandex_state.detail_limit or 'unlimited'}"
            )
            if not yandex_state.detail_limit:
                warnings.append(
                    "YANDEX_MAX_DETAIL_REQUESTS=0 disables the production safety bound"
                )
        except ValueError as exc:
            errors.append(str(exc))

    if config.auto_notify:
        missing = [
            key
            for key in ("TG_BOT_TOKEN", "TG_CHAT_ID")
            if not (os.getenv(key) or "").strip()
        ]
        if missing:
            errors.append(f"Telegram delivery enabled but missing: {', '.join(missing)}")

    folder_id = (os.getenv("GDRIVE_FOLDER_ID") or "").strip()
    if config.auto_upload:
        from utils.gdrive import configured_token_path

        token_path = _resolve_config_path(configured_token_path())
        service_account = (os.getenv("GDRIVE_SERVICE_ACCOUNT") or "").strip()
        sa_path = _resolve_config_path(service_account) if service_account else None
        if not folder_id:
            errors.append("AUTO_UPLOAD=1 requires GDRIVE_FOLDER_ID")
        if not token_path.is_file() and not (sa_path and sa_path.is_file()):
            errors.append(
                "Google Drive delivery enabled but neither OAuth token nor service-account file exists"
            )
    elif folder_id and config.dry_run:
        warnings.append("Google Drive is configured but disabled by DRY_RUN")

    if config.dry_run:
        warnings.append("DRY_RUN: Telegram and Google Drive delivery are disabled")
        warnings.append(f"DRY_RUN: isolated run root is {run_root}")

    try:
        enrich_max_sites = _env_int("ENRICH_MAX_SITES", 400)
        if not config.skip_enrichment and enrich_max_sites == 0:
            warnings.append("ENRICH_MAX_SITES=0 removes the enrichment time bound")
    except ConfigError as exc:
        errors.append(str(exc))
    if not config.headless:
        warnings.append("HEADLESS=0 is not suitable for the current headless systemd unit")

    try:
        probe = run_root if run_root.exists() else run_root.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free_mb = shutil.disk_usage(probe).free // (1024 * 1024)
        if free_mb < config.min_free_disk_mb:
            errors.append(
                f"Free disk {free_mb} MiB is below MIN_FREE_DISK_MB={config.min_free_disk_mb}"
            )
    except OSError as exc:
        errors.append(f"Cannot inspect output filesystem: {exc}")

    return errors, warnings


def _detect_resume() -> set[str]:
    """Reject a live duplicate; return completed source labels for stale resume."""
    prev = progress.read()
    if prev.get("status") != "running":
        return set()
    last_update_str = prev.get("last_update")
    if not last_update_str:
        return set()
    try:
        last_update = datetime.fromisoformat(last_update_str)
    except (ValueError, TypeError):
        return set()
    age = datetime.now() - last_update
    if age < timedelta(hours=1):
        raise AlreadyRunning(
            f"progress.json is active; last update was {int(age.total_seconds())} seconds ago"
        )
    print(f"♻ Stale progress.json ({age.total_seconds() / 3600:.1f}h) — RESUME")
    return set(prev.get("completed_sources", []))


def _redact(text: str) -> str:
    result = str(text)
    for key in ("VK_TOKEN", "TG_BOT_TOKEN", "TG_CHAT_ID"):
        secret = (os.getenv(key) or "").strip()
        if len(secret) >= 6:
            result = result.replace(secret, "<redacted>")
    return result


@contextmanager
def _source_limits(spec: SourceSpec, config: RunConfig, stats: dict[str, int]):
    """Apply the common source/city/query/item limit contract temporarily.

    Parsers may also read these environment variables directly. This adapter
    keeps existing v1 modules bounded without changing their public runner API.
    """
    mutations: list[tuple[str, Any]] = []
    env_mutations: list[tuple[str, str | None]] = []

    # Dry-run defaults live in RunConfig even when MAX_* is absent from the
    # process environment. Parsers that read the environment directly must
    # see those resolved caps too, otherwise a nominal 1×1×25 canary becomes
    # an unlimited crawl. Preserve explicit non-empty caller values.
    for env_name, resolved_value in (
        ("MAX_CITIES", config.max_cities),
        ("MAX_QUERIES_PER_SOURCE", config.max_queries_per_source),
        ("MAX_ITEMS_PER_SOURCE", config.max_items_per_source),
    ):
        previous = os.environ.get(env_name)
        if resolved_value and not str(previous or "").strip():
            env_mutations.append((env_name, previous))
            os.environ[env_name] = str(resolved_value)

    def replace(name: str, value: Any) -> None:
        if hasattr(spec.module, name):
            mutations.append((name, getattr(spec.module, name)))
            setattr(spec.module, name, value)

    if config.max_cities:
        if hasattr(spec.module, "CITIES"):
            replace("CITIES", list(getattr(spec.module, "CITIES"))[: config.max_cities])
        if hasattr(spec.module, "VK_CITIES"):
            replace(
                "VK_CITIES",
                dict(list(getattr(spec.module, "VK_CITIES").items())[: config.max_cities]),
            )

    if config.max_queries_per_source and hasattr(spec.module, "QUERIES"):
        queries = getattr(spec.module, "QUERIES")
        replace("QUERIES", type(queries)(list(queries)[: config.max_queries_per_source]))
        if hasattr(spec.module, "EXTRA_QUERIES_GLOBAL"):
            replace("EXTRA_QUERIES_GLOBAL", [])

    original_save = getattr(spec.module, "save_item", None)
    if callable(original_save):
        mutations.append(("save_item", original_save))

        def limited_save(item):
            stats["observed"] += 1
            if config.max_items_per_source and stats["observed"] > config.max_items_per_source:
                stats["limited"] += 1
                return False
            return original_save(item)

        setattr(spec.module, "save_item", limited_save)

    try:
        yield
    finally:
        for name, original in reversed(mutations):
            setattr(spec.module, name, original)
        for name, previous in reversed(env_mutations):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def _reported_count(result: Any, stats: dict[str, int], added: int) -> int:
    """Normalize runner results while retaining compatibility with v1 runners."""
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for key in ("observed", "found", "count", "added"):
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return stats["observed"] if stats["observed"] else added


async def _run_one_source(
    spec: SourceSpec,
    context: Any,
    config: RunConfig,
) -> tuple[dict[str, Any], str | None]:
    started = time.monotonic()
    before = storage.total()
    stats = {"observed": 0, "limited": 0}
    error: str | None = None
    status = "ok"
    result: Any = None

    try:
        with _source_limits(spec, config, stats):
            result = await spec.runner(context)
    except Exception as exc:
        status = "failed"
        error = _redact(f"{type(exc).__name__}: {exc}")[:500]
        print(_redact(traceback.format_exc()))

    added = max(0, storage.total() - before)
    observed = _reported_count(result, stats, added)
    minimum = config.min_records.get(spec.key, 0)
    if status == "ok" and observed < minimum:
        status = "failed"
        error = f"observed {observed} records; minimum for {spec.key} is {minimum}"

    source_result = {
        "key": spec.key,
        "label": spec.label,
        "status": status,
        "observed": observed,
        "added": added,
        "minimum": minimum,
        "limited_items": stats["limited"],
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "error": error,
    }
    return source_result, error


async def _run_sources(
    selected: list[SourceSpec], config: RunConfig, summary: dict[str, Any]
) -> list[str]:
    failures: list[str] = []

    async def loop(context: Any) -> None:
        for spec in selected:
            progress.mark_stage(f"source:{spec.key}", source=spec.key)
            result, error = await _run_one_source(spec, context, config)
            summary["sources"].append(result)
            progress.mark_source_result(result)
            print(
                f"[source:{spec.key}] status={result['status']} observed={result['observed']} "
                f"added={result['added']} elapsed={result['elapsed_seconds']}s"
            )
            if error:
                failures.append(f"{spec.key}: {error}")
                if config.fail_fast:
                    break
            else:
                progress.mark_completed_source(spec.label)
                if config.auto_notify:
                    try:
                        tg_checkpoint(
                            spec.label,
                            result["added"],
                            storage.total(),
                            int(result["elapsed_seconds"]),
                        )
                    except Exception as exc:
                        summary["warnings"].append(
                            f"Telegram checkpoint failed for {spec.key}: {_redact(str(exc))[:200]}"
                        )

    needs_browser = any(spec.key == "yandex" for spec in selected)
    if not needs_browser:
        await loop(None)
        return failures

    async with async_playwright() as playwright:
        browser, context = await create_browser_context(playwright, headless=config.headless)
        try:
            await loop(context)
        finally:
            await browser.close()
    return failures


def _count_csv_rows(path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return sum(1 for _ in csv.DictReader(handle, delimiter=";"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)


def _immutable_copy(source: Path, target: Path) -> None:
    """Create a run-specific artifact once; never overwrite different bytes."""
    if target.exists():
        if _sha256(source) == _sha256(target):
            return
        raise RuntimeError(f"immutable handoff artifact already exists: {target}")
    _atomic_copy(source, target)


def _write_handoff(
    config: RunConfig,
    output_dir: Path,
    master_csv: str,
    master_xlsx: str,
    row_count: int,
    failures: list[str],
    outreach: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish immutable artifacts plus an atomic, approval-gated manifest."""
    handoff_dir = output_dir / "handoff"
    csv_source = Path(master_csv)
    csv_target = handoff_dir / f"master_all_{config.run_id}.csv"
    _immutable_copy(csv_source, csv_target)

    xlsx_target: Path | None = None
    if master_xlsx and Path(master_xlsx).is_file():
        xlsx_target = handoff_dir / f"master_all_{config.run_id}.xlsx"
        _immutable_copy(Path(master_xlsx), xlsx_target)

    digest = _sha256(csv_target)
    if failures:
        state = "failed"
    elif config.dry_run:
        state = "dry_run_review"
    else:
        state = "ready_for_review"

    outreach_manifest: dict[str, Any] | None = None
    if outreach:
        outreach_manifest = {
            "state": "ready_for_review" if outreach.get("ready_rows", 0) else "empty",
            "approved_for_send": False,
            "ready_rows": outreach.get("ready_rows", 0),
            "review_rows": outreach.get("review_rows", 0),
            "min_confidence": outreach.get("min_confidence"),
        }
        for key in ("ready_csv", "ready_xlsx", "review_csv"):
            source_value = outreach.get(key)
            source_path = Path(source_value) if source_value else None
            if not source_path or not source_path.is_file():
                continue
            suffix = "".join(source_path.suffixes)
            target = handoff_dir / f"{source_path.stem}_{config.run_id}{suffix}"
            _immutable_copy(source_path, target)
            outreach_manifest[key] = str(target.resolve())
            outreach_manifest[key + "_sha256"] = _sha256(target)
        outreach_digest = outreach_manifest.get("ready_csv_sha256", "")
        outreach_manifest["idempotency_key"] = (
            f"b2b-outreach-v1:{outreach_digest}" if outreach_digest else None
        )

    quarantine_manifest: dict[str, Any] | None = None
    quarantine_source = output_dir / "master_quarantine.csv"
    if quarantine_source.is_file():
        quarantine_target = handoff_dir / f"master_quarantine_{config.run_id}.csv"
        _immutable_copy(quarantine_source, quarantine_target)
        quarantine_digest = _sha256(quarantine_target)
        quarantine_manifest = {
            "csv": str(quarantine_target.resolve()),
            "sha256": quarantine_digest,
            "row_count": _count_csv_rows(str(quarantine_target)),
            "automation_eligible": False,
            "idempotency_key": f"b2b-quarantine-v1:{quarantine_digest}",
        }

    manifest = {
        "schema_version": 3,
        "run_id": config.run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "state": state,
        "dry_run": config.dry_run,
        "row_count": row_count,
        "master_csv": str(csv_target.resolve()),
        "master_xlsx": str(xlsx_target.resolve()) if xlsx_target else None,
        "sha256": digest,
        "idempotency_key": f"b2b-master-v1:{digest}",
        "approved_for_send": False,
        "auto_send_allowed": False,
        "approval_policy": "manual_approval_required",
        "failures": failures,
        "columns": list(storage.FIELDS),
        "outreach": outreach_manifest,
        "quarantine": quarantine_manifest,
    }
    manifest_path = handoff_dir / f"handoff_{config.run_id}.json"
    _atomic_json(manifest_path, manifest)
    _atomic_json(handoff_dir / "latest.json", manifest)
    manifest["manifest_path"] = str(manifest_path.resolve())
    return manifest


def _base_summary(config: RunConfig, output_dir: Path) -> dict[str, Any]:
    return {
        "run_id": config.run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
        "project_root": str(PROJECT_ROOT),
        "output_dir": str(output_dir.resolve()),
        "loaded_env_files": list(LOADED_ENV_FILES),
        "config": config.public_dict(),
        "preflight": {"errors": [], "warnings": []},
        "sources": [],
        "warnings": [],
        "failures": [],
        "artifacts": {},
        "master_rows": 0,
        "elapsed_seconds": None,
    }


def _finish(
    summary: dict[str, Any], output_dir: Path, failures: list[str], started: float
) -> int:
    summary["failures"] = failures
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
    summary["status"] = "failed" if failures else "ok"
    summary["exit_code"] = EXIT_FAILED if failures else 0

    summary_path = output_dir / RUN_SUMMARY_NAME
    _atomic_json(summary_path, summary)
    if failures:
        progress.mark_failed(
            "; ".join(failures)[:500],
            exit_code=EXIT_FAILED,
            run_summary=str(summary_path.resolve()),
            failure_count=len(failures),
        )
    else:
        progress.mark_finished(
            "ok",
            exit_code=0,
            run_summary=str(summary_path.resolve()),
            master_rows=summary["master_rows"],
        )

    print("\n" + "=" * 60)
    print(
        f"RUN SUMMARY status={summary['status']} exit={summary['exit_code']} "
        f"rows={summary['master_rows']} elapsed={summary['elapsed_seconds']}s"
    )
    print(f"output={output_dir.resolve()}")
    for source in summary["sources"]:
        print(
            f"  {source['key']}: {source['status']}, observed={source['observed']}, "
            f"added={source['added']}, {source['elapsed_seconds']}s"
        )
    for failure in failures:
        print(f"  FAILURE: {failure}")
    print("=" * 60)
    return summary["exit_code"]


async def _build_outreach(
    config: RunConfig,
    output_dir: Path,
    master_csv: str,
    summary: dict[str, Any],
    failures: list[str],
) -> dict[str, Any]:
    """Web signals (bounded) → cross-base exclusions → outreach artifacts."""
    from utils import cross_base, web_signals
    from utils.outreach_export import build_outreach_exports

    signals_path = output_dir / "web_signals.json"
    if config.skip_enrichment:
        summary["web_signals"] = "skipped"
    else:
        progress.mark_stage("web_signals")
        try:
            summary["web_signals"] = await web_signals.refresh_signals(
                master_csv,
                str(signals_path),
                max_sites=_env_int("ENRICH_MAX_SITES", 400),
            )
        except Exception as exc:
            failures.append(f"web_signals: {_redact(str(exc))[:300]}")

    emails, domains, warnings = cross_base.load_exclusions(cross_base.env_paths())
    summary["warnings"].extend(warnings)
    outreach = build_outreach_exports(
        master_csv,
        str(output_dir),
        run_id=config.run_id,
        signals_cache=web_signals.load_cache(str(signals_path)),
        other_base=(emails, domains),
    )
    summary["segments"] = {
        "ready_by_segment": outreach["by_segment"],
        "ready_by_signal": outreach["by_signal"],
    }
    return outreach


async def _pipeline(config: RunConfig, output_dir: Path) -> int:
    started = time.monotonic()
    summary = _base_summary(config, output_dir)
    failures: list[str] = []
    selected = _selected_sources(config)

    errors, warnings = preflight(config, selected, output_dir.parent)
    summary["preflight"] = {"errors": errors, "warnings": warnings}
    summary["warnings"].extend(warnings)

    try:
        completed = _detect_resume()
    except AlreadyRunning as exc:
        # Do not overwrite progress.json owned by the live process.
        print(f"Already running: {exc}")
        return EXIT_ALREADY_RUNNING

    progress.mark_started(
        run_id=config.run_id,
        dry_run=config.dry_run,
        output_dir=str(output_dir.resolve()),
    )
    progress.mark_stage("preflight")
    if errors:
        failures.extend(f"preflight: {error}" for error in errors)
        return _finish(summary, output_dir, failures, started)

    if completed:
        selected = [spec for spec in selected if spec.label not in completed]
        progress.update(completed_sources=sorted(completed))
        summary["warnings"].append(f"Resume skipped completed sources: {sorted(completed)}")

    print("=" * 60)
    print("B2B CRIMEA PARSER")
    print(f"run_id={config.run_id} dry_run={config.dry_run}")
    print(f"sources={[spec.key for spec in selected]}")
    print(f"output={output_dir.resolve()}")
    print("=" * 60)

    try:
        failures.extend(await _run_sources(selected, config, summary))

        progress.mark_stage("cross_source_merge")
        try:
            merged = storage.cross_source_merge()
            print(f"[merge] enriched cells={merged}")
        except Exception as exc:
            failures.append(f"cross_source_merge: {_redact(str(exc))[:300]}")

        latest = storage.get_output_file()
        if not config.skip_enrichment and os.path.exists(latest):
            progress.mark_stage("email_finder")
            print(f"[email_finder] input={latest}")
            try:
                await run_enrichment(latest)
            except Exception as exc:
                failures.append(f"email_finder: {_redact(str(exc))[:300]}")
        else:
            progress.mark_stage("email_finder_skipped")
            reason = "configured" if config.skip_enrichment else "no current result file"
            print(f"[email_finder] skipped: {reason}")

        # The accumulated master is a core artifact, not a Drive-only side effect.
        progress.mark_stage("build_master")
        master_csv = ""
        master_xlsx = ""
        outreach: dict[str, Any] | None = None
        try:
            from utils.merger import build_master_xlsx

            master_csv, master_xlsx = build_master_xlsx(str(output_dir))
            summary["artifacts"].update(
                master_csv=str(Path(master_csv).resolve()) if master_csv else None,
                master_xlsx=str(Path(master_xlsx).resolve()) if master_xlsx else None,
                master_quarantine=str(
                    (output_dir / "master_quarantine.csv").resolve()
                ) if (output_dir / "master_quarantine.csv").is_file() else None,
            )
            summary["master_rows"] = _count_csv_rows(master_csv)
            if summary["master_rows"] < config.min_records_total:
                failures.append(
                    f"master: {summary['master_rows']} rows; minimum is {config.min_records_total}"
                )

            outreach = await _build_outreach(
                config, output_dir, master_csv, summary, failures
            )
            summary["artifacts"]["outreach"] = outreach
            if not outreach["ready_rows"]:
                summary["warnings"].append(
                    "outreach_ready is empty; no contact passed email/quality gates"
                )
        except Exception as exc:
            failures.append(f"build_master: {_redact(str(exc))[:300]}")

        can_deliver = not failures or config.allow_partial_delivery
        progress.mark_stage("delivery")
        if config.dry_run:
            summary["artifacts"]["delivery"] = "disabled_by_dry_run"
        elif not can_deliver:
            summary["artifacts"]["delivery"] = "suppressed_due_to_failures"
        else:
            if config.auto_notify and master_csv:
                try:
                    tg_notify(master_csv, source_label="weekly master", xlsx_path=master_xlsx)
                    summary["artifacts"]["telegram"] = "attempted"
                except Exception as exc:
                    failures.append(f"telegram: {_redact(str(exc))[:300]}")
            else:
                summary["artifacts"]["telegram"] = "disabled"

            if config.auto_upload and master_csv:
                try:
                    from utils.gdrive import upload_file

                    csv_link = upload_file(master_csv)
                    xlsx_link = upload_file(master_xlsx) if master_xlsx else None
                    outreach_path = str((outreach or {}).get("ready_xlsx") or "")
                    outreach_link = upload_file(outreach_path) if outreach_path else None
                    if (
                        not csv_link
                        or (master_xlsx and not xlsx_link)
                        or (outreach_path and not outreach_link)
                    ):
                        failures.append("gdrive: one or more master uploads failed")
                    summary["artifacts"]["gdrive"] = {
                        "csv_uploaded": bool(csv_link),
                        "xlsx_uploaded": bool(xlsx_link) if master_xlsx else None,
                        "outreach_uploaded": bool(outreach_link) if outreach_path else None,
                    }
                except Exception as exc:
                    failures.append(f"gdrive: {_redact(str(exc))[:300]}")
            else:
                summary["artifacts"]["gdrive"] = "disabled"

        if master_csv and Path(master_csv).is_file():
            progress.mark_stage("handoff")
            try:
                manifest = _write_handoff(
                    config,
                    output_dir,
                    master_csv,
                    master_xlsx,
                    summary["master_rows"],
                    failures,
                    outreach,
                )
                summary["artifacts"]["handoff"] = manifest
            except Exception as exc:
                failures.append(f"handoff: {_redact(str(exc))[:300]}")

    except Exception as exc:
        detail = _redact(f"{type(exc).__name__}: {exc}")[:500]
        failures.append(f"orchestrator: {detail}")
        print(_redact(traceback.format_exc()))

    return _finish(summary, output_dir, failures, started)


async def main() -> int:
    try:
        config = RunConfig.from_env()
        run_root, output_dir = _resolve_paths(config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    original_cwd = Path.cwd()
    try:
        run_root.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(run_root)
        try:
            with _exclusive_run_lock(output_dir / "run.lock"):
                return await _pipeline(config, output_dir)
        except AlreadyRunning as exc:
            print(f"Already running: {exc}", file=sys.stderr)
            return EXIT_ALREADY_RUNNING
    except OSError as exc:
        print(f"OUTPUT ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    finally:
        os.chdir(original_cwd)


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        try:
            progress.mark_failed("KeyboardInterrupt", exit_code=130)
        except Exception:
            pass
        raise SystemExit(130)
    except Exception as exc:
        try:
            progress.mark_failed(_redact(str(exc)), exit_code=EXIT_FAILED)
        except Exception:
            pass
        raise
    raise SystemExit(exit_code)
