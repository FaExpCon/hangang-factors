"""Cross-sectional percentile factor scores for point-in-time eligible stocks."""

from dataclasses import dataclass, field
from typing import Mapping

from kr_quant.config import BacktestConfig, QualityMetric
from kr_quant.contracts import Fundamental, Snapshot
from kr_quant.universe import filter_universe


@dataclass(frozen=True)
class FactorScore:
    ticker: str
    value: float
    approximate_roe: float  # PBR / PER, not filed ROE
    quality: float
    momentum_return: float
    momentum: float
    composite: float
    quality_raw: float | None = None
    quality_metric: QualityMetric = QualityMetric.PBR_PER


@dataclass(frozen=True)
class ScoredUniverse:
    scores: tuple[FactorScore, ...]
    rejected: Mapping[str, tuple[str, ...]]
    accounting_flags: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def accounting_flags(fundamental: Fundamental | None, ratio: float = 3.0) -> tuple[str, ...]:
    """Possible one-off gains, not proof of cause and never an eligibility rule."""
    if fundamental is None or fundamental.net_income is None or fundamental.operating_income is None:
        return ()
    net, operating = fundamental.net_income, fundamental.operating_income
    if net <= 0:
        return ()
    if operating <= 0:
        return ("positive_net_income_with_nonpositive_operating_income",)
    if net > ratio * operating:
        return ("net_income_above_operating_income_multiple",)
    return ()


def _percentiles(values: Mapping[str, float]) -> dict[str, float]:
    """Higher raw value is better; tied ranks average, singleton scores 1."""
    ordered = sorted(values.items(), key=lambda pair: (pair[1], pair[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    result = {}
    i = 0
    while i < len(ordered):
        end = i + 1
        while end < len(ordered) and ordered[end][1] == ordered[i][1]:
            end += 1
        rank = ((i + end - 1) / 2) / (len(ordered) - 1)
        for ticker, _ in ordered[i:end]:
            result[ticker] = rank
        i = end
    return result


def score_factors(snapshot: Snapshot, config: BacktestConfig) -> ScoredUniverse:
    universe = filter_universe(snapshot, config)
    stocks = universe.eligible
    flags = {s.ticker: reasons for s in snapshot.stocks
             if (reasons := accounting_flags(s.fundamentals, config.net_income_operating_income_flag_ratio))}
    if not stocks:
        return ScoredUniverse((), universe.rejected, flags)
    per = _percentiles({s.ticker: -s.fundamentals.per for s in stocks})
    pbr = _percentiles({s.ticker: -s.fundamentals.pbr for s in stocks})
    roe = {s.ticker: s.fundamentals.pbr / s.fundamentals.per for s in stocks}
    quality_raw = ({s.ticker: s.fundamentals.operating_income / s.fundamentals.quality_assets for s in stocks}
                   if config.quality_metric is QualityMetric.OPERATING_INCOME_TO_ASSETS else roe)
    quality = _percentiles(quality_raw)
    calendars = {index.market: tuple(bar.date for bar in index.closes) for index in snapshot.indices}
    momentum_raw = {}
    for stock in stocks:
        closes = {bar.date: bar.close for bar in stock.closes}
        days = calendars[stock.market]
        momentum_raw[stock.ticker] = closes[days[-22]] / closes[days[-253]] - 1
    momentum = _percentiles(momentum_raw)
    weights = config.factors
    scores = []
    for stock in stocks:
        ticker = stock.ticker
        value = weights.inverse_per * per[ticker] + weights.inverse_pbr * pbr[ticker]
        composite = weights.value * value + weights.quality * quality[ticker] + weights.momentum * momentum[ticker]
        scores.append(FactorScore(ticker, value, roe[ticker], quality[ticker], momentum_raw[ticker], momentum[ticker],
                                  composite, quality_raw[ticker], config.quality_metric))
    return ScoredUniverse(tuple(sorted(scores, key=lambda s: (-s.composite, s.ticker))), universe.rejected, flags)
