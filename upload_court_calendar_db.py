from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from ftplib import FTP
from pathlib import Path

FTP_HOST = os.environ["FTP_HOST"]
FTP_USER = os.environ["FTP_USER"]
FTP_PASS = os.environ["FTP_PASS"]

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "state" / "court_calendar.db"
REMOTE_DIR = "/domains/upnexx.xyz/public_html/osint/private"
REMOTE_FILE = "court_calendar.db"
PUBLIC_REMOTE_DIR = "/domains/upnexx.xyz/public_html/osint"
PUBLIC_RECORDS_FILE = "court_records.json"
DETAIL_TABLES = [
    "case_aliases",
    "case_arrests",
    "case_attorneys",
    "case_bonds",
    "case_charge_dispositions",
    "case_charge_sentences",
    "case_charges",
    "case_cross_reference_numbers",
    "case_demographics",
    "case_documents",
    "case_events",
    "case_financial_transactions",
    "case_financials",
    "case_flags",
    "case_hearing_documents",
    "case_hearings",
    "case_parties",
    "case_party_addresses",
    "case_party_identifiers",
    "case_warrants",
    "external_case_links",
    "related_cases",
]


def ensure_remote_dir(ftp: FTP, remote_dir: str) -> None:
    parts = [part for part in remote_dir.split("/") if part]
    ftp.cwd("/")
    current = ""
    for part in parts:
        current += "/" + part
        try:
            ftp.cwd(current)
        except Exception:
            ftp.mkd(current)
            ftp.cwd(current)


def ftp_download_bytes(ftp: FTP, remote_name: str) -> bytes | None:
    buffer = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {remote_name}", buffer.write)
    except Exception as exc:
        if "550" in str(exc):
            return None
        raise
    return buffer.getvalue()


def ftp_upload_bytes(ftp: FTP, remote_name: str, data: bytes) -> None:
    ftp.storbinary(f"STOR {remote_name}", io.BytesIO(data))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def table_exists(cur: sqlite3.Cursor, table_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    )
    return cur.fetchone() is not None


def count_rows(cur: sqlite3.Cursor, table_name: str) -> int:
    if not table_exists(cur, table_name):
        return 0
    cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
    value = cur.fetchone()[0]
    return int(value or 0)


