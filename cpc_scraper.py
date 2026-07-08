"""
Scrape Citation Processing Center for Administrative Citations by citation number + date.

Uses httpx to handle ASP.NET WebForms __VIEWSTATE flow.
No JavaScript needed — just GET for tokens, then POST with form data.
"""

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
DB_PATH = STATE_DIR / "court_calendar.db"
PROPERTY_DB = STATE_DIR / "property_records.db"

CPC_URL = "https://www.citationprocessingcenter.com/CitationSearch.aspx"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 60


B_NUMBER_CASES = [
    ("B02892", "2023-02-07"),
    ("B02863", "2023-07-19"),
    ("B03484", "2024-10-29"),
    ("B03492", "2025-02-07"),
    ("B03339", "2025-09-30"),
    ("B03501", "2026-04-15"),
    ("B02431", "2026-04-28"),
    ("B02407", "2026-04-17"),
]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def get_hidden_fields(client):
    r = client.get(CPC_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    fields = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        el = soup.find("input", {"name": name})
        if el:
            fields[name] = el.get("value", "")
    return fields


def search_citation(client, hidden_fields, citation_number, date_str):
    data = {
        **hidden_fields,
        "ctl00$contentMain$drpCitationType": "admin",
        "ctl00$contentMain$drpSearchTypeAdmin": "citationdate",
        "ctl00$contentMain$txtCodeCite": citation_number.replace("-", ""),
        "ctl00$contentMain$txtCodeCiteDate": date_str,
        "ctl00$contentMain$btnCitationDateSearch": "Search",
    }
    r = client.post(
        CPC_URL,
        data=data,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    return r.text


def parse_results(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    error_el = soup.find("span", {"id": "ctl00_contentMain_lblMessage"})
    if error_el and error_el.get_text(strip=True):
        error_text = error_el.get_text(strip=True)
        if "no results" in error_text.lower() or "not found" in error_text.lower() or "does not exist" in error_text.lower():
            return results, error_text

    tables = soup.find_all("table")
    violations = []
    for table in tables:
        rows = table.find_all("tr")
        headers = []
        for row in rows:
            cells = row.find_all(["th", "td"])
            texts = [c.get_text(strip=True) for c in cells]
            if any("violation" in t.lower() for t in texts):
                headers = texts
                continue
            if headers and texts and len(texts) == len(headers):
                violations.append(dict(zip(headers, texts)))

    if violations:
        results.extend(violations)

    return results, None


def store_result(conn, b_number, file_date, raw_html, violations, error_msg, scraped_at):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cpc_scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            citation_number TEXT NOT NULL,
            file_date TEXT,
            scraped_at TEXT,
            raw_html TEXT,
            violation_count INTEGER,
            error_message TEXT
        )
    """)
    conn.execute(
        "INSERT INTO cpc_scrape_log (citation_number, file_date, scraped_at, raw_html, violation_count, error_message) VALUES (?, ?, ?, ?, ?, ?)",
        (b_number, file_date, scraped_at, raw_html, len(violations), error_msg),
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Scrape CPC for B-number citations")
    parser.add_argument("--numbers", nargs="+", help="Specific B-numbers to search (e.g., B02863)")
    parser.add_argument("--dates", nargs="+", help="Corresponding dates (same order as --numbers)")
    parser.add_argument("--all", action="store_true", help="Search all known B-number cases")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be searched")
    args = parser.parse_args()

    if args.all:
        targets = B_NUMBER_CASES
    elif args.numbers:
        dates = args.dates or []
        targets = []
        for i, num in enumerate(args.numbers):
            d = dates[i] if i < len(dates) else ""
            targets.append((num.upper(), d))
    else:
        parser.print_help()
        sys.exit(1)

    if args.dry_run:
        print(f"Would search {len(targets)} citations:")
        for num, date in targets:
            print(f"  {num} ({date})")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    with httpx.Client(verify=False) as client:
        print("Fetching page to get __VIEWSTATE tokens...")
        hidden = get_hidden_fields(client)
        print(f"  __VIEWSTATE: {hidden.get('__VIEWSTATE', '')[:40]}...")
        print(f"  __EVENTVALIDATION: {hidden.get('__EVENTVALIDATION', '')[:40]}...")

        for citation_number, file_date in targets:
            print(f"\nSearching {citation_number} ({file_date})...")
            scraped_at = now_iso()

            try:
                html = search_citation(client, hidden, citation_number, file_date)
                violations, error_msg = parse_results(html)

                if violations:
                    print(f"  Found {len(violations)} violation(s):")
                    for v in violations:
                        print(f"    {v}")
                else:
                    print(f"  No violations found (error: {error_msg})")

                store_result(conn, citation_number, file_date, html, violations, error_msg or "", scraped_at)
                print(f"  Stored in cpc_scrape_log")

            except Exception as e:
                print(f"  ERROR: {e}")
                store_result(conn, citation_number, file_date, "", [], str(e), scraped_at)

            time.sleep(2)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
