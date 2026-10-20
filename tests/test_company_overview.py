"""Fake transport and cache tests; never contact a data provider."""

from datetime import date
import json
from unittest.mock import Mock

import pytest
import requests

from kr_quant.bulk_cached import AnnualAccounts
from kr_quant.company_overview import (CompanyOverviewDownloader, download_overviews,
    ksic_division, load_current_classifications, output_corporations, save_json, overview_plan)
from scripts.audit_correction_availability import correction_impact


def inputs(tmp_path):
    financials = tmp_path / "annual.json"
    save_json(financials, [
        {"stock_code": "111111", "year": 2015, "filings": [{"receipt_no": "old"}]},
        {"stock_code": "111111", "year": 2016, "filings": [{"receipt_no": "new"}]},
        {"stock_code": "222222", "year": 2015, "filings": [{"receipt_no": "two"}]}])
    disclosures = tmp_path / "list"
    save_json(disclosures / "page.json", {"list": [
        {"stock_code": "111111", "corp_code": "00000001", "rcept_no": "old"},
        {"stock_code": "111111", "corp_code": "00000001", "rcept_no": "new"},
        {"stock_code": "222222", "corp_code": "00000002", "rcept_no": "two"},
        {"stock_code": "333333", "corp_code": "00000003", "rcept_no": "outside-output"}]})
    return financials, disclosures


def test_scope_exact_receipt_join_and_deduplication(tmp_path):
    financials, disclosures = inputs(tmp_path)
    assert output_corporations(financials, disclosures) == {"00000001": {"111111"}, "00000002": {"222222"}}
    saved = json.loads(financials.read_text(encoding="utf-8"))
    saved[0]["filings"][0]["receipt_no"] = "absent"
    save_json(financials, saved)
    with pytest.raises(ValueError, match="Missing/ambiguous corporate identity"):
        output_corporations(financials, disclosures)


def test_cached_resume_budget_shared_by_clients_and_throttling(tmp_path):
    now = [1_800_000_000.0]
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    transport = Mock(return_value=Mock(json=lambda: {"status": "000", "induty_code": "26110"}))
    kwargs = dict(cache_dir=tmp_path, daily_limit=2, clock=lambda: now[0], sleep=sleep, transport=transport)
    first = CompanyOverviewDownloader("test-secret", **kwargs)
    first.fetch("00000001")
    second = CompanyOverviewDownloader("test-secret", **kwargs)
    second.fetch("00000002")
    assert sleeps[-1] == pytest.approx(0.5)
    assert second.fetch("00000001")["response"]["induty_code"] == "26110"
    assert second.cache_hits == 1 and transport.call_count == 2
    with pytest.raises(RuntimeError, match="daily ceiling"):
        second.fetch("00000003")
    assert transport.call_count == 2
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))
    assert "test-secret" not in text
    # Korean calendar day rollover resets the count, preserving cache.
    now[0] += 86400
    second.fetch("00000003")
    assert transport.call_count == 3


@pytest.mark.parametrize("status", ["013", "020", "010"])
def test_provider_responses_cached_no_retry_and_checkpoint(tmp_path, status):
    transport = Mock(return_value=Mock(json=lambda: {"status": status, "message": "secret-url"}))
    downloader = CompanyOverviewDownloader("secret-url", cache_dir=tmp_path, transport=transport, sleep=lambda d: None)
    if status == "013":
        result = download_overviews({"00000001": {"111111"}}, downloader, tmp_path / "progress.json")
        assert result["status"] == "complete" and result["unknown_industry"] == 1
        downloader.fetch("00000001")
    else:
        with pytest.raises(RuntimeError, match=f"status {status}"):
            download_overviews({"00000001": {"111111"}}, downloader, tmp_path / "progress.json")
        assert json.loads((tmp_path / "progress.json").read_text())["status"] == "stopped"
        with pytest.raises(RuntimeError, match=f"status {status}"):
            downloader.fetch("00000001")
    assert transport.call_count == 1
    assert "secret-url" not in (tmp_path / "company" / "00000001.json").read_text()


def test_transport_failure_redacted_counted_and_resume(tmp_path):
    transport = Mock(side_effect=requests.ConnectionError("https://example/?crtfc_key=verysecret"))
    downloader = CompanyOverviewDownloader("verysecret", cache_dir=tmp_path, daily_limit=1, transport=transport)
    with pytest.raises(RuntimeError, match="transport/JSON failure") as caught:
        downloader.fetch("00000001")
    assert "verysecret" not in str(caught.value)
    assert downloader.request_count == 1
    with pytest.raises(RuntimeError, match="daily ceiling"):
        downloader.fetch("00000001")
    assert transport.call_count == 1


@pytest.mark.parametrize("code,division", [("64121", "64"), ("65110", "65"), ("66199", "66"),
    ("26110", "26"), ("01110", "01"), ("", None), (None, None), ("abc", None), ("0", None), ("00000", None)])
