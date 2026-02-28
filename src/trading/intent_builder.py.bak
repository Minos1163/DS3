from typing import Optional

from src.trading.intents import IntentAction, PositionSide, TradeIntent


class IntentBuilder:
    """交易意图构建器 V2"""

    @staticmethod
    def build_open_long(
        symbol: str,
        quantity: float,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> TradeIntent:
        """构建开多仓意图"""
        return TradeIntent(
            symbol=symbol,
            action=IntentAction.OPEN,
            side=PositionSide.LONG,
            quantity=quantity,
            order_type="MARKET",  # ✅ 默认 MARKET
            reduce_only=False,
            take_profit=take_profit,
            stop_loss=stop_loss,
            reason="开多仓",
        )

    @staticmethod
    def build_open_short(
        symbol: str,
        quantity: float,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> TradeIntent:
        """构建开空仓意图"""
        return TradeIntent(
            symbol=symbol,
            action=IntentAction.OPEN,
            side=PositionSide.SHORT,
            quantity=quantity,
            order_type="MARKET",  # ✅ 默认 MARKET
            reduce_only=False,
            take_profit=take_profit,
            stop_loss=stop_loss,
            reason="开空仓",
        )

    @staticmethod
    def build_close(
        symbol: str, side: PositionSide, quantity: Optional[float] = None
    ) -> TradeIntent:
        """
        构建平仓意图
        - quantity=None 或 quantity=0 表示全仓平掉（PAPI 需要同时传 closePosition=True 和 quantity）
        - quantity>0 表示部分平仓（使用 quantity + reduceOnly=True）
        - 自动处理 reduce_only
        """
        if quantity is None or quantity == 0:
            # 全仓平仓：reduce_only=False，实际使用 closePosition=True
            # 注意：PAPI 全仓平仓需要同时传 quantity 和 closePosition
            reduce_only = False
        else:
            # 部分平仓：明确指定 reduceOnly=True
            reduce_only = True

        return TradeIntent(
            symbol=symbol,
            action=IntentAction.CLOSE,
            side=side,
            quantity=quantity,
            order_type="MARKET",  # ✅ 平仓也默认 MARKET
            reduce_only=reduce_only,
            take_profit=None,
            stop_loss=None,
            reason="平仓",
        )

    @staticmethod
    def build_reduce(symbol: str, side: PositionSide, quantity: float) -> TradeIntent:
        """专门用于部分平仓（使用 CLOSE action）"""
        return IntentBuilder.build_close(symbol, side, quantity)

    @staticmethod
    def build_set_protection(
        symbol: str, tp: Optional[float] = None, sl: Optional[float] = None
    ) -> TradeIntent:
        return TradeIntent(
            symbol=symbol,
            action=IntentAction.SET_PROTECTION,
            take_profit=tp,
            stop_loss=sl,
            order_type=None,
            reason=None,
        )
