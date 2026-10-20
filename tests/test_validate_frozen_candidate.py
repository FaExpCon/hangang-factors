"""Validation-launcher tests: fake data and writes exclusively in tmp_path."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date

import pytest

from scripts import validate_frozen_candidate as launcher
from kr_quant.backtest import HaltPolicy
from kr_quant.config import FactorWeights


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    design = tmp_path / "design"; design.mkdir()
    for name in ("cache", "company", "disclosures"):
        (tmp_path / name).mkdir()
    financials = tmp_path / "annual.json"; financials.write_text("[]", encoding="utf-8")
    identity = {"financials_sha256": hashlib.sha256(b"[]").hexdigest(),
                "source_metadata_sha256": "fake-source", "source_file_count": 0,
                "code_sha256": "fake-engine"}
    configs = {"W-532": replace(launcher.BASE, factors=FactorWeights(.5,.3,.2)),
               "baseline": launcher.BASE, "T-50": replace(launcher.BASE, retention_rank=50)}
    for name in configs:
        for halt in ("none", "resume"):
            record = {"candidate": name, "halt_policy": halt, "config": asdict(configs[name]),
                      "data_identity": identity, "protocol_sha256": "fake-protocol",
                      "performance": {"strategy": {"cagr": .1, "mdd": -.2, "sharpe": .5}}, "days_halted": 2}
            (design / f"{halt}_{name}.json").write_text(json.dumps(record), encoding="utf-8")
    sha = hashlib.sha256((design / "resume_W-532.json").read_bytes()).hexdigest()
    monkeypatch.setattr(launcher, "WINNER_SHA", sha)  # Fake signed artifact; never points at real outputs.
    reg = {"freeze_type": "W-532-validation-v1", "configuration_sha256": launcher.configuration_fingerprint(asdict(configs["W-532"])),
           "winner_artifact_sha256": sha, "design_protocol_sha256": "fake-protocol",
           "financials_sha256": identity["financials_sha256"], "source_metadata_sha256": "fake-source",
           "source_file_count": 0, "engine_code_sha256": "fake-engine", "start": "2021-01", "end": "2024-12"}
    docs = tmp_path / "experiments.md"; docs.write_text("```json\n"+json.dumps(reg)+"\n```", encoding="utf-8")
    monkeypatch.setattr(launcher, "input_identity", lambda *a: identity.copy())
    monkeypatch.setattr(launcher, "code_without_launcher", lambda: "fake-engine")
    readiness = {"status": "ready", "month_count": 48, "warmup_sessions": 260,
                 "sessions_in_range": 2, "first_session": "2021-01-04", "last_session": "2024-12-30"}
    calls = []
    def audit(start, end, cache):
        calls.append((start,end)); assert cache == tmp_path/"cache"
        return readiness.copy()
    monkeypatch.setattr(launcher, "audit_range", audit)
    args = launcher.parse_args(["--design-runs",str(design), "--output-dir",str(tmp_path/"validation"),
        "--financials",str(financials),"--cache-dir",str(tmp_path/"cache"),
        "--company-cache-dir",str(tmp_path/"company"),"--disclosures",str(tmp_path/"disclosures"),
        "--experiments",str(docs)])
    return args, configs, reg, readiness, calls


def test_dry_run_loads_frozen_config_but_never_simulates_or_writes(frozen, monkeypatch, capsys):
    args, configs, reg, readiness, calls = frozen
    def forbidden(*a, **kw): pytest.fail("Dry run attempted simulation/write")
    for name in ("BulkCachedData", "run_cached_backtest", "reference_output", "save_json", "write_table", "judgment"):
        monkeypatch.setattr(launcher, name, forbidden)
    args.dry_run = True
    before = {p: p.read_bytes() for p in args.experiments.parent.rglob("*") if p.is_file()}
    result = launcher.run(args)
    assert result["status"] == "dry_run_verified" and not args.output_dir.exists()
    assert result["configuration_sha256"] == reg["configuration_sha256"]
    assert result["planned_runs"] == [("W-532","resume"),("eligible_equal_weight","resume"),
        ("W-532","none"),("baseline","resume"),("baseline","none"),("T-50","none")]
    assert calls == [("2021-01","2024-12")]
    assert all(p.read_bytes() == data for p,data in before.items())
    text = capsys.readouterr().out
    assert "0.411" not in text and "CAGR=" not in text and "Sharpe=" not in text


@pytest.mark.parametrize("field,value", [("halt_policy","none"),("candidate","T-50"),
    ("retention_rank",40),("rebalance_frequency","quarterly"),
    ("factors",asdict(FactorWeights())),
    ("costs",{"commission_bps":0,"transaction_tax_bps":0,"slippage_bps":0})])
def test_other_configuration_is_refused(frozen, monkeypatch, field, value):
    args, configs, reg, readiness, calls = frozen
    path = args.design_runs / "resume_W-532.json"
    saved = launcher.read(path)
    if field in ("halt_policy","candidate"): saved[field] = value
    else: saved["config"][field] = value
    path.write_text(json.dumps(saved), encoding="utf-8")
    # Even re-signing a fake artifact/doc cannot permit another strategy config.
    sha = hashlib.sha256(path.read_bytes()).hexdigest(); monkeypatch.setattr(launcher,"WINNER_SHA",sha)
    reg["winner_artifact_sha256"] = sha
    reg["configuration_sha256"] = launcher.configuration_fingerprint(saved["config"])
    args.experiments.write_text("```json\n"+json.dumps(reg)+"\n```", encoding="utf-8")
    with pytest.raises(ValueError, match="primary|configuration differs"):
        launcher.run(args)
    assert not args.output_dir.exists()


def test_doc_fingerprint_and_frozen_window_cannot_drift(frozen):
    args, _, reg, _, _ = frozen
    reg["configuration_sha256"] = "wrong"
    args.experiments.write_text("```json\n"+json.dumps(reg)+"\n```", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        launcher.run(args)
    reg["end"] = "2025-12"
    args.experiments.write_text("```json\n"+json.dumps(reg)+"\n```", encoding="utf-8")
    with pytest.raises(ValueError, match="window"):
        launcher.run(args)


def test_existing_directory_refused_before_data_reads_including_empty_directory(frozen, monkeypatch):
    args, *_ = frozen
    args.output_dir.mkdir()
    monkeypatch.setattr(launcher,"frozen_registration",lambda *a: pytest.fail("Read after existing-output check"))
    with pytest.raises(FileExistsError, match="not be overwritten"):
        launcher.run(args)


@pytest.mark.parametrize("ws,es,ixs,wm,ixm,cagr,expected", [
    (.9,.7,1.,-.3,-.35,.1,"PASS"),       # Sharpe branch only.
    (.6,.5,1.,-.2,-.35,.1,"PASS"),       # MDD branch only.
    (.6,.5,1.,-.4,-.5,.1,"PASS"),        # Inclusive 10-percentage-point boundary.
    (.8,.7,1.,-.4,-.35,.1,"PASS"),       # Inclusive 0.8 boundary.
    (.7,.7,1.,-.1,-.35,.1,"FAIL"),       # Criterion 1 strictly greater.
    (.7,.6,1.,-.3,-.35,.1,"FAIL"),       # Both branches fail.
    (.9,.7,1.,-.1,-.35,0.,"FAIL"),       # CAGR strictly positive.
    (.9,.7,1.,-.1,-.35,-.1,"FAIL"),
    (-.1,-.2,-.5,-.3,-.35,.1,"PASS"),   # Literal negative index Sharpe.
    (None,.5,1.,-.2,-.35,.1,"BLOCKED"),
    (float("nan"),.5,1.,-.2,-.35,.1,"BLOCKED"),
    (.9,.7,1.,-.2,-.35,float("inf"),"BLOCKED"),
])
def test_primary_criteria_known_fake_numbers(ws,es,ixs,wm,ixm,cagr,expected):
    chosen={"performance":{"strategy":{"sharpe":ws,"mdd":wm,"cagr":cagr},
                           "KOSPI200":{"sharpe":ixs,"mdd":ixm}}}
    eligible={"performance":{"strategy":{"sharpe":es}}}
    assert launcher.judgment(chosen,eligible)["status"] == expected


def test_fake_execution_is_only_validation_window_and_temp_writes(frozen, tmp_path, monkeypatch):
    args, configs, reg, readiness, calls = frozen
    constructed, jobs = [], []
    class Cache:
        start = date(2021,1,1)
        trading_days = (date(2020,12,30),date(2021,1,4),date(2024,12,30))
    def cache(start,end,**kwargs):
        constructed.append((start,end)); return Cache()
    monkeypatch.setattr(launcher,"BulkCachedData",cache)
    def output():
        metric={"sharpe":.9,"mdd":-.2,"cagr":.1}
        return {"performance":{"strategy":metric,"KOSPI200":metric,"annual_turnover":1,"trades":1},
                "days_halted":0,"equity":[{"date":"2021-01-04","equity":100,"cash":100},
                    {"date":"2024-12-30","equity":110,"cash":10}],
                "benchmarks":{"KOSPI200":[{"date":"2021-01-04","close":100},{"date":"2024-12-30","close":110}]}}
    def simulate(cache,cfg,halt_policy):
        jobs.append((cfg,halt_policy)); return output()
    monkeypatch.setattr(launcher,"run_cached_backtest",simulate)
    monkeypatch.setattr(launcher,"reference_output",lambda *a:output())
    save = launcher.save_json; table = launcher.write_table
    def guard(fn):
        def wrapped(path,*a):
            assert path.resolve().is_relative_to(tmp_path.resolve()), "Non-temporary output write"
            return fn(path,*a)
        return wrapped
    monkeypatch.setattr(launcher,"save_json",guard(save)); monkeypatch.setattr(launcher,"write_table",guard(table))
    launcher.run(args)
    assert constructed == [("2021-01","2024-12")]
    assert jobs[0] == (configs["W-532"],HaltPolicy.RESUME)
    assert len(jobs)==5 and (args.output_dir/"judgment.json").exists()
    with pytest.raises(FileExistsError): launcher.run(args)


def test_bad_actual_calendar_stops_before_any_fake_trades(frozen, monkeypatch):
    args, *_ = frozen
    class Cache:
        start = date(2021,1,1)
        trading_days = (date(2021,1,4),date(2025,1,2))
    monkeypatch.setattr(launcher,"BulkCachedData",lambda *a,**k:Cache())
    monkeypatch.setattr(launcher,"run_cached_backtest",lambda *a,**k:pytest.fail("Escaped window simulated"))
    with pytest.raises(ValueError,match="calendar"):
        launcher.run(args)


def test_missing_primary_metric_blocks_instead_of_passing():
    assert launcher.judgment({"performance":{"strategy":{}}}, {})["status"] == "BLOCKED"


def test_insufficient_or_missing_real_data_blocks_dry_run(frozen, monkeypatch):
    args, *_ = frozen
    args.dry_run = True
    monkeypatch.setattr(launcher,"audit_range",lambda *a:{"status":"incomplete"})
    with pytest.raises(ValueError,match="data missing"):
        launcher.run(args)
    assert not args.output_dir.exists()
