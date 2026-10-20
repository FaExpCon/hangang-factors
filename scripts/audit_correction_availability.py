"""Compare original-filing and correction-receipt policies using local cache only."""

import argparse
from datetime import date, timedelta
import json
from pathlib import Path

from kr_quant.bulk_cached import load_fundamentals, month_bounds
from kr_quant.company_overview import save_json


def correction_impact(fundamentals, month_members):
    """Count unavailable latest years separately from stocks with no older fallback.

    month_members maps actual signal dates to observed member tickers. A usable
    fundamental needs matched, nonzero income and equity (losses still count as
    usable data). No price/liquidity/financial eligibility assumptions are made.
    """
    affected, missing, fallback, details, monthly = set(), set(), set(), {}, []
    for signal, members in sorted(month_members.items()):
        counts = {"signal_date": str(signal), "latest_year_delayed": 0,
                  "no_usable_fundamentals": 0, "older_year_fallback": 0}
        for ticker in sorted(members):
            records = list(fundamentals.get(ticker, ()))
            original = [r for r in records if r.available_on <= signal]
            if not original:
                continue
            latest = max(original, key=lambda r: (r.period_end, r.available_on))
            if latest.income == 0 or latest.equity == 0:
                continue  # Original-date policy also supplies no usable ratios.
            if not latest.corrected or (latest.receipt_available_on is not None and latest.receipt_available_on <= signal):
                continue
            key = (ticker, latest.period_end.isoformat())
            old = [r for r in records if r.receipt_available_on is not None and r.receipt_available_on <= signal]
            old_latest = max(old, key=lambda r: (r.period_end, r.receipt_available_on)) if old else None
            affected.add(key)
            counts["latest_year_delayed"] += 1
            item = details.setdefault(key, {"ticker": ticker, "period_end": str(latest.period_end),
                                          "original_filing_date": str(latest.available_on),
                                          "receipt_date": str(latest.receipt_available_on) if latest.receipt_available_on else None,
                                          "months_with_no_fundamentals": [], "months_with_older_fallback": []})
            if old_latest is not None and old_latest.income != 0 and old_latest.equity != 0:
                fallback.add(key)
                counts["older_year_fallback"] += 1
                item["months_with_older_fallback"].append(str(signal))
            else:
                missing.add(key)
                counts["no_usable_fundamentals"] += 1
                item["months_with_no_fundamentals"].append(str(signal))
        monthly.append(counts)
    return {"affected_company_years": len(affected), "company_years_with_no_usable_fundamentals": len(missing),
            "company_years_with_older_fallback": len(fallback),
            "company_years_with_both_outcomes_in_different_months": len(missing & fallback),
            "company_years_missing_for_at_least_two_month_ends": sum(len(d["months_with_no_fundamentals"]) >= 2 for d in details.values()),
            "stock_months_with_no_usable_fundamentals": sum(m["no_usable_fundamentals"] for m in monthly),
            "stock_months_with_older_fallback": sum(m["older_year_fallback"] for m in monthly),
            "months": monthly, "company_years": list(details.values())}


def cached_month_members(cache, start, end):
    first, _ = month_bounds(start)
    _, last = month_bounds(end)
    sessions = {}
    day = first
    while day <= last:
        if day.weekday() < 5:
            path = Path(cache) / "kospi_dd_trd" / f"{day:%Y%m%d}.json"
            rows = json.loads(path.read_text(encoding="utf-8"))["OutBlock_1"]
            if rows:
                sessions[(day.year, day.month)] = day
        day += timedelta(days=1)
    result = {}
    for signal in sorted(sessions.values()):
        result[signal] = {row["ISU_SRT_CD"] for service in ("stk_isu_base_info", "ksq_isu_base_info")
                          for row in json.loads((Path(cache) / service / f"{signal:%Y%m%d}.json").read_text(encoding="utf-8"))["OutBlock_1"]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-07")
    parser.add_argument("--end", default="2020-11")
    parser.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    funds = load_fundamentals(args.financials)
    result = correction_impact(funds, cached_month_members(args.cache_dir, args.start, args.end))
    result.update(start=args.start, end=args.end, network_requests=0,
                  policy="APPROXIMATE: original filing date with final corrected values; no extra fixed lag",
                  corrected_account_company_years=sum(r.corrected for rows in funds.values() for r in rows),
                  definition="Output company-years with matched nonzero accounts; member at monthly signal. Latest delayed year may have older fallback. No strategy eligibility filters.")
    save_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ("months", "company_years")}, indent=2))
    print("CORRECTION_AUDIT_PASSED")


if __name__ == "__main__":
    main()
