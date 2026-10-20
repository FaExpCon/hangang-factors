"""Run the registered design-period comparisons only; never validation returns.

Twelve baseline/candidate runs plus two monthly eligible equal-weight references.
Expected to exceed two minutes on the real cache; run in a separate cmd window.
"""

import argparse
import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from statistics import mean

from kr_quant.backtest import HaltPolicy, run_backtest
from kr_quant.bulk_cached import BulkCachedData
from kr_quant.company_overview import DEFAULT_CACHE, save_json
from kr_quant.config import BacktestConfig, DataMode, FactorWeights, QualityMetric
from kr_quant.contracts import Targets, validate_targets
from scripts.audit_offline_range import input_identity
from scripts.compare_offline_backtests import write_table
from scripts.diagnose_offline_backtest import yearly_returns
from scripts.run_offline_assembler import offline_only
from scripts.run_offline_backtest import check_mechanics, run_cached_backtest
from kr_quant.report import report


START, END = "2016-07", "2020-12"
BASE = BacktestConfig(data_mode=DataMode.APPROXIMATE, fundamental_lag_days=0)
VARIANTS = {
    "baseline": BASE,
    "T-Q": replace(BASE, rebalance_frequency="quarterly"),
    "T-40": replace(BASE, retention_rank=40),
    "T-50": replace(BASE, retention_rank=50),
    "Q-OA": replace(BASE, quality_metric=QualityMetric.OPERATING_INCOME_TO_ASSETS),
    "W-532": replace(BASE, factors=FactorWeights(value=.5, quality=.3, momentum=.2)),
}


def configurations():
    for halt in (HaltPolicy.NONE, HaltPolicy.RESUME):
        for name, cfg in VARIANTS.items():
            yield halt, name, cfg


def validate_design(start, end):
    if (start, end) != (START, END):
        raise ValueError("Registered runner accepts only design 2016-07 through 2020-12; validation is locked")


def assert_design_sessions(days):
    if not days or days[0] < date(2016, 7, 1) or days[-1] > date(2020, 12, 31):
        raise ValueError("Returned sessions escape the design period")


def equal_weight_targets(snapshot, holdings, risk, config):
    from kr_quant.universe import filter_universe
    from kr_quant.risk import RiskLevel
    eligible = filter_universe(snapshot, config).eligible
    calendars = {i.market: i.closes for i in snapshot.indices}
    weights = {}
    for s in sorted(eligible, key=lambda s: s.ticker):
        bars = calendars[s.market]
        if len(bars) < 200 or bars[-1].date != snapshot.signal_date:
            raise ValueError("Reference requires current 200-session index")
        scale = 1.0 if bars[-1].close >= mean(b.close for b in bars[-200:]) else config.below_ma_exposure
        target = scale / len(eligible)
        weights[s.ticker] = min(target, holdings.get(s.ticker, 0)) if risk.level is RiskLevel.HALTED else target
    for t in holdings:
        weights.setdefault(t, 0.0)
    targets = Targets(snapshot.signal_date, weights, {t: ("eligible_reference_exit",) for t in holdings if not weights[t]})
    validate_targets(targets, snapshot)
    return targets


def reference_output(cache, halt):
    inputs = cache.backtest_inputs(BASE)
    result = run_backtest(**inputs, config=BASE, halt_policy=halt,
        target_for=lambda s, h, r: equal_weight_targets(s, h, r, BASE))
    return {"performance": report(result)["full"], "config": asdict(BASE),
            "halt_policy": halt.value, "network_requests": 0,
            "days_halted": sum(p.risk.value == "HALTED" for p in result.equity) if halt is not HaltPolicy.NONE else 0,
            "mechanics": check_mechanics(result, inputs["days"], 1_000_000),
            "equity": [{**asdict(p), "date": str(p.date), "risk": p.risk.value} for p in result.equity],
            "trade_log": [{**asdict(e), "date": str(e.date)} for e in result.events],
            "benchmarks": {n: [{"date": str(d), "close": v} for d,v in series]
                           for n,series in result.benchmarks.items()}}


