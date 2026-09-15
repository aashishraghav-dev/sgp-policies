"""Fetcher abstraction.

A fetcher knows *how* to talk to an origin (plain HTTP vs a real browser);
it knows nothing about what the bytes mean.  Connectors declare which
fetcher they need via config, so moving a source from HTTP to a browser
(or back) never touches connector code.
"""

from __future__ import annotations

import abc
import threading
import time
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from policy_scraper.core.metrics import NULL_RECORDER, MetricsRecorder
from policy_scraper.core.models import MediaType


class FetchResponse(BaseModel):
    """Normalised response, independent of the transport that produced it."""

    model_config = ConfigDict(frozen=True)

    url: str
    status: int
    content: bytes
    headers: dict[str, str] = {}

    @property
    def text(self) -> str:
        return self.content.decode(self._charset(), errors="replace")

    @property
    def media_type(self) -> MediaType:
        return MediaType.from_header(self.headers.get("content-type"))

    def json(self) -> Any:
        import json

        return json.loads(self.text)

    def _charset(self) -> str:
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            return content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        return "utf-8"


class RateLimiter:
    """Thread-safe minimum-interval throttle, shared across a fetcher."""

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class Fetcher(abc.ABC):
    """Retrieves bytes from an origin.

    Implementations are context managers because both transports hold
    resources (a connection pool, a browser process) that must outlive
    individual requests but be released at the end of a run.
    """

    name: str = "fetcher"

    #: Whether this transport may be driven from several threads at once.
    #: Playwright's sync API is bound to the thread that created it, so the
    #: pipeline reads this to decide a source's worker count rather than
    #: special-casing transports by name.
    supports_concurrency: bool = True

    #: Set by the pool. Replaced rather than checked for None so no call
    #: site needs to know whether metrics are switched on.
    recorder: MetricsRecorder = NULL_RECORDER

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        """Retrieve a URL, following redirects. Raises on non-2xx.

        Template method: subclasses implement :meth:`_fetch`, and every
        request is measured here so request counts and transport timings
        are recorded once rather than in each transport.
        """
        with self.recorder.timed("fetch", fetcher=self.name, url=url) as event:
            response = self._fetch(url, headers=headers)
            event["status"] = response.status
            event["bytes"] = len(response.content)
            event["media_type"] = response.media_type.value
        return response

    @abc.abstractmethod
    def _fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        """Perform the actual retrieval. Called by :meth:`get`."""

    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        """Retrieve and parse a JSON endpoint."""
        return self.get(url, headers={"Accept": "application/json", **(headers or {})}).json()

    def open(self) -> None:  # noqa: A003 - mirrors close()
        """Acquire transport resources. Idempotent."""

    def close(self) -> None:
        """Release transport resources. Idempotent."""

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
