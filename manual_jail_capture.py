import argparse
import asyncio
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = SCRIPT_DIR / "output" / "jail_import"
PROFILE_DIR = SCRIPT_DIR / ".cap_profile" / "jail_capture_profile"
DB_PATH = SCRIPT_DIR / "state" / "court_calendar.db"
DEFAULT_URL = "https://jimsnetil.shr.sbcounty.gov/bookingsearch.aspx"

FIELD_SELECTORS = {
    "booking": "#txtBookNo",
    "last_name": "#txtLastName",
    "first_name": "#txtFirstName",
    "middle_name": "#txtMiddleName",
    "dob": "#txtDOB",
    "age": "#txtAge",
}
GENDER_SELECTORS = {
    "M": "#radGender_0",
    "F": "#radGender_1",
}
SEARCH_BUTTON_SELECTORS = {
    "booking": "input[value='Search by Booking']",
    "name": "input[value='Search by Name']",
}


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def digits_only(value):
    return re.sub(r"\D+", "", clean_text(value))


def normalize_booking(value):
    return digits_only(value)


def normalize_age(value):
    digits = digits_only(value)
    if not digits:
        return ""
    age_value = int(digits)
    if age_value <= 0 or age_value > 120:
        return ""
    return str(age_value)


def normalize_gender(value):
    raw = clean_text(value).lower()
    if raw in {"m", "male"}:
        return "M"
    if raw in {"f", "female"}:
        return "F"
    return ""


def normalize_dob(value):
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


def build_search_payload(args):
    payload = {
        "url": clean_text(args.url) or DEFAULT_URL,
        "case_id": clean_text(args.case_id),
        "case_number": clean_text(args.case_number),
        "defendant_name": clean_text(args.defendant_name),
        "booking": normalize_booking(args.booking),
        "last_name": clean_text(args.last),
        "first_name": clean_text(args.first),
        "middle_name": clean_text(args.middle),
        "dob": normalize_dob(args.dob),
        "age": normalize_age(args.age),
        "gender": normalize_gender(args.gender),
    }
    payload["mode"] = "booking" if payload["booking"] else "name" if any(
        payload[key] for key in ("last_name", "first_name", "middle_name", "dob", "age", "gender")
    ) else "browse"

    missing = []
    if payload["mode"] == "name":
        if not payload["last_name"]:
            missing.append("last name")
        if not payload["first_name"]:
            missing.append("first name")
        if not payload["gender"]:
            missing.append("gender")
        if not (payload["dob"] or payload["age"]):
            missing.append("DOB or age")

    payload["missing_requirements"] = missing
    payload["can_auto_search"] = payload["mode"] == "booking" or (
        payload["mode"] == "name" and not missing
    )
    return payload


