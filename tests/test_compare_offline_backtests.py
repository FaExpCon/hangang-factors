"""Bounded synthetic checks only; never load the historical comparison cache."""

import csv
import json
import socket

import pytest

from kr_quant.backtest import HaltPolicy
from kr_quant.config import BacktestConfig, QualityMetric
from scripts import compare_offline_backtests as comparison
from scripts.run_offline_backtest import parse_args as backtest_args


def test_matrix_has_every_independent_combination_and_preserves_other_defaults():
    from dataclasses import replace
    jobs = list(comparison.configurations())
    assert len(jobs) == 12
    assert {(policy.value, variant) for policy, variant, _ in jobs} == {
        (policy, variant) for policy in ("none", "permanent", "resume")
        for variant in ("default", "operating_income_filter_only", "operating_income_assets_quality_only", "both")}
    for policy, variant, config in jobs:
        assert config.exclude_nonpositive_operating_income == (variant in ("operating_income_filter_only", "both"))
        assert (config.quality_metric is QualityMetric.OPERATING_INCOME_TO_ASSETS) == (variant in ("operating_income_assets_quality_only", "both"))
        assert replace(config, exclude_nonpositive_operating_income=False, quality_metric=QualityMetric.PBR_PER) == BacktestConfig(fundamental_lag_days=0)
    assert backtest_args([]).halt_policy == "permanent"
    assert backtest_args(["--halt-policy", "resume"]).halt_policy == "resume"
    with pytest.raises(SystemExit):
        backtest_args(["--halt-policy", "bad"])


def fake_output(policy):
    return {"network_requests": 0, "days_halted": 0 if policy is HaltPolicy.NONE else 7,
            "performance": {"strategy": {"start": "2016-07-01", "end": "2020-11-30",
                "cagr": .1, "mdd": -.3, "sharpe": .7, "sortino": 1.1},
                "annual_turnover": 1.5, "trades": 12,
                "KOSPI200": {"total_return": .6, "cagr": .12, "mdd": -.2, "sharpe": .9, "sortino": 1.2},
                "KOSDAQ": {"total_return": .3, "cagr": .06, "mdd": -.4, "sharpe": .4, "sortino": .5}}}


def test_matrix_writes_one_table_full_runs_and_flushed_progress_under_network_guard(tmp_path, monkeypatch, capsys):
    instances = []
    calls = []
    def cache(*args, **kwargs):
        with pytest.raises(RuntimeError, match="Network access is forbidden"):
            socket.create_connection(("example.invalid",443))
        instances.append(object())
        return instances[-1]
    def run(cache, config, halt_policy):
        assert cache is instances[0]
        calls.append((halt_policy, config))
        with pytest.raises(RuntimeError, match="Network access is forbidden"):
            socket.getaddrinfo("example.invalid",443)
        return fake_output(halt_policy)
    monkeypatch.setattr(comparison,"BulkCachedData",cache)
    monkeypatch.setattr(comparison,"run_cached_backtest",run)
    args = comparison.parse_args(["--output",str(tmp_path / "table.csv"),"--runs-dir",str(tmp_path / "runs")])
    assert (args.start,args.end) == ("2016-07","2020-11")
    rows = comparison.run_comparison(args)
    assert len(instances) == 1 and len(calls) == len(rows) == 12
    with args.output.open(encoding="utf-8-sig",newline="") as stream:
        saved = list(csv.DictReader(stream))
    assert len(saved) == 12
    assert float(saved[0]["cagr"]) == .1 and float(saved[0]["mdd"]) == -.3
    assert float(saved[0]["annual_turnover"]) == 1.5
    assert saved[0]["trades"] == "12" and saved[0]["days_halted"] == "0"
    assert saved[4]["days_halted"] == "7"
    assert float(saved[0]["kospi200_buy_hold_total_return"]) == .6
    assert float(saved[0]["kosdaq_buy_hold_cagr"]) == .06
    files = list(args.runs_dir.glob("*.json"))
    assert len(files) == 12
    assert {json.loads(f.read_text(encoding="utf-8"))["variant"] for f in files} == set(comparison.VARIANTS)
    progress = capsys.readouterr().out
    assert progress.count("Completed halt=") == 12
    assert "[12/12] Completed" in progress


def test_matrix_failure_preserves_completed_rows_and_does_not_fabricate_result(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(comparison,"BulkCachedData",lambda *args, **kwargs: object())
    def run(cache, config, halt_policy):
        calls.append(halt_policy)
        if len(calls) == 2:
            raise FileNotFoundError("missing synthetic cache")
        return fake_output(halt_policy)
    monkeypatch.setattr(comparison,"run_cached_backtest",run)
    args = comparison.parse_args(["--output",str(tmp_path / "table.csv"),"--runs-dir",str(tmp_path / "runs")])
    with pytest.raises(FileNotFoundError,match="missing synthetic cache"):
        comparison.run_comparison(args)
    with args.output.open(encoding="utf-8-sig",newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 1
    assert len(list(args.runs_dir.glob("*.json"))) == 1
