"""Run reporting.

Structured rather than log-scraped, so the future scheduler can assert on
it and alert when, say, a source suddenly returns zero documents.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter

from pydantic import BaseModel, Field

from policy_scraper.core.models import ChangeType, DocumentOutcome


class CategoryReport(BaseModel):
    source: str
    category_key: str
    category_display_name: str
    discovered: int = 0
    outcomes: list[DocumentOutcome] = Field(default_factory=list)
    error: str | None = None

    @property
    def counts(self) -> Counter[str]:
        return Counter(outcome.change.value for outcome in self.outcomes)


class SourceReport(BaseModel):
    source: str
    categories_discovered: int = 0
    categories_scraped: int = 0
    category_reports: list[CategoryReport] = Field(default_factory=list)
    error: str | None = None

    @property
    def outcomes(self) -> list[DocumentOutcome]:
        return [o for c in self.category_reports for o in c.outcomes]


class RunReport(BaseModel):
    started_at: _dt.datetime
    finished_at: _dt.datetime | None = None
    dry_run: bool = False
    storage_backend: str = ""
    storage_root: str = ""
    source_reports: list[SourceReport] = Field(default_factory=list)

    @property
    def outcomes(self) -> list[DocumentOutcome]:
        return [o for s in self.source_reports for o in s.outcomes]

    @property
    def counts(self) -> Counter[str]:
        return Counter(o.change.value for o in self.outcomes)

    @property
    def failed(self) -> list[DocumentOutcome]:
        return [o for o in self.outcomes if o.change is ChangeType.FAILED]

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or _dt.datetime.now(_dt.timezone.utc)
        return round((end - self.started_at).total_seconds(), 2)

    @property
    def ok(self) -> bool:
        """True when nothing failed at document or source level."""
        return not self.failed and all(s.error is None for s in self.source_reports)

    def totals(self) -> dict[str, int | bool]:
        """Flat counters, for the ``run_end`` metrics event."""
        counts = self.counts
        return {
            "documents": len(self.outcomes),
            "sources": len(self.source_reports),
            "created": counts.get("created", 0),
            "updated": counts.get("updated", 0),
            "unchanged": counts.get("unchanged", 0),
            "skipped": counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
            "ok": self.ok,
        }

    def summary(self) -> str:
        """Human-readable digest, printed at the end of a run."""
        counts = self.counts
        lines = [
            "",
            "=" * 78,
            f"SCRAPE SUMMARY{'  (DRY RUN - nothing written)' if self.dry_run else ''}",
            "=" * 78,
            f"  Storage      : {self.storage_backend} -> {self.storage_root}",
            f"  Duration     : {self.duration_seconds}s",
            f"  Documents    : {len(self.outcomes)} "
            f"(created={counts.get('created', 0)} updated={counts.get('updated', 0)} "
            f"unchanged={counts.get('unchanged', 0)} skipped={counts.get('skipped', 0)} "
            f"failed={counts.get('failed', 0)})",
            "-" * 78,
        ]

        for source in self.source_reports:
            status = f"ERROR: {source.error}" if source.error else ""
            lines.append(
                f"  {source.source}  "
                f"[{source.categories_scraped}/{source.categories_discovered} categories] {status}"
            )
            for category in source.category_reports:
                counts = category.counts
                detail = (
                    f"created={counts.get('created', 0)} "
                    f"updated={counts.get('updated', 0)} "
                    f"unchanged={counts.get('unchanged', 0)} "
                    f"failed={counts.get('failed', 0)}"
                )
                suffix = f"  ERROR: {category.error}" if category.error else ""
                lines.append(
                    f"      - {category.category_display_name:<42} "
                    f"discovered={category.discovered:<4} {detail}{suffix}"
                )

        if self.failed:
            lines += ["-" * 78, "  FAILURES:"]
            for outcome in self.failed:
                lines.append(f"      ! [{outcome.source}] {outcome.title[:60]}: {outcome.error}")

        lines.append("=" * 78)
        return "\n".join(lines)
