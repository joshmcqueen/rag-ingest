"""
Army Publications Scraper — two-step workflow:

  Step 1: Build a manifest of all publications and their PDF URLs
    python scraper.py build

  Step 2: Download only the PDFs listed in the manifest
    python scraper.py download

  Step 3: Print manifest statistics
    python scraper.py stats

  Step 4: Seed pipeline.db from existing JSONL files (one-time migration)
    python scraper.py sync-db

Each command accepts:
  --category training_doctrine/FM   # Scope to one category
  --status ACTIVE                   # Filter: ACTIVE, INACTIVE, RESCINDED
  --limit 10                        # Cap per category (useful for testing)
  --delay 2.0                       # Seconds between requests (default: 1.5)
  --output ./downloads              # Output directory (default: downloads)
  --manifest manifest.jsonl         # Manifest filename (default: manifest.jsonl)
"""

import argparse
import json
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
import os
from pathlib import Path
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    import db as _db
except ImportError:
    _db = None

BASE_URL = "https://armypubs.army.mil"
CATEGORY_BASE = f"{BASE_URL}/ProductMaps/PubForm"

CATEGORIES = {
    # Administrative
    "administrative/Web_Series":    "Web_Series.aspx",
    "administrative/ALARACT":       "ALARACT.aspx",
    "administrative/ArmyDir":       "ArmyDir.aspx",
    "administrative/AR":            "AR.aspx",
    "administrative/AGO_active":    "AGO.aspx",
    "administrative/AGO_inactive":  "AGO_Inactive.aspx",
    "administrative/DAMEMO":        "DAMEMO.aspx",
    "administrative/HQDA_Policy":   "HQDAPolicyNotice.aspx",
    "administrative/PAM":           "PAM.aspx",
    "administrative/POG":           "PogProponent.aspx",
    "administrative/PPM":           "PPM.aspx",

    # Technical & Equipment
    "technical_equipment/EM":           "EM.aspx",
    "technical_equipment/FT":           "FT.aspx",
    "technical_equipment/LO":           "LO.aspx",
    "technical_equipment/MWO":          "MWO.aspx",
    "technical_equipment/SB":           "SB.aspx",
    "technical_equipment/SC":           "SC.aspx",
    "technical_equipment/TB":           "TB.aspx",
    "technical_equipment/TM_1_8":       "TM_1_8.aspx",
    "technical_equipment/TM_9":         "TM_9.aspx",
    "technical_equipment/TM_10":        "TM_10.aspx",
    "technical_equipment/TM_11_4":      "TM_11_4.aspx",
    "technical_equipment/TM_11_5":      "TM_11_5.aspx",
    "technical_equipment/TM_11_6_7":    "TM_11_6_7.aspx",
    "technical_equipment/TM_14_750":    "TM_14_750.aspx",

    # Training and Doctrine
    "training_doctrine/ADP":   "ADP.aspx",
    "training_doctrine/ADRP":  "ADRP.aspx",
    "training_doctrine/ATP":   "ATP.aspx",
    "training_doctrine/ATTP":  "ATTP.aspx",
    "training_doctrine/CTA":   "CTA.aspx",
    "training_doctrine/FM":    "FM.aspx",
    "training_doctrine/GTA":   "GTA.aspx",
    "training_doctrine/JTA":   "JTA.aspx",
    "training_doctrine/PB":    "PB.aspx",
    "training_doctrine/STP":   "STP.aspx",
    "training_doctrine/TC":    "TC.aspx",

    # Engineering
    "engineering/TM": "TM_Admin.aspx",
    "engineering/TB": "TB_Admin.aspx",

    # Medical
    "medical/TM": "TM_Cal.aspx",
    "medical/TB": "TB_Cal.aspx",
    "medical/SB": "SB_Cal.aspx",
    "medical/SC": "SC_Cal.aspx",

    # Miscellaneous
    "miscellaneous/MCM": "MISC.aspx",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return session


def fetch_with_retry(session: requests.Session, url: str, delay: float, max_retries: int = 3) -> Optional[requests.Response]:
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503):
                wait = delay * (2 ** attempt) + 5
                print(f"  Rate limited ({resp.status_code}), waiting {wait:.0f}s...")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code} for {url}")
                return None
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                print(f"  Request failed: {e}")
                return None
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def fetch_category_page(session: requests.Session, aspx_slug: str, delay: float) -> List[dict]:
    url = f"{CATEGORY_BASE}/{aspx_slug}"
    resp = fetch_with_retry(session, url, delay)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", id="MainContent_GridView1")
    if not table:
        print(f"  No table found at {url}")
        return []

    publications = []
    for row in table.find_all("tr")[1:]:  # skip header
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[0].find("a")
        if not link:
            continue
        href = link.get("href", "")
        pub_id = href.split("PUB_ID=")[-1].strip() if "PUB_ID=" in href else None
        publications.append({
            "pub_id": pub_id,
            "pub_number": cells[0].get_text(strip=True),
            "status": cells[1].get_text(strip=True),
            "date": cells[2].get_text(strip=True),
            "title": cells[3].get_text(strip=True),
            "proponent": cells[4].get_text(strip=True) if len(cells) > 4 else "",
        })

    return publications


