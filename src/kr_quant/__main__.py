"""Command-line interface for bulk download."""
import argparse
from datetime import date

from kr_quant.bulk import BulkDownloader, BulkRequest


def main():
    parser = argparse.ArgumentParser(description="Download historical KRX data")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--max-tickers", type=int, default=None, 
                        help="Limit number of tickers (for smoke testing only)")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Delay between API calls in seconds (default: 0.5)")
    
    args = parser.parse_args()
    
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    
    request = BulkRequest(start=start, end=end, max_tickers=args.max_tickers)
    downloader = BulkDownloader(request, delay=args.delay)
    
    print(f"Starting bulk download: {start} to {end}")
    if args.max_tickers:
        print(f"SMOKE TEST MODE: limited to {args.max_tickers} tickers")
    
    summary = downloader.run()
    
    print("\n" + "="*60)
    print("DOWNLOAD COMPLETE")
    print("="*60)
    print(f"Status: {summary['status']}")
    print(f"Trading days: {summary['trading_days_in_range']}")
    print(f"Unique tickers: {summary['historical_distinct_tickers']}")
    print(f"Network requests: {summary['network_requests']}")
    print(f"Cache hits: {summary['cache_hits']}")
    print(f"Elapsed: {summary['elapsed_seconds']:.1f}s")
    
    if summary.get('empty_price_years'):
        print(f"Empty price responses: {len(summary['empty_price_years'])}")


if __name__ == "__main__":
    main()
