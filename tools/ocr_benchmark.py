#!/usr/bin/env python3
"""Score docling OCR configurations against hand-checked ground truth.

Run this when choosing or changing an OCR engine, not on every scrape. It
exists because these are banking policy documents: a configuration that
reads "₹5,000" as "<5000" is worse than useless, and that is not visible
from a spot check of the markdown.

    python tools/ocr_benchmark.py                    # every engine, every case
    python tools/ocr_benchmark.py -e easyocr tesseract
    python tools/ocr_benchmark.py --json out.json

Deliberately standalone: it imports docling directly rather than going
through the scraper, so a configuration can be evaluated before it is
adopted in config/sources.yaml.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

TOOLS_DIR = Path(__file__).resolve().parent

# Each entry is a full docling configuration, not just an engine name --
# the PDF backend turned out to matter more than the engine.
CONFIGS: dict[str, dict[str, str]] = {
    "easyocr": {"engine": "easyocr", "backend": "pypdfium", "mode": "default"},
    "easyocr-dlparse": {"engine": "easyocr", "backend": "docling_parse", "mode": "default"},
    "easyocr-fullpage": {"engine": "easyocr", "backend": "pypdfium", "mode": "full_page"},
    "tesseract": {"engine": "tesseract", "backend": "pypdfium", "mode": "default"},
    "tesseract-fullpage": {"engine": "tesseract", "backend": "pypdfium", "mode": "full_page"},
    "rapidocr": {"engine": "rapidocr", "backend": "pypdfium", "mode": "default"},
}


# --------------------------------------------------------------------------- scoring


def levenshtein(a: str, b: str) -> int:
    """Edit distance, iterative with a single row of state."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def normalise(text: str) -> str:
    """Collapse differences that are not OCR errors.

    Line breaks, markdown decoration and repeated spaces are layout, not
    content, and penalising them would drown out the errors that matter.
    Unicode is NFKC-folded so a composed and decomposed rupee sign compare
    equal, and the common typographic quote/dash variants are unified.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"',
                                         "”": '"', "–": "-", "—": "-",
                                         " ": " "}))
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)  # docling image markers
    text = re.sub(r"[#*_`|]+", " ", text)                      # markdown decoration
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@dataclass
class CaseResult:
    case_id: str
    config: str
    chars: int
    seconds: float
    cer: float
    wer: float
    critical_found: list[str] = field(default_factory=list)
    critical_missing: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def critical_recall(self) -> float:
        total = len(self.critical_found) + len(self.critical_missing)
        return len(self.critical_found) / total if total else 1.0


def score(output: str, truth: str, critical: list[str]) -> tuple[float, float, list, list]:
    hypothesis, reference = normalise(output), normalise(truth)

    cer = levenshtein(hypothesis, reference) / max(len(reference), 1)
    wer = levenshtein_words(hypothesis.split(), reference.split()) / max(len(reference.split()), 1)

    # Critical tokens are matched against the normalised text so that
    # spacing and markdown do not cause a false miss.
    found, missing = [], []
    for token in critical:
        (found if normalise(token) in hypothesis else missing).append(token)

    return min(cer, 1.0), min(wer, 1.0), found, missing


def levenshtein_words(a: list[str], b: list[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, wa in enumerate(a, start=1):
        current = [i]
        for j, wb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (wa != wb))
            )
        previous = current
    return previous[-1]


# --------------------------------------------------------------------------- conversion


def build_converter(config: dict[str, str]):
    """Construct a docling converter for one benchmark configuration."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        EasyOcrOptions,
        OcrMode,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    engines = {
        "easyocr": EasyOcrOptions,
        "tesseract": TesseractCliOcrOptions,
        "rapidocr": RapidOcrOptions,
    }

    options = PdfPipelineOptions()
    options.do_ocr = True
    options.do_table_structure = True
    options.table_structure_options.mode = TableFormerMode.ACCURATE

    ocr_options = engines[config["engine"]]()
    if hasattr(ocr_options, "lang"):
        # Tesseract spells English "eng"; the others use "en".
        ocr_options.lang = ["eng"] if config["engine"] == "tesseract" else ["en"]
    ocr_options.mode = OcrMode(config["mode"])
    options.ocr_options = ocr_options

    format_kwargs = {"pipeline_options": options}
    if config["backend"] == "pypdfium":
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

        format_kwargs["backend"] = PyPdfiumDocumentBackend

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(**format_kwargs)}
    )


def run_case(converter, pdf_path: Path) -> tuple[str, float]:
    started = time.perf_counter()
    result = converter.convert(pdf_path)
    return result.document.export_to_markdown(), time.perf_counter() - started


# --------------------------------------------------------------------------- reporting


