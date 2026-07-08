"""
Scrape AssessorSearch.com for APN<->address lookup.

Reverse-engineered from client-side JS:
  POST https://assessorsearch.com/api/proxy
  Body: {
    "endpoint": "api/search/apn?q={APN}&limit=1",
    "options": {"method": "GET", "headers": {"Content-Type": "application/json"}}
  }
  Returns: [{"property_id":"...","apn":"...","address":"..."}]

Works without authentication. Also supports address search via:
  endpoint: "api/search?q={address}&limit=1"
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
PROPERTY_DB = STATE_DIR / "property_records.db"
COURT_DB = STATE_DIR / "court_calendar.db"

PROXY_URL = "https://assessorsearch.com/api/proxy"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 60


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_apn(apn):
    """Strip hyphens/spaces, return continuous digits."""
    return apn.strip().replace("-", "").replace(" ", "")


def format_apn_for_search(apn):
    """AssessorSearch returns APN in format 0424-171-22-0000, search with raw digits."""
    return normalize_apn(apn)


def call_api(endpoint, params=None):
    """Call AssessorSearch proxy API."""
    qs = f"?{params}" if params else ""
    url = f"{endpoint}{qs}"
    payload = {
        "endpoint": url,
        "options": {
            "method": "GET",
            "headers": {"Content-Type": "application/json"}
        }
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Referer": "https://assessorsearch.com/apn-lookup"
    }
    with httpx.Client(verify=False, timeout=REQUEST_TIMEOUT) as client:
        r = client.post(PROXY_URL, json=payload, headers=headers, follow_redirects=True)
        r.raise_for_status()
        return r.json()


def apn_to_address(apn, limit=1):
    """Look up address by APN. Returns list of {property_id, apn, address}."""
    apn_clean = format_apn_for_search(apn)
    data = call_api("api/search/apn", f"q={quote(apn_clean)}&limit={limit}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return []


def address_to_apn(address, limit=1):
    """Look up APN by address. Returns list of {property_id, apn?, address}."""
    data = call_api("api/search", f"q={quote(address)}&limit={limit}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return []


def log_lookup(pconn, apn, query_type, query_value, result_json):
    pconn.execute(
        """INSERT INTO property_lookup_log
           (apn, query_type, query_value, result_json)
           VALUES (?, ?, ?, ?)""",
        (apn or "", query_type, query_value, json.dumps(result_json)),
    )
    pconn.commit()


def ensure_address_record(pconn, apn, address, source="assessorsearch"):
    """Insert into property_addresses if not already present."""
    if not apn:
        return False
    apn_norm = normalize_apn(apn)
    existing = pconn.execute(
        "SELECT id FROM property_addresses WHERE apn=?", (apn_norm,)
    ).fetchone()
    if existing:
        return False

    # Parse address into components
    parts = address.split(",") if address else [""]
    street = parts[0].strip() if len(parts) > 0 else ""
    city = parts[1].strip() if len(parts) > 1 else ""
    state_zip = parts[2].strip() if len(parts) > 2 else ""

    # Split state and zip
    state = ""
    zip_code = ""
    st_zip = state_zip.strip().split() if state_zip else []
    if len(st_zip) >= 1:
        state = st_zip[0]
    if len(st_zip) >= 2:
        zip_code = st_zip[1]

    # Split street number and name
    street_num = ""
    street_name = street
    st_parts = street.split(None, 1)
    if len(st_parts) == 2 and st_parts[0].isdigit():
        street_num = st_parts[0]
        street_name = st_parts[1]

    pconn.execute(
        """INSERT OR IGNORE INTO property_addresses
           (apn, street_number, street_name, city, state, zip, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (apn_norm, street_num, street_name, city, state, zip_code, source),
    )
    pconn.commit()
    return True


def link_case_to_apn(cconn, case_number, apn, link_type="assessorsearch", notes=""):
    """Create a case_property_links entry."""
    existing = cconn.execute(
        "SELECT id FROM case_property_links WHERE case_number=? AND apn=?",
        (case_number, normalize_apn(apn)),
    ).fetchone()
    if existing:
        return False
    cconn.execute(
        "INSERT INTO case_property_links (apn, case_number, link_type, notes) VALUES (?, ?, ?, ?)",
        (normalize_apn(apn), case_number, link_type, notes),
    )
    cconn.commit()
    return True


