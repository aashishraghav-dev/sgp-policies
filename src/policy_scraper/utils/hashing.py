"""Stable fingerprints used for change detection."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

_PREFIX = "sha256:"


def sha256_of_bytes(data: bytes) -> str:
    return _PREFIX + hashlib.sha256(data).hexdigest()


def sha256_of_text(text: str) -> str:
    return sha256_of_bytes(text.encode("utf-8"))


def revision_key(*parts: Any) -> str:
    """Fingerprint the listing-visible signals that indicate a change.

    ``None`` parts are preserved as a distinct marker rather than dropped,
    so a signal appearing or disappearing still changes the key.
    """
    return sha256_of_text("\x1f".join("\x00" if p is None else str(p) for p in parts))


def sha256_of_stream(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return _PREFIX + digest.hexdigest()
