# OCR accuracy on NPCI circulars

NPCI publishes its circulars as **scanned images with no text layer**. Every
word the pipeline stores for them has been through OCR. These are banking
policy documents, so a wrong figure is worse than a missing one, and this
file records what was measured rather than what was assumed.

Reproduce any of it with:

```bash
python tools/ocr_benchmark.py --dump-dir /tmp/ocr_dump --json /tmp/ocr.json
```

Ground truth in `tools/ground_truth/` was transcribed by hand from the
rendered pages. Two sample PDFs is deliberate — they are the two distinct
shapes NPCI publishes, and each OCR run takes minutes.

---

## Finding 1 — docling's default PDF backend silently returns blank pages

The first symptom was every NPCI document failing with *"docling produced no
text … check that OCR is enabled"*.

The obvious hypothesis — OCR being skipped because docling detected a text
layer — **was tested and is wrong**. Forcing `OcrMode.FULL_PAGE` produced 0
characters, exactly as `DEFAULT` did.

What the measurements actually showed for `npci-upi-oc-226a.pdf`:

| Probe | Result |
|---|---|
| Text characters in the PDF | 0 |
| Embedded objects | 2 images |
| Page render | correct, fully legible |
| EasyOCR run directly on that render | 43 regions, correct text, 20.4s |
| docling conversion status | `SUCCESS`, `errors: []`, **0 texts, 0 pictures** |

docling reported success and returned nothing. The cause is the PDF backend,
not OCR:

| Backend | oc-186a | oc-226a |
|---|---|---|
| `DoclingParseV4` (docling default) | 3383 chars | **0 chars** |
| `PyPdfiumDocumentBackend` | 3372 chars | **1112 chars** |

A blank page with a `SUCCESS` status is the dangerous part: nothing in
docling's output distinguishes "this document is empty" from "this backend
could not read it".

**Applied:** `conversion.pdf.docling_local.pdf_backend: pypdfium`.

---

## Finding 2 — engine selection

Scored against hand-checked ground truth. CER and WER are computed after
normalising away layout, markdown decoration and unicode variants, so they
measure OCR errors rather than formatting differences.

| Config | mean CER | mean WER | total secs |
|---|---|---|---|
| **tesseract** | **0.109** | **0.150** | 27.4 |
| tesseract (full_page) | 0.109 | 0.150 | 21.9 |
| easyocr | 0.145 | 0.237 | 99.9 |
| easyocr (full_page) | 0.169 | 0.272 | 146.1 |
| rapidocr | 0.224 | 0.316 | 53.8 |
| easyocr + docling_parse | 0.557 | 0.618 | 106.5 |

Tesseract is both the most accurate and roughly 4x the fastest. Confirmed
end-to-end in the pipeline: the same two-document NPCI run takes **56s with
tesseract against 173s with easyocr**.

`full_page` mode scores the same as `default` and is not enabled — see
Finding 3 for why it is in fact slightly *worse* here.

**Applied:** `ocr_engine: tesseract`, `ocr_mode: default`.

Typical residual errors, none of which change a figure: `Subiect` for
`Subject`, `SirIMadam` for `Sir/Madam`, `AII` for `All`, `NPC!` for `NPCI`.

---

## Finding 3 — no OCR engine reads the rupee sign

This is the finding that matters, and no configuration fixes it.

Ground truth `₹5,000`, as read by each engine:

| Engine | Output | Failure mode |
|---|---|---|
| tesseract | `%5,000`, `¥5000`, `$5000` | wrong glyph, visibly wrong |
| tesseract (full_page) | `25,000` | **wrong amount, looks correct** |
| easyocr | `<5,000`, `<10,000` | wrong glyph |
| rapidocr | `5,000` | symbol dropped entirely |

Also tested and ruled out:

- **Render scale.** `ocr_options.scale` is already 3.0; raising it to 6.0
  changed nothing (`%5,000` at both).
- **Language data.** Installing `tesseract-ocr-hin` (Devanagari, which
  contains ₹) changed nothing.

The `25,000` case is the reason this needed more than a better engine. It is
a plausible-looking number that is simply wrong, and nothing downstream can
tell. It is also why `full_page` mode stays off.

### What the pipeline does about it

`src/policy_scraper/convert/ocr_repair.py` classifies every suspect numeric
span into three tiers by what can honestly be known:

| Tier | When | Action |
|---|---|---|
| `repaired` | glyph has **no valid reading** before a number (`%`, `°`, `~`) | rewritten to `₹`; cannot destroy a correct reading |
| `substituted` | glyph is a real currency sign or operator (`$`, `¥`, `£`, `<`) | rewritten to `₹`, **original preserved in front matter** so it is auditable and reversible |
| `unverifiable` | amount has no currency marker at all, in a money context | **never rewritten** — only counted and reported |

Bare numbers are only reported when a money cue (`limit`, `capped`,
`ceiling`, `enhanced`, `revised`, …) appears within the preceding clause.
Without that filter the report fills with years and circular numbers, and a
report nobody can read protects nobody.

Every finding lands in the document's YAML front matter:

```yaml
currency_repaired: 0
currency_substituted: 2
amounts_unverifiable: 0
findings:
  - confidence: substituted
    original: <5000
    replacement: ₹5000
    context: that per transaction limit is capped at <5000, up to which additional
```

`tools/analyze_metrics.py` aggregates these across a run.

### The actual guarantee

Not that OCR is correct — it demonstrably is not. The guarantee is that
**where OCR cannot be trusted, the document says so.** An agent reading
`amounts_unverifiable: 2` knows to cite the source PDF instead.

The one case this does not cover is an engine misreading digits into a
plausible different number with no glyph disturbance at all. `tools/
ocr_benchmark.py` exists to catch that class of regression when the engine,
backend or docling version changes — which is why `critical` token lists in
`tools/ocr_cases.yaml` name specific amounts and circular numbers.

---

## Before changing any OCR setting

1. Add the document to `tools/ocr_cases.yaml` with a hand-checked ground
   truth file and a `critical` list of its amounts and references.
2. Run `python tools/ocr_benchmark.py`.
3. Rank by critical-token recall first, CER second, speed last.
