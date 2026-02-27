from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class Operation(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"


class TimeInForce(str, Enum):
    IOC = "Ioc"
    GTC = "Gtc"


class ExecutionMode(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


@dataclass
class FundFlowDecision:
    operation: Operation
    symbol: str
    target_portion_of_balance: float = 0.0
    leverage: int = 1
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.IOC
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    tp_execution: ExecutionMode = ExecutionMode.LIMIT
    sl_execution: ExecutionMode = ExecutionMode.LIMIT
    reason: str = ""
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation.value,
            "symbol": self.symbol,
            "target_portion_of_balance": self.target_portion_of_balance,
            "leverage": self.leverage,
            "max_price": self.max_price,
            "min_price": self.min_price,
            "time_in_force": self.time_in_force.value,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "tp_execution": self.tp_execution.value,
            "sl_execution": self.sl_execution.value,
            "reason": self.reason,
            "metadata": self.metadata or {},
        }

