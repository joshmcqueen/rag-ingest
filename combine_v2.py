#!/usr/bin/env python3
"""combine_v2.py — Enhanced publication combiner for Army RAG pipeline.

Reads per-page markdown from the SQLite pipeline.db + flat markdown/ layout,
applies cleanup/dedup/normalization, and writes one consolidated .md per pub.

Usage:
    python combine_v2.py --pub-id 1031408 --output-dir ./combined_markdown \\
        --report-dir ./reports --verbose
    python combine_v2.py --all --output-dir ./combined_markdown \\
        --report-dir ./reports --workers 8
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("Error: pyyaml required. Run: pip install pyyaml>=6.0", file=sys.stderr)
    sys.exit(1)

import db as dbmodule

_HERE = Path(__file__).parent

# ── Regex constants ────────────────────────────────────────────────────────────

_RE_BLANK_PAGE = re.compile(r"^\s*(?:<!--\s*Empty\s*-->)?\s*$", re.IGNORECASE | re.DOTALL)
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
# All-caps bold line on its own: **SECTION TITLE** or **CAPABILITIES**
_RE_BOLD_HEADING = re.compile(r"^\*\*([A-Z][A-Z0-9\s/()\-&,'.]+)\*\*\s*$")
_RE_LIST_ITEM = re.compile(r"^(\s*)[-+](\s+)")
_RE_PARA_NUM = re.compile(r"^[\s*]*(\d+)-(\d+)(?:[.\s]|$)")
_RE_FIGURE = re.compile(r"\bFigure\s+(\d+-\d+)\.?\s*(.*)", re.IGNORECASE)
_RE_TABLE = re.compile(r"\bTable\s+(\d+-\d+)\.?\s*(.*)", re.IGNORECASE)
_RE_CHAPTER_HEADING = re.compile(
    r"^#{1,2}\s+(Chapter\s+\d+[:\s–—\-].+)$", re.IGNORECASE
)
_RE_APPENDIX_HEADING = re.compile(
    r"^#{1,2}\s+(Appendix\s+[A-Z][:\s–—\-].+)$", re.IGNORECASE
)
_RE_CHAPTER_TEXT = re.compile(
    r"^[\*\s]*Chapter\s+(\d+)[:\s–—\-]\s*(.+)$", re.IGNORECASE
)
_RE_APPENDIX_TEXT = re.compile(
    r"^[\*\s]*Appendix\s+([A-Z])[:\s–—\-]\s*(.+)$", re.IGNORECASE
)
_RE_DOC_TYPE = re.compile(r"^([A-Z]{2,6})\s")
_RE_FILENAME_UNSAFE = re.compile(r"[^\w.\-]")
_RE_EXCESS_BLANKS = re.compile(r"\n{4,}")

_BOUNDARY_WINDOW = 150
_HEADING_WINDOW = 500
_BOUNDARY_THRESHOLD = 0.85


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class DedupAction:
    pub_id: str
    page_n: int
    page_n1: int
    matched_text: str


@dataclass
class PubResult:
    pub_id: str
    pub_number: str
    total_pages: int
    content_pages: int
    empty_pages: int
    dedup_actions: int
    paragraph_warnings: int
    figures_found: int
    tables_found: int
    output_file: str
    output_size_bytes: int
    success: bool
    error: Optional[str] = None


# ── I/O helpers ───────────────────────────────────────────────────────────────

def read_page_safe(path: Path, verbose: bool = False) -> tuple[str, Optional[str]]:
    """Read page markdown with UTF-8 → latin-1 fallback.

    Returns (content, encoding_note) where encoding_note is None on clean UTF-8 read.
    """
    try:
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        note = f"UTF-8 decode failed for {path.name}, used latin-1"
        if verbose:
            print(f"  [encoding] {note}")
        try:
            return path.read_text(encoding="latin-1"), note
        except Exception as exc:
            return "", f"read error for {path.name}: {exc}"
    except Exception as exc:
        return "", f"read error for {path.name}: {exc}"


def sanitize_filename(pub_number: str, title: str) -> str:
    """Build a filesystem-safe filename stem from pub_number and title.

    Example: ('ATP 3-20.15', 'TANK PLATOON') → 'ATP_3-20.15_TANK_PLATOON'
    """
    raw = f"{pub_number}_{title}".replace(" ", "_")
    safe = _RE_FILENAME_UNSAFE.sub("", raw)
    return safe[:200]


# ── Blank page detection ───────────────────────────────────────────────────────

def is_blank(content: str) -> bool:
    """Return True if content is empty, whitespace-only, or just <!-- Empty -->."""
    stripped = content.strip()
    if not stripped:
        return True
    if stripped.lower() == "<!-- empty -->":
        return True
    return False


# ── Page-boundary text dedup ──────────────────────────────────────────────────

def trim_boundary_overlap(
    tail: str,
    head: str,
    threshold: float = _BOUNDARY_THRESHOLD,
) -> tuple[str, str, Optional[str]]:
    """Detect and remove overlapping text between the end of page N and start of page N+1.

    Compares the trailing _BOUNDARY_WINDOW chars of `tail` with the leading
    _BOUNDARY_WINDOW chars of `head` using SequenceMatcher fuzzy matching.
    When overlap ratio meets `threshold`, trims the duplicate from `head`.

    Returns (tail_unchanged, trimmed_head, matched_text_or_None).
    """
    tail_window = tail[-_BOUNDARY_WINDOW:]
    head_window = head[:_BOUNDARY_WINDOW]

    if not tail_window.strip() or not head_window.strip():
        return tail, head, None

    matcher = difflib.SequenceMatcher(None, tail_window, head_window, autojunk=False)
    if matcher.ratio() < threshold:
        return tail, head, None

    match = matcher.find_longest_match(0, len(tail_window), 0, len(head_window))
    if match.size < 10:
        return tail, head, None

    matched_text = head_window[match.b : match.b + match.size]
    trim_to = match.b + match.size
    trimmed_head = head[trim_to:].lstrip()
    return tail, trimmed_head, matched_text


# ── Structural normalization ───────────────────────────────────────────────────

def normalize_list_markers(text: str) -> str:
    """Replace - and + unordered list markers with * for consistency."""
    return _RE_LIST_ITEM.sub(r"\1*\2", text)


def normalize_bold_headings(text: str) -> str:
    """Convert lone all-caps bold lines preceded by a blank line to ### headings.

    Targets the pattern where vision LLM renders section titles as **BOLD ALL-CAPS**
    instead of markdown headings.
    """
    lines = text.split("\n")
    result: list[str] = []
    for i, line in enumerate(lines):
        m = _RE_BOLD_HEADING.match(line)
        if m:
            preceded_by_blank = i == 0 or lines[i - 1].strip() == ""
            if preceded_by_blank:
                result.append(f"### {m.group(1).strip()}")
                continue
        result.append(line)
    return "\n".join(result)


def strip_trailing_whitespace(text: str) -> str:
    """Strip trailing whitespace from every line."""
    return "\n".join(line.rstrip() for line in text.split("\n"))


def collapse_blank_lines(text: str) -> str:
    """Collapse runs of 3+ consecutive blank lines to exactly 2."""
    return _RE_EXCESS_BLANKS.sub("\n\n\n", text)


def normalize(text: str) -> str:
    """Apply all structural normalizations to a page's content."""
    text = normalize_list_markers(text)
    text = normalize_bold_headings(text)
    text = strip_trailing_whitespace(text)
    text = collapse_blank_lines(text)
    return text


