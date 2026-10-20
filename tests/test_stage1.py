from datetime import date, timedelta

import pytest

from kr_quant.config import BacktestConfig, Costs, DataMode, FactorWeights, Period, Periods
from kr_quant.contracts import (
    CloseBar,
    DatedValue,
    Fundamental,
    IndexHistory,
    Market,
    SecurityType,
    Snapshot,
    Stock,
    Targets,
    validate_snapshot,
    validate_targets,
)


SIGNAL = date(2025, 12, 31)


def snapshot(fundamentals=None, **changes):
    stock = Stock(
        ticker="005930",
        market=Market.KOSPI,
        sector="Technology",
        security_type=SecurityType.COMMON,
        is_financial=False,
        classification_available_on=date(2024, 1, 1),
        market_cap=DatedValue(100_000_000_000, SIGNAL),
        avg_trading_value_20d=DatedValue(2_000_000_000, SIGNAL),
        fundamentals=fundamentals,
        closes=(CloseBar(date(2025, 12, 30), 99), CloseBar(SIGNAL, 100)),
    )
    from dataclasses import replace

    stock = replace(stock, **changes)
    return Snapshot(SIGNAL, (stock,), (IndexHistory(Market.KOSPI, (CloseBar(SIGNAL, 3000),)),))


def test_defaults_and_fixed_lag_boundary():
    cfg = BacktestConfig()
    assert cfg.data_mode is DataMode.APPROXIMATE
    assert cfg.fundamental_lag_days == 90
    eligible_date = SIGNAL - timedelta(days=90)
    validate_snapshot(snapshot(Fundamental(12, 1.5, eligible_date)), cfg)
    with pytest.raises(ValueError, match="fixed lag"):
        validate_snapshot(snapshot(Fundamental(12, 1.5, eligible_date + timedelta(days=1))), cfg)


def test_strict_requires_availability_and_does_not_apply_estimated_lag():
    cfg = BacktestConfig(data_mode=DataMode.STRICT)
    with pytest.raises(ValueError, match="availability date"):
        validate_snapshot(snapshot(Fundamental(12, 1.5, SIGNAL)), cfg)
    validate_snapshot(snapshot(Fundamental(12, 1.5, SIGNAL, SIGNAL)), cfg)
    with pytest.raises(ValueError, match="not available"):
        validate_snapshot(snapshot(Fundamental(12, 1.5, SIGNAL, SIGNAL + timedelta(days=1))), cfg)


def test_explicit_availability_is_respected_in_approximate_mode():
    old = SIGNAL - timedelta(days=100)
    with pytest.raises(ValueError, match="not available"):
        validate_snapshot(snapshot(Fundamental(12, 1.5, old, SIGNAL + timedelta(days=1))), BacktestConfig())


@pytest.mark.parametrize("changes", [
    {"market_cap": DatedValue(1, SIGNAL + timedelta(days=1))},
    {"classification_available_on": SIGNAL + timedelta(days=1)},
    {"closes": (CloseBar(SIGNAL + timedelta(days=1), 100),)},
])
def test_rejects_future_stock_data(changes):
    with pytest.raises(ValueError, match="not available"):
        validate_snapshot(snapshot(**changes), BacktestConfig())


def test_rejects_future_index_and_duplicate_stocks():
    s = snapshot()
    with pytest.raises(ValueError, match="not available"):
        validate_snapshot(Snapshot(SIGNAL, s.stocks, (IndexHistory(Market.KOSDAQ, (CloseBar(date(2026, 1, 2), 900),)),)), BacktestConfig())
    with pytest.raises(ValueError, match="unique"):
        validate_snapshot(Snapshot(SIGNAL, s.stocks * 2, s.indices), BacktestConfig())


def test_nonpositive_fundamentals_are_data_not_zero_scores():
    validate_snapshot(snapshot(Fundamental(0, -1, date(2025, 1, 1))), BacktestConfig())


def test_configuration_rejects_inconsistent_values():
    with pytest.raises(ValueError):
        FactorWeights(value=0.5)
    with pytest.raises(ValueError):
        Costs(commission_bps=-1)
    with pytest.raises(ValueError):
        BacktestConfig(below_ma_exposure=1.1)
    with pytest.raises(ValueError):
        BacktestConfig(warning_drawdown=0.25)
    with pytest.raises(ValueError):
        BacktestConfig(data_mode="strict")
    with pytest.raises(ValueError):
        Periods(Period(date(2020, 1, 1), date(2021, 1, 1)), Period(date(2021, 1, 1), date(2022, 1, 1)), Period(date(2023, 1, 1), date(2024, 1, 1)))


def test_targets_are_only_weights_for_known_stocks():
    s = snapshot()
    validate_targets(Targets(SIGNAL, {"005930": 0.05}), s)
    with pytest.raises(ValueError):
        validate_targets(Targets(SIGNAL, {"UNKNOWN": 0.05}), s)
    with pytest.raises(ValueError):
        validate_targets(Targets(SIGNAL, {"005930": 1.1}), s)
