"""Monthly target weights only: no orders, fills or between-month adjustments."""

from math import isfinite
from typing import Mapping

from kr_quant.config import BacktestConfig
from kr_quant.contracts import Market, Snapshot, Targets, TradingStatus, validate_targets
from kr_quant.factors import score_factors
from kr_quant.risk import RiskLevel, RiskState


class FactorStrategy:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def targets(self, snapshot: Snapshot, holdings: Mapping[str, float], risk_state: RiskState) -> Targets:
        """Rank-retained names first; fill vacancies from top entry ranks.

        Holdings are current equity fractions, not execution instructions.
        Ineligible but tradable holdings get explicit zero targets and reasons;
        untradable holdings require executor/corporate-action resolution.
        """
        if not isinstance(risk_state, RiskState):
            raise ValueError("risk_state must be RiskState")
        if any(not isfinite(v) or v < 0 or v > 1 for v in holdings.values()) or sum(holdings.values()) > 1 + 1e-9:
            raise ValueError("holdings must be finite nonnegative weights totalling <= 1")
        by_ticker = {stock.ticker: stock for stock in snapshot.stocks}
        held = {ticker for ticker, weight in holdings.items() if weight > 0}
        if held - set(by_ticker):
            raise ValueError("held ticker absent from snapshot: requires explicit delisting resolution")
        for ticker in held:
            stock = by_ticker[ticker]
            if stock.trading_status is not TradingStatus.TRADING or not stock.closes or stock.closes[-1].date != snapshot.signal_date:
                raise ValueError(f"held ticker {ticker} untradable: executor/corporate action resolution required")
        calendars = {index.market: index.closes for index in snapshot.indices}
        for market in {stock.market for stock in snapshot.stocks}:
            bars = calendars.get(market, ())
            if len(bars) < 200 or bars[-1].date != snapshot.signal_date:
                raise ValueError(f"missing current 200-session MA for {market.value}")
        scored = score_factors(snapshot, self.config)
        exposure: dict[Market, float] = {}
        for market in {by_ticker[s.ticker].market for s in scored.scores}:
            bars = calendars.get(market, ())
            mean = sum(bar.close for bar in bars[-200:]) / 200
            exposure[market] = 1.0 if bars[-1].close >= mean else self.config.below_ma_exposure

        ranks = {score.ticker: rank for rank, score in enumerate(scored.scores, start=1)}
        selected: list[str] = []
        sector_weights: dict[str, float] = {}
        def add(ticker: str) -> None:
            sector = by_ticker[ticker].sector
            if len(selected) >= self.config.max_positions:
                return
            if sector_weights.get(sector, 0) + self.config.base_weight > self.config.sector_cap + 1e-9:
                return
            selected.append(ticker)
            sector_weights[sector] = sector_weights.get(sector, 0) + self.config.base_weight

        for score in scored.scores:
            if score.ticker in held and ranks[score.ticker] <= self.config.retention_rank:
                add(score.ticker)
        if risk_state.level is not RiskLevel.HALTED:
            for score in scored.scores:
                if ranks[score.ticker] <= self.config.entry_rank and score.ticker not in held:
                    add(score.ticker)
        weights = {}
        for ticker in selected:
            desired = self.config.base_weight * exposure[by_ticker[ticker].market]
            weights[ticker] = min(desired, holdings.get(ticker, 0)) if risk_state.level is RiskLevel.HALTED else desired
        exit_reasons = {}
        for ticker in held - set(selected):
            reasons = scored.rejected.get(ticker)
            if reasons is None:
                reasons = (f"rank_outside_top_{self.config.retention_rank}",) if ranks[ticker] > self.config.retention_rank else ("portfolio_capacity_or_sector_cap",)
            weights[ticker] = 0.0
            exit_reasons[ticker] = reasons
        result = Targets(snapshot.signal_date, weights, exit_reasons)
        validate_targets(result, snapshot)
        return result
