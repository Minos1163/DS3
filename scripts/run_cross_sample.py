from tools.backtest_15m30d_v2 import ConservativeBacktester

import os
import sys
import csv
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_config(path="config/trading_config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_config_to_backtester(cfg, bt: ConservativeBacktester):
    strat = cfg.get("strategy", {})
    risk = cfg.get("risk", {})
    bt.position_percent = strat.get("position_percent", bt.position_percent)
    bt.leverage = strat.get("leverage", bt.leverage)
    bt.stop_loss_pct = strat.get("stop_loss_percent", bt.stop_loss_pct)
    bt.take_profit_pct = strat.get("take_profit_percent", bt.take_profit_pct)
    bt.rsi_oversold = strat.get("rsi_oversold", bt.rsi_oversold)
    bt.rsi_overbought = strat.get("rsi_overbought", bt.rsi_overbought)
    bt.short_rsi_overbought = strat.get("short_rsi_overbought", bt.short_rsi_overbought)
    bt.use_volume_quantile_filter = strat.get("use_volume_quantile_filter", bt.use_volume_quantile_filter)
    bt.volume_quantile = strat.get("volume_quantile", bt.volume_quantile)
    bt.short_volume_quantile = strat.get("short_volume_quantile", bt.short_volume_quantile)
    bt.volume_window = strat.get("volume_window", bt.volume_window)
    bt.use_time_filter = strat.get("use_time_filter", bt.use_time_filter)
    allowed = strat.get("allowed_hours_utc", None)
    if allowed is not None:
        bt.allowed_hours = set(allowed)
    bt.trailing_start_pct = strat.get("trailing_start_pct", bt.trailing_start_pct)
    bt.trailing_stop_pct = strat.get("trailing_stop_pct", bt.trailing_stop_pct)
    bt.max_hold_bars = strat.get("max_hold_bars", bt.max_hold_bars)
    bt.cooldown_bars = strat.get("cooldown_bars", bt.cooldown_bars)
    bt.max_consecutive_losses = strat.get("max_consecutive_losses", bt.max_consecutive_losses)
    # risk overrides
    # 如果配置为整数（如10），表示百分比；如果为小数（如0.2）按之前的兼容规则处理
    raw_max = risk.get("max_daily_loss_percent", bt.max_drawdown_percent)
    try:
        rv = float(raw_max)
    except Exception:
        rv = bt.max_drawdown_percent
    if rv > 1.0:
        bt.max_drawdown_percent = rv
    else:
        # rv <= 1.0，视为分数形式（0.2 => 20%）
        bt.max_drawdown_percent = rv * 100


def run_cross_samples():
    cfg = load_config()
    data_files = [
        "data/SOLUSDT_15m_60d.csv",
        "data/SOLUSDT_15m_30d.csv",
        "data/SOLUSDT_15m_15d.csv",
        "data/SOLUSDT_5m_15d.csv",
    ]

    out_csv = f"logs/cross_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    os.makedirs("logs", exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["data_file", "final_capital", "total_trades", "win_rate", "max_drawdown"])

        for dfpath in data_files:
            bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
            apply_config_to_backtester(cfg, bt)
            print(f"\n=== Running cross-sample on {dfpath} ===")
            df = bt.load_data(dfpath)
            if df is None:
                print(f"File {dfpath} missing, skipping")
                continue
            df = bt.calculate_indicators(df)
            bt.run_backtest(df)
            bt.analyze_results()
            total_trades = len(bt.trades)
            winning = len([t for t in bt.trades if t["pnl"] > 0])
            win_rate = (winning / total_trades * 100) if total_trades > 0 else 0
            # compute drawdown from trades
            import pandas as pd

            if bt.trades:
                dft = pd.DataFrame(bt.trades)
                dft["cumulative_capital"] = 100.0 + dft["pnl"].cumsum()
                dft["peak"] = dft["cumulative_capital"].cummax()
                dft["dd"] = (dft["peak"] - dft["cumulative_capital"]) / dft["peak"]
                max_dd = dft["dd"].max() * 100
            else:
                max_dd = 0
            writer.writerow([dfpath, f"{bt.capital:.2f}", total_trades, f"{win_rate:.2f}", f"{max_dd:.2f}"])
            f.flush()
    print(f"\nCross-sample results saved to {out_csv}")


if __name__ == "__main__":
    run_cross_samples()
