"""Offline diagnostic experiments use real scores and the common execution engine."""

from dataclasses import asdict, replace
from datetime import date
import json
from math import prod
import socket

import pandas as pd
import pytest

from kr_quant.backtest import DailyBar, HaltPolicy, run_backtest
from kr_quant.config import BacktestConfig, Costs
from kr_quant.contracts import (CloseBar, DatedValue, Fundamental, IndexHistory,
                                Market, SecurityType, Snapshot, Stock)
from kr_quant.portfolio import FactorStrategy
from kr_quant.report import report
from kr_quant.risk import RiskState
from scripts import diagnose_offline_backtest as diagnostic


def snapshot(signal=date(2020,1,31)):
    days = tuple(d.date() for d in pd.bdate_range(end=signal, periods=253))
    stocks = []
    for i in range(1,26):
        # Same sector intentionally: diagnostics must not impose the six-slot cap.
        closes = tuple(CloseBar(d, 100 if j < 231 else 100 + (i % 5) * 10)
                       for j,d in enumerate(days))
        stocks.append(Stock(f"{i:06d}",Market.KOSPI,"26",SecurityType.COMMON,False,days[0],
            DatedValue(i*1e10,signal),DatedValue(2e9,signal),
            Fundamental(i,i*i,days[0],eps=1),closes))
    stocks.append(replace(stocks[0],ticker="BAD",market_cap=DatedValue(1e20,signal),
                          fundamentals=Fundamental(0,0,days[0],eps=0)))
    return Snapshot(signal,tuple(stocks),(IndexHistory(Market.KOSPI,tuple(CloseBar(d,100) for d in days)),))


def test_yearly_returns_include_year_boundary_and_partial_periods():
    series = ((date(2016,7,1),100),(date(2016,12,30),110),
              (date(2017,1,2),121),(date(2017,12,29),132),(date(2018,1,2),99))
    rows = diagnostic.yearly_returns(series)
    assert [r["total_return"] for r in rows] == pytest.approx([.1,.2,-.25])
    assert rows[1]["base_date"] == "2016-12-30"
    assert prod(1+r["total_return"] for r in rows) == pytest.approx(.99)
    assert diagnostic.yearly_returns(((date(2020,11,30),100),))[0]["total_return"] == 0
    with pytest.raises(ValueError):
        diagnostic.yearly_returns(series[::-1])


def test_universe_equal_weights_all_eligible_without_sector_cap_and_with_exit():
    data = snapshot()
    cfg = replace(diagnostic.BASE,market_cap_exclusion_fraction=0)
    targets = diagnostic.diagnostic_targets(data,{"BAD":.1},cfg,"eligible_equal_weight")
    assert len([w for w in targets.weights.values() if w>0]) == 25
    assert all(w==pytest.approx(1/25) for t,w in targets.weights.items() if t!="BAD")
    assert targets.weights["BAD"] == 0 and targets.exit_reasons["BAD"]
    # Actual default eligibility still removes bottom 30% before benchmarking.
    default = diagnostic.diagnostic_targets(data,{},diagnostic.BASE,"eligible_equal_weight")
    assert set(default.weights) == {f"{i:06d}" for i in range(8,26)}
    assert len(FactorStrategy(cfg).targets(data,{},RiskState(1e6)).weights) == 6
    assert list(default.weights) == sorted(default.weights)


def test_empty_eligible_universe_produces_cash_target_without_division_by_zero():
    data = snapshot()
    rejected = replace(data,stocks=(data.stocks[-1],))
    for mode in ("eligible_equal_weight","value","quality","momentum"):
        targets = diagnostic.diagnostic_targets(rejected,{"BAD":.5},diagnostic.BASE,mode)
        assert targets.weights == {"BAD":0}


@pytest.mark.parametrize("mode,expected", [
    ("value",set(range(1,21))), ("quality",set(range(6,26))),
    ("momentum",{i for i in range(1,26) if i%5!=0})])
def test_factors_are_strict_top_twenty_no_retention_sector_or_market_filter(mode,expected):
    data = snapshot()
    cfg = replace(diagnostic.BASE,market_cap_exclusion_fraction=0)
    targets = diagnostic.diagnostic_targets(data,{"000025":.05,"000001":.05},cfg,mode)
    assert {int(t) for t,w in targets.weights.items() if w>0} == expected
    assert all(w==.05 for w in targets.weights.values() if w>0)
    # Down index: diagnostic weights stay fully invested.
    bars = data.indices[0].closes
    down = replace(data,indices=(replace(data.indices[0],closes=(*bars[:-1],CloseBar(data.signal_date,1))),))
    assert diagnostic.diagnostic_targets(down,{},cfg,mode).weights == {t:.05 for t in sorted(f"{i:06d}" for i in expected)}


def test_experiments_only_change_explicit_filter_and_cost_parameters():
    jobs = list(diagnostic.experiments())
    assert len(jobs)==8 and len({n for n,_,_ in jobs})==8
    assert jobs[0] == ("default_filter_on",diagnostic.BASE,None)
    for name,config,mode in jobs:
        assert replace(config,costs=Costs(),below_ma_exposure=.5)==diagnostic.BASE
        assert config.costs==(Costs(0,0,0) if "zero_cost" in name else Costs())
        assert config.below_ma_exposure==(1 if "filter_off" in name else .5)
    assert BacktestConfig().factors.value==.4 and BacktestConfig().below_ma_exposure==.5


