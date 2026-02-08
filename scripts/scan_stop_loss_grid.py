import os
import importlib.util
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path


def build_120d():
    os.makedirs("data", exist_ok=True)
    parts = []
    candidates = [
        "data/SOLUSDT_15m_60d.csv",
        "data/SOLUSDT_15m_30d.csv",
        "data/SOLUSDT_15m_15d.csv",
        "data/SOLUSDT_5m_15d.csv",
    ]

    for p in candidates:
        if not os.path.exists(p):
            continue
        df = __import__("pandas").read_csv(p, index_col="timestamp", parse_dates=True)
        if "5m" in os.path.basename(p):
            df = (
                df.resample("15T")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                .dropna()
            )
        parts.append(df)

    if not parts:
        print("未找到可用数据文件来构建 120 天样本（请提供数据）。")
        return None

    import pandas as pd

    df_all = pd.concat(parts)
    df_all = df_all[~df_all.index.duplicated(keep="first")]
    df_all = df_all.sort_index()
    out = "data/SOLUSDT_15m_120d.csv"
    df_all.to_csv(out, index_label="timestamp")
    print(f"已保存: {out}")
    return out


def import_backtester_class():
    script_path = os.path.join(os.path.dirname(__file__), "..", "tools", "backtest", "backtest_15m30d_v2.py")
    script_path = os.path.abspath(script_path)
    spec = importlib.util.spec_from_file_location("backtest_15m30d_v2", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ConservativeBacktester


def run_scan(stop_loss_list):
    build_120d()
    ConservativeBacktester = import_backtester_class()
    results = []
    data_file = "data/SOLUSDT_15m_120d.csv"
    for sl in stop_loss_list:
        print(f"Running backtest for stop_loss_pct={sl}")
        bt = ConservativeBacktester(initial_capital=100.0, leverage=10.0)
        bt.stop_loss_pct = sl
        df = bt.load_data(data_file)
        df = bt.calculate_indicators(df)
        bt.run_backtest(df)
        bt.analyze_results()

        trades_df = pd.DataFrame(bt.trades)
        total_pnl = trades_df["pnl"].sum() if not trades_df.empty else 0.0
        final_capital = bt.capital
        trades_count = len(bt.trades)
        # compute max drawdown from cumulative capital over trades
        if not trades_df.empty:
            cum = bt.initial_capital + trades_df["pnl"].cumsum()
            peak = cum.cummax()
            drawdown = ((peak - cum) / peak).max() * 100
        else:
            drawdown = 0.0

        results.append(
            {
                "stop_loss_pct": sl,
                "final_capital": final_capital,
                "total_pnl": total_pnl,
                "trades": trades_count,
                "max_drawdown_pct": drawdown,
            }
        )

    df_res = pd.DataFrame(results)
    Path("logs").mkdir(parents=True, exist_ok=True)
    out_csv = "logs/stop_loss_grid_results.csv"
    df_res.to_csv(out_csv, index=False)
    print("Saved grid results:", out_csv)

    # line plot
    plt.figure(figsize=(8, 4))
    plt.plot(df_res["stop_loss_pct"] * 100, df_res["final_capital"], marker="o")
    plt.xlabel("Stop loss (%)")
    plt.ylabel("Final capital (USDT)")
    plt.grid(True)
    out_line = "logs/stop_loss_grid_line.png"
    plt.savefig(out_line)
    print("Saved plot:", out_line)

    # heatmap (1 x N)

    heat = df_res["final_capital"].values.reshape(1, -1)
    plt.figure(figsize=(8, 2))
    plt.imshow(heat, aspect="auto", cmap="RdYlGn")
    plt.colorbar(label="Final capital")
    plt.xticks(range(len(df_res)), [f"{x * 100:.2f}%" for x in df_res["stop_loss_pct"]])
    plt.yticks([])
    out_heat = "logs/stop_loss_grid_heatmap.png"
    plt.savefig(out_heat)
    print("Saved heatmap:", out_heat)

    return df_res


def main():
    stop_losses = [0.006, 0.01, 0.015, 0.02, 0.03]
    res = run_scan(stop_losses)
    print("\nScan complete. Results:")
    print(res)


if __name__ == "__main__":
    main()
