from datetime import date

import pandas as pd
import pytest

from kr_quant.bulk import BulkDownloader, BulkRequest


class FakeBulk:
    def __init__(self):
        self.calls = []
        # Three month-end dates for 2024-10-01 to 2024-12-31
        self.trading_days = ["2024-10-30", "2024-11-29", "2024-12-30"]

    def get_index_ohlcv_by_date(self, start, end, ticker, name_display=False):
        self.calls.append(("index", start, end, ticker))
        # Return all trading days in the test range (just the month-ends for simplicity)
        return pd.DataFrame({"종가": [100, 101, 102]}, index=pd.to_datetime(self.trading_days))

    def get_market_fundamental_by_ticker(self, day, market):
        self.calls.append(("fund", day, market))
        return pd.DataFrame({"PER": [1, 2]}, index=["000001", "000002"])

    def get_market_cap_by_ticker(self, day, market):
        self.calls.append(("cap", day, market))
        return pd.DataFrame({"시가총액": [100, 200]}, index=["000001", "000002"])

    def get_market_ticker_list(self, day, market):
        self.calls.append(("tickers", day, market))
        return ["000001", "000002"]

    def get_market_ohlcv_by_ticker(self, day, market):
        # This is the date-wise full-market OHLCV call
        self.calls.append(("daily_ohlcv", day, market))
        return pd.DataFrame({"시가": [100, 101], "종가": [101, 102], "등락률": [1.0, 1.0]}, 
                           index=["000001", "000002"])


def test_resumes_every_response_without_network_and_partial_smoke(tmp_path):
    api = FakeBulk()
    req = BulkRequest(date(2024,10,1), date(2024,12,31), max_tickers=1)
    run = BulkDownloader(req, cache_dir=tmp_path/"cache", output_dir=tmp_path/"out", api=api, delay=0, progress=lambda _: None)
    result = run.run()
    assert result["status"] == "complete"
    assert result["network_requests"] == len(api.calls)
    assert result["last_observed_market_session_each_month"] == ["2024-10-30", "2024-11-29", "2024-12-30"]
    assert result["month_end_membership"]["2024-10-30_KOSPI"] == ["000001", "000002"]
    assert (tmp_path/"out"/"bulk_2024-10-01_2024-12-31_smoke_summary.json").exists()
    again = BulkDownloader(req, cache_dir=tmp_path/"cache", output_dir=tmp_path/"out", api=api, delay=0, progress=lambda _: None).run()
    assert again["network_requests"] == 0
    assert again["cache_hits"] == result["completed_jobs"]


def test_delisted_name_from_old_month_remains_in_history(tmp_path):
    api = FakeBulk()
    def membership(day, market):
        api.calls.append(("tickers", day, market))
        return ["000001", "000002"] if day == "20241030" else ["000002"]
    api.get_market_ticker_list = membership
    def frame(day, market):
        return pd.DataFrame({"PER": [1,2] if day == "20241030" else [2]},
                            index=["000001","000002"] if day == "20241030" else ["000002"])
    api.get_market_fundamental_by_ticker = frame
    def caps(day, market):
        return pd.DataFrame({"시가총액": [100,200] if day == "20241030" else [200]},
                            index=["000001","000002"] if day == "20241030" else ["000002"])
    api.get_market_cap_by_ticker = caps
    result = BulkDownloader(BulkRequest(date(2024,10,1), date(2024,12,31)),
                            cache_dir=tmp_path/"cache", output_dir=tmp_path/"out", api=api,
                            delay=0, progress=lambda _: None).run()
    assert result["historical_distinct_tickers"] == 2
    # No per-ticker price requests anymore; date-wise OHLCV captures all tickers
    assert "price_tickers_planned" not in result


def test_empty_ticker_list_falls_back_to_market_cap_and_is_cached(tmp_path):
    api = FakeBulk()

    def empty_tickers(day, market):
        api.calls.append(("tickers", day, market))
        return []

    api.get_market_ticker_list = empty_tickers
    request = BulkRequest(date(2024, 10, 1), date(2024, 12, 31))

    result = BulkDownloader(request, cache_dir=tmp_path / "cache", output_dir=tmp_path / "out",
                            api=api, delay=0, progress=lambda _: None).run()

    assert result["status"] == "complete"
    assert {fallback["source"] for fallback in result["membership_fallbacks"]} == {"market_cap_by_ticker"}
    assert result["month_end_membership"]["2024-10-30_KOSPI"] == ["000001", "000002"]

    calls_after_first_run = len(api.calls)
    again = BulkDownloader(request, cache_dir=tmp_path / "cache", output_dir=tmp_path / "out",
                           api=api, delay=0, progress=lambda _: None).run()
    assert again["status"] == "complete"
    assert len(api.calls) == calls_after_first_run
    assert again["membership_fallbacks"] == result["membership_fallbacks"]


def test_incomplete_run_writes_summary_and_reruns_without_repeating_completed_jobs(tmp_path):
    api = FakeBulk()
    original = api.get_market_cap_by_ticker
    attempts = [0]
    def fail_once(day, market):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("API unavailable")
        return original(day, market)
    api.get_market_cap_by_ticker = fail_once
    req = BulkRequest(date(2024,10,1),date(2024,12,31),1)
    def downloader():
        return BulkDownloader(req, cache_dir=tmp_path/"cache",output_dir=tmp_path/"out",api=api,delay=0,progress=lambda _: None)
    with pytest.raises(RuntimeError, match="incomplete"):
        downloader().run()
    import json
    report=json.loads((tmp_path/"out"/"bulk_2024-10-01_2024-12-31_smoke_summary.json").read_text(encoding="utf-8"))
    assert report["status"] == "incomplete"
    assert downloader().run()["status"] == "complete"
    assert len([call for call in api.calls if call[0] == "index" and call[3] == "1001"]) == 1


def test_bad_range_fails_closed(tmp_path):
    with pytest.raises(ValueError):
        BulkRequest(date(2024,12,31),date(2024,10,1))
