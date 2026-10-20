"""Validated configuration; no market-data downloads or trading side effects."""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from math import isfinite


class DataMode(str, Enum):
    APPROXIMATE = "approximate"
    STRICT = "strict"


class QualityMetric(str, Enum):
    PBR_PER = "pbr_per"
    OPERATING_INCOME_TO_ASSETS = "operating_income_to_assets"


@dataclass(frozen=True)
class FactorWeights:
    value: float = 0.4
    quality: float = 0.3
    momentum: float = 0.3
    inverse_per: float = 0.5
    inverse_pbr: float = 0.5

    def __post_init__(self) -> None:
        for group in ((self.value, self.quality, self.momentum), (self.inverse_per, self.inverse_pbr)):
            if any(not isfinite(v) or v < 0 for v in group) or abs(sum(group) - 1.0) > 1e-9:
                raise ValueError("factor weight groups must be nonnegative, finite, and sum to 1")


@dataclass(frozen=True)
class Costs:
    commission_bps: float = 15.0
    transaction_tax_bps: float = 30.0
    slippage_bps: float = 10.0

    def __post_init__(self) -> None:
        if any(not isfinite(v) or v < 0 for v in (self.commission_bps, self.transaction_tax_bps, self.slippage_bps)):
            raise ValueError("costs must be finite and nonnegative basis points")


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("period start must not exceed end")


@dataclass(frozen=True)
class Periods:
    design: Period
    validation: Period
    final_test: Period

    def __post_init__(self) -> None:
        if not (self.design.end < self.validation.start and self.validation.end < self.final_test.start):
            raise ValueError("design, validation and final test must be chronological and non-overlapping")


@dataclass(frozen=True)
class BacktestConfig:
    data_mode: DataMode = DataMode.APPROXIMATE
    fundamental_lag_days: int = 90
    minimum_avg_trading_value_20d: float = 1_000_000_000.0  # KRW; adjustable
    market_cap_exclusion_fraction: float = 0.30
    factors: FactorWeights = field(default_factory=FactorWeights)
    max_positions: int = 20
    base_weight: float = 0.05
    entry_rank: int = 20
    retention_rank: int = 30
    sector_cap: float = 0.30
    below_ma_exposure: float = 0.5
    warning_drawdown: float = 0.15
    halt_drawdown: float = 0.20
    costs: Costs = field(default_factory=Costs)
    periods: Periods | None = None
    exclude_holding_companies: bool = False
    untradable_conversion_days: int = 20  # market sessions, approximate
    untradable_recovery_fraction: float = 0.0
    exclude_nonpositive_operating_income: bool = False
    quality_metric: QualityMetric = QualityMetric.PBR_PER
    net_income_operating_income_flag_ratio: float = 3.0  # Diagnostic only; not eligibility.
    rebalance_frequency: str = "monthly"

    def __post_init__(self) -> None:
        if self.rebalance_frequency not in ("monthly", "quarterly"):
            raise ValueError("rebalance_frequency must be monthly or quarterly")
        if not isinstance(self.exclude_nonpositive_operating_income, bool):
            raise ValueError("exclude_nonpositive_operating_income must be boolean")
        if not isinstance(self.quality_metric, QualityMetric):
            raise ValueError("quality_metric must be QualityMetric")
        ratio = self.net_income_operating_income_flag_ratio
        if isinstance(ratio, bool) or not isfinite(ratio) or ratio <= 1:
            raise ValueError("net/operating income flag ratio must be finite and greater than 1")
        if not isinstance(self.exclude_holding_companies, bool):
            raise ValueError("exclude_holding_companies must be boolean")
        if isinstance(self.untradable_conversion_days, bool) or not isinstance(self.untradable_conversion_days, int) or self.untradable_conversion_days < 1:
            raise ValueError("untradable_conversion_days must be a positive integer")
        if not isfinite(self.untradable_recovery_fraction) or not 0 <= self.untradable_recovery_fraction <= 1:
            raise ValueError("untradable_recovery_fraction must be between 0 and 1")
        if not isinstance(self.data_mode, DataMode):
            raise ValueError("data_mode must be a DataMode")
        if isinstance(self.fundamental_lag_days, bool) or not isinstance(self.fundamental_lag_days, int) or self.fundamental_lag_days < 0:
            raise ValueError("fundamental_lag_days must be a nonnegative integer")
        for name in ("minimum_avg_trading_value_20d", "market_cap_exclusion_fraction", "base_weight", "sector_cap", "below_ma_exposure", "warning_drawdown", "halt_drawdown"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not 0 <= self.market_cap_exclusion_fraction < 1:
            raise ValueError("market_cap_exclusion_fraction must be below 1")
        if not 0 <= self.below_ma_exposure <= 1:
            raise ValueError("below_ma_exposure must be between 0 and 1")
        if not 0 < self.warning_drawdown < self.halt_drawdown < 1:
            raise ValueError("drawdown thresholds must satisfy 0 < warning < halt < 1")
        if not 0 < self.base_weight <= self.sector_cap <= 1 or self.max_positions * self.base_weight > 1 + 1e-9:
            raise ValueError("position, sector, and total base weights are inconsistent")
        if isinstance(self.max_positions, bool) or not isinstance(self.max_positions, int) or self.max_positions < 1:
            raise ValueError("max_positions must be a positive integer")
        if not (1 <= self.entry_rank <= self.retention_rank):
            raise ValueError("rank thresholds must satisfy 1 <= entry <= retention")
