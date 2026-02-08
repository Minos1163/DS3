import os
import glob
import importlib.util
import pandas as pd
from pathlib import Path


def import_backtester_class():
    script_path = os.path.join(os.path.dirname(__file__), "..", "tools", "backtest", "backtest_15m30d_v2.py")
    script_path = os.path.abspath(script_path)
    spec = importlib.util.spec_from_file_location("backtest_15m30d_v2", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ConservativeBacktester


def compute_drawdown_from_trades(trades, initial=100.0):
    if trades.empty:
        return 0.0
    cum = initial + trades["pnl"].cumsum()
    peak = cum.cummax()
    drawdown = ((peak - cum) / peak).max() * 100
    return float(drawdown)


def run_on_file(filepath, best_cfg):
    ConservativeBacktester = import_backtester_class()
    # baseline
    bt_base = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
    df = bt_base.load_data(filepath)
    if df is None:
        return None
    df = bt_base.calculate_indicators(df)
    bt_base.run_backtest(df)
    bt_base.analyze_results()
    trades_base = pd.DataFrame(bt_base.trades)
    base_final = bt_base.capital
    base_pnl = trades_base["pnl"].sum() if not trades_base.empty else 0.0
    base_draw = compute_drawdown_from_trades(trades_base, bt_base.initial_capital)

    # best config
    bt_best = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
    bt_best.stop_loss_pct = best_cfg.get("stop_loss_pct", bt_best.stop_loss_pct)
    bt_best.position_percent = best_cfg.get("position_percent", bt_best.position_percent)
    # for best config use same data (already loaded above)
    # reload raw csv for bt_best to ensure fresh state
    raw_df = pd.read_csv(filepath, index_col="timestamp", parse_dates=True)
    df2 = bt_best.calculate_indicators(raw_df)
    bt_best.run_backtest(df2)
    bt_best.analyze_results()
    trades_best = pd.DataFrame(bt_best.trades)
    best_final = bt_best.capital
    best_pnl = trades_best["pnl"].sum() if not trades_best.empty else 0.0
    best_draw = compute_drawdown_from_trades(trades_best, bt_best.initial_capital)

    return {
        "file": filepath,
        "symbol": Path(filepath).stem,
        "baseline_final": float(base_final),
        "baseline_pnl": float(base_pnl),
        "baseline_trades": int(len(trades_base)),
        "baseline_drawdown_pct": base_draw,
        "best_final": float(best_final),
        "best_pnl": float(best_pnl),
        "best_trades": int(len(trades_best)),
        "best_drawdown_pct": best_draw,
    }


def main():
    # best config found earlier
    best_cfg = {"stop_loss_pct": 0.025, "position_percent": 0.40}

    files = sorted(glob.glob("data/*_15m_*.csv") + glob.glob("data/*_5m_*.csv"))
    # filter reasonable-size files
    cand = []
    for f in files:
        try:
            if "_5m_" in os.path.basename(f):
                # resample 5m -> 15m in-memory and save temp file
                df5 = pd.read_csv(f, index_col="timestamp", parse_dates=True)
                df15 = (
                    df5.resample("15T")
                    .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                    .dropna()
                )
                if len(df15) < 1000:
                    continue
                tmp = os.path.join("data", f"tmp_resampled_{Path(f).stem}_15m.csv")
                df15.to_csv(tmp, index_label="timestamp")
                cand.append(tmp)
            else:
                df = pd.read_csv(f, index_col="timestamp", parse_dates=True)
                if len(df) >= 1000:
                    cand.append(f)
        except Exception:
            continue

    if not cand:
        print("No suitable 15m data files found in data/.")
        return 1

    results = []
    Path("logs").mkdir(parents=True, exist_ok=True)
    for f in cand:
        print("Running OOS validation on", f)
        res = run_on_file(f, best_cfg)
        if res:
            results.append(res)

    df_res = pd.DataFrame(results)
    out = "logs/oos_validation_best_config.csv"
    df_res.to_csv(out, index=False)
    print("Saved OOS validation results to", out)
    print(df_res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
