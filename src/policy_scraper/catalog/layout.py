"""The single source of truth for object keys.

Every path in the bucket is produced here, so the layout can be changed in
one place rather than hunted through the codebase.

    <root>/                                  e.g. policies/
      _index.yaml                            run-level roll-up of all manifests
      <source>/                              rbi, npci
        <category>/                          commercial-banks, upi
          _manifest.yaml                     metadata for every doc below
          documents/
            <title-slug>-<doc-id>.md
            <title-slug>-<doc-id>.v2.md      only for NEW_VERSION sources
"""

from __future__ import annotations

from dataclasses import dataclass

from policy_scraper.storage.base import join_path

MANIFEST_FILENAME = "_manifest.yaml"
INDEX_FILENAME = "_index.yaml"
DOCUMENTS_DIR = "documents"


@dataclass(frozen=True)
class StorageLayout:
    """Builds object keys beneath a configured root prefix."""

    root_prefix: str = "policies"

    def index_path(self) -> str:
        return join_path(self.root_prefix, INDEX_FILENAME)

    def source_prefix(self, source: str) -> str:
        return join_path(self.root_prefix, source)

    def category_prefix(self, source: str, category_key: str) -> str:
        return join_path(self.source_prefix(source), category_key)

    def manifest_path(self, source: str, category_key: str) -> str:
        return join_path(self.category_prefix(source, category_key), MANIFEST_FILENAME)

    def documents_prefix(self, source: str, category_key: str) -> str:
        return join_path(self.category_prefix(source, category_key), DOCUMENTS_DIR)

    def document_path(self, source: str, category_key: str, slug: str, *, version: int = 1) -> str:
        """Key for one markdown document.

        Version 1 has no suffix so REPLACE_IN_PLACE sources keep a stable,
        predictable key across amendments. NEW_VERSION sources get
        ``.v2``, ``.v3`` ... appended, preserving prior revisions.
        """
        filename = f"{slug}.md" if version <= 1 else f"{slug}.v{version}.md"
        return join_path(self.documents_prefix(source, category_key), filename)
