from datetime import date

import pytest

from kr_quant.backtest import DailyBar, run_backtest
from kr_quant.config import BacktestConfig, Costs, Period, Periods
from kr_quant.contracts import Snapshot, Targets, Stock, SecurityType, Market, DatedValue, Fundamental, CloseBar, IndexHistory
from kr_quant.report import metrics, report
from kr_quant.universe import filter_universe


def test_defaults_and_holding_company_option():
    cfg = BacktestConfig()
    assert cfg.costs.commission_bps > 0
    assert cfg.costs.transaction_tax_bps > 0
    assert cfg.costs.slippage_bps > 0
    assert cfg.exclude_holding_companies is False
    with pytest.raises(ValueError):
        BacktestConfig(untradable_conversion_days=0)


def test_metrics_and_slices():
    a, b, c = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    result = metrics(((a, 100), (b, 80), (c, 120)))
    assert result["total_return"] == pytest.approx(.2)
    assert result["mdd"] == pytest.approx(-.2)
    assert result["sharpe"] is not None and result["sortino"] is not None


def test_month_end_signal_executes_next_open_and_costs(monkeypatch):
    import kr_quant.backtest as engine
    a, b, c = date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2)
    class Strategy:
        def __init__(self, cfg): pass
        def targets(self, snapshot, holdings, risk): return Targets(snapshot.signal_date, {"A": .5})
    monkeypatch.setattr(engine, "FactorStrategy", Strategy)
    cfg = BacktestConfig(costs=Costs(commission_bps=100, transaction_tax_bps=200, slippage_bps=100))
    data = {"A": {a: DailyBar(1, 10), b: DailyBar(20, 30), c: DailyBar(31, 31)}}
    idx = {"KOSPI200": {a: 100, b: 100, c: 100}, "KOSDAQ": {a: 100, b: 100, c: 100}}
    result = run_backtest(days=(a, b, c), bars=data, index_closes=idx,
                          snapshot_for=lambda d: Snapshot(d, (), ()), config=cfg, initial_cash=1000)
    buys = [e for e in result.events if e.action == "buy"]
    assert len(buys) == 1 and buys[0].date == b and buys[0].price == pytest.approx(20.2)
    assert buys[0].cash_change < -500  # buy-side slippage and commission
    assert result.equity[0].equity == 1000
    assert result.equity[1].equity > 1000  # marked at 30 close, not 20 open


def test_untradable_freeze_then_approximate_conversion(monkeypatch):
    import kr_quant.backtest as engine
    a, b, c, d, e = [date(2024, 1, x) for x in (31,)] + [date(2024, 2, x) for x in (1, 2, 5, 6)]
    class Strategy:
        def __init__(self, cfg): pass
        def targets(self, snapshot, holdings, risk): return Targets(snapshot.signal_date, {"A": .5})
    monkeypatch.setattr(engine, "FactorStrategy", Strategy)
    cfg = BacktestConfig(untradable_conversion_days=2, untradable_recovery_fraction=.25,
                         costs=Costs(0, 0, 0))
    idx = {name: {day: 100 for day in (a,b,c,d,e)} for name in ("KOSPI200", "KOSDAQ")}
    bars = {"A": {b: DailyBar(10, 10), c: DailyBar(None, None, "suspended"),
                  d: DailyBar(None, None, "suspended")}}
    result = run_backtest(days=(a,b,c,d,e), bars=bars, index_closes=idx,
                          snapshot_for=lambda day: Snapshot(day, (), ()), config=cfg, initial_cash=1000)
    assert result.equity[1].equity == 1000
    assert result.equity[2].equity == 1000  # last close frozen
    conversion = [x for x in result.events if x.action == "approximate_cash_conversion"]
    assert len(conversion) == 1 and conversion[0].date == e
    assert conversion[0].cash_change == pytest.approx(125)
    assert result.equity[-1].equity == pytest.approx(625)


