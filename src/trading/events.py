from enum import Enum
from dataclasses import dataclass
from typing import Optional

class ExchangeEventType(Enum):
    # 订单成交（包括完全成交与部分成交）
    ORDER_FILLED = "ORDER_FILLED"
    # 订单取消（手动或 API 撤单）
    ORDER_CANCELED = "ORDER_CANCELED"
    # 仓位变更（来自 ACCOUNT_UPDATE 推送或定时轮询同步）
    POSITION_UPDATE = "POSITION_UPDATE"

@dataclass
class ExchangeEvent:
    type: ExchangeEventType
    symbol: str

    order_id: Optional[int] = None           # Binance 使用 numeric ID
    side: Optional[str] = None               # BUY / SELL (订单方向)
    position_side: Optional[str] = None      # LONG / SHORT (持仓方向，Hedge 模式关键)
    filled_qty: Optional[float] = None       # 本次成交数量
    
    position_amt: Optional[float] = None     # 最新持仓净值 (用于 POSITION_UPDATE)
    
    # 扩展信息
    price: Optional[float] = None
    event_time: Optional[float] = None

    @classmethod
    def from_binance_order_update(cls, data: dict):
        """从 WebSocket ORDER_TRADE_UPDATE 转换为统一事件"""
        # 这里的 map 逻辑可以根据具体推送格式细化
        return cls(
            type=ExchangeEventType.ORDER_FILLED if data.get('X') == 'FILLED' else ExchangeEventType.ORDER_CANCELED,
            symbol=data.get('s', ''),
            order_id=data.get('i'),
            side=data.get('S'),
            position_side=data.get('ps', 'BOTH'),
            filled_qty=float(data.get('l', 0)),
            event_time=data.get('E')
        )
