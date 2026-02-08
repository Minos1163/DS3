from src.config.env_manager import EnvManager

from src.api.binance_client import BinanceClient

from src.trading.trade_executor import TradeExecutor

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        EnvManager.load_env_file(str(env_path))


def _get_test_symbol(config_path: Path) -> str:
    import json

    cfg = json.load(open(config_path, "r", encoding="utf-8"))
    dca = cfg.get("dca_rotation", {})
    symbols = dca.get("symbols", [])
    symbol = os.getenv("REAL_TEST_SYMBOL") or (symbols[0] if symbols else "BTC")
    symbol = symbol.upper()
    return symbol if symbol.endswith("USDT") else f"{symbol}USDT"


def main() -> None:
    if os.getenv("BINANCE_DRY_RUN") == "1":
        raise RuntimeError("BINANCE_DRY_RUN=1，真实测试单已禁用")

    if os.getenv("REAL_TEST_ORDER", "").lower() not in ("1", "true", "yes"):
        raise RuntimeError("未设置 REAL_TEST_ORDER=1，真实测试单未启用")

    _load_env()

    api_key, api_secret = EnvManager.get_api_credentials()
    client = BinanceClient(api_key=api_key, api_secret=api_secret)

    config_path = ROOT / "config" / "trading_config.json"
    symbol = _get_test_symbol(config_path)

    # 避免干扰已有仓位
    pos_long = client.get_position(symbol, side="LONG")
    pos_short = client.get_position(symbol, side="SHORT")
    if (pos_long and abs(float(pos_long.get("positionAmt", 0))) > 0) or (
        pos_short and abs(float(pos_short.get("positionAmt", 0))) > 0
    ):
        raise RuntimeError(f"{symbol} 已有仓位，请先清理再做测试")

    executor = TradeExecutor(client, {})

    ticker = client.get_ticker(symbol)
    price = float(ticker.get("lastPrice", 0)) if ticker else 0.0
    if price <= 0:
        raise RuntimeError("无法获取价格")

    # 使用 1 USDT 名义，自动规整为最小下单量
    qty_raw = 1.0 / price
    qty = client.format_quantity(symbol, qty_raw)
    qty = client.ensure_min_notional_quantity(symbol, qty, price)

    # 小幅 TP/SL 测试
    tp_long = price * 1.002
    sl_long = price * 0.998
    tp_short = price * 0.998
    sl_short = price * 1.002

    # 读取杠杆（从配置中）
    import json

    cfg = json.load(open(config_path, "r", encoding="utf-8"))
    leverage = int(cfg.get("dca_rotation", {}).get("params", {}).get("leverage", 3))

    print(f"[TEST] symbol={symbol} qty={qty} leverage={leverage}")
    print("[TEST] open SHORT")
    executor.open_short(symbol, quantity=qty, leverage=leverage, take_profit=tp_short, stop_loss=sl_short)

    print("[TEST] open LONG")
    executor.open_long(symbol, quantity=qty, leverage=leverage, take_profit=tp_long, stop_loss=sl_long)

    print("[TEST] wait 60s...")
    time.sleep(60)

    print("[TEST] close SHORT")
    executor.close_short(symbol)

    print("[TEST] close LONG")
    executor.close_long(symbol)

    print("[TEST] done")


if __name__ == "__main__":
    main()
