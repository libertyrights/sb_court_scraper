import argparse
import re
import sqlite3
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "state" / "court_calendar.db"


STATUTE_PREFIXES = {
    "PC", "VC", "HS", "BP", "WI", "HN", "FG", "PR", "CC", "GC",
    "RT",  # Revenue and Taxation Code, needed for RT55363-F
}


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_name(value):
    value = clean(value).upper()
    value = re.sub(r"\b(JR|SR|III|II|IV)\b\.?", "", value)
    value = re.sub(r"[^A-Z0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_charge(value):
    value = clean(value).upper()
    value = value.replace("§", "")
    value = re.sub(r"[^A-Z0-9()/:.\- ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def statute_tokens(text):
    text = normalize_charge(text)
    tokens = set()

    prefix_pattern = "|".join(sorted(STATUTE_PREFIXES, key=len, reverse=True))

    patterns = [
        rf"\b({prefix_pattern})\s*[-:]?\s*(\d+[A-Z0-9.]*)",
        r"\b(PENAL CODE|VEHICLE CODE|HEALTH AND SAFETY CODE|REVENUE AND TAXATION CODE)\s*(\d+[A-Z0-9.]*)",
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, text):
            prefix = m.group(1).upper()
            num = m.group(2).upper()

            if prefix == "PENAL CODE":
                prefix = "PC"
            elif prefix == "VEHICLE CODE":
                prefix = "VC"
            elif prefix == "HEALTH AND SAFETY CODE":
                prefix = "HS"
            elif prefix == "REVENUE AND TAXATION CODE":
                prefix = "RT"

            tokens.add(f"{prefix}{num}")
            tokens.add(num)

    return sorted(tokens)


def keyword_tokens(text):
    stop = {
        "THE", "AND", "OR", "OF", "IN", "ON", "TO", "FOR", "A", "AN",
        "WITH", "WITHOUT", "BY", "FROM", "RESULTING", "RESULT", "FELONY",
        "MISDEMEANOR", "CHARGE", "PLEA", "ALLEGATION", "COUNT", "PRIOR",
        "CONVICTIONS", "CONVICTION", "NOT", "AVAILABLE",
    }

    words = re.findall(r"[A-Z0-9]{3,}", normalize_charge(text))
    return sorted({w for w in words if w not in stop})


def parse_cap_charge_parts(charge_text):
    raw = clean(charge_text)
    statute_raw = ""
    description = raw

    if ":" in raw:
        left, right = raw.split(":", 1)
        statute_raw = clean(left)
        description = clean(right)
    else:
        statute_raw = raw

    prefix = ""
    number = ""
    suffix = ""
    severity = ""

    prefix_pattern = "|".join(sorted(STATUTE_PREFIXES, key=len, reverse=True))

    m = re.match(
        rf"^\s*({prefix_pattern})\s*([0-9][0-9A-Z.]*)\s*([^-\s:]*)\s*(?:-\s*([A-Z]))?",
        statute_raw,
        flags=re.I,
    )

    if m:
        prefix = clean(m.group(1)).upper()
        number = clean(m.group(2)).upper()
        suffix = clean(m.group(3)).upper()
        severity = clean(m.group(4)).upper()

    return {
        "raw": raw,
        "statute_raw": statute_raw,
        "prefix": prefix,
        "number": number,
        "suffix": suffix,
        "severity": severity,
        "description": description,
        "tokens": statute_tokens(raw),
        "keywords": keyword_tokens(raw),
    }


def parse_person_name(name):
    name = clean(name)

    if not name:
        return "", ""

    if "," in name:
        last, rest = name.split(",", 1)
        rest_parts = [x for x in clean(rest).split(" ") if x]
        first = rest_parts[0] if rest_parts else ""
        return clean(first).title(), clean(last).title()

    parts = [x for x in name.split(" ") if x]

    if len(parts) == 1:
        return "", parts[0].title()

    return clean(parts[0]).title(), clean(parts[-1]).title()


def table_exists(conn, table):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def columns(conn, table):
    if not table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def require_table(conn, table):
    if not table_exists(conn, table):
        raise SystemExit(f"Missing table: {table}")


def split_aliases(raw):
    raw = clean(raw)
    if not raw:
        return []

    parts = re.split(r"\s+-\s+| -|;|\|", raw)
    return [clean(p) for p in parts if clean(p)]


def get_lookup_rows(conn, args):
    require_table(conn, "lcn_lookup_status")

    where = []
    params = []

    if args.lookup_id:
        where.append("id = ?")
        params.append(args.lookup_id)

    if args.cap_case_id:
        where.append("cap_case_id = ?")
        params.append(args.cap_case_id)

    if args.case_number:
        where.append("case_number = ?")
        params.append(args.case_number)

    if args.name:
        where.append("UPPER(defendant_name) LIKE ?")
        params.append(f"%{args.name.upper()}%")

    sql = """
        SELECT *
        FROM lcn_lookup_status
    """

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY id LIMIT ?"
    params.append(args.limit)

    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_party_rows(conn, cap_case_id):
    if not table_exists(conn, "case_parties"):
        return []

    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT *
            FROM case_parties
            WHERE cap_case_id = ?
            ORDER BY is_defendant DESC, full_name
            """,
            (cap_case_id,),
        ).fetchall()
    ]


def get_charge_rows(conn, cap_case_id):
    require_table(conn, "case_charges")

    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT
                charge_id,
                charge_number,
                offense_date,
                degree,
                offense_description,
                statute_raw,
                statute_prefix,
                statute_number,
                statute_suffix,
                severity_code,
                plea,
                plea_date,
                citation_number
            FROM case_charges
            WHERE cap_case_id = ?
            ORDER BY
                CASE
                    WHEN charge_number GLOB '[0-9]*' THEN CAST(charge_number AS INTEGER)
                    ELSE 999999
                END,
                charge_number
            """,
            (cap_case_id,),
        ).fetchall()
    ]


