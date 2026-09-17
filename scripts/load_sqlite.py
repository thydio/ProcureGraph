"""Load the generated CSVs into SQLite (data/procuregraph.db)."""

import os
import sqlite3

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
DB = os.path.join(ROOT, "data", "procuregraph.db")

TABLES = {
    "buyers": "buyers.csv",
    "vendors": "vendors.csv",
    "vendor_persons": "vendor_persons.csv",
    "tenders": "tenders.csv",
    "bids": "bids.csv",
    "awards": "awards.csv",
    "contracts": "contracts.csv",
    "amendments": "amendments.csv",
    "tender_items": "tender_items.csv",
    "thresholds": "thresholds.csv",
    "suppression_list": "suppression_list.csv",
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_bids_tender ON bids(tender_id)",
    "CREATE INDEX IF NOT EXISTS ix_bids_vendor ON bids(vendor_id)",
    "CREATE INDEX IF NOT EXISTS ix_tenders_buyer ON tenders(buyer_id)",
    "CREATE INDEX IF NOT EXISTS ix_tenders_cat ON tenders(category_code)",
    "CREATE INDEX IF NOT EXISTS ix_tenders_id ON tenders(tender_id)",
    "CREATE INDEX IF NOT EXISTS ix_amend_contract ON amendments(contract_id)",
    "CREATE INDEX IF NOT EXISTS ix_persons_vendor ON vendor_persons(vendor_id)",
]


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    for table, fname in TABLES.items():
        path = os.path.join(RAW, fname)
        df = pd.read_csv(path)
        df.to_sql(table, con, if_exists="replace", index=False)
        print(f"{table:20s} {len(df):6d} rows")
    for stmt in INDEXES:
        con.execute(stmt)
    con.commit()
    con.close()
    print(f"\nwrote {DB}")


if __name__ == "__main__":
    main()
