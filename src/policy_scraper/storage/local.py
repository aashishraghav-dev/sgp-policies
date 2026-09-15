"""Filesystem object store.

Mirrors the GCS key layout exactly so a local run is a faithful rehearsal
of a bucket run -- useful for development and for CI without credentials.
"""

from __future__ import annotations

from pathlib import Path

from policy_scraper.config.models import LocalStoreConfig
from policy_scraper.core.errors import StorageError
from policy_scraper.core.models import StoredObject
from policy_scraper.storage.base import ObjectStore
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)


class LocalObjectStore(ObjectStore):
    """Writes objects as files under a base directory."""

    name = "local"

    def __init__(self, config: LocalStoreConfig) -> None:
        self._base = Path(config.base_path).expanduser().resolve()
        self._base.mkdir(parents=True, exist_ok=True)
        logger.info("Local object store rooted at %s", self._base)

    def _resolve(self, path: str) -> Path:
        target = (self._base / path.strip("/")).resolve()
        # Guard against a crafted key escaping the base directory.
        if not target.is_relative_to(self._base):
            raise StorageError(f"Refusing to access {path!r}: resolves outside the store root.")
        return target

    def write_text(self, path: str, content: str, *, content_type: str = "text/plain") -> StoredObject:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        try:
            target.write_bytes(data)
        except OSError as exc:
            raise StorageError(f"Failed to write {path}: {exc}") from exc
        return StoredObject(path=path, uri=target.as_uri(), byte_size=len(data))

    def read_text(self, path: str) -> str | None:
        target = self._resolve(path)
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Failed to read {path}: {exc}") from exc

    def exists(self, path: str) -> bool:
        return self._resolve(path).is_file()

    def list_paths(self, prefix: str) -> list[str]:
        root = self._resolve(prefix)
        if not root.is_dir():
            return []
        return sorted(
            p.relative_to(self._base).as_posix() for p in root.rglob("*") if p.is_file()
        )

    def delete(self, path: str) -> bool:
        target = self._resolve(path)
        if not target.is_file():
            return False
        target.unlink()
        return True

    def uri(self, path: str) -> str:
        return self._resolve(path).as_uri()