def test_report_benchmark_periods_and_trade_log(monkeypatch):
    import kr_quant.backtest as engine
    days = (date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2))
    class Strategy:
        def __init__(self, cfg): pass
        def targets(self, snapshot, holdings, risk): return Targets(snapshot.signal_date, {"A": .5})
    monkeypatch.setattr(engine, "FactorStrategy", Strategy)
    cfg = BacktestConfig(periods=Periods(Period(days[0],days[0]), Period(days[1],days[1]), Period(days[2],days[2])))
    result = run_backtest(days=days, bars={"A": {days[1]: DailyBar(10,10), days[2]: DailyBar(10,10)}},
                          index_closes={name: dict(zip(days,[100,110,120])) for name in ("KOSPI200", "KOSDAQ")},
                          snapshot_for=lambda day: Snapshot(day, (), ()), config=cfg)
    summary = report(result)
    assert summary["label"] == "APPROXIMATE"
    assert summary["full"]["KOSPI200"]["total_return"] == pytest.approx(.2)
    assert summary["periods"]["final_test"]["strategy"]["sessions"] == 1
    assert summary["periods"]["design"]["strategy"]["start"] == days[0].isoformat()
    assert summary["periods"]["validation"]["KOSDAQ"]["start"] == days[1].isoformat()
    assert any(item["action"] == "buy" for item in summary["trade_log"])


def test_holding_company_switch_only_excludes_identified_names():
    from dataclasses import replace
    import pandas as pd
    days = tuple(x.date() for x in pd.bdate_range("2023-01-01", periods=253))
    def stock(ticker, holding):
        return Stock(ticker, Market.KOSPI, "A", SecurityType.COMMON, False, days[0],
                     DatedValue(1e11,days[-1]),DatedValue(2e9,days[-1]),
                     Fundamental(10,1,days[0],eps=10),tuple(CloseBar(d,100) for d in days),
                     is_holding_company=holding)
    snapshot = Snapshot(days[-1], (stock("A",True),stock("B",None)),
                        (IndexHistory(Market.KOSPI,tuple(CloseBar(d,100) for d in days)),))
    cfg = BacktestConfig(market_cap_exclusion_fraction=0,exclude_holding_companies=True)
    eligible = filter_universe(snapshot,cfg)
    assert eligible.rejected["A"] == ("holding_company",)
    assert [s.ticker for s in eligible.eligible] == ["B"]


def test_missing_open_does_not_fill_or_retry_later(monkeypatch):
    import kr_quant.backtest as engine
    days = (date(2024,1,31),date(2024,2,1),date(2024,2,2))
    class Strategy:
        def __init__(self,cfg): pass
        def targets(self,snapshot,holdings,risk): return Targets(snapshot.signal_date,{"A":.5})
    monkeypatch.setattr(engine,"FactorStrategy",Strategy)
    result = run_backtest(days=days,bars={"A":{days[1]:DailyBar(None,10),days[2]:DailyBar(10,10)}},
        index_closes={name:{d:100 for d in days} for name in ("KOSPI200","KOSDAQ")},
        snapshot_for=lambda d: Snapshot(d,(),()),config=BacktestConfig())
    assert any(e.action=="unfilled_buy" and e.date==days[1] for e in result.events)
    assert not any(e.action=="buy" for e in result.events)


@pytest.mark.parametrize("conversion_days", [1,20])
def test_frozen_holding_sells_at_returning_open_without_waiting_for_close(monkeypatch, conversion_days):
    import kr_quant.backtest as engine
    days=(date(2024,1,31),date(2024,2,1),date(2024,2,29),date(2024,3,1))
    class Strategy:
        def __init__(self,cfg): pass
        def targets(self,snapshot,holdings,risk): return Targets(snapshot.signal_date,{"A":.5} if snapshot.signal_date.month==1 else {})
    monkeypatch.setattr(engine,"FactorStrategy",Strategy)
    bars={"A":{days[1]:DailyBar(10,10),days[2]:DailyBar(None,None,"suspended"),days[3]:DailyBar(10,10)}}
    result=run_backtest(days=days,bars=bars,
        index_closes={name:{day:100 for day in days} for name in ("KOSPI200","KOSDAQ")},
        snapshot_for=lambda d: Snapshot(d,(),()),config=BacktestConfig(untradable_conversion_days=conversion_days))
    assert any(e.action=="sell" and e.date==days[3] for e in result.events)
    assert not any(e.action=="deferred_sell" for e in result.events)


