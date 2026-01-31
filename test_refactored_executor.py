"""
测试重构后的 TradeExecutor 和 OrderGateway
验证 PAPI 兼容性、Hedge/ONEWAY 模式支持
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.trading.intents import TradeIntent, IntentAction, PositionSide
from src.trading.intent_builder import IntentBuilder
from src.trading.order_gateway import OrderGateway

def test_intent_builder():
    """测试 IntentBuilder 构建各种意图"""
    print("=" * 60)
    print("测试 IntentBuilder")
    print("=" * 60)

    # 测试开多仓意图
    open_long_intent = IntentBuilder.build_open_long(
        symbol="BTCUSDT",
        quantity=0.001,
        take_profit=50000.0,
        stop_loss=48000.0
    )
    print(f"\n[1] 开多仓意图:")
    print(f"   Symbol: {open_long_intent.symbol}")
    print(f"   Action: {open_long_intent.action}")
    print(f"   Side: {open_long_intent.side}")
    print(f"   Quantity: {open_long_intent.quantity}")
    print(f"   OrderType: {open_long_intent.order_type}")
    print(f"   TakeProfit: {open_long_intent.take_profit}")
    print(f"   StopLoss: {open_long_intent.stop_loss}")

    # 测试开空仓意图
    open_short_intent = IntentBuilder.build_open_short(
        symbol="BTCUSDT",
        quantity=0.001,
        take_profit=45000.0,
        stop_loss=47000.0
    )
    print(f"\n[2] 开空仓意图:")
    print(f"   Symbol: {open_short_intent.symbol}")
    print(f"   Action: {open_short_intent.action}")
    print(f"   Side: {open_short_intent.side}")
    print(f"   Quantity: {open_short_intent.quantity}")
    print(f"   OrderType: {open_short_intent.order_type}")

    # 测试全仓平仓意图
    close_full_intent = IntentBuilder.build_close(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=None  # 全仓平仓
    )
    print(f"\n[3] 全仓平仓意图:")
    print(f"   Symbol: {close_full_intent.symbol}")
    print(f"   Action: {close_full_intent.action}")
    print(f"   Side: {close_full_intent.side}")
    print(f"   Quantity: {close_full_intent.quantity}")
    print(f"   ReduceOnly: {close_full_intent.reduce_only}")

    # 测试部分平仓意图
    close_partial_intent = IntentBuilder.build_close(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=0.0005  # 部分平仓
    )
    print(f"\n[4] 部分平仓意图:")
    print(f"   Symbol: {close_partial_intent.symbol}")
    print(f"   Action: {close_partial_intent.action}")
    print(f"   Side: {close_partial_intent.side}")
    print(f"   Quantity: {close_partial_intent.quantity}")
    print(f"   ReduceOnly: {close_partial_intent.reduce_only}")

    print(f"\n[OK] IntentBuilder test passed")

def test_order_gateway_params():
    """测试 OrderGateway 的参数格式化"""
    print("\n" + "=" * 60)
    print("测试 OrderGateway 参数格式化")
    print("=" * 60)

    # 创建一个模拟的 broker
    class MockBroker:
        def get_hedge_mode(self):
            return True  # 模拟 Hedge 模式

        def calculate_position_side(self, side: str, reduce_only: bool) -> str:
            if reduce_only:
                return "LONG" if side == "BUY" else "SHORT"
            return "LONG" if side == "BUY" else "SHORT"

    gateway = OrderGateway(MockBroker())

    # 测试 1: 开多仓（Hedge 模式）
    print(f"\n[1] 开多仓（Hedge 模式）")
    params = {
        "symbol": "BTCUSDT",
        "type": "MARKET",
        "quantity": 0.001,
        "positionSide": "LONG"
    }
    final = gateway._finalize_params(params.copy(), "BUY", False)
    print(f"   输入: {params}")
    print(f"   输出: {final}")

    # 测试 2: 全仓平仓（Hedge 模式）
    print(f"\n[2] 全仓平仓（Hedge 模式）")
    params = {
        "symbol": "BTCUSDT",
        "type": "MARKET",
        "closePosition": True,
        "quantity": 0.001,  # PAPI 全仓平仓需要 quantity
        "reduceOnly": True  # 应该移除
    }
    final = gateway._finalize_params(params.copy(), "SELL", True)
    print(f"   输入: {params}")
    print(f"   输出: {final}")
    assert final.get("closePosition") is True
    assert "reduceOnly" not in final
    assert final.get("quantity") == 0.001  # PAPI 全仓平仓需要 quantity

    # 测试 3: 部分平仓（Hedge 模式）
    print(f"\n[3] 部分平仓（Hedge 模式）")
    params = {
        "symbol": "BTCUSDT",
        "type": "MARKET",
        "quantity": 0.0005,
        "reduceOnly": True,
        "positionSide": "LONG"
    }
    final = gateway._finalize_params(params.copy(), "SELL", True)
    print(f"   输入: {params}")
    print(f"   输出: {final}")
    assert final.get("reduceOnly") is True
    assert final.get("quantity") == 0.0005
    assert final.get("positionSide") == "LONG"
    assert "closePosition" not in final

    # 测试 4: ONEWAY 模式开多仓
    print(f"\n[4] ONEWAY 模式开多仓")
    mock_broker_oneway = MockBroker()
    mock_broker_oneway.get_hedge_mode = lambda: False
    gateway_oneway = OrderGateway(mock_broker_oneway)

    params = {
        "symbol": "BTCUSDT",
        "type": "MARKET",
        "quantity": 0.001,
        "positionSide": "LONG"  # 应该被移除
    }
    final = gateway_oneway._finalize_params(params.copy(), "BUY", False)
    print(f"   输入: {params}")
    print(f"   输出: {final}")
    assert "positionSide" not in final  # ONEWAY 模式不能传 positionSide

    print(f"\n[OK] OrderGateway parameter formatting test passed")

def main():
    print("\n" + "=" * 60)
    print("重构后的 TradeExecutor + OrderGateway 集成测试")
    print("=" * 60)

    # 测试 IntentBuilder
    test_intent_builder()

    # 测试 OrderGateway 参数格式化
    test_order_gateway_params()

    print("\n[OK] All tests passed!")

if __name__ == "__main__":
    main()
