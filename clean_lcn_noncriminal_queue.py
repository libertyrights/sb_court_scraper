import sqlite3
from pathlib import Path


DB = Path("state") / "court_calendar.db"


def main():
    conn = sqlite3.connect(DB)

    before = conn.execute("SELECT COUNT(*) FROM lcn_lookup_status").fetchone()[0]

    conn.execute(
        """
        DELETE FROM lcn_lookup_status
        WHERE
            UPPER(COALESCE(case_number, '')) LIKE 'CIV%'
            OR UPPER(COALESCE(case_number, '')) LIKE 'FL%'
            OR UPPER(COALESCE(case_number, '')) LIKE 'FAM%'
            OR UPPER(COALESCE(case_number, '')) LIKE 'PRO%'
            OR UPPER(COALESCE(case_number, '')) LIKE 'PR%'
            OR UPPER(COALESCE(case_number, '')) LIKE 'APP%'
            OR UPPER(COALESCE(case_number, '')) LIKE 'UD%'
            OR UPPER(COALESCE(defendant_name, '')) LIKE 'DOES %'
            OR UPPER(COALESCE(defendant_name, '')) LIKE '% TRUST%'
            OR UPPER(COALESCE(defendant_name, '')) LIKE '% LLC%'
            OR UPPER(COALESCE(defendant_name, '')) LIKE '% INC%'
            OR UPPER(COALESCE(defendant_name, '')) LIKE '% CORPORATION%'
        """
    )

    conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM lcn_lookup_status").fetchone()[0]
    conn.close()

    print(f"Before: {before}")
    print(f"After:  {after}")
    print(f"Deleted: {before - after}")


if __name__ == "__main__":
    main()