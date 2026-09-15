#!/usr/bin/env python3
"""Manual scrape runner -- the one file to run during development.

This is deliberately a thin wrapper over the same ``ScrapePipeline`` the
CLI uses, so what you exercise here is exactly what a scheduler will run
later. When the pipeline is wired to Cloud Scheduler / Cloud Run, this file
can be deleted without touching anything else.

    python run_scrape.py                      # every enabled source
    python run_scrape.py --source rbi         # one source
    python run_scrape.py --dry-run            # discover + diff, write nothing
    python run_scrape.py --force              # reconvert everything
    python run_scrape.py --max-documents 2    # small smoke run
    python run_scrape.py --list-sources       # what's configured/registered

Environment:
    STORAGE_BACKEND=gcs GCS_BUCKET=my-bucket python run_scrape.py
    DOCLING_BACKEND=docling_remote DOCLING_URL=http://docling:8080 python run_scrape.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running straight from a checkout, before `pip install -e .`.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from policy_scraper.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
