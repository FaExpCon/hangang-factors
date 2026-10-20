"""Synthetic official-format cache tests. No data-provider requests."""

from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import socket

import pandas as pd
import pytest

from kr_quant.backtest import run_backtest
from kr_quant.bulk_cached import BulkCachedData, cached_date, load_fundamentals
from kr_quant.config import BacktestConfig, DataMode
from kr_quant.contracts import ClassificationKind, SecurityType, TradingStatus
from kr_quant.factors import score_factors
from scripts.run_offline_assembler import assemble_report, offline_only


def write(cache, service, day, rows):
    path = cache / service / f"{day:%Y%m%d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"OutBlock_1": rows}), encoding="utf-8")


def financial(ticker="000001", year=2019, filing="20200320", receipt="20200320000001",
              income=100, equity=1000, corrected=False):
    return {"stock_code": ticker, "year": year, "filing_date": filing,
            "currently_listed": False,
            "filings": [{"report_name": f"사업보고서 ({year}.12)", "filing_date": filing,
                         "receipt_no": receipt, "had_correction": corrected}],
            "accounts": {str(i): {"account_nm": name, "fs_div": "CFS", "sj_div": statement,
                                  "amount": str(value)}
                         for i, (name, statement, value) in enumerate([
                             ("당기순이익(손실)", "IS", income), ("자본총계", "BS", equity)])}}


@pytest.fixture
def fake(tmp_path):
    cache = tmp_path / "cache"
    start, end = date(2020, 3, 1), date(2020, 4, 30)
    first = start - timedelta(days=400)
    days = [d.date() for d in pd.date_range(first, end) if d.weekday() < 5]
    holiday = date(2020, 4, 30)  # Deliberately closed calendar month-end.
    sessions = [d for d in days if d != holiday]
    split = date(2020, 4, 15)
    for day in days:
        if day == holiday:
            write(cache, "kospi_dd_trd", day, [])
            continue
        write(cache, "kospi_dd_trd", day, [
            {"BAS_DD": f"{day:%Y%m%d}", "IDX_NM": name, "CLSPRC_IDX": "100"}
            for name in ("코스피", "코스피 200", "코스피 200 중소형주")])
        write(cache, "kosdaq_dd_trd", day, [
            {"BAS_DD": f"{day:%Y%m%d}", "IDX_NM": "코스닥", "CLSPRC_IDX": "200"}])
        for service, tickers in (("stk_bydd_trd", ("000001", "000002", "000003")),
                                 ("ksq_bydd_trd", ("000004",))):
            rows = []
            for ticker in tickers:
                # 000002 disappears after March; it must still exist in March.
                if ticker == "000002" and day.month == 4 and day.year == 2020:
                    continue
                close = 20 if day >= split else 1000
                rows.append({"BAS_DD": f"{day:%Y%m%d}", "ISU_CD": ticker, "ISU_NM": ticker,
                             "TDD_CLSPRC": str(close), "TDD_OPNPRC": str(close * .9),
                             "TDD_HGPRC": str(close), "TDD_LWPRC": str(close),
                             "ACC_TRDVOL": "100", "ACC_TRDVAL": "2,000,000,000",
                             "FLUC_RT": "0", "MKTCAP": "10000", "LIST_SHRS": "100"})
            write(cache, service, day, rows)
    for signal in (date(2020, 3, 31), date(2020, 4, 29)):
        for service, tickers in (("stk_isu_base_info", ("000001", "000002", "000005")),
                                 ("ksq_isu_base_info", ("000004",))):
            rows = [{"ISU_SRT_CD": ticker, "ISU_ABBRV": ticker, "LIST_DD": "20000101",
                     "KIND_STKCERT_TP_NM": "보통주", "SECUGRP_NM": "주권",
                     "SECT_TP_NM": "벤처기업부" if ticker == "000004" else ""}
                    for ticker in tickers if ticker != "000002" or signal.month == 3]
            if signal.month == 4 and service == "stk_isu_base_info":
                rows[0]["KIND_STKCERT_TP_NM"] = "신형우선주"
            write(cache, service, signal, rows)
    financials = tmp_path / "financials.json"
    financials.write_text(json.dumps([financial(), financial("000002", filing="20200410", receipt="20200410000001"),
                                      financial("000004", receipt="20200420000001", corrected=True)]), encoding="utf-8")
    return cache, financials, sessions


