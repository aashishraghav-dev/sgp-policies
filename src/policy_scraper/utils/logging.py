"""Logging setup shared by the CLI and the manual runner."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-38s %(message)s"
_NOISY = ("httpx", "httpcore", "urllib3", "google", "PIL", "asyncio")

#: Loggers that report non-events at ERROR. Tesseract's orientation
#: detection fails on small text rectangles ("Too few characters") on
#: essentially every scanned circular; OCR proceeds regardless, and
#: leaving these at ERROR trains a reader to ignore the level that
#: actually matters.
_FALSE_ALARMS = ("docling.models.stages.ocr.tesseract_ocr_cli_model",)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Silenced only at INFO and above, so --log-level DEBUG still shows them.
    if logging.getLogger().level > logging.DEBUG:
        for name in _FALSE_ALARMS:
            logging.getLogger(name).setLevel(logging.CRITICAL)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
