"""Playwright-backed fetcher for origins behind a bot wall.

Why this exists (measured against npci.org.in, which sits behind Akamai
Bot Manager):

  * ``curl``/``httpx`` with full browser headers  -> 403 on every path
  * Playwright page navigation                    -> 200
  * Playwright ``APIRequestContext`` *carrying the
    browser's own cookies*                        -> 403

That last result is the important one. Replaying cookies out-of-band is
not enough, because the edge also fingerprints the TLS/HTTP2 client. The
request has to originate from inside the page, so every retrieval here is
performed with an in-page ``fetch()`` and the bytes are handed back over
the CDP bridge.

Binary payloads come back base64-encoded in chunks; encoding in one pass
blows the JS argument limit on multi-megabyte PDFs.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from policy_scraper.config.models import BrowserFetcherConfig
from policy_scraper.core.errors import AccessDeniedError, FetchError, PermanentError
from policy_scraper.fetch.base import Fetcher, FetchResponse, RateLimiter
from policy_scraper.utils.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cost avoided at runtime
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright

logger = get_logger(__name__)

_PERMANENT_STATUSES = {400, 401, 404, 405, 410, 451}

# Runs inside the page, so it inherits the document's cookies, TLS session
# and header profile. Chunked base64 keeps large PDFs within the
# String.fromCharCode argument limit.
_FETCH_JS = """
async ([url, headers]) => {
  const response = await fetch(url, { headers, credentials: 'include' });
  const buffer = new Uint8Array(await response.arrayBuffer());
  let binary = '';
  const CHUNK = 8192;
  for (let i = 0; i < buffer.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, buffer.subarray(i, i + CHUNK));
  }
  const responseHeaders = {};
  response.headers.forEach((value, key) => { responseHeaders[key] = value; });
  return { status: response.status, headers: responseHeaders, body: btoa(binary) };
}
"""


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class BrowserFetcher(Fetcher):
    """Headless-Chromium fetcher that issues requests from page context.

    The browser process is expensive to start, so it is created once per
    run and reused. ``bootstrap`` is called at most once per origin to let
    the edge's JS challenge run and plant its clearance cookies.
    """

    name = "browser"
    # Playwright's sync API may only be driven from its creating thread.
    supports_concurrency = False

    def __init__(self, config: BrowserFetcherConfig) -> None:
        self._config = config
        self._limiter = RateLimiter(config.requests_per_second)
        self._playwright: "Playwright | None" = None
        self._browser: "Browser | None" = None
        self._context: "BrowserContext | None" = None
        self._page: "Page | None" = None
        self._bootstrapped: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise PermanentError(
                "playwright is required for browser-based sources. "
                "Install it with: pip install playwright && playwright install chromium"
            ) from exc

        logger.info("Launching headless browser (headless=%s)", self._config.headless)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._config.headless, args=self._config.launch_args
        )
        self._context = self._browser.new_context(
            user_agent=self._config.user_agent,
            viewport={"width": self._config.viewport_width, "height": self._config.viewport_height},
            locale=self._config.locale,
        )
        self._context.set_default_timeout(self._config.timeout_seconds * 1000)
        self._page = self._context.new_page()

    def close(self) -> None:
        for resource, closer in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                getattr(resource, closer)()
            except Exception:  # pragma: no cover - teardown must not mask real errors
                logger.debug("Error releasing browser resource", exc_info=True)
        self._playwright = self._browser = self._context = self._page = None
        self._bootstrapped.clear()

    # ------------------------------------------------------------------ navigation

    def bootstrap(self, url: str, *, force: bool = False) -> FetchResponse:
        """Navigate to ``url`` so the origin's bot challenge can complete.

        Subsequent :meth:`get` calls against the same origin reuse the
        clearance cookies this plants. Also returns the rendered HTML, so a
        connector can parse the landing page from the same visit.
        """
        self.open()
        assert self._page is not None
        origin = _origin_of(url)
        if origin in self._bootstrapped and not force:
            logger.debug("Origin %s already bootstrapped", origin)

        self._limiter.acquire()
        logger.debug("NAVIGATE %s", url)
        try:
            response = self._page.goto(
                url,
                wait_until=self._config.navigation_wait_until,
                timeout=self._config.timeout_seconds * 1000,
            )
        except Exception as exc:
            raise FetchError(f"Navigation to {url} failed: {exc}") from exc

        if self._config.settle_ms:
            self._page.wait_for_timeout(self._config.settle_ms)

        status = response.status if response is not None else 0
        if status == 403:
            raise AccessDeniedError(f"Navigation to {url} was refused (403) by the origin.")
        if status and status >= 400:
            raise FetchError(f"Navigation to {url} returned {status}.")

        self._bootstrapped.add(origin)
        return FetchResponse(
            url=self._page.url,
            status=status or 200,
            content=self._page.content().encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    # ------------------------------------------------------------------ retrieval

    def _fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        self.open()
        origin = _origin_of(url)
        if origin not in self._bootstrapped:
            # The page must already be on this origin for an in-page fetch
            # to be same-origin and to carry the clearance cookies.
            self.bootstrap(origin + "/")

        @retry(
            stop=stop_after_attempt(self._config.max_retries),
            wait=wait_exponential(multiplier=self._config.backoff_seconds, max=60),
            retry=retry_if_exception_type(FetchError),
            reraise=True,
        )
        def _attempt() -> FetchResponse:
            return self._in_page_fetch(url, headers)

        return _attempt()

    def _in_page_fetch(self, url: str, headers: dict[str, str] | None) -> FetchResponse:
        assert self._page is not None
        self._limiter.acquire()
        logger.debug("IN-PAGE GET %s", url)

        try:
            result: dict[str, Any] = self._page.evaluate(_FETCH_JS, [url, headers or {}])
        except Exception as exc:
            raise FetchError(f"In-page fetch of {url} failed: {exc}") from exc

        status = int(result["status"])
        if status == 403:
            raise AccessDeniedError(f"In-page fetch of {url} was refused (403).")
        if status in _PERMANENT_STATUSES:
            raise PermanentError(f"In-page fetch of {url} returned {status}.")
        if status >= 400:
            raise FetchError(f"In-page fetch of {url} returned {status}.")

        return FetchResponse(
            url=url,
            status=status,
            content=base64.b64decode(result["body"]),
            headers={k.lower(): v for k, v in (result.get("headers") or {}).items()},
        )
