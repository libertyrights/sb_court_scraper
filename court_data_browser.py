import argparse
import atexit
import base64
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode
from wsgiref.simple_server import make_server

from jinja2 import Environment, FileSystemLoader, select_autoescape


SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "state" / "court_calendar.db"
PROPERTY_DB_PATH = SCRIPT_DIR / "state" / "property_records.db"
TEMPLATES_DIR = SCRIPT_DIR / "templates"
STATIC_DIR = SCRIPT_DIR / "static"
JAIL_CAPTURE_SCRIPT = SCRIPT_DIR / "manual_jail_capture.py"
BACKGROUND_JOB_RUNNER = SCRIPT_DIR / "background_job_runner.py"
LCN_SCRAPE_SCRIPT = SCRIPT_DIR / "lcn_scrape.py"
COURT_SCRAPER_SCRIPT = SCRIPT_DIR / "vv_court_criminal_calendar_watch.py"
INSTANCE_LOCK_PATH = SCRIPT_DIR / "state" / "court_data_browser.lock"
LOGS_DIR = SCRIPT_DIR / "logs"
JOB_LOG_DIR = SCRIPT_DIR / "output" / "browser_jobs"
JAIL_INMATE_LOCATOR_URL = "https://jimsnetil.shr.sbcounty.gov/bookingsearch.aspx"
LCN_HOME_URL = "https://www.localcrimenews.com/"
COURT_CALENDAR_URL = "https://cap.sb-court.org/calendar/Victorville/Victorville"
CALLLOG_BROWSER_URL = "https://upnexx.xyz/osint/sbsd.html"
CALLLOG_INDEX_PATH = Path(r"C:\Users\mark\Documents\python\calllog_arrest_index.json")

RESULTS_PER_PAGE = 50
REPORT_ROWS_PER_PAGE = 100
NEW_CASE_WINDOW_DAYS = 7
CHARGE_BOOK_ORDER = ["PC", "P", "VC", "HS", "BP", "WI", "HN", "FA", "FG", "GC", "CC", "PR", "RT"]
VALID_CASE_TABS = {"cases", "case-detail", "jail-info", "booking-numbers", "calls", "links", "report"}
DEFAULT_REPORT_FIELD_KEYS = [
    "subject_name",
    "subject_aliases",
    "subject_dob",
    "case_number",
    "case_status",
    "case_file_date",
    "case_next_hearing",
    "case_top_charge",
    "jail_latest_booking",
    "calllog_numbers",
]

jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

INSTANCE_LOCK_HANDLE = None


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_browser_compat_schema(conn)
    conn.create_function("strip_charge_suffix", 1, strip_charge_suffix)
    conn.create_function("extract_charge_book", 1, extract_charge_book)
    conn.create_function("charge_book_rank", 1, charge_book_rank)
    conn.create_function("clean_charge_description", 2, clean_charge_description)
    conn.create_function("age_group_label", 2, age_group_label)
    return conn


def release_instance_lock():
    global INSTANCE_LOCK_HANDLE
    handle = INSTANCE_LOCK_HANDLE
    if handle is None:
        return

    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass
        INSTANCE_LOCK_HANDLE = None


def acquire_instance_lock(host, port):
    global INSTANCE_LOCK_HANDLE

    INSTANCE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(INSTANCE_LOCK_PATH, "a+", encoding="utf-8")
    existing = ""

    try:
        if os.name == "nt":
            import msvcrt

            handle.write(" ")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            existing = INSTANCE_LOCK_PATH.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        owner = clean_text(existing.splitlines()[0]) if existing else ""
        try:
            handle.close()
        except OSError:
            pass
        return False, owner

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n{host}\n{port}\n{now_iso()}\n")
    handle.flush()
    INSTANCE_LOCK_HANDLE = handle
    atexit.register(release_instance_lock)
    return True, str(os.getpid())


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def safe_filename(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(value))
    return cleaned.strip("._") or "item"


def case_url(cap_case_id):
    case_id = clean_text(cap_case_id)
    return f"/case/{quote(case_id)}" if case_id else "/"


def person_url(name):
    clean_name = clean_text(name)
    return f"/person/{quote(clean_name)}" if clean_name else "/"


def property_url(apn):
    apn_clean = clean_text(apn)
    return f"/property/{quote(apn_clean)}" if apn_clean else "/"


def case_tab_url(cap_case_id, tab="", **params):
    target = case_url(cap_case_id)
    query = {}
    clean_tab = clean_text(tab)
    if clean_tab:
        query["tab"] = clean_tab
    for key, value in params.items():
        if value not in (None, "", False):
            query[key] = str(value)
    return f"{target}?{urlencode(query)}" if query else target


def clean_case_tab(value):
    tab = clean_text(value)
    return tab if tab in VALID_CASE_TABS else "case-detail"


def jail_import_url(cap_case_id):
    case_id = clean_text(cap_case_id)
    return f"/jail-import?case={quote(case_id)}" if case_id else "/jail-import"


def official_court_case_url(cap_case_id):
    case_id = clean_text(cap_case_id)
    if not case_id:
        return COURT_CALENDAR_URL
    encoded = base64.b64encode(case_id.encode("utf-8")).decode("ascii")
    return f"https://cap.sb-court.org/case/{encoded}"


def lcn_lookup_url(detail):
    preferred_lcn_url = clean_text(detail.get("preferred_lcn_url"))
    if preferred_lcn_url and preferred_lcn_url != LCN_HOME_URL:
        return preferred_lcn_url

    defendant_name = row_text(detail["overview"], "defendant_name")
    if defendant_name:
        query = f'site:localcrimenews.com "{defendant_name}"'
        return f"https://www.google.com/search?{urlencode({'q': query})}"
    return LCN_HOME_URL


def cap_credentials_configured():
    username = clean_text(os.environ.get("CAP_USERNAME", ""))
    password = os.environ.get("CAP_PASSWORD", "")
    if username and password:
        return True

    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        return False

    try:
        env_text = env_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    username_match = re.search(r"(?m)^\s*CAP_USERNAME\s*=\s*(.+?)\s*$", env_text)
    password_match = re.search(r"(?m)^\s*CAP_PASSWORD\s*=\s*(.+?)\s*$", env_text)
    return bool(
        username_match
        and clean_text(username_match.group(1))
        and password_match
        and clean_text(password_match.group(1))
    )


def allowed_log_roots():
    return [LOGS_DIR.resolve(), JOB_LOG_DIR.resolve()]


def safe_log_path(value):
    raw = clean_text(value)
    if not raw:
        return None

    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        return None

    for root in allowed_log_roots():
        if str(path).startswith(str(root)) and path.exists() and path.is_file():
            return path
    return None


