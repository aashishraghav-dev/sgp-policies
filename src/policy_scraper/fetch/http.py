"""Plain-HTTP fetcher.

Correct for origins that serve complete markup server-side.  RBI is the
case in point: its Master Directions index and every detail page render
fully without JavaScript, so this path avoids the cost of a browser
entirely.
"""

from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from policy_scraper.config.models import HttpFetcherConfig
from policy_scraper.core.errors import AccessDeniedError, FetchError, PermanentError
from policy_scraper.fetch.base import Fetcher, FetchResponse, RateLimiter
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

# Status codes that will never succeed on retry.
_PERMANENT_STATUSES = {400, 401, 404, 405, 410, 451}


class HttpFetcher(Fetcher):
    """httpx-backed fetcher with politeness throttling and bounded retries."""

    name = "http"

    def __init__(self, config: HttpFetcherConfig) -> None:
        self._config = config
        self._limiter = RateLimiter(config.requests_per_second)
        self._client: httpx.Client | None = None

    def open(self) -> None:
        if self._client is not None:
            return
        headers = {
            "User-Agent": self._config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            **self._config.headers,
        }
        self._client = httpx.Client(
            headers=headers,
            timeout=self._config.timeout_seconds,
            follow_redirects=True,
            verify=self._config.verify_tls,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        self.open()

        # Retry policy is built per-instance so max_retries stays configurable.
        @retry(
            stop=stop_after_attempt(self._config.max_retries),
            wait=wait_exponential(multiplier=self._config.backoff_seconds, max=60),
            retry=retry_if_exception_type(FetchError),
            reraise=True,
        )
        def _attempt() -> FetchResponse:
            return self._request(url, headers)

        return _attempt()

    def _request(self, url: str, headers: dict[str, str] | None) -> FetchResponse:
        assert self._client is not None  # set by open()
        self._limiter.acquire()
        logger.debug("GET %s", url)

        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise FetchError(f"GET {url} failed: {exc}") from exc

        if response.status_code == 403:
            raise AccessDeniedError(
                f"GET {url} returned 403. The origin is refusing non-browser clients; "
                f"set this source's fetcher to 'browser'."
            )
        if response.status_code in _PERMANENT_STATUSES:
            raise PermanentError(f"GET {url} returned {response.status_code}.")
        if response.status_code >= 400:
            raise FetchError(f"GET {url} returned {response.status_code}.")

        return FetchResponse(
            url=str(response.url),
            status=response.status_code,
            content=response.content,
            headers={k.lower(): v for k, v in response.headers.items()},
        )
