"""
实际环境开多单测试
在真实的币安环境中测试开多仓功能
⚠️ 警告：此脚本会进行真实的交易操作！
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import pytest

from src.api.binance_client import BinanceClient
from src.trading.trade_executor import TradeExecutor
from src.config.env_manager import EnvManager

def test_open_long_real():
    """在真实环境中测试开多仓"""
    print("=" * 80)
    print("真实环境开多单测试")
    print("=" * 80)
    print("\n⚠️  警告：此操作将进行真实交易！")
    print("⚠️  请确保：")
    print("   1. API Key 配置正确")
    print("   2. 账户有足够的保证金")
    print("   3. 清楚此操作的风险")
    print("\n" + "=" * 80)

    # 在 pytest 环境中跳过交互式真实下单测试
    if os.getenv("PYTEST_RUNNING") == "1" or os.getenv("AUTO_CONFIRM_REAL_TEST", "0") != "1":
        pytest.skip("跳过交互式真实下单测试 (运行于 pytest 或未设置 AUTO_CONFIRM_REAL_TEST=1)")

    try:
        # 获取 API 凭证
        api_key, api_secret = EnvManager.get_api_credentials()
        if not api_key or not api_secret:
            print("❌ 错误：API凭证未配置")
            print("\n请在项目根目录的 .env 文件中配置：")
            print("   BINANCE_API_KEY=your_api_key")
            print("   BINANCE_API_SECRET=your_api_secret")
            return

        # 初始化客户端
        print("\n[1] 初始化币安客户端...")
        client = BinanceClient(api_key=api_key, api_secret=api_secret)
        print("   [OK] 客户端初始化成功")

        # 检查账户模式
        print("\n[2] 检查账户模式...")
        account_mode = client.account_mode.value
        hedge_mode = client.get_hedge_mode()
        print(f"   账户模式: {account_mode}")
        print(f"   Hedge 模式: {hedge_mode}")

        # 获取账户信息
        print("\n[3] 获取账户信息...")
        account = client.get_account()
        if account:
            print(f"   总权益: {account.get('totalWalletBalance', 'N/A')}")
            print(f"   可用余额: {account.get('availableBalance', 'N/A')}")
        else:
            print("   [WARNING] 无法获取账户信息")

        # 获取当前持仓
        print("\n[4] 检查当前持仓...")
        positions = client.get_all_positions()
        sol_positions = [p for p in positions if p.get('symbol') == 'SOLUSDT' and abs(float(p.get('positionAmt', 0))) > 0]

        if sol_positions:
            print(f"   ⚠️  SOLUSDT 已有 {len(sol_positions)} 个持仓:")
            for pos in sol_positions:
                side = pos.get('positionSide', 'BOTH')
                amt = pos.get('positionAmt', '0')
                print(f"      - {side}: {amt}")
            print("\n   建议先平仓或切换到其他币种")
            return
        else:
            print("   [OK] SOLUSDT 无持仓，可以开仓")

        # 创建交易执行器
        print("\n[5] 创建交易执行器...")
        executor = TradeExecutor(client, {})
        print("   [OK] 执行器初始化成功")

        # 获取当前价格
        print("\n[6] 获取当前价格...")
        ticker = client.get_ticker("SOLUSDT")
        if ticker:
            current_price = float(ticker.get('lastPrice', 0))
            print(f"   当前价格: {current_price} USDT")
        else:
            print("   [ERROR] 无法获取价格")
            return

        # 计算开仓参数
        print("\n[7] 计算开仓参数...")
        quantity = 0.1  # 测试数量
        leverage = 5  # 5倍杠杆
        take_profit = current_price * 1.05  # 5% 止盈
        stop_loss = current_price * 0.98  # 2% 止损

        print(f"   开仓数量: {quantity}")
        print(f"   杠杆: {leverage}x")
        print(f"   止盈价格: {take_profit:.2f} (+5%)")
        print(f"   止损价格: {stop_loss:.2f} (-2%)")

        # 执行开多仓
        print("\n[8] 执行开多仓...")
        print("=" * 80)

        result = executor.open_long(
            symbol="SOLUSDT",
            quantity=quantity,
            leverage=leverage,
            take_profit=take_profit,
            stop_loss=stop_loss
        )

        print("=" * 80)

        # 验证结果
        print("\n[9] 验证结果...")
        print(f"   返回结果: {result}")

        if result.get("status") == "error":
            print(f"\n   ❌ SOLUSDT 开多仓失败")
            print(f"   错误信息: {result.get('message', '未知错误')}")
        else:
            print(f"\n   ✅ SOLUSDT 开多仓成功")
            print(f"   订单ID: {result.get('orderId', 'N/A')}")

        # 获取最终持仓
        print("\n[10] 获取最终持仓...")
        final_positions = client.get_all_positions()
        sol_final_positions = [p for p in final_positions if p.get('symbol') == 'SOLUSDT' and abs(float(p.get('positionAmt', 0))) > 0]

        if sol_final_positions:
            print(f"   ✅ SOLUSDT 当前持仓:")
            for pos in sol_final_positions:
                side = pos.get('positionSide', 'BOTH')
                amt = pos.get('positionAmt', '0')
                pnl = pos.get('unRealizedProfit', '0')
                print(f"      - {side}: {amt} (盈亏: {pnl})")
        else:
            print(f"   [INFO] SOLUSDT 无持仓")

    except KeyboardInterrupt:
        print("\n\n操作已取消")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)

if __name__ == "__main__":
    test_open_long_real()