def recent_log_files(limit=20):
    files = []
    for root in allowed_log_roots():
        if not root.exists():
            continue
        for path in root.rglob("*.log"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({
                "name": path.name,
                "path": str(path.resolve()),
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "size": stat.st_size,
            })

    files.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return files[:limit]


def read_log_tail(path, max_lines=160, max_chars=24000):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Could not read log: {exc}"

    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail or "(log is empty)"


def row_text(row, key):
    if row is None:
        return ""
    try:
        return clean_text(row[key])
    except Exception:
        if isinstance(row, dict):
            return clean_text(row.get(key))
    return ""


def normalize_booking_number(value):
    return re.sub(r"\D+", "", clean_text(value))


def normalize_age_for_jail(value):
    digits = re.sub(r"\D+", "", clean_text(value))
    if not digits:
        return ""
    age_value = int(digits)
    if age_value <= 0 or age_value > 120:
        return ""
    return str(age_value)


def normalize_gender_for_jail(value):
    raw = clean_text(value).lower()
    if raw in {"m", "male"}:
        return "M"
    if raw in {"f", "female"}:
        return "F"
    return ""


def gender_label_for_jail(value):
    normalized = normalize_gender_for_jail(value)
    if normalized == "M":
        return "Male"
    if normalized == "F":
        return "Female"
    return ""


def normalize_date_for_jail(value):
    raw = clean_text(value)
    if not raw:
        return ""

    candidate = raw[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(candidate, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue

    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        return f"{match.group(2)}/{match.group(3)}/{match.group(1)}"

    return raw


def split_full_name_parts(full_name):
    raw = clean_text(full_name)
    if not raw:
        return "", "", ""

    if "," in raw:
        last_name, rest = [clean_text(part) for part in raw.split(",", 1)]
        tokens = rest.split()
        first_name = tokens[0] if tokens else ""
        middle_name = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        return first_name, middle_name, last_name

    tokens = raw.split()
    if len(tokens) == 1:
        return tokens[0], "", ""

    return tokens[0], " ".join(tokens[1:-1]), tokens[-1]


def first_nonempty_from_rows(rows, *keys, transform=None):
    for row in rows:
        for key in keys:
            value = row_text(row, key)
            if transform:
                value = transform(value)
            if value:
                return value, row
    return "", None


def format_capture_source(value):
    raw = clean_text(value).upper()
    if raw == "CAP":
        return "CAP court detail"
    if raw == "LCN":
        return "LCN arrest match"
    return raw or "case context"


def build_empty_jail_prefill():
    return {
        "case_id": "",
        "case_number": "",
        "defendant_name": "",
        "booking": "",
        "last_name": "",
        "first_name": "",
        "middle_name": "",
        "dob": "",
        "age": "",
        "gender": "",
        "agency": "",
        "location": "",
        "source_notes": [],
    }


def finalize_jail_prefill(prefill):
    merged = build_empty_jail_prefill()
    merged.update(prefill or {})
    merged["booking"] = normalize_booking_number(merged.get("booking"))
    merged["first_name"] = clean_text(merged.get("first_name"))
    merged["middle_name"] = clean_text(merged.get("middle_name"))
    merged["last_name"] = clean_text(merged.get("last_name"))
    merged["dob"] = normalize_date_for_jail(merged.get("dob"))
    merged["age"] = normalize_age_for_jail(merged.get("age"))
    merged["gender"] = normalize_gender_for_jail(merged.get("gender"))
    merged["agency"] = clean_text(merged.get("agency"))
    merged["location"] = clean_text(merged.get("location"))
    merged["case_id"] = clean_text(merged.get("case_id"))
    merged["case_number"] = clean_text(merged.get("case_number"))
    merged["defendant_name"] = clean_text(merged.get("defendant_name"))

    deduped_notes = []
    seen_notes = set()
    for note in merged.get("source_notes", []):
        clean_note = clean_text(note)
        if clean_note and clean_note not in seen_notes:
            seen_notes.add(clean_note)
            deduped_notes.append(clean_note)
    merged["source_notes"] = deduped_notes

    if merged["booking"]:
        search_mode = "booking"
        search_ready = True
        missing = []
        readiness = "Booking search ready."
    elif any(merged[key] for key in ("first_name", "last_name", "middle_name", "dob", "age", "gender")):
        search_mode = "name"
        missing = []
        if not merged["last_name"]:
            missing.append("last name")
        if not merged["first_name"]:
            missing.append("first name")
        if not merged["gender"]:
            missing.append("gender")
        if not (merged["dob"] or merged["age"]):
            missing.append("DOB or age")
        search_ready = not missing
        if search_ready:
            readiness = "Name search ready. The helper can submit and wait for the CAPTCHA."
        else:
            readiness = "Manual completion needed before the helper can submit the name search."
    else:
        search_mode = "browse"
        search_ready = False
        missing = ["booking number or defendant search fields"]
        readiness = "No jail search fields are available yet."

    merged["search_mode"] = search_mode
    merged["search_ready"] = search_ready
    merged["missing_requirements"] = missing
    merged["readiness"] = readiness
    merged["gender_label"] = gender_label_for_jail(merged["gender"])
    merged["manual_finish_needed"] = search_mode == "name" and not search_ready
    return merged


def build_case_jail_prefill(detail):
    prefill = build_empty_jail_prefill()
    overview = detail["overview"]
    parties = detail.get("parties", [])
    demographics = detail.get("demographics", [])
    arrests = detail.get("arrests", [])

    defendant = next((row for row in parties if row_text(row, "is_defendant") in {"1", "true", "True"}), None)
    if defendant is None and parties:
        defendant = parties[0]

    first_name = row_text(defendant, "first_name")
    middle_name = row_text(defendant, "middle_name")
    last_name = row_text(defendant, "last_name")
    full_name = row_text(defendant, "full_name") or row_text(overview, "defendant_name")
    if not first_name or not last_name:
        parsed_first, parsed_middle, parsed_last = split_full_name_parts(full_name)
        first_name = first_name or parsed_first
        middle_name = middle_name or parsed_middle
        last_name = last_name or parsed_last

    prefill.update(
        {
            "case_id": row_text(overview, "cap_case_id"),
            "case_number": row_text(overview, "case_number"),
            "defendant_name": full_name,
            "booking": first_nonempty_from_rows(arrests, "booking_number", transform=normalize_booking_number)[0],
            "first_name": first_name,
            "middle_name": middle_name,
            "last_name": last_name,
            "agency": first_nonempty_from_rows(arrests, "arresting_agency")[0],
            "location": first_nonempty_from_rows(arrests, "arrest_location", "city_state")[0],
        }
    )

    dob, dob_row = first_nonempty_from_rows(demographics, "date_of_birth_text", "date_of_birth", transform=normalize_date_for_jail)
    if dob:
        prefill["dob"] = dob
        prefill["source_notes"].append(f"DOB from {format_capture_source(row_text(dob_row, 'source_system'))}")

    age, age_row = first_nonempty_from_rows(demographics, "age_text", transform=normalize_age_for_jail)
    if not age:
        age, age_row = first_nonempty_from_rows(arrests, "age_text", transform=normalize_age_for_jail)
    if age:
        prefill["age"] = age
        prefill["source_notes"].append(f"Age from {format_capture_source(row_text(age_row, 'source_system'))}")

    gender, gender_row = first_nonempty_from_rows(demographics, "sex", transform=normalize_gender_for_jail)
    if not gender:
        gender, gender_row = first_nonempty_from_rows(arrests, "gender", transform=normalize_gender_for_jail)
    if gender:
        prefill["gender"] = gender
        prefill["source_notes"].append(f"Gender from {format_capture_source(row_text(gender_row, 'source_system'))}")

    if prefill["agency"]:
        agency_row = first_nonempty_from_rows(arrests, "arresting_agency")[1]
        prefill["source_notes"].append(f"Agency from {format_capture_source(row_text(agency_row, 'source_system'))}")

    if prefill["location"]:
        location_row = first_nonempty_from_rows(arrests, "arrest_location", "city_state")[1]
        prefill["source_notes"].append(f"Location from {format_capture_source(row_text(location_row, 'source_system'))}")

    return finalize_jail_prefill(prefill)


def merge_case_prefill(prefill, params):
    merged = dict(prefill or build_empty_jail_prefill())
    field_map = {
        "booking": "booking",
        "last": "last_name",
        "first": "first_name",
        "middle": "middle_name",
        "dob": "dob",
        "age": "age",
        "gender": "gender",
    }
    for param_name, target_key in field_map.items():
        if param_name in params:
            merged[target_key] = clean_text(params.get(param_name, ""))
    return finalize_jail_prefill(merged)


def build_manual_jail_capture_commands(prefill):
    launch_args = [sys.executable, str(JAIL_CAPTURE_SCRIPT)]
    display_args = ["python", "manual_jail_capture.py"]

    argument_pairs = [
        ("--booking", prefill.get("booking")),
        ("--last", prefill.get("last_name")),
        ("--first", prefill.get("first_name")),
        ("--middle", prefill.get("middle_name")),
        ("--dob", prefill.get("dob")),
        ("--age", prefill.get("age")),
        ("--gender", prefill.get("gender")),
    ]
    for flag, value in argument_pairs:
        clean_value = clean_text(value)
        if clean_value:
            launch_args.extend([flag, clean_value])
            display_args.extend([flag, clean_value])

    extra_pairs = [
        ("--case-id", prefill.get("case_id")),
        ("--case-number", prefill.get("case_number")),
        ("--defendant-name", prefill.get("defendant_name")),
        ("--db", str(DB_PATH)),
    ]
    for flag, value in extra_pairs:
        clean_value = clean_text(value)
        if clean_value:
            launch_args.extend([flag, clean_value])
            display_args.extend([flag, clean_value])

    return {
        "launch": launch_args,
        "display": subprocess.list2cmdline(display_args),
    }


def build_thats_them_search_url(prefill):
    first_name = clean_text(prefill.get("first_name"))
    last_name = clean_text(prefill.get("last_name"))
    if not first_name or not last_name:
        return ""
    query = f'site:thatsthem.com "{first_name} {last_name}" California age'
    return f"https://www.bing.com/search?{urlencode({'q': query})}"


def strip_charge_suffix(value):
    raw = clean_text(value)
    if not raw:
        return ""
    return re.sub(r"-[A-Z]{1,4}$", "", raw)


def extract_charge_book(value):
    raw = strip_charge_suffix(value).upper()
    match = re.match(r"([A-Z]{1,4})", raw)
    return match.group(1) if match else ""


def charge_book_rank(value):
    book = extract_charge_book(value)
    try:
        return CHARGE_BOOK_ORDER.index(book) + 1
    except ValueError:
        return 999


def clean_charge_description(description, charge_code=""):
    desc = clean_text(description)
    code = strip_charge_suffix(charge_code)
    if not desc:
        return ""

    if code:
        upper_desc = desc.upper()
        upper_code = code.upper()
        if upper_desc.startswith(upper_code):
            desc = desc[len(code) :].lstrip(" :-")

    return clean_text(desc)


def age_group_label(age_text, dob_text=""):
    age_value = None
    raw_age = clean_text(age_text)

    if raw_age.isdigit():
        age_value = int(raw_age)
    else:
        dob = clean_text(dob_text)
        if dob:
            try:
                born = datetime.strptime(dob[:10], "%Y-%m-%d").date()
                today = date.today()
                age_value = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            except Exception:
                age_value = None

    if age_value is None or age_value < 0:
        return ""
    if age_value < 18:
        return "Under 18"
    if age_value <= 24:
        return "18-24"
    if age_value <= 34:
        return "25-34"
    if age_value <= 44:
        return "35-44"
    if age_value <= 54:
        return "45-54"
    if age_value <= 64:
        return "55-64"
    return "65+"


def bool_param(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def parse_int(value, default=1):
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def parse_request(environ):
    merged = {}
    raw_query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    merged.update({key: values[-1] for key, values in raw_query.items()})

    if environ.get("REQUEST_METHOD", "GET").upper() == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH", "0") or "0")
        except ValueError:
            length = 0
        if length > 0:
            body = environ["wsgi.input"].read(length).decode("utf-8", errors="ignore")
            raw_body = parse_qs(body, keep_blank_values=True)
            merged.update({key: values[-1] for key, values in raw_body.items()})
    return merged


def replace_query(params, **updates):
    merged = dict(params)
    for key, value in updates.items():
        if value in (None, "", False):
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    return urlencode(merged)


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(conn, table_name, column_name):
    if not table_exists(conn, table_name):
        return False
    return any(row["name"] == column_name for row in conn.execute(f'PRAGMA table_info("{table_name}")'))


def add_column_if_missing(conn, table_name, column_name, definition):
    if table_exists(conn, table_name) and not column_exists(conn, table_name, column_name):
        conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}')


def ensure_browser_compat_schema(conn):
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

        CREATE INDEX IF NOT EXISTS idx_external_case_links_case
            ON external_case_links(cap_case_id, external_source, created_at DESC);

        CREATE TABLE IF NOT EXISTS browser_saved_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            preset_name TEXT,
            field_keys_csv TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    add_column_if_missing(conn, "lcn_arrests", "city_state", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "age_text", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "gender", "TEXT")
    add_column_if_missing(conn, "lcn_arrests", "race_text", "TEXT")
    conn.commit()


def browser_job_is_active(row):
    return row is not None and clean_text(row["status"]).lower() in {"starting", "running"}


def status_tone(status):
    value = clean_text(status).lower()
    if value in {"running", "starting"}:
        return "running"
    if value == "complete":
        return "success"
    if value == "error":
        return "error"
    return "idle"


def status_label(status):
    value = clean_text(status).lower()
    if value == "starting":
        return "Starting"
    if value == "running":
        return "Running"
    if value == "complete":
        return "Complete"
    if value == "error":
        return "Error"
    return "Idle"


def get_browser_job(conn, job_key):
    if not table_exists(conn, "browser_jobs"):
        return None
    return conn.execute(
        "SELECT * FROM browser_jobs WHERE job_key = ?",
        (clean_text(job_key),),
    ).fetchone()


def set_browser_job(
    conn,
    *,
    job_key,
    job_type,
    label,
    status,
    detail="",
    target_case_id="",
    target_case_number="",
    pid=0,
    command_text="",
    log_path="",
):
    ensure_browser_compat_schema(conn)
    now = now_iso()
    existing = get_browser_job(conn, job_key)
    if existing:
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
                finished_at = CASE WHEN ? IN ('complete', 'error') THEN ? ELSE NULL END
            WHERE job_key = ?
            """,
            (
                clean_text(job_type),
                clean_text(label),
                clean_text(status),
                clean_text(detail),
                clean_text(target_case_id),
                clean_text(target_case_number),
                int(pid or 0),
                clean_text(command_text),
                clean_text(log_path),
                now,
                clean_text(status).lower(),
                now,
                clean_text(job_key),
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
                clean_text(job_key),
                clean_text(job_type),
                clean_text(label),
                clean_text(status),
                clean_text(detail),
                clean_text(target_case_id),
                clean_text(target_case_number),
                int(pid or 0),
                clean_text(command_text),
                clean_text(log_path),
                now,
                now,
                now if clean_text(status).lower() in {"complete", "error"} else None,
            ),
        )
    conn.commit()


def recent_browser_jobs(conn, *, target_case_id="", limit=5):
    if not table_exists(conn, "browser_jobs"):
        return []
    params = []
    where = ""
    if clean_text(target_case_id):
        where = "WHERE COALESCE(target_case_id, '') = ?"
        params.append(clean_text(target_case_id))
    return conn.execute(
        f"""
        SELECT *
        FROM browser_jobs
        {where}
        ORDER BY COALESCE(updated_at, started_at, '') DESC, id DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    ).fetchall()


def build_global_status(conn, *, target_case_id=""):
    jobs = recent_browser_jobs(conn, limit=6)
    case_jobs = recent_browser_jobs(conn, target_case_id=target_case_id, limit=4) if target_case_id else []

    def decorate(rows):
        items = []
        for row in rows:
            items.append(
                {
                    "job_key": row["job_key"],
                    "label": clean_text(row["label"]) or clean_text(row["job_type"]) or "Background job",
                    "status": clean_text(row["status"]).lower() or "idle",
                    "status_label": status_label(row["status"]),
                    "detail": clean_text(row["detail"]),
                    "tone": status_tone(row["status"]),
                    "updated_at": clean_text(row["updated_at"] or row["started_at"]),
                    "log_path": clean_text(row["log_path"]),
                }
            )
        return items

    active = [row for row in jobs if browser_job_is_active(row)]
    return {
        "jobs": decorate(jobs),
        "case_jobs": decorate(case_jobs),
        "has_active_jobs": bool(active),
        "auto_refresh_seconds": 4 if active else 0,
    }


def latest_case_jail_capture(conn, cap_case_id):
    if not cap_case_id or not table_exists(conn, "case_jail_captures"):
        return None
    row = conn.execute(
        """
        SELECT *
        FROM case_jail_captures
        WHERE cap_case_id = ?
        ORDER BY COALESCE(captured_at, created_at, '') DESC, id DESC
        LIMIT 1
        """,
        (clean_text(cap_case_id),),
    ).fetchone()
    return row


def case_jail_capture_history(conn, cap_case_id, limit=8):
    if not cap_case_id or not table_exists(conn, "case_jail_captures"):
        return []
    return conn.execute(
        """
        SELECT *
        FROM case_jail_captures
        WHERE cap_case_id = ?
        ORDER BY COALESCE(captured_at, created_at, '') DESC, id DESC
        LIMIT ?
        """,
        (clean_text(cap_case_id), int(limit)),
    ).fetchall()


def case_external_links(conn, cap_case_id):
    if not cap_case_id or not table_exists(conn, "external_case_links"):
        return []
    return conn.execute(
        """
        SELECT *
        FROM external_case_links
        WHERE cap_case_id = ?
        ORDER BY
            CASE LOWER(COALESCE(external_source, ''))
                WHEN 'calllog' THEN 0
                WHEN 'localcrimenews' THEN 1
                ELSE 2
            END,
            COALESCE(updated_at, created_at, '') DESC,
            id DESC
        """,
        (clean_text(cap_case_id),),
    ).fetchall()


def case_summary_rows_by_ids(conn, case_ids):
    cleaned_ids = [clean_text(item) for item in case_ids if clean_text(item)]
    if not cleaned_ids:
        return []

    placeholders = ",".join("?" for _ in cleaned_ids)
    arrests_sql = combined_arrests_sql(conn)
    return conn.execute(
        f"""
        SELECT
            c.cap_case_id,
            c.case_number,
            c.style,
            c.file_date,
            c.status,
            c.latest_seen_at,
            c.detail_scraped_at,
            c.citation_number,
            COALESCE((
                SELECT p.full_name
                FROM case_parties p
                WHERE p.cap_case_id = c.cap_case_id
                  AND COALESCE(p.full_name, '') <> ''
                ORDER BY p.is_defendant DESC, p.id ASC
                LIMIT 1
            ), c.style) AS defendant_name,
            (
                SELECT h.hearing_date
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND COALESCE(h.hearing_date, '') <> ''
                ORDER BY
                    CASE WHEN date(h.hearing_date) >= date('now') THEN 0 ELSE 1 END,
                    date(h.hearing_date) ASC,
                    COALESCE(h.hearing_time, '') ASC
                LIMIT 1
            ) AS next_hearing_date,
            (
                SELECT h.hearing_time
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND COALESCE(h.hearing_date, '') <> ''
                ORDER BY
                    CASE WHEN date(h.hearing_date) >= date('now') THEN 0 ELSE 1 END,
                    date(h.hearing_date) ASC,
                    COALESCE(h.hearing_time, '') ASC
                LIMIT 1
            ) AS next_hearing_time,
            (
                SELECT h.calendar_text
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND COALESCE(h.hearing_date, '') <> ''
                ORDER BY
                    CASE WHEN date(h.hearing_date) >= date('now') THEN 0 ELSE 1 END,
                    date(h.hearing_date) ASC,
                    COALESCE(h.hearing_time, '') ASC
                LIMIT 1
            ) AS next_hearing_calendar,
            (
                SELECT ca.courtroom_text
                FROM calendar_appearances ca
                WHERE ca.cap_case_id = c.cap_case_id
                  AND COALESCE(ca.courtroom_text, '') <> ''
                ORDER BY date(COALESCE(ca.calendar_date, ca.session_date)) DESC, COALESCE(ca.session_start_time, '') DESC
                LIMIT 1
            ) AS latest_department,
            (
                SELECT ch.statute_raw
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
                  AND COALESCE(ch.statute_raw, '') <> ''
                ORDER BY ch.id ASC
                LIMIT 1
            ) AS top_statute,
            (
                SELECT ch.offense_description
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
                  AND COALESCE(ch.offense_description, '') <> ''
                ORDER BY ch.id ASC
                LIMIT 1
            ) AS charge_preview,
            (
                SELECT COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, ''))
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, '')) <> ''
                ORDER BY CASE WHEN ar.source_system = 'CAP' THEN 0 ELSE 1 END ASC, ar.total_score DESC, COALESCE(ar.arrest_datetime, ar.arrest_date, '') DESC
                LIMIT 1
            ) AS arrest_date,
            (
                SELECT ar.arresting_agency
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND COALESCE(ar.arresting_agency, '') <> ''
                ORDER BY CASE WHEN ar.source_system = 'CAP' THEN 0 ELSE 1 END ASC, ar.total_score DESC, COALESCE(ar.arrest_datetime, ar.arrest_date, '') DESC
                LIMIT 1
            ) AS arresting_agency,
            (
                SELECT COUNT(*)
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
            ) AS charge_count,
            c.category
        FROM cases c
        WHERE c.cap_case_id IN ({placeholders})
        ORDER BY COALESCE(NULLIF(c.file_date, ''), '1900-01-01') DESC, COALESCE(NULLIF(c.latest_seen_at, ''), '') DESC
        """.format(arrests_sql=arrests_sql),
        cleaned_ids,
    ).fetchall()


def person_identity(detail):
    parties = detail.get("parties", [])
    defendant_row = None
    for row in parties:
        if row_text(row, "is_defendant") in {"1", "true", "True"}:
            defendant_row = row
            break
    if defendant_row is None and parties:
        defendant_row = parties[0]

    alias_names = []
    seen_aliases = set()
    for raw_name in [row_text(detail["overview"], "defendant_name")] + [row_text(row, "full_name") for row in detail.get("aliases", [])]:
        clean_name = clean_text(raw_name)
        if clean_name and clean_name not in seen_aliases:
            seen_aliases.add(clean_name)
            alias_names.append(clean_name)

    return {
        "party_entity_id": row_text(defendant_row, "party_entity_id") if defendant_row else "",
        "defendant_name": row_text(detail["overview"], "defendant_name"),
        "alias_names": alias_names,
    }


def related_person_cases(conn, detail, limit=30):
    identity = person_identity(detail)
    current_case_id = row_text(detail["overview"], "cap_case_id")
    party_entity_id = clean_text(identity["party_entity_id"])
    case_ids = []

    if party_entity_id and table_exists(conn, "case_parties"):
        case_ids = [
            row["cap_case_id"]
            for row in conn.execute(
                """
                SELECT DISTINCT cap_case_id
                FROM case_parties
                WHERE COALESCE(cap_case_id, '') <> ''
                  AND COALESCE(is_defendant, 0) = 1
                  AND CAST(COALESCE(party_entity_id, 0) AS TEXT) = ?
                ORDER BY cap_case_id DESC
                LIMIT ?
                """,
                (party_entity_id, int(limit)),
            ).fetchall()
        ]

    if not case_ids:
        upper_names = [clean_text(name).upper() for name in identity["alias_names"] if clean_text(name)]
        if upper_names:
            placeholders = ",".join("?" for _ in upper_names)
            case_ids = [
                row["cap_case_id"]
                for row in conn.execute(
                    f"""
                    SELECT DISTINCT cap_case_id
                    FROM case_parties
                    WHERE COALESCE(cap_case_id, '') <> ''
                      AND COALESCE(is_defendant, 0) = 1
                      AND UPPER(COALESCE(full_name, '')) IN ({placeholders})
                    ORDER BY cap_case_id DESC
                    LIMIT ?
                    """,
                    [*upper_names, int(limit)],
                ).fetchall()
            ]

    if current_case_id and current_case_id not in case_ids:
        case_ids.insert(0, current_case_id)

    summaries = case_summary_rows_by_ids(conn, case_ids[:limit])
    return summaries


def person_case_ids(detail):
    return [row_text(row, "cap_case_id") for row in detail.get("person_cases", []) if row_text(row, "cap_case_id")]


def person_external_links(conn, detail):
    if not table_exists(conn, "external_case_links"):
        return []

    case_ids = person_case_ids(detail)
    party_entity_id = clean_text(detail.get("person_identity", {}).get("party_entity_id"))
    params = []
    clauses = []
    if case_ids:
        placeholders = ",".join("?" for _ in case_ids)
        clauses.append(f"cap_case_id IN ({placeholders})")
        params.extend(case_ids)
    if party_entity_id:
        clauses.append("CAST(COALESCE(party_entity_id, 0) AS TEXT) = ?")
        params.append(party_entity_id)
    if not clauses:
        clauses.append("cap_case_id = ?")
        params.append(row_text(detail["overview"], "cap_case_id"))

    return conn.execute(
        f"""
        SELECT *
        FROM external_case_links
        WHERE {' OR '.join(clauses)}
        ORDER BY
            CASE LOWER(COALESCE(external_source, ''))
                WHEN 'calllog' THEN 0
                WHEN 'localcrimenews' THEN 1
                ELSE 2
            END,
            COALESCE(updated_at, created_at, '') DESC,
            id DESC
        """,
        params,
    ).fetchall()


def person_jail_captures(conn, detail, limit=30):
    if not table_exists(conn, "case_jail_captures"):
        return []

    case_ids = person_case_ids(detail)
    params = []
    clauses = []
    if case_ids:
        placeholders = ",".join("?" for _ in case_ids)
        clauses.append(f"cap_case_id IN ({placeholders})")
        params.extend(case_ids)

    defendant_name = clean_text(detail.get("person_identity", {}).get("defendant_name"))
    if defendant_name:
        clauses.append("UPPER(COALESCE(defendant_name, '')) = ?")
        params.append(defendant_name.upper())

    if not clauses:
        return case_jail_capture_history(conn, row_text(detail["overview"], "cap_case_id"), limit=limit)

    return conn.execute(
        f"""
        SELECT *
        FROM case_jail_captures
        WHERE {' OR '.join(clauses)}
        ORDER BY COALESCE(captured_at, created_at, '') DESC, id DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    ).fetchall()


def booking_number_rows(detail):
    rows = []
    seen = set()

    for capture in detail.get("person_jail_captures", []):
        booking_number = normalize_booking_number(row_text(capture, "booking_number"))
        if not booking_number or booking_number in seen:
            continue
        seen.add(booking_number)
        rows.append(
            {
                "booking_number": booking_number,
                "case_number": row_text(capture, "case_number"),
                "captured_at": row_text(capture, "captured_at") or row_text(capture, "created_at"),
                "inmate_name": row_text(capture, "inmate_name") or row_text(capture, "defendant_name"),
                "agency": row_text(capture, "arrest_agency"),
                "location": row_text(capture, "arrest_location"),
                "source": "jail_capture",
            }
        )

    prefill_booking = normalize_booking_number(detail.get("jail_prefill", {}).get("booking"))
    if prefill_booking and prefill_booking not in seen:
        rows.insert(
            0,
            {
                "booking_number": prefill_booking,
                "case_number": row_text(detail["overview"], "case_number"),
                "captured_at": "",
                "inmate_name": row_text(detail["overview"], "defendant_name"),
                "agency": detail.get("jail_prefill", {}).get("agency", ""),
                "location": detail.get("jail_prefill", {}).get("location", ""),
                "source": "case_prefill",
            },
        )
    return rows


def get_person_summary(conn, name):
    clean_name = clean_text(name)
    if not clean_name:
        return None
    upper_name = clean_name.upper()
    case_ids = [
        row["cap_case_id"]
        for row in conn.execute(
            """
            SELECT DISTINCT cap_case_id
            FROM case_parties
            WHERE COALESCE(cap_case_id, '') <> ''
              AND COALESCE(is_defendant, 0) = 1
              AND UPPER(COALESCE(full_name, '')) = ?
            ORDER BY cap_case_id DESC
            """,
            (upper_name,),
        ).fetchall()
    ]
    if not case_ids:
        case_ids = [
            row["cap_case_id"]
            for row in conn.execute(
                """
                SELECT DISTINCT cap_case_id
                FROM cases
                WHERE COALESCE(cap_case_id, '') <> ''
                  AND UPPER(COALESCE(style, '')) = ?
                ORDER BY cap_case_id DESC
                """,
                (upper_name,),
            ).fetchall()
        ]
    if not case_ids:
        return None

    case_summaries = case_summary_rows_by_ids(conn, case_ids)

    status_breakdown = {}
    category_breakdown = {}
    total_charges = 0
    top_charges = {}
    top_departments = {}
    next_hearing = None

    for row in case_summaries:
        status = clean_text(row["status"])
        if status:
            status_breakdown[status] = status_breakdown.get(status, 0) + 1
        category = clean_text(row.get("category", ""))
        if category:
            category_breakdown[category] = category_breakdown.get(category, 0) + 1
        charge = clean_text(row.get("top_statute", ""))
        if charge:
            top_charges[charge] = top_charges.get(charge, 0) + 1
        dept = clean_text(row.get("latest_department", ""))
        if dept:
            top_departments[dept] = top_departments.get(dept, 0) + 1

        h_date = clean_text(row.get("next_hearing_date", ""))
        h_time = clean_text(row.get("next_hearing_time", ""))
        h_text = clean_text(row.get("next_hearing_calendar", ""))
        if h_date and (next_hearing is None or h_date < next_hearing["date"]):
            next_hearing = {"date": h_date, "time": h_time, "calendar": h_text, "case_number": row["case_number"]}

    jail_captures = []
    booking_numbers = []
    external_links = []
    if table_exists(conn, "case_jail_captures"):
        placeholders = ",".join("?" for _ in case_ids)
        jail_captures = conn.execute(
            f"""
            SELECT *
            FROM case_jail_captures
            WHERE cap_case_id IN ({placeholders})
            ORDER BY COALESCE(captured_at, created_at, '') DESC, id DESC
            LIMIT 50
            """,
            case_ids,
        ).fetchall()
        seen_bn = set()
        for cap in jail_captures:
            bn = normalize_booking_number(row_text(cap, "booking_number"))
            if bn and bn not in seen_bn:
                seen_bn.add(bn)
                booking_numbers.append(bn)

    if table_exists(conn, "external_case_links"):
        placeholders = ",".join("?" for _ in case_ids)
        external_links = conn.execute(
            f"""
            SELECT *
            FROM external_case_links
            WHERE cap_case_id IN ({placeholders})
            ORDER BY
                CASE LOWER(COALESCE(external_source, ''))
                    WHEN 'calllog' THEN 0
                    WHEN 'localcrimenews' THEN 1
                    ELSE 2
                END,
                COALESCE(updated_at, created_at, '') DESC
            LIMIT 100
            """,
            case_ids,
        ).fetchall()

    return {
        "name": clean_name,
        "case_count": len(case_summaries),
        "case_summaries": case_summaries,
        "status_breakdown": status_breakdown,
        "category_breakdown": category_breakdown,
        "total_charges": total_charges,
        "top_charges": dict(sorted(top_charges.items(), key=lambda x: -x[1])[:10]),
        "top_departments": dict(sorted(top_departments.items(), key=lambda x: -x[1])[:10]),
        "next_hearing": next_hearing,
        "jail_captures": jail_captures,
        "booking_numbers": booking_numbers,
        "external_links": external_links,
        "party_entity_id": None,
    }


def get_property_data(conn, apn):
    apn_clean = clean_text(apn)
    if not apn_clean:
        return None
    if not PROPERTY_DB_PATH.exists():
        return None
    pconn = sqlite3.connect(str(PROPERTY_DB_PATH))
    pconn.row_factory = sqlite3.Row
    try:
        property_row = pconn.execute(
            "SELECT * FROM property_addresses WHERE apn = ?",
            (apn_clean,),
        ).fetchone()
        if not property_row:
            return None
        prop = {k: property_row[k] for k in property_row.keys()}

        linked_cases = conn.execute(
            """
            SELECT cpl.*, c.cap_case_id, c.status, c.category
            FROM case_property_links cpl
            LEFT JOIN cases c ON c.case_number = cpl.case_number
            WHERE cpl.apn = ?
            ORDER BY cpl.created_at DESC
            """,
            (apn_clean,),
        ).fetchall()
        prop["linked_cases"] = linked_cases
        return prop
    finally:
        pconn.close()


def calllog_link_rows(detail):
    return [row for row in detail.get("person_links", []) if clean_text(row["external_source"]).lower() == "calllog"]


def build_report_field_catalog(detail):
    latest_jail = detail.get("latest_jail_capture") or {}
    aliases = [row_text(row, "full_name") for row in detail.get("aliases", []) if row_text(row, "full_name")]
    demographics = detail.get("demographics", [])
    primary_demo = demographics[0] if demographics else {}
    person_cases = detail.get("person_cases", [])
    booking_numbers = [row.get("booking_number", "") for row in detail.get("booking_numbers", []) if row.get("booking_number")]
    calllog_numbers = [row_text(row, "external_id") for row in detail.get("calllog_links", []) if row_text(row, "external_id")]

    values = [
        {"key": "subject_name", "section": "Subject", "label": "Subject Name", "value": row_text(detail["overview"], "defendant_name")},
        {"key": "subject_party_id", "section": "Subject", "label": "Party Entity ID", "value": detail.get("person_identity", {}).get("party_entity_id", "")},
        {"key": "subject_aliases", "section": "Subject", "label": "Aliases", "value": ", ".join(aliases)},
        {"key": "subject_dob", "section": "Subject", "label": "DOB", "value": row_text(primary_demo, "date_of_birth_text") or row_text(latest_jail, "dob")},
        {"key": "subject_age", "section": "Subject", "label": "Age", "value": row_text(primary_demo, "age_text") or row_text(latest_jail, "age")},
        {"key": "subject_sex", "section": "Subject", "label": "Sex", "value": row_text(primary_demo, "sex") or row_text(latest_jail, "sex")},
        {"key": "subject_race", "section": "Subject", "label": "Race", "value": row_text(primary_demo, "race")},
        {"key": "case_number", "section": "Case", "label": "Current Case Number", "value": row_text(detail["overview"], "case_number")},
        {"key": "case_status", "section": "Case", "label": "Case Status", "value": row_text(detail["overview"], "status")},
        {"key": "case_file_date", "section": "Case", "label": "Filed Date", "value": row_text(detail["overview"], "file_date")},
        {"key": "case_next_hearing", "section": "Case", "label": "Next Hearing", "value": row_text(detail["overview"], "next_hearing")},
        {"key": "case_citation", "section": "Case", "label": "Citation Number", "value": row_text(detail["overview"], "citation_number")},
        {"key": "case_top_charge", "section": "Case", "label": "Top Charge", "value": clean_charge_description(row_text(detail["charges"][0], "offense_description"), row_text(detail["charges"][0], "statute_raw")) if detail.get("charges") else ""},
        {"key": "case_charge_count", "section": "Case", "label": "Charge Count", "value": str(len(detail.get("charges", [])))},
        {"key": "case_related_count", "section": "Case", "label": "Related Case Count", "value": str(len(person_cases))},
        {"key": "jail_latest_booking", "section": "Jail", "label": "Latest Booking Number", "value": row_text(latest_jail, "booking_number") or detail.get("jail_prefill", {}).get("booking", "")},
        {"key": "jail_latest_housing", "section": "Jail", "label": "Housing Facility", "value": row_text(latest_jail, "housing_facility")},
        {"key": "jail_latest_agency", "section": "Jail", "label": "Arrest Agency", "value": row_text(latest_jail, "arrest_agency") or detail.get("jail_prefill", {}).get("agency", "")},
        {"key": "jail_latest_location", "section": "Jail", "label": "Arrest Location", "value": row_text(latest_jail, "arrest_location") or detail.get("jail_prefill", {}).get("location", "")},
        {"key": "booking_numbers", "section": "Jail", "label": "All Booking Numbers", "value": ", ".join(booking_numbers)},
        {"key": "calllog_numbers", "section": "Links", "label": "Linked Call Numbers", "value": ", ".join(calllog_numbers)},
        {"key": "lcn_url", "section": "Links", "label": "Preferred LCN URL", "value": detail.get("preferred_lcn_url", ""), "url": detail.get("preferred_lcn_url", "")},
        {"key": "court_url", "section": "Links", "label": "Court URL", "value": detail.get("open_court_url", ""), "url": detail.get("open_court_url", "")},
        {"key": "jail_url", "section": "Links", "label": "Jail Workflow URL", "value": detail.get("open_jail_url", ""), "url": detail.get("open_jail_url", "")},
    ]

    for row in values:
        row["value"] = clean_text(row.get("value", ""))
    return values


def selected_report_field_keys(params, preset_row=None):
    explicit_keys = sorted(
        key[len("field_") :]
        for key, value in params.items()
        if key.startswith("field_") and bool_param(value)
    )
    if explicit_keys:
        return explicit_keys
    if preset_row and row_text(preset_row, "field_keys_csv"):
        return [clean_text(item) for item in row_text(preset_row, "field_keys_csv").split(",") if clean_text(item)]
    return list(DEFAULT_REPORT_FIELD_KEYS)


def load_report_presets(conn):
    if not table_exists(conn, "browser_saved_reports"):
        return []
    return conn.execute(
        """
        SELECT *
        FROM browser_saved_reports
        ORDER BY COALESCE(updated_at, created_at, '') DESC, id DESC
        """
    ).fetchall()


def get_report_preset(conn, preset_id):
    preset_id = parse_int(preset_id, default=0)
    if preset_id <= 0 or not table_exists(conn, "browser_saved_reports"):
        return None
    return conn.execute(
        "SELECT * FROM browser_saved_reports WHERE id = ?",
        (preset_id,),
    ).fetchone()


def save_report_preset(conn, preset_name, field_keys):
    ensure_browser_compat_schema(conn)
    now = now_iso()
    clean_name = clean_text(preset_name) or "Saved Report"
    field_keys_csv = ",".join(clean_text(item) for item in field_keys if clean_text(item))

    existing = conn.execute(
        "SELECT * FROM browser_saved_reports WHERE LOWER(COALESCE(preset_name, '')) = LOWER(?) ORDER BY id DESC LIMIT 1",
        (clean_name,),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE browser_saved_reports
            SET field_keys_csv = ?, updated_at = ?
            WHERE id = ?
            """,
            (field_keys_csv, now, existing["id"]),
        )
        preset_id = existing["id"]
    else:
        conn.execute(
            """
            INSERT INTO browser_saved_reports (preset_name, field_keys_csv, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (clean_name, field_keys_csv, now, now),
        )
        preset_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return int(preset_id)
def upsert_external_case_link(
    conn,
    *,
    cap_case_id,
    case_number,
    party_entity_id,
    link_type,
    external_source,
    external_id,
    external_url,
    match_confidence,
    match_basis,
    manually_confirmed,
    notes="",
):
    ensure_browser_compat_schema(conn)
    now = now_iso()
    conn.execute(
        """
        DELETE FROM external_case_links
        WHERE cap_case_id = ?
          AND LOWER(COALESCE(external_source, '')) = LOWER(?)
        """,
        (clean_text(cap_case_id), clean_text(external_source)),
    )
    conn.execute(
        """
        INSERT INTO external_case_links (
            cap_case_id,
            case_number,
            party_entity_id,
            link_type,
            external_source,
            external_id,
            external_url,
            match_confidence,
            match_basis,
            manually_confirmed,
            notes,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_text(cap_case_id),
            clean_text(case_number),
            int(party_entity_id or 0) if str(party_entity_id or "").strip() else None,
            clean_text(link_type),
            clean_text(external_source),
            clean_text(external_id),
            clean_text(external_url),
            clean_text(match_confidence),
            clean_text(match_basis),
            1 if manually_confirmed else 0,
            clean_text(notes),
            now,
            now,
        ),
    )
    conn.commit()


def normalize_match_name(value):
    return re.sub(r"[^A-Z0-9]+", " ", clean_text(value).upper()).strip()


def first_defendant_party_id(detail):
    for row in detail.get("parties", []):
        if row_text(row, "is_defendant") in {"1", "true", "True"}:
            return row_text(row, "party_entity_id")
    return row_text(detail["parties"][0], "party_entity_id") if detail.get("parties") else ""


def best_lcn_link_candidate(conn, cap_case_id):
    if not cap_case_id or not table_exists(conn, "case_lcn_match_candidates") or not table_exists(conn, "lcn_arrests"):
        return None
    return conn.execute(
        f"""
        SELECT
            m.*,
            COALESCE(a.detail_url, '') AS detail_url,
            COALESCE(a.lcn_person_id, '') AS detail_person_id,
            COALESCE(a.lcn_arrest_id, m.lcn_arrest_id, '') AS detail_arrest_id
        FROM case_lcn_match_candidates m
        LEFT JOIN lcn_arrests a ON a.id = m.lcn_arrest_row_id
        WHERE m.cap_case_id = ?
          AND {safe_lcn_match_predicate('m')}
        ORDER BY {lcn_match_confidence_rank_sql('m')} DESC, COALESCE(m.total_score, 0) DESC, m.id DESC
        LIMIT 1
        """,
        (clean_text(cap_case_id),),
    ).fetchone()


def auto_link_lcn_case(conn, detail):
    overview = detail["overview"]
    candidate = best_lcn_link_candidate(conn, row_text(overview, "cap_case_id"))
    if not candidate:
        return None
    external_id = row_text(candidate, "detail_arrest_id") or row_text(candidate, "lcn_arrest_id") or row_text(candidate, "detail_person_id")
    upsert_external_case_link(
        conn,
        cap_case_id=row_text(overview, "cap_case_id"),
        case_number=row_text(overview, "case_number"),
        party_entity_id=first_defendant_party_id(detail),
        link_type="arrest",
        external_source="localcrimenews",
        external_id=external_id,
        external_url=row_text(candidate, "detail_url"),
        match_confidence=row_text(candidate, "match_confidence") or "possible",
        match_basis=row_text(candidate, "match_basis") or "LCN candidate match",
        manually_confirmed=False,
        notes=f"score={row_text(candidate, 'total_score')}",
    )
    return candidate


_CALLLOG_INDEX_CACHE = None


def load_calllog_index():
    global _CALLLOG_INDEX_CACHE
    if _CALLLOG_INDEX_CACHE is not None:
        return _CALLLOG_INDEX_CACHE
    try:
        payload = json.loads(CALLLOG_INDEX_PATH.read_text(encoding="utf-8"))
        _CALLLOG_INDEX_CACHE = payload.get("calls", {}) if isinstance(payload, dict) else {}
    except Exception:
        _CALLLOG_INDEX_CACHE = {}
    return _CALLLOG_INDEX_CACHE


def extract_case_names(detail):
    names = []
    for row in detail.get("parties", []):
        full_name = row_text(row, "full_name")
        if full_name and row_text(row, "is_defendant") in {"1", "true", "True"}:
            names.append(full_name)
    for row in detail.get("aliases", []):
        if row_text(row, "full_name"):
            names.append(row_text(row, "full_name"))
    if not names and row_text(detail["overview"], "defendant_name"):
        names.append(row_text(detail["overview"], "defendant_name"))
    seen = set()
    out = []
    for name in names:
        norm = normalize_match_name(name)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def extract_case_dates(detail):
    values = set()
    for row in detail.get("arrests", []):
        for key in ("arrest_date", "arrest_datetime"):
            raw = row_text(row, key)
            if not raw:
                continue
            match = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
            if match:
                values.add(match.group(1))
    for row in detail.get("charges", []):
        if row_text(row, "offense_date"):
            values.add(row_text(row, "offense_date"))
    return values


def extract_case_statute_tokens(detail):
    tokens = set()
    for row in detail.get("charges", []):
        raw = strip_charge_suffix(row_text(row, "statute_raw")).upper()
        if raw:
            tokens.add(raw.replace(" ", ""))
            number_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw)
            if number_match:
                tokens.add(number_match.group(1))
    return tokens


def extract_linked_lcn_urls(conn, detail):
    urls = set()
    for row in case_external_links(conn, row_text(detail["overview"], "cap_case_id")):
        if clean_text(row["external_source"]).lower() == "localcrimenews" and row_text(row, "external_url"):
            urls.add(row_text(row, "external_url"))
    for row in detail.get("lcn_matches", []):
        arrest_row_id = row_text(row, "lcn_arrest_row_id")
        if arrest_row_id and table_exists(conn, "lcn_arrests"):
            linked = conn.execute(
                "SELECT detail_url FROM lcn_arrests WHERE id = ?",
                (arrest_row_id,),
            ).fetchone()
            if linked and row_text(linked, "detail_url"):
                urls.add(row_text(linked, "detail_url"))
    return urls


def calllog_auto_candidate(detail, conn):
    calls = load_calllog_index()
    if not calls:
        return None

    names = extract_case_names(detail)
    dates = extract_case_dates(detail)
    statutes = extract_case_statute_tokens(detail)
    lcn_urls = extract_linked_lcn_urls(conn, detail)
    best = None

    for base_call_number, entry in calls.items():
        for match in entry.get("arrest_matches") or []:
            score = 0
            reasons = []
            arrest_name = normalize_match_name(match.get("arrest_name"))
            if arrest_name and arrest_name in names:
                score += 50
                reasons.append("name_match")
            elif arrest_name:
                for name in names:
                    overlap = set(arrest_name.split()) & set(name.split())
                    if len(overlap) >= 2:
                        score += 20
                        reasons.append("partial_name_overlap")
                        break

            arrest_date_key = clean_text(match.get("arrest_date_key"))
            if arrest_date_key and arrest_date_key in dates:
                score += 30
                reasons.append("date_match")

            charge_upper = clean_text(match.get("charge")).upper().replace(" ", "")
            if charge_upper:
                for token in statutes:
                    if token and token in charge_upper:
                        score += 15
                        reasons.append("statute_match")
                        break

            detail_url = clean_text(match.get("detail_url"))
            if detail_url and detail_url in lcn_urls:
                score += 120
                reasons.append("linked_lcn_detail_match")

            agency = clean_text(match.get("source_agency")).lower()
            if "sheriff" in agency:
                score += 8
                reasons.append("sheriff_source")

            if score <= 0:
                continue

            candidate = {
                "base_call_number": clean_text(entry.get("base_call_number") or base_call_number),
                "call_number": clean_text(entry.get("call_number")),
                "call_date_key": clean_text(entry.get("call_date_key")),
                "date_time": clean_text(entry.get("date_time")),
                "location": clean_text(entry.get("location")),
                "report_number": clean_text(entry.get("report_number")),
                "arrest_name": clean_text(match.get("arrest_name")),
                "arrest_date_key": arrest_date_key,
                "charge": clean_text(match.get("charge")),
                "detail_url": detail_url,
                "score": score,
                "reasons": reasons,
            }
            if best is None or candidate["score"] > best["score"]:
                best = candidate

    return best


def auto_link_calllog_case(conn, detail):
    candidate = calllog_auto_candidate(detail, conn)
    if not candidate or candidate["score"] < 45:
        return None
    confidence = "strong" if candidate["score"] >= 100 else "likely" if candidate["score"] >= 70 else "possible"
    apply_calllog_link_to_person_cases(
        conn,
        detail,
        external_id=candidate["base_call_number"],
        match_confidence=confidence,
        match_basis=", ".join(candidate["reasons"]),
        manually_confirmed=False,
        notes=f"{candidate['call_number']} | {candidate['date_time']} | {candidate['charge']}",
    )
    return candidate


def apply_calllog_link_to_person_cases(
    conn,
    detail,
    *,
    external_id,
    match_confidence,
    match_basis,
    manually_confirmed,
    notes="",
):
    case_rows = detail.get("person_cases") or [detail["overview"]]
    linked = 0
    for row in case_rows:
        case_id = row_text(row, "cap_case_id")
        if not case_id:
            continue
        upsert_external_case_link(
            conn,
            cap_case_id=case_id,
            case_number=row_text(row, "case_number"),
            party_entity_id=first_defendant_party_id(detail),
            link_type="incident",
            external_source="calllog",
            external_id=clean_text(external_id),
            external_url=CALLLOG_BROWSER_URL,
            match_confidence=clean_text(match_confidence),
            match_basis=clean_text(match_basis),
            manually_confirmed=manually_confirmed,
            notes=clean_text(notes),
        )
        linked += 1
    return linked


def build_case_jail_prefill_with_guess(detail):
    return build_case_jail_prefill(detail)


def spawn_background_job(
    conn,
    *,
    job_key,
    job_type,
    label,
    command,
    target_case_id="",
    target_case_number="",
    interactive=False,
):
    ensure_browser_compat_schema(conn)
    existing = get_browser_job(conn, job_key)
    if browser_job_is_active(existing):
        return {
            "started": False,
            "message": f"{clean_text(existing['label']) or label} is already running.",
            "job": existing,
        }

    if not BACKGROUND_JOB_RUNNER.exists():
        raise FileNotFoundError(f"{BACKGROUND_JOB_RUNNER} is missing.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = SCRIPT_DIR / "output" / "browser_jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{safe_filename(job_key)}_{stamp}.log"

    set_browser_job(
        conn,
        job_key=job_key,
        job_type=job_type,
        label=label,
        status="starting",
        detail="Preparing background job.",
        target_case_id=target_case_id,
        target_case_number=target_case_number,
        command_text=subprocess.list2cmdline(command),
        log_path=str(log_path),
    )

    runner_command = [
        sys.executable,
        str(BACKGROUND_JOB_RUNNER),
        "--db",
        str(DB_PATH),
        "--job-key",
        clean_text(job_key),
        "--job-type",
        clean_text(job_type),
        "--label",
        clean_text(label),
        "--target-case-id",
        clean_text(target_case_id),
        "--target-case-number",
        clean_text(target_case_number),
        "--cwd",
        str(SCRIPT_DIR),
        "--log-path",
        str(log_path),
        "--",
        *command,
    ]

    popen_kwargs = {"cwd": str(SCRIPT_DIR)}
    if interactive:
        creation_flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        if creation_flag:
            popen_kwargs["creationflags"] = creation_flag
    subprocess.Popen(runner_command, **popen_kwargs)

    return {
        "started": True,
        "message": f"{label} started.",
        "job_key": job_key,
        "log_path": str(log_path),
    }


def extract_dept_codes_from_text(value):
    matches = re.findall(r"Dept\s+([A-Za-z0-9]+)", clean_text(value), flags=re.I)
    return [clean_text(item).upper() for item in matches if clean_text(item)]


def case_refresh_scope(detail):
    dates = []
    depts = []

    for row in detail.get("hearings", [])[:8]:
        if row_text(row, "hearing_date"):
            dates.append(row_text(row, "hearing_date"))
    for row in detail.get("appearances", [])[:8]:
        if row_text(row, "calendar_date"):
            dates.append(row_text(row, "calendar_date"))
        if row_text(row, "courtroom_text"):
            depts.extend(extract_dept_codes_from_text(row_text(row, "courtroom_text")))

    depts.extend(extract_dept_codes_from_text(row_text(detail["overview"], "next_hearing")))

    ordered_dates = []
    seen_dates = set()
    for value in dates:
        parsed = normalize_date_for_jail(value)
        iso = ""
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                iso = datetime.strptime(parsed, fmt).strftime("%Y-%m-%d")
                break
            except Exception:
                pass
        if not iso:
            iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", clean_text(value))
            if iso_match:
                iso = iso_match.group(1)
        if iso and iso not in seen_dates:
            seen_dates.add(iso)
            ordered_dates.append(iso)

    ordered_depts = []
    seen_depts = set()
    for dept in depts:
        if dept and dept not in seen_depts:
            seen_depts.add(dept)
            ordered_depts.append(dept)

    return {
        "dates": ordered_dates[:3],
        "depts": ordered_depts[:3],
    }


def lcn_match_confidence_rank_sql(alias="m"):
    return (
        f"CASE COALESCE({alias}.match_confidence, '') "
        "WHEN 'strong' THEN 3 "
        "WHEN 'likely' THEN 2 "
        "WHEN 'possible' THEN 1 "
        "ELSE 0 END"
    )


def safe_lcn_match_predicate(alias="m"):
    return f"""
        (
            COALESCE({alias}.match_confidence, '') IN ('strong', 'likely')
            OR (
                COALESCE({alias}.match_confidence, '') = 'possible'
                AND (
                    COALESCE({alias}.charge_score, 0) > 0
                    OR COALESCE({alias}.citation_score, 0) > 0
                    OR COALESCE({alias}.date_score, 0) >= 12
                )
            )
        )
    """


def empty_lcn_arrests_sql():
    return """
        SELECT
            '' AS cap_case_id,
            '' AS case_number,
            '' AS party_entity_id,
            0 AS lcn_arrest_row_id,
            '' AS lcn_arrest_id,
            '' AS arrest_date,
            '' AS arresting_agency,
            '' AS arrest_location,
            '' AS city_state,
            '' AS age_text,
            '' AS gender,
            '' AS race_text,
            '' AS arrested_for_text,
            '' AS normalized_charge_text,
            '' AS detected_code_prefix,
            '' AS detected_code_number,
            '' AS detected_code_suffix,
            '' AS match_confidence,
            '' AS match_basis,
            0 AS total_score,
            0 AS charge_score,
            0 AS date_score,
            0 AS citation_score,
            0 AS demographic_rank
        WHERE 0
    """


def lcn_fallback_arrests_sql(conn):
    if not table_exists(conn, "case_lcn_match_candidates") or not table_exists(conn, "lcn_arrests"):
        return empty_lcn_arrests_sql()

    confidence_rank = lcn_match_confidence_rank_sql("m")
    safe_predicate = safe_lcn_match_predicate("m")
    return f"""
        SELECT *
        FROM (
            SELECT
                m.cap_case_id,
                m.case_number,
                COALESCE(m.party_entity_id, '') AS party_entity_id,
                a.id AS lcn_arrest_row_id,
                COALESCE(a.lcn_arrest_id, '') AS lcn_arrest_id,
                COALESCE(a.arrest_date, '') AS arrest_date,
                COALESCE(a.source_agency, '') AS arresting_agency,
                COALESCE(a.arrest_location, '') AS arrest_location,
                COALESCE(a.city_state, '') AS city_state,
                COALESCE(a.age_text, '') AS age_text,
                COALESCE(a.gender, '') AS gender,
                COALESCE(a.race_text, '') AS race_text,
                COALESCE(a.arrested_for_text, '') AS arrested_for_text,
                COALESCE(a.normalized_charge_text, '') AS normalized_charge_text,
                COALESCE(a.detected_code_prefix, '') AS detected_code_prefix,
                COALESCE(a.detected_code_number, '') AS detected_code_number,
                COALESCE(a.detected_code_suffix, '') AS detected_code_suffix,
                COALESCE(m.match_confidence, '') AS match_confidence,
                COALESCE(m.match_basis, '') AS match_basis,
                COALESCE(m.total_score, 0) AS total_score,
                COALESCE(m.charge_score, 0) AS charge_score,
                COALESCE(m.date_score, 0) AS date_score,
                COALESCE(m.citation_score, 0) AS citation_score,
                ROW_NUMBER() OVER (
                    PARTITION BY m.cap_case_id, COALESCE(m.party_entity_id, ''), a.id
                    ORDER BY
                        {confidence_rank} DESC,
                        COALESCE(m.charge_score, 0) DESC,
                        COALESCE(m.citation_score, 0) DESC,
                        COALESCE(m.date_score, 0) DESC,
                        COALESCE(m.total_score, 0) DESC,
                        m.id DESC
                ) AS dedupe_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY m.cap_case_id, COALESCE(m.party_entity_id, '')
                    ORDER BY
                        CASE
                            WHEN COALESCE(a.gender, '') <> '' OR COALESCE(a.race_text, '') <> '' OR COALESCE(a.age_text, '') <> '' THEN 0
                            ELSE 1
                        END ASC,
                        {confidence_rank} DESC,
                        COALESCE(m.charge_score, 0) DESC,
                        COALESCE(m.citation_score, 0) DESC,
                        COALESCE(m.date_score, 0) DESC,
                        COALESCE(m.total_score, 0) DESC,
                        m.id DESC
                ) AS demographic_rank
            FROM case_lcn_match_candidates m
            JOIN lcn_arrests a ON a.id = m.lcn_arrest_row_id
            WHERE {safe_predicate}
        )
        WHERE dedupe_rank = 1
    """


def combined_arrests_sql(conn):
    selects = []
    if table_exists(conn, "case_arrests"):
        selects.append(
            """
            SELECT
                cap_case_id,
                case_number,
                COALESCE(CAST(party_entity_id AS TEXT), '') AS party_entity_id,
                COALESCE(arrest_date, '') AS arrest_date,
                COALESCE(arrest_datetime, arrest_date, '') AS arrest_datetime,
                COALESCE(arresting_agency, '') AS arresting_agency,
                COALESCE(arrest_location, '') AS arrest_location,
                COALESCE(booking_number, '') AS booking_number,
                COALESCE(arrest_report_number, '') AS arrest_report_number,
                COALESCE(citation_number, '') AS citation_number,
                '' AS city_state,
                '' AS age_text,
                '' AS gender,
                '' AS race_text,
                'CAP' AS source_system,
                COALESCE(source_field_seen, 'caseArrestInformation') AS source_note,
                '' AS match_basis,
                9999 AS total_score
            FROM case_arrests
            """
        )
    if table_exists(conn, "case_lcn_match_candidates") and table_exists(conn, "lcn_arrests"):
        selects.append(
            f"""
            SELECT
                cap_case_id,
                case_number,
                party_entity_id,
                arrest_date,
                arrest_date AS arrest_datetime,
                arresting_agency,
                arrest_location,
                '' AS booking_number,
                '' AS arrest_report_number,
                '' AS citation_number,
                city_state,
                age_text,
                gender,
                race_text,
                'LCN' AS source_system,
                match_confidence AS source_note,
                match_basis,
                total_score
            FROM ({lcn_fallback_arrests_sql(conn)})
            """
        )
    if not selects:
        return """
            SELECT
                '' AS cap_case_id,
                '' AS case_number,
                '' AS party_entity_id,
                '' AS arrest_date,
                '' AS arrest_datetime,
                '' AS arresting_agency,
                '' AS arrest_location,
                '' AS booking_number,
                '' AS arrest_report_number,
                '' AS citation_number,
                '' AS city_state,
                '' AS age_text,
                '' AS gender,
                '' AS race_text,
                '' AS source_system,
                '' AS source_note,
                '' AS match_basis,
                0 AS total_score
            WHERE 0
        """
    return "\nUNION ALL\n".join(selects)


def combined_demographics_sql(conn):
    selects = []
    if table_exists(conn, "case_demographics"):
        selects.append(
            """
            SELECT
                cap_case_id,
                COALESCE(CAST(party_entity_id AS TEXT), '') AS party_entity_id,
                COALESCE(sex, '') AS sex,
                COALESCE(race, '') AS race,
                COALESCE(date_of_birth, '') AS date_of_birth,
                COALESCE(date_of_birth_text, '') AS date_of_birth_text,
                '' AS age_text,
                'CAP' AS source_system,
                9999 AS total_score
            FROM case_demographics
            WHERE COALESCE(sex, '') <> ''
               OR COALESCE(race, '') <> ''
               OR COALESCE(date_of_birth, '') <> ''
               OR COALESCE(date_of_birth_text, '') <> ''
            """
        )
    if table_exists(conn, "case_lcn_match_candidates") and table_exists(conn, "lcn_arrests"):
        selects.append(
            f"""
            SELECT
                cap_case_id,
                party_entity_id,
                COALESCE(gender, '') AS sex,
                COALESCE(race_text, '') AS race,
                '' AS date_of_birth,
                '' AS date_of_birth_text,
                COALESCE(age_text, '') AS age_text,
                'LCN' AS source_system,
                total_score
            FROM ({lcn_fallback_arrests_sql(conn)})
            WHERE demographic_rank = 1
              AND (
                    COALESCE(gender, '') <> ''
                    OR COALESCE(race_text, '') <> ''
                    OR COALESCE(age_text, '') <> ''
                  )
            """
        )
    if not selects:
        return """
            SELECT
                '' AS cap_case_id,
                '' AS party_entity_id,
                '' AS sex,
                '' AS race,
                '' AS date_of_birth,
                '' AS date_of_birth_text,
                '' AS age_text,
                '' AS source_system,
                0 AS total_score
            WHERE 0
        """
    return "\nUNION ALL\n".join(selects)


def next_sort_dir(current_sort, current_dir, target_sort, default_dir="asc"):
    if clean_text(current_sort) != clean_text(target_sort):
        return default_dir
    return "desc" if clean_text(current_dir).lower() == "asc" else "asc"


def sort_indicator(current_sort, current_dir, target_sort):
    if clean_text(current_sort) != clean_text(target_sort):
        return ""
    return "↑" if clean_text(current_dir).lower() == "asc" else "↓"


def default_case_sort_dir(sort_key):
    return "asc" if clean_text(sort_key) in {"next_hearing", "case_number", "defendant", "department", "charge", "status"} else "desc"


def default_report_sort_dir(sort_key):
    return "asc" if clean_text(sort_key) in {"charge_code", "sample_description"} else "desc"


def render_template(name, **context):
    template = jinja_env.get_template(name)
    return template.render(**context)


def response(start_response, body, status="200 OK", content_type="text/html; charset=utf-8"):
    payload = body.encode("utf-8") if isinstance(body, str) else body
    start_response(
        status,
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [payload]


def redirect(start_response, location):
    start_response("302 Found", [("Location", location), ("Content-Length", "0")])
    return [b""]


def redirect_with_message(start_response, location, message, state="success"):
    separator = "&" if "?" in location else "?"
    return redirect(
        start_response,
        f"{location}{separator}{urlencode({'flash': clean_text(message), 'flash_state': clean_text(state)})}",
    )


def serve_static(start_response, relative_path):
    file_path = (STATIC_DIR / relative_path).resolve()
    if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
        return response(start_response, "Not found", status="404 Not Found", content_type="text/plain; charset=utf-8")

    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    data = file_path.read_bytes()
    start_response(
        "200 OK",
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "public, max-age=300"),
        ],
    )
    return [data]


def ensure_browser_search_index(conn):
    arrests_sql = combined_arrests_sql(conn)
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS browser_case_search
        USING fts5(
            cap_case_id UNINDEXED,
            case_number,
            style,
            party_names,
            aliases,
            charges,
            attorneys,
            arrest_agency,
            arrest_location,
            cross_refs,
            departments,
            hearing_types,
            judges,
            tokenize = 'unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute("DELETE FROM browser_case_search")

    rows = conn.execute(
        """
        SELECT
            c.cap_case_id,
            COALESCE(c.case_number, '') AS case_number,
            COALESCE(c.style, '') AS style,
            COALESCE((
                SELECT group_concat(full_name, ' ')
                FROM (
                    SELECT DISTINCT full_name
                    FROM case_parties
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(full_name, '') <> ''
                    ORDER BY is_defendant DESC, id ASC
                )
            ), '') AS party_names,
            COALESCE((
                SELECT group_concat(full_name, ' ')
                FROM (
                    SELECT DISTINCT full_name
                    FROM case_aliases
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(full_name, '') <> ''
                    ORDER BY id ASC
                )
            ), '') AS aliases,
            COALESCE((
                SELECT group_concat(offense_description, ' ')
                FROM (
                    SELECT DISTINCT offense_description
                    FROM case_charges
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(offense_description, '') <> ''
                    ORDER BY id ASC
                )
            ), '') AS charges,
            COALESCE((
                SELECT group_concat(full_name, ' ')
                FROM (
                    SELECT DISTINCT full_name
                    FROM case_attorneys
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(full_name, '') <> ''
                    ORDER BY id ASC
                )
            ), '') AS attorneys,
            COALESCE((
                SELECT group_concat(arresting_agency, ' ')
                FROM (
                    SELECT DISTINCT arresting_agency
                    FROM ({arrests_sql})
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(arresting_agency, '') <> ''
                    ORDER BY total_score DESC, arresting_agency ASC
                )
            ), '') AS arrest_agency,
            COALESCE((
                SELECT group_concat(arrest_location, ' ')
                FROM (
                    SELECT DISTINCT arrest_location
                    FROM ({arrests_sql})
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(arrest_location, '') <> ''
                    ORDER BY total_score DESC, arrest_location ASC
                )
            ), '') AS arrest_location,
            COALESCE((
                SELECT group_concat(reference_number, ' ')
                FROM (
                    SELECT DISTINCT reference_number
                    FROM case_cross_reference_numbers
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(reference_number, '') <> ''
                    ORDER BY id ASC
                )
            ), '') AS cross_refs,
            COALESCE((
                SELECT group_concat(courtroom_text, ' ')
                FROM (
                    SELECT DISTINCT courtroom_text
                    FROM calendar_appearances
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(courtroom_text, '') <> ''
                    ORDER BY id DESC
                )
            ), '') AS departments,
            COALESCE((
                SELECT group_concat(hearing_type_text, ' ')
                FROM (
                    SELECT DISTINCT hearing_type_text
                    FROM case_hearings
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(hearing_type_text, '') <> ''
                    ORDER BY id DESC
                )
            ), '') AS hearing_types,
            COALESCE((
                SELECT group_concat(judge_text, ' ')
                FROM (
                    SELECT DISTINCT judge_text
                    FROM case_hearings
                    WHERE cap_case_id = c.cap_case_id
                      AND COALESCE(judge_text, '') <> ''
                    ORDER BY id DESC
                )
            ), '') AS judges
        FROM cases c
        ORDER BY c.cap_case_id
        """.format(arrests_sql=arrests_sql)
    ).fetchall()

    conn.executemany(
        """
        INSERT INTO browser_case_search (
            cap_case_id,
            case_number,
            style,
            party_names,
            aliases,
            charges,
            attorneys,
            arrest_agency,
            arrest_location,
            cross_refs,
            departments,
            hearing_types,
            judges
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["cap_case_id"],
                row["case_number"],
                row["style"],
                row["party_names"],
                row["aliases"],
                row["charges"],
                row["attorneys"],
                row["arrest_agency"],
                row["arrest_location"],
                row["cross_refs"],
                row["departments"],
                row["hearing_types"],
                row["judges"],
            )
            for row in rows
        ],
    )
    conn.commit()


def build_fts_query(raw_query):
    tokens = re.findall(r"[A-Za-z0-9]+", clean_text(raw_query))
    if not tokens:
        return ""
    return " AND ".join(f"{token}*" for token in tokens)


def get_charge_code_options(conn):
    return conn.execute(
        """
        SELECT
            COALESCE(NULLIF(strip_charge_suffix(statute_raw), ''), '(missing statute)') AS value,
            extract_charge_book(COALESCE(NULLIF(strip_charge_suffix(statute_raw), ''), '(missing statute)')) AS code_book,
            COUNT(*) AS n,
            MIN(COALESCE(NULLIF(clean_charge_description(offense_description, statute_raw), ''), '')) AS description
        FROM case_charges
        GROUP BY value
        ORDER BY
            charge_book_rank(value) ASC,
            extract_charge_book(value) ASC,
            value ASC
        """
    ).fetchall()


def query_top_charge_by_demographic(conn, filters, dimension):
    demo_sql = combined_demographics_sql(conn)
    where_sql, params = build_charge_report_where(filters)

    if dimension == "sex":
        value_expr = "COALESCE(NULLIF(d.sex, ''), '(unknown)')"
    elif dimension == "race":
        value_expr = "COALESCE(NULLIF(d.race, ''), '(unknown)')"
    elif dimension == "age_group":
        value_expr = "COALESCE(NULLIF(age_group_label(d.age_text, d.date_of_birth), ''), '(unknown)')"
    else:
        raise ValueError(f"Unsupported demographic dimension: {dimension}")

    return conn.execute(
        f"""
        WITH demo AS (
            {demo_sql}
        ),
        grouped AS (
            SELECT
                {value_expr} AS demographic_value,
                COALESCE(NULLIF(strip_charge_suffix(ch.statute_raw), ''), '(missing statute)') AS charge_code,
                COUNT(*) AS charge_count,
                COUNT(DISTINCT ch.cap_case_id) AS case_count,
                MIN(COALESCE(NULLIF(clean_charge_description(ch.offense_description, ch.statute_raw), ''), '')) AS sample_description
            FROM case_charges ch
            JOIN cases c ON c.cap_case_id = ch.cap_case_id
            JOIN demo d ON d.cap_case_id = ch.cap_case_id
            {where_sql}
            GROUP BY demographic_value, charge_code
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY demographic_value
                    ORDER BY charge_count DESC, case_count DESC, charge_code ASC
                ) AS rn
            FROM grouped
            WHERE demographic_value <> '(unknown)'
        )
        SELECT demographic_value, charge_code, charge_count, case_count, sample_description
        FROM ranked
        WHERE rn = 1
        ORDER BY demographic_value ASC
        LIMIT 24
        """,
        params,
    ).fetchall()


def browser_jobs_for_logs(conn, status="", limit=40):
    if not table_exists(conn, "browser_jobs"):
        return []

    status = clean_text(status).lower()
    params = []
    where = ""
    if status:
        where = "WHERE LOWER(COALESCE(status, '')) = ?"
        params.append(status)

    return conn.execute(
        f"""
        SELECT *
        FROM browser_jobs
        {where}
        ORDER BY COALESCE(updated_at, started_at) DESC, id DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    ).fetchall()


def get_nav_context(conn):
    summary = {
        "total_cases": conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
        "active_cases": conn.execute("SELECT COUNT(*) FROM cases WHERE status = 'Active'").fetchone()[0],
        "appearance_rows": conn.execute("SELECT COUNT(*) FROM calendar_appearances").fetchone()[0],
        "charge_rows": conn.execute("SELECT COUNT(*) FROM case_charges").fetchone()[0],
    }

    recent_cases = conn.execute(
        """
        SELECT case_number, cap_case_id, style, file_date
        FROM cases
        ORDER BY COALESCE(NULLIF(latest_seen_at, ''), NULLIF(file_date, ''), '') DESC
        LIMIT 6
        """
    ).fetchall()

    top_departments = conn.execute(
        """
        SELECT courtroom_text AS label, COUNT(*) AS n
        FROM calendar_appearances
        WHERE COALESCE(courtroom_text, '') <> ''
        GROUP BY courtroom_text
        ORDER BY n DESC, courtroom_text
        LIMIT 6
        """
    ).fetchall()

    top_charge_codes = conn.execute(
        """
        SELECT
            charge_code AS label,
            n,
            description
        FROM (
            SELECT
                COALESCE(NULLIF(strip_charge_suffix(statute_raw), ''), '(missing statute)') AS charge_code,
                COUNT(*) AS n,
                MIN(COALESCE(NULLIF(clean_charge_description(offense_description, statute_raw), ''), '')) AS description
            FROM case_charges
            WHERE COALESCE(statute_raw, '') <> ''
            GROUP BY charge_code
        )
        ORDER BY n DESC, charge_code ASC
        LIMIT 6
        """
    ).fetchall()

    return {
        "summary": summary,
        "recent_cases": recent_cases,
        "top_departments": top_departments,
        "top_charge_codes": top_charge_codes,
    }


def get_filter_options(conn):
    arrests_sql = combined_arrests_sql(conn)

    def fetch_values(sql):
        return [row["value"] for row in conn.execute(sql)]

    return {
        "categories": fetch_values(
            """
            SELECT DISTINCT category AS value
            FROM cases
            WHERE COALESCE(category, '') <> ''
            ORDER BY category
            """
        ),
        "statuses": fetch_values(
            """
            SELECT DISTINCT status AS value
            FROM cases
            WHERE COALESCE(status, '') <> ''
            ORDER BY status
            """
        ),
        "departments": fetch_values(
            """
            SELECT DISTINCT courtroom_text AS value
            FROM calendar_appearances
            WHERE COALESCE(courtroom_text, '') <> ''
            ORDER BY courtroom_text
            """
        ),
        "hearing_types": fetch_values(
            """
            SELECT DISTINCT hearing_type_text AS value
            FROM case_hearings
            WHERE COALESCE(hearing_type_text, '') <> ''
            ORDER BY hearing_type_text
            """
        ),
        "arrest_agencies": fetch_values(
            f"""
            SELECT DISTINCT arresting_agency AS value
            FROM ({arrests_sql})
            WHERE COALESCE(arresting_agency, '') <> ''
            ORDER BY arresting_agency
            """
        ),
        "charge_codes": get_charge_code_options(conn),
    }


def get_data_availability(conn):
    arrests_sql = combined_arrests_sql(conn)
    demographics_sql = combined_demographics_sql(conn)

    cap_arrest_rows = conn.execute(
        "SELECT COUNT(*) FROM case_arrests" if table_exists(conn, "case_arrests") else "SELECT 0"
    ).fetchone()[0]
    lcn_match_rows = conn.execute(
        f"SELECT COUNT(*) FROM ({lcn_fallback_arrests_sql(conn)})"
    ).fetchone()[0]
    arrest_rows = conn.execute(f"SELECT COUNT(*) FROM ({arrests_sql})").fetchone()[0]
    demographics_rows = conn.execute(f"SELECT COUNT(*) FROM ({demographics_sql})").fetchone()[0]
    arrest_location_rows = conn.execute(
        f"SELECT COUNT(*) FROM ({arrests_sql}) WHERE COALESCE(arrest_location, '') <> ''"
    ).fetchone()[0]
    arrest_agency_rows = conn.execute(
        f"SELECT COUNT(*) FROM ({arrests_sql}) WHERE COALESCE(arresting_agency, '') <> ''"
    ).fetchone()[0]

    return {
        "cap_arrest_rows": cap_arrest_rows,
        "lcn_match_rows": lcn_match_rows,
        "arrest_rows": arrest_rows,
        "arrest_location_rows": arrest_location_rows,
        "arrest_agency_rows": arrest_agency_rows,
        "demographics_rows": demographics_rows,
        "has_arrest_data": arrest_rows > 0,
        "has_arrest_locations": arrest_location_rows > 0,
        "has_arrest_agencies": arrest_agency_rows > 0,
        "has_demographics": demographics_rows > 0,
        "using_lcn_fallback": lcn_match_rows > 0,
    }


def build_case_where(conn, filters):
    clauses = []
    params = []
    arrests_sql = combined_arrests_sql(conn)

    fts_query = build_fts_query(filters.get("q", ""))
    if fts_query:
        clauses.append(
            """
            c.cap_case_id IN (
                SELECT cap_case_id
                FROM browser_case_search
                WHERE browser_case_search MATCH ?
            )
            """
        )
        params.append(fts_query)

    status = clean_text(filters.get("status"))
    if status:
        clauses.append("c.status = ?")
        params.append(status)

    filed_from = clean_text(filters.get("filed_from"))
    if filed_from:
        clauses.append("date(c.file_date) >= date(?)")
        params.append(filed_from)

    filed_to = clean_text(filters.get("filed_to"))
    if filed_to:
        clauses.append("date(c.file_date) <= date(?)")
        params.append(filed_to)

    department = clean_text(filters.get("department"))
    if department:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM calendar_appearances ca
                WHERE ca.cap_case_id = c.cap_case_id
                  AND ca.courtroom_text = ?
            )
            """
        )
        params.append(department)

    hearing_type = clean_text(filters.get("hearing_type"))
    if hearing_type:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND h.hearing_type_text = ?
            )
            """
        )
        params.append(hearing_type)

    charge_code = clean_text(filters.get("charge_code"))
    if charge_code:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
                  AND strip_charge_suffix(ch.statute_raw) = ?
            )
            """
        )
        params.append(charge_code)

    arrest_agency = clean_text(filters.get("arrest_agency"))
    if arrest_agency:
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND ar.arresting_agency = ?
            )
            """
        )
        params.append(arrest_agency)

    arrest_date_from = clean_text(filters.get("arrest_date_from"))
    if arrest_date_from:
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND date(COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, ''), '1900-01-01')) >= date(?)
            )
            """
        )
        params.append(arrest_date_from)

    arrest_date_to = clean_text(filters.get("arrest_date_to"))
    if arrest_date_to:
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND date(COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, ''), '1900-01-01')) <= date(?)
            )
            """
        )
        params.append(arrest_date_to)

    category = clean_text(filters.get("category"))
    if category:
        clauses.append("c.category = ?")
        params.append(category)

    arrest_location = clean_text(filters.get("arrest_location"))
    if arrest_location:
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND COALESCE(ar.arrest_location, '') LIKE ?
            )
            """
        )
        params.append(f"%{arrest_location}%")

    if bool_param(filters.get("new_only", "")):
        clauses.append(
            """
            date(COALESCE(NULLIF(substr(c.first_seen_at, 1, 10), ''), NULLIF(c.file_date, ''), '1900-01-01'))
            >= date('now', ?)
            """
        )
        params.append(f"-{NEW_CASE_WINDOW_DAYS} day")

    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params


def case_sort_sql(sort_key, arrests_sql):
    sort_key = clean_text(sort_key) or "recent"
    return {
        "recent": "COALESCE(NULLIF(c.latest_seen_at, ''), NULLIF(c.file_date, ''), '')",
        "filed": "COALESCE(NULLIF(c.file_date, ''), '1900-01-01')",
        "case_number": "COALESCE(c.case_number, '')",
        "next_hearing": """
            COALESCE((
                SELECT MIN(date(h.hearing_date))
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND COALESCE(h.hearing_date, '') <> ''
                  AND date(h.hearing_date) >= date('now')
            ), '9999-12-31')
        """,
        "defendant": """
            COALESCE((
                SELECT p.full_name
                FROM case_parties p
                WHERE p.cap_case_id = c.cap_case_id
                  AND COALESCE(p.full_name, '') <> ''
                ORDER BY p.is_defendant DESC, p.id ASC
                LIMIT 1
            ), c.style, '')
        """,
        "department": """
            COALESCE((
                SELECT ca.courtroom_text
                FROM calendar_appearances ca
                WHERE ca.cap_case_id = c.cap_case_id
                  AND COALESCE(ca.courtroom_text, '') <> ''
                ORDER BY date(COALESCE(ca.calendar_date, ca.session_date)) DESC, COALESCE(ca.session_start_time, '') DESC
                LIMIT 1
            ), '')
        """,
        "status": "COALESCE(c.status, '')",
        "arrest_date": f"""
            COALESCE((
                SELECT COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, ''))
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                ORDER BY
                    CASE WHEN ar.source_system = 'CAP' THEN 0 ELSE 1 END ASC,
                    COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, ''), '1900-01-01') DESC,
                    ar.total_score DESC
                LIMIT 1
            ), '')
        """,
        "charge": """
            COALESCE((
                SELECT strip_charge_suffix(ch.statute_raw)
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
                  AND COALESCE(ch.statute_raw, '') <> ''
                ORDER BY ch.id ASC
                LIMIT 1
            ), '')
        """,
    }.get(sort_key, "COALESCE(NULLIF(c.latest_seen_at, ''), NULLIF(c.file_date, ''), '')")


def query_cases(conn, filters, include_all=False):
    page = parse_int(filters.get("page"), default=1)
    offset = (page - 1) * RESULTS_PER_PAGE
    where_sql, params = build_case_where(conn, filters)
    sort_key = clean_text(filters.get("sort", "recent")) or "recent"
    sort_dir = clean_text(filters.get("sort_dir", "desc")).lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"
    arrests_sql = combined_arrests_sql(conn)
    sort_expr = case_sort_sql(sort_key, arrests_sql)
    secondary_dir = "ASC" if sort_dir == "asc" else "DESC"
    sort_sql = f"{sort_expr} {sort_dir.upper()}, COALESCE(c.case_number, '') {secondary_dir}"

    total = conn.execute(f"SELECT COUNT(*) FROM cases c {where_sql}", params).fetchone()[0]

    limit_sql = "" if include_all else "LIMIT ? OFFSET ?"
    final_params = list(params)
    if not include_all:
        final_params.extend([RESULTS_PER_PAGE, offset])

    rows = conn.execute(
        f"""
        SELECT
            c.cap_case_id,
            c.case_number,
            c.style,
            c.file_date,
            c.status,
            c.first_seen_at,
            c.latest_seen_at,
            c.detail_scraped_at,
            c.citation_number,
            COALESCE((
                SELECT p.full_name
                FROM case_parties p
                WHERE p.cap_case_id = c.cap_case_id
                  AND COALESCE(p.full_name, '') <> ''
                ORDER BY p.is_defendant DESC, p.id ASC
                LIMIT 1
            ), c.style) AS defendant_name,
            (
                SELECT h.hearing_date
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND COALESCE(h.hearing_date, '') <> ''
                ORDER BY
                    CASE WHEN date(h.hearing_date) >= date('now') THEN 0 ELSE 1 END,
                    date(h.hearing_date) ASC,
                    COALESCE(h.hearing_time, '') ASC
                LIMIT 1
            ) AS next_hearing_date,
            (
                SELECT h.hearing_time
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND COALESCE(h.hearing_date, '') <> ''
                ORDER BY
                    CASE WHEN date(h.hearing_date) >= date('now') THEN 0 ELSE 1 END,
                    date(h.hearing_date) ASC,
                    COALESCE(h.hearing_time, '') ASC
                LIMIT 1
            ) AS next_hearing_time,
            (
                SELECT h.calendar_text
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND COALESCE(h.hearing_date, '') <> ''
                ORDER BY
                    CASE WHEN date(h.hearing_date) >= date('now') THEN 0 ELSE 1 END,
                    date(h.hearing_date) ASC,
                    COALESCE(h.hearing_time, '') ASC
                LIMIT 1
            ) AS next_hearing_calendar,
            (
                SELECT ca.courtroom_text
                FROM calendar_appearances ca
                WHERE ca.cap_case_id = c.cap_case_id
                  AND COALESCE(ca.courtroom_text, '') <> ''
                ORDER BY date(COALESCE(ca.calendar_date, ca.session_date)) DESC, COALESCE(ca.session_start_time, '') DESC
                LIMIT 1
            ) AS latest_department,
            (
                SELECT ch.statute_raw
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
                  AND COALESCE(ch.statute_raw, '') <> ''
                ORDER BY ch.id ASC
                LIMIT 1
            ) AS top_statute,
            (
                SELECT ch.offense_description
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
                  AND COALESCE(ch.offense_description, '') <> ''
                ORDER BY ch.id ASC
                LIMIT 1
            ) AS charge_preview,
            (
                SELECT COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, ''))
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND COALESCE(NULLIF(ar.arrest_datetime, ''), NULLIF(ar.arrest_date, '')) <> ''
                ORDER BY CASE WHEN ar.source_system = 'CAP' THEN 0 ELSE 1 END ASC, ar.total_score DESC, COALESCE(ar.arrest_datetime, ar.arrest_date, '') DESC
                LIMIT 1
            ) AS arrest_date,
            (
                SELECT ar.arresting_agency
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND COALESCE(ar.arresting_agency, '') <> ''
                ORDER BY CASE WHEN ar.source_system = 'CAP' THEN 0 ELSE 1 END ASC, ar.total_score DESC, COALESCE(ar.arrest_datetime, ar.arrest_date, '') DESC
                LIMIT 1
            ) AS arresting_agency,
            (
                SELECT ar.arrest_location
                FROM ({arrests_sql}) ar
                WHERE ar.cap_case_id = c.cap_case_id
                  AND COALESCE(ar.arrest_location, '') <> ''
                ORDER BY CASE WHEN ar.source_system = 'CAP' THEN 0 ELSE 1 END ASC, ar.total_score DESC, COALESCE(ar.arrest_datetime, ar.arrest_date, '') DESC
                LIMIT 1
            ) AS arrest_location,
            (
                SELECT COUNT(*)
                FROM case_charges ch
                WHERE ch.cap_case_id = c.cap_case_id
            ) AS charge_count,
            c.category
        FROM cases c
        {where_sql}
        ORDER BY {sort_sql}
        {limit_sql}
        """.format(arrests_sql=arrests_sql),
        final_params,
    ).fetchall()

    total_pages = max(1, (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE) if not include_all else 1
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "total_pages": total_pages,
    }


def get_case_detail(conn, cap_case_id):
    if not cap_case_id:
        return None
    arrests_sql = combined_arrests_sql(conn)
    demographics_sql = combined_demographics_sql(conn)
    lcn_matches_sql = lcn_fallback_arrests_sql(conn)

    overview = conn.execute(
        """
        SELECT
            c.*,
            COALESCE((
                SELECT p.full_name
                FROM case_parties p
                WHERE p.cap_case_id = c.cap_case_id
                  AND COALESCE(p.full_name, '') <> ''
                ORDER BY p.is_defendant DESC, p.id ASC
                LIMIT 1
            ), c.style) AS defendant_name
        FROM cases c
        WHERE c.cap_case_id = ?
        """,
        (cap_case_id,),
    ).fetchone()

    if not overview:
        return None

    return {
        "overview": overview,
        "parties": conn.execute(
            """
            SELECT *
            FROM case_parties
            WHERE cap_case_id = ?
            ORDER BY is_defendant DESC, party_type, id
            """,
            (cap_case_id,),
        ).fetchall(),
        "aliases": conn.execute(
            """
            SELECT *
            FROM case_aliases
            WHERE cap_case_id = ?
            ORDER BY full_name
            """,
            (cap_case_id,),
        ).fetchall(),
        "attorneys": conn.execute(
            """
            SELECT *
            FROM case_attorneys
            WHERE cap_case_id = ?
            ORDER BY is_lead DESC, full_name
            """,
            (cap_case_id,),
        ).fetchall(),
        "charges": conn.execute(
            """
            SELECT *
            FROM case_charges
            WHERE cap_case_id = ?
            ORDER BY COALESCE(NULLIF(offense_date, ''), '1900-01-01') DESC, charge_number
            """,
            (cap_case_id,),
        ).fetchall(),
        "hearings": conn.execute(
            """
            SELECT *
            FROM case_hearings
            WHERE cap_case_id = ?
            ORDER BY COALESCE(NULLIF(hearing_date, ''), '1900-01-01') DESC, COALESCE(hearing_time, '') DESC
            LIMIT 40
            """,
            (cap_case_id,),
        ).fetchall(),
        "appearances": conn.execute(
            """
            SELECT *
            FROM calendar_appearances
            WHERE cap_case_id = ?
            ORDER BY COALESCE(NULLIF(calendar_date, ''), '1900-01-01') DESC, COALESCE(session_start_time, '') DESC
            LIMIT 30
            """,
            (cap_case_id,),
        ).fetchall(),
        "events": conn.execute(
            """
            SELECT *
            FROM case_events
            WHERE cap_case_id = ?
            ORDER BY COALESCE(NULLIF(event_date, ''), '1900-01-01') DESC, id DESC
            LIMIT 40
            """,
            (cap_case_id,),
        ).fetchall(),
        "documents": conn.execute(
            """
            SELECT *
            FROM case_documents
            WHERE cap_case_id = ?
            ORDER BY COALESCE(NULLIF(effective_date, ''), '1900-01-01') DESC, id DESC
            LIMIT 25
            """,
            (cap_case_id,),
        ).fetchall(),
        "warrants": conn.execute(
            """
            SELECT *
            FROM case_warrants
            WHERE cap_case_id = ?
            ORDER BY COALESCE(NULLIF(issue_date, ''), '1900-01-01') DESC, id DESC
            """,
            (cap_case_id,),
        ).fetchall(),
        "financials": conn.execute(
            """
            SELECT *
            FROM case_financials
            WHERE cap_case_id = ?
            ORDER BY COALESCE(NULLIF(due_date, ''), '1900-01-01') DESC, id DESC
            LIMIT 20
            """,
            (cap_case_id,),
        ).fetchall(),
        "arrests": conn.execute(
            f"""
            SELECT *
            FROM ({arrests_sql})
            WHERE cap_case_id = ?
            ORDER BY
                CASE WHEN source_system = 'CAP' THEN 0 ELSE 1 END ASC,
                COALESCE(NULLIF(arrest_datetime, ''), NULLIF(arrest_date, ''), '1900-01-01') DESC,
                total_score DESC
            """,
            (cap_case_id,),
        ).fetchall(),
        "demographics": conn.execute(
            f"""
            SELECT *
            FROM ({demographics_sql})
            WHERE cap_case_id = ?
            ORDER BY
                CASE WHEN source_system = 'CAP' THEN 0 ELSE 1 END ASC,
                total_score DESC
            """,
            (cap_case_id,),
        ).fetchall(),
        "lcn_matches": conn.execute(
            f"""
            SELECT *
            FROM ({lcn_matches_sql})
            WHERE cap_case_id = ?
            ORDER BY total_score DESC, arrest_date DESC, lcn_arrest_row_id DESC
            LIMIT 12
            """,
            (cap_case_id,),
        ).fetchall(),
        "cross_refs": conn.execute(
            """
            SELECT *
            FROM case_cross_reference_numbers
            WHERE cap_case_id = ?
            ORDER BY reference_type, reference_number
            """,
            (cap_case_id,),
        ).fetchall(),
    }


def enrich_case_detail(conn, detail):
    if not detail:
        return None
    cap_case_id = row_text(detail["overview"], "cap_case_id")
    detail["jail_prefill"] = build_case_jail_prefill(detail)
    detail["latest_jail_capture"] = latest_case_jail_capture(conn, cap_case_id)
    detail["jail_capture_history"] = case_jail_capture_history(conn, cap_case_id)
    detail["external_links"] = case_external_links(conn, cap_case_id)
    detail["case_jobs"] = recent_browser_jobs(conn, target_case_id=cap_case_id, limit=4)
    detail["best_lcn_candidate"] = best_lcn_link_candidate(conn, cap_case_id)
    detail["best_calllog_candidate"] = calllog_auto_candidate(detail, conn)
    preferred_lcn_url = ""
    for row in detail["external_links"]:
        if clean_text(row["external_source"]).lower() == "localcrimenews" and row_text(row, "external_url"):
            preferred_lcn_url = row_text(row, "external_url")
            break
    if not preferred_lcn_url and detail["best_lcn_candidate"]:
        preferred_lcn_url = row_text(detail["best_lcn_candidate"], "detail_url")
    detail["preferred_lcn_url"] = preferred_lcn_url or LCN_HOME_URL
    detail["open_jail_url"] = jail_import_url(cap_case_id)
    detail["open_court_url"] = official_court_case_url(cap_case_id)
    detail["open_lcn_url"] = lcn_lookup_url(detail)
    detail["person_identity"] = person_identity(detail)
    detail["person_cases"] = related_person_cases(conn, detail)
    detail["person_links"] = person_external_links(conn, detail)
    detail["calllog_links"] = calllog_link_rows(detail)
    detail["person_jail_captures"] = person_jail_captures(conn, detail)
    detail["booking_numbers"] = booking_number_rows(detail)
    case_number = row_text(detail["overview"], "case_number")
    if table_exists(conn, "case_property_links") and case_number:
        detail["property_links"] = conn.execute(
            "SELECT * FROM case_property_links WHERE case_number = ? ORDER BY created_at DESC",
            (case_number,),
        ).fetchall()
    else:
        detail["property_links"] = []
    return detail


def render_case_csv(conn, filters, start_response):
    result = query_cases(conn, filters, include_all=True)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "case_number",
            "cap_case_id",
            "defendant_name",
            "style",
            "file_date",
            "status",
            "latest_department",
            "next_hearing_date",
            "next_hearing_time",
            "next_hearing_calendar",
            "top_statute",
            "charge_preview",
            "category",
            "arrest_date",
            "arresting_agency",
            "arrest_location",
            "charge_count",
            "latest_seen_at",
        ]
    )

    for row in result["rows"]:
        writer.writerow(
            [
                row["case_number"],
                row["cap_case_id"],
                row["defendant_name"],
                row["style"],
                row["file_date"],
                row["status"],
                row["latest_department"],
                row["next_hearing_date"],
                row["next_hearing_time"],
                row["next_hearing_calendar"],
                row["top_statute"],
                row["charge_preview"],
                row["category"],
                row["arrest_date"],
                row["arresting_agency"],
                row["arrest_location"],
                row["charge_count"],
                row["latest_seen_at"],
            ]
        )

    payload = out.getvalue().encode("utf-8")
    start_response(
        "200 OK",
        [
            ("Content-Type", "text/csv; charset=utf-8"),
            ("Content-Disposition", 'attachment; filename="court_browser_cases.csv"'),
            ("Content-Length", str(len(payload))),
        ],
    )
    return [payload]


def render_browser(environ, start_response):
    params = parse_request(environ)
    sort_key = clean_text(params.get("sort", "recent")) or "recent"
    filters = {
        "q": clean_text(params.get("q", "")),
        "status": clean_text(params.get("status", "")),
        "department": clean_text(params.get("department", "")),
        "hearing_type": clean_text(params.get("hearing_type", "")),
        "charge_code": clean_text(params.get("charge_code", "")),
        "category": clean_text(params.get("category", "")),
        "arrest_agency": clean_text(params.get("arrest_agency", "")),
        "arrest_date_from": clean_text(params.get("arrest_date_from", "")),
        "arrest_date_to": clean_text(params.get("arrest_date_to", "")),
        "arrest_location": clean_text(params.get("arrest_location", "")),
        "filed_from": clean_text(params.get("filed_from", "")),
        "filed_to": clean_text(params.get("filed_to", "")),
        "sort": sort_key,
        "sort_dir": clean_text(params.get("sort_dir", "")) or default_case_sort_dir(sort_key),
        "new_only": "1" if bool_param(params.get("new_only", "")) else "",
        "page": str(parse_int(params.get("page"), 1)),
    }
    selected_case_id = clean_text(params.get("case", ""))

    conn = db_connect()
    try:
        if params.get("download") == "csv":
            return render_case_csv(conn, filters, start_response)

        results = query_cases(conn, filters)
        if not selected_case_id and results["rows"]:
            selected_case_id = clean_text(results["rows"][0]["cap_case_id"])

        detail = enrich_case_detail(conn, get_case_detail(conn, selected_case_id)) if selected_case_id else None
        filter_options = get_filter_options(conn)
        nav_context = get_nav_context(conn)
        availability = get_data_availability(conn)
        global_status = build_global_status(conn, target_case_id=selected_case_id)

        page_title = "Victorville Court Browser"
        body = render_template(
            "court_browser_search.html",
            page_title=page_title,
            active_nav="search",
            filters=filters,
            params=params,
            results=results,
            selected_case_id=selected_case_id,
            detail=detail,
            filter_options=filter_options,
            nav_context=nav_context,
            availability=availability,
            global_status=global_status,
            replace_query=replace_query,
            next_sort_dir=next_sort_dir,
            sort_indicator=sort_indicator,
            strip_charge_suffix=strip_charge_suffix,
            clean_charge_description=clean_charge_description,
            case_url=case_url,
            person_url=person_url,
            property_url=property_url,
        )
        return response(start_response, body)
    finally:
        conn.close()


def render_case_detail(environ, start_response, cap_case_id):
    params = parse_request(environ)
    cap_case_id = clean_text(cap_case_id)
    conn = db_connect()
    try:
        detail = enrich_case_detail(conn, get_case_detail(conn, cap_case_id))
        if not detail:
            return response(start_response, "Case not found", status="404 Not Found", content_type="text/plain; charset=utf-8")

        nav_context = get_nav_context(conn)
        availability = get_data_availability(conn)
        global_status = build_global_status(conn, target_case_id=cap_case_id)
        active_case_tab = clean_case_tab(params.get("tab", "case-detail"))
        report_presets = load_report_presets(conn)
        selected_preset = get_report_preset(conn, params.get("report", ""))
        report_field_catalog = build_report_field_catalog(detail)
        selected_report_keys = selected_report_field_keys(params, selected_preset)
        selected_report_key_set = set(selected_report_keys)
        report_rows = [row for row in report_field_catalog if row["key"] in selected_report_key_set]
        body = render_template(
            "court_browser_case_detail.html",
            page_title=f"Case {row_text(detail['overview'], 'case_number')}",
            active_nav="search",
            detail=detail,
            nav_context=nav_context,
            availability=availability,
            global_status=global_status,
            flash=clean_text(params.get("flash", "")),
            flash_state=clean_text(params.get("flash_state", "")) or "success",
            official_jail_url=JAIL_INMATE_LOCATOR_URL,
            lcn_home_url=LCN_HOME_URL,
            court_calendar_url=COURT_CALENDAR_URL,
            calllog_browser_url=CALLLOG_BROWSER_URL,
            active_case_tab=active_case_tab,
            case_tab_url=case_tab_url,
            report_presets=report_presets,
            selected_report_preset=selected_preset,
            report_field_catalog=report_field_catalog,
            selected_report_keys=selected_report_keys,
            report_rows=report_rows,
            thats_them_url=build_thats_them_search_url(detail["jail_prefill"]),
            case_url=case_url,
            person_url=person_url,
            property_url=property_url,
            replace_query=replace_query,
            strip_charge_suffix=strip_charge_suffix,
            clean_charge_description=clean_charge_description,
        )
        return response(start_response, body)
    finally:
        conn.close()


def launch_case_refresh(conn, detail):
    scope = case_refresh_scope(detail)
    command = [sys.executable, str(COURT_SCRAPER_SCRIPT), "--force-details-all"]
    if not cap_credentials_configured():
        command.append("--skip-login")
    if scope["dates"]:
        command.extend(["--only-dates", ",".join(scope["dates"])])
    else:
        command.extend(["--start-date", date.today().isoformat(), "--days", "1"])
    if scope["depts"]:
        command.extend(["--only-depts", ",".join(scope["depts"])])

    overview = detail["overview"]
    return spawn_background_job(
        conn,
        job_key=f"court_refresh:{row_text(overview, 'cap_case_id')}",
        job_type="court_refresh",
        label=f"Court refresh for {row_text(overview, 'case_number')}",
        command=command,
        target_case_id=row_text(overview, "cap_case_id"),
        target_case_number=row_text(overview, "case_number"),
        interactive=False,
    )


def handle_case_action(environ, start_response, cap_case_id):
    if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
        return redirect(start_response, case_url(cap_case_id))

    params = parse_request(environ)
    action = clean_text(params.get("action", ""))
    cap_case_id = clean_text(cap_case_id)
    next_tab = clean_case_tab(params.get("next_tab", "case-detail"))

    if action == "launch_jail":
        return redirect(
            start_response,
            f"/jail-import/launch?{urlencode({'case': cap_case_id, 'return_to': 'case', 'case_tab': next_tab})}",
        )

    conn = db_connect()
    try:
        detail = enrich_case_detail(conn, get_case_detail(conn, cap_case_id))
        if not detail:
            return response(start_response, "Case not found", status="404 Not Found", content_type="text/plain; charset=utf-8")

        overview = detail["overview"]
        if action == "court_refresh":
            launch = launch_case_refresh(conn, detail)
            state = "success" if launch["started"] else "error"
            return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), launch["message"], state=state)

        if action == "auto_link_lcn":
            candidate = auto_link_lcn_case(conn, detail)
            if not candidate:
                return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), "No strong LCN match candidate is ready yet.", state="error")
            message = f"Linked the best LCN candidate for {row_text(overview, 'case_number')}."
            return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), message)

        if action == "auto_link_calllog":
            candidate = auto_link_calllog_case(conn, detail)
            if not candidate:
                return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), "No good call-log match was found yet.", state="error")
            linked_count = len(detail.get("person_cases") or [detail["overview"]])
            message = f"Linked call log {candidate['base_call_number']} across {linked_count} related case(s)."
            return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), message)

        if action == "manual_link_calllog":
            call_number = clean_text(params.get("call_number", ""))
            if not call_number:
                return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), "Enter a call number first.", state="error")
            linked_count = apply_calllog_link_to_person_cases(
                conn,
                detail,
                external_id=call_number,
                match_confidence="manual",
                match_basis="manual_call_number",
                manually_confirmed=True,
                notes="Added manually from the case detail page.",
            )
            return redirect_with_message(
                start_response,
                case_tab_url(cap_case_id, next_tab),
                f"Linked call log {call_number} across {linked_count} related case(s).",
            )

        if action == "manual_link_lcn":
            lcn_url = clean_text(params.get("lcn_url", ""))
            if not lcn_url:
                return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), "Paste an LCN URL first.", state="error")
            match = re.search(r"/welcome/detail/(\d+)", lcn_url)
            external_id = match.group(1) if match else lcn_url
            upsert_external_case_link(
                conn,
                cap_case_id=cap_case_id,
                case_number=row_text(overview, "case_number"),
                party_entity_id=first_defendant_party_id(detail),
                link_type="arrest",
                external_source="localcrimenews",
                external_id=external_id,
                external_url=lcn_url,
                match_confidence="manual",
                match_basis="manual_lcn_url",
                manually_confirmed=True,
                notes="Added manually from the case detail page.",
            )
            return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), "Linked the pasted LCN record.")

        if action == "save_report_preset":
            field_keys = selected_report_field_keys(params, None)
            if not field_keys:
                return redirect_with_message(
                    start_response,
                    case_tab_url(cap_case_id, "report"),
                    "Choose at least one field before saving a report preset.",
                    state="error",
                )
            preset_id = save_report_preset(conn, params.get("preset_name", ""), field_keys)
            return redirect_with_message(
                start_response,
                case_tab_url(cap_case_id, "report", report=preset_id),
                "Saved the report field preset.",
            )

        return redirect_with_message(start_response, case_tab_url(cap_case_id, next_tab), "Unknown case action.", state="error")
    finally:
        conn.close()


def build_charge_report_where(filters):
    clauses = []
    params = []

    q = clean_text(filters.get("q", ""))
    if q:
        clauses.append(
            """
            (
                COALESCE(ch.statute_raw, '') LIKE ?
                OR COALESCE(ch.offense_description, '') LIKE ?
                OR COALESCE(ch.degree, '') LIKE ?
            )
            """
        )
        like = f"%{q}%"
        params.extend([like, like, like])

    status = clean_text(filters.get("status"))
    if status:
        clauses.append("c.status = ?")
        params.append(status)

    filed_from = clean_text(filters.get("filed_from"))
    if filed_from:
        clauses.append("date(c.file_date) >= date(?)")
        params.append(filed_from)

    filed_to = clean_text(filters.get("filed_to"))
    if filed_to:
        clauses.append("date(c.file_date) <= date(?)")
        params.append(filed_to)

    department = clean_text(filters.get("department"))
    if department:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM calendar_appearances ca
                WHERE ca.cap_case_id = c.cap_case_id
                  AND ca.courtroom_text = ?
            )
            """
        )
        params.append(department)

    hearing_type = clean_text(filters.get("hearing_type"))
    if hearing_type:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM case_hearings h
                WHERE h.cap_case_id = c.cap_case_id
                  AND h.hearing_type_text = ?
            )
            """
        )
        params.append(hearing_type)

    charge_code = clean_text(filters.get("charge_code"))
    if charge_code:
        clauses.append("strip_charge_suffix(ch.statute_raw) = ?")
        params.append(charge_code)

    if bool_param(filters.get("new_only", "")):
        clauses.append(
            """
            date(COALESCE(NULLIF(substr(c.first_seen_at, 1, 10), ''), NULLIF(c.file_date, ''), '1900-01-01'))
            >= date('now', ?)
            """
        )
        params.append(f"-{NEW_CASE_WINDOW_DAYS} day")

    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def query_charge_report(conn, filters, include_all=False):
    page = parse_int(filters.get("page"), default=1)
    offset = (page - 1) * REPORT_ROWS_PER_PAGE
    where_sql, params = build_charge_report_where(filters)
    sort_key = clean_text(filters.get("sort", "charge_count")) or "charge_count"
    sort_dir = clean_text(filters.get("sort_dir", "desc")).lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"

    sort_expr = {
        "charge_code": "charge_code",
        "charge_count": "charge_count",
        "case_count": "case_count",
        "felony_count": "felony_count",
        "misdemeanor_count": "misdemeanor_count",
        "infraction_count": "infraction_count",
        "sample_description": "sample_description",
    }.get(sort_key, "charge_count")
    sort_sql = f"{sort_expr} {sort_dir.upper()}, charge_code ASC"

    total = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT COALESCE(NULLIF(strip_charge_suffix(ch.statute_raw), ''), '(missing statute)') AS charge_code
            FROM case_charges ch
            JOIN cases c ON c.cap_case_id = ch.cap_case_id
            {where_sql}
            GROUP BY charge_code
        )
        """,
        params,
    ).fetchone()[0]

    limit_sql = "" if include_all else "LIMIT ? OFFSET ?"
    final_params = list(params)
    if not include_all:
        final_params.extend([REPORT_ROWS_PER_PAGE, offset])

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(NULLIF(strip_charge_suffix(ch.statute_raw), ''), '(missing statute)') AS charge_code,
            COUNT(*) AS charge_count,
            COUNT(DISTINCT ch.cap_case_id) AS case_count,
            SUM(CASE WHEN UPPER(COALESCE(ch.severity_code, '')) = 'F' THEN 1 ELSE 0 END) AS felony_count,
            SUM(CASE WHEN UPPER(COALESCE(ch.severity_code, '')) = 'M' THEN 1 ELSE 0 END) AS misdemeanor_count,
            SUM(CASE WHEN UPPER(COALESCE(ch.severity_code, '')) IN ('I', 'INF') THEN 1 ELSE 0 END) AS infraction_count,
            MIN(COALESCE(NULLIF(ch.offense_description, ''), '')) AS sample_description
        FROM case_charges ch
        JOIN cases c ON c.cap_case_id = ch.cap_case_id
        {where_sql}
        GROUP BY charge_code
        ORDER BY {sort_sql}
        {limit_sql}
        """,
        final_params,
    ).fetchall()

    totals = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_charge_rows,
            COUNT(DISTINCT ch.cap_case_id) AS total_cases_seen
        FROM case_charges ch
        JOIN cases c ON c.cap_case_id = ch.cap_case_id
        {where_sql}
        """,
        params,
    ).fetchone()

    total_pages = max(1, (total + REPORT_ROWS_PER_PAGE - 1) // REPORT_ROWS_PER_PAGE) if not include_all else 1
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "totals": totals,
    }