def load(fake, **kwargs):
    cache, financials, _ = fake
    kwargs.setdefault("company_cache_dir", cache / "overview")
    kwargs.setdefault("disclosure_dir", cache / "disclosures")
    return BulkCachedData("2020-03", "2020-04", cache_dir=cache, financials_path=financials, **kwargs)


def test_calendar_one_pass_and_historical_members(fake, monkeypatch):
    with offline_only():
        data = load(fake)
    assert data.month_ends == (date(2020, 3, 31), date(2020, 4, 29))
    assert data.daily.index.names == ["date", "ticker"]
    assert data.daily_files_read == len(data.trading_days) * 2
    count = data.files_read
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("Snapshot reread a cache file"))
    march = data.snapshot_for(data.month_ends[0])
    april = data.snapshot_for(data.month_ends[1])
    assert data.files_read == count
    assert "000002" in {s.ticker for s in march.stocks}
    assert "000002" not in {s.ticker for s in april.stocks}
    by = {s.ticker: s for s in march.stocks}
    assert by["000003"].sector == "unknown" and by["000003"].security_type is None
    assert by["000005"].trading_status is TradingStatus.SUSPENDED
    assert by["000004"].sector == "unknown"  # listing segment is not industry
    assert by["000004"].is_financial is None
    assert by["000001"].security_type is SecurityType.COMMON
    assert next(s for s in april.stocks if s.ticker == "000001").security_type is SecurityType.PREFERRED
    assert all(s.classification_kind is ClassificationKind.HISTORICAL for s in march.stocks)


def test_filing_dates_corrections_and_ratios(fake):
    data = load(fake)
    march = {s.ticker: s for s in data.snapshot_for(date(2020, 3, 31)).stocks}
    april = {s.ticker: s for s in data.snapshot_for(date(2020, 4, 29)).stocks}
    fund = march["000001"].fundamentals
    assert (fund.per, fund.pbr, fund.eps) == (100, 10, 1)
    assert fund.observed_on == date(2019, 12, 31)
    assert fund.available_on == date(2020, 3, 20)
    assert march["000002"].fundamentals is None
    assert march["000004"].fundamentals.available_on == date(2020, 3, 20)
    assert march["000004"].fundamentals.approximate is True
    assert april["000004"].fundamentals.available_on == date(2020, 3, 20)
    assert data.fundamentals["000002"][0].available_on == date(2020, 4, 10)
    assert march["000001"].avg_trading_value_20d.value == 2_000_000_000
    assert all(b.date <= date(2020, 3, 31) for b in march["000001"].closes)


def test_split_adjusted_prices_opens_and_no_future_dependency(fake):
    data = load(fake)
    prices = data.daily.xs("000001", level="ticker")
    assert prices.loc[date(2020, 4, 14), "close"] == 1000
    assert prices.loc[date(2020, 4, 15), "close"] == 20
    assert prices["adjusted_close"].min() == pytest.approx(1000)
    assert prices["adjusted_close"].max() == pytest.approx(1000)
    assert prices["adjusted_open"].min() == pytest.approx(900)
    shortened = BulkCachedData("2020-03", "2020-03", cache_dir=fake[0], financials_path=fake[1],
                               company_cache_dir=fake[0] / "overview", disclosure_dir=fake[0] / "disclosures")
    assert shortened.snapshot_for(date(2020, 3, 31)) == data.snapshot_for(date(2020, 3, 31))


@pytest.mark.parametrize("mutation,match", [("missing", "Offline cache miss"),
                                          ("rate", "FLUC_RT"), ("duplicate", "Duplicate daily"),
                                          ("index", "exact index"), ("future", "Future listing")])
def test_fail_closed_cache_errors(fake, mutation, match):
    cache = fake[0]
    day = date(2020, 3, 31)
    service = "stk_bydd_trd"
    if mutation == "index":
        service = "kospi_dd_trd"
    if mutation == "future":
        service = "stk_isu_base_info"
    path = cache / service / f"{day:%Y%m%d}.json"
    if mutation == "missing":
        path.unlink()
    else:
        rows = json.loads(path.read_text(encoding="utf-8"))["OutBlock_1"]
        if mutation == "rate":
            rows[0]["FLUC_RT"] = "-"
        elif mutation == "duplicate":
            rows.append(rows[0])
        elif mutation == "index":
            rows = [r for r in rows if r["IDX_NM"] != "코스피 200"]
        else:
            rows[0]["LIST_DD"] = "20200401"
        write(cache, service, day, rows)
    with pytest.raises((ValueError, FileNotFoundError), match=match), offline_only():
        load(fake)


