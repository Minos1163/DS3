"""
测试 close_position 的完整调用链，复现实际运行时的错误
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from typing import Dict, Any, Optional
from src.trading.intents import TradeIntent, IntentAction, PositionSide
from src.trading.intent_builder import IntentBuilder
from src.trading.order_gateway import OrderGateway
from src.trading.position_state_machine import PositionStateMachineV2

class MockBinanceBroker:
    """模拟 BinanceBroker"""
    def __init__(self):
        self.hedge_mode = True

    def get_hedge_mode(self) -> bool:
        return True

    def calculate_position_side(self, side: str, reduce_only: bool) -> str:
        return "LONG" if side == "BUY" else "SHORT"

    def um_base(self) -> str:
        return "https://papi.binance.com"

    def request(self, method: str, url: str, params: Dict[str, Any] = None,
                signed: bool = False, allow_error: bool = False) -> Any:
        print(f"\n[DEBUG BinanceBroker.request] Called with params: {params}")
        print(f"[DEBUG BinanceBroker.request] Checking for quantity: {params.get('quantity') if params else 'N/A'}")
        print(f"[DEBUG BinanceBroker.request] Checking for closePosition: {params.get('closePosition') if params else 'N/A'}")

        # 返回模拟响应
        class MockResponse:
            def __init__(self):
                self.status_code = 200
                self.text = '{"orderId": 123456}'
            def json(self):
                return {"orderId": 123456, "status": "FILLED"}
        return MockResponse()

class MockPositionGateway:
    """模拟仓位网关"""
    def get_position(self, symbol: str, side: str = None) -> Optional[Dict[str, Any]]:
        # 模拟 SHORT 仓位
        return {
            "symbol": symbol,
            "positionAmt": "-0.5",  # SHORT 仓位
            "positionSide": "SHORT",
            "unrealizedProfit": "10.5"
        }

class MockClient:
    """模拟客户端"""
    def __init__(self):
        self.broker = MockBinanceBroker()
        self.broker.position = MockPositionGateway()

    def get_position(self, symbol: str, side: str = None) -> Optional[Dict[str, Any]]:
        return self.broker.position.get_position(symbol, side)

    def _execute_order_v2(self, params: Dict[str, Any], side: str, reduce_only: bool) -> Dict[str, Any]:
        print(f"\n[DEBUG _execute_order_v2] Called with:")
        print(f"   params={params}")
        print(f"   side={side}")
        print(f"   reduce_only={reduce_only}")

        # 调用 OrderGateway
        return order_gateway.place_standard_order(
            symbol=params.get("symbol", ""),
            side=side,
            params=params,
            reduce_only=reduce_only
        )

def test_close_position_full_call_chain():
    """测试完整的 close_position 调用链"""
    print("=" * 80)
    print("测试完整 close_position 调用链（模拟实际运行场景）")
    print("=" * 80)

    # 创建模拟客户端
    mock_client = MockClient()

    # 创建 OrderGateway
    global order_gateway
    order_gateway = OrderGateway(mock_client.broker)

    # 创建状态机
    state_machine = PositionStateMachineV2(mock_client)

    # 模拟构建平仓意图
    print("\n[1] 构建平仓意图")
    intent = IntentBuilder.build_close(
        symbol="SOLUSDT",
        side=PositionSide.SHORT,
        quantity=None  # 全仓平仓
    )
    print(f"   Intent: symbol={intent.symbol}, action={intent.action}, side={intent.side}, quantity={intent.quantity}")

    # 调用状态机的 _close 方法
    print("\n[2] 调用 PositionStateMachineV2._close")
    result = state_machine._close(intent)
    print(f"   Result: {result}")

    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)

if __name__ == "__main__":
    test_close_position_full_call_chain()
