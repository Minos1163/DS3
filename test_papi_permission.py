"""
PAPI 权限测试脚本
用途：排查 400 错误，验证 API Key 权限配置

使用方法：
1. 正常测试：python test_papi_permission.py
2. Dry-Run 模式：set BINANCE_DRY_RUN=1 && python test_papi_permission.py
"""
import os
import sys

# 🔥 加载 .env 文件
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.trading.trade_executor import TradeExecutor

def test_papi_permission():
    """测试 PAPI 下单权限"""
    print("=" * 60)
    print("🔍 PAPI 权限测试")
    print("=" * 60)

    # 从环境变量读取 API Key（如果未设置，会使用配置文件中的）
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("❌ 未设置环境变量 BINANCE_API_KEY 或 BINANCE_SECRET")
        print("请先设置环境变量或使用配置文件")
        return

    # 检查是否启用 Dry-Run
    dry_run = os.getenv("BINANCE_DRY_RUN") == "1"
    print(f"\n🔧 配置:")
    print(f"  Dry-Run 模式: {'✅ 已启用（模拟下单）' if dry_run else '❌ 未启用（真实下单）'}")
    print()

    try:
        # 初始化客户端
        client = BinanceClient(api_key, api_secret)
        executor = TradeExecutor(client, {})

        print(f"✅ API Key 连接成功")
        print(f"  账户模式: {client.broker.account_mode.value}")
        print(f"  API 能力: {client.broker.capability.value}")
        print()

        # 测试账户信息（读取权限）
        print("📊 测试账户信息读取...")
        account = client.get_account()
        if account:
            print("  ✅ 账户信息读取成功")
            print(f"  可用余额: {account.get('availableBalance', 'N/A')}")
        else:
            print("  ❌ 账户信息读取失败")
        print()

        # 测试下单（Portfolio Margin Trading 权限）
        print("🧪 测试 PAPI 下单权限...")
        print("  尝试下单: SOLUSDT MARKET 0.1")

        try:
            # 使用 Dry-Run 模式测试
            if dry_run:
                print("  ⚠️  Dry-Run 模式：只验证参数，不真实下单")
            else:
                print("  ⚠️  真实下单模式：会实际扣费")

            result = executor.open_long("SOLUSDT", 0.1, leverage=None, take_profit=None, stop_loss=None)

            if result:
                if result.get("dryRun"):
                    print("  ✅ Dry-Run 成功（参数验证通过）")
                    print(f"  模拟下单参数: {result.get('params', {})}")
                else:
                    print("  ✅ 真实下单成功")
                    print(f"  订单ID: {result.get('orderId', 'N/A')}")
                    print(f"  订单状态: {result.get('status', 'N/A')}")

                print()
                print("=" * 60)
                print("🎉 权限测试通过！")
                print("=" * 60)
                print()
                print("✅ 说明:")
                print("   1. 如果看到订单ID，说明 API Key 权限完整")
                print("   2. 如果仍然是 400，请检查:")
                print("      - API Key 是否勾选了 'Enable Portfolio Margin Trading'")
                print("      - 等待 30-60 秒让权限生效")
                print("   3. 使用 Dry-Run 模式可以安全测试参数合法性")

        except Exception as e:
            print(f"  ❌ 下单失败: {e}")
            print()
            print("🔍 可能的原因:")
            print("   1. ❌ API Key 缺少 'Enable Portfolio Margin Trading' 权限")
            print("   2. ❌ 账户余额不足")
            print("   3. ❌ 网络问题或 API 服务异常")
            print()
            print("✅ 解决步骤:")
            print("   1. 登录 Binance → API 管理")
            print("   2. 编辑当前 API Key")
            print("   3. ✅ 勾选 'Enable Portfolio Margin Trading'")
            print("   4. 保存并等待 30-60 秒")
            print("   5. 重新运行此测试")

    except Exception as e:
        print(f"❌ 连接失败: {e}")
        print()
        print("🔍 可能的原因:")
        print("   1. API Key 或 Secret 错误")
        print("   2. 网络连接问题")
        print("   3. IP 被限制")

if __name__ == "__main__":
    test_papi_permission()