def test_sell_commission_tax_and_slippage_are_sell_side_only(monkeypatch):
    import kr_quant.backtest as engine
    days=(date(2024,1,31),date(2024,2,1),date(2024,2,29),date(2024,3,1))
    class Strategy:
        def __init__(self,cfg): pass
        def targets(self,snapshot,holdings,risk):
            return Targets(snapshot.signal_date,{"A":.5} if snapshot.signal_date.month==1 else {"A":0},
                           {} if snapshot.signal_date.month==1 else {"A":("low_liquidity",)})
    monkeypatch.setattr(engine,"FactorStrategy",Strategy)
    cfg=BacktestConfig(costs=Costs(100,200,100))
    result=run_backtest(days=days,bars={"A":{d:DailyBar(10,10) for d in days}},
                        index_closes={name:{d:100 for d in days} for name in ("KOSPI200","KOSDAQ")},
                        snapshot_for=lambda d: Snapshot(d,(),()),config=cfg,initial_cash=1000)
    buy=next(e for e in result.events if e.action=="buy")
    sell=next(e for e in result.events if e.action=="sell")
    assert buy.date==days[1] and sell.date==days[3]
    assert sell.reason=="low_liquidity"
    assert sell.cash_change==pytest.approx(sell.shares*9.9*.97)
    assert buy.cash_change==pytest.approx(-buy.shares*10.1*1.01)


@pytest.mark.parametrize("conversion_days", [2, 20])
def test_deferred_sale_retries_first_tradable_open_between_rebalances(monkeypatch, conversion_days):
    import kr_quant.backtest as engine
    from scripts.run_offline_backtest import check_mechanics
    days = tuple(date(2024, m, d) for m, d in ((1,31),(2,1),(2,29),(3,1),(3,4),(3,5)))
    class Strategy:
        def __init__(self, cfg): pass
        def targets(self, snapshot, holdings, risk):
            return Targets(snapshot.signal_date, {"A": .5} if snapshot.signal_date.month == 1 else {})
    monkeypatch.setattr(engine, "FactorStrategy", Strategy)
    bars = {"A": {days[1]: engine.DailyBar(10,10),
                  days[2]: engine.DailyBar(None,None,"suspended"),
                  days[3]: engine.DailyBar(None,None,"suspended"),
                  # No current close: the sale must use only this open.
                  days[4]: engine.DailyBar(12,None), days[5]: engine.DailyBar(100,100)}}
    result = run_backtest(days=days, bars=bars,
        index_closes={name: {d:100 for d in days} for name in ("KOSPI200","KOSDAQ")},
        snapshot_for=lambda d: Snapshot(d,(),()), initial_cash=1000,
        config=BacktestConfig(costs=Costs(0,0,0), untradable_conversion_days=conversion_days))
    sales = [e for e in result.events if e.action == "sell"]
    assert len(sales) == 1 and sales[0].date == days[4]
    assert sales[0].shares == 50 and sales[0].cash_change == 600
    assert sales[0].reason.startswith("deferred_sell:")
    assert result.equity[-1].cash == result.equity[-1].equity == 1100
    assert not any(e.action == "approximate_cash_conversion" for e in result.events)
    assert check_mechanics(result, days, 1000)["status"] == "passed"


@pytest.mark.parametrize("blocked", [DailyBar(None,None), DailyBar(0,None),
    DailyBar(float("nan"),None), DailyBar(12,12,"suspended")])
def test_deferred_sale_waits_through_invalid_opens_and_clears_after_conversion(monkeypatch, blocked):
    import kr_quant.backtest as engine
    days = tuple(date(2024,m,d) for m,d in ((1,31),(2,1),(2,29),(3,1),(3,4),(3,5),(3,6)))
    class Strategy:
        def __init__(self,cfg): pass
        def targets(self,snapshot,holdings,risk):
            return Targets(snapshot.signal_date,{"A":.5} if snapshot.signal_date.month==1 else {})
    monkeypatch.setattr(engine,"FactorStrategy",Strategy)
    for conversion_days in (2,20):
        result = run_backtest(days=days, bars={"A":{days[1]:DailyBar(10,10),
            days[2]:DailyBar(None,None,"suspended"), days[3]:blocked,
            days[4]:DailyBar(None,None,"suspended"), days[5]:DailyBar(12,12), days[6]:DailyBar(12,12)}},
            index_closes={name:{d:100 for d in days} for name in ("KOSPI200","KOSDAQ")},
            snapshot_for=lambda d:Snapshot(d,(),()),initial_cash=1000,
            config=BacktestConfig(untradable_conversion_days=conversion_days,costs=Costs(0,0,0)))
        sales = [e for e in result.events if e.action == "sell"]
        if conversion_days == 20:
            assert len(sales)==1 and sales[0].date==days[5]
        else:
            assert not sales
            assert [e.date for e in result.events if e.action=="approximate_cash_conversion"] == [days[4]]


