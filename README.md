# SB Court Scraper

San Bernardino County CAP court calendar scraper, sequential case number scanner, LCN article matcher, and jail booking scanner.

## Components

- **`vv_court_criminal_calendar_watch.py`** — Playwright-based scraper for the CAP portal (Victorville criminal calendar). Logs in, scrapes calendar appearances, and crawls case details.
- **`sequential_case_scraper.py`** — Scans sequential case numbers via the CAP validate API (no auth required) to discover unindexed cases.
- **`continuous_case_scanner.py`** — Background wrapper that runs the sequential scanner in crash-recovery mode.
- **`lcn_scrape.py`** — Local Crime News article matcher. Links court charges to LCN arrest articles.
- **`jail_booking_scan.py`** — Scans booking numbers on the jail inmate locator.
- **`court_data_browser.py`** — Flask web UI for browsing the court database with cross-references to LCN, jail, call log, and property records.
- **`upload_court_calendar_db.py`** — Uploads the SQLite database to serv00 storage.
- **`fetch_db.py`** — Downloads the current database from serv00 at workflow start.
- **`upload_status.py`** — Posts a `scraper_status.json` to the serv00 dashboard.

## Infrastructure

| Resource | Location |
|---|---|
| Source code | `github.com/libertyrights/sb_court_scraper` |
| DB storage | serv00.com (`upnexx.xyz/osint/private/`) |
| Status dashboard | `upnexx.xyz/status/` |
| Schedule | GitHub Actions weekdays 12:00 UTC |
| Local fallback | Windows Task Scheduler 8:00 AM weekdays |

## Required Secrets (GitHub Actions)

- `CAP_USERNAME`, `CAP_PASSWORD` — CAP portal login
- `SERV00_FTP_HOST`, `SERV00_FTP_USER`, `SERV00_FTP_PASS` — serv00 FTP for DB storage + status
