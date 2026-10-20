"""Registered experiment tests use tiny in-memory inputs and temporary files."""

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from kr_quant.backtest import DailyBar, HaltPolicy, run_backtest
from kr_quant.config import BacktestConfig, Costs, FactorWeights, QualityMetric
from kr_quant.contracts import Snapshot, Targets
from scripts import compare_registered_candidates as runner
from scripts.audit_offline_range import audit_range
from scripts.run_offline_backtest import check_mechanics


def test_registered_matrix_is_isolated_filter_on_and_defaults_unchanged():
    jobs = list(runner.configurations())
    assert len(jobs) == 12
    assert {(h.value,n) for h,n,c in jobs} == {(h,n) for h in ("none","resume") for n in runner.VARIANTS}
    for h, n, cfg in jobs:
        assert not cfg.exclude_nonpositive_operating_income and cfg.below_ma_exposure == .5
        assert cfg.entry_rank == 20 and cfg.costs == Costs()
        assert replace(cfg, retention_rank=30, rebalance_frequency="monthly",
                       quality_metric=QualityMetric.PBR_PER, factors=FactorWeights()) == runner.BASE
    assert runner.VARIANTS["T-Q"].rebalance_frequency == "quarterly"
    assert runner.VARIANTS["T-40"].retention_rank == 40 and runner.VARIANTS["T-50"].retention_rank == 50
    assert runner.VARIANTS["W-532"].factors == FactorWeights(.5,.3,.2)
    assert BacktestConfig().rebalance_frequency == "monthly" and BacktestConfig().retention_rank == 30


def test_validation_window_rejected_before_any_cache_read(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "input_identity", lambda *a: pytest.fail("Read before design guard"))
    for end in ("2021-01","2024-12","2020-11"):
        args = runner.parse_args(["--end",end,"--runs-dir",str(tmp_path)])
        with pytest.raises(ValueError, match="validation is locked"):
            runner.run_comparison(args)
    with pytest.raises(ValueError):
        runner.assert_design_sessions((date(2020,12,30),date(2021,1,4)))


def test_retention_changes_do_not_expand_entry_rank(monkeypatch):
    from types import SimpleNamespace
    from tests.test_diagnose_offline_backtest import snapshot
    from kr_quant.portfolio import FactorStrategy
    from kr_quant.risk import RiskState
    import kr_quant.portfolio as portfolio
    s=snapshot()
    template=s.stocks[0]
    s=replace(s,stocks=tuple(replace(template,ticker=f"{i:06d}",sector=str(i)) for i in range(1,61)))
    scores=tuple(SimpleNamespace(ticker=f"{i:06d}") for i in range(1,61))
    monkeypatch.setattr(portfolio,"score_factors",lambda *a:SimpleNamespace(scores=scores,rejected={}))
    holdings={"000035":.05,"000045":.05,"000055":.05}
    for rank,kept in [(30,set()),(40,{"000035"}),(50,{"000035","000045"})]:
        target=FactorStrategy(replace(runner.BASE,retention_rank=rank)).targets(s,holdings,RiskState(1e6))
        assert {t for t in holdings if target.weights.get(t,0)>0}==kept
        assert all(int(t)<=20 or t in kept for t,w in target.weights.items() if w>0)


def test_quarterly_signals_next_open_and_monthly_default():
    days = (date(2016,7,29),date(2016,8,1),date(2016,8,31),date(2016,9,1),
            date(2016,9,30),date(2016,10,3),date(2016,10,31),date(2016,11,1),
            date(2016,11,30),date(2016,12,1),date(2016,12,30))
    inputs = {"days":days,"bars":{"A":{d:DailyBar(10,10) for d in days}},
              "index_closes":{n:{d:100 for d in days} for n in ("KOSPI200","KOSDAQ")},
              "snapshot_for":lambda d: Snapshot(d,(),()),
              "target_for":lambda s,h,r: Targets(s.signal_date,{"A":.5})}
    cfg = replace(runner.BASE, costs=Costs(0,0,0), rebalance_frequency="quarterly")
    quarterly = run_backtest(**inputs,config=cfg,halt_policy=HaltPolicy.NONE)
    assert quarterly.signal_dates == (date(2016,9,30),)
    assert next(e.date for e in quarterly.events if e.action=="buy") == date(2016,10,3)
    assert len(quarterly.equity)==len(days)
    assert check_mechanics(quarterly,days,1e6)["signals"] == 1
    monthly = run_backtest(**inputs,config=replace(cfg,rebalance_frequency="monthly"),halt_policy=HaltPolicy.NONE)
    assert monthly.signal_dates[0]==date(2016,7,29)
    with pytest.raises(ValueError):
        BacktestConfig(rebalance_frequency="weekly")


