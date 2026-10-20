"""Offline coverage and resume tests for the historical KRX downloader."""

import json
from datetime import date
from unittest.mock import Mock

import pytest

from scripts.download_krx_full import download


def test_month_end_is_last_observed_trading_date(tmp_path):
    client = Mock()
    seen = []

    def get(service, day):
        seen.append((service, day.isoformat()))
        if day == date(2024, 1, 1) or day == date(2024, 2, 1):
            return {"OutBlock_1": []}  # closed weekdays
        return {"OutBlock_1": [{"IDX_NM": "코스피 200"}]}

    client.get.side_effect = get
    client.request_count = 0
    state = download(client, date(2024, 1, 1), date(2024, 2, 2), tmp_path / "progress.json")
    assert state["status"] == "complete"
    assert state["closed_weekdays"] == 2
    assert state["snapshots"] == 2
    assert ("stk_isu_base_info", "2024-01-31") in seen
    assert ("ksq_isu_base_info", "2024-02-02") in seen
    assert ("stk_bydd_trd", "2024-02-01") not in seen
    assert json.loads((tmp_path / "progress.json").read_text())["status"] == "complete"


def test_partial_date_resumes_from_cache_without_skipping(tmp_path, monkeypatch):
    monkeypatch.setenv("KRX_API_KEY", "dummy")
    from kr_quant.krx import KRXClient

    cache = tmp_path / "cache"
    attempts = []

    class Response:
        status_code = 200
        ok = True

        def json(self):
            return {"OutBlock_1": [{"ISU_CD": "005930", "IDX_NM": "코스피 200"}]}

    def request(url, **kwargs):
        attempts.append(url.rsplit("/", 1)[-1])
        return Response()

    progress = tmp_path / "progress.json"
    with monkeypatch.context() as patched:
        patched.setattr("kr_quant.krx.requests.get", request)
        with pytest.raises(RuntimeError, match="ceiling"):
            download(KRXClient(cache_dir=cache, daily_limit=2),
                     date(2024, 3, 4), date(2024, 3, 4), progress)
        assert attempts == ["kospi_dd_trd", "stk_bydd_trd"]
        assert json.loads(progress.read_text())["status"] == "paused"
        # Simulate next Korean day by moving the ledger aside inside tmp_path.
        import time
        from datetime import datetime, timedelta, timezone
        korean_day = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
        ledger = cache / f"requests_{korean_day}.json"
        ledger.rename(cache / "previous_day_test_fixture.json")
        state = download(KRXClient(cache_dir=cache), date(2024, 3, 4), date(2024, 3, 4), progress)
    assert state["status"] == "complete"
    assert state["completed_dates"] == 1
    assert attempts.count("kospi_dd_trd") == 1
    assert attempts.count("stk_bydd_trd") == 1
    assert len(attempts) == 6  # Four daily services plus two snapshot services.


def test_rejects_trading_day_without_exact_kospi_200(tmp_path):
    client = Mock()
    client.get.return_value = {"OutBlock_1": [{"IDX_NM": "코스피 200 중소형주"}]}
    with pytest.raises(ValueError, match="KOSPI 200"):
        download(client, date(2024, 3, 4), date(2024, 3, 4), tmp_path / "progress.json")
    client.get.assert_called_once_with("kospi_dd_trd", date(2024, 3, 4))
