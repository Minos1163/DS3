"""
实盘交易启动脚本
用途：确保实盘模式正确配置，启动真实交易
"""
import os
import sys

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

def check_live_trading_config():
    """检查实盘配置是否正确"""
    print("=" * 60)
    print("🔍 实盘交易配置检查")
    print("=" * 60)
    print()

    # 检查 1: 环境变量
    print("[1/4] 检查环境变量...")
    dry_run_env = os.getenv("BINANCE_DRY_RUN", "")
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_SECRET", "")

    if dry_run_env == "1":
        print("❌ 错误: BINANCE_DRY_RUN=1 (这是 Dry-Run 模式)")
        print("   解决方案: 设置 BINANCE_DRY_RUN= 或在 .env 中删除此行")
        return False
    else:
        print("✅ BINANCE_DRY_RUN 未设置（实盘模式）")

    if not api_key or api_key == "your_api_key_here":
        print("❌ 错误: BINANCE_API_KEY 未设置或使用默认值")
        print("   解决方案: 在 .env 文件中设置真实的 API 密钥")
        return False
    else:
        print("✅ BINANCE_API_KEY 已设置")

    if not api_secret or api_secret == "your_secret_here":
        print("❌ 错误: BINANCE_SECRET 未设置或使用默认值")
        print("   解决方案: 在 .env 文件中设置真实的 API Secret")
        return False
    else:
        print("✅ BINANCE_SECRET 已设置")
    print()

    # 检查 2: 配置文件
    print("[2/4] 检查配置文件...")
    config_path = os.path.join(PROJECT_ROOT, 'config', 'trading_config.json')
    if not os.path.exists(config_path):
        print(f"❌ 错误: 配置文件不存在: {config_path}")
        return False

    try:
        import json
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        dry_run_config = config.get("dry_run", True)
        if dry_run_config:
            print(f"❌ 错误: 配置文件中 dry_run={dry_run_config} (这是 Dry-Run 模式)")
            print("   解决方案: 在 config/trading_config.json 中设置 'dry_run': false")
            return False
        else:
            print("✅ 配置文件: dry_run=false (实盘模式)")
    except Exception as e:
        print(f"❌ 错误: 无法读取配置文件: {e}")
        return False
    print()

    # 检查 3: API 权限
    print("[3/4] 检查 API 权限...")
    try:
        from src.api.binance_client import BinanceClient
        client = BinanceClient()

        # 检测账户模式
        print(f"   账户类型: {client.broker.account_mode.value}")
        print(f"   持仓模式: {'双向 (Hedge)' if client.broker.get_hedge_mode() else '单向 (One-way)'}")

        # 测试连接
        if client.test_connection():
            print("✅ API 连接正常")
        else:
            print("❌ 错误: API 连接失败")
            return False
    except Exception as e:
        print(f"❌ 错误: API 测试失败: {e}")
        return False
    print()

    # 检查 4: 账户余额
    print("[4/4] 检查账户余额...")
    try:
        account = client.get_account()
        balance = float(account.get("totalWalletBalance", 0))
        available = float(account.get("availableBalance", 0))

        print(f"   总资产: ${balance:.2f}")
        print(f"   可用余额: ${available:.2f}")

        if available <= 0:
            print("⚠️  警告: 可用余额为 0，无法开仓")
            print("   建议: 请先充值")
            return False
        elif available < 10:
            print("⚠️  警告: 可用余额过低 ($10)，建议充值")
        else:
            print("✅ 账户余额充足")
    except Exception as e:
        print(f"❌ 错误: 无法获取账户余额: {e}")
        return False
    print()

    return True


def print_live_trading_warning():
    """打印实盘交易警告信息"""
    print("=" * 60)
    print("⚠️  实盘交易警告")
    print("=" * 60)
    print()
    print("⚠️  正在启动**实盘交易模式**，这将：")
    print("   • 使用真实资金进行交易")
    print("   • 真实调用 Binance API")
    print("   • 所有交易操作都会被执行")
    print()
    print("💡 建议：")
    print("   • 先使用小仓位测试")
    print("   • 严格设置止盈止损")
    print("   • 保持风控参数保守")
    print()
    print("=" * 60)
    print()


def main():
    """主函数"""
    print()

    # 检查配置
    if not check_live_trading_config():
        print()
        print("=" * 60)
        print("❌ 实盘配置检查失败，请修复后重试")
        print("=" * 60)
        print()
        print("📚 帮助文档: LIVE_TRADING_CHECKLIST.md")
        return

    # 打印警告（不需要确认）
    print_live_trading_warning()

    # 启动交易机器人
    print()
    print("=" * 60)
    print("🚀 启动实盘交易机器人...")
    print("=" * 60)
    print()

    try:
        from src.main import TradingBot

        # 创建并启动机器人
        bot = TradingBot()
        bot.run()

    except KeyboardInterrupt:
        print()
        print("=" * 60)
        print("⚠️  用户中断，正在停止...")
        print("=" * 60)
    except Exception as e:
        print()
        print("=" * 60)
        print(f"❌ 运行错误: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
