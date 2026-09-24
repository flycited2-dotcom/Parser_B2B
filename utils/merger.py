"""Build a canonical master from per-source observation CSV files."""
from __future__ import annotations

import csv
import glob
import os
import shutil
from collections import OrderedDict

from utils.csv_safety import neutralize_csv_formula
from utils.entity_resolution import (
    CONTACT_FIELDS,
    MULTI_CONTACT_FIELDS,
    canonicalize_cluster,
    cluster_rows,
    observation_identity,
    provenance_for_rows,
)
from utils.quality import is_master_anchor, row_flags
from utils.storage import FIELDS


_HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(os.path.dirname(_HERE), "output")

# Public alias retained for callers/tests from the original implementation.
FIELDNAMES = FIELDS

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _restore_csv_text(value: object) -> str:
    """Undo our serialization marker for internal matching/normalization."""
    text = str(value or "")
    if text.startswith("'") and text[1:].lstrip().startswith(_FORMULA_PREFIXES):
        return text[1:]
    return text


def _csv_safe_row(row: dict) -> dict:
    return {
        field: neutralize_csv_formula(str(row.get(field) or ""))
        for field in FIELDNAMES
    }


def _stage_quarantine(path: str, clusters: list[list[dict]]) -> str:
    """Write a complete quarantine candidate without publishing it yet."""
    tmp = path + ".tmp"
    headers = [*FIELDNAMES, "quarantine_reason"]
    try:
        with open(tmp, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=headers,
                delimiter=";",
                extrasaction="ignore",
                restval="",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            for cluster in clusters:
                row = canonicalize_cluster(cluster, FIELDNAMES)
                row["quarantine_reason"] = "vk_missing_primary_food_signal"
                writer.writerow({
                    header: neutralize_csv_formula(str(row.get(header) or ""))
                    for header in headers
                })
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return tmp


def _stage_master(path: str, rows: list[dict]) -> str:
    """Write a complete master candidate without publishing it yet."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=FIELDNAMES,
                delimiter=";",
                extrasaction="ignore",
                restval="",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            writer.writerows(_csv_safe_row(row) for row in rows)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return tmp


def _publish_pair(staged: list[tuple[str, str]]) -> None:
    """Publish related CSVs together, rolling back either on replace failure."""
    backups: dict[str, str] = {}
    committed: set[str] = set()
    try:
        # No public file changes until every candidate and rollback copy exists.
        for target, _tmp in staged:
            if os.path.exists(target):
                backup = target + ".rollback"
                shutil.copyfile(target, backup)
                backups[target] = backup
        for target, tmp in staged:
            os.replace(tmp, target)
            committed.add(target)
    except Exception:
        for target, _tmp in reversed(staged):
            backup = backups.get(target)
            try:
                if backup and os.path.exists(backup):
                    os.replace(backup, target)
                elif target in committed and os.path.exists(target):
                    os.remove(target)
            except OSError:
                # Preserve the original publishing error.  A leftover rollback
                # file remains available for manual recovery in this rare case.
                pass
        raise
    finally:
        for target, tmp in staged:
            for leftover in (tmp, backups.get(target, "")):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass


def _dedup_key(row: dict) -> str:
    """Backward-compatible name for the per-source observation key."""
    return observation_identity(row)


def _canonicalize_master_cluster(cluster: list[dict]) -> dict:
    anchors = [row for row in cluster if is_master_anchor(row)]
    weak_vk_rows = [row for row in cluster if not is_master_anchor(row)]
    if not weak_vk_rows:
        return canonicalize_cluster(cluster, FIELDNAMES)

    # Canonical business identity and preferred values come from trusted
    # anchors.  Weak VK observations can fill a missing contact, but can never
    # replace an anchor contact or expand its alternates silently.
    result = canonicalize_cluster(anchors, FIELDNAMES)
    full = canonicalize_cluster(cluster, FIELDNAMES)
    weak = canonicalize_cluster(weak_vk_rows, FIELDNAMES)
    donated_fields: list[str] = []
    reverse_multi = {
        primary: multi for multi, primary in MULTI_CONTACT_FIELDS.items()
    }
    for field in CONTACT_FIELDS:
        if result.get(field) or not weak.get(field):
            continue
        result[field] = weak[field]
        multi_field = reverse_multi.get(field)
        if multi_field:
            result[multi_field] = weak.get(multi_field) or weak[field]
        donated_fields.append(field)

    # Retain full audit metadata without allowing weak rows to choose the
    # representative source, category, confidence, or primary contacts.
    for field in (
        "entity_id", "sources", "first_seen", "last_seen",
        "matched_query", "provenance",
    ):
        result[field] = full.get(field, result.get(field, ""))

    flags = set().union(*(row_flags(row) for row in anchors))
    flags.add("vk_weak_observation_linked")
    if donated_fields:
        flags.update({"vk_weak_contact_donor", "manual_review"})
        flags.update(f"vk_weak_{field}_donor" for field in donated_fields)
    result["quality_flags"] = "|".join(sorted(flags))
    return result


def _selected_result_files(output_dir: str) -> tuple[list[str], int]:
    pattern = os.path.join(output_dir, "result_*.csv")
    raw_files = sorted(
        [path for path in glob.glob(pattern) if "enriched" not in os.path.basename(path)],
        key=os.path.getmtime,
    )
    enriched_files = sorted(
        glob.glob(os.path.join(output_dir, "*enriched*.csv")),
        key=os.path.getmtime,
    )
    enriched_bases = {
        os.path.basename(path).replace("_enriched", "")
        for path in enriched_files
    }
    selected_raw = [
        path for path in raw_files
        if os.path.basename(path) not in enriched_bases
    ]
    return selected_raw + enriched_files, len(raw_files)


def _normalize_row(row: dict) -> dict:
    normalized = {
        field: _restore_csv_text(row.get(field))
        for field in FIELDNAMES
    }
    normalized["observation_id"] = observation_identity(normalized)
    normalized["sources"] = normalized.get("sources") or normalized.get("source", "")
    fallback_seen = normalized.get("parsed_at", "")
    normalized["first_seen"] = normalized.get("first_seen") or fallback_seen
    normalized["last_seen"] = normalized.get("last_seen") or fallback_seen
    normalized["provenance"] = provenance_for_rows([normalized])
    return normalized


def _merge_refresh(existing: dict, incoming: dict) -> dict:
    """Combine repeated observations across runs without freezing stale data."""
    merged = canonicalize_cluster([existing, incoming], FIELDNAMES)
    # It remains the same source observation until entity clustering below.
    merged["observation_id"] = existing["observation_id"]
    latest = max(
        (existing, incoming),
        key=lambda row: str(row.get("last_seen") or row.get("parsed_at") or ""),
    )
    for field in ("source", "source_id", "source_url"):
        merged[field] = latest.get(field) or existing.get(field) or incoming.get(field) or ""
    # Empty is authoritative for flags: a later parser version may
    # legitimately clear a stale classification from the same observation.
    merged["quality_flags"] = str(latest.get("quality_flags") or "")
    for field in ("confidence", "raw_category", "category", "client_type"):
        merged[field] = latest.get(field) or existing.get(field) or incoming.get(field) or ""
    first_seen = sorted(
        str(row.get("first_seen") or "") for row in (existing, incoming)
        if row.get("first_seen")
    )
    last_seen = sorted(
        str(row.get("last_seen") or "") for row in (existing, incoming)
        if row.get("last_seen")
    )
    merged["first_seen"] = first_seen[0] if first_seen else ""
    merged["last_seen"] = last_seen[-1] if last_seen else ""
    merged["provenance"] = provenance_for_rows([existing, incoming])
    return merged


def build_master(output_dir: str = OUTPUT_DIR) -> str:
    """Merge result files into one conservative, provenance-rich entity master."""
    selected_files, raw_count = _selected_result_files(output_dir)
    observations: OrderedDict[str, dict] = OrderedDict()
    for filepath in selected_files:
        _load_csv_into(filepath, observations)

    clusters = cluster_rows(list(observations.values()))
    included_clusters: list[list[dict]] = []
    excluded: list[list[dict]] = []
    for cluster in clusters:
        target = included_clusters if any(is_master_anchor(row) for row in cluster) else excluded
        target.append(cluster)
    excluded_clusters = len(excluded)
    excluded_observations = sum(len(cluster) for cluster in excluded)
    canonical_rows = [
        _canonicalize_master_cluster(cluster)
        for cluster in included_clusters
    ]
    master_path = os.path.join(output_dir, "master_all.csv")
    quarantine_path = os.path.join(output_dir, "master_quarantine.csv")
    os.makedirs(output_dir, exist_ok=True)
    master_tmp = _stage_master(master_path, canonical_rows)
    try:
        quarantine_tmp = _stage_quarantine(quarantine_path, excluded)
    except Exception:
        if os.path.exists(master_tmp):
            os.remove(master_tmp)
        raise
    _publish_pair([
        (master_path, master_tmp),
        (quarantine_path, quarantine_tmp),
    ])

    print(
        f"[merger] master_all.csv: {len(canonical_rows)} entities from "
        f"{len(observations)} observations / {raw_count} raw files; "
        f"quarantined={excluded_clusters} entities/{excluded_observations} observations "
        f"-> {quarantine_path}"
    )
    return master_path


def _load_csv_into(filepath: str, observations: OrderedDict) -> None:
    try:
        with open(filepath, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            for raw_row in reader:
                row = _normalize_row(raw_row)
                key = row["observation_id"]
                if key not in observations:
                    observations[key] = row
                else:
                    observations[key] = _merge_refresh(observations[key], row)
    except Exception as exc:
        print(f"[merger] error reading {filepath}: {exc}")


def build_master_xlsx(output_dir: str = OUTPUT_DIR) -> tuple[str, str]:
    """Build master_all.csv and master_all.xlsx. Returns (csv_path, xlsx_path)."""
    csv_path = build_master(output_dir)
    xlsx_path = os.path.join(output_dir, "master_all.xlsx")
    try:
        from utils.excel_export import build_xlsx

        result = build_xlsx(csv_path, xlsx_path)
        xlsx_path = result or xlsx_path
    except Exception as exc:
        print(f"[merger] xlsx error: {exc}")
        xlsx_path = ""
    return csv_path, xlsx_path
