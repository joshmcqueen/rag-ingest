#!/usr/bin/env python3
"""Combine per-page markdowns for a publication into a single document with page provenance."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db

_HERE = Path(__file__).parent


def combine_pub(conn, pub_id: str, output_dir: Path) -> Path:
    pub = conn.execute("SELECT * FROM publications WHERE pub_id = ?", (pub_id,)).fetchone()
    if not pub:
        raise ValueError(f"Publication not found: {pub_id}")

    pages = db.get_pages_for_pub(conn, pub_id)
    extractable = [p for p in pages if not p["is_blank"] and p["markdown_path"]]
    if not extractable:
        raise ValueError(f"No extracted pages found for pub_id={pub_id}. Run extract.py first.")

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{pub['pdf_stem']}.md"
    now = datetime.now(timezone.utc).isoformat()
    total = pub["total_pages"] or len(pages)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f'pub_id: "{pub["pub_id"]}"\n')
        f.write(f'pub_number: "{pub["pub_number"]}"\n')
        f.write(f'title: "{pub["title"]}"\n')
        f.write(f'category: "{pub["category"]}"\n')
        f.write(f"total_pages: {total}\n")
        f.write(f'combined_at: "{now}"\n')
        f.write("---\n\n")

        for page in pages:
            if page["is_blank"] or not page["markdown_path"]:
                continue
            md_path = _HERE / page["markdown_path"]
            if not md_path.exists():
                print(f"  Warning: missing markdown file {md_path} (skipping page {page['page_number']})")
                continue
            content = md_path.read_text(encoding="utf-8").strip()
            f.write(f'<!-- page_start: {page["page_number"]}, total_pages: {page["total_pages"]}, pub_id: {pub["pub_id"]} -->\n')
            f.write(content)
            f.write(f'\n<!-- page_end: {page["page_number"]} -->\n\n')

    db.set_combined(conn, pub_id)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Combine per-page markdowns into a single document with page provenance.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pub-id", help="Publication ID (from manifest)")
    group.add_argument("--pub-stem", help="PDF filename stem (e.g. ARN44282-ATP_3-20.15-000-WEB-1)")
    parser.add_argument("--output-dir", type=Path, default=Path("combined"), help="Output directory")
    args = parser.parse_args()

    conn = db.get_db()

    if args.pub_stem:
        pub = db.get_pub_by_stem(conn, args.pub_stem)
        if not pub:
            print(f"Error: no publication found with stem '{args.pub_stem}'", file=sys.stderr)
            sys.exit(1)
        pub_id = pub["pub_id"]
    else:
        pub_id = args.pub_id

    try:
        out_path = combine_pub(conn, pub_id, args.output_dir)
        print(f"Written: {out_path}")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
