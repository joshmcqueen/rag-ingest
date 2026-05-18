#!/usr/bin/env python3
"""PDF to image converter — Phase 1 of the RAG ingest pipeline."""

import argparse
import re
import sys
from pathlib import Path

try:
    import db as _db
except ImportError:
    _db = None

_BLANK_PAGE_RE = re.compile(
    r'this page (intentionally left blank|is intentionally left blank|left intentionally blank)',
    re.IGNORECASE,
)

def _is_intentionally_blank(text: str) -> bool:
    return bool(_BLANK_PAGE_RE.search(text))


def parse_pages(pages_arg: str, total_pages: int) -> list[int]:
    """Parse a page spec like '1-5' or '1,3,5' into a 0-based index list."""
    indices = set()
    for part in pages_arg.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            indices.update(range(int(start) - 1, int(end)))
        else:
            indices.add(int(part) - 1)
    valid = sorted(i for i in indices if 0 <= i < total_pages)
    if not valid:
        raise ValueError(f"No valid pages in '{pages_arg}' (PDF has {total_pages} pages)")
    return valid


def convert_pdf(
    pdf_path: Path,
    output_dir: Path,
    dpi: int,
    fmt: str,
    page_indices: list[int],
    skip_blank: bool = False,
    pdf_total_pages: int = None,
    pub_id: str = None,
    conn=None,
) -> None:
    import fitz  # pymupdf — imported here so the CLI can print a helpful error if missing

    doc = fitz.open(pdf_path)
    scale = dpi / 72  # PyMuPDF's native resolution is 72 DPI
    mat = fitz.Matrix(scale, scale)
    ext = "jpg" if fmt == "jpeg" else fmt
    stem = pdf_path.stem
    total = len(page_indices)
    skipped = 0
    _total = pdf_total_pages or total

    output_dir.mkdir(parents=True, exist_ok=True)

    for n, idx in enumerate(page_indices, start=1):
        page = doc[idx]
        is_blank = False

        if skip_blank:
            text = page.get_text()
            if _is_intentionally_blank(text):
                print(f"[{n}/{total}] page {idx + 1} → SKIPPED (intentionally blank)")
                skipped += 1
                is_blank = True

        if pub_id and conn and _db:
            _db.insert_page(conn, pub_id, idx + 1, _total, is_blank,
                            None if is_blank else str(output_dir / f"{stem}_page_{idx + 1:04d}.{ext}"))

        if is_blank:
            continue

        pix = page.get_pixmap(matrix=mat)
        out_file = output_dir / f"{stem}_page_{idx + 1:04d}.{ext}"
        pix.save(str(out_file))
        print(f"[{n}/{total}] page {idx + 1} → {out_file}")

    doc.close()

    if pub_id and conn and _db:
        _db.set_ingested(conn, pub_id, _total)

    saved = total - skipped
    suffix = f" ({skipped} blank page(s) skipped)" if skipped else ""
    print(f"\nDone. {saved} image(s) written to {output_dir}/{suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert PDF pages to images.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF file")
    parser.add_argument("--dpi", type=int, default=300, help="Render resolution")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"), help="Output directory"
    )
    parser.add_argument(
        "--format",
        choices=["png", "jpeg"],
        default="png",
        help="Image format",
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="Page range, e.g. '1-5' or '1,3,5' (default: all pages)",
    )
    parser.add_argument(
        "--skip-blank",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip pages whose text matches 'This page intentionally left blank' (and variants)",
    )
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"Error: file not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    try:
        import fitz
    except ImportError:
        print(
            "Error: pymupdf is not installed. Run: pip install pymupdf",
            file=sys.stderr,
        )
        sys.exit(1)

    doc = fitz.open(args.pdf)
    total_pages = len(doc)
    doc.close()

    if args.pages:
        try:
            page_indices = parse_pages(args.pages, total_pages)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        page_indices = list(range(total_pages))

    print(f"PDF: {args.pdf} ({total_pages} pages total)")
    print(f"Rendering {len(page_indices)} page(s) at {args.dpi} DPI as {args.format.upper()}\n")

    _conn = None
    _pub_id = None
    if _db:
        _conn = _db.get_db()
        pub = _db.get_pub_by_stem(_conn, args.pdf.stem)
        if pub:
            _pub_id = pub["pub_id"]
        else:
            print(f"  (no DB record for '{args.pdf.stem}' — run 'scrape.py sync-db' to enable tracking)\n")

    convert_pdf(
        pdf_path=args.pdf,
        output_dir=args.output_dir,
        dpi=args.dpi,
        fmt=args.format,
        page_indices=page_indices,
        skip_blank=args.skip_blank,
        pdf_total_pages=total_pages,
        pub_id=_pub_id,
        conn=_conn,
    )

    if _conn:
        _conn.close()


if __name__ == "__main__":
    main()
