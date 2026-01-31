from typing import TYPE_CHECKING
from src.trading.events import ExchangeEvent, ExchangeEventType

if TYPE_CHECKING:
    from src.trading.position_state_machine import PositionStateMachineV2

class ExchangeEventRouter:
    """
    事件调度器：将外部交易所事件路由至状态机的相应处理方法
    """
    def __init__(self, state_machine: "PositionStateMachineV2"):
        self.sm = state_machine

    def dispatch(self, event: ExchangeEvent):
        """主分发入口"""
        if event.type == ExchangeEventType.ORDER_FILLED:
            self._on_order_filled(event)

        elif event.type == ExchangeEventType.ORDER_CANCELED:
            self._on_order_canceled(event)

        elif event.type == ExchangeEventType.POSITION_UPDATE:
            self._on_position_update(event)

    def _on_order_filled(self, e: ExchangeEvent):
        self.sm.on_order_filled(
            symbol=e.symbol,
            position_side=e.position_side,
            order_id=e.order_id,
            filled_qty=e.filled_qty,
        )

    def _on_order_canceled(self, e: ExchangeEvent):
        self.sm.on_order_canceled(
            symbol=e.symbol,
            order_id=e.order_id,
        )

    def _on_position_update(self, e: ExchangeEvent):
        self.sm.on_position_update(
            symbol=e.symbol,
            position_amt=e.position_amt,
            position_side=e.position_side,
        )
