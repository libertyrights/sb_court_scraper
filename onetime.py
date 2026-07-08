import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "state" / "court_calendar.db"


DEFAULT_TABLES = [
    "case_details",
]


OPTIONAL_DETAIL_TABLES = [
    "case_details_raw",
    "case_details_raw_probe",
    "case_tab_snapshots",
    "case_tab_snapshots_probe",
    "extracted_tables",
    "extracted_tables_probe",
]


def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def count_rows(conn, table_name):
    return conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]


def backup_db(db_path):
    backup_path = db_path.with_name(f"{db_path.stem}_backup_before_clear_{now_stamp()}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to SQLite DB.")
    parser.add_argument("--tables", default=",".join(DEFAULT_TABLES), help="Comma-separated table list.")
    parser.add_argument("--include-optional-detail-tables", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    tables = [x.strip() for x in args.tables.split(",") if x.strip()]

    if args.include_optional_detail_tables:
        tables.extend(OPTIONAL_DETAIL_TABLES)

    # Deduplicate while preserving order.
    tables = list(dict.fromkeys(tables))

    conn = sqlite3.connect(db_path)

    existing = []
    missing = []

    for table in tables:
        if table_exists(conn, table):
            existing.append(table)
        else:
            missing.append(table)

    print(f"DB: {db_path}")
    print("\nTables found:")

    total_rows = 0
    for table in existing:
        n = count_rows(conn, table)
        total_rows += n
        print(f"  {table}: {n} rows")

    if missing:
        print("\nTables not found/skipped:")
        for table in missing:
            print(f"  {table}")

    if args.dry_run:
        print("\nDRY RUN ONLY. Nothing was deleted.")
        conn.close()
        return

    if not existing:
        print("\nNo matching tables to clear.")
        conn.close()
        return

    if not args.yes:
        print("\nRefusing to delete without --yes.")
        print("Run with --yes after reviewing the table list.")
        conn.close()
        return

    backup_path = backup_db(db_path)
    print(f"\nBackup created: {backup_path}")

    with conn:
        for table in existing:
            print(f"Clearing {table}...")
            conn.execute(f'DELETE FROM "{table}"')

    if args.vacuum:
        print("Running VACUUM...")
        conn.execute("VACUUM")

    conn.close()

    print(f"\nDone. Deleted {total_rows} rows from {len(existing)} table(s).")


if __name__ == "__main__":
    main()