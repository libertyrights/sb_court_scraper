import argparse
import asyncio
import getpass
import hashlib
import json
import os
import queue
import random
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

if load_dotenv:
    load_dotenv(ENV_PATH)

BASE_SITE = "https://cap.sb-court.org"
LOGIN_URL = "https://cap.sb-court.org/login"
BASE_CALENDAR_URL = "https://cap.sb-court.org/calendar/Victorville/Victorville"

LOCATION = "Victorville"
LOCATION_PATH = "Victorville/Victorville"

PROFILE_DIR = SCRIPT_DIR / ".cap_profile"
STATE_DIR = SCRIPT_DIR / "state"
OUT_DIR = SCRIPT_DIR / "output"
DB_PATH = STATE_DIR / "court_calendar.db"
LCN_SCRIPT_PATH = SCRIPT_DIR / "lcn_scrape.py"

SCRAPER_VERSION = "2026-06-27-normalized-v1"

DEPT_BUTTON_SELECTOR = 'button[ng-repeat="dept in vm.depts"]'

CASE_CATEGORY_VALUES = {
    "Any": "string:Any",
    "Appellate": "string:AP",
    "Criminal": "string:CR",
    "Civil": "string:CV",
    "Family": "string:FAM",
    "Probate": "string:PR",
    "Probate or Mental Health": "string:PR",
}

API_CATEGORY_CANDIDATES = {
    "Any": ["Any"],
    "Appellate": ["Appellate", "AP"],
    "Criminal": ["Criminal", "CR"],
    "Civil": ["Civil", "CV"],
    "Family": ["Family", "FAM"],
    "Probate": ["Probate", "PR"],
    "Probate or Mental Health": ["Probate", "PR"],
}

DETAIL_TABLES = [
    "case_parties",
    "case_attorneys",
    "case_events",
    "case_hearings",
    "case_hearing_documents",
    "case_charges",
    "case_charge_dispositions",
    "case_charge_sentences",
    "case_documents",
    "case_aliases",
    "case_demographics",
    "case_party_addresses",
    "case_party_identifiers",
    "case_arrests",
    "case_flags",
    "case_warrants",
    "case_bonds",
    "case_financials",
    "case_financial_transactions",
    "case_cross_reference_numbers",
    "related_cases",
    "case_link_candidates",
]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_string(value):
    value = clean_text(value).upper()
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value):
    value = clean_text(value).upper()
    value = re.sub(r"\b(JR|SR|II|III|IV)\b\.?", "", value)
    value = re.sub(r"[^A-Z0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def bool_int(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if value in (1, "1", "true", "True", "TRUE", "yes", "YES"):
        return 1
    return 0


def json_hash(obj):
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def safe_filename(value):
    value = clean_text(value) or "record"
    value = re.sub(r"[^\w\-\. ]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:150] or "record"


def parse_date_arg(value):
    value = clean_text(value)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Could not parse date: {value}")


def cap_api_date(d):
    return d.strftime("%m-%d-%Y")


def cap_display_date(d):
    return d.strftime("%m/%d/%Y")


def db_date(value):
    value = clean_text(value)
    if not value:
        return ""

    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass

    return value


def disposition_date_pair(value):
    raw = clean_text(value)
    normalized = db_date(raw)

    if normalized in {"1900-01-01", "1/1/1900"}:
        return raw, ""

    return raw, normalized


def get_value(d, *keys, default=""):
    if not isinstance(d, dict):
        return default
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def split_alias_string(raw):
    raw = clean_text(raw)
    if not raw:
        return []

    parts = re.split(r"\s+-\s+| -|;|\|", raw)
    out = []

    for part in parts:
        part = clean_text(part)
        if part:
            out.append(part)

    return list(dict.fromkeys(out))


def parse_charge_parts(offense_description):
    """
    CAP example:
      VC20001(B)(1)-F: Hit and Run Resulting in Death or Injury
      PC422(A)-F: Criminal Threats which will Result in Death or GBI
    """
    raw = clean_text(offense_description)
    statute_raw = ""
    desc = raw

    if ":" in raw:
        left, right = raw.split(":", 1)
        statute_raw = clean_text(left)
        desc = clean_text(right)
    else:
        statute_raw = raw

    statute_prefix = ""
    statute_number = ""
    statute_suffix = ""
    severity_code = ""

    m = re.match(
        r"^\s*([A-Z]{1,4})\s*([0-9][0-9A-Z.]*)\s*([^-\s:]*)\s*(?:-\s*([A-Z]))?",
        statute_raw,
        flags=re.I,
    )

    if m:
        statute_prefix = clean_text(m.group(1)).upper()
        statute_number = clean_text(m.group(2)).upper()
        statute_suffix = clean_text(m.group(3)).upper()
        severity_code = clean_text(m.group(4)).upper()

    normalized_charge_text = normalize_string(raw)

    return {
        "statute_raw": statute_raw,
        "statute_prefix": statute_prefix,
        "statute_number": statute_number,
        "statute_suffix": statute_suffix,
        "severity_code": severity_code,
        "normalized_charge_text": normalized_charge_text,
        "charge_description_only": desc,
    }


def make_appearance_uid(row, calendar_date, category):
    parts = [
        clean_text(get_value(row, "caseId")),
        clean_text(get_value(row, "caseNbr")),
        clean_text(get_value(row, "sessionDate")),
        clean_text(get_value(row, "sessionStartTime")),
        clean_text(get_value(row, "courtroom")),
        clean_text(get_value(row, "hearingType")),
        clean_text(calendar_date),
        clean_text(category),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()


def make_attorney_uid(cap_case_id, attorney):
    parts = [
        clean_text(cap_case_id),
        clean_text(get_value(attorney, "casePartyID", "casePartyId")),
        clean_text(get_value(attorney, "barNumber")),
        clean_text(get_value(attorney, "firstName")),
        clean_text(get_value(attorney, "middleName")),
        clean_text(get_value(attorney, "lastName")),
        clean_text(get_value(attorney, "representing")),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


SCHEMA = {
    "runs": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "status": "TEXT",
        "category": "TEXT",
        "date_start": "TEXT",
        "date_end": "TEXT",
        "notes": "TEXT",
    },
    "scan_progress": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "calendar_date": "TEXT",
        "category": "TEXT",
        "department": "TEXT",
        "completed_at": "TEXT",
        "row_count": "INTEGER DEFAULT 0",
        "detail_count": "INTEGER DEFAULT 0",
        "notes": "TEXT",
    },
    "linked_entities": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "entity_type": "TEXT",
        "source_key": "TEXT",
        "linked_string": "TEXT",
        "normalized_string": "TEXT",
        "source_system": "TEXT DEFAULT 'CAP'",
        "first_seen_at": "TEXT",
        "latest_seen_at": "TEXT",
    },
    "cases": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "case_category": "TEXT",
        "case_category_id": "TEXT",
        "case_type": "TEXT",
        "type_id": "TEXT",
        "case_sub_type": "TEXT",
        "style": "TEXT",
        "file_date": "TEXT",
        "status": "TEXT",
        "court_location": "TEXT",
        "court_location_entity_id": "INTEGER",
        "assigned_judicial_officer_text": "TEXT",
        "assigned_judicial_officer_entity_id": "INTEGER",
        "next_hearing": "TEXT",
        "node_id": "TEXT",
        "citation_number": "TEXT",
        "security_group": "TEXT",
        "permission": "TEXT",
        "is_secured_access": "INTEGER",
        "allow_favorites": "INTEGER",
        "is_criminal": "INTEGER",
        "source_hash": "TEXT",
        "first_seen_at": "TEXT",
        "latest_seen_at": "TEXT",
        "detail_scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "calendar_appearances": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "appearance_uid": "TEXT",
        "run_id": "INTEGER",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "case_name": "TEXT",
        "style": "TEXT",
        "calendar_date": "TEXT",
        "courtroom_code": "TEXT",
        "courtroom_text": "TEXT",
        "courtroom_entity_id": "INTEGER",
        "courtroom_desc": "TEXT",
        "session_date": "TEXT",
        "session_start_time": "TEXT",
        "hearing_type_text": "TEXT",
        "hearing_type_entity_id": "INTEGER",
        "case_type_text": "TEXT",
        "case_type_entity_id": "INTEGER",
        "judicial_officer_text": "TEXT",
        "judicial_officer_entity_id": "INTEGER",
        "attorney_text": "TEXT",
        "attorney_entity_id": "INTEGER",
        "attorney_first_name": "TEXT",
        "attorney_middle_name": "TEXT",
        "attorney_last_name": "TEXT",
        "bar_number": "TEXT",
        "judge_id": "TEXT",
        "node_id": "TEXT",
        "source_endpoint": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_parties": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "case_party_id": "TEXT",
        "party_entity_id": "INTEGER",
        "party_type": "TEXT",
        "first_name": "TEXT",
        "middle_name": "TEXT",
        "last_name": "TEXT",
        "nickname": "TEXT",
        "business_name": "TEXT",
        "full_name": "TEXT",
        "is_defendant": "INTEGER",
        "connection_type": "TEXT",
        "inactive": "INTEGER",
        "aliases_raw": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_attorneys": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "case_attorney_uid": "TEXT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "attorney_entity_id": "INTEGER",
        "case_party_id": "TEXT",
        "representing_party_entity_id": "INTEGER",
        "representing_text": "TEXT",
        "first_name": "TEXT",
        "middle_name": "TEXT",
        "last_name": "TEXT",
        "full_name": "TEXT",
        "bar_number": "TEXT",
        "is_lead": "INTEGER",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_events": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "event_id": "TEXT",
        "event_date": "TEXT",
        "event_type": "TEXT",
        "event_type_entity_id": "INTEGER",
        "filed_by": "TEXT",
        "comment": "TEXT",
        "event_type_id": "TEXT",
        "document_id": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_hearings": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "hearing_id": "TEXT",
        "calendar_text": "TEXT",
        "calendar_entity_id": "INTEGER",
        "courtroom_entity_id": "INTEGER",
        "hearing_type_text": "TEXT",
        "hearing_type_entity_id": "INTEGER",
        "hearing_date": "TEXT",
        "hearing_time": "TEXT",
        "hearing_result": "TEXT",
        "document_id": "TEXT",
        "interpreter": "TEXT",
        "hearing_flag": "TEXT",
        "judge_text": "TEXT",
        "judge_entity_id": "INTEGER",
        "court_reporter": "TEXT",
        "cancel_reason": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_hearing_documents": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "hearing_id": "TEXT",
        "document_id": "TEXT",
        "document_name": "TEXT",
        "document_entity_id": "INTEGER",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_charges": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "charge_id": "TEXT",
        "charge_number": "TEXT",
        "offense_date": "TEXT",
        "degree": "TEXT",
        "offense_description": "TEXT",
        "statute_raw": "TEXT",
        "statute_prefix": "TEXT",
        "statute_number": "TEXT",
        "statute_suffix": "TEXT",
        "severity_code": "TEXT",
        "normalized_charge_text": "TEXT",
        "disposition_date_raw": "TEXT",
        "disposition_date": "TEXT",
        "disposition_desc": "TEXT",
        "disposition_code": "TEXT",
        "security_group": "TEXT",
        "plea": "TEXT",
        "plea_date": "TEXT",
        "jurisdiction": "TEXT",
        "jurisdiction_entity_id": "INTEGER",
        "citation_number": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_charge_dispositions": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "charge_id": "TEXT",
        "disposition_id": "TEXT",
        "disposition_date_raw": "TEXT",
        "disposition_date": "TEXT",
        "disposition_desc": "TEXT",
        "disposition_code": "TEXT",
        "result_text": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_charge_sentences": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "charge_id": "TEXT",
        "sentence_id": "TEXT",
        "sentence_date": "TEXT",
        "sentence_type": "TEXT",
        "sentence_text": "TEXT",
        "amount_text": "TEXT",
        "term_text": "TEXT",
        "status": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_documents": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "doc_id": "TEXT",
        "document_version_id": "TEXT",
        "document_type_id": "TEXT",
        "name": "TEXT",
        "doc_type": "TEXT",
        "doc_type_entity_id": "INTEGER",
        "page_count": "TEXT",
        "effective_date": "TEXT",
        "doc_security": "TEXT",
        "document_security_group": "TEXT",
        "document_fee_type": "TEXT",
        "document_fee_amount": "REAL",
        "total_fee": "REAL",
        "require_purchase": "INTEGER",
        "purchase_enabled": "INTEGER",
        "purchased": "INTEGER",
        "saved": "INTEGER",
        "is_preview": "INTEGER",
        "show_cart_options": "INTEGER",
        "require_login": "INTEGER",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_aliases": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "alias_id": "TEXT",
        "party_entity_id": "INTEGER",
        "case_party_id": "TEXT",
        "full_name": "TEXT",
        "normalized_name": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_demographics": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "party_entity_id": "INTEGER",
        "case_party_id": "TEXT",
        "race": "TEXT",
        "sex": "TEXT",
        "date_of_birth": "TEXT",
        "date_of_birth_text": "TEXT",
        "height": "TEXT",
        "weight": "TEXT",
        "hair_color": "TEXT",
        "eye_color": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_party_addresses": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "party_entity_id": "INTEGER",
        "case_party_id": "TEXT",
        "street_name": "TEXT",
        "city": "TEXT",
        "state": "TEXT",
        "zip": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_party_identifiers": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "party_entity_id": "INTEGER",
        "case_party_id": "TEXT",
        "identifier_type": "TEXT",
        "identifier_id": "TEXT",
        "identifier_value": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_arrests": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "party_entity_id": "INTEGER",
        "case_party_id": "TEXT",
        "arrest_id": "TEXT",
        "arrest_date": "TEXT",
        "arrest_time": "TEXT",
        "arrest_datetime": "TEXT",
        "arresting_agency": "TEXT",
        "arresting_agency_entity_id": "INTEGER",
        "arrest_location": "TEXT",
        "booking_number": "TEXT",
        "arrest_report_number": "TEXT",
        "citation_number": "TEXT",
        "source_field_seen": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_flags": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "flag_id": "TEXT",
        "flag_type": "TEXT",
        "flag_text": "TEXT",
        "status": "TEXT",
        "effective_date": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_warrants": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "warrant_id": "TEXT",
        "warrant_type": "TEXT",
        "warrant_status": "TEXT",
        "issue_date": "TEXT",
        "recall_date": "TEXT",
        "bail_amount": "TEXT",
        "notes": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_bonds": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "bond_id": "TEXT",
        "bond_type": "TEXT",
        "bond_status": "TEXT",
        "bond_amount": "TEXT",
        "posted_date": "TEXT",
        "forfeited_date": "TEXT",
        "notes": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_financials": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "financial_id": "TEXT",
        "financial_type": "TEXT",
        "description": "TEXT",
        "amount": "REAL",
        "balance": "REAL",
        "due_date": "TEXT",
        "status": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_financial_transactions": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "transaction_id": "TEXT",
        "transaction_date": "TEXT",
        "transaction_type": "TEXT",
        "description": "TEXT",
        "amount": "REAL",
        "receipt_number": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_cross_reference_numbers": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "cross_reference_id": "TEXT",
        "reference_type": "TEXT",
        "reference_number": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "related_cases": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "related_cap_case_id": "TEXT",
        "related_case_number": "TEXT",
        "relationship_type": "TEXT",
        "relationship_text": "TEXT",
        "source_hash": "TEXT",
        "scraped_at": "TEXT",
        "scraper_version": "TEXT",
    },
    "case_link_candidates": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "party_entity_id": "INTEGER",
        "candidate_type": "TEXT",
        "candidate_value": "TEXT",
        "confidence": "TEXT",
        "match_basis": "TEXT",
        "source_system": "TEXT",
        "created_at": "TEXT",
    },
    "lcn_lookup_status": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "party_entity_id": "TEXT",
        "defendant_name": "TEXT",
        "alias_csv": "TEXT",
        "charge_summary": "TEXT",
        "target_date": "TEXT",
        "target_date_source": "TEXT",
        "lcn_checked": "INTEGER DEFAULT 0",
        "check_count": "INTEGER DEFAULT 0",
        "max_checks": "INTEGER DEFAULT 3",
        "status": "TEXT DEFAULT 'pending'",
        "first_checked_at": "TEXT",
        "last_checked_at": "TEXT",
        "next_check_after": "TEXT",
        "last_error": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    },
    "case_details_debug_dumps": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "cap_case_id": "TEXT",
        "case_number": "TEXT",
        "api_url": "TEXT",
        "dump_path": "TEXT",
        "html_dump_path": "TEXT",
        "source_hash": "TEXT",
        "created_at": "TEXT",
        "notes": "TEXT",
    },
}


INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cases_cap_case_id ON cases(cap_case_id) WHERE cap_case_id IS NOT NULL AND cap_case_id <> ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_calendar_appearance_uid ON calendar_appearances(appearance_uid) WHERE appearance_uid IS NOT NULL AND appearance_uid <> ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_entities_type_key ON linked_entities(entity_type, source_key, source_system)",
    "CREATE INDEX IF NOT EXISTS idx_calendar_date_dept ON calendar_appearances(session_date, courtroom_code)",
    "CREATE INDEX IF NOT EXISTS idx_parties_case_def ON case_parties(cap_case_id, is_defendant)",
    "CREATE INDEX IF NOT EXISTS idx_events_case_date ON case_events(cap_case_id, event_date)",
    "CREATE INDEX IF NOT EXISTS idx_hearings_case_date ON case_hearings(cap_case_id, hearing_date)",
    "CREATE INDEX IF NOT EXISTS idx_charges_case ON case_charges(cap_case_id)",
    "CREATE INDEX IF NOT EXISTS idx_charges_statute ON case_charges(statute_prefix, statute_number)",
    "CREATE INDEX IF NOT EXISTS idx_docs_case ON case_documents(cap_case_id)",
    "CREATE INDEX IF NOT EXISTS idx_aliases_case ON case_aliases(cap_case_id)",
    "CREATE INDEX IF NOT EXISTS idx_aliases_name ON case_aliases(normalized_name)",
    "CREATE INDEX IF NOT EXISTS idx_lcn_lookup_due ON lcn_lookup_status(status, next_check_after, check_count)",
]


def db_connect():
    STATE_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    ensure_schema(conn)
    return conn


def table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def column_exists(conn, table, column):
    if not table_exists(conn, table):
        return False
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return any(r[1] == column for r in rows)


def ensure_schema(conn):
    for table, cols in SCHEMA.items():
        col_sql = ",\n".join(f"{quote_ident(name)} {definition}" for name, definition in cols.items())
        conn.execute(f"CREATE TABLE IF NOT EXISTS {quote_ident(table)} ({col_sql})")

        for name, definition in cols.items():
            if name == "id":
                continue
            if not column_exists(conn, table, name):
                conn.execute(f"ALTER TABLE {quote_ident(table)} ADD COLUMN {quote_ident(name)} {definition}")

    for sql in INDEXES:
        try:
            conn.execute(sql)
        except sqlite3.Error as e:
            print(f"WARNING: could not create index: {e}")

    conn.commit()


def start_run(conn, args, dates):
    date_start = dates[0].isoformat() if dates else ""
    date_end = dates[-1].isoformat() if dates else ""

    conn.execute(
        """
        INSERT INTO runs (started_at, status, category, date_start, date_end, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            "running",
            args.category,
            date_start,
            date_end,
            "",
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def finish_run(conn, run_id, status, notes=""):
    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?, status = ?, notes = ?
        WHERE id = ?
        """,
        (now_iso(), status, notes, run_id),
    )
    conn.commit()


def progress_completed(conn, calendar_date, category, department):
    row = conn.execute(
        """
        SELECT completed_at
        FROM scan_progress
        WHERE calendar_date = ?
          AND category = ?
          AND department = ?
          AND completed_at IS NOT NULL
          AND completed_at <> ''
        ORDER BY id DESC
        LIMIT 1
        """,
        (calendar_date, category, department),
    ).fetchone()
    return row is not None