def main():
    parser = argparse.ArgumentParser(description="AssessorSearch APN<->Address lookup")
    sub = parser.add_subparsers(dest="command")

    # apn -> address
    apn_p = sub.add_parser("apn", help="Look up address by APN")
    apn_p.add_argument("apn", help="APN (with or without hyphens)")
    apn_p.add_argument("--save", action="store_true", help="Save to property_records.db")

    # address -> apn
    addr_p = sub.add_parser("address", help="Look up APN by address")
    addr_p.add_argument("address", help="Street address (e.g. '33542 National Trails Hwy, Daggett, CA')")
    addr_p.add_argument("--save", action="store_true", help="Save to property_records.db")

    # batch: process all APNs from property_addresses
    batch_p = sub.add_parser("batch-apn", help="Look up all unmatched APNs in property_addresses")
    batch_p.add_argument("--delay", type=float, default=1.0, help="Delay between requests (default: 1.0s)")
    batch_p.add_argument("--limit", type=int, default=0, help="Max APNs to process (0=all)")

    # batch: lookup by defendant name (search Thatsthem-style)
    defendant_p = sub.add_parser("defendant", help="Look up APN by defendant name")
    defendant_p.add_argument("first_name", help="First name")
    defendant_p.add_argument("last_name", help="Last name")
    defendant_p.add_argument("--city", default="Victorville", help="City to narrow search")
    defendant_p.add_argument("--case", help="Case number to link results to")
    defendant_p.add_argument("--save", action="store_true", help="Save results to DB")

    # batch: process code enforcement defendants from court DB
    ce_p = sub.add_parser("batch-defendants", help="Look up all code enforcement defendants from court DB")
    ce_p.add_argument("--delay", type=float, default=2.0, help="Delay between searches (default: 2.0s)")

    args = parser.parse_args()

    if args.command == "apn":
        results = apn_to_address(args.apn)
        print(json.dumps(results, indent=2))
        if args.save and results:
            pconn = sqlite3.connect(str(PROPERTY_DB))
            for r in results:
                apn_out = normalize_apn(r.get("apn", args.apn))
                addr = r.get("address", "")
                log_lookup(pconn, apn_out, "assessorsearch_apn", args.apn, r)
                ensure_address_record(pconn, apn_out, addr, "assessorsearch")
            pconn.close()

    elif args.command == "address":
        results = address_to_apn(args.address)
        print(json.dumps(results, indent=2))
        if args.save and results:
            pconn = sqlite3.connect(str(PROPERTY_DB))
            for r in results:
                apn_out = normalize_apn(r.get("apn", ""))
                addr = r.get("address", args.address)
                log_lookup(pconn, apn_out, "assessorsearch_address", args.address, r)
                if apn_out:
                    ensure_address_record(pconn, apn_out, addr, "assessorsearch")
                else:
                    # No APN returned, just log
                    pass
            pconn.close()

    elif args.command == "batch-apn":
        pconn = sqlite3.connect(str(PROPERTY_DB))
        rows = pconn.execute(
            "SELECT apn, street_name, city FROM property_addresses WHERE apn IS NOT NULL AND apn != ''"
        ).fetchall()
        if args.limit:
            rows = rows[:args.limit]

        print(f"Processing {len(rows)} APNs...")
        found = 0
        not_found = 0
        for i, (apn, street, city) in enumerate(rows):
            try:
                results = apn_to_address(apn)
                if results:
                    log_lookup(pconn, apn, "assessorsearch_batch_apn", apn, results)
                    for r in results:
                        addr = r.get("address", "")
                        if addr:
                            ensure_address_record(pconn, apn, addr, "assessorsearch_batch")
                    found += 1
                else:
                    log_lookup(pconn, apn, "assessorsearch_batch_apn", apn, [])
                    not_found += 1
            except Exception as e:
                log_lookup(pconn, apn, "assessorsearch_batch_apn", apn, {"error": str(e)})
                not_found += 1
                print(f"  Error on {apn}: {e}")

            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(rows)}] found={found} not_found={not_found}")
                pconn.commit()
            if args.delay:
                time.sleep(args.delay)

        pconn.commit()
        print(f"Done: {found} found, {not_found} not found out of {len(rows)}")

    elif args.command == "defendant":
        queries = [f"{args.first_name} {args.last_name} {args.city}"]
        if args.city:
            queries.append(f"{args.first_name} {args.last_name}, {args.city}")

        all_results = []
        for q in queries:
            try:
                results = address_to_apn(q, limit=5)
                all_results.extend(results)
            except Exception as e:
                print(f"  Error searching '{q}': {e}")

        # Deduplicate
        seen = set()
        unique = []
        for r in all_results:
            key = r.get("address", "") + r.get("apn", "")
            if key not in seen:
                seen.add(key)
                unique.append(r)

        print(f"Found {len(unique)} result(s) for {args.first_name} {args.last_name}:")
        print(json.dumps(unique, indent=2))

        if args.save and unique:
            pconn = sqlite3.connect(str(PROPERTY_DB))
            cconn = sqlite3.connect(str(COURT_DB))
            for r in unique:
                apn_out = normalize_apn(r.get("apn", ""))
                addr = r.get("address", "")
                log_lookup(pconn, apn_out, "assessorsearch_defendant",
                           f"{args.first_name} {args.last_name}", r)
                if apn_out:
                    ensure_address_record(pconn, apn_out, addr, "assessorsearch_defendant")
                if apn_out and args.case:
                    link_case_to_apn(cconn, args.case, apn_out,
                                     f"assessorsearch_defendant:{args.first_name} {args.last_name}",
                                     f"Defendant: {args.first_name} {args.last_name}")
            pconn.commit()
            cconn.commit()
            pconn.close()
            cconn.close()

    elif args.command == "batch-defendants":
        pconn = sqlite3.connect(str(PROPERTY_DB))
        cconn = sqlite3.connect(str(COURT_DB))

        # Get code enforcement defendants (IPMC/SBCC charges)
        rows = cconn.execute("""
            SELECT DISTINCT c.case_number, cp.first_name, cp.last_name
            FROM cases c
            JOIN case_charges cc ON c.case_number = cc.case_number
            JOIN case_parties cp ON c.case_number = cp.case_number AND cp.is_defendant = 1
            WHERE (cc.statute_prefix = 'IPMC' OR cc.statute_prefix = 'SBCC')
              AND cp.first_name != ''
            ORDER BY c.case_number
        """).fetchall()

        print(f"Found {len(rows)} code enforcement defendants to search...")

        for i, (case_number, first_name, last_name) in enumerate(rows):
            # Check if already linked
            existing_links = cconn.execute(
                "SELECT id FROM case_property_links WHERE case_number=?", (case_number,)
            ).fetchall()
            if existing_links:
                print(f"  [{i+1}/{len(rows)}] {case_number} ({first_name} {last_name}) - already linked, skipping")
                continue

            print(f"  [{i+1}/{len(rows)}] Searching {first_name} {last_name} (case: {case_number})...")

            # Search with name + Victorville to find address
            queries = [
                f"{first_name} {last_name} Victorville CA",
                f"{first_name} {last_name}, Victorville",
            ]
            found = False
            for q in queries:
                try:
                    results = address_to_apn(q, limit=3)
                    for r in results:
                        apn_out = normalize_apn(r.get("apn", ""))
                        addr = r.get("address", "")
                        log_lookup(pconn, apn_out, "assessorsearch_batch_defendant",
                                   f"{case_number}:{first_name} {last_name}", r)
                        if apn_out:
                            ensure_address_record(pconn, apn_out, addr,
                                                  f"assessorsearch_defendant:{case_number}")
                            link_case_to_apn(cconn, case_number, apn_out,
                                             f"assessorsearch_defendant:{first_name} {last_name}",
                                             f"Defendant: {first_name} {last_name}")
                            found = True
                except Exception as e:
                    print(f"    Error: {e}")

                if found:
                    break

            if not found:
                log_lookup(pconn, "", "assessorsearch_batch_defendant",
                           f"{case_number}:{first_name} {last_name}",
                           {"error": "not found"})

            if args.delay:
                time.sleep(args.delay)

        pconn.commit()
        cconn.commit()
        pconn.close()
        cconn.close()
        print("Batch defendant search complete.")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
