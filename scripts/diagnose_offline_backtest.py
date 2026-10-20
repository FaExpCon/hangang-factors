"""Offline attribution experiments for default accounting rules and halt=none.

Saved-only mode is a fast audit of existing output. Full mode loads the local
cache once and runs diagnostic portfolios; it never changes production defaults.
"""

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import date
import json
from math import isclose
from pathlib import Path
from statistics import mean
import time

from kr_quant.backtest import HaltPolicy, month_ends, run_backtest
from kr_quant.bulk_cached import BulkCachedData
from kr_quant.company_overview import DEFAULT_CACHE, save_json
from kr_quant.config import BacktestConfig, Costs, DataMode
from kr_quant.contracts import Targets, validate_targets
from kr_quant.factors import score_factors
from kr_quant.report import metrics, report
from scripts.compare_offline_backtests import write_table
from scripts.run_offline_assembler import offline_only
from scripts.run_offline_backtest import check_mechanics


BASE = BacktestConfig(data_mode=DataMode.APPROXIMATE, fundamental_lag_days=0)
CONVENTIONS = [
    "All simulations use default accounting eligibility, halt=none, cash initialization, month-end signals and next-session opens.",
    "Eligible equal-weight: all eligible stocks, 1/N weights, monthly rebalance, no market filter, sector or position cap.",
    "Single factors: strict monthly top 20 by value, quality (PBR/PER), or 12-1 momentum; ticker breaks ties; 1/N weights; no retention buffer, sector cap or market filter. This isolates ranks, not the full default construction rules.",
    "Default filter off changes only below_ma_exposure to 1. Zero costs remove commission, sell tax AND slippage; signals/weights are recomputed, not a fixed-trade fee add-back.",
    "Default filter-on/off x costs-on/off is a two-by-two experiment. Differences are conditional comparisons, not additive causal contributions.",
    "All simulated portfolios use identical illustrative costs when enabled; index buy-and-hold remains a frictionless price-level benchmark.",
    "Equal-weight and top-20 weights are targets: cash limits, costs, missing opens, frozen positions and artificial conversion can cause realized weights to differ. No renormalization to surviving names or future-known availability is used.",
    "Universe orders use ticker order, independent of composite rank; factor orders use descending factor rank. The common executor can leave small cash-constrained allocation differences after costs.",
    "Annual returns use prior year-end close as denominator, including the first session's return. First/last years are partial (2016 Jul-Dec, 2020 Jan-Nov); annual returns are not annualized.",
    "All portfolios start in cash; July's first month-end signal buys in August. Indices start at the first July close; the eligible benchmark shares the strategy's cash start.",
    "Dividends are not modeled. Annual payout totals without per-share entitlements and ex/record/payment dates cannot establish a portfolio total return.",
    "Final-corrected annual accounts at original filing dates, current KSIC, OpenDART survivor coverage gaps and artificial suspension conversion remain approximate.",
]


def yearly_returns(series):
    """Link years without dropping the first session's return at each boundary."""
    if not series or tuple(sorted(set(d for d, _ in series))) != tuple(d for d, _ in series):
        raise ValueError("nonempty ordered unique daily series required")
    metrics(series)  # Validate the series using the common metric contract.
    rows = []
    base_day, base_value = series[0]
    years = sorted({d.year for d, _ in series})
    for year in years:
        observations = [(d, v) for d, v in series if d.year == year]
        end_day, end_value = observations[-1]
        rows.append({"year": year, "base_date": str(base_day), "end_date": str(end_day),
                     "total_return": end_value / base_value - 1 if base_value > 0 else None})
        base_day, base_value = end_day, end_value
    return rows


