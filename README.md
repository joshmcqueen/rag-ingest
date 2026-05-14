# rag-ingest

A RAG (Retrieval-Augmented Generation) ingestion pipeline. Built incrementally — Phase 1 converts PDF pages to images.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# All pages at default 150 DPI
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
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--dpi` | `150` | Render resolution |
| `--format` | `png` | Image format: `png` or `jpeg` |
| `--pages` | all | Page range, e.g. `1-5` or `1,3,5` |
| `--output-dir` | `output/` | Directory to write images into |

Output files are named `<pdf-stem>_page_0001.png`, `_page_0002.png`, etc.

## Roadmap

- [x] Phase 1 — PDF → images
- [ ] Phase 2 — Images → markdown (Claude vision)
- [ ] Phase 3 — Markdown → vector store
