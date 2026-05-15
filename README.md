# rag-ingest

A two-phase RAG (Retrieval-Augmented Generation) ingestion pipeline: PDF pages → images → markdown (via vision LLM).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# All pages at default 300 DPI
python ingest.py docs/file.pdf

# Custom DPI
python ingest.py docs/file.pdf --dpi 300

# JPEG output
python ingest.py docs/file.pdf --format jpeg

# Specific pages
python ingest.py docs/file.pdf --pages 1-5
python ingest.py docs/file.pdf --pages 1,3,5

# Custom output directory
python ingest.py docs/file.pdf --output-dir my-output/

# Blank page skipping is on by default; disable with --no-skip-blank
python ingest.py docs/file.pdf --no-skip-blank
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--dpi` | `300` | Render resolution |
| `--format` | `png` | Image format: `png` or `jpeg` |
| `--pages` | all | Page range, e.g. `1-5` or `1,3,5` |
| `--output-dir` | `output/` | Directory to write images into |
| `--skip-blank` / `--no-skip-blank` | on | Skip pages whose text matches "This page intentionally left blank" (and common variants) |

Output files are named `<pdf-stem>_page_0001.png`, `_page_0002.png`, etc.

## Phase 2 — Images → Markdown

Requires a running [LM Studio](https://lmstudio.ai/) instance with a vision-capable model loaded.

```bash
# First 5 pages only (good for prompt tuning)
python extract.py --limit 5

# All pages
python extract.py

# Custom host or model
python extract.py --host http://192.168.0.58:1234 --model qwen3.6-35b --limit 10
```

| Option | Default | Description |
|--------|---------|-------------|
| `--input-dir` | `output/` | Directory of page images from Phase 1 |
| `--output-dir` | `markdown/` | Where to write `.md` files |
| `--host` | `http://192.168.0.58:1234` | LM Studio base URL |
| `--model` | `qwen3.6-35b` | Model name as shown in LM Studio |
| `--limit` | all | Process only the first N images |

Already-extracted pages are skipped automatically, so re-runs are safe.

### Blank page detection

Military and government documents often contain many "This page intentionally left blank." pages. Blank page skipping is **on by default** in Phase 1 — these pages are detected via PyMuPDF's text layer before rendering, so no image file is written and no LLM call is made. Pass `--no-skip-blank` to disable. Only exact phrase matches are skipped, so illustration-only pages and pages with short captions are unaffected.

## Roadmap

- [x] Phase 1 — PDF → images
- [x] Phase 2 — Images → markdown (LM Studio vision)
- [ ] Phase 3 — Markdown → vector store
