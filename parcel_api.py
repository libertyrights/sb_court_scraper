"""
Query San Bernardino County Parcel REST API by APN.

API uses ParcelNumber (9-digit format, e.g. 042417122) matching the first 9
digits of the 13-digit county index APN (e.g. 0424171220000).

OwnerName is protected per CA Gov Code 7928.205, so the API is mainly
useful for parcel characteristics: zoning, land use, values, acreage, geometry.
"""

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
PROPERTY_DB = STATE_DIR / "property_records.db"
COURT_DB = STATE_DIR / "court_calendar.db"

PARCEL_URL = (
    "https://services.arcgis.com/aA3snZwJfFkVyDuP/arcgis/rest/services/"
    "Parcels_for_San_Bernardino_County/FeatureServer/0/query"
)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 120


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def truncate_apn(apn):
    """Accept 9-digit or 13-digit APN; return first 9 digits for API."""
    s = apn.strip().replace("-", "").replace(" ", "")
    if len(s) >= 9:
        return s[:9]
    if len(s) > 0:
        return s.zfill(9)
    return s


def query_parcels(where=None, out_fields="*", return_geometry=False, result_offset=0, result_record_count=1000):
    params = {
        "where": where or "1=1",
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "f": "json",
        "resultOffset": result_offset,
        "resultRecordCount": min(result_record_count, 2000),
    }
    with httpx.Client(verify=False) as client:
        r = client.get(
            PARCEL_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()


def query_by_apn(apn, return_geometry=False):
    pn = truncate_apn(apn)
    where = f"ParcelNumber = '{pn}'"
    return query_parcels(where=where, return_geometry=return_geometry)


def search_county_index(conn, apn=None, address=None):
    if apn:
        apn_norm = apn.strip().replace("-", "").replace(" ", "")
        rows = conn.execute(
            "SELECT * FROM property_addresses WHERE apn LIKE ?", (f"%{apn_norm}%",)
        ).fetchall()
        return rows
    if address:
        rows = conn.execute(
            "SELECT * FROM property_addresses WHERE street_name LIKE ?",
            (f"%{address}%",),
        ).fetchall()
        return rows
    return []


FIELDS_DISPLAY = [
    ("ParcelNumber", "APN"),
    ("OwnerName", "Owner"),
    ("LandValue", "Land Value"),
    ("ImprovementValue", "Improve Value"),
    ("PersonalPropertyValue", "Pers Prop Value"),
    ("ExemptionValue", "Exempt Value"),
    ("Acreage", "Acreage"),
    ("TaxStatus", "Tax Status"),
    ("TaxRateArea", "TRA"),
    ("Zoning", "Zoning"),
    ("ZoningDescription", "Zoning Desc"),
    ("Jurisdiction", "Jurisdiction"),
    ("BaseYear", "Base Year"),
    ("PageMap", "Page Map"),
    ("AssessDescription", "Assess Desc"),
    ("AssessClass", "Assess Class"),
]


def print_parcel(attrs):
    print(f"  --- Parcel {attrs.get('ParcelNumber', 'N/A')} ---")
    for key, label in FIELDS_DISPLAY:
        val = attrs.get(key)
        if val is not None:
            print(f"    {label}: {val}")
    print()


def main():
    parser = argparse.ArgumentParser(description="San Bernardino County Parcel API tool")
    sub = parser.add_subparsers(dest="command")

    apn_p = sub.add_parser("apn", help="Query by APN (9 or 13 digit)")
    apn_p.add_argument("apn", help="APN with or without hyphens")

    index_p = sub.add_parser("index", help="Search imported county index data")
    index_p.add_argument("--apn", help="APN to search (partial match)")
    index_p.add_argument("--address", help="Address to search (partial match)")

    import_csv_p = sub.add_parser("import-csv", help="Import county parcel CSV into property_records.db")
    import_csv_p.add_argument("file", help="Path to San_Bernardino_County_Parcel_Dataset.csv")
    import_csv_p.add_argument("--limit", type=int, default=0, help="Limit rows (default: all)")

    import_db_p = sub.add_parser("import-index-cases", help="Look up all APNs from county index against API")

    args = parser.parse_args()

    if args.command == "apn":
        try:
            data = query_by_apn(args.apn)
            features = data.get("features", [])
            print(f"Found {len(features)} parcel(s)")
            for f in features:
                print_parcel(f.get("attributes", {}))

                pn = str(f["attributes"].get("ParcelNumber", ""))
                if pn:
                    pconn = sqlite3.connect(str(PROPERTY_DB))
                    pconn.execute("PRAGMA journal_mode=WAL")
                    pconn.execute(
                        "INSERT OR IGNORE INTO property_lookup_log (apn, query_type, query_value, result_json) VALUES (?, ?, ?, ?)",
                        (pn, "api_apn", args.apn, json.dumps(f["attributes"])),
                    )
                    pconn.commit()
                    pconn.close()
        except Exception as e:
            print(f"  ERROR: {e}")
            sys.exit(1)

    elif args.command == "index":
        pconn = sqlite3.connect(str(PROPERTY_DB))
        pconn.row_factory = sqlite3.Row
        rows = search_county_index(pconn, apn=args.apn, address=args.address)
        print(f"Found {len(rows)} record(s) in county index")
        for r in rows:
            print(f"  APN: {r['apn']}")
            print(f"  Address: {r['street_number']} {r['street_name']}".strip())
            print(f"  City: {r['city']}")
            print(f"  Source: {r['source']}")
            print()
        pconn.close()

    elif args.command == "import-csv":
        fpath = Path(args.file)
        if not fpath.exists():
            print(f"ERROR: File not found: {fpath}")
            sys.exit(1)

        print(f"Importing {fpath.name} into property_records.db...")
        pconn = sqlite3.connect(str(PROPERTY_DB))
        pconn.execute("PRAGMA journal_mode=WAL")
        pconn.execute("""
            CREATE TABLE IF NOT EXISTS county_parcels (
                apn TEXT PRIMARY KEY,
                objectid INTEGER,
                land_value TEXT,
                impr_value TEXT,
                pers_value TEXT,
                exempt_value TEXT,
                hox_flag TEXT,
                acreage REAL,
                base_year TEXT,
                tax_status TEXT,
                tra TEXT,
                const_year TEXT,
                eff_year TEXT,
                latitude REAL,
                longitude REAL
            )
        """)

        imported = 0
        skipped = 0
        with open(str(fpath), newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if args.limit and i >= args.limit:
                    break
                apn = row.get("APN", "").strip()
                if not apn:
                    skipped += 1
                    continue

                apn_norm = apn.replace("-", "").replace(" ", "")
                try:
                    pconn.execute(
                        """INSERT OR IGNORE INTO county_parcels
                           (apn, objectid, land_value, impr_value, pers_value, exempt_value,
                            hox_flag, acreage, base_year, tax_status, tra,
                            const_year, eff_year, latitude, longitude)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            apn_norm,
                            int(row.get("OBJECTID", 0) or 0),
                            row.get("LAND_VALUE", ""),
                            row.get("IMPR_VALUE", ""),
                            row.get("PERS_VALUE", ""),
                            row.get("EXEM_VALUE", ""),
                            row.get("HOX_FLAG", ""),
                            float(row.get("ACREAGE", 0) or 0),
                            row.get("BASE_YEAR", ""),
                            row.get("TAX_STATUS", ""),
                            row.get("TRA", ""),
                            row.get("CONST_YEAR", ""),
                            row.get("EFF_YEAR", ""),
                            float(row.get("Latitude", 0) or 0),
                            float(row.get("Longitude", 0) or 0),
                        ),
                    )
                    imported += 1
                except Exception as e:
                    skipped += 1
                    continue

                if imported > 0 and imported % 50000 == 0:
                    pconn.commit()
                    print(f"  {imported} rows imported...")

        pconn.commit()
        print(f"Done: {imported} rows imported, {skipped} skipped")
        pconn.close()

    elif args.command == "import-index-cases":
        pconn = sqlite3.connect(str(PROPERTY_DB))
        pconn.row_factory = sqlite3.Row
        index_apns = pconn.execute(
            "SELECT DISTINCT apn FROM property_addresses WHERE apn IS NOT NULL AND apn != ''"
        ).fetchall()
        print(f"Looking up {len(index_apns)} APNs from county index against API...")

        found = 0
        not_found = 0
        for row in index_apns:
            apn = row["apn"]
            pn = truncate_apn(apn)
            try:
                data = query_by_apn(pn)
                features = data.get("features", [])
                if features:
                    pconn.execute(
                        "INSERT OR IGNORE INTO property_lookup_log (apn, query_type, query_value, result_json) VALUES (?, ?, ?, ?)",
                        (apn, "api_index_lookup", pn, json.dumps(features[0]["attributes"])),
                    )
                    found += 1
                else:
                    not_found += 1
            except Exception:
                not_found += 1
            time.sleep(0.5)

        pconn.commit()
        print(f"Found: {found}, Not found: {not_found}")
        pconn.close()

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
