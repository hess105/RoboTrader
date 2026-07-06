"""Export realized gains/losses for a tax year in Form 8949 column layout.

  python -m scripts.export_tax_csv --year 2026 --out exports/8949_2026.csv
"""
from __future__ import annotations

import argparse

from core.settings import load_settings
from journal.tax import TaxLedger


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    db = load_settings().raw["logging"]["audit_db"]
    n = TaxLedger(db).export_8949_csv(args.year, args.out)
    print(f"wrote {n} realized rows for {args.year} to {args.out}")
    print("Reminder: the broker's 1099-B is authoritative for filing; "
          "this export is for verification and estimated-tax planning.")


if __name__ == "__main__":
    main()
