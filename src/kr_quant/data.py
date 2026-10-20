"""Bounded pykrx downloads and point-in-time snapshot assembly.

Raw pykrx fundamentals carry observation dates, not filing availability dates.
No historical sector or delisting coverage is inferred from ticker names.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import time
from typing import Callable, Mapping

import pandas as pd

from kr_quant.config import BacktestConfig, DataMode
from kr_quant.contracts import (ClassificationKind, CloseBar, DatedValue, Fundamental, IndexHistory,
                                Market, SecurityType, Snapshot, Stock, validate_snapshot)


INDEX_CODES = {"KOSPI": "1001", "KOSDAQ": "2001", "KOSPI200": "1028"}
CACHE_VERSION = 1


class CachedKrxData:
    """Read-only interface to bulk-downloaded KRX data cache.
    
    Column names from pykrx daily market OHLCV are in Korean:
    - 시가: open price
    - 고가: high price
    - 저가: low price
    - 종가: close price
    - 거래량: volume (shares)
    - 거래대금: trading value (KRW) - use this directly instead of volume × price
    - 등락률: daily change rate (%) - use this to build split-adjusted returns
    - 시가총액: market cap (KRW)
    """
    
    def __init__(self, cache_dir: Path = Path("data/cache/bulk")):
        from kr_quant.bulk import VERSION as BULK_VERSION
        self.cache_dir = Path(cache_dir) / f"v{BULK_VERSION}"
        if not self.cache_dir.exists():
            raise FileNotFoundError(f"cache directory not found: {self.cache_dir}")
    
    def _path(self, key: dict) -> Path:
        """Generate cache file path from key."""
        from kr_quant.bulk import VERSION as BULK_VERSION
        key_with_version = {"version": BULK_VERSION, **key}
        digest = sha256(json.dumps(key_with_version, sort_keys=True).encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"
    
    def _load(self, key: dict) -> dict:
        """Load cached data by key."""
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(f"no cached data for {key}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "data" not in payload:
            raise ValueError(f"invalid cache format: {path}")
        return payload["data"]
    
    def get_daily_market_ohlcv(self, date: date, market: str) -> pd.DataFrame | None:
        """Retrieve full-market OHLCV for a single date.
        
        Returns DataFrame with ticker as index and columns:
        시가 (open), 고가 (high), 저가 (low), 종가 (close),
        거래량 (volume), 거래대금 (trading_value), 등락률 (change_rate), 시가총액 (market_cap)
        
        Returns None if not cached.
        """
        key = {"kind": "daily_market_ohlcv", "date": date.isoformat(), "market": market}
        try:
            data = self._load(key)
        except FileNotFoundError:
            return None
        
        if "columns" in data and "rows" in data and "index" in data:
            df = pd.DataFrame(data["rows"], columns=data["columns"], index=data["index"])
            return df
        raise ValueError(f"unexpected cache format for daily_market_ohlcv: {key}")
    
    def get_split_adjusted_closes(self, ticker: str, trading_days: tuple[date, ...], market: str) -> dict[date, float]:
        """Build split-adjusted close prices from date-wise OHLCV using 등락률.
        
        Anchors on the latest available close and works backward, applying the inverse
        of each day's change rate to remove the effect of splits. Missing dates or
        tickers on a given date are skipped.
        
        Returns:
            dict mapping date to adjusted close price for dates where the ticker traded.
        """
        if not trading_days:
            return {}
        
        # Collect raw closes and change rates for all requested dates
        raw_data: list[tuple[date, float, float]] = []
        for day in trading_days:
            df = self.get_daily_market_ohlcv(day, market)
            if df is None or ticker not in df.index:
                continue
            row = df.loc[ticker]
            close = row.get("종가")
            change_rate = row.get("등락률")
            if pd.notna(close) and pd.notna(change_rate):
                raw_data.append((day, float(close), float(change_rate)))
        
        if not raw_data:
            return {}
        
        # Sort by date ascending
        raw_data.sort(key=lambda x: x[0])
        
        # Anchor on the latest close (most recent data is already post-split)
        # and work backward, undoing each day's change to get adjusted prices
        adjusted: dict[date, float] = {}
        adjusted[raw_data[-1][0]] = raw_data[-1][1]  # Latest close is the anchor
        
        # Work backward: if today's unadjusted price changed by r%,
        # then yesterday_adjusted = today_adjusted / (1 + r/100)
        for i in range(len(raw_data) - 2, -1, -1):
            day, _, _ = raw_data[i]
            next_day, _, next_change_rate = raw_data[i + 1]
            adjusted[day] = adjusted[next_day] / (1 + next_change_rate / 100)
        
        return adjusted
    
    def get_split_adjusted_ohlc(self, ticker: str, trading_days: tuple[date, ...], market: str) -> dict[date, tuple[float, float]]:
        """Build split-adjusted (open, close) pairs from date-wise OHLCV using 등락률.
        
        Uses the same adjustment factor for both open and close on each day, ensuring
        that fills right after a split use correctly adjusted prices.
        
        Returns:
            dict mapping date to (adjusted_open, adjusted_close) for dates where the ticker traded.
        """
        if not trading_days:
            return {}
        
        # Collect raw opens, closes, and change rates
        raw_data: list[tuple[date, float, float, float]] = []
        for day in trading_days:
            df = self.get_daily_market_ohlcv(day, market)
            if df is None or ticker not in df.index:
                continue
            row = df.loc[ticker]
            open_price = row.get("시가")
            close_price = row.get("종가")
            change_rate = row.get("등락률")
            if pd.notna(open_price) and pd.notna(close_price) and pd.notna(change_rate):
                raw_data.append((day, float(open_price), float(close_price), float(change_rate)))
        
        if not raw_data:
            return {}
        
        raw_data.sort(key=lambda x: x[0])
        
        # First, build adjusted closes using the same backward-chaining logic
        adjusted_closes: dict[date, float] = {}
        adjusted_closes[raw_data[-1][0]] = raw_data[-1][2]  # Latest close is the anchor
        
        for i in range(len(raw_data) - 2, -1, -1):
            day = raw_data[i][0]
            next_day = raw_data[i + 1][0]
            next_change_rate = raw_data[i + 1][3]
            adjusted_closes[day] = adjusted_closes[next_day] / (1 + next_change_rate / 100)
        
        # Now compute adjusted opens by applying the same factor: adjusted_close / raw_close
        result: dict[date, tuple[float, float]] = {}
        for day, raw_open, raw_close, _ in raw_data:
            factor = adjusted_closes[day] / raw_close
            adjusted_open = raw_open * factor
            result[day] = (adjusted_open, adjusted_closes[day])
        
        return result
    
    def get_month_end_tickers(self, date: date, market: str) -> pd.Index | None:
        """Get the list of tickers trading on the month-end date.
        
        Returns Index of ticker strings, or None if not cached.
        """
        key = {"kind": "ticker_list", "date": date.isoformat(), "market": market}
        try:
            data = self._load(key)
        except FileNotFoundError:
            return None
        
        if isinstance(data, list):
            return pd.Index(data)
        raise ValueError(f"unexpected cache format for ticker_list: {key}")
    
    def get_month_end_fundamentals(self, date: date, market: str) -> pd.DataFrame | None:
        """Get month-end fundamentals by ticker.
        
        Returns DataFrame with ticker as index and columns including PER, PBR, EPS, etc.
        Returns None if not cached.
        """
        key = {"kind": "fundamentals_by_ticker", "date": date.isoformat(), "market": market}
        try:
            data = self._load(key)
        except FileNotFoundError:
            return None
        
        if "columns" in data and "rows" in data and "index" in data:
            df = pd.DataFrame(data["rows"], columns=data["columns"], index=data["index"])
            return df
        raise ValueError(f"unexpected cache format for fundamentals_by_ticker: {key}")
    
    def get_month_end_market_cap(self, date: date, market: str) -> pd.DataFrame | None:
        """Get month-end market caps by ticker.
        
        Returns DataFrame with ticker as index and columns including 시가총액 (market cap).
        Returns None if not cached.
        """
        key = {"kind": "market_cap_by_ticker", "date": date.isoformat(), "market": market}
        try:
            data = self._load(key)
        except FileNotFoundError:
            return None
        
        if "columns" in data and "rows" in data and "index" in data:
            df = pd.DataFrame(data["rows"], columns=data["columns"], index=data["index"])
            return df
        raise ValueError(f"unexpected cache format for market_cap_by_ticker: {key}")
    
    def get_index_ohlcv(self, date: date, index_name: str) -> pd.DataFrame | None:
        """Get index OHLCV for a date range.
        
        This is a simplified accessor - the actual cache stores index data
        by date range, not by single date. For now, return None (user can extend).
        """
        # Index data is stored by date range in the bulk cache, not by single date.
        # A full implementation would need to map from (date, index_name) to the
        # appropriate cached range. For now, return None.
        return None


@dataclass(frozen=True)
class Classification:
    market: Market
    sector: str
    security_type: SecurityType | None
    is_financial: bool | None
    available_on: date
    kind: ClassificationKind = ClassificationKind.HISTORICAL
    source: str = ""
    is_holding_company: bool | None = None

    @classmethod
    def current_proxy(cls, market: Market, sector: str | None,
                      security_type: SecurityType | None, is_financial: bool | None,
                      *, source: str, retrieved_on: date) -> "Classification":
        if not source.strip():
            raise ValueError("proxy source is required")
        return cls(market, sector.strip() if sector and sector.strip() else "unknown",
                   security_type, is_financial, retrieved_on, ClassificationKind.CURRENT_PROXY, source)


def current_classifications(tickers: tuple[str, ...], sector_frames: Mapping[Market, pd.DataFrame],
                            checks: Mapping[str, tuple[SecurityType | None, bool | None]] | None = None,
                            *, retrieved_on: date, source: str = "pykrx current sector classifications") -> dict[str, Classification]:
    """Adapt current pykrx industry/name tables using explicit approximate checks.

    Name/industry rules are a proxy, never a verified historical classification.
    `checks` can replace guessed fields with separately audited current flags.
    Ambiguous/missing names or sectors fail closed for type or financial status.
    """
    checks = checks or {}
    result = {}
    for ticker in tickers:
        matches = []
        for market, frame in sector_frames.items():
            if ticker in frame.index:
                matches.append((market, frame.loc[ticker]))
        if len(matches) != 1:
            raise ValueError(f"{ticker} needs exactly one verified current market; no market inferred")
        market, row = matches[0]
        sector = row.get("업종명")
        name = row.get("종목명")
        security, financial = approximate_current_checks(name, sector)
        if ticker in checks:
            security, financial = checks[ticker]
        result[ticker] = Classification.current_proxy(market, sector if isinstance(sector, str) else None,
                                                       security, financial, source=source, retrieved_on=retrieved_on)
        if isinstance(name, str):
            from dataclasses import replace
            normalized = name.upper().replace(" ", "")
            result[ticker] = replace(result[ticker], is_holding_company=any(token in normalized for token in ("지주", "홀딩스", "HOLDINGS")))
    return result


def approximate_current_checks(name: object, sector: object) -> tuple[SecurityType | None, bool | None]:
    """Conservative name/industry heuristics; not proof of historical security type."""
    security: SecurityType | None = None
    financial: bool | None = None
    if isinstance(name, str) and name.strip():
        normalized = name.strip().upper().replace(" ", "")
        if "스팩" in normalized or "SPAC" in normalized:
            security = SecurityType.SPAC
        elif "리츠" in normalized or "REIT" in normalized:
            security = SecurityType.REIT
        elif normalized.endswith(("우", "우B", "우C", "우D", "PREFERRED", "PREF")):
            security = SecurityType.PREFERRED
        else:
            security = SecurityType.COMMON
    if isinstance(sector, str) and sector.strip() and sector.strip() != "unknown":
        financial = any(word in sector for word in ("금융", "은행", "증권", "보험", "캐피탈", "여신", "저축은행"))
    return security, financial


@dataclass(frozen=True)
class SampleRequest:
    start: date
    end: date
    tickers: tuple[str, ...]
    # A bounded probe is intentional; explicit opt-in is required for broader downloads.
    max_tickers: int = 30
    max_days: int = 730

    def __post_init__(self) -> None:
        if self.start > self.end or (self.end - self.start).days > self.max_days:
            raise ValueError("sample date range exceeds limit")
        if not self.tickers or len(self.tickers) > self.max_tickers or len(self.tickers) != len(set(self.tickers)):
            raise ValueError("sample needs unique, bounded tickers")
        if any(len(t) != 6 or not t.isdigit() for t in self.tickers):
            raise ValueError("tickers must be six digits")


@dataclass(frozen=True)
class Sample:
    request: SampleRequest
    stocks: Mapping[str, Mapping[str, pd.DataFrame]]
    indices: Mapping[str, pd.DataFrame]
    seconds: float
    requests: int
    cache_hits: int


def _native(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    return value.item() if hasattr(value, "item") else value


class PykrxLoader:
    def __init__(self, cache_dir: Path = Path("data/cache"), min_interval_seconds: float = 0.3,
                 api: object | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if min_interval_seconds < 0:
            raise ValueError("request interval cannot be negative")
        self.cache_dir = Path(cache_dir)
        self.min_interval_seconds = min_interval_seconds
        if api is None:
            from dotenv import load_dotenv
            load_dotenv()
            if not os.environ.get("KRX_ID") or not os.environ.get("KRX_PW"):
                raise RuntimeError("KRX_ID and KRX_PW must be configured; no request sent")
            # pykrx logs the login ID at import time. Capture it, then redact
            # both credentials before surfacing any diagnostic.
            output = io.StringIO()
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    from pykrx import stock
                    from pykrx.website.comm import webio
            except Exception as exc:
                detail = str(exc).replace(os.environ["KRX_ID"], "[REDACTED]").replace(os.environ["KRX_PW"], "[REDACTED]")
                raise RuntimeError(f"pykrx authentication error: {type(exc).__name__}: {detail}") from None
            if webio._session is None or not webio._session.is_authenticated:
                diagnostic = output.getvalue()
                for secret in (os.environ["KRX_ID"], os.environ["KRX_PW"]):
                    diagnostic = diagnostic.replace(secret, "[REDACTED]")
                raise RuntimeError(f"pykrx login failed: {diagnostic.strip() or 'no diagnostic returned'}")
            api = stock
        self.api = api
        self.clock = clock
        self.sleep = sleep
        self._last_request: float | None = None

    def _fetch(self, kind: str, ticker: str, start: date, end: date, offline: bool) -> tuple[pd.DataFrame, bool]:
        spec = {"version": CACHE_VERSION, "kind": kind, "ticker": ticker,
                "start": start.isoformat(), "end": end.isoformat(), "adjusted": False}
        key = sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"v{CACHE_VERSION}" / f"{key}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("spec") != spec:
                raise ValueError(f"cache metadata mismatch: {path}")
            frame = pd.DataFrame(payload["rows"], columns=payload["columns"])
            frame.index = pd.to_datetime(payload["dates"])
            return frame, True
        if offline:
            raise FileNotFoundError(f"uncached {kind} {ticker} {start}..{end}")
        if self._last_request is not None:
            self.sleep(max(0.0, self.min_interval_seconds - (self.clock() - self._last_request)))
        # Update immediately before invocation: errors must still count against the throttle.
        self._last_request = self.clock()
        first, last = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        if kind == "prices":
            frame = self.api.get_market_ohlcv_by_date(first, last, ticker, adjusted=False)
        elif kind == "fundamentals":
            frame = self.api.get_market_fundamental_by_date(first, last, ticker)
        elif kind == "market_cap":
            frame = self.api.get_market_cap_by_date(first, last, ticker)
        else:
            frame = self.api.get_index_ohlcv_by_date(first, last, ticker, name_display=False)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ValueError(f"empty/invalid pykrx response: {kind} {ticker}; not cached")
        frame = frame.sort_index()
        if frame.index.has_duplicates:
            raise ValueError(f"duplicate dates in pykrx response: {kind} {ticker}")
        dates = pd.to_datetime(frame.index)
        if any(d.date() < start or d.date() > end for d in dates):
            raise ValueError(f"out-of-range response: {kind} {ticker}")
        payload = {"spec": spec, "columns": list(frame.columns),
                   "dates": [d.date().isoformat() for d in dates],
                   "rows": [[_native(v) for v in row] for row in frame.itertuples(index=False, name=None)]}
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temp.replace(path)
        return frame, False

    def load_sample(self, request: SampleRequest, *, offline: bool = False) -> Sample:
        start_time = self.clock()
        stock_frames: dict[str, dict[str, pd.DataFrame]] = {}
        indices: dict[str, pd.DataFrame] = {}
        hits = calls = 0
        for ticker in request.tickers:
            stock_frames[ticker] = {}
            for kind in ("prices", "fundamentals", "market_cap"):
                frame, hit = self._fetch(kind, ticker, request.start, request.end, offline)
                stock_frames[ticker][kind] = frame
                hits += hit
                calls += not hit
        for name, code in INDEX_CODES.items():
            indices[name], hit = self._fetch("index", code, request.start, request.end, offline)
            hits += hit
            calls += not hit
        return Sample(request, stock_frames, indices, self.clock() - start_time, calls, hits)


def _last_valid(frame: pd.DataFrame, column: str, cutoff: date) -> tuple[date, float] | None:
    if column not in frame:
        return None
    rows = frame.loc[frame.index.date <= cutoff, column].dropna()
    if rows.empty:
        return None
    return rows.index[-1].date(), float(rows.iloc[-1])


def _closes(frame: pd.DataFrame, signal: date) -> tuple[CloseBar, ...]:
    if "종가" not in frame:
        raise ValueError("missing close column")
    return tuple(CloseBar(d.date(), float(value)) for d, value in frame.loc[frame.index.date <= signal, "종가"].dropna().items())


def make_snapshot(sample: Sample, signal_date: date, config: BacktestConfig,
                  classifications: Mapping[str, Classification]) -> Snapshot:
    """No guessed classifications or filings; strict mode rejects pykrx fundamentals."""
    if not sample.request.start <= signal_date <= sample.request.end:
        raise ValueError("signal date outside downloaded window")
    if any(cls.kind is ClassificationKind.CURRENT_PROXY for cls in classifications.values()) and config.data_mode is DataMode.STRICT:
        raise ValueError("strict mode rejects current-classification proxy")
    if config.data_mode is DataMode.STRICT:
        raise ValueError("strict mode requires external availability-dated fundamental snapshots; pykrx is insufficient")
    if set(classifications) != set(sample.stocks):
        raise ValueError("explicit classification required for every stock")
    stocks = []
    for ticker, frames in sample.stocks.items():
        cls = classifications[ticker]
        if cls.kind is ClassificationKind.HISTORICAL and cls.available_on > signal_date:
            raise ValueError(f"future classification for {ticker}")
        if cls.kind is ClassificationKind.CURRENT_PROXY and config.data_mode is DataMode.STRICT:
            raise ValueError("strict mode rejects current-classification proxy")
        prices, caps, fundamentals = frames["prices"], frames["market_cap"], frames["fundamentals"]
        cap = _last_valid(caps, "시가총액", signal_date)
        if "거래대금" not in prices:
            raise ValueError(f"missing trading value column for {ticker}")
        trading = prices.loc[prices.index.date <= signal_date, "거래대금"].tail(20)
        # Preserve a stale/missing observation as INELIGIBLE, not a current
        # tradable value. A held suspended/delisted name must stay in snapshots
        # so the executor can freeze and settle it without a fake market sale.
        cap_value = DatedValue(cap[1], cap[0]) if cap else DatedValue(0, sample.request.start)
        liquidity = DatedValue(float(trading.mean()), trading.index[-1].date()) if len(trading) == 20 and trading.notna().all() else DatedValue(0, sample.request.start)
        cutoff = signal_date - timedelta(days=config.fundamental_lag_days)
        # Use one historical row for all three; missing EPS means ineligible.
        if not {"PER", "PBR", "EPS"}.issubset(fundamentals.columns):
            raise ValueError(f"missing PER/PBR/EPS columns for {ticker}")
        known = fundamentals.loc[fundamentals.index.date <= cutoff, ["PER", "PBR", "EPS"]]
        fund = None
        if not known.empty:
            row = known.iloc[-1]
            if row.notna().all():
                fund = Fundamental(float(row["PER"]), float(row["PBR"]), known.index[-1].date(), eps=float(row["EPS"]))
        stocks.append(Stock(ticker, cls.market, cls.sector, cls.security_type, cls.is_financial,
                            cls.available_on, cap_value, liquidity,
                            fund, _closes(prices, signal_date), cls.kind, cls.source,
                            cls.available_on if cls.kind is ClassificationKind.CURRENT_PROXY else None,
                            is_holding_company=cls.is_holding_company))
    indexes = tuple(IndexHistory(market, _closes(sample.indices[market.value], signal_date)) for market in Market)
    result = Snapshot(signal_date, tuple(stocks), indexes)
    validate_snapshot(result, config)
    return result


def coverage(sample: Sample) -> dict:
    """Observed fields/nulls and missing trading sessions relative to KOSPI."""
    calendar = set(pd.to_datetime(sample.indices["KOSPI"].index).date)
    def describe(frame: pd.DataFrame) -> dict:
        dates = set(pd.to_datetime(frame.index).date)
        return {"rows": len(frame), "fields": list(frame.columns),
                "missing_sessions_vs_kospi": sorted(d.isoformat() for d in calendar - dates),
                "null_values": {str(k): int(v) for k, v in frame.isna().sum().items()},
                "zero_values": {str(k): int(v) for k, v in frame.eq(0).sum().items()}}
    return {"mode": "APPROXIMATE", "start": sample.request.start.isoformat(),
            "end": sample.request.end.isoformat(), "tickers": list(sample.request.tickers),
            "seconds": round(sample.seconds, 3), "network_requests": sample.requests,
            "cache_hits": sample.cache_hits,
            "stocks": {t: {k: describe(f) for k, f in data.items()} for t, data in sample.stocks.items()},
            "indices": {k: describe(v) for k, v in sample.indices.items()},
            "historical_sector_classifications": "not provided by this loader; external dated metadata required",
            "delisted_stocks": "not established by this sample; survivorship bias possible",
            "fundamental_filing_dates": "not provided by pykrx; fixed-lag approximation only"}
