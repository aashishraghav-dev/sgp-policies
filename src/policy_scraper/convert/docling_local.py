"""In-process docling PDF -> markdown conversion.

Kept behind the same :class:`DocumentConverter` interface as the remote
backend so the deployment choice is pure configuration.

This backend imports ``docling`` lazily. That matters: docling drags in
torch/transformers/triton (~4GB), and a deployment that points at a
docling *service* should not pay for those at image-build time. The
dependency lives in the optional ``[docling]`` extra.

OCR defaults to on because NPCI circulars are scanned images with no text
layer at all -- without OCR they convert to empty markdown.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from policy_scraper.config.models import DoclingLocalConfig
from policy_scraper.convert.base import DocumentConverter, converter_registry
from policy_scraper.convert.html import normalise_markdown
from policy_scraper.convert.ocr_repair import apply_repair
from policy_scraper.core.errors import ConversionError
from policy_scraper.core.models import ContentPayload, ConversionResult, MediaType
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

#: Only the codes this corpus plausibly needs; anything else passes through
#: unchanged so an unusual language is a config change, not a code change.
_TESSERACT_LANGS = {"en": "eng", "hi": "hin", "mr": "mar", "bn": "ben", "ta": "tam"}


@converter_registry.register("docling_local")
class LocalDoclingConverter(DocumentConverter):
    """Runs docling's ``DocumentConverter`` inside this process."""

    name = "docling_local"
    supported_media_types = frozenset({MediaType.PDF})

    def __init__(self, config: DoclingLocalConfig, *, repair_currency: bool = True) -> None:
        self._config = config
        self._repair_currency = repair_currency
        self._converter: Any | None = None
        # docling's pipeline is not documented as thread-safe and holds
        # model state; serialise access so max_workers > 1 stays safe.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ setup

    def _ensure_converter(self) -> Any:
        if self._converter is not None:
            return self._converter
        with self._lock:
            if self._converter is None:
                self._converter = self._build_converter()
        return self._converter

    def _build_converter(self) -> Any:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                TableFormerMode,
            )
            from docling.document_converter import DocumentConverter as _Docling
            from docling.document_converter import PdfFormatOption
        except ImportError as exc:
            raise ConversionError(
                "docling is not installed. Either install the extra "
                "(pip install -e '.[docling]') or switch "
                "conversion.pdf.backend to 'docling_remote'."
            ) from exc

        options = PdfPipelineOptions()
        options.do_ocr = self._config.ocr_enabled
        options.do_table_structure = self._config.table_structure_enabled
        options.images_scale = self._config.images_scale
        if self._config.table_structure_enabled:
            options.table_structure_options.mode = (
                TableFormerMode.ACCURATE
                if self._config.table_mode == "accurate"
                else TableFormerMode.FAST
            )
        if self._config.artifacts_path:
            options.artifacts_path = self._config.artifacts_path
        if self._config.ocr_enabled:
            self._apply_ocr_options(options)

        format_kwargs: dict[str, Any] = {"pipeline_options": options}
        if self._config.pdf_backend == "pypdfium":
            # Not a preference. docling's default backend returns a blank
            # page -- SUCCESS status, no error, zero text -- for some NPCI
            # scans. See docs/ocr-accuracy.md.
            from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

            format_kwargs["backend"] = PyPdfiumDocumentBackend

        logger.info(
            "Initialising local docling (ocr=%s engine=%s mode=%s backend=%s tables=%s)",
            self._config.ocr_enabled,
            self._config.ocr_engine,
            self._config.ocr_mode,
            self._config.pdf_backend,
            self._config.table_mode,
        )
        return _Docling(format_options={InputFormat.PDF: PdfFormatOption(**format_kwargs)})

    def _apply_ocr_options(self, options: Any) -> None:
        """Select the OCR engine, tolerating docling's evolving option names."""
        from docling.datamodel import pipeline_options as po

        engine_classes = {
            "easyocr": "EasyOcrOptions",
            "tesseract": "TesseractCliOcrOptions",
            "rapidocr": "RapidOcrOptions",
        }
        class_name = engine_classes[self._config.ocr_engine]
        ocr_options_cls = getattr(po, class_name, None)
        if ocr_options_cls is None:
            logger.warning(
                "docling build has no %s; falling back to its default OCR engine.", class_name
            )
            return

        ocr_options = ocr_options_cls()
        # Config carries ISO 639-1; tesseract wants ISO 639-2/T. Translating
        # here keeps the engine choice a one-line config change.
        if hasattr(ocr_options, "lang"):
            ocr_options.lang = [
                _TESSERACT_LANGS.get(code, code) if self._config.ocr_engine == "tesseract" else code
                for code in self._config.ocr_languages
            ]
        if hasattr(po, "OcrMode"):
            ocr_options.mode = po.OcrMode(self._config.ocr_mode)
        options.ocr_options = ocr_options

    # ------------------------------------------------------------------ convert

    def convert(self, payload: ContentPayload, *, title: str | None = None) -> ConversionResult:
        converter = self._ensure_converter()
        started = time.perf_counter()

        # docling reads from a path, so the in-memory payload is spooled to
        # a temp file that is always cleaned up.
        with tempfile.TemporaryDirectory(prefix="policy-docling-") as tmpdir:
            pdf_path = Path(tmpdir) / "document.pdf"
            pdf_path.write_bytes(payload.data)

            kwargs: dict[str, Any] = {}
            if self._config.max_pages is not None:
                kwargs["max_num_pages"] = self._config.max_pages

            try:
                with self._lock:
                    result = converter.convert(pdf_path, **kwargs)
                markdown = result.document.export_to_markdown()
                page_count = len(getattr(result.document, "pages", []) or []) or None
            except Exception as exc:
                raise ConversionError(
                    f"docling failed to convert {payload.origin_url}: {exc}"
                ) from exc

        markdown = normalise_markdown(markdown)
        if not markdown.strip():
            raise ConversionError(
                f"docling produced no text for {payload.origin_url}. "
                f"If this is a scanned PDF, check that OCR is enabled, and see "
                f"conversion.pdf.docling_local.pdf_backend -- docling's default "
                f"backend returns a blank page for some scans."
            )

        markdown, findings = apply_repair(
            markdown,
            enabled=self._config.ocr_enabled and self._repair_currency,
            origin=payload.origin_url,
        )
        extra: dict[str, Any] = {
            "ocr": self._config.ocr_enabled,
            "engine": self._config.ocr_engine,
            "pdf_backend": self._config.pdf_backend,
            **findings,
        }

        return ConversionResult(
            markdown=markdown,
            converter=self.name,
            source_media_type=MediaType.PDF,
            page_count=page_count,
            duration_seconds=round(time.perf_counter() - started, 3),
            extra=extra,
        )

    def health_check(self) -> bool:
        try:
            self._ensure_converter()
            return True
        except ConversionError:
            return False

    def close(self) -> None:
        self._converter = None
