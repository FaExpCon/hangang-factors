"""Offline evidence for historical coverage, conservative identity and resume."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from kr_quant.dart_gap import (
    GapClient, atomic_json, audit_gap, build_record, common_stock, company_filings,
    download_gap, inventory, mapping_for, merge_records, parse_corp_xml, read_json,
    single_company_accounts,
)


def basic(code="000001", name="테스트", **fields):
    return {"ISU_SRT_CD": code, "ISU_ABBRV": name, "ISU_NM": "주식회사 " + name,
            "KIND_STKCERT_TP_NM": "보통주", "SECUGRP_NM": "주권", "SECT_TP_NM": "", **fields}


def company(code="000001", corp="00000001", **fields):
    return {"stock_code": code, "names": ["테스트"], "markets": ["KOSDAQ"],
            "existing_years": [], "missing_years": [2015], "active_years": [2015],
            "mapping": {"status": "confident", "corp_code": corp, "direct_candidates": [corp],
                        "method": "stock_code"}, **fields}


def filing(corp="00000001", year=2015, date="20160301", stock="", correction="", **fields):
    return {"corp_code": corp, "stock_code": stock, "corp_name": "테스트",
            "report_nm": f"{correction}사업보고서 ({year}.12)", "rcept_dt": date,
            "rcept_no": date + "000001", **fields}


def account(code="000001", corp=None, **fields):
    row = {"stock_code": code, "bsns_year": "2015", "account_nm": "자본총계",
           "fs_div": "CFS", "sj_div": "BS", "thstrm_amount": "123",
           "thstrm_dt": "2015.12.31 현재", **fields}
    if corp is not None:
        row["corp_code"] = corp
    return row


def test_full_xml_preserves_empty_stock_codes():
    rows = parse_corp_xml(b"<result><list><corp_code>00000001</corp_code><corp_name>A</corp_name>"
                          b"<stock_code> </stock_code></list><list><corp_code>00000002</corp_code>"
                          b"<corp_name>B</corp_name><stock_code>000001</stock_code></list></result>")
    assert len(rows) == 2 and rows[0]["stock_code"] == ""
    assert rows[1]["stock_code"] == "000001"


@pytest.mark.parametrize("fields", [{"KIND_STKCERT_TP_NM": "구형우선주"},
    {"SECUGRP_NM": "부동산투자회사"}, {"ISU_ABBRV": "테스트스팩"}, {"SECT_TP_NM": "SPAC"}])
def test_common_classifier_negative_and_positive_controls(fields):
    assert common_stock(basic())
    assert not common_stock(basic(**fields))


def test_mapping_conflicts_and_unique_names_are_not_auto_accepted():
    rows = [{"corp_code": "00000001", "stock_code": "000001", "corp_name": "테스트"},
            {"corp_code": "00000002", "stock_code": "", "corp_name": "동명이인"}]
    assert mapping_for("000001", ["주식회사 테스트"], rows, [])["name_agrees"]
    assert mapping_for("000001", [], rows, [])["status"] == "confident"
    conflict = mapping_for("000001", [], rows, [filing(corp="00000003", stock="000001")])
    assert conflict["status"] == "ambiguous" and conflict["corp_code"] is None
    name_only = mapping_for("000002", ["동명이인"], rows, [])
    assert name_only["status"] == "ambiguous" and name_only["name_candidates"] == ["00000002"]
    assert mapping_for("000003", ["미상"], rows, [])["status"] == "unmapped"


def seed_inventory(tmp_path):
    krx = tmp_path / "krx"
    atomic_json(krx / "stk_isu_base_info/20150130.json", {"OutBlock_1": [basic(), basic("000002", "기존")]})
    atomic_json(krx / "stk_isu_base_info/20160129.json", {"OutBlock_1": [basic(), basic("000002", "기존")]})
    atomic_json(krx / "stk_isu_base_info/20140131.json", {"OutBlock_1": [basic("000009")]})
    atomic_json(krx / "stk_bydd_trd/20150102.json", {"OutBlock_1": [
        {"ISU_CD": "000001", "ISU_NM": "구상호"}, {"ISU_CD": "000003", "ISU_NM": "분류미상"}]})
    atomic_json(krx / "ksq_bydd_trd/20170102.json", {"OutBlock_1": [{"ISU_CD": "000001", "ISU_NM": "새상호"}]})
    return krx


def test_inventory_unions_daily_data_and_reports_unclassified(tmp_path):
    universe, source = inventory(seed_inventory(tmp_path))
    assert set(universe) == {(2015, "000001"), (2015, "000002"), (2016, "000001"),
                             (2016, "000002"), (2017, "000001")}
    assert "구상호" in universe[(2015, "000001")]["names"]
    assert source["daily_added_company_years"] == 1
    assert source["unclassified_daily"] == [{"year": 2015, "stock_code": "000003", "names": ["분류미상"]}]


def test_audit_yearly_vs_never_gap_and_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", Mock(side_effect=AssertionError("network forbidden")))
    krx = seed_inventory(tmp_path)
    dart = tmp_path / "dart"
    atomic_json(dart / "corpCode.json", {"000001": {"stock_code": "000001", "corp_code": "00000001", "corp_name": "테스트"},
                                          "000002": {"stock_code": "000002", "corp_code": "00000002", "corp_name": "기존"}})
    output = tmp_path / "annual.json"
    atomic_json(output, [{"stock_code": "000002", "year": 2015}])
    before = output.read_bytes()
    result = audit_gap(krx, dart, output, tmp_path / "audit.json")
    assert result["yearly"][0] == {"year": 2015, "common_stocks": 2, "missing_annual": 1, "no_records_any_year": 1}
    assert result["yearly"][1]["missing_annual"] == 2
    assert result["no_records_any_year"] == 1
    assert result["mapping_counts"] == {"confident": 2}
    assert result["requests"]["all_missing_company_years"] == 19
    assert result["requests"]["base_requests"] == 12
    assert result["requests"]["requests_with_all_fallbacks_upper_bound"] == 31
    assert result["companies"][1]["existing_years"] == [2015]
    assert output.read_bytes() == before
    requests.get.assert_not_called()
    from scripts.audit_dart_gap import write_inventory
    write_inventory(result, tmp_path / "inventory.md")
    assert "00000001" in (tmp_path / "inventory.md").read_text(encoding="utf-8")


def test_disclosure_pagination_omits_market_filter_and_preserves_versions():
    client = Mock()
    client.request.side_effect = [
        {"total_page": 2, "list": [filing()]},
        {"total_page": 2, "list": [filing(date="20170401", correction="[기재정정]")]},
    ]
    result = company_filings(client, "00000001", "20261020")
    assert len(result) == 2
    for call in client.request.call_args_list:
        params = call.args[1]
        assert "corp_cls" not in params and params["last_reprt_at"] == "N"
        assert params["bgn_de"] == "20150101" and params["end_de"] == "20261020"
    assert client.request.call_args_list[-1].args[1]["page_no"] == "2"


def test_empty_disclosures_are_valid_but_wrong_corporation_rejected():
    client = Mock()
    client.request.return_value = {"status": "013"}
    assert company_filings(client, "00000001", "20261020") == []
    client.request.return_value = {"list": [filing(corp="00000002")]}
    with pytest.raises(ValueError, match="mismatch"):
        company_filings(client, "00000001", "20261020")


def test_build_joins_blank_stock_by_corp_and_latest_correction():
    rows = [filing(), filing(date="20170401", correction="[기재정정]")]
    record = build_record(company(), 2015, rows, [account(code="", corp="00000001"), account("999999", corp="99999999")])
    assert record["filing_date"] == "20160301"
    assert record["filings"][0]["receipt_no"].startswith("20170401")
    assert record["filings"][0]["had_correction"]
    assert record["accounts"]["자본총계_CFS_BS"]["amount"] == "123"
    assert build_record(company(), 2016, rows, []) is None


def test_account_stock_fallback_and_no_data_outcome():
    record = build_record(company(), 2015, [filing()], [account()])
    assert record["gap_fill"]["accounts_status"] == "available"
    assert build_record(company(), 2015, [filing()], [])["gap_fill"]["accounts_status"] == "no_accounts_returned"
    with pytest.raises(ValueError, match="stock-code mismatch"):
        build_record(company(), 2015, [filing()], [account("999999", corp="00000001")])


def test_multiple_fiscal_periods_are_preserved_not_arbitrarily_picked():
    rows = [filing(year=2021, date="20210917", report_nm="사업보고서 (2021.06)"),
            filing(year=2021, date="20220323", report_nm="사업보고서 (2021.12)")]
    accounts = [
        account(bsns_year="2021", thstrm_dt="2021.07.01 ~ 2021.12.31"),
        account(bsns_year="2021", thstrm_dt="2021.06.30 현재"),
    ]
    record = build_record(company(), 2021, rows, accounts)
    assert len(record["fiscal_periods"]) == 2
    assert [(p["start"], p["end"], p["months"]) for p in record["fiscal_periods"]] == [
        (None, "2021-06-30", None), ("2021-07-01", "2021-12-31", 6)]
    assert record["fiscal_periods"][0]["is_full_year"] is None
    assert record["fiscal_periods"][1]["is_full_year"] is False
    assert [r["report_name"] for r in record["filings"]] == ["사업보고서 (2021.06)", "사업보고서 (2021.12)"]
    assert record["filings"][1]["filing_date"] == "20220323"


def test_merge_preserves_objects_backup_and_idempotence(tmp_path):
    path = tmp_path / "annual.json"
    original = [{"stock_code": "000001", "year": 2015, "accounts": {"user": "preserve"}}]
    atomic_json(path, original)
    raw = path.read_bytes()
    assert merge_records(path, [{"stock_code": "000001", "year": 2015, "accounts": {}},
                                {"stock_code": "000002", "year": 2015}]) == 1
    assert read_json(path)[0] == original[0]
    assert next((tmp_path / "gap_backups").glob("*.json")).read_bytes() == raw
    merged = path.read_bytes()
    assert merge_records(path, read_json(path)) == 0 and path.read_bytes() == merged
    merge_records(path, [{"stock_code": "000003", "year": 2016}])
    assert len(list((tmp_path / "gap_backups").glob("*.json"))) == 1
    assert next((tmp_path / "gap_backups").glob("*.json")).read_bytes() == raw


def test_attempts_persist_before_failure_and_secret_url_is_hidden(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DART_API_KEY=secret_fixture", encoding="utf-8")
    mock = Mock(side_effect=requests.ConnectionError("URL?crtfc_key=secret_fixture"))
    monkeypatch.setattr(requests, "get", mock)
    client = GapClient(tmp_path / "cache", daily_limit=1, env_path=env)
    with pytest.raises(RuntimeError, match="transport failure") as error:
        client.request("list", {})
    assert "secret_fixture" not in str(error.value)
    assert read_json(next((tmp_path / "cache/gap_requests").glob("*.json")))["attempts"] == 1
    with pytest.raises(RuntimeError, match="ceiling"):
        GapClient(tmp_path / "cache", daily_limit=1, env_path=env).request("list", {})
    assert mock.call_count == 1


def test_cache_reuse_no_data_provider_stop_and_budget(tmp_path, monkeypatch):
    client = GapClient(tmp_path, env_path=tmp_path / ".env")
    client.env_path.write_text("DART_API_KEY=fixture", encoding="utf-8")
    mock = Mock()
    mock.return_value.json.return_value = {"status": "013"}
    monkeypatch.setattr(requests, "get", mock)
    assert client.request("list", {})["status"] == "013"
    assert client.request("list", {})["status"] == "013"
    assert mock.call_count == 1
    monkeypatch.setattr("kr_quant.dart_gap.time.sleep", lambda _: None)
    mock.return_value.json.return_value = {"status": "020"}
    with pytest.raises(RuntimeError, match="020"):
        client.request("list", {"page_no": "2"})
    assert not client.cache_path("list", {"page_no": "2"}).exists()
    assert list((tmp_path / "gap_errors").glob("*.json"))
    with pytest.raises(ValueError):
        GapClient(tmp_path, daily_limit=1001)
    with pytest.raises(ValueError):
        GapClient(tmp_path, min_delay=0.5)


def test_throttle_uses_prior_day_and_cache_matches_legacy(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from kr_quant.opendart import OpenDartClient
    client = GapClient(tmp_path)
    now = datetime.now(timezone(timedelta(hours=9)))
    atomic_json(tmp_path / "gap_requests" / f"{now-timedelta(days=1):%Y%m%d}.json",
                {"attempts": 1, "last_epoch": 99.5})
    monkeypatch.setattr("kr_quant.dart_gap.time.time", lambda: 100.0)
    sleep = Mock()
    monkeypatch.setattr("kr_quant.dart_gap.time.sleep", sleep)
    client._reserve()
    sleep.assert_called_once_with(0.5)
    params = {"bsns_year": "2015", "corp_code": "00000001", "reprt_code": "11011"}
    assert client.cache_path("fnlttMultiAcnt", params) == OpenDartClient(tmp_path, api_key="fixture")._cache_path("fnlttMultiAcnt", params)


def test_downloader_resume_stable_batches_and_never_overwrites(tmp_path, monkeypatch):
    client = GapClient(tmp_path / "cache")
    calls = []
    fail = [True]

    def response(endpoint, params):
        calls.append((endpoint, params.copy()))
        if endpoint == "list":
            return {"status": "000", "list": [filing(stock="000001")], "total_page": "1"}
        if params["bsns_year"] == "2016" and fail[0]:
            fail[0] = False
            raise RuntimeError("interrupt")
        return {"status": "000", "list": [account()] if params["bsns_year"] == "2015" else []}

    monkeypatch.setattr(client, "request", response)
    output = tmp_path / "annual.json"
    original = {"stock_code": "999999", "year": 2015, "user": "unchanged"}
    atomic_json(output, [original])
    plan = {"companies": [company(), company("000002", "00000002", mapping={"status": "ambiguous"})]}
    with pytest.raises(RuntimeError):
        download_gap(client, plan, output, tmp_path / "work", end_date="20261020", progress=lambda _: None)
    assert read_json(output)[0] == original and len(read_json(output)) == 2
    assert read_json(tmp_path / "work/progress.json")["status"] == "paused"
    first_params = calls[1]
    result = download_gap(client, plan, output, tmp_path / "work", end_date="20261021", progress=lambda _: None)
    assert result["status"] == "complete" and result["added_this_run"] == 0
    assert result["receipt_cutoff"] == "20261020"
    assert calls[4] == first_params  # list, 2015, failure 2016, resumed list, same 2015
    assert len(read_json(output)) == 2
    assert len(result["outcomes"]) == 10
    with pytest.raises(ValueError, match="Audit changed"):
        download_gap(client, {**plan, "changed": True}, output, tmp_path / "work", progress=lambda _: None)


def test_downloader_accounts_batch_maximum(tmp_path):
    companies = [company(f"{i:06}", f"{i:08}", existing_years=list(range(2016, 2025))) for i in range(1, 102)]
    client = Mock(request_count=0)
    sizes = []
    def response(endpoint, params):
        if endpoint == "list":
            return {"status": "013"}
        sizes.append(len(params["corp_code"].split(",")))
        return {"status": "013"}
    client.request.side_effect = response
    output = tmp_path / "annual.json"
    atomic_json(output, [])
    result = download_gap(client, {"companies": companies}, output, tmp_path / "work", progress=lambda _: None)
    assert sizes == [100, 1] and result["added_this_run"] == 0


def test_single_company_fallback_resolves_completely_blank_account_identity(tmp_path):
    client = Mock(request_count=0)
    def response(endpoint, params):
        if endpoint == "list":
            return {"list": [filing()], "total_page": 1}
        if endpoint == "fnlttMultiAcnt":
            return {"list": [account(code="")]}
        assert endpoint == "fnlttSinglAcnt" and params["corp_code"] == "00000001"
        return {"list": [account(code="")]}
    client.request.side_effect = response
    output = tmp_path / "annual.json"
    atomic_json(output, [])
    download_gap(client, {"companies": [company(existing_years=list(range(2016, 2025)))]},
                 output, tmp_path / "work", progress=lambda _: None)
    record = read_json(output)[0]
    assert record["accounts"]["자본총계_CFS_BS"]["amount"] == "123"
    assert record["gap_fill"]["used_single_company_fallback"]
    assert client.request.call_count == 3


def test_single_company_fallback_rejects_wrong_corporation():
    client = Mock()
    client.request.return_value = {"list": [account(corp="99999999")]}
    with pytest.raises(ValueError, match="corporation mismatch"):
        single_company_accounts(client, "00000001", 2015)


def test_full_corp_fetch_cached_without_repeating_request(tmp_path, monkeypatch):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", "<result><list><corp_code>00000001</corp_code><corp_name>A</corp_name><stock_code> </stock_code></list></result>")
    response = Mock(content=buffer.getvalue())
    client = GapClient(tmp_path)
    get = Mock(return_value=response)
    monkeypatch.setattr(client, "_get", get)
    result = client.corp_master()
    assert result["rows"][0]["stock_code"] == ""
    assert (tmp_path / "CORPCODE.xml").exists()
    assert client.corp_master() == result
    get.assert_called_once_with("corpCode.xml", {})


def test_actual_period_routes_different_dart_label_and_retains_label():
    rows = [filing(year=2022, report_nm="사업보고서 (2022.06)", date="20220920")]
    record = build_record(company(), 2022, rows, [account(bsns_year="2021",
        thstrm_dt="2021.07.01 ~ 2022.06.30")])
    p = record["fiscal_periods"][0]
    assert (p["start"], p["end"], p["months"]) == ("2021-07-01", "2022-06-30", 12)
    assert p["dart_bsns_years"] == ["2021"]
    assert p["first_filing_date"] == "20220920"
    assert record["accounts"]["자본총계_CFS_BS"]["dart_bsns_years"] == ["2021"]


def test_later_period_never_assigned_to_requested_year():
    accounts = [account(bsns_year=y, thstrm_dt="2025.01.01 ~ 2025.12.31") for y in ("2024", "2025")]
    filings = [filing(year=2024, date="20250324"), filing(year=2025, date="20260324")]
    wrong = build_record(company(), 2024, filings, accounts)
    assert wrong["accounts"] == {}
    assert wrong["gap_fill"]["accounts_status"] == "account_period_mismatch"
    assert len(wrong["gap_fill"]["unassigned_periods"]) == 2
    right = build_record(company(), 2025, filings, accounts)
    assert right["fiscal_periods"][0]["months"] == 12
    assert right["fiscal_periods"][0]["dart_bsns_years"] == ["2024", "2025"]
    assert len(right["accounts"]) == 1  # Duplicate labels do not duplicate a period/account.


def test_merge_rejects_period_under_second_year(tmp_path):
    output = tmp_path / "annual.json"
    p = {"end": "2022-06-30"}
    atomic_json(output, [{"stock_code": "000001", "year": 2022, "fiscal_periods": [p]}])
    with pytest.raises(ValueError, match="Duplicate fiscal period"):
        merge_records(output, [{"stock_code": "000001", "year": 2021, "fiscal_periods": [p]}])


def test_cache_only_uses_real_files_and_never_fabricates_or_attempts(tmp_path, monkeypatch):
    client = GapClient(tmp_path / "cache", cache_only=True)
    mock = Mock(side_effect=AssertionError("Network forbidden"))
    monkeypatch.setattr(requests, "get", mock)
    with pytest.raises(FileNotFoundError, match="Uncached"):
        client.request("fnlttMultiAcnt", {"bsns_year": "2024"})
    atomic_json(client.cache_path("list", {}), {"status": "013"})
    assert client.request("list", {}) == {"status": "013"}
    assert len(client.responses_used) == 1
    assert client.request_count == 0
    mock.assert_not_called()
    assert not (client.cache_dir / "gap_requests").exists()


def test_cache_only_download_preserves_pending_not_fake_no_data(tmp_path):
    client = GapClient(tmp_path / "cache", cache_only=True)
    params = {"corp_code": "00000001", "bgn_de": "20150101", "end_de": "20261020",
              "pblntf_detail_ty": "A001", "page_no": "1", "page_count": "100", "last_reprt_at": "N"}
    atomic_json(client.cache_path("list", params), {"status": "000", "list": [filing()]})
    output = tmp_path / "annual.json"
    atomic_json(output, [])
    state = download_gap(client, {"companies": [company(existing_years=list(range(2016,2025)))]},
                         output, tmp_path / "work", end_date="20261020", progress=lambda _: None)
    assert state["status"] == "pending_cache" and len(state["pending_requests"]) == 1
    assert state["added_this_run"] == 0 and read_json(output) == []


def test_cache_replay_records_provenance_and_verifier_rejects_synthetic(tmp_path):
    import hashlib
    from scripts.verify_dart_recovery import verify
    cache = tmp_path / "cache"
    client = GapClient(cache, cache_only=True)
    pars = {"corp_code": "00000001", "bgn_de": "20150101", "end_de": "20261020",
            "pblntf_detail_ty": "A001", "page_no": "1", "page_count": "100", "last_reprt_at": "N"}
    atomic_json(client.cache_path("list", pars), {"status": "000", "list": [filing()]})
    atomic_json(client.cache_path("fnlttMultiAcnt", {"corp_code": "00000001", "bsns_year": "2015",
                "reprt_code": "11011"}), {"status": "000", "list": [account()]})
    plan = {"companies": [company(existing_years=list(range(2016,2025)))]}
    plan_path = tmp_path / "plan.json"; atomic_json(plan_path, plan)
    baseline = tmp_path / "baseline.json"; atomic_json(baseline, [])
    output = tmp_path / "annual.json"; atomic_json(output, [])
    digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
    state = download_gap(client, plan, output, tmp_path / "work", end_date="20261020",
                         progress=lambda _: None, merge_at_end=True)
    assert state["added_this_run"] == 1 and state["status"] == "complete"
    assert verify(output, baseline, plan_path, expected_baseline_sha=digest)["cache_verified_additions"] == 1
    saved = read_json(output)
    saved[0]["accounts"]["자본총계_CFS_BS"]["amount"] = "synthetic"
    atomic_json(output, saved)
    with pytest.raises(AssertionError, match="not reproduced"):
        verify(output, baseline, plan_path, expected_baseline_sha=digest)


def test_identity_review_holds_never_reach_requests_or_estimates(tmp_path):
    from kr_quant.dart_gap import estimate_requests
    allowed = company(existing_years=list(range(2016, 2025)))
    held = company("000002", "00000002", identity_review={"download_allowed": False})
    rejected = company("000003", "00000003", identity_review={
        "status": "different_company", "download_allowed": False})
    budget = estimate_requests([allowed, held, rejected], set())
    assert budget["mapped_companies"] == 1 and budget["all_missing_company_years"] == 10
    client = Mock(request_count=0)
    def response(endpoint, params):
        assert params["corp_code"] == "00000001"
        return {"status": "013"}
    client.request.side_effect = response
    output = tmp_path / "annual.json"
    atomic_json(output, [])
    result = download_gap(client, {"companies": [allowed, held, rejected]}, output,
                          tmp_path / "work", progress=lambda _: None)
    assert result["filing_companies_total"] == 1
    assert client.request.call_count == 2


def test_identity_classification_requires_independent_name_bridge():
    from scripts.review_dart_tickers import classify
    history = [{"short_name": "테스트", "full_name": "테스트보통주", "english_name": "TEST Co.,Ltd.",
                "isin": "KR7000001000", "listing_date": "20000101"}]
    corp = {"corp_code": "00000001", "corp_name": "다른이름", "corp_eng_name": "OTHER INC.", "modify_date": "20261020"}
    result = classify(company(), corp, history, [])
    assert result["status"] == "unresolved_possible_name_change" and not result["download_allowed"]
    # Same stock code alone is deliberately insufficient; a same-corp historical
    # disclosure with the old name supplies an independent bridge.
    result = classify(company(), corp, history, [filing(stock="000001")])
    assert result["status"] == "name_change_disclosure_bridge" and result["download_allowed"]
    result = classify(company(), {**corp, "corp_eng_name": "TEST Company Limited"}, history, [])
    assert result["status"] == "name_change_english_bridge" and result["download_allowed"]
    result = classify(company(), {**corp, "corp_name": "테스트"}, history, [])
    assert result["status"] == "historical_alias_match" and result["download_allowed"]


def test_identity_spelling_equivalence_and_changed_isin_control():
    from scripts.review_dart_tickers import classify
    history = [{"short_name": "IHQ", "full_name": "IHQ보통주", "english_name": "IHQ",
                "isin": "KR7000001000", "listing_date": "20000101"}]
    corp = {"corp_code": "00000001", "corp_name": "아이에이치큐", "corp_eng_name": "IHQ, INC.", "modify_date": "20261020"}
    result = classify(company(names=["IHQ"]), corp, history, [])
    assert result["status"] == "spelling_or_share_label" and result["download_allowed"]
    result = classify(company(names=["IHQ"]), corp, history + [{**history[0], "isin": "KR7000002000"}], [])
    assert result["status"] == "identity_change_requires_review" and not result["download_allowed"]


def test_review_produces_complete_mismatches_and_recomputed_plan(tmp_path):
    from scripts.review_dart_tickers import review, write_review
    original = {"companies": [company(), company("000002", "00000002", names=["구이름"])], "requests": {"base_requests": 99}}
    master = {"rows": [
        {"corp_code": "00000001", "corp_name": "테스트", "corp_eng_name": "Test", "stock_code": "000001", "modify_date": "20261020"},
        {"corp_code": "00000002", "corp_name": "새이름", "corp_eng_name": "NEW", "stock_code": "000002", "modify_date": "20261020"}]}
    history = {code: [{"short_name": name, "full_name": name+"보통주", "english_name": name,
                      "isin": "KR7"+code, "listing_date": "20000101", "first": "20150130", "last": "20241230"}]
               for code, name in [("000001", "테스트"), ("000002", "구이름")]}
    result = review(original, master, history, [], [])
    assert "identity_review" not in original["companies"][0]
    assert result["ticker_identity_review"]["checked"] == 2
    assert result["ticker_identity_review"]["strict_mismatches"] == 1
    assert result["ticker_identity_review"]["held"] == ["000002"]
    assert result["requests"]["mapped_companies"] == 1
    path = tmp_path / "review.md"
    write_review(result, path)
    assert "00000002 / 새이름" in path.read_text(encoding="utf-8")
    assert "구이름" in path.read_text(encoding="utf-8")
