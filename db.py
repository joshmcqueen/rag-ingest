#!/usr/bin/env python3
"""SQLite state layer for the RAG ingest pipeline."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
DEFAULT_DB = _HERE / "pipeline.db"

_SCHEMA = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS publications (
    pub_id          TEXT PRIMARY KEY,
    pub_number      TEXT,
    title           TEXT,
    category        TEXT,
    status          TEXT,
    pub_date        TEXT,
    pdf_url         TEXT,
    pdf_stem        TEXT,
    local_path      TEXT,
    total_pages     INTEGER,
    pipeline_status TEXT NOT NULL DEFAULT 'scanned',
    downloaded_at   TEXT,
    ingested_at     TEXT,
    extracted_at    TEXT,
    combined_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pub_stem ON publications(pdf_stem);

CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pub_id        TEXT NOT NULL REFERENCES publications(pub_id),
    page_number   INTEGER NOT NULL,
    total_pages   INTEGER NOT NULL,
    is_blank      INTEGER NOT NULL DEFAULT 0,
    image_path    TEXT,
    markdown_path TEXT,
    extracted_at  TEXT,
    UNIQUE(pub_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_pages_pub ON pages(pub_id, page_number);
"""


def get_db(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stem_from_url(pdf_url: Optional[str]) -> Optional[str]:
    if not pdf_url:
        return None
    return Path(pdf_url.split("?")[0].split("/")[-1]).stem


def upsert_publication(conn: sqlite3.Connection, entry: dict) -> None:
    """Insert or update a publication from a manifest or download_log entry."""
    pdf_stem = _stem_from_url(entry.get("pdf_url"))
    local_path = entry.get("local_path")
    downloaded_at = entry.get("downloaded_at") or entry.get("timestamp")

    conn.execute("""
        INSERT OR IGNORE INTO publications
            (pub_id, pub_number, title, category, status, pub_date, pdf_url, pdf_stem)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        entry.get("pub_id"),
        entry.get("pub_number"),
        entry.get("title"),
        entry.get("category"),
        entry.get("status"),
        entry.get("date") or entry.get("pub_date"),
        entry.get("pdf_url"),
        pdf_stem,
    ))
    conn.execute("""
        UPDATE publications SET
            pub_number    = COALESCE(?, pub_number),
            title         = COALESCE(?, title),
            pdf_url       = COALESCE(?, pdf_url),
            pdf_stem      = COALESCE(?, pdf_stem),
            local_path    = COALESCE(?, local_path),
            downloaded_at = COALESCE(?, downloaded_at),
            pipeline_status = CASE
                WHEN ? IS NOT NULL AND pipeline_status = 'scanned' THEN 'downloaded'
                ELSE pipeline_status
            END
        WHERE pub_id = ?
    """, (
        entry.get("pub_number"),
        entry.get("title"),
        entry.get("pdf_url"),
        pdf_stem,
        local_path,
        downloaded_at,
        local_path,
        entry.get("pub_id"),
    ))


def seed_from_manifest(conn: sqlite3.Connection, manifest_path: Path) -> int:
    """Populate publications table from an existing manifest.jsonl."""
    count = 0
    with open(manifest_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not entry.get("pub_id"):
                continue
            upsert_publication(conn, entry)
            count += 1
            if count % 500 == 0:
                conn.commit()
    conn.commit()
    return count


def seed_from_download_log(conn: sqlite3.Connection, log_path: Path) -> int:
    """Update publications with local_path/downloaded_at from an existing download_log.jsonl."""
    _GOOD = {"downloaded", "skipped"}
    count = 0
    with open(log_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("result") not in _GOOD:
                continue
            if not entry.get("pub_id"):
                continue
            upsert_publication(conn, entry)
            count += 1
            if count % 500 == 0:
                conn.commit()
    conn.commit()
    return count


def get_pub_by_stem(conn: sqlite3.Connection, stem: str) -> Optional[dict]:
    """Look up a publication by its PDF filename stem."""
    row = conn.execute("SELECT * FROM publications WHERE pdf_stem = ?", (stem,)).fetchone()
    return dict(row) if row else None


def insert_page(
    conn: sqlite3.Connection,
    pub_id: str,
    page_number: int,
    total_pages: int,
    is_blank: bool,
    image_path: Optional[str],
) -> None:
    """Record a rendered (or skipped-blank) page. image_path is None for blank pages."""
    conn.execute("""
        INSERT OR IGNORE INTO pages (pub_id, page_number, total_pages, is_blank, image_path)
        VALUES (?, ?, ?, ?, ?)
    """, (pub_id, page_number, total_pages, int(is_blank), image_path))
    conn.commit()


def set_ingested(conn: sqlite3.Connection, pub_id: str, total_pages: int) -> None:
    conn.execute("""
        UPDATE publications
        SET pipeline_status = 'images_rendered', total_pages = ?, ingested_at = ?
        WHERE pub_id = ?
    """, (total_pages, _now(), pub_id))
    conn.commit()


def _parse_image_stem(image_path: str) -> tuple[Optional[str], Optional[int]]:
    """Return (pdf_stem, page_number) from a filename like 'ARN123-FM_1_page_0007.png'."""
    stem = Path(image_path).stem
    if "_page_" not in stem:
        return None, None
    pdf_stem, page_str = stem.rsplit("_page_", 1)
    try:
        return pdf_stem, int(page_str)
    except ValueError:
        return None, None


def get_page_context(conn: sqlite3.Connection, image_path: str) -> Optional[dict]:
    """Return {pub_id, title, pub_number, page_number, total_pages} for an image path."""
    pdf_stem, page_number = _parse_image_stem(image_path)
    if not pdf_stem or page_number is None:
        return None
    row = conn.execute("""
        SELECT pub.pub_id, pub.title, pub.pub_number, pg.page_number, pg.total_pages
        FROM publications pub
        JOIN pages pg ON pub.pub_id = pg.pub_id
        WHERE pub.pdf_stem = ? AND pg.page_number = ?
    """, (pdf_stem, page_number)).fetchone()
    return dict(row) if row else None


def set_page_extracted(conn: sqlite3.Connection, image_path: str, markdown_path: str) -> None:
    pdf_stem, page_number = _parse_image_stem(image_path)
    if not pdf_stem or page_number is None:
        return
    pub = conn.execute("SELECT pub_id FROM publications WHERE pdf_stem = ?", (pdf_stem,)).fetchone()
    if not pub:
        return
    conn.execute("""
        UPDATE pages SET markdown_path = ?, extracted_at = ?
        WHERE pub_id = ? AND page_number = ?
    """, (markdown_path, _now(), pub["pub_id"], page_number))
    conn.commit()


def set_extracted(conn: sqlite3.Connection, pub_id: str) -> None:
    conn.execute("""
        UPDATE publications SET pipeline_status = 'markdown_extracted', extracted_at = ?
        WHERE pub_id = ?
    """, (_now(), pub_id))
    conn.commit()


def get_pages_for_pub(conn: sqlite3.Connection, pub_id: str) -> list[dict]:
    rows = conn.execute("""
        SELECT * FROM pages WHERE pub_id = ? ORDER BY page_number
    """, (pub_id,)).fetchall()
    return [dict(r) for r in rows]


def set_combined(conn: sqlite3.Connection, pub_id: str) -> None:
    conn.execute("""
        UPDATE publications SET pipeline_status = 'combined', combined_at = ?
        WHERE pub_id = ?
    """, (_now(), pub_id))
    conn.commit()