def test_account_basis_non_december_and_correction_missing_receipt(tmp_path):
    record = financial()
    record["filings"][0]["report_name"] = "사업보고서 (2019.03)"
    record["accounts"]["1"]["fs_div"] = "OFS"  # CFS income cannot pair with OFS equity.
    path = tmp_path / "fund.json"
    path.write_text(json.dumps([record]), encoding="utf-8")
    assert load_fundamentals(path) == {}
    record["accounts"]["0"]["fs_div"] = "OFS"
    path.write_text(json.dumps([record]), encoding="utf-8")
    result = load_fundamentals(path)["000001"][0]
    assert result.basis == "OFS" and result.period_end == date(2019, 3, 31)
    record["filings"][0].update(had_correction=True, receipt_no="")
    path.write_text(json.dumps([record]), encoding="utf-8")
    result = load_fundamentals(path)["000001"][0]
    assert result.available_on == date(2020, 3, 20)
    assert result.receipt_available_on is None and result.corrected


def test_transition_period_falls_back_to_public_full_year(fake):
    rows = json.loads(fake[1].read_text(encoding="utf-8"))
    rows[0]["accounts"]["op"] = {"account_nm":"영업이익", "fs_div":"CFS", "sj_div":"IS", "amount":"20"}
    rows[0]["accounts"]["assets"] = {"account_nm":"자산총계", "fs_div":"CFS", "sj_div":"BS", "amount":"2000"}
    transition = financial(year=2020, filing="20200410", receipt="20200410000001")
    transition["filings"][0]["report_name"] = "사업보고서 (2020.03)"
    transition["accounts"]["assets"] = {"account_nm": "자산총계", "fs_div": "CFS", "sj_div": "BS", "amount": "1000"}
    transition["fiscal_periods"] = [{
        "fiscal_year": 2020, "start": "2020-01-01", "end": "2020-03-31",
        "months": 3, "is_full_year": False, "period_length_source": "account_range",
        "first_filing_date": "20200410", "latest_filing_date": "20200410",
        "receipt_no": "20200410000001", "report_name": "사업보고서 (2020.03)",
        "had_correction": False, "filings": 1}]
    rows.append(transition)
    fake[1].write_text(json.dumps(rows), encoding="utf-8")
    data = load(fake)
    march = next(s for s in data.snapshot_for(date(2020, 3, 31)).stocks if s.ticker == "000001").fundamentals
    april = next(s for s in data.snapshot_for(date(2020, 4, 29)).stocks if s.ticker == "000001").fundamentals
    assert march.observed_on == date(2019, 12, 31)
    assert april.observed_on == date(2020, 3, 31)
    assert april.available_on == date(2020, 4, 10)
    assert april.per == 100  # 2019 income, not the 3-month transition income.
    assert april.pbr == 10   # 2020-03 balance sheet is allowed.
    assert april.eps == 1
    assert april.operating_income == 20
    assert april.net_income == 100
    assert april.total_assets == 1000
    assert april.income_period_end == date(2019,12,31)
    assert april.quality_assets == 2000  # Same full-year report as numerator.


def test_full_range_data_only_mode_does_not_score_or_build_targets(fake, monkeypatch):
    from scripts import run_offline_assembler as assembler
    monkeypatch.setattr(assembler,"score_factors",lambda *a: pytest.fail("Data-only scored strategy"))
    monkeypatch.setattr(assembler,"FactorStrategy",lambda *a: pytest.fail("Data-only built targets"))
    result=assembler.assemble_report("2020-03","2020-04",cache_dir=fake[0],financials_path=fake[1],
        company_cache_dir=fake[0]/"overview",disclosure_dir=fake[0]/"disclosures",data_only=True)
    assert result["status"]=="assembled_data_only" and result["sample"] is None
    assert all(row["target_count"] is None and row["eligible"] is None for row in result["months"])


