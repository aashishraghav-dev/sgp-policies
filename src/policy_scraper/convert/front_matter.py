"""Renders the markdown file that actually lands in the bucket.

Each document is written with a YAML front-matter block. The manifest is
the index an agent searches; the front matter makes a single file
self-describing once it has been retrieved, so provenance survives being
read in isolation.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any

import yaml

_FENCE = "---"


def _scalar(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _scalar(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_scalar(v) for v in value]
    return value


def render_document(markdown: str, metadata: dict[str, Any], *, include_front_matter: bool = True) -> str:
    """Combine metadata and body into the final markdown file."""
    body = markdown.strip() + "\n"
    if not include_front_matter:
        return body

    cleaned = {k: _scalar(v) for k, v in metadata.items() if v is not None and v != {}}
    block = yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True, width=100).strip()
    return f"{_FENCE}\n{block}\n{_FENCE}\n\n{body}"
