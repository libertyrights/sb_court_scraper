from __future__ import annotations

import hashlib
import io
import os
import sqlite3
from datetime import datetime, timezone
from ftplib import FTP
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "state" / "court_calendar.db"
REMOTE_DIR = "/domains/upnexx.xyz/public_html/osint/private"
REMOTE_FILE = "court_calendar.db"

FTP_HOST = os.environ["FTP_HOST"]
FTP_USER = os.environ["FTP_USER"]
FTP_PASS = os.environ["FTP_PASS"]


def ftp_download_bytes(ftp: FTP, remote_name: str) -> bytes | None:
    buffer = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {remote_name}", buffer.write)
    except Exception as exc:
        if "550" in str(exc):
            return None
        raise
    return buffer.getvalue()


def download_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    ftp = FTP(FTP_HOST)
    try:
        ftp.login(user=FTP_USER, passwd=FTP_PASS)
        ftp.cwd(REMOTE_DIR)

        data = ftp_download_bytes(ftp, REMOTE_FILE)
        if data is None:
            print(f"[-] No remote DB found at {REMOTE_DIR}/{REMOTE_FILE}; starting fresh.")
            return

        sha256 = hashlib.sha256(data).hexdigest()
        print(f"[+] Downloaded {REMOTE_FILE}: {len(data):,} bytes, SHA256: {sha256}")

        DB_PATH.write_bytes(data)
        print(f"[+] Written to {DB_PATH}")
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


if __name__ == "__main__":
    download_db()
