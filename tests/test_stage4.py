from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest

from kr_quant.config import BacktestConfig, DataMode
from kr_quant.contracts import (ClassificationKind, CloseBar, DatedValue, Fundamental,
                                IndexHistory, Market, SecurityType, Snapshot, Stock, Targets,
                                TradingStatus, validate_snapshot, validate_targets)
from kr_quant.data import Classification, PykrxLoader, SampleRequest, approximate_current_checks, current_classifications, make_snapshot
from kr_quant.portfolio import FactorStrategy
from kr_quant.risk import RiskLevel, RiskState
from kr_quant.universe import filter_universe
from test_data import FakeApi

DAYS = tuple(d.date() for d in pd.bdate_range("2024-01-01", periods=253))
SIGNAL = DAYS[-1]


def stock(i, market=Market.KOSPI, sector="A", **changes):
    s = Stock(f"{i:06d}", market, sector, SecurityType.COMMON, False, DAYS[0],
              DatedValue(i * 1e9, SIGNAL), DatedValue(2e9, SIGNAL),
              Fundamental(10 + i, 1, SIGNAL - timedelta(days=120), eps=10),
              tuple(CloseBar(d, 100 + i) for d in DAYS))
    return replace(s, **changes)


def snapshot(*stocks, kosdaq_below=False):
    return Snapshot(SIGNAL, stocks, tuple(IndexHistory(m, tuple(CloseBar(day, 200 if kosdaq_below and m is Market.KOSDAQ and day != SIGNAL else 100) for day in DAYS)) for m in Market))


def config(**kwargs):
    return BacktestConfig(market_cap_exclusion_fraction=0, **kwargs)


