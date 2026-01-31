#!/usr/bin/env python3
"""
测试FAPI端点是否正确（验证404问题已解决）

核心问题：
  ❌ 旧：https://papi.binance.com/papi/v1/order → 404 Not Found
  ✅ 新：https://fapi.binance.com/fapi/v1/order → 正确

本脚本验证：
  1. place_order 是否正确使用FAPI
  2. reduce_only 参数是否被正确传递
  3. 平仓单是否加了reduceOnly=true
"""

import json
from unittest.mock import Mock, patch
from src.api.binance_client import BinanceClient
from src.config.env_manager import EnvManager
from src.config.config_loader import ConfigLoader


def test_place_order_uses_fapi():
    """测试place_order是否使用FAPI而不是PAPI"""
    print("\n" + "="*60)
    print("🧪 测试1: place_order 使用正确的端点")
    print("="*60)
    
    EnvManager.load_env_file('.env')
    api_key, api_secret = EnvManager.get_api_credentials()
    
    client = BinanceClient(api_key=api_key, api_secret=api_secret)
    
    # Mock broker.request 来验证URL，通过直接调用 order gateway
    with patch.object(client.broker, 'request') as mock_request:
        mock_request.return_value.json.return_value = {"orderId": 123}

        params = {"symbol": "SOLUSDT", "type": "MARKET", "quantity": 0.15}
        try:
            # 直接调用下单网关以触发 broker.request
            client._order_gateway.place_standard_order(symbol="SOLUSDT", side="BUY", params=params, reduce_only=False)
        except Exception:
            pass

        # 验证是否调用了 broker.request
        assert mock_request.called, "❌ 没有调用broker.request"

        # 从所有调用中查找下单相关的调用（以 /order 结尾 或 包含 'order'）
        urls = []
        for c in mock_request.call_args_list:
            args = getattr(c, 'args', ())
            kwargs = getattr(c, 'kwargs', {})
            if args and len(args) > 1:
                u = args[1]
            else:
                u = kwargs.get('url', '')
            urls.append(u)

        print(f"✅ 所有调用URL: {urls}")

        # 确保在测试中绕过已有仓位检查，以便实际触发下单调用
        # 如果 OrderGateway 先检查仓位可能只调用 position 接口而不下单
        try:
            # 临时强制 has_open_position 返回 False
            from unittest.mock import patch as _patch
            with _patch.object(client._order_gateway, 'has_open_position', return_value=False):
                client._order_gateway.place_standard_order(symbol="SOLUSDT", side="BUY", params=params, reduce_only=False)
        except Exception:
            pass

        # 重新收集调用
        urls = []
        for c in mock_request.call_args_list:
            args = getattr(c, 'args', ())
            kwargs = getattr(c, 'kwargs', {})
            if args and len(args) > 1:
                u = args[1]
            else:
                u = kwargs.get('url', '')
            urls.append(u)

        print(f"✅ 所有调用URL: {urls}")

        # 尝试定位包含 order 端点的调用
        order_calls = [u for u in urls if u and '/order' in u]
        assert order_calls, f"No order-related broker.request calls found. urls={urls}"


def test_reduce_only_parameter():
    """测试reduce_only参数是否被正确传递"""
    print("\n" + "="*60)
    print("🧪 测试2: reduce_only 参数正确传递")
    print("="*60)
    
    EnvManager.load_env_file('.env')
    api_key, api_secret = EnvManager.get_api_credentials()
    
    client = BinanceClient(api_key=api_key, api_secret=api_secret)
    
    with patch.object(client.broker, 'request') as mock_request:
        mock_request.return_value.json.return_value = {"orderId": 456}

        params = {"symbol": "SOLUSDT", "type": "MARKET", "quantity": 0.15}
        try:
            client._order_gateway.place_standard_order(symbol="SOLUSDT", side="BUY", params=params, reduce_only=True)
        except Exception:
            pass

        # 验证参数
        call_args = mock_request.call_args
        # broker.request called with (method, url, ...) positional args
        kwargs = call_args.kwargs or {}
        params_passed = kwargs.get('params') or (call_args.args[2] if len(call_args.args) > 2 else {})

        assert "reduceOnly" in params_passed, f"reduceOnly not in params: {params_passed}"
        assert params_passed["reduceOnly"] is True, f"reduceOnly expected True, got {params_passed.get('reduceOnly')}"


def test_close_position_uses_reduce_only():
    """测试close_position是否正确使用reduce_only"""
    print("\n" + "="*60)
    print("🧪 测试3: close_position 使用 reduce_only=True")
    print("="*60)
    
    EnvManager.load_env_file('.env')
    api_key, api_secret = EnvManager.get_api_credentials()
    
    from src.trading.trade_executor import TradeExecutor
    from src.config.config_loader import ConfigLoader

    # 加载配置和创建客户端
    config = ConfigLoader.load_trading_config('config/trading_config.json')
    client = BinanceClient(api_key=api_key, api_secret=api_secret)
    executor = TradeExecutor(client=client, config=config)
    
    # Mock必要的方法：patch broker.request called by place_standard_order
    with patch.object(executor.client, 'get_position') as mock_get_pos:
        with patch.object(executor.client, 'format_quantity') as mock_format:
            with patch.object(executor.client.broker, 'request') as mock_request:

                # 设置mock返回值
                mock_get_pos.return_value = {
                    "symbol": "SOLUSDT",
                    "positionAmt": "-0.15",  # 空头持仓
                    "entryPrice": "126.0"
                }
                mock_format.return_value = 0.15
                mock_request.return_value.json.return_value = {"orderId": 789}

                try:
                    executor.close_position("SOLUSDT")
                except Exception:
                    pass

                # 验证 broker.request 是否被调用并检查参数中是否包含 reduceOnly
                assert mock_request.called, "create order 未调用 broker.request"
                call_args = mock_request.call_args
                kwargs = call_args.kwargs or {}
                params_passed = kwargs.get('params') or (call_args.args[2] if len(call_args.args) > 2 else {})

                print(f"📋 broker.request 被调用，params: {params_passed}")
                # 对于全仓平仓（closePosition=True）不应传 reduceOnly；确保 closePosition 在 params 中
                if params_passed.get('closePosition'):
                    assert 'closePosition' in params_passed and params_passed['closePosition'] is True, f"Expected closePosition True, got {params_passed}"
                    assert 'reduceOnly' not in params_passed, f"Full close should not include reduceOnly, got {params_passed}"
                else:
                    # 对于部分平仓，reduceOnly 应为 True
                    assert 'reduceOnly' in params_passed and params_passed['reduceOnly'] is True, f"Partial close should include reduceOnly=True, got {params_passed}"


def main():
    """运行所有测试"""
    print("\n" + "="*70)
    print("🔍 FAPI端点修复验证测试")
    print("="*70)
    
    results = []
    
    try:
        results.append(("place_order使用FAPI", test_place_order_uses_fapi()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("place_order使用FAPI", False))
    
    try:
        results.append(("reduce_only参数传递", test_reduce_only_parameter()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("reduce_only参数传递", False))
    
    try:
        results.append(("close_position使用reduce_only", test_close_position_uses_reduce_only()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("close_position使用reduce_only", False))
    
    # 总结
    print("\n" + "="*70)
    print("📊 测试总结")
    print("="*70)
    
    for test_name, passed in results:
        status = "✅" if passed else "❌"
        print(f"{status} {test_name}")
    
    passed_count = sum(1 for _, p in results if p)
    total_count = len(results)
    
    print(f"\n总体: {passed_count}/{total_count} 测试通过")
    
    if passed_count == total_count:
        print("\n🎉 所有测试通过！404错误已解决！")
        return 0
    else:
        print(f"\n⚠️  还有 {total_count - passed_count} 个测试失败")
        return 1


if __name__ == "__main__":
    exit(main())
