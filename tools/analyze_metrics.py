#!/usr/bin/env python3
"""Turn a run's JSONL metrics into a readable performance matrix.

Standalone on purpose: it imports nothing from ``policy_scraper``, reads
only the files the scraper writes, and can be pointed at a run captured on
another machine.

    python tools/analyze_metrics.py                       # newest run
    python tools/analyze_metrics.py metrics/run-*.jsonl   # several runs
    python tools/analyze_metrics.py --slowest 20
    python tools/analyze_metrics.py --json summary.json

The questions it answers are the ones that decide where to spend effort:
which stage owns the wall clock, whether the browser transport or the OCR
is the real cost, and which documents are the outliers dragging a run out.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

DEFAULT_GLOB = "metrics/*.jsonl"

# Stages, in the order a document passes through them.
STAGES = ("fetch", "convert", "store")


# --------------------------------------------------------------------------- loading


def load(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Read every event, skipping lines a killed run left half-written."""
    events: list[dict[str, Any]] = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  ! {path.name}:{number} is not valid JSON, skipped", file=sys.stderr)
    return events


def describe(paths: list[Path]) -> str:
    """Name the inputs without letting the header scroll off the screen.

    Pointing this at a metrics directory that has accumulated a few hundred
    runs is the normal case, so past a handful of files the count is the
    only useful thing to print.
    """
    if len(paths) <= 3:
        return ", ".join(p.name for p in paths)
    return f"{len(paths)} files ({paths[0].name} .. {paths[-1].name})"


def resolve_paths(patterns: list[str]) -> list[Path]:
    if patterns:
        paths = [Path(p) for p in patterns if Path(p).is_file()]
        if paths:
            return paths
        # A shell that did not expand the glob, or a directory.
        expanded: list[Path] = []
        for pattern in patterns:
            base = Path(pattern)
            if base.is_dir():
                expanded += sorted(base.glob("*.jsonl"))
            else:
                expanded += sorted(Path().glob(pattern))
        return expanded

    candidates = sorted(Path().glob(DEFAULT_GLOB), key=lambda p: p.stat().st_mtime)
    return candidates[-1:]


# --------------------------------------------------------------------------- formatting


def _fmt(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.1f}s"


def _bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{count:.0f}{unit}" if unit == "B" else f"{count:.1f}{unit}"
        count /= 1024
    return f"{count:.1f}GB"


def _rule(width: int = 86) -> None:
    print("-" * width)


def _heading(text: str) -> None:
    print(f"\n{text}")
    _rule()


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Runs are small; interpolation would imply
    a precision the sample size does not support."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1)
    return ordered[max(0, index)]


class Stat:
    """Running count/total/series for one grouping key."""

    __slots__ = ("count", "total", "series", "bytes", "failures")

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.series: list[float] = []
        self.bytes = 0
        self.failures = 0

    def add(self, seconds: float, *, byte_count: int = 0, ok: bool = True) -> None:
        self.count += 1
        self.total += seconds
        self.series.append(seconds)
        self.bytes += byte_count
        if not ok:
            self.failures += 1

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.series) if self.series else 0.0

    @property
    def p95(self) -> float:
        return _percentile(self.series, 0.95)


def _tally(
    events: list[dict[str, Any]], event_name: str, key: Any
) -> dict[Any, Stat]:
    """Group one event type by a caller-supplied key function."""
    stats: dict[Any, Stat] = defaultdict(Stat)
    for event in events:
        if event.get("event") != event_name:
            continue
        stats[key(event)].add(
            float(event.get("seconds") or 0.0),
            byte_count=int(event.get("bytes") or 0),
            ok=event.get("ok", True) is not False,
        )
    return stats


# --------------------------------------------------------------------------- sections


def report_runs(events: list[dict[str, Any]]) -> None:
    starts = {e["run_id"]: e for e in events if e.get("event") == "run_start"}
    ends = {e["run_id"]: e for e in events if e.get("event") == "run_end"}

    _heading("RUNS")
    print(f"{'run':<14}{'started':<22}{'sources':<22}{'docs':>6}{'failed':>8}{'wall':>9}")
    for run_id, start in starts.items():
        end = ends.get(run_id, {})
        sources = ",".join(start.get("sources", []))[:20]
        print(
            f"{run_id:<14}{start.get('ts', '')[:19]:<22}{sources:<22}"
            f"{end.get('documents', 0):>6}{end.get('failed', 0):>8}"
            f"{_fmt(float(end.get('seconds') or 0)):>9}"
        )
        if not end:
            print(f"{'':<14}(no run_end: this run was interrupted)")


