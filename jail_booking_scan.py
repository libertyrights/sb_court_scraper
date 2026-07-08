import argparse
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from manual_jail_capture import (
    DEFAULT_URL,
    FIELD_SELECTORS,
    PROFILE_DIR,
    clean_text,
    extract_result_summary,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "state" / "court_calendar.db"
OUT_DIR = SCRIPT_DIR / "output" / "jail_import"


def digits_only(value):
    return "".join(ch for ch in clean_text(value) if ch.isdigit())


def booking_sequence(start_booking, end_booking, step):
    start_value = int(digits_only(start_booking))
    end_value = int(digits_only(end_booking))
    width = max(10, len(digits_only(start_booking)), len(digits_only(end_booking)))
    if step <= 0:
        raise ValueError("Step must be positive.")
    if end_value < start_value:
        raise ValueError("End booking must be greater than or equal to start booking.")
    return [str(value).zfill(width) for value in range(start_value, end_value + 1, step)]


def db_connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jail_booking_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            booking_number TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            detail_status TEXT NOT NULL,
            bypass_state TEXT,
            inmate_name TEXT,
            alias TEXT,
            sex TEXT,
            dob TEXT,
            age TEXT,
            height TEXT,
            weight TEXT,
            arrest_date TEXT,
            arrest_location TEXT,
            arrest_agency TEXT,
            housing_facility TEXT,
            release_date TEXT,
            released_to TEXT,
            status_message TEXT,
            raw_summary_json TEXT NOT NULL,
            UNIQUE(run_id, booking_number)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jail_booking_scans_booking
        ON jail_booking_scans (booking_number)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jail_booking_scans_arrest_date
        ON jail_booking_scans (arrest_date)
        """
    )
    conn.commit()
    return conn


async def open_search_page(page, url):
    await page.goto(url, wait_until="domcontentloaded")
    await page.locator(FIELD_SELECTORS["booking"]).wait_for(timeout=15000)


async def clear_search(page):
    try:
        await page.evaluate("clearScreen()")
    except Exception:
        await page.goto(DEFAULT_URL, wait_until="domcontentloaded")
    await page.locator(FIELD_SELECTORS["booking"]).wait_for(timeout=15000)


async def inspect_page_state(page):
    return await page.evaluate(
        """
        () => {
          const visibleText = (selector) => {
            const el = document.querySelector(selector);
            if (!el) {
              return "";
            }
            const style = window.getComputedStyle(el);
            if (style.display === "none" || style.visibility === "hidden") {
              return "";
            }
            return (el.textContent || "").replace(/\\s+/g, " ").trim();
          };

          const isVisible = (selector) => {
            const el = document.querySelector(selector);
            if (!el) {
              return false;
            }
            const style = window.getComputedStyle(el);
            return style.display !== "none" && style.visibility !== "hidden";
          };

          const booking = visibleText("#celBooking");
          return {
            booking,
            detail_visible: isVisible("#divStatus") && booking.length > 0,
            captcha_visible: isVisible("#divCaptcha"),
            results_text: visibleText("#spnResults"),
            status_message: visibleText("#spnStatusMessage"),
            bypass_state: ((document.querySelector("#hdnBypassCaptcha") || {}).value || "").trim(),
          };
        }
        """
    )


async def wait_for_terminal_state(page, timeout_seconds):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        state = await inspect_page_state(page)
        if state["detail_visible"]:
            return state, "detail"
        if state["results_text"] and "Searching Inmates" not in state["results_text"]:
            return state, "message"
        if state["status_message"] and "Getting Booking Information" not in state["status_message"]:
            return state, "message"
        await asyncio.sleep(0.5)
    return await inspect_page_state(page), "timeout"


async def search_booking(page, booking_number, timeout_seconds):
    await clear_search(page)
    await page.locator(FIELD_SELECTORS["booking"]).fill(booking_number)
    await page.locator("input[value='Search by Booking']").click()

    state, terminal_kind = await wait_for_terminal_state(page, timeout_seconds=2)
    if state["captcha_visible"]:
        print("")
        print(f"[{booking_number}] CAPTCHA is visible.")
        print("Complete it in the browser, wait for the page to settle, then press Enter here.")
        await asyncio.to_thread(input, "")
        state, terminal_kind = await wait_for_terminal_state(page, timeout_seconds=timeout_seconds)

    summary = {}
    if terminal_kind == "detail":
        summary = await extract_result_summary(page)

    status_message = clean_text(state.get("results_text") or state.get("status_message"))
    if terminal_kind == "timeout" and not status_message:
        status_message = "Timed out waiting for inmate detail or result message."

    return {
        "booking_number": booking_number,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "detail_status": terminal_kind,
        "bypass_state": clean_text(state.get("bypass_state")),
        "status_message": status_message,
        "summary": summary,
    }


def write_result(conn, out_path, run_id, result):
    summary = dict(result.get("summary") or {})
    record = {
        "run_id": run_id,
        "booking_number": clean_text(result.get("booking_number")),
        "captured_at": clean_text(result.get("captured_at")),
        "detail_status": clean_text(result.get("detail_status")),
        "bypass_state": clean_text(result.get("bypass_state")),
        "status_message": clean_text(result.get("status_message")),
        "summary": summary,
    }

    conn.execute(
        """
        INSERT OR REPLACE INTO jail_booking_scans (
            run_id,
            booking_number,
            captured_at,
            detail_status,
            bypass_state,
            inmate_name,
            alias,
            sex,
            dob,
            age,
            height,
            weight,
            arrest_date,
            arrest_location,
            arrest_agency,
            housing_facility,
            release_date,
            released_to,
            status_message,
            raw_summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["run_id"],
            record["booking_number"],
            record["captured_at"],
            record["detail_status"],
            record["bypass_state"],
            clean_text(summary.get("inmate_name")),
            clean_text(summary.get("alias")),
            clean_text(summary.get("sex")),
            clean_text(summary.get("dob")),
            clean_text(summary.get("age")),
            clean_text(summary.get("height")),
            clean_text(summary.get("weight")),
            clean_text(summary.get("arrest_date")),
            clean_text(summary.get("arrest_location")),
            clean_text(summary.get("arrest_agency")),
            clean_text(summary.get("housing_facility")),
            clean_text(summary.get("release_date")),
            clean_text(summary.get("released_to")),
            record["status_message"],
            json.dumps(summary, sort_keys=True),
        ),
    )
    conn.commit()

    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=True) + "\n")


