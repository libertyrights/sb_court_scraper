import argparse
import asyncio
import hashlib
import html
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from urllib.parse import urljoin


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "state" / "court_calendar.db"
DEFAULT_HTML_DIR = SCRIPT_DIR / "output" / "lcn_pages"
DEFAULT_DUMP_DIR = SCRIPT_DIR / "output" / "lcn_live_pages"
SQLITE_BUSY_TIMEOUT_MS = 15000

LCN_BASE = "https://www.localcrimenews.com"
LCN_HOME = "https://www.localcrimenews.com/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

STATUTE_PREFIXES = {
    "PC", "VC", "HS", "BP", "WI", "HN", "FG", "PR", "CC", "GC", "RT"
}

NON_SUBSTANTIVE_DEGREES = {
    "ALLEGATION",
    "ENHANCEMENT",
    "INFRACTION",
    "Z-COMM",
}

# These charges are often citation/license/registration style records that
# tend to produce low-yield LCN searches relative to the request volume.
LOW_SIGNAL_VC_STATUTES = {
    "12500",
    "12500.5",
    "14601",
    "14601.1",
    "14601.2",
    "14601.3",
    "14601.5",
    "16028",
    "4000",
    "40508",
    "22350",
    "22356",
    "22107",
    "26710",
    "5200",
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_date():
    return date.today()


def db_connect(db_path):
    conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def clean_text(value):
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def mapping_get(row, key, default=""):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def normalize_name(value):
    value = clean_text(value).upper()
    value = re.sub(r"\b(JR|SR|III|II|IV)\b\.?", "", value)
    value = re.sub(r"[^A-Z0-9 ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_charge(value):
    value = clean_text(value).upper()
    value = value.replace("§", "")
    value = re.sub(r"[^A-Z0-9()/:.\- ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_any_date(value):
    value = clean_text(value)
    if not value:
        return ""

    for fmt in (
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%m-%d-%Y",
        "%m/%d/%y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass

    return ""


def days_between(date_a, date_b):
    date_a = clean_text(date_a)
    date_b = clean_text(date_b)

    if not date_a or not date_b:
        return None

    try:
        d1 = datetime.strptime(date_a, "%Y-%m-%d").date()
        d2 = datetime.strptime(date_b, "%Y-%m-%d").date()
    except ValueError:
        return None

    return abs((d1 - d2).days)


def text_hash(value):
    return hashlib.sha256((value or "").encode("utf-8", errors="ignore")).hexdigest()


def safe_filename(value):
    value = clean_text(value) or "record"
    value = re.sub(r"[^\w\-\. ]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:150] or "record"


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(conn, table_name, column_name):
    if not table_exists(conn, table_name):
        return False
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return any(r[1] == column_name for r in rows)


def columns(conn, table):
    if not table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def pick_col(cols, *names):
    for name in names:
        if name in cols:
            return name
    return None


def add_column_if_missing(conn, table, column, definition):
    if not table_exists(conn, table):
        return
    if not column_exists(conn, table, column):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def statute_tokens(text):
    text = normalize_charge(text)
    tokens = set()

    prefix_pattern = "|".join(sorted(STATUTE_PREFIXES, key=len, reverse=True))

    patterns = [
        rf"\b({prefix_pattern})\s*[-:]?\s*(\d+[A-Z0-9.]*)",
        r"\b(PENAL CODE|VEHICLE CODE|HEALTH AND SAFETY CODE|REVENUE AND TAXATION CODE)\s*(\d+[A-Z0-9.]*)",
        r"(?:^|[\s:;,\-])(\d+[A-Z0-9.]*(?:\([A-Z0-9.]+\))*)",
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, text, flags=re.I):
            if len(m.groups()) >= 2:
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
            else:
                num = clean_text(m.group(1)).upper()
                if num:
                    tokens.add(num)

    return tokens


def keyword_tokens(text):
    stop = {
        "THE", "AND", "OR", "OF", "IN", "ON", "TO", "FOR", "A", "AN",
        "WITH", "WITHOUT", "BY", "FROM", "RESULTING", "RESULT", "FELONY",
        "MISDEMEANOR", "CHARGE", "PLEA", "ALLEGATION", "COUNT", "PRIOR",
        "CONVICTIONS", "CONVICTION", "NOT", "AVAILABLE",
    }

    words = re.findall(r"[A-Z0-9]{3,}", normalize_charge(text))
    return {w for w in words if w not in stop}


def parse_cap_charge_parts(charge_text):
    raw = clean_text(charge_text)
    statute_raw = ""
    description = raw

    if ":" in raw:
        left, right = raw.split(":", 1)
        statute_raw = clean_text(left)
        description = clean_text(right)
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
        prefix = clean_text(m.group(1)).upper()
        number = clean_text(m.group(2)).upper()
        suffix = clean_text(m.group(3)).upper()
        severity = clean_text(m.group(4)).upper()

    return {
        "raw": raw,
        "statute_raw": statute_raw,
        "prefix": prefix,
        "number": number,
        "suffix": suffix,
        "severity": severity,
        "description": description,
        "tokens": sorted(statute_tokens(raw)),
        "keywords": sorted(keyword_tokens(raw)),
    }


def normalize_degree(value):
    return clean_text(value).upper()


def parse_charge_summary_lines(summary_text):
    charges = []

    for raw_line in str(summary_text or "").splitlines():
        line = clean_text(raw_line)
        if not line:
            continue

        parts = [clean_text(x) for x in line.split("|")]
        charges.append(
            {
                "charge_number": parts[0] if len(parts) >= 1 else "",
                "offense_date": parts[1] if len(parts) >= 2 else "",
                "degree": parts[2] if len(parts) >= 3 else "",
                "description": parts[3] if len(parts) >= 4 else line,
                "citation_number": parts[4] if len(parts) >= 5 else "",
            }
        )

    return charges


def charge_is_lcn_worthy(charge):
    degree = normalize_degree(charge.get("degree"))
    parsed = parse_cap_charge_parts(charge.get("description", ""))

    if degree in NON_SUBSTANTIVE_DEGREES:
        return False

    if parsed["prefix"] == "VC" and parsed["number"] in LOW_SIGNAL_VC_STATUTES:
        return False

    if degree:
        return True

    return bool(parsed["prefix"] or parsed["number"] or parsed["keywords"])


def charges_have_lcn_signal(charges):
    return any(charge_is_lcn_worthy(ch) for ch in charges)


def lookup_row_has_lcn_signal(lookup_row):
    return charges_have_lcn_signal(parse_charge_summary_lines(mapping_get(lookup_row, "charge_summary", "")))


def split_aliases(raw):
    raw = clean_text(raw)
    if not raw:
        return []

    parts = re.split(r"\s+-\s+| -|;|\|", raw)
    aliases = []

    for p in parts:
        p = clean_text(p)
        if p:
            aliases.append(p)

    return list(dict.fromkeys(aliases))


def parse_person_name(name):
    name = clean_text(name)

    if not name:
        return "", ""

    if "," in name:
        last, rest = name.split(",", 1)
        rest_parts = [x for x in clean_text(rest).split(" ") if x]
        first = rest_parts[0] if rest_parts else ""
        return clean_text(first).title(), clean_text(last).title()

    parts = [x for x in name.split(" ") if x]

    if len(parts) == 1:
        return "", parts[0].title()

    first = parts[0]
    last = parts[-1]

    return clean_text(first).title(), clean_text(last).title()


def build_search_names(defendant_name, alias_csv):
    names = []
    if defendant_name:
        names.append(clean_text(defendant_name))

    for alias in split_aliases(alias_csv):
        names.append(alias)

    seen = set()
    out = []

    for name in names:
        norm = normalize_name(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(name)

    return out


def choose_target_date(case_info, charges, arrests=None):
    valid_arrest_dates = []

    for arrest in arrests or []:
        d = arrest.get("arrest_date", "")
        if d and d != "1900-01-01":
            valid_arrest_dates.append(d)

    if valid_arrest_dates:
        return sorted(valid_arrest_dates)[-1], "arrest_date"

    valid_charge_dates = []

    for ch in charges:
        d = ch.get("offense_date", "")
        if d and d != "1900-01-01":
            valid_charge_dates.append(d)

    if valid_charge_dates:
        return sorted(valid_charge_dates)[-1], "offense_date"

    if case_info.get("file_date"):
        return case_info["file_date"], "file_date"

    return "", ""


def build_charge_summary(charges):
    parts = []

    for ch in charges:
        charge_number = clean_text(ch.get("charge_number"))
        degree = clean_text(ch.get("degree"))
        desc = clean_text(ch.get("description"))
        citation = clean_text(ch.get("citation_number"))
        offense_date = clean_text(ch.get("offense_date"))

        piece = " | ".join(x for x in [charge_number, offense_date, degree, desc, citation] if x)
        if piece:
            parts.append(piece)

    return "\n".join(parts)


def init_lcn_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lcn_lookup_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cap_case_id TEXT,
            case_number TEXT,
            party_entity_id TEXT,
            defendant_name TEXT,
            alias_csv TEXT,
            charge_summary TEXT,
            target_date TEXT,
            target_date_source TEXT,
            lcn_checked INTEGER DEFAULT 0,
            check_count INTEGER DEFAULT 0,
            max_checks INTEGER DEFAULT 3,
            status TEXT DEFAULT 'pending',
            first_checked_at TEXT,
            last_checked_at TEXT,
            next_check_after TEXT,
            last_error TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lcn_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lcn_person_id TEXT,
            profile_url TEXT,
            display_name TEXT,
            city_state TEXT,
            age_text TEXT,
            gender TEXT,
            race_text TEXT,
            first_seen_at TEXT,
            latest_seen_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lcn_arrests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lcn_person_id TEXT,
            lcn_arrest_id TEXT,
            detail_url TEXT,
            arrest_name TEXT,
            arrest_date TEXT,
            release_date TEXT,
            county_of_arrest TEXT,
            source_agency TEXT,
            arrest_location TEXT,
            arrested_for_text TEXT,
            city_state TEXT,
            age_text TEXT,
            gender TEXT,
            race_text TEXT,
            normalized_charge_text TEXT,
            detected_code_prefix TEXT,
            detected_code_number TEXT,
            detected_code_suffix TEXT,
            charge_keywords TEXT,
            bail_amount TEXT,
            scraped_at TEXT,
            source_html_hash TEXT
        );

        CREATE TABLE IF NOT EXISTS case_lcn_match_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lookup_id INTEGER,
            cap_case_id TEXT,
            case_number TEXT,
            party_entity_id TEXT,
            charge_id TEXT,
            charge_summary TEXT,
            lcn_person_id TEXT,
            lcn_arrest_id TEXT,
            lcn_arrest_row_id INTEGER,
            lcn_name TEXT,
            lcn_arrest_date TEXT,
            lcn_source_agency TEXT,
            lcn_charge_text TEXT,
            name_score INTEGER,
            alias_score INTEGER,
            date_score INTEGER,
            agency_score INTEGER,
            charge_score INTEGER,
            citation_score INTEGER,
            total_score INTEGER,
            match_confidence TEXT,
            match_basis TEXT,
            manually_confirmed INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS external_case_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cap_case_id TEXT,
            case_number TEXT,
            party_entity_id INTEGER,
            link_type TEXT,
            external_source TEXT,
            external_id TEXT,
            external_url TEXT,
            match_confidence TEXT,
            match_basis TEXT,
            manually_confirmed INTEGER DEFAULT 0,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_lcn_lookup_due
            ON lcn_lookup_status(status, next_check_after, check_count);

        CREATE INDEX IF NOT EXISTS idx_lcn_lookup_case
            ON lcn_lookup_status(cap_case_id, case_number);

        CREATE INDEX IF NOT EXISTS idx_lcn_matches_case
            ON case_lcn_match_candidates(cap_case_id, case_number);

        CREATE INDEX IF NOT EXISTS idx_lcn_matches_lookup
            ON case_lcn_match_candidates(lookup_id);

        CREATE INDEX IF NOT EXISTS idx_lcn_arrests_date_source
            ON lcn_arrests(arrest_date, source_agency);
        """
    )

    add_column_if_missing(conn, "lcn_arrests", "normalized_charge_text", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "detected_code_prefix", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "detected_code_number", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "detected_code_suffix", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "charge_keywords", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "city_state", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "age_text", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "gender", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "race_text", "TEXT")
    add_column_if_missing(conn, "lcn_people", "race_text", "TEXT")

    add_column_if_missing(conn, "case_lcn_match_candidates", "alias_score", "INTEGER")
    add_column_if_missing(conn, "case_lcn_match_candidates", "citation_score", "INTEGER")
    add_column_if_missing(conn, "case_lcn_match_candidates", "charge_id", "TEXT")

    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_lcn_lookup_one_defendant
            ON lcn_lookup_status(cap_case_id, party_entity_id, defendant_name)
            WHERE cap_case_id IS NOT NULL AND cap_case_id <> ''
              AND defendant_name IS NOT NULL AND defendant_name <> ''
            """
        )
    except sqlite3.Error as e:
        print(f"WARNING: could not create lookup unique index: {e}")

    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_lcn_arrests
            ON lcn_arrests(lcn_arrest_id, detail_url)
            WHERE (lcn_arrest_id IS NOT NULL AND lcn_arrest_id <> '')
               OR (detail_url IS NOT NULL AND detail_url <> '')
            """
        )
    except sqlite3.Error as e:
        print(f"WARNING: could not create arrest unique index: {e}")

    conn.commit()


def get_case_map(conn):
    case_tables = ["cases", "cases_probe"]
    out = {}

    for table in case_tables:
        if not table_exists(conn, table):
            continue

        cols = columns(conn, table)
        cap_col = pick_col(cols, "cap_case_id", "case_id", "id")
        num_col = pick_col(cols, "case_number", "caseNbr", "case_no")
        file_col = pick_col(cols, "file_date", "fileDate", "filing_date")
        citation_col = pick_col(cols, "citation_number", "citationNumber")
        loc_col = pick_col(cols, "court_location", "courtLocation")

        if not cap_col:
            continue

        select_cols = [cap_col]
        for c in [num_col, file_col, citation_col, loc_col]:
            if c:
                select_cols.append(c)

        sql = f'SELECT {", ".join(select_cols)} FROM "{table}"'

        for row in conn.execute(sql):
            row = dict(zip(select_cols, row))
            cap_id = clean_text(row.get(cap_col))

            if not cap_id:
                continue

            case_number_value = clean_text(row.get(num_col)) if num_col else ""

            # Criminal case number prefixes seen in this database include FVI/MVI/MSB/etc.
            # Explicitly exclude common civil/family/probate prefixes when the scraper has all categories.
            is_probably_criminal = True
            upper_case_number = case_number_value.upper()

            if upper_case_number.startswith(("CIV", "FL", "FAM", "PRO", "PR", "APP", "UD")):
                is_probably_criminal = False

            out[cap_id] = {
                "cap_case_id": cap_id,
                "case_number": case_number_value,
                "file_date": parse_any_date(row.get(file_col)) if file_col else "",
                "citation_number": clean_text(row.get(citation_col)) if citation_col else "",
                "court_location": clean_text(row.get(loc_col)) if loc_col else "",
                "is_probably_criminal": is_probably_criminal,
            }

    return out


def get_charge_map(conn):
    charge_tables = ["case_charges", "case_charges_probe"]
    out = {}

    for table in charge_tables:
        if not table_exists(conn, table):
            continue

        cols = columns(conn, table)
        cap_col = pick_col(cols, "cap_case_id", "case_id")
        charge_id_col = pick_col(cols, "charge_id", "chargeId")
        desc_col = pick_col(cols, "offense_description", "offenseDescription", "details")
        date_col = pick_col(cols, "offense_date", "offenseDate", "entry_date")
        citation_col = pick_col(cols, "citation_number", "citationNumber")
        degree_col = pick_col(cols, "degree")
        charge_num_col = pick_col(cols, "charge_number", "chargeNumber", "count_number")
        statute_prefix_col = pick_col(cols, "statute_prefix")
        statute_number_col = pick_col(cols, "statute_number")

        if not cap_col:
            continue

        select_cols = [cap_col]
        for c in [
            charge_id_col,
            desc_col,
            date_col,
            citation_col,
            degree_col,
            charge_num_col,
            statute_prefix_col,
            statute_number_col,
        ]:
            if c:
                select_cols.append(c)

        sql = f'SELECT {", ".join(select_cols)} FROM "{table}"'

        for dbrow in conn.execute(sql):
            row = dict(zip(select_cols, dbrow))
            cap_id = clean_text(row.get(cap_col))

            if not cap_id:
                continue

            desc = clean_text(row.get(desc_col)) if desc_col else ""

            if statute_prefix_col and statute_number_col:
                statute = clean_text(f"{row.get(statute_prefix_col) or ''}{row.get(statute_number_col) or ''}")
                if statute and statute not in desc:
                    desc = f"{statute}: {desc}" if desc else statute

            out.setdefault(cap_id, []).append(
                {
                    "charge_id": clean_text(row.get(charge_id_col)) if charge_id_col else "",
                    "description": desc,
                    "offense_date": parse_any_date(row.get(date_col)) if date_col else "",
                    "citation_number": clean_text(row.get(citation_col)) if citation_col else "",
                    "degree": clean_text(row.get(degree_col)) if degree_col else "",
                    "charge_number": clean_text(row.get(charge_num_col)) if charge_num_col else "",
                }
            )

    return out


def get_case_arrest_map(conn):
    arrest_tables = ["case_arrests", "case_arrests_probe"]
    out = {}

    for table in arrest_tables:
        if not table_exists(conn, table):
            continue

        cols = columns(conn, table)
        cap_col = pick_col(cols, "cap_case_id", "case_id")
        arrest_date_col = pick_col(cols, "arrest_date", "arrestDate")
        citation_col = pick_col(cols, "citation_number", "citationNumber")
        agency_col = pick_col(cols, "arresting_agency", "arrestingAgency")
        location_col = pick_col(cols, "arrest_location", "arrestLocation")

        if not cap_col:
            continue

        select_cols = [cap_col]
        for c in [arrest_date_col, citation_col, agency_col, location_col]:
            if c:
                select_cols.append(c)

        sql = f'SELECT {", ".join(select_cols)} FROM "{table}"'

        for dbrow in conn.execute(sql):
            row = dict(zip(select_cols, dbrow))
            cap_id = clean_text(row.get(cap_col))

            if not cap_id:
                continue

            out.setdefault(cap_id, []).append(
                {
                    "arrest_date": parse_any_date(row.get(arrest_date_col)) if arrest_date_col else "",
                    "citation_number": clean_text(row.get(citation_col)) if citation_col else "",
                    "arresting_agency": clean_text(row.get(agency_col)) if agency_col else "",
                    "arrest_location": clean_text(row.get(location_col)) if location_col else "",
                }
            )

    return out


def get_defendants(conn):
    party_tables = ["case_parties", "case_parties_probe"]
    defendants = []

    for table in party_tables:
        if not table_exists(conn, table):
            continue

        cols = columns(conn, table)
        cap_col = pick_col(cols, "cap_case_id", "case_id")
        party_id_col = pick_col(cols, "party_entity_id", "case_party_id", "casePartyId", "id")
        type_col = pick_col(cols, "party_type", "type")
        is_def_col = pick_col(cols, "is_defendant", "isDefendant")
        full_col = pick_col(cols, "full_name", "fullName")
        first_col = pick_col(cols, "first_name", "firstName")
        middle_col = pick_col(cols, "middle_name", "middleName")
        last_col = pick_col(cols, "last_name", "lastName")
        alias_col = pick_col(cols, "aliases_raw", "aliases")

        if not cap_col:
            continue

        select_cols = [cap_col]
        for c in [party_id_col, type_col, is_def_col, full_col, first_col, middle_col, last_col, alias_col]:
            if c:
                select_cols.append(c)

        sql = f'SELECT {", ".join(select_cols)} FROM "{table}"'

        for dbrow in conn.execute(sql):
            row = dict(zip(select_cols, dbrow))

            party_type = clean_text(row.get(type_col)) if type_col else ""
            is_def = clean_text(row.get(is_def_col)) if is_def_col else ""

            looks_defendant = (
                party_type.lower() == "defendant"
                or is_def in {"1", "true", "True", "TRUE"}
            )

            if not looks_defendant:
                continue

            full_name = clean_text(row.get(full_col)) if full_col else ""

            if not full_name:
                full_name = clean_text(
                    " ".join(
                        x
                        for x in [
                            row.get(first_col) if first_col else "",
                            row.get(middle_col) if middle_col else "",
                            row.get(last_col) if last_col else "",
                        ]
                        if clean_text(x)
                    )
                )

            if not full_name:
                continue

            aliases = split_aliases(row.get(alias_col)) if alias_col else []

            defendants.append(
                {
                    "cap_case_id": clean_text(row.get(cap_col)),
                    "party_entity_id": clean_text(row.get(party_id_col)) if party_id_col else "",
                    "defendant_name": full_name,
                    "aliases": aliases,
                }
            )

    return defendants


def queue_candidates(conn, min_age_days=0):
    init_lcn_tables(conn)

    case_map = get_case_map(conn)
    charge_map = get_charge_map(conn)
    arrest_map = get_case_arrest_map(conn)
    defendants = get_defendants(conn)

    created = 0
    updated = 0
    skipped_too_new = 0
    skipped_non_criminal = 0
    skipped_low_probability = 0

    for d in defendants:
        cap_id = d["cap_case_id"]
        case_info = case_map.get(cap_id, {})
        charges = charge_map.get(cap_id, [])
        arrests = arrest_map.get(cap_id, [])

        target_date, target_source = choose_target_date(case_info, charges, arrests=arrests)

        if target_date and min_age_days:
            try:
                d0 = datetime.strptime(target_date, "%Y-%m-%d").date()
                if d0 > today_date() - timedelta(days=min_age_days):
                    skipped_too_new += 1
                    continue
            except ValueError:
                pass

        case_number = case_info.get("case_number", "")

        # LCN is for arrest/criminal matching. Do not queue civil/family/probate/appellate defendants.
        if not case_info.get("is_probably_criminal", True):
            skipped_non_criminal += 1
            continue

        # If there are no criminal charges in case_charges, skip it.
        # This prevents civil defendants/trusts/DOES from being queued after an all-category scrape.
        if not charges:
            skipped_non_criminal += 1
            continue

        if not charges_have_lcn_signal(charges):
            skipped_low_probability += 1
            continue

        charge_summary = build_charge_summary(charges)
        alias_csv = "; ".join(d.get("aliases") or [])

        existing = conn.execute(
            """
            SELECT id
            FROM lcn_lookup_status
            WHERE cap_case_id = ?
              AND COALESCE(party_entity_id, '') = COALESCE(?, '')
              AND defendant_name = ?
            """,
            (cap_id, d["party_entity_id"], d["defendant_name"]),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE lcn_lookup_status
                SET case_number = COALESCE(NULLIF(?, ''), case_number),
                    alias_csv = ?,
                    charge_summary = ?,
                    target_date = COALESCE(NULLIF(?, ''), target_date),
                    target_date_source = COALESCE(NULLIF(?, ''), target_date_source),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    case_number,
                    alias_csv,
                    charge_summary,
                    target_date,
                    target_source,
                    now_iso(),
                    existing["id"],
                ),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO lcn_lookup_status (
                    cap_case_id,
                    case_number,
                    party_entity_id,
                    defendant_name,
                    alias_csv,
                    charge_summary,
                    target_date,
                    target_date_source,
                    lcn_checked,
                    check_count,
                    max_checks,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 3, 'pending', ?, ?)
                """,
                (
                    cap_id,
                    case_number,
                    d["party_entity_id"],
                    d["defendant_name"],
                    alias_csv,
                    charge_summary,
                    target_date,
                    target_source,
                    now_iso(),
                    now_iso(),
                ),
            )
            created += 1

    conn.commit()

    print(f"Queued new: {created}")
    print(f"Updated existing: {updated}")
    print(f"Skipped too new: {skipped_too_new}")
    print(f"Skipped non-criminal/no-charge: {skipped_non_criminal}")
    print(f"Skipped low-probability LCN lookups: {skipped_low_probability}")


def get_due_rows(conn, limit, retry_hours=48, include_low_probability=False):
    init_lcn_tables(conn)

    retry_modifier = f"-{int(retry_hours)} hours"

    sql = """
        SELECT l.*
        FROM lcn_lookup_status l
        WHERE l.status IN ('pending', 'retry_pending', 'error')
          AND l.check_count < l.max_checks
          AND (
                l.next_check_after IS NULL
                OR l.next_check_after = ''
                OR datetime(l.next_check_after) <= datetime('now')
              )
          AND (
                l.check_count = 0
                OR l.last_checked_at IS NULL
                OR l.last_checked_at = ''
                OR datetime(l.last_checked_at) <= datetime('now', ?)
              )
          AND NOT EXISTS (
                SELECT 1
                FROM case_lcn_match_candidates m
                WHERE m.cap_case_id = l.cap_case_id
                  AND COALESCE(m.party_entity_id, '') = COALESCE(l.party_entity_id, '')
                  AND COALESCE(m.match_confidence, '') IN ('weak', 'possible', 'likely', 'strong', 'manual', 'matched')
              )
          AND NOT EXISTS (
                SELECT 1
                FROM external_case_links e
                WHERE e.cap_case_id = l.cap_case_id
                  AND COALESCE(CAST(e.party_entity_id AS TEXT), '') = COALESCE(l.party_entity_id, '')
                  AND LOWER(COALESCE(e.external_source, '')) IN ('localcrimenews', 'lcn')
              )
        ORDER BY
          CASE l.status
            WHEN 'pending' THEN 0
            WHEN 'retry_pending' THEN 1
            ELSE 2
          END,
          l.target_date ASC,
          l.id ASC
    """

    out = []

    for row in conn.execute(sql, (retry_modifier,)):
        record = dict(row)

        if not include_low_probability and not lookup_row_has_lcn_signal(record):
            continue

        out.append(record)

        if limit and int(limit) > 0 and len(out) >= int(limit):
            break

    return out


def html_to_text(raw_html):
    raw_html = re.sub(r"(?is)<script.*?</script>", " ", raw_html)
    raw_html = re.sub(r"(?is)<style.*?</style>", " ", raw_html)
    raw_html = re.sub(r"(?is)<br\s*/?>", "\n", raw_html)
    raw_html = re.sub(r"(?is)</p\s*>", "\n", raw_html)
    raw_html = re.sub(r"(?is)</div\s*>", "\n", raw_html)
    raw_html = re.sub(r"(?is)</li\s*>", "\n", raw_html)
    text = re.sub(r"(?is)<.*?>", " ", raw_html)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_label(text, labels):
    next_labels = (
        "Arrest Name|Address|City, State|Age / Gender|Race|Hair / Eyes|Height / Weight|"
        "Arrested For|Arrest Date|Release Date|Bail Amount|Arrest Location|County of Arrest|"
        "Source|Previous Arrests|Purchase This Story|Subscribe"
    )

    for label in labels:
        pattern = rf"{re.escape(label)}\s*:?\s*(.+)"
        m = re.search(pattern, text, flags=re.I)

        if not m:
            continue

        value = clean_text(m.group(1))
        value = re.split(rf"\b({next_labels})\b\s*:?", value, maxsplit=1, flags=re.I)[0]
        return clean_text(value)

    return ""


def extract_lcn_ids_from_url(url):
    url = clean_text(url)
    arrest_id = ""
    person_id = ""

    m = re.search(r"/welcome/detail/(\d+)", url)
    if m:
        arrest_id = m.group(1)

    for pattern in [
        r"/person/(\d+)",
        r"/profile/(\d+)",
        r"personid=(\d+)",
        r"person_id=(\d+)",
        r"pid=(\d+)",
    ]:
        m = re.search(pattern, url, flags=re.I)
        if m:
            person_id = m.group(1)
            break

    return person_id, arrest_id


def split_age_gender(value):
    value = clean_text(value)
    if not value:
        return "", ""

    parts = [clean_text(x) for x in re.split(r"\s*/\s*", value) if clean_text(x)]
    if len(parts) >= 2:
        return parts[0], parts[1].title()

    age_match = re.search(r"\b(\d{1,3})\b", value)
    gender_match = re.search(r"\b(MALE|FEMALE|M|F)\b", value, flags=re.I)
    age_text = age_match.group(1) if age_match else ""
    gender = clean_text(gender_match.group(1)).upper() if gender_match else ""

    if gender == "M":
        gender = "Male"
    elif gender == "F":
        gender = "Female"
    elif gender:
        gender = gender.title()

    return age_text, gender


def parse_lcn_html(raw_html, source_url=""):
    text = html_to_text(raw_html)

    hrefs = re.findall(r'href=["\']([^"\']+)["\']', raw_html, flags=re.I)
    detail_urls = []

    for href in hrefs:
        if "/welcome/detail/" in href:
            detail_urls.append(urljoin(LCN_BASE, href))

    detail_urls = list(dict.fromkeys(detail_urls))

    arrest_name = extract_label(text, ["Arrest Name", "Name"])
    arrest_date = parse_any_date(extract_label(text, ["Arrest Date", "Date Arrested"]))
    release_date = parse_any_date(extract_label(text, ["Release Date"]))
    county = extract_label(text, ["County of Arrest", "County"])
    source_agency = extract_label(text, ["Source", "Arresting Agency", "Agency"])
    arrested_for = extract_label(text, ["Arrested For", "Charges", "Charge"])
    bail_amount = extract_label(text, ["Bail Amount", "Bail"])
    city_state = extract_label(text, ["City, State"])
    age_gender = extract_label(text, ["Age / Gender"])
    race_text = extract_label(text, ["Race"])
    age_text, gender = split_age_gender(age_gender)

    if not arrest_name:
        m = re.search(r"Arrest Information for\s+([A-Z][A-Za-z,\-.' ]+)", text, flags=re.I)
        if m:
            arrest_name = clean_text(m.group(1))

    if not arrest_name:
        m = re.search(r"Official Arrest Record for\s+([A-Z][A-Za-z,\-.' ]+)", text, flags=re.I)
        if m:
            arrest_name = clean_text(m.group(1))

    if not arrested_for:
        m = re.search(r"Arrested For\s*-\s*(.*?)\s+Arrest Date", text, flags=re.I | re.S)
        if m:
            arrested_for = clean_text(m.group(1))

    arrests = []

    if arrest_name or arrest_date or arrested_for or source_url:
        person_id, arrest_id = extract_lcn_ids_from_url(source_url)

        arrests.append(
            {
                "lcn_person_id": person_id,
                "lcn_arrest_id": arrest_id,
                "detail_url": source_url,
                "arrest_name": arrest_name,
                "arrest_date": arrest_date,
                "release_date": release_date,
                "county_of_arrest": county,
                "source_agency": source_agency,
                "arrest_location": extract_label(text, ["Arrest Location", "Location"]),
                "arrested_for_text": arrested_for,
                "bail_amount": bail_amount,
                "city_state": city_state,
                "age_text": age_text,
                "gender": gender,
                "race_text": race_text,
            }
        )

    elif detail_urls:
        for url in detail_urls:
            person_id, arrest_id = extract_lcn_ids_from_url(url)
            arrests.append(
                {
                    "lcn_person_id": person_id,
                    "lcn_arrest_id": arrest_id,
                    "detail_url": url,
                    "arrest_name": "",
                    "arrest_date": "",
                    "release_date": "",
                    "county_of_arrest": "",
                    "source_agency": "",
                    "arrest_location": "",
                    "arrested_for_text": "",
                    "bail_amount": "",
                    "city_state": "",
                    "age_text": "",
                    "gender": "",
                    "race_text": "",
                }
            )

    return arrests, detail_urls, text


def insert_lcn_people_if_possible(conn, arrest):
    person_id = clean_text(arrest.get("lcn_person_id"))
    profile_url = clean_text(arrest.get("profile_url"))
    display_name = clean_text(arrest.get("arrest_name"))
    city_state = clean_text(arrest.get("city_state"))
    age_text = clean_text(arrest.get("age_text"))
    gender = clean_text(arrest.get("gender"))
    race_text = clean_text(arrest.get("race_text"))

    if not person_id and not profile_url:
        return None

    conn.execute(
        """
        INSERT OR IGNORE INTO lcn_people (
            lcn_person_id,
            profile_url,
            display_name,
            city_state,
            age_text,
            gender,
            race_text,
            first_seen_at,
            latest_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            person_id,
            profile_url,
            display_name,
            city_state,
            age_text,
            gender,
            race_text,
            now_iso(),
            now_iso(),
        ),
    )

    conn.execute(
        """
        UPDATE lcn_people
        SET display_name = COALESCE(NULLIF(?, ''), display_name),
            city_state = COALESCE(NULLIF(?, ''), city_state),
            age_text = COALESCE(NULLIF(?, ''), age_text),
            gender = COALESCE(NULLIF(?, ''), gender),
            race_text = COALESCE(NULLIF(?, ''), race_text),
            latest_seen_at = ?
        WHERE COALESCE(lcn_person_id, '') = COALESCE(?, '')
          AND COALESCE(profile_url, '') = COALESCE(?, '')
        """,
        (
            display_name,
            city_state,
            age_text,
            gender,
            race_text,
            now_iso(),
            person_id,
            profile_url,
        ),
    )

    row = conn.execute(
        """
        SELECT id
        FROM lcn_people
        WHERE COALESCE(lcn_person_id, '') = COALESCE(?, '')
          AND COALESCE(profile_url, '') = COALESCE(?, '')
        ORDER BY id DESC
        LIMIT 1
        """,
        (person_id, profile_url),
    ).fetchone()

    return row["id"] if row else None


def detect_charge_parts(charge_text):
    charge_norm = normalize_charge(charge_text)
    prefix_pattern = "|".join(sorted(STATUTE_PREFIXES, key=len, reverse=True))

    detected_prefix = ""
    detected_number = ""
    detected_suffix = ""

    m = re.search(
        rf"\b({prefix_pattern})\s*[-:]?\s*(\d+[A-Z0-9.]*)([^,\s;]*)",
        charge_norm,
        flags=re.I,
    )

    if m:
        detected_prefix = clean_text(m.group(1)).upper()
        detected_number = clean_text(m.group(2)).upper()
        detected_suffix = clean_text(m.group(3)).upper()
    else:
        bare = re.search(r"(?:^|[\s:;,\-])(\d+[A-Z0-9.]*(?:\([A-Z0-9.]+\))*)", charge_norm, flags=re.I)
        if bare:
            detected_number = clean_text(bare.group(1)).upper()

    return detected_prefix, detected_number, detected_suffix


def insert_lcn_arrest(conn, arrest, html_hash):
    insert_lcn_people_if_possible(conn, arrest)

    charge_text = clean_text(arrest.get("arrested_for_text"))
    charge_norm = normalize_charge(charge_text)
    keyword_text = " ".join(sorted(keyword_tokens(charge_text)))
    detected_prefix, detected_number, detected_suffix = detect_charge_parts(charge_text)

    person_id = clean_text(arrest.get("lcn_person_id"))
    arrest_id = clean_text(arrest.get("lcn_arrest_id"))
    detail_url = clean_text(arrest.get("detail_url"))

    conn.execute(
        """
        INSERT OR IGNORE INTO lcn_arrests (
            lcn_person_id,
            lcn_arrest_id,
            detail_url,
            arrest_name,
            arrest_date,
            release_date,
            county_of_arrest,
            source_agency,
            arrest_location,
            arrested_for_text,
            city_state,
            age_text,
            gender,
            race_text,
            normalized_charge_text,
            detected_code_prefix,
            detected_code_number,
            detected_code_suffix,
            charge_keywords,
            bail_amount,
            scraped_at,
            source_html_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            person_id,
            arrest_id,
            detail_url,
            clean_text(arrest.get("arrest_name")),
            clean_text(arrest.get("arrest_date")),
            clean_text(arrest.get("release_date")),
            clean_text(arrest.get("county_of_arrest")),
            clean_text(arrest.get("source_agency")),
            clean_text(arrest.get("arrest_location")),
            charge_text,
            clean_text(arrest.get("city_state")),
            clean_text(arrest.get("age_text")),
            clean_text(arrest.get("gender")),
            clean_text(arrest.get("race_text")),
            charge_norm,
            detected_prefix,
            detected_number,
            detected_suffix,
            keyword_text,
            clean_text(arrest.get("bail_amount")),
            now_iso(),
            html_hash,
        ),
    )

    conn.execute(
        """
        UPDATE lcn_arrests
        SET arrest_name = COALESCE(NULLIF(?, ''), arrest_name),
            arrest_date = COALESCE(NULLIF(?, ''), arrest_date),
            release_date = COALESCE(NULLIF(?, ''), release_date),
            county_of_arrest = COALESCE(NULLIF(?, ''), county_of_arrest),
            source_agency = COALESCE(NULLIF(?, ''), source_agency),
            arrest_location = COALESCE(NULLIF(?, ''), arrest_location),
            arrested_for_text = COALESCE(NULLIF(?, ''), arrested_for_text),
            city_state = COALESCE(NULLIF(?, ''), city_state),
            age_text = COALESCE(NULLIF(?, ''), age_text),
            gender = COALESCE(NULLIF(?, ''), gender),
            race_text = COALESCE(NULLIF(?, ''), race_text),
            normalized_charge_text = COALESCE(NULLIF(?, ''), normalized_charge_text),
            detected_code_prefix = COALESCE(NULLIF(?, ''), detected_code_prefix),
            detected_code_number = COALESCE(NULLIF(?, ''), detected_code_number),
            detected_code_suffix = COALESCE(NULLIF(?, ''), detected_code_suffix),
            charge_keywords = COALESCE(NULLIF(?, ''), charge_keywords),
            bail_amount = COALESCE(NULLIF(?, ''), bail_amount),
            scraped_at = ?,
            source_html_hash = ?
        WHERE COALESCE(lcn_arrest_id, '') = COALESCE(?, '')
          AND COALESCE(detail_url, '') = COALESCE(?, '')
        """,
        (
            clean_text(arrest.get("arrest_name")),
            clean_text(arrest.get("arrest_date")),
            clean_text(arrest.get("release_date")),
            clean_text(arrest.get("county_of_arrest")),
            clean_text(arrest.get("source_agency")),
            clean_text(arrest.get("arrest_location")),
            charge_text,
            clean_text(arrest.get("city_state")),
            clean_text(arrest.get("age_text")),
            clean_text(arrest.get("gender")),
            clean_text(arrest.get("race_text")),
            charge_norm,
            detected_prefix,
            detected_number,
            detected_suffix,
            keyword_text,
            clean_text(arrest.get("bail_amount")),
            now_iso(),
            html_hash,
            arrest_id,
            detail_url,
        ),
    )

    row = conn.execute(
        """
        SELECT id
        FROM lcn_arrests
        WHERE COALESCE(lcn_arrest_id, '') = COALESCE(?, '')
          AND COALESCE(detail_url, '') = COALESCE(?, '')
        ORDER BY id DESC
        LIMIT 1
        """,
        (arrest_id, detail_url),
    ).fetchone()

    return row["id"] if row else None


def score_match(lookup, arrest):
    names = build_search_names(lookup.get("defendant_name", ""), lookup.get("alias_csv", ""))

    norm_lcn_name = normalize_name(arrest.get("arrest_name"))
    norm_names = [normalize_name(x) for x in names if x]

    name_score = 0
    alias_score = 0
    basis = []

    if norm_lcn_name and norm_lcn_name in norm_names:
        if norm_names and norm_lcn_name == norm_names[0]:
            name_score = 40
            basis.append("exact_defendant_name")
        else:
            alias_score = 40
            basis.append("exact_alias_name")
    elif norm_lcn_name:
        lcn_parts = set(norm_lcn_name.split())
        for idx, n in enumerate(norm_names):
            parts = set(n.split())
            overlap = parts & lcn_parts

            if len(overlap) >= 2:
                if idx == 0:
                    name_score = max(name_score, 20)
                    basis.append("partial_defendant_name")
                else:
                    alias_score = max(alias_score, 20)
                    basis.append("partial_alias_name")

    target_date = clean_text(lookup.get("target_date"))
    arrest_date = clean_text(arrest.get("arrest_date"))

    date_score = 0
    if target_date and arrest_date:
        if target_date == arrest_date:
            date_score = 25
            basis.append("date_match")
        else:
            try:
                d1 = datetime.strptime(target_date, "%Y-%m-%d").date()
                d2 = datetime.strptime(arrest_date, "%Y-%m-%d").date()
                delta = abs((d1 - d2).days)
                if delta <= 2:
                    date_score = 12
                    basis.append("date_within_2_days")
            except ValueError:
                pass

    charge_summary = clean_text(lookup.get("charge_summary"))
    lcn_charge = clean_text(arrest.get("arrested_for_text"))

    cap_statutes = statute_tokens(charge_summary)
    lcn_statutes = statute_tokens(lcn_charge)

    charge_score = 0
    if cap_statutes and lcn_statutes and (cap_statutes & lcn_statutes):
        charge_score = 25
        basis.append("statute_match")
    else:
        cap_kw = keyword_tokens(charge_summary)
        lcn_kw = keyword_tokens(lcn_charge)

        if cap_kw and lcn_kw:
            overlap = cap_kw & lcn_kw
            if len(overlap) >= 3:
                charge_score = 15
                basis.append("charge_keywords")
            elif len(overlap) >= 1:
                charge_score = 7
                basis.append("some_charge_words")

    citation_score = 0
    citation_matches = re.findall(r"\b[A-Z]{1,5}\d{2,}\b", charge_summary.upper())
    for c in citation_matches:
        if c and c in normalize_charge(lcn_charge):
            citation_score = 30
            basis.append("citation_text_match")
            break

    agency_score = 0
    source = normalize_charge(arrest.get("source_agency"))
    county = normalize_charge(arrest.get("county_of_arrest"))

    if "SAN BERNARDINO" in source and ("SHERIFF" in source or "SD" in source):
        agency_score = 10
        basis.append("sbsd_source")
    elif "SAN BERNARDINO" in source or "SAN BERNARDINO" in county:
        agency_score = 5
        basis.append("san_bernardino")

    total = name_score + alias_score + date_score + charge_score + citation_score + agency_score

    name_component = max(name_score, alias_score)
    has_supporting_signal = any([date_score, charge_score, citation_score, agency_score])

    if not name_component:
        confidence = "none"
    elif not has_supporting_signal:
        confidence = "name_only"
    elif total >= 80:
        confidence = "strong"
    elif total >= 60:
        confidence = "likely"
    elif total >= 35:
        confidence = "possible"
    else:
        confidence = "weak"

    return {
        "name_score": name_score,
        "alias_score": alias_score,
        "date_score": date_score,
        "agency_score": agency_score,
        "charge_score": charge_score,
        "citation_score": citation_score,
        "total_score": total,
        "match_confidence": confidence,
        "match_basis": ", ".join(dict.fromkeys(basis)),
    }


def should_insert_match(score):
    return score["match_confidence"] in {"possible", "likely", "strong"}


def insert_match_candidate(conn, lookup, arrest, lcn_arrest_row_id, score):
    if not should_insert_match(score):
        return False

    conn.execute(
        """
        INSERT OR IGNORE INTO case_lcn_match_candidates (
            lookup_id,
            cap_case_id,
            case_number,
            party_entity_id,
            charge_summary,
            lcn_person_id,
            lcn_arrest_id,
            lcn_arrest_row_id,
            lcn_name,
            lcn_arrest_date,
            lcn_source_agency,
            lcn_charge_text,
            name_score,
            alias_score,
            date_score,
            agency_score,
            charge_score,
            citation_score,
            total_score,
            match_confidence,
            match_basis,
            manually_confirmed,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            lookup["id"],
            lookup.get("cap_case_id"),
            lookup.get("case_number"),
            lookup.get("party_entity_id"),
            lookup.get("charge_summary"),
            arrest.get("lcn_person_id"),
            arrest.get("lcn_arrest_id"),
            lcn_arrest_row_id,
            arrest.get("arrest_name"),
            arrest.get("arrest_date"),
            arrest.get("source_agency"),
            arrest.get("arrested_for_text"),
            score["name_score"],
            score["alias_score"],
            score["date_score"],
            score["agency_score"],
            score["charge_score"],
            score["citation_score"],
            score["total_score"],
            score["match_confidence"],
            score["match_basis"],
            now_iso(),
        ),
    )

    return True


def mark_checked(conn, lookup_id, matched, retry_hours, error=""):
    row = conn.execute(
        "SELECT check_count, max_checks FROM lcn_lookup_status WHERE id=?",
        (lookup_id,),
    ).fetchone()

    if not row:
        return

    old_count = int(row["check_count"] or 0)
    max_checks = int(row["max_checks"] or 3)
    new_count = old_count + 1

    if matched:
        status = "matched"
        next_check_after = ""
    elif new_count >= max_checks:
        status = "exhausted_no_match"
        next_check_after = ""
    else:
        status = "retry_pending"
        next_check_after = (datetime.now() + timedelta(hours=retry_hours)).isoformat(timespec="seconds")

    if error:
        status = "error"
        next_check_after = (datetime.now() + timedelta(hours=retry_hours)).isoformat(timespec="seconds")

    now = now_iso()

    conn.execute(
        """
        UPDATE lcn_lookup_status
        SET lcn_checked = 1,
            check_count = ?,
            status = ?,
            first_checked_at = COALESCE(first_checked_at, ?),
            last_checked_at = ?,
            next_check_after = ?,
            last_error = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            new_count,
            status,
            now,
            now,
            next_check_after,
            error,
            now,
            lookup_id,
        ),
    )


def render_lookup_record(conn, lookup):
    cap_id = lookup.get("cap_case_id", "")
    charges = []

    if table_exists(conn, "case_charges"):
        for row in conn.execute(
            """
            SELECT charge_id, charge_number, offense_date, degree, offense_description,
                   statute_prefix, statute_number, statute_suffix, severity_code,
                   plea, plea_date, citation_number
            FROM case_charges
            WHERE cap_case_id = ?
            ORDER BY charge_number
            """,
            (cap_id,),
        ):
            d = dict(row)
            parsed = parse_cap_charge_parts(d.get("offense_description", ""))
            charges.append((d, parsed))

    search_names = build_search_names(lookup.get("defendant_name", ""), lookup.get("alias_csv", ""))

    lines = []
    lines.append("=" * 80)
    lines.append("LCN DB TEST RECORD")
    lines.append("=" * 80)
    lines.append(f"lookup_id: {lookup.get('id')}")
    lines.append(f"cap_case_id: {lookup.get('cap_case_id')}")
    lines.append(f"case_number: {lookup.get('case_number')}")
    lines.append(f"party_entity_id: {lookup.get('party_entity_id')}")
    lines.append(f"defendant_name: {lookup.get('defendant_name')}")
    lines.append(f"aliases: {lookup.get('alias_csv')}")
    lines.append(f"target_date: {lookup.get('target_date')} ({lookup.get('target_date_source')})")
    lines.append(f"status: {lookup.get('status')}")
    lines.append(f"check_count: {lookup.get('check_count')}/{lookup.get('max_checks')}")
    lines.append(f"last_checked_at: {lookup.get('last_checked_at')}")
    lines.append(f"next_check_after: {lookup.get('next_check_after')}")
    lines.append("")
    lines.append("Rendered LCN search names:")
    for name in search_names:
        first, last = parse_person_name(name)
        lines.append(f"  - raw={name!r} | first={first!r} | last={last!r}")
    lines.append("")
    lines.append("Rendered charge summary from lookup row:")
    if lookup.get("charge_summary"):
        for line in lookup.get("charge_summary", "").splitlines():
            lines.append(f"  {line}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Structured case_charges render:")
    if not charges:
        lines.append("  No rows found in case_charges for this case.")
    else:
        for d, parsed in charges:
            lines.append(f"  Charge ID: {d.get('charge_id')}")
            lines.append(f"    number: {d.get('charge_number')}")
            lines.append(f"    offense_date: {d.get('offense_date')}")
            lines.append(f"    degree: {d.get('degree')}")
            lines.append(f"    offense_description: {d.get('offense_description')}")
            lines.append(f"    db statute: {d.get('statute_prefix')}{d.get('statute_number')}{d.get('statute_suffix') or ''} severity={d.get('severity_code')}")
            lines.append(f"    parsed statute_raw: {parsed['statute_raw']}")
            lines.append(f"    parsed prefix/number/suffix/severity: {parsed['prefix']} / {parsed['number']} / {parsed['suffix']} / {parsed['severity']}")
            lines.append(f"    parsed tokens: {', '.join(parsed['tokens'])}")
            lines.append(f"    parsed keywords: {', '.join(parsed['keywords'][:20])}")
            lines.append(f"    plea: {d.get('plea')} {d.get('plea_date')}")
            lines.append(f"    citation: {d.get('citation_number')}")
            lines.append("")
    lines.append("=" * 80)

    return "\n".join(lines)


def print_due(conn, limit, retry_hours, include_low_probability=False):
    due = get_due_rows(
        conn,
        limit,
        retry_hours=retry_hours,
        include_low_probability=include_low_probability,
    )

    if not due:
        print("No due LCN lookup rows.")
        return

    for row in due:
        print(
            f"[{row['id']}] {row.get('defendant_name')} | "
            f"{row.get('case_number')} | target={row.get('target_date')} "
            f"({row.get('target_date_source')}) | checks={row.get('check_count')}/{row.get('max_checks')} | "
            f"status={row.get('status')}"
        )


def requeue_unlinked_lookups(conn, include_low_probability=False):
    init_lcn_tables(conn)
    now = now_iso()
    count = 0

    rows = conn.execute(
        """
        SELECT l.id
        FROM lcn_lookup_status l
        WHERE NOT EXISTS (
            SELECT 1
            FROM case_lcn_match_candidates m
            WHERE m.cap_case_id = l.cap_case_id
              AND COALESCE(m.party_entity_id, '') = COALESCE(l.party_entity_id, '')
              AND COALESCE(m.match_confidence, '') IN ('possible', 'likely', 'strong', 'manual', 'matched')
        )
        ORDER BY l.id
        """
    ).fetchall()

    for row in rows:
        lookup = conn.execute("SELECT * FROM lcn_lookup_status WHERE id = ?", (row["id"],)).fetchone()
        if not lookup:
            continue
        if not include_low_probability and not lookup_row_has_lcn_signal(lookup):
            continue

        conn.execute(
            """
            UPDATE lcn_lookup_status
            SET lcn_checked = 0,
                check_count = 0,
                status = 'pending',
                first_checked_at = NULL,
                last_checked_at = NULL,
                next_check_after = NULL,
                last_error = '',
                updated_at = ?
            WHERE id = ?
            """,
            (now, row["id"]),
        )
        count += 1

    conn.commit()
    return count


async def run_backfill_live(conn, args):
    if not args.no_queue_refresh:
        queue_candidates(conn, min_age_days=args.min_age_days)

    requeued = requeue_unlinked_lookups(conn, include_low_probability=args.include_low_probability)
    print(f"Requeued unlinked lookup rows: {requeued}")

    due = get_due_rows(
        conn,
        limit=args.limit,
        retry_hours=args.retry_hours,
        include_low_probability=args.include_low_probability,
    )

    if not due:
        print("No due LCN lookup rows after backfill refresh.")
        return

    print(f"Backfill rows selected: {len(due)}")
    print(f"Delay between records: {args.delay}s + jitter up to {args.delay_jitter}s")
    await run_live_authorized(conn, due, args)


async def run_worker_loop(conn, args):
    batch_size = args.worker_batch_size if args.worker_batch_size > 0 else 5
    print(
        f"Starting LCN worker loop. batch_size={batch_size}, poll={args.worker_sleep}s, "
        f"include_low_probability={bool(args.include_low_probability)}"
    )

    while True:
        if not args.no_queue_refresh:
            queue_candidates(conn, min_age_days=args.min_age_days)

        due = get_due_rows(
            conn,
            limit=batch_size,
            retry_hours=args.retry_hours,
            include_low_probability=args.include_low_probability,
        )

        if due:
            print(f"Worker picked up {len(due)} due lookup rows.")
            await run_live_authorized(conn, due, args)
            continue

        print(f"No due LCN lookup rows. Sleeping {args.worker_sleep:.1f}s...")
        await asyncio.sleep(max(1.0, args.worker_sleep))


def show_summary(conn):
    init_lcn_tables(conn)

    print("LCN lookup status counts:")

    for row in conn.execute(
        """
        SELECT status, COUNT(*)
        FROM lcn_lookup_status
        GROUP BY status
        ORDER BY COUNT(*) DESC
        """
    ):
        print(f"  {row[0]}: {row[1]}")

    total_matches = conn.execute("SELECT COUNT(*) FROM case_lcn_match_candidates").fetchone()[0]
    print(f"\nMatch candidates: {total_matches}")

    for row in conn.execute(
        """
        SELECT match_confidence, COUNT(*)
        FROM case_lcn_match_candidates
        GROUP BY match_confidence
        ORDER BY COUNT(*) DESC
        """
    ):
        print(f"  {row[0]}: {row[1]}")


def extract_detail_links_from_html(raw_html):
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', raw_html, flags=re.I)
    urls = []

    for href in hrefs:
        if "/welcome/detail/" in href:
            urls.append(urljoin(LCN_BASE, href))

    return list(dict.fromkeys(urls))


def parse_search_result_summary_text(text, href="", position=0):
    text = clean_text(text)
    reported_date = ""
    county = ""
    arrested_for = ""

    m = re.search(r"County:\s*(.*?)\s*Reported On:", text, flags=re.I)
    if m:
        county = clean_text(m.group(1))

    m = re.search(r"Reported On:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", text, flags=re.I)
    if m:
        reported_date = parse_any_date(m.group(1))

    m = re.search(r"Arrested For:\s*(.*?)(?:View Arrest Details|$)", text, flags=re.I)
    if m:
        arrested_for = clean_text(m.group(1))

    return {
        "href": clean_text(href),
        "summary_text": text,
        "reported_date": reported_date,
        "county": county,
        "arrested_for_text": arrested_for,
        "position": int(position or 0),
    }


def score_search_result_summary(lookup, result):
    basis = []
    score = 0

    target_date = clean_text(lookup.get("target_date"))
    reported_date = clean_text(result.get("reported_date"))

    delta = days_between(target_date, reported_date)
    if delta is not None:
        if delta == 0:
            score += 40
            basis.append("summary_date_match")
        elif delta <= 7:
            score += 25
            basis.append("summary_date_within_7_days")
        elif delta <= 31:
            score += 10
            basis.append("summary_date_within_31_days")

    charge_summary = clean_text(lookup.get("charge_summary"))
    summary_charge = clean_text(result.get("arrested_for_text"))

    cap_statutes = statute_tokens(charge_summary)
    lcn_statutes = statute_tokens(summary_charge)

    if cap_statutes and lcn_statutes and (cap_statutes & lcn_statutes):
        score += 25
        basis.append("summary_statute_match")
    else:
        cap_kw = keyword_tokens(charge_summary)
        lcn_kw = keyword_tokens(summary_charge)
        overlap = cap_kw & lcn_kw if cap_kw and lcn_kw else set()

        if len(overlap) >= 3:
            score += 15
            basis.append("summary_charge_keywords")
        elif len(overlap) >= 1:
            score += 7
            basis.append("summary_some_charge_words")

    county = normalize_charge(result.get("county"))
    if "SAN BERNARDINO" in county:
        score += 5
        basis.append("summary_san_bernardino")

    return {
        "summary_score": score,
        "summary_basis": ", ".join(dict.fromkeys(basis)),
    }


async def extract_search_result_summaries(page):
    try:
        rows = await page.evaluate(
            """
            () => {
              const anchors = Array.from(document.querySelectorAll('a[href*="/welcome/detail/"]'));
              const seen = new Set();
              const out = [];

              for (const anchor of anchors) {
                const href = anchor.href || "";
                if (!href || seen.has(href)) continue;
                seen.add(href);

                let node = anchor;
                let cardText = "";

                for (let depth = 0; node && depth < 10; depth += 1) {
                  const next = node.parentElement;
                  const text = ((next?.innerText) || (next?.textContent) || "").replace(/\\s+/g, " ").trim();
                  if (text.includes("Reported On:") && text.includes("Arrested For:")) {
                    cardText = text;
                    break;
                  }
                  node = next;
                }

                out.push({
                  href,
                  text: cardText,
                  position: out.length,
                });

                if (out.length >= 60) break;
              }

              return out;
            }
            """
        )
    except Exception:
        return []

    out = []
    for row in rows or []:
        parsed = parse_search_result_summary_text(
            row.get("text", ""),
            href=row.get("href", ""),
            position=row.get("position", 0),
        )
        if parsed.get("href"):
            out.append(parsed)

    return out


def choose_detail_links_for_lookup(lookup, search_results, fallback_links, max_detail_links):
    fallback_links = list(dict.fromkeys(fallback_links or []))

    if not search_results:
        return fallback_links[:max_detail_links]

    ranked = []
    for result in search_results:
        score = score_search_result_summary(lookup, result)
        ranked.append(
            {
                **result,
                **score,
            }
        )

    ranked.sort(
        key=lambda item: (
            item.get("summary_score", 0),
            -(item.get("position", 0)),
        ),
        reverse=True,
    )

    chosen = []

    if ranked and ranked[0].get("summary_score", 0) > 0:
        for item in ranked:
            href = item.get("href")
            if href and href not in chosen:
                chosen.append(href)
            if len(chosen) >= max_detail_links:
                break

    if not chosen:
        chosen = fallback_links[:max_detail_links]

    return chosen


async def fill_first_visible(page, selectors, value):
    value = clean_text(value)

    if not value:
        return False

    for selector in selectors:
        loc = page.locator(selector)

        try:
            count = await loc.count()
        except Exception:
            continue

        for i in range(count):
            item = loc.nth(i)
            try:
                if await item.is_visible() and await item.is_enabled():
                    await item.fill(value)
                    return True
            except Exception:
                pass

    return False


async def fill_lcn_search_form(page, first, last, city=""):
    first = clean_text(first)
    last = clean_text(last)
    city = clean_text(city)

    filled_first = await fill_first_visible(
        page,
        [
            "#doFirstNameHeader",
            "input[name='firstname']",
            "input[placeholder*='First' i]",
            "input[name*='first' i]",
            "input[id*='first' i]",
            "input[aria-label*='First' i]",
        ],
        first,
    )

    filled_last = await fill_first_visible(
        page,
        [
            "#doLastNameHeader",
            "input[name='lastname']",
            "input[placeholder*='Last' i]",
            "input[name*='last' i]",
            "input[id*='last' i]",
            "input[aria-label*='Last' i]",
        ],
        last,
    )

    filled_city = False
    if city:
        filled_city = await fill_first_visible(
            page,
            [
                "#doCityHeader",
                "input[name='city']",
                "input[placeholder*='City' i]",
                "input[name*='city' i]",
                "input[id*='city' i]",
                "input[aria-label*='City' i]",
            ],
            city,
        )

    if filled_first or filled_last or filled_city:
        return True

    inputs = page.locator("input:not([type='hidden']):not([type='password']):not([type='submit'])")
    visible = []

    try:
        count = await inputs.count()
    except Exception:
        count = 0

    for i in range(count):
        item = inputs.nth(i)
        try:
            if await item.is_visible() and await item.is_enabled():
                visible.append(item)
        except Exception:
            pass

    if not visible:
        return False

    values = [first, last, city]
    idx = 0

    for item in visible:
        while idx < len(values) and not values[idx]:
            idx += 1
        if idx >= len(values):
            break

        try:
            await item.fill(values[idx])
            idx += 1
        except Exception:
            pass

    return idx > 0


async def submit_lcn_search(page):
    selectors = [
        "#arrests-find",
        "input#arrests-find",
        "input[type='button'][value='Find Arrests']",
        "button:has-text('Search')",
        "input[type='submit'][value*='Search' i]",
        "input[type='button'][value*='Search' i]",
        "button[type='submit']",
    ]

    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            continue

        for i in range(count):
            item = loc.nth(i)
            try:
                if await item.is_visible() and await item.is_enabled():
                    await item.click()
                    return True
            except Exception:
                pass

    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


async def lcn_search_one_name(
    context,
    name,
    city="",
    dump_html=False,
    dump_dir=DEFAULT_DUMP_DIR,
    max_detail_links=5,
    lookup=None,
):
    page = await context.new_page()

    first, last = parse_person_name(name)

    result = {
        "name": name,
        "first": first,
        "last": last,
        "city": city,
        "search_results": [],
        "search_detail_links": [],
        "detail_arrests": [],
        "error": "",
    }

    try:
        await page.goto(LCN_HOME, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1000)

        ok = await fill_lcn_search_form(page, first, last, city=city)
        if not ok:
            result["error"] = "Could not fill LCN search form."
            return result

        submitted = await submit_lcn_search(page)
        if not submitted:
            result["error"] = "Could not submit LCN search form."
            return result

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            await page.wait_for_timeout(3000)

        raw = await page.content()

        if dump_html:
            dump_dir = Path(dump_dir)
            dump_dir.mkdir(parents=True, exist_ok=True)
            p = dump_dir / f"search_{safe_filename(name)}_{int(time.time())}.html"
            p.write_text(raw, encoding="utf-8", errors="ignore")

        detail_links = extract_detail_links_from_html(raw)
        search_results = await extract_search_result_summaries(page)
        result["search_results"] = search_results

        if lookup:
            result["search_detail_links"] = choose_detail_links_for_lookup(
                lookup,
                search_results,
                detail_links,
                max_detail_links,
            )
        else:
            result["search_detail_links"] = detail_links[:max_detail_links]

        for link in result["search_detail_links"]:
            detail_page = await context.new_page()
            try:
                await detail_page.goto(link, wait_until="domcontentloaded", timeout=45000)
                try:
                    await detail_page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    await detail_page.wait_for_timeout(1000)

                detail_html = await detail_page.content()

                if dump_html:
                    dump_dir = Path(dump_dir)
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    p = dump_dir / f"detail_{safe_filename(name)}_{safe_filename(link)}.html"
                    p.write_text(detail_html, encoding="utf-8", errors="ignore")

                arrests, _links, _text = parse_lcn_html(detail_html, source_url=link)

                for arrest in arrests:
                    if not arrest.get("detail_url"):
                        arrest["detail_url"] = link
                    result["detail_arrests"].append(arrest)

            finally:
                await detail_page.close()

    except Exception as e:
        result["error"] = str(e)

    finally:
        await page.close()

    return result


async def run_live_authorized(conn, rows, args):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: pip install playwright && playwright install chromium")

    matched_count = 0
    checked_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.show_browser)
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1400, "height": 1000})

        for idx, lookup in enumerate(rows, start=1):
            print()
            print(f"[{idx}/{len(rows)}] LCN lookup: {lookup.get('defendant_name')} | {lookup.get('case_number')} | {lookup.get('cap_case_id')}")

            names = build_search_names(lookup.get("defendant_name", ""), lookup.get("alias_csv", ""))
            if args.max_names_per_record > 0:
                names = names[: args.max_names_per_record]

            any_candidate = False
            any_error = ""

            for name in names:
                print(f"  Searching name: {name}")

                result = await lcn_search_one_name(
                    context,
                    name,
                    city=args.city,
                    dump_html=args.dump_html,
                    dump_dir=args.dump_dir,
                    max_detail_links=args.max_detail_links,
                    lookup=lookup,
                )

                if result.get("error"):
                    print(f"    Search error: {result['error']}")
                    any_error = result["error"]
                    continue

                if result.get("search_results"):
                    print(f"    Search result cards: {len(result['search_results'])}")
                print(f"    Detail links selected: {len(result['search_detail_links'])}")
                print(f"    Detail arrests parsed: {len(result['detail_arrests'])}")

                for arrest in result["detail_arrests"]:
                    h = text_hash(json.dumps(arrest, ensure_ascii=False, sort_keys=True))
                    arrest_row_id = insert_lcn_arrest(conn, arrest, h)
                    score = score_match(lookup, arrest)

                    print(
                        f"    Candidate: {arrest.get('arrest_name')} | "
                        f"{arrest.get('arrest_date')} | {arrest.get('source_agency')} | "
                        f"score={score['total_score']} confidence={score['match_confidence']} basis={score['match_basis']}"
                    )

                    inserted = insert_match_candidate(conn, lookup, arrest, arrest_row_id, score)
                    if inserted:
                        any_candidate = True

                conn.commit()

                if args.alias_delay > 0:
                    await asyncio.sleep(args.alias_delay)

            mark_checked(
                conn,
                lookup["id"],
                matched=any_candidate,
                retry_hours=args.retry_hours,
                error="" if not any_error else any_error,
            )
            conn.commit()

            checked_count += 1
            if any_candidate:
                matched_count += 1

            if idx < len(rows):
                wait_for = max(0.0, args.delay)
                jitter = random.uniform(0, args.delay_jitter) if args.delay_jitter > 0 else 0
                total_wait = wait_for + jitter
                print(f"  Waiting {total_wait:.1f}s before next record...")
                await asyncio.sleep(total_wait)

        await context.close()
        await browser.close()

    print()
    print(f"Live authorized run complete. Checked={checked_count}, matched_candidate={matched_count}")


