import os
import argparse
import pandas as pd


def analyze(csv_path: str, min_trades_per_day: float, max_drawdown: float, min_win_rate: float, min_return: float):
    if not os.path.exists(csv_path):
        print(f"❌ 文件不存在: {csv_path}")
        return

    df = pd.read_csv(csv_path)
    df = df.sort_values(by="total_return_pct", ascending=False)

    filtered = df[
        (df["trades_per_day"] >= min_trades_per_day)
        & (df["max_drawdown_pct"] <= max_drawdown)
        & (df["win_rate_pct"] >= min_win_rate)
        & (df["total_return_pct"] >= min_return)
    ]

    print(f"总结果: {len(df)} | 满足条件: {len(filtered)}")
    if filtered.empty:
        print("❌ 无满足条件的组合")
        print("Top10 (按收益):")
        print(df.head(10).to_string(index=False))
        return

    print("✅ 满足条件的Top10:")
    print(filtered.head(10).to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--min-trades-per-day", type=float, default=10)
    parser.add_argument("--max-drawdown", type=float, default=20)
    parser.add_argument("--min-win-rate", type=float, default=60)
    parser.add_argument("--min-return", type=float, default=80)
    args = parser.parse_args()

    analyze(
        args.csv,
        min_trades_per_day=args.min_trades_per_day,
        max_drawdown=args.max_drawdown,
        min_win_rate=args.min_win_rate,
        min_return=args.min_return,
    )