async def run_scan(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"booking_scan_{run_id}.jsonl"
    bookings = booking_sequence(args.start_booking, args.end_booking, args.step)
    conn = db_connect(Path(args.db_path))

    successes = 0
    detail_failures = 0
    timeouts = 0

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile_dir)),
            headless=False,
            viewport={"width": 1400, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await open_search_page(page, clean_text(args.url) or DEFAULT_URL)

        print("")
        print(f"Starting booking scan for {len(bookings)} booking numbers.")
        print(f"Results will be appended to: {out_path}")
        print("If a CAPTCHA appears, solve it in the browser and press Enter here.")

        for index, booking_number in enumerate(bookings, start=1):
            print(f"[{index}/{len(bookings)}] Searching booking {booking_number} ...")
            try:
                result = await search_booking(page, booking_number, timeout_seconds=args.timeout_seconds)
            except PlaywrightTimeoutError as exc:
                result = {
                    "booking_number": booking_number,
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "detail_status": "timeout",
                    "bypass_state": "",
                    "status_message": f"Playwright timeout: {exc}",
                    "summary": {},
                }

            write_result(conn, out_path, run_id, result)
            summary = result.get("summary") or {}
            if result["detail_status"] == "detail" and summary.get("booking_number"):
                successes += 1
                print(
                    f"  Captured {clean_text(summary.get('inmate_name')) or booking_number}"
                    f" | arrest date: {clean_text(summary.get('arrest_date')) or 'n/a'}"
                )
            elif result["detail_status"] == "timeout":
                timeouts += 1
                print(f"  Timed out: {result['status_message']}")
            else:
                detail_failures += 1
                print(f"  No detail: {result['status_message'] or 'No visible detail returned.'}")

        await context.close()
    conn.close()

    print("")
    print("Booking scan complete.")
    print(f"Detail captures: {successes}")
    print(f"No-detail results: {detail_failures}")
    print(f"Timeouts: {timeouts}")
    print(f"Saved log: {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Semi-automated sequential jail booking scan. Walks a booking range in the live Sheriff locator, "
            "captures detail when available, and pauses for human CAPTCHA solves when needed."
        )
    )
    parser.add_argument("--start-booking", required=True, help="First booking number in the scan range.")
    parser.add_argument("--end-booking", required=True, help="Last booking number in the scan range.")
    parser.add_argument("--step", type=int, default=1, help="Booking increment. Default: 1")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Locator URL. Default: {DEFAULT_URL}")
    parser.add_argument("--timeout-seconds", type=int, default=45, help="Wait time after each booking search.")
    parser.add_argument("--profile-dir", default=str(PROFILE_DIR), help="Persistent Chromium profile directory.")
    parser.add_argument("--db-path", default=str(DB_PATH), help="SQLite DB path for saving booking scan results.")
    args = parser.parse_args()

    asyncio.run(run_scan(args))


if __name__ == "__main__":
    main()
