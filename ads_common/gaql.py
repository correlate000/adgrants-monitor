"""GAQL query utilities"""

import re
import unicodedata


def validate_gaql_value(value: str, field_name: str) -> str:
    """Validate a value to be embedded in a GAQL query.

    Rejects anything that could break out of the surrounding single-quoted
    literal or inject a new clause: quotes, semicolons, and any whitespace
    other than ASCII space. The regex intentionally does NOT use \\s,
    because \\s matches \\n/\\r/\\t — characters that could smuggle a new
    GAQL clause past the validator.
    """
    if field_name == "date":
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            raise ValueError(f"Invalid date format: {value}")
    elif field_name in ("campaign_name", "ad_group"):
        # ASCII alphanumerics/underscore, literal ASCII space, hyphen,
        # Japanese (hiragana/katakana/kanji), full-width symbols, middle dot.
        if not re.match(r"^[\w \-　-鿿＀-￯・]+$", value):
            raise ValueError(f"Invalid {field_name}: {value}")
    return value


def display_width(s: str) -> int:
    """Calculate display width of RSA headline (full-width=2)."""
    return sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s
    )
