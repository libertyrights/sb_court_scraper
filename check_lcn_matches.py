import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB = Path("state") / "court_calendar.db"


def table_exists(conn, table):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def clean(v):
    if v is None:
        return ""
    return str(v).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--all", action="store_true", help="Show all candidates, including weak/possible.")
    parser.add_argument(
        "--min-confidence",
        default="possible",
        choices=["weak", "possible", "likely", "strong"],
        help="Minimum confidence to show. Default: possible."
    )
    args = parser.parse_args()

    db_path = Path(args.db)

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("=" * 80)
    print("LCN MATCH CHECK")
    print("=" * 80)

    if table_exists(conn, "lcn_lookup_status"):
        print("\nLookup status counts:")
        for row in conn.execute("""
            SELECT status, COUNT(*) AS count
            FROM lcn_lookup_status
            GROUP BY status
            ORDER BY count DESC
        """):
            print(f"  {row['status']}: {row['count']}")
    else:
        print("No lcn_lookup_status table found.")

    if not table_exists(conn, "case_lcn_match_candidates"):
        print("\nNo case_lcn_match_candidates table found.")
        conn.close()
        return

    total = conn.execute(
        "SELECT COUNT(*) FROM case_lcn_match_candidates"
    ).fetchone()[0]

    print(f"\nTotal LCN match candidates: {total}")

    if total == 0:
        print("\nNo LCN matches/candidates found yet.")
        print("Note: --mode queue and --mode due do not search LCN. They only prepare/list rows.")
        print("Matches only appear after --mode live-authorized or --mode import-html finds candidates.")
        conn.close()
        return

    print("\nMatch confidence counts:")
    for row in conn.execute("""
        SELECT COALESCE(match_confidence, '(blank)') AS confidence, COUNT(*) AS count
        FROM case_lcn_match_candidates
        GROUP BY COALESCE(match_confidence, '(blank)')
        ORDER BY count DESC
    """):
        print(f"  {row['confidence']}: {row['count']}")

    confidence_rank = {
        "weak": 1,
        "possible": 2,
        "likely": 3,
        "strong": 4,
    }

    min_rank = confidence_rank.get(args.min_confidence, 2)

    if args.all:
        where = "1=1"
        params = []
    else:
        allowed = [
            conf for conf, rank in confidence_rank.items()
            if rank >= min_rank
        ]
        placeholders = ",".join("?" for _ in allowed)
        where = f"COALESCE(m.match_confidence, '') IN ({placeholders})"
        params = allowed

    print("\nMatched/candidate cases:")
    print("-" * 80)

    rows = conn.execute(
        f"""
        SELECT
            m.id,
            m.lookup_id,
            m.cap_case_id,
            m.case_number,
            m.party_entity_id,
            m.lcn_person_id,
            m.lcn_arrest_id,
            m.lcn_name,
            m.lcn_arrest_date,
            m.lcn_source_agency,
            m.lcn_charge_text,
            m.name_score,
            m.alias_score,
            m.date_score,
            m.agency_score,
            m.charge_score,
            m.citation_score,
            m.total_score,
            m.match_confidence,
            m.match_basis,
            m.created_at,
            l.defendant_name,
            l.target_date,
            l.target_date_source,
            l.charge_summary
        FROM case_lcn_match_candidates m
        LEFT JOIN lcn_lookup_status l
          ON l.id = m.lookup_id
        WHERE {where}
        ORDER BY
            CASE m.match_confidence
                WHEN 'strong' THEN 4
                WHEN 'likely' THEN 3
                WHEN 'possible' THEN 2
                WHEN 'weak' THEN 1
                ELSE 0
            END DESC,
            m.total_score DESC,
            m.created_at DESC
        LIMIT ?
        """,
        params + [args.limit],
    ).fetchall()

    if not rows:
        print("No candidates matched your filter.")
        conn.close()
        return

    for r in rows:
        print()
        print(f"Candidate ID: {r['id']}")
        print(f"Confidence:   {clean(r['match_confidence'])} / score {clean(r['total_score'])}")
        print(f"Basis:        {clean(r['match_basis'])}")
        print(f"CAP case:     {clean(r['case_number'])} / cap_case_id {clean(r['cap_case_id'])}")
        print(f"Defendant:    {clean(r['defendant_name'])}")
        print(f"CAP date:     {clean(r['target_date'])} ({clean(r['target_date_source'])})")
        print(f"LCN name:     {clean(r['lcn_name'])}")
        print(f"LCN arrest:   {clean(r['lcn_arrest_date'])}")
        print(f"LCN agency:   {clean(r['lcn_source_agency'])}")
        print(f"LCN arrest ID:{clean(r['lcn_arrest_id'])}")
        print(f"LCN person ID:{clean(r['lcn_person_id'])}")
        print(f"Scores:       name={clean(r['name_score'])}, alias={clean(r['alias_score'])}, date={clean(r['date_score'])}, agency={clean(r['agency_score'])}, charge={clean(r['charge_score'])}, citation={clean(r['citation_score'])}")
        print()
        print("CAP charges:")
        charge_summary = clean(r["charge_summary"])
        if charge_summary:
            for line in charge_summary.splitlines():
                print(f"  {line}")
        else:
            print("  (none)")

        print()
        print("LCN charge:")
        print(f"  {clean(r['lcn_charge_text'])}")

    conn.close()


if __name__ == "__main__":
    main()