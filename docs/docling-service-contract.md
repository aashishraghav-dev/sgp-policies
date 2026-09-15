# docling service contract

This is the interface the scraper expects when
`conversion.pdf.backend` is set to `docling_remote`. Implement it and the
scraper needs no code change — only the config block below.

The motivation for running docling as a service: it pulls in
torch/transformers (~4GB) and wants 2–4GB of RAM per worker, plus a GPU if
you have one. Keeping it out of the scraper image lets the two scale
independently, and lets other systems reuse the same parser.

The client lives in
[docling_remote.py](../src/policy_scraper/convert/docling_remote.py).

## Endpoints

### `POST {convert_path}` — convert one PDF

Default path `/v1/convert`. Request is `multipart/form-data`:

| Part      | Type                    | Notes                                            |
| --------- | ----------------------- | ------------------------------------------------ |
| `file`    | `application/pdf`       | Filename is the document title, for logging only |
| `options` | JSON string             | Currently `{"source_url": "https://..."}`         |

Treat `options` as advisory and forward-compatible: ignore keys you do not
recognise rather than rejecting the request.

If `api_key` is configured, it is sent in the `X-API-Key` header
(header name is configurable via `api_key_header`).

**Success — `200`, `application/json`:**

```json
{
  "markdown": "## Subject: ...\n\nWith reference to ...",
  "page_count": 2
}
```

The client accepts `markdown`, `md`, `content` or `text` for the body, and
`page_count`, `pages` or `num_pages` for the count, so docling-serve and a
thin in-house wrapper both work unmodified. Prefer `markdown` and
`page_count` for new services. `page_count` is optional; everything else in
the response is ignored.

The markdown must be the plain document text. The client normalises
whitespace and decodes HTML entities on receipt, so you do not need to.

### `GET {health_path}` — readiness

Default path `/health`. Any status `< 400` means healthy. The scraper calls
this before a run and logs a warning if it fails; it does not gate the run,
so return unhealthy while models are still loading.

## Error semantics

The status code decides whether the scraper retries, so map failures
carefully:

| Status  | Client behaviour                                                       |
| ------- | ---------------------------------------------------------------------- |
| `2xx`   | Parse the body as above                                                 |
| `4xx`   | Permanent. Document is marked failed; the run continues                |
| `5xx`   | Transient. Retried up to `max_retries` with exponential backoff        |
| timeout | Transient. Same retry path (`timeout_seconds` defaults to 900)          |

Put the reason in the response body — the first 200 characters are logged.

Use `4xx` for anything a retry cannot fix (encrypted PDF, corrupt file,
unsupported format) and `5xx` only for genuine service-side faults (model
not loaded, out of memory, queue full). A `5xx` on an undecodable PDF costs
three retries and a 15-minute timeout each.

## OCR is mandatory, and its settings are not free choices

NPCI circulars are scanned images with **no text layer at all**. Without an
OCR engine, docling returns an empty string and the scraper raises a
conversion error.

A service must match the local backend's configuration, because these
values were chosen by measurement and diverging from them changes the
stored text. Full evidence in [ocr-accuracy.md](ocr-accuracy.md).

```python
options = PdfPipelineOptions()
options.do_ocr = True
options.do_table_structure = True
options.table_structure_options.mode = TableFormerMode.ACCURATE
options.ocr_options = TesseractCliOcrOptions()     # measured most accurate
options.ocr_options.lang = ["eng"]
options.ocr_options.mode = OcrMode.DEFAULT         # full_page is worse here

DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
    pipeline_options=options,
    backend=PyPdfiumDocumentBackend,               # NOT docling's default
)})
```

**The backend is the critical one.** docling's default (`DoclingParseV4`)
returns a *blank page* for some NPCI scans — `ConversionStatus.SUCCESS`,
empty `errors`, zero text objects. A service built on the default will
report success and return nothing, and the only symptom downstream is a
document that vanishes.

A service that cannot match these should say so in its `/health` response
rather than silently substituting its own, so the mismatch is visible
before it reaches the corpus.

### Currency repair stays on the client

The scraper runs post-OCR currency repair itself
([ocr_repair.py](../src/policy_scraper/convert/ocr_repair.py)) — no OCR
engine tested reads `₹`. A service should return OCR output **unrepaired**;
doing it twice is harmless but doing it differently is not, and the
findings must be attributed to the document in the scraper's front matter.

## Throughput expectations

Measured on this project's CPU-only VM, no GPU, with the configuration
above:

- **~20 s per page** with tesseract. EasyOCR was ~3× slower for worse
  accuracy; a service choosing it should expect ~50 s for a 2-page circular.
- One-off model loading per process on top of that
- The scraper serialises conversions behind a lock in the local backend; a
  remote service is free to run them concurrently, which is the main reason
  to move to one

Size the service's own concurrency to its RAM: 2–4 GB per in-flight
document.

## Configuration

```yaml
conversion:
  pdf:
    backend: docling_remote
    docling_remote:
      base_url: ${DOCLING_URL:http://localhost:8080}
      convert_path: /v1/convert
      health_path: /health
      timeout_seconds: 900
      max_retries: 3
      backoff_seconds: 5.0
      # api_key: ${DOCLING_API_KEY:}
      # api_key_header: X-API-Key
```

With this backend selected the scraper never imports docling, so the
`[docling]` extra can be dropped from the deployment image.
