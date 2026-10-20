"""Offline KRX/OpenDART assembler. No client, credentials, or network fallback.

Daily files are read once into a (date, ticker) table. Prices chain forward
from the first observed close using FLUC_RT, so later splits cannot change
earlier snapshots. Both execution opens and closes use that same price space.

KRX basic-info supplies historical security type. Cached OpenDART company
overviews supply approximate current KSIC divisions and financial status.
Final corrected accounts use ORIGINAL filing availability by explicit policy;
this is approximate and does not reconstruct original-as-filed vintages.

Fiscal-period policy: all periods are preserved. The latest already-public
period supplies balance-sheet values, even when it is a transition period.
Income-based values (net income, operating income, PER, EPS, earnings yield,
quality) come only from the latest already-public 12-month period; short
periods are never annualized or mixed into income metrics.
"""

from calendar import monthrange
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from math import isfinite
from pathlib import Path
import re

import pandas as pd

from kr_quant.backtest import DailyBar
from kr_quant.config import BacktestConfig, DataMode
from kr_quant.company_overview import DEFAULT_CACHE, load_current_classifications
from kr_quant.contracts import (ClassificationKind, CloseBar, DatedValue, Fundamental, IndexHistory,
                                Market, SecurityType, Snapshot, Stock,
                                TradingStatus, validate_snapshot)


SERVICES = {Market.KOSPI: ("stk_bydd_trd", "stk_isu_base_info"),
            Market.KOSDAQ: ("ksq_bydd_trd", "ksq_isu_base_info")}
FIELDS = {"open": "TDD_OPNPRC", "high": "TDD_HGPRC", "low": "TDD_LWPRC",
          "close": "TDD_CLSPRC", "volume": "ACC_TRDVOL", "value": "ACC_TRDVAL",
          "change_pct": "FLUC_RT", "market_cap": "MKTCAP", "shares": "LIST_SHRS"}


def number(value):
    if value is None or pd.isna(value) or str(value).strip() in ("", "-"):
        return None
    result = float(str(value).replace(",", ""))
    if not isfinite(result):
        raise ValueError("Nonfinite cached number")
    return result


def month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(month + "-01")
    return first, date(first.year, first.month, monthrange(first.year, first.month)[1])


def cached_date(value: str) -> date:
    """Accept compact provider dates on Python 3.10 as well as ISO dates."""
    return datetime.strptime(value, "%Y%m%d").date() if len(value) == 8 else date.fromisoformat(value)


@dataclass(frozen=True)
class AnnualAccounts:
    period_end: date
    available_on: date
    income: float | None
    equity: float | None
    basis: str
    corrected: bool
    period_start: date | None = None
    period_months: int | None = None
    receipt_available_on: date | None = None
    operating_income: float | None = None
    total_assets: float | None = None
    income_period_end: date | None = None
    income_available_on: date | None = None
    income_period_months: int | None = None


def period_records(records):
    """One assembler observation per period, with that period's filing date."""
    for record in records:
        periods = record.get("fiscal_periods")
        if not periods:
            yield record
            continue
        for p in periods:
            accounts = p.get("accounts")
            if accounts is None:
                accounts = {k: a for k, a in record.get("accounts", {}).items()
                            if a.get("period_end") == p["end"] or len(periods) == 1}
            yield {**record, "filing_date": p["first_filing_date"], "accounts": accounts,
                   "fiscal_periods": [p], "filings": [{"report_name": p["report_name"],
                    "filing_date": p["first_filing_date"], "receipt_no": p.get("receipt_no", ""),
                    "had_correction": p.get("had_correction", False)}]}