def test_transition_period_without_prior_full_year_suppresses_income_metrics(fake):
    rows = [r for r in json.loads(fake[1].read_text(encoding="utf-8")) if r["stock_code"] != "000001"]
    short = financial(year=2020, filing="20200410", receipt="20200410000001")
    short["filings"][0]["report_name"] = "사업보고서 (2020.03)"
    short["fiscal_periods"] = [{
        "fiscal_year": 2020, "start": "2020-01-01", "end": "2020-03-31",
        "months": 3, "is_full_year": False, "period_length_source": "account_range",
        "first_filing_date": "20200410", "latest_filing_date": "20200410",
        "receipt_no": "20200410000001", "report_name": "사업보고서 (2020.03)",
        "had_correction": False, "filings": 1}]
    rows.append(short)
    fake[1].write_text(json.dumps(rows), encoding="utf-8")
    data = load(fake)
    april = next(s for s in data.snapshot_for(date(2020, 4, 29)).stocks if s.ticker == "000001").fundamentals
    assert april is None


def test_multiple_periods_have_separate_accounts_and_publication_dates(tmp_path):
    from kr_quant.dart_gap import build_record
    company = {"stock_code": "000001", "names": ["Example"], "markets": ["KOSDAQ"],
               "mapping": {"corp_code": "00000001", "direct_candidates": ["00000001"], "method": "stock_code"}}
    filings = [{"report_nm": f"사업보고서 (2019.{month})", "rcept_dt": day,
                "rcept_no": day + "000001", "corp_name": "Example"}
               for month, day in [("06", "20190920"), ("12", "20200320")]]
    accounts = []
    for dt, income, equity in [("2018.07.01 ~ 2019.06.30", "100", "1000"),
                               ("2019.07.01 ~ 2019.12.31", "9999", "2000")]:
        for name, sj, amount in [("당기순이익", "IS", income), ("자본총계", "BS", equity)]:
            accounts.append({"corp_code": "00000001", "stock_code": "000001", "bsns_year": "2019",
                             "account_nm": name, "fs_div": "CFS", "sj_div": sj,
                             "thstrm_dt": dt, "thstrm_amount": amount})
    record = build_record(company, 2019, filings, accounts)
    path = tmp_path / "annual.json"
    path.write_text(json.dumps([record]), encoding="utf-8")
    full, short = load_fundamentals(path)["000001"]
    assert (full.period_end, full.available_on, full.period_months, full.income) == (
        date(2019,6,30), date(2019,9,20), 12, 100)
    assert (short.period_end, short.available_on, short.period_months, short.income, short.equity) == (
        date(2019,12,31), date(2020,3,20), 6, None, 2000)


def test_unknown_financial_status_report_and_existing_backtest(fake):
    with offline_only():
        report = assemble_report("2020-03", "2020-04", cache_dir=fake[0], financials_path=fake[1],
                                 company_cache_dir=fake[0] / "overview", disclosure_dir=fake[0] / "disclosures")
        assert report["snapshot_count"] == 2 and report["network_requests"] == 0
        assert report["data_mode"] == "approximate" and report["fundamental_lag_days"] == 0
        assert "original filing date" in report["correction_policy"]
        assert report["sample"]["targets"] == []
        assert report["months"][0]["rejections"]["unknown_financial_status"] == 5
        data = load(fake)
        config = BacktestConfig(data_mode=DataMode.APPROXIMATE, fundamental_lag_days=0)
        inputs = data.backtest_inputs(config)
        result = run_backtest(**inputs, config=config)
    assert len(result.equity) == len(inputs["days"])
    assert result.signal_dates == (date(2020, 3, 31),)
    assert inputs["bars"]["000001"][date(2020, 4, 15)].close == pytest.approx(1000)
    snapshot = data.snapshot_for(date(2020, 3, 31))
    # A positive classification control proves it is the missing flag that blocks eligibility.
    verified = replace(snapshot, stocks=tuple(replace(s, is_financial=False) for s in snapshot.stocks))
    assert score_factors(verified, replace(config, market_cap_exclusion_fraction=0)).scores
    verified_config = replace(config, market_cap_exclusion_fraction=0)
    inputs["snapshot_for"] = lambda d: replace(data.snapshot_for(d), stocks=tuple(
        replace(s, is_financial=False) for s in data.snapshot_for(d).stocks))
    result = run_backtest(**inputs, config=verified_config)
    assert any(e.action == "buy" for e in result.events)
    equity = {point.date: point.equity for point in result.equity}
    assert equity[date(2020, 4, 15)] == pytest.approx(equity[date(2020, 4, 14)])


def test_offline_guard_blocks_connection_and_dns():
    with offline_only(), pytest.raises(RuntimeError, match="Network access is forbidden"):
        socket.create_connection(("example.invalid", 443))
    with offline_only(), pytest.raises(RuntimeError, match="Network access is forbidden"):
        socket.getaddrinfo("example.invalid", 443)


