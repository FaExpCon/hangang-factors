from datetime import date

import pandas as pd
import pytest

from kr_quant.config import BacktestConfig, DataMode
from kr_quant.contracts import Market, SecurityType
from kr_quant.data import Classification, PykrxLoader, SampleRequest, coverage, make_snapshot


class FakeApi:
    def __init__(self):
        self.calls = []

    def get_market_ohlcv_by_date(self, first, last, ticker, adjusted=True):
        self.calls.append(("price", first, last, ticker, adjusted))
        return pd.DataFrame({"종가": range(100, 140), "시가": range(99, 139),
                             "거래대금": [2e9] * 40}, index=pd.bdate_range("2024-01-01", periods=40))

    def get_market_fundamental_by_date(self, first, last, ticker):
        self.calls.append(("fund", first, last, ticker))
        return pd.DataFrame({"PER": [10, 12], "PBR": [1, 2], "EPS": [100, 120]},
                            index=pd.to_datetime(["2024-01-02", "2024-02-20"]))

    def get_market_cap_by_date(self, first, last, ticker):
        self.calls.append(("cap", first, last, ticker))
        return pd.DataFrame({"시가총액": [1e11]}, index=pd.to_datetime(["2024-02-23"]))

    def get_index_ohlcv_by_date(self, first, last, ticker, name_display=True):
        self.calls.append(("index", first, last, ticker))
        return pd.DataFrame({"종가": [3000, 3100]}, index=pd.to_datetime(["2024-02-22", "2024-02-23"]))


def test_cache_and_offline_no_requests(tmp_path):
    api = FakeApi()
    clock = [0.0]
    sleeps = []
    def sleep(n):
        sleeps.append(n)
        clock[0] += n
    loader = PykrxLoader(tmp_path, 0.2, api, lambda: clock[0], sleep)
    request = SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",))
    first = loader.load_sample(request)
    assert first.requests == 6 and first.cache_hits == 0
    assert len(api.calls) == 6 and len(sleeps) == 5 and all(x >= 0.2 for x in sleeps)
    second = loader.load_sample(request, offline=True)
    assert second.requests == 0 and second.cache_hits == 6 and len(api.calls) == 6
    assert coverage(second)["indices"]["KOSPI200"]["fields"] == ["종가"]
    assert second.stocks["005930"]["prices"].equals(first.stocks["005930"]["prices"])


def test_lags_and_strict_fail_closed(tmp_path):
    loader = PykrxLoader(tmp_path, 0, FakeApi())
    sample = loader.load_sample(SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",)))
    classification = {"005930": Classification(Market.KOSPI, "Technology", SecurityType.COMMON, False, date(2023, 1, 1))}
    snap = make_snapshot(sample, date(2024, 2, 23), BacktestConfig(fundamental_lag_days=30), classification)
    assert snap.stocks[0].fundamentals.per == 10
    assert snap.stocks[0].fundamentals.eps == 100
    assert make_snapshot(sample, date(2024, 2, 23), BacktestConfig(fundamental_lag_days=0), classification).stocks[0].fundamentals.per == 12
    assert make_snapshot(sample, date(2024, 2, 23), BacktestConfig(fundamental_lag_days=120), classification).stocks[0].fundamentals is None
    with pytest.raises(ValueError, match="availability-dated"):
        make_snapshot(sample, date(2024, 2, 23), BacktestConfig(data_mode=DataMode.STRICT), classification)
    with pytest.raises(ValueError, match="classification required"):
        make_snapshot(sample, date(2024, 2, 23), BacktestConfig(), {})


def test_range_bound_and_missing_cache(tmp_path):
    assert SampleRequest(date(2023, 1, 1), date(2024, 12, 31), ("005930",)).max_days == 730
    with pytest.raises(ValueError, match="limit"):
        SampleRequest(date(2023, 1, 1), date(2025, 2, 1), ("005930",))
    with pytest.raises(ValueError, match="bounded"):
        SampleRequest(date(2024, 1, 1), date(2024, 2, 1), ("005930",) * 2)
    loader = PykrxLoader(tmp_path, api=FakeApi())
    with pytest.raises(FileNotFoundError):
        loader.load_sample(SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",)), offline=True)


def test_empty_response_is_not_cached_and_reports_are_observed(tmp_path):
    api = FakeApi()
    api.get_market_ohlcv_by_date = lambda *args, **kwargs: pd.DataFrame()
    request = SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",))
    loader = PykrxLoader(tmp_path, 0, api)
    with pytest.raises(ValueError, match="not cached"):
        loader.load_sample(request)
    with pytest.raises(FileNotFoundError):
        loader.load_sample(request, offline=True)


def test_coverage_counts_nulls_zeros_and_missing_sessions(tmp_path):
    api = FakeApi()
    original = api.get_market_ohlcv_by_date
    def with_gaps(*args, **kwargs):
        frame = original(*args, **kwargs)
        frame.loc[frame.index[0], "거래대금"] = None
        frame.loc[frame.index[1], "거래대금"] = 0
        return frame
    api.get_market_ohlcv_by_date = with_gaps
    sample = PykrxLoader(tmp_path, 0, api).load_sample(
        SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",)))
    report = coverage(sample)
    assert report["stocks"]["005930"]["prices"]["null_values"]["거래대금"] == 1
    assert report["stocks"]["005930"]["prices"]["zero_values"]["거래대금"] == 1
    assert report["stocks"]["005930"]["fundamentals"]["missing_sessions_vs_kospi"] == ["2024-02-22", "2024-02-23"]


def test_missing_credentials_fail_before_pykrx_import(tmp_path, monkeypatch):
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda: False)
    with pytest.raises(RuntimeError, match="must be configured"):
        PykrxLoader(tmp_path)


def test_missing_latest_eps_does_not_fall_back_to_older_row(tmp_path):
    api = FakeApi()
    def fundamentals(first, last, ticker):
        return pd.DataFrame({"PER": [10, 12], "PBR": [1, 2], "EPS": [100, None]},
                            index=pd.to_datetime(["2024-01-02", "2024-02-20"]))
    api.get_market_fundamental_by_date = fundamentals
    sample = PykrxLoader(tmp_path, 0, api).load_sample(
        SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",)))
    cls = {"005930": Classification(Market.KOSPI, "Tech", SecurityType.COMMON, False, date(2023, 1, 1))}
    assert make_snapshot(sample, date(2024, 2, 23), BacktestConfig(fundamental_lag_days=0), cls).stocks[0].fundamentals is None


def test_stale_market_cap_is_preserved_for_executor_not_tradable(tmp_path):
    api = FakeApi()
    api.get_market_cap_by_date = lambda first, last, ticker: pd.DataFrame(
        {"시가총액": [1e11]}, index=pd.to_datetime(["2024-01-02"]))
    sample = PykrxLoader(tmp_path, 0, api).load_sample(
        SampleRequest(date(2024, 1, 1), date(2024, 2, 23), ("005930",)))
    cls = {"005930": Classification(Market.KOSPI, "Tech", SecurityType.COMMON, False, date(2023, 1, 1))}
    snap = make_snapshot(sample, date(2024, 2, 23), BacktestConfig(), cls)
    assert snap.stocks[0].market_cap.observed_on == date(2024, 1, 2)
    from kr_quant.universe import filter_universe
    assert "missing_current_market_cap" in filter_universe(snap, BacktestConfig()).rejected["005930"]
