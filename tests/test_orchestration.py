import asyncio
import csv
import json
import os
from pathlib import Path
from types import SimpleNamespace

import main


CONFIG_KEYS = (
    "DRY_RUN", "DRY_RUN_ID", "DRY_RUN_ROOT", "DRY_RUN_ENRICHMENT",
    "HEADLESS", "ONLY_SOURCE", "SKIP_ENRICHMENT", "AUTO_NOTIFY",
    "AUTO_UPLOAD", "ALLOW_PARTIAL_DELIVERY", "FAIL_FAST", "MAX_SOURCES",
    "MAX_CITIES", "MAX_QUERIES_PER_SOURCE", "MAX_ITEMS_PER_SOURCE",
    "MIN_RECORDS_TOTAL", "MIN_RECORDS_OSM", "MIN_RECORDS_VK",
    "MIN_RECORDS_YANDEX", "MIN_RECORDS_CRAWLER", "CRITICAL_SOURCES",
    "MIN_FREE_DISK_MB", "VK_TOKEN", "TG_BOT_TOKEN", "TG_CHAT_ID",
    "GDRIVE_FOLDER_ID", "GDRIVE_TOKEN", "GDRIVE_SERVICE_ACCOUNT",
    "ENRICH_MAX_SITES",
    "YANDEX_CITY_OFFSET", "YANDEX_QUERY_OFFSET", "YANDEX_RESULTS_PER_QUERY",
    "YANDEX_MAX_SCROLLS", "YANDEX_MAX_DETAIL_REQUESTS",
    "YANDEX_MAX_CONSECUTIVE_FAILURES", "YANDEX_MAX_CAPTCHA_HITS",
    "YANDEX_FAILURE_WINDOW", "YANDEX_FAILURE_RATIO", "YANDEX_MAX_EMPTY_RATIO",
)


