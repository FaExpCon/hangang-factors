"""Daily, long-only simulation; no broker or live-trading integration."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from math import isfinite
from typing import Callable, Mapping

from kr_quant.config import BacktestConfig
from kr_quant.contracts import Snapshot, Targets
from kr_quant.portfolio import FactorStrategy
from kr_quant.risk import RiskLevel, RiskState


class HaltPolicy(str, Enum):
    """Simulation policies only; RiskState's operator/manual rule is unchanged."""

    NONE = "none"
    PERMANENT = "permanent"
    RESUME = "resume"


@dataclass(frozen=True)
class DailyBar:
    open: float | None
    close: float | None
    status: str = "trading"


@dataclass(frozen=True)
class Event:
    date: date
    ticker: str
    action: str
    reason: str
    shares: float = 0.0
    price: float = 0.0
    cash_change: float = 0.0


@dataclass(frozen=True)
class EquityPoint:
    date: date
    equity: float
    cash: float
    risk: RiskLevel
    reference_peak: float | None = None
    lifetime_peak: float | None = None


@dataclass(frozen=True)
class BacktestResult:
    equity: tuple[EquityPoint, ...]
    events: tuple[Event, ...]
    benchmarks: Mapping[str, tuple[tuple[date, float], ...]]
    signal_dates: tuple[date, ...]
    config: BacktestConfig
    halt_policy: HaltPolicy = HaltPolicy.PERMANENT


def month_ends(days: tuple[date, ...]) -> set[date]:
    return {day for i, day in enumerate(days) if i == len(days) - 1 or (days[i + 1].year, days[i + 1].month) != (day.year, day.month)}


def rebalance_dates(days, config):
    ends = month_ends(days)
    return ends if config.rebalance_frequency == "monthly" else {d for d in ends if d.month in (3, 6, 9, 12)}


def recovery_closes(days, index_closes):
    """Causal dual-index recovery dates, including supplied pre-run MA warm-up.

    Missing/invalid warm-up observations break both the MA window and streak.
    Twenty consecutive *market sessions* must each pass their trailing 200 MA.
    """
    names = ("KOSPI200", "KOSDAQ")
    calendar = sorted(set(days).union(*(index_closes[n] for n in names)))
    windows = {name: [] for name in names}
    streak = 0
    qualified = set()
    for day in calendar:
        if day > days[-1]:
            break
        above = True
        for name in names:
            value = index_closes[name].get(day)
            window = windows[name]
            if value is None or not isfinite(value) or value <= 0:
                window.clear()
                above = False
                continue
            window.append(value)
            if len(window) > 200:
                del window[0]
            above = above and len(window) == 200 and value >= sum(window) / 200
        streak = streak + 1 if above else 0
        if streak >= 20:
            qualified.add(day)
    return qualified