def render_person(environ, start_response, name):
    conn = db_connect()
    try:
        person = get_person_summary(conn, name)
        if not person:
            return response(start_response, "Person not found", status="404 Not Found", content_type="text/plain; charset=utf-8")

        nav_context = get_nav_context(conn)
        availability = get_data_availability(conn)
        global_status = build_global_status(conn)

        body = render_template(
            "court_browser_person.html",
            page_title=f"Person: {person['name']}",
            active_nav="search",
            person=person,
            nav_context=nav_context,
            availability=availability,
            global_status=global_status,
            replace_query=replace_query,
            case_url=case_url,
            person_url=person_url,
            property_url=property_url,
            strip_charge_suffix=strip_charge_suffix,
            clean_charge_description=clean_charge_description,
        )
        return response(start_response, body)
    finally:
        conn.close()


def render_property(environ, start_response, apn):
    conn = db_connect()
    try:
        prop = get_property_data(conn, apn)
        if not prop:
            return response(start_response, "Property not found", status="404 Not Found", content_type="text/plain; charset=utf-8")

        nav_context = get_nav_context(conn)
        availability = get_data_availability(conn)
        global_status = build_global_status(conn)

        body = render_template(
            "court_browser_property.html",
            page_title=f"Property {apn}",
            active_nav="search",
            prop=prop,
            nav_context=nav_context,
            availability=availability,
            global_status=global_status,
            replace_query=replace_query,
            case_url=case_url,
            person_url=person_url,
            property_url=property_url,
            strip_charge_suffix=strip_charge_suffix,
            clean_charge_description=clean_charge_description,
        )
        return response(start_response, body)
    finally:
        conn.close()