# ── Duplicate heading removal ──────────────────────────────────────────────────

def remove_duplicate_headings(assembled: str) -> tuple[str, list[str]]:
    """Remove repeated running headers introduced by per-page LLM rendering.

    Maintains a rolling 500-character window of previously-seen content.
    Removes a heading whose text (case-insensitive, level-stripped) appeared in
    that window at the same heading level.  Level mismatches are logged but kept.

    Returns (cleaned_text, log_messages).
    """
    lines = assembled.split("\n")
    result: list[str] = []
    logs: list[str] = []
    recent: list[str] = []  # lines already added, used for window

    def window() -> str:
        return "\n".join(recent)[-_HEADING_WINDOW:]

    for line in lines:
        m = _RE_HEADING.match(line)
        if m:
            level = len(m.group(1))
            text_key = m.group(2).strip().lower()

            prev_level: Optional[int] = None
            for wline in window().split("\n"):
                wm = _RE_HEADING.match(wline)
                if wm and wm.group(2).strip().lower() == text_key:
                    prev_level = len(wm.group(1))
                    break

            if prev_level is not None:
                if prev_level == level:
                    logs.append(f"removed duplicate heading (level {level}): {line!r}")
                    continue
                else:
                    logs.append(
                        f"heading level mismatch (kept): {line!r} "
                        f"vs prior level {prev_level}"
                    )
        result.append(line)
        recent.append(line)

    return "\n".join(result), logs


