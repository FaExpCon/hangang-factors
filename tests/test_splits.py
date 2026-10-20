"""Test split-adjusted price handling using date-wise OHLCV."""
import json
import pytest
from datetime import date
from pathlib import Path
from kr_quant.data import CachedKrxData


@pytest.fixture
def fake_split_cache(tmp_path):
    """Create a minimal bulk cache with a synthetic 50:1 split.
    
    Stock ABC on KOSPI:
    - 2024-01-02: close=2,000,000, 등락률=0.0
    - 2024-01-03: close=2,050,000, 등락률=2.5
    - 2024-01-04: 50:1 split occurs; close=40,000 (2,000,000 / 50), 등락률=-2.439
      The 등락률 reflects -2.439% genuine price movement, not the split
    - 2024-01-05: close=41,000, 등락률=2.5
    
    If we anchor on 2024-01-05's 41,000 and work backward using 등락률:
    - 2024-01-05: adjusted=41,000 (anchor)
    - 2024-01-04: adjusted=41,000 / 1.025 = 40,000
    - 2024-01-03: adjusted=40,000 / 0.97561 = 41,000 (approximately)
    - 2024-01-02: adjusted=41,000 / 1.025 = 40,000 (approximately)
    
    Adjusted series should show no 50:1 drop.
    """
    cache_dir = tmp_path / "bulk" / "v2"
    cache_dir.mkdir(parents=True)
    
    # Date-wise market OHLCV for each day
    days = [
        ("2024-01-02", {"ABC": (2000000, 2100000, 1900000, 2000000, 100000, 200000000000, 0.0, 50000000000)}),
        ("2024-01-03", {"ABC": (2000000, 2100000, 1950000, 2050000, 120000, 246000000000, 2.5, 51250000000)}),
        ("2024-01-04", {"ABC": (42000, 43000, 39000, 40000, 5000000, 200000000000, -2.439, 1000000000)}),  # Post-split
        ("2024-01-05", {"ABC": (40000, 42000, 39500, 41000, 5100000, 209100000000, 2.5, 1025000000)}),
    ]
    
    for day_str, tickers in days:
        key = {"version": 2, "kind": "daily_market_ohlcv", "date": day_str, "market": "KOSPI"}
        data = {
            "key": key,
            "data": {
                "index": list(tickers.keys()),
                "columns": ["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률", "시가총액"],
                "rows": [list(vals) for vals in tickers.values()]
            }
        }
        path = cache_dir / f"{_digest(key)}.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    
    return tmp_path


def _digest(key: dict) -> str:
    from hashlib import sha256
    return sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def test_split_adjusted_series_no_gap(fake_split_cache):
    """Split-adjusted closes should show no 50:1 price drop."""
    cache = CachedKrxData(cache_dir=fake_split_cache / "bulk")
    
    trading_days = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))
    adjusted = cache.get_split_adjusted_closes("ABC", trading_days, "KOSPI")
    
    assert len(adjusted) == 4, "Should have adjusted prices for all 4 days"
    
    # All adjusted closes should be in a narrow range (no 50x gap)
    values = list(adjusted.values())
    assert all(35000 < v < 45000 for v in values), \
        f"Adjusted closes should all be ~40k, got {values}"
    
    # Check day-over-day changes are small
    dates = sorted(adjusted.keys())
    for i in range(len(dates) - 1):
        ratio = adjusted[dates[i+1]] / adjusted[dates[i]]
        assert 0.95 < ratio < 1.05, \
            f"Day-over-day should be near 1.0, got {dates[i]} → {dates[i+1]}: {ratio:.4f}"


def test_momentum_across_split(fake_split_cache):
    """12-1 month momentum should use split-adjusted prices."""
    cache = CachedKrxData(cache_dir=fake_split_cache / "bulk")
    
    trading_days = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))
    adjusted = cache.get_split_adjusted_closes("ABC", trading_days, "KOSPI")
    
    # Momentum = (latest / earliest) - 1
    momentum = adjusted[date(2024, 1, 5)] / adjusted[date(2024, 1, 2)] - 1
    
    # Should be near 0%, not -98% from an unadjusted split
    assert abs(momentum) < 0.10, \
        f"Momentum should be small (not a 50:1 artifact), got {momentum:.2%}"


def test_backtest_position_value_across_split(fake_split_cache):
    """Position value should remain constant across a split when using adjusted prices consistently."""
    cache = CachedKrxData(cache_dir=fake_split_cache / "bulk")
    
    trading_days = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))
    adjusted = cache.get_split_adjusted_closes("ABC", trading_days, "KOSPI")
    
    # Simulate: buy on 2024-01-02 using adjusted close for sizing
    # In a split-adjusted backtest, we work entirely in the adjusted price space
    adjusted_buy_price = adjusted[date(2024, 1, 2)]
    capital = 10_000_000
    shares = capital / adjusted_buy_price
    
    # Mark position value each day using adjusted closes
    values = [shares * adjusted[d] for d in trading_days]
    
    # Initial value on 2024-01-02
    initial_value = values[0]
    assert abs(initial_value - capital) < 100, "Initial position value should match capital"
    
    # Value on 2024-01-04 (split day) should not drop to ~2% of initial
    split_day_value = values[2]
    assert split_day_value > 0.9 * capital, \
        f"Split day value shouldn't collapse; got {split_day_value/capital:.1%} of initial"
    
    # All daily values should stay close to initial (modulo genuine price changes)
    for i, d in enumerate(trading_days):
        ratio = values[i] / capital
        assert 0.95 < ratio < 1.05, \
            f"Day {d} value should stay near capital; got {ratio:.1%}"


def test_adjusted_opens_consistent_with_closes(fake_split_cache):
    """Open and close prices should be adjusted by the same factor, so fills right after a split are correct."""
    cache = CachedKrxData(cache_dir=fake_split_cache / "bulk")
    
    trading_days = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))
    ohlc = cache.get_split_adjusted_ohlc("ABC", trading_days, "KOSPI")
    
    assert len(ohlc) == 4, "Should have OHLC for all 4 days"
    
    # On the split day (2024-01-04), the raw data has open=42,000 and close=40,000
    # Adjusted values should both be in the 40k range, not 2M vs 40k
    split_day_open, split_day_close = ohlc[date(2024, 1, 4)]
    assert 38000 < split_day_open < 45000, \
        f"Split day adjusted open should be ~40k, got {split_day_open}"
    assert 38000 < split_day_close < 45000, \
        f"Split day adjusted close should be ~40k, got {split_day_close}"
    
    # The intraday change (open → close) should be preserved
    # Raw: 42,000 → 40,000 is about -4.76%
    # Adjusted should show the same percentage change
    intraday_change = (split_day_close - split_day_open) / split_day_open
    assert -0.06 < intraday_change < -0.03, \
        f"Split day intraday change should be ~-4.76%, got {intraday_change:.2%}"
    
    # Check that all opens and closes are in the same scale (no 50x gaps)
    all_opens = [ohlc[d][0] for d in sorted(ohlc.keys())]
    all_closes = [ohlc[d][1] for d in sorted(ohlc.keys())]
    
    assert all(35000 < o < 45000 for o in all_opens), \
        f"All adjusted opens should be ~40k, got {all_opens}"
    assert all(35000 < c < 45000 for c in all_closes), \
        f"All adjusted closes should be ~40k, got {all_closes}"
