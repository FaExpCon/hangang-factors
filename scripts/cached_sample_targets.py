"""Offline audit of the 2024-12-30 targets using current (2026) proxy metadata."""

from dataclasses import replace
from datetime import date
import json
from pathlib import Path

import pandas as pd

from kr_quant.config import BacktestConfig
from kr_quant.contracts import Market
from kr_quant.data import PykrxLoader, SampleRequest, current_classifications, make_snapshot
from kr_quant.factors import score_factors
from kr_quant.portfolio import FactorStrategy
from kr_quant.risk import RiskState
from scripts.sample_probe import EXAMPLE_TICKERS


def main() -> None:
    retrieved_on = date(2026, 10, 14)
    sample = PykrxLoader(api=object()).load_sample(
        SampleRequest(date(2023, 1, 1), date(2024, 12, 31), EXAMPLE_TICKERS), offline=True)
    frames = {}
    for market in Market:
        path = Path("data/cache/current_sector") / f"{retrieved_on}_{market.value}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["retrieved_on"] != retrieved_on.isoformat() or payload["market"] != market.value:
            raise ValueError(f"sector cache metadata mismatch: {path}")
        frames[market] = pd.DataFrame.from_records(
            ({"ticker": row["ticker"], "종목명": row["name"], "업종명": row["sector"]}
             for row in payload["records"])).set_index("ticker")

    matched = tuple(ticker for ticker in EXAMPLE_TICKERS if sum(ticker in frame.index for frame in frames.values()) == 1)
    unmatched = tuple(ticker for ticker in EXAMPLE_TICKERS if ticker not in matched)
    classified = current_classifications(matched, frames, retrieved_on=retrieved_on,
                                         source="pykrx current KOSPI/KOSDAQ sector/name tables (retrieved 2026-10-14)")
    subset = replace(sample, stocks={ticker: sample.stocks[ticker] for ticker in matched})
    signal = date(2024, 12, 30)
    config = BacktestConfig()
    snapshot = make_snapshot(subset, signal, config, classified)
    scored = score_factors(snapshot, config)
    targets = FactorStrategy(config).targets(snapshot, {}, RiskState(1.0))
    by_code = {stock.ticker: stock for stock in snapshot.stocks}
    score_by_code = {score.ticker: score for score in scored.scores}
    selected = [{"ticker": ticker, "name": frames[by_code[ticker].market].loc[ticker, "종목명"],
                 "market": by_code[ticker].market.value, "sector": by_code[ticker].sector,
                 "security_type": by_code[ticker].security_type.value if by_code[ticker].security_type else None,
                 "type_inferred_from_name": True,
                 "is_financial": by_code[ticker].is_financial,
                 "financial_status_inferred_from_name": False,
                 "financial_status_inferred_from_industry": True,
                 "composite": score_by_code[ticker].composite,
                 "value": score_by_code[ticker].value,
                 "quality": score_by_code[ticker].quality,
                 "momentum": score_by_code[ticker].momentum,
                 "target_weight": weight}
                for ticker, weight in targets.weights.items() if weight > 0]
    excluded = []
    for ticker in EXAMPLE_TICKERS:
        if ticker in unmatched:
            reasons = ["missing_current_classification"]
            prices = sample.stocks[ticker]["prices"]
            if prices.empty or prices.index[-1].date() != signal:
                reasons.append("missing_signal_close")
            caps = sample.stocks[ticker]["market_cap"]
            if caps.empty or caps.index[-1].date() != signal:
                reasons.append("missing_current_market_cap")
            prices_value = prices.get("거래대금")
            if prices_value is None or len(prices_value) < 20 or prices_value.index[-1].date() != signal:
                reasons.append("missing_current_liquidity")
            reasons.append("cannot_assign_market_or_sector_without_guessing")
            excluded.append({"ticker": ticker, "name": None, "market": None, "sector": None,
                             "reasons": reasons, "type_inferred_from_name": False,
                             "financial_status_inferred_from_name": False})
        elif ticker in scored.rejected:
            stock = by_code[ticker]
            excluded.append({"ticker": ticker, "name": frames[stock.market].loc[ticker, "종목명"],
                             "market": stock.market.value, "sector": stock.sector,
                             "reasons": list(scored.rejected[ticker]),
                             "security_type": stock.security_type.value if stock.security_type else None,
                             "is_financial": stock.is_financial,
                             "type_inferred_from_name": True,
                             "financial_status_inferred_from_name": False,
                             "financial_status_inferred_from_industry": True})
    # Eligible but not selected names should not be mislabeled universe exclusions.
    not_selected = [s.ticker for s in scored.scores if s.ticker not in targets.weights]
    if len(selected) + len(excluded) + len(not_selected) != len(EXAMPLE_TICKERS):
        raise ValueError("sample reconciliation failed")
    filters = {}
    for market in Market:
        bars = next(index.closes for index in snapshot.indices if index.market is market)
        ma = sum(bar.close for bar in bars[-200:]) / 200
        filters[market.value] = {"close": bars[-1].close, "ma200": ma,
                                 "above_or_at_ma": bars[-1].close >= ma,
                                 "applied_exposure": 1 if bars[-1].close >= ma else config.below_ma_exposure}
    report = {"label": "APPROXIMATE — current 2026 classification proxy for 2024 signal",
              "signal_date": signal.isoformat(), "proxy_retrieved_on": retrieved_on.isoformat(),
              "source": "pykrx.stock.get_market_sector_classifications",
              "sample_count": len(EXAMPLE_TICKERS), "classified_count": len(matched),
              "classification_missing": list(unmatched), "market_filters": filters,
              "selected": selected, "excluded": excluded, "eligible_not_selected": not_selected,
              "notes": ["Empty starting holdings; NORMAL risk state, default configuration including 90-calendar-day fundamental lag.",
                        "All matched security types inferred from CURRENT names and financial flags from CURRENT industry names, not historical filings or verified common-share status.",
                        "091990 absent from current sector tables; no market or sector backfilled.",
                        "Market-cap bottom 30% is calculated among the 23 classified names, not a complete exchange universe."]}
    path = Path("outputs/2024-12-30_sample_targets_approximate.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"APPROXIMATE offline sample targets: {len(selected)} selected, {len(excluded)} excluded; {path}")


if __name__ == "__main__":
    main()