# ── Frontmatter / metadata ────────────────────────────────────────────────────

def extract_doc_type(pub_number: Optional[str]) -> str:
    """Extract doc type prefix from pub_number.

    Examples: 'ATP 3-20.15' → 'ATP', 'FM 3-0' → 'FM', 'AR 600-20' → 'AR'
    """
    if not pub_number:
        return "UNKNOWN"
    m = _RE_DOC_TYPE.match(pub_number.strip())
    if m:
        return m.group(1)
    parts = pub_number.split()
    return parts[0].upper() if parts else "UNKNOWN"


def _normalize_section_title(label: str, num: str, name: str) -> str:
    """Build canonical 'Label N: Name' from extracted parts."""
    clean_name = name.rstrip(":.").strip()
    return f"{label} {num}: {clean_name}"


def extract_chapters_appendices(content: str) -> tuple[list[str], list[str]]:
    """Extract chapter and appendix titles from combined content.

    Primary pass: looks for markdown headings matching Chapter/Appendix patterns.
    Fallback pass: scans body text for 'Chapter N—Title' style lines (e.g. intro lists).
    Deduplicates on normalized title text.
    """
    chapters: list[str] = []
    appendices: list[str] = []
    seen_ch: set[str] = set()
    seen_ap: set[str] = set()

    def add_chapter(title: str) -> None:
        if title not in seen_ch:
            chapters.append(title)
            seen_ch.add(title)

    def add_appendix(title: str) -> None:
        if title not in seen_ap:
            appendices.append(title)
            seen_ap.add(title)

    for line in content.split("\n"):
        stripped = line.strip()

        # Primary: heading-based detection
        m = _RE_CHAPTER_HEADING.match(stripped)
        if m:
            raw = m.group(1).strip()
            # Normalize separator to ": "
            norm = re.sub(r"\s*[:–—\-]\s*", ": ", raw, count=1)
            add_chapter(norm)
            continue

        m = _RE_APPENDIX_HEADING.match(stripped)
        if m:
            raw = m.group(1).strip()
            norm = re.sub(r"\s*[:–—\-]\s*", ": ", raw, count=1)
            add_appendix(norm)
            continue

        # Fallback: body-text patterns (intro summary lists)
        m = _RE_CHAPTER_TEXT.match(stripped)
        if m:
            title = _normalize_section_title("Chapter", m.group(1), m.group(2))
            add_chapter(title)
            continue

        m = _RE_APPENDIX_TEXT.match(stripped)
        if m:
            title = _normalize_section_title("Appendix", m.group(1), m.group(2))
            add_appendix(title)

    return chapters, appendices


def extract_paragraph_range(content: str) -> str:
    """Return 'first_para through last_para' string, or empty string if none found."""
    found: list[tuple[int, int]] = []
    for line in content.split("\n"):
        m = _RE_PARA_NUM.match(line)
        if m:
            found.append((int(m.group(1)), int(m.group(2))))
    if not found:
        return ""
    first = found[0]
    last = found[-1]
    return f"{first[0]}-{first[1]} through {last[0]}-{last[1]}"


def build_frontmatter(pub: dict, computed: dict) -> str:
    """Build YAML frontmatter block with controlled field order.

    Writes fields in a deterministic order regardless of yaml library sorting.
    """
    data: dict = {
        "pub_id": str(pub.get("pub_id") or ""),
        "pub_number": pub.get("pub_number") or "",
        "title": pub.get("title") or "",
        "category": pub.get("category") or "",
        "doc_type": computed["doc_type"],
        "total_pages": pub.get("total_pages") or 0,
        "content_pages": computed["content_pages"],
        "combined_at": computed["combined_at"],
        "chapters": computed.get("chapters") or [],
        "appendices": computed.get("appendices") or [],
    }
    paragraph_range = computed.get("paragraph_range", "")
    if paragraph_range:
        data["paragraph_range"] = paragraph_range

    yaml_body = yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{yaml_body}---\n"


# ── Figure / table registry ───────────────────────────────────────────────────

