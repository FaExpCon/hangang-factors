"""Accounting alternatives remain opt-in; diagnostics never change targets."""

from dataclasses import replace
from datetime import date, timedelta
from math import nan

import pandas as pd
import pytest


def test_income_quality_uses_same_report_assets_during_transition_fallback():
    from datetime import date
    from kr_quant.contracts import Fundamental
    fund=Fundamental(10,1,date(2020,6,30),eps=1,operating_income=20,total_assets=9999,
                     income_period_end=date(2019,12,31),income_period_assets=100)
    assert fund.quality_assets==100 and fund.operating_income/fund.quality_assets==.2
    from dataclasses import replace
    assert replace(fund,income_period_assets=None).quality_assets is None  # No cross-period backfill.

from kr_quant.config import BacktestConfig, FactorWeights, QualityMetric
from kr_quant.contracts import (CloseBar, DatedValue, Fundamental, IndexHistory,
                                Market, SecurityType, Snapshot, Stock)
from kr_quant.factors import accounting_flags, score_factors
from kr_quant.portfolio import FactorStrategy
from kr_quant.risk import RiskState
from kr_quant.universe import filter_universe
from scripts.run_offline_backtest import check_mechanics, config_for, parse_args


DAYS = tuple(d.date() for d in pd.bdate_range(end="2020-11-30", periods=253))
SIGNAL = DAYS[-1]
CLOSES = tuple(CloseBar(d, 100) for d in DAYS)


def stock(ticker, *, per=10, pbr=1, operating=10, assets=100, net=20, cap=100):
    return Stock(ticker, Market.KOSPI, "26", SecurityType.COMMON, False, DAYS[0],
                 DatedValue(cap, SIGNAL), DatedValue(2e9, SIGNAL),
                 Fundamental(per, pbr, date(2019, 12, 31), date(2020, 3, 30), 1,
                             operating_income=operating, total_assets=assets, net_income=net), CLOSES)


def snapshot(*stocks):
    return Snapshot(SIGNAL, stocks, (IndexHistory(Market.KOSPI, CLOSES),))


def config(**kwargs):
    return BacktestConfig(fundamental_lag_days=0, market_cap_exclusion_fraction=0, **kwargs)


def test_defaults_preserve_scores_and_targets_without_optional_accounts():
    stocks = (stock("000001", per=5, pbr=1, operating=-10, net=100),
              stock("000002", per=10, pbr=2, operating=None, assets=None, net=None),
              stock("000003", per=20, pbr=4, operating=1, net=100))
    data = snapshot(*stocks)
    legacy = replace(data, stocks=tuple(replace(s, fundamentals=replace(
        s.fundamentals, operating_income=None, total_assets=None, net_income=None)) for s in stocks))
    assert score_factors(data, config()).scores == score_factors(legacy, config()).scores
    strategy = FactorStrategy(config())
    assert strategy.targets(data, {}, RiskState(1e6)) == strategy.targets(legacy, {}, RiskState(1e6))
    assert BacktestConfig().quality_metric is QualityMetric.PBR_PER
    assert BacktestConfig().exclude_nonpositive_operating_income is False
    scored = score_factors(data, config())
    assert set(scored.accounting_flags) == {"000001", "000003"}
    changed_threshold = config(net_income_operating_income_flag_ratio=200)
    assert score_factors(data, changed_threshold).scores == scored.scores
    assert FactorStrategy(changed_threshold).targets(data, {}, RiskState(1e6)) == strategy.targets(data, {}, RiskState(1e6))


@pytest.mark.parametrize("operating,reason", [(0, "nonpositive_operating_income"),
    (-1, "nonpositive_operating_income"), (None, "missing_operating_income")])
def test_optional_positive_operating_income_filter(operating, reason):
    data = snapshot(stock("000001", operating=operating), stock("000002", operating=1))
    assert len(filter_universe(data, config()).eligible) == 2
    rejected = filter_universe(data, config(exclude_nonpositive_operating_income=True))
    assert [s.ticker for s in rejected.eligible] == ["000002"]
    assert reason in rejected.rejected["000001"]


def test_operating_filter_does_not_change_bottom_cap_population():
    data = snapshot(*(stock(f"{i:06d}", cap=i, operating=-1 if i <= 3 else 1) for i in range(1, 11)))
    cfg = replace(config(exclude_nonpositive_operating_income=True), market_cap_exclusion_fraction=.3)
    rejected = filter_universe(data, cfg).rejected
    assert {ticker for ticker, reasons in rejected.items() if "bottom_market_cap" in reasons} == {"000001", "000002", "000003"}