def read_saved(path):
    output = json.loads(path.read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(asdict(BASE)))
    if output.get("halt_policy") != "none" or output.get("config") != expected:
        raise ValueError("Saved baseline must be default accounting/configuration with halt=none and zero extra lag")
    series = tuple((date.fromisoformat(p["date"]), p["equity"]) for p in output["equity"])
    metrics(series)
    for name in ("KOSPI200", "KOSDAQ"):
        benchmark_days = tuple(date.fromisoformat(p["date"]) for p in output["benchmarks"][name])
        if benchmark_days != tuple(d for d, _ in series):
            raise ValueError("Saved benchmark sessions differ from strategy sessions")
    return output


def direct_costs(output):
    """Exact modeled cash cost audit; explicitly not a no-cost backtest."""
    costs = output["config"]["costs"]
    slip = costs["slippage_bps"] / 10000
    result = {"commission": 0.0, "sell_tax": 0.0, "slippage": 0.0}
    for event in output["trade_log"]:
        if event["action"] not in ("buy", "sell"):
            continue
        buy = event["action"] == "buy"
        raw_open = event["price"] / (1 + slip if buy else 1 - slip)
        notional = event["shares"] * event["price"]
        result["commission"] += notional * costs["commission_bps"] / 10000
        if not buy:
            result["sell_tax"] += notional * costs["transaction_tax_bps"] / 10000
        result["slippage"] += event["shares"] * abs(event["price"] - raw_open)
    result["total"] = sum(result.values())
    elapsed = (date.fromisoformat(output["equity"][-1]["date"]) - date.fromisoformat(output["equity"][0]["date"])).days
    result["annual_cost_over_average_equity"] = result["total"] / mean(p["equity"] for p in output["equity"]) * 365.25 / elapsed if elapsed else None
    result["convention"] = "Sum of modeled trading frictions; annualized over average net equity. Not a counterfactual return or CAGR drag."
    return result


def summarize_output(output):
    series = tuple((date.fromisoformat(p["date"]), p["equity"]) for p in output["equity"])
    exposures = [1 - p["cash"] / p["equity"] if p["equity"] else 0 for p in output["equity"]]
    peak_date, peak = series[0]
    worst = 0.0
    drawdown = {"peak_date": str(peak_date), "trough_date": str(peak_date)}
    for day, value in series:
        if value > peak:
            peak_date, peak = day, value
        if value / peak - 1 < worst:
            worst = value / peak - 1
            drawdown = {"peak_date": str(peak_date), "trough_date": str(day)}
    return {"metrics": metrics(series), "yearly": yearly_returns(series), "drawdown_dates": drawdown,
            "initial_equity": series[0][1],
            "annual_turnover": output["performance"]["annual_turnover"],
            "trades": output["performance"]["trades"], "mean_exposure": mean(exposures),
            "yearly_mean_exposure": {str(y): mean(e for (d, _), e in zip(series, exposures) if d.year == y)
                                     for y in sorted({d.year for d, _ in series})},
            "direct_costs": direct_costs(output),
            "event_counts": dict(Counter(e["action"] for e in output["trade_log"]))}


def saved_diagnosis(output):
    portfolios = {"default_filter_on": summarize_output(output)}
    for name in ("KOSPI200", "KOSDAQ"):
        series = tuple((date.fromisoformat(p["date"]), p["close"]) for p in output["benchmarks"][name])
        portfolios[name] = {"metrics": metrics(series), "yearly": yearly_returns(series)}
    return {"status": "saved_baseline_only; counterfactual runs pending", "network_requests": 0,
            "conventions": CONVENTIONS, "portfolios": portfolios, "experiments": {},
            "dividends": {"status": "unavailable in backtest inputs",
                          "reason": "Official daily price responses and assembled annual accounts do not provide holding-level dated dividend entitlements. Legacy sample DIV/DPS fields do not cover this historical universe."}}


