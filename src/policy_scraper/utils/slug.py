"""Path-safe identifier generation."""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DASHES = re.compile(r"-{2,}")


def slugify(value: str, *, max_length: int = 120) -> str:
    """Convert arbitrary text into a lowercase, dash-separated slug.

    Truncation happens on a word boundary where possible so slugs stay
    readable, and the result is never empty (callers use it in paths).
    """
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _DASHES.sub("-", _NON_ALNUM.sub("-", ascii_only)).strip("-")

    if len(slug) > max_length:
        slug = slug[:max_length].rsplit("-", 1)[0] or slug[:max_length]

    return slug.strip("-") or "untitled"
