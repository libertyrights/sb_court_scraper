"""
Slow background case scanner with self-heal.

Runs continuously, scanning remaining case number ranges in small
chunks.  On crash or restart it reads the sequential_scanned.log to
pick up where it left off, so no progress is lost.

Usage:
  python continuous_case_scanner.py --delay 0.5 --chunk 200
  python continuous_case_scanner.py --prefixes VI --years 2021-2023 --slow
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
SCANNED_LOG = STATE_DIR / "sequential_scanned.log"
FOUND_LOG = STATE_DIR / "sequential_found.log"
SCANNER_SCRIPT = SCRIPT_DIR / "sequential_case_scraper.py"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def last_scanned_for(prefix_pattern):
    if not SCANNED_LOG.exists():
        return 0
    lines = SCANNED_LOG.read_text().strip().splitlines()
    last_seq = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith(prefix_pattern):
            try:
                seq = int(line[len(prefix_pattern):])
                if seq > last_seq:
                    last_seq = seq
            except ValueError:
                pass
    return last_seq


def scan_range(args, prefix, year, level, start_seq, count):
    cmd = [
        sys.executable,
        str(SCANNER_SCRIPT),
        "--year", str(year),
        "--prefixes", prefix,
        "--levels", level,
        "--start-seq", str(start_seq),
        "--count", str(count),
        "--delay", str(args.delay),
        "--max-misses", str(args.max_misses),
    ]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.limit_per_prefix:
        cmd += ["--limit-per-prefix", str(args.limit_per_prefix)]
    log_msg = f"[{now_iso()}] {' '.join(cmd)}"
    print(log_msg)
    sys.stdout.flush()
    result = subprocess.run(cmd, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        print(f"  {line}")
    if result.stderr:
        for line in result.stderr.splitlines():
            print(f"  ERR: {line}")
    if result.returncode != 0:
        print(f"  [!] exited code {result.returncode}")
    return result.returncode


def build_plan(args):
    years = list(range(args.start_year, args.end_year + 1))
    prefixes = [p.strip().upper() for p in args.prefixes.split(",")]
    levels = [l.strip().upper() for l in args.levels.split(",")]
    plan = []
    for year in years:
        for level in levels:
            for prefix in prefixes:
                label = f"{level}{prefix}{str(year)[-2:]}"
                last_seq = last_scanned_for(label)
                remaining = args.max_seq - last_seq
                if remaining <= 0:
                    print(f"  {label}: fully scanned (last seq {last_seq})")
                    continue
                plan.append((prefix, year, level, last_seq + 1, remaining, label))
    return plan


def main():
    parser = argparse.ArgumentParser(description="Slow self-healing background case scanner.")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between validate calls (default: 0.5)")
    parser.add_argument("--chunk", type=int, default=200, help="Numbers per scanner subprocess call (default: 200)")
    parser.add_argument("--max-seq", type=int, default=20000, help="Highest sequence number to scan (default: 20000)")
    parser.add_argument("--max-misses", type=int, default=1000, help="Consecutive misses before skipping prefix (default: 1000)")
    parser.add_argument("--limit", type=int, default=0, help="Stop after finding N cases total (0=unlimited)")
    parser.add_argument("--limit-per-prefix", type=int, default=0, help="Stop after finding N cases per prefix (0=unlimited)")
    parser.add_argument("--prefixes", type=str, default="VI", help="Comma-separated court prefixes (default: VI)")
    parser.add_argument("--start-year", type=int, default=2020, help="Start year (default: 2020)")
    parser.add_argument("--end-year", type=int, default=2023, help="End year (default: 2023)")
    parser.add_argument("--levels", type=str, default="F,M", help="Charge levels (default: F,M)")
    parser.add_argument("--loop", action="store_true", help="Keep looping until all ranges are done")
    parser.add_argument("--slow", action="store_true", help="Alias for --delay 1.0 --chunk 100 --loop")
    args = parser.parse_args()

    if args.slow:
        args.delay = 1.0
        args.chunk = 100
        args.loop = True

    print(f"Continuous scanner starting at {now_iso()}")
    print(f"  Delay: {args.delay}s, chunk: {args.chunk}, max-seq: {args.max_seq}")
    print(f"  Prefixes: {args.prefixes}, years: {args.start_year}-{args.end_year}, levels: {args.levels}")
    sys.stdout.flush()

    iteration = 0
    while True:
        iteration += 1
        plan = build_plan(args)
        if not plan:
            print(f"[{now_iso()}] All ranges complete. Exiting.")
            break

        print(f"\n[{now_iso()}] Iteration {iteration}: {len(plan)} range(s) remaining")
        for prefix, year, level, start_seq, remaining, label in plan:
            chunk = min(args.chunk, remaining)
            print(f"  {label}: scanning {start_seq}..{start_seq + chunk - 1} ({remaining} remaining)")
            sys.stdout.flush()
            scan_range(args, prefix, year, level, start_seq, chunk)
            time.sleep(1)

        if not args.loop:
            break

        if iteration % 5 == 0:
            print(f"[{now_iso()}] Heartbeat: iteration {iteration}, sleeping 5s between loops...")
        time.sleep(2)


if __name__ == "__main__":
    main()