def diagnostic_targets(snapshot, holdings, config, mode, scores=None):
    scores = score_factors(snapshot, config).scores if scores is None else scores
    if mode == "eligible_equal_weight":
        selected = sorted(s.ticker for s in scores)
    else:
        if mode not in ("value", "quality", "momentum"):
            raise ValueError("unknown diagnostic portfolio")
        selected = [s.ticker for s in sorted(scores, key=lambda s: (-getattr(s, mode), s.ticker))[:20]]
    weights = {ticker: 1 / len(selected) for ticker in selected}
    # Absent/frozen names are handled by the existing executor's union of shares
    # and targets. Explicit zeros apply to present tradable strategy holdings.
    present = {s.ticker for s in snapshot.stocks}
    exits = {t: ("diagnostic_monthly_exit",) for t in holdings if t not in weights and t in present}
    weights.update({t: 0.0 for t in exits})
    targets = Targets(snapshot.signal_date, weights, exits)
    validate_targets(targets, snapshot)
    return targets


def experiments():
    for name, config in (("default_filter_on", BASE),
                         ("default_filter_off", replace(BASE, below_ma_exposure=1.0)),
                         ("default_filter_on_zero_cost", replace(BASE, costs=Costs(0, 0, 0))),
                         ("default_filter_off_zero_cost", replace(BASE, below_ma_exposure=1.0, costs=Costs(0, 0, 0)))):
        yield name, config, None
    for mode in ("eligible_equal_weight", "value", "quality", "momentum"):
        yield mode, BASE, mode


def counterfactuals(portfolios):
    def delta(left, right):
        return {key: portfolios[left]["metrics"][key] - portfolios[right]["metrics"][key]
                if portfolios[left]["metrics"][key] is not None and portfolios[right]["metrics"][key] is not None else None
                for key in ("total_return", "cagr", "mdd")}
    return {"filter_off_minus_on_net": delta("default_filter_off", "default_filter_on"),
            "filter_off_minus_on_zero_cost": delta("default_filter_off_zero_cost", "default_filter_on_zero_cost"),
            "zero_cost_minus_net_filter_on": delta("default_filter_on_zero_cost", "default_filter_on"),
            "zero_cost_minus_net_filter_off": delta("default_filter_off_zero_cost", "default_filter_off"),
            "selection_and_construction_minus_universe": delta("default_filter_off", "eligible_equal_weight"),
            **{f"{mode}_minus_universe": delta(mode, "eligible_equal_weight") for mode in ("value", "quality", "momentum")}}


def pct(value):
    return "n/a" if value is None else f"{value:.2%}"