def print_report(results: list[CaseResult], cases: list[dict]) -> None:
    pages = {c["id"]: c.get("pages", 1) for c in cases}

    print("\n" + "=" * 100)
    print("OCR BENCHMARK")
    print("=" * 100)
    print(
        f"{'config':<20}{'case':<22}{'chars':>7}{'CER':>8}{'WER':>8}"
        f"{'critical':>10}{'secs':>8}{'s/page':>8}"
    )
    print("-" * 100)

    for result in results:
        if result.error:
            print(f"{result.config:<20}{result.case_id:<22}  ERROR: {result.error[:44]}")
            continue
        per_page = result.seconds / max(pages.get(result.case_id, 1), 1)
        print(
            f"{result.config:<20}{result.case_id:<22}{result.chars:>7}"
            f"{result.cer:>8.3f}{result.wer:>8.3f}"
            f"{len(result.critical_found)}/{len(result.critical_found) + len(result.critical_missing):>8}"
            f"{result.seconds:>8.1f}{per_page:>8.1f}"
        )

    print("-" * 100)
    print("\nAGGREGATE  (ranked by critical-token recall, then CER)")
    print("-" * 100)
    print(f"{'config':<20}{'mean CER':>10}{'mean WER':>10}{'critical':>12}{'total secs':>12}")

    by_config: dict[str, list[CaseResult]] = {}
    for result in results:
        by_config.setdefault(result.config, []).append(result)

    rows = []
    for config, group in by_config.items():
        ok = [r for r in group if not r.error]
        if not ok:
            rows.append((config, 1.0, 1.0, 0.0, 0.0, len(group)))
            continue
        found = sum(len(r.critical_found) for r in ok)
        total = found + sum(len(r.critical_missing) for r in ok)
        rows.append((
            config,
            sum(r.cer for r in ok) / len(ok),
            sum(r.wer for r in ok) / len(ok),
            found / total if total else 1.0,
            sum(r.seconds for r in ok),
            len(group) - len(ok),
        ))

    for config, cer, wer, recall, secs, failures in sorted(rows, key=lambda r: (-r[3], r[1])):
        suffix = f"   ({failures} failed)" if failures else ""
        print(f"{config:<20}{cer:>10.3f}{wer:>10.3f}{recall:>11.0%}{secs:>12.1f}{suffix}")

    print("-" * 100)

    missing = {}
    for result in results:
        for token in result.critical_missing:
            missing.setdefault(token, []).append(result.config)
    if missing:
        print("\nCRITICAL TOKENS LOST  (these are the ones that make a document unsafe to cite)")
        for token, configs in sorted(missing.items()):
            print(f"  {token!r:<34} lost by: {', '.join(sorted(set(configs)))}")
    print()


# --------------------------------------------------------------------------- entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-e", "--engines", nargs="+", choices=sorted(CONFIGS), default=sorted(CONFIGS),
        help="Configurations to benchmark.",
    )
    parser.add_argument("-c", "--cases", nargs="+", help="Case ids to run (default: all).")
    parser.add_argument("--cases-file", default=str(TOOLS_DIR / "ocr_cases.yaml"))
    parser.add_argument("--json", help="Also write raw results here.")
    parser.add_argument("--dump-dir", help="Write each conversion's markdown here for eyeballing.")
    args = parser.parse_args(argv)

    spec = yaml.safe_load(Path(args.cases_file).read_text(encoding="utf-8"))
    cases = spec["cases"]
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases]
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    results: list[CaseResult] = []

    for config_name in args.engines:
        config = CONFIGS[config_name]
        print(f"\n>>> {config_name}: {config}", flush=True)
        try:
            converter = build_converter(config)
        except Exception as exc:
            print(f"    unavailable: {exc}", file=sys.stderr)
            for case in cases:
                results.append(
                    CaseResult(case["id"], config_name, 0, 0.0, 1.0, 1.0, error=str(exc))
                )
            continue

        for case in cases:
            pdf = TOOLS_DIR / case["pdf"]
            truth = (TOOLS_DIR / case["ground_truth"]).read_text(encoding="utf-8")
            print(f"    {case['id']} ...", end="", flush=True)
            try:
                markdown, seconds = run_case(converter, pdf)
            except Exception as exc:
                print(f" ERROR {exc}")
                results.append(
                    CaseResult(case["id"], config_name, 0, 0.0, 1.0, 1.0, error=str(exc))
                )
                continue

            cer, wer, found, missing = score(markdown, truth, case.get("critical", []))
            results.append(
                CaseResult(
                    case_id=case["id"], config=config_name, chars=len(markdown),
                    seconds=round(seconds, 2), cer=round(cer, 4), wer=round(wer, 4),
                    critical_found=found, critical_missing=missing,
                )
            )
            print(f" {seconds:.1f}s  CER={cer:.3f}  critical={len(found)}/{len(found) + len(missing)}")

            if args.dump_dir:
                out = Path(args.dump_dir)
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{case['id']}.{config_name}.md").write_text(markdown, encoding="utf-8")

    print_report(results, cases)

    if args.json:
        Path(args.json).write_text(
            json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
        )
        print(f"Raw results -> {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
