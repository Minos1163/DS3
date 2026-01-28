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
    
    # Mock broker.request 来验证URL
    with patch.object(client.broker, 'request') as mock_request:
        mock_request.return_value.json.return_value = {"orderId": 123}
        
        # 测试下单
        try:
            client.order.place_order(
                symbol="SOLUSDT",
                side="BUY",
                quantity=0.15,
                reduce_only=False
            )
        except:
            pass
        
        # 验证是否调用了FAPI
        assert mock_request.called, "❌ 没有调用broker.request"
        
        call_args = mock_request.call_args
        # request(method, url, params=..., signed=...)
        # 所以 url 在第二个位置参数
        if call_args.args and len(call_args.args) > 1:
            url = call_args.args[1]
        else:
            url = call_args.kwargs.get('url', '')
        
        print(f"✅ 调用URL: {url}")
        
        if "fapi.binance.com" in url:
            print("✅ 正确使用FAPI端点 (fapi.binance.com)")
            return True
        elif "papi.binance.com" in url:
            print("❌ 错误！使用了PAPI端点 (papi.binance.com)")
            return False
        else:
            print(f"⚠️  未识别的URL: {url}")
            return False


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
        
        # 测试平仓单（应该加reduce_only）
        try:
            client.order.place_order(
                symbol="SOLUSDT",
                side="BUY",
                quantity=0.15,
                reduce_only=True
            )
        except:
            pass
        
        # 验证参数
        call_args = mock_request.call_args
        params = call_args.kwargs.get('params', {})
        
        if "reduceOnly" in params:
            print(f"✅ reduceOnly 参数已添加: {params['reduceOnly']}")
            if params['reduceOnly'] == "true":
                print("✅ reduceOnly 值正确 (true)")
                return True
            else:
                print(f"❌ reduceOnly 值错误: {params['reduceOnly']}")
                return False
        else:
            print("❌ reduceOnly 参数未传递")
            print(f"   收到的参数: {params}")
            return False


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
    
    # Mock必要的方法
    with patch.object(executor.client, 'get_position') as mock_get_pos:
        with patch.object(executor.client, 'format_quantity') as mock_format:
            with patch.object(executor.client, 'cancel_all_orders') as mock_cancel:
                with patch.object(executor.client, 'create_market_order') as mock_order:
                    
                    # 设置mock返回值
                    mock_get_pos.return_value = {
                        "symbol": "SOLUSDT",
                        "positionAmt": "-0.15",  # 空头持仓
                        "entryPrice": "126.0"
                    }
                    mock_format.return_value = 0.15
                    mock_order.return_value = {"orderId": 789}
                    
                    try:
                        executor.close_position("SOLUSDT")
                    except:
                        pass
                    
                    # 验证调用参数
                    if mock_order.called:
                        call_args = mock_order.call_args
                        kwargs = call_args.kwargs
                        
                        print(f"📋 create_market_order 被调用，参数: {kwargs}")
                        
                        if "reduce_only" in kwargs:
                            if kwargs["reduce_only"] == True:
                                print("✅ close_position 正确传递了 reduce_only=True")
                                return True
                            else:
                                print(f"❌ reduce_only值错误: {kwargs['reduce_only']}")
                                return False
                        else:
                            print("❌ close_position 未传递 reduce_only 参数")
                            print(f"   实际参数: {kwargs}")
                            return False
                    else:
                        print("⚠️  create_market_order 未被调用")
                        return False


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
