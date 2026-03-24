"""GAQL query utilities"""

import re
import unicodedata


def validate_gaql_value(value: str, field_name: str) -> str:
    """Validate a value to be embedded in a GAQL query."""
    if field_name == "date":
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            raise ValueError(f"Invalid date format: {value}")
    elif field_name in ("campaign_name", "ad_group"):
        # Allow Japanese (hiragana/katakana/kanji), alphanumeric, space, hyphen, middle dot, full-width symbols
        if not re.match(r"^[\w\s\-\u3000-\u9FFF\uFF00-\uFFEF・]+$", value):
            raise ValueError(f"Invalid {field_name}: {value}")
    return value


def display_width(s: str) -> int:
    """Calculate display width of RSA headline (full-width=2)."""
    return sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s
    )