def test_proxy_metadata_unknown_group_and_strict_rejection(tmp_path):
    sample = PykrxLoader(tmp_path, 0, FakeApi()).load_sample(
        SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",)))
    cls = Classification.current_proxy(Market.KOSPI, None, SecurityType.COMMON, False,
                                        source="current KRX classification", retrieved_on=date(2026, 10, 14))
    s = make_snapshot(sample, date(2024, 2, 23), config(), {"005930": cls})
    assert s.stocks[0].sector == "unknown"
    assert s.stocks[0].classification_kind is ClassificationKind.CURRENT_PROXY
    assert s.stocks[0].classification_retrieved_on == date(2026, 10, 14)
    with pytest.raises(ValueError, match="proxy"):
        validate_snapshot(s, BacktestConfig(data_mode=DataMode.STRICT))
    with pytest.raises(ValueError, match="proxy"):
        make_snapshot(sample, date(2024, 2, 23), BacktestConfig(data_mode=DataMode.STRICT), {"005930": cls})


@pytest.mark.parametrize("security,financial,expected", [
    (SecurityType.PREFERRED, False, "not_common"),
    (SecurityType.SPAC, False, "not_common"),
    (SecurityType.REIT, False, "not_common"),
    (SecurityType.COMMON, True, "financial"),
    (None, False, "unknown_security_type"),
    (SecurityType.COMMON, None, "unknown_financial_status"),
])
def test_proxy_type_financial_fail_closed(security, financial, expected):
    s = stock(1, sector="unknown", security_type=security, is_financial=financial,
              classification_kind=ClassificationKind.CURRENT_PROXY,
              classification_source="current checked classifications",
              classification_available_on=date(2026, 10, 14),
              classification_retrieved_on=date(2026, 10, 14))
    result = filter_universe(snapshot(s), config())
    assert expected in result.rejected[s.ticker]


def test_proxy_source_and_date_required():
    s = stock(1, classification_kind=ClassificationKind.CURRENT_PROXY)
    with pytest.raises(ValueError, match="source and retrieval"):
        validate_snapshot(snapshot(s), config())
    with pytest.raises(ValueError, match="source"):
        Classification.current_proxy(Market.KOSPI, None, SecurityType.COMMON, False,
                                     source=" ", retrieved_on=date(2026, 1, 1))


def test_pykrx_sector_adapter_needs_separate_verified_type_flags():
    frames = {Market.KOSPI: pd.DataFrame({"업종명": [None, "제조업"], "종목명": ["보통주", "우선주우"]}, index=["000001", "000002"]),
              Market.KOSDAQ: pd.DataFrame({"업종명": ["IT"], "종목명": [None]}, index=["000003"])}
    mapped = current_classifications(("000001", "000002", "000003"), frames,
                                     {"000001": (SecurityType.COMMON, False),
                                      "000002": (SecurityType.PREFERRED, False)},
                                     retrieved_on=date(2026, 10, 14))
    assert mapped["000001"].sector == "unknown"
    assert mapped["000002"].security_type is SecurityType.PREFERRED
    assert mapped["000003"].security_type is None
    assert mapped["000003"].is_financial is False
    with pytest.raises(ValueError, match="verified current market"):
        current_classifications(("000004",), frames, {}, retrieved_on=date(2026, 10, 14))


@pytest.mark.parametrize("name,sector,security,financial", [
    ("테스트우", "제조업", SecurityType.PREFERRED, False),
    ("테스트스팩1호", "금융", SecurityType.SPAC, True),
    ("테스트리츠", "서비스업", SecurityType.REIT, False),
    ("테스트은행", "은행", SecurityType.COMMON, True),
    ("테스트", "IT", SecurityType.COMMON, False),
    (None, "IT", None, False),
    ("테스트", None, SecurityType.COMMON, None),
])
def test_current_proxy_name_and_industry_checks(name, sector, security, financial):
    assert approximate_current_checks(name, sector) == (security, financial)


def test_entry_retention_sector_cap_and_next_candidates():
    names = [stock(i, sector="A" if i <= 8 else f"S{i}") for i in range(1, 34)]
    # Higher cap-independent composite: lower PER means stronger value.
    strategy = FactorStrategy(config())
    result = strategy.targets(snapshot(*names), {"000025": 0.05}, RiskState(100))
    assert result.weights["000025"] == pytest.approx(0.05)
    assert "000007" not in result.weights  # A sector already has 6 positions
    assert "000008" not in result.weights
    assert "000021" not in result.weights  # new entry outside top 20
    assert len(result.weights) <= 20
    assert sum(result.weights[t] for t in result.weights if int(t) <= 8) <= 0.3 + 1e-9


def test_top20_entry_top30_retention_boundary_and_warning_targets():
    names = [stock(i, sector=f"S{i}") for i in range(1, 33)]
    strategy = FactorStrategy(config())
    targets = strategy.targets(snapshot(*names), {"000030": 0.05, "000031": 0.05}, RiskState(100))
    assert "000030" in targets.weights
    assert targets.weights["000031"] == 0
    assert targets.exit_reasons["000031"] == ("rank_outside_top_30",)
    assert "000019" in targets.weights
    assert "000020" not in targets.weights  # retained rank 30 uses a slot
    assert "000021" not in targets.weights
    assert len([weight for weight in targets.weights.values() if weight > 0]) == 20
    assert targets.weights == strategy.targets(snapshot(*names), {"000030": 0.05, "000031": 0.05}, RiskState(100).observe(85, config())).weights


def test_unknown_group_cap_and_market_filter_exposure():
    names = [stock(i, market=Market.KOSDAQ, sector="unknown") for i in range(1, 11)]
    result = FactorStrategy(config(below_ma_exposure=0.5)).targets(snapshot(*names, kosdaq_below=True), {}, RiskState(100))
    assert len(result.weights) == 6
    assert set(result.weights.values()) == {0.025}
    # Base sector allocation is 30%; below-MA exposure makes realized target 15%.
    assert sum(result.weights.values()) == pytest.approx(0.15)


def test_warning_alert_only_halt_sells_only_and_manual_resume():
    cfg = config()
    normal = RiskState(100)
    warning = normal.observe(85, cfg)
    assert warning.level is RiskLevel.WARNING
    names = snapshot(stock(1), stock(2))
    strategy = FactorStrategy(cfg)
    assert len(strategy.targets(names, {}, warning).weights) == 2
    halted = warning.observe(80, cfg)
    assert halted.level is RiskLevel.HALTED
    assert strategy.targets(names, {}, halted).weights == {}
    assert strategy.targets(names, {"000001": 0.08}, halted).weights == {"000001": 0.05}
    assert halted.observe(110, cfg).level is RiskLevel.HALTED
    assert halted.manual_resume().observe(110, cfg).level is RiskLevel.NORMAL
    assert halted.manual_resume().observe(79, cfg).level is RiskLevel.HALTED


def test_disappeared_holding_requires_explicit_resolution():
    strategy = FactorStrategy(config())
    with pytest.raises(ValueError, match="delisting resolution"):
        strategy.targets(snapshot(stock(1)), {"091990": 0.05}, RiskState(100))
    vanished = replace(stock(2), closes=tuple(CloseBar(d, 100) for d in DAYS[:8]))
    with pytest.raises(ValueError, match="untradable"):
        strategy.targets(snapshot(stock(1), vanished), {vanished.ticker: 0.05}, RiskState(100))


def test_market_filter_is_per_market_and_no_index_fails_closed():
    strategy = FactorStrategy(config(below_ma_exposure=0))
    data = snapshot(stock(1), stock(2, Market.KOSDAQ), kosdaq_below=True)
    targets = strategy.targets(data, {}, RiskState(100))
    assert targets.weights == {"000001": 0.05, "000002": 0}
    with pytest.raises(ValueError, match="MA"):
        strategy.targets(replace(data, indices=()), {}, RiskState(100))


def test_halt_preserves_only_decreases_under_market_filter():
    strategy = FactorStrategy(config(below_ma_exposure=0.5))
    data = snapshot(stock(1, Market.KOSDAQ), kosdaq_below=True)
    target = strategy.targets(data, {"000001": 0.01}, RiskState(100, RiskLevel.HALTED))
    assert target.weights == {"000001": 0.01}  # no 1% -> 2.5% buy
    assert strategy.targets(data, {"000001": 0.08}, RiskState(100, RiskLevel.HALTED)).weights == {"000001": 0.025}


@pytest.mark.parametrize("changes,reason", [
    ({"fundamentals": Fundamental(0, 1, SIGNAL - timedelta(days=120), eps=10)}, "nonpositive_or_missing_per_pbr_eps"),
    ({"fundamentals": Fundamental(10, 0, SIGNAL - timedelta(days=120), eps=10)}, "nonpositive_or_missing_per_pbr_eps"),
    ({"fundamentals": Fundamental(10, 1, SIGNAL - timedelta(days=120), eps=0)}, "nonpositive_or_missing_per_pbr_eps"),
    ({"avg_trading_value_20d": DatedValue(1, SIGNAL)}, "low_liquidity"),
    ({"security_type": None}, "unknown_security_type"),
    ({"is_financial": None}, "unknown_financial_status"),
    ({"security_type": SecurityType.PREFERRED}, "not_common"),
    ({"is_financial": True}, "financial"),
])
def test_tradable_ineligible_holding_gets_zero_target_and_reason(changes, reason):
    rejected = replace(stock(1), **changes)
    target = FactorStrategy(config()).targets(snapshot(rejected, stock(2)), {rejected.ticker: 0.05}, RiskState(100))
    assert target.weights[rejected.ticker] == 0
    assert reason in target.exit_reasons[rejected.ticker]
    assert target.weights["000002"] == 0.05
    validate_targets(target, snapshot(rejected, stock(2)))


def test_bottom_cap_holding_sells_and_halt_allows_reduction():
    names = [stock(i, sector=f"S{i}") for i in range(1, 11)]
    cfg = BacktestConfig()
    target = FactorStrategy(cfg).targets(snapshot(*names), {"000001": 0.05}, RiskState(100, RiskLevel.HALTED))
    assert target.weights == {"000001": 0}
    assert target.exit_reasons["000001"] == ("bottom_market_cap",)


@pytest.mark.parametrize("status", [TradingStatus.SUSPENDED, TradingStatus.MERGED, TradingStatus.DELISTED])
def test_explicit_untradable_status_defers_even_with_a_current_close(status):
    untradable = replace(stock(1), trading_status=status)
    with pytest.raises(ValueError, match="untradable"):
        FactorStrategy(config()).targets(snapshot(untradable), {untradable.ticker: 0.05}, RiskState(100))
    # An unheld untradable candidate is excluded, not selected for a new buy.
    assert FactorStrategy(config()).targets(snapshot(untradable), {}, RiskState(100)).weights == {}


def test_zero_weight_exit_reasons_must_match_zero_targets():
    s = snapshot(stock(1))
    with pytest.raises(ValueError, match="exit reasons"):
        validate_targets(Targets(SIGNAL, {"000001": 0.05}, {"000001": ("low_liquidity",)}), s)
