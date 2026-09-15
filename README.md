# policy-scraper

Crawls regulator websites, converts what it finds to markdown, and keeps a
versioned archive of it in object storage (GCS or the local filesystem).

The archive is the point: downstream agents read the markdown to answer
questions against current regulation, so the job is to keep it fresh and to
know precisely what changed.

Sources today: **RBI Master Directions** and **NPCI circulars** (UPI, IMPS,
AePS). Adding a third is one class and one YAML block — see
[Adding a source](#adding-a-source).

For the component layout and the run flow as diagrams, see
[docs/architecture.md](docs/architecture.md).

---

## Quick start

```bash
uv venv
uv pip install -e '.[docling]'      # omit [docling] if using a docling service
.venv/bin/playwright install chromium

python run_scrape.py --dry-run      # discover + diff, write nothing
python run_scrape.py                # full run
```

On a bare Linux box Chromium needs system libraries:

```bash
sudo apt-get install -y libnspr4 libnss3 libasound2t64 libatk1.0-0t64 \
  libatk-bridge2.0-0t64 libcups2t64 libdrm2 libgbm1 libxkbcommon0 \
  libpango-1.0-0 libcairo2
```

Output lands in `./data/object-store/policies/` by default.

[run_scrape.py](run_scrape.py) is the manual runner. It is a thin wrapper
around the CLI, there so you can run the scraper without installing the
package; a scheduler would call `policy-scraper` (or
`python -m policy_scraper.cli`) directly.

### Useful flags

| Flag                     | Effect                                            |
| ------------------------ | ------------------------------------------------- |
| `--source rbi`           | Run one source (repeatable)                       |
| `--dry-run`              | Discover and diff, write nothing                  |
| `--force`                | Ignore the catalog, re-convert everything         |
| `--max-categories N`     | Override the per-source category cap              |
| `--max-documents N`      | Override the per-category document cap            |
| `--list-sources`         | Print configured sources and registered types     |
| `--log-level DEBUG`      | Verbose                                           |

Exit code is `0` on success, `1` if any document failed, `2` on a config or
startup error — so a job runner can tell a bad run from a clean one.

---

## What ends up in the bucket

```
policies/
  _index.yaml                        roll-up of every category
  rbi/
    commercial-banks/
      _manifest.yaml                 metadata for every doc below
      documents/
        reserve-bank-...-rbi-md-13141.md
  npci/
    upi/
      _manifest.yaml
      documents/
        upi-oc-no-186a-...-npci-upi-3963.md
        upi-oc-no-186a-...-npci-upi-3963.v2.md
```

Keys are built in one place,
[catalog/layout.py](src/policy_scraper/catalog/layout.py) — change the
layout there, not by string-building elsewhere.

Every markdown file also carries YAML front matter (document id, title,
source URL, publication date, content hash, converter, version), so a file
is self-describing even when read outside the manifest.

### The manifests

`_manifest.yaml` per category is the catalog the incremental logic reads.
Per document it records `revision_key` (the change signal),
`content_sha256` (the converted output), `version`, `first_seen_at`,
`last_updated_at`, and the source-specific `extra` block.

`_index.yaml` at the root is a roll-up: one line per category with its
manifest path, document count and last run time. It exists so a consumer
can find everything without listing the bucket.

---

## How updates are decided

The scraper diffs *before* downloading. Each connector computes a
`revision_key` from signals visible on the listing page alone — for RBI the
title, PDF URL, size label and date; for NPCI the CMS content hash embedded
in the upload path. If the key matches the manifest, the document is
skipped without ever being fetched. This is what keeps a re-run cheap.

Two update strategies, chosen per source in config:

**`replace_in_place`** (RBI). A Master Direction is a living document: an
amendment changes the same document. The markdown is overwritten at a
stable key, and the manifest version increments so you can see it moved.
Path stays constant so downstream links never break.

**`new_version`** (NPCI). A circular is immutable; an amendment is a *new*
circular. Revisions are written alongside as `.v2.md`, `.v3.md`, and
history is preserved.

There is a second safety net after conversion: if the re-converted markdown
hashes identically to what is stored, an `updated` is downgraded to
`unchanged` and nothing is written. A cosmetic tweak upstream does not
churn the bucket.

---

## Per-site transport

Sites differ in how hostile they are, so the fetcher is chosen per source:

**RBI — plain HTTP** (`fetcher: http`). httpx is enough. Detail pages carry
the full text of each direction as HTML.

**NPCI — browser** (`fetcher: browser`). npci.org.in sits behind Akamai Bot
Manager and returns 403 to every non-browser client. Worth recording what
was actually measured, because the obvious workarounds do not work:

- `curl` / `httpx`, even with a complete set of browser headers → 403
- Playwright navigation → 200
- Playwright `APIRequestContext` replaying the browser's own cookies → 403

The cookies are not sufficient; TLS and HTTP/2 fingerprinting are applied
too. So *every* NPCI request — the JSON API and the PDF binaries — is
issued from inside the page via `page.evaluate` + `fetch()`, with binaries
returned as chunked base64. One navigation per origin plants the clearance
cookies for the whole run. See
[fetch/browser.py](src/policy_scraper/fetch/browser.py).

The Playwright sync API is thread-bound, so `BrowserFetcher` sets
`supports_concurrency = False` and the orchestrator drops to one worker for
that source automatically. Nothing to configure.

---

## Conversion

**HTML first, PDF as fallback.** RBI publishes the complete text as markup,
and HTML→markdown is lossless, needs no OCR and is ~1000× cheaper than
running a PDF through docling. The RBI connector only falls back to the PDF
when the extracted HTML body is implausibly short (`min_html_chars`).

RBI nests entire documents inside layout tables. Converted naively, a whole
direction collapses into a single GFM table cell and every paragraph
boundary is lost. [convert/html.py](src/policy_scraper/convert/html.py)
detects layout tables — the discriminator is that a genuine data table
holds only inline content in its cells — and rewrites them as divs, while
leaving real nested data tables (indexes, rate schedules) intact.

**NPCI needs OCR, unavoidably.** The circulars are scanned images with no
text layer at all. Without an OCR engine docling returns an empty string.
Budget roughly 20 s per page on CPU, plus one-off model loading.

Three OCR settings are **measured, not preferred**, and changing any of them
without re-running the benchmark will quietly degrade accuracy:

| Setting | Value | Why |
|---|---|---|
| `pdf_backend` | `pypdfium` | docling's default returns a *blank page* — `SUCCESS` status, no error, zero text — for some NPCI scans |
| `ocr_engine` | `tesseract` | CER 0.109 / WER 0.150 vs easyocr's 0.145 / 0.237, and ~4× faster |
| `ocr_mode` | `default` | `full_page` scores no better and misread `₹5,000` as `25,000` |

**No OCR engine tested reads the rupee sign.** They emit `%`, `¥`, `$`, `<`
or nothing at all. Raising the render scale and adding Devanagari language
data changed nothing. Since a wrong amount in a banking policy is worse than
a missing one, [convert/ocr_repair.py](src/policy_scraper/convert/ocr_repair.py)
repairs the spans where a rewrite cannot destroy a correct reading, records
the ones it rewrites so they stay auditable, and **flags rather than guesses**
the amounts it cannot verify. The findings travel in each document's front
matter, so an agent reading `amounts_unverifiable: 2` knows to cite the PDF.

Full evidence, including the hypotheses that were tested and disproved:
[docs/ocr-accuracy.md](docs/ocr-accuracy.md). Re-run the benchmark with
`python tools/ocr_benchmark.py` before touching any of it.

### Running docling as a service

`conversion.pdf.backend` switches between `docling_local` (in-process) and
`docling_remote` (HTTP). It is config only — no code changes — and with the
remote backend the scraper never imports docling, so the ~4GB of ML
dependencies can be dropped from the image entirely.

The wire format is specified in
[docs/docling-service-contract.md](docs/docling-service-contract.md).

---

## Configuration

Everything lives in [config/sources.yaml](config/sources.yaml), which is
commented throughout. Values support `${VAR}` and `${VAR:default}` env
interpolation, so the same file works across environments.

### Switching to GCS

```bash
export STORAGE_BACKEND=gcs
export GCS_BUCKET=your-bucket-name
export GCP_PROJECT=your-project
```

Authentication uses Application Default Credentials; set
`storage.gcs.credentials_path` to point at a service-account key instead.
The key layout is identical to local, so local runs are a faithful
rehearsal.

### Current milestone limits

Deliberately capped for the first milestone, not hardcoded — RBI categories
are discovered from the page (19 unique, 377 documents available) and the
first 5 are taken in page order:

```yaml
rbi:   max_categories: 5,  max_documents_per_category: 10,  include: []
npci:  max_categories: 3,  max_documents_per_category: 10,  include: [upi, imps, aeps]
```

Raise the caps or list categories in `include` to widen the crawl. Nothing
else changes.

---

## Run metrics

Every run appends JSON lines to `metrics/run-{date}-{run_id}.jsonl` — one row
per request, conversion, write, document, category and run. Analysis is a
separate program, so it can change without touching the pipeline:

```bash
python tools/analyze_metrics.py                # newest run
python tools/analyze_metrics.py metrics/       # every run on disk
python tools/analyze_metrics.py --slowest 20 --json summary.json
```

```
STAGE BREAKDOWN  (summed document time, not wall clock)
stage           calls     total   share     mean   median      p95
fetch               2      0.5s     1%     0.2s     0.2s     0.3s
convert             2     47.2s    99%    23.6s    23.6s    27.6s  ####################
store               2      0.0s     0%     0.0s     0.0s     0.0s
```

It breaks a run down by stage, transport (requests, bytes, latency, req/s),
converter (s/page), source × category, slowest documents, failures grouped by
error type, and OCR currency confidence. The headline for NPCI: conversion is
~99% of document cost and fetching is noise, so any optimisation that is not
about OCR is not worth doing.

`fetch` events are recorded in `Fetcher.get()` in the base class, so a new
transport is measured the moment it exists — subclasses implement `_fetch`
and never think about metrics. Disable with `run.metrics.enabled: false`.
Details in [docs/metrics.md](docs/metrics.md).

---

## Tests

```bash
uv pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The suite is offline by design and runs in about a second. Connectors take
a `Fetcher`, so a stub serving canned responses exercises all the parsing
and diffing without touching either regulator's site. What it pins is the
behaviour that would otherwise only fail against the live site or, worse,
fail silently:

- layout-table detection, including the case where a genuine data table is
  nested inside a layout wrapper
- RBI category headers vs date sub-headers, which share a CSS class
- `revision_key` stability, and that it moves when a document is republished
- `version` vs `path_version` under both update strategies
- NPCI items with no attached file
- that `config/sources.yaml` itself still loads and keeps OCR enabled
- post-OCR currency repair, against literals taken verbatim from real engine
  output — including that `25,000` misread from `₹5,000` is flagged and
  *never* rewritten
- that a metrics write failure disables recording instead of killing the run

Two of these were written, then deliberately broken to confirm they fail:
reverting the HTML-entity fix, the layout-table parent check, the OCR audit
tiers and the currency year-guard each failed exactly the tests that claim to
cover them. A test that passes for the wrong reason was found this way — a
trailing full stop, not the year guard, was what made an assertion pass — and
that bug is now fixed in both the code and the test.

---

## Adding a source

Four plugin points, each a registry keyed by a string used in config:
source connectors, fetchers, converters, object stores. A new website means
subclassing `SourceConnector` and adding a YAML block.

```python
@source_registry.register("example.notices")
class ExampleConnector(SourceConnector[ExampleOptions]):
    options_model = ExampleOptions

    def discover_categories(self) -> list[Category]: ...
    def discover_documents(self, category) -> Iterable[DocumentRef]: ...
    def fetch_payload(self, ref) -> ContentPayload: ...
```

Site-specific knowledge — selectors, URL templates, pagination — belongs in
the connector's `options_model` so it is configurable rather than buried.
The pipeline, catalog, conversion and storage layers stay untouched.

---

## Layout

```
src/policy_scraper/
  core/        domain models and errors; the vocabulary every layer speaks
  config/      pydantic config schema + YAML loader with env interpolation
  fetch/       Fetcher ABC, httpx and Playwright implementations
  sources/     per-site connectors (rbi/, npci/)
  convert/     HTML and PDF (docling local/remote) converters + router
  catalog/     manifests, change detection, storage layout
  storage/     ObjectStore ABC, local and GCS backends
  pipeline/    orchestrator and run report
  utils/       registry, hashing, slugs, logging
```

---

## Scheduling

Not implemented, by design. The CLI is built to be the scheduled unit: it
takes no interactive input, is safe to re-run (unchanged documents are
skipped without being fetched), and signals failure through its exit code.
Point Cloud Scheduler at a Cloud Run job invoking `policy-scraper`, a few
times a week.
