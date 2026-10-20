"""Explicitly bounded, resumable small pykrx probe. No automatic full-history fetch."""

import argparse
from datetime import date
import json
import os
from pathlib import Path
import time

from kr_quant.data import PykrxLoader, SampleRequest, coverage


# Liquid, recognisable example codes, not an as-of-date universe or proof of listing.
EXAMPLE_TICKERS = (
    "005930", "000660", "005380", "000270", "035420", "035720",
    "051910", "006400", "068270", "012330", "105560", "055550",
    "207940", "028260", "066570", "017670", "086790", "096770",
    "247540", "086520", "196170", "293490", "035900", "091990",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Small approximate pykrx data coverage probe")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 12, 31))
    parser.add_argument("--tickers", nargs="+", default=list(EXAMPLE_TICKERS))
    parser.add_argument("--delay", type=float, default=0.5, help="minimum seconds between requests")
    parser.add_argument("--offline", action="store_true", help="require cached responses")
    parser.add_argument("--out", type=Path, default=Path("outputs/stage2_coverage.json"))
    args = parser.parse_args()
    if args.offline and args.out == Path("outputs/stage2_coverage.json"):
        args.out = Path("outputs/stage2_coverage_offline.json")
    request = SampleRequest(args.start, args.end, tuple(args.tickers))
    start = time.monotonic()
    try:
        loader = PykrxLoader(min_interval_seconds=args.delay, api=object() if args.offline else None)
        report = coverage(loader.load_sample(request, offline=args.offline))
        report["status"] = "complete"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        for secret in (os.environ.get("KRX_ID"), os.environ.get("KRX_PW")):
            if secret:
                reason = reason.replace(secret, "[REDACTED]")
        report = {"status": "blocked", "mode": "APPROXIMATE",
                  "requested_start": args.start.isoformat(), "requested_end": args.end.isoformat(),
                  "requested_tickers": args.tickers, "elapsed_seconds": round(time.monotonic() - start, 3),
                  "reason": reason,
                  "historical_sector_classifications": "not established; external dated classifications required",
                  "delisted_stocks": "not established; survivorship bias possible",
                  "fundamental_filing_dates": "not provided by pykrx"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['status']}: {args.out}")
    if report["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
