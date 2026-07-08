import argparse
import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "state" / "court_calendar.db"
DEFAULT_OUT = SCRIPT_DIR / "output" / "caseid_sequence_report.csv"


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_int(value):
    value = clean(value)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_date(value):
    value = clean(value)
    if not value:
        return ""

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass

    return value


def parse_case_number(case_number):
    """
    Rough parser for things like:
      FVI26001968
      MVI26001234
      TVI26000001

    Returns:
      prefix_letters, year_2, sequence_int
    """
    case_number = clean(case_number).upper()

    m = re.match(r"^([A-Z]+)(\d{2})(\d+)$", case_number)
    if not m:
        return "", "", None

    prefix = m.group(1)
    yy = m.group(2)
    seq = int(m.group(3))

    return prefix, yy, seq


def table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def columns(conn, table):
    if not table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def pick_col(cols, *names):
    for name in names:
        if name in cols:
            return name
    return None


def load_cases(conn):
    """
    Pulls from cases and calendar_appearances because some cases may have
    calendar rows before detail rows.
    """
    rows = {}

    if table_exists(conn, "cases"):
        cols = columns(conn, "cases")

        cap_col = pick_col(cols, "cap_case_id", "case_id")
        num_col = pick_col(cols, "case_number")
        file_col = pick_col(cols, "file_date")
        type_col = pick_col(cols, "case_type", "type")
        loc_col = pick_col(cols, "court_location")
        status_col = pick_col(cols, "status")

        if cap_col:
            select_cols = [cap_col]
            for c in [num_col, file_col, type_col, loc_col, status_col]:
                if c:
                    select_cols.append(c)

            sql = f'SELECT {", ".join(select_cols)} FROM cases'

            for dbrow in conn.execute(sql):
                d = dict(zip(select_cols, dbrow))
                cap_id = clean(d.get(cap_col))
                cap_int = parse_int(cap_id)
                if cap_int is None:
                    continue

                rows[cap_id] = {
                    "cap_case_id": cap_id,
                    "cap_case_id_int": cap_int,
                    "case_number": clean(d.get(num_col)) if num_col else "",
                    "file_date": parse_date(d.get(file_col)) if file_col else "",
                    "case_type": clean(d.get(type_col)) if type_col else "",
                    "court_location": clean(d.get(loc_col)) if loc_col else "",
                    "status": clean(d.get(status_col)) if status_col else "",
                    "first_calendar_date": "",
                    "last_calendar_date": "",
                    "departments": set(),
                }

    if table_exists(conn, "calendar_appearances"):
        cols = columns(conn, "calendar_appearances")

        cap_col = pick_col(cols, "cap_case_id", "case_id")
        num_col = pick_col(cols, "case_number")
        date_col = pick_col(cols, "calendar_date", "session_date")
        dept_col = pick_col(cols, "courtroom_code", "courtroom_text")
        type_col = pick_col(cols, "case_type_text")

        if cap_col:
            select_cols = [cap_col]
            for c in [num_col, date_col, dept_col, type_col]:
                if c:
                    select_cols.append(c)

            sql = f'SELECT {", ".join(select_cols)} FROM calendar_appearances'

            for dbrow in conn.execute(sql):
                d = dict(zip(select_cols, dbrow))
                cap_id = clean(d.get(cap_col))
                cap_int = parse_int(cap_id)
                if cap_int is None:
                    continue

                if cap_id not in rows:
                    rows[cap_id] = {
                        "cap_case_id": cap_id,
                        "cap_case_id_int": cap_int,
                        "case_number": clean(d.get(num_col)) if num_col else "",
                        "file_date": "",
                        "case_type": clean(d.get(type_col)) if type_col else "",
                        "court_location": "",
                        "status": "",
                        "first_calendar_date": "",
                        "last_calendar_date": "",
                        "departments": set(),
                    }
                else:
                    if not rows[cap_id]["case_number"] and num_col:
                        rows[cap_id]["case_number"] = clean(d.get(num_col))
                    if not rows[cap_id]["case_type"] and type_col:
                        rows[cap_id]["case_type"] = clean(d.get(type_col))

                cal_date = parse_date(d.get(date_col)) if date_col else ""
                if cal_date:
                    cur_first = rows[cap_id]["first_calendar_date"]
                    cur_last = rows[cap_id]["last_calendar_date"]

                    if not cur_first or cal_date < cur_first:
                        rows[cap_id]["first_calendar_date"] = cal_date
                    if not cur_last or cal_date > cur_last:
                        rows[cap_id]["last_calendar_date"] = cal_date

                if dept_col and clean(d.get(dept_col)):
                    rows[cap_id]["departments"].add(clean(d.get(dept_col)))

    out = []

    for row in rows.values():
        prefix, yy, seq = parse_case_number(row["case_number"])
        row["case_prefix"] = prefix
        row["case_year_2"] = yy
        row["case_sequence"] = seq
        row["departments"] = ",".join(sorted(row["departments"]))
        out.append(row)

    return out


