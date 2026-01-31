"""
验证 PAPI 全仓平仓修复
运行此脚本以确保所有修复都已正确加载
"""
import sys
import os

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

print("=" * 80)
print("PAPI 全仓平仓修复验证")
print("=" * 80)

# 1. 验证模块导入
print("\n[1] 验证模块导入...")
try:
    from src.trading.trade_executor import TradeExecutor
    from src.trading.order_gateway import OrderGateway
    from src.trading.position_state_machine import PositionStateMachineV2
    from src.trading.intent_builder import IntentBuilder
    from src.trading.intents import TradeIntent, IntentAction, PositionSide
    print("    [OK] 所有模块导入成功")
except Exception as e:
    print(f"    [ERROR] 模块导入失败: {e}")
    sys.exit(1)

# 2. 验证 IntentBuilder.build_close 逻辑
print("\n[2] 验证 IntentBuilder.build_close 逻辑...")
intent = IntentBuilder.build_close("BTCUSDT", PositionSide.LONG, None)
print(f"    Intent.quantity={intent.quantity}")
print(f"    Intent.reduce_only={intent.reduce_only}")
if intent.quantity is None:
    print("    [OK] 全仓平仓时 quantity=None")
else:
    print(f"    [OK] 全仓平仓时 quantity={intent.quantity}")

# 3. 验证 TradeExecutor._execute_close 逻辑
print("\n[3] 验证 TradeExecutor._execute_close 逻辑...")
import inspect
source = inspect.getsource(TradeExecutor._execute_close)
if "abs(float(pos" in source:
    print("    [OK] _execute_close 使用 abs() 处理持仓数量")
else:
    print("    [WARNING] _execute_close 可能未使用 abs() 处理持仓数量")

if "intent.quantity is None or intent.quantity == 0" in source:
    print("    [OK] _execute_close 正确判断全仓平仓")
else:
    print("    [WARNING] _execute_close 全仓平仓判断可能不正确")

# 4. 验证 PositionStateMachineV2._close 逻辑
print("\n[4] 验证 PositionStateMachineV2._close 逻辑...")
source = inspect.getsource(PositionStateMachineV2._close)
if '"quantity": quantity' in source:
    print("    [OK] _close 正确添加 quantity 参数")
else:
    print("    [ERROR] _close 未正确添加 quantity 参数")

if '"closePosition": True' in source:
    print("    [OK] _close 正确添加 closePosition=True 参数")
else:
    print("    [ERROR] _close 未正确添加 closePosition=True 参数")

# 5. 验证 OrderGateway._finalize_params 逻辑
print("\n[5] 验证 OrderGateway._finalize_params 逻辑...")
source = inspect.getsource(OrderGateway._finalize_params)
if 'if "quantity" not in p or not p["quantity"]' in source:
    print("    [OK] _finalize_params 检查 quantity 是否存在")
else:
    print("    [WARNING] _finalize_params quantity 检查逻辑可能不正确")

if 'p["quantity"] = abs(float(pos.get("positionAmt", 0)))' in source:
    print("    [OK] _finalize_params 从仓位获取 quantity")
else:
    print("    [WARNING] _finalize_params 可能未从仓位获取 quantity")

# 6. 验证 BinanceBroker.request 逻辑
print("\n[6] 验证 BinanceBroker.request 逻辑...")
from src.api.binance_client import BinanceBroker
source = inspect.getsource(BinanceBroker.request)
if '# 保持 quantity 字段' in source or 'PAPI 全仓平仓需要这个参数' in source:
    print("    [OK] request 不移除 quantity 参数")
else:
    print("    [WARNING] request 可能会错误地移除 quantity 参数")

print("\n" + "=" * 80)
print("验证完成！")
print("=" * 80)
print("\n重要提示：")
print("1. 如果所有验证都通过，请重启您的交易程序")
print("2. 确保没有使用旧版本的 .pyc 文件")
print("3. 重新运行 close_position('SOLUSDT') 测试")
print("\n如果问题仍然存在，请检查：")
print("- 是否有其他代码覆盖了这些修复")
print("- 是否有多个版本的代码文件")
print("- Python 环境/虚拟环境是否正确")