def ensure_capture_schema(conn):
    conn.executescript(
        """
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


def persist_capture_record(db_path, payload, summary, files, result_detected):
    db_file = Path(db_path).resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        ensure_capture_schema(conn)
        captured_at = clean_text(summary.get("captured_at")) or now_iso()
        conn.execute(
            """
            INSERT INTO case_jail_captures (
                cap_case_id,
                case_number,
                defendant_name,
                booking_number,
                inmate_name,
                dob,
                age,
                sex,
                arrest_date,
                arrest_agency,
                arrest_location,
                housing_facility,
                release_date,
                captured_at,
                result_detected,
                source_url,
                search_mode,
                search_payload_json,
                summary_json,
                html_path,
                screenshot_path,
                meta_path,
                json_path,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_text(payload.get("case_id")),
                clean_text(payload.get("case_number")),
                clean_text(payload.get("defendant_name")),
                clean_text(summary.get("booking_number")),
                clean_text(summary.get("inmate_name")),
                clean_text(summary.get("dob")),
                clean_text(summary.get("age")),
                clean_text(summary.get("sex")),
                clean_text(summary.get("arrest_date")),
                clean_text(summary.get("arrest_agency")),
                clean_text(summary.get("arrest_location")),
                clean_text(summary.get("housing_facility")),
                clean_text(summary.get("release_date")),
                captured_at,
                1 if result_detected else 0,
                clean_text(payload.get("url")),
                clean_text(payload.get("mode")),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                str(files.get("html") or ""),
                str(files.get("screenshot") or ""),
                str(files.get("meta") or ""),
                str(files.get("json") or ""),
                now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def print_instructions(payload):
    print("")
    print("Browser opened.")
    if payload["mode"] == "booking":
        print("Booking search has been prefilled and submitted.")
        print("Complete the CAPTCHA manually when prompted.")
    elif payload["mode"] == "name" and payload["can_auto_search"]:
        print("Name search has been prefilled and submitted.")
        print("Complete the CAPTCHA manually when prompted.")
    elif payload["mode"] == "name":
        missing = ", ".join(payload["missing_requirements"])
        print("Name search fields were prefilled with what we have.")
        print(f"Finish the missing search requirements in the browser: {missing}.")
        print("Then click Search by Name and complete the CAPTCHA manually.")
    else:
        print("No search fields were supplied.")
        print("Use the opened browser to perform the inmate search manually.")
        print("Complete the CAPTCHA yourself when prompted.")
    print("The helper will wait for the results panel. If it cannot detect the result automatically,")
    print("come back here and press Enter once the inmate result screen is visible.")


async def clear_and_fill(page, selector, value):
    field = page.locator(selector)
    await field.wait_for(timeout=15000)
    await field.fill(value)


async def prefill_search(page, payload):
    for key, selector in FIELD_SELECTORS.items():
        await clear_and_fill(page, selector, payload.get(key, ""))

    if payload.get("gender") in GENDER_SELECTORS:
        await page.locator(GENDER_SELECTORS[payload["gender"]]).check()

    if payload["mode"] == "booking" and payload["can_auto_search"]:
        await page.locator(SEARCH_BUTTON_SELECTORS["booking"]).click()
    elif payload["mode"] == "name" and payload["can_auto_search"]:
        await page.locator(SEARCH_BUTTON_SELECTORS["name"]).click()


async def wait_for_results(page, timeout_seconds):
    try:
        await page.wait_for_function(
            """
            () => {
              const status = document.querySelector("#divStatus");
              if (!status) {
                return false;
              }
              const style = window.getComputedStyle(status);
              const visible = style.display !== "none" && style.visibility !== "hidden";
              if (!visible) {
                return false;
              }
              const booking = document.querySelector("#celBooking");
              return Boolean(
                (booking && booking.textContent && booking.textContent.trim()) ||
                (status.textContent && status.textContent.includes("Inmate Information"))
              );
            }
            """,
            timeout=timeout_seconds * 1000,
        )
        return True
    except PlaywrightTimeoutError:
        return False


async def extract_result_summary(page):
    return await page.evaluate(
        """
        () => {
          const text = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const summary = {
            booking_number: text(document.querySelector("#celBooking")?.textContent),
            inmate_name: text(document.querySelector("#celName")?.textContent),
            alias: text(document.querySelector("#celAlias")?.textContent),
            dob: text(document.querySelector("#celDOB")?.textContent),
            age: text(document.querySelector("#celAge")?.textContent),
            sex: text(document.querySelector("#celSex")?.textContent),
          };

          const status = document.querySelector("#divStatus");
          if (!status) {
            return summary;
          }

          const normalizeKey = (label) =>
            text(label)
              .toLowerCase()
              .replace(/[^a-z0-9]+/g, "_")
              .replace(/^_+|_+$/g, "");

          for (const row of status.querySelectorAll("tr")) {
            const cells = [...row.querySelectorAll("th, td")]
              .map((cell) => text(cell.textContent))
              .filter(Boolean);
            for (let i = 0; i + 1 < cells.length; i += 2) {
              const key = normalizeKey(cells[i]);
              if (!key || summary[key]) {
                continue;
              }
              summary[key] = cells[i + 1];
            }
          }

          return summary;
        }
        """
    )


async def capture_jail_page(payload, out_dir, profile_dir, timeout_seconds, db_path, hold_open_seconds):
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1400, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(payload["url"], wait_until="domcontentloaded")
        await prefill_search(page, payload)
        print_instructions(payload)

        result_detected = await wait_for_results(page, timeout_seconds=timeout_seconds)
        if result_detected:
            print("Result panel detected automatically. Capturing now.")
        else:
            print("")
            print("Automatic result detection timed out.")
            print("Press Enter once the inmate result page you want saved is visible.")
            await asyncio.to_thread(input, "")

        summary = await extract_result_summary(page)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = out_dir / f"jail_capture_{stamp}.html"
        screenshot_path = out_dir / f"jail_capture_{stamp}.png"
        meta_path = out_dir / f"jail_capture_{stamp}.txt"
        json_path = out_dir / f"jail_capture_{stamp}.json"

        html_path.write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(screenshot_path), full_page=True)

        meta_lines = [
            f"url={page.url}",
            f"captured_at={datetime.now().isoformat(timespec='seconds')}",
            f"auto_submitted={payload['can_auto_search']}",
            f"result_detected={result_detected}",
            f"html={html_path}",
            f"screenshot={screenshot_path}",
            f"json={json_path}",
        ]
        meta_path.write_text("\n".join(meta_lines), encoding="utf-8")

        record_payload = {
            "captured_at": now_iso(),
            "url": page.url,
            "search": payload,
            "result_detected": result_detected,
            "summary": summary,
            "files": {
                "html": str(html_path),
                "screenshot": str(screenshot_path),
                "meta": str(meta_path),
                "json": str(json_path),
            },
        }

        json_path.write_text(
            json.dumps(
                record_payload,
                indent=2,
            ),
            encoding="utf-8",
        )

        summary_with_capture = dict(summary)
        summary_with_capture["captured_at"] = record_payload["captured_at"]
        persist_capture_record(
            db_path=db_path,
            payload=payload,
            summary=summary_with_capture,
            files=record_payload["files"],
            result_detected=result_detected,
        )

        print(f"Saved HTML: {html_path}")
        print(f"Saved screenshot: {screenshot_path}")
        print(f"Saved metadata: {meta_path}")
        print(f"Saved structured summary: {json_path}")
        if hold_open_seconds > 0:
            print(f"Keeping the captured result open for {hold_open_seconds} seconds before closing.")
            await page.wait_for_timeout(int(hold_open_seconds * 1000))
        print("You can close the browser window when done.")

        await context.close()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Semi-automated San Bernardino inmate locator helper. Prefills booking or name search, "
            "waits for you to complete the CAPTCHA manually, then saves the live result page."
        )
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Inmate search URL to open. Default: {DEFAULT_URL}")
    parser.add_argument("--booking", default="", help="Booking number. If present, booking search is used.")
    parser.add_argument("--last", default="", help="Last name for name search.")
    parser.add_argument("--first", default="", help="First name for name search.")
    parser.add_argument("--middle", default="", help="Middle name for name search.")
    parser.add_argument("--dob", default="", help="Date of birth for name search.")
    parser.add_argument("--age", default="", help="Approximate age for name search when DOB is unavailable.")
    parser.add_argument("--gender", default="", help="Gender for name search (M/F or Male/Female).")
    parser.add_argument("--case-id", default="", help="Optional CAP case id for storing the captured result.")
    parser.add_argument("--case-number", default="", help="Optional case number for storing the captured result.")
    parser.add_argument("--defendant-name", default="", help="Optional defendant name for storing the captured result.")
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Seconds to wait for a result panel before prompting manually.")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="Output folder for captured HTML and screenshots.")
    parser.add_argument("--profile-dir", default=str(PROFILE_DIR), help="Persistent Chromium profile directory.")
    parser.add_argument("--db", default=str(DB_PATH), help="Court browser SQLite DB for persisting captures.")
    parser.add_argument("--hold-open-seconds", type=int, default=4, help="Seconds to keep the captured result visible before closing.")
    args = parser.parse_args()

    payload = build_search_payload(args)
    asyncio.run(
        capture_jail_page(
            payload=payload,
            out_dir=Path(args.out_dir),
            profile_dir=Path(args.profile_dir),
            timeout_seconds=max(args.timeout_seconds, 30),
            db_path=Path(args.db),
            hold_open_seconds=max(args.hold_open_seconds, 0),
        )
    )


if __name__ == "__main__":
    main()
