"""High-water-mark risk state; daily updates belong to the future executor."""

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from kr_quant.config import BacktestConfig


class RiskLevel(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    HALTED = "HALTED"


@dataclass(frozen=True)
class RiskState:
    peak_equity: float
    level: RiskLevel = RiskLevel.NORMAL

    def __post_init__(self) -> None:
        if not isfinite(self.peak_equity) or self.peak_equity <= 0:
            raise ValueError("peak equity must be positive and finite")

    def observe(self, equity: float, config: BacktestConfig) -> "RiskState":
        if not isfinite(equity) or equity < 0:
            raise ValueError("equity must be finite and nonnegative")
        peak = max(self.peak_equity, equity)
        if self.level is RiskLevel.HALTED or equity <= peak * (1 - config.halt_drawdown):
            level = RiskLevel.HALTED
        elif equity <= peak * (1 - config.warning_drawdown):
            level = RiskLevel.WARNING
        else:
            level = RiskLevel.NORMAL
        return RiskState(peak, level)

    def manual_resume(self) -> "RiskState":
        """Explicit operator action; next observation can re-halt below threshold."""
        return RiskState(self.peak_equity, RiskLevel.NORMAL)
