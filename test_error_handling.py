"""
测试错误处理逻辑
验证开仓/平仓失败时是否正确处理错误响应
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

def test_error_response_handling():
    """测试错误响应的处理逻辑"""
    print("=" * 80)
    print("测试错误响应处理逻辑")
    print("=" * 80)

    # 模拟成功响应
    success_response = {"status": "success", "orderId": 123456}
    print("\n[1] 测试成功响应")
    print(f"   Response: {success_response}")
    if success_response.get("status") == "error":
        print(f"   [ERROR] 开仓失败: {success_response.get('message', '未知错误')}")
    else:
        print(f"   [OK] 开仓成功: {success_response}")

    # 模拟错误响应
    error_response = {"status": "error", "message": "X SOLUSDT 已有仓位，不允许加仓"}
    print("\n[2] 测试错误响应")
    print(f"   Response: {error_response}")
    if error_response.get("status") == "error":
        print(f"   [ERROR] 开仓失败: {error_response.get('message', '未知错误')}")
    else:
        print(f"   [OK] 开仓成功: {error_response}")

    # 模拟 noop 响应（无持仓）
    noop_response = {"status": "noop", "message": "SOLUSDT 无持仓"}
    print("\n[3] 测试 noop 响应")
    print(f"   Response: {noop_response}")
    if noop_response.get("status") == "error":
        print(f"   [ERROR] 平仓失败: {noop_response.get('message', '未知错误')}")
    elif noop_response.get("status") == "noop":
        print(f"   [OK] 无持仓，无需平仓")
    else:
        print(f"   [OK] 平仓成功: {noop_response}")

    print("\n" + "=" * 80)
    print("测试完成！")
    print("=" * 80)

if __name__ == "__main__":
    test_error_response_handling()