def load_fundamentals(path: Path) -> dict[str, tuple[AnnualAccounts, ...]]:
    """Use matched CFS income/equity, otherwise matched OFS; never mix bases."""
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    result: dict[str, list[AnnualAccounts]] = {}
    seen = set()
    for record in period_records(records):
        ticker, year = record["stock_code"], int(record["year"])
        filings = record.get("filings", [])
        periods = {match.group(0) for filing in filings
                   for match in re.finditer(r"\(\d{4}\.\d{2}\)", filing.get("report_name", ""))}
        if len(periods) != 1:
            continue  # A fiscal-year label alone does not establish a report date.
        period = next(iter(periods))[1:-1]
        period_year, month = map(int, period.split("."))
        if period_year != year:
            raise ValueError(f"Fiscal year/report mismatch: {ticker} {year}")
        observed = date(year, month, monthrange(year, month)[1])
        if (ticker, observed) in seen:
            raise ValueError(f"Ambiguous duplicate financial period: {ticker} {observed}")
        seen.add((ticker, observed))
        explicit = record.get("fiscal_periods")
        if explicit:
            if len(explicit) != 1:
                raise ValueError("Multiple fiscal periods in one company-year record")
            period_meta = explicit[0]
            period_start = date.fromisoformat(period_meta["start"]) if period_meta.get("start") else None
            months = period_meta.get("months")
            if period_start is not None and months != (observed.year - period_start.year) * 12 + observed.month - period_start.month + 1:
                raise ValueError("Invalid fiscal period length")
            is_full = months == 12
        else:
            period_start = None
            months = None
            is_full = True  # Legacy labels carry one period; length unknown.
        dates = [record.get("filing_date")]
        corrected = any(f.get("had_correction", False) for f in filings)
        receipt_dates = []
        receipt_unknown = False
        for filing in filings:
            dates.append(filing.get("filing_date"))
            receipt = filing.get("receipt_no", "")
            if len(receipt) >= 8 and receipt[:8].isdigit():
                receipt_dates.append(cached_date(receipt[:8]))
            elif filing.get("had_correction"):
                receipt_unknown = True
        if not dates or any(not d for d in dates):
            continue
        available = min(cached_date(d) if isinstance(d, str) else d for d in dates)
        receipt_available = None if receipt_unknown else max([available, *receipt_dates,
                                                              *(cached_date(d) if isinstance(d, str) else d for d in dates)])
        if available < observed:
            continue
        accounts = list(record.get("accounts", {}).values())
        for basis in ("CFS", "OFS"):
            def amount(names, statements):
                matches = [(-abs(number(a.get("amount"))) if a.get("account_nm") == "영업손실"
                            and number(a.get("amount")) is not None else number(a.get("amount"))) for a in accounts
                           if a.get("fs_div") == basis and a.get("sj_div") in statements
                           and a.get("account_nm") in names]
                matches = [v for v in matches if v is not None]
                return matches[0] if matches and len(set(matches)) == 1 else None
            income = amount(("당기순이익", "당기순이익(손실)"), ("IS", "CIS"))
            equity = amount(("자본총계",), ("BS",))
            if equity is not None and (income is not None or not is_full):
                operating = amount(("영업이익", "영업이익(손실)", "영업손실"), ("IS", "CIS"))
                assets = amount(("자산총계",), ("BS",))
                result.setdefault(ticker, []).append(AnnualAccounts(
                    observed, available, income if is_full else None, equity, basis, corrected,
                    period_start, months, receipt_available,
                    operating if is_full else None, assets,
                    observed if is_full else None, available if is_full else None,
                    months if is_full else None))
                break
    return {ticker: tuple(sorted(items, key=lambda r: (r.period_end, r.available_on)))
            for ticker, items in result.items()}