def render_charge_report_csv(conn, filters, start_response):
    report = query_charge_report(conn, filters, include_all=True)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "charge_code",
            "charge_count",
            "case_count",
            "felony_count",
            "misdemeanor_count",
            "infraction_count",
            "sample_description",
        ]
    )

    for row in report["rows"]:
        writer.writerow(
            [
                row["charge_code"],
                row["charge_count"],
                row["case_count"],
                row["felony_count"],
                row["misdemeanor_count"],
                row["infraction_count"],
                row["sample_description"],
            ]
        )

    payload = out.getvalue().encode("utf-8")
    start_response(
        "200 OK",
        [
            ("Content-Type", "text/csv; charset=utf-8"),
            ("Content-Disposition", 'attachment; filename="charge_report.csv"'),
            ("Content-Length", str(len(payload))),
        ],
    )
    return [payload]


def render_charge_report(environ, start_response):
    params = parse_request(environ)
    sort_key = clean_text(params.get("sort", "charge_count")) or "charge_count"
    filters = {
        "q": clean_text(params.get("q", "")),
        "status": clean_text(params.get("status", "")),
        "department": clean_text(params.get("department", "")),
        "hearing_type": clean_text(params.get("hearing_type", "")),
        "charge_code": clean_text(params.get("charge_code", "")),
        "filed_from": clean_text(params.get("filed_from", "")),
        "filed_to": clean_text(params.get("filed_to", "")),
        "new_only": "1" if bool_param(params.get("new_only", "")) else "",
        "sort": sort_key,
        "sort_dir": clean_text(params.get("sort_dir", "")) or default_report_sort_dir(sort_key),
        "page": str(parse_int(params.get("page"), 1)),
    }

    conn = db_connect()
    try:
        if params.get("download") == "csv":
            return render_charge_report_csv(conn, filters, start_response)

        report = query_charge_report(conn, filters)
        filter_options = get_filter_options(conn)
        nav_context = get_nav_context(conn)
        availability = get_data_availability(conn)
        global_status = build_global_status(conn)
        demographic_breakdowns = {
            "sex": query_top_charge_by_demographic(conn, filters, "sex") if availability["has_demographics"] else [],
            "race": query_top_charge_by_demographic(conn, filters, "race") if availability["has_demographics"] else [],
            "age_group": query_top_charge_by_demographic(conn, filters, "age_group") if availability["has_demographics"] else [],
        }

        body = render_template(
            "court_browser_charge_report.html",
            page_title="Charge Report",
            active_nav="reports",
            filters=filters,
            params=params,
            flash=clean_text(params.get("flash", "")),
            flash_state=clean_text(params.get("flash_state", "")) or "success",
            report=report,
            filter_options=filter_options,
            nav_context=nav_context,
            availability=availability,
            global_status=global_status,
            demographic_breakdowns=demographic_breakdowns,
            replace_query=replace_query,
            next_sort_dir=next_sort_dir,
            sort_indicator=sort_indicator,
            strip_charge_suffix=strip_charge_suffix,
            clean_charge_description=clean_charge_description,
            case_url=case_url,
        )
        return response(start_response, body)
    finally:
        conn.close()


