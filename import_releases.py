"""
Import jail release list into court_calendar.db and cross-reference with defendants.

Sources:
  - releases.csv (from daily_release_list.py running via scheduled task)
  - GetReleaseLog API (no captcha required)
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
COURT_DB = STATE_DIR / "court_calendar.db"
DEFAULT_CSV = Path.home() / "Documents" / "python" / "releases.csv"

RELEASE_API = "https://jimsnetil.shr.sbcounty.gov/bookingsearch.aspx/GetReleaseLog"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jail_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sex TEXT,
            age INTEGER,
            height TEXT,
            weight INTEGER,
            release_date TEXT NOT NULL,
            source TEXT DEFAULT 'releases_csv',
            imported_at TEXT DEFAULT (datetime('now')),
            UNIQUE(name, release_date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jail_releases_name
        ON jail_releases (name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jail_releases_date
        ON jail_releases (release_date)
    """)
    conn.commit()


def import_csv(conn, csv_path, verbose=True):
    path = Path(csv_path)
    if not path.exists():
        print(f"ERROR: CSV not found: {path}")
        return 0

    with open(str(path), newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        imported = 0
        skipped = 0
        for row in reader:
            name = (row.get("Name") or "").strip()
            release_date = (row.get("Release Date") or "").strip()
            if not name or not release_date:
                skipped += 1
                continue

            sex = (row.get("Sex") or "").strip()
            age = None
            try:
                age = int(row.get("Age", 0))
            except (ValueError, TypeError):
                pass
            height = (row.get("Height") or "").strip()
            weight = None
            try:
                weight = int(row.get("Weight", 0))
            except (ValueError, TypeError):
                pass

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO jail_releases
                       (name, sex, age, height, weight, release_date, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (name, sex, age, height, weight, release_date, "releases_csv"),
                )
                if conn.total_changes > 0:
                    imported += 1
            except Exception:
                skipped += 1

        conn.commit()
        if verbose:
            print(f"Imported {imported} records from releases.csv ({skipped} skipped)")
        return imported


def fetch_last_import_date(conn):
    row = conn.execute(
        "SELECT MAX(release_date) FROM jail_releases"
    ).fetchone()
    return row[0] if row[0] else None


def fetch_recent_releases(conn, days_back=7):
    """Fetch releases from API for the last N days (no captcha)."""
    today = datetime.now()
    count = 0
    for i in range(days_back):
        date = today - timedelta(days=i)
        date_str = date.strftime("%m/%d/%Y")

        try:
            payload = {
                "mdl": {
                    "__type": "InmateLocator.mdlReleaseLog",
                    "ReleaseDate": date_str,
                }
            }
            headers = {
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
                "Origin": "https://jimsnetil.shr.sbcounty.gov",
                "Referer": "https://jimsnetil.shr.sbcounty.gov/bookingsearch.aspx",
                "User-Agent": USER_AGENT,
            }
            r = httpx.post(RELEASE_API, json=payload, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            html = data.get("d", {}).get("SearchResults", "")
            if not html:
                continue

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table", id="grdResults_grid")
            if not table:
                continue

            for row in table.find_all("tr"):
                cols = [col.get_text(strip=True) for col in row.find_all("td")]
                if len(cols) >= 5:
                    name = cols[0]
                    sex = cols[1] if len(cols) > 1 else ""
                    age = int(cols[2]) if len(cols) > 2 and cols[2].isdigit() else None
                    height = cols[3] if len(cols) > 3 else ""
                    weight = int(cols[4]) if len(cols) > 4 and cols[4].isdigit() else None
                    conn.execute(
                        """INSERT OR IGNORE INTO jail_releases
                           (name, sex, age, height, weight, release_date, source)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (name, sex, age, height, weight, date_str, "api_get_release_log"),
                    )
                    if conn.total_changes > 0:
                        count += 1
            conn.commit()
        except Exception as e:
            print(f"  Error fetching {date_str}: {e}")

    return count


def match_to_defendants(conn, verbose=True):
    """Cross-reference release names against court case defendants."""
    matches = conn.execute("""
        SELECT DISTINCT jr.name, jr.sex, jr.age, jr.release_date,
               cp.case_number, cp.first_name, cp.last_name, cp.full_name,
               c.style
        FROM jail_releases jr
        JOIN case_parties cp ON (
            jr.name LIKE '%' || REPLACE(COALESCE(cp.last_name, ''), ' ', '%') || '%'
            AND jr.name LIKE '%' || REPLACE(COALESCE(cp.first_name, ''), ' ', '%') || '%'
        )
        JOIN cases c ON cp.case_number = c.case_number
        WHERE cp.is_defendant = 1
          AND cp.last_name != ''
          AND cp.first_name != ''
        ORDER BY jr.name, cp.case_number
    """).fetchall()

    if verbose:
        print(f"Found {len(matches)} defendant-release matches")
        for m in matches[:50]:
            print(f"  {m[0]} (released {m[3]}) -> {m[4]} ({m[7]})")

    # Create person_matches for new links
    created = 0
    for m in matches:
        name, sex, age, release_date, case_number, fn, ln, full_name, style = m
        try:
            conn.execute(
                """INSERT OR IGNORE INTO person_matches
                   (case_number, person_name, match_type, source, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (case_number, name, "jail_release", "import_releases",
                 f"Released {release_date} | Sex:{sex} Age:{age}"),
            )
            if conn.total_changes > 0:
                created += 1
        except Exception as e:
            if verbose:
                print(f"  Error inserting person_match for {name}/{case_number}: {e}")

    conn.commit()
    if verbose:
        print(f"Created {created} new person_matches entries")
    return len(matches), created


def main():
    parser = argparse.ArgumentParser(description="Import jail release data")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to releases.csv")
    parser.add_argument("--fetch-api", type=int, default=0,
                        help="Fetch last N days from API (no captcha)")
    parser.add_argument("--match", action="store_true",
                        help="Cross-reference releases against court defendants")
    parser.add_argument("--force", action="store_true",
                        help="Re-import CSV even if already imported")

    args = parser.parse_args()

    conn = sqlite3.connect(str(COURT_DB))
    conn.row_factory = sqlite3.Row
    ensure_tables(conn)

    # Check current state
    existing = conn.execute("SELECT COUNT(*) FROM jail_releases").fetchone()[0]
    print(f"Current jail_releases count: {existing}")

    # Import CSV
    csv_imported = 0
    if existing == 0 or args.force:
        csv_imported = import_csv(conn, args.csv)
    else:
        print("  (skipping CSV import, use --force to re-import)")

    # Fetch from API
    api_imported = 0
    if args.fetch_api > 0:
        print(f"Fetching last {args.fetch_api} days from API...")
        api_imported = fetch_recent_releases(conn, args.fetch_api)
        print(f"  Imported {api_imported} records from API")

    # Match to defendants
    if args.match or (csv_imported > 0 or api_imported > 0):
        matches, person_links = match_to_defendants(conn)

    # Summary
    total = conn.execute("SELECT COUNT(*) FROM jail_releases").fetchone()[0]
    print(f"\nTotal jail_releases: {total}")
    if args.match:
        print(f"Defendant matches: {matches}, person_matches created: {person_links}")

    conn.close()


if __name__ == "__main__":
    main()
