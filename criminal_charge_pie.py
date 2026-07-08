import argparse
import sqlite3
import re
from pathlib import Path
from collections import Counter

import matplotlib.pyplot as plt


DEFAULT_DB = Path("state") / "court_calendar.db"
DEFAULT_OUT = Path("output") / "criminal_charge_pie.png"


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def is_probably_criminal_case_number(case_number: str) -> bool:
    cn = clean(case_number).upper()
    if not cn:
        return False

    civil_prefixes = (
        "CIV", "FL", "FAM", "PRO", "PR", "APP", "UD"
    )
    if cn.startswith(civil_prefixes):
        return False

    return True


def get_case_number_map(conn):
    case_map = {}

    tables = ["cases", "cases_probe"]
    for table in tables:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not row:
            continue

        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if "cap_case_id" not in cols:
            continue

        case_number_col = None
        for c in ["case_number", "caseNbr", "case_no"]:
            if c in cols:
                case_number_col = c
                break

        if not case_number_col:
            continue

        sql = f'''
            SELECT cap_case_id, "{case_number_col}"
            FROM "{table}"
            WHERE cap_case_id IS NOT NULL
              AND cap_case_id <> ''
        '''

        for cap_case_id, case_number in conn.execute(sql):
            case_map[clean(cap_case_id)] = clean(case_number)

    return case_map


def charge_label_from_row(row, mode="statute_desc"):
    statute_prefix = clean(row["statute_prefix"]).upper()
    statute_number = clean(row["statute_number"]).upper()
    statute_suffix = clean(row["statute_suffix"]).upper()
    offense_description = clean(row["offense_description"])

    statute = f"{statute_prefix}{statute_number}{statute_suffix}"

    # If description looks like "HS11358(d)(3)-F: Cultivating ..." strip the leading code
    desc_only = offense_description
    if ":" in offense_description:
        left, right = offense_description.split(":", 1)
        if re.search(r"[A-Z]{1,5}\s*\d", left.upper()):
            desc_only = clean(right)

    if mode == "statute":
        if statute:
            return statute
        return offense_description or "(Unknown Charge)"

    if mode == "description":
        return desc_only or offense_description or "(Unknown Charge)"

    # default: statute_desc
    if statute and desc_only:
        return f"{statute}: {desc_only}"
    if statute:
        return statute
    if offense_description:
        return offense_description
    return "(Unknown Charge)"


def main():
    parser = argparse.ArgumentParser(description="Create pie chart of criminal cases by charge.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to SQLite database")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output image file")
    parser.add_argument("--top", type=int, default=10, help="Top N charges to show individually")
    parser.add_argument(
        "--mode",
        choices=["statute", "description", "statute_desc"],
        default="statute_desc",
        help="How to group charges"
    )
    parser.add_argument(
        "--min-cases",
        type=int,
        default=1,
        help="Only include charges appearing in at least this many distinct cases"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Need case_charges
    has_case_charges = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='case_charges'"
    ).fetchone()
    if not has_case_charges:
        conn.close()
        raise SystemExit("Table case_charges not found.")

    case_number_map = get_case_number_map(conn)

    # Count each charge only once per case
    seen_case_charge = set()
    counter = Counter()

    sql = """
        SELECT
            cap_case_id,
            charge_id,
            charge_number,
            offense_description,
            statute_prefix,
            statute_number,
            statute_suffix
        FROM case_charges
        WHERE cap_case_id IS NOT NULL
          AND cap_case_id <> ''
    """

    total_charge_rows = 0
    total_distinct_case_charge_pairs = 0

    for row in conn.execute(sql):
        total_charge_rows += 1

        cap_case_id = clean(row["cap_case_id"])
        case_number = case_number_map.get(cap_case_id, "")

        if not is_probably_criminal_case_number(case_number):
            continue

        label = charge_label_from_row(row, mode=args.mode)
        key = (cap_case_id, label)

        if key in seen_case_charge:
            continue

        seen_case_charge.add(key)
        counter[label] += 1
        total_distinct_case_charge_pairs += 1

    conn.close()

    # Filter low-count charges if requested
    items = [(label, count) for label, count in counter.items() if count >= args.min_cases]
    items.sort(key=lambda x: x[1], reverse=True)

    if not items:
        raise SystemExit("No criminal charge data found after filtering.")

    top_n = max(1, args.top)
    top_items = items[:top_n]
    other_items = items[top_n:]

    labels = [label for label, _ in top_items]
    sizes = [count for _, count in top_items]

    other_sum = sum(count for _, count in other_items)
    if other_sum > 0:
        labels.append("Other")
        sizes.append(other_sum)

    total_cases_with_charges = sum(count for _, count in items)

    # Make chart
    plt.figure(figsize=(12, 12))
    plt.pie(
        sizes,
        labels=labels,
        autopct=lambda pct: f"{pct:.1f}%\n({int(round(pct/100*sum(sizes)))})",
        startangle=90
    )
    plt.title(
        f"Criminal Cases by Charge\n"
        f"Top {top_n} charge groups ({args.mode})\n"
        f"Distinct case/charge pairs counted: {total_cases_with_charges}"
    )
    plt.axis("equal")
    plt.tight_layout()
    plt.savefig(out_path, dpi=200)
    plt.close()

    print(f"Wrote chart: {out_path}")
    print()
    print("Top charges:")
    for i, (label, count) in enumerate(top_items, start=1):
        pct = (count / total_cases_with_charges) * 100 if total_cases_with_charges else 0
        print(f"{i:2d}. {label} -> {count} cases ({pct:.1f}%)")

    if other_sum:
        pct = (other_sum / total_cases_with_charges) * 100 if total_cases_with_charges else 0
        print(f"    Other -> {other_sum} cases ({pct:.1f}%)")


if __name__ == "__main__":
    main()