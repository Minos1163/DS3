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
    print("🔍 开始检测API Key权限...")
    print("=" * 60)
    
    try:
        # 初始化客户端
        client = BinanceClient()
        broker = client.broker
        
        print(f"✅ API Key连接成功")
        print(f"📊 账户模式: {broker.account_mode.value}")
        print(f"🔑 API能力: {broker.capability.value}")
        
        if broker.capability == ApiCapability.PAPI_ONLY:
            print("\n❌ 检测到问题：当前API Key是PAPI_ONLY类型")
            print("=" * 60)
            print("📋 问题分析：")
            print("- 您的Key仅具备Portfolio Margin权限")
            print("- 无法调用标准期货FAPI接口")
            print("- 机器人将无法下单")
            print("\n👉 解决方案：")
            print("1. 登录币安官网 (https://www.binance.com)")
            print("2. 进入API管理页面")
            print("3. 创建一个新的API Key（不要勾选Portfolio Margin）")
            print("4. 确保勾选「Enable Futures」权限")
            print("5. 更新.env文件中的API Key和Secret")
            print("6. 重新运行此脚本验证")
            print("=" * 60)
            return False
            
        elif broker.capability == ApiCapability.STANDARD:
            print("\n🎉 API Key权限正确！")
            print("=" * 60)
            print("✅ 当前Key具备标准期货FAPI权限")
            print("✅ 机器人可以正常下单")
            print("✅ 账户模式适合机器人运行")
            print("=" * 60)
            
            # 测试账户信息获取
            try:
                account = client.get_account()
                equity = account.get('equity', 0)
                available = account.get('available', 0)
                print(f"📈 账户权益: ${equity:.2f}")
                print(f"💰 可用资金: ${available:.2f}")
            except Exception as e:
                print(f"⚠️  获取账户信息时出现警告: {e}")
                
            return True
            
    except Exception as e:
        print(f"❌ API Key检测失败: {e}")
        print("\n可能的原因：")
        print("1. API Key或Secret错误")
        print("2. IP地址未添加到白名单")
        print("3. 网络连接问题")
        print("4. Key权限不足")
        return False

if __name__ == "__main__":
    success = check_api_key()
    sys.exit(0 if success else 1)