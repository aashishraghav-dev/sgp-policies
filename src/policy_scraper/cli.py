"""Command-line entry point.

This is the interface a scheduler (Cloud Scheduler -> Cloud Run job, cron)
will invoke later. It takes no interactive input and exits non-zero when
anything failed, so a job runner can detect a bad run.
"""

from __future__ import annotations

import argparse
import sys

from policy_scraper.config.loader import load_config
from policy_scraper.core.errors import ScraperError
from policy_scraper.pipeline.orchestrator import ScrapePipeline
from policy_scraper.sources import load_connectors, source_registry
from policy_scraper.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG = "config/sources.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="policy-scraper",
        description="Scrape regulator websites into markdown in object storage.",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="Path to the YAML config.")
    parser.add_argument(
        "-s",
        "--source",
        action="append",
        dest="sources",
        help="Run only this source (repeatable). Defaults to every enabled source.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and diff, but write nothing to storage.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the catalog and re-convert every document.",
    )
    parser.add_argument("--max-categories", type=int, help="Override the per-source category cap.")
    parser.add_argument(
        "--max-documents", type=int, help="Override the per-category document cap."
    )
    parser.add_argument("--workers", type=int, help="Override concurrent documents per category.")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR.")
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="Print configured sources and registered connector types, then exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ScraperError as exc:
        configure_logging("INFO")
        logger.error("%s", exc)
        return 2

    configure_logging(args.log_level or config.run.log_level)

    # CLI flags win over the file, so a scheduled job and an ad-hoc run can
    # share one config.
    if args.dry_run:
        config.run.dry_run = True
    if args.force:
        config.run.force_refetch = True
    if args.workers:
        config.run.max_workers = args.workers
    for source in config.sources:
        if args.max_categories:
            source.limits.max_categories = args.max_categories
        if args.max_documents:
            source.limits.max_documents_per_category = args.max_documents

    if args.list_sources:
        load_connectors()
        print("Configured sources:")
        for source in config.sources:
            state = "enabled" if source.enabled else "disabled"
            print(f"  - {source.name:<24} {source.type:<26} [{state}] fetcher={source.fetcher}")
        print("\nRegistered connector types:")
        for key in source_registry.keys():
            print(f"  - {key}")
        return 0

    try:
        report = ScrapePipeline(config).run(args.sources)
    except ScraperError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130

    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
