import sqlite3
conn = sqlite3.connect(r'C:\Users\mark\Documents\python\sb_court_scraper\state\court_calendar.db')
# Format: [level][prefix][year2][seq6] = 11 chars
# e.g. FVI20000865 -> F=level, VI=prefix(pos2-3), 20=year, 000865=seq
cur = conn.execute("""
    SELECT 
        SUBSTR(case_number, 1, 1) as level,
        SUBSTR(case_number, 4, 2) as yr,
        COUNT(*) as cnt,
        MIN(CAST(SUBSTR(case_number, 6) AS INTEGER)) as min_seq,
        MAX(CAST(SUBSTR(case_number, 6) AS INTEGER)) as max_seq
    FROM cases 
    WHERE LENGTH(case_number) = 11
      AND SUBSTR(case_number, 2, 2) = 'VI'
    GROUP BY yr, level
    ORDER BY yr, level
""")
rows = cur.fetchall()
print(f"New-format VI cases found: {len(rows)} groups")
for r in rows:
    print(f"  {r[0]}VI{r[1]}: count={r[2]:>4}, seq_range={r[3]:>6}-{r[4]:>6}")
print()

# Also check what the max sequence per year/level was in the sequential found.log
print("Sequential found.log parsing:")
found = {}
with open(r'C:\Users\mark\Documents\python\sb_court_scraper\state\sequential_found.log') as f:
    for line in f:
        line = line.strip()
        if '|' in line and len(line) > 10:
            parts = line.split('|')
            case_num = parts[0]
            if len(case_num) == 11 and case_num[2:4] == 'VI':
                key = case_num[:4]
                seq = int(case_num[4:])
                if key not in found:
                    found[key] = []
                found[key].append(seq)
for key in sorted(found.keys()):
    seqs = found[key]
    print(f"  {key}: found {len(seqs)} cases, seq_range={min(seqs):>6}-{max(seqs):>6}")

conn.close()