def test_ksic_codes(code, division):
    assert ksic_division(code) == division


def test_current_classification_identity_and_missing_codes(tmp_path):
    financials, disclosures = inputs(tmp_path)
    cache = tmp_path / "company_cache"
    for code, ticker, industry in (("00000001", "111111", "66190"), ("00000002", "222222", "")):
        save_json(cache / "company" / f"{code}.json", {
            "corp_code": code, "retrieved_on": "2026-10-18",
            "response": {"status": "000", "stock_code": ticker, "induty_code": industry}})
    result = load_current_classifications(financials, cache, disclosures)
    assert set(result) == {"111111"}
    assert result["111111"]["sector"] == "66" and result["111111"]["is_financial"] is True
    assert result["111111"]["retrieved_on"] == date(2026, 10, 18)
    path = cache / "company" / "00000001.json"
    saved = json.loads(path.read_text())
    saved["response"]["stock_code"] = "999999"
    save_json(path, saved)
    assert load_current_classifications(financials, cache, disclosures) == {}


def test_ambiguous_ticker_ownership_is_not_classified(tmp_path):
    financials, disclosures = inputs(tmp_path)
    path = disclosures / "page.json"
    saved = json.loads(path.read_text())
    saved["list"][1]["corp_code"] = "00000009"
    save_json(path, saved)
    cache = tmp_path / "company_cache"
    save_json(cache / "company" / "00000001.json", {
        "corp_code": "00000001", "retrieved_on": "2026-10-18",
        "response": {"status": "000", "stock_code": "111111", "induty_code": "26110"}})
    assert load_current_classifications(financials, cache, disclosures) == {}


def test_correction_audit_distinguishes_missing_from_older_fallback():
    old = AnnualAccounts(date(2018, 12, 31), date(2019, 3, 20), 10, 100, "CFS", False,
                         receipt_available_on=date(2019, 3, 20))
    corrected = AnnualAccounts(date(2019, 12, 31), date(2020, 3, 20), 20, 200, "CFS", True,
                               receipt_available_on=date(2020, 6, 15))
    funds = {"missing": (corrected,), "fallback": (old, corrected), "zero": (
        AnnualAccounts(date(2019, 12, 31), date(2020, 3, 20), 0, 200, "CFS", True,
                       receipt_available_on=date(2020, 6, 15)),)}
    result = correction_impact(funds, {date(2020, m, 30): set(funds) for m in (3, 4, 6)})
    assert result["affected_company_years"] == 2
    assert result["company_years_with_no_usable_fundamentals"] == 1
    assert result["company_years_with_older_fallback"] == 1
    assert result["company_years_missing_for_at_least_two_month_ends"] == 1
    assert result["stock_months_with_no_usable_fundamentals"] == 2
    assert result["stock_months_with_older_fallback"] == 2
    assert result["months"][-1]["latest_year_delayed"] == 0


def test_cache_retains_response_fields_and_removes_echoed_key(tmp_path):
    transport = Mock(return_value=Mock(json=lambda: {
        "status": "000", "induty_code": "26110", "ceo_nm": "Sample",
        "crtfc_key": "sensitive", "message": "echo sensitive"}))
    client = CompanyOverviewDownloader("sensitive", cache_dir=tmp_path, transport=transport)
    saved = client.fetch("00000001")
    assert saved["response"]["ceo_nm"] == "Sample"
    assert "crtfc_key" not in saved["response"]
    assert saved["response"]["message"] == "echo [REDACTED]"
    assert "sensitive" not in (tmp_path / "company" / "00000001.json").read_text()


def test_resume_download_uses_only_scoped_codes(tmp_path):
    financials, disclosures = inputs(tmp_path)
    calls = []
    def transport(url, params, timeout):
        calls.append(params["corp_code"])
        return Mock(json=lambda: {"status": "000", "induty_code": "26110"})
    client = CompanyOverviewDownloader("secret-key", cache_dir=tmp_path / "cache", transport=transport,
                                       sleep=lambda d: None)
    scope = output_corporations(financials, disclosures)
    progress = tmp_path / "progress.json"
    result = download_overviews(scope, client, progress)
    assert calls == ["00000001", "00000002"]
    assert result["completed"] == 2
    download_overviews(scope, client, progress)
    assert calls == ["00000001", "00000002"] and client.cache_hits == 2


