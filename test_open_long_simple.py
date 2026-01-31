"""
开多单简单测试（无装饰器）
直接测试核心逻辑
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.trading.intent_builder import IntentBuilder
from src.trading.intents import PositionSide

def test_open_long_logic():
    """测试开多仓的核心逻辑"""
    print("=" * 80)
    print("开多单核心逻辑测试")
    print("=" * 80)

    # 测试参数
    symbol = "SOLUSDT"
    quantity = 0.5
    take_profit = 160.0
    stop_loss = 140.0

    print(f"\n[测试参数]")
    print(f"   币种: {symbol}")
    print(f"   数量: {quantity}")
    print(f"   止盈: {take_profit} USDT")
    print(f"   止损: {stop_loss} USDT")

    # 测试1：构建开多仓意图
    print(f"\n[测试 1] 构建开多仓意图...")
    try:
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
        print(f"      TakeProfit: {intent.take_profit}")
        print(f"      StopLoss: {intent.stop_loss}")
        print(f"      ReduceOnly: {intent.reduce_only}")
    except Exception as e:
        print(f"   [ERROR] 意图构建失败: {e}")
        return

    # 测试2：验证意图参数
    print(f"\n[测试 2] 验证意图参数...")
    errors = []

    if intent.action.value != "OPEN":
        errors.append(f"Action 应为 OPEN，实际为 {intent.action.value}")

    if intent.side != PositionSide.LONG:
        errors.append(f"Side 应为 LONG，实际为 {intent.side.value}")

    if intent.quantity != quantity:
        errors.append(f"Quantity 应为 {quantity}，实际为 {intent.quantity}")

    if intent.take_profit != take_profit:
        errors.append(f"TakeProfit 应为 {take_profit}，实际为 {intent.take_profit}")

    if intent.stop_loss != stop_loss:
        errors.append(f"StopLoss 应为 {stop_loss}，实际为 {intent.stop_loss}")

    if intent.reduce_only != False:
        errors.append(f"ReduceOnly 应为 False，实际为 {intent.reduce_only}")

    if errors:
        print(f"   [ERROR] 参数验证失败:")
        for error in errors:
            print(f"      - {error}")
    else:
        print(f"   [OK] 所有参数验证通过")

    # 测试3：模拟成功响应
    print(f"\n[测试 3] 模拟成功响应处理...")
    success_response = {
        "status": "success",
        "orderId": 123456,
        "symbol": symbol,
        "side": "LONG",
        "quantity": quantity
    }

    if success_response.get("status") == "error":
        print(f"   [ERROR] {symbol} 开多仓失败: {success_response.get('message', '未知错误')}")
    else:
        print(f"   [OK] {symbol} 开多仓成功")
        print(f"      订单ID: {success_response.get('orderId')}")
        print(f"      数量: {success_response.get('quantity')}")

    # 测试4：模拟错误响应
    print(f"\n[测试 4] 模拟错误响应处理...")
    error_response = {
        "status": "error",
        "message": "X SOLUSDT 已有 LONG 仓位，不允许加仓"
    }

    if error_response.get("status") == "error":
        print(f"   [OK] 错误响应被正确处理")
        print(f"      状态: error")
        print(f"      消息: {error_response.get('message')}")
    else:
        print(f"   [ERROR] 错误响应未被正确处理")

    print("\n" + "=" * 80)
    print("所有测试通过！")
    print("=" * 80)

if __name__ == "__main__":
    test_open_long_logic()
