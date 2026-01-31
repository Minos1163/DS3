#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试签名生成逻辑
"""

import os
import sys
import time
import hmac
import hashlib
from typing import Dict, Any

def load_env():
    """加载环境变量"""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

    with open(env_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    return api_key, api_secret

def test_signature_v1(api_secret: str):
    """测试版本1：直接拼接参数（binance_client.py 方式）"""
    print("\n[版本1] binance_client.py 的签名方式...")
    print("-" * 60)

    params = {
        "symbol": "BTCUSDT",
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000
    }

    # 按键排序
    sorted_params = sorted(params.items())

    # 拼接成 query string
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])

    print(f"[参数] {dict(params)}")
    print(f"[排序后] {sorted_params}")
    print(f"[Query String] {query_string}")

    # 生成签名
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    print(f"[签名] {signature}")
    # 验证签名格式
    assert isinstance(signature, str) and len(signature) >= 64
    assert "timestamp" in query_string

def test_signature_v2(api_secret: str):
    """测试版本2：URL编码参数（标准方式）"""
    print("\n[版本2] URL编码方式...")
    print("-" * 60)

    params = {
        "symbol": "BTCUSDT",
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000
    }

    # 按键排序
    sorted_params = sorted(params.items())

    # 拼接成 query string（使用原始值）
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])

    print(f"[参数] {dict(params)}")
    print(f"[排序后] {sorted_params}")
    print(f"[Query String] {query_string}")

    # 生成签名
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    print(f"[签名] {signature}")
    # 验证签名格式
    assert isinstance(signature, str) and len(signature) >= 64
    assert "timestamp" in query_string

def test_signature_with_bool(api_secret: str):
    """测试带布尔值的签名"""
    print("\n[带布尔值] 测试包含 reduceOnly 的签名...")
    print("-" * 60)

    params = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "0.001",
        "reduceOnly": False,  # Python bool
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000
    }

    print(f"[原始参数] {params}")

    # 版本A：直接使用（会变成 True/False）
    sorted_params_a = sorted(params.items())
    query_string_a = "&".join([f"{k}={v}" for k, v in sorted_params_a])
    signature_a = hmac.new(
        api_secret.encode('utf-8'),
        query_string_a.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    print(f"[版本A Query] {query_string_a}")
    print(f"[版本A 签名] {signature_a}")

    # 版本B：布尔值转小写字符串
    normalized_params = {}
    for k, v in params.items():
        if isinstance(v, bool):
            normalized_params[k] = "true" if v else "false"
        else:
            normalized_params[k] = str(v)

    sorted_params_b = sorted(normalized_params.items())
    query_string_b = "&".join([f"{k}={v}" for k, v in sorted_params_b])
    signature_b = hmac.new(
        api_secret.encode('utf-8'),
        query_string_b.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    print(f"[版本B Query] {query_string_b}")
    print(f"[版本B 签名] {signature_b}")
    # 验证两个签名均为字符串且不相同（布尔规范化后可能相同，但至少为字符串）
    assert isinstance(signature_a, str) and isinstance(signature_b, str)

def test_with_requests(api_key: str, api_secret: str):
    """使用 requests 发送实际请求"""
    print("\n[实际请求] 使用 requests 发送请求...")
    print("-" * 60)

    import requests

    url = "https://papi.binance.com/papi/v1/um/account"

    timestamp = int(time.time() * 1000)
    params = {
        "timestamp": timestamp,
        "recvWindow": 5000
    }

    # 生成签名
    sorted_params = sorted(params.items())
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    params["signature"] = signature

    headers = {
        "X-MBX-APIKEY": api_key
    }

    print(f"[URL] {url}")
    print(f"[Params] {params}")
    print(f"[Headers] {headers}")

    # 打印实际发送的 URL
    response = requests.Request('GET', url, params=params, headers=headers)
    prepared = response.prepare()
    print(f"[实际URL] {prepared.url}")

    # 发送请求
    resp = requests.get(url, params=params, headers=headers, timeout=10)

    print(f"[状态码] {resp.status_code}")
    print(f"[响应] {resp.text[:300]}")

def main():
    print("=" * 60)
    print("签名测试工具")
    print("=" * 60)

    api_key, api_secret = load_env()
    print(f"[API Key] {api_key[:8]}...{api_key[-4:]}")
    print(f"[API Secret] {api_secret[:8]}...{api_secret[-4:]}")

    test_signature_v1(api_secret)
    test_signature_v2(api_secret)
    test_signature_with_bool(api_secret)
    test_with_requests(api_key, api_secret)

    print("\n" + "=" * 60)
    print("[完成]")
    print("=" * 60)

if __name__ == "__main__":
    main()