def test_final_output_scope_includes_delisted_explicit_codes_and_fetches_only_missing(tmp_path):
    financials, disclosures = inputs(tmp_path)
    rows = json.loads(financials.read_text(encoding="utf-8"))
    rows += [{"stock_code": "999999", "year": y, "corp_code": "00000009",
              "currently_listed": False, "filings": []} for y in (2015, 2016)]
    save_json(financials, rows)
    scope = output_corporations(financials, disclosures)
    assert scope == {"00000001": {"111111"}, "00000002": {"222222"}, "00000009": {"999999"}}
    cache = tmp_path / "cache"
    for code in ("00000001", "00000002"):
        save_json(cache / "company" / f"{code}.json", {"corp_code": code, "retrieved_on": "2026-10-18",
                  "response": {"status": "000", "induty_code": "26110"}})
    before = {p: p.read_bytes() for p in (cache / "company").glob("*.json")}
    assert overview_plan(scope, cache) == {"total": 3, "cached": 2, "missing": ["00000009"], "maximum_new_calls": 1}
    transport = Mock(return_value=Mock(json=lambda: {"status": "013"}))
    client = CompanyOverviewDownloader("fixture", cache_dir=cache, transport=transport, sleep=lambda _: None)
    state = download_overviews(scope, client, cache / "progress.json")
    assert transport.call_count == 1 and transport.call_args.kwargs["params"]["corp_code"] == "00000009"
    assert state["completed"] == 3 and state["unavailable"] == ["00000009"]
    assert state["outcomes"]["00000009"]["stock_codes"] == ["999999"]
    assert all(p.read_bytes() == data for p, data in before.items())
    download_overviews(scope, client, cache / "progress.json")
    assert transport.call_count == 1  # Cached unavailability is not re-requested.
    assert "999999" not in load_current_classifications(financials, cache, disclosures)


def test_unavailable_and_missing_industry_are_distinct_and_checkpointed(tmp_path):
    responses = iter([{"status": "013"}, {"status": "000", "induty_code": ""}, {"status": "020"}])
    client = CompanyOverviewDownloader("fixture", cache_dir=tmp_path,
        transport=Mock(side_effect=lambda *a, **kw: Mock(json=lambda: next(responses))), sleep=lambda _: None)
    with pytest.raises(RuntimeError, match="status 020"):
        download_overviews({"00000001": {"111111"}, "00000002": {"222222"}, "00000003": {"333333"}},
                           client, tmp_path / "progress.json")
    state = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert state["status"] == "stopped" and state["completed"] == 2
    assert state["unavailable"] == ["00000001"] and state["missing_industry"] == ["00000002"]
    assert state["request_attempts"] == 3


def test_response_corp_identity_mismatch_is_not_classified(tmp_path):
    client = CompanyOverviewDownloader("fixture", cache_dir=tmp_path,
        transport=Mock(return_value=Mock(json=lambda: {"status": "000", "corp_code": "00000099", "induty_code": "26110"})))
    with pytest.raises(ValueError, match="response identity mismatch"):
        client.fetch("00000001")


def test_shared_division_uses_existing_sector_cap():
    from kr_quant.config import BacktestConfig
    from kr_quant.contracts import (Stock, Market, SecurityType, DatedValue, Fundamental,
                                     CloseBar, IndexHistory, Snapshot, ClassificationKind)
    from kr_quant.portfolio import FactorStrategy
    from kr_quant.risk import RiskState
    import pandas as pd

    days = tuple(d.date() for d in pd.bdate_range(end="2020-03-31", periods=253))
    closes = tuple(CloseBar(d, 100) for d in days)
    signal = days[-1]
    stocks = tuple(Stock(f"{i:06d}", Market.KOSPI, "26", SecurityType.COMMON, False,
                         date(2026, 10, 18), DatedValue(1e12, signal), DatedValue(2e9, signal),
                         Fundamental(10, 1, date(2019, 12, 31), date(2020, 3, 20), 1), closes,
                         classification_kind=ClassificationKind.CURRENT_PROXY,
                         classification_source="OpenDART approximate current KSIC",
                         classification_retrieved_on=date(2026, 10, 18)) for i in range(1, 9))
    snapshot = Snapshot(signal, stocks, (IndexHistory(Market.KOSPI, closes),))
    config = BacktestConfig(fundamental_lag_days=0, market_cap_exclusion_fraction=0)
    targets = FactorStrategy(config).targets(snapshot, {}, RiskState(1e6))
    assert len(targets.weights) == 6
    assert sum(targets.weights.values()) == pytest.approx(0.30)


def test_audit_does_not_fall_back_past_a_newer_zero_income_report():
    original = AnnualAccounts(date(2017, 12, 31), date(2018, 3, 20), 10, 100, "CFS", False, date(2018, 3, 20))
    zero = AnnualAccounts(date(2018, 12, 31), date(2019, 3, 20), 0, 100, "CFS", False, date(2019, 3, 20))
    corrected = AnnualAccounts(date(2019, 12, 31), date(2020, 3, 20), 10, 100, "CFS", True, date(2020, 6, 20))
    result = correction_impact({"one": (original, zero, corrected)}, {date(2020, 3, 31): {"one"}})
    assert result["company_years_with_no_usable_fundamentals"] == 1
    assert result["company_years_with_older_fallback"] == 0
