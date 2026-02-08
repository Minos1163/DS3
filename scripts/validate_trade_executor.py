from src.trading.trade_executor import TradeExecutor

from src.trading.intent_builder import IntentBuilder

from src.trading.intents import PositionSide as IntentPositionSide

# 验证 TradeExecutor._execute_open 在异常情况下优先以交易所持仓为准
# 直接调用 _execute_open 避免装饰器导致的长时间重试

import sys
from pathlib import Path

# Ensure project root is in sys.path so `src` package can be imported
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class StateMachine:
    def __init__(self):
        self.snapshots = {}


class PositionGateway:
    def change_leverage(self, symbol, lev):
        pass


class BaseMockClient:
    def __init__(self):
        self.state_machine = StateMachine()
        self.position_gateway = PositionGateway()

    def format_quantity(self, symbol, q):
        return q

    def get_ticker(self, symbol):
        return {"lastPrice": "1"}

    def _execute_protection_v2(self, *a, **k):
        return None


# Test 1: execute_intent 抛出 [OPEN BLOCKED]，get_position 返回非 0 -> 应视为成功并创建快照


class MockClient1(BaseMockClient):
    def execute_intent(self, intent):
        raise Exception("[OPEN BLOCKED] SOLUSDT already has open position (real check via positionAmt)")

    def get_position(self, symbol, side=None):
        return {"positionAmt": "0.12", "positionSide": "SHORT"}

    def sync_state(self):
        pass


# Test 2: execute_intent 抛出，[get_position 返回 0]，但 sync_state 会创建本地快照 -> 回退成功


class MockClient2(BaseMockClient):
    def execute_intent(self, intent):
        raise Exception("[OPEN BLOCKED] simulated")

    def get_position(self, symbol, side=None):
        return {"positionAmt": "0"}

    def sync_state(self):

        class Snap:
            def __init__(self):
                self.side = IntentPositionSide.SHORT

            def is_open(self):
                return True

        self.state_machine.snapshots["SOLUSDT"] = Snap()


# Test 3: 正常返回 success


class MockClient3(BaseMockClient):
    def execute_intent(self, intent):
        return {"status": "success", "order": {"id": 123}}

    def get_position(self, symbol, side=None):
        return {"positionAmt": "0"}


def run_test(client, expect_position_exists=False):
    ex = TradeExecutor(client, config={})
    # 使用 IntentBuilder 构造 intent（与 open_short 内部一致）
    intent = IntentBuilder.build_open_short(symbol="SOLUSDT", quantity=0.12, take_profit=None, stop_loss=None)
    try:
        res = ex._execute_open(intent)
    except Exception as e:
        print("Exception from _execute_open:", e)
        return False

    print("Result:", res)
    status_ok = res.get("status") == "success"
    pos_flag = bool(res.get("position_exists", False))
    return status_ok and (pos_flag == expect_position_exists)


def main():
    ok1 = run_test(MockClient1(), expect_position_exists=True)
    ok2 = run_test(MockClient2(), expect_position_exists=True)
    ok3 = run_test(MockClient3(), expect_position_exists=False)

    all_ok = ok1 and ok2 and ok3
    print("Tests results:", {"test1": ok1, "test2": ok2, "test3": ok3})
    print("ALL OK:", all_ok)
    if not all_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