def result_row(halt, name, output):
    perf = output["performance"]
    series = tuple((date.fromisoformat(r["date"]), r["equity"]) for r in output["equity"])
    assert_design_sessions(tuple(d for d, _ in series))
    row = {"halt_policy": halt.value, "candidate": name,
           **{k: perf["strategy"][k] for k in ("total_return", "cagr", "mdd", "sharpe", "sortino")},
           "annual_turnover": perf["annual_turnover"], "trades": perf["trades"],
           "mean_exposure": mean(1-r["cash"]/r["equity"] if r["equity"] else 0 for r in output["equity"]),
           "days_halted": output["days_halted"]}
    for y in yearly_returns(series):
        row[f"return_{y['year']}"] = y["total_return"]
    for index in ("KOSPI200", "KOSDAQ"):
        for metric in ("total_return", "cagr", "mdd", "sharpe", "sortino"):
            row[f"{index}_{metric}"] = perf[index][metric]
        if "benchmarks" in output:
            for y in yearly_returns(tuple((date.fromisoformat(p["date"]),p["close"])
                                          for p in output["benchmarks"][index])):
                row[f"{index}_return_{y['year']}"] = y["total_return"]
    return row


def run_comparison(args):
    validate_design(args.start, args.end)  # Must precede file reads/cache construction.
    if args.output.exists() or (args.runs_dir.exists() and any(args.runs_dir.iterdir())):
        raise FileExistsError("Preserve earlier attempts: choose a new output and empty runs directory")
    with offline_only():
        identity = input_identity(args.financials, args.cache_dir, args.company_cache_dir, args.disclosures)
        protocol = {"start": START, "end": END, "initial_cash": 1_000_000,
                    "warmup": "400 calendar days, >=253 sessions for snapshots",
                    "jobs": [{"halt": h.value, "candidate": n, "config": asdict(c)} for h,n,c in configurations()],
                    "market_filter": "on; current index vs trailing 200 sessions, below exposure 0.5",
                    "selection": "none for selection; resume sensitivity only; manual review, no automatic adoption",
                    "reference": "monthly eligible 1/N with market filter, same costs/halt; no sector/20-position cap",
                    "startup": "cash July 2016; monthly first signal July, quarterly first signal September",
                    "data_identity": identity}
        protocol_digest = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
        args.runs_dir.mkdir(parents=True, exist_ok=True)
        save_json(args.runs_dir / "protocol.json", {**protocol, "protocol_sha256": protocol_digest})
        print(f"Loading design cache only {START}..{END}; 12 candidates/baselines + 2 references", flush=True)
        cache = BulkCachedData(START, END, cache_dir=args.cache_dir, financials_path=args.financials,
                               company_cache_dir=args.company_cache_dir, disclosure_dir=args.disclosures)
        # Backtest session boundaries checked before any strategy is run.
        days = tuple(d for d in cache.trading_days if d >= cache.start)
        assert_design_sessions(days)
        rows = []
        jobs = [*configurations(), (HaltPolicy.NONE, "eligible_equal_weight", BASE),
                (HaltPolicy.RESUME, "eligible_equal_weight", BASE)]
        for i, (halt, name, cfg) in enumerate(jobs, 1):
            print(f"[{i}/{len(jobs)}] halt={halt.value} {name}", flush=True)
            started = time.monotonic()
            output = reference_output(cache, halt) if name == "eligible_equal_weight" else run_cached_backtest(cache, cfg, halt_policy=halt)
            row = result_row(halt, name, output)
            output.update(candidate=name, start=START, end=END, protocol_sha256=protocol_digest,
                          data_identity=identity, elapsed_seconds=round(time.monotonic()-started, 2))
            save_json(args.runs_dir / f"{halt.value}_{name}.json", output)
            rows.append(row)
            write_table(args.output, rows)
            save_json(args.runs_dir / "progress.json", {"completed": i, "total": len(jobs), "protocol_sha256": protocol_digest})
            print(f"Saved {name}; {output['elapsed_seconds']}s", flush=True)
        return rows


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default=START)
    p.add_argument("--end", default=END)
    p.add_argument("--output", type=Path, default=Path("outputs/registered_design_2016-07_2020-12/comparison.csv"))
    p.add_argument("--runs-dir", type=Path, default=Path("outputs/registered_design_2016-07_2020-12/runs"))
    p.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    p.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    p.add_argument("--company-cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--disclosures", type=Path, default=Path("cache/opendart/list"))
    return p.parse_args(argv)


if __name__ == "__main__":
    run_comparison(parse_args())