def run_backtest(*, days: tuple[date, ...], bars: Mapping[str, Mapping[date, DailyBar]],
                 index_closes: Mapping[str, Mapping[date, float]],
                 snapshot_for: Callable[[date], Snapshot], config: BacktestConfig,
                 initial_cash: float = 1_000_000.0,
                 manual_resumes: frozenset[date] = frozenset(),
                 halt_policy: HaltPolicy = HaltPolicy.PERMANENT,
                 target_for: Callable[[Snapshot, Mapping[str, float], RiskState], Targets] | None = None) -> BacktestResult:
    """Signals after month-end close; next-session opens, with persistent sell retries.

    Untradable shares freeze at last valid close; after N market sessions they
    convert at last close times configured recovery fraction. Conversion is
    artificial cash recovery, NEVER an executable market sell.
    An optional target callback supports diagnostic portfolios; the default is
    always FactorStrategy(config).targets and no strategy configuration changes.
    """
    if not days or tuple(sorted(set(days))) != days or not isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("days must be unique/ordered and initial cash positive")
    halt_policy = HaltPolicy(halt_policy)
    if not {"KOSPI200", "KOSDAQ"}.issubset(index_closes):
        raise ValueError("both benchmark indices required")
    for name, series in index_closes.items():
        if any(day not in series or not isfinite(series[day]) or series[day] <= 0 for day in days):
            raise ValueError(f"missing/invalid {name} index close")
    cash = initial_cash
    shares: dict[str, float] = {}
    last_close: dict[str, float] = {}
    missing_days: dict[str, int] = {}
    events: list[Event] = []
    equity: list[EquityPoint] = []
    signals: list[date] = []
    risk = RiskState(initial_cash)
    pending: Targets | None = None
    build_targets = target_for if target_for is not None else FactorStrategy(config).targets
    ends = month_ends(days)
    rebalance_ends = rebalance_dates(days, config)
    frozen_until_conversion: set[str] = set()
    deferred_sells: dict[str, tuple[float, str]] = {}
    halted_at: int | None = None
    resume_equity: float | None = None
    lifetime_peak = initial_cash
    recovered = recovery_closes(days, index_closes) if halt_policy is HaltPolicy.RESUME else set()

    def sell(ticker, quantity, reason, day):
        nonlocal cash
        bar = bars.get(ticker, {}).get(day)
        if not bar or bar.status != "trading" or bar.open is None or not isfinite(bar.open) or bar.open <= 0:
            return False
        qty = min(shares[ticker], quantity)
        price = bar.open * (1 - config.costs.slippage_bps / 10000)
        proceeds = qty * price * (1 - (config.costs.commission_bps + config.costs.transaction_tax_bps) / 10000)
        shares[ticker] -= qty
        if shares[ticker] <= 1e-9:
            del shares[ticker]
            missing_days.pop(ticker, None)
        cash += proceeds
        events.append(Event(day, ticker, "sell", reason, qty, price, proceeds))
        return True

    for session, day in enumerate(days):
        if resume_equity is not None:
            old_peak = risk.peak_equity
            risk = RiskState(resume_equity)
            events.append(Event(day, "", "auto_resume", f"qualified at prior close {equity[-1].date}; at least 63 halted sessions"))
            events.append(Event(day, "", "risk_reference_reset",
                                f"reference_peak={old_peak} -> {resume_equity}; prior_close_equity={resume_equity}; lifetime_peak={lifetime_peak}"))
            resume_equity = None
            halted_at = None
        if day in manual_resumes:
            risk = risk.manual_resume()
            halted_at = None
            events.append(Event(day, "", "manual_resume", "operator action"))
        # A returning executable open gets priority over artificial conversion.
        retried = set()
        for ticker, (quantity, reason) in list(deferred_sells.items()):
            if sell(ticker, quantity, f"deferred_sell:{reason}", day):
                del deferred_sells[ticker]
                retried.add(ticker)
        # Before the open, settle already-stale holdings using yesterday's
        # frozen mark. Today's missing close is recognized after the close.
        for ticker in list(shares):
            if missing_days.get(ticker, 0) >= config.untradable_conversion_days:
                # A newly requested full exit also deserves today's open before
                # conversion, even if it has not entered the retry queue yet.
                bar = bars.get(ticker, {}).get(day)
                if (pending is not None and pending.weights.get(ticker, 0) == 0
                        and bar and bar.status == "trading" and bar.open is not None
                        and isfinite(bar.open) and bar.open > 0):
                    continue
                recovery = shares[ticker] * last_close[ticker] * config.untradable_recovery_fraction
                cash += recovery
                events.append(Event(day, ticker, "approximate_cash_conversion",
                                    f"{missing_days[ticker]} missing sessions; recovery_fraction={config.untradable_recovery_fraction}",
                                    shares[ticker], last_close[ticker] * config.untradable_recovery_fraction, recovery))
                del shares[ticker]
                missing_days.pop(ticker, None)
                deferred_sells.pop(ticker, None)
                frozen_until_conversion.add(ticker)
        if pending is not None:
            # All sizing uses information at the execution open, never the
            # signal close. Unavailable opens yield logged unfilled orders.
            marks = {}
            for ticker, quantity in shares.items():
                bar = bars.get(ticker, {}).get(day)
                open_price = bar.open if bar and bar.status == "trading" else None
                marks[ticker] = open_price if open_price is not None and isfinite(open_price) and open_price > 0 else last_close[ticker]
            open_equity = cash + sum(quantity * marks[ticker] for ticker, quantity in shares.items())
            for ticker in sorted(set(shares) | set(pending.weights)):
                if ticker in deferred_sells:
                    continue
                desired = pending.weights.get(ticker, 0) * open_equity
                current = shares.get(ticker, 0) * marks.get(ticker, 0)
                if desired >= current - 1e-8:
                    continue
                qty = min(shares[ticker], (current - desired) / marks[ticker])
                reason = ",".join(pending.exit_reasons.get(ticker, ("rebalance",)))
                if not sell(ticker, qty, reason, day):
                    deferred_sells[ticker] = (qty, reason)
                    events.append(Event(day, ticker, "unfilled_sell", "missing/untradable next-session open"))
                    events.append(Event(day, ticker, "deferred_sell", reason, qty))
            for ticker, weight in pending.weights.items():
                if weight <= 0 or ticker in deferred_sells or ticker in retried:
                    continue
                bar = bars.get(ticker, {}).get(day)
                current = shares.get(ticker, 0) * (bar.open if bar and bar.open else last_close.get(ticker, 0))
                amount = weight * open_equity - current
                if amount <= 1e-8:
                    continue
                if (halt_policy is not HaltPolicy.NONE and risk.level is RiskLevel.HALTED) or ticker in frozen_until_conversion:
                    events.append(Event(day, ticker, "blocked_buy", "HALTED or prior untradable conversion"))
                    continue
                if not bar or bar.status != "trading" or bar.open is None or not isfinite(bar.open) or bar.open <= 0:
                    events.append(Event(day, ticker, "unfilled_buy", "missing/untradable next-session open"))
                    continue
                price = bar.open * (1 + config.costs.slippage_bps / 10000)
                cost_factor = 1 + config.costs.commission_bps / 10000
                qty = min(amount / bar.open, cash / (price * cost_factor))
                if qty <= 1e-9:
                    continue
                spent = qty * price * cost_factor
                cash -= spent
                shares[ticker] = shares.get(ticker, 0) + qty
                last_close.setdefault(ticker, bar.open)
                events.append(Event(day, ticker, "buy", f"{config.rebalance_frequency}_rebalance", qty, price, -spent))
            pending = None
        for ticker in list(shares):
            bar = bars.get(ticker, {}).get(day)
            if bar and bar.status == "trading" and bar.close is not None and isfinite(bar.close) and bar.close > 0:
                last_close[ticker] = bar.close
                missing_days[ticker] = 0
            else:
                missing_days[ticker] = missing_days.get(ticker, 0) + 1
                if missing_days[ticker] == 1:
                    events.append(Event(day, ticker, "frozen", "missing close or suspended/merged/delisted; last close retained"))
        total = cash + sum(quantity * last_close[ticker] for ticker, quantity in shares.items())
        before = risk.level
        risk = risk.observe(total, config)
        lifetime_peak = max(lifetime_peak, total)
        if risk.level is not before:
            events.append(Event(day, "", "risk_state", risk.level.value))
            if risk.level is RiskLevel.HALTED:
                halted_at = session
        equity.append(EquityPoint(day, total, cash, risk.level, risk.peak_equity, lifetime_peak))
        if day in ends and day != days[-1]:
            if (halt_policy is HaltPolicy.RESUME and risk.level is RiskLevel.HALTED
                    and halted_at is not None and session > halted_at
                    and session - halted_at >= 63 and day in recovered and total > 0):
                resume_equity = total
                events.append(Event(day, "", "resume_scheduled",
                                    f"{session - halted_at} full sessions since halt close; both indices >= own 200-session MA for 20 sessions; next open"))
            if day not in rebalance_ends:
                continue  # Daily risk/marks and monthly recovery checks still run.
            weights = {ticker: quantity * last_close[ticker] / total for ticker, quantity in shares.items()} if total > 0 else {}
            # Frozen holdings cannot be valued as executable sells. The
            # executor holds them outside strategy selection until conversion.
            tradable_weights = {ticker: weight for ticker, weight in weights.items() if missing_days.get(ticker, 0) == 0}
            try:
                snapshot = snapshot_for(day)
                selection_risk = RiskState(risk.peak_equity) if halt_policy is HaltPolicy.NONE or resume_equity is not None else risk
                pending = build_targets(snapshot, tradable_weights, selection_risk)
                if frozen_until_conversion:
                    pending = Targets(day, {ticker: weight for ticker, weight in pending.weights.items() if ticker not in frozen_until_conversion},
                                      {ticker: reason for ticker, reason in pending.exit_reasons.items() if ticker not in frozen_until_conversion})
            except ValueError as exc:
                raise ValueError(f"signal {day} failed: {exc}") from exc
            signals.append(day)
    benchmarks = {name: tuple((day, series[day]) for day in days) for name, series in index_closes.items()}
    return BacktestResult(tuple(equity), tuple(events), benchmarks, tuple(signals), config, halt_policy)
