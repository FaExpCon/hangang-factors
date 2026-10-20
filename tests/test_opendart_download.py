"""Offline regression tests for cached annual report download behavior."""

from unittest.mock import Mock, patch

import pytest

from kr_quant.opendart import OpenDartClient, download_annual_financials
from scripts.download_full_annual_data import listing_snapshot


def test_013_is_cached_and_recorded(tmp_path):
    client = OpenDartClient(cache_dir=tmp_path, api_key="test")
    response = Mock()
    response.json.return_value = {"status": "013", "message": "No data"}
    response.raise_for_status.return_value = None
    with patch("kr_quant.opendart.requests.get", return_value=response) as get:
        assert client.get_multi_company_accounts(["00000001"], 2015) == []
        assert client.get_multi_company_accounts(["00000001"], 2015) == []
    assert get.call_count == 1
    assert len(client.no_data_account_batches) == 2  # Includes cached empty result.
    assert list((tmp_path / "fnlttMultiAcnt").glob("*.json"))


def test_non_013_failure_is_not_cached(tmp_path):
    client = OpenDartClient(cache_dir=tmp_path, api_key="test")
    response = Mock()
    response.json.return_value = {"status": "020", "message": "limit"}
    with patch("kr_quant.opendart.requests.get", return_value=response):
        with pytest.raises(ValueError, match="020"):
            client.get_multi_company_accounts(["00000001"], 2015)
    assert not list((tmp_path / "fnlttMultiAcnt").glob("*.json"))


def test_disclosure_audit_includes_non_december_and_unparseable(tmp_path):
    client = OpenDartClient(cache_dir=tmp_path, api_key="test")

    def fake_request(endpoint, params):
        if params["bgn_de"] != "20150101":
            return {"list": [], "total_page": 1}
        return {"total_page": 1, "list": [
            {"corp_code": "1", "stock_code": "000001", "report_nm": "사업보고서 (2015.03)", "rcept_dt": "20150501"},
            {"corp_code": "2", "stock_code": "000002", "report_nm": "사업보고서 (2015.12)", "rcept_dt": "20160301"},
            {"corp_code": "3", "stock_code": "000003", "report_nm": "사업보고서 (별도)", "rcept_dt": "20160501"},
        ]}

    with patch.object(client, "_request", side_effect=fake_request):
        results, stats = client.get_annual_report_disclosures([2015])
    assert len(results[2015]) == 2
    assert stats["non_december_filings"] == 1
    assert stats["skipped_unparseable"] == 1
    assert stats["skipped_unparseable_names"] == [("사업보고서 (별도)", 1)]


def test_filers_survive_missing_accounts_and_listing_status_unknown(tmp_path, capsys):
    client = OpenDartClient(cache_dir=tmp_path, api_key="test")
    client.get_corp_codes = Mock(return_value={"000001": {"corp_name": "Historical"}})
    filing = {"corp_code": "1", "stock_code": "000001", "corp_name": "Historical",
              "report_nm": "사업보고서 (2015.12)", "rcept_no": "123", "filing_date_approx": "20160301"}
    stats = {"total_company_years": 1, "corrections": 0, "correction_rate": 0,
             "raw_filings": 1, "skipped_unparseable": 0, "skipped_missing_corp_code": 0,
             "outside_requested_years": 0, "non_december_filings": 0,
             "skipped_unparseable_names": []}
    client.get_annual_report_disclosures = Mock(side_effect=[({2015: [filing]}, stats), ({2015: []}, stats)])
    client.get_multi_company_accounts = Mock(return_value=[])
    summary = download_annual_financials(client, [2015], tmp_path)
    import json
    with open(summary["output_file"], encoding="utf-8") as file:
        records = json.load(file)
    assert len(records) == 1
    assert records[0]["accounts"] == {}
    assert records[0]["currently_listed"] is None
    assert "unknown" in capsys.readouterr().out


def test_disclosure_stock_code_is_used_when_corp_map_has_no_entry(tmp_path):
    client = OpenDartClient(cache_dir=tmp_path, api_key="test")
    client.get_corp_codes = Mock(return_value={})
    filing = {"corp_code": "00000001", "stock_code": "000001", "corp_name": "Former filer",
              "report_nm": "사업보고서 (2015.12)", "rcept_no": "123", "filing_date_approx": "20160301"}
    stats = {"total_company_years": 1, "corrections": 0, "correction_rate": 0,
             "raw_filings": 1, "skipped_unparseable": 0, "skipped_missing_corp_code": 0,
             "outside_requested_years": 0, "non_december_filings": 0,
             "skipped_unparseable_names": []}
    client.get_annual_report_disclosures = Mock(side_effect=[({2015: [filing]}, stats), ({2015: []}, stats)])
    client.get_multi_company_accounts = Mock(return_value=[])

    summary = download_annual_financials(client, [2015], tmp_path, current_listing_codes=set())
    import json
    with open(summary["output_file"], encoding="utf-8") as file:
        records = json.load(file)

    assert len(records) == 1
    assert records[0]["stock_code"] == "000001"
    assert records[0]["currently_listed"] is False
    assert records[0]["in_corp_code_map"] is False
    client.get_multi_company_accounts.assert_called_once_with(["00000001"], 2015)


def test_fy2014_rejected_before_disclosure_calls(tmp_path):
    client = OpenDartClient(cache_dir=tmp_path, api_key="test")
    with patch.object(client, "get_annual_report_disclosures") as disclosures:
        with pytest.raises(ValueError, match="FY2015"):
            download_annual_financials(client, [2014, 2015], tmp_path)
    disclosures.assert_not_called()


def test_listing_snapshot_uses_krx_short_codes_and_caches(tmp_path):
    from datetime import date

    mock_client = Mock()
    mock_client.get.side_effect = [
        {"OutBlock_1": [{"ISU_CD": "KR7000001000", "ISU_SRT_CD": "000001"}]},
        {"OutBlock_1": [{"ISU_CD": "KR7000002000", "ISU_SRT_CD": "000002"}]},
    ]
    with patch("scripts.download_full_annual_data.KRXClient", return_value=mock_client) as factory:
        first = listing_snapshot(date(2024, 3, 4), tmp_path)
        second = listing_snapshot(date(2024, 3, 4), tmp_path)
    assert first == second == {"000001", "000002"}
    assert mock_client.get.call_count == 2
    factory.assert_called_once_with(daily_limit=2)
