"""
PAPI 完整集成测试
模拟从 TradeExecutor 到 OrderGateway 再到 BinanceBroker 的完整流程
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
    """模拟 BinanceBroker，用于测试"""

    def __init__(self, hedge_mode=True):
        self.hedge_mode = hedge_mode
        self.requests_log = []
        self.PAPI_BASE = "https://testnet.binancefuture.com"  # 模拟 PAPI 基础路径
        self.position = MockPositionGateway()  # 添加 position 属性

    def get_hedge_mode(self) -> bool:
        return self.hedge_mode

    def calculate_position_side(self, side: str, reduce_only: bool) -> str:
        if reduce_only:
            return "LONG" if side == "BUY" else "SHORT"
        return "LONG" if side == "BUY" else "SHORT"

    def request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
                signed: bool = False, allow_error: bool = False) -> Any:
        """模拟API请求，记录参数而不真正发送"""
        self.requests_log.append({
            "method": method,
            "url": url,
            "params": params,
            "signed": signed
        })
        # 返回一个模拟的响应对象
        class MockResponse:
            def __init__(self):
                self.status_code = 200
                self.text = '{"orderId": 123456}'
            def json(self):
                return {"orderId": 123456, "status": "FILLED"}
        return MockResponse()

    def um_base(self) -> str:
        return "https://papi.binance.com" if self.hedge_mode else "https://fapi.binance.com"

    def is_papi_only(self) -> bool:
        """模拟 PAPI_ONLY 模式"""
        return True

class MockPositionGateway:
    """模拟仓位网关"""

    def __init__(self, has_position=True):
        self.has_position = has_position

    def get_position(self, symbol: str, side: str) -> Dict[str, Any]:
        if self.has_position:
            # 模拟有持仓的情况
            return {
                "symbol": symbol,
                "positionAmt": "0.001",
                "positionSide": side,
                "unrealizedProfit": "10.5"
            }
        else:
            # 模拟无持仓的情况
            return {
                "symbol": symbol,
                "positionAmt": "0.0",
                "positionSide": side,
                "unrealizedProfit": "0.0"
            }

def test_full_close_workflow_hedge_mode():
    """测试全仓平仓完整流程（Hedge 模式）"""
    print("=" * 60)
    print("测试全仓平仓完整流程（Hedge 模式）")
    print("=" * 60)

    # 创建模拟组件
    broker = MockBinanceBroker(hedge_mode=True)
    gateway = OrderGateway(broker)

    # 构建全仓平仓意图
    print("\n[1] 构建全仓平仓意图")
    intent = IntentBuilder.build_close(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=None  # 全仓平仓
    )
    print(f"   Symbol: {intent.symbol}")
    print(f"   Action: {intent.action}")
    print(f"   Side: {intent.side}")
    print(f"   Quantity: {intent.quantity}")

    # 模拟获取持仓信息
    print("\n[2] 获取持仓信息")
    mock_position_gateway = MockPositionGateway()
    pos = mock_position_gateway.get_position("BTCUSDT", "LONG")
    print(f"   持仓数量: {pos['positionAmt']}")

    # 格式化订单参数
    print("\n[3] 格式化订单参数")
    params = {
        "symbol": intent.symbol,
        "type": "MARKET",
        "closePosition": True,
        "quantity": float(pos["positionAmt"]),  # 从持仓获取
        "positionSide": "LONG"
    }
    print(f"   原始参数: {params}")

    final_params = gateway._finalize_params(params.copy(), "SELL", True)
    print(f"   最终参数: {final_params}")

    # 验证参数
    assert final_params.get("closePosition") is True, "closePosition 应该为 True"
    assert final_params.get("quantity") == 0.001, "quantity 应该保留（PAPI 要求）"
    assert "reduceOnly" not in final_params, "reduceOnly 应该被移除"
    assert final_params.get("positionSide") == "LONG", "positionSide 应该保留（Hedge 模式）"

    # 模拟发送请求
    print("\n[4] 发送订单请求")
    gateway.place_standard_order("BTCUSDT", "SELL", final_params, reduce_only=True)

    # 验证请求参数
    assert len(broker.requests_log) == 1, "应该有1个请求"
    request_params = broker.requests_log[0]["params"]
    print(f"   请求参数: {request_params}")

    # 关键验证：确保 quantity 参数存在
    assert "quantity" in request_params, "PAPI 全仓平仓必须包含 quantity 参数"
    assert request_params["quantity"] == 0.001, "quantity 值应该正确"
    assert request_params.get("closePosition") is True, "closePosition 应该为 True"
    assert "reduceOnly" not in request_params, "reduceOnly 不应该在请求中"

    print("\n[OK] 全仓平仓流程测试通过（Hedge 模式）")

def test_partial_close_workflow_hedge_mode():
    """测试部分平仓完整流程（Hedge 模式）"""
    print("\n" + "=" * 60)
    print("测试部分平仓完整流程（Hedge 模式）")
    print("=" * 60)

    # 创建模拟组件
    broker = MockBinanceBroker(hedge_mode=True)
    gateway = OrderGateway(broker)

    # 构建部分平仓意图
    print("\n[1] 构建部分平仓意图")
    intent = IntentBuilder.build_close(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=0.0005  # 部分平仓
    )
    print(f"   Symbol: {intent.symbol}")
    print(f"   Action: {intent.action}")
    print(f"   Side: {intent.side}")
    print(f"   Quantity: {intent.quantity}")

    # 格式化订单参数
    print("\n[2] 格式化订单参数")
    params = {
        "symbol": intent.symbol,
        "type": "MARKET",
        "quantity": intent.quantity,
        "reduceOnly": True,
        "positionSide": "LONG"
    }
    print(f"   原始参数: {params}")

    final_params = gateway._finalize_params(params.copy(), "SELL", True)
    print(f"   最终参数: {final_params}")

    # 验证参数
    assert final_params.get("reduceOnly") is True, "reduceOnly 应该为 True"
    assert final_params.get("quantity") == 0.0005, "quantity 应该保留"
    assert "closePosition" not in final_params, "closePosition 不应该存在"
    assert final_params.get("positionSide") == "LONG", "positionSide 应该保留"

    # 模拟发送请求
    print("\n[3] 发送订单请求")
    gateway.place_standard_order("BTCUSDT", "SELL", final_params, reduce_only=True)

    # 验证请求参数
    request_params = broker.requests_log[0]["params"]
    print(f"   请求参数: {request_params}")

    assert request_params.get("reduceOnly") is True, "reduceOnly 应该为 True"
    assert request_params.get("quantity") == 0.0005, "quantity 值应该正确"
    assert "closePosition" not in request_params, "closePosition 不应该在请求中"

    print("\n[OK] 部分平仓流程测试通过（Hedge 模式）")

def test_open_long_workflow_hedge_mode():
    """测试开多仓完整流程（Hedge 模式）"""
    print("\n" + "=" * 60)
    print("测试开多仓完整流程（Hedge 模式）")
    print("=" * 60)

    # 创建模拟组件
    broker = MockBinanceBroker(hedge_mode=True)
    broker.position = MockPositionGateway(has_position=False)  # 开仓时模拟无仓位
    gateway = OrderGateway(broker)

    # 构建开多仓意图
    print("\n[1] 构建开多仓意图")
    intent = IntentBuilder.build_open_long(
        symbol="BTCUSDT",
        quantity=0.001,
        take_profit=50000.0,
        stop_loss=48000.0
    )
    print(f"   Symbol: {intent.symbol}")
    print(f"   Action: {intent.action}")
    print(f"   Side: {intent.side}")
    print(f"   Quantity: {intent.quantity}")
    print(f"   TakeProfit: {intent.take_profit}")
    print(f"   StopLoss: {intent.stop_loss}")

    # 格式化订单参数
    print("\n[2] 格式化订单参数")
    params = {
        "symbol": intent.symbol,
        "type": "MARKET",
        "quantity": intent.quantity,
        "positionSide": "LONG"
    }
    print(f"   原始参数: {params}")

    final_params = gateway._finalize_params(params.copy(), "BUY", False)
    print(f"   最终参数: {final_params}")

    # 验证参数
    assert final_params.get("quantity") == 0.001, "quantity 应该保留"
    assert "reduceOnly" not in final_params, "reduceOnly 不应该存在"
    assert "closePosition" not in final_params, "closePosition 不应该存在"
    assert final_params.get("positionSide") == "LONG", "positionSide 应该保留"

    # 模拟发送请求
    print("\n[3] 发送订单请求")
    gateway.place_standard_order("BTCUSDT", "BUY", final_params, reduce_only=False)

    # 验证请求参数
    request_params = broker.requests_log[0]["params"]
    print(f"   请求参数: {request_params}")

    assert request_params.get("quantity") == 0.001, "quantity 值应该正确"
    assert request_params.get("side") == "BUY", "side 应该为 BUY"
    assert request_params.get("positionSide") == "LONG", "positionSide 应该为 LONG"

    # 模拟 TP/SL 订单
    print("\n[4] 发送 TP/SL 保护订单")
    gateway.place_protection_orders("BTCUSDT", "LONG", intent.take_profit, intent.stop_loss)

    # 验证 TP/SL 订单
    assert len(broker.requests_log) == 3, "应该有3个请求（1个主订单 + 2个保护订单）"

    # 验证 TP 订单
    tp_params = broker.requests_log[1]["params"]
    print(f"   TP 订单参数: {tp_params}")
    assert tp_params.get("type") == "TAKE_PROFIT_MARKET", "TP 订单类型应该正确"
    assert tp_params.get("stopPrice") == 50000.0, "TP 价格应该正确"
    assert tp_params.get("closePosition") is True, "TP 订单应该使用 closePosition"

    # 验证 SL 订单
    sl_params = broker.requests_log[2]["params"]
    print(f"   SL 订单参数: {sl_params}")
    assert sl_params.get("type") == "STOP_MARKET", "SL 订单类型应该正确"
    assert sl_params.get("stopPrice") == 48000.0, "SL 价格应该正确"
    assert sl_params.get("closePosition") is True, "SL 订单应该使用 closePosition"

    print("\n[OK] 开多仓流程测试通过（Hedge 模式）")

def main():
    print("\n" + "=" * 60)
    print("PAPI 完整集成测试")
    print("=" * 60)

    # 测试全仓平仓
    test_full_close_workflow_hedge_mode()

    # 测试部分平仓
    test_partial_close_workflow_hedge_mode()

    # 测试开多仓
    test_open_long_workflow_hedge_mode()

    print("\n" + "=" * 60)
    print("[OK] All PAPI integration tests passed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
