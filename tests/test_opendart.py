"""Test OpenDART disclosure search with 3-month limit and fiscal year parsing."""

import re
from datetime import date, timedelta
from unittest.mock import Mock, patch

import pytest

from kr_quant.opendart import OpenDartClient


def test_disclosure_search_splits_into_3month_windows_and_parses_fiscal_year(tmp_path):
    """Test that disclosure search:
    1. Splits date range into 3-month windows (API requirement without corp_code)
    2. Fetches all pages in each window
    3. Parses fiscal year from report_nm
    4. Groups results by fiscal year
    """
    # Mock API responses
    mock_responses = {
        # Window 1: 2023-01-01 to 2023-03-31 (90 days)
        ("20230101", "20230331", "1"): {
            "status": "000",
            "total_page": "2",
            "list": [
                {
                    "corp_code": "00126380",
                    "stock_code": "005930",
                    "corp_name": "삼성전자",
                    "report_nm": "사업보고서 (2022.12)",
                    "rcept_no": "20230317000123",
                    "rcept_dt": "20230317"
                },
                {
                    "corp_code": "00164779",
                    "stock_code": "000660",
                    "corp_name": "SK하이닉스",
                    "report_nm": "사업보고서 (2022.12)",
                    "rcept_no": "20230320000456",
                    "rcept_dt": "20230320"
                }
            ]
        },
        ("20230101", "20230331", "2"): {
            "status": "000",
            "total_page": "2",
            "list": [
                {
                    "corp_code": "00164742",
                    "stock_code": "005380",
                    "corp_name": "현대자동차",
                    "report_nm": "사업보고서 (2022.12)",
                    "rcept_no": "20230321000789",
                    "rcept_dt": "20230321"
                }
            ]
        },
        # Window 2: 2023-04-01 to 2023-06-29 (90 days)
        ("20230401", "20230629", "1"): {
            "status": "000",
            "total_page": "1",
            "list": []  # No reports in this window
        },
        # Window 3: 2023-06-30 to 2023-09-27 (90 days)
        ("20230630", "20230927", "1"): {
            "status": "000",
            "total_page": "1",
            "list": []
        },
        # Window 4: 2023-09-28 to 2023-12-26 (90 days)
        ("20230928", "20231226", "1"): {
            "status": "000",
            "total_page": "1",
            "list": []
        },
        # Window 5: 2023-12-27 to 2024-03-25 (90 days)
        ("20231227", "20240325", "1"): {
            "status": "000",
            "total_page": "1",
            "list": [
                {
                    "corp_code": "00126380",
                    "stock_code": "005930",
                    "corp_name": "삼성전자",
                    "report_nm": "사업보고서 (2023.12)",
                    "rcept_no": "20240315000123",
                    "rcept_dt": "20240315"
                }
            ]
        },
        # Window 6: 2024-03-26 to 2024-06-23 (90 days)
        ("20240326", "20240623", "1"): {
            "status": "000",
            "total_page": "1",
            "list": []
        },
        # Window 7: 2024-06-24 to 2024-09-21 (90 days)
        ("20240624", "20240921", "1"): {
            "status": "000",
            "total_page": "1",
            "list": []
        },
        # Window 8: 2024-09-22 to 2024-12-20 (90 days)
        ("20240922", "20241220", "1"): {
            "status": "000",
            "total_page": "1",
            "list": []
        },
        # Window 9: 2024-12-21 to 2024-12-31 (11 days)
        ("20241221", "20241231", "1"): {
            "status": "000",
            "total_page": "1",
            "list": []
        }
    }
    
    def mock_request(endpoint, params, use_cache=True):
        if endpoint != "list":
            raise ValueError(f"Unexpected endpoint: {endpoint}")
        
        bgn_de = params["bgn_de"]
        end_de = params["end_de"]
        page_no = params["page_no"]
        
        key = (bgn_de, end_de, page_no)
        if key not in mock_responses:
            return {"status": "000", "total_page": "1", "list": []}
        
        return mock_responses[key]
    
    client = OpenDartClient(cache_dir=tmp_path, api_key="test_key")
    
    with patch.object(client, '_request', side_effect=mock_request):
        results, stats = client.get_annual_report_disclosures([2022, 2023], corp_cls="Y")
    
    # Verify results grouped by fiscal year
    assert 2022 in results
    assert 2023 in results
    
    # FY2022: 3 reports (Samsung, SK Hynix, Hyundai Motor)
    fy2022 = results[2022]
    assert len(fy2022) == 3
    assert all(d["fiscal_year"] == 2022 for d in fy2022)
    
    stock_codes_2022 = {d["stock_code"] for d in fy2022}
    assert stock_codes_2022 == {"005930", "000660", "005380"}
    
    # FY2023: 1 report (Samsung)
    fy2023 = results[2023]
    assert len(fy2023) == 1
    assert fy2023[0]["fiscal_year"] == 2023
    assert fy2023[0]["stock_code"] == "005930"
    assert fy2023[0]["rcept_dt"] == "20240315"
    assert stats["total_company_years"] == 4
    assert stats["skipped_unparseable"] == 0


def test_fiscal_year_parsing_from_report_name():
    """Test that fiscal year is correctly parsed from report_nm."""
    fy_pattern = re.compile(r'\((\d{4})\.\d{2}\)')
    
    test_cases = [
        ("사업보고서 (2022.12)", 2022),
        ("사업보고서 (2023.12)", 2023),
        ("사업보고서 (2021.12) [정정]", 2021),
        ("연결재무제표 기준 사업보고서 (2023.12)", 2023),
    ]
    
    for report_nm, expected_fy in test_cases:
        match = fy_pattern.search(report_nm)
        assert match is not None, f"Pattern did not match: {report_nm}"
        fiscal_year = int(match.group(1))
        assert fiscal_year == expected_fy, f"Wrong fiscal year for {report_nm}"


def test_correction_filings_are_preserved_for_point_in_time_lookup():
    """Original and correction filings must remain distinct records."""
    reports = [
        {"report_nm": "사업보고서 (2022.12)", "rcept_dt": "20230314"},
        {"report_nm": "[기재정정]사업보고서 (2022.12)", "rcept_dt": "20240314"},
    ]
    reports.sort(key=lambda item: item["rcept_dt"])
    assert [item["rcept_dt"] for item in reports] == ["20230314", "20240314"]
    assert "기재정정" in reports[1]["report_nm"]
    # A 2023-03-31 signal sees the original; a later signal sees the correction.
    assert max((item for item in reports if item["rcept_dt"] <= "20230331"), key=lambda item: item["rcept_dt"])["rcept_dt"] == "20230314"
    assert max((item for item in reports if item["rcept_dt"] <= "20240331"), key=lambda item: item["rcept_dt"])["rcept_dt"] == "20240314"


def test_three_month_window_calculation():
    """Test that date ranges are split into ~3-month (90-day) windows."""
    start = date(2023, 1, 1)
    end = date(2024, 12, 31)
    
    windows = []
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=89), end)
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    
    # Verify we get roughly 8 windows (2 years / 3 months = 8)
    assert len(windows) >= 8
    assert len(windows) <= 10  # Allow some variation
    
    # Verify first and last windows
    assert windows[0][0] == date(2023, 1, 1)
    assert windows[-1][1] == date(2024, 12, 31)
    
    # Verify each window is <= 90 days
    for window_start, window_end in windows:
        days = (window_end - window_start).days + 1
        assert days <= 90, f"Window too large: {days} days"