def policy_run(monkeypatch, policy, *, end="2023-06-06", halt_day=date(2023,2,1),
               index_changes=None, after_resume=None, initial_weight=1.0, exit_while_halted=False):
    """Full synthetic daily engine with warm-up and risk-aware target construction."""
    import pandas as pd
    import kr_quant.backtest as engine
    from kr_quant.risk import RiskLevel
    days = tuple(d.date() for d in pd.bdate_range("2023-01-31", end))
    warm = tuple(d.date() for d in pd.bdate_range(end=days[0], periods=240))
    calendar = tuple(sorted(set(warm + days)))
    indices = {name: {d: level for d in calendar} for name, level in (("KOSPI200",100),("KOSDAQ",200))}
    for name, updates in (index_changes or {}).items():
        indices[name].update(updates)
    observed = {}
    class Strategy:
        def __init__(self, cfg): pass
        def targets(self, snapshot, holdings, risk):
            observed[snapshot.signal_date] = risk.level
            # Retain existing positions, block entries and increases under HALTED.
            weights = ({} if exit_while_halted else dict(holdings)) if risk.level is RiskLevel.HALTED else {
                "A": initial_weight if snapshot.signal_date == days[0] else 1.0}
            return Targets(snapshot.signal_date, weights)
    monkeypatch.setattr(engine,"FactorStrategy",Strategy)
    bars = {"A": {d: DailyBar(100 if d <= halt_day else 70, 100 if d < halt_day else 70) for d in days}}
    bars["A"].update(after_resume or {})
    result = run_backtest(days=days, bars=bars, index_closes=indices,
        snapshot_for=lambda d: Snapshot(d,(),()), config=BacktestConfig(costs=Costs(0,0,0)),
        initial_cash=100, halt_policy=policy)
    return result, observed


def test_none_logs_warning_halt_but_does_not_restrict_targets_or_buys(monkeypatch):
    from kr_quant.backtest import HaltPolicy
    from kr_quant.risk import RiskLevel
    # March's lower open creates an increase, which the real executor must allow.
    for policy in (HaltPolicy.NONE, HaltPolicy.PERMANENT):
        result, observed = policy_run(monkeypatch, policy, initial_weight=.8,
            after_resume={date(2023,2,1): DailyBar(100,80), date(2023,3,1): DailyBar(60,70)})
        states = [e.reason for e in result.events if e.action == "risk_state"]
        assert "WARNING" in states and "HALTED" in states
        assert result.halt_policy is policy
        if policy is HaltPolicy.NONE:
            assert all(level is RiskLevel.NORMAL for level in observed.values())
            assert any(e.action == "buy" and e.date == date(2023,3,1) for e in result.events)
            assert not any(e.action == "blocked_buy" for e in result.events)
        else:
            assert observed[date(2023,2,28)] is RiskLevel.HALTED
            assert not any(e.action == "buy" and e.date > date(2023,2,1) for e in result.events)
            assert not any(e.action == "auto_resume" for e in result.events)


def test_resume_uses_prior_close_resets_reference_and_rehalts_with_lifetime_mdd(monkeypatch):
    from kr_quant.backtest import HaltPolicy
    from kr_quant.risk import RiskLevel
    result, observed = policy_run(monkeypatch, HaltPolicy.RESUME,
        # Today's index plunge cannot veto yesterday's scheduled resumption.
        index_changes={"KOSDAQ": {date(2023,6,1): 1}},
        after_resume={date(2023,6,2): DailyBar(70,59), date(2023,6,5): DailyBar(59,55),
                      date(2023,6,6): DailyBar(55,55)})
    resumes = [e for e in result.events if e.action == "auto_resume"]
    assert [e.date for e in resumes] == [date(2023,6,1)]
    assert observed[date(2023,4,28)] is RiskLevel.HALTED  # only 62 full sessions
    assert observed[date(2023,5,31)] is RiskLevel.NORMAL  # targets for the next open
    points = {p.date:p for p in result.equity}
    assert points[date(2023,5,31)].risk is RiskLevel.HALTED
    assert points[date(2023,6,1)].reference_peak == 70
    assert points[date(2023,6,1)].lifetime_peak == 100
    assert points[date(2023,6,2)].risk is RiskLevel.WARNING
    assert points[date(2023,6,5)].risk is RiskLevel.HALTED
    resets = [e for e in result.events if e.action == "risk_reference_reset"]
    assert len(resets) == 1 and "prior_close_equity=70" in resets[0].reason
    assert report(result)["full"]["strategy"]["mdd"] == pytest.approx(-.45)