def mark_progress(conn, calendar_date, category, department, row_count, detail_count, notes=""):
    conn.execute(
        """
        INSERT INTO scan_progress (
            calendar_date,
            category,
            department,
            completed_at,
            row_count,
            detail_count,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            calendar_date,
            category,
            department,
            now_iso(),
            row_count,
            detail_count,
            notes,
        ),
    )
    conn.commit()


def upsert_entity(conn, entity_type, source_key, linked_string, source_system="CAP"):
    entity_type = clean_text(entity_type)
    source_key = clean_text(source_key)
    linked_string = clean_text(linked_string)

    if not source_key:
        source_key = linked_string

    if not source_key:
        return None

    now = now_iso()

    row = conn.execute(
        """
        SELECT id
        FROM linked_entities
        WHERE entity_type = ?
          AND source_key = ?
          AND source_system = ?
        """,
        (entity_type, source_key, source_system),
    ).fetchone()

    if row:
        entity_id = int(row["id"])
        conn.execute(
            """
            UPDATE linked_entities
            SET linked_string = COALESCE(NULLIF(?, ''), linked_string),
                normalized_string = COALESCE(NULLIF(?, ''), normalized_string),
                latest_seen_at = ?
            WHERE id = ?
            """,
            (
                linked_string,
                normalize_string(linked_string or source_key),
                now,
                entity_id,
            ),
        )
        return entity_id

    conn.execute(
        """
        INSERT INTO linked_entities (
            entity_type,
            source_key,
            linked_string,
            normalized_string,
            source_system,
            first_seen_at,
            latest_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            source_key,
            linked_string,
            normalize_string(linked_string or source_key),
            source_system,
            now,
            now,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def case_has_details(conn, cap_case_id):
    row = conn.execute(
        """
        SELECT detail_scraped_at
        FROM cases
        WHERE cap_case_id = ?
          AND detail_scraped_at IS NOT NULL
          AND detail_scraped_at <> ''
        """,
        (clean_text(cap_case_id),),
    ).fetchone()
    return row is not None


def clear_case_detail_rows(conn, cap_case_id):
    for table in DETAIL_TABLES:
        if table_exists(conn, table) and column_exists(conn, table, "cap_case_id"):
            conn.execute(f"DELETE FROM {quote_ident(table)} WHERE cap_case_id = ?", (cap_case_id,))


def upsert_case_from_calendar(conn, row):
    cap_case_id = clean_text(get_value(row, "caseId"))
    case_number = clean_text(get_value(row, "caseNbr"))
    style = clean_text(get_value(row, "style"))
    case_type = clean_text(get_value(row, "caseType"))

    if not cap_case_id and not case_number:
        return

    now = now_iso()

    existing = None
    if cap_case_id:
        existing = conn.execute(
            "SELECT id FROM cases WHERE cap_case_id = ?",
            (cap_case_id,),
        ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE cases
            SET case_number = COALESCE(NULLIF(?, ''), case_number),
                case_type = COALESCE(NULLIF(?, ''), case_type),
                style = COALESCE(NULLIF(?, ''), style),
                node_id = COALESCE(NULLIF(?, ''), node_id),
                latest_seen_at = ?,
                scraper_version = ?
            WHERE id = ?
            """,
            (
                case_number,
                case_type,
                style,
                clean_text(get_value(row, "nodeId")),
                now,
                SCRAPER_VERSION,
                existing["id"],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO cases (
                cap_case_id,
                case_number,
                case_type,
                style,
                node_id,
                first_seen_at,
                latest_seen_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                case_type,
                style,
                clean_text(get_value(row, "nodeId")),
                now,
                now,
                SCRAPER_VERSION,
            ),
        )


def insert_calendar_appearance(conn, run_id, calendar_date, category, api_endpoint, row):
    cap_case_id = clean_text(get_value(row, "caseId"))
    case_number = clean_text(get_value(row, "caseNbr"))
    courtroom_code = clean_text(get_value(row, "courtroom"))
    courtroom_desc = clean_text(get_value(row, "courtroomDesc"))
    courtroom_text = f"Dept {courtroom_code}" if courtroom_code else courtroom_desc
    judicial_officer = clean_text(get_value(row, "judicialOfficer"))
    judge_id = clean_text(get_value(row, "judgeID"))
    hearing_type = clean_text(get_value(row, "hearingType"))
    case_type = clean_text(get_value(row, "caseType"))

    atty_first = clean_text(get_value(row, "attyFirstName"))
    atty_middle = clean_text(get_value(row, "attyMiddleName"))
    atty_last = clean_text(get_value(row, "attyLastName"))
    bar_number = clean_text(get_value(row, "barNum"))
    attorney_text = clean_text(" ".join(x for x in [atty_first, atty_middle, atty_last] if x))

    courtroom_entity_id = upsert_entity(
        conn,
        "courtroom",
        f"courtroom:{courtroom_code}" if courtroom_code else courtroom_desc,
        courtroom_desc or courtroom_text,
    )

    judicial_officer_entity_id = upsert_entity(
        conn,
        "judge",
        f"judgeID:{judge_id}" if judge_id else judicial_officer,
        judicial_officer,
    )

    hearing_type_entity_id = upsert_entity(conn, "hearing_type", hearing_type, hearing_type)
    case_type_entity_id = upsert_entity(conn, "case_type", case_type, case_type)

    attorney_entity_id = None
    if attorney_text or bar_number:
        attorney_entity_id = upsert_entity(
            conn,
            "attorney",
            f"bar:{bar_number}" if bar_number else attorney_text,
            attorney_text,
        )

    appearance_uid = make_appearance_uid(row, calendar_date, category)

    conn.execute(
        "DELETE FROM calendar_appearances WHERE appearance_uid = ?",
        (appearance_uid,),
    )

    conn.execute(
        """
        INSERT INTO calendar_appearances (
            appearance_uid,
            run_id,
            cap_case_id,
            case_number,
            case_name,
            style,
            calendar_date,
            courtroom_code,
            courtroom_text,
            courtroom_entity_id,
            courtroom_desc,
            session_date,
            session_start_time,
            hearing_type_text,
            hearing_type_entity_id,
            case_type_text,
            case_type_entity_id,
            judicial_officer_text,
            judicial_officer_entity_id,
            attorney_text,
            attorney_entity_id,
            attorney_first_name,
            attorney_middle_name,
            attorney_last_name,
            bar_number,
            judge_id,
            node_id,
            source_endpoint,
            source_hash,
            scraped_at,
            scraper_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            appearance_uid,
            run_id,
            cap_case_id,
            case_number,
            clean_text(get_value(row, "caseName")),
            clean_text(get_value(row, "style")),
            calendar_date,
            courtroom_code,
            courtroom_text,
            courtroom_entity_id,
            courtroom_desc,
            db_date(get_value(row, "sessionDate")),
            clean_text(get_value(row, "sessionStartTime")),
            hearing_type,
            hearing_type_entity_id,
            case_type,
            case_type_entity_id,
            judicial_officer,
            judicial_officer_entity_id,
            attorney_text,
            attorney_entity_id,
            atty_first,
            atty_middle,
            atty_last,
            bar_number,
            judge_id,
            clean_text(get_value(row, "nodeId")),
            api_endpoint,
            json_hash(row),
            now_iso(),
            SCRAPER_VERSION,
        ),
    )

    upsert_case_from_calendar(conn, row)


def upsert_case_from_detail(conn, cap_case_id, api_url, api_json):
    data = api_json.get("data", {}) if isinstance(api_json, dict) else {}

    case_number = clean_text(get_value(data, "caseNumber", "caseNbr"))
    court_location = clean_text(get_value(data, "courtLocation"))
    judicial_officer = clean_text(get_value(data, "judicialOfficer"))

    court_location_entity_id = upsert_entity(conn, "court_location", court_location, court_location)
    judge_entity_id = upsert_entity(conn, "judge", judicial_officer, judicial_officer)

    now = now_iso()

    existing = conn.execute(
        "SELECT id FROM cases WHERE cap_case_id = ?",
        (cap_case_id,),
    ).fetchone()

    values = (
        cap_case_id,
        case_number,
        clean_text(get_value(data, "caseCategory")),
        clean_text(get_value(data, "type")),
        clean_text(get_value(data, "typeId")),
        clean_text(get_value(data, "caseSubType")),
        clean_text(get_value(data, "style")),
        db_date(get_value(data, "fileDate")),
        clean_text(get_value(data, "status")),
        court_location,
        court_location_entity_id,
        judicial_officer,
        judge_entity_id,
        clean_text(get_value(data, "nextHearing")),
        clean_text(get_value(data, "nodeId")),
        clean_text(get_value(data, "citationNumber")),
        clean_text(get_value(data, "securityGroup")),
        clean_text(get_value(data, "permission")),
        bool_int(get_value(data, "isSecuredAccess")),
        bool_int(get_value(data, "allowFavorites")),
        bool_int(get_value(data, "isCriminal")),
        json_hash(data),
        now,
        now,
        now,
        SCRAPER_VERSION,
    )

    if existing:
        conn.execute(
            """
            UPDATE cases
            SET case_number = COALESCE(NULLIF(?, ''), case_number),
                case_category_id = COALESCE(NULLIF(?, ''), case_category_id),
                case_type = COALESCE(NULLIF(?, ''), case_type),
                type_id = COALESCE(NULLIF(?, ''), type_id),
                case_sub_type = COALESCE(NULLIF(?, ''), case_sub_type),
                style = COALESCE(NULLIF(?, ''), style),
                file_date = COALESCE(NULLIF(?, ''), file_date),
                status = COALESCE(NULLIF(?, ''), status),
                court_location = COALESCE(NULLIF(?, ''), court_location),
                court_location_entity_id = COALESCE(?, court_location_entity_id),
                assigned_judicial_officer_text = COALESCE(NULLIF(?, ''), assigned_judicial_officer_text),
                assigned_judicial_officer_entity_id = COALESCE(?, assigned_judicial_officer_entity_id),
                next_hearing = COALESCE(NULLIF(?, ''), next_hearing),
                node_id = COALESCE(NULLIF(?, ''), node_id),
                citation_number = COALESCE(NULLIF(?, ''), citation_number),
                security_group = COALESCE(NULLIF(?, ''), security_group),
                permission = COALESCE(NULLIF(?, ''), permission),
                is_secured_access = ?,
                allow_favorites = ?,
                is_criminal = ?,
                source_hash = ?,
                latest_seen_at = ?,
                detail_scraped_at = ?,
                scraper_version = ?
            WHERE cap_case_id = ?
            """,
            (
                values[1],
                values[2],
                values[3],
                values[4],
                values[5],
                values[6],
                values[7],
                values[8],
                values[9],
                values[10],
                values[11],
                values[12],
                values[13],
                values[14],
                values[15],
                values[16],
                values[17],
                values[18],
                values[19],
                values[20],
                values[21],
                values[23],
                values[24],
                values[25],
                cap_case_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO cases (
                cap_case_id,
                case_number,
                case_category_id,
                case_type,
                type_id,
                case_sub_type,
                style,
                file_date,
                status,
                court_location,
                court_location_entity_id,
                assigned_judicial_officer_text,
                assigned_judicial_officer_entity_id,
                next_hearing,
                node_id,
                citation_number,
                security_group,
                permission,
                is_secured_access,
                allow_favorites,
                is_criminal,
                source_hash,
                first_seen_at,
                latest_seen_at,
                detail_scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    return case_number


def insert_case_detail(conn, cap_case_id, api_url, api_json, dump_json=False, dump_html_path=""):
    data = api_json.get("data", {}) if isinstance(api_json, dict) else {}
    if not isinstance(data, dict):
        return ""

    case_number = upsert_case_from_detail(conn, cap_case_id, api_url, api_json)

    clear_case_detail_rows(conn, cap_case_id)

    party_entity_by_case_party_id = {}
    party_entity_by_name = {}

    parties = data.get("caseParties") or []
    for party in parties:
        case_party_id = clean_text(get_value(party, "casePartyId"))
        full_name = clean_text(get_value(party, "fullName"))
        if not full_name:
            full_name = clean_text(" ".join(x for x in [
                get_value(party, "firstName"),
                get_value(party, "middleName"),
                get_value(party, "lastName"),
            ] if clean_text(x)))

        party_entity_id = upsert_entity(
            conn,
            "party",
            f"casePartyId:{case_party_id}" if case_party_id else full_name,
            full_name,
        )

        if case_party_id:
            party_entity_by_case_party_id[case_party_id] = party_entity_id
        if full_name:
            party_entity_by_name[normalize_name(full_name)] = party_entity_id

        aliases_raw = clean_text(get_value(party, "aliases"))

        conn.execute(
            """
            INSERT OR REPLACE INTO case_parties (
                cap_case_id,
                case_number,
                case_party_id,
                party_entity_id,
                party_type,
                first_name,
                middle_name,
                last_name,
                nickname,
                business_name,
                full_name,
                is_defendant,
                connection_type,
                inactive,
                aliases_raw,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                case_party_id,
                party_entity_id,
                clean_text(get_value(party, "type")),
                clean_text(get_value(party, "firstName")),
                clean_text(get_value(party, "middleName")),
                clean_text(get_value(party, "lastName")),
                clean_text(get_value(party, "nickName")),
                clean_text(get_value(party, "businessName")),
                full_name,
                bool_int(get_value(party, "isDefendant")),
                clean_text(get_value(party, "connectionType")),
                bool_int(get_value(party, "inactive")),
                aliases_raw,
                json_hash(party),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

        if aliases_raw:
            for alias_name in split_alias_string(aliases_raw):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO case_aliases (
                        cap_case_id,
                        case_number,
                        alias_id,
                        party_entity_id,
                        case_party_id,
                        full_name,
                        normalized_name,
                        source_hash,
                        scraped_at,
                        scraper_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cap_case_id,
                        case_number,
                        "",
                        party_entity_id,
                        case_party_id,
                        alias_name,
                        normalize_name(alias_name),
                        json_hash({"alias": alias_name, "source": "caseParties.aliases"}),
                        now_iso(),
                        SCRAPER_VERSION,
                    ),
                )

    aliases = data.get("casePartyAliases") or []
    for alias in aliases:
        alias_name = clean_text(get_value(alias, "fullName"))
        alias_id = clean_text(get_value(alias, "id"))

        if not alias_name:
            continue

        conn.execute(
            """
            INSERT OR IGNORE INTO case_aliases (
                cap_case_id,
                case_number,
                alias_id,
                party_entity_id,
                case_party_id,
                full_name,
                normalized_name,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                alias_id,
                None,
                "",
                alias_name,
                normalize_name(alias_name),
                json_hash(alias),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

    attorneys = data.get("caseAttornies") or data.get("caseAttorneys") or []
    for attorney in attorneys:
        first = clean_text(get_value(attorney, "firstName"))
        middle = clean_text(get_value(attorney, "middleName"))
        last = clean_text(get_value(attorney, "lastName"))
        full_name = clean_text(" ".join(x for x in [first, middle, last] if x))
        bar_number = clean_text(get_value(attorney, "barNumber"))
        representing_text = clean_text(get_value(attorney, "representing"))
        case_party_id = clean_text(get_value(attorney, "casePartyID", "casePartyId"))

        attorney_entity_id = upsert_entity(
            conn,
            "attorney",
            f"bar:{bar_number}" if bar_number else full_name,
            full_name,
        )

        representing_party_entity_id = (
            party_entity_by_case_party_id.get(case_party_id)
            or party_entity_by_name.get(normalize_name(representing_text))
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO case_attorneys (
                case_attorney_uid,
                cap_case_id,
                case_number,
                attorney_entity_id,
                case_party_id,
                representing_party_entity_id,
                representing_text,
                first_name,
                middle_name,
                last_name,
                full_name,
                bar_number,
                is_lead,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_attorney_uid(cap_case_id, attorney),
                cap_case_id,
                case_number,
                attorney_entity_id,
                case_party_id,
                representing_party_entity_id,
                representing_text,
                first,
                middle,
                last,
                full_name,
                bar_number,
                bool_int(get_value(attorney, "isLead")),
                json_hash(attorney),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

    events = data.get("caseEvents") or []
    for event in events:
        event_type = clean_text(get_value(event, "type"))
        event_type_entity_id = upsert_entity(conn, "event_type", event_type, event_type)

        conn.execute(
            """
            INSERT OR REPLACE INTO case_events (
                cap_case_id,
                case_number,
                event_id,
                event_date,
                event_type,
                event_type_entity_id,
                filed_by,
                comment,
                event_type_id,
                document_id,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(event, "eventId")),
                db_date(get_value(event, "date")),
                event_type,
                event_type_entity_id,
                clean_text(get_value(event, "filedBy")),
                clean_text(get_value(event, "comment")),
                clean_text(get_value(event, "eventTypeID")),
                clean_text(get_value(event, "documentId")),
                json_hash(event),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

    hearings = data.get("caseHearings") or []
    for hearing in hearings:
        hearing_id = clean_text(get_value(hearing, "hearingId"))
        calendar_text = clean_text(get_value(hearing, "calendar"))
        hearing_type = clean_text(get_value(hearing, "type"))
        judge_text = clean_text(get_value(hearing, "judge"))

        calendar_entity_id = upsert_entity(conn, "calendar", calendar_text, calendar_text)
        courtroom_entity_id = upsert_entity(conn, "courtroom", calendar_text, calendar_text)
        hearing_type_entity_id = upsert_entity(conn, "hearing_type", hearing_type, hearing_type)
        judge_entity_id = upsert_entity(conn, "judge", judge_text, judge_text)

        conn.execute(
            """
            INSERT OR REPLACE INTO case_hearings (
                cap_case_id,
                case_number,
                hearing_id,
                calendar_text,
                calendar_entity_id,
                courtroom_entity_id,
                hearing_type_text,
                hearing_type_entity_id,
                hearing_date,
                hearing_time,
                hearing_result,
                document_id,
                interpreter,
                hearing_flag,
                judge_text,
                judge_entity_id,
                court_reporter,
                cancel_reason,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                hearing_id,
                calendar_text,
                calendar_entity_id,
                courtroom_entity_id,
                hearing_type,
                hearing_type_entity_id,
                db_date(get_value(hearing, "date")),
                clean_text(get_value(hearing, "time")),
                clean_text(get_value(hearing, "hearingResult")),
                clean_text(get_value(hearing, "documentId")),
                clean_text(get_value(hearing, "interpreter")),
                clean_text(get_value(hearing, "hearingFlag")),
                judge_text,
                judge_entity_id,
                clean_text(get_value(hearing, "courtReporter")),
                clean_text(get_value(hearing, "cancelReason")),
                json_hash(hearing),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

        for doc in hearing.get("documents") or []:
            doc_id = clean_text(get_value(doc, "documentId"))
            doc_name = clean_text(get_value(doc, "documentName"))
            document_entity_id = upsert_entity(conn, "document", doc_id or doc_name, doc_name)

            conn.execute(
                """
                INSERT OR REPLACE INTO case_hearing_documents (
                    cap_case_id,
                    case_number,
                    hearing_id,
                    document_id,
                    document_name,
                    document_entity_id,
                    source_hash,
                    scraped_at,
                    scraper_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cap_case_id,
                    case_number,
                    hearing_id,
                    doc_id,
                    doc_name,
                    document_entity_id,
                    json_hash(doc),
                    now_iso(),
                    SCRAPER_VERSION,
                ),
            )

    charges = data.get("caseCharges") or []
    for charge in charges:
        offense_description = clean_text(get_value(charge, "offenseDescription"))
        parsed = parse_charge_parts(offense_description)
        disposition_raw, disposition_normal = disposition_date_pair(get_value(charge, "dispositionDate"))
        jurisdiction = clean_text(get_value(charge, "jurisdiction"))
        jurisdiction_entity_id = upsert_entity(conn, "jurisdiction", jurisdiction, jurisdiction)

        charge_id = clean_text(get_value(charge, "chargeId"))

        conn.execute(
            """
            INSERT OR REPLACE INTO case_charges (
                cap_case_id,
                case_number,
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
                normalized_charge_text,
                disposition_date_raw,
                disposition_date,
                disposition_desc,
                disposition_code,
                security_group,
                plea,
                plea_date,
                jurisdiction,
                jurisdiction_entity_id,
                citation_number,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                charge_id,
                clean_text(get_value(charge, "chargeNumber")),
                db_date(get_value(charge, "offenseDate")),
                clean_text(get_value(charge, "degree")),
                offense_description,
                parsed["statute_raw"],
                parsed["statute_prefix"],
                parsed["statute_number"],
                parsed["statute_suffix"],
                parsed["severity_code"],
                parsed["normalized_charge_text"],
                disposition_raw,
                disposition_normal,
                clean_text(get_value(charge, "dispositionDesc")),
                clean_text(get_value(charge, "dispositionCode")),
                clean_text(get_value(charge, "securityGroup")),
                clean_text(get_value(charge, "plea")),
                db_date(get_value(charge, "pleaDate")),
                jurisdiction,
                jurisdiction_entity_id,
                clean_text(get_value(charge, "citationNumber")),
                json_hash(charge),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

        for disp in charge.get("dispositions") or []:
            disp_raw, disp_normal = disposition_date_pair(
                get_value(disp, "dispositionDate", "date")
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO case_charge_dispositions (
                    cap_case_id,
                    case_number,
                    charge_id,
                    disposition_id,
                    disposition_date_raw,
                    disposition_date,
                    disposition_desc,
                    disposition_code,
                    result_text,
                    source_hash,
                    scraped_at,
                    scraper_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cap_case_id,
                    case_number,
                    charge_id,
                    clean_text(get_value(disp, "dispositionId", "id")),
                    disp_raw,
                    disp_normal,
                    clean_text(get_value(disp, "dispositionDesc", "description")),
                    clean_text(get_value(disp, "dispositionCode", "code")),
                    clean_text(get_value(disp, "result", "resultText")),
                    json_hash(disp),
                    now_iso(),
                    SCRAPER_VERSION,
                ),
            )

        for sentence in charge.get("sentences") or []:
            conn.execute(
                """
                INSERT OR REPLACE INTO case_charge_sentences (
                    cap_case_id,
                    case_number,
                    charge_id,
                    sentence_id,
                    sentence_date,
                    sentence_type,
                    sentence_text,
                    amount_text,
                    term_text,
                    status,
                    source_hash,
                    scraped_at,
                    scraper_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cap_case_id,
                    case_number,
                    charge_id,
                    clean_text(get_value(sentence, "sentenceId", "id")),
                    db_date(get_value(sentence, "sentenceDate", "date")),
                    clean_text(get_value(sentence, "sentenceType", "type")),
                    clean_text(get_value(sentence, "sentenceText", "description", "text")),
                    clean_text(get_value(sentence, "amount", "amountText")),
                    clean_text(get_value(sentence, "term", "termText")),
                    clean_text(get_value(sentence, "status")),
                    json_hash(sentence),
                    now_iso(),
                    SCRAPER_VERSION,
                ),
            )

    documents = data.get("caseDocuments") or []
    for doc in documents:
        doc_type = clean_text(get_value(doc, "docType", "name"))
        doc_type_entity_id = upsert_entity(conn, "document_type", doc_type, doc_type)

        conn.execute(
            """
            INSERT OR REPLACE INTO case_documents (
                cap_case_id,
                case_number,
                doc_id,
                document_version_id,
                document_type_id,
                name,
                doc_type,
                doc_type_entity_id,
                page_count,
                effective_date,
                doc_security,
                document_security_group,
                document_fee_type,
                document_fee_amount,
                total_fee,
                require_purchase,
                purchase_enabled,
                purchased,
                saved,
                is_preview,
                show_cart_options,
                require_login,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(doc, "docId")),
                clean_text(get_value(doc, "documentVersionId")),
                clean_text(get_value(doc, "documentTypeID")),
                clean_text(get_value(doc, "name")),
                doc_type,
                doc_type_entity_id,
                clean_text(get_value(doc, "pageCount")),
                db_date(get_value(doc, "effectiveDate")),
                clean_text(get_value(doc, "docSecurity")),
                clean_text(get_value(doc, "documentSecurityGroup")),
                clean_text(get_value(doc, "documentFeeType")),
                float(get_value(doc, "documentFeeAmount", default=0) or 0),
                float(get_value(doc, "totalFee", default=0) or 0),
                bool_int(get_value(doc, "requirePurchase")),
                bool_int(get_value(doc, "purchaseEnabled")),
                bool_int(get_value(doc, "purchased")),
                bool_int(get_value(doc, "saved")),
                bool_int(get_value(doc, "isPreview")),
                bool_int(get_value(doc, "showCartOptions")),
                bool_int(get_value(doc, "requireLogin")),
                json_hash(doc),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

    demographics = data.get("casePartyDemographics") or {}
    if isinstance(demographics, dict) and any(v is not None for v in demographics.values()):
        conn.execute(
            """
            INSERT OR REPLACE INTO case_demographics (
                cap_case_id,
                case_number,
                race,
                sex,
                date_of_birth,
                date_of_birth_text,
                height,
                weight,
                hair_color,
                eye_color,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(demographics, "race")),
                clean_text(get_value(demographics, "sex")),
                db_date(get_value(demographics, "dateOfBirth")),
                clean_text(get_value(demographics, "dateOfBirth")),
                clean_text(get_value(demographics, "height")),
                clean_text(get_value(demographics, "weight")),
                clean_text(get_value(demographics, "hairColor")),
                clean_text(get_value(demographics, "eyeColor")),
                json_hash(demographics),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

    address = data.get("casePartyAddress") or {}
    if isinstance(address, dict) and any(v is not None for v in address.values()):
        conn.execute(
            """
            INSERT OR REPLACE INTO case_party_addresses (
                cap_case_id,
                case_number,
                street_name,
                city,
                state,
                zip,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(address, "streetName")),
                clean_text(get_value(address, "city")),
                clean_text(get_value(address, "state")),
                clean_text(get_value(address, "zip")),
                json_hash(address),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

    insert_identifiers(conn, cap_case_id, case_number, "party_identification", data.get("casePartyIdentification") or [])
    insert_identifiers(conn, cap_case_id, case_number, "state_identification", data.get("casePartyStateIdentification") or [])

    fbi = data.get("casePartyFBINumIdentification") or {}
    if isinstance(fbi, dict) and clean_text(get_value(fbi, "fbiNum")):
        conn.execute(
            """
            INSERT OR REPLACE INTO case_party_identifiers (
                cap_case_id,
                case_number,
                identifier_type,
                identifier_id,
                identifier_value,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                "fbi",
                clean_text(get_value(fbi, "fbiNumID")),
                clean_text(get_value(fbi, "fbiNum")),
                json_hash(fbi),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )

    insert_generic_arrests(conn, cap_case_id, case_number, data.get("caseArrestInfomation") or data.get("caseArrestInformation") or [])
    insert_generic_flags(conn, cap_case_id, case_number, data.get("caseFlags") or [], "case_flags")
    insert_generic_flags(conn, cap_case_id, case_number, data.get("partyInJailFlags") or [], "case_flags", flag_type_override="party_in_jail")
    insert_generic_warrants(conn, cap_case_id, case_number, data.get("caseWarrants") or [])
    insert_generic_bonds(conn, cap_case_id, case_number, data.get("caseBonds") or [])
    insert_generic_financials(conn, cap_case_id, case_number, data.get("caseFinancials") or [])
    insert_generic_financial_transactions(conn, cap_case_id, case_number, data.get("caseFinancialTransactions") or [])
    insert_cross_refs(conn, cap_case_id, case_number, data.get("caseCrossReferenceNumbers") or [])
    insert_related_cases(conn, cap_case_id, case_number, data.get("relatedCases") or [])

    insert_link_candidates(conn, cap_case_id, case_number)

    if dump_json:
        dump_case_json(conn, cap_case_id, case_number, api_url, api_json, dump_html_path)

    conn.commit()
    return case_number


def insert_identifiers(conn, cap_case_id, case_number, identifier_type, rows):
    for item in rows:
        if not isinstance(item, dict):
            continue

        identifier_id = clean_text(
            get_value(item, "id", "identifierId", "identificationId", "stateId")
        )
        identifier_value = clean_text(
            get_value(item, "value", "number", "identifier", "stateIdNumber")
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO case_party_identifiers (
                cap_case_id,
                case_number,
                identifier_type,
                identifier_id,
                identifier_value,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                identifier_type,
                identifier_id,
                identifier_value,
                json_hash(item),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_generic_arrests(conn, cap_case_id, case_number, rows):
    for arrest in rows:
        if not isinstance(arrest, dict):
            continue

        agency = clean_text(get_value(arrest, "arrestingAgency", "agency", "sourceAgency"))
        agency_entity_id = upsert_entity(conn, "agency", agency, agency)

        conn.execute(
            """
            INSERT OR REPLACE INTO case_arrests (
                cap_case_id,
                case_number,
                arrest_id,
                arrest_date,
                arrest_time,
                arrest_datetime,
                arresting_agency,
                arresting_agency_entity_id,
                arrest_location,
                booking_number,
                arrest_report_number,
                citation_number,
                source_field_seen,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(arrest, "arrestId", "id")),
                db_date(get_value(arrest, "arrestDate", "date")),
                clean_text(get_value(arrest, "arrestTime", "time")),
                clean_text(get_value(arrest, "arrestDateTime", "datetime")),
                agency,
                agency_entity_id,
                clean_text(get_value(arrest, "location", "arrestLocation")),
                clean_text(get_value(arrest, "bookingNumber", "bookingNo")),
                clean_text(get_value(arrest, "reportNumber", "agencyReportNumber", "arrestReportNumber")),
                clean_text(get_value(arrest, "citationNumber")),
                "caseArrestInfomation",
                json_hash(arrest),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_generic_flags(conn, cap_case_id, case_number, rows, table, flag_type_override=""):
    for flag in rows:
        if not isinstance(flag, dict):
            continue

        flag_type = flag_type_override or clean_text(get_value(flag, "type", "flagType"))
        conn.execute(
            """
            INSERT OR REPLACE INTO case_flags (
                cap_case_id,
                case_number,
                flag_id,
                flag_type,
                flag_text,
                status,
                effective_date,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(flag, "flagId", "id")),
                flag_type,
                clean_text(get_value(flag, "text", "description", "flagText")),
                clean_text(get_value(flag, "status")),
                db_date(get_value(flag, "effectiveDate", "date")),
                json_hash(flag),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_generic_warrants(conn, cap_case_id, case_number, rows):
    for warrant in rows:
        if not isinstance(warrant, dict):
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO case_warrants (
                cap_case_id,
                case_number,
                warrant_id,
                warrant_type,
                warrant_status,
                issue_date,
                recall_date,
                bail_amount,
                notes,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(warrant, "warrantId", "id")),
                clean_text(get_value(warrant, "type", "warrantType")),
                clean_text(get_value(warrant, "status", "warrantStatus")),
                db_date(get_value(warrant, "issueDate")),
                db_date(get_value(warrant, "recallDate")),
                clean_text(get_value(warrant, "bailAmount", "amount")),
                clean_text(get_value(warrant, "notes", "comment")),
                json_hash(warrant),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_generic_bonds(conn, cap_case_id, case_number, rows):
    for bond in rows:
        if not isinstance(bond, dict):
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO case_bonds (
                cap_case_id,
                case_number,
                bond_id,
                bond_type,
                bond_status,
                bond_amount,
                posted_date,
                forfeited_date,
                notes,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(bond, "bondId", "id")),
                clean_text(get_value(bond, "type", "bondType")),
                clean_text(get_value(bond, "status", "bondStatus")),
                clean_text(get_value(bond, "amount", "bondAmount")),
                db_date(get_value(bond, "postedDate")),
                db_date(get_value(bond, "forfeitedDate")),
                clean_text(get_value(bond, "notes", "comment")),
                json_hash(bond),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_generic_financials(conn, cap_case_id, case_number, rows):
    for fin in rows:
        if not isinstance(fin, dict):
            continue

        conn.execute(
            """
            INSERT OR REPLACE INTO case_financials (
                cap_case_id,
                case_number,
                financial_id,
                financial_type,
                description,
                amount,
                balance,
                due_date,
                status,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(fin, "financialId", "id")),
                clean_text(get_value(fin, "type", "financialType")),
                clean_text(get_value(fin, "description")),
                float(get_value(fin, "amount", default=0) or 0),
                float(get_value(fin, "balance", default=0) or 0),
                db_date(get_value(fin, "dueDate")),
                clean_text(get_value(fin, "status")),
                json_hash(fin),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_generic_financial_transactions(conn, cap_case_id, case_number, rows):
    for tx in rows:
        if not isinstance(tx, dict):
            continue

        conn.execute(
            """
            INSERT OR REPLACE INTO case_financial_transactions (
                cap_case_id,
                case_number,
                transaction_id,
                transaction_date,
                transaction_type,
                description,
                amount,
                receipt_number,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(tx, "transactionId", "id")),
                db_date(get_value(tx, "transactionDate", "date")),
                clean_text(get_value(tx, "type", "transactionType")),
                clean_text(get_value(tx, "description")),
                float(get_value(tx, "amount", default=0) or 0),
                clean_text(get_value(tx, "receiptNumber")),
                json_hash(tx),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_cross_refs(conn, cap_case_id, case_number, rows):
    for ref in rows:
        if not isinstance(ref, dict):
            continue

        conn.execute(
            """
            INSERT OR REPLACE INTO case_cross_reference_numbers (
                cap_case_id,
                case_number,
                cross_reference_id,
                reference_type,
                reference_number,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(ref, "id", "crossReferenceId")),
                clean_text(get_value(ref, "type", "referenceType")),
                clean_text(get_value(ref, "number", "referenceNumber")),
                json_hash(ref),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_related_cases(conn, cap_case_id, case_number, rows):
    for related in rows:
        if not isinstance(related, dict):
            continue

        conn.execute(
            """
            INSERT OR REPLACE INTO related_cases (
                cap_case_id,
                case_number,
                related_cap_case_id,
                related_case_number,
                relationship_type,
                relationship_text,
                source_hash,
                scraped_at,
                scraper_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_case_id,
                case_number,
                clean_text(get_value(related, "caseId", "relatedCaseId")),
                clean_text(get_value(related, "caseNumber", "caseNbr")),
                clean_text(get_value(related, "relationshipType", "type")),
                clean_text(get_value(related, "relationshipText", "description")),
                json_hash(related),
                now_iso(),
                SCRAPER_VERSION,
            ),
        )


def insert_link_candidates(conn, cap_case_id, case_number):
    rows = conn.execute(
        """
        SELECT party_entity_id, full_name, aliases_raw
        FROM case_parties
        WHERE cap_case_id = ?
          AND is_defendant = 1
        """,
        (cap_case_id,),
    ).fetchall()

    charges = conn.execute(
        """
        SELECT charge_id, offense_date, offense_description, statute_prefix, statute_number, citation_number
        FROM case_charges
        WHERE cap_case_id = ?
        """,
        (cap_case_id,),
    ).fetchall()

    for party in rows:
        party_entity_id = party["party_entity_id"]

        candidates = []
        if party["full_name"]:
            candidates.append(("defendant_name", party["full_name"], "weak", "CAP defendant name"))

        for alias_name in split_alias_string(party["aliases_raw"]):
            candidates.append(("alias", alias_name, "weak", "CAP alias"))

        for ch in charges:
            if ch["citation_number"]:
                candidates.append(("citation_number", ch["citation_number"], "strong", "CAP charge citation number"))
            if ch["offense_date"]:
                candidates.append(("offense_date", ch["offense_date"], "weak", "CAP charge offense date"))
            if ch["offense_description"]:
                candidates.append(("charge_text", ch["offense_description"], "possible", "CAP charge description"))
            statute = clean_text(f"{ch['statute_prefix']}{ch['statute_number']}")
            if statute:
                candidates.append(("statute", statute, "possible", "CAP parsed statute"))

        for ctype, cvalue, confidence, basis in candidates:
            conn.execute(
                """
                INSERT OR IGNORE INTO case_link_candidates (
                    cap_case_id,
                    case_number,
                    party_entity_id,
                    candidate_type,
                    candidate_value,
                    confidence,
                    match_basis,
                    source_system,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cap_case_id,
                    case_number,
                    party_entity_id,
                    ctype,
                    clean_text(cvalue),
                    confidence,
                    basis,
                    "CAP",
                    now_iso(),
                ),
            )


def dump_case_json(conn, cap_case_id, case_number, api_url, api_json, html_dump_path=""):
    dump_dir = OUT_DIR / "debug_case_json"
    dump_dir.mkdir(parents=True, exist_ok=True)

    path = dump_dir / f"{safe_filename(case_number or cap_case_id)}_{cap_case_id}.json"
    path.write_text(json.dumps(api_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    conn.execute(
        """
        INSERT INTO case_details_debug_dumps (
            cap_case_id,
            case_number,
            api_url,
            dump_path,
            html_dump_path,
            source_hash,
            created_at,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cap_case_id,
            case_number,
            api_url,
            str(path),
            html_dump_path,
            json_hash(api_json),
            now_iso(),
            "Debug dump requested by --dump-json/--dump-html",
        ),
    )


async def first_visible(page, selectors):
    for selector in selectors:
        loc = page.locator(selector)
        count = await loc.count()

        for i in range(count):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    return item
            except Exception:
                pass

    return None


async def login_if_needed(page, manual_login=False, headless_mode=False):
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    print(f".env path checked: {ENV_PATH}")

    username = os.environ.get("CAP_USERNAME", "").strip()
    password = os.environ.get("CAP_PASSWORD", "")

    print(f"CAP_USERNAME loaded: {'yes' if username else 'no'}")
    print(f"CAP_PASSWORD loaded: {'yes' if password else 'no'}")

    if manual_login:
        if headless_mode:
            raise RuntimeError("Manual login requires --show-browser.")
        print("\nManual login mode.")
        print("Log in in the opened browser window.")
        input("When finished, press ENTER here...")
        return

    if not username:
        username = input("CAP username/email: ").strip()

    if not password:
        password = getpass.getpass("CAP password: ")

    user_input = await first_visible(page, [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[name*='user' i]",
        "input[id*='user' i]",
        "input[type='text']",
    ])

    pass_input = await first_visible(page, [
        "input[type='password']",
        "input[name*='password' i]",
        "input[id*='password' i]",
    ])

    if not user_input or not pass_input:
        if headless_mode:
            raise RuntimeError("Could not find login fields. Try --manual-login --show-browser.")
        print("\nCould not confidently find login fields.")
        print("Log in manually in the opened browser window.")
        input("When finished, press ENTER here...")
        return

    await user_input.fill(username)
    await pass_input.fill(password)

    submit = await first_visible(page, [
        "button:has-text('Login')",
        "button:has-text('Log In')",
        "input[type='submit']",
        "button[type='submit']",
        "text=Login",
        "text=Log In",
    ])

    if submit:
        await submit.click()
    else:
        await pass_input.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(1000)


async def set_date_filter(page, calendar_date):
    date_input = await first_visible(page, [
        "input[type='date']",
        "input[placeholder*='Date' i]",
        "input[name*='date' i]",
        "input[id*='date' i]",
        "input[aria-label*='Date' i]",
    ])

    if not date_input:
        raise RuntimeError("Date input not found.")

    input_type = await date_input.get_attribute("type")

    if input_type and input_type.lower() == "date":
        await date_input.fill(calendar_date.isoformat())
    else:
        await date_input.fill(cap_display_date(calendar_date))

    await date_input.evaluate(
        """
        el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.blur();
        }
        """
    )

    await page.wait_for_timeout(800)


async def set_category_filter(page, category):
    category = clean_text(category or "Any")

    if category not in CASE_CATEGORY_VALUES:
        raise RuntimeError(f"Unknown category: {category}")

    value = CASE_CATEGORY_VALUES[category]

    sel = page.locator("select#caseCategory").first

    if await sel.count() and await sel.is_visible():
        try:
            await sel.select_option(value=value)
        except Exception:
            await sel.select_option(label=re.compile(category, re.I))

        await sel.evaluate(
            """
            el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }
            """
        )

        await page.wait_for_timeout(800)
        return

    raise RuntimeError("Category select not found.")


async def wait_for_departments_or_alert(page, timeout_ms=15000):
    try:
        await page.wait_for_function(
            f"""
            () => {{
                const deptCount = document.querySelectorAll('{DEPT_BUTTON_SELECTOR}').length;
                const text = document.body.innerText || '';
                return deptCount > 0 || /No Departments scheduled/i.test(text);
            }}
            """,
            timeout=timeout_ms,
        )
    except Exception:
        pass


async def apply_filters(page, calendar_date, category):
    await page.goto(BASE_CALENDAR_URL, wait_until="domcontentloaded")

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(500)

    print(f"Setting date/category: {calendar_date.isoformat()} / {category}")

    await set_date_filter(page, calendar_date)
    await set_category_filter(page, category)
    await wait_for_departments_or_alert(page)
    await page.wait_for_timeout(1000)


async def click_department(page, dept):
    wanted = clean_text(dept)

    if wanted.lower().startswith("dept "):
        short = re.sub(r"^dept\s+", "", wanted, flags=re.I)
        variants = {wanted.lower(), short.lower()}
    else:
        variants = {wanted.lower(), f"dept {wanted.lower()}"}

    buttons = page.locator(DEPT_BUTTON_SELECTOR)
    count = await buttons.count()

    print(f"Department buttons found: {count}")

    for i in range(count):
        btn = buttons.nth(i)

        try:
            if not await btn.is_visible():
                continue

            text = clean_text(await btn.inner_text())
            short_text = re.sub(r"^dept\s+", "", text, flags=re.I)

            if text.lower() in variants or short_text.lower() in variants:
                print(f"Clicking department: {text}")
                await btn.click()

                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

                await page.wait_for_timeout(1500)
                return text

        except Exception:
            pass

    raise RuntimeError(f"Could not find/click department: {dept}")


async def page_fetch_json(page, api_path):
    url = urljoin(BASE_SITE, api_path)

    if "/api/users" in url.lower():
        raise RuntimeError("Refusing to fetch /api/users")

    result = await page.evaluate(
        """
        async ({url}) => {
            const res = await fetch(url, { credentials: 'include' });
            const text = await res.text();
            let data = null;
            try {
                data = JSON.parse(text);
            } catch (e) {}
            return {
                ok: res.ok,
                status: res.status,
                url: res.url,
                text,
                data
            };
        }
        """,
        {"url": url},
    )

    if not result["ok"]:
        raise RuntimeError(f"API fetch failed {result['status']} {url}: {result['text'][:300]}")

    return result["data"], result["url"]


async def get_departments_api(page, calendar_date, category):
    api_date = cap_api_date(calendar_date)
    candidates = API_CATEGORY_CANDIDATES.get(category, [category])

    last_error = None

    for api_cat in candidates:
        path = f"/api/calendarbydate/{quote(LOCATION)}/{api_date}/{quote(api_cat)}"

        try:
            data, url = await page_fetch_json(page, path)
            rows = data.get("data") if isinstance(data, dict) else []

            if isinstance(rows, list):
                return rows, api_cat, url

        except Exception as e:
            last_error = e

    raise RuntimeError(f"Could not get departments by API for {calendar_date} / {category}: {last_error}")


async def get_department_calendar_api(page, dept_code, calendar_date, api_category):
    api_date = cap_api_date(calendar_date)
    dept_code = clean_text(dept_code).replace("Dept ", "").replace("Department ", "")
    path = f"/api/calendardepartment/{quote(LOCATION)}/{quote(dept_code)}/{api_date}/{quote(api_category)}"

    data, url = await page_fetch_json(page, path)
    rows = data.get("data") if isinstance(data, dict) else []

    if not isinstance(rows, list):
        rows = []

    return rows, url


def dept_matches_filter(dept_row, only_depts):
    if not only_depts:
        return True

    code = clean_text(get_value(dept_row, "courtroom"))
    desc = clean_text(get_value(dept_row, "description"))
    variants = {
        code.lower(),
        f"dept {code.lower()}",
        f"department {code.lower()}",
        desc.lower(),
    }

    for item in only_depts:
        item = clean_text(item).lower()
        if item in variants:
            return True

    return False


def parse_only_depts(value):
    value = clean_text(value)
    if not value:
        return []
    return [clean_text(x) for x in value.split(",") if clean_text(x)]


def parse_dates(args):
    if args.only_dates:
        out = []
        for part in args.only_dates.split(","):
            part = clean_text(part)
            if part:
                out.append(parse_date_arg(part))
        return sorted(list(dict.fromkeys(out)))

    start = parse_date_arg(args.start_date) if args.start_date else date.today()
    return [start + timedelta(days=i) for i in range(args.days)]


async def find_view_link(page, case_number):
    case_number = clean_text(case_number)

    selectors = []
    if case_number:
        selectors.extend([
            f'a[aria-label="View case {case_number}"]',
            f'a[aria-label*="{case_number}"]',
        ])

    selectors.extend([
        'a[ng-click*="goToCase"][aria-label^="View case"]',
        'a[aria-label^="View case"]',
        'a[ng-click*="goToCase"]',
        'a:has-text("View")',
    ])

    for selector in selectors:
        loc = page.locator(selector)
        count = await loc.count()

        for i in range(count):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    return item
            except Exception:
                pass

    return None


async def click_view_and_capture_detail(page, case_number):
    view_link = await find_view_link(page, case_number)

    if not view_link:
        raise RuntimeError(f"Could not find visible View link for case {case_number or '(first case)'}")

    label = clean_text(await view_link.get_attribute("aria-label"))
    print(f"Clicking detail link: {label or 'View'}")

    async with page.expect_response(
        lambda r: "/api/case/" in r.url.lower() and r.status == 200,
        timeout=45000,
    ) as response_info:
        await view_link.click()

    response = await response_info.value
    api_url = response.url

    if "/api/users" in api_url.lower():
        raise RuntimeError("Unexpected /api/users capture blocked")

    api_json = await response.json()

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(1000)

    m = re.search(r"/api/case/(\d+)", api_url)
    cap_case_id = m.group(1) if m else ""

    return cap_case_id, api_url, api_json


async def dump_active_tab_html_if_requested(page, cap_case_id, case_number, dump_html):
    if not dump_html:
        return ""

    dump_dir = OUT_DIR / "debug_case_html"
    dump_dir.mkdir(parents=True, exist_ok=True)

    try:
        html_text = await page.locator("[ui-view]").first.evaluate("el => el.outerHTML")
    except Exception:
        html_text = await page.content()

    path = dump_dir / f"{safe_filename(case_number or cap_case_id)}_{cap_case_id}.html"
    path.write_text(html_text, encoding="utf-8", errors="ignore")
    return str(path)


async def restore_calendar_department(page, calendar_date, category, dept_code):
    await apply_filters(page, calendar_date, category)
    await click_department(page, dept_code)


def parse_refresh_date(value):
    value = clean_text(value)
    if not value:
        return None

    # Pull date out of strings like:
    #   6/30/2026 8:30AM Dept V10 - Victorville
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", value)
    if m:
        value = m.group(1)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    return None


def parse_refresh_datetime(value):
    value = clean_text(value)
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    return None


def get_detail_scraped_at(conn, cap_case_id):
    if not table_exists(conn, "cases") or not column_exists(conn, "cases", "detail_scraped_at"):
        return None

    row = conn.execute(
        """
        SELECT detail_scraped_at
        FROM cases
        WHERE cap_case_id = ?
        LIMIT 1
        """,
        (clean_text(cap_case_id),),
    ).fetchone()

    if not row:
        return None

    try:
        value = row["detail_scraped_at"]
    except Exception:
        value = row[0]

    return parse_refresh_datetime(value)


def get_most_recent_court_date(conn, cap_case_id):
    cap_case_id = clean_text(cap_case_id)
    dates = []

    checks = [
        ("case_hearings", "hearing_date"),
        ("calendar_appearances", "session_date"),
        ("calendar_appearances", "calendar_date"),
    ]

    for table, col in checks:
        if not table_exists(conn, table) or not column_exists(conn, table, col):
            continue

        rows = conn.execute(
            f"""
            SELECT DISTINCT {quote_ident(col)}
            FROM {quote_ident(table)}
            WHERE cap_case_id = ?
              AND {quote_ident(col)} IS NOT NULL
              AND {quote_ident(col)} <> ''
            """,
            (cap_case_id,),
        ).fetchall()

        for row in rows:
            value = row[0]
            d = parse_refresh_date(value)
            if d:
                dates.append(d)

    if table_exists(conn, "cases") and column_exists(conn, "cases", "next_hearing"):
        row = conn.execute(
            """
            SELECT next_hearing
            FROM cases
            WHERE cap_case_id = ?
            LIMIT 1
            """,
            (cap_case_id,),
        ).fetchone()

        if row:
            try:
                value = row["next_hearing"]
            except Exception:
                value = row[0]

            d = parse_refresh_date(value)
            if d:
                dates.append(d)

    if not dates:
        return None

    return max(dates)


def should_detail_scrape(conn, cap_case_id, args):
    if args.no_details:
        return False

    # No existing detail record means scrape.
    if not case_has_details(conn, cap_case_id):
        return True

    # Emergency/manual override.
    if getattr(args, "force_details_all", False):
        return True

    # Normal mode: existing details are skipped.
    if not args.details_all:
        return False

    refresh_days = int(getattr(args, "refresh_days", 30) or 30)

    most_recent_court_date = get_most_recent_court_date(conn, cap_case_id)
    today = date.today()

    # If the record still has a future court/hearing/calendar date,
    # keep it fresh because the case is still moving.
    if most_recent_court_date and today < most_recent_court_date:
        return True

    detail_scraped_at = get_detail_scraped_at(conn, cap_case_id)

    # If we cannot tell when it was scraped, refresh it.
    if not detail_scraped_at:
        return True

    age_days = (datetime.now() - detail_scraped_at).days

    # Catch non-hearing updates every N days.
    if age_days >= refresh_days:
        return True

    return False


async def process_department(page, conn, run_id, calendar_date, category, dept_row, api_category, args):
    dept_code = clean_text(get_value(dept_row, "courtroom"))
    dept_name = f"Dept {dept_code}" if dept_code else clean_text(get_value(dept_row, "description"))

    calendar_date_iso = calendar_date.isoformat()
    previously_completed = args.resume and progress_completed(conn, calendar_date_iso, category, dept_name)

    if previously_completed:
        print(f"Previously completed chunk found; rechecking for new cases: {calendar_date_iso} / {category} / {dept_name}")

    print(f"\n=== {calendar_date_iso} / {category} / {dept_name} ===")

    rows, dept_api_url = await get_department_calendar_api(page, dept_code, calendar_date, api_category)

    print(f"Calendar rows from API: {len(rows)}")

    for row in rows:
        insert_calendar_appearance(conn, run_id, calendar_date_iso, category, dept_api_url, row)

    conn.commit()

    detail_count = 0

    if args.no_details:
        progress_note = "recheck calendar only" if previously_completed else "calendar only"
        mark_progress(conn, calendar_date_iso, category, dept_name, len(rows), detail_count, progress_note)
        return len(rows), detail_count

    if rows:
        await restore_calendar_department(page, calendar_date, category, dept_code)

    for i, row in enumerate(rows, start=1):
        cap_case_id = clean_text(get_value(row, "caseId"))
        case_number = clean_text(get_value(row, "caseNbr"))

        if not cap_case_id and not case_number:
            continue

        if not should_detail_scrape(conn, cap_case_id, args):
            print(f"[{i}/{len(rows)}] Details already scraped, skipping: {case_number} / {cap_case_id}")
            continue

        print(f"[{i}/{len(rows)}] Detail scrape: {case_number} / {cap_case_id}")

        try:
            clicked_case_id, api_url, api_json = await click_view_and_capture_detail(page, case_number)

            if clicked_case_id and cap_case_id and clicked_case_id != cap_case_id:
                print(f"WARNING: clicked API case id {clicked_case_id}, expected {cap_case_id}")

            actual_case_id = clicked_case_id or cap_case_id

            case_number_from_api = clean_text(
                get_value(api_json.get("data", {}) if isinstance(api_json, dict) else {}, "caseNumber", "caseNbr")
            ) or case_number

            html_dump_path = await dump_active_tab_html_if_requested(
                page,
                actual_case_id,
                case_number_from_api,
                args.dump_html,
            )

            insert_case_detail(
                conn,
                actual_case_id,
                api_url,
                api_json,
                dump_json=args.dump_json,
                dump_html_path=html_dump_path,
            )

            conn.commit()
            detail_count += 1

        except Exception as e:
            print(f"DETAIL ERROR {case_number or cap_case_id}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

        if i < len(rows):
            await restore_calendar_department(page, calendar_date, category, dept_code)

        await asyncio.sleep(random.uniform(args.delay_min, args.delay_max))

    progress_note = "recheck ok" if previously_completed else "ok"
    mark_progress(conn, calendar_date_iso, category, dept_name, len(rows), detail_count, progress_note)
    return len(rows), detail_count


def stream_prefixed_output(proc, prefix):
    try:
        if not proc.stdout:
            return
        for line in proc.stdout:
            text = line.rstrip("\n")
            print(f"{prefix}{text}")
    finally:
        print(f"{prefix}[process ended]")


def terminate_process(proc, label):
    if not proc or proc.poll() is not None:
        return

    print(f"{label} stopping...")
    proc.terminate()

    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        print(f"{label} did not exit after terminate(); killing it.")
        proc.kill()
        proc.wait(timeout=5)


def start_lcn_worker_process(args):
    if not LCN_SCRIPT_PATH.exists():
        print(f"[LCN] Script not found, skipping concurrent worker: {LCN_SCRIPT_PATH}")
        return None

    cmd = [
        sys.executable,
        "-u",
        str(LCN_SCRIPT_PATH),
        "--db",
        str(DB_PATH),
        "--mode",
        "worker",
        "--worker-batch-size",
        str(max(1, int(args.lcn_worker_batch_size))),
        "--worker-sleep",
        str(max(5.0, float(args.lcn_worker_sleep))),
    ]

    if args.dump_html:
        cmd.append("--dump-html")
    if args.lcn_include_low_probability:
        cmd.append("--include-low-probability")

    print("[LCN] Starting concurrent worker:")
    print("[LCN] " + " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    threading.Thread(target=stream_prefixed_output, args=(proc, "[LCN] "), daemon=True).start()
    return proc


async def run_scraper(args):
    dates = parse_dates(args)
    only_depts = parse_only_depts(args.only_depts)

    conn = db_connect()
    run_id = start_run(conn, args, dates)
    lcn_worker_proc = None

    total_rows = 0
    total_details = 0

    headless = not args.show_browser

    try:
        if args.run_lcn_concurrently:
            lcn_worker_proc = start_lcn_worker_process(args)

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                accept_downloads=True,
                viewport={"width": 1400, "height": 1000},
            )

            page = context.pages[0] if context.pages else await context.new_page()

            if not args.skip_login:
                await login_if_needed(
                    page,
                    manual_login=args.manual_login,
                    headless_mode=headless,
                )

            for calendar_date in dates:
                print(f"\n\n######## DATE {calendar_date.isoformat()} ########")

                await apply_filters(page, calendar_date, args.category)

                departments, api_category, dept_api_url = await get_departments_api(
                    page,
                    calendar_date,
                    args.category,
                )

                print(f"Departments from API: {len(departments)} using category path: {api_category}")

                filtered_depts = [
                    d for d in departments
                    if dept_matches_filter(d, only_depts)
                ]

                print(f"Departments after filter: {len(filtered_depts)}")

                for dept_row in filtered_depts:
                    dept_code = clean_text(get_value(dept_row, "courtroom"))

                    try:
                        rows_count, details_count = await process_department(
                            page,
                            conn,
                            run_id,
                            calendar_date,
                            args.category,
                            dept_row,
                            api_category,
                            args,
                        )
                        total_rows += rows_count
                        total_details += details_count

                    except Exception as e:
                        print(f"DEPARTMENT ERROR {calendar_date.isoformat()} / {dept_code}: {e}")

            await context.close()

        finish_run(
            conn,
            run_id,
            "ok",
            f"rows={total_rows}, details={total_details}",
        )

        print("\nDONE.")
        print(f"DB: {DB_PATH}")
        print(f"Calendar rows saved: {total_rows}")
        print(f"Case details saved: {total_details}")

    except Exception as e:
        finish_run(conn, run_id, "error", str(e))
        raise

    finally:
        terminate_process(lcn_worker_proc, "[LCN]")
        conn.close()


def list_departments_from_db():
    conn = db_connect()

    rows = conn.execute(
        """
        SELECT DISTINCT courtroom_text
        FROM calendar_appearances
        WHERE courtroom_text IS NOT NULL
          AND courtroom_text <> ''
        ORDER BY courtroom_text
        """
    ).fetchall()

    conn.close()
    return [r["courtroom_text"] for r in rows]


def build_cli_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument("--gui", action="store_true", help="Open GUI launcher.")

    parser.add_argument("--start-date", default="", help="YYYY-MM-DD. Default: today.")
    parser.add_argument("--days", type=int, default=7, help="Number of days from start-date. Default: 7.")
    parser.add_argument("--only-dates", default="", help="Comma dates, e.g. 2026-06-29,2026-06-30.")
    parser.add_argument("--only-depts", default="", help='Comma departments, e.g. "V10,V11" or "Dept V10,Dept V11".')

    parser.add_argument(
        "--category",
        default="Any",
        choices=list(CASE_CATEGORY_VALUES.keys()),
        help="CAP case category. Default: Any.",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="Recheck completed departments for new cases while still skipping already-scraped case details.",
    )
    parser.add_argument("--no-details", action="store_true", help="Only save calendar rows; do not click View/case details.")
    parser.add_argument("--details-all", action="store_true", help="Refresh existing details only when future court activity exists or refresh window passed.")
    parser.add_argument("--refresh-days", type=int, default=30, help="With --details-all, refresh existing details after this many days. Default: 30.")
    parser.add_argument("--force-details-all", action="store_true", help="Force re-scrape all details, ignoring refresh policy.")

    parser.add_argument("--dump-json", action="store_true", help="Debug only: save raw /api/case JSON files.")
    parser.add_argument("--dump-html", action="store_true", help="Debug only: save case page HTML files.")

    parser.add_argument("--show-browser", action="store_true", help="Show browser window.")
    parser.add_argument("--manual-login", action="store_true", help="Manual login in browser. Requires --show-browser.")
    parser.add_argument("--skip-login", action="store_true", help="Skip login and reuse existing browser profile session.")

    parser.add_argument("--delay-min", type=float, default=0.4, help="Min delay between details. Default: 0.4")
    parser.add_argument("--delay-max", type=float, default=1.2, help="Max delay between details. Default: 1.2")
    parser.add_argument("--run-lcn-concurrently", action="store_true", help="Run Local Crime News lookups in a background worker while court scraping is running.")
    parser.add_argument("--lcn-worker-batch-size", type=int, default=5, help="Concurrent LCN worker rows per batch. Default: 5.")
    parser.add_argument("--lcn-worker-sleep", type=float, default=45.0, help="Concurrent LCN worker idle sleep seconds. Default: 45.")
    parser.add_argument("--lcn-include-low-probability", action="store_true", help="Also include lower-yield criminal cases in LCN queue refreshes/backfills.")

    return parser


def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Victorville CAP Calendar Scraper")
    root.geometry("980x690")

    output_queue = queue.Queue()
    proc_holder = {"proc": None}

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)

    row = 0

    ttk.Label(frm, text="Start date (YYYY-MM-DD, blank=today):").grid(row=row, column=0, sticky="w")
    start_var = tk.StringVar()
    ttk.Entry(frm, textvariable=start_var, width=25).grid(row=row, column=1, sticky="w")
    row += 1

    ttk.Label(frm, text="Days:").grid(row=row, column=0, sticky="w")
    days_var = tk.StringVar(value="7")
    ttk.Entry(frm, textvariable=days_var, width=10).grid(row=row, column=1, sticky="w")
    row += 1

    ttk.Label(frm, text="Only dates comma list:").grid(row=row, column=0, sticky="w")
    only_dates_var = tk.StringVar()
    ttk.Entry(frm, textvariable=only_dates_var, width=55).grid(row=row, column=1, columnspan=3, sticky="we")
    row += 1

    ttk.Label(frm, text="Only departments comma list:").grid(row=row, column=0, sticky="w")
    only_depts_var = tk.StringVar()
    ttk.Entry(frm, textvariable=only_depts_var, width=55).grid(row=row, column=1, columnspan=3, sticky="we")
    row += 1

    ttk.Label(frm, text="Known departments from DB:").grid(row=row, column=0, sticky="nw")

    dept_list = tk.Listbox(frm, selectmode="extended", height=8, width=35)
    dept_list.grid(row=row, column=1, sticky="w")

    try:
        for dept in list_departments_from_db():
            dept_list.insert("end", dept)
    except Exception as e:
        dept_list.insert("end", f"(Could not load DB depts: {e})")

    row += 1

    category_var = tk.StringVar(value="Any")
    ttk.Label(frm, text="Category:").grid(row=row, column=0, sticky="w")
    ttk.OptionMenu(frm, category_var, "Any", *CASE_CATEGORY_VALUES.keys()).grid(row=row, column=1, sticky="w")
    row += 1

    resume_var = tk.BooleanVar(value=True)
    show_browser_var = tk.BooleanVar(value=True)
    manual_login_var = tk.BooleanVar(value=False)
    no_details_var = tk.BooleanVar(value=False)
    details_all_var = tk.BooleanVar(value=False)
    dump_json_var = tk.BooleanVar(value=False)
    dump_html_var = tk.BooleanVar(value=False)
    lcn_concurrent_var = tk.BooleanVar(value=True)
    lcn_low_prob_var = tk.BooleanVar(value=False)

    ttk.Checkbutton(frm, text="Resume / recheck departments for new cases", variable=resume_var).grid(row=row, column=0, sticky="w")
    ttk.Checkbutton(frm, text="Show browser", variable=show_browser_var).grid(row=row, column=1, sticky="w")
    row += 1

    ttk.Checkbutton(frm, text="Manual login", variable=manual_login_var).grid(row=row, column=0, sticky="w")
    ttk.Checkbutton(frm, text="No details", variable=no_details_var).grid(row=row, column=1, sticky="w")
    row += 1

    ttk.Checkbutton(frm, text="Details all / re-scrape", variable=details_all_var).grid(row=row, column=0, sticky="w")
    ttk.Checkbutton(frm, text="Dump JSON debug files", variable=dump_json_var).grid(row=row, column=1, sticky="w")
    ttk.Checkbutton(frm, text="Dump HTML debug files", variable=dump_html_var).grid(row=row, column=2, sticky="w")
    row += 1

    ttk.Checkbutton(frm, text="Run LCN concurrently (criminal only)", variable=lcn_concurrent_var).grid(row=row, column=0, sticky="w")
    ttk.Checkbutton(frm, text="Include low-probability LCN cases", variable=lcn_low_prob_var).grid(row=row, column=1, sticky="w")
    row += 1

    output = tk.Text(frm, height=18, wrap="word")
    output.grid(row=row, column=0, columnspan=4, sticky="nsew", pady=10)
    frm.rowconfigure(row, weight=1)
    frm.columnconfigure(3, weight=1)
    row += 1

    btn_frame = ttk.Frame(frm)
    btn_frame.grid(row=row, column=0, columnspan=4, sticky="we")

    def append_output(text):
        output.insert("end", text)
        output.see("end")

    def reader_thread(proc):
        try:
            for line in proc.stdout:
                output_queue.put(line)
        finally:
            output_queue.put("\n[process ended]\n")

    def poll_output():
        try:
            while True:
                append_output(output_queue.get_nowait())
        except queue.Empty:
            pass

        root.after(150, poll_output)

    def build_command():
        cmd = [sys.executable, "-u", str(Path(__file__).resolve())]

        if start_var.get().strip():
            cmd += ["--start-date", start_var.get().strip()]

        if days_var.get().strip():
            cmd += ["--days", days_var.get().strip()]

        if only_dates_var.get().strip():
            cmd += ["--only-dates", only_dates_var.get().strip()]

        selected_depts = [dept_list.get(i) for i in dept_list.curselection()]
        manual_depts = only_depts_var.get().strip()

        all_depts = []
        if manual_depts:
            all_depts.extend([x.strip() for x in manual_depts.split(",") if x.strip()])
        all_depts.extend(selected_depts)

        if all_depts:
            cmd += ["--only-depts", ",".join(all_depts)]

        cmd += ["--category", category_var.get()]

        if resume_var.get():
            cmd.append("--resume")
        if show_browser_var.get():
            cmd.append("--show-browser")
        if manual_login_var.get():
            cmd.append("--manual-login")
        if no_details_var.get():
            cmd.append("--no-details")
        if details_all_var.get():
            cmd.append("--details-all")
        if dump_json_var.get():
            cmd.append("--dump-json")
        if dump_html_var.get():
            cmd.append("--dump-html")
        if lcn_concurrent_var.get():
            cmd.append("--run-lcn-concurrently")
        if lcn_low_prob_var.get():
            cmd.append("--lcn-include-low-probability")

        return cmd

    def launch_process(cmd, label):
        if proc_holder["proc"] and proc_holder["proc"].poll() is None:
            messagebox.showinfo("Already running", "A scraper task is already running.")
            return

        append_output(f"\n{label}:\n" + " ".join(cmd) + "\n\n")

        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        proc_holder["proc"] = proc
        threading.Thread(target=reader_thread, args=(proc,), daemon=True).start()

    def run_clicked():
        launch_process(build_command(), "Running")

    def run_lcn_backfill_clicked():
        cmd = [
            sys.executable,
            "-u",
            str(LCN_SCRIPT_PATH),
            "--db",
            str(DB_PATH),
            "--mode",
            "backfill-live",
            "--include-low-probability",
        ]
        if show_browser_var.get():
            cmd.append("--show-browser")
        if dump_html_var.get():
            cmd.append("--dump-html")
        launch_process(cmd, "Running LCN backfill")

    def stop_clicked():
        proc = proc_holder.get("proc")
        if proc and proc.poll() is None:
            append_output("\n[stopping process]\n")
            proc.terminate()

    def send_enter_clicked():
        proc = proc_holder.get("proc")
        if proc and proc.poll() is None and proc.stdin:
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
            except Exception:
                pass

    ttk.Button(btn_frame, text="Run Scraper", command=run_clicked).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Run LCN Backfill", command=run_lcn_backfill_clicked).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Stop Scraper", command=stop_clicked).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Send ENTER", command=send_enter_clicked).pack(side="left", padx=5)

    poll_output()
    root.mainloop()


def main():
    parser = build_cli_parser()
    args = parser.parse_args()

    if args.gui:
        run_gui()
        return

    asyncio.run(run_scraper(args))


if __name__ == "__main__":
    main()