def render(summary):
    portfolios = summary["portfolios"]
    lines = ["# Offline default/no-halt diagnosis", "", f"Status: {summary['status']}", "",
             "## Year-by-year price returns", "",
             "2016 is July–December; 2020 is January–November. Each full year includes its first session's return.", "",
             "| Year | " + " | ".join(portfolios) + " |", "|---|" + "---:|" * len(portfolios)]
    years = sorted({r["year"] for p in portfolios.values() for r in p["yearly"]})
    for year in years:
        lines.append(f"| {year} | " + " | ".join(pct(next((r["total_return"] for r in p["yearly"] if r["year"] == year), None)) for p in portfolios.values()) + " |")
    lines += ["", "## Full period", "", "| Portfolio | CAGR | MDD | Sharpe | Sortino | Mean exposure |", "|---|---:|---:|---:|---:|---:|"]
    for name, portfolio in portfolios.items():
        m = portfolio["metrics"]
        ratios = ["n/a" if m[k] is None else f"{m[k]:.3f}" for k in ("sharpe", "sortino")]
        lines.append(f"| {name} | {pct(m['cagr'])} | {pct(m['mdd'])} | {ratios[0]} | {ratios[1]} | {pct(portfolio.get('mean_exposure'))} |")
    lines += ["", "## Conditional comparisons", ""]
    for name, values in summary["experiments"].items():
        lines.append(f"- {name}: CAGR difference {pct(values['cagr'])}; cumulative return difference {pct(values['total_return'])}; MDD difference {pct(values['mdd'])} (percentage points).")
    if not summary["experiments"]:
        lines.append("Equal-weight universe, single factors, market-filter effect and no-cost returns require the full offline command. No attribution to these causes is established yet.")
    else:
        effects = summary["experiments"]
        ordered = sorted(("value", "quality", "momentum"), key=lambda n: portfolios[n]["metrics"]["total_return"], reverse=True)
        universe_gap = portfolios["eligible_equal_weight"]["metrics"]["cagr"] - portfolios["KOSPI200"]["metrics"]["cagr"]
        lines += ["", "## Reading the experiment", "",
                  f"- Eligible-universe CAGR minus KOSPI200 CAGR: {pct(universe_gap)} points. This measures the combined eligibility/weighting/cost/start-convention difference from the index, not pure size exposure.",
                  f"- Single-factor cumulative-return order: {' > '.join(ordered)}. Compare each annual row with eligible_equal_weight to see whether outperformance is persistent or concentrated in one year; this short sample does not establish statistical significance.",
                  f"- Filter-off minus filter-on net CAGR: {pct(effects['filter_off_minus_on_net']['cagr'])} points. Positive means the filter reduced returns in this sample; compare MDD before judging its trade-off.",
                  f"- Removing all modeled frictions changes default/filter-on CAGR by {pct(effects['zero_cost_minus_net_filter_on']['cagr'])} points. This rerun includes compounding and cash/holdings changes."]
    base = portfolios["default_filter_on"]
    lines += ["", "## Baseline observations", "",
              f"- Average invested exposure: {pct(base['mean_exposure'])}; annual turnover: {base['annual_turnover']:.3f}; trades: {base['trades']}.",
              f"- Direct modeled frictions: {base['direct_costs']['total']:.2f} on initial equity {base['initial_equity']:.2f}; annualized / average net equity: {pct(base['direct_costs']['annual_cost_over_average_equity'])}. This is not a no-cost return or CAGR drag.",
              f"- Lifetime MDD spans peak {base['drawdown_dates']['peak_date']} to trough {base['drawdown_dates']['trough_date']}.",
              "- Average invested exposure by year: " + "; ".join(f"{y}: {pct(e)}" for y,e in base["yearly_mean_exposure"].items()) + ".",
              f"- Executor event counts: {base['event_counts']}. Missing fills/conversions should be checked separately from rank performance.",
              "- Factor ranks and default portfolio construction are distinct: strict top-20 factor portfolios remove the top-30 retention buffer and sector cap. The default/off vs universe comparison includes selection, concentration and these construction effects.",
              "", "## Dividends", "", summary["dividends"]["reason"],
              "No defensible portfolio dividend-return estimate is available. Both strategy and indices omit dividends; only their differential dividend contribution would explain the relative gap.",
              "", "## Conventions and limitations", ""]
    lines.extend(f"- {c}" for c in summary["conventions"])
    return "\n".join(lines) + "\n"


def save_summary(directory, summary):
    directory.mkdir(parents=True, exist_ok=True)
    save_json(directory / "summary.json", summary)
    (directory / "diagnosis.md").write_text(render(summary), encoding="utf-8")
    rows = [{"portfolio": name, **row} for name, p in summary["portfolios"].items() for row in p["yearly"]]
    write_table(directory / "yearly_returns.csv", rows)
    write_table(directory / "full_period.csv", [{"portfolio": name, **p["metrics"],
        "annual_turnover": p.get("annual_turnover"), "trades": p.get("trades"),
        "mean_exposure": p.get("mean_exposure")} for name, p in summary["portfolios"].items()])


