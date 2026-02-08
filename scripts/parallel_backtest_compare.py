"""Parallel backtest comparison: ATR-risk sizing vs Random vs Fixed-position baseline

Usage:
  .venv\Scripts\python.exe scripts/parallel_backtest_compare.py

Outputs saved to logs/: comparison CSV and equity-curve PNG
"""

from __future__ import annotations

from typing import Any, Dict, List

import matplotlib

import matplotlib.pyplot as plt

import pandas as pd

import sys

import os

from src.backtest import BacktestEngine

import random
from datetime import datetime

matplotlib.use("Agg")


# ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# Filtering / risk tuning defaults
MAX_HOLD_CANDLES = 200  # max candles to hold a trade (defaults to 200)
# tuned defaults from dense grid search
AI_MIN_CONF_DEFAULT = 0.8
MIN_ATR_PCT = 0.0  # require ATR >= 0% of price at entry (min_atr handled in grid scripts)


def idx_of_time(df: pd.DataFrame, time_str: str):
    t = pd.to_datetime(time_str, format="%Y-%m-%d %H:%M")
    try:
        return df.index.get_loc(t)
    except Exception:
        # try nearest
        idx = df.index.get_indexer([t], method="nearest")[0]
        return idx


def simulate_atr_risk(
    df: pd.DataFrame,
    signals: List[Dict[str, Any]],
    initial: float = 10000.0,
    risk_pct: float = 0.25,
    atr_mult: float = 3.0,
):
    equity = initial
    equity_curve = [equity]
    trades = []

    for s in signals:
        sig_time = s["time"]
        sig_type = s["signal"]
        i = idx_of_time(df, sig_time)
        entry_price = float(df["close"].iloc[i])
        atr = float(df["atr"].iloc[i]) if pd.notna(df["atr"].iloc[i]) else None
        if not atr or atr <= 0:
            continue
        # require minimum ATR relative to price to avoid tiny-stop trades
        if atr / entry_price < MIN_ATR_PCT:
            continue

        if sig_type == "BUY":
            stop = entry_price - atr_mult * atr
            if stop >= entry_price:
                continue
            risk_amount = equity * (risk_pct / 100)
            risk_per_unit = entry_price - stop
            qty = risk_amount / risk_per_unit
            # find exit: rsi>70 or stop hit
            exit_price = None
            for j in range(i + 1, len(df)):
                close = float(df["close"].iloc[j])
                if close <= stop:
                    exit_price = stop
                    exit_time = df.index[j]
                    break
                if float(df["rsi"].iloc[j]) > 70:
                    exit_price = close
                    exit_time = df.index[j]
                    break
                if j - i > MAX_HOLD_CANDLES:
                    exit_price = close
                    exit_time = df.index[j]
                    break
            if exit_price is None:
                exit_price = float(df["close"].iloc[-1])
                exit_time = df.index[-1]

            pnl = (exit_price - entry_price) * qty
            equity += pnl
            trades.append(
                {
                    "entry_time": sig_time,
                    "exit_time": exit_time,
                    "pnl": pnl,
                    "entry_price": entry_price,
                    "atr": atr,
                    "rsi": float(df["rsi"].iloc[i]) if pd.notna(df["rsi"].iloc[i]) else None,
                    "macd_hist": float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else None,
                    "ema_20": float(df.get("ema_20").iloc[i])
                    if "ema_20" in df.columns and pd.notna(df.get("ema_20").iloc[i])
                    else None,
                    "ema_50": float(df.get("ema_50").iloc[i])
                    if "ema_50" in df.columns and pd.notna(df.get("ema_50").iloc[i])
                    else None,
                    "volume": float(df["volume"].iloc[i]) if pd.notna(df["volume"].iloc[i]) else None,
                }
            )
            equity_curve.append(equity)

        elif sig_type == "SELL":
            stop = entry_price + atr_mult * atr
            risk_amount = equity * (risk_pct / 100)
            risk_per_unit = stop - entry_price
            if risk_per_unit <= 0:
                continue
            qty = risk_amount / risk_per_unit
            exit_price = None
            for j in range(i + 1, len(df)):
                close = float(df["close"].iloc[j])
                if close >= stop:
                    exit_price = stop
                    exit_time = df.index[j]
                    break
                if float(df["rsi"].iloc[j]) < 30:
                    exit_price = close
                    exit_time = df.index[j]
                    break
                if j - i > MAX_HOLD_CANDLES:
                    exit_price = close
                    exit_time = df.index[j]
                    break
            if exit_price is None:
                exit_price = float(df["close"].iloc[-1])
                exit_time = df.index[-1]

            pnl = (entry_price - exit_price) * qty
            equity += pnl
            trades.append(
                {
                    "entry_time": sig_time,
                    "exit_time": exit_time,
                    "pnl": pnl,
                    "entry_price": entry_price,
                    "atr": atr,
                    "rsi": float(df["rsi"].iloc[i]) if pd.notna(df["rsi"].iloc[i]) else None,
                    "macd_hist": float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else None,
                    "ema_20": float(df.get("ema_20").iloc[i])
                    if "ema_20" in df.columns and pd.notna(df.get("ema_20").iloc[i])
                    else None,
                    "ema_50": float(df.get("ema_50").iloc[i])
                    if "ema_50" in df.columns and pd.notna(df.get("ema_50").iloc[i])
                    else None,
                    "volume": float(df["volume"].iloc[i]) if pd.notna(df["volume"].iloc[i]) else None,
                }
            )
            equity_curve.append(equity)

    return equity_curve, trades


