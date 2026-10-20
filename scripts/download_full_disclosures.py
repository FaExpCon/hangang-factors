"""Download full KOSPI annual disclosures for FY2014-2024.

This script downloads:
- Annual report filing dates for all KOSPI companies
- Financial statements (revenue, net income, assets, equity, liabilities)
- Uses original filing date with final corrected values
- Tracks and reports correction statistics

Estimated: ~384 API requests
- 1 corp codes
- ~339 disclosure pages (39 three-month windows × 8.69 pages/window)
- 44 financial account calls (11 years × 4 batches of ~25 companies)

Run time: ~5-10 minutes with 0.5s throttle
"""

import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kr_quant.opendart import OpenDartClient


def download_full_disclosures(
    client: OpenDartClient,
    fiscal_years: list[int],
    output_dir: Path
) -> dict:
    """Download full KOSPI annual disclosures and financial data.
    
    Args:
        client: OpenDART client
        fiscal_years: List of fiscal years (e.g., list(range(2014, 2025)))
        output_dir: Output directory
        
    Returns:
        Summary dict with counts and statistics
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print("="*60)
    print("KOSPI Annual Disclosure Download")
    print("="*60)
    print(f"Fiscal years: {min(fiscal_years)}-{max(fiscal_years)}")
    print(f"Output: {output_dir}")
    print()
    
    # Step 1: Get company codes
    print("Step 1: Downloading company codes...")
    corp_map = client.get_corp_codes()
    print(f"  Found {len(corp_map)} listed companies")
    print(f"  Requests: {client.request_count}")
    print()
    
    # Step 2: Get all annual report disclosures
    print("Step 2: Getting annual report disclosures...")
    disclosures_by_fy, correction_stats = client.get_annual_report_disclosures(
        fiscal_years, 
        corp_cls="Y"
    )
    
    total_disclosures = sum(len(discs) for discs in disclosures_by_fy.values())
    print(f"  Found {total_disclosures} total annual reports")
    print(f"  Corrections: {correction_stats['corrections']} of "
          f"{correction_stats['total_company_years']} company-years "
          f"({correction_stats['correction_rate']:.1%})")
    print(f"  Requests: {client.request_count}")
    print()
    
    # Save disclosure dates by fiscal year
    disclosures_file = output_dir / "annual_disclosures_FY2014-2024.json"
    disclosure_summary = {
        "fiscal_years": fiscal_years,
        "correction_stats": correction_stats,
        "by_year": {}
    }
    
    for fy, disclosures in disclosures_by_fy.items():
        disclosure_summary["by_year"][fy] = {
            "count": len(disclosures),
            "disclosures": [
                {
                    "corp_code": d.get("corp_code", ""),
                    "stock_code": d.get("stock_code", ""),
                    "corp_name": corp_map.get(d.get("stock_code", ""), {}).get("corp_name", ""),
                    "report_name": d.get("report_nm", ""),
                    "filing_date_approx": d.get("filing_date_approx", ""),
                    "receipt_no": d.get("rcept_no", ""),
                    "had_correction": d.get("had_correction", False)
                }
                for d in sorted(disclosures, key=lambda x: x.get("stock_code", ""))
            ]
        }
    
    with open(disclosures_file, "w", encoding="utf-8") as f:
        json.dump(disclosure_summary, f, ensure_ascii=False, indent=2)
    
    print(f"Saved disclosure dates to {disclosures_file}")
    print()
    
    # Step 3: Get financial accounts for each year
    print("Step 3: Getting financial accounts (this may take several minutes)...")
    
    # Get all KOSPI corp_codes
    kospi_corp_codes = [
        corp_map[stock_code]["corp_code"]
        for stock_code in sorted(corp_map.keys())
    ]
    
    print(f"  Total KOSPI companies: {len(kospi_corp_codes)}")
    
    # Download in batches of 100 companies per request
    batch_size = 100
    accounts_by_year = {}
    
    for year in fiscal_years:
        print(f"\n  Processing FY{year}...")
        year_accounts = []
        
        for i in range(0, len(kospi_corp_codes), batch_size):
            batch = kospi_corp_codes[i:i+batch_size]
            accounts = client.get_multi_company_accounts(batch, year)
            year_accounts.extend(accounts)
            print(f"    Batch {i//batch_size + 1}/{(len(kospi_corp_codes) + batch_size - 1)//batch_size}: "
                  f"retrieved {len(accounts)} records")
        
        accounts_by_year[year] = year_accounts
        print(f"  FY{year}: {len(year_accounts)} total account records")
    
    print(f"\n  Total requests: {client.request_count}")
    print()
    
    # Save financial accounts
    accounts_file = output_dir / "annual_accounts_FY2014-2024.json"
    with open(accounts_file, "w", encoding="utf-8") as f:
        json.dump(accounts_by_year, f, ensure_ascii=False, indent=2)
    
    print(f"Saved financial accounts to {accounts_file}")
    print()
    
    return {
        "total_requests": client.request_count,
        "companies": len(kospi_corp_codes),
        "fiscal_years": fiscal_years,
        "total_disclosures": total_disclosures,
        "total_accounts": sum(len(accts) for accts in accounts_by_year.values()),
        "correction_stats": correction_stats,
        "output_files": {
            "disclosures": str(disclosures_file),
            "accounts": str(accounts_file)
        }
    }


if __name__ == "__main__":
    fiscal_years = list(range(2014, 2025))  # FY2014 through FY2024
    output_dir = Path("outputs/opendart_full")
    
    client = OpenDartClient()
    
    print("This will download ~11 years of KOSPI annual data")
    print(f"Estimated: ~384 API requests")
    print(f"Estimated time: ~5-10 minutes")
    print()
    
    summary = download_full_disclosures(client, fiscal_years, output_dir)
    
    print("="*60)
    print("DOWNLOAD COMPLETE")
    print("="*60)
    print(f"Total API requests: {summary['total_requests']}")
    print(f"Companies: {summary['companies']}")
    print(f"Fiscal years: {min(fiscal_years)}-{max(fiscal_years)}")
    print(f"Total disclosures: {summary['total_disclosures']}")
    print(f"Total account records: {summary['total_accounts']}")
    print(f"Correction rate: {summary['correction_stats']['correction_rate']:.1%}")
    print()
    print("Output files:")
    for name, path in summary['output_files'].items():
        print(f"  {name}: {path}")
