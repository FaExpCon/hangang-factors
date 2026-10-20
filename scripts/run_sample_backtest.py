"""Offline mechanics run; 2023 cached observations serve only as warm-up."""

from datetime import date
import json
from pathlib import Path

import pandas as pd

from kr_quant.backtest import DailyBar, run_backtest
from kr_quant.config import BacktestConfig
from kr_quant.contracts import Market
from kr_quant.data import PykrxLoader, SampleRequest, current_classifications, make_snapshot
from kr_quant.report import report
from scripts.sample_probe import EXAMPLE_TICKERS


def main() -> None:
    sample = PykrxLoader(api=object()).load_sample(
        SampleRequest(date(2023, 1, 1), date(2024, 12, 31), EXAMPLE_TICKERS), offline=True)
    frames = {}
    retrieved_on = date(2026, 10, 14)
    for market in Market:
        payload = json.loads((Path("data/cache/current_sector") / f"{retrieved_on}_{market.value}.json").read_text(encoding="utf-8"))
        frames[market] = pd.DataFrame.from_records(
            ({"ticker": row["ticker"], "종목명": row["name"], "업종명": row["sector"]} for row in payload["records"])).set_index("ticker")
    matched = tuple(t for t in EXAMPLE_TICKERS if sum(t in f.index for f in frames.values()) == 1)
    classifications = current_classifications(matched, frames, retrieved_on=retrieved_on)
    # Only stocks with a current market classification can be scored. Keep
    # unclassified price histories for explicit held-position handling later.
    from dataclasses import replace
    scored_sample = replace(sample, stocks={t: sample.stocks[t] for t in matched})
    calendar = tuple(d.date() for d in sample.indices["KOSPI"].index if d.year == 2024)
    cfg = BacktestConfig()
    bars = {ticker: {d.date(): DailyBar(float(row["시가"]), float(row["종가"]))
                     for d, row in dataset["prices"].iterrows()}
            for ticker, dataset in sample.stocks.items()}
    indices = {name: {d.date(): float(value) for d, value in frame["종가"].items()}
               for name, frame in sample.indices.items()}
    result = run_backtest(days=calendar, bars=bars, index_closes=indices,
                          snapshot_for=lambda day: make_snapshot(scored_sample, day, cfg, classifications),
                          config=cfg)
    output = report(result)
    output["sample_note"] = "MECHANICS CHECK ONLY: 24 selected example codes; 23 currently classifiable; 2023 indicator warm-up; 2024 equity begins in cash and first trade is after January month-end; not a full-universe performance claim"
    output["excluded_current_classification"] = sorted(set(EXAMPLE_TICKERS) - set(matched))
    path = Path("outputs/sample_backtest_2024_approximate.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    if len(result.equity) != len(calendar) or not result.signal_dates:
        raise AssertionError("daily equity/signals missing")
    print(f"SAMPLE_BACKTEST_OK days={len(result.equity)} signals={len(result.signal_dates)} events={len(result.events)} output={path}")


if __name__ == "__main__":
    main()
