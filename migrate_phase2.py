"""
Phase 2 migration: add category column, create property_records.db,
case_property_links, person_matches, and entity_links tables.

Idempotent — safe to re-run.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
DB_PATH = STATE_DIR / "court_calendar.db"
PROPERTY_DB_PATH = STATE_DIR / "property_records.db"

PREFIX_CATEGORY_MAP = {
    "F": "criminal",
    "M": "criminal",
    "CIV": "civil",
    "CV": "civil",
    "LLT": "civil",
    "SC": "civil",
    "UD": "civil",
    "CON": "conservatorship",
    "PRO": "probate",
    "GAR": "guardianship",
    "TRU": "trust",
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def db_connect(path=None):
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row is not None


def column_exists(conn, table_name, column_name):
    if not table_exists(conn, table_name):
        return False
    cols = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    return any(c["name"] == column_name for c in cols)


def add_column_if_missing(conn, table_name, column_name, definition):
    if table_exists(conn, table_name) and not column_exists(conn, table_name, column_name):
        conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}')
        print(f"  Added column {table_name}.{column_name}")
        return True
    return False


def classify_case_prefix(case_number):
    if not case_number:
        return "unknown"
    if len(case_number) == 11 and case_number[0] in ("F", "M"):
        return "criminal"
    for prefix, category in sorted(PREFIX_CATEGORY_MAP.items(), key=lambda x: -len(x[0])):
        if case_number.upper().startswith(prefix):
            return category
    return "unknown"


def create_property_db():
    print("\nCreating property_records.db...")
    conn = db_connect(PROPERTY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS property_addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            apn TEXT,
            street_number TEXT,
            street_name TEXT,
            unit TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            owner_name TEXT,
            property_value REAL,
            acreage REAL,
            latitude REAL,
            longitude REAL,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS property_lookup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_term TEXT,
            source TEXT,
            results_count INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_property_apn ON property_addresses(apn)",
        "CREATE INDEX IF NOT EXISTS idx_property_owner ON property_addresses(owner_name)",
        "CREATE INDEX IF NOT EXISTS idx_property_zip ON property_addresses(zip)",
    ]:
        conn.execute(idx)
    conn.commit()
    conn.close()
    print("  Done.")


def add_category_column():
    print("\nAdding category column to cases...")
    conn = db_connect()
    add_column_if_missing(conn, "cases", "category", "TEXT")
    conn.commit()
    conn.close()
    print("  Done.")


def classify_existing_cases():
    print("\nClassifying existing cases by case_number prefix...")
    conn = db_connect()
    total = conn.execute("SELECT COUNT(*) FROM cases WHERE category IS NULL").fetchone()[0]
    if total == 0:
        print("  All cases already classified.")
        conn.close()
        return

    rows = conn.execute(
        "SELECT id, case_number FROM cases WHERE category IS NULL"
    ).fetchall()
    updates = 0
    for row in rows:
        cat = classify_case_prefix(row["case_number"])
        conn.execute("UPDATE cases SET category=? WHERE id=?", (cat, row["id"]))
        updates += 1
        if updates % 5000 == 0:
            conn.commit()
            print(f"  {updates}/{total}")
    conn.commit()
    print(f"  Classified {updates} cases.")
    conn.close()


def create_case_property_links():
    print("\nCreating case_property_links table...")
    conn = db_connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS case_property_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            apn TEXT,
            case_number TEXT,
            link_type TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_cpl_apn ON case_property_links(apn)",
        "CREATE INDEX IF NOT EXISTS idx_cpl_case ON case_property_links(case_number)",
    ]:
        conn.execute(idx)
    conn.commit()
    conn.close()
    print("  Done.")


def create_person_matches():
    print("\nCreating person_matches table...")
    conn = db_connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS person_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_name TEXT,
            case_number TEXT,
            match_type TEXT,
            url TEXT,
            source TEXT,
            notes TEXT,
            confirmed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_pm_name ON person_matches(person_name)",
        "CREATE INDEX IF NOT EXISTS idx_pm_case ON person_matches(case_number)",
        "CREATE INDEX IF NOT EXISTS idx_pm_confirmed ON person_matches(confirmed)",
    ]:
        conn.execute(idx)
    conn.commit()
    conn.close()
    print("  Done.")


def create_entity_links():
    print("\nCreating entity_links table...")
    conn = db_connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS entity_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            link_value TEXT NOT NULL,
            label TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_el_entity ON entity_links(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_el_link_type ON entity_links(link_type)",
    ]:
        conn.execute(idx)
    conn.commit()
    conn.close()
    print("  Done.")


def import_media_matches_from_report():
    """Import existing media cross-references from the report into person_matches."""
    import re as _re

    report_path = Path(
        r"C:\Users\mark\AppData\Local\Temp\opencode\media_cross_reference_report.md"
    )
    if not report_path.exists():
        print("\n  Skipping media import: report file not found.")
        return

    print("\nImporting media matches from report...")
    conn = db_connect()
    text = report_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    imported = 0
    skipped = 0
    current_name = None
    current_case = None
    in_urls_block = False

    for line in lines:
        name_match = _re.match(r"^####\s+\d+\.\s+(.+)", line)
        if name_match:
            current_name = name_match.group(1).strip()
            current_case = None
            in_urls_block = False
            continue

        case_match = _re.match(r"^-\s+\*\*Case[s]?:\*\*\s*(.*)", line)
        if case_match:
            raw = case_match.group(1).strip()
            parts = [p.strip() for p in _re.split(r"[+/]", raw) if p.strip()]
            current_case = parts[0] if parts else raw
            continue

        url_match = _re.match(r"^-\s+\*\*URL[s]?:\*\*\s*(.*)", line)
        if url_match:
            in_urls_block = True
            url = url_match.group(1).strip()
            if url and current_case and current_name:
                existing = conn.execute(
                    "SELECT 1 FROM person_matches WHERE url=? AND case_number=?",
                    (url, current_case),
                ).fetchone()
                if existing:
                    skipped += 1
                else:
                    conn.execute(
                        "INSERT INTO person_matches (person_name, case_number, match_type, url, notes, confirmed) VALUES (?, ?, 'media_article', ?, ?, 1)",
                        (current_name, current_case, url, ""),
                    )
                    imported += 1
            continue

        if in_urls_block:
            ul_match = _re.match(r"^\s+-\s+(https?://\S+)", line)
            if ul_match:
                url = ul_match.group(1).strip()
                if current_case and current_name:
                    existing = conn.execute(
                        "SELECT 1 FROM person_matches WHERE url=? AND case_number=?",
                        (url, current_case),
                    ).fetchone()
                    if existing:
                        skipped += 1
                    else:
                        conn.execute(
                            "INSERT INTO person_matches (person_name, case_number, match_type, url, notes, confirmed) VALUES (?, ?, 'media_article', ?, ?, 1)",
                            (current_name, current_case, url, ""),
                        )
                        imported += 1
                continue
            in_urls_block = False

    conn.commit()
    conn.close()
    print(f"  Imported {imported}, skipped {skipped} duplicates.")


def print_summary():
    conn = db_connect()
    PROPERTY_DB_PATH
    pconn = db_connect(PROPERTY_DB_PATH) if PROPERTY_DB_PATH.exists() else None

    print(f"\n{'='*60}")
    print("MIGRATION SUMMARY")
    print(f"{'='*60}")

    total = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    classified = conn.execute("SELECT COUNT(*) FROM cases WHERE category IS NOT NULL").fetchone()[0]
    cats = conn.execute(
        "SELECT category, COUNT(*) FROM cases WHERE category IS NOT NULL GROUP BY category ORDER BY category"
    ).fetchall()

    print(f"  cases: {total} total, {classified} classified")
    for c in cats:
        print(f"    {c[0]}: {c[1]}")

    for tbl in ["case_property_links", "person_matches", "entity_links"]:
        if table_exists(conn, tbl):
            cnt = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl}: {cnt} rows")

    if pconn:
        for tbl in ["property_addresses", "property_lookup_log"]:
            if table_exists(pconn, tbl):
                cnt = pconn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                print(f"  property_records.db/{tbl}: {cnt} rows")
        pconn.close()

    conn.close()


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("DRY RUN — no changes will be made")
        print("\nWould execute:")
        print("  1. Create property_records.db with property_addresses + lookup_log")
        print("  2. Add category column to cases table")
        print("  3. Classify all existing cases by prefix")
        print("  4. Create case_property_links table")
        print("  5. Create person_matches table")
        print("  6. Create entity_links table")
        print("  7. Import media matches from report")
        print_summary()
        return

    print(f"Phase 2 Migration — {now_iso()}")
    print(f"Database: {DB_PATH}")

    create_property_db()
    add_category_column()
    classify_existing_cases()
    create_case_property_links()
    create_person_matches()
    create_entity_links()
    import_media_matches_from_report()

    print_summary()
    print(f"\nMigration complete — {now_iso()}")


if __name__ == "__main__":
    main()