def launch_demographics_backfill(environ, start_response):
    conn = db_connect()
    try:
        launch = spawn_background_job(
            conn,
            job_key="lcn_demographics_backfill",
            job_type="lcn_backfill",
            label="Demographics / LCN backfill",
            command=[sys.executable, str(LCN_SCRAPE_SCRIPT), "--db", str(DB_PATH), "--mode", "backfill-live"],
            interactive=False,
        )
        return redirect_with_message(
            start_response,
            "/reports/charges",
            "Demographics backfill started in the background." if launch["started"] else launch["message"],
            state="success" if launch["started"] else "error",
        )
    finally:
        conn.close()


def render_logs(environ, start_response):
    params = parse_request(environ)
    status_filter = clean_text(params.get("status", ""))
    selected_job_key = clean_text(params.get("job", ""))
    selected_path_value = clean_text(params.get("path", ""))

    conn = db_connect()
    try:
        nav_context = get_nav_context(conn)
        availability = get_data_availability(conn)
        global_status = build_global_status(conn)
        jobs = browser_jobs_for_logs(conn, status=status_filter, limit=60)

        summary_rows = []
        if table_exists(conn, "browser_jobs"):
            summary_rows = conn.execute(
                """
                SELECT LOWER(COALESCE(status, '')) AS status, COUNT(*) AS n
                FROM browser_jobs
                GROUP BY LOWER(COALESCE(status, ''))
                """
            ).fetchall()

        job_totals = {"total": 0, "running": 0, "error": 0, "complete": 0}
        for row in summary_rows:
            count = int(row["n"] or 0)
            status = clean_text(row["status"]).lower()
            job_totals["total"] += count
            if status in job_totals:
                job_totals[status] += count

        selected_job = get_browser_job(conn, selected_job_key) if selected_job_key else None
        if selected_job is None and jobs:
            selected_job = jobs[0]

        selected_log_path = safe_log_path(selected_path_value)
        if selected_job is not None and selected_log_path is None:
            selected_log_path = safe_log_path(row_text(selected_job, "log_path"))

        log_files = recent_log_files(limit=30)
        if selected_log_path is None and log_files:
            selected_log_path = safe_log_path(log_files[0]["path"])

        selected_log_text = read_log_tail(selected_log_path) if selected_log_path else "No logs found yet."

        body = render_template(
            "court_browser_logs.html",
            page_title="Logs",
            active_nav="logs",
            nav_context=nav_context,
            availability=availability,
            global_status=global_status,
            jobs=jobs,
            job_totals=job_totals,
            status_filter=status_filter,
            selected_job=selected_job,
            selected_log_path=str(selected_log_path) if selected_log_path else "",
            selected_log_text=selected_log_text,
            log_files=log_files,
            replace_query=replace_query,
            urlencode=urlencode,
            case_url=case_url,
        )
        return response(start_response, body)
    finally:
        conn.close()


