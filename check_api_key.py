#!/usr/bin/env python3
"""
API Key检测脚本
用于验证Binance API Key的权限是否正确
"""

import os
import sys
from src.api.binance_client import BinanceClient, ApiCapability

def check_api_key():
    """检查API Key权限"""
    print("[检测] 开始检测API Key权限...")
    print("=" * 60)

    try:
        # 初始化客户端
        client = BinanceClient()
        broker = client.broker

        print(f"[成功] API Key连接成功")
        print(f"[模式] 账户模式: {broker.account_mode.value}")
        print(f"[能力] API能力: {broker.capability.value}")

        if broker.capability == ApiCapability.PAPI_ONLY:
            print("\n[通过] API Key是PAPI_ONLY类型（统一保证金账户）")
            print("=" * 60)
            print("[模式] 当前模式：Portfolio Margin统一保证金")
            print("[支持] ✅ 所有下单将走PAPI-UM接口")
            print("[支持] ✅ 自动添加reduceOnly和positionSide参数")
            print("[说明] 标准期货FAPI不会被使用")
            print("=" * 60)

            # 测试账户信息获取
            try:
                account = client.get_account()
                equity = account.get('equity', 0)
                available = account.get('available', 0)
                print(f"[权益] 账户权益: ${equity:.2f}")
                print(f"[资金] 可用资金: ${available:.2f}")
            except Exception as e:
                print(f"[警告] 获取账户信息时出现警告: {e}")

            return True

        elif broker.capability == ApiCapability.STANDARD:
            print("\n[通过] API Key是STANDARD类型（标准期货账户）")
            print("=" * 60)
            print("[支持] ✅ 标准期货FAPI权限")
            print("[支持] ✅ 机器人可以正常下单")
            print("[支持] ✅ 账户模式适合机器人运行")
            print("=" * 60)

            # 测试账户信息获取
            try:
                account = client.get_account()
                equity = account.get('equity', 0)
                available = account.get('available', 0)
                print(f"[权益] 账户权益: ${equity:.2f}")
                print(f"[资金] 可用资金: ${available:.2f}")
            except Exception as e:
                print(f"[警告] 获取账户信息时出现警告: {e}")

            return True

    except Exception as e:
        print(f"[失败] API Key检测失败: {e}")
        print("\n可能的原因：")
        print("1. API Key或Secret错误")
        print("2. IP地址未添加到白名单")
        print("3. 网络连接问题")
        print("4. Key权限不足")
        return False

if __name__ == "__main__":
    success = check_api_key()
    sys.exit(0 if success else 1)