def run_diagnosis(args):
    with offline_only():
        baseline = read_saved(args.baseline)
        summary = saved_diagnosis(baseline)
        if args.saved_only:
            save_summary(args.output_dir, summary)
            print(f"Saved baseline audit complete: {args.output_dir / 'diagnosis.md'}", flush=True)
            return summary
        print(f"Loading local cache {args.start}..{args.end}; eight diagnostic runs", flush=True)
        cache = BulkCachedData(args.start, args.end, cache_dir=args.cache_dir, financials_path=args.financials,
                               company_cache_dir=args.company_cache_dir, disclosure_dir=args.disclosures)
        inputs = cache.backtest_inputs(BASE)
        if (str(inputs["days"][0]), str(inputs["days"][-1])) != (baseline["equity"][0]["date"], baseline["equity"][-1]["date"]):
            raise ValueError("Requested period differs from saved baseline")
        for name in ("KOSPI200", "KOSDAQ"):
            if any(not isclose(inputs["index_closes"][name][date.fromisoformat(p["date"])], p["close"], rel_tol=1e-12)
                   for p in baseline["benchmarks"][name]):
                raise ValueError("Cached benchmark differs from saved baseline")
        signals = sorted(month_ends(inputs["days"]) - {inputs["days"][-1]})
        snapshots = {}
        scored = {}
        for number, signal in enumerate(signals, 1):
            snapshots[signal] = cache.snapshot_for(signal, BASE)
            scored[signal] = score_factors(snapshots[signal], BASE).scores
            print(f"Snapshots [{number}/{len(signals)}] {signal}", flush=True)
        inputs["snapshot_for"] = snapshots.__getitem__
        for number, (name, config, mode) in enumerate(experiments(), 1):
            started = time.monotonic()
            print(f"[{number}/8] Starting {name}", flush=True)
            target = (lambda s, h, r, cfg=config, m=mode: diagnostic_targets(s, h, cfg, m, scored[s.signal_date])) if mode else None
            result = run_backtest(**inputs, config=config, halt_policy=HaltPolicy.NONE, target_for=target)
            performance = report(result)["full"]
            output = {"config": asdict(config), "halt_policy": "none", "performance": performance,
                      "equity": [{**asdict(p), "date": str(p.date), "risk": p.risk.value} for p in result.equity],
                      "trade_log": [{**asdict(e), "date": str(e.date)} for e in result.events],
                      "mechanics": check_mechanics(result, inputs["days"], 1_000_000),
                      "eligible_counts": {str(d): len(scores) for d, scores in scored.items()}}
            if name == "default_filter_on":
                if len(result.equity) != len(baseline["equity"]) or any(
                        str(p.date) != old["date"] or not isclose(p.equity, old["equity"], rel_tol=1e-10, abs_tol=1e-5)
                        for p, old in zip(result.equity, baseline["equity"])):
                    raise ValueError("Default rerun differs from saved baseline; review data/code before attributing results")
            summary["portfolios"][name] = summarize_output(output)
            save_json(args.output_dir / "runs" / f"{name}.json", output)
            summary["status"] = f"partial: {number}/8 diagnostic runs complete"
            save_summary(args.output_dir, summary)
            print(f"[{number}/8] Completed {name}: CAGR={pct(performance['strategy']['cagr'])}; {time.monotonic()-started:.1f}s", flush=True)
        summary["experiments"] = counterfactuals(summary["portfolios"])
        summary["status"] = "complete: 8/8 diagnostic runs; saved baseline reproduced"
        save_summary(args.output_dir, summary)
        print(f"Diagnosis complete: {args.output_dir / 'diagnosis.md'}", flush=True)
        return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-only", action="store_true")
    parser.add_argument("--baseline", type=Path, default=Path("outputs/halt_selection_comparison/2016-07_2020-11_none_default.json"))
    parser.add_argument("--start", default="2016-07")
    parser.add_argument("--end", default="2020-11")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/diagnosis_2016-07_2020-11"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    parser.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--company-cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--disclosures", type=Path, default=Path("cache/opendart/list"))
    return parser.parse_args(argv)


def main():
    run_diagnosis(parse_args())


if __name__ == "__main__":
    main()