def import_html_dir(conn, html_dir, limit, retry_hours, include_low_probability=False):
    init_lcn_tables(conn)

    html_dir = Path(html_dir)
    if not html_dir.exists():
        raise SystemExit(f"HTML dir not found: {html_dir}")

    due = get_due_rows(
        conn,
        limit=limit,
        retry_hours=retry_hours,
        include_low_probability=include_low_probability,
    )
    if not due:
        print("No due lookup rows.")
        return

    files = list(html_dir.glob("*.html")) + list(html_dir.glob("*.htm")) + list(html_dir.glob("*.txt"))

    if not files:
        print(f"No HTML/TXT files found in {html_dir}")
        return

    print(f"Due lookup rows: {len(due)}")
    print(f"HTML files: {len(files)}")

    total_arrests = 0
    total_matches = 0
    matched_lookup_ids = set()

    for file_path in files:
        raw = file_path.read_text(encoding="utf-8", errors="ignore")
        h = text_hash(raw)
        arrests, _detail_urls, _text = parse_lcn_html(raw, source_url="")

        if not arrests:
            continue

        for arrest in arrests:
            total_arrests += 1
            arrest_row_id = insert_lcn_arrest(conn, arrest, h)

            for lookup in due:
                score = score_match(lookup, arrest)
                inserted = insert_match_candidate(conn, lookup, arrest, arrest_row_id, score)

                if inserted:
                    total_matches += 1
                    matched_lookup_ids.add(lookup["id"])

    for lookup in due:
        mark_checked(
            conn,
            lookup["id"],
            matched=lookup["id"] in matched_lookup_ids,
            retry_hours=retry_hours,
        )

    conn.commit()

    print(f"LCN arrests parsed: {total_arrests}")
    print(f"Match candidates inserted/seen: {total_matches}")
    print(f"Lookup rows marked checked: {len(due)}")
    print(f"Matched rows: {len(matched_lookup_ids)}")


