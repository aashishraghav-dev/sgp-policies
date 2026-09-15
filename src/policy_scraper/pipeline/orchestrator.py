"""The scrape pipeline.

Per source, per category, the flow is:

    discover categories -> filter/limit -> discover documents -> diff
    against the manifest -> (only for changed docs) fetch -> convert ->
    store -> update manifest -> refresh the catalog index

Everything expensive sits *after* the diff, so a re-run over unchanged
sources costs one index page per source and nothing else.
"""

from __future__ import annotations

import datetime as _dt
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Iterator

from policy_scraper.catalog.diff import DocumentDecision, decide
from policy_scraper.catalog.layout import StorageLayout
from policy_scraper.catalog.models import CategoryManifest, ManifestEntry
from policy_scraper.catalog.repository import CatalogRepository
from policy_scraper.config.models import AppConfig, SourceConfig
from policy_scraper.convert.front_matter import render_document
from policy_scraper.convert.router import ConverterRouter
from policy_scraper.core.errors import ScraperError
from policy_scraper.core.metrics import NULL_RECORDER, MetricsRecorder
from policy_scraper.core.models import Category, ChangeType, DocumentOutcome
from policy_scraper.fetch.factory import FetcherPool
from policy_scraper.pipeline.report import CategoryReport, RunReport, SourceReport
from policy_scraper.sources import load_connectors
from policy_scraper.sources.base import SourceConnector, build_connector
from policy_scraper.storage.factory import build_object_store
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


