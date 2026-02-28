from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskConfig:
    """Lightweight risk gate configuration."""

    max_drawdown: float = 0.05
    max_exposure_per_trade: float = 0.25
    trailing_atr_mul: float = 2.0
    trend_weight: float = 0.4
    momentum_weight: float = 0.3
    volatility_weight: float = 0.2
    drawdown_weight: float = 0.3
    entry_threshold: float = 0.5

