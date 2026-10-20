"""Reconcile FY2015-2024 cached filings, account batches, and output offline."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from kr_quant.opendart import OpenDartClient


def main() -> None:
    cache = Path("cache/opendart")
    client = OpenDartClient(cache_dir=cache, api_key="offline")

    def cached_only(endpoint, params, use_cache=True):
        path = client._cache_path(endpoint, params)
        if not path.exists():
            raise FileNotFoundError(f"Missing cached {endpoint} page: {path.name}")
        with path.open(encoding="utf-8") as file:
            return json.load(file)

    client._request = cached_only
    disclosures = {}
    for name, market in (("KOSPI", "Y"), ("KOSDAQ", "K")):
        disclosures[name], stats = client.get_annual_report_disclosures(list(range(2015, 2025)), market)
        print(name, "parsed company-years:", stats["total_company_years"])

    output = json.loads(Path("outputs/opendart_full/annual_financials_FY2015_2024.json").read_text(encoding="utf-8"))
    snapshot = json.loads(Path("outputs/opendart_full/listing_snapshot_20261015.json").read_text(encoding="utf-8"))
    present = {(record["year"], record["stock_code"]) for record in output}
    listed = set(snapshot["stock_codes"])
    missing = []
    all_filings = []
    code_owners = defaultdict(set)
    for name in ("KOSPI", "KOSDAQ"):
        for year, reports in disclosures[name].items():
            for report in reports:
                all_filings.append((year, name, report))
                code_owners[(year, report["stock_code"])].add(report["corp_code"])
                if (year, report["stock_code"]) not in present:
                    missing.append((year, name, report))
    for label, rows in (("disclosures", all_filings), ("missing", missing)):
        print(label, "company-years:", len(rows), "unique corp_codes:", len({r["corp_code"] for _, _, r in rows}),
              "empty stock_code:", sum(not r["stock_code"] for _, _, r in rows),
              "not in KRX snapshot:", sum(r["stock_code"] not in listed for _, _, r in rows))
    print("output company-years:", len(output), "unique stock codes:", len({r["stock_code"] for r in output}))
    print("disclosure unique corp_codes:", len({r["corp_code"] for _, _, r in all_filings}),
          "company-years:", len({(year, r["corp_code"]) for year, _, r in all_filings}))
    collisions = {key: owners for key, owners in code_owners.items() if len(owners) > 1}
    print("same-year stock-code collisions between corp_codes:", len(collisions),
          "examples:", list(collisions.items())[:5])
    print("missing per year:", dict(sorted(Counter(year for year, _, _ in missing).items())))
    print("missing stock_code samples:", [(year, name, r["corp_code"], r["stock_code"]) for year, name, r in missing[:12]])
    yearly = {year: {r["stock_code"] for fy, _, r in all_filings if fy == year}
              for year in range(2015, 2025)}
    print("first-year codes absent in final-year filings:", len(yearly[2015] - yearly[2024]))
    print("2015 filers absent from current KRX snapshot:", len(yearly[2015] - listed))
    print("account cache files:", len(list((cache / "fnlttMultiAcnt").glob("*.json"))))
    print("NOTE: response-only account cache does not record requested corp_code batches;")
    print("      it cannot prove whether absent filers were ever requested.")


if __name__ == "__main__":
    main()
