# rag-ingest

A three-stage RAG (Retrieval-Augmented Generation) pipeline for Army publications:

```
scrape.py → ingest.py → extract.py
 web → PDFs   PDFs → images   images → markdown
```

Covers all 43 public-facing categories from the [Army Publishing Directorate](https://armypubs.army.mil) — field manuals, technical manuals, administrative regulations, training circulars, legal documents, and more.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and configure your LLM host and downloads path.

---

## Stage 1 — Scrape: web → PDFs

`scrape.py` crawls armypubs.army.mil and downloads PDFs organized by publication type.

### Quick start

```bash
# Step 1 — crawl all categories and write downloads/manifest.jsonl (no downloading yet)
python scrape.py build

# Step 2 — download every PDF listed in the manifest
python scrape.py download

# Print manifest and download coverage statistics at any time
python scrape.py stats
```

A full build + download covers thousands of publications and takes several hours. Use `--category` and `--limit` to test a subset first.

### `build` — scan and record PDF URLs

Crawls every category page, visits each publication's detail page, and records the PDF URL (or `null` if unavailable) into `manifest.jsonl`. Safe to interrupt and resume — already-processed pub IDs are skipped.

```bash
python scrape.py build
python scrape.py build --category training_doctrine/FM
python scrape.py build --category administrative/AR --limit 10
python scrape.py build --status ACTIVE
```

### `download` — pull PDFs from the manifest

Before making any requests, `download` classifies every manifest entry:

| Class | Action |
|---|---|
| Already on disk (non-empty file exists) | Skip |
| Permanent failure in log (`404`, `403`, `no_pdf`, `empty`) | Skip |
| Transient failure in log (`SSLError`, `503`, timeout, etc.) | **Retry** |
| Never attempted | Download |

Re-running `download` is safe — it always picks up where it left off.

```bash
python scrape.py download
python scrape.py download --category training_doctrine/FM
python scrape.py download --limit 10
python scrape.py download --delay 2.0   # slow down if rate-limited
```

**Example pre-flight output:**
```
Scanning manifest...

  Already on disk         :  4,393  (skipping)
  Permanent failures      :      1  (skipping — 404/403/no_pdf)
  Transient failures      :    738  (retrying)
  Never attempted         :      0  (new)
  ──────────────────────────────────
  Work this run           :    738
```

### `stats` — print manifest and download coverage

Reads `manifest.jsonl` and the on-disk file tree without making network requests.

```bash
python scrape.py stats
```

**Example output:**
```
=== Manifest Summary ===

  Total publications  : 14,953
  With PDF URL        : 5,132  (34.3%)
  No PDF URL          : 9,821  (65.7%)
  Already on disk     : 4,441  (86.5% of those with PDF)
  Still to download   :   691  (13.5% of those with PDF)

  By status:
    ACTIVE               : 14,759  (98.7%)
    INACTIVE             :    194  ( 1.3%)

  By category group:
    administrative         :  4,082  (27.3%)
    miscellaneous          :      1  ( 0.0%)
    technical_equipment    :  9,514  (63.6%)
    training_doctrine      :  1,356  ( 9.1%)

  Unique proponents   : 175
  Publication dates   : 15 Sep 1940 – 01 Jun 2026
```

### Scraper options

All three commands accept:

| Option | Default | Description |
|---|---|---|
| `--category` | all | Scope to one category (e.g. `training_doctrine/FM`) |
| `--status` | all | Filter by `ACTIVE`, `INACTIVE`, or `RESCINDED` |
| `--limit` | 0 (all) | Max publications per category |
| `--delay` | `1.5` | Seconds between requests |
| `--output` | `$DOWNLOADS_DIR` or `downloads/` | Base output directory |
| `--manifest` | `manifest.jsonl` | Manifest filename |

### Categories

#### Administrative
| `--category` | Publication Type |
|---|---|
| `administrative/AR` | Army Regulations |
| `administrative/ALARACT` | Army ALARACT Messages |
| `administrative/ArmyDir` | Army Directives |
| `administrative/AGO_active` | Army General Orders (Active) |
| `administrative/AGO_inactive` | Army General Orders (Inactive) |
| `administrative/DAMEMO` | DA Memorandums |
| `administrative/HQDA_Policy` | HQDA Policy Notices |
| `administrative/PAM` | DA Pamphlets |
| `administrative/POG` | Principal Officials' Guidance |
| `administrative/PPM` | Proponent Policy Memorandums |
| `administrative/Web_Series` | Administrative Series Collection |

#### Technical & Equipment
| `--category` | Publication Type |
|---|---|
| `technical_equipment/EM` | Electronic Media |
| `technical_equipment/FT` | Firing Tables |
| `technical_equipment/LO` | Lubrication Orders |
| `technical_equipment/MWO` | Modification Work Orders |
| `technical_equipment/SB` | Supply Bulletins |
| `technical_equipment/SC` | Supply Catalogs |
| `technical_equipment/TB` | Technical Bulletins |
| `technical_equipment/TM_1_8` | Technical Manuals (Range 1–8) |
| `technical_equipment/TM_9` | Technical Manuals (Range 9) |
| `technical_equipment/TM_10` | Technical Manuals (Range 10) |
| `technical_equipment/TM_11_4` | Technical Manuals (Range 11-4) |
| `technical_equipment/TM_11_5` | Technical Manuals (Range 11-5) |
| `technical_equipment/TM_11_6_7` | Technical Manuals (Range 11-6 & 7) |
| `technical_equipment/TM_14_750` | Technical Manuals (Range ≥14) |

#### Training & Doctrine
| `--category` | Publication Type |
|---|---|
| `training_doctrine/ADP` | Army Doctrine Publications |
| `training_doctrine/ADRP` | Army Doctrine Reference Publications |
| `training_doctrine/ATP` | Army Techniques Publications |
| `training_doctrine/ATTP` | Army Tactics, Techniques, and Procedures |
| `training_doctrine/CTA` | Common Tables of Allowance |
| `training_doctrine/FM` | Field Manuals |
| `training_doctrine/GTA` | Graphic Training Aids |
| `training_doctrine/JTA` | Joint Tables of Allowance |
| `training_doctrine/PB` | Professional Bulletins |
| `training_doctrine/STP` | Soldier Training Publications |
| `training_doctrine/TC` | Training Circulars |

#### Engineering
| `--category` | Publication Type |
|---|---|
| `engineering/TM` | Technical Manuals |
| `engineering/TB` | Technical Bulletins |

#### Medical
| `--category` | Publication Type |
|---|---|
| `medical/TM` | Technical Manuals |
| `medical/TB` | Technical Bulletins |
| `medical/SB` | Supply Bulletins |
| `medical/SC` | Supply Catalogs |

#### Miscellaneous
| `--category` | Publication Type |
|---|---|
| `miscellaneous/MCM` | Manuals for Courts-Martial |

### Scraper output files

**`downloads/manifest.jsonl`** — written by `build`. One JSON object per publication (including those with no PDF). Safe to inspect with `jq`.

```json
{
  "pub_id": "1031029",
  "pub_number": "FM 1",
  "category": "training_doctrine/FM",
  "status": "ACTIVE",
  "date": "04/16/2025",
  "title": "THE ARMY: A PRIMER TO OUR PROFESSION OF ARMS",
  "proponent": "OCSA",
  "pdf_url": "https://armypubs.army.mil/epubs/DR_pubs/DR_a/ARN43687-FM_1-000-WEB-2.pdf",
  "scanned_at": "2026-05-14T18:13:45.123456"
}
```

**`downloads/{category}/{filename}.pdf`** — written by `download`. Named using the official filename from the source URL.

**`downloads/download_log.jsonl`** — written by `download`. One entry per attempted download (appended across runs).

```json
{
  "pub_id": "1031029",
  "pub_number": "FM 1",
  "category": "training_doctrine/FM",
  "status": "ACTIVE",
  "pdf_url": "https://armypubs.army.mil/...",
  "local_path": "downloads/training_doctrine/FM/ARN43687-FM_1-000-WEB-2.pdf",
  "result": "downloaded",
  "bytes": 2457600,
  "timestamp": "2026-05-14T18:13:58.486163"
}
```

`result` values: `downloaded`, `skipped`, `no_pdf`, `http_404`, `http_503`, `error:SSLError`, `error:ConnectionError`, etc.

### Notes

- Only public/unclassified documents are available without a CAC. Some URLs redirect to DoD SSO (`federation.eams.army.mil`) and will fail with an SSL error — these are retried each run but won't resolve without a valid CAC session.
- Some ACTIVE publications have no downloadable PDF — they appear in the manifest with `"pdf_url": null` and are skipped by `download`.

---

## Stage 2 — Ingest: PDFs → images

`ingest.py` renders PDF pages as high-resolution images for the vision LLM in Stage 3.

```bash
# All pages at default 300 DPI
python ingest.py docs/file.pdf

# Custom DPI, format, or page range
python ingest.py docs/file.pdf --dpi 300
python ingest.py docs/file.pdf --format jpeg
python ingest.py docs/file.pdf --pages 1-5
python ingest.py docs/file.pdf --pages 1,3,5

# Custom output directory
python ingest.py docs/file.pdf --output-dir my-output/

# Blank page skipping is on by default; disable with --no-skip-blank
python ingest.py docs/file.pdf --no-skip-blank
```

| Option | Default | Description |
|---|---|---|
| `--dpi` | `300` | Render resolution |
| `--format` | `png` | Image format: `png` or `jpeg` |
| `--pages` | all | Page range, e.g. `1-5` or `1,3,5` |
| `--output-dir` | `output/` | Directory to write images into |
| `--skip-blank` / `--no-skip-blank` | on | Skip "This page intentionally left blank" pages |

Output files are named `<pdf-stem>_page_0001.png`, `_page_0002.png`, etc.

### Blank page detection

Military documents frequently contain "This page intentionally left blank." pages. Blank page skipping is **on by default** — pages are detected via PyMuPDF's text layer before rendering, so no image is written and no LLM call is made downstream. Pass `--no-skip-blank` to disable. Only exact phrase matches are skipped, so illustration-only pages and pages with short captions are unaffected.

---

## Stage 3 — Extract: images → markdown

`extract.py` sends page images to a vision LLM and writes structured markdown optimized for search and retrieval. Requires a running [LM Studio](https://lmstudio.ai/) or Ollama instance with a vision-capable model loaded.

```bash
# First 5 pages only (good for prompt tuning)
python extract.py --limit 5

# All pages
python extract.py

# Custom host or model
python extract.py --host http://192.168.0.58:1234 --model qwen3.6-35b --limit 10
```

| Option | Default | Description |
|---|---|---|
| `--input-dir` | `output/` | Directory of page images from Stage 2 |
| `--output-dir` | `markdown/` | Where to write `.md` files |
| `--host` | `$LLM_HOST` | LM Studio / Ollama base URL |
| `--model` | `$LLM_MODEL` | Model name |
| `--api-key` | `$LLM_API_KEY` | API key (`ollama` or `lm-studio`) |
| `--limit` | all | Process only the first N images |
| `--timeout` | `300` | Request timeout in seconds |
| `--retries` | `3` | Retry attempts for blank or failed responses |

Already-extracted pages are skipped automatically, so re-runs are safe.

---

## Roadmap

- [x] Stage 1 — web → PDFs (scrape.py)
- [x] Stage 2 — PDFs → images (ingest.py)
- [x] Stage 3 — images → markdown (extract.py)
- [ ] Stage 4 — markdown → vector store
