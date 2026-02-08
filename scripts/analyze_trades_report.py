#!/usr/bin/env python3
import os
import glob
import pandas as pd
from datetime import datetime


def find_latest_trades(logs_dir):
    pattern = os.path.join(logs_dir, "dca_rotation_trades_*.csv")
    files = glob.glob(pattern)
    if not files:
        raise FileNotFoundError("No trades files found")
    files.sort()
    return files[-1]


def analyze(trades_csv, out_dir, initial_capital=100.0):
    df = pd.read_csv(trades_csv, parse_dates=["entry_time", "exit_time"])
    df = df.sort_values("exit_time").reset_index(drop=True)

    # equity timeline by exit_time
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
    equity = (df["pnl"].cumsum() + initial_capital).rename("equity")
    equity.index = df["exit_time"]

    equity_df = equity.reset_index().rename(columns={"index": "time"})

    # monthly pnl and monthly returns (pct change of equity at month boundaries)
    monthly = equity.resample("M").last().ffill()
    monthly_pnl = monthly.diff().fillna(monthly - initial_capital)
    monthly_pct = monthly.pct_change().fillna(monthly / initial_capital - 1.0)
    monthly_summary = pd.DataFrame(
        {"month_end": monthly.index, "equity_end": monthly.values, "pnl": monthly_pnl.values, "pct": monthly_pct.values}
    )

    # per-position stats
    total_trades = len(df)
    wins = (df["pnl"] > 0).sum()
    avg_pnl = df["pnl"].mean()
    median_pnl = df["pnl"].median()
    std_pnl = df["pnl"].std()
    winrate = wins / total_trades * 100 if total_trades > 0 else 0

    per_pos = {
        "total_trades": total_trades,
        "wins": wins,
        "winrate_pct": winrate,
        "avg_pnl": avg_pnl,
        "median_pnl": median_pnl,
        "std_pnl": std_pnl,
        "total_pnl": df["pnl"].sum(),
        "final_equity": equity.iloc[-1],
    }

    # drawdown timeline
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max * 100
    drawdown_df = drawdown.reset_index().rename(columns={"index": "time", 0: "drawdown_pct"})
    drawdown_df.columns = ["time", "drawdown_pct"]

    # save outputs
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(out_dir, exist_ok=True)
    equity_df.to_csv(os.path.join(out_dir, f"equity_timeline_{ts}.csv"), index=False)
    monthly_summary.to_csv(os.path.join(out_dir, f"monthly_summary_{ts}.csv"), index=False)
    pd.Series(per_pos).to_csv(os.path.join(out_dir, f"per_position_stats_{ts}.csv"))
    drawdown_df.to_csv(os.path.join(out_dir, f"drawdown_timeline_{ts}.csv"), index=False)

    # plots
    try:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
        ax[0].plot(equity.index, equity.values)
        ax[0].set_title("Equity Curve")
        ax[0].set_ylabel("Equity (USDT)")
        ax[1].plot(drawdown.index, drawdown.values)
        ax[1].set_title("Drawdown (%)")
        ax[1].set_ylabel("Drawdown %")
        plt.tight_layout()
        plot_path = os.path.join(out_dir, f"equity_drawdown_{ts}.png")
        plt.savefig(plot_path)
        plt.close(fig)
    except Exception:
        plot_path = None

    return {
        "trades_csv": trades_csv,
        "equity_csv": os.path.join(out_dir, f"equity_timeline_{ts}.csv"),
        "monthly_csv": os.path.join(out_dir, f"monthly_summary_{ts}.csv"),
        "per_pos_csv": os.path.join(out_dir, f"per_position_stats_{ts}.csv"),
        "drawdown_csv": os.path.join(out_dir, f"drawdown_timeline_{ts}.csv"),
        "plot": plot_path,
        "summary": per_pos,
    }


if __name__ == "__main__":
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    logs_dir = os.path.join(repo_root, "logs")
    latest = find_latest_trades(logs_dir)
    out = analyze(latest, logs_dir)
    print("分析完成。结果：")
    for k, v in out["summary"].items():
        print(f"{k}: {v}")
    print("输出文件：")
    print(out["equity_csv"])
    print(out["monthly_csv"])
    print(out["drawdown_csv"])
    if out["plot"]:
        print("图表:", out["plot"])
