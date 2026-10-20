"""OpenDART API downloader for financial statements and disclosure dates.

This module downloads:
- Company codes for all listed companies
- Annual financial statements (filing dates + key accounts)
- Caches all responses to avoid repeated calls

Key design principles:
- Multi-company endpoint (fnlttMultiAcnt) for financial data: up to 100 companies per call
- Disclosure search without corp_code to get filing dates for many companies at once
- Stay well below 10,000 daily call limit
- Cache every response
- Never log or print API keys
"""

import json
import math
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


class OpenDartClient:
    """OpenDART API client with caching and rate limiting."""

    BASE_URL = "https://opendart.fss.or.kr/api"
    
    def __init__(self, cache_dir: Optional[Path] = None, api_key: Optional[str] = None):
        """Initialize client with API key from .env and cache directory.
        
        Args:
            cache_dir: Cache directory (defaults to cache/opendart)
            api_key: API key (defaults to DART_API_KEY from .env)
        """
        if api_key is None:
            load_dotenv()
            api_key = os.getenv("DART_API_KEY")
            if not api_key:
                raise ValueError("DART_API_KEY not found in environment")
        
        self.api_key = api_key
        self.cache_dir = cache_dir or Path("cache/opendart")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Rate limiting: conservative to stay well below daily limits
        self.min_delay = 0.5  # seconds between requests
        self.last_request_time: Optional[float] = None
        self.request_count = 0
        self.no_data_account_batches: list[dict[str, Any]] = []
    
    def _wait_for_rate_limit(self):
        """Enforce minimum delay between requests."""
        if self.last_request_time is not None:
            elapsed = time.time() - self.last_request_time
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed)
    
    def _cache_path(self, endpoint: str, params: dict[str, Any]) -> Path:
        """Generate cache file path for endpoint and parameters."""
        # Exclude api_key from cache key
        cache_key = {k: v for k, v in sorted(params.items()) if k != "crtfc_key"}
        key_str = json.dumps(cache_key, sort_keys=True, ensure_ascii=False)
        
        from hashlib import sha256
        digest = sha256(key_str.encode()).hexdigest()[:16]
        
        return self.cache_dir / endpoint / f"{digest}.json"
    
    def _request(self, endpoint: str, params: dict[str, Any], use_cache: bool = True) -> dict[str, Any]:
        """Make API request with caching.
        
        Args:
            endpoint: API endpoint name (e.g., "corpCode", "list", "fnlttMultiAcnt")
            params: Request parameters (crtfc_key will be added automatically)
            use_cache: Whether to use cached responses
            
        Returns:
            JSON response as dict
            
        Raises:
            ValueError: If API returns an error status
        """
        params["crtfc_key"] = self.api_key
        
        cache_path = self._cache_path(endpoint, params)
        
        # Return cached response if available
        if use_cache and cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        
        # Make API request
        self._wait_for_rate_limit()
        
        url = f"{self.BASE_URL}/{endpoint}.json"
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        self.last_request_time = time.time()
        self.request_count += 1
        
        data = response.json()
        
        # Check API status
        status = data.get("status")
        if status == "013" and endpoint == "fnlttMultiAcnt":
            # OpenDART: no data for this batch. Cache it so resume does not retry.
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return data
        if status != "000":
            message = data.get("message", "Unknown error")
            raise ValueError(f"OpenDART API error {status}: {message}")
        
        # Cache response
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return data
    
    def get_corp_codes(self) -> dict[str, dict[str, str]]:
        """Download company codes for all listed companies.
        
        Returns:
            Dict mapping stock_code to {corp_code, corp_name, stock_code}
            Only includes companies with stock codes (listed companies)
        """
        # Download the ZIP file containing corp codes
        self._wait_for_rate_limit()
        
        url = f"{self.BASE_URL}/corpCode.xml"
        params = {"crtfc_key": self.api_key}
        
        cache_path = self.cache_dir / "corpCode.json"
        
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        self.last_request_time = time.time()
        self.request_count += 1
        
        # Extract ZIP and parse XML
        import zipfile
        import io
        import xml.etree.ElementTree as ET
        
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            xml_data = z.read("CORPCODE.xml")
        
        root = ET.fromstring(xml_data)
        
        # Build mapping: stock_code -> {corp_code, corp_name, stock_code}
        corp_map = {}
        for corp in root.findall("list"):
            stock_code = corp.findtext("stock_code", "").strip()
            if not stock_code:
                continue  # Skip unlisted companies
            
            corp_code = corp.findtext("corp_code", "").strip()
            corp_name = corp.findtext("corp_name", "").strip()
            
            corp_map[stock_code] = {
                "corp_code": corp_code,
                "corp_name": corp_name,
                "stock_code": stock_code
            }
        
        # Cache the mapping
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(corp_map, f, ensure_ascii=False, indent=2)
        
        return corp_map
    
    def get_annual_report_disclosures(
        self,
        fiscal_years: list[int],
        corp_cls: str = "Y"
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
        """Get annual report disclosures (사업보고서) for given fiscal years.
        
        Without corp_code, the API limits searches to 3 months. This method:
        - Splits the filing date range into 3-month windows
        - Parses fiscal year from report_nm (e.g., "사업보고서 (2023.12)")
        - Groups by (corp_code, fiscal_year), keeping original filing date with final corrected values
        - Counts corrections
        
        Args:
            fiscal_years: List of fiscal years to collect (e.g., [2022, 2023])
            corp_cls: Corporation class - Y (KOSPI), K (KOSDAQ), N (KONEX), E (other)
            
        Returns:
            Tuple of (disclosure_dict, stats_dict) where:
            - disclosure_dict: fiscal_year -> list of disclosure records with:
                corp_code, stock_code, rcept_no, rcept_dt, report_nm, fiscal_year,
                filing_date_approx (original date), had_correction (bool)
            - stats_dict: correction statistics
        """
        # For FY2015 resume, anchor at 2015 to reuse the existing FY2014-2024
        # cached windows and include non-December fiscal-year filings.
        min_year = min(fiscal_years)
        max_year = max(fiscal_years)
        
        search_start = date(2015 if min_year == 2015 else min_year + 1, 1, 1)
        search_end = date(max_year + 1, 12, 31)
        
        all_disclosures = []
        
        # Split into 3-month windows (API requirement without corp_code)
        current = search_start
        while current <= search_end:
            window_end = min(current + timedelta(days=89), search_end)  # ~3 months
            
            # Fetch all pages in this window
            page_no = 1
            while True:
                params = {
                    "bgn_de": current.strftime("%Y%m%d"),
                    "end_de": window_end.strftime("%Y%m%d"),
                    "pblntf_detail_ty": "A001",  # Annual report (사업보고서)
                    "corp_cls": corp_cls,
                    "page_no": str(page_no),
                    "page_count": "100",  # Max per page
                    # Get both original and corrections separately
                    "last_reprt_at": "N"  # Return all versions to track corrections
                }
                
                data = self._request("list", params)
                
                items = data.get("list", [])
                if not items:
                    break
                
                all_disclosures.extend(items)
                
                # Check if there are more pages
                total_page = int(data.get("total_page", 0))
                if page_no >= total_page:
                    break
                
                page_no += 1
            
            # Move to next window
            current = window_end + timedelta(days=1)
        
        # Parse fiscal year and detect corrections
        # Pattern for FY: "사업보고서 (2023.12)" -> FY 2023
        # Pattern for corrections: "정정" in report_nm
        fy_pattern = re.compile(r'\((\d{4})\.\d{2}\)')
        correction_pattern = re.compile(r'정정')
        
        # Group by (corp_code, fiscal_year) to find originals and corrections
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        
        skipped_names: dict[str, int] = {}
        skipped_missing_corp = 0
        outside_requested_years = 0
        non_december_filings = 0
        for disclosure in all_disclosures:
            report_nm = disclosure.get("report_nm", "")
            match = fy_pattern.search(report_nm)
            if not match:
                skipped_names[report_nm] = skipped_names.get(report_nm, 0) + 1
                continue
            
            fiscal_year = int(match.group(1))
            if fiscal_year not in fiscal_years:
                outside_requested_years += 1
                continue
            
            corp_code = disclosure.get("corp_code", "")
            if not corp_code:
                skipped_missing_corp += 1
                continue
            if not report_nm[match.start():match.end()].endswith(".12)"):
                non_december_filings += 1
            key = (corp_code, fiscal_year)
            
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(disclosure)
        
        # For each group, use original filing date with final corrected values
        correction_count = 0
        company_year_count = len(grouped)
        
        results_by_fy: dict[int, list[dict[str, Any]]] = {fy: [] for fy in fiscal_years}
        
        for (corp_code, fiscal_year), filings in grouped.items():
            # Sort by filing date
            filings_sorted = sorted(filings, key=lambda x: x.get("rcept_dt", ""))
            
            # Original filing (earliest)
            original = filings_sorted[0]
            original_date = original.get("rcept_dt", "")
            
            # Check if there were corrections
            has_correction = len(filings_sorted) > 1 or bool(correction_pattern.search(original.get("report_nm", "")))
            if has_correction:
                correction_count += 1
            
            # Use the LAST filing (most corrected) but annotate with original date
            final_filing = filings_sorted[-1]
            
            # Create a clean record with only the fields we need
            clean_record = {
                "corp_code": final_filing.get("corp_code", ""),
                "stock_code": final_filing.get("stock_code", ""),
                "corp_name": final_filing.get("corp_name", ""),
                "rcept_no": final_filing.get("rcept_no", ""),
                "rcept_dt": final_filing.get("rcept_dt", ""),
                "report_nm": final_filing.get("report_nm", ""),
                "fiscal_year": fiscal_year,
                "filing_date_approx": original_date,
                "had_correction": has_correction
            }
            
            results_by_fy[fiscal_year].append(clean_record)
        
        stats = {
            "total_company_years": company_year_count,
            "corrections": correction_count,
            "correction_rate": correction_count / company_year_count if company_year_count > 0 else 0.0,
            "raw_filings": len(all_disclosures),
            "skipped_unparseable": sum(skipped_names.values()),
            "skipped_unparseable_names": sorted(skipped_names.items(), key=lambda pair: (-pair[1], pair[0]))[:10],
            "skipped_missing_corp_code": skipped_missing_corp,
            "outside_requested_years": outside_requested_years,
            "non_december_filings": non_december_filings,
        }
        
        return results_by_fy, stats
    
    def get_multi_company_accounts(
        self,
        corp_codes: list[str],
        bsns_year: int,
        reprt_code: str = "11011"
    ) -> list[dict[str, Any]]:
        """Get major financial accounts for multiple companies (up to 100).
        
        Args:
            corp_codes: List of corp_codes (max 100)
            bsns_year: Business year (e.g., 2023)
            reprt_code: Report code - 11011 (annual), 11012 (semi), 11013 (Q1), 11014 (Q3)
            
        Returns:
            List of account records with stock_code, account_nm, fs_div, sj_div, amounts
        """
        if len(corp_codes) > 100:
            raise ValueError("Maximum 100 companies per request")
        
        params = {
            "corp_code": ",".join(corp_codes),
            "bsns_year": str(bsns_year),
            "reprt_code": reprt_code
        }
        
        data = self._request("fnlttMultiAcnt", params)
        if data.get("status") == "013":
            self.no_data_account_batches.append({
                "fiscal_year": bsns_year,
                "corp_codes": list(corp_codes),
            })
            return []
        return data.get("list", [])


def download_annual_financials(
    client: OpenDartClient,
    fiscal_years: list[int],
    output_dir: Optional[Path] = None,
    daily_limit: int = 9000,  # Conservative limit (official is 10,000)
    current_listing_codes: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Download annual financial data for ALL listed companies (KOSPI + KOSDAQ).
    
    Args:
        client: OpenDART client
        fiscal_years: List of fiscal years (e.g., [2014, 2015, ..., 2024])
        output_dir: Output directory (defaults to outputs/opendart_full)
        daily_limit: Maximum API requests per day (default 9000, official limit 10000)
        
    Returns:
        Summary dict with request counts and data file location
    """
    if min(fiscal_years) < 2015:
        raise ValueError("fnlttMultiAcnt provides financial data from FY2015 onward")
    if output_dir is None:
        output_dir = Path("outputs/opendart_full")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Estimate requests before starting
    num_years = len(fiscal_years)
    min_year = min(fiscal_years)
    max_year = max(fiscal_years)
    
    # Filing dates span from (min_year+1) Jan 1 to (max_year+1) Dec 31
    search_start = date(2015 if min_year == 2015 else min_year + 1, 1, 1)
    search_end = date(max_year + 1, 12, 31)
    disclosure_windows = math.ceil(((search_end - search_start).days + 1) / 90)
    
    # Estimate disclosure pages (conservative: 3 pages/window for both markets)
    disclosure_requests_per_market = disclosure_windows * 3
    disclosure_requests = disclosure_requests_per_market * 2  # KOSPI + KOSDAQ
    
    # Estimate account requests
    # Provisional: disclosure-derived companies including delisted filers.
    account_batches_per_year = 27
    accounts_requests = num_years * account_batches_per_year
    
    total_estimated = 1 + disclosure_requests + accounts_requests  # +1 for corp codes
    
    print("=" * 70)
    print("FULL MARKET DOWNLOAD: FY{}-FY{} (KOSPI + KOSDAQ)".format(min_year, max_year))
    print("=" * 70)
    print(f"\nEstimated requests: {total_estimated}")
    print(f"  - Corp codes: 1")
    print(f"  - Disclosure search: {disclosure_windows} windows × 2 markets × ~3 pages = {disclosure_requests}")
    print(f"  - Financial accounts: {num_years} years × ~{account_batches_per_year} batches = {accounts_requests}")
    print(f"\nDaily limit: {daily_limit} (official limit: 10,000)")
    
    if total_estimated > daily_limit:
        print(f"\n⚠️  WARNING: Estimated requests ({total_estimated}) exceeds daily limit ({daily_limit})")
        print("    The script will stop safely and can resume tomorrow from cache.")
    
    print(f"\nAll responses are cached. Safe to interrupt and resume.")
    print()
    
    # Download company codes
    print("Downloading company codes...")
    corp_map = client.get_corp_codes()
    print(f"Found {len(corp_map)} listed companies")
    
    if client.request_count >= daily_limit:
        print(f"\n⚠️  Reached daily limit ({daily_limit}). Stopping safely.")
        return {"status": "stopped", "reason": "daily_limit", "request_count": client.request_count}
    
    print(f"Request count: {client.request_count}")
    
    # Process both KOSPI and KOSDAQ
    all_disclosures = {}
    all_correction_stats = {} 
    
    for market_name, corp_cls in [("KOSPI", "Y"), ("KOSDAQ", "K")]:
        print(f"\n{'='*70}")
        print(f"Getting {market_name} annual report disclosures for FY{fiscal_years}...")
        print(f"{'='*70}")
        
        disclosures_by_fy, correction_stats = client.get_annual_report_disclosures(fiscal_years, corp_cls=corp_cls)
        
        total_disclosures = sum(len(discs) for discs in disclosures_by_fy.values())
        print(f"Found {total_disclosures} total {market_name} annual reports across all years")
        print(f"Corrections detected: {correction_stats['corrections']} of {correction_stats['total_company_years']} "
              f"company-years ({correction_stats['correction_rate']:.1%})")
        print(f"  Raw filings: {correction_stats['raw_filings']}; unparseable names: "
              f"{correction_stats['skipped_unparseable']}; missing corp code: "
              f"{correction_stats['skipped_missing_corp_code']}; outside requested FY: "
              f"{correction_stats['outside_requested_years']}; non-December filings: "
              f"{correction_stats['non_december_filings']}")
        print(f"  Unparseable name samples: {correction_stats['skipped_unparseable_names']}")
        print(f"Request count: {client.request_count}")
        
        if client.request_count >= daily_limit:
            print(f"\n⚠️  Reached daily limit ({daily_limit}). Stopping safely.")
            print(f"Progress saved in cache. Resume tomorrow to continue from {market_name}.")
            return {"status": "stopped", "reason": "daily_limit", "request_count": client.request_count, 
                    "last_market": market_name}
        
        all_disclosures[market_name] = disclosures_by_fy
        all_correction_stats[market_name] = correction_stats
    
    # Download financial data for all companies
    all_data = []
    
    for year in fiscal_years:
        print(f"\n{'='*70}")
        print(f"Processing FY{year}")
        print(f"{'='*70}")
        
        # Build from returned disclosures, not corpCode.xml. The Y/K search
        # may itself omit historically listed, now-delisted companies.
        year_companies = {}
        
        for market_name in ["KOSPI", "KOSDAQ"]:
            disclosures = all_disclosures[market_name].get(year, [])
            print(f"{market_name}: {len(disclosures)} companies filed reports")
            
            for disclosure in disclosures:
                stock_code = disclosure.get("stock_code", "")
                corp_code = disclosure.get("corp_code", "")
                corp_name = disclosure.get("corp_name", "")
                
                if not stock_code or not corp_code:
                    continue
                
                # Keep every company returned by this disclosure search.
                if stock_code not in year_companies:
                    # Get name from disclosure first, fall back to current corp_map
                    if not corp_name and stock_code in corp_map:
                        corp_name = corp_map[stock_code]["corp_name"]
                    
                    year_companies[stock_code] = {
                        "corp_code": corp_code,
                        "corp_name": corp_name or f"Unknown-{stock_code}",
                        "market": market_name,
                        "disclosure": disclosure,
                        # corpCode.xml retains historical codes; it is not an
                        # exchange listing-status snapshot.
                        "currently_listed": (stock_code in current_listing_codes if current_listing_codes is not None else None),
                        "in_corp_code_map": stock_code in corp_map,
                    }
        
        # Report survivorship statistics
        total_companies = len(year_companies)
        in_corp_map = sum(1 for c in year_companies.values() if c["in_corp_code_map"])
        
        print(f"Total companies this year: {total_companies}")
        print(f"  In corpCode.xml: {in_corp_map}; absent: {total_companies - in_corp_map}")
        if current_listing_codes is None:
            print("  Currently listed / no longer listed: unknown (requires dated KRX listing data)")
        else:
            listed = sum(c["currently_listed"] for c in year_companies.values())
            print(f"  Stock-code matches in KRX snapshot: {listed}; absent: {total_companies - listed}")
        
        # Get financial accounts in batches of 100
        all_accounts = []
        corp_codes = [info["corp_code"] for info in year_companies.values()]
        
        for batch_start in range(0, len(corp_codes), 100):
            batch_corp_codes = corp_codes[batch_start:batch_start + 100]
            batch_num = batch_start // 100 + 1
            total_batches = (len(corp_codes) + 99) // 100
            
            print(f"  Getting financial accounts batch {batch_num}/{total_batches} ({len(batch_corp_codes)} companies)...")
            
            accounts = client.get_multi_company_accounts(batch_corp_codes, year)
            all_accounts.extend(accounts)
            
            print(f"    Retrieved {len(accounts)} account records (cumulative: {len(all_accounts)})")
            print(f"    Request count: {client.request_count}")
            
            if client.request_count >= daily_limit:
                print(f"\n⚠️  Reached daily limit ({daily_limit}). Stopping safely.")
                print(f"Progress: Completed through FY{year-1} fully.")
                print(f"          Partial data for FY{year} (batch {batch_num}/{total_batches}).")
                print(f"\nResume tomorrow - cached disclosures will be reused.")
                
                # Save partial results
                if all_data:
                    partial_file = output_dir / f"annual_financials_FY{min_year}_{max_year}_PARTIAL.json"
                    with open(partial_file, "w", encoding="utf-8") as f:
                        json.dump(all_data, f, ensure_ascii=False, indent=2)
                    print(f"Partial results saved to: {partial_file}")
                
                return {
                    "status": "stopped",
                    "reason": "daily_limit",
                    "request_count": client.request_count,
                    "completed_years": fiscal_years[:fiscal_years.index(year)],
                    "partial_year": year,
                    "partial_file": str(partial_file) if all_data else None
                }
        
        # Keep every filer even when its batch has no financial accounts.
        company_data = {
            stock_code: {
                "stock_code": stock_code,
                "corp_name": info["corp_name"],
                "market": info["market"],
                "year": year,
                "filing_date": None,
                "currently_listed": info["currently_listed"],
                "in_corp_code_map": info["in_corp_code_map"],
                "filings": [],
                "accounts": {},
            }
            for stock_code, info in year_companies.items()
        }
        for account in all_accounts:
            stock_code = account.get("stock_code", "")
            if not stock_code or stock_code not in year_companies:
                continue
            
            # Extract account details
            account_nm = account.get("account_nm", "")
            fs_div = account.get("fs_div", "")
            sj_div = account.get("sj_div", "")
            thstrm_amount = account.get("thstrm_amount", "")
            
            key = f"{account_nm}_{fs_div}_{sj_div}"
            company_data[stock_code]["accounts"][key] = {
                "account_nm": account_nm,
                "fs_div": fs_div,
                "fs_nm": account.get("fs_nm", ""),
                "sj_div": sj_div,
                "sj_nm": account.get("sj_nm", ""),
                "amount": thstrm_amount
            }
        
        # Add filing information using original filing date with final corrected values
        for stock_code, info in year_companies.items():
            disclosure = info["disclosure"]
            filing_date_approx = disclosure.get("filing_date_approx", "")
            had_correction = disclosure.get("had_correction", False)
            
            company_data[stock_code]["filings"].append({
                "report_name": disclosure.get("report_nm", ""),
                "filing_date": filing_date_approx,
                "receipt_no": disclosure.get("rcept_no", ""),
                "had_correction": had_correction,
            })
        
        # Set main filing date (use latest if multiple)
        for item in company_data.values():
            item["filings"].sort(key=lambda x: x["filing_date"])
            item["filing_date"] = item["filings"][-1]["filing_date"] if item["filings"] else None
        
        all_data.extend(company_data.values())
        
        print(f"Completed FY{year}: {len(company_data)} companies added (total: {len(all_data)} records)")
    
    # corpCode.xml includes delisted companies and cannot establish current listing.
    total_unique_companies = len(set(record["stock_code"] for record in all_data))
    
    # Save final results
    output_file = output_dir / f"annual_financials_FY{min_year}_{max_year}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    audit_file = output_dir / f"annual_financials_FY{min_year}_{max_year}_audit.json"
    with open(audit_file, "w", encoding="utf-8") as f:
        json.dump({"correction_stats": all_correction_stats,
                   "no_data_account_batches": client.no_data_account_batches,
                   "listing_status_as_of": "provided snapshot" if current_listing_codes is not None else None},
                  f, ensure_ascii=False, indent=2)
    
    print("\n" + "=" * 70)
    print("DOWNLOAD COMPLETE")
    print("=" * 70)
    print(f"Output file: {output_file}")
    print(f"Audit file: {audit_file}")
    print(f"Total API requests: {client.request_count}")
    print(f"Total records: {len(all_data)}")
    print(f"\nSurvivorship Bias Analysis:")
    print(f"  Unique companies across all years: {total_unique_companies}")
    if current_listing_codes is None:
        print("  Currently listed / no longer listed: unknown without an as-of exchange snapshot")
    else:
        listed = len({item["stock_code"] for item in all_data if item["currently_listed"]})
        print(f"  Stock-code matches in KRX snapshot: {listed}; absent: {total_unique_companies - listed}")
        print("  This compares only companies returned by the Y/K disclosure search;")
        print("  it cannot count delisted companies missing from that search.")
    print(f"  Empty financial-account batches (013): {len(client.no_data_account_batches)}")
    
    # Print correction statistics
    print(f"\nCorrection Statistics:")
    for market_name in ["KOSPI", "KOSDAQ"]:
        stats = all_correction_stats[market_name]
        print(f"  {market_name}:")
        print(f"    Total company-years: {stats['total_company_years']}")
        print(f"    Had corrections: {stats['corrections']}")
        print(f"    Rate: {stats['correction_rate']:.1%}")
    
    print(f"\nNote: Filing dates are approximate for corrected reports.")
    print(f"      Using original filing date with final corrected values.")
    
    return {
        "status": "complete",
        "request_count": client.request_count,
        "fiscal_years": fiscal_years,
        "total_records": len(all_data),
        "no_data_account_batches": client.no_data_account_batches,
        "output_file": str(output_file),
        "audit_file": str(audit_file),
        "correction_stats": all_correction_stats
    }



if __name__ == "__main__":
    # Test with FY2022-2023 annual reports
    client = OpenDartClient()
    summary = download_annual_financials(client, [2022, 2023])
    print("\nSummary:")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
