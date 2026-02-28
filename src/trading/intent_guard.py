from src.trading.intents import IntentAction, PositionSide, TradeIntent


class IntentGuardViolation(RuntimeError):
    pass


class IntentGuard:
    """
    🛡️ 风控/语义哨兵：在 Intent 进入状态机前进行深度合法性校验
    """

    @staticmethod
    def validate(intent: TradeIntent, last_price: float) -> None:
        if intent.action == IntentAction.OPEN:
            # 价格合理性校验
            if intent.side == PositionSide.LONG:
                if intent.take_profit and intent.take_profit <= last_price:
                    raise IntentGuardViolation(f"❌ 多单 TP ({intent.take_profit}) 必须高于当前价 ({last_price})")
                if intent.stop_loss and intent.stop_loss >= last_price:
                    raise IntentGuardViolation(f"❌ 多单 SL ({intent.stop_loss}) 必须低于当前价 ({last_price})")

            if intent.side == PositionSide.SHORT:
                if intent.take_profit and intent.take_profit >= last_price:
                    raise IntentGuardViolation(f"❌ 空单 TP ({intent.take_profit}) 必须低于当前价 ({last_price})")
                if intent.stop_loss and intent.stop_loss <= last_price:
                    raise IntentGuardViolation(f"❌ 空单 SL ({intent.stop_loss}) 必须高于当前价 ({last_price})")

        # 其他风控 logic 可在此扩展（如最大下单金额限制）