def simulate_random(
    df: pd.DataFrame, n_entries: int, initial: float = 10000.0, risk_pct: float = 0.25, atr_mult: float = 3.0
):
    equity = initial
    equity_curve = [equity]
    trades = []
    rng = random.Random(42)
    possible_idx = list(range(60, len(df) - 2))
    picks = rng.sample(possible_idx, min(n_entries, len(possible_idx)))

    for i in sorted(picks):
        entry_price = float(df["close"].iloc[i])
        atr = float(df["atr"].iloc[i]) if pd.notna(df["atr"].iloc[i]) else None
        if not atr or atr <= 0:
            continue
        if atr / entry_price < MIN_ATR_PCT:
            continue
        stop = entry_price - atr_mult * atr
        if stop >= entry_price:
            continue
        risk_amount = equity * (risk_pct / 100)
        risk_per_unit = entry_price - stop
        qty = risk_amount / risk_per_unit

        exit_price = None
        for j in range(i + 1, min(i + MAX_HOLD_CANDLES, len(df))):
            close = float(df["close"].iloc[j])
            if close <= stop:
                exit_price = stop
                exit_time = df.index[j]
                break
        if exit_price is None:
            exit_price = float(df["close"].iloc[min(i + MAX_HOLD_CANDLES, len(df) - 1)])
            exit_time = df.index[min(i + MAX_HOLD_CANDLES, len(df) - 1)]

        pnl = (exit_price - entry_price) * qty
        equity += pnl
        trades.append(
            {
                "entry_time": df.index[i],
                "exit_time": exit_time,
                "pnl": pnl,
                "entry_price": entry_price,
                "atr": atr,
                "rsi": float(df["rsi"].iloc[i]) if pd.notna(df["rsi"].iloc[i]) else None,
                "macd_hist": float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else None,
                "ema_20": float(df.get("ema_20").iloc[i])
                if "ema_20" in df.columns and pd.notna(df.get("ema_20").iloc[i])
                else None,
                "ema_50": float(df.get("ema_50").iloc[i])
                if "ema_50" in df.columns and pd.notna(df.get("ema_50").iloc[i])
                else None,
                "volume": float(df["volume"].iloc[i]) if pd.notna(df["volume"].iloc[i]) else None,
            }
        )
        equity_curve.append(equity)

    return equity_curve, trades


def simulate_fixed_position(
    df: pd.DataFrame, signals: List[Dict[str, Any]], initial: float = 10000.0, position_pct: float = 10.0
):
    equity = initial
    equity_curve = [equity]
    trades = []

    for s in signals:
        sig_time = s["time"]
        sig_type = s["signal"]
        i = idx_of_time(df, sig_time)
        entry_price = float(df["close"].iloc[i])
        position_value = equity * (position_pct / 100)
        qty = position_value / entry_price

        # exit on opposite signal or after max hold candles
        exit_price = None
        for j in range(i + 1, len(df)):
            close = float(df["close"].iloc[j])
            if sig_type == "BUY" and float(df["rsi"].iloc[j]) > 70:
                exit_price = close
                exit_time = df.index[j]
                break
            if sig_type == "SELL" and float(df["rsi"].iloc[j]) < 30:
                exit_price = close
                exit_time = df.index[j]
                break
            if j - i > MAX_HOLD_CANDLES:
                exit_price = close
                exit_time = df.index[j]
                break
        if exit_price is None:
            exit_price = float(df["close"].iloc[-1])
            exit_time = df.index[-1]

        if sig_type == "BUY":
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty

        equity += pnl
        trades.append(
            {
                "entry_time": sig_time,
                "exit_time": exit_time,
                "pnl": pnl,
                "entry_price": entry_price,
                "atr": float(df["atr"].iloc[i]) if pd.notna(df["atr"].iloc[i]) else None,
                "rsi": float(df["rsi"].iloc[i]) if pd.notna(df["rsi"].iloc[i]) else None,
                "macd_hist": float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else None,
                "ema_20": float(df.get("ema_20").iloc[i])
                if "ema_20" in df.columns and pd.notna(df.get("ema_20").iloc[i])
                else None,
                "ema_50": float(df.get("ema_50").iloc[i])
                if "ema_50" in df.columns and pd.notna(df.get("ema_50").iloc[i])
                else None,
                "volume": float(df["volume"].iloc[i]) if pd.notna(df["volume"].iloc[i]) else None,
            }
        )
        equity_curve.append(equity)

    return equity_curve, trades


def summarize_trades(trades: List[Dict[str, Any]], equity_curve: List[float], initial: float):
    total = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = total - wins
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    expectancy = sum(t["pnl"] for t in trades) / total if total else 0
    final_equity = equity_curve[-1] if equity_curve else initial
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total * 100) if total else 0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "expectancy": expectancy,
        "final_equity": final_equity,
    }


def simulate_ai(
    df: pd.DataFrame,
    signals: List[Dict[str, Any]],
    initial: float = 10000.0,
    base_risk_pct: float = 0.5,
    atr_mult: float = 3.0,
    min_conf: float = AI_MIN_CONF_DEFAULT,
    ema_field: str = "ema_50",
    momentum_mode: str = "ema_and_macd",
):
    """Simulate AI decisions by assigning a confidence to each signal and sizing risk by confidence.
    - base_risk_pct: percentage of equity at confidence==1.0 (e.g., 0.5 means 0.5% per trade at conf=1.0)
    - final risk_pct_per_trade = base_risk_pct * confidence
    """
    rng = random.Random(123)
    equity = initial
    equity_curve = [equity]
    trades = []

    for s in signals:
        sig_time = s["time"]
        sig_type = s["signal"]
        # simulate confidence biased toward medium-high
        confidence = max(0.0, min(1.0, rng.betavariate(2, 1.5)))
        if confidence < min_conf:
            continue

        # risk per trade as percent
        risk_pct = base_risk_pct * confidence

        i = idx_of_time(df, sig_time)
        entry_price = float(df["close"].iloc[i])
        atr = float(df["atr"].iloc[i]) if pd.notna(df["atr"].iloc[i]) else None
        if not atr or atr <= 0:
            continue

        macd_hist = float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else 0.0
        ema_val = (
            float(df.get(ema_field).iloc[i])
            if ema_field in df.columns and pd.notna(df.get(ema_field).iloc[i])
            else None
        )

        # require minimum ATR relative to price to avoid tiny-stop trades
        if atr / entry_price < MIN_ATR_PCT:
            continue

        # momentum filtering modes
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
        else:
            if ema_val is None:
                continue
            if sig_type == "BUY" and not (macd_hist > 0 and entry_price > ema_val):
                continue
            if sig_type == "SELL" and not (macd_hist < 0 and entry_price < ema_val):
                continue

        if sig_type == "BUY":
            stop = entry_price - atr_mult * atr
            if stop >= entry_price:
                continue
            risk_amount = equity * (risk_pct / 100)
            risk_per_unit = entry_price - stop
            if risk_per_unit <= 0:
                continue
            qty = risk_amount / risk_per_unit

            exit_price = None
            for j in range(i + 1, len(df)):
                close = float(df["close"].iloc[j])
                if close <= stop:
                    exit_price = stop
                    exit_time = df.index[j]
                    break
                if float(df["rsi"].iloc[j]) > 70:
                    exit_price = close
                    exit_time = df.index[j]
                    break
                if j - i > MAX_HOLD_CANDLES:
                    exit_price = close
                    exit_time = df.index[j]
                    break
            if exit_price is None:
                exit_price = float(df["close"].iloc[-1])
                exit_time = df.index[-1]

            pnl = (exit_price - entry_price) * qty
            equity += pnl
            trades.append(
                {
                    "entry_time": sig_time,
                    "exit_time": exit_time,
                    "pnl": pnl,
                    "confidence": confidence,
                    "entry_price": entry_price,
                    "atr": atr,
                    "rsi": float(df["rsi"].iloc[i]) if pd.notna(df["rsi"].iloc[i]) else None,
                    "macd_hist": float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else None,
                    "ema_20": float(df.get("ema_20").iloc[i])
                    if "ema_20" in df.columns and pd.notna(df.get("ema_20").iloc[i])
                    else None,
                    "ema_50": float(df.get("ema_50").iloc[i])
                    if "ema_50" in df.columns and pd.notna(df.get("ema_50").iloc[i])
                    else None,
                    "chosen_ema": float(df.get(ema_field).iloc[i])
                    if ema_field in df.columns and pd.notna(df.get(ema_field).iloc[i])
                    else None,
                    "volume": float(df["volume"].iloc[i]) if pd.notna(df["volume"].iloc[i]) else None,
                }
            )
            equity_curve.append(equity)

        elif sig_type == "SELL":
            stop = entry_price + atr_mult * atr
            risk_amount = equity * (risk_pct / 100)
            risk_per_unit = stop - entry_price
            if risk_per_unit <= 0:
                continue
            qty = risk_amount / risk_per_unit
            exit_price = None
            for j in range(i + 1, len(df)):
                close = float(df["close"].iloc[j])
                if close >= stop:
                    exit_price = stop
                    exit_time = df.index[j]
                    break
                if float(df["rsi"].iloc[j]) < 30:
                    exit_price = close
                    exit_time = df.index[j]
                    break
                if j - i > MAX_HOLD_CANDLES:
                    exit_price = close
                    exit_time = df.index[j]
                    break
            if exit_price is None:
                exit_price = float(df["close"].iloc[-1])
                exit_time = df.index[-1]

            pnl = (entry_price - exit_price) * qty
            equity += pnl
            trades.append(
                {
                    "entry_time": sig_time,
                    "exit_time": exit_time,
                    "pnl": pnl,
                    "confidence": confidence,
                    "entry_price": entry_price,
                    "atr": atr,
                    "rsi": float(df["rsi"].iloc[i]) if pd.notna(df["rsi"].iloc[i]) else None,
                    "macd_hist": float(df["macd_hist"].iloc[i]) if pd.notna(df["macd_hist"].iloc[i]) else None,
                    "ema_20": float(df.get("ema_20").iloc[i])
                    if "ema_20" in df.columns and pd.notna(df.get("ema_20").iloc[i])
                    else None,
                    "ema_50": float(df.get("ema_50").iloc[i])
                    if "ema_50" in df.columns and pd.notna(df.get("ema_50").iloc[i])
                    else None,
                    "chosen_ema": float(df.get(ema_field).iloc[i])
                    if ema_field in df.columns and pd.notna(df.get(ema_field).iloc[i])
                    else None,
                    "volume": float(df["volume"].iloc[i]) if pd.notna(df["volume"].iloc[i]) else None,
                }
            )
            equity_curve.append(equity)

    return equity_curve, trades


def run_for_symbol(
    symbol: str,
    interval: str = "15m",
    days: int = 15,
    min_conf: float = AI_MIN_CONF_DEFAULT,
    max_hold: int = MAX_HOLD_CANDLES,
    ema_field: str = "ema_50",
    momentum_mode: str = "ema_only",
):
    """Run the parallel backtest comparison for a single symbol and return report dict and paths."""
    global MAX_HOLD_CANDLES
    old_max_hold = MAX_HOLD_CANDLES
    MAX_HOLD_CANDLES = max_hold

    engine = BacktestEngine(symbol=symbol, interval=interval, days=days)
    engine.download_data()
    engine.calculate_indicators()
    analysis = engine.analyze_signals()

    signals = analysis.get("signals", [])
    initial = 10000.0

    atr_eq, atr_trades = simulate_atr_risk(engine.df, signals, initial=initial, risk_pct=0.25, atr_mult=3.0)
    rnd_eq, rnd_trades = simulate_random(
        engine.df, n_entries=len(signals), initial=initial, risk_pct=0.25, atr_mult=3.0
    )
    fix_eq, fix_trades = simulate_fixed_position(engine.df, signals, initial=initial, position_pct=10.0)
    ai_eq, ai_trades = simulate_ai(
        engine.df,
        signals,
        initial=initial,
        base_risk_pct=0.5,
        atr_mult=3.0,
        min_conf=min_conf,
        ema_field=ema_field,
        momentum_mode=momentum_mode,
    )

    now_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # save equity CSV
    df_out = pd.DataFrame(
        {
            "atr_equity": atr_eq + [None] * (max(len(rnd_eq), len(fix_eq)) - len(atr_eq)),
            "random_equity": rnd_eq + [None] * (max(len(atr_eq), len(fix_eq)) - len(rnd_eq)),
            "fixed_equity": fix_eq + [None] * (max(len(atr_eq), len(rnd_eq)) - len(fix_eq)),
            "ai_equity": ai_eq + [None] * (max(len(atr_eq), len(rnd_eq), len(fix_eq)) - len(ai_eq)),
        }
    )
    out_csv = os.path.join(logs_dir, f"parallel_backtest_{symbol}_{now_ts}.csv")
    df_out.to_csv(out_csv, index=False)
    # save per-strategy trades
    trades_dir = os.path.join(logs_dir, f"parallel_trades_{symbol}_{now_ts}")
    os.makedirs(trades_dir, exist_ok=True)

    def save_trades(trades, name):
        import pandas as _pd

        if not trades:
            return None
        rows = []
        for t in trades:
            rows.append({k: (t.get(k) if not hasattr(t.get(k), "strftime") else t.get(k).isoformat()) for k in t})
        df_t = _pd.DataFrame(rows)
        path = os.path.join(trades_dir, f"trades_{name}.csv")
        df_t.to_csv(path, index=False)
        return path

    _p_atr = save_trades(atr_trades, "atr")
    _p_rnd = save_trades(rnd_trades, "random")
    _p_fix = save_trades(fix_trades, "fixed")
    _p_ai = save_trades(ai_trades, "ai")

    # plot
    plt.figure(figsize=(8, 4))
    plt.plot(atr_eq, label="ATR risk sizing")
    plt.plot(rnd_eq, label="Random entries")
    plt.plot(fix_eq, label="Fixed 10% pos")
    plt.plot(ai_eq, label="Simulated AI")
    plt.legend()
    plt.title(f"Parallel backtest equity curves ({symbol})")
    plt.xlabel("Trade #")
    plt.ylabel("Equity")
    out_png = os.path.join(logs_dir, f"parallel_backtest_{symbol}_{now_ts}.png")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(out_png, dpi=150)

    # Detailed plots: drawdown and pnl distribution
    import numpy as _np

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    # equity curves
    axes[0, 0].plot(atr_eq, label="ATR")
    axes[0, 0].plot(rnd_eq, label="Random")
    axes[0, 0].plot(fix_eq, label="Fixed")
    axes[0, 0].plot(ai_eq, label="AI")
    axes[0, 0].set_title("Equity Curves")
    axes[0, 0].legend()

    # drawdowns

    def drawdown(eq):
        a = _np.array(eq)
        peak = _np.maximum.accumulate(a)
        dd = (a - peak) / peak
        return dd

    axes[0, 1].plot(drawdown(atr_eq), label="ATR")
    axes[0, 1].plot(drawdown(rnd_eq), label="Random")
    axes[0, 1].plot(drawdown(fix_eq), label="Fixed")
    axes[0, 1].plot(drawdown(ai_eq), label="AI")
    axes[0, 1].set_title("Drawdowns")
    axes[0, 1].legend()

    # pnl histograms (use trades)

    def pnl_list(trades):
        return [t["pnl"] for t in trades] if trades else []

    axes[1, 0].hist(pnl_list(atr_trades), bins=50, alpha=0.7, label="ATR")
    axes[1, 0].hist(pnl_list(rnd_trades), bins=50, alpha=0.5, label="Random")
    axes[1, 0].set_title("PnL Distribution (ATR vs Random)")
    axes[1, 0].legend()

    axes[1, 1].hist(pnl_list(fix_trades), bins=50, alpha=0.7, label="Fixed")
    axes[1, 1].hist(pnl_list(ai_trades), bins=50, alpha=0.5, label="AI")
    axes[1, 1].set_title("PnL Distribution (Fixed vs AI)")
    axes[1, 1].legend()

    out_detail_png = os.path.join(logs_dir, f"parallel_backtest_details_{symbol}_{now_ts}.png")
    fig.tight_layout()
    fig.savefig(out_detail_png, dpi=150)

    # summaries
    s_atr = summarize_trades(atr_trades, atr_eq, initial)
    s_rnd = summarize_trades(rnd_trades, rnd_eq, initial)
    s_fix = summarize_trades(fix_trades, fix_eq, initial)
    s_ai = summarize_trades(ai_trades, ai_eq, initial)

    report = {
        "atr": s_atr,
        "random": s_rnd,
        "fixed": s_fix,
        "ai": s_ai,
        "csv": out_csv,
        "png": out_png,
    }

    # write summary report to reports/
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, f"parallel_report_{symbol}_{now_ts}.txt")
    try:
        import json

        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write(f"Parallel backtest detailed report for {symbol}\n")
            rf.write(json.dumps(report, indent=2, ensure_ascii=False))
    except Exception:
        pass

    # restore global
    MAX_HOLD_CANDLES = old_max_hold
    return report, out_csv, out_png, out_detail_png, trades_dir, report_path