def fetch_pdf_urls(session: requests.Session, pub_id: str, delay: float) -> List[str]:
    url = f"{CATEGORY_BASE}/Details.aspx?PUB_ID={pub_id}"
    time.sleep(delay)
    resp = fetch_with_retry(session, url, delay)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")

    # Scope to the detail table only — the site nav contains unrelated PDF links
    container = soup.find(id="MainContent_tblContainer1")
    search_root = container if container else soup

    pdf_urls = []
    for a in search_root.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        if href.startswith("http"):
            pdf_urls.append(href)
        elif href.startswith("../../"):
            pdf_urls.append(f"{BASE_URL}/{href[6:]}")
        elif href.startswith("/"):
            pdf_urls.append(f"{BASE_URL}{href}")
        else:
            pdf_urls.append(urllib.parse.urljoin(f"{CATEGORY_BASE}/", href))

    return pdf_urls


# ---------------------------------------------------------------------------
# Step 1: build
# ---------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / args.manifest

    categories = _resolve_categories(args.category)
    session = make_session()

    total_found = 0
    total_with_pdf = 0
    total_no_pdf = 0

    # Load already-processed pub_ids so we can resume an interrupted build
    seen_ids: set = set()
    if manifest_path.exists():
        with open(manifest_path) as f:
            for line in f:
                try:
                    seen_ids.add(json.loads(line)["pub_id"])
                except (json.JSONDecodeError, KeyError):
                    pass
        print(f"Resuming — {len(seen_ids)} publications already in manifest.\n")

    for category_path, aspx_slug in categories.items():
        print(f"[{category_path}] Fetching listing...")
        pubs = fetch_category_page(session, aspx_slug, args.delay)

        if args.status:
            pubs = [p for p in pubs if p["status"].upper() == args.status.upper()]

        limit = args.limit or len(pubs)
        pubs = pubs[:limit]

        # Filter to only pubs not yet in the manifest
        new_pubs = [p for p in pubs if p.get("pub_id") and p["pub_id"] not in seen_ids]
        if not new_pubs:
            print(f"  {len(pubs)} publications — already complete, skipping.")
            continue
        if len(new_pubs) < len(pubs):
            print(f"  {len(pubs)} publications — resuming ({len(new_pubs)} remaining)")
        else:
            print(f"  {len(pubs)} publications")

        for pub in tqdm(new_pubs, desc=category_path.split("/")[-1], unit="pub"):
            pub_id = pub["pub_id"]

            pdf_urls = fetch_pdf_urls(session, pub_id, args.delay)
            pdf_url = pdf_urls[0] if pdf_urls else None

            entry = {
                "pub_id": pub_id,
                "pub_number": pub["pub_number"],
                "category": category_path,
                "status": pub["status"],
                "date": pub["date"],
                "title": pub["title"],
                "proponent": pub["proponent"],
                "pdf_url": pdf_url,
                "scanned_at": datetime.utcnow().isoformat(),
            }

            with open(manifest_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

            seen_ids.add(pub_id)
            total_found += 1
            if pdf_url:
                total_with_pdf += 1
            else:
                total_no_pdf += 1

    print(f"\nManifest written to: {manifest_path}")
    if total_found:
        print(f"  Added this run  : {total_found}  (with PDF: {total_with_pdf}, no PDF: {total_no_pdf})")
    else:
        print("  Nothing new added this run — manifest is already up to date.")

    print()
    _print_manifest_stats(manifest_path, Path(args.output))


# ---------------------------------------------------------------------------
# Manifest statistics
# ---------------------------------------------------------------------------

def _print_manifest_stats(manifest_path: Path, output_dir: Path) -> None:
    entries = []
    with open(manifest_path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if not entries:
        print("Manifest is empty.")
        return

    total = len(entries)
    with_pdf = [e for e in entries if e.get("pdf_url")]
    no_pdf = total - len(with_pdf)

    # Check which PDFs are already on disk
    on_disk = 0
    for e in with_pdf:
        filename = e["pdf_url"].split("/")[-1]
        local_path = output_dir / e["category"] / filename
        if local_path.exists() and local_path.stat().st_size > 0:
            on_disk += 1

    still_needed = len(with_pdf) - on_disk

    # Status breakdown
    status_counts: Counter = Counter(e.get("status", "Unknown") for e in entries)

    # Category group breakdown (top-level before the /)
    group_counts: Counter = Counter(e["category"].split("/")[0] for e in entries)

    # Unique proponents
    proponents = {e.get("proponent", "").strip() for e in entries if e.get("proponent", "").strip()}

    # Publication date range
    pub_dates = []
    for e in entries:
        raw = e.get("date", "")
        try:
            pub_dates.append(datetime.strptime(raw, "%m/%d/%Y"))
        except (ValueError, TypeError):
            pass

    def _pct(n: int, d: int) -> str:
        return f"{n / d * 100:.1f}%" if d else "—"

    print("=== Manifest Summary ===\n")
    print(f"  Total publications  : {total:,}")
    print(f"  With PDF URL        : {len(with_pdf):,}  ({_pct(len(with_pdf), total)})")
    print(f"  No PDF URL          : {no_pdf:,}  ({_pct(no_pdf, total)})")
    if with_pdf:
        print(f"  Already on disk     : {on_disk:,}  ({_pct(on_disk, len(with_pdf))} of those with PDF)")
        print(f"  Still to download   : {still_needed:,}  ({_pct(still_needed, len(with_pdf))} of those with PDF)")

    print()
    print("  By status:")
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"    {status:<20} : {count:>6,}  ({_pct(count, total)})")

    print()
    print("  By category group:")
    for group, count in sorted(group_counts.items()):
        print(f"    {group:<22} : {count:>6,}  ({_pct(count, total)})")

    print()
    print(f"  Unique proponents   : {len(proponents):,}")

    if pub_dates:
        earliest = min(pub_dates).strftime("%d %b %Y")
        latest   = max(pub_dates).strftime("%d %b %Y")
        print(f"  Publication dates   : {earliest} – {latest}")

    print()


def cmd_stats(args: argparse.Namespace) -> None:
    manifest_path = Path(args.output) / args.manifest
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}")
        print("Run 'python scraper.py build' first.")
        return
    _print_manifest_stats(manifest_path, Path(args.output))


