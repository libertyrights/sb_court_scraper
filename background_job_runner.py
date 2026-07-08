import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS browser_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_key TEXT UNIQUE,
            job_type TEXT,
            label TEXT,
            status TEXT,
            detail TEXT,
            target_case_id TEXT,
            target_case_number TEXT,
            pid INTEGER,
            command_text TEXT,
            log_path TEXT,
            started_at TEXT,
            updated_at TEXT,
            finished_at TEXT,
            metadata_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_browser_jobs_status
            ON browser_jobs(status, updated_at);

        CREATE TABLE IF NOT EXISTS case_jail_captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cap_case_id TEXT,
            case_number TEXT,
            defendant_name TEXT,
            booking_number TEXT,
            inmate_name TEXT,
            dob TEXT,
            age TEXT,
            sex TEXT,
            arrest_date TEXT,
            arrest_agency TEXT,
            arrest_location TEXT,
            housing_facility TEXT,
            release_date TEXT,
            captured_at TEXT,
            result_detected INTEGER DEFAULT 0,
            source_url TEXT,
            search_mode TEXT,
            search_payload_json TEXT,
            summary_json TEXT,
            html_path TEXT,
            screenshot_path TEXT,
            meta_path TEXT,
            json_path TEXT,
            created_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_case_jail_captures_case_time
            ON case_jail_captures(cap_case_id, captured_at DESC);
        """
    )
    conn.commit()


def set_job(
    conn,
    *,
    job_key,
    job_type,
    label,
    status,
    detail,
    target_case_id,
    target_case_number,
    pid,
    command_text,
    log_path,
    finished=False,
):
    now = now_iso()
    row = conn.execute("SELECT id, started_at FROM browser_jobs WHERE job_key = ?", (job_key,)).fetchone()
    started_at = row["started_at"] if row and row["started_at"] else now

    if row:
        conn.execute(
            """
            UPDATE browser_jobs
            SET job_type = ?,
                label = ?,
                status = ?,
                detail = ?,
                target_case_id = ?,
                target_case_number = ?,
                pid = ?,
                command_text = ?,
                log_path = ?,
                updated_at = ?,
                finished_at = CASE WHEN ? THEN ? ELSE NULL END
            WHERE job_key = ?
            """,
            (
                job_type,
                label,
                status,
                detail,
                target_case_id,
                target_case_number,
                pid,
                command_text,
                log_path,
                now,
                1 if finished else 0,
                now,
                job_key,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO browser_jobs (
                job_key,
                job_type,
                label,
                status,
                detail,
                target_case_id,
                target_case_number,
                pid,
                command_text,
                log_path,
                started_at,
                updated_at,
                finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_key,
                job_type,
                label,
                status,
                detail,
                target_case_id,
                target_case_number,
                pid,
                command_text,
                log_path,
                started_at,
                now,
                now if finished else None,
            ),
        )
    conn.commit()


def iter_process_output(proc, handle):
    trailing = []
    if not proc.stdout:
        return trailing

    for line in proc.stdout:
        handle.write(line)
        handle.flush()
        text = line.rstrip()
        if text:
            trailing.append(text)
            trailing = trailing[-12:]
    return trailing


def main():
    parser = argparse.ArgumentParser(description="Run a browser-side background job and persist its status.")
    parser.add_argument("--db", required=True, help="Path to the court browser SQLite DB.")
    parser.add_argument("--job-key", required=True)
    parser.add_argument("--job-type", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--target-case-id", default="")
    parser.add_argument("--target-case-number", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--log-path", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("No background command supplied.")

    db_path = Path(args.db).resolve()
    log_path = Path(args.log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        set_job(
            conn,
            job_key=args.job_key,
            job_type=args.job_type,
            label=args.label,
            status="running",
            detail="Background job started.",
            target_case_id=args.target_case_id,
            target_case_number=args.target_case_number,
            pid=0,
            command_text=subprocess.list2cmdline(command),
            log_path=str(log_path),
            finished=False,
        )
    finally:
        conn.close()

    with log_path.open("a", encoding="utf-8", errors="ignore") as handle:
        handle.write(f"[{now_iso()}] Starting job {args.job_key}\n")
        handle.write(f"Command: {subprocess.list2cmdline(command)}\n\n")
        handle.flush()

        proc = subprocess.Popen(
            command,
            cwd=args.cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            ensure_schema(conn)
            set_job(
                conn,
                job_key=args.job_key,
                job_type=args.job_type,
                label=args.label,
                status="running",
                detail="Background job is running.",
                target_case_id=args.target_case_id,
                target_case_number=args.target_case_number,
                pid=proc.pid,
                command_text=subprocess.list2cmdline(command),
                log_path=str(log_path),
                finished=False,
            )
        finally:
            conn.close()

        tail = iter_process_output(proc, handle)
        exit_code = proc.wait()

        detail = "Completed successfully."
        status = "complete"
        if exit_code != 0:
            status = "error"
            detail = f"Exited with code {exit_code}."
        elif tail:
            detail = tail[-1]

        handle.write(f"\n[{now_iso()}] Job finished with exit code {exit_code}\n")
        handle.flush()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        set_job(
            conn,
            job_key=args.job_key,
            job_type=args.job_type,
            label=args.label,
            status=status,
            detail=detail,
            target_case_id=args.target_case_id,
            target_case_number=args.target_case_number,
            pid=proc.pid,
            command_text=subprocess.list2cmdline(command),
            log_path=str(log_path),
            finished=True,
        )
    finally:
        conn.close()

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
