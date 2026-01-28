#!/usr/bin/env python3
"""
持仓模式检测脚本
验证账户的持仓模式（单向/双向）
"""

import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.api.binance_client import BinanceClient, AccountMode, ApiCapability


def check_position_mode():
    """检查当前持仓模式"""
    print("=" * 70)
    print("[检测] 账户持仓模式检测")
    print("=" * 70)

    try:
        # 初始化客户端
        client = BinanceClient()
        broker = client.broker

        print(f"\n[模式] 账户模式: {broker.account_mode.value}")
        print(f"[能力] API能力: {broker.capability.value}")

        # 获取持仓模式
        is_hedge = client.order._get_hedge_mode()

        if is_hedge:
            print(f"\n[持仓] 双向持仓模式（Hedge Mode）")
            print("=" * 70)
            print("[特性] ✅ 可以同时持有多空仓位")
            print("[要求] ❌ 下单必须指定LONG或SHORT")
            print("[禁止] ❌ 禁止使用positionSide=BOTH")
            print("[说明] 这是PAPI + Hedge Mode的必需要求")
            print("=" * 70)
            print("\n[下单示例]")
            print("\n开多仓：")
            print('  params = {side:"BUY", positionSide:"LONG", reduceOnly:"false"}')
            print("\n平多仓：")
            print('  params = {side:"SELL", positionSide:"LONG", reduceOnly:"true"}')
            print("\n开空仓：")
            print('  params = {side:"SELL", positionSide:"SHORT", reduceOnly:"false"}')
            print("\n平空仓：")
            print('  params = {side:"BUY", positionSide:"SHORT", reduceOnly:"true"}')
        else:
            print(f"\n[持仓] 单向持仓模式")
            print("=" * 70)
            print("[特性] ✅ 同一时间只能持有一方向")
            print("[要求] ✅ 必须使用positionSide=BOTH")
            print("[说明] 传统的期货账户模式")
            print("=" * 70)
            print("\n[下单示例]")
            print("\n开仓：")
            print('  params = {side:"BUY", positionSide:"BOTH", reduceOnly:"false"}')
            print("\n平仓：")
            print('  params = {side:"SELL", positionSide:"BOTH", reduceOnly:"true"}')

        # 验证positionSide计算
        print("\n" + "=" * 70)
        print("[测试] 验证positionSide自动计算")
        print("=" * 70)

        test_cases = [
            ("开多仓", "BUY", False),
            ("平多仓", "SELL", True),
            ("开空仓", "SELL", False),
            ("平空仓", "BUY", True),
        ]

        for desc, side, reduce_only in test_cases:
            position_side = client.order._position_side(side, reduce_only)
            print(f"{desc:8s} | side={side:4s} | reduceOnly={reduce_only!s:5s} | → positionSide={position_side}")

        print("\n" + "=" * 70)

        # 给出明确建议
        if is_hedge and broker.capability == ApiCapability.PAPI_ONLY:
            print("\n[重要] 您的系统配置：")
            print("  ✅ API Key: PAPI_ONLY（统一保证金）")
            print("  ✅ 账户模式: 双向持仓（Hedge Mode）")
            print("  ✅ 下单接口: PAPI-UM")
            print("  ✅ 参数适配: 自动计算positionSide")
            print("\n[状态] 完美匹配！可以正常下单")
        elif not is_hedge and broker.capability == ApiCapability.PAPI_ONLY:
            print("\n[警告] 您的系统配置：")
            print("  ⚠️  API Key: PAPI_ONLY（统一保证金）")
            print("  ⚠️  账户模式: 单向持仓（One-way）")
            print("  ✅ 下单接口: PAPI-UM")
            print("  ✅ 参数适配: positionSide=BOTH")
            print("\n[状态] 可以正常下单")

        return True

    except Exception as e:
        print(f"[失败] 检测失败: {e}")
        print("\n[提示] 可能原因：")
        print("1. API Key或Secret错误")
        print("2. IP地址未添加到白名单")
        print("3. 网络连接问题")
        return False


if __name__ == "__main__":
    success = check_position_mode()
    sys.exit(0 if success else 1)
