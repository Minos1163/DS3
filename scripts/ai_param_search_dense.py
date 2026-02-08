"""Dense local grid search for AI params (min_conf vs max_hold) using 15m x 15D cached data.

Search ranges:
  min_conf in [0.75, 0.85] step 0.02
  max_hold in [150, 300] step 10

Produces: reports/ai_param_search_dense_<ts>.csv and summary CSV.
"""

from __future__ import annotations

from datetime import datetime

from typing import Any, Dict, List

import pandas as pd

from src.backtest import BacktestEngine

import os
import sys
import random


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def idx_of_time(df: pd.DataFrame, time_str: str):
    t = pd.to_datetime(time_str, format="%Y-%m-%d %H:%M")
    try:
        return df.index.get_loc(t)
    except Exception:
        idx = df.index.get_indexer([t], method="nearest")[0]
        return idx


def simulate_ai_local(
    df: pd.DataFrame,
    signals: List[Dict[str, Any]],
    min_conf: float = 0.6,
    ema_field: str = "ema_50",
    momentum_mode: str = "ema_only",
    min_atr: float = 0.0,
    max_hold: int = 200,
):
    rng = random.Random(123)
    trades = []

    for s in signals:
        sig_time = s["time"]
        sig_type = s["signal"]
        confidence = max(0.0, min(1.0, rng.betavariate(2, 1.5)))
        if confidence < min_conf:
            continue
        i = idx_of_time(df, sig_time)
        entry_price = float(df["close"].iloc[i])
        atr = float(df["atr"].iloc[i]) if pd.notna(df["atr"].iloc[i]) else None
        if not atr or atr <= 0:
            continue
        if atr < min_atr:
            continue
        macd_hist = float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else 0.0
        ema_val = (
            float(df.get(ema_field).iloc[i])
            if ema_field in df.columns and pd.notna(df.get(ema_field).iloc[i])
            else None
        )

        # momentum checks (we focus on ema_only for dense search but keep logic)
        if momentum_mode == "ema_only":
            if ema_val is None:
                continue
            if sig_type == "BUY" and not (entry_price > ema_val):
                continue
            if sig_type == "SELL" and not (entry_price < ema_val):
                continue
        elif momentum_mode == "macd_only":
            if sig_type == "BUY" and not (macd_hist > 0):
                continue
            if sig_type == "SELL" and not (macd_hist < 0):
                continue
        else:  # ema_and_macd
            if ema_val is None:
                continue
            if sig_type == "BUY" and not (macd_hist > 0 and entry_price > ema_val):
                continue
            if sig_type == "SELL" and not (macd_hist < 0 and entry_price < ema_val):
                continue

        stop = entry_price - 3 * atr if sig_type == "BUY" else entry_price + 3 * atr
        pnl = 0.0
        for j in range(i + 1, min(i + max_hold, len(df))):
            close = float(df["close"].iloc[j])
            if sig_type == "BUY":
                if close <= stop:
                    pnl = stop - entry_price
                    break
                if float(df["rsi"].iloc[j]) > 70:
                    pnl = close - entry_price
                    break
            else:
                if close >= stop:
                    pnl = entry_price - stop
                    break
                if float(df["rsi"].iloc[j]) < 30:
                    pnl = entry_price - close
                    break
        if pnl == 0.0:
            last = float(df["close"].iloc[min(i + max_hold, len(df) - 1)])
            pnl = (last - entry_price) if sig_type == "BUY" else (entry_price - last)

        trades.append(
            {
                "entry_time": sig_time,
                "pnl": pnl,
                "confidence": confidence,
                "atr": atr,
                "macd_hist": macd_hist,
                "ema": ema_val,
            }
        )

    total = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    gross = sum(t["pnl"] for t in trades)
    final_equity = 10000.0 + gross
    return {
        "min_con": min_conf,
        "ema_field": ema_field,
        "momentum": momentum_mode,
        "min_atr": min_atr,
        "max_hold": max_hold,
        "total": total,
        "wins": wins,
        "final_equity": final_equity,
        "expectancy": (gross / total) if total else 0,
    }


def main():
    engine = BacktestEngine(symbol="SOLUSDT", interval="15m", days=15)
    engine.download_data()
    engine.calculate_indicators()
    analysis = engine.analyze_signals()
    signals = analysis.get("signals", [])

    # dense grid
    min_confs = [round(0.75 + i * 0.02, 2) for i in range(6)]  # 0.75..0.85
    max_holds = list(range(150, 301, 10))
    ema_field = "ema_50"
    momentum_mode = "ema_only"
    min_atr = 0.0

    results = []
    for mc in min_confs:
        for mh in max_holds:
            r = simulate_ai_local(
                engine.df,
                signals,
                min_conf=mc,
                ema_field=ema_field,
                momentum_mode=momentum_mode,
                min_atr=min_atr,
                max_hold=mh,
            )
            results.append(r)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join("reports", f"ai_param_search_dense_{ts}.csv")
    os.makedirs("reports", exist_ok=True)
    df = pd.DataFrame(results)
    df.to_csv(out, index=False)

    summary = df.sort_values(["final_equity", "expectancy"], ascending=False).head(30)
    summary_out = os.path.join("reports", f"ai_param_search_dense_summary_{ts}.csv")
    summary.to_csv(summary_out, index=False)

    print("Wrote dense grid results to", out)
    print("Wrote dense summary to", summary_out)


if __name__ == "__main__":
    main()
