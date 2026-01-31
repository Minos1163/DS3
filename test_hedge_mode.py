"""
持仓模式测试脚本
用途：检测和诊断 -4061 错误（positionSide 不匹配）

使用方法：
1. 检测当前持仓模式：python test_hedge_mode.py
2. 切换到双向持仓：python test_hedge_mode.py --set-hedge
"""
import os
import sys
import argparse

# 🔥 加载 .env 文件
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient


def test_hedge_mode():
    """检测当前持仓模式"""
    print("=" * 60)
    print("🔍 持仓模式检测")
    print("=" * 60)
    print()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("❌ 未设置环境变量 BINANCE_API_KEY 或 BINANCE_SECRET")
        return

    try:
        client = BinanceClient(api_key, api_secret)

        print(f"✅ 连接成功")
        print(f"  账户类型: {client.broker.account_mode.value}")
        print()

        # 检测持仓模式
        if client.broker.account_mode.value == "UNIFIED":
            is_hedge = client.broker.get_hedge_mode()

            print(f"📊 持仓模式:")
            print(f"  {'✅ 双向持仓 (Hedge Mode)' if is_hedge else '❌ 单向持仓 (One-way Mode)'}")
            print()

            if not is_hedge:
                print("⚠️  当前是单向持仓模式")
                print()
                print("🔍 单向持仓模式的限制:")
                print("   ❌ 禁止使用 positionSide 参数")
                print("   ❌ 同一方向只能持有一个持仓")
                print("   ✅ 系统会自动移除任何 positionSide 参数")
                print()
                print("✅ 解决方案:")
                print("   1. 保持单向持仓：系统已自动处理，无需额外操作")
                print("   2. 切换双向持仓：运行 python test_hedge_mode.py --set-hedge")
                print()
                print("⚠️  注意：切换持仓模式会清空当前所有持仓！")
            else:
                print("✅ 当前是双向持仓模式")
                print()
                print("📊 双向持仓模式的优势:")
                print("   ✅ 允许同时持有多空两个方向的持仓")
                print("   ✅ 可以精确控制平仓方向")
                print("   ✅ 支持复杂的对冲策略")
        else:
            print("📊 经典账户（Classic）")
            print("  ⚠️  经典账户仅支持单向持仓模式")

        print()
        print("=" * 60)

    except Exception as e:
        print(f"❌ 检测失败: {e}")
        import traceback
        traceback.print_exc()


def set_hedge_mode():
    """切换到双向持仓模式"""
    print("=" * 60)
    print("🔧 切换到双向持仓模式")
    print("=" * 60)
    print()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("❌ 未设置环境变量 BINANCE_API_KEY 或 BINANCE_SECRET")
        return

    print("⚠️  警告：切换持仓模式会清空当前所有持仓！")
    print()

    confirm = input("确认要切换到双向持仓模式吗？(yes/no): ")
    if confirm.lower() not in ["yes", "y"]:
        print("❌ 操作已取消")
        return

    try:
        client = BinanceClient(api_key, api_secret)

        # 切换到双向持仓
        result = client.broker.set_hedge_mode(True)

        if result:
            print("✅ 已成功切换到双向持仓模式")
            print()
            print("📊 现在可以:")
            print("   ✅ 同时持有多空两个方向的持仓")
            print("   ✅ 使用 positionSide 参数控制平仓方向")
            print("   ✅ 执行复杂的对冲策略")
        else:
            print("❌ 切换失败，请检查账户状态")

    except Exception as e:
        print(f"❌ 切换失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Binance 持仓模式检测和切换工具")
    parser.add_argument("--set-hedge", action="store_true", help="切换到双向持仓模式")

    args = parser.parse_args()

    if args.set_hedge:
        set_hedge_mode()
    else:
        test_hedge_mode()
