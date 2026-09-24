"""Shared quality policy for broad-source observations.

VK is intentionally collected broadly, but rows without a primary business
segment signal (or that look like community noise) must not independently
enter the working master, seed the crawler, or become eligible for outreach.
Keeping this policy in one module prevents the pipeline stages from drifting
apart.
"""
from __future__ import annotations


VK_QUARANTINE_FLAGS = frozenset({
    "vk_no_primary_segment_signal",
    "vk_noise_primary",
})

VK_RELEVANCE_FLAGS = frozenset({
    *VK_QUARANTINE_FLAGS,
    "manual_review",
})


def row_flags(row: dict) -> set[str]:
    """Return normalized pipe-separated quality flags for an observation."""
    return {
        flag.strip().casefold()
        for flag in str(row.get("quality_flags") or "").split("|")
        if flag.strip()
    }


def is_weak_vk_candidate(row: dict) -> bool:
    """Whether a row is a broad VK candidate lacking a primary segment signal."""
    source = str(row.get("source") or "").strip().casefold()
    return source.startswith("vk") and bool(row_flags(row) & VK_QUARANTINE_FLAGS)


def is_master_anchor(row: dict) -> bool:
    """Whether an observation may independently anchor a canonical entity."""
    return not is_weak_vk_candidate(row)


def can_seed_crawler(row: dict) -> bool:
    """Whether a row's website may be used as a crawler network target."""
    if not is_master_anchor(row):
        return False
    flags = row_flags(row)
    # Also protects a stale pre-quarantine master where a VK flag may already
    # have been copied onto a non-VK representative.
    if flags & VK_QUARANTINE_FLAGS:
        return False
    # An anchored entity may contain other weak donated contacts.  Only a
    # website specifically donated by weak VK evidence is unsafe as a seed.
    return "vk_weak_website_donor" not in flags
