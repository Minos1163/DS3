import os
import sys

import pandas as pd


def normalize_pct(v, default):
    try:
        val = float(v)
    except Exception:
        return default
    if val == 0:
        return 0.0
    sign = -1.0 if val < 0 else 1.0
    val = abs(val)
    if val > 1.0:
        val = val / 100.0
    return sign * val


def find_data_file(symbol: str, interval: str):
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    sym = symbol.upper()
    candidates = [f for f in os.listdir(data_dir) if f.startswith(sym)]
    for c in candidates:
        if interval in c:
            return os.path.join(data_dir, c)
    if candidates:
        return os.path.join(data_dir, candidates[0])
    return None


def main():
    # Ensure project root on path for local imports when run as script
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    from src.config.config_loader import ConfigLoader
    from src.utils.indicators import calculate_atr

    cfg = ConfigLoader.load_trading_config()
    atr_cfg = ConfigLoader.get_atr_config(cfg)
    print("ATR config:", atr_cfg)

    symbol = os.environ.get("TEST_SYMBOL", "SOLUSDT")
    atr_tf = atr_cfg.get("atr_timeframe") or "15m"
    print(f"Using symbol={symbol}, atr_tf={atr_tf}")

    path = find_data_file(symbol, atr_tf)
    if not path:
        print("无法找到本地数据文件，检查 data/ 是否包含对应symbol/interval")
        sys.exit(2)

    df = pd.read_csv(path)
    df = df[["open", "high", "low", "close"]].tail(200)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    atr_val = calculate_atr(df["high"], df["low"], df["close"], period=14)
    print(f"ATR (atr_14) = {atr_val}")

    last_close = float(df["close"].iloc[-1])
    print(f"current_price = {last_close}")

    decision_sl = -0.006
    sl_pct = normalize_pct(decision_sl, -0.006)
    if atr_cfg.get("use_atr_stop_loss") and atr_val and atr_val > 0:
        atr_multiplier = float(atr_cfg.get("atr_multiplier", 3.0))
        sl_price_atr = last_close - atr_val * atr_multiplier
        computed_sl_pct = (sl_price_atr / last_close) - 1.0
        print(f"computed_sl_pct from ATR = {computed_sl_pct:.6f} ({computed_sl_pct * 100:.3f}%)")
        if abs(computed_sl_pct) > abs(sl_pct):
            print("ATR-based SL is wider, applying it")
            sl_pct = computed_sl_pct
        else:
            print("Existing SL is wider or equal, keeping original")

    max_sl_abs_raw = cfg.get("trading", {}).get("max_stop_loss_abs", 0.6)
    max_sl_abs = abs(normalize_pct(max_sl_abs_raw, 0.006))
    print(f"max_sl_abs fraction = {max_sl_abs} ({max_sl_abs * 100:.3f}%)")
    if abs(sl_pct) > max_sl_abs:
        print(f"sl {sl_pct * 100:.3f}% exceeds max allowed, capping")
        sl_pct = -abs(max_sl_abs)

    stop_loss_price = last_close * (1 + sl_pct)
    print(f"Final sl_pct = {sl_pct:.6f} ({sl_pct * 100:.3f}%), stop_loss_price = {stop_loss_price}")


if __name__ == "__main__":
    main()
