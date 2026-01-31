"""
测试 closePosition=True 平仓方法

验证：
1. close_long/close_short 使用 closePosition=True 而非 reduceOnly=True + quantity
2. 在 Hedge Mode 下能够确定性平仓
3. 平仓后确认 positionAmt 是否归零
"""

import os
import time
from dotenv import load_dotenv
from src.api.binance_client import BinanceClient
from src.trading.trade_executor import TradeExecutor

load_dotenv()

def test_close_position_fix():
    """测试平仓修复"""
    print("=" * 60)
    print("测试 closePosition=True 平仓方法")
    print("=" * 60)

    client = BinanceClient()
    executor = TradeExecutor(client, {})

    # 测试步骤：
    # 1. 先开一个小仓位
    # 2. 使用新方法平仓
    # 3. 验证仓位是否真的归零

    symbol = "BTCUSDT"
    quantity = 0.001

    print("\n[步骤 1] 开多仓...")
    try:
        result = executor.open_long(symbol, quantity)
        print(f"✓ 开多仓成功: {result.get('orderId')}")
    except Exception as e:
        print(f"❌ 开多仓失败: {e}")
        return

    time.sleep(1)

    # 检查仓位
    print("\n[步骤 2] 检查当前仓位...")
    positions = client.get_all_positions()
    btc_positions = [p for p in positions if p['symbol'] == symbol]
    for pos in btc_positions:
        position_amt = float(pos.get('positionAmt', 0))
        position_side = pos.get('positionSide', 'N/A')
        print(f"  仓位: {symbol} | side={position_side} | amt={position_amt}")

    time.sleep(1)

    # 使用新方法平仓（不再需要传入 quantity）
    print("\n[步骤 3] 使用 close_long 平仓（closePosition=True）...")
    try:
        result = executor.close_long(symbol)  # 不再需要传入 quantity
        print(f"✓ 平多仓成功: {result.get('orderId')}")
    except Exception as e:
        print(f"❌ 平多仓失败: {e}")
        print(f"   详情: {e.response.text if hasattr(e, 'response') else str(e)}")
        return

    time.sleep(2)  # 等待平仓完成

    # 验证仓位是否真的归零
    print("\n[步骤 4] 验证仓位是否归零...")
    positions = client.get_all_positions()
    btc_positions = [p for p in positions if p['symbol'] == symbol]

    if not btc_positions:
        print("✓ 仓位已完全平掉（无持仓）")
    else:
        for pos in btc_positions:
            position_amt = float(pos.get('positionAmt', 0))
            position_side = pos.get('positionSide', 'N/A')
            if abs(position_amt) < 1e-6:
                print(f"✓ {position_side} 仓位已归零: {position_amt}")
            else:
                print(f"⚠️  {position_side} 仓位未完全平掉: {position_amt}")
                print("   可能原因：")
                print("   1. 市价单部分成交")
                print("   2. 网络延迟")
                print("   3. API 限制")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


def test_close_short_position():
    """测试平空仓"""
    print("\n" + "=" * 60)
    print("测试平空仓")
    print("=" * 60)

    client = BinanceClient()

    symbol = "BTCUSDT"
    quantity = 0.001

    print("\n[步骤 1] 开空仓...")
    try:
        result = executor.open_short(symbol, quantity)
        print(f"✓ 开空仓成功: {result.get('orderId')}")
    except Exception as e:
        print(f"❌ 开空仓失败: {e}")
        return

    time.sleep(1)

    # 检查仓位
    print("\n[步骤 2] 检查当前仓位...")
    positions = client.get_all_positions()
    btc_positions = [p for p in positions if p['symbol'] == symbol]
    for pos in btc_positions:
        position_amt = float(pos.get('positionAmt', 0))
        position_side = pos.get('positionSide', 'N/A')
        print(f"  仓位: {symbol} | side={position_side} | amt={position_amt}")

    time.sleep(1)

    # 使用新方法平空仓
    print("\n[步骤 3] 使用 close_short 平仓（closePosition=True）...")
    try:
        result = executor.close_short(symbol)  # 不再需要传入 quantity
        print(f"✓ 平空仓成功: {result.get('orderId')}")
    except Exception as e:
        print(f"❌ 平空仓失败: {e}")
        print(f"   详情: {e.response.text if hasattr(e, 'response') else str(e)}")
        return

    time.sleep(2)  # 等待平仓完成

    # 验证仓位是否真的归零
    print("\n[步骤 4] 验证仓位是否归零...")
    positions = client.get_all_positions()
    btc_positions = [p for p in positions if p['symbol'] == symbol]

    if not btc_positions:
        print("✓ 仓位已完全平掉（无持仓）")
    else:
        for pos in btc_positions:
            position_amt = float(pos.get('positionAmt', 0))
            position_side = pos.get('positionSide', 'N/A')
            if abs(position_amt) < 1e-6:
                print(f"✓ {position_side} 仓位已归零: {position_amt}")
            else:
                print(f"⚠️  {position_side} 仓位未完全平掉: {position_amt}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


def verify_hedge_mode():
    """验证持仓模式"""
    print("\n" + "=" * 60)
    print("验证持仓模式")
    print("=" * 60)

    client = BinanceClient()
    is_hedge = client.broker.get_hedge_mode()

    if is_hedge:
        print("✓ 当前模式: 双向持仓 (Hedge Mode)")
        print("  说明: 必须使用 closePosition=True 来确保确定性平仓")
    else:
        print("✓ 当前模式: 单向持仓 (One-way Mode)")
        print("  说明: 可以使用 reduceOnly 或 closePosition")

    print("=" * 60)


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("CLOSE POSITION FIX 测试套件")
    print("=" * 60)

    # 先验证持仓模式
    verify_hedge_mode()

    # 询问用户是否要执行实际测试
    print("\n⚠️  警告: 此测试会进行实际交易")
    print("  1. 先开一个小仓位")
    print("  2. 然后使用新方法平仓")
    print("\n是否继续？(y/n): ", end="")

    try:
        choice = input().strip().lower()
        if choice == 'y':
            # 测试平多仓
            test_close_position_fix()

            time.sleep(2)

            # 测试平空仓
            test_close_short_position()

            print("\n✅ 所有测试完成")
        else:
            print("已取消测试")
    except KeyboardInterrupt:
        print("\n\n已取消测试")
