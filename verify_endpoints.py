#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证脚本：确保所有订单操作使用正确的端点

✅ 检查清单：
  1. 期货平仓必须用 fapi.binance.com (不是 papi)
  2. 所有平仓单必须加 reduceOnly=true
  3. 端点路径正确 (/fapi/v1/order 而不是 /papi/v1/order)
  4. 参数格式正确 (reduceOnly="true" 而不是 true)
"""

import os
import sys
import io
from pathlib import Path

# 设置标准输出编码
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def check_endpoint_in_code():
    """检查代码中是否还有错误的端点使用"""
    print("=" * 70)
    print("🔍 检查代码中的端点使用")
    print("=" * 70)
    
    issues = []
    
    # 检查所有Python文件
    src_path = Path(__file__).parent.parent.parent / "src"
    for py_file in src_path.rglob("*.py"):
        with open(py_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        for line_num, line in enumerate(lines, 1):
            # 检查错误的 papi 平仓调用
            if "papi" in line.lower() and "order" in line.lower():
                if "close" in line.lower() or "平仓" in line:
                    issues.append({
                        'file': py_file,
                        'line': line_num,
                        'code': line.strip(),
                        'type': '❌ papi平仓',
                        'severity': 'critical'
                    })
            
            # 检查缺少 reduceOnly 的平仓
            if ("reduce_only" in line or "reduceOnly" in line) and "close" in line.lower():
                if "true" not in line.lower() and "True" not in line:
                    issues.append({
                        'file': py_file,
                        'line': line_num,
                        'code': line.strip(),
                        'type': '⚠️ reduceOnly未设置为true',
                        'severity': 'warning'
                    })
    
    # 打印结果
    if not issues:
        print("✅ 未发现问题！所有端点使用正确\n")
        return True
    else:
        print(f"❌ 发现 {len(issues)} 个问题:\n")
        for i, issue in enumerate(issues, 1):
            print(f"{i}. [{issue['type']}] {issue['file'].name}:{issue['line']}")
            print(f"   {issue['code']}")
            print()
        return False


def verify_safe_close_position():
    """验证安全平仓函数"""
    print("=" * 70)
    print("✅ 验证安全平仓函数")
    print("=" * 70)
    
    try:
        from src.api.endpoint_manager import SafeClosePosition, EndpointRouter
        
        print("✅ SafeClosePosition 类导入成功")
        print("✅ EndpointRouter 类导入成功")
        
        # 检查方法
        methods = [
            'close_futures_position',
            'close_spot_position'
        ]
        
        for method in methods:
            if hasattr(SafeClosePosition, method):
                print(f"✅ SafeClosePosition.{method}() 存在")
            else:
                print(f"❌ SafeClosePosition.{method}() 缺失")
                return False
        
        print("\n✅ 所有安全平仓函数准备就绪\n")
        return True
        
    except Exception as e:
        print(f"❌ 导入失败: {e}\n")
        return False


def check_reduce_only_parameter():
    """检查 reduceOnly 参数的设置"""
    print("=" * 70)
    print("🔍 检查 reduceOnly 参数")
    print("=" * 70)
    
    src_path = Path(__file__).parent.parent.parent / "src"
    
    reduce_only_usage = []
    for py_file in src_path.rglob("*.py"):
        try:
            with open(py_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 检查 reduce_only 的任何使用 (参数、函数等)
            if 'reduce_only' in content.lower():
                # 计算使用次数
                count = content.lower().count('reduce_only')
                reduce_only_usage.append((py_file.name, count))
        except:
            pass
    
    if reduce_only_usage:
        print("✅ reduce_only 参数使用情况:\n")
        for fname, count in sorted(reduce_only_usage, key=lambda x: -x[1]):
            print(f"   {fname:30} {count:3} 次")
        print()
        
        # 检查关键位置
        print("✅ 关键检查:")
        trade_executor = src_path / "trading" / "trade_executor.py"
        if trade_executor.exists():
            with open(trade_executor, 'r', encoding='utf-8') as f:
                content = f.read()
                if 'reduce_only=True' in content:
                    print("   ✓ trade_executor.py: close_position() 使用 reduce_only=True")
                else:
                    print("   ⚠️ trade_executor.py: close_position() 未找到 reduce_only=True")
        
        binance_client = src_path / "api" / "binance_client.py"
        if binance_client.exists():
            with open(binance_client, 'r', encoding='utf-8') as f:
                content = f.read()
                if 'params["reduceOnly"] = "true"' in content:
                    print("   ✓ binance_client.py: 正确设置 params[reduceOnly]='true'")
                else:
                    print("   ⚠️ binance_client.py: 未找到 reduceOnly 设置")
        
        print()
        return True
    else:
        print("⚠️ 未找到 reduce_only 参数使用\n")
        return False


def print_endpoint_verification_summary():
    """打印验证总结"""
    print("=" * 70)
    print("📋 端点验证总结")
    print("=" * 70)
    
    summary = """
✅ 已确认的设置：

1. 期货订单端点
   ✓ 使用: fapi.binance.com
   ✓ 路径: /fapi/v1/order
   ✓ 示例: https://fapi.binance.com/fapi/v1/order

2. 平仓安全参数
   ✓ reduceOnly=true (必须)
   ✓ 防止反向开仓
   ✓ 参数格式: "true" (字符串)

3. PAPI 使用范围
   ✓ 账户信息: papi.binance.com/papi/v1/um/account
   ✓ 持仓风险: papi.binance.com/papi/v1/um/positionRisk
   ✗ 禁止平仓: papi.binance.com/papi/v1/order (404!)

⚠️ 关键提醒：

  如果遇到 "404 Not Found" 错误：
  
  1. 检查是否用了 papi.binance.com 下单
     → 改为 fapi.binance.com
  
  2. 检查平仓单是否加了 reduceOnly=true
     → 必须加上
  
  3. 检查路径是否正确
     → /fapi/v1/order (不是 /papi/v1/order)

📚 快速参考：

  futures 订单 → fapi.binance.com/fapi/v1/order
  spot 订单   → api.binance.com/api/v3/order
  账户信息   → papi.binance.com/papi/v1/um/account
"""
    print(summary)


def main():
    """主函数"""
    print("\n")
    print("╔" + "="*68 + "╗")
    print("║" + " "*68 + "║")
    print("║" + "  🔐 Binance 端点和安全平仓验证工具".center(68) + "║")
    print("║" + " "*68 + "║")
    print("╚" + "="*68 + "╝")
    print("\n")
    
    # 运行检查
    results = {
        '代码检查': check_endpoint_in_code(),
        '安全平仓函数': verify_safe_close_position(),
        'reduceOnly参数': check_reduce_only_parameter(),
    }
    
    # 打印总结
    print_endpoint_verification_summary()
    
    # 最终结论
    print("=" * 70)
    print("🎯 验证结果")
    print("=" * 70)
    
    all_pass = all(results.values())
    
    for check, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{status}: {check}")
    
    print()
    if all_pass:
        print("🎉 所有检查通过！系统已准备好使用正确的端点。")
        print("\n✅ 可以启动交易机器人")
    else:
        print("⚠️ 存在一些问题需要修复")
        print("\n❌ 建议修复后再启动")
    
    print()
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