def run_batch(
    symbols: List[str],
    interval: str = "15m",
    days: int = 15,
    min_conf: float = AI_MIN_CONF_DEFAULT,
    max_hold: int = MAX_HOLD_CANDLES,
    ema_field: str = "ema_50",
    momentum_mode: str = "ema_only",
):
    """Run the comparison for multiple symbols and save a combined summary CSV."""
    all_reports = []
    for s in symbols:
        print(f"Running for {s} (interval={interval}, days={days})...")
        rep, csvp, pngp, detpng, trades_dir, rpt = run_for_symbol(
            s,
            interval=interval,
            days=days,
            min_conf=min_conf,
            max_hold=max_hold,
            ema_field=ema_field,
            momentum_mode=momentum_mode,
        )
        rec = {"symbol": s, "report_path": rpt, "csv": csvp, "png": pngp}
        rec.update(rep.get("ai", {}))
        all_reports.append(rec)

    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "reports",
        f"parallel_batch_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    )
    pd.DataFrame(all_reports).to_csv(out, index=False)
    print("Wrote batch summary to", out)
    return out


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", action="store_true", help="run batch across multiple symbols")
    parser.add_argument("--symbols", type=str, default="SOLUSDT", help="comma-separated symbol list for batch")
    parser.add_argument("--interval", type=str, default="15m")
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--min_con", type=float, default=AI_MIN_CONF_DEFAULT)
    parser.add_argument("--max_hold", type=int, default=MAX_HOLD_CANDLES)
    args = parser.parse_args()

    if args.batch:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
        run_batch(symbols, interval=args.interval, days=args.days, min_conf=args.min_conf, max_hold=args.max_hold)
        return

    # default single run for backwards compatibility
    rep, out_csv, out_png, out_detail_png, trades_dir, report_path = run_for_symbol(
        "SOLUSDT", interval=args.interval, days=args.days, min_conf=args.min_conf, max_hold=args.max_hold
    )
    print("Single run complete.")

    now_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # load equity CSV produced by run_for_symbol
    try:
        df_out = pd.read_csv(out_csv)
    except Exception:
        # fall back to empty frame if not present
        df_out = pd.DataFrame()

    # extract equity lists (drop NaNs)
    atr_eq = df_out.get("atr_equity")
    rnd_eq = df_out.get("random_equity")
    fix_eq = df_out.get("fixed_equity")
    ai_eq = df_out.get("ai_equity")
    if atr_eq is not None:
        atr_eq = [x for x in atr_eq.tolist() if pd.notna(x)]
    else:
        atr_eq = []
    if rnd_eq is not None:
        rnd_eq = [x for x in rnd_eq.tolist() if pd.notna(x)]
    else:
        rnd_eq = []
    if fix_eq is not None:
        fix_eq = [x for x in fix_eq.tolist() if pd.notna(x)]
    else:
        fix_eq = []
    if ai_eq is not None:
        ai_eq = [x for x in ai_eq.tolist() if pd.notna(x)]
    else:
        ai_eq = []

    # load trades if saved
    def load_trades_csv(path):
        if not path or not os.path.exists(path):
            return []
        try:
            df_t = pd.read_csv(path)
            return df_t.to_dict(orient="records")
        except Exception:
            return []

    atr_trades = load_trades_csv(os.path.join(trades_dir, "trades_atr.csv"))
    rnd_trades = load_trades_csv(os.path.join(trades_dir, "trades_random.csv"))
    fix_trades = load_trades_csv(os.path.join(trades_dir, "trades_fixed.csv"))
    ai_trades = load_trades_csv(os.path.join(trades_dir, "trades_ai.csv"))
    # save per-strategy trades
    trades_dir = os.path.join(logs_dir, f"parallel_trades_{now_ts}")
    os.makedirs(trades_dir, exist_ok=True)

    def save_trades(trades, name):
        import pandas as _pd

        if not trades:
            return None
        rows = []
        for t in trades:
            rows.append({k: (t.get(k) if not hasattr(t.get(k), "strftime") else t.get(k).isoformat()) for k in t})
        df_t = _pd.DataFrame(rows)
        path = os.path.join(trades_dir, f"trades_{name}.csv")
        df_t.to_csv(path, index=False)
        return path

    _p_atr = save_trades(atr_trades, "atr")
    _p_rnd = save_trades(rnd_trades, "random")
    _p_fix = save_trades(fix_trades, "fixed")
    _p_ai = save_trades(ai_trades, "ai")

    # plot
    plt.figure(figsize=(8, 4))
    plt.plot(atr_eq, label="ATR risk sizing")
    plt.plot(rnd_eq, label="Random entries")
    plt.plot(fix_eq, label="Fixed 10% pos")
    plt.plot(ai_eq, label="Simulated AI")
    plt.legend()
    plt.title("Parallel backtest equity curves")
    plt.xlabel("Trade #")
    plt.ylabel("Equity")
    out_png = os.path.join(logs_dir, f"parallel_backtest_{now_ts}.png")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(out_png, dpi=150)

    # Detailed plots: drawdown and pnl distribution
    import numpy as _np

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    # equity curves
    axes[0, 0].plot(atr_eq, label="ATR")
    axes[0, 0].plot(rnd_eq, label="Random")
    axes[0, 0].plot(fix_eq, label="Fixed")
    axes[0, 0].plot(ai_eq, label="AI")
    axes[0, 0].set_title("Equity Curves")
    axes[0, 0].legend()

    # drawdowns

    def drawdown(eq):
        a = _np.array(eq)
        peak = _np.maximum.accumulate(a)
        dd = (a - peak) / peak
        return dd

    axes[0, 1].plot(drawdown(atr_eq), label="ATR")
    axes[0, 1].plot(drawdown(rnd_eq), label="Random")
    axes[0, 1].plot(drawdown(fix_eq), label="Fixed")
    axes[0, 1].plot(drawdown(ai_eq), label="AI")
    axes[0, 1].set_title("Drawdowns")
    axes[0, 1].legend()

    # pnl histograms (use trades)

    def pnl_list(trades):
        return [t["pnl"] for t in trades] if trades else []

    axes[1, 0].hist(pnl_list(atr_trades), bins=50, alpha=0.7, label="ATR")
    axes[1, 0].hist(pnl_list(rnd_trades), bins=50, alpha=0.5, label="Random")
    axes[1, 0].set_title("PnL Distribution (ATR vs Random)")
    axes[1, 0].legend()

    axes[1, 1].hist(pnl_list(fix_trades), bins=50, alpha=0.7, label="Fixed")
    axes[1, 1].hist(pnl_list(ai_trades), bins=50, alpha=0.5, label="AI")
    axes[1, 1].set_title("PnL Distribution (Fixed vs AI)")
    axes[1, 1].legend()

    out_detail_png = os.path.join(logs_dir, f"parallel_backtest_details_{now_ts}.png")
    fig.tight_layout()
    fig.savefig(out_detail_png, dpi=150)

    # (report and notebook generation moved below after summaries are computed)

    # summaries
    # default initial equity for single-run reporting (matches run_for_symbol default)
    initial = 10000.0

    s_atr = summarize_trades(atr_trades, atr_eq, initial)
    s_rnd = summarize_trades(rnd_trades, rnd_eq, initial)
    s_fix = summarize_trades(fix_trades, fix_eq, initial)

    s_ai = summarize_trades(ai_trades, ai_eq, initial)

    report = {
        "atr": s_atr,
        "random": s_rnd,
        "fixed": s_fix,
        "ai": s_ai,
        "csv": out_csv,
        "png": out_png,
    }

    # write summary report to reports/
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, f"parallel_report_{now_ts}.txt")
    try:
        import json

        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write("Parallel backtest detailed report\n")
            rf.write(json.dumps(report, indent=2, ensure_ascii=False))
    except Exception:
        pass

    # generate minimal notebook that loads the CSVs and plots (lightweight)
    try:
        nb_path = os.path.join(reports_dir, f"parallel_report_{now_ts}.ipynb")
        nb = {
            "cells": [
                {
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": [
                        f"# Parallel Backtest Report ({now_ts})\n",
                        "This notebook loads CSVs and displays equity curves and PnL distributions.",
                    ],
                },
                {
                    "cell_type": "code",
                    "metadata": {},
                    "source": [
                        "import pandas as pd\n",
                        "import matplotlib.pyplot as plt\n",
                        f"df = pd.read_csv(r'{out_csv}')\n",
                        "df.plot(figsize=(10,4))\n",
                        "plt.show()\n",
                    ],
                },
            ],
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        import json as _json

        with open(nb_path, "w", encoding="utf-8") as f:
            _json.dump(nb, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    print("Parallel backtest complete. Summaries:")
    print(report)

    print("Saved:")
    print("  equity csv:", out_csv)
    print("  equity png:", out_png)
    print("  details png:", out_detail_png)
    print("  trades dir:", trades_dir)
    print("  report:", report_path)


if __name__ == "__main__":
    main()
