import os
import time
from typing import Any, Dict, List, Optional

from src.api.market_gateway import MarketGateway
from src.trading.intents import TradeIntent, PositionSide as IntentPositionSide
from src.trading.position_state_machine_v2 import PositionStateMachineV2
from src.trading.order_gateway import OrderGateway

# (此处保留之前的 AccountMode, ApiCapability, BinanceBroker, PositionGateway, BalanceEngine 定义)
# 由于文件较长，我将在下一步中使用 replace_string_in_file 进行精确瘦身操作。