class BulkCachedData:
    """Read complete calendar months and 400 calendar days of warm-up, offline."""

    def __init__(self, start: str, end: str, *,
                 cache_dir=Path("data/cache/krx_open_api/v1"),
                 financials_path=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"),
                 company_cache_dir=DEFAULT_CACHE, disclosure_dir=Path("cache/opendart/list")):
        self.start, _ = month_bounds(start)
        _, self.end = month_bounds(end)
        if self.start > self.end:
            raise ValueError("Start month follows end month")
        self.cache_dir = Path(cache_dir)
        self.files_read = 0
        self.daily_files_read = 0
        self.fundamentals = load_fundamentals(Path(financials_path))
        self.current_classifications = load_current_classifications(
            Path(financials_path), company_cache_dir, disclosure_dir)
        warmup = self.start - timedelta(days=400)
        calendar = []
        indices = {"KOSPI": {}, "KOSPI200": {}, "KOSDAQ": {}}
        day = warmup
        while day <= self.end:
            if day.weekday() < 5:
                rows = self._read("kospi_dd_trd", day)
                if rows:
                    calendar.append(day)
                    indices["KOSPI"][day] = self._index(rows, "코스피", day)
                    indices["KOSPI200"][day] = self._index(rows, "코스피 200", day)
                    indices["KOSDAQ"][day] = self._index(self._read("kosdaq_dd_trd", day), "코스닥", day)
            day += timedelta(days=1)
        self.trading_days = tuple(calendar)
        if not calendar:
            raise ValueError("No cached trading calendar")
        self.index_closes = indices
        self.month_ends = tuple(d for i, d in enumerate(calendar)
                               if d >= self.start and (i == len(calendar)-1 or
                                  (d.year, d.month) != (calendar[i+1].year, calendar[i+1].month)))
        expected = pd.period_range(start, end, freq="M")
        if [str(d)[:7] for d in self.month_ends] != [str(p) for p in expected]:
            raise ValueError("Missing calendar month")
        self.members = {}
        self.membership_mismatches = {}
        for signal in self.month_ends:
            members = {}
            for market, (_, service) in SERVICES.items():
                basic_rows = self._read(service, signal)
                if not basic_rows:
                    raise ValueError(f"Empty basic-info snapshot: {service} {signal}")
                for row in basic_rows:
                    ticker = row["ISU_SRT_CD"]
                    if ticker in members:
                        raise ValueError(f"Duplicate basic-info ticker: {signal} {ticker}")
                    if row.get("LIST_DD") and cached_date(row["LIST_DD"]) > signal:
                        raise ValueError(f"Future listing in historical snapshot: {signal} {ticker}")
                    members[ticker] = (market, row)
            self.members[signal] = members
        frames = []
        self.names = {}
        for day in calendar:
            records = []
            for market, (service, _) in SERVICES.items():
                rows = self._read(service, day)
                self.daily_files_read += 1
                if not rows:
                    raise ValueError(f"Empty trading data: {service} {day}")
                for row in rows:
                    ticker = row["ISU_CD"]
                    if row.get("BAS_DD") != day.strftime("%Y%m%d"):
                        raise ValueError(f"Daily date mismatch: {day} {ticker}")
                    if not re.fullmatch(r"[0-9A-Z]{6}", ticker):
                        raise ValueError("Daily ticker must be a six-character KRX short code")
                    records.append({"ticker": ticker, "market": market.value,
                                    **{key: number(row.get(field)) for key, field in FIELDS.items()}})
                    if day in self.members:
                        self.names[(day, ticker)] = row.get("ISU_NM", ticker)
            frame = pd.DataFrame.from_records(records).set_index("ticker")
            if frame.index.has_duplicates:
                raise ValueError(f"Duplicate daily ticker: {day}")
            frames.append(frame)
        self.daily = pd.concat(frames, keys=calendar, names=["date", "ticker"])
        self.daily["market"] = self.daily["market"].astype("category")
        del frames
        self._adjust_prices()
        self.daily["avg_value_20d"] = (self.daily["value"].unstack("ticker")
                                      .reindex(self.trading_days).rolling(20, min_periods=20)
                                      .mean().stack())
        self._by_ticker = {ticker: frame.droplevel("ticker") for ticker, frame in
                           self.daily.groupby(level="ticker", sort=False)}
        self._closes = {}
        for ticker, frame in self._by_ticker.items():
            bars = tuple(CloseBar(d, float(v)) for d, v in frame["adjusted_close"].items()
                         if pd.notna(v) and v > 0)
            self._closes[ticker] = (tuple(b.date for b in bars), bars)
        for signal in self.month_ends:
            daily = set(self.daily.loc[signal].index)
            members = set(self.members[signal])
            self.membership_mismatches[signal] = {"daily_only": sorted(daily-members),
                                                   "basic_only": sorted(members-daily)}

    def _read(self, service, day):
        path = self.cache_dir / service / f"{day:%Y%m%d}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Offline cache miss: {service} {day}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.files_read += 1
        if not isinstance(payload, dict) or not isinstance(payload.get("OutBlock_1"), list):
            raise ValueError(f"Invalid offline cache: {service} {day}")
        return payload["OutBlock_1"]

    @staticmethod
    def _index(rows, name, day):
        matches = [r for r in rows if r.get("IDX_NM", "").strip() == name]
        if len(matches) != 1 or matches[0].get("BAS_DD") != day.strftime("%Y%m%d"):
            raise ValueError(f"Missing/ambiguous exact index: {name} {day}")
        value = number(matches[0].get("CLSPRC_IDX"))
        if value is None or value <= 0:
            raise ValueError(f"Invalid index close: {name} {day}")
        return value

    def _adjust_prices(self):
        frame = self.daily
        positive = frame["close"].gt(0)
        if (positive & (frame["change_pct"].isna() | frame["change_pct"].le(-100))).any():
            raise ValueError("Positive close requires valid FLUC_RT for split adjustment")
        # Normalize each ticker's forward cumulative returns at its first price.
        valid = frame.loc[positive]
        gross = 1 + valid["change_pct"] / 100
        chain = gross.groupby(level="ticker").cumprod()
        initial_chain = chain.groupby(level="ticker").transform("first")
        initial_close = valid["close"].groupby(level="ticker").transform("first")
        frame["adjusted_close"] = chain / initial_chain * initial_close
        frame["adjusted_open"] = frame["open"].where(frame["open"].gt(0)) * frame["adjusted_close"] / frame["close"]

    def snapshot_for(self, signal: date, config: BacktestConfig | None = None) -> Snapshot:
        config = config or BacktestConfig(data_mode=DataMode.APPROXIMATE, fundamental_lag_days=0)
        if signal not in self.members:
            raise ValueError("Signal must be a complete cached trading-calendar month-end")
        days = tuple(d for d in self.trading_days if d <= signal)
        if len(days) < 253:
            raise ValueError("Snapshot requires 253 trading sessions including signal")
        members = self.members[signal]
        current = self.daily.loc[signal].to_dict("index")
        stocks = []
        for ticker in sorted(set(current) | set(members)):
            basic_market, basic = members.get(ticker, (None, {}))
            market = Market(current[ticker]["market"]) if ticker in current else basic_market
            if basic_market is not None and market != basic_market:
                raise ValueError(f"Historical market mismatch: {signal} {ticker}")
            row = current.get(ticker, {})
            cap = row.get("market_cap", 0)
            cap = cap if pd.notna(cap) and cap > 0 else 0
            liquidity = number(row.get("avg_value_20d")) or 0
            close_dates, close_bars = self._closes.get(ticker, ((), ()))
            closes = close_bars[bisect_right(close_dates, days[-253] - timedelta(days=1)):
                                bisect_right(close_dates, signal)]
            # Zero/missing open and volume represent an unexecutable/suspended bar.
            tradable = (number(row.get("open")) or 0) > 0 and (number(row.get("volume")) or 0) > 0
            fund = None
            usable = [r for r in self.fundamentals.get(ticker, ()) if r.available_on <= signal
                      and (config.data_mode is DataMode.STRICT or
                           r.period_end + timedelta(days=config.fundamental_lag_days) <= signal)]
            if usable and cap > 0 and (number(row.get("shares")) or 0) > 0:
                latest = max(usable, key=lambda r: (r.period_end, r.available_on))
                if latest.income not in (None, 0) and latest.equity not in (None, 0):
                    fund = Fundamental(cap / latest.income, cap / latest.equity,
                                        latest.period_end, latest.available_on,
                                        latest.income / float(row["shares"]),
                                        approximate=latest.corrected,
                                        operating_income=latest.operating_income,
                                        total_assets=latest.total_assets, net_income=latest.income)
                elif latest.income is None and latest.equity not in (None, 0):
                    # Latest short period supplies balance sheet only; income metrics
                    # fall back to the latest already-public 12-month period.
                    full = [r for r in usable if r.income_period_end is not None]
                    if full:
                        income_period = max(full, key=lambda r: (r.income_period_end, r.income_available_on))
                        if income_period.income not in (None, 0) and income_period.equity is not None:
                            fund = Fundamental(cap / income_period.income, cap / latest.equity,
                                                latest.period_end, latest.available_on,
                                                income_period.income / float(row["shares"]),
                                                approximate=latest.corrected or income_period.corrected,
                                                operating_income=income_period.operating_income,
                                                total_assets=latest.total_assets,
                                                net_income=income_period.income,
                                                income_period_end=income_period.period_end,
                                                income_period_assets=income_period.total_assets)
            name = basic.get("ISU_ABBRV", "").upper().replace(" ", "")
            kind, group, segment = (basic.get(k, "") for k in
                                    ("KIND_STKCERT_TP_NM", "SECUGRP_NM", "SECT_TP_NM"))
            security = None
            if "SPAC" in segment.upper() or "스팩" in name:
                security = SecurityType.SPAC
            elif group == "부동산투자회사":
                security = SecurityType.REIT
            elif "우선주" in kind:
                security = SecurityType.PREFERRED
            elif kind == "보통주" and group in ("주권", "외국주권"):
                security = SecurityType.COMMON
            elif basic:
                security = SecurityType.OTHER
            classification = self.current_classifications.get(ticker)
            stocks.append(Stock(ticker, market, classification["sector"] if classification else "unknown",
                                security, classification["is_financial"] if classification else None,
                                classification["retrieved_on"] if classification else signal,
                                DatedValue(cap, signal), DatedValue(liquidity, signal), fund,
                                closes, classification_kind=ClassificationKind.CURRENT_PROXY if classification else ClassificationKind.HISTORICAL,
                                classification_source=(classification["source"] + "; security type from dated KRX basic-info"
                                                       if classification else "KRX dated basic-info; unknown industry/financial status"),
                                classification_retrieved_on=classification["retrieved_on"] if classification else None,
                                trading_status=TradingStatus.TRADING if tradable else TradingStatus.SUSPENDED,
                                is_holding_company=None))
        indices = tuple(IndexHistory(market, tuple(CloseBar(d, self.index_closes[name][d])
                                                   for d in days[-253:]))
                        for market, name in ((Market.KOSPI, "KOSPI200"), (Market.KOSDAQ, "KOSDAQ")))
        snapshot = Snapshot(signal, tuple(stocks), indices)
        validate_snapshot(snapshot, config)
        return snapshot

    def backtest_inputs(self, config: BacktestConfig | None = None) -> dict:
        """Arguments for run_backtest; fractional shares in adjusted price space."""
        bars = {}
        for ticker, frame in self._by_ticker.items():
            selected = frame.loc[self.start:self.end]
            bars[ticker] = {d: DailyBar(
                float(row.adjusted_open) if pd.notna(row.adjusted_open) else None,
                float(row.adjusted_close) if pd.notna(row.adjusted_close) else None,
                "trading" if row.open > 0 and row.volume > 0 else "suspended")
                for d, row in selected.iterrows()}
        return {"days": tuple(d for d in self.trading_days if d >= self.start),
                "bars": bars, "index_closes": self.index_closes,
                "snapshot_for": lambda d: self.snapshot_for(d, config)}


def make_bulk_snapshot(cache: BulkCachedData, signal_date: date,
                       config: BacktestConfig | None = None) -> Snapshot:
    return cache.snapshot_for(signal_date, config)
