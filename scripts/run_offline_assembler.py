"""Assemble complete cached months, audit eligibility, and report sample targets."""

import argparse
from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import time
from unittest.mock import patch

from kr_quant.bulk_cached import BulkCachedData
from kr_quant.company_overview import DEFAULT_CACHE
from kr_quant.config import BacktestConfig, DataMode
from kr_quant.factors import score_factors
from kr_quant.portfolio import FactorStrategy
from kr_quant.risk import RiskState


@contextmanager
def offline_only():
    """Fail closed even if a future dependency accidentally attempts networking."""
    def blocked(*args, **kwargs):
        raise RuntimeError("Network access is forbidden in the offline assembler")
    with patch.object(socket.socket, "connect", blocked), \
            patch.object(socket.socket, "connect_ex", blocked), \
            patch.object(socket, "create_connection", blocked), \
            patch.object(socket, "getaddrinfo", blocked):
        yield


def assemble_report(start, end, *, cache_dir, financials_path, sample_month=None,
                    company_cache_dir=DEFAULT_CACHE, disclosure_dir=Path("cache/opendart/list"), data_only=False):
    config = BacktestConfig(data_mode=DataMode.APPROXIMATE, fundamental_lag_days=0)
    cache = BulkCachedData(start, end, cache_dir=cache_dir, financials_path=financials_path,
                           company_cache_dir=company_cache_dir, disclosure_dir=disclosure_dir)
    rows, sample = [], None
    sample_month = sample_month or end
    for signal in cache.month_ends:
        snapshot = cache.snapshot_for(signal, config)
        scored = None if data_only else score_factors(snapshot, config)
        targets = None if data_only else FactorStrategy(config).targets(snapshot, {}, RiskState(1_000_000))
        reasons = Counter(reason for values in scored.rejected.values() for reason in values) if scored else Counter()
        summary = {"signal_date": str(signal), "stocks": len(snapshot.stocks),
                   "with_fundamentals": sum(s.fundamentals is not None for s in snapshot.stocks),
                   "with_current_ksic": sum(s.is_financial is not None for s in snapshot.stocks),
                    "eligible": len(scored.scores) if scored else None,
                    "target_count": len(targets.weights) if targets else None,
                   "rejections": dict(reasons),
                   "membership_mismatch": cache.membership_mismatches[signal]}
        rows.append(summary)
        if not data_only and str(signal)[:7] == sample_month:
            stocks = {s.ticker: s for s in snapshot.stocks}
            scores = {s.ticker: s for s in scored.scores}
            sample = {**summary, "holdings_assumption": "empty; fresh monthly targets, not a simulated portfolio",
                      "targets": [{"ticker": ticker, "name": cache.names.get((signal, ticker), ticker),
                                   "weight": weight, "score": scores[ticker].composite,
                                   "market": stocks[ticker].market.value, "sector": stocks[ticker].sector}
                                  for ticker, weight in targets.weights.items() if weight > 0]}
    if sample is None and not data_only:
        raise ValueError("Sample month must be in requested range")
    assert len(rows) == len(cache.month_ends)
    assert cache.daily_files_read == len(cache.trading_days) * 2
    return {"status": "assembled_data_only" if data_only else "assembled", "data_only": data_only,
             "start": start, "end": end, "network_requests": 0,
             "data_mode": config.data_mode.value, "daily_files_read": cache.daily_files_read,
            "fundamental_lag_days": 0,
            "classification_policy": "approximate current company.json KSIC 2-digit division; financial=64/65/66",
            "correction_policy": "approximate: original filing date with final corrected values",
            "daily_rows": len(cache.daily), "loaded_sessions": len(cache.trading_days),
            "snapshot_count": len(rows), "months": rows, "sample": sample,
            "limitations": [
                "Security type uses dated KRX basic info; sector and financial status use approximate current OpenDART KSIC. Missing/invalid codes remain unknown and excluded.",
                "All historical KRX members are retained. Missing OpenDART fundamentals remain missing; the Y/K export has known survivor coverage gaps.",
                "Final corrected values use original filing availability by explicit approximate policy; this introduces revision look-ahead. Original filing vintages are unavailable.",
                "PER=signal market cap/annual income, PBR=signal market cap/annual equity, EPS=annual income/current listed shares. Matched CFS preferred, OFS fallback; total income/equity may include minority interests and preferred capital.",
                "FLUC_RT is rounded by KRX; chained prices have rounding drift and exclude dividends. Suspension is inferred from zero/missing open or volume, not a verified delisting date.",
                "This is snapshot assembly and target generation, not a performance backtest."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--sample-month")
    parser.add_argument("--data-only", action="store_true", help="Assemble data without scoring, targets or strategy evaluation")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    parser.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--company-cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--disclosures", type=Path, default=Path("cache/opendart/list"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    with offline_only():
        report = assemble_report(args.start, args.end, cache_dir=args.cache_dir,
                                  financials_path=args.financials, sample_month=args.sample_month,
                                  company_cache_dir=args.company_cache_dir, disclosure_dir=args.disclosures,
                                  data_only=args.data_only)
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(args.output)
    print(f"{report['snapshot_count']} snapshots; {report['daily_rows']} daily rows; "
          f"{report['elapsed_seconds']} seconds; zero network requests")
    if report["sample"] is not None:
        print(f"Sample {report['sample']['signal_date']}: {report['sample']['target_count']} targets")
    print(f"Report: {args.output}")
    print("ASSEMBLER_OFFLINE_RUN_PASSED")


if __name__ == "__main__":
    main()