def launch_jail_import(environ, start_response):
    params = parse_request(environ)
    selected_case_id = clean_text(params.get("case", ""))
    return_to = clean_text(params.get("return_to", ""))
    case_tab = clean_case_tab(params.get("case_tab", "jail-info"))

    conn = db_connect()
    try:
        detail = enrich_case_detail(conn, get_case_detail(conn, selected_case_id)) if selected_case_id else None
        base_prefill = build_case_jail_prefill(detail) if detail else build_empty_jail_prefill()
        prefill = merge_case_prefill(base_prefill, params)
        if not any(prefill[key] for key in ("booking", "first_name", "last_name", "middle_name", "dob", "age", "gender")):
            target = case_tab_url(selected_case_id, case_tab) if return_to == "case" and selected_case_id else "/jail-import"
            if target == "/jail-import":
                redirect_params = {"launch": "error", "message": "No booking or defendant search fields were available to launch."}
                if selected_case_id:
                    redirect_params["case"] = selected_case_id
                return redirect(start_response, f"/jail-import?{urlencode(redirect_params)}")
            return redirect_with_message(start_response, target, "No booking or defendant search fields were available to launch.", state="error")

        if not JAIL_CAPTURE_SCRIPT.exists():
            raise FileNotFoundError(f"{JAIL_CAPTURE_SCRIPT} is missing.")

        command = build_manual_jail_capture_commands(prefill)["launch"]
        case_number = prefill.get("case_number") or (row_text(detail["overview"], "case_number") if detail else "")
        launch = spawn_background_job(
            conn,
            job_key=f"jail_capture:{selected_case_id or 'manual'}",
            job_type="jail_capture",
            label=f"Jail capture for {case_number or 'manual lookup'}",
            command=command,
            target_case_id=selected_case_id,
            target_case_number=case_number,
            interactive=True,
        )

        if return_to == "case" and selected_case_id:
            return redirect_with_message(
                start_response,
                case_tab_url(selected_case_id, case_tab),
                "Jail helper launched. This page will refresh and show the captured result as soon as it lands." if launch["started"] else launch["message"],
                state="success" if launch["started"] else "error",
            )

        redirect_params = {}
        if selected_case_id:
            redirect_params["case"] = selected_case_id
        redirect_params.update(
            {
                "booking": prefill.get("booking", ""),
                "last": prefill.get("last_name", ""),
                "first": prefill.get("first_name", ""),
                "middle": prefill.get("middle_name", ""),
                "dob": prefill.get("dob", ""),
                "age": prefill.get("age", ""),
                "gender": prefill.get("gender", ""),
                "launch": "ok" if launch["started"] else "error",
                "message": "Jail helper launched. This page will refresh and show the captured result as soon as it lands." if launch["started"] else launch["message"],
            }
        )
        return redirect(start_response, f"/jail-import?{urlencode(redirect_params)}")
    except Exception as exc:
        target = case_tab_url(selected_case_id, case_tab) if return_to == "case" and selected_case_id else "/jail-import"
        return redirect_with_message(start_response, target, f"Launch failed: {exc}", state="error")
    finally:
        conn.close()


