"""Structured run metrics.

Every run appends newline-delimited JSON events to a file. The scraper
never reads them back -- analysis is a separate concern, handled by
``tools/analyze_metrics.py`` -- which keeps this module a writer only and
lets the analysis evolve without touching the pipeline.

JSONL rather than a summary blob because the interesting questions are not
known in advance: "which stage dominates", "how much of the run is OCR",
"is the browser fetcher the bottleneck", "which documents are slowest".
All of those are aggregations over per-event rows.

Events emitted:

    run_start   one per run, with the effective configuration
    fetch       one per HTTP/browser request: transport, bytes, seconds
    convert     one per conversion: converter, pages, chars, seconds
    store       one per object written: path, bytes, seconds
    document    one per document: outcome plus its per-stage breakdown
    category    one per (source, category): counts and wall time
    run_end     one per run: totals

The recorder is thread-safe: documents within a category are processed
concurrently for transports that allow it.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class MetricsRecorder:
    """Appends run events as JSON lines.

    A failure to record must never break a scrape, so write errors are
    logged once and then suppressed -- metrics are diagnostics, not the
    product.
    """

    def __init__(self, path: str | Path, *, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.path = Path(str(path).format(run_id=self.run_id, date=_dt.date.today().isoformat()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8")
        self._broken = False
        logger.info("Recording run metrics to %s", self.path)

    # ------------------------------------------------------------------ writing

    def emit(self, event: str, **fields: Any) -> None:
        if self._broken:
            return
        record = {
            "ts": _utc_now_iso(),
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        try:
            # Serialisation is inside the guard too: a field holding an
            # object with a raising __str__ must not take down a scrape
            # either.
            line = json.dumps(record, default=str)
            with self._lock:
                self._handle.write(line + "\n")
                self._handle.flush()
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            # Deliberately broad. A full disk raises OSError, a handle
            # closed by an earlier teardown raises ValueError, and a field
            # can raise anything at all. None of them justify losing a run,
            # so recording switches off once and stays quiet after that.
            self._broken = True
            logger.warning("Metrics recording disabled after write error: %s", exc)

    @contextmanager
    def timed(self, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """Time a block and emit one event for it.

        The yielded dict is writable, so a caller can attach results that
        are only known once the work is done (byte counts, page counts).
        Failures are recorded too, with ``ok: false`` -- a run where one
        stage is slow because it keeps failing should be visible as such.
        """
        extra: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            yield extra
        except Exception as exc:
            self.emit(
                event,
                seconds=round(time.perf_counter() - started, 3),
                ok=False,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
                **fields,
                **extra,
            )
            raise
        self.emit(
            event,
            seconds=round(time.perf_counter() - started, 3),
            ok=True,
            **fields,
            **extra,
        )

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass


class NullRecorder(MetricsRecorder):
    """No-op recorder, so callers never need to check for ``None``."""

    def __init__(self) -> None:  # noqa: D107 - deliberately does not open a file
        self.run_id = "null"
        self.path = Path(os.devnull)
        self._broken = True
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        return

    def close(self) -> None:
        return


NULL_RECORDER = NullRecorder()
