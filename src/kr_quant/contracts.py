"""Point-in-time input and target-output contracts shared by strategy and executor."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from math import isfinite
from typing import Mapping, Protocol

from kr_quant.config import BacktestConfig, DataMode


class Market(str, Enum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


class SecurityType(str, Enum):
    COMMON = "common"
    PREFERRED = "preferred"
    SPAC = "spac"
    REIT = "reit"
    OTHER = "other"


class ClassificationKind(str, Enum):
    HISTORICAL = "historical"
    CURRENT_PROXY = "current_proxy"


class TradingStatus(str, Enum):
    TRADING = "trading"
    SUSPENDED = "suspended"
    MERGED = "merged"
    DELISTED = "delisted"


@dataclass(frozen=True)
class DatedValue:
    """A value known at the close of `observed_on`, unless available_on is later."""

    value: float
    observed_on: date
    available_on: date | None = None


@dataclass(frozen=True)
class Fundamental:
    """PER/PBR from one observation; available_on is mandatory in strict mode."""

    per: float
    pbr: float
    observed_on: date
    available_on: date | None = None
    eps: float | None = None
    approximate: bool = False  # Final corrected values paired with original filing date.
    operating_income: float | None = None
    total_assets: float | None = None
    net_income: float | None = None
    income_period_end: date | None = None
    income_period_assets: float | None = None

    @property
    def quality_assets(self):
        """Operating-income quality denominator must share its report period."""
        return self.income_period_assets if self.income_period_end is not None else self.total_assets


@dataclass(frozen=True)
class CloseBar:
    date: date
    close: float


@dataclass(frozen=True)
class Stock:
    ticker: str
    market: Market
    sector: str
    security_type: SecurityType | None
    is_financial: bool | None
    classification_available_on: date
    market_cap: DatedValue
    avg_trading_value_20d: DatedValue
    fundamentals: Fundamental | None
    closes: tuple[CloseBar, ...] = ()
    classification_kind: ClassificationKind = ClassificationKind.HISTORICAL
    classification_source: str = ""
    classification_retrieved_on: date | None = None
    trading_status: TradingStatus = TradingStatus.TRADING
    is_holding_company: bool | None = None


@dataclass(frozen=True)
class IndexHistory:
    market: Market
    closes: tuple[CloseBar, ...]


@dataclass(frozen=True)
class Snapshot:
    """Data visible after `signal_date` close; never includes next-day execution prices."""

    signal_date: date
    stocks: tuple[Stock, ...]
    indices: tuple[IndexHistory, ...]


@dataclass(frozen=True)
class Targets:
    """Desired weights; explicit zeroes request exits with reason codes, not fills."""

    signal_date: date
    weights: Mapping[str, float]
    exit_reasons: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


class StrategyEngine(Protocol):
    def targets(self, snapshot: Snapshot, holdings: Mapping[str, float], risk_state: str) -> Targets:
        """Validate snapshot before scoring; do not execute trades here."""
        ...


def _check_date(label: str, observed: date, signal: date, available: date | None = None) -> None:
    if observed > signal or (available is not None and (available < observed or available > signal)):
        raise ValueError(f"{label} is not available by signal-date close")


def _check_closes(label: str, closes: tuple[CloseBar, ...], signal: date) -> None:
    dates = [bar.date for bar in closes]
    if dates != sorted(set(dates)):
        raise ValueError(f"{label} dates must be unique and chronological")
    for bar in closes:
        _check_date(label, bar.date, signal)
        if not isfinite(bar.close) or bar.close <= 0:
            raise ValueError(f"{label} close must be positive and finite")


def validate_snapshot(snapshot: Snapshot, config: BacktestConfig) -> None:
    """Reject future/unavailable input; strict mode never infers filing availability."""

    tickers: set[str] = set()
    for stock in snapshot.stocks:
        if not stock.ticker or stock.ticker in tickers:
            raise ValueError("stock tickers must be nonempty and unique")
        tickers.add(stock.ticker)
        if not isinstance(stock.trading_status, TradingStatus):
            raise ValueError(f"invalid trading status for {stock.ticker}")
        if stock.classification_kind is ClassificationKind.CURRENT_PROXY:
            if config.data_mode is DataMode.STRICT:
                raise ValueError(f"strict mode rejects current-classification proxy for {stock.ticker}")
            if not stock.classification_source.strip() or stock.classification_retrieved_on is None:
                raise ValueError(f"{stock.ticker} proxy requires source and retrieval date")
            if stock.classification_available_on != stock.classification_retrieved_on:
                raise ValueError(f"{stock.ticker} proxy date must equal retrieval date")
        elif stock.classification_kind is ClassificationKind.HISTORICAL:
            _check_date(f"{stock.ticker} classification", stock.classification_available_on, snapshot.signal_date)
            if stock.classification_retrieved_on is not None:
                raise ValueError("historical classification must not carry proxy retrieval date")
        else:
            raise ValueError("unknown classification kind")
        for label, datum in (("market cap", stock.market_cap), ("trading value", stock.avg_trading_value_20d)):
            _check_date(f"{stock.ticker} {label}", datum.observed_on, snapshot.signal_date, datum.available_on)
            if not isfinite(datum.value) or datum.value < 0:
                raise ValueError(f"{stock.ticker} {label} must be finite and nonnegative")
        if stock.fundamentals is not None:
            fund = stock.fundamentals
            if config.data_mode is DataMode.STRICT and fund.approximate:
                raise ValueError("strict mode rejects approximate corrected fundamentals")
            _check_date(f"{stock.ticker} fundamentals", fund.observed_on, snapshot.signal_date, fund.available_on)
            if not isfinite(fund.per) or not isfinite(fund.pbr):
                raise ValueError("PER/PBR must be finite; nonpositive values are filtered in Stage 3")
            if fund.eps is not None and not isfinite(fund.eps):
                raise ValueError("EPS must be finite when supplied")
            for label, value in (("operating income", fund.operating_income),
                                 ("total assets", fund.total_assets), ("net income", fund.net_income),
                                 ("income-period assets", fund.income_period_assets)):
                if value is not None and not isfinite(value):
                    raise ValueError(f"{label} must be finite when supplied")
            if fund.income_period_end is not None:
                _check_date(f"{stock.ticker} income period", fund.income_period_end, snapshot.signal_date)
            if config.data_mode is DataMode.STRICT and fund.available_on is None:
                raise ValueError(f"strict mode requires {stock.ticker} fundamental availability date")
            if config.data_mode is DataMode.APPROXIMATE and fund.observed_on + timedelta(days=config.fundamental_lag_days) > snapshot.signal_date:
                raise ValueError(f"{stock.ticker} fundamentals have not passed the fixed lag")
        _check_closes(f"{stock.ticker} prices", stock.closes, snapshot.signal_date)
    markets: set[Market] = set()
    for index in snapshot.indices:
        if index.market in markets:
            raise ValueError("index histories must have unique markets")
        markets.add(index.market)
        _check_closes(f"{index.market.value} index", index.closes, snapshot.signal_date)


def validate_targets(targets: Targets, snapshot: Snapshot) -> None:
    """Validate a strategy decision separately from executor fill mechanics."""

    if targets.signal_date != snapshot.signal_date:
        raise ValueError("targets and snapshot must share a signal date")
    allowed = {stock.ticker for stock in snapshot.stocks}
    if any(ticker not in allowed or not isfinite(weight) or weight < 0 or weight > 1 for ticker, weight in targets.weights.items()):
        raise ValueError("target tickers/weights must be present, finite and between 0 and 1")
    if sum(targets.weights.values()) > 1 + 1e-9:
        raise ValueError("target weights cannot exceed 100%")
    if any(ticker not in targets.weights or targets.weights[ticker] != 0 or not reasons
           or not all(isinstance(reason, str) and reason for reason in reasons)
           for ticker, reasons in targets.exit_reasons.items()):
        raise ValueError("exit reasons require a zero-weight target and nonempty reason codes")
