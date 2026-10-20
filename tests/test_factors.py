from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from kr_quant.config import BacktestConfig, DataMode, FactorWeights
from kr_quant.contracts import CloseBar, DatedValue, Fundamental, IndexHistory, Market, SecurityType, Snapshot, Stock
from kr_quant.factors import score_factors
from kr_quant.universe import filter_universe


DAYS = tuple(d.date() for d in pd.bdate_range("2024-01-01", periods=253))
SIGNAL = DAYS[-1]
OLD = SIGNAL - timedelta(days=120)


def stock(ticker, cap=100, per=10, pbr=1, eps=5, market=Market.KOSPI, security=SecurityType.COMMON,
          financial=False, liquidity=2e9, closes=None):
    return Stock(ticker, market, "Tech", security, financial, DAYS[0],
                 DatedValue(cap, SIGNAL), DatedValue(liquidity, SIGNAL),
                 Fundamental(per, pbr, OLD, eps=eps),
                 tuple(CloseBar(day, 100) for day in DAYS) if closes is None else closes)


def snap(*stocks):
    indices = tuple(IndexHistory(market, tuple(CloseBar(day, 1000) for day in DAYS)) for market in Market)
    return Snapshot(SIGNAL, stocks, indices)


@pytest.mark.parametrize("per,pbr,eps", [
    (0, 1, 1), (-1, 1, 1), (10, 0, 1), (10, -1, 1),
    (10, 1, 0), (10, 1, -1), (10, 1, None),
])
def test_nonpositive_or_missing_fundamentals_are_ineligible_not_cheap(per, pbr, eps):
    s = snap(stock("000001", per=per, pbr=pbr, eps=eps), stock("000002"))
    result = score_factors(s, BacktestConfig(market_cap_exclusion_fraction=0))
    assert [x.ticker for x in result.scores] == ["000002"]
    assert "nonpositive_or_missing_per_pbr_eps" in result.rejected["000001"]


def test_filters_share_common_pre_fundamental_cap_population():
    names = [stock(f"{i:06d}", cap=i, liquidity=2e9) for i in range(1, 11)]
    names[1] = replace(names[1], fundamentals=Fundamental(0, 1, OLD, eps=1))
    names[2] = replace(names[2], avg_trading_value_20d=DatedValue(1, SIGNAL))
    names += [stock("000011", cap=0), stock("000012", security=SecurityType.PREFERRED),
              stock("000013", security=SecurityType.SPAC), stock("000014", security=SecurityType.REIT),
              stock("000015", financial=True), stock("000016", market=Market.KOSDAQ)]
    result = filter_universe(snap(*names), BacktestConfig())
    # 11 common classified positive-cap nonfinancial names: floor(11*.3)=3.
    assert {"000001", "000002", "000003"} <= set(result.rejected)
    assert "bottom_market_cap" in result.rejected["000001"]
    assert "low_liquidity" in result.rejected["000003"]
    assert "not_common" in result.rejected["000012"]
    assert "financial" in result.rejected["000015"]
    assert "000016" in {s.ticker for s in result.eligible}


def test_ties_and_weighted_composite_and_trading_day_momentum():
    a = stock("000001", per=5, pbr=1, closes=tuple(CloseBar(d, 100 if i != 231 else 120) for i, d in enumerate(DAYS)))
    b = stock("000002", per=10, pbr=2)
    c = stock("000003", per=20, pbr=4)
    cfg = BacktestConfig(market_cap_exclusion_fraction=0,
                         factors=FactorWeights(value=0.4, quality=0.3, momentum=0.3))
    scores = {s.ticker: s for s in score_factors(snap(a, b, c), cfg).scores}
    # Equal PBR/PER means quality is tied at midpoint; 21 trading days ago is index 231.
    assert all(s.quality == 0.5 for s in scores.values())
    assert scores["000001"].momentum_return == pytest.approx(0.2)
    assert scores["000001"].value == 1
    assert scores["000001"].composite == pytest.approx(0.85)
    assert scores["000002"].momentum == scores["000003"].momentum == 0.25


def test_disappeared_or_short_history_is_explicitly_rejected():
    vanished = replace(stock("000001"), closes=tuple(CloseBar(d, 100) for d in DAYS[:8]),
                       avg_trading_value_20d=DatedValue(2e9, DAYS[7]),
                       market_cap=DatedValue(100, DAYS[7]))
    result = score_factors(snap(vanished), BacktestConfig(market_cap_exclusion_fraction=0))
    assert result.scores == ()
    assert {"missing_signal_close", "insufficient_momentum_history", "missing_current_liquidity", "missing_current_market_cap"} <= set(result.rejected["000001"])


def test_missing_reference_trading_session_rejected_even_with_253_stock_bars():
    extended = tuple(CloseBar(day, 100) for day in (DAYS[0] - timedelta(days=3),) + DAYS[:231] + DAYS[232:])
    result = score_factors(snap(stock("000001", closes=extended)), BacktestConfig(market_cap_exclusion_fraction=0))
    assert "insufficient_momentum_history" in result.rejected["000001"]


def test_point_in_time_validation_before_scoring():
    future = replace(stock("000001"), fundamentals=Fundamental(10, 1, SIGNAL + timedelta(days=1), eps=1))
    with pytest.raises(ValueError, match="not available"):
        score_factors(snap(future), BacktestConfig())
    with pytest.raises(ValueError, match="availability date"):
        score_factors(snap(stock("000001")), BacktestConfig(data_mode=DataMode.STRICT))


def test_inverse_pbr_and_configurable_value_subweights():
    a = stock("000001", per=5, pbr=4)
    b = stock("000002", per=20, pbr=1)
    result = score_factors(snap(a, b), BacktestConfig(market_cap_exclusion_fraction=0,
        factors=FactorWeights(inverse_per=0.25, inverse_pbr=0.75)))
    by_ticker = {score.ticker: score for score in result.scores}
    assert by_ticker["000001"].value == 0.25
    assert by_ticker["000002"].value == 0.75
    assert by_ticker["000001"].approximate_roe == pytest.approx(0.8)
