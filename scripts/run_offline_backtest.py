"""Offline cached-universe backtest; approximate data and illustrative execution."""

import argparse
from collections import Counter
from dataclasses import asdict
from math import isfinite
from pathlib import Path
import time

from kr_quant.backtest import HaltPolicy, month_ends, rebalance_dates, run_backtest
from kr_quant.bulk_cached import BulkCachedData
from kr_quant.company_overview import DEFAULT_CACHE, save_json
from kr_quant.config import BacktestConfig, DataMode, QualityMetric
from kr_quant.factors import score_factors
from kr_quant.report import report
from scripts.run_offline_assembler import offline_only


def check_mechanics(result, days, initial_cash):
    """Check actual engine results; do not infer strategy merit from returns."""
    expected = tuple(d for d in days if d in rebalance_dates(days, result.config) and d != days[-1])
    if tuple(point.date for point in result.equity) != days or result.signal_dates != expected:
        raise AssertionError("Missing daily equity or month-end signals")
    if any(not isfinite(p.equity) or not isfinite(p.cash) or p.equity < -1e-6 or p.cash < -1e-6
           or p.cash > p.equity + 1e-6 for p in result.equity):
        raise AssertionError("Invalid equity/cash accounting")
    if abs(result.equity[0].equity - initial_cash) > 1e-6:
        raise AssertionError("Simulation did not start in cash")
    next_sessions = {days[days.index(signal) + 1] for signal in expected}
    deferred = {}
    for event in result.events:
        if event.date not in days or not all(isfinite(v) for v in (event.shares, event.price, event.cash_change)):
            raise AssertionError("Invalid event date or value")
        if event.action in ("buy", "sell"):
            retry = event.action == "sell" and event.reason.startswith("deferred_sell:")
            if retry:
                queued = deferred.get(event.ticker)
                if not queued or event.date <= queued[0] or event.shares > queued[1] + 1e-8:
                    raise AssertionError("Deferred sale lacks an earlier pending sale")
            if (event.date not in next_sessions and not retry) or event.shares <= 0 or event.price <= 0:
                raise AssertionError("Fill did not occur at a valid next-session rebalance or deferred sale")
            if (event.action == "buy" and event.cash_change >= 0) or (event.action == "sell" and event.cash_change <= 0):
                raise AssertionError("Wrong fill cash direction")
        if event.action == "deferred_sell":
            deferred[event.ticker] = (event.date, event.shares)
        elif event.action in ("sell", "approximate_cash_conversion"):
            deferred.pop(event.ticker, None)
    return {"status": "passed", "sessions": len(days), "signals": len(expected),
            "rebalance_frequency": result.config.rebalance_frequency,
            "first_session": str(days[0]), "last_session": str(days[-1]),
            "first_signal": str(expected[0]) if expected else None,
            "last_signal": str(expected[-1]) if expected else None,
            "event_counts": dict(Counter(e.action for e in result.events)),
            "risk_session_counts": dict(Counter(p.risk.value for p in result.equity)),
             "checks": ["one equity row per session", "expected configured rebalance signals except final session",
                       "finite nonnegative equity/cash", "cash does not exceed long-only equity",
                        "starts in cash", "buys/rebalances at next session; sale retries backed by earlier deferrals",
                       "finite event values and positive fill shares/prices", "correct fill cash direction"]}