def test_resume_can_reenter_from_cash_at_next_open_using_prior_close_only(monkeypatch):
    from kr_quant.backtest import HaltPolicy
    result, _ = policy_run(monkeypatch, HaltPolicy.RESUME, end="2023-06-02", exit_while_halted=True,
        index_changes={"KOSPI200": {date(2023,6,1): 1}},
        after_resume={date(2023,6,1): DailyBar(140,140)})
    points = {p.date:p for p in result.equity}
    assert points[date(2023,5,31)].cash == points[date(2023,5,31)].equity == 70
    buy = next(e for e in result.events if e.action == "buy" and e.date == date(2023,6,1))
    assert buy.price == 140 and buy.shares == .5
    assert points[buy.date].reference_peak == 70 and points[buy.date].lifetime_peak == 100
    assert points[buy.date].cash == 0


def test_each_new_halt_starts_a_fresh_wait_before_another_resume(monkeypatch):
    import pandas as pd
    from kr_quant.backtest import HaltPolicy
    changes = {d.date(): DailyBar(55,55) for d in pd.bdate_range("2023-06-05","2023-10-03")}
    result, _ = policy_run(monkeypatch,HaltPolicy.RESUME,end="2023-10-03",after_resume=changes)
    assert [e.date for e in result.events if e.action == "auto_resume"] == [date(2023,6,1),date(2023,9,1)]
    assert [e.date for e in result.events if e.action == "risk_reference_reset"] == [date(2023,6,1),date(2023,9,1)]


@pytest.mark.parametrize("elapsed,expected", [(62, date(2023,7,3)), (63, date(2023,6,1))])
def test_resume_requires_63_full_sessions_and_a_later_month_end(monkeypatch, elapsed, expected):
    import pandas as pd
    from kr_quant.backtest import HaltPolicy
    calendar = [d.date() for d in pd.bdate_range("2023-01-31", "2023-07-04")]
    halt = calendar[calendar.index(date(2023,5,31)) - elapsed]
    result, _ = policy_run(monkeypatch, HaltPolicy.RESUME, end="2023-07-04", halt_day=halt)
    assert next(e.date for e in result.events if e.action == "auto_resume") == expected


@pytest.mark.parametrize("name", ["KOSPI200", "KOSDAQ"])
@pytest.mark.parametrize("passing_sessions,expected", [(19, date(2023,7,3)), (20, date(2023,6,1))])
def test_both_indices_need_20_consecutive_sessions_above_own_ma(monkeypatch, name, passing_sessions, expected):
    import pandas as pd
    from kr_quant.backtest import HaltPolicy
    calendar = [d.date() for d in pd.bdate_range("2023-01-31", "2023-05-31")]
    bad_day = calendar[-1 - passing_sessions]
    result, _ = policy_run(monkeypatch, HaltPolicy.RESUME, end="2023-07-04",
                          index_changes={name: {bad_day: 1}})
    assert next(e.date for e in result.events if e.action == "auto_resume") == expected


def test_recovery_requires_complete_200_session_windows_and_includes_current_close():
    from datetime import timedelta
    from kr_quant.backtest import recovery_closes
    days = tuple(date(2020,1,1) + timedelta(days=i) for i in range(220))
    indices = {name: {d:100 for d in days} for name in ("KOSPI200","KOSDAQ")}
    assert recovery_closes(days, indices) == set(days[218:])  # equality passes; 200 + 19
    del indices["KOSDAQ"][days[20]]
    assert not recovery_closes(days, indices)
    indices["KOSDAQ"][days[20]] = 100
    indices["KOSDAQ"][days[218]] = 1
    assert not recovery_closes(days, indices)


def test_invalid_halt_policy_rejected(monkeypatch):
    with pytest.raises(ValueError):
        policy_run(monkeypatch, "automatic-live")


def test_turnover_uses_fill_notional_not_fee_and_tax_cash_movements():
    from kr_quant.backtest import BacktestResult, EquityPoint, Event
    from kr_quant.risk import RiskLevel
    days = (date(2024,1,1),date(2025,1,1))
    result = BacktestResult(tuple(EquityPoint(d,100,100,RiskLevel.NORMAL) for d in days),
        (Event(days[0],"A","buy","test",5,10,-55), Event(days[1],"A","sell","test",5,12,54),
         Event(days[1],"B","approximate_cash_conversion","test",100,1,100)),
        {name:tuple((d,100) for d in days) for name in ("KOSPI200","KOSDAQ")}, (), BacktestConfig())
    summary = report(result)["full"]
    assert summary["annual_turnover"] == pytest.approx(110 / 200 * 365.25 / 366)
    assert summary["trades"] == 2