def analyze(rows):
    print(f"Total unique CAP case IDs observed: {len(rows)}")

    if not rows:
        return

    ids = sorted(r["cap_case_id_int"] for r in rows)
    print(f"Lowest observed caseId: {ids[0]}")
    print(f"Highest observed caseId: {ids[-1]}")
    print(f"Observed ID span: {ids[-1] - ids[0] + 1}")
    print(f"Observed density inside span: {len(ids) / max(1, (ids[-1] - ids[0] + 1)):.6f}")

    gaps = []
    for a, b in zip(ids, ids[1:]):
        if b - a > 1:
            gaps.append(b - a - 1)

    print(f"Gaps between observed IDs: {len(gaps)}")
    if gaps:
        print(f"Median-ish gap: {sorted(gaps)[len(gaps)//2]}")
        print(f"Max gap: {max(gaps)}")

    with_file_dates = [r for r in rows if r["file_date"] and re.match(r"^\d{4}-\d{2}-\d{2}$", r["file_date"])]
    print(f"Rows with parsed file_date: {len(with_file_dates)}")

    if len(with_file_dates) >= 2:
        by_date = sorted(with_file_dates, key=lambda r: (r["file_date"], r["cap_case_id_int"]))

        inversions = 0
        prev_id = None
        examples = []

        for r in by_date:
            if prev_id is not None and r["cap_case_id_int"] < prev_id:
                inversions += 1
                if len(examples) < 10:
                    examples.append(r)
            prev_id = max(prev_id or r["cap_case_id_int"], r["cap_case_id_int"])

        print(f"Date-order inversions: {inversions}")

        if inversions == 0:
            print("Result: observed file_date order is consistent with mostly increasing caseId.")
        else:
            print("Result: observed data has inversions, so caseId is not strictly file-date sequential in this sample.")

            print("\nExample inversion rows:")
            for r in examples:
                print(f"  {r['file_date']} | {r['cap_case_id']} | {r['case_number']} | {r['case_type']}")

    groups = {}

    for r in rows:
        key = (r["case_prefix"], r["case_year_2"])
        if not key[0] or r["case_sequence"] is None:
            continue
        groups.setdefault(key, []).append(r)

    print("\nPublic case-number sequence groups:")
    for key, group in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        seqs = sorted(r["case_sequence"] for r in group if r["case_sequence"] is not None)
        if not seqs:
            continue

        print(
            f"  {key[0]}{key[1]}: "
            f"observed={len(seqs)}, "
            f"min_seq={min(seqs)}, "
            f"max_seq={max(seqs)}, "
            f"density={len(seqs) / max(1, (max(seqs) - min(seqs) + 1)):.4f}"
        )

    # Check whether public sequence and internal ID increase together within each prefix/year.
    print("\nWithin public case-number prefix/year, sequence vs caseId test:")

    for key, group in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        group = [r for r in group if r["case_sequence"] is not None]
        if len(group) < 5:
            continue

        by_seq = sorted(group, key=lambda r: r["case_sequence"])
        inversions = 0
        prev_id = None

        for r in by_seq:
            if prev_id is not None and r["cap_case_id_int"] < prev_id:
                inversions += 1
            prev_id = max(prev_id or r["cap_case_id_int"], r["cap_case_id_int"])

        print(
            f"  {key[0]}{key[1]}: records={len(group)}, "
            f"caseId inversions when sorted by public sequence={inversions}"
        )


def write_csv(rows, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "cap_case_id",
        "cap_case_id_int",
        "case_number",
        "case_prefix",
        "case_year_2",
        "case_sequence",
        "file_date",
        "first_calendar_date",
        "last_calendar_date",
        "case_type",
        "court_location",
        "status",
        "departments",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for r in sorted(rows, key=lambda x: x["cap_case_id_int"]):
            writer.writerow({k: r.get(k, "") for k in fields})

    print(f"\nWrote CSV: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    db_path = Path(args.db)

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = load_cases(conn)
    conn.close()

    analyze(rows)
    write_csv(rows, Path(args.out))


if __name__ == "__main__":
    main()