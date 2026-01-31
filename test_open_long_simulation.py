"""
开多单模拟测试
模拟完整的开多仓流程，无需真实API
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.trading.trade_executor import TradeExecutor
from src.trading.intent_builder import IntentBuilder
from src.trading.intents import PositionSide

class MockBinanceClient:
    """模拟币安客户端（不进行真实交易）"""
    def __init__(self):
        self.state_machine = MockStateMachine()
        self._mock_positions = {}

    def get_ticker(self, symbol: str):
        return {"lastPrice": "150.00"}

    def format_quantity(self, symbol: str, quantity: float):
        return round(quantity, 3)

    def execute_intent(self, intent):
        return self.state_machine.apply_intent(intent)

    def get_position(self, symbol: str, side: str = None):
        # 模拟无初始持仓
        return None

class MockStateMachine:
    """模拟状态机"""
    def __init__(self):
        self.snapshots = {}

    def apply_intent(self, intent):
        snapshot = self.snapshots.get(intent.symbol)

        # 状态违规检查
        if snapshot and snapshot['quantity'] > 0:
            return {
                "status": "error",
                "message": f"X {intent.symbol} 已有 {intent.side.value} 仓位，不允许加仓"
            }

        # 模拟成功开仓
        self.snapshots[intent.symbol] = {
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "take_profit": intent.take_profit,
            "stop_loss": intent.stop_loss
        }

        return {
            "status": "success",
            "orderId": 123456,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "quantity": intent.quantity
        }

def main():
    """主测试函数"""
    print("=" * 80)
    print("开多单模拟测试")
    print("=" * 80)
    print("\n此测试使用模拟环境，不会进行真实交易\n")

    # 创建模拟客户端和执行器
    mock_client = MockBinanceClient()
    executor = TradeExecutor(mock_client, {})

    # 测试参数
    symbol = "SOLUSDT"
    quantity = 0.5
    leverage = 5
    take_profit = 160.0
    stop_loss = 140.0

    print(f"[测试参数]")
    print(f"   币种: {symbol}")
    print(f"   数量: {quantity}")
    print(f"   杠杆: {leverage}x")
    print(f"   止盈: {take_profit} USDT")
    print(f"   止损: {stop_loss} USDT")

    print(f"\n[步骤 1] 构建开多仓意图...")
    intent = IntentBuilder.build_open_long(
        symbol=symbol,
        quantity=quantity,
        take_profit=take_profit,
        stop_loss=stop_loss
    )
    print(f"   [OK] 意图构建成功")
    print(f"      Action: {intent.action.value}")
    print(f"      Side: {intent.side.value}")
    print(f"      Quantity: {intent.quantity}")

    print(f"\n[步骤 2] 执行开多仓...")
    try:
        result = executor.open_long(
            symbol=symbol,
            quantity=quantity,
            leverage=leverage,
            take_profit=take_profit,
            stop_loss=stop_loss
        )

        print(f"\n[步骤 3] 验证结果...")
        print(f"   返回状态: {result.get('status')}")
        print(f"   返回消息: {result.get('message', 'N/A')}")

        if result.get("status") == "error":
            print(f"\n   [ERROR] {symbol} 开多仓失败")
            print(f"   错误: {result.get('message', '未知错误')}")
        else:
            print(f"\n   [OK] {symbol} 开多仓成功")
            print(f"   订单ID: {result.get('orderId', 'N/A')}")
            print(f"   开仓数量: {result.get('quantity', 'N/A')}")

        print("\n" + "=" * 80)
        print("测试通过！")
        print("=" * 80)

    except Exception as e:
        print(f"\n   [ERROR] 开多仓异常: {e}")
        import traceback
        traceback.print_exc()

    # 测试重复开仓
    print(f"\n[步骤 4] 测试重复开仓保护...")
    print(f"   尝试再次开多仓...")

    try:
        result2 = executor.open_long(
            symbol=symbol,
            quantity=0.3,
            leverage=leverage,
            take_profit=170.0,
            stop_loss=130.0
        )

        print(f"\n   [验证结果]")
        if result2.get("status") == "error":
            print(f"   [OK] 重复开仓被正确拒绝")
            print(f"   错误信息: {result2.get('message')}")
        else:
            print(f"   [ERROR] 重复开仓未被阻止！")

    except RuntimeError as e:
        print(f"   [OK] 重复开仓被正确阻止（RuntimeError）")
        print(f"   错误: {e}")

    print("\n" + "=" * 80)
    print("所有测试完成！")
    print("=" * 80)

if __name__ == "__main__":
    main()
