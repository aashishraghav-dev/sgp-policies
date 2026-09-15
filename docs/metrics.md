# Run metrics

Every run appends newline-delimited JSON to `metrics/run-{date}-{run_id}.jsonl`.
The scraper only writes them; analysis is a separate program, so it can change
without touching the pipeline.

```bash
python tools/analyze_metrics.py                      # newest run
python tools/analyze_metrics.py metrics/            # every run on disk
python tools/analyze_metrics.py --slowest 20 --json summary.json
```

Turn it off with `run.metrics.enabled: false`, or point it somewhere else with
`run.metrics.path` (accepts `{run_id}` and `{date}`; a fixed name appends
across runs, which is what you want for trend tracking).

## Why JSONL

The interesting questions are not known in advance — *which stage dominates*,
*is the browser the bottleneck or is it OCR*, *which documents are outliers*.
All of those are aggregations over per-event rows, so the rows are what gets
written. A pre-computed summary would have to guess the question.

## Events

| Event | One per | Key fields |
|---|---|---|
| `run_start` | run | `sources`, `dry_run`, `max_workers`, `storage_backend` |
| `fetch` | HTTP/browser request | `fetcher`, `url`, `status`, `bytes`, `seconds`, `ok` |
| `convert` | conversion | `converter`, `pages`, `chars`, `seconds` |
| `store` | object written | `backend`, `path`, `bytes`, `seconds` |
| `document` | document | `change`, `stages{fetch,convert,store}`, OCR confidence counts |
| `category` | source × category | `discovered`, `needed_work`, `outcomes{}`, `seconds` |
| `run_end` | run | `documents`, `created`/`updated`/`unchanged`/`failed`, `seconds` |

Every row also carries `ts`, `run_id`, `schema_version`.

`fetch` is recorded in `Fetcher.get()` in the base class, not in each
transport, so a new fetcher is measured the moment it exists — subclasses
implement `_fetch` and never think about metrics.

## Reading the output

```
STAGE BREAKDOWN  (summed document time, not wall clock)
stage           calls     total   share     mean   median      p95
fetch               2      0.5s     1%     0.2s     0.2s     0.3s
convert             2     47.2s    99%    23.6s    23.6s    27.6s  #####################
store               2      0.0s     0%     0.0s     0.0s     0.0s
```

Stage totals are **summed per document, not elapsed** — with concurrency they
exceed wall time. They answer "what does a document cost", not "how long did
the run take"; `RUNS` answers the latter.

The headline result for NPCI: conversion is ~99% of document cost and fetching
is noise. Any optimisation that is not about OCR is not worth doing. For RBI,
which serves HTML, the shape inverts.

## Failure handling

A metrics failure must never break a scrape, so write errors disable recording
(logged once) and are otherwise swallowed. `NullRecorder` is substituted when
metrics are off, so no call site ever checks for `None`.

The analyzer skips unparseable lines rather than refusing the file, so a run
killed mid-write still yields a report for everything before the tear.
