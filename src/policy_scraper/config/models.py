"""Typed configuration schema.

Every knob the pipeline reads lives here and is validated at load time, so
a typo in the YAML fails immediately with a precise message instead of
halfway through a scrape.

Source-specific settings deliberately live in the free-form
``SourceConfig.options`` dict: adding a new website must not require
editing this file.  Each connector validates its own options into its own
model (see ``sources/rbi/master_directions.py``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from policy_scraper.core.models import UpdateStrategy


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- fetching


class HttpFetcherConfig(_Base):
    timeout_seconds: float = 60.0
    max_retries: int = 3
    backoff_seconds: float = 2.0
    requests_per_second: float = Field(default=2.0, gt=0, description="Politeness throttle.")
    verify_tls: bool = True
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    headers: dict[str, str] = Field(default_factory=dict)


class BrowserFetcherConfig(_Base):
    """Settings for the Playwright fetcher.

    Needed by origins behind a bot wall (NPCI sits behind Akamai, which
    rejects every non-browser client including server-side requests made
    from a browser's own cookie jar). Requests must originate inside the
    page, so this fetcher exposes in-page fetch helpers.
    """

    headless: bool = True
    timeout_seconds: float = 120.0
    max_retries: int = 3
    backoff_seconds: float = 3.0
    requests_per_second: float = Field(default=2.0, gt=0)
    navigation_wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = (
        "networkidle"
    )
    settle_ms: int = Field(default=2000, ge=0, description="Extra pause after navigation.")
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "en-US"
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    launch_args: list[str] = Field(default_factory=lambda: ["--no-sandbox", "--disable-dev-shm-usage"])


class FetchingConfig(_Base):
    http: HttpFetcherConfig = Field(default_factory=HttpFetcherConfig)
    browser: BrowserFetcherConfig = Field(default_factory=BrowserFetcherConfig)


# --------------------------------------------------------------------------- conversion


class HtmlConversionConfig(_Base):
    """HTML -> markdown. Cheap, lossless and OCR-free, so it is preferred
    wherever a source publishes the full document as HTML."""

    strip_tags: list[str] = Field(
        default_factory=lambda: ["script", "style", "noscript", "iframe", "form", "button"]
    )
    strip_selectors: list[str] = Field(
        default_factory=list, description="Extra CSS selectors removed before conversion."
    )
    heading_style: Literal["ATX", "SETEXT"] = "ATX"
    bullet_chars: str = "-"
    wrap_width: int = Field(default=0, ge=0, description="0 disables hard wrapping.")
    unwrap_layout_tables: bool = Field(
        default=True,
        description=(
            "Rewrite positioning tables as divs before conversion. RBI nests whole "
            "documents inside layout tables; without this the entire body collapses "
            "into a single markdown table cell and paragraph structure is lost."
        ),
    )


class DoclingLocalConfig(_Base):
    """In-process docling. Pulls ~4GB of ML deps; install the extra:
    ``pip install -e '.[docling]'``."""

    ocr_enabled: bool = Field(
        default=True,
        description="Required for NPCI circulars, which are scanned images with no text layer.",
    )
    ocr_engine: Literal["easyocr", "tesseract", "rapidocr"] = Field(
        default="tesseract",
        description=(
            "Measured on NPCI circulars by tools/ocr_benchmark.py: tesseract "
            "scores CER 0.109 / WER 0.150 against easyocr's 0.145 / 0.237, "
            "and is ~4x faster. Re-run that benchmark before changing this."
        ),
    )
    ocr_languages: list[str] = Field(
        default_factory=lambda: ["en"],
        description="ISO 639-1. Translated to each engine's spelling by the converter.",
    )
    ocr_mode: Literal["default", "full_page"] = Field(
        default="default",
        description=(
            "'full_page' OCRs the whole page instead of only the regions "
            "docling thinks are images. Measured as no more accurate here, "
            "so it stays off; switch it on for a source whose text layer "
            "is present but wrong."
        ),
    )
    pdf_backend: Literal["pypdfium", "docling_parse"] = Field(
        default="pypdfium",
        description=(
            "docling's default (docling_parse) silently returns a *blank "
            "page* for some NPCI scans -- a successful conversion with zero "
            "text, no error. pypdfium reads them. See docs/ocr-accuracy.md."
        ),
    )
    table_structure_enabled: bool = True
    table_mode: Literal["fast", "accurate"] = "accurate"
    images_scale: float = 2.0
    max_pages: int | None = None
    artifacts_path: str | None = Field(
        default=None, description="Pre-downloaded model dir, for offline/container runs."
    )


class DoclingRemoteConfig(_Base):
    """HTTP client for a standalone docling service.

    Keeps the scraper image small and lets the GPU/RAM-hungry parser scale
    independently. Swapping to it is a config change, not a code change.
    """

    base_url: str = "http://localhost:8080"
    convert_path: str = "/v1/convert"
    health_path: str = "/health"
    timeout_seconds: float = 900.0
    max_retries: int = 3
    backoff_seconds: float = 5.0
    api_key: str | None = None
    api_key_header: str = "X-API-Key"


class PdfConversionConfig(_Base):
    backend: Literal["docling_local", "docling_remote"] = "docling_local"
    repair_currency: bool = Field(
        default=True,
        description=(
            "No OCR engine tested reads the rupee sign; they emit %, ¥, $, "
            "< or nothing. Repairs the unambiguous cases and flags the rest "
            "rather than letting a wrong amount through silently. Sits here "
            "rather than under a backend because it is a property of the "
            "documents, not of where docling runs -- switching to a service "
            "must not quietly drop it."
        ),
    )
    docling_local: DoclingLocalConfig = Field(default_factory=DoclingLocalConfig)
    docling_remote: DoclingRemoteConfig = Field(default_factory=DoclingRemoteConfig)


class ConversionConfig(_Base):
    html: HtmlConversionConfig = Field(default_factory=HtmlConversionConfig)
    pdf: PdfConversionConfig = Field(default_factory=PdfConversionConfig)
    front_matter: bool = Field(
        default=True, description="Prepend YAML front matter to each stored markdown file."
    )


# --------------------------------------------------------------------------- storage


class LocalStoreConfig(_Base):
    base_path: str = "./data/object-store"


class GcsStoreConfig(_Base):
    bucket: str = ""
    project: str | None = None
    credentials_path: str | None = Field(
        default=None, description="Service-account JSON; omit to use Application Default Credentials."
    )
    timeout_seconds: float = 120.0


class StorageConfig(_Base):
    backend: Literal["local", "gcs"] = "local"
    root_prefix: str = Field(default="policies", description="Top-level key prefix in the bucket.")
    local: LocalStoreConfig = Field(default_factory=LocalStoreConfig)
    gcs: GcsStoreConfig = Field(default_factory=GcsStoreConfig)

    @model_validator(mode="after")
    def _require_bucket_for_gcs(self) -> "StorageConfig":
        if self.backend == "gcs" and not self.gcs.bucket:
            raise ValueError("storage.gcs.bucket is required when storage.backend is 'gcs'.")
        return self


# --------------------------------------------------------------------------- sources


class CategoryFilterConfig(_Base):
    """Selects which categories to scrape without naming them in code.

    ``include`` empty means "everything discovered", which is what keeps
    the RBI connector scalable: categories are read off the page, and
    ``limits.max_categories`` caps the volume for milestone 1.
    """

    include: list[str] = Field(default_factory=list, description="Category keys; empty = all.")
    exclude: list[str] = Field(default_factory=list)

    def matches(self, category_key: str) -> bool:
        if category_key in self.exclude:
            return False
        return not self.include or category_key in self.include


class SourceLimitsConfig(_Base):
    max_categories: int | None = Field(default=None, ge=1)
    max_documents_per_category: int | None = Field(default=None, ge=1)
    max_documents_total: int | None = Field(default=None, ge=1)


class SourceConfig(_Base):
    name: str = Field(description="Unique run-level name; also the storage folder for the source.")
    type: str = Field(description="Connector registry key, e.g. 'rbi.master_directions'.")
    enabled: bool = True
    display_name: str | None = None
    fetcher: Literal["http", "browser"] = "http"
    update_strategy: UpdateStrategy = UpdateStrategy.REPLACE_IN_PLACE
    limits: SourceLimitsConfig = Field(default_factory=SourceLimitsConfig)
    categories: CategoryFilterConfig = Field(default_factory=CategoryFilterConfig)
    options: dict[str, Any] = Field(
        default_factory=dict, description="Connector-specific; validated by the connector itself."
    )

    @property
    def label(self) -> str:
        return self.display_name or self.name


# --------------------------------------------------------------------------- run


class MetricsConfig(_Base):
    """Per-run timing and volume records, written as JSON lines.

    Kept local rather than shipped to the bucket: they describe a run, not
    the policy corpus, and ``tools/analyze_metrics.py`` reads them offline.
    """

    enabled: bool = True
    path: str = Field(
        default="metrics/run-{date}-{run_id}.jsonl",
        description="Accepts {run_id} and {date}. A fixed name appends across runs.",
    )


class RunConfig(_Base):
    max_workers: int = Field(default=4, ge=1, description="Concurrent documents per category.")
    force_refetch: bool = Field(
        default=False, description="Ignore the catalog and reconvert everything."
    )
    dry_run: bool = Field(default=False, description="Discover and diff, but write nothing.")
    continue_on_error: bool = True
    log_level: str = "INFO"
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)


class AppConfig(_Base):
    version: int = 1
    run: RunConfig = Field(default_factory=RunConfig)
    fetching: FetchingConfig = Field(default_factory=FetchingConfig)
    conversion: ConversionConfig = Field(default_factory=ConversionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sources: list[SourceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_source_names(self) -> "AppConfig":
        names = [s.name for s in self.sources]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"Duplicate source names: {', '.join(sorted(duplicates))}.")
        return self

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]