def build_charge_summary_from_db(charges):
    parts = []

    for ch in charges:
        piece = " | ".join(
            x for x in [
                clean(ch.get("charge_number")),
                clean(ch.get("offense_date")),
                clean(ch.get("degree")),
                clean(ch.get("offense_description")),
                clean(ch.get("citation_number")),
            ]
            if x
        )
        if piece:
            parts.append(piece)

    return "\n".join(parts)


def compare_multiline(a, b):
    a_lines = [clean(x) for x in clean(a).splitlines() if clean(x)]
    b_lines = [clean(x) for x in clean(b).splitlines() if clean(x)]
    return a_lines == b_lines, a_lines, b_lines


def verify_lookup(conn, lookup, fix_summary=False):
    cap_id = clean(lookup.get("cap_case_id"))
    lookup_id = lookup.get("id")

    print("=" * 100)
    print(f"VERIFY LOOKUP #{lookup_id}")
    print("=" * 100)
    print(f"cap_case_id: {lookup.get('cap_case_id')}")
    print(f"case_number: {lookup.get('case_number')}")
    print(f"party_entity_id: {lookup.get('party_entity_id')}")
    print(f"defendant_name: {lookup.get('defendant_name')}")
    print(f"alias_csv: {lookup.get('alias_csv')}")
    print(f"target_date: {lookup.get('target_date')} ({lookup.get('target_date_source')})")
    print(f"status: {lookup.get('status')}")
    print()

    parties = get_party_rows(conn, cap_id)
    charges = get_charge_rows(conn, cap_id)

    print("1) Defendant/name check")
    print("-" * 100)

    lookup_norm = normalize_name(lookup.get("defendant_name"))
    party_match = False

    if not parties:
        print("WARN: No case_parties rows found for this cap_case_id.")
    else:
        for p in parties:
            full = clean(p.get("full_name"))
            aliases_raw = clean(p.get("aliases_raw"))
            is_def = clean(p.get("is_defendant"))
            ptype = clean(p.get("party_type"))

            if normalize_name(full) == lookup_norm:
                party_match = True

            print(f"party: {full} | type={ptype} | is_defendant={is_def} | aliases={aliases_raw}")

    print(f"Name exists in case_parties: {'PASS' if party_match else 'WARN/NO'}")
    print()

    print("2) Rendered LCN search names")
    print("-" * 100)

    names = [clean(lookup.get("defendant_name"))]
    names.extend(split_aliases(lookup.get("alias_csv")))

    seen = set()
    rendered_names = []

    for name in names:
        norm = normalize_name(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        rendered_names.append(name)

    for name in rendered_names:
        first, last = parse_person_name(name)
        print(f"  - raw={name!r} | first={first!r} | last={last!r}")

    print()

    print("3) Charge summary check")
    print("-" * 100)

    stored_summary = clean(lookup.get("charge_summary"))
    rebuilt_summary = build_charge_summary_from_db(charges)
    same, stored_lines, rebuilt_lines = compare_multiline(stored_summary, rebuilt_summary)

    print(f"Stored lookup charge_summary lines: {len(stored_lines)}")
    print(f"Rebuilt case_charges summary lines: {len(rebuilt_lines)}")
    print(f"Stored summary matches case_charges rebuild: {'PASS' if same else 'FAIL/WARN'}")
    print()

    if not same:
        print("Stored lookup summary:")
        for line in stored_lines:
            print(f"  STORED:  {line}")

        print()
        print("Rebuilt from case_charges:")
        for line in rebuilt_lines:
            print(f"  REBUILT: {line}")

        if fix_summary:
            conn.execute(
                """
                UPDATE lcn_lookup_status
                SET charge_summary = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (rebuilt_summary, lookup_id),
            )
            conn.commit()
            print()
            print("FIXED: lcn_lookup_status.charge_summary updated from case_charges.")

    print()

    print("4) Structured case_charges verification")
    print("-" * 100)

    if not charges:
        print("FAIL/WARN: No case_charges rows found.")
    else:
        for ch in charges:
            parsed = parse_cap_charge_parts(ch.get("offense_description"))

            db_prefix = clean(ch.get("statute_prefix")).upper()
            db_number = clean(ch.get("statute_number")).upper()
            db_suffix = clean(ch.get("statute_suffix")).upper()
            db_severity = clean(ch.get("severity_code")).upper()

            parsed_prefix = parsed["prefix"]
            parsed_number = parsed["number"]
            parsed_suffix = parsed["suffix"]
            parsed_severity = parsed["severity"]

            statute_match = (
                db_prefix == parsed_prefix
                and db_number == parsed_number
                and (db_suffix == parsed_suffix or not db_suffix or not parsed_suffix)
                and (db_severity == parsed_severity or not db_severity or not parsed_severity)
            )

            token_status = "PASS" if parsed["tokens"] else "WARN/NO TOKENS"

            print(f"Charge ID: {ch.get('charge_id')}")
            print(f"  number: {ch.get('charge_number')}")
            print(f"  offense_date: {ch.get('offense_date')}")
            print(f"  degree: {ch.get('degree')}")
            print(f"  offense_description: {ch.get('offense_description')}")
            print(f"  db statute: {db_prefix}{db_number}{db_suffix} severity={db_severity}")
            print(f"  parsed statute_raw: {parsed['statute_raw']}")
            print(f"  parsed prefix/number/suffix/severity: {parsed_prefix} / {parsed_number} / {parsed_suffix} / {parsed_severity}")
            print(f"  statute parse matches DB fields: {'PASS' if statute_match else 'FAIL/WARN'}")
            print(f"  parsed tokens: {', '.join(parsed['tokens'])} [{token_status}]")
            print(f"  parsed keywords: {', '.join(parsed['keywords'][:25])}")
            print(f"  plea: {ch.get('plea')} {ch.get('plea_date')}")
            print(f"  citation: {ch.get('citation_number')}")
            print()

    print("5) Likely issues")
    print("-" * 100)

    issues = []

    if not party_match:
        issues.append("Lookup defendant_name did not exactly match a case_parties full_name.")

    if not same:
        issues.append("lcn_lookup_status.charge_summary is stale or differs from case_charges.")

    for ch in charges:
        parsed = parse_cap_charge_parts(ch.get("offense_description"))
        if not parsed["tokens"]:
            issues.append(
                f"Charge {ch.get('charge_number')} produced no statute tokens from {ch.get('offense_description')!r}."
            )

    if issues:
        for issue in issues:
            print(f"WARN: {issue}")
    else:
        print("PASS: Lookup row, parties, charge summary, and charge parsing look consistent.")

    print("=" * 100)
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--lookup-id", type=int, default=None)
    parser.add_argument("--cap-case-id", default="")
    parser.add_argument("--case-number", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--fix-summary", action="store_true", help="If charge_summary differs, update it from case_charges.")
    args = parser.parse_args()

    db_path = Path(args.db)

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = get_lookup_rows(conn, args)

    if not rows:
        print("No matching lcn_lookup_status rows found.")
        conn.close()
        return

    print(f"Found lookup rows: {len(rows)}")
    print()

    for lookup in rows:
        verify_lookup(conn, lookup, fix_summary=args.fix_summary)

    conn.close()


if __name__ == "__main__":
    main()