"""Point-in-time eligibility; rejected names remain visible for audit."""

from dataclasses import dataclass
from math import floor
from typing import Mapping

from kr_quant.config import BacktestConfig, QualityMetric
from kr_quant.contracts import SecurityType, Snapshot, Stock, TradingStatus, validate_snapshot


@dataclass(frozen=True)
class Universe:
    eligible: tuple[Stock, ...]
    rejected: Mapping[str, tuple[str, ...]]


def filter_universe(snapshot: Snapshot, config: BacktestConfig) -> Universe:
    """Exclude lowest floor(n * fraction) among stocks with current positive caps.

    Bottom-cap population is *all* common, nonfinancial, classified stocks
    with signal-day market caps; liquidity/fundamental exclusions do not change
    the cap threshold. Ties are resolved by ticker for reproducibility.
    """
    validate_snapshot(snapshot, config)
    calendars = {index.market: tuple(bar.date for bar in index.closes if bar.date <= snapshot.signal_date)
                 for index in snapshot.indices}
    initial: dict[str, list[str]] = {}
    cap_pool: list[Stock] = []
    for stock in snapshot.stocks:
        reasons: list[str] = []
        if stock.trading_status is not TradingStatus.TRADING:
            reasons.append(f"untradable_{stock.trading_status.value}")
        if stock.security_type is None:
            reasons.append("unknown_security_type")
        elif stock.security_type is not SecurityType.COMMON:
            reasons.append("not_common")
        if stock.is_financial is None:
            reasons.append("unknown_financial_status")
        elif stock.is_financial:
            reasons.append("financial")
        if config.exclude_holding_companies and stock.is_holding_company is True:
            reasons.append("holding_company")
        if not stock.sector.strip():
            reasons.append("unknown_sector")
        if stock.market_cap.observed_on != snapshot.signal_date or stock.market_cap.value <= 0:
            reasons.append("missing_current_market_cap")
        if not reasons:
            cap_pool.append(stock)
        initial[stock.ticker] = reasons

    for stock in sorted(cap_pool, key=lambda s: (s.market_cap.value, s.ticker))[:floor(len(cap_pool) * config.market_cap_exclusion_fraction)]:
        initial[stock.ticker].append("bottom_market_cap")

    eligible = []
    for stock in snapshot.stocks:
        reasons = initial[stock.ticker]
        if stock.avg_trading_value_20d.observed_on != snapshot.signal_date:
            reasons.append("missing_current_liquidity")
        elif stock.avg_trading_value_20d.value < config.minimum_avg_trading_value_20d:
            reasons.append("low_liquidity")
        fundamental = stock.fundamentals
        if fundamental is None:
            reasons.append("missing_fundamentals")
        elif fundamental.per <= 0 or fundamental.pbr <= 0 or fundamental.eps is None or fundamental.eps <= 0:
            reasons.append("nonpositive_or_missing_per_pbr_eps")
        if fundamental is not None:
            if config.exclude_nonpositive_operating_income:
                if fundamental.operating_income is None:
                    reasons.append("missing_operating_income")
                elif fundamental.operating_income <= 0:
                    reasons.append("nonpositive_operating_income")
            if config.quality_metric is QualityMetric.OPERATING_INCOME_TO_ASSETS:
                if fundamental.operating_income is None:
                    if "missing_operating_income" not in reasons:
                        reasons.append("missing_operating_income")
                if fundamental.quality_assets is None or fundamental.quality_assets <= 0:
                    reasons.append("missing_or_nonpositive_total_assets")
        if not stock.closes or stock.closes[-1].date != snapshot.signal_date:
            reasons.append("missing_signal_close")
        calendar = calendars.get(stock.market, ())
        if len(calendar) < 253 or calendar[-1] != snapshot.signal_date:
            reasons.append("insufficient_market_calendar")
        elif not {calendar[-22], calendar[-253]}.issubset({bar.date for bar in stock.closes}):
            reasons.append("insufficient_momentum_history")
        if not reasons:
            eligible.append(stock)
    return Universe(tuple(eligible), {ticker: tuple(reasons) for ticker, reasons in initial.items() if reasons})