def test_alphanumeric_preferred_share_is_retained(fake):
    signal = date(2020, 3, 31)
    for service, field in (("stk_bydd_trd", "ISU_CD"), ("stk_isu_base_info", "ISU_SRT_CD")):
        path = fake[0] / service / f"{signal:%Y%m%d}.json"
        rows = json.loads(path.read_text(encoding="utf-8"))["OutBlock_1"]
        extra = {**rows[0], field: "00104K"}
        if service == "stk_isu_base_info":
            extra["KIND_STKCERT_TP_NM"] = "신형우선주"
        rows.append(extra)
        write(fake[0], service, signal, rows)
    stock = next(s for s in load(fake).snapshot_for(signal).stocks if s.ticker == "00104K")
    assert stock.security_type is SecurityType.PREFERRED


def test_real_returns_and_empty_basic_info(fake):
    signal = date(2020, 4, 29)
    rows = json.loads((fake[0] / "stk_bydd_trd" / "20200429.json").read_text(encoding="utf-8"))["OutBlock_1"]
    rows[0].update(TDD_CLSPRC="22", TDD_OPNPRC="21", FLUC_RT="10")
    write(fake[0], "stk_bydd_trd", signal, rows)
    data = load(fake)
    row = data.daily.loc[(signal, "000001")]
    assert row.adjusted_close == pytest.approx(1100)
    assert row.adjusted_open == pytest.approx(1050)
    assert cached_date("20200429") == cached_date("2020-04-29")
    write(fake[0], "stk_isu_base_info", signal, [])
    with pytest.raises(ValueError, match="Empty basic-info"):
        load(fake)


def test_liquidity_requires_twenty_market_sessions_and_approximate_lag(fake):
    day = date(2020, 3, 30)
    path = fake[0] / "stk_bydd_trd" / f"{day:%Y%m%d}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["OutBlock_1"]
    write(fake[0], "stk_bydd_trd", day, [r for r in rows if r["ISU_CD"] != "000001"])
    data = load(fake)
    snapshot = data.snapshot_for(date(2020, 3, 31), BacktestConfig(fundamental_lag_days=100))
    stock = next(s for s in snapshot.stocks if s.ticker == "000001")
    assert stock.avg_trading_value_20d.value == 0
    assert stock.fundamentals is None
    dated = data.snapshot_for(date(2020, 3, 31))
    assert next(s for s in dated.stocks if s.ticker == "000001").fundamentals is not None
    with pytest.raises(ValueError, match="month-end"):
        data.snapshot_for(date(2020, 4, 30))


def test_current_ksic_is_approximate_and_historical_types_preserved(fake):
    from kr_quant.company_overview import save_json
    cache = fake[0] / "overview"
    rows = json.loads(fake[1].read_text(encoding="utf-8"))
    for i, row in enumerate(rows, 1):
        row["corp_code"] = f"{i:08d}"
        save_json(cache / "company" / f"{i:08d}.json", {
            "corp_code": f"{i:08d}", "retrieved_on": "2026-10-18",
            "response": {"status": "000", "stock_code": row["stock_code"],
                         "induty_code": "64121" if i == 2 else "26110"}})
    fake[1].write_text(json.dumps(rows), encoding="utf-8")
    data = load(fake, company_cache_dir=cache)
    snapshot = data.snapshot_for(date(2020, 3, 31))
    stocks = {s.ticker: s for s in snapshot.stocks}
    assert stocks["000001"].sector == "26" and stocks["000001"].is_financial is False
    assert stocks["000002"].sector == "64" and stocks["000002"].is_financial is True
    assert stocks["000003"].sector == "unknown" and stocks["000003"].is_financial is None
    assert stocks["000001"].classification_kind is ClassificationKind.CURRENT_PROXY
    assert stocks["000001"].classification_retrieved_on == date(2026, 10, 18)
    assert stocks["000001"].security_type is SecurityType.COMMON
    scored = score_factors(snapshot, BacktestConfig(market_cap_exclusion_fraction=0, fundamental_lag_days=0))
    assert {s.ticker for s in scored.scores} == {"000001", "000004"}
    with pytest.raises(ValueError, match="current-classification proxy"):
        data.snapshot_for(date(2020, 3, 31), BacktestConfig(data_mode=DataMode.STRICT))


