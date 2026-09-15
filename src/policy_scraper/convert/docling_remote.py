"""HTTP client for a standalone docling service.

Swapping ``conversion.pdf.backend`` from ``docling_local`` to
``docling_remote`` moves PDF parsing onto its own deployment with no code
change anywhere else -- the scraper keeps talking to
:class:`DocumentConverter`.

Expected service contract (see ``docs/docling-service-contract.md``):

    POST {base_url}{convert_path}
        multipart/form-data: file=<pdf bytes>, options=<json>
    200 -> {"markdown": "...", "page_count": 12}

The response reader accepts a few common key spellings so this client
works against docling-serve or a thin in-house wrapper without edits.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from policy_scraper.config.models import DoclingRemoteConfig
from policy_scraper.convert.base import DocumentConverter, converter_registry
from policy_scraper.convert.html import normalise_markdown
from policy_scraper.core.errors import ConversionError, TransientError
from policy_scraper.core.models import ContentPayload, ConversionResult, MediaType
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

_MARKDOWN_KEYS = ("markdown", "md", "content", "text")
_PAGE_COUNT_KEYS = ("page_count", "pages", "num_pages")


@converter_registry.register("docling_remote")
class RemoteDoclingConverter(DocumentConverter):
    """Posts PDFs to a docling service and returns its markdown."""

    name = "docling_remote"
    supported_media_types = frozenset({MediaType.PDF})

    def __init__(self, config: DoclingRemoteConfig) -> None:
        self._config = config
        self._client: httpx.Client | None = None

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            headers = {"Accept": "application/json"}
            if self._config.api_key:
                headers[self._config.api_key_header] = self._config.api_key
            self._client = httpx.Client(
                base_url=self._config.base_url.rstrip("/"),
                timeout=self._config.timeout_seconds,
                headers=headers,
            )
        return self._client

    def convert(self, payload: ContentPayload, *, title: str | None = None) -> ConversionResult:
        client = self._ensure_client()
        started = time.perf_counter()

        @retry(
            stop=stop_after_attempt(self._config.max_retries),
            wait=wait_exponential(multiplier=self._config.backoff_seconds, max=120),
            retry=retry_if_exception_type(TransientError),
            reraise=True,
        )
        def _post() -> dict[str, Any]:
            try:
                response = client.post(
                    self._config.convert_path,
                    files={"file": (f"{title or 'document'}.pdf", payload.data, "application/pdf")},
                    data={"options": json.dumps({"source_url": payload.origin_url})},
                )
            except httpx.HTTPError as exc:
                raise TransientError(f"docling service unreachable: {exc}") from exc

            if response.status_code >= 500:
                raise TransientError(
                    f"docling service returned {response.status_code}: {response.text[:200]}"
                )
            if response.status_code >= 400:
                raise ConversionError(
                    f"docling service rejected {payload.origin_url} "
                    f"({response.status_code}): {response.text[:200]}"
                )
            return response.json()

        body = _post()
        markdown = next((body[k] for k in _MARKDOWN_KEYS if isinstance(body.get(k), str)), None)
        if markdown is None:
            raise ConversionError(
                f"docling service response had no markdown field "
                f"(looked for {', '.join(_MARKDOWN_KEYS)}); got keys: {sorted(body)}"
            )

        markdown = normalise_markdown(markdown)
        if not markdown.strip():
            raise ConversionError(f"docling service returned empty markdown for {payload.origin_url}.")

        page_count = next((body[k] for k in _PAGE_COUNT_KEYS if isinstance(body.get(k), int)), None)
        return ConversionResult(
            markdown=markdown,
            converter=self.name,
            source_media_type=MediaType.PDF,
            page_count=page_count,
            duration_seconds=round(time.perf_counter() - started, 3),
            extra={"service": self._config.base_url},
        )

    def health_check(self) -> bool:
        try:
            return self._ensure_client().get(self._config.health_path).status_code < 400
        except httpx.HTTPError:
            logger.warning("docling service health check failed at %s", self._config.base_url)
            return False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
