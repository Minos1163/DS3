"""
测试 positionSide 修复
用途：验证 Hedge Mode 下开仓时是否正确添加 positionSide

使用方法：
1. Dry-Run 模式测试：python test_position_side_fix.py
2. 真实下单测试：set BINANCE_DRY_RUN=1 && python test_position_side_fix.py
"""
import os
import sys

# 🔥 加载 .env 文件
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.trading.trade_executor import TradeExecutor


def test_position_side_logic():
    """测试 positionSide 逻辑"""
    print("=" * 60)
    print("🔍 positionSide 逻辑测试")
    print("=" * 60)
    print()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("❌ 未设置环境变量 BINANCE_API_KEY 或 BINANCE_SECRET")
        return

    dry_run = os.getenv("BINANCE_DRY_RUN") == "1"
    print(f"🔧 配置:")
    print(f"  Dry-Run 模式: {'✅ 已启用' if dry_run else '❌ 未启用（真实下单）'}")
    print()

    try:
        client = BinanceClient(api_key, api_secret)
        executor = TradeExecutor(client, {})

        # 检测持仓模式
        is_hedge = client.broker.get_hedge_mode()

        print(f"📊 账户信息:")
        print(f"  账户类型: {client.broker.account_mode.value}")
        print(f"  持仓模式: {'双向 (Hedge Mode)' if is_hedge else '单向 (One-way Mode)'}")
        print()

        if not is_hedge:
            print("❌ 当前是单向持仓模式")
            print()
            print("🔍 测试单向持仓模式（应该删除 positionSide）:")
            print("   系统会自动删除任何 positionSide 参数")
        else:
            print("✅ 当前是双向持仓模式")
            print()
            print("🔍 测试双向持仓模式（应该自动补全 positionSide）:")
            print("   系统会自动添加 positionSide 参数")
        print()

        # 测试开多（开仓）
        print("=" * 60)
        print("🧪 测试 1: 开多（OPEN_LONG）")
        print("=" * 60)
        try:
            result = executor.open_long("SOLUSDT", 0.1, leverage=None, take_profit=None, stop_loss=None)
            if dry_run or result.get("dryRun"):
                print("✅ Dry-Run 成功")
            else:
                print(f"✅ 下单成功，订单ID: {result.get('orderId', 'N/A')}")
        except Exception as e:
            print(f"❌ 失败: {e}")
        print()

        # 测试开空（开仓）
        print("=" * 60)
        print("🧪 测试 2: 开空（OPEN_SHORT）")
        print("=" * 60)
        try:
            result = executor.open_short("SOLUSDT", 0.1, leverage=None, take_profit=None, stop_loss=None)
            if dry_run or result.get("dryRun"):
                print("✅ Dry-Run 成功")
            else:
                print(f"✅ 下单成功，订单ID: {result.get('orderId', 'N/A')}")
        except Exception as e:
            print(f"❌ 失败: {e}")
        print()

        # 测试平多（平仓）
        print("=" * 60)
        print("🧪 测试 3: 平多（CLOSE_LONG）")
        print("   ✅ 使用 closePosition=True，无需传入 quantity")
        print("=" * 60)
        try:
            result = executor.close_long("SOLUSDT", None)
            if dry_run or result.get("dryRun"):
                print("✅ Dry-Run 成功")
            else:
                print(f"✅ 下单成功，订单ID: {result.get('orderId', 'N/A')}")
        except Exception as e:
            print(f"❌ 失败: {e}")
        print()

        # 测试平空（平仓）
        print("=" * 60)
        print("🧪 测试 4: 平空（CLOSE_SHORT）")
        print("   ✅ 使用 closePosition=True，无需传入 quantity")
        print("=" * 60)
        try:
            result = executor.close_short("SOLUSDT", None)
            if dry_run or result.get("dryRun"):
                print("✅ Dry-Run 成功")
            else:
                print(f"✅ 下单成功，订单ID: {result.get('orderId', 'N/A')}")
        except Exception as e:
            print(f"❌ 失败: {e}")
        print()

        print("=" * 60)
        print("✅ 测试完成")
        print("=" * 60)
        print()
        print("📊 预期结果:")
        print()
        if is_hedge:
            print("双向持仓模式:")
            print("   ✓ 开多: positionSide=LONG")
            print("   ✓ 开空: positionSide=SHORT")
            print("   ✓ 不会出现 -4061 错误")
        else:
            print("单向持仓模式:")
            print("   ✓ 任何操作都不会包含 positionSide")
            print("   ✓ 不会出现 -4061 错误")
        print()
        print("🎯 如果看到 -4061 错误，请检查:")
        print("   1. 系统日志中是否有自动补全 positionSide 的信息")
        print("   2. 持仓模式检测结果是否正确")
        print("   3. 是否有其他地方手动添加了 positionSide")

    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_position_side_logic()
