"""Audit multi-period fiscal labels in existing OpenDART output; offline only."""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.run_offline_assembler import offline_only

PERIOD = re.compile(r"\((\d{4})\.(\d{2})\)")


def audit(path: Path):
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    multiple = []
    short = []
    unknown = []
    for record in records:
        periods = {match.group(0) for filing in record.get("filings", [])
                   for match in PERIOD.finditer(filing.get("report_name", ""))}
        if len(periods) > 1:
            multiple.append(record)
        for period in record.get("fiscal_periods", []):
            if period.get("months") is not None and period["months"] != 12:
                short.append((record, period))
            elif period.get("months") is None:
                unknown.append((record, period))
    return {"file": str(path), "records": len(records),
            "multi_period_label_records": len(multiple),
            "multi_period_label_companies": len({r["stock_code"] for r in multiple}),
            "short_period_records": len(short),
            "short_period_companies": len({r["stock_code"] for r, _ in short}),
            "unknown_length_periods": len(unknown),
            "multi_period_examples": [
                {"stock_code": r["stock_code"], "year": r["year"], "corp_name": r.get("corp_name", ""),
                 "periods": sorted({m.group(0) for f in r.get("filings", []) for m in PERIOD.finditer(f.get("report_name", ""))})}
                for r in multiple[:20]],
            "short_period_examples": [
                {"stock_code": r["stock_code"], "year": r["year"], "corp_name": r.get("corp_name", ""),
                 "period": p} for r, p in short[:20]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path,
                        default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with offline_only():
        result = audit(args.path)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