def run_test(conn, args):
    queue_candidates(conn, min_age_days=args.min_age_days)
    due = get_due_rows(
        conn,
        limit=1,
        retry_hours=args.retry_hours,
        include_low_probability=args.include_low_probability,
    )

    if not due:
        print("No due LCN lookup rows found for test.")
        return

    print(render_lookup_record(conn, due[0]))


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to court SQLite DB.")
    parser.add_argument(
        "--mode",
        default="due",
        choices=["queue", "due", "test", "summary", "import-html", "live-authorized", "worker", "backfill-live"],
    )

    parser.add_argument("--test", action="store_true", help="Queue DB, pull one due record, render it, and exit. No LCN web access.")
    parser.add_argument("--limit", type=int, default=0, help="Max due rows to process. 0 means all due rows.")
    parser.add_argument("--min-age-days", type=int, default=2)
    parser.add_argument("--retry-hours", type=int, default=48)
    parser.add_argument("--no-queue-refresh", action="store_true", help="Do not refresh lcn_lookup_status from case DB before due/live modes.")
    parser.add_argument(
        "--include-low-probability",
        action="store_true",
        help="Include lower-yield traffic/license style cases that are skipped by default to reduce LCN load.",
    )

    parser.add_argument("--html-dir", default=str(DEFAULT_HTML_DIR))
    parser.add_argument("--city", default="", help="Optional LCN city field. Usually leave blank unless you want a narrow search.")

    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between live LCN DB records. Default: 2.")
    parser.add_argument("--delay-jitter", type=float, default=1.5, help="Random extra seconds added to --delay. Default: 1.5.")
    parser.add_argument("--alias-delay", type=float, default=1.0, help="Seconds between alias searches within one record. Default: 1.")
    parser.add_argument("--max-names-per-record", type=int, default=3, help="Defendant + aliases to try per DB record. Default: 3. 0 means all.")
    parser.add_argument("--max-detail-links", type=int, default=5, help="Max LCN detail links to open per searched name. Default: 5.")
    parser.add_argument("--worker-batch-size", type=int, default=5, help="Worker mode: due rows per batch. Default: 5.")
    parser.add_argument("--worker-sleep", type=float, default=45.0, help="Worker mode: seconds to sleep when idle. Default: 45.")

    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--dump-html", action="store_true", help="Save live LCN search/detail HTML pages.")
    parser.add_argument("--dump-dir", default=str(DEFAULT_DUMP_DIR))

    args = parser.parse_args()

    db_path = Path(args.db).resolve()

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = db_connect(db_path)
    init_lcn_tables(conn)

    try:
        if args.test:
            run_test(conn, args)
            return

        if args.mode == "queue":
            queue_candidates(conn, min_age_days=args.min_age_days)

        elif args.mode == "due":
            if not args.no_queue_refresh:
                queue_candidates(conn, min_age_days=args.min_age_days)
            print_due(
                conn,
                limit=args.limit,
                retry_hours=args.retry_hours,
                include_low_probability=args.include_low_probability,
            )

        elif args.mode == "test":
            run_test(conn, args)

        elif args.mode == "summary":
            show_summary(conn)

        elif args.mode == "import-html":
            if not args.no_queue_refresh:
                queue_candidates(conn, min_age_days=args.min_age_days)
            import_html_dir(
                conn,
                html_dir=args.html_dir,
                limit=args.limit,
                retry_hours=args.retry_hours,
                include_low_probability=args.include_low_probability,
            )

        elif args.mode == "live-authorized":
            if not args.no_queue_refresh:
                queue_candidates(conn, min_age_days=args.min_age_days)

            due = get_due_rows(
                conn,
                limit=args.limit,
                retry_hours=args.retry_hours,
                include_low_probability=args.include_low_probability,
            )

            if not due:
                print("No due LCN lookup rows.")
                return

            print(f"Due rows selected: {len(due)}")
            print(f"Delay between records: {args.delay}s + jitter up to {args.delay_jitter}s")
            print("Live mode name is intentional: use only when you have authorization for automated access.")

            asyncio.run(run_live_authorized(conn, due, args))

        elif args.mode == "backfill-live":
            print("Backfill mode: refreshing criminal-case queue, requeuing unlinked rows, then processing due LCN lookups.")
            asyncio.run(run_backfill_live(conn, args))

        elif args.mode == "worker":
            print("Worker mode: continuously queueing criminal cases and processing due LCN lookups.")
            asyncio.run(run_worker_loop(conn, args))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
