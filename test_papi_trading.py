#!/usr/bin/env python3
"""
PAPI交易测试脚本
验证PAPI Unified Margin下单、平仓功能是否正常
"""

import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.api.binance_client import BinanceClient


def test_papi_trading():
    """测试PAPI交易功能"""
    print("=" * 70)
    print("[测试] PAPI交易功能测试")
    print("=" * 70)

    try:
        # 初始化客户端
        client = BinanceClient()
        broker = client.broker

        print(f"\n[模式] 账户模式: {broker.account_mode.value}")
        print(f"[能力] API能力: {broker.capability.value}")

        # 选择交易对
        symbol = "SOLUSDT"
        test_quantity = 0.01  # 最小测试数量

        print(f"\n[信息] 使用交易对: {symbol}")
        print(f"[信息] 测试数量: {test_quantity}")

        # 获取当前价格
        print("\n[步骤1] 获取当前价格...")
        ticker = client.get_ticker(symbol)
        if ticker:
            current_price = float(ticker.get('lastPrice', 0))
            print(f"[价格] 当前价格: ${current_price:.2f}")
        else:
            print("[错误] 无法获取价格")
            return False

        # 1️⃣ 开仓测试
        print("\n[步骤2] 开多仓测试...")
        print(f"[操作] 购买 {test_quantity} {symbol}")
        print("[参数] reduce_only=False")

        try:
            open_order = broker.order.place_order(
                symbol=symbol,
                side="BUY",
                quantity=test_quantity,
                reduce_only=False
            )
            print(f"[成功] 开仓订单ID: {open_order.get('orderId', 'N/A')}")
            print(f"[状态] 订单状态: {open_order.get('status', 'N/A')}")
        except Exception as e:
            print(f"[失败] 开仓失败: {e}")
            return False

        # 等待订单成交
        import time
        print("\n[等待] 等待订单成交（5秒）...")
        time.sleep(5)

        # 2️⃣ 平仓测试
        print("\n[步骤3] 平多仓测试...")
        print(f"[操作] 卖出 {test_quantity} {symbol}")
        print("[参数] reduce_only=True")

        try:
            close_order = broker.order.place_order(
                symbol=symbol,
                side="SELL",
                quantity=test_quantity,
                reduce_only=True
            )
            print(f"[成功] 平仓订单ID: {close_order.get('orderId', 'N/A')}")
            print(f"[状态] 订单状态: {close_order.get('status', 'N/A')}")
        except Exception as e:
            print(f"[失败] 平仓失败: {e}")
            return False

        # 3️⃣ 查询账户
        print("\n[步骤4] 查询账户信息...")
        try:
            account = client.get_account()
            equity = account.get('equity', 0)
            available = account.get('available', 0)
            initial_margin = account.get('totalInitialMargin', 0)

            print(f"[账户] 账户权益: ${equity:.2f}")
            print(f"[账户] 可用资金: ${available:.2f}")
            print(f"[账户] 初始保证金: ${initial_margin:.4f}")

            if initial_margin > 0:
                print("\n[成功] PAPI交易测试完成！")
                print("[通过] ✅ 开仓功能正常")
                print("[通过] ✅ 平仓功能正常")
                print("[通过] ✅ 账户信息查询正常")
                return True
            else:
                print("\n[警告] 保证金信息异常")
                return False

        except Exception as e:
            print(f"[失败] 账户查询失败: {e}")
            return False

    except Exception as e:
        print(f"[错误] 测试失败: {e}")
        print("\n[提示] 请检查：")
        print("1. API Key和Secret是否正确")
        print("2. 账户是否有足够的保证金")
        print("3. 是否启用了IP白名单限制")
        print("4. 账户是否支持SOLUSDT交易")
        return False

    finally:
        print("\n" + "=" * 70)
        print("[结束] 测试完成")
        print("=" * 70)


if __name__ == "__main__":
    print("\n[警告] 此脚本将进行真实的交易操作！")
    print("[警告] 请确保：")
    print("  1. 账户有足够的保证金")
    print("  2. 了解交易风险")
    print("  3. 使用最小测试数量")
    print()

    confirm = input("确认继续测试？(输入 YES 继续): ")
    if confirm.upper() == "YES":
        success = test_papi_trading()
        sys.exit(0 if success else 1)
    else:
        print("\n[取消] 测试已取消")
        sys.exit(0)
