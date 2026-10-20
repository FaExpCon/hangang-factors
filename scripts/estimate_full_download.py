"""Estimate API requests for full FY2014-2024 download with delisted companies."""

print("=" * 70)
print("API REQUEST ESTIMATE: FY2014-2024 Full Market Download")
print("=" * 70)
print("\nKEY CHANGE: Using disclosure-based company list (includes delisted)")
print("instead of current listing (avoids survivorship bias)")

# Request 1: Corp codes
print("\n1. Corporation codes: 1 request")

# Request 2: Disclosure search
print("\n2. Disclosure search (builds company list per year):")
print("   - Date range: 2015-01-01 to 2025-12-31")
print("   - Window size: 3 months")
print("   - Total windows: 44 (11 years × 12 months / 3)")
print("   - Markets: KOSPI + KOSDAQ = 2")

# Use observed pagination from test runs
avg_pages_kospi = 3  # Conservative based on test data
avg_pages_kosdaq = 4  # KOSDAQ has more companies

disclosure_requests_kospi = 44 * avg_pages_kospi
disclosure_requests_kosdaq = 44 * avg_pages_kosdaq
disclosure_requests_total = disclosure_requests_kospi + disclosure_requests_kosdaq

print(f"\n   Estimate (based on test runs):")
print(f"   - KOSPI: 44 windows × {avg_pages_kospi} pages = {disclosure_requests_kospi}")
print(f"   - KOSDAQ: 44 windows × {avg_pages_kosdaq} pages = {disclosure_requests_kosdaq}")
print(f"   - Total disclosure requests: {disclosure_requests_total}")

# Request 3: Financial accounts
print("\n3. Financial accounts (multi-company calls):")
print("   - Companies per API call: 100 (max)")
print("   - Company estimation:")

# The key change: we now get companies from DISCLOSURES not current listing
# Historical data suggests:
# - KOSPI had ~800-900 listed companies during this period
# - KOSDAQ had ~1,300-1,600 listed companies
# - Delisted companies add ~10-15% more

kospi_estimate = 950  # ~850 + delisted
kosdaq_estimate = 1700  # ~1500 + delisted
total_companies = kospi_estimate + kosdaq_estimate

print(f"     KOSPI (2014-2024 avg, incl. delisted): ~{kospi_estimate}")
print(f"     KOSDAQ (2014-2024 avg, incl. delisted): ~{kosdaq_estimate}")
print(f"     Total: ~{total_companies} companies per year")
print(f"     (includes delisted companies to avoid survivorship bias)")

batches_per_year = (total_companies + 99) // 100  # Round up
fiscal_years = 11
accounts_requests = batches_per_year * fiscal_years

print(f"\n   - Batches per year: {batches_per_year}")
print(f"   - Fiscal years: {fiscal_years}")
print(f"   - Total account requests: {batches_per_year} × {fiscal_years} = {accounts_requests}")

# Total
total_requests = 1 + disclosure_requests_total + accounts_requests
daily_limit = 10000
conservative_limit = 9000

print("\n" + "=" * 70)
print("TOTAL ESTIMATE")
print("=" * 70)
print(f"Corp codes:           1")
print(f"Disclosure search:    {disclosure_requests_total}")
print(f"Financial accounts:   {accounts_requests}")
print(f"-" * 70)
print(f"Total requests:       {total_requests}")
print(f"\nDaily limit (official):     {daily_limit:,}")
print(f"Daily limit (conservative): {conservative_limit:,}")
print(f"Usage:                      {100*total_requests/conservative_limit:.1f}% of conservative limit")

if total_requests < conservative_limit:
    print(f"\n[OK] Safe to run in single session")
    print(f"     Estimated time: 10-20 minutes")
else:
    print(f"\n[WARNING] May need to resume next day")

print(f"\nSurvivorship bias correction:")
print(f"  Without delisted companies: ~2,300 companies")
print(f"  With delisted companies:    ~2,650 companies (+15%)")
print(f"  Delisted companies saved:   ~350 per year across 11 years")
