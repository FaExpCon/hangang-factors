from unittest.mock import patch

import pytest

from kr_quant.krx import KRXClient, parse_number


def test_official_route_header_and_date(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_API_KEY", "dummy")
    client = KRXClient(daily_limit=1, cache_dir=tmp_path)

    class Response:
        status_code = 200
        ok = True

        def json(self):
            return {"OutBlock_1": [{"FLUC_RT": "1.25"}]}

    with patch("kr_quant.krx.requests.get", return_value=Response()) as get:
        assert client.get("stk_bydd_trd", "2024-03-04")["OutBlock_1"]
        with pytest.raises(RuntimeError, match="ceiling"):
            client.get("stk_bydd_trd", "2024-03-05")
    assert get.call_count == 1
    args, kwargs = get.call_args
    assert args[0] == "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
    assert kwargs["params"] == {"basDd": "20240304"}
    assert kwargs["headers"] == {"AUTH_KEY": "dummy"}
    assert KRXClient(daily_limit=1, cache_dir=tmp_path).get("stk_bydd_trd", "2024-03-04")["OutBlock_1"]
    with pytest.raises(RuntimeError, match="ceiling"):
        KRXClient(daily_limit=1, cache_dir=tmp_path).get("stk_bydd_trd", "2024-03-06")


def test_auth_failure_counts_attempt_and_does_not_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_API_KEY", "dummy")
    client = KRXClient(cache_dir=tmp_path)

    class Response:
        status_code = 403
        ok = False

    with patch("kr_quant.krx.requests.get", return_value=Response()) as get:
        with pytest.raises(PermissionError, match="403"):
            client.get("kospi_dd_trd", "20240304")
    assert client.request_count == 1
    assert get.call_count == 1
    assert not (tmp_path / "kospi_dd_trd" / "20240304.json").exists()


def test_client_rejects_relaxed_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_API_KEY", "dummy")
    with pytest.raises(ValueError, match="ceiling"):
        KRXClient(daily_limit=2501, cache_dir=tmp_path)
    with pytest.raises(ValueError, match="spacing"):
        KRXClient(min_delay=0.5, cache_dir=tmp_path)


def test_numeric_strings_and_missing():
    assert parse_number("1,234") == 1234
    assert parse_number("-1.25") == -1.25
    assert parse_number("-") is None
