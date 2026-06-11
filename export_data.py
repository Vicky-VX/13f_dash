"""
export_data.py — Export SQLite tables to CSV for Streamlit Cloud deployment
Usage: python export_data.py
"""

import sqlite3
import json
import csv
import os
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

DB_PATH = BASE / "holdings.db"


def export_table(conn, table: str, path: Path):
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)
    print(f"  Exported {table}: {len(rows)} rows -> {path.name}")


def export_json_file(src: Path, dst: Path):
    if src.exists():
        import shutil
        shutil.copy2(src, dst)
        print(f"  Copied {src.name} -> {dst.name}")


def main():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        return

    conn = sqlite3.connect(str(DB_PATH))

    print("Exporting holdings.db tables...")
    export_table(conn, "funds",    DATA / "funds.csv")
    export_table(conn, "filings",  DATA / "filings.csv")
    export_table(conn, "holdings", DATA / "holdings.csv")

    conn.close()

    print("Copying JSON caches...")
    for fname in ["cusip_ticker_cache.json", "sector_cache.json"]:
        export_json_file(BASE / fname, DATA / fname)

    print(f"\nDone. Files in {DATA}/")
    for f in sorted(DATA.iterdir()):
        size = f.stat().st_size
        print(f"  {f.name:35s} {size:>10,} bytes")


if __name__ == "__main__":
    main()