def summarize_db(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        detail_total = 0
        if table_exists(cur, "case_details"):
            detail_total = count_rows(cur, "case_details")
        else:
            detail_total = sum(count_rows(cur, table_name) for table_name in DETAIL_TABLES)
        summary = {}
        summary["cases"] = count_rows(cur, "cases")
        summary["appearances"] = count_rows(cur, "calendar_appearances")
        summary["details"] = detail_total
        if table_exists(cur, "runs"):
            cur.execute(
                "SELECT finished_at FROM runs WHERE TRIM(COALESCE(finished_at,''))<>'' ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            summary["latest_run_finished_at"] = (row[0] if row else "") or ""
        else:
            summary["latest_run_finished_at"] = ""
        return summary
    finally:
        conn.close()


def row_value(row: sqlite3.Row, key: str) -> str:
    value = row[key] if key in row.keys() else ""
    return str(value or "").strip()


def export_public_court_records(db_path: Path) -> bytes:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        for row in conn.execute(
            """
            SELECT
                c.cap_case_id,
                c.case_number,
                c.case_type,
                c.style,
                c.file_date,
                c.status,
                c.court_location,
                c.assigned_judicial_officer_text,
                c.next_hearing,
                c.citation_number,
                c.is_criminal,
                c.first_seen_at,
                c.latest_seen_at,
                c.detail_scraped_at,
                (
                    SELECT GROUP_CONCAT(DISTINCT cp.full_name)
                    FROM case_parties cp
                    WHERE cp.cap_case_id = c.cap_case_id
                      AND COALESCE(cp.full_name, '') <> ''
                      AND (cp.is_defendant = 1 OR UPPER(COALESCE(cp.party_type, '')) LIKE '%DEF%')
                ) AS defendants,
                (
                    SELECT GROUP_CONCAT(DISTINCT ca.full_name)
                    FROM case_aliases ca
                    WHERE ca.cap_case_id = c.cap_case_id
                      AND COALESCE(ca.full_name, '') <> ''
                ) AS aliases,
                (
                    SELECT GROUP_CONCAT(DISTINCT TRIM(COALESCE(cc.statute_raw, '') || ' ' || COALESCE(cc.offense_description, '')))
                    FROM case_charges cc
                    WHERE cc.cap_case_id = c.cap_case_id
                      AND TRIM(COALESCE(cc.statute_raw, '') || COALESCE(cc.offense_description, '')) <> ''
                ) AS charges,
                (
                    SELECT MAX(COALESCE(ch.hearing_date, ''))
                    FROM case_hearings ch
                    WHERE ch.cap_case_id = c.cap_case_id
                ) AS latest_hearing_date,
                (
                    SELECT COUNT(*)
                    FROM case_events ce
                    WHERE ce.cap_case_id = c.cap_case_id
                ) AS event_count
            FROM cases c
            ORDER BY COALESCE(c.file_date, '') DESC, c.case_number DESC
            """
        ):
            rows.append(
                {
                    "cap_case_id": row_value(row, "cap_case_id"),
                    "caseNumber": row_value(row, "case_number"),
                    "caseType": row_value(row, "case_type"),
                    "caseName": row_value(row, "style"),
                    "name": row_value(row, "defendants") or row_value(row, "style"),
                    "aliases": row_value(row, "aliases"),
                    "fileDate": row_value(row, "file_date"),
                    "date": row_value(row, "file_date"),
                    "status": row_value(row, "status"),
                    "court": row_value(row, "court_location"),
                    "judge": row_value(row, "assigned_judicial_officer_text"),
                    "nextHearing": row_value(row, "next_hearing"),
                    "latestHearingDate": row_value(row, "latest_hearing_date"),
                    "citationNumber": row_value(row, "citation_number"),
                    "charge": row_value(row, "charges"),
                    "eventCount": int(row["event_count"] or 0),
                    "isCriminal": bool(row["is_criminal"]),
                    "firstSeenAt": row_value(row, "first_seen_at"),
                    "latestSeenAt": row_value(row, "latest_seen_at"),
                    "detailScrapedAt": row_value(row, "detail_scraped_at"),
                }
            )
    finally:
        conn.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(db_path),
        "count": len(rows),
        "records": rows,
    }
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def rotate_remote_file(ftp: FTP, remote_name: str, data: bytes) -> bool:
    remote_bytes = ftp_download_bytes(ftp, remote_name)
    if remote_bytes == data:
        print(f"[+] Remote {remote_name} already matches local DB; skipping upload.")
        return False

    temp_remote = f"{remote_name}.new"
    backup_remote = f"{remote_name}.bak"
    if remote_bytes is not None and len(remote_bytes) > len(data):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        larger_backup = f"{remote_name}.larger-before-override.{stamp}.bak"
        ftp_upload_bytes(ftp, larger_backup, remote_bytes)
        print(f"[+] Backed up larger target before override: {larger_backup}")

    ftp_upload_bytes(ftp, temp_remote, data)
    print(f"[+] Uploaded {temp_remote}")

    try:
        ftp.delete(backup_remote)
    except Exception:
        pass

    try:
        ftp.rename(remote_name, backup_remote)
        print(f"[+] Rotated {remote_name} to {backup_remote}")
    except Exception:
        print(f"[i] No existing {remote_name} found to rotate")

    ftp.rename(temp_remote, remote_name)
    print(f"[+] Promoted {temp_remote} to {remote_name}")
    return True


def upload_db() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Local SQLite DB not found: {DB_PATH}")

    local_bytes = DB_PATH.read_bytes()
    summary = summarize_db(DB_PATH)

    print(f"[+] Local DB: {DB_PATH}")
    print(f"[+] Size: {len(local_bytes):,} bytes")
    print(f"[+] SHA256: {sha256_bytes(local_bytes)}")
    print(
        "[+] Summary: "
        f"{summary['cases']} cases, "
        f"{summary['appearances']} appearances, "
        f"{summary['details']} detail rows"
    )
    if summary.get("latest_run_finished_at"):
        print(f"[+] Latest scraper run finished at: {summary['latest_run_finished_at']}")
    print(f"[+] Upload started: {datetime.now(timezone.utc).isoformat()}")

    ftp = FTP(FTP_HOST)
    try:
        ftp.login(user=FTP_USER, passwd=FTP_PASS)
        print("[+] Connected to FTP server")
        ensure_remote_dir(ftp, REMOTE_DIR)
        ftp.cwd(REMOTE_DIR)
        print(f"[+] Changed directory to {REMOTE_DIR}")
        rotate_remote_file(ftp, REMOTE_FILE, local_bytes)

        public_records = export_public_court_records(DB_PATH)
        ensure_remote_dir(ftp, PUBLIC_REMOTE_DIR)
        ftp.cwd(PUBLIC_REMOTE_DIR)
        print(f"[+] Changed directory to {PUBLIC_REMOTE_DIR}")
        print(f"[+] Public court records: {len(public_records):,} bytes")
        rotate_remote_file(ftp, PUBLIC_RECORDS_FILE, public_records)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


if __name__ == "__main__":
    upload_db()
