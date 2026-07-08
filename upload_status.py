from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from ftplib import FTP
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "state" / "court_calendar.db"
REMOTE_DIR = "/domains/upnexx.xyz/public_html/status"
REMOTE_STATUS_FILE = "scraper_status.json"

FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")

DETAIL_TABLES = [
    "case_aliases", "case_arrests", "case_attorneys", "case_bonds",
    "case_charge_dispositions", "case_charge_sentences", "case_charges",
    "case_cross_reference_numbers", "case_demographics", "case_documents",
    "case_events", "case_financial_transactions", "case_financials",
    "case_flags", "case_hearing_documents", "case_hearings", "case_parties",
    "case_party_addresses", "case_party_identifiers", "case_warrants",
    "external_case_links", "related_cases",
]

GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
GITHUB_SERVER_URL = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")


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


def build_status() -> dict:
    if not DB_PATH.exists():
        return {"error": "DB not found", "timestamp": datetime.now(timezone.utc).isoformat()}

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        detail_total = sum(count_rows(cur, t) for t in DETAIL_TABLES)

        status = {
            "source": "sb_court_scraper",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "db_size_bytes": DB_PATH.stat().st_size,
            "db_sha256": hashlib.sha256(DB_PATH.read_bytes()).hexdigest(),
            "cases": count_rows(cur, "cases"),
            "appearances": count_rows(cur, "calendar_appearances"),
            "details": detail_total,
            "github_run_id": GITHUB_RUN_ID,
            "github_repo": GITHUB_REPOSITORY,
        }

        if table_exists(cur, "runs"):
            cur.execute(
                "SELECT finished_at FROM runs WHERE TRIM(COALESCE(finished_at,''))<>'' ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                status["latest_run_finished_at"] = row[0]

        if table_exists(cur, "browser_jobs"):
            cur.execute(
                "SELECT status, COUNT(*) as cnt FROM browser_jobs GROUP BY status"
            )
            job_statuses = {}
            for row in cur.fetchall():
                job_statuses[row[0]] = row[1]
            status["job_statuses"] = job_statuses

        return status
    finally:
        conn.close()


def ftp_upload_bytes(ftp: FTP, remote_name: str, data: bytes) -> None:
    ftp.storbinary(f"STOR {remote_name}", io.BytesIO(data))


def ensure_remote_dir(ftp: FTP, remote_dir: str) -> None:
    parts = [part for part in remote_dir.split("/") if part]
    ftp.cwd("/")
    for part in parts:
        try:
            ftp.cwd(part)
        except Exception:
            ftp.mkd(part)
            ftp.cwd(part)


def upload_status() -> None:
    status = build_status()
    status_json = json.dumps(status, indent=2)
    print(f"[+] Status:\n{status_json}")

    if not FTP_HOST:
        print("[i] No FTP_HOST set; skipping upload.")
        return

    GITHUB_STEP_SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if GITHUB_STEP_SUMMARY:
        summary_path = Path(GITHUB_STEP_SUMMARY)
        summary_path.write_text(f"```json\n{status_json}\n```")

    ftp = FTP(FTP_HOST)
    try:
        ftp.login(user=FTP_USER, passwd=FTP_PASS)
        ensure_remote_dir(ftp, REMOTE_DIR)
        ftp.cwd(REMOTE_DIR)
        ftp_upload_bytes(ftp, REMOTE_STATUS_FILE, status_json.encode("utf-8"))
        print(f"[+] Uploaded {REMOTE_STATUS_FILE} to {REMOTE_DIR}/")
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


if __name__ == "__main__":
    upload_status()
