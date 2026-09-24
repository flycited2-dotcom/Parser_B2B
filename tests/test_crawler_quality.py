import asyncio
import csv

from parsers import crawler
from utils.quality import VK_QUARANTINE_FLAGS


def _write_seed_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source", "website", "quality_flags"],
            delimiter=";",
        )
        writer.writeheader()
        writer.writerows(rows)


def test_crawler_filters_every_weak_vk_seed_but_keeps_trusted_rows(tmp_path):
    rows = [
        {
            "source": "VK Groups",
            "website": f"https://weak-{index}.example",
            "quality_flags": flag,
        }
        for index, flag in enumerate(sorted(VK_QUARANTINE_FLAGS))
    ]
    rows.extend([
        {
            "source": "VK", "website": "https://manual.example",
            "quality_flags": "manual_review|vk_manual_business_segment",
        },
        {
            "source": "OSM", "website": "https://trusted.example",
            "quality_flags": "",
        },
        {
            "source": "OSM", "website": "https://weak-donor.example",
            "quality_flags": "manual_review|vk_weak_website_donor",
        },
        {
            "source": "OSM", "website": "https://stale-noise.example",
            "quality_flags": "vk_accommodation_primary",
        },
    ])
    path = tmp_path / "seeds.csv"
    _write_seed_csv(path, rows)

    assert crawler._load_seeds_from_csv(str(path)) == [
        "https://manual.example",
        "https://trusted.example",
    ]


def test_weak_only_crawler_run_stops_before_network(monkeypatch, tmp_path):
    path = tmp_path / "weak.csv"
    _write_seed_csv(path, [{
        "source": "VK", "website": "https://hotel.example",
        "quality_flags": "vk_accommodation_primary",
    }])
    monkeypatch.setattr(crawler, "_latest_csv", lambda: str(path))

    class NetworkMustNotStart:
        def __init__(self, *args, **kwargs):
            raise AssertionError("crawler network started for weak-only input")

    monkeypatch.setattr(crawler.aiohttp, "TCPConnector", NetworkMustNotStart)
    asyncio.run(crawler.run(None))


def test_quality_policy_requires_actual_vk_source_for_quarantine():
    # A strong source can carry an audit flag in provenance without becoming a
    # weak standalone VK observation.  Field-specific website donation is the
    # separate crawler gate tested above.
    from utils.quality import is_master_anchor

    assert is_master_anchor({
        "source": "OSM", "quality_flags": "vk_accommodation_primary",
    })
    assert not is_master_anchor({
        "source": "VK", "quality_flags": "vk_accommodation_primary",
    })
