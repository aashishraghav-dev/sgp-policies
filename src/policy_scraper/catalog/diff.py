"""Change detection.

The rule the pipeline follows, before spending anything on a download or
an OCR pass:

  * id not in manifest              -> CREATED
  * id present, revision_key equal  -> UNCHANGED (skip entirely)
  * id present, revision_key differs-> UPDATED

``revision_key`` is built by the connector from signals visible on the
listing page. Both sources give us a strong one for free:

  RBI   the PDF filename embeds a content hash
        (``.../PDFs/378MD65D46FCC...PDF``), and amended directions
        additionally carry "(Updated as on <date>)" in the title.
  NPCI  the Strapi upload URL carries a content hash suffix
        (``UPI_I_OC_No_186_A_..._e695625b85.pdf``).

Where an UPDATED document turns out to have identical markdown after
conversion, the pipeline downgrades it to UNCHANGED, so a cosmetic
republish does not churn the bucket.
"""

from __future__ import annotations

from dataclasses import dataclass

from policy_scraper.catalog.models import CategoryManifest, ManifestEntry
from policy_scraper.core.models import ChangeType, DocumentRef, UpdateStrategy


@dataclass(frozen=True)
class DocumentDecision:
    """What to do with one discovered document, and where to put it.

    ``version`` and ``path_version`` are deliberately distinct.  The former
    always counts how many times we have seen this document change and is
    recorded in the manifest.  The latter selects the object key, and under
    REPLACE_IN_PLACE it is pinned to 1 so an amended document overwrites
    itself at a stable, predictable path.
    """

    ref: DocumentRef
    change: ChangeType
    previous: ManifestEntry | None
    version: int
    path_version: int

    @property
    def needs_work(self) -> bool:
        return self.change in (ChangeType.CREATED, ChangeType.UPDATED)


def decide(
    ref: DocumentRef,
    manifest: CategoryManifest,
    *,
    update_strategy: UpdateStrategy,
    force: bool = False,
) -> DocumentDecision:
    """Classify a discovered document against what we already hold."""
    previous = manifest.by_id().get(ref.document_id)

    if previous is None:
        return DocumentDecision(ref, ChangeType.CREATED, None, version=1, path_version=1)

    if not force and previous.revision_key == ref.revision_key:
        return DocumentDecision(
            ref,
            ChangeType.UNCHANGED,
            previous,
            version=previous.version,
            path_version=_path_version(previous.version, update_strategy),
        )

    # The document changed (or we were told to ignore the catalog).
    #
    # REPLACE_IN_PLACE overwrites the original key -- correct for RBI,
    # where an amended Master Direction is still the same instrument and
    # consumers should always resolve the current text at one address.
    #
    # NEW_VERSION writes to a fresh key, preserving the old object --
    # correct for NPCI, where a re-issued circular is a distinct artefact.
    next_version = previous.version + 1
    return DocumentDecision(
        ref,
        ChangeType.UPDATED,
        previous,
        version=next_version,
        path_version=_path_version(next_version, update_strategy),
    )


def _path_version(version: int, update_strategy: UpdateStrategy) -> int:
    return 1 if update_strategy is UpdateStrategy.REPLACE_IN_PLACE else version