def test_quarterly_keeps_daily_risk_and_sell_retries_between_quarters():
    days=(date(2016,9,30),date(2016,10,3),date(2016,10,4),date(2016,12,30),
          date(2017,1,2),date(2017,1,3),date(2017,1,4))
    bars={"A":{d:DailyBar(10,10) for d in days}}
    bars["A"][days[2]]=DailyBar(10,5)  # Daily drawdown between rebalance dates.
    bars["A"][days[3]]=DailyBar(5,5)
    bars["A"][days[4]]=DailyBar(None,None,"suspended")
    bars["A"][days[5]]=DailyBar(5,5)
    cfg=replace(runner.BASE,costs=Costs(0,0,0),rebalance_frequency="quarterly")
    result=run_backtest(days=days,bars=bars,
        index_closes={n:{d:100 for d in days} for n in ("KOSPI200","KOSDAQ")},
        snapshot_for=lambda d: Snapshot(d,(),()),config=cfg,halt_policy=HaltPolicy.RESUME,
        target_for=lambda s,h,r: Targets(s.signal_date,{"A":1} if s.signal_date.month==9 else {"A":0}))
    assert result.equity[2].risk.value=="HALTED"
    assert any(e.action=="deferred_sell" and e.date==days[4] for e in result.events)
    assert any(e.action=="sell" and e.date==days[5] for e in result.events)
    assert result.signal_dates==(days[0],days[3])


def test_equal_weight_reference_keeps_market_filter_and_halt():
    from tests.test_diagnose_offline_backtest import snapshot
    from kr_quant.contracts import CloseBar
    from kr_quant.risk import RiskState, RiskLevel
    s=snapshot();bars=s.indices[0].closes
    s=replace(s,indices=(replace(s.indices[0],closes=(*bars[:-1],CloseBar(s.signal_date,1))),))
    cfg=replace(runner.BASE,market_cap_exclusion_fraction=0)
    targets=runner.equal_weight_targets(s,{},RiskState(1e6),cfg)
    positive=[w for w in targets.weights.values() if w>0]
    assert len(positive)==25 and sum(positive)==pytest.approx(.5)
    halted=runner.equal_weight_targets(s,{},RiskState(1e6,RiskLevel.HALTED),cfg)
    assert all(w==0 for w in halted.weights.values())


def test_readiness_audits_full_range_without_any_strategy(tmp_path):
    start=date(2016,7,1)-timedelta(days=400); end=date(2024,12,31)
    d=start; sessions=[]
    # One real synthetic session per month is enough for this FILE-PRESENCE audit.
    while d<=end:
        if d.weekday()<5:
            rows=[{"IDX_NM":"코스피 200","BAS_DD":f"{d:%Y%m%d}"}] if d.day<=3 else []
            p=tmp_path/"kospi_dd_trd"/f"{d:%Y%m%d}.json";p.parent.mkdir(exist_ok=True)
            p.write_text(json.dumps({"OutBlock_1":rows}),encoding="utf-8")
            if rows:
                sessions.append(d)
                for s in ("stk_bydd_trd","ksq_bydd_trd","kosdaq_dd_trd"):
                    p=tmp_path/s/f"{d:%Y%m%d}.json";p.parent.mkdir(exist_ok=True);p.write_text('{}')
        d+=timedelta(days=1)
    ends=[d for i,d in enumerate(sessions) if d>=date(2016,7,1) and
          (i==len(sessions)-1 or sessions[i+1].month!=d.month)]
    for d in ends:
        for s in ("stk_isu_base_info","ksq_isu_base_info"):
            p=tmp_path/s/f"{d:%Y%m%d}.json";p.parent.mkdir(exist_ok=True);p.write_text('{}')
    report=audit_range("2016-07","2024-12",tmp_path)
    assert report["status"]=="ready" and report["month_count"]==102
    assert report["network_requests"]==0 and "performance" not in report
    (tmp_path/"stk_bydd_trd"/f"{ends[-1]:%Y%m%d}.json").unlink()
    assert audit_range("2016-07","2024-12",tmp_path)["status"]=="incomplete"


def test_candidate_runner_checkpoint_table_before_later_failure(tmp_path,monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(runner,"input_identity",lambda *a: {"fixture":"temporary"})
    class Cache:
        start=date(2016,7,1)
        trading_days=(date(2016,7,1),date(2020,12,30))
    monkeypatch.setattr(runner,"BulkCachedData",lambda *a,**k:Cache())
    calls=[]
    def run(cache,cfg,halt_policy):
        calls.append((cfg,halt_policy))
        if len(calls)==2: raise RuntimeError("interrupted")
        assert (tmp_path/"runs/protocol.json").exists()
        metrics={"total_return":.1,"cagr":.02,"mdd":-.1,"sharpe":.3,"sortino":.5}
        return {"performance":{"strategy":metrics,"KOSPI200":metrics,"KOSDAQ":metrics,
                "annual_turnover":1,"trades":2},"equity":[{"date":"2016-07-01","equity":100,"cash":100},
                {"date":"2020-12-30","equity":110,"cash":10}],"days_halted":0}
    monkeypatch.setattr(runner,"run_cached_backtest",run)
    args=runner.parse_args(["--runs-dir",str(tmp_path/"runs"),"--output",str(tmp_path/"table.csv")])
    with pytest.raises(RuntimeError,match="interrupted"):runner.run_comparison(args)
    assert (tmp_path/"runs/none_baseline.json").exists()
    assert len((tmp_path/"table.csv").read_text(encoding="utf-8-sig").splitlines())==2