def build_registry(
    lines_with_page: list[tuple[str, int]],
    pub_number: str,
) -> dict:
    """Scan (line, page_number) pairs and return a figure/table registry dict."""
    figures: list[dict] = []
    tables: list[dict] = []

    for idx, (line, page_num) in enumerate(lines_with_page):
        def _next_caption() -> str:
            if idx + 1 < len(lines_with_page):
                nxt = lines_with_page[idx + 1][0].strip()
                if nxt and not _RE_FIGURE.search(nxt) and not _RE_TABLE.search(nxt):
                    return nxt
            return ""

        mf = _RE_FIGURE.search(line)
        if mf:
            caption = mf.group(2).strip() or _next_caption()
            figures.append({"number": mf.group(1), "caption": caption, "page": page_num})

        mt = _RE_TABLE.search(line)
        if mt:
            caption = mt.group(2).strip() or _next_caption()
            tables.append({"number": mt.group(1), "caption": caption, "page": page_num})

    return {"pub_number": pub_number, "figures": figures, "tables": tables}


# ── Paragraph validation ──────────────────────────────────────────────────────

def validate_paragraphs(content: str, pub_number: str) -> list[str]:
    """Validate paragraph number sequences per chapter; return warning strings.

    Extracts top-level paragraph numbers (e.g. 1-1, 2-3), groups by chapter,
    and checks for duplicates and gaps.  Does NOT auto-fix anything.
    """
    warnings: list[str] = []
    chapters: dict[int, list[int]] = {}

    for line in content.split("\n"):
        m = _RE_PARA_NUM.match(line)
        if m:
            ch, para = int(m.group(1)), int(m.group(2))
            chapters.setdefault(ch, []).append(para)

    for ch_num in sorted(chapters):
        nums = chapters[ch_num]
        seen: set[int] = set()
        for n in nums:
            if n in seen:
                warnings.append(
                    f"WARNING: {pub_number} - Duplicate paragraph number {ch_num}-{n} found"
                )
            seen.add(n)

        unique = sorted(seen)
        if len(unique) < 2:
            continue

        for i in range(len(unique) - 1):
            expected = unique[i] + 1
            actual = unique[i + 1]
            if actual != expected:
                missing = list(range(expected, min(actual, expected + 5)))
                missing_str = ", ".join(f"{ch_num}-{m}" for m in missing)
                warnings.append(
                    f"WARNING: {pub_number} - Expected {ch_num}-{expected} but found "
                    f"{ch_num}-{actual} (possible missing: {missing_str})"
                )

    return warnings


# ── Per-publication orchestration ─────────────────────────────────────────────

