import os
import sys
import time

# ensure project root on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.trading.position_state_machine import PositionStateMachineV2
from src.trading.intent_builder import IntentBuilder


class FakeBroker:
    def __init__(self, hedge_mode=False):
        self._hedge = hedge_mode
    def get_hedge_mode(self):
        return self._hedge


class FakeClient:
    def __init__(self, position_amt=0.16, hedge_mode=False):
        self.broker = FakeBroker(hedge_mode=hedge_mode)
        self._position_amt = position_amt

    def cancel_all_open_orders(self, symbol):
        pass

    def _execute_order_v2(self, params, side, reduce_only):
        # 模拟下单失败但交易所已存在仓位
        return {
            "warning": "order_failed_but_position_exists",
            "symbol": params.get("symbol"),
            "side": side,
            "error": {"code": -1116, "msg": "Invalid orderType."},
            "position_exists": True
        }

    def get_position(self, symbol, side=None):
        return {"symbol": symbol, "positionAmt": str(self._position_amt), "positionSide": "SHORT"}

    def _execute_protection_v2(self, symbol, side, tp, sl):
        return {"status": "success", "orders": []}


def test_open_short_handles_existing_position():
    client = FakeClient(position_amt=0.16, hedge_mode=False)
    sm = PositionStateMachineV2(client)

    intent = IntentBuilder.build_open_short(symbol="SOLUSDT", quantity=0.16, take_profit=1.0, stop_loss=2.0)
    res = sm.apply_intent(intent)

    assert res.get("status") == "success"
    assert res.get("position_exists") is True or "open" in res
    assert "SOLUSDT" in sm.snapshots
    snap = sm.snapshots["SOLUSDT"]
    assert abs(snap.quantity - 0.16) < 1e-8