# ---------------------------------------------------------------------------
# Step 4: sync-db — seed pipeline.db from existing JSONL files
# ---------------------------------------------------------------------------

def cmd_sync_db(args: argparse.Namespace) -> None:
    if not _db:
        print("Error: db.py not found. Place db.py in the same directory as scrape.py.")
        return

    conn = _db.get_db()
    output_dir = Path(args.output)

    manifest_path = output_dir / args.manifest
    if manifest_path.exists():
        print(f"Seeding from {manifest_path} ...")
        count = _db.seed_from_manifest(conn, manifest_path)
        print(f"  {count:,} publications processed")
    else:
        print(f"Manifest not found: {manifest_path} — skipping")

    log_path = output_dir / "download_log.jsonl"
    if log_path.exists():
        print(f"Updating from {log_path} ...")
        count = _db.seed_from_download_log(conn, log_path)
        print(f"  {count:,} download records applied")
    else:
        print(f"Download log not found: {log_path} — skipping")

    total = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    print(f"\npipeline.db: {total:,} publications")
    for row in conn.execute(
        "SELECT pipeline_status, COUNT(*) n FROM publications GROUP BY pipeline_status ORDER BY n DESC"
    ).fetchall():
        print(f"  {row[0]:<25} : {row[1]:>6,}")

    conn.close()