def process_pub(
    pub_id: str,
    db_path: str,
    base_dir: str,
    output_dir: str,
    report_dir: str,
    verbose: bool,
) -> PubResult:
    """Process a single publication end-to-end.

    Opens its own DB connection (safe for use in multiprocessing workers).
    Steps:
      1. Load pub metadata and page list from DB
      2. Read, filter, and normalize per-page markdown
      3. Fuzzy-dedup page-boundary overlaps
      4. Assemble with simplified page markers
      5. Remove duplicate running headings
      6. Build enhanced YAML frontmatter
      7. Write combined .md file
      8. Write per-pub figure/table registry JSON
      9. Write validation/log report
      10. Mark combined in DB
    """
    out_dir = Path(output_dir)
    rep_dir = Path(report_dir)
    base = Path(base_dir)

    def _fail(msg: str) -> PubResult:
        return PubResult(
            pub_id=pub_id, pub_number="", total_pages=0, content_pages=0,
            empty_pages=0, dedup_actions=0, paragraph_warnings=0,
            figures_found=0, tables_found=0, output_file="",
            output_size_bytes=0, success=False, error=msg,
        )

    # ── Load from DB ──────────────────────────────────────────────────────────
    try:
        conn = dbmodule.get_db(Path(db_path))
        row = conn.execute(
            "SELECT * FROM publications WHERE pub_id = ?", (pub_id,)
        ).fetchone()
        if not row:
            conn.close()
            return _fail("pub not found in DB")
        pub = dict(row)
        pages = dbmodule.get_pages_for_pub(conn, pub_id)
        conn.close()
    except Exception as exc:
        return _fail(f"DB read error: {exc}")

    pub_number: str = pub.get("pub_number") or pub_id
    title: str = pub.get("title") or ""
    total_pages: int = pub.get("total_pages") or len(pages)

    # ── Phase 1: Read and normalize pages ────────────────────────────────────
    content_pages: list[tuple[int, str]] = []  # (page_number, normalized_content)
    empty_pages = 0
    encoding_notes: list[str] = []
    read_errors: list[str] = []

    for page in pages:
        page_num: int = page["page_number"]
        md_path_rel: Optional[str] = page.get("markdown_path")

        if page.get("is_blank") or not md_path_rel:
            empty_pages += 1
            continue

        md_path = base / md_path_rel
        if not md_path.exists():
            read_errors.append(f"missing file page {page_num}: {md_path_rel}")
            empty_pages += 1
            if verbose:
                print(f"  [{pub_number}] missing page {page_num}: {md_path_rel}")
            continue

        raw, enc_note = read_page_safe(md_path, verbose)
        if enc_note:
            if "error" in enc_note:
                read_errors.append(enc_note)
                empty_pages += 1
                continue
            encoding_notes.append(enc_note)

        if is_blank(raw):
            empty_pages += 1
            continue

        content_pages.append((page_num, normalize(raw.strip())))

    # ── Phase 2: Page-boundary dedup ─────────────────────────────────────────
    dedup_actions: list[DedupAction] = []

    for i in range(len(content_pages) - 1):
        pn, c_n = content_pages[i]
        pn1, c_n1 = content_pages[i + 1]
        _, trimmed_n1, matched = trim_boundary_overlap(c_n, c_n1)
        if matched:
            dedup_actions.append(DedupAction(pub_id, pn, pn1, matched))
            content_pages[i + 1] = (pn1, trimmed_n1)

    # ── Phase 3: Assemble with page markers ──────────────────────────────────
    page_parts: list[str] = []
    lines_with_page: list[tuple[str, int]] = []

    for page_num, content in content_pages:
        page_parts.append(f"<!-- p.{page_num} -->\n\n{content}")
        for line in content.split("\n"):
            lines_with_page.append((line, page_num))

    assembled = "\n\n".join(page_parts)

    # ── Phase 4: Duplicate heading removal ───────────────────────────────────
    assembled, heading_logs = remove_duplicate_headings(assembled)

    # ── Phase 5: Build frontmatter ────────────────────────────────────────────
    doc_type = extract_doc_type(pub_number)
    chapters, appendices = extract_chapters_appendices(assembled)
    paragraph_range = extract_paragraph_range(assembled)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    computed = {
        "doc_type": doc_type,
        "content_pages": len(content_pages),
        "combined_at": now,
        "chapters": chapters,
        "appendices": appendices,
        "paragraph_range": paragraph_range,
    }
    frontmatter = build_frontmatter(pub, computed)

    # ── Phase 6: Write output .md ─────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pub.get("pdf_stem") or sanitize_filename(pub_number, title)
    filename = stem + ".md"
    out_path = out_dir / filename

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter)
        fh.write("\n")
        fh.write(assembled)
        if not assembled.endswith("\n"):
            fh.write("\n")

    output_size = out_path.stat().st_size

    # ── Phase 7: Figure / table registry ─────────────────────────────────────
    rep_dir.mkdir(parents=True, exist_ok=True)
    registry = build_registry(lines_with_page, pub_number)
    safe_num = stem
    registry_path = rep_dir / f"{safe_num}_figures_tables.json"
    registry_path.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── Phase 8: Validation / log report ─────────────────────────────────────
    para_warnings = validate_paragraphs(assembled, pub_number)
    log_lines: list[str] = []
    log_lines.extend(para_warnings)
    log_lines.extend(f"HEADING: {pub_number} - {msg}" for msg in heading_logs)
    log_lines.extend(f"ENCODING: {pub_number} - {note}" for note in encoding_notes)
    log_lines.extend(f"READ_ERROR: {pub_number} - {err}" for err in read_errors)
    for da in dedup_actions:
        log_lines.append(
            f"DEDUP: {pub_number} - pages {da.page_n}/{da.page_n1}: "
            f"trimmed {len(da.matched_text)} chars: {da.matched_text[:80]!r}"
        )
    val_path = rep_dir / f"{safe_num}_validation.txt"
    val_path.write_text(
        ("\n".join(log_lines) + "\n") if log_lines else "", encoding="utf-8"
    )

    # ── Phase 9: Update DB status ─────────────────────────────────────────────
    try:
        conn2 = dbmodule.get_db(Path(db_path))
        dbmodule.set_combined(conn2, pub_id)
        conn2.close()
    except Exception:
        pass  # non-fatal; output file was already written

    if verbose:
        print(
            f"  [{pub_number}] content={len(content_pages)} empty={empty_pages} "
            f"dedup={len(dedup_actions)} para_warn={len(para_warnings)} "
            f"figs={len(registry['figures'])} tables={len(registry['tables'])}"
        )

    return PubResult(
        pub_id=pub_id,
        pub_number=pub_number,
        total_pages=total_pages,
        content_pages=len(content_pages),
        empty_pages=empty_pages,
        dedup_actions=len(dedup_actions),
        paragraph_warnings=len(para_warnings),
        figures_found=len(registry["figures"]),
        tables_found=len(registry["tables"]),
        output_file=filename,
        output_size_bytes=output_size,
        success=True,
    )


