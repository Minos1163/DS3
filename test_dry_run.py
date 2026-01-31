"""
测试 TradeExecutor Dry-Run 模式
用途：演示 Dry-Run 模式下的交易模拟，包括 TP/SL 挂单打印
"""
import os
import sys

# 🔥 加载 .env 文件
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.trading.trade_executor import TradeExecutor


def test_dry_run():
    """测试 Dry-Run 模式"""
    print("=" * 60)
    print("🔍 TradeExecutor Dry-Run 模式测试")
    print("=" * 60)
    print()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("❌ 未设置环境变量 BINANCE_API_KEY 或 BINANCE_SECRET")
        return

    # 启用 Dry-Run 模式
    config = {"dry_run": True}
    executor = TradeExecutor(BinanceClient(api_key, api_secret), config)

    print("🔧 配置:")
    print(f"  Dry-Run 模式: ✅ 已启用")
    print(f"  不会实际下单，只打印意图和预期挂单")
    print()

    # 测试 1: 开多仓 + TP/SL
    print("=" * 60)
    print("🧪 测试 1: 开多仓 + TP/SL")
    print("=" * 60)
    try:
        result = executor.open_long(
            "SOLUSDT",
            0.1,
            leverage=10,
            take_profit=25.0,
            stop_loss=20.0
        )
        print(f"✅ 结果: {result}")
    except Exception as e:
        print(f"❌ 失败: {e}")
    print()

    # 测试 2: 开空仓 + TP/SL
    print("=" * 60)
    print("🧪 测试 2: 开空仓 + TP/SL")
    print("=" * 60)
    try:
        result = executor.open_short(
            "SOLUSDT",
            0.1,
            leverage=10,
            take_profit=20.0,
            stop_loss=25.0
        )
        print(f"✅ 结果: {result}")
    except Exception as e:
        print(f"❌ 失败: {e}")
    print()

    # 测试 3: 开仓不带 TP/SL
    print("=" * 60)
    print("🧪 测试 3: 开多仓不带 TP/SL")
    print("=" * 60)
    try:
        result = executor.open_long("SOLUSDT", 0.1)
        print(f"✅ 结果: {result}")
    except Exception as e:
        print(f"❌ 失败: {e}")
    print()

    # 测试 4: 平仓
    print("=" * 60)
    print("🧪 测试 4: 平多仓")
    print("=" * 60)
    try:
        result = executor.close_long("SOLUSDT")
        print(f"✅ 结果: {result}")
    except Exception as e:
        print(f"❌ 失败: {e}")
    print()

    # 测试 5: 部分平仓
    print("=" * 60)
    print("🧪 测试 5: 部分平仓")
    print("=" * 60)
    try:
        result = executor.reduce_position("SOLUSDT", 0.05, IntentPositionSide.LONG)
        print(f"✅ 结果: {result}")
    except Exception as e:
        print(f"❌ 失败: {e}")
    print()

    print("=" * 60)
    print("✅ 测试完成")
    print("=" * 60)
    print()
    print("📊 预期结果:")
    print("  ✅ 所有操作都不会实际下单")
    print("  ✅ 打印意图和预期挂单信息")
    print("  ✅ TP/SL 价格会显示在预期挂单中")


if __name__ == "__main__":
    from src.trading.intents import PositionSide as IntentPositionSide
    test_dry_run()