def render_jail_import(environ, start_response):
    params = parse_request(environ)
    selected_case_id = clean_text(params.get("case", ""))
    conn = db_connect()
    try:
        detail = enrich_case_detail(conn, get_case_detail(conn, selected_case_id)) if selected_case_id else None
        base_prefill = build_case_jail_prefill(detail) if detail else build_empty_jail_prefill()
        if detail:
            detail["jail_prefill"] = base_prefill
        jail_prefill = merge_case_prefill(base_prefill, params)
        command = build_manual_jail_capture_commands(jail_prefill)
        nav_context = get_nav_context(conn)
        availability = get_data_availability(conn)
        global_status = build_global_status(conn, target_case_id=selected_case_id)
        body = render_template(
            "court_browser_jail_import.html",
            page_title="Jail Import",
            active_nav="jail-import",
            params=params,
            selected_case_id=selected_case_id,
            detail=detail,
            jail_prefill=jail_prefill,
            jail_command=command["display"],
            official_jail_url=JAIL_INMATE_LOCATOR_URL,
            thats_them_url=build_thats_them_search_url(jail_prefill),
            launch_state=clean_text(params.get("launch", "")),
            launch_message=clean_text(params.get("message", "")),
            nav_context=nav_context,
            availability=availability,
            global_status=global_status,
            replace_query=replace_query,
            case_url=case_url,
        )
        return response(start_response, body)
    finally:
        conn.close()


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")

    if path.startswith("/static/"):
        relative = path[len("/static/") :]
        return serve_static(start_response, relative)

    if path.startswith("/case/") and path.endswith("/action"):
        case_id = path[len("/case/") : -len("/action")]
        return handle_case_action(environ, start_response, case_id)

    if path.startswith("/case/"):
        case_id = path[len("/case/") :]
        return render_case_detail(environ, start_response, case_id)

    if path.startswith("/person/"):
        name = path[len("/person/"):]
        return render_person(environ, start_response, name)

    if path.startswith("/property/"):
        apn = path[len("/property/"):]
        return render_property(environ, start_response, apn)

    if path == "/":
        return render_browser(environ, start_response)

    if path == "/reports/charges":
        return render_charge_report(environ, start_response)

    if path == "/reports/demographics/continue":
        return launch_demographics_backfill(environ, start_response)

    if path == "/logs":
        return render_logs(environ, start_response)

    if path == "/jail-import/launch":
        return launch_jail_import(environ, start_response)

    if path == "/jail-import":
        return render_jail_import(environ, start_response)

    if path == "/favicon.ico":
        return response(start_response, b"", content_type="image/x-icon")

    return response(start_response, "Not found", status="404 Not Found", content_type="text/plain; charset=utf-8")


def main():
    parser = argparse.ArgumentParser(description="Local browser for Victorville court scraper data.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. Default: 8765")
    args = parser.parse_args()

    acquired, owner = acquire_instance_lock(args.host, args.port)
    if not acquired:
        message = "Victorville Court Browser is already running."
        if owner:
            message += f" Existing PID: {owner}."
        print(message, file=sys.stderr)
        return 1

    with db_connect() as conn:
        ensure_browser_search_index(conn)

    print(f"Victorville Court Browser running at http://{args.host}:{args.port}")
    print(f"SQLite DB: {DB_PATH}")
    print("Press Ctrl+C to stop.")

    with make_server(args.host, args.port, application) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
