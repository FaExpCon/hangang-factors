"""Audit local KRX/OpenDART coverage; no network unless --fetch-corp-master."""

import argparse
import json
from pathlib import Path

from kr_quant.dart_gap import AUDIT, OUTPUT, GapClient, audit_gap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-corp-master", action="store_true",
                        help="Fetch full official corporation ZIP once if not cached (1 OpenDART request)")
    parser.add_argument("--krx-cache", type=Path, default=Path("data/cache/krx_open_api/v1"))
    parser.add_argument("--dart-cache", type=Path, default=Path("cache/opendart"))
    parser.add_argument("--output-file", type=Path, default=OUTPUT)
    parser.add_argument("--audit-file", type=Path, default=AUDIT)
    args = parser.parse_args()
    if args.fetch_corp_master:
        GapClient(args.dart_cache).corp_master()
    result = audit_gap(args.krx_cache, args.dart_cache, args.output_file, args.audit_file)
    write_inventory(result, args.audit_file.with_suffix(".md"))
    print(json.dumps({k: result[k] for k in ("yearly", "no_records_any_year", "mapping_counts",
                                            "corp_master", "requests")}, ensure_ascii=True, indent=2))
    print(f"Audit and complete company list: {args.audit_file}")


def write_inventory(result, path):
    lines = ["# OpenDART common-stock coverage gap inventory", "",
             "All codes below lack at least one annual row while observed in local KRX data.",
             "`Never` means no annual row in any year of the existing FY2015-FY2024 output.",
             "The downloader targets every absent FY2015-FY2024 for these companies.", "",
             "| Stock code | Historical KRX names | Never | Missing observed years | Corp code | Mapping |",
             "| --- | --- | --- | --- | --- | --- |"]
    for c in result["companies"]:
        names = ", ".join(c["names"]).replace("|", "\\|")
        years = ", ".join(str(y) for y in c["missing_years"])
        lines.append(f"| {c['stock_code']} | {names} | {'yes' if c['no_records_any_year'] else 'no'} | "
                     f"{years} | {c['mapping']['corp_code'] or ''} | {c['mapping']['status']} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