def report_stages(events: list[dict[str, Any]]) -> None:
    """Where the wall clock actually goes, per stage.

    Summed from the per-document breakdown rather than from the stage
    events, so concurrent work is attributed to the document that caused
    it -- these are cost shares, not elapsed time.
    """
    totals: dict[str, Stat] = defaultdict(Stat)
    for event in events:
        if event.get("event") != "document":
            continue
        for stage, seconds in (event.get("stages") or {}).items():
            totals[stage].add(float(seconds))

    if not totals:
        return

    grand = sum(s.total for s in totals.values())
    _heading("STAGE BREAKDOWN  (summed document time, not wall clock)")
    print(f"{'stage':<14}{'calls':>7}{'total':>10}{'share':>8}{'mean':>9}{'median':>9}{'p95':>9}")
    ordered = [s for s in STAGES if s in totals] + [s for s in totals if s not in STAGES]
    for stage in ordered:
        stat = totals[stage]
        share = stat.total / grand if grand else 0
        bar = "#" * int(share * 24)
        print(
            f"{stage:<14}{stat.count:>7}{_fmt(stat.total):>10}{share:>7.0%}"
            f"{stat.mean:>8.1f}s{stat.median:>8.1f}s{stat.p95:>8.1f}s  {bar}"
        )
    print(f"{'TOTAL':<14}{'':>7}{_fmt(grand):>10}")


def report_fetchers(events: list[dict[str, Any]]) -> None:
    stats = _tally(events, "fetch", lambda e: e.get("fetcher", "?"))
    if not stats:
        return

    _heading("REQUESTS BY TRANSPORT")
    print(
        f"{'fetcher':<14}{'requests':>10}{'failed':>8}{'bytes':>11}"
        f"{'total':>9}{'mean':>9}{'p95':>9}{'req/s':>8}"
    )
    for name, stat in sorted(stats.items(), key=lambda kv: -kv[1].total):
        rate = stat.count / stat.total if stat.total else 0
        print(
            f"{name:<14}{stat.count:>10}{stat.failures:>8}{_bytes(stat.bytes):>11}"
            f"{_fmt(stat.total):>9}{stat.mean:>8.2f}s{stat.p95:>8.2f}s{rate:>8.2f}"
        )


def report_converters(events: list[dict[str, Any]]) -> None:
    rows: dict[str, dict[str, float]] = defaultdict(
        lambda: {"docs": 0, "pages": 0, "chars": 0, "seconds": 0.0}
    )
    for event in events:
        if event.get("event") != "convert":
            continue
        row = rows[event.get("converter", "?")]
        row["docs"] += 1
        row["pages"] += int(event.get("pages") or 0)
        row["chars"] += int(event.get("chars") or 0)
        row["seconds"] += float(event.get("seconds") or 0)

    if not rows:
        return

    _heading("CONVERSION")
    print(f"{'converter':<18}{'docs':>7}{'pages':>7}{'chars':>10}{'total':>9}{'s/doc':>9}{'s/page':>9}")
    for name, row in sorted(rows.items(), key=lambda kv: -kv[1]["seconds"]):
        per_doc = row["seconds"] / row["docs"] if row["docs"] else 0
        per_page = row["seconds"] / row["pages"] if row["pages"] else 0
        page_cell = f"{per_page:.1f}s" if row["pages"] else "-"
        print(
            f"{name:<18}{int(row['docs']):>7}{int(row['pages']):>7}{int(row['chars']):>10}"
            f"{_fmt(row['seconds']):>9}{per_doc:>8.1f}s{page_cell:>9}"
        )


def report_sources(events: list[dict[str, Any]]) -> None:
    rows = [e for e in events if e.get("event") == "category"]
    if not rows:
        return

    _heading("BY SOURCE / CATEGORY")
    print(
        f"{'source':<12}{'category':<30}{'found':>7}{'work':>6}"
        f"{'created':>9}{'updated':>9}{'failed':>8}{'time':>9}"
    )
    for event in sorted(rows, key=lambda e: (e.get("source", ""), e.get("category", ""))):
        outcomes = event.get("outcomes") or {}
        print(
            f"{event.get('source', '?'):<12}{str(event.get('category', '?'))[:29]:<30}"
            f"{event.get('discovered', 0):>7}{event.get('needed_work', 0):>6}"
            f"{outcomes.get('created', 0):>9}{outcomes.get('updated', 0):>9}"
            f"{outcomes.get('failed', 0):>8}{_fmt(float(event.get('seconds') or 0)):>9}"
        )