# ── CLI + parallel orchestration ─────────────────────────────────────────────

def main() -> None:
    """Parse CLI args, enumerate publications, dispatch to workers, write report."""
    parser = argparse.ArgumentParser(
        description="Enhanced combiner for Army publications RAG pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pub-id", help="Process a single publication by ID")
    group.add_argument(
        "--all", action="store_true",
        help="Process all publications with extracted markdown pages",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("combined_markdown"),
        help="Output directory for combined .md files",
    )
    parser.add_argument(
        "--report-dir", type=Path, default=Path("reports"),
        help="Directory for combine_report.json, registries, and validation files",
    )
    parser.add_argument(
        "--workers", type=int, default=os.cpu_count() or 4,
        help="Number of parallel worker processes",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-page detail during processing",
    )
    parser.add_argument(
        "--db", type=Path, default=dbmodule.DEFAULT_DB,
        help="Path to pipeline.db",
    )
    args = parser.parse_args()

    conn = dbmodule.get_db(args.db)
    if args.pub_id:
        rows = conn.execute(
            "SELECT pub_id FROM publications WHERE pub_id = ?", (args.pub_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT p.pub_id
               FROM publications p
               JOIN pages pg ON p.pub_id = pg.pub_id
               WHERE pg.markdown_path IS NOT NULL"""
        ).fetchall()
    conn.close()

    pub_ids = [r[0] for r in rows]
    total = len(pub_ids)
    if total == 0:
        print("No publications found to process.", file=sys.stderr)
        sys.exit(1)

    workers = min(args.workers, total)
    print(f"Processing {total} publication(s) with {workers} worker(s)...")

    db_path = str(args.db)
    base_dir = str(_HERE)
    output_dir = str(args.output_dir)
    report_dir = str(args.report_dir)

    results: list[PubResult] = []
    failed_pubs: list[str] = []
    counter = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                process_pub,
                pub_id, db_path, base_dir, output_dir, report_dir, args.verbose,
            ): pub_id
            for pub_id in pub_ids
        }
        for future in as_completed(futures):
            pub_id = futures[future]
            counter += 1
            try:
                result: PubResult = future.result()
            except Exception as exc:
                print(f"[{counter}/{total}] {pub_id} — EXCEPTION: {exc}")
                failed_pubs.append(pub_id)
                results.append(PubResult(
                    pub_id=pub_id, pub_number="", total_pages=0, content_pages=0,
                    empty_pages=0, dedup_actions=0, paragraph_warnings=0,
                    figures_found=0, tables_found=0, output_file="",
                    output_size_bytes=0, success=False, error=str(exc),
                ))
                continue

            results.append(result)
            if not result.success:
                failed_pubs.append(pub_id)
            status = "OK" if result.success else f"FAIL ({result.error})"
            print(
                f"[{counter}/{total}] {result.pub_number or pub_id} "
                f"({result.content_pages} pages) — {status}"
            )

    # ── Write combine report ──────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    good = [r for r in results if r.success]
    report = {
        "processed_at": now,
        "total_publications": total,
        "successful": len(good),
        "failed": len(failed_pubs),
        "failed_pubs": failed_pubs,
        "total_content_pages": sum(r.content_pages for r in good),
        "total_dedup_actions": sum(r.dedup_actions for r in good),
        "total_paragraph_warnings": sum(r.paragraph_warnings for r in good),
        "publications": [
            {
                "pub_id": r.pub_id,
                "pub_number": r.pub_number,
                "total_pages": r.total_pages,
                "content_pages": r.content_pages,
                "empty_pages": r.empty_pages,
                "dedup_actions": r.dedup_actions,
                "paragraph_warnings": r.paragraph_warnings,
                "figures_found": r.figures_found,
                "tables_found": r.tables_found,
                "output_file": r.output_file,
                "output_size_bytes": r.output_size_bytes,
            }
            for r in results
        ],
    }

    rep_dir = Path(report_dir)
    rep_dir.mkdir(parents=True, exist_ok=True)
    report_path = rep_dir / "combine_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(
        f"\nDone. Report: {report_path}\n"
        f"  Success: {len(good)}/{total}"
        f"  |  Dedup actions: {report['total_dedup_actions']}"
        f"  |  Para warnings: {report['total_paragraph_warnings']}"
    )


if __name__ == "__main__":
    main()