class ScrapePipeline:
    """Runs configured sources end to end."""

    def __init__(self, config: AppConfig) -> None:
        load_connectors()  # populate the connector registry
        self.config = config
        self.layout = StorageLayout(root_prefix=config.storage.root_prefix)
        self.store = build_object_store(config.storage)
        self.catalog = CatalogRepository(self.store, self.layout)
        self.converter = ConverterRouter(config.conversion)
        self.metrics = (
            MetricsRecorder(config.run.metrics.path)
            if config.run.metrics.enabled
            else NULL_RECORDER
        )
        self.fetchers = FetcherPool(config.fetching, recorder=self.metrics)

    # ------------------------------------------------------------------ entrypoint

    def run(self, source_names: list[str] | None = None) -> RunReport:
        selected = self._select_sources(source_names)
        report = RunReport(
            started_at=_utc_now(),
            dry_run=self.config.run.dry_run,
            storage_backend=self.store.name,
            storage_root=self.store.uri(self.layout.root_prefix),
        )
        touched: list[CategoryManifest] = []

        self.metrics.emit(
            "run_start",
            sources=[s.name for s in selected],
            dry_run=self.config.run.dry_run,
            force_refetch=self.config.run.force_refetch,
            max_workers=self.config.run.max_workers,
            storage_backend=self.store.name,
            pdf_converter=self.config.conversion.pdf.backend,
        )
        started = time.perf_counter()

        try:
            for source_config in selected:
                source_report, manifests = self._run_source(source_config)
                report.source_reports.append(source_report)
                touched.extend(manifests)

            if touched and not self.config.run.dry_run:
                path = self.catalog.refresh_index(touched)
                logger.info("Updated catalog index: %s", self.store.uri(path))
        finally:
            report.finished_at = _utc_now()
            self.metrics.emit(
                "run_end",
                seconds=round(time.perf_counter() - started, 3),
                **report.totals(),
            )
            self.close()

        return report

    def _select_sources(self, source_names: list[str] | None) -> list[SourceConfig]:
        available = self.config.enabled_sources
        if not source_names:
            return available

        by_name = {s.name: s for s in self.config.sources}
        unknown = [n for n in source_names if n not in by_name]
        if unknown:
            raise ScraperError(
                f"Unknown source(s): {', '.join(unknown)}. "
                f"Configured: {', '.join(sorted(by_name)) or '(none)'}."
            )
        return [by_name[n] for n in source_names]

    # ------------------------------------------------------------------ per source

    def _run_source(self, source_config: SourceConfig) -> tuple[SourceReport, list[CategoryManifest]]:
        report = SourceReport(source=source_config.name)
        manifests: list[CategoryManifest] = []
        logger.info("=== Source: %s (%s) ===", source_config.label, source_config.type)

        try:
            fetcher = self.fetchers.get(source_config.fetcher)
            connector = build_connector(source_config, fetcher)
        except Exception as exc:
            logger.exception("Could not start source %s", source_config.name)
            report.error = str(exc)
            return report, manifests

        try:
            categories = connector.discover_categories()
            report.categories_discovered = len(categories)
            selected = self._select_categories(categories, source_config)
            report.categories_scraped = len(selected)

            logger.info(
                "%s: %d categories discovered, %d selected -> %s",
                source_config.name,
                len(categories),
                len(selected),
                ", ".join(c.display_name for c in selected) or "(none)",
            )

            budget = source_config.limits.max_documents_total
            for category in selected:
                category_report, manifest = self._run_category(
                    connector, source_config, category, budget
                )
                report.category_reports.append(category_report)
                if manifest is not None:
                    manifests.append(manifest)
                if budget is not None:
                    budget -= len([o for o in category_report.outcomes])
                    if budget <= 0:
                        logger.info("%s: max_documents_total reached.", source_config.name)
                        break
        except Exception as exc:
            logger.exception("Source %s failed", source_config.name)
            report.error = str(exc)
            if not self.config.run.continue_on_error:
                raise
        finally:
            connector.close()

        return report, manifests

    def _select_categories(
        self, categories: list[Category], source_config: SourceConfig
    ) -> list[Category]:
        """Apply the include/exclude filter, then the count cap.

        Discovery always returns everything the site publishes; this is
        where "only N categories for milestone 1" is enforced, purely from
        config. When ``include`` is set, its order is honoured so the
        selection is deterministic rather than dependent on page order.
        """
        matched = [c for c in categories if source_config.categories.matches(c.key)]

        include = source_config.categories.include
        if include:
            rank = {key: index for index, key in enumerate(include)}
            matched.sort(key=lambda c: rank.get(c.key, len(rank)))

        limit = source_config.limits.max_categories
        return matched[:limit] if limit else matched

    # ------------------------------------------------------------------ per category

    def _run_category(
        self,
        connector: SourceConnector,
        source_config: SourceConfig,
        category: Category,
        budget: int | None,
    ) -> tuple[CategoryReport, CategoryManifest | None]:
        report = CategoryReport(
            source=source_config.name,
            category_key=category.key,
            category_display_name=category.display_name,
        )

        manifest = self.catalog.load_manifest(
            source_config.name,
            category.key,
            display_name=category.display_name,
            category_url=category.source_url,
        )

        try:
            refs = list(connector.discover_documents(category))
        except Exception as exc:
            logger.exception("Discovery failed for %s/%s", source_config.name, category.key)
            report.error = str(exc)
            if not self.config.run.continue_on_error:
                raise
            return report, None

        limit = source_config.limits.max_documents_per_category
        if limit:
            refs = refs[:limit]
        if budget is not None:
            refs = refs[:budget]
        report.discovered = len(refs)

        decisions = [
            decide(
                ref,
                manifest,
                update_strategy=source_config.update_strategy,
                force=self.config.run.force_refetch,
            )
            for ref in refs
        ]
        pending = [d for d in decisions if d.needs_work]

        logger.info(
            "%s/%s: %d documents (%d need work, %d unchanged)",
            source_config.name,
            category.key,
            len(decisions),
            len(pending),
            len(decisions) - len(pending),
        )

        for decision in decisions:
            if not decision.needs_work:
                report.outcomes.append(
                    DocumentOutcome(
                        document_id=decision.ref.document_id,
                        title=decision.ref.title,
                        source=source_config.name,
                        category_key=category.key,
                        change=ChangeType.UNCHANGED,
                        storage_path=decision.previous.storage_path if decision.previous else None,
                    )
                )

        category_started = time.perf_counter()
        if pending:
            report.outcomes.extend(
                self._process_documents(connector, source_config, category, manifest, pending)
            )

        if not self.config.run.dry_run:
            self.catalog.save_manifest(manifest)

        self.metrics.emit(
            "category",
            source=source_config.name,
            category=category.key,
            discovered=len(decisions),
            needed_work=len(pending),
            seconds=round(time.perf_counter() - category_started, 3),
            # Namespaced: 'unchanged' is both a decision count and an
            # outcome count, and they are not the same number.
            outcomes={k: v for k, v in report.counts.items()},
        )

        return report, manifest

    def _process_documents(
        self,
        connector: SourceConnector,
        source_config: SourceConfig,
        category: Category,
        manifest: CategoryManifest,
        decisions: list[DocumentDecision],
    ) -> list[DocumentOutcome]:
        """Fetch, convert and store each changed document.

        Concurrency is capped at one for transports that cannot be shared
        across threads (Playwright), which the fetcher declares itself.
        """
        workers = self.config.run.max_workers
        if not connector.fetcher.supports_concurrency:
            workers = 1
            logger.debug("Serialising %s: %s fetcher is single-threaded.", category.key, connector.fetcher.name)
        workers = max(1, min(workers, len(decisions)))

        outcomes: list[DocumentOutcome] = []

        if workers == 1:
            for decision in decisions:
                outcomes.append(
                    self._process_one(connector, source_config, category, manifest, decision)
                )
            return outcomes

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._process_one, connector, source_config, category, manifest, decision
                ): decision
                for decision in decisions
            }
            for future in as_completed(futures):
                outcomes.append(future.result())
        return outcomes

    def _process_one(
        self,
        connector: SourceConnector,
        source_config: SourceConfig,
        category: Category,
        manifest: CategoryManifest,
        decision: DocumentDecision,
    ) -> DocumentOutcome:
        ref = decision.ref
        started = time.perf_counter()
        base = {
            "document_id": ref.document_id,
            "title": ref.title,
            "source": source_config.name,
            "category_key": category.key,
        }

        # Stage timings are kept here rather than inside each stage so the
        # analyzer can attribute a document's wall time without joining
        # across events.
        stages: dict[str, float] = {}

        try:
            with self._stage(stages, "fetch"):
                payload = connector.fetch_payload(ref)
            with self._stage(stages, "convert"):
                conversion = self.converter.convert(payload, title=ref.title)
            self.metrics.emit(
                "convert",
                converter=conversion.converter,
                media_type=conversion.source_media_type.value,
                pages=conversion.page_count,
                chars=len(conversion.markdown),
                seconds=conversion.duration_seconds,
                document_id=ref.document_id,
                source=source_config.name,
            )

            # A republish with identical text is not a real change; do not
            # churn the bucket or bump the version for it.
            if (
                decision.previous is not None
                and decision.previous.content_sha256 == conversion.content_sha256
            ):
                logger.info("%s: content identical after conversion, recording as unchanged.", ref.document_id)
                decision.previous.revision_key = ref.revision_key
                decision.previous.last_updated_at = _utc_now()
                manifest.upsert(decision.previous)
                return self._record_document(
                    DocumentOutcome(
                        **base,
                        change=ChangeType.UNCHANGED,
                        storage_path=decision.previous.storage_path,
                        converter=conversion.converter,
                        duration_seconds=round(time.perf_counter() - started, 3),
                    ),
                    stages,
                    conversion=conversion,
                )

            path = self.layout.document_path(
                source_config.name, category.key, ref.slug, version=decision.path_version
            )
            entry = ManifestEntry(
                document_id=ref.document_id,
                title=ref.title,
                storage_path=path,
                source_url=ref.source_url,
                origin_url=payload.origin_url,
                revision_key=ref.revision_key,
                content_sha256=conversion.content_sha256,
                source_media_type=conversion.source_media_type,
                converter=conversion.converter,
                page_count=conversion.page_count,
                published_at=ref.published_at,
                source_updated_at=ref.source_updated_at,
                version=decision.version,
                extra={**connector.document_metadata(ref), **payload.extra, **conversion.extra},
            )

            if self.config.run.dry_run:
                logger.info("[dry-run] would write %s", path)
            else:
                document = render_document(
                    conversion.markdown,
                    self._front_matter(entry, category, source_config),
                    include_front_matter=self.config.conversion.front_matter,
                )
                with self._stage(stages, "store"):
                    stored = self.store.write_text(path, document, content_type="text/markdown")
                entry.byte_size = stored.byte_size
                self.metrics.emit(
                    "store",
                    backend=self.store.name,
                    path=path,
                    bytes=stored.byte_size,
                    seconds=stages["store"],
                )
                logger.info("%s %s -> %s", decision.change.value, ref.document_id, path)

            entry.last_updated_at = _utc_now()
            manifest.upsert(entry)

            return self._record_document(
                DocumentOutcome(
                    **base,
                    change=decision.change,
                    storage_path=path,
                    converter=conversion.converter,
                    duration_seconds=round(time.perf_counter() - started, 3),
                ),
                stages,
                conversion=conversion,
            )

        except Exception as exc:
            logger.warning("Failed %s (%s): %s", ref.document_id, ref.source_url, exc)
            logger.debug("Traceback for %s", ref.document_id, exc_info=True)
            if not self.config.run.continue_on_error:
                raise
            return self._record_document(
                DocumentOutcome(
                    **base,
                    change=ChangeType.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_seconds=round(time.perf_counter() - started, 3),
                ),
                stages,
                error_type=type(exc).__name__,
            )

    @contextmanager
    def _stage(self, stages: dict[str, float], name: str) -> Iterator[None]:
        """Time one stage of a document, recording it even when it raises."""
        started = time.perf_counter()
        try:
            yield
        finally:
            stages[name] = round(time.perf_counter() - started, 3)

    def _record_document(
        self,
        outcome: DocumentOutcome,
        stages: dict[str, float],
        *,
        conversion: object | None = None,
        error_type: str | None = None,
    ) -> DocumentOutcome:
        """Emit the per-document event and pass the outcome straight through.

        Returning the outcome lets call sites stay single-expression
        ``return`` statements rather than growing a temporary each time.
        """
        self.metrics.emit(
            "document",
            document_id=outcome.document_id,
            title=outcome.title,
            source=outcome.source,
            category=outcome.category_key,
            change=outcome.change.value,
            seconds=outcome.duration_seconds,
            stages=stages,
            converter=outcome.converter,
            pages=getattr(conversion, "page_count", None),
            chars=len(getattr(conversion, "markdown", "")) or None,
            error_type=error_type,
            # Converter-reported quality signals (OCR currency confidence
            # today) ride along, so one file answers both "how fast" and
            # "how trustworthy".
            **{
                key: value
                for key, value in getattr(conversion, "extra", {}).items()
                if isinstance(value, (int, float, str, bool))
            },
        )
        return outcome

    @staticmethod
    def _front_matter(
        entry: ManifestEntry, category: Category, source_config: SourceConfig
    ) -> dict[str, object]:
        """Metadata block embedded at the top of each markdown file."""
        return {
            "document_id": entry.document_id,
            "title": entry.title,
            "source": source_config.name,
            "source_label": source_config.label,
            "category": category.display_name,
            "category_key": category.key,
            "source_url": entry.source_url,
            "origin_url": entry.origin_url,
            "published_at": entry.published_at,
            "source_updated_at": entry.source_updated_at,
            "retrieved_at": entry.last_updated_at,
            "version": entry.version,
            "content_sha256": entry.content_sha256,
            "revision_key": entry.revision_key,
            "source_media_type": entry.source_media_type,
            "converter": entry.converter,
            "page_count": entry.page_count,
            **entry.extra,
        }

    # ------------------------------------------------------------------ teardown

    def close(self) -> None:
        self.fetchers.close()
        self.converter.close()
        self.store.close()
        self.metrics.close()
