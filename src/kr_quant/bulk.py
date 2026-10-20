"""Historical date-wise KRX acquisition, separate from the bounded sample loader."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import time
from typing import Callable

import pandas as pd

from kr_quant.data import INDEX_CODES, PykrxLoader


VERSION = 2
MARKETS = ("KOSPI", "KOSDAQ")


def _plain(value):
    if pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


@dataclass(frozen=True)
class BulkRequest:
    start: date
    end: date
    max_tickers: int | None = None  # smoke test ONLY

    def __post_init__(self):
        if self.start > self.end or self.start.year < 2010 or self.end > date.today():
            raise ValueError("invalid historical date range")
        if self.max_tickers is not None and self.max_tickers < 1:
            raise ValueError("max_tickers must be positive")


class BulkDownloader:
    def __init__(self, request: BulkRequest, cache_dir=Path("data/cache/bulk"),
                 output_dir=Path("outputs"), delay=0.5, api=None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 progress: Callable[[str], None] = print):
        if delay < 0:
            raise ValueError("delay must be nonnegative")
        self.request = request
        self.cache_dir = Path(cache_dir) / f"v{VERSION}"
        self.output_dir = Path(output_dir)
        self.delay, self.clock, self.sleep, self.progress = delay, clock, sleep, progress
        self.api = api
        self._last_call = None
        self.network = self.hits = 0
        self.live_seconds = 0.0
        self.run_started = clock()
        self.errors: list[str] = []

    def _api(self):
        if self.api is None:
            # Reuse credential loading and import-time redaction; no API login
            # on a cache-only rerun unless an actual request is needed.
            self.api = PykrxLoader().api
        return self.api

    def _call(self, method: str, *args, **kwargs):
        if self._last_call is not None:
            self.sleep(max(0, self.delay - (self.clock() - self._last_call)))
        self._last_call = self.clock()
        capture = io.StringIO()
        try:
            with redirect_stdout(capture), redirect_stderr(capture):
                answer = getattr(self._api(), method)(*args, **kwargs)
        except Exception as exc:
            # Never echo pykrx's raw output (it may contain an account name).
            text = f"{type(exc).__name__}: {exc}"
            for key in ("KRX_ID", "KRX_PW"):
                if os.environ.get(key):
                    text = text.replace(os.environ[key], "[REDACTED]")
            raise RuntimeError(f"{method}: {text}") from None
        self.network += 1
        return answer

    def _path(self, key: dict) -> Path:
        digest = sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _fetch(self, kind: str, params: dict, method: str, *args, allow_empty=False, **kwargs):
        key = {"version": VERSION, "kind": kind, **params}
        path = self._path(key)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("key") != key or "data" not in payload:
                raise ValueError(f"cache mismatch: {path}")
            self.hits += 1
            return payload["data"]
        started = self.clock()
        answer = self._call(method, *args, **kwargs)
        self.live_seconds += self.clock() - started
        if isinstance(answer, pd.DataFrame):
            if answer.empty and not allow_empty:
                raise ValueError(f"empty response: {kind} {params}")
            if answer.index.has_duplicates:
                raise ValueError(f"duplicate tickers/dates: {kind} {params}")
            data = {"index": [v.date().isoformat() if hasattr(v, "date") else str(v) for v in answer.index],
                    "columns": [str(v) for v in answer.columns],
                    "rows": [[_plain(v) for v in row] for row in answer.itertuples(index=False, name=None)]}
        elif isinstance(answer, (list, tuple)):
            if not answer and kind != "ticker_list":
                raise ValueError(f"empty membership: {params}")
            data = [str(x) for x in answer]
        else:
            raise ValueError(f"invalid response type: {kind} {params}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "data": data}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        tmp.replace(path)
        return data

    def _frame(self, data):
        return pd.DataFrame(data["rows"], columns=data["columns"], index=data["index"])

    def _year_spans(self):
        for year in range(self.request.start.year, self.request.end.year + 1):
            yield max(self.request.start, date(year, 1, 1)), min(self.request.end, date(year, 12, 31))

    def _write_summary(self, summary):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        dest = self.output_dir / f"bulk_{self.request.start}_{self.request.end}{'_smoke' if self.request.max_tickers else ''}_summary.json"
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dest)
        return dest

    def _progress(self, done, total, stage):
        remaining = max(0, total-done)
        rate = max(self.delay, self.live_seconds / self.network) if self.network else None
        eta = round(remaining * rate) if rate is not None else None
        self.progress(f"{stage}: done={done}/{total} remaining={remaining} estimated_seconds={eta if eta is not None else 'unknown'} cache_hits={self.hits} network={self.network}")

    def run(self):
        r = self.request
        summary = {"status": "incomplete", "start": str(r.start), "end": str(r.end),
                   "mode": "APPROXIMATE", "price_cap": r.max_tickers,
                   "source": "pykrx KRX date-wise KOSPI/KOSDAQ; split adjustment via 등락률",
                   "warnings": ["Month-end membership includes delisted names only if KRX historical endpoints return them; verify coverage.",
                                "Fundamental observation date is not filing availability date.",
                                "Split adjustment computed from 등락률 (daily change rate %) in date-wise market OHLCV.",
                                "Daily market OHLCV requested date-wise to capture delisted stocks."],
                    "errors": self.errors,
                    "membership_fallbacks": []}
        try:
            # KOSPI calendar defines actual final trading days in each calendar month
            calendar_data = []
            for first, last in self._year_spans():
                key = {"index": "KOSPI", "first": first.isoformat(), "last": last.isoformat(), "adjusted": False}
                data = self._fetch("index_prices", key, "get_index_ohlcv_by_date",
                                   first.strftime("%Y%m%d"), last.strftime("%Y%m%d"), INDEX_CODES["KOSPI"], name_display=False)
                calendar_data.extend(date.fromisoformat(d) for d in data["index"])
            
            trading_days = sorted(set(calendar_data))
            if not trading_days or min(trading_days) < self.request.start or max(trading_days) > self.request.end:
                raise ValueError("invalid KOSPI index calendar")
            month_ends = [d for i, d in enumerate(trading_days) 
                         if i == len(trading_days)-1 or (d.year, d.month) != (trading_days[i+1].year, trading_days[i+1].month)]
            
            summary["last_observed_market_session_each_month"] = [str(x) for x in month_ends]
            summary["trading_days_in_range"] = len(trading_days)
            membership: dict[str, list[str]] = {}
            
            # Planned requests: KOSPI calendar + month-end snapshots + other indices + date-wise daily OHLCV
            kospi_calendar_requests = len(list(self._year_spans()))
            month_end_requests = len(month_ends) * 2 * 3
            other_indices_requests = len(list(self._year_spans())) * 2  # KOSDAQ and KOSPI200
            daily_ohlcv_requests = len(trading_days) * 2
            total = kospi_calendar_requests + month_end_requests + other_indices_requests + daily_ohlcv_requests
            planned = total
            done = kospi_calendar_requests  # KOSPI calendar already fetched above
            for day in month_ends:
                keydate = day.strftime("%Y%m%d")
                for market in MARKETS:
                    base = {"date": day.isoformat(), "market": market}
                    fundamental = self._fetch("fundamentals_by_ticker", base,
                                              "get_market_fundamental_by_ticker", keydate, market,
                                              allow_empty=True)
                    done += 1
                    cap = self._fetch("market_cap_by_ticker", base,
                                      "get_market_cap_by_ticker", keydate, market,
                                      allow_empty=True)
                    done += 1
                    tickers = self._fetch("ticker_list", base, "get_market_ticker_list", keydate, market,
                                          allow_empty=True)
                    done += 1
                    cap_codes = set(cap["index"])
                    fundamental_codes = set(fundamental["index"])
                    listed = set(tickers)
                    if not listed:
                        if cap_codes:
                            listed = cap_codes
                            summary["membership_fallbacks"].append({
                                **base, "source": "market_cap_by_ticker", "count": len(listed)
                            })
                        elif fundamental_codes:
                            listed = fundamental_codes
                            summary["membership_fallbacks"].append({
                                **base, "source": "fundamentals_by_ticker", "count": len(listed)
                            })
                        else:
                            daily = self._fetch("daily_market_ohlcv", base,
                                                "get_market_ohlcv_by_ticker", keydate, market=market,
                                                allow_empty=True)
                            daily_codes = set(daily["index"])
                            if daily_codes:
                                listed = daily_codes
                                summary["membership_fallbacks"].append({**base, "source": "daily_market_ohlcv", "count": len(listed)})
                            else:
                                raise ValueError(f"empty membership: {base}; all sources empty")
                    if not cap_codes and not fundamental_codes and not listed:
                        raise ValueError(f"empty membership: {base}; all sources empty")
                    if not cap_codes.issubset(listed) or not fundamental_codes.issubset(listed):
                        raise ValueError(f"disagreeing month-end membership {base}; investigate before resuming")
                    if cap_codes and fundamental_codes and not cap_codes & fundamental_codes:
                        raise ValueError(f"no overlap in market cap/fundamentals {base}")
                    membership[f"{day}_{market}"] = sorted(listed)
                self._progress(done, planned, "month-end")
            for first, last in self._year_spans():
                for name in ("KOSDAQ", "KOSPI200"):
                    data = self._fetch("index_prices", {"index": name, "first": str(first), "last": str(last), "adjusted": False},
                                       "get_index_ohlcv_by_date", first.strftime("%Y%m%d"), last.strftime("%Y%m%d"),
                                       INDEX_CODES[name], name_display=False)
                    if not data["index"]:
                        raise ValueError(f"empty index {name} {first}..{last}")
                    done += 1
                self._progress(done, planned, "indices")
            
            # Date-wise daily market OHLCV: captures all stocks trading on each date,
            # including those later delisted. This provides split-adjusted price data via 등락률.
            for trading_day in trading_days:
                keydate = trading_day.strftime("%Y%m%d")
                for market in MARKETS:
                    base = {"date": trading_day.isoformat(), "market": market}
                    self._fetch("daily_market_ohlcv", base,
                               "get_market_ohlcv_by_ticker", keydate, market=market)
                    done += 1
                if done % 50 == 0:
                    self._progress(done, planned, "daily-market-ohlcv")
            self._progress(done, planned, "daily-market-ohlcv")
            
            # All tickers discovered from month-end membership
            all_codes = sorted({ticker for codes in membership.values() for ticker in codes})
            summary["historical_distinct_tickers"] = len(all_codes)
            summary["historical_tickers"] = all_codes
            summary["month_end_tickers"] = {k: len(v) for k, v in membership.items()}
            summary["month_end_membership"] = membership
            summary.update(status="complete", completed_jobs=done, remaining_jobs=0)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            for key in ("KRX_ID", "KRX_PW"):
                if os.environ.get(key):
                    message = message.replace(os.environ[key], "[REDACTED]")
            self.errors.append(message)
            summary["error"] = message
        summary.update(elapsed_seconds=round(self.clock()-self.run_started, 2),
                       network_requests=self.network, cache_hits=self.hits)
        path = self._write_summary(summary)
        self.progress(f"summary={path} status={summary['status']} network={self.network} cache_hits={self.hits}")
        if summary["status"] == "incomplete":
            raise RuntimeError(f"bulk download incomplete; see {path} for redacted error")
        return summary
