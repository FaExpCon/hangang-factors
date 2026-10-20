"""Explicit return conventions for approximate backtest and index comparisons."""

from datetime import date
from math import sqrt
from statistics import mean, stdev

from kr_quant.backtest import BacktestResult
from kr_quant.config import Period


def metrics(series: tuple[tuple[date, float], ...]) -> dict:
    if not series or series[0][1] <= 0 or any(value < 0 for _, value in series):
        raise ValueError("nonempty nonnegative series with positive starting value required")
    values = [v for _, v in series]
    returns = [values[i] / values[i - 1] - 1 if values[i - 1] else 0.0 for i in range(1, len(values))]
    elapsed = (series[-1][0] - series[0][0]).days
    peak = values[0]
    mdd = 0.0
    for value in values:
        peak = max(peak, value)
        mdd = min(mdd, value / peak - 1)
    negative = [min(value, 0.0) for value in returns]
    downside = sqrt(mean(v * v for v in negative)) if negative else 0.0
    return {"start": series[0][0].isoformat(), "end": series[-1][0].isoformat(),
            "sessions": len(values), "total_return": values[-1] / values[0] - 1,
            "cagr": (values[-1] / values[0]) ** (365.25 / elapsed) - 1 if elapsed else None,
            "mdd": mdd,
            "sharpe": mean(returns) / stdev(returns) * sqrt(252) if len(returns) > 1 and stdev(returns) > 0 else None,
            "sortino": mean(returns) / downside * sqrt(252) if downside > 0 else None}


def report(result: BacktestResult) -> dict:
    def period_report(period: Period | None) -> dict | None:
        equity = tuple((row.date, row.equity) for row in result.equity
                       if period is None or period.start <= row.date <= period.end)
        if not equity:
            return None
        start, end = equity[0][0], equity[-1][0]
        duration = max(1, (end - start).days)
        traded = sum(event.shares * event.price for event in result.events
                     if event.action in ("buy", "sell") and start <= event.date <= end)
        avg_equity = mean(value for _, value in equity)
        return {"strategy": metrics(equity),
                "KOSPI200": metrics(tuple((day, value) for day, value in result.benchmarks["KOSPI200"] if start <= day <= end)),
                "KOSDAQ": metrics(tuple((day, value) for day, value in result.benchmarks["KOSDAQ"] if start <= day <= end)),
                "annual_turnover": traded / (2 * avg_equity) * 365.25 / duration,
                "trades": sum(event.action in ("buy", "sell") and start <= event.date <= end for event in result.events)}
    cfg = result.config
    slices = {name: period_report(getattr(cfg.periods, name)) for name in ("design", "validation", "final_test")} if cfg.periods else None
    return {"label": "APPROXIMATE" if cfg.data_mode.value == "approximate" else "STRICT",
             "conventions": "KRW equity; close-to-close returns; CAGR calendar days 365.25; daily Sharpe/Sortino 252 sessions, risk-free 0; turnover half of buy+sell fill notional (shares * fill price, excluding commission/tax) / average equity annualized; no dividends or corporate actions",
            "limitations": ["pykrx PER/EPS reflect annual reports; earnings changes may lag over a year; DART quarterly filings prioritized for v2",
                            "fixed fundamental availability lag is approximate", "current classification proxy is forward-looking",
                            "delisted histories may be missing; forced conversion is artificial, not an executable sale",
                            "transaction tax varied by year and market; verify historical rates before interpreting results"],
            "full": period_report(None), "periods": slices,
            "signal_dates": [day.isoformat() for day in result.signal_dates],
            "equity": [{"date": x.date.isoformat(), "equity": x.equity, "cash": x.cash, "risk": x.risk.value} for x in result.equity],
            "trade_log": [{"date": event.date.isoformat(), "ticker": event.ticker, "action": event.action,
                           "reason": event.reason, "shares": event.shares, "price": event.price,
                           "cash_change": event.cash_change} for event in result.events]}
