"""Run three halt policies x four independent accounting variants, offline."""

import argparse
import csv
from dataclasses import replace
from pathlib import Path
import time

from kr_quant.backtest import HaltPolicy
from kr_quant.bulk_cached import BulkCachedData
from kr_quant.company_overview import DEFAULT_CACHE, save_json
from kr_quant.config import BacktestConfig, DataMode, QualityMetric
from scripts.run_offline_assembler import offline_only
from scripts.run_offline_backtest import run_cached_backtest


VARIANTS = {
    "default": (False, QualityMetric.PBR_PER),
    "operating_income_filter_only": (True, QualityMetric.PBR_PER),
    "operating_income_assets_quality_only": (False, QualityMetric.OPERATING_INCOME_TO_ASSETS),
    "both": (True, QualityMetric.OPERATING_INCOME_TO_ASSETS),
}


def configurations():
    base = BacktestConfig(data_mode=DataMode.APPROXIMATE, fundamental_lag_days=0)
    for policy in HaltPolicy:
        for variant, (positive_filter, quality) in VARIANTS.items():
            yield policy, variant, replace(base, exclude_nonpositive_operating_income=positive_filter,
                                           quality_metric=quality)


def comparison_row(policy, variant, output):
    performance = output["performance"]
    strategy = performance["strategy"]
    row = {"halt_policy": policy.value, "variant": variant,
           "start": strategy["start"], "end": strategy["end"]}
    row.update({key: strategy[key] for key in ("cagr", "mdd", "sharpe", "sortino")})
    row.update(annual_turnover=performance["annual_turnover"], trades=performance["trades"],
               days_halted=output["days_halted"])
    for name in ("KOSPI200", "KOSDAQ"):
        row.update({f"{name.lower()}_buy_hold_{key}": performance[name][key]
                    for key in ("total_return", "cagr", "mdd", "sharpe", "sortino")})
    return row


def write_table(path, rows):
    """Replace atomically after each run; partial progress remains reviewable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def run_comparison(args):
    """Read cache once, run fresh portfolios, checkpoint each result and CSV row."""
    started = time.monotonic()
    rows = []
    jobs = list(configurations())
    with offline_only():
        print(f"Loading offline cache {args.start}..{args.end}; {len(jobs)} runs", flush=True)
        cache = BulkCachedData(args.start, args.end, cache_dir=args.cache_dir,
                               financials_path=args.financials, company_cache_dir=args.company_cache_dir,
                               disclosure_dir=args.disclosures)
        # An interrupted rerun starts a new table, never mixes old and new runs.
        for number, (policy, variant, config) in enumerate(jobs, 1):
            run_started = time.monotonic()
            print(f"[{number}/{len(jobs)}] Starting halt={policy.value}, variant={variant}", flush=True)
            output = run_cached_backtest(cache, config, halt_policy=policy)
            output.update(start=args.start, end=args.end, variant=variant,
                          elapsed_seconds=round(time.monotonic() - run_started, 2))
            run_path = args.runs_dir / f"{args.start}_{args.end}_{policy.value}_{variant}.json"
            save_json(run_path, output)
            rows.append(comparison_row(policy, variant, output))
            write_table(args.output, rows)
            last = rows[-1]
            print(f"[{number}/{len(jobs)}] Completed halt={policy.value}, variant={variant}; "
                  f"CAGR={last['cagr']}; MDD={last['mdd']}; trades={last['trades']}; "
                  f"days halted={last['days_halted']}; {output['elapsed_seconds']}s; table={args.output}", flush=True)
    print(f"Comparison complete: {len(rows)} rows; {time.monotonic() - started:.1f}s; {args.output}", flush=True)
    return rows


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-07")
    parser.add_argument("--end", default="2020-11")
    parser.add_argument("--output", type=Path, default=Path("outputs/comparison_2016-07_2020-11.csv"))
    parser.add_argument("--runs-dir", type=Path, default=Path("outputs/halt_selection_comparison"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    parser.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--company-cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--disclosures", type=Path, default=Path("cache/opendart/list"))
    return parser.parse_args(argv)


def main():
    run_comparison(parse_args())


if __name__ == "__main__":
    main()