def run_cached_backtest(cache, config, initial_cash=1_000_000.0, halt_policy=HaltPolicy.PERMANENT):
    """Run entirely from cache; retain full diagnostics for each policy."""
    halt_policy = HaltPolicy(halt_policy)
    diagnostics = []

    def snapshot_for(signal):
        snapshot = cache.snapshot_for(signal, config)
        scored = score_factors(snapshot, config)
        flags = []
        by_ticker = {s.ticker: s for s in snapshot.stocks}
        for ticker, reasons in scored.accounting_flags.items():
            fund = by_ticker[ticker].fundamentals
            flags.append({"ticker": ticker, "name": cache.names.get((signal, ticker), ticker),
                          "reasons": list(reasons), "net_income": fund.net_income,
                          "operating_income": fund.operating_income,
                          "net_to_operating_ratio": fund.net_income / fund.operating_income if fund.operating_income > 0 else None,
                          "observed_on": str(fund.observed_on), "available_on": str(fund.available_on),
                          "eligible": ticker not in scored.rejected})
        diagnostics.append({"signal_date": str(signal), "stocks": len(snapshot.stocks),
                            "eligible": len(scored.scores), "accounting_flags": flags,
                            "rejection_counts": dict(Counter(r for reasons in scored.rejected.values() for r in reasons))})
        return snapshot

    inputs = cache.backtest_inputs(config)
    inputs["snapshot_for"] = snapshot_for
    result = run_backtest(**inputs, config=config, initial_cash=initial_cash, halt_policy=halt_policy)
    mechanics = check_mechanics(result, inputs["days"], initial_cash)
    summary = report(result)
    halted_observations = sum(p.risk.value == "HALTED" for p in result.equity)
    return {"label": "APPROXIMATE — OFFLINE POLICY/SELECTION COMPARISON", "network_requests": 0,
            "purpose": "Compare halt and accounting alternatives under identical approximate data policies",
            "halt_policy": halt_policy.value,
            "days_halted": halted_observations if halt_policy is not HaltPolicy.NONE else 0,
            "informational_halted_sessions": halted_observations,
            "halt_count_convention": "end-of-session HALTED observations; none reports zero restrictive days",
            "performance": summary["full"], "metric_conventions": summary["conventions"],
            "config": asdict(config), "initial_cash": initial_cash, "mechanics": mechanics,
            "accounting_policy": "original filing availability with final corrected values; current KSIC proxy; zero extra lag",
            "diagnostic_policy": "Flags never change eligibility, ranking, weights, or execution",
            "limitations": ["annual accounts; no original filing vintages or quarterly updates",
                            "operating income and end-of-year total assets from the same report/basis as net income/equity",
                            "net/operating flags indicate a discrepancy, not proof of one-off gains",
                            "current KSIC classifications are forward-looking",
                            "OpenDART Y/K financial export has known historical survivor coverage gaps",
                            "fractional shares in FLUC_RT-adjusted price space; no dividends; other corporate actions not modeled",
                            "fixed illustrative costs/taxes; untradable recovery is artificial",
                            f"backtest halt policy={halt_policy.value}; live/shared risk rule remains manual",
                            "last session has no new executable signal because next-session prices are outside the window"],
            "signal_dates": [str(d) for d in result.signal_dates], "signal_diagnostics": diagnostics,
            "equity": [{**asdict(p), "date": str(p.date), "risk": p.risk.value} for p in result.equity],
            "trade_log": [{**asdict(e), "date": str(e.date)} for e in result.events],
            "benchmarks": {name: [{"date": str(d), "close": v} for d, v in series]
                           for name, series in result.benchmarks.items()}}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-07")
    parser.add_argument("--end", default="2020-11")
    parser.add_argument("--output", type=Path, default=Path("outputs/backtest_2016-07_2020-11_default_mechanics.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    parser.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--company-cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--disclosures", type=Path, default=Path("cache/opendart/list"))
    parser.add_argument("--exclude-nonpositive-operating-income", action="store_true")
    parser.add_argument("--quality-metric", choices=[m.value for m in QualityMetric], default=QualityMetric.PBR_PER.value)
    parser.add_argument("--net-income-flag-ratio", type=float, default=3.0)
    parser.add_argument("--halt-policy", choices=[p.value for p in HaltPolicy], default=HaltPolicy.PERMANENT.value)
    return parser.parse_args(argv)


def config_for(args):
    return BacktestConfig(data_mode=DataMode.APPROXIMATE, fundamental_lag_days=0,
                          exclude_nonpositive_operating_income=args.exclude_nonpositive_operating_income,
                          quality_metric=QualityMetric(args.quality_metric),
                          net_income_operating_income_flag_ratio=args.net_income_flag_ratio)


def main():
    args = parse_args()
    config = config_for(args)
    started = time.monotonic()
    print(f"Offline backtest {args.start}..{args.end}; halt={args.halt_policy}; quality={config.quality_metric.value}; "
          f"positive operating filter={config.exclude_nonpositive_operating_income}", flush=True)
    with offline_only():
        cache = BulkCachedData(args.start, args.end, cache_dir=args.cache_dir, financials_path=args.financials,
                               company_cache_dir=args.company_cache_dir, disclosure_dir=args.disclosures)
        print(f"Cache loaded: {len(cache.daily)} rows; constructing bars and running existing engine", flush=True)
        output = run_cached_backtest(cache, config, halt_policy=HaltPolicy(args.halt_policy))
    output.update(start=args.start, end=args.end, elapsed_seconds=round(time.monotonic() - started, 2))
    save_json(args.output, output)
    print(f"{output['mechanics']['sessions']} sessions; {output['mechanics']['signals']} signals; "
          f"events={output['mechanics']['event_counts']}; output={args.output}")
    print("OFFLINE_BACKTEST_MECHANICS_PASSED")


if __name__ == "__main__":
    main()
