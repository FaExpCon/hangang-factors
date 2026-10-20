"""Download current OpenDART KSIC overviews for corporate identities in annual output."""

import argparse
from pathlib import Path
from dotenv import dotenv_values

from kr_quant.company_overview import (DEFAULT_CACHE, CompanyOverviewDownloader,
                                        download_overviews, output_corporations, overview_plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--disclosures", type=Path, default=Path("cache/opendart/list"))
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--daily-limit", type=int, default=3000)
    parser.add_argument("--min-delay", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    scope = output_corporations(args.financials, args.disclosures)
    plan = overview_plan(scope, args.cache_dir)
    print(f"Output-scoped corporate codes: {plan['total']}; cached: {plan['cached']}; "
          f"maximum new calls: {plan['maximum_new_calls']}", flush=True)
    if args.dry_run:
        print("Zero API requests; no credentials loaded")
        print("COMPANY_OVERVIEW_PLAN_PASSED")
        return
    downloader = CompanyOverviewDownloader(dotenv_values(".env").get("DART_API_KEY"),
                                           cache_dir=args.cache_dir, daily_limit=args.daily_limit,
                                           min_delay=args.min_delay)
    try:
        result = download_overviews(scope, downloader, args.cache_dir / "progress.json",
                                    display=lambda message: print(message, flush=True))
    except (RuntimeError, ValueError) as error:
        print(str(error))
        raise SystemExit(1) from None
    print(f"Complete: {result['completed']} companies; {result['unknown_industry']} unknown industries; "
          f"{downloader.request_count} new attempts; {downloader.cache_hits} cache hits")
    print(f"Unavailable overviews: {len(result['unavailable'])}; "
          f"available but missing industry: {len(result['missing_industry'])}; "
          f"details: {args.cache_dir / 'progress.json'}")


if __name__ == "__main__":
    main()