def test_approximate_corrected_fundamentals_rejected_by_strict(fake):
    data = load(fake)
    with pytest.raises(ValueError, match="approximate corrected fundamentals"):
        data.snapshot_for(date(2020, 3, 31), BacktestConfig(data_mode=DataMode.STRICT))


def test_optional_accounts_same_basis_and_latest_report_no_fallback(fake):
    rows = json.loads(fake[1].read_text(encoding="utf-8"))
    first = rows[0]
    first["accounts"]["operating"] = {"account_nm": "영업이익", "fs_div": "CFS", "sj_div": "IS", "amount": "-20"}
    first["accounts"]["assets"] = {"account_nm": "자산총계", "fs_div": "CFS", "sj_div": "BS", "amount": "2,000"}
    first["accounts"]["other_basis"] = {"account_nm": "영업이익", "fs_div": "OFS", "sj_div": "IS", "amount": "9999"}
    newer = financial(year=2020, filing="20200410", receipt="20200410000001")
    newer["filings"][0]["report_name"] = "사업보고서 (2020.03)"
    rows.append(newer)
    fake[1].write_text(json.dumps(rows), encoding="utf-8")
    data = load(fake)
    march = next(s for s in data.snapshot_for(date(2020, 3, 31)).stocks if s.ticker == "000001").fundamentals
    april = next(s for s in data.snapshot_for(date(2020, 4, 29)).stocks if s.ticker == "000001").fundamentals
    assert (march.operating_income, march.total_assets, march.net_income) == (-20, 2000, 100)
    assert april.operating_income is None and april.total_assets is None
    from kr_quant.config import QualityMetric
    from kr_quant.factors import score_factors
    scored = score_factors(data.snapshot_for(date(2020, 4, 29)), BacktestConfig(
        fundamental_lag_days=0, quality_metric=QualityMetric.OPERATING_INCOME_TO_ASSETS))
    assert "missing_operating_income" in scored.rejected["000001"]


@pytest.mark.parametrize("policy", ["none", "permanent", "resume"])
def test_offline_backtest_runner_uses_existing_engine_and_diagnostic_flags(fake, policy):
    from kr_quant.company_overview import SOURCE
    from scripts.run_offline_backtest import run_cached_backtest
    rows = json.loads(fake[1].read_text(encoding="utf-8"))
    rows[0]["accounts"]["op"] = {"account_nm": "영업이익", "fs_div": "CFS", "sj_div": "IS", "amount": "-1"}
    rows[0]["accounts"]["assets"] = {"account_nm": "자산총계", "fs_div": "CFS", "sj_div": "BS", "amount": "2000"}
    fake[1].write_text(json.dumps(rows), encoding="utf-8")
    with offline_only():
        data = load(fake)
        data.current_classifications = {ticker: {"sector": "26", "is_financial": False,
            "retrieved_on": date(2026, 10, 18), "source": SOURCE} for ticker in ("000001", "000004")}
        output = run_cached_backtest(data, BacktestConfig(fundamental_lag_days=0, market_cap_exclusion_fraction=0),
                                     halt_policy=policy)
    assert output["mechanics"]["status"] == "passed"
    assert output["mechanics"]["event_counts"]["buy"] >= 1
    assert output["mechanics"]["signals"] == 1
    assert output["network_requests"] == 0
    assert output["halt_policy"] == policy
    assert output["days_halted"] == (0 if policy == "none" else output["mechanics"]["risk_session_counts"].get("HALTED",0))
    assert set(output["performance"]["strategy"]) >= {"cagr", "mdd", "sharpe", "sortino"}
    assert output["performance"]["strategy"]["start"] == output["performance"]["KOSPI200"]["start"]
    assert output["performance"]["strategy"]["end"] == output["performance"]["KOSDAQ"]["end"]
    assert "reference_peak" in output["equity"][0] and "lifetime_peak" in output["equity"][0]
    assert output["config"]["exclude_nonpositive_operating_income"] is False
    assert output["config"]["quality_metric"].value == "pbr_per"
    flagged = output["signal_diagnostics"][0]["accounting_flags"]
    assert flagged[0]["ticker"] == "000001" and flagged[0]["eligible"]
    assert any(e["ticker"] == "000001" and e["action"] == "buy" for e in output["trade_log"])
    equity = {p["date"]: p["equity"] for p in output["equity"]}
    assert equity["2020-04-15"] == pytest.approx(equity["2020-04-14"])
