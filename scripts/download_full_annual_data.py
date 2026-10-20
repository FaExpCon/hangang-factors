"""Download FY2015-FY2024 annual financial data for KOSPI and KOSDAQ filers.

OpenDART's multi-company accounts API begins with FY2015.
All responses are cached, so interrupting and resuming is safe.
"""
import json
from datetime import date, timedelta
from pathlib import Path
from kr_quant.opendart import OpenDartClient, download_annual_financials
from kr_quant.krx import KRXClient


def listing_snapshot(snapshot_date: date, output_dir: Path) -> set[str]:
    """Use approved KRX basic-info services, caching the dated membership."""
    snapshot_file = output_dir / f"listing_snapshot_{snapshot_date:%Y%m%d}.json"
    if snapshot_file.exists():
        with open(snapshot_file, encoding="utf-8") as file:
            saved = json.load(file)
        return set(saved["stock_codes"])

    krx = KRXClient(daily_limit=2)
    codes = set()
    for service in ("stk_isu_base_info", "ksq_isu_base_info"):
        rows = krx.get(service, snapshot_date)["OutBlock_1"]
        if not rows:
            raise ValueError(f"Empty KRX listing snapshot for {service}")
        codes.update(row["ISU_SRT_CD"] for row in rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(snapshot_file, "w", encoding="utf-8") as file:
        json.dump({"as_of": snapshot_date.isoformat(), "stock_codes": sorted(codes)}, file)
    print(f"KRX listing snapshot: {len(codes)} codes as of {snapshot_date}")
    return codes

if __name__ == "__main__":
    client = OpenDartClient()
    
    fiscal_years = list(range(2015, 2025))
    
    print("Starting full market download...")
    print(f"Fiscal years: FY{fiscal_years[0]} through FY{fiscal_years[-1]}")
    print(f"Markets: KOSPI + KOSDAQ")
    print(f"All responses cached for safe resumption\n")
    
    # Use the previous weekday to avoid an incomplete same-day snapshot.
    snapshot_date = date.today() - timedelta(days=1)
    while snapshot_date.weekday() >= 5:
        snapshot_date -= timedelta(days=1)
    output_dir = Path("outputs/opendart_full")
    listed_codes = listing_snapshot(snapshot_date, output_dir)
    result = download_annual_financials(
        client, fiscal_years, output_dir, current_listing_codes=listed_codes
    )
    
    if result.get("status") == "stopped":
        print("\n" + "=" * 70)
        print("STOPPED DUE TO DAILY LIMIT")
        print("=" * 70)
        print(f"Reason: {result.get('reason')}")
        print(f"Request count: {result['request_count']}")
        if result.get("completed_years"):
            print(f"Completed years: FY{result['completed_years'][0]}-FY{result['completed_years'][-1]}")
        if result.get("partial_year"):
            print(f"Partial year: FY{result['partial_year']}")
        if result.get("partial_file"):
            print(f"Partial results saved: {result['partial_file']}")
        print(f"\nRun this script again tomorrow to resume from cache.")
    
    elif result.get("status") == "complete":
        print("\n" + "=" * 70)
        print("SUCCESS - All data downloaded")
        print("=" * 70)
        print(f"Total records: {result['total_records']}")
        print(f"Output file: {result['output_file']}")
