"""Fill reviewed OpenDART gaps; never requests KRX data. Run separately."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kr_quant.dart_gap import AUDIT, OUTPUT, GapClient, download_gap, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-file", type=Path, default=AUDIT)
    parser.add_argument("--output-file", type=Path, default=OUTPUT)
    parser.add_argument("--work-dir", type=Path, default=Path("outputs/opendart_gap/download"))
    parser.add_argument("--daily-limit", type=int, default=1000)
    parser.add_argument("--cache-only", action="store_true", help="Production re-merge using real cache files only; no network or synthetic no-data")
    parser.add_argument("--merge-at-end", action="store_true", help="Stage cached records in memory and perform one atomic output merge")
    args = parser.parse_args()
    plan = read_json(args.audit_file)
    client = GapClient(daily_limit=args.daily_limit, cache_only=args.cache_only)
    cutoff = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    try:
        result = download_gap(client, plan, args.output_file, args.work_dir, end_date=cutoff, merge_at_end=args.merge_at_end)
    except (RuntimeError, ValueError) as exc:
        print(f"Stopped: {exc}. Progress and responses saved; rerun the same command to resume.")
        raise SystemExit(1) from None
    print(f"{result['status']}: added {result['added_this_run']} records this run; "
          f"{client.request_count} new attempts; see {args.work_dir / 'progress.json'}")
    print(f"Pending requests: {len(result['pending_requests'])}; period reviews: {len(result['period_review'])}")


if __name__ == "__main__":
    main()
