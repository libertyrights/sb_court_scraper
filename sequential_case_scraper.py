"""
Sequential case number scraper for San Bernardino CAP portal.

Scans sequential case numbers (e.g., FVI21000001..FVI21001000) via the
CAP validate API to discover cases not yet in the database.

The validate API requires NO authentication and returns case header info
(case number, style/defendant, status, location, type, judge, file date, internal CAP ID).

For full detail (charges, parties, hearings), run the main calendar scraper
which will pick up newly discovered cases when they appear on a calendar.
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
DB_PATH = STATE_DIR / "court_calendar.db"
STATE_DIR.mkdir(parents=True, exist_ok=True)

VALIDATE_URL = "https://cap.sb-court.org/api/case/validate/"

SCRAPER_VERSION = "2026-07-04-sequential-v3"
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

FOUND_LOG = STATE_DIR / "sequential_found.log"
SCANNED_LOG = STATE_DIR / "sequential_scanned.log"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def clean_text(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return value.strip()


def db_connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def upsert_case(conn, cap_case_id, case_number, api_json):
    raw_data = api_json.get("data", {}) if isinstance(api_json, dict) else {}
    if isinstance(raw_data, list):
        raw_data = raw_data[0] if raw_data else {}
    if not isinstance(raw_data, dict):
        return case_number
    data = raw_data

    now = now_iso()
    existing = conn.execute(
        "SELECT id FROM cases WHERE cap_case_id = ?", (cap_case_id,)
    ).fetchone()

    values = (
        cap_case_id, case_number,
        clean_text(data.get("caseCategory")),
        clean_text(data.get("type")),
        clean_text(data.get("typeId")),
        clean_text(data.get("caseSubType")),
        clean_text(data.get("style")),
        clean_text(data.get("fileDate")),
        clean_text(data.get("status")),
        clean_text(data.get("courtLocation")),
        clean_text(data.get("judicialOfficer")),
        clean_text(data.get("nextHearing")),
        clean_text(data.get("nodeId")),
        clean_text(data.get("citationNumber")),
        json.dumps(data, default=str),
        now, now, now, SCRAPER_VERSION,
    )

    if existing:
        conn.execute(
            """UPDATE cases SET case_number=?, case_category_id=?,
               case_type=?, type_id=?, case_sub_type=?, style=?,
               file_date=?, status=?, court_location=?,
               assigned_judicial_officer_text=?, next_hearing=?,
               node_id=?, citation_number=?, source_hash=?,
               latest_seen_at=?, detail_scraped_at=?, scraper_version=?
               WHERE cap_case_id=?""",
            values[1:15] + values[16:19] + (cap_case_id,),
        )
    else:
        conn.execute(
            """INSERT INTO cases (cap_case_id, case_number, case_category_id,
               case_type, type_id, case_sub_type, style, file_date, status,
               court_location, assigned_judicial_officer_text,
               next_hearing, node_id, citation_number, source_hash,
               first_seen_at, latest_seen_at, detail_scraped_at,
               scraper_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )

    return case_number


def load_scanned_set():
    if not SCANNED_LOG.exists():
        return set()
    return {line.strip() for line in SCANNED_LOG.read_text().strip().splitlines() if line.strip()}


def mark_scanned(case_number):
    with open(SCANNED_LOG, "a") as f:
        f.write(case_number + "\n")


def mark_found(case_number, cap_case_id, style):
    with open(FOUND_LOG, "a") as f:
        f.write(f"{case_number}|{cap_case_id}|{style[:80]}|{now_iso()}\n")


def generate_case_numbers(year, prefix, level, start_seq=1, count=1000):
    yr = str(year)[-2:].zfill(2)
    return [f"{level}{prefix}{yr}{str(i).zfill(6)}" for i in range(start_seq, start_seq + count)]


def call_validate(case_number):
    url = VALIDATE_URL + case_number
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except Exception:
        return None


