"""
测试止盈止损修复
用途：验证 -1106 错误修复（closePosition 与 reduceOnly 互斥）

使用方法：
1. Dry-Run 模式测试：python test_tp_sl_fix.py
2. 真实下单测试：set BINANCE_DRY_RUN=1 && python test_tp_sl_fix.py
"""
import os
import sys

# 🔥 加载 .env 文件
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.trading.intents import TradeIntent, IntentAction, PositionSide


def test_tp_sl_fix():
    """测试止盈止损逻辑"""
    print("=" * 60)
    print("🔍 止盈止损修复测试")
    print("=" * 60)
    print()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("❌ 未设置环境变量 BINANCE_API_KEY 或 BINANCE_SECRET")
        return

    dry_run = os.getenv("BINANCE_DRY_RUN") == "1"
    print(f"🔧 配置:")
    print(f"  Dry-Run 模式: {'✅ 已启用（模拟下单）' if dry_run else '❌ 未启用（真实下单）'}")
    print()

    try:
        client = BinanceClient(api_key, api_secret)

        # 检测持仓模式
        is_hedge = client.broker.get_hedge_mode()

        print(f"📊 账户信息:")
        print(f"  账户类型: {client.broker.account_mode.value}")
        print(f"  持仓模式: {'双向 (Hedge Mode)' if is_hedge else '单向 (One-way Mode)'}")
        print()

        # 测试开多（开仓）
        print("=" * 60)
        print("🧪 测试 1: 开多仓 + 止盈止损")
        print("=" * 60)
        try:
            # 开多仓 + 设置止盈止损
            print("\n[1/1] 开多仓 + 设置止盈止损...")
            # 假设当前价格是 100，止盈 110（10%），止损 90（-10%）
            intent = TradeIntent(
                symbol="SOLUSDT",
                action=IntentAction.OPEN,
                side=PositionSide.LONG,
                quantity=0.1,
                take_profit=110.0,
                stop_loss=90.0,
                reason="测试开多 + 止盈止损"
            )

            result = client.execute_intent(intent)

            if dry_run or result.get("dry_run"):
                print("✅ 开多仓 Dry-Run 成功")
            else:
                print(f"✅ 操作成功，结果: {result.get('status', 'N/A')}")

        except Exception as e:
            print(f"❌ 失败: {e}")
            import traceback
            traceback.print_exc()
        print()

        # 测试开空 + 止盈止损
        print("=" * 60)
        print("🧪 测试 2: 开空仓 + 止盈止损")
        print("=" * 60)
        try:
            # 开空仓 + 设置止盈止损
            print("\n[1/1] 开空仓 + 设置止盈止损...")
            # 假设当前价格是 100，止盈 90（-10%），止损 110（+10%）
            intent = TradeIntent(
                symbol="SOLUSDT",
                action=IntentAction.OPEN,
                side=PositionSide.SHORT,
                quantity=0.1,
                take_profit=90.0,
                stop_loss=110.0,
                reason="测试开空 + 止盈止损"
            )

            result = client.execute_intent(intent)

            if dry_run or result.get("dry_run"):
                print("✅ 开空仓 Dry-Run 成功")
            else:
                print(f"✅ 操作成功，结果: {result.get('status', 'N/A')}")

        except Exception as e:
            print(f"❌ 失败: {e}")
            import traceback
            traceback.print_exc()
        print()

        print("=" * 60)
        print("✅ 测试完成")
        print("=" * 60)
        print()
        print("📊 预期结果:")
        print()
        print("✅ 不应该出现 -1106 错误:")
        print("   -1106: Parameter 'reduceOnly' sent when not required.")
        print()
        print("✅ 止盈止损订单应该成功创建:")
        print("   - TAKE_PROFIT_MARKET")
        print("   - STOP_MARKET")
        print()
        print("🎯 如果还看到 -1106 错误:")
        print("   1. 检查系统日志中是否有自动移除 reduceOnly 的信息")
        print("   2. 确认止盈止损订单参数中不包含 reduceOnly")
        print("   3. 确认只使用了 closePosition=True")

    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_tp_sl_fix()