@pytest.fixture
def small_cache():
    days = (date(2020,1,31),date(2020,2,3),date(2020,2,28),date(2020,3,2),date(2020,3,3))
    bars = {f"{i:06d}":{d:DailyBar(100+i+j,100+i+j) for j,d in enumerate(days)} for i in range(1,26)}
    indices = {n:{d:100+j for j,d in enumerate(days)} for n in ("KOSPI200","KOSDAQ")}
    class Cache:
        def backtest_inputs(self,config):
            return {"days":days,"bars":bars,"index_closes":indices,"snapshot_for":lambda d:self.snapshot_for(d,config)}
        def snapshot_for(self,signal,config):
            return snapshot(signal)
    return Cache()


def save_baseline(path,cache):
    result = run_backtest(**cache.backtest_inputs(diagnostic.BASE),config=diagnostic.BASE,halt_policy=HaltPolicy.NONE)
    output = {"config":asdict(diagnostic.BASE),"halt_policy":"none","performance":report(result)["full"],
        "equity":[{**asdict(p),"date":str(p.date),"risk":p.risk.value} for p in result.equity],
        "benchmarks":{n:[{"date":str(d),"close":v} for d,v in s] for n,s in result.benchmarks.items()},
        "trade_log":[{**asdict(e),"date":str(e.date)} for e in result.events]}
    path.write_text(json.dumps(output),encoding="utf-8")
    return output


def test_full_diagnostic_runs_same_executor_reproduces_baseline_and_writes_all_tables(tmp_path,small_cache,monkeypatch,capsys):
    path=tmp_path/"baseline.json"
    baseline=save_baseline(path,small_cache)
    calls=[]
    def cache(*args,**kwargs):
        calls.append(args)
        with pytest.raises(RuntimeError,match="forbidden"):
            socket.create_connection(("example.invalid",443))
        return small_cache
    monkeypatch.setattr(diagnostic,"BulkCachedData",cache)
    args=diagnostic.parse_args(["--baseline",str(path),"--output-dir",str(tmp_path/"diagnosis")])
    summary=diagnostic.run_diagnosis(args)
    assert len(calls)==1
    assert summary["status"].startswith("complete") and summary["network_requests"]==0
    assert len(summary["portfolios"])==10 and len(summary["experiments"])==8
    assert summary["portfolios"]["default_filter_on"]["metrics"]==baseline["performance"]["strategy"]
    assert len(list((args.output_dir/"runs").glob("*.json")))==8
    for name in ("summary.json","diagnosis.md","yearly_returns.csv","full_period.csv"):
        assert (args.output_dir/name).is_file()
    text=(args.output_dir/"diagnosis.md").read_text(encoding="utf-8")
    assert "eligible_equal_weight" in text and "value_minus_universe" in text
    assert summary["experiments"]["zero_cost_minus_net_filter_on"]["cagr"] > 0
    assert capsys.readouterr().out.count("Completed ")==8
    raw=json.loads((args.output_dir/"runs"/"eligible_equal_weight.json").read_text(encoding="utf-8"))
    assert raw["mechanics"]["status"]=="passed" and raw["config"]["costs"]==asdict(Costs())


def test_saved_only_never_loads_cache_and_rejects_wrong_config(tmp_path,small_cache,monkeypatch):
    path=tmp_path/"baseline.json"
    output=save_baseline(path,small_cache)
    monkeypatch.setattr(diagnostic,"BulkCachedData",lambda *a,**k:pytest.fail("saved-only loaded cache"))
    args=diagnostic.parse_args(["--saved-only","--baseline",str(path),"--output-dir",str(tmp_path/"out")])
    summary=diagnostic.run_diagnosis(args)
    assert not summary["experiments"] and "pending" in summary["status"]
    output["config"]["quality_metric"]="operating_income_to_assets"
    path.write_text(json.dumps(output),encoding="utf-8")
    with pytest.raises(ValueError,match="default accounting"):
        diagnostic.read_saved(path)


def test_mismatched_baseline_fails_before_counterfactuals(tmp_path,small_cache,monkeypatch):
    path=tmp_path/"baseline.json"
    output=save_baseline(path,small_cache)
    output["equity"][-1]["equity"]+=1
    path.write_text(json.dumps(output),encoding="utf-8")
    monkeypatch.setattr(diagnostic,"BulkCachedData",lambda *a,**k:small_cache)
    args=diagnostic.parse_args(["--baseline",str(path),"--output-dir",str(tmp_path/"out")])
    with pytest.raises(ValueError,match="rerun differs"):
        diagnostic.run_diagnosis(args)
    assert not (args.output_dir/"runs").exists()


def test_benchmark_revision_fails_before_simulations(tmp_path,small_cache,monkeypatch):
    path=tmp_path/"baseline.json"
    output=save_baseline(path,small_cache)
    output["benchmarks"]["KOSPI200"][-1]["close"]+=1
    path.write_text(json.dumps(output),encoding="utf-8")
    monkeypatch.setattr(diagnostic,"BulkCachedData",lambda *a,**k:small_cache)
    args=diagnostic.parse_args(["--baseline",str(path),"--output-dir",str(tmp_path/"out")])
    with pytest.raises(ValueError,match="Cached benchmark differs"):
        diagnostic.run_diagnosis(args)
    assert not args.output_dir.exists()


def test_direct_cost_audit_does_not_label_cost_sum_as_counterfactual_return():
    output={"config":{"costs":asdict(Costs(100,200,100))},
        "trade_log":[{"action":"buy","shares":5,"price":10.1},
                     {"action":"sell","shares":5,"price":9.9}],
        "equity":[{"date":"2020-01-01","equity":100},{"date":"2021-01-01","equity":100}]}
    result=diagnostic.direct_costs(output)
    assert result["commission"]==pytest.approx(1)
    assert result["sell_tax"]==pytest.approx(.99)
    assert result["slippage"]==pytest.approx(1)
    assert result["total"]==pytest.approx(2.99)
    assert "Not a counterfactual" in result["convention"]
