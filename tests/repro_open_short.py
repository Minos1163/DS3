"""复现并验证：当下单返回 order_failed_but_position_exists 时，PositionStateMachine 应将其视为成功并建立快照。"""

from src.trading.position_state_machine import PositionStateMachineV2

from src.trading.intent_builder import IntentBuilder

import os
import sys

# ensure project root on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class FakeBroker:
    def __init__(self, hedge_mode=False):
        self._hedge = hedge_mode

    def get_hedge_mode(self):
        return self._hedge


class FakeClient:
    def __init__(self, position_amt=0.16, hedge_mode=False):
        self.broker = FakeBroker(hedge_mode=hedge_mode)
        self._position_amt = position_amt
        self._cancel_calls = 0
        self._protection_calls = 0

    def cancel_all_open_orders(self, symbol):
        self._cancel_calls += 1

    def _execute_order_v2(self, params, side, reduce_only):
        print(f"FakeClient._execute_order_v2 called with params={params}, side={side}, reduce_only={reduce_only}")
        # 模拟下单失败但交易所已存在仓位
        return {
            "warning": "order_failed_but_position_exists",
            "symbol": params.get("symbol"),
            "side": side,
            "error": {"code": -1116, "msg": "Invalid orderType."},
            "position_exists": True,
        }

    def get_position(self, symbol, side=None):
        # 返回一个模拟的持仓（positionAmt 表示数量）
        return {"symbol": symbol, "positionAmt": str(self._position_amt), "positionSide": "SHORT"}

    def _execute_protection_v2(self, symbol, side, tp, sl):
        self._protection_calls += 1
        return {"status": "success", "orders": []}


def run():
    client = FakeClient(position_amt=0.16, hedge_mode=False)
    sm = PositionStateMachineV2(client)

    # 构建一个开空意图
    intent = IntentBuilder.build_open_short(symbol="SOLUSDT", quantity=0.16, take_profit=1.0, stop_loss=2.0)
    print("Applying intent:", intent)
    res = sm.apply_intent(intent)
    print("Result:", res)
    print("Snapshots:", sm.snapshots)


if __name__ == "__main__":
    run()