def report_slowest(events: list[dict[str, Any]], limit: int) -> None:
    documents = [
        e for e in events if e.get("event") == "document" and (e.get("seconds") or 0) > 0
    ]
    if not documents:
        return

    documents.sort(key=lambda e: -float(e.get("seconds") or 0))
    _heading(f"SLOWEST {min(limit, len(documents))} DOCUMENTS")
    print(f"{'secs':>7}  {'stages (fetch/convert/store)':<30}{'pages':>6}  title")
    for event in documents[:limit]:
        stages = event.get("stages") or {}
        breakdown = "/".join(f"{float(stages.get(s, 0)):.1f}" for s in STAGES)
        pages = event.get("pages") or "-"
        title = str(event.get("title") or event.get("document_id") or "")[:44]
        print(f"{float(event['seconds']):>7.1f}  {breakdown:<30}{pages:>6}  {title}")


def report_failures(events: list[dict[str, Any]]) -> None:
    """Grouped by error type: ten timeouts are one problem, not ten."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("event") == "document" and event.get("change") == "failed":
            groups[event.get("error_type") or "Unknown"].append(event)
        elif event.get("ok") is False and event.get("event") != "document":
            groups[event.get("error_type") or "Unknown"].append(event)

    if not groups:
        return

    _heading("FAILURES  (grouped by error type)")
    for error_type, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {error_type}  x{len(group)}")
        for event in group[:3]:
            detail = event.get("error") or event.get("title") or event.get("url") or ""
            print(f"      - [{event.get('event')}] {str(detail)[:70]}")
        if len(group) > 3:
            print(f"      ... and {len(group) - 3} more")


def report_ocr(events: list[dict[str, Any]]) -> None:
    """Accuracy signal, not a timing one, but it belongs in the same digest:
    a fast run that silently mangled amounts is not a good run."""
    repaired = substituted = unverifiable = affected = 0
    for event in events:
        if event.get("event") != "document":
            continue
        counts = (
            int(event.get("currency_repaired") or 0),
            int(event.get("currency_substituted") or 0),
            int(event.get("amounts_unverifiable") or 0),
        )
        if any(counts):
            affected += 1
        repaired, substituted, unverifiable = (
            repaired + counts[0], substituted + counts[1], unverifiable + counts[2]
        )

    if not affected:
        return

    _heading("OCR CURRENCY CONFIDENCE")
    print(f"  documents with findings : {affected}")
    print(f"  repaired (unambiguous)  : {repaired}")
    print(f"  substituted (auditable) : {substituted}")
    print(f"  unverifiable amounts    : {unverifiable}"
          f"{'   <-- review these before citing' if unverifiable else ''}")


# --------------------------------------------------------------------------- entry


def build_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Machine-readable form of the same numbers, for trend tracking."""
    stages: dict[str, Stat] = defaultdict(Stat)
    for event in events:
        if event.get("event") == "document":
            for stage, seconds in (event.get("stages") or {}).items():
                stages[stage].add(float(seconds))

    fetchers = _tally(events, "fetch", lambda e: e.get("fetcher", "?"))
    ends = [e for e in events if e.get("event") == "run_end"]

    return {
        "events": len(events),
        "runs": len({e.get("run_id") for e in events}),
        "documents": sum(int(e.get("documents") or 0) for e in ends),
        "failed": sum(int(e.get("failed") or 0) for e in ends),
        "wall_seconds": sum(float(e.get("seconds") or 0) for e in ends),
        "stages": {
            name: {"calls": s.count, "seconds": round(s.total, 2), "mean": round(s.mean, 3)}
            for name, s in stages.items()
        },
        "fetchers": {
            name: {"requests": s.count, "bytes": s.bytes, "seconds": round(s.total, 2)}
            for name, s in fetchers.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help=f"JSONL files (default: newest {DEFAULT_GLOB}).")
    parser.add_argument("--slowest", type=int, default=10, help="How many slow documents to list.")
    parser.add_argument("--json", help="Also write a machine-readable summary here.")
    args = parser.parse_args(argv)

    paths = resolve_paths(args.files)
    if not paths:
        print(
            f"No metrics files found (looked for {DEFAULT_GLOB}). "
            f"Runs write them when run.metrics.enabled is true.",
            file=sys.stderr,
        )
        return 2

    events = load(paths)
    if not events:
        print("Metrics files contained no events.", file=sys.stderr)
        return 2

    print("=" * 86)
    print(f"RUN METRICS  -  {len(events)} events from {describe(paths)}")
    print("=" * 86)

    report_runs(events)
    report_stages(events)
    report_fetchers(events)
    report_converters(events)
    report_sources(events)
    report_ocr(events)
    report_slowest(events, args.slowest)
    report_failures(events)
    print()

    if args.json:
        Path(args.json).write_text(json.dumps(build_summary(events), indent=2), encoding="utf-8")
        print(f"Summary -> {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
