# intents.py
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class IntentAction(Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    REDUCE = "REDUCE"
    SET_PROTECTION = "SET_PROTECTION"
    UPDATE_PROTECTION = "UPDATE_PROTECTION"
    CANCEL_PROTECTION = "CANCEL_PROTECTION"


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class TradeIntent:
    """
    🔥 Strategy → System 的唯一通信协议
    """
    symbol: str
    action: IntentAction
    side: Optional[PositionSide] = None

    # 仓位参数
    quantity: Optional[float] = None
    leverage: Optional[int] = None
    order_type: Optional[str] = None  # MARKET, LIMIT, etc.
    reduce_only: Optional[bool] = None  # 用于部分平仓（CLOSE/REDUCE）

    # 保护参数
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None

    # 语义标签（日志 / 回测 / Debug）
    reason: Optional[str] = None