def _clean_config(monkeypatch):
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_dry_run_is_bounded_and_disables_delivery(monkeypatch, tmp_path):
    _clean_config(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("DRY_RUN_ID", "canary")
    monkeypatch.setenv("DRY_RUN_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTO_NOTIFY", "1")
    monkeypatch.setenv("AUTO_UPLOAD", "1")
    monkeypatch.setenv("GDRIVE_FOLDER_ID", "configured-but-disabled")

    config = main.RunConfig.from_env()
    run_root, output_dir = main._resolve_paths(config)

    assert config.dry_run is True
    assert config.auto_notify is False
    assert config.auto_upload is False
    assert config.skip_enrichment is True
    assert config.max_sources == 1
    assert config.max_cities == 1
    assert config.max_queries_per_source == 1
    assert config.max_items_per_source == 25
    assert run_root == (tmp_path / "canary").resolve()
    assert output_dir == run_root / "output"


def test_invalid_source_returns_config_exit_without_network(monkeypatch):
    _clean_config(monkeypatch)
    monkeypatch.setenv("ONLY_SOURCE", "not-a-source")
    assert asyncio.run(main.main()) == main.EXIT_CONFIG


def test_preflight_requires_vk_token_for_vk_canary(monkeypatch, tmp_path):
    _clean_config(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("ONLY_SOURCE", "vk")
    config = main.RunConfig.from_env()
    selected = main._selected_sources(config)

    errors, _warnings = main.preflight(config, selected, tmp_path)

    assert any("VK_TOKEN" in error for error in errors)


def test_preflight_rejects_invalid_yandex_batch_before_browser(monkeypatch, tmp_path):
    _clean_config(monkeypatch)
    monkeypatch.setenv("ONLY_SOURCE", "yandex")
    monkeypatch.setenv("YANDEX_CITY_OFFSET", "999")
    config = main.RunConfig.from_env()

    errors, warnings = main.preflight(
        config, main._selected_sources(config), tmp_path
    )

    assert any("YANDEX_CITY_OFFSET" in error for error in errors)
    assert any("Yandex limits" in warning for warning in warnings)


def test_preflight_rejects_unparseable_yandex_limit(monkeypatch, tmp_path):
    _clean_config(monkeypatch)
    monkeypatch.setenv("ONLY_SOURCE", "yandex")
    monkeypatch.setenv("YANDEX_MAX_DETAIL_REQUESTS", "many")
    config = main.RunConfig.from_env()

    errors, _warnings = main.preflight(
        config, main._selected_sources(config), tmp_path
    )

    assert any("YANDEX_MAX_DETAIL_REQUESTS" in error for error in errors)


def test_source_limit_adapter_restores_module_and_caps_items(monkeypatch):
    _clean_config(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("MAX_CITIES", "1")
    monkeypatch.setenv("MAX_QUERIES_PER_SOURCE", "1")
    monkeypatch.setenv("MAX_ITEMS_PER_SOURCE", "2")
    config = main.RunConfig.from_env()

    saved = []
    module = SimpleNamespace(
        CITIES=["A", "B"],
        QUERIES=["q1", "q2"],
        EXTRA_QUERIES_GLOBAL=["extra"],
        save_item=lambda item: saved.append(item) or True,
    )

    async def runner(_context):
        assert module.CITIES == ["A"]
        assert module.QUERIES == ["q1"]
        assert module.EXTRA_QUERIES_GLOBAL == []
        for number in range(4):
            module.save_item({"n": number})

    spec = main.SourceSpec("Dummy", "osm", runner, module)
    stats = {"observed": 0, "limited": 0}
    original_save = module.save_item

    with main._source_limits(spec, config, stats):
        asyncio.run(runner(None))

    assert saved == [{"n": 0}, {"n": 1}]
    assert stats == {"observed": 4, "limited": 2}
    assert module.CITIES == ["A", "B"]
    assert module.QUERIES == ["q1", "q2"]
    assert module.EXTRA_QUERIES_GLOBAL == ["extra"]
    assert module.save_item is original_save


def test_dry_run_resolved_caps_reach_environment_reading_parser(monkeypatch):
    _clean_config(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    config = main.RunConfig.from_env()
    observed = {}
    module = SimpleNamespace(save_item=lambda _item: True)

    async def runner(_context):
        observed["cities"] = int(os.environ["MAX_CITIES"])
        observed["queries"] = int(os.environ["MAX_QUERIES_PER_SOURCE"])
        observed["items"] = int(os.environ["MAX_ITEMS_PER_SOURCE"])
        observed["detail_limit"] = main.yandex_maps._new_run_state(
            observed["items"]
        ).detail_limit

    spec = main.SourceSpec("Yandex", "yandex", runner, module)
    stats = {"observed": 0, "limited": 0}
    with main._source_limits(spec, config, stats):
        asyncio.run(runner(None))

    assert observed == {
        "cities": 1,
        "queries": 1,
        "items": 25,
        "detail_limit": 25,
    }
    assert "MAX_CITIES" not in os.environ
    assert "MAX_QUERIES_PER_SOURCE" not in os.environ
    assert "MAX_ITEMS_PER_SOURCE" not in os.environ


def test_empty_critical_source_is_failed(monkeypatch):
    _clean_config(monkeypatch)
    monkeypatch.setenv("ONLY_SOURCE", "osm")
    monkeypatch.setattr(main.storage, "total", lambda: 0)
    module = SimpleNamespace(save_item=lambda item: True)

    async def empty_runner(_context):
        return None

    spec = main.SourceSpec("OSM", "osm", empty_runner, module)
    config = main.RunConfig.from_env()
    result, error = asyncio.run(main._run_one_source(spec, None, config))

    assert result["status"] == "failed"
    assert result["observed"] == 0
    assert error and "minimum" in error


def test_handoff_is_atomic_idempotent_and_never_auto_approved(monkeypatch, tmp_path):
    _clean_config(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("DRY_RUN_ID", "handoff-test")
    config = main.RunConfig.from_env()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    master = output_dir / "master_all.csv"
    with master.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["city", "name"])
        writer.writerow(["Ялта", "Тест"])
    quarantine = output_dir / "master_quarantine.csv"
    with quarantine.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["name", "quarantine_reason"])
        writer.writerow(["Шум", "vk_missing_primary_food_signal"])

    manifest = main._write_handoff(
        config, output_dir, str(master), "", 1, failures=[]
    )
    latest = json.loads((output_dir / "handoff" / "latest.json").read_text(encoding="utf-8"))

    assert manifest["state"] == "dry_run_review"
    assert manifest["schema_version"] == 3
    assert manifest["approved_for_send"] is False
    assert manifest["auto_send_allowed"] is False
    assert latest["idempotency_key"] == manifest["idempotency_key"]
    assert Path(manifest["master_csv"]).is_file()
    immutable_quarantine = Path(manifest["quarantine"]["csv"])
    assert immutable_quarantine.is_file()
    assert manifest["quarantine"]["row_count"] == 1
    assert manifest["quarantine"]["automation_eligible"] is False
    assert main._sha256(immutable_quarantine) == manifest["quarantine"]["sha256"]
    immutable_bytes = immutable_quarantine.read_bytes()
    quarantine.write_text("changed\n", encoding="utf-8")
    assert immutable_quarantine.read_bytes() == immutable_bytes
    assert not list((output_dir / "handoff").glob("*.tmp"))


def test_process_lock_rejects_a_second_writer(tmp_path):
    lock_path = tmp_path / "output" / "run.lock"
    with main._exclusive_run_lock(lock_path):
        try:
            with main._exclusive_run_lock(lock_path):
                raise AssertionError("second lock unexpectedly succeeded")
        except main.AlreadyRunning:
            pass

    # The descriptor lock is released after the first context exits.
    with main._exclusive_run_lock(lock_path):
        pass
    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert metadata["pid"] > 0