def scan_prefix(args, conn, year, prefix, level, scanned):
    yr = str(year)[-2:].zfill(2)
    prefix_label = f"{level}{prefix}{yr}"
    case_nums = generate_case_numbers(year, prefix, level, args.start_seq, args.count)
    total = len(case_nums)
    found = 0
    consecutive_misses = 0
    pfx_found = 0

    print(f"  {prefix_label}: {case_nums[0]}..{case_nums[-1]} ({total})")

    for i, case_num in enumerate(case_nums):
        if args.resume and case_num in scanned:
            continue

        resp_json = call_validate(case_num)
        mark_scanned(case_num)

        if resp_json and isinstance(resp_json.get("data"), list) and len(resp_json["data"]) >= 1:
            entry = resp_json["data"][0]
            cap_case_id = clean_text(entry.get("id"))
            style = clean_text(entry.get("style", ""))
            status = clean_text(entry.get("status", ""))
            case_type = clean_text(entry.get("type", ""))
            court = clean_text(entry.get("courtLocation", ""))

            upsert_case(conn, cap_case_id, case_num, resp_json)
            conn.commit()
            mark_found(case_num, cap_case_id, style)

            name = style.replace("The People of the State of California vs.", "").strip()
            print(f"    [{i+1}/{total}] FOUND {case_num} -> {name[:50]} ({case_type}, {status})")
            found += 1
            pfx_found += 1
            consecutive_misses = 0

            if args.limit and found >= args.limit:
                print(f"    Reached overall limit of {args.limit}")
                return found
            if args.limit_per_prefix and pfx_found >= args.limit_per_prefix:
                print(f"    Reached prefix limit of {args.limit_per_prefix}")
                return found
        else:
            consecutive_misses += 1
            if consecutive_misses >= args.max_misses:
                break

        if args.delay > 0:
            time.sleep(args.delay)

    return found


def print_report(found_total, found_by_prefix, elapsed):
    print(f"\n{'='*60}")
    print(f"SCAN COMPLETE")
    print(f"{'='*60}")
    print(f"Total cases found: {found_total}")
    print(f"Time elapsed: {elapsed:.1f}s")
    print(f"Rate: {found_total / max(elapsed, 1):.1f} cases/min")
    print()
    if found_by_prefix:
        print("By prefix:")
        for key, count in sorted(found_by_prefix.items()):
            print(f"  {key}: {count}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Discover CAP cases by scanning sequential case numbers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan FVI20xxxxx (Felony Victorville 2020)
  python sequential_case_scraper.py --year 2020 --prefixes VI --levels F

  # Scan F+ M for VI and WV, years 2020-2022
  python sequential_case_scraper.py --year 2020 --end-year 2022 --prefixes VI,WV

  # Scan first 5000 of FVI21 and MVI21
  python sequential_case_scraper.py --year 2021 --count 5000

  # Resume a previous scan, 0.2s between requests
  python sequential_case_scraper.py --year 2020 --delay 0.2
        """,
    )
    parser.add_argument("--year", type=int, default=2020, help="Starting year (default: 2020)")
    parser.add_argument("--end-year", type=int, help="End year (inclusive, default: same as --year)")
    parser.add_argument("--count", type=int, default=1000, help="Case numbers to try per prefix per year (default: 1000)")
    parser.add_argument("--start-seq", type=int, default=1, help="Starting sequence number (default: 1)")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay in seconds between requests (default: 0.2)")
    parser.add_argument("--max-misses", type=int, default=500, help="Max consecutive misses before skipping prefix (default: 500)")
    parser.add_argument("--prefixes", type=str, default="VI", help="Comma-separated court prefixes (default: VI)")
    parser.add_argument("--levels", type=str, default="F,M", help="Charge levels: F=Felony, M=Misdemeanor (default: F,M)")
    parser.add_argument("--no-resume", dest="resume", action="store_false", default=True, help="Re-check numbers even if already scanned")
    parser.add_argument("--limit", type=int, default=0, help="Stop after finding N cases total (0=unlimited)")
    parser.add_argument("--limit-per-prefix", type=int, default=0, help="Stop after finding N cases per prefix (0=unlimited)")
    args = parser.parse_args()

    prefixes = [p.strip().upper() for p in args.prefixes.split(",")]
    levels = [l.strip().upper() for l in args.levels.split(",")]
    end_year = args.end_year or args.year
    years = list(range(args.year, end_year + 1))

    total_combos = len(years) * len(levels) * len(prefixes)
    estimated_requests = total_combos * args.count
    estimated_time = estimated_requests * args.delay
    print(f"Plan: {total_combos} combos, ~{estimated_requests} requests, ~{estimated_time:.0f}s ({estimated_time/60:.1f} min)")

    scanned = load_scanned_set()
    if scanned:
        print(f"Resuming: {len(scanned)} case numbers already scanned")

    conn = db_connect()
    found_total = 0
    found_by_prefix = {}
    start_time = time.time()

    for year in years:
        for level in levels:
            for prefix in prefixes:
                label = f"{level}{prefix}{str(year)[-2:]}"
                t0 = time.time()
                found = scan_prefix(args, conn, year, prefix, level, scanned)
                elapsed = time.time() - t0
                found_by_prefix[label] = found
                found_total += found
                print(f"  -> {found} found in {elapsed:.1f}s")

    conn.close()
    print_report(found_total, found_by_prefix, time.time() - start_time)


if __name__ == "__main__":
    main()
