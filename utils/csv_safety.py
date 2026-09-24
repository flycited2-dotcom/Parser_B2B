"""Small helpers for safely exporting untrusted text to spreadsheets."""
from __future__ import annotations


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize_csv_formula(value: str) -> str:
    """Prevent Excel/Sheets from evaluating an untrusted value as a formula.

    A leading apostrophe is the spreadsheet-standard text marker. Whitespace is
    retained, while the first non-space character is used for detection.
    """
    if value is None:
        return ""
    text = str(value)
    candidate = text.lstrip()
    if candidate.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text
