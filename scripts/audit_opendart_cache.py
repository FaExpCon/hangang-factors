"""Audit existing disclosure pages without issuing network requests."""

import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from kr_quant.opendart import OpenDartClient


def main() -> None:
    client = OpenDartClient(api_key="offline", cache_dir=Path("cache/opendart"))
    missing = []
    raw = Counter()
    unparseable = Counter()
    non_december = Counter()
    company_years = {"Y": set(), "K": set()}
    for market in ("Y", "K"):
        current = date(2015, 1, 1)
        while current <= date(2025, 12, 31):
            end = min(current + timedelta(days=89), date(2025, 12, 31))
            page = 1
            while True:
                params = {"bgn_de": current.strftime("%Y%m%d"),
                          "end_de": end.strftime("%Y%m%d"), "pblntf_detail_ty": "A001",
                          "corp_cls": market, "page_no": str(page),
                          "page_count": "100", "last_reprt_at": "N"}
                path = client._cache_path("list", params)
                if not path.exists():
                    missing.append((market, params["bgn_de"], page))
                    break
                with open(path, encoding="utf-8") as file:
                    data = json.load(file)
                for item in data.get("list", []):
                    name = item.get("report_nm", "")
                    match = re.search(r"\((\d{4})\.(\d{2})\)", name)
                    if match is None:
                        unparseable[(market, item.get("rcept_dt", "")[:4], name)] += 1
                        continue
                    year = int(match.group(1))
                    raw[(market, year)] += 1
                    if match.group(2) != "12":
                        non_december[(market, year)] += 1
                    if item.get("corp_code"):
                        company_years[market].add((year, item["corp_code"]))
                if page >= int(data.get("total_page", 0)) or not data.get("list"):
                    break
                page += 1
            current = end + timedelta(days=1)

    for market in ("Y", "K"):
        for year in (2014, 2015):
            print(f"{market} FY{year}: {sum(y == year for y, _ in company_years[market])} distinct corp codes; "
                  f"{raw[(market, year)]} matched filings; {non_december[(market, year)]} non-December filings")
        skipped = [((filing_year, name), count) for (m, filing_year, name), count in unparseable.items() if m == market]
        print(f"{market}: {sum(count for _, count in skipped)} unparseable filing names (receipt year, name); "
              f"top examples: {sorted(skipped, key=lambda x: -x[1])[:5]}")
    print(f"Missing expected cached pages: {len(missing)}; examples: {missing[:5]}")
    print("FY2014 non-December reports filed before 2015 are outside this cached search range.")


if __name__ == "__main__":
    main()