# ---------------------------------------------------------------------------
# Step 2: download
# ---------------------------------------------------------------------------

# Errors that won't resolve on retry (permanent failures)
_PERMANENT_ERRORS = {"http_404", "http_403", "http_410", "no_pdf", "empty"}


def _fmt_bytes(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def download_pdf(session: requests.Session, pdf_url: str, dest_path: Path, delay: float):
    """Returns (result_code: str, file_bytes: int)."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return "skipped", dest_path.stat().st_size

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    time.sleep(delay)

    try:
        resp = session.get(pdf_url, stream=True, timeout=60)
        if resp.status_code != 200:
            return f"http_{resp.status_code}", 0

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type.lower():
            return "no_pdf", 0

        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        size = dest_path.stat().st_size
        if size == 0:
            dest_path.unlink()
            return "empty", 0

        return "downloaded", size
    except requests.RequestException as e:
        return f"error:{type(e).__name__}", 0


def cmd_download(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    manifest_path = output_dir / args.manifest
    log_path = output_dir / "download_log.jsonl"

    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}")
        print("Run 'python scraper.py build' first.")
        return

    # --- Load manifest (apply filters) ---
    print("Scanning manifest...")
    all_entries = []
    with open(manifest_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not entry.get("pdf_url"):
                continue
            if args.category and entry["category"] != args.category:
                continue
            if args.status and entry["status"].upper() != args.status.upper():
                continue
            all_entries.append(entry)

    if not all_entries:
        print("No downloadable entries found in manifest matching your filters.")
        return

    if args.limit:
        counts: Dict[str, int] = defaultdict(int)
        filtered = []
        for e in all_entries:
            if counts[e["category"]] < args.limit:
                filtered.append(e)
                counts[e["category"]] += 1
        all_entries = filtered

    # --- Build prior-results index from log (last result per pub_id wins) ---
    prior_results: Dict[str, str] = {}
    if log_path.exists():
        with open(log_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    prior_results[rec["pub_id"]] = rec["result"]
                except (json.JSONDecodeError, KeyError):
                    pass

    # --- Pre-classify each entry ---
    on_disk: List[dict] = []
    to_retry: List[dict] = []      # previously failed with a transient error
    permanent_skip: List[dict] = []  # previously failed with a permanent error
    new_work: List[dict] = []

    for e in all_entries:
        filename = e["pdf_url"].split("/")[-1]
        dest_path = output_dir / e["category"] / filename
        if dest_path.exists() and dest_path.stat().st_size > 0:
            on_disk.append(e)
        elif e["pub_id"] in prior_results:
            prior = prior_results[e["pub_id"]]
            if prior in _PERMANENT_ERRORS:
                permanent_skip.append(e)
            else:
                to_retry.append(e)
        else:
            new_work.append(e)

    retry_pub_ids = {e["pub_id"] for e in to_retry}
    work = new_work + to_retry

    print(f"\n  Already on disk         : {len(on_disk):>6,}  (skipping)")
    print(f"  Permanent failures      : {len(permanent_skip):>6,}  (skipping — 404/403/no_pdf)")
    print(f"  Transient failures      : {len(to_retry):>6,}  (retrying)")
    print(f"  Never attempted         : {len(new_work):>6,}  (new)")
    print(f"  {'─' * 34}")
    print(f"  Work this run           : {len(work):>6,}\n")

    if not work:
        print("Nothing to do — all downloads are up to date.")
        print()
        _print_manifest_stats(manifest_path, output_dir)
        return

    # --- Download loop ---
    session = make_session()
    _conn = _db.get_db() if _db else None

    sess_new = 0
    sess_retried_ok = 0
    sess_failed = 0
    sess_bytes = 0
    error_counts: Counter = Counter()

    with tqdm(total=len(work), unit="pdf", dynamic_ncols=True) as bar:
        for entry in work:
            pdf_url = entry["pdf_url"]
            filename = pdf_url.split("/")[-1]
            dest_path = output_dir / entry["category"] / filename

            bar.set_description(f"{entry['pub_number'][:22]:<22}")
            result, file_bytes = download_pdf(session, pdf_url, dest_path, args.delay)

            if result in ("downloaded", "skipped"):
                if entry["pub_id"] in retry_pub_ids:
                    sess_retried_ok += 1
                else:
                    sess_new += 1
                sess_bytes += file_bytes
            else:
                sess_failed += 1
                error_counts[result] += 1
                tqdm.write(f"  FAIL  {entry['pub_number']}: {result}")

            log_ts = datetime.utcnow().isoformat()
            with open(log_path, "a") as lf:
                lf.write(json.dumps({
                    "pub_id": entry["pub_id"],
                    "pub_number": entry["pub_number"],
                    "category": entry["category"],
                    "status": entry["status"],
                    "pdf_url": pdf_url,
                    "local_path": str(dest_path),
                    "result": result,
                    "bytes": file_bytes,
                    "timestamp": log_ts,
                }) + "\n")

            if _conn and result in ("downloaded", "skipped"):
                _db.upsert_publication(_conn, {
                    **entry,
                    "local_path": str(dest_path),
                    "downloaded_at": log_ts,
                })
                _conn.commit()

            bar.update(1)

    # --- Session summary ---
    print("\n=== Session Results ===\n")
    total_ok = sess_new + sess_retried_ok
    print(f"  Downloaded (new)        : {sess_new:>6,}  ({_fmt_bytes(sess_bytes)})")
    if sess_retried_ok:
        print(f"  Downloaded (retried)    : {sess_retried_ok:>6,}")
    print(f"  Skipped (on disk)       : {len(on_disk):>6,}")
    print(f"  Skipped (permanent err) : {len(permanent_skip):>6,}")
    if sess_failed:
        print(f"  Failed this run         : {sess_failed:>6,}")
        print()
        print("  Failures by type:")
        for err, count in sorted(error_counts.items(), key=lambda x: -x[1]):
            print(f"    {err:<26} : {count:>5,}")

    if _conn:
        _conn.close()

    print(f"\n  Log written to: {log_path}\n")
    _print_manifest_stats(manifest_path, output_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_categories(category_arg: Optional[str]) -> Dict[str, str]:
    if not category_arg:
        return CATEGORIES
    if category_arg not in CATEGORIES:
        print(f"Unknown category: {category_arg}")
        print("Available categories:")
        for cat in CATEGORIES:
            print(f"  {cat}")
        raise SystemExit(1)
    return {category_arg: CATEGORIES[category_arg]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Army Publications scraper — two-step workflow: build then download.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--category", help="Scope to one category (e.g. training_doctrine/FM)")
    shared.add_argument("--status", help="Filter by status: ACTIVE, INACTIVE, RESCINDED")
    shared.add_argument("--limit", type=int, default=0, help="Max publications per category (0 = all)")
    shared.add_argument("--delay", type=float, default=1.5, help="Seconds between requests (default: 1.5)")
    shared.add_argument("--output", default=os.getenv("DOWNLOADS_DIR", "downloads"), help="Output directory (default: $DOWNLOADS_DIR or 'downloads')")
    shared.add_argument("--manifest", default="manifest.jsonl", help="Manifest filename (default: manifest.jsonl)")

    subparsers.add_parser(
        "build",
        help="Crawl all categories and record PDF URLs into a manifest (no downloading).",
        parents=[shared],
    )
    subparsers.add_parser(
        "download",
        help="Download PDFs listed in the manifest.",
        parents=[shared],
    )
    subparsers.add_parser(
        "stats",
        help="Print manifest statistics without scraping or downloading.",
        parents=[shared],
    )
    subparsers.add_parser(
        "sync-db",
        help="Seed pipeline.db from existing manifest.jsonl and download_log.jsonl.",
        parents=[shared],
    )

    args = parser.parse_args()

    if args.command == "build":
        cmd_build(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "sync-db":
        cmd_sync_db(args)


if __name__ == "__main__":
    main()
