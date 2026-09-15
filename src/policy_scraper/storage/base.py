"""Object-store abstraction.

Both backends address objects by a POSIX-style key, so the bucket layout
and the local test layout are byte-for-byte identical. That is what lets a
run be verified on disk and then repointed at GCS with a config flag.
"""

from __future__ import annotations

import abc

from policy_scraper.core.models import StoredObject


class ObjectStore(abc.ABC):
    """Key/value store for markdown documents and YAML manifests."""

    name: str = "store"

    @abc.abstractmethod
    def write_text(self, path: str, content: str, *, content_type: str = "text/plain") -> StoredObject:
        """Create or overwrite the object at ``path``."""

    @abc.abstractmethod
    def read_text(self, path: str) -> str | None:
        """Return the object's contents, or ``None`` if it does not exist."""

    @abc.abstractmethod
    def exists(self, path: str) -> bool: ...

    @abc.abstractmethod
    def list_paths(self, prefix: str) -> list[str]:
        """List every object key beneath ``prefix``."""

    @abc.abstractmethod
    def delete(self, path: str) -> bool:
        """Remove an object. Returns whether it existed."""

    @abc.abstractmethod
    def uri(self, path: str) -> str:
        """Fully-qualified address of ``path``, for logs and manifests."""

    def close(self) -> None:
        """Release resources. Idempotent."""

    def __enter__(self) -> "ObjectStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def join_path(*parts: str) -> str:
    """Join key segments with '/', dropping empties and duplicate slashes."""
    cleaned = [str(p).strip("/") for p in parts if p and str(p).strip("/")]
    return "/".join(cleaned)
