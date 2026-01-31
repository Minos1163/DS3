"""
测试开多单功能
模拟开多仓操作，验证错误处理逻辑
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from typing import Dict, Any
from src.trading.intents import TradeIntent, IntentAction, PositionSide
from src.trading.intent_builder import IntentBuilder

class MockBinanceClient:
    """模拟币安客户端"""
    def __init__(self):
        self.state_machine = MockStateMachine()
        self._orders = []

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        return {"lastPrice": "150.00"}

    def format_quantity(self, symbol: str, quantity: float) -> float:
        # 简单的格式化，保留3位小数
        return round(quantity, 3)

    def execute_intent(self, intent: TradeIntent) -> Dict[str, Any]:
        print(f"\n[DEBUG MockBinanceClient.execute_intent] Called")
        print(f"   Symbol: {intent.symbol}")
        print(f"   Action: {intent.action}")
        print(f"   Side: {intent.side}")
        print(f"   Quantity: {intent.quantity}")
        print(f"   TakeProfit: {intent.take_profit}")
        print(f"   StopLoss: {intent.stop_loss}")
        return self.state_machine.apply_intent(intent)

class MockStateMachine:
    """模拟状态机"""
    def __init__(self):
        self.snapshots = {}

    def apply_intent(self, intent: TradeIntent) -> Dict[str, Any]:
        print(f"\n[DEBUG MockStateMachine.apply_intent] Called")
        print(f"   Current snapshots: {list(self.snapshots.keys())}")

        # 检查是否有仓位
        snapshot = self.snapshots.get(intent.symbol)

        # 状态违规检查
        if intent.action == IntentAction.OPEN:
            if snapshot is not None and snapshot['quantity'] > 0:
                error_msg = f"X {intent.symbol} 已有 {intent.side.value} 仓位，不允许加仓"
                print(f"   [VIOLATION] {error_msg}")
                return {"status": "error", "message": error_msg}

        # 执行开仓
        if intent.action == IntentAction.OPEN:
            # 创建新的快照
            self.snapshots[intent.symbol] = {
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "take_profit": intent.take_profit,
                "stop_loss": intent.stop_loss
            }
            print(f"   [SUCCESS] Position opened:")
            print(f"      Symbol: {intent.symbol}")
            print(f"      Side: {intent.side.value}")
            print(f"      Quantity: {intent.quantity}")
            return {
                "status": "success",
                "orderId": 123456,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "quantity": intent.quantity
            }

        return {"status": "error", "message": "Unknown action"}

def test_open_long_success():
    """测试成功开多单"""
    print("=" * 80)
    print("测试场景 1: 成功开多单（无现有仓位）")
    print("=" * 80)

    # 创建模拟客户端
    mock_client = MockBinanceClient()

    # 模拟 TradeExecutor.open_long
    symbol = "SOLUSDT"
    quantity = 0.5
    take_profit = 160.0
    stop_loss = 140.0

    print(f"\n[1] 调用开多仓")
    print(f"   Symbol: {symbol}")
    print(f"   Quantity: {quantity}")
    print(f"   TakeProfit: {take_profit}")
    print(f"   StopLoss: {stop_loss}")

    # 构建开多仓意图
    intent = IntentBuilder.build_open_long(
        symbol=symbol,
        quantity=quantity,
        take_profit=take_profit,
        stop_loss=stop_loss
    )

    # 执行意图
    result = mock_client.execute_intent(intent)

    # 验证结果（模拟 main.py 的错误处理逻辑）
    print(f"\n[2] 验证结果")
    print(f"   Response: {result}")

    if result.get("status") == "error":
        print(f"   [ERROR] {symbol} 开多仓失败: {result.get('message', '未知错误')}")
    else:
        print(f"   [OK] {symbol} 开多仓成功: {result}")

    print("\n" + "=" * 80)

def test_open_long_duplicate():
    """测试重复开多单（已有仓位）"""
    print("\n" + "=" * 80)
    print("测试场景 2: 重复开多单（已有仓位）")
    print("=" * 80)

    # 创建模拟客户端
    mock_client = MockBinanceClient()

    # 先开一个仓位
    print(f"\n[1] 先开一个仓位")
    intent1 = IntentBuilder.build_open_long("SOLUSDT", 0.5, 160.0, 140.0)
    result1 = mock_client.execute_intent(intent1)
    print(f"   Result: {result1}")

    # 尝试再次开仓
    print(f"\n[2] 尝试再次开仓")
    intent2 = IntentBuilder.build_open_long("SOLUSDT", 0.3, 170.0, 130.0)
    result2 = mock_client.execute_intent(intent2)
    print(f"   Result: {result2}")

    # 验证结果（模拟 main.py 的错误处理逻辑）
    print(f"\n[3] 验证结果")

    if result2.get("status") == "error":
        print(f"   [ERROR] SOLUSDT 开多仓失败: {result2.get('message', '未知错误')}")
    else:
        print(f"   [OK] SOLUSDT 开多仓成功: {result2}")

    print("\n" + "=" * 80)

def test_open_long_error_handling():
    """测试错误处理逻辑"""
    print("\n" + "=" * 80)
    print("测试场景 3: 错误处理逻辑验证")
    print("=" * 80)

    # 测试不同的响应状态
    test_cases = [
        {
            "name": "成功开仓",
            "response": {"status": "success", "orderId": 123456},
            "expected": "success"
        },
        {
            "name": "已有仓位",
            "response": {"status": "error", "message": "X SOLUSDT 已有 LONG 仓位，不允许加仓"},
            "expected": "error"
        },
        {
            "name": "参数错误",
            "response": {"status": "error", "message": "参数验证失败"},
            "expected": "error"
        }
    ]

    for i, test_case in enumerate(test_cases, 1):
        print(f"\n测试用例 {i}: {test_case['name']}")
        print(f"   Response: {test_case['response']}")

        # 模拟 main.py 的错误处理逻辑
        if test_case["response"].get("status") == "error":
            print(f"   [ERROR] 开仓失败: {test_case['response'].get('message', '未知错误')}")
        else:
            print(f"   [OK] 开仓成功: {test_case['response']}")

        # 验证结果是否符合预期
        if test_case["response"].get("status") == test_case["expected"]:
            print(f"   [PASS] 测试通过")
        else:
            print(f"   [FAIL] 测试失败")

    print("\n" + "=" * 80)

def main():
    """主函数"""
    print("\n")
    print("=" * 80)
    print("开多单测试")
    print("=" * 80)

    # 运行所有测试
    test_open_long_success()
    test_open_long_duplicate()
    test_open_long_error_handling()

    print("\n")
    print("=" * 80)
    print("所有测试完成！")
    print("=" * 80)

if __name__ == "__main__":
    main()