def test_alternative_quality_ranks_operating_income_over_assets_instead_of_roe():
    data = snapshot(stock("000001", pbr=4, operating=1, assets=100),
                    stock("000002", pbr=1, operating=20, assets=100),
                    stock("000003", pbr=2, operating=-10, assets=100))
    default = {s.ticker: s for s in score_factors(data, config()).scores}
    assert default["000001"].quality == 1
    cfg = config(quality_metric=QualityMetric.OPERATING_INCOME_TO_ASSETS,
                 factors=FactorWeights(value=0, quality=1, momentum=0))
    scores = score_factors(data, cfg).scores
    assert [s.ticker for s in scores] == ["000002", "000001", "000003"]
    assert [s.quality for s in scores] == [1, .5, 0]
    assert [s.quality_raw for s in scores] == pytest.approx([.2, .01, -.1])
    assert scores[0].approximate_roe == pytest.approx(.1)  # Original ratio remains separately named.
    assert scores[0].quality_metric is QualityMetric.OPERATING_INCOME_TO_ASSETS
    assert all(s.value == default[s.ticker].value and s.momentum == default[s.ticker].momentum for s in scores)


@pytest.mark.parametrize("operating,assets,reason", [(None, 100, "missing_operating_income"),
    (10, None, "missing_or_nonpositive_total_assets"), (10, 0, "missing_or_nonpositive_total_assets"),
    (10, -1, "missing_or_nonpositive_total_assets")])
def test_alternative_quality_missing_data_fails_closed(operating, assets, reason):
    scored = score_factors(snapshot(stock("000001", operating=operating, assets=assets)),
                          config(quality_metric=QualityMetric.OPERATING_INCOME_TO_ASSETS))
    assert not scored.scores and reason in scored.rejected["000001"]


@pytest.mark.parametrize("net,operating,expected", [(30, 10, ()), (30.01, 10, ("net_income_above_operating_income_multiple",)),
    (1, 0, ("positive_net_income_with_nonpositive_operating_income",)),
    (1, -10, ("positive_net_income_with_nonpositive_operating_income",)),
    (-1, 10, ()), (0, -1, ()), (None, 1, ()), (1, None, ())])
def test_one_off_gain_flags_handle_threshold_and_nonpositive_denominators(net, operating, expected):
    assert accounting_flags(stock("000001", net=net, operating=operating).fundamentals) == expected


def test_flags_survive_eligibility_rejection():
    result = score_factors(snapshot(stock("000001", operating=-1, net=20)),
                           config(exclude_nonpositive_operating_income=True))
    assert not result.scores
    assert result.accounting_flags["000001"] == ("positive_net_income_with_nonpositive_operating_income",)


@pytest.mark.parametrize("changes", [{"quality_metric": "bad"}, {"exclude_nonpositive_operating_income": 1},
    {"net_income_operating_income_flag_ratio": 1}, {"net_income_operating_income_flag_ratio": nan}])
def test_comparison_configuration_validation(changes):
    with pytest.raises(ValueError):
        BacktestConfig(**changes)


def test_optional_accounts_must_be_finite():
    for field in ("operating_income", "total_assets", "net_income"):
        s = stock("000001")
        with pytest.raises(ValueError, match="finite when supplied"):
            score_factors(snapshot(replace(s, fundamentals=replace(s.fundamentals, **{field: nan}))), config())


def test_backtest_cli_defaults_and_comparison_switches():
    cfg = config_for(parse_args([]))
    assert cfg == BacktestConfig(fundamental_lag_days=0)
    args = parse_args(["--exclude-nonpositive-operating-income", "--quality-metric", "operating_income_to_assets",
                       "--net-income-flag-ratio", "5"])
    cfg = config_for(args)
    assert cfg.exclude_nonpositive_operating_income
    assert cfg.quality_metric is QualityMetric.OPERATING_INCOME_TO_ASSETS
    assert cfg.net_income_operating_income_flag_ratio == 5


def test_mechanics_checker_rejects_bad_cash_and_off_schedule_fills():
    from kr_quant.backtest import BacktestResult, EquityPoint, Event
    from kr_quant.risk import RiskLevel
    days = (date(2020, 3, 31), date(2020, 4, 1), date(2020, 4, 2))
    result = BacktestResult(tuple(EquityPoint(d, 100, 100, RiskLevel.NORMAL) for d in days), (), {}, (days[0],), config())
    assert check_mechanics(result, days, 100)["status"] == "passed"
    with pytest.raises(AssertionError, match="cash accounting"):
        check_mechanics(replace(result, equity=(EquityPoint(days[0], 100, -1, RiskLevel.NORMAL), *result.equity[1:])), days, 100)
    with pytest.raises(AssertionError, match="next-session"):
        check_mechanics(replace(result, events=(Event(days[2], "000001", "buy", "bad", 1, 10, -10),)), days, 100)
