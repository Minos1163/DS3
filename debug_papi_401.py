#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PAPI 401 错误排查脚本
详细测试 PAPI 端点和签名问题
"""

import os
import sys
import time
import hmac
import hashlib
from typing import Dict, Any

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def load_env():
    """加载环境变量"""
    # 直接读取 .env 文件
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

    if not os.path.exists(env_file):
        print(f"[ERROR] 找不到 .env 文件: {env_file}")
        sys.exit(1)

    # 读取 .env 文件
    with open(env_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("[ERROR] .env 文件中未找到 BINANCE_API_KEY 或 BINANCE_SECRET")
        sys.exit(1)

    return api_key, api_secret

def test_public_endpoint():
    """测试公开端点（不需要签名）"""
    print("\n[测试 1] 测试公开端点...")
    print("-" * 60)

    import requests

    try:
        url = "https://papi.binance.com/papi/v1/um/exchangeInfo"
        response = requests.get(url, timeout=10)
        print(f"[状态码] {response.status_code}")

        if response.status_code == 200:
            print("[OK] 公开端点正常 - 网络连接正常")
            return True
        else:
            print(f"[FAIL] 公开端点异常: {response.text[:200]}")
            return False

    except Exception as e:
        print(f"[FAIL] 公开端点测试失败: {e}")
        return False

def test_signed_read_only(api_key: str, api_secret: str):
    """测试只读签名端点（需要签名，不需要交易权限）"""
    print("\n[测试 2] 测试只读签名端点...")
    print("-" * 60)

    import requests

    url = "https://papi.binance.com/papi/v1/um/account"

    try:
        timestamp = int(time.time() * 1000)
        params = {
            "timestamp": timestamp,
            "recvWindow": 5000
        }

        # 生成签名
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

        params["signature"] = signature

        headers = {
            "X-MBX-APIKEY": api_key
        }

        print(f"[请求] {url}")
        print(f"[参数] timestamp={timestamp}")
        print(f"[签名] {signature[:16]}...")

        response = requests.get(url, params=params, headers=headers, timeout=10)

        print(f"[状态码] {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print("[OK] 只读端点正常 - 签名正确")
            print(f"[账户] 总权益: {data.get('accountEquity', 'N/A')}")
            return True
        elif response.status_code == 401:
            print(f"[FAIL] 401 Unauthorized - 签名错误或 API Key 无效")
            print(f"[响应] {response.text}")
            return False
        else:
            print(f"[FAIL] 其他错误: {response.text[:200]}")
            return False

    except Exception as e:
        print(f"[FAIL] 只读端点测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_hedge_mode(api_key: str, api_secret: str):
    """测试查询对冲模式端点"""
    print("\n[测试 3] 测试查询对冲模式...")
    print("-" * 60)

    import requests

    url = "https://papi.binance.com/papi/v1/um/positionSide/dual"

    try:
        timestamp = int(time.time() * 1000)
        params = {
            "timestamp": timestamp,
            "recvWindow": 5000
        }

        # 生成签名
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

        params["signature"] = signature

        headers = {
            "X-MBX-APIKEY": api_key
        }

        print(f"[请求] {url}")

        response = requests.get(url, params=params, headers=headers, timeout=10)

        print(f"[状态码] {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            dual = data.get("dualSidePosition", False)
            mode = "双向持仓 (Hedge Mode)" if dual else "单向持仓"
            print(f"[OK] 查询成功")
            print(f"[模式] {mode}")
            return True
        elif response.status_code == 401:
            print(f"[FAIL] 401 Unauthorized - 签名错误或 API Key 无效")
            return False
        else:
            print(f"[FAIL] 其他错误: {response.text}")
            return False

    except Exception as e:
        print(f"[FAIL] 对冲模式查询失败: {e}")
        return False

def test_leverage_endpoint(api_key: str, api_secret: str):
    """测试杠杆设置端点（需要交易权限）"""
    print("\n[测试 4] 测试杠杆设置端点...")
    print("-" * 60)

    import requests

    url = "https://papi.binance.com/papi/v1/um/leverage"

    try:
        timestamp = int(time.time() * 1000)
        params = {
            "symbol": "BTCUSDT",
            "leverage": 1,
            "timestamp": timestamp,
            "recvWindow": 5000
        }

        # 生成签名
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

        params["signature"] = signature

        headers = {
            "X-MBX-APIKEY": api_key
        }

        print(f"[请求] {url}")
        print(f"[参数] symbol=BTCUSDT, leverage=1")

        response = requests.post(url, params=params, headers=headers, timeout=10)

        print(f"[状态码] {response.status_code}")

        if response.status_code == 200:
            print("[OK] 杠杆设置成功 - API Key 有交易权限")
            return True
        elif response.status_code == 401:
            print(f"[FAIL] 401 Unauthorized - API Key 没有交易权限")
            print(f"[响应] {response.text}")
            return False
        elif response.status_code == 400:
            print(f"[WARN] 400 Bad Request - 参数错误或权限问题")
            print(f"[响应] {response.text[:300]}")
            return False
        else:
            print(f"[FAIL] 其他错误: {response.text[:200]}")
            return False

    except Exception as e:
        print(f"[FAIL] 杠杆设置测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_order_endpoint(api_key: str, api_secret: str):
    """测试下单端点（需要交易权限）- 仅测试参数构造，不实际下单"""
    print("\n[测试 5] 测试下单端点签名（不实际下单）...")
    print("-" * 60)

    import requests

    url = "https://papi.binance.com/papi/v1/um/order/test"  # 使用测试端点

    try:
        timestamp = int(time.time() * 1000)

        # 构造一个测试订单参数（不会实际下单）
        params = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.001",
            "reduceOnly": "false",
            "positionSide": "LONG",
            "timestamp": timestamp,
            "recvWindow": 5000
        }

        # 生成签名
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

        params["signature"] = signature

        headers = {
            "X-MBX-APIKEY": api_key
        }

        print(f"[请求] {url}")
        print(f"[参数] symbol=BTCUSDT, side=BUY, type=MARKET")
        print(f"[参数] reduceOnly=false, positionSide=LONG")
        print(f"[签名] {signature[:16]}...")

        response = requests.post(url, params=params, headers=headers, timeout=10)

        print(f"[状态码] {response.status_code}")

        if response.status_code == 200:
            print("[OK] 测试端点成功 - 签名正确，参数格式正确")
            return True
        elif response.status_code == 401:
            print(f"[FAIL] 401 Unauthorized - API Key 没有交易权限")
            print(f"[响应] {response.text}")
            return False
        elif response.status_code == 400:
            data = response.json()
            print(f"[WARN] 400 Bad Request - 参数或权限问题")
            print(f"[错误码] {data.get('code', 'N/A')}")
            print(f"[错误信息] {data.get('msg', 'N/A')}")

            if "signature" in str(data.get('msg', '')).lower():
                print("[提示] 签名错误，请检查 API Secret")
            elif "permission" in str(data.get('msg', '')).lower() or "auth" in str(data.get('msg', '')).lower():
                print("[提示] 权限不足，请检查 API Key 权限设置")

            return False
        else:
            print(f"[FAIL] 其他错误: {response.text[:200]}")
            return False

    except Exception as e:
        print(f"[FAIL] 下单端点测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def print_diagnostics():
    """输出诊断建议"""
    print("\n" + "=" * 60)
    print("[诊断] 根据测试结果，可能的原因：")
    print("=" * 60)

    print("\n1. 如果所有测试都失败 401：")
    print("   -> API Key 或 Secret 错误")
    print("   -> 检查 .env 文件中的 API Key 是否正确复制")
    print("   -> 确保没有多余的空格或换行符")

    print("\n2. 如果公开端点成功，但签名端点失败 401：")
    print("   -> API Secret 错误")
    print("   -> 重新生成 API Secret 并更新到 .env 文件")

    print("\n3. 如果只读端点成功，但交易端点失败 401：")
    print("   -> API Key 没有交易权限")
    print("   -> 登录 Binance，检查 API Key 权限设置")
    print("   -> 确保启用了 'Enable Futures' 和 'Enable Spot & Margin Trading'")
    print("   -> 统一保证金账户需要 PAPI 权限")

    print("\n4. 如果出现 IP 限制错误：")
    print("   -> 检查 Binance API 设置中的 IP 白名单")
    print("   -> 将你的公网 IP 添加到白名单")

    print("\n5. 检查 API Key 是否被禁用或过期：")
    print("   -> 登录 Binance，查看 API Key 状态")
    print("   -> 确保状态为 'Enabled'")

    print("\n6. 确认账户类型：")
    print("   -> PAPI Key 只能用于统一保证金账户")
    print("   -> 标准账户应该使用 STANDARD API Key")

def main():
    print("=" * 60)
    print("PAPI 401 错误诊断工具")
    print("=" * 60)

    # 加载环境变量
    api_key, api_secret = load_env()
    print(f"[API Key] {api_key[:8]}...{api_key[-4:]}")
    print(f"[API Secret] {api_secret[:8]}...{api_secret[-4:]}")

    # 运行测试
    results = {}

    results["public"] = test_public_endpoint()
    results["read_only"] = test_signed_read_only(api_key, api_secret)
    results["hedge_mode"] = test_hedge_mode(api_key, api_secret)
    results["leverage"] = test_leverage_endpoint(api_key, api_secret)
    results["order"] = test_order_endpoint(api_key, api_secret)

    # 输出总结
    print("\n" + "=" * 60)
    print("[总结] 测试结果")
    print("=" * 60)

    for test_name, result in results.items():
        status = "[PASS]" if result else "[FAIL]"
        print(f"{test_name:20s} {status}")

    # 输出诊断建议
    print_diagnostics()

    # 返回结果
    all_passed = all(results.values())
    if all_passed:
        print("\n[OK] 所有测试通过！API Key 配置正确。")
        return 0
    else:
        print("\n[FAIL] 部分测试失败，请根据上面的诊断建议检查。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
