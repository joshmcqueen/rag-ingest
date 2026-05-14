#!/usr/bin/env python3
"""PDF to image converter — Phase 1 of the RAG ingest pipeline."""

import argparse
import sys
from pathlib import Path


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
) -> None:
    import fitz  # pymupdf — imported here so the CLI can print a helpful error if missing

    doc = fitz.open(pdf_path)
    scale = dpi / 72  # PyMuPDF's native resolution is 72 DPI
    mat = fitz.Matrix(scale, scale)
    ext = "jpg" if fmt == "jpeg" else fmt
    stem = pdf_path.stem
    total = len(page_indices)

    output_dir.mkdir(parents=True, exist_ok=True)

    for n, idx in enumerate(page_indices, start=1):
        page = doc[idx]
        pix = page.get_pixmap(matrix=mat)
        out_file = output_dir / f"{stem}_page_{idx + 1:04d}.{ext}"
        pix.save(str(out_file))
        print(f"[{n}/{total}] page {idx + 1} → {out_file}")

    doc.close()
    print(f"\nDone. {total} image(s) written to {output_dir}/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert PDF pages to images.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF file")
    parser.add_argument("--dpi", type=int, default=150, help="Render resolution")
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

    convert_pdf(
        pdf_path=args.pdf,
        output_dir=args.output_dir,
        dpi=args.dpi,
        fmt=args.format,
        page_indices=page_indices,
    )


if __name__ == "__main__":
    main()
