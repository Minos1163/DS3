import os
import sys
from dotenv import load_dotenv

# 添加项目根目录到Python路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

load_dotenv()

try:
    from src.api.binance_client import BinanceClient
    from src.trading.intents import TradeIntent, IntentAction, PositionSide
    from src.trading.position_state_machine import PositionStateMachineV2
    from src.trading.trade_executor import TradeExecutor
    
    print("✅ 所有核心类加载成功")
    
    # 模拟初始化
    api_key = os.getenv("BINANCE_API_KEY", "fake_key")
    api_secret = os.getenv("BINANCE_SECRET", "fake_secret")
    
    # 注意：初始化会触发网络请求去检测能力，所以我们要么提供真实 Key，要么 mock 掉网络。
    # 这里我们只测试编译和基础导入。
    
    client = BinanceClient(api_key=api_key, api_secret=api_secret)
    print(f"✅ BinanceClient 实例创建成功 (模式: {client.broker.account_mode})")
    
    executor = TradeExecutor(client, {})
    print("✅ TradeExecutor 初始化成功")
    
    print("\n🚀 重构后的整体脉络验证通过!")
    
except Exception as e:
    print(f"❌ 加载或初始化失败: {e}")
    import traceback
    traceback.print_exc()
