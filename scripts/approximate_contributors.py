import glob
import pandas as pd
from pathlib import Path
import argparse
import matplotlib.pyplot as plt


def analyze(date_str: str, out_prefix: str = None):
    # primary pattern expects date_str like '2026-02-08'
    files = sorted(glob.glob(f"logs/{date_str}/DCA_dashboard_{date_str}_*.csv"))
    # fallback: allow using a shorter prefix like '2026-02' (match any file containing the prefix)
    if not files:
        alt = sorted(glob.glob(f"logs/{date_str}/DCA_dashboard_*"))
        if alt:
            # filter those that contain the date_str after prefix
            files = [p for p in alt if f"DCA_dashboard_{date_str}" in Path(p).name]

    if not files:
        print("No files found for", date_str)
        return 1

    parts = []
    for f in files:
        df = pd.read_csv(f, parse_dates=["timestamp"])
        parts.append(df)

    df = pd.concat(parts, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp")
    equity = df.groupby("timestamp")["equity"].first().sort_index()
    df["pnl_percent"] = pd.to_numeric(df["pnl_percent"], errors="coerce")
    pivot = df.pivot_table(index="timestamp", columns="symbol", values="pnl_percent", aggfunc="first")
    pivot = pivot.reindex(equity.index)
    equity_diff = equity.diff()
    pnl_diff = pivot.diff()

    contrib = {}
    contrib_counts = {}
    for ts in equity_diff.index[1:]:
        ed = equity_diff.loc[ts]
        if pd.isna(ed) or ed >= 0:
            continue
        row = pnl_diff.loc[ts]
        neg = row[row < 0].dropna()
        if neg.empty:
            continue
        weights = neg.abs()
        s = weights.sum()
        if s == 0:
            continue
        for sym, w in weights.items():
            share = (w / s) * ed
            contrib[sym] = contrib.get(sym, 0.0) + share
            contrib_counts[sym] = contrib_counts.get(sym, 0) + 1

    if not contrib:
        print("No negative contributors found")
        return 0

    res = pd.DataFrame(
        [
            {"symbol": k, "contribution_usdt": v, "count_negative_events": contrib_counts.get(k, 0)}
            for k, v in contrib.items()
        ]
    )
    res = res.sort_values("contribution_usdt")
    res["abs_usdt"] = res["contribution_usdt"].abs()
    res["pct_of_total_loss"] = res["abs_usdt"] / res["abs_usdt"].sum() * 100

    out_dir = Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = (
        out_dir / f"top_negative_contributors_{date_str}.csv" if out_prefix is None else out_dir / f"{out_prefix}.csv"
    )
    res.to_csv(out_csv, index=False)
    print("Saved:", out_csv)

    # generate bar chart of top 10 contributors
    top = res.sort_values("abs_usdt", ascending=False).head(10)
    plt.figure(figsize=(8, 4))
    plt.bar(top["symbol"], top["abs_usdt"])
    plt.xticks(rotation=45, ha="right")
    plt.ylabel("Absolute USDT loss")
    plt.title(f"Top negative contributors {date_str}")
    plt.tight_layout()
    out_png = (
        out_dir / f"top_negative_contributors_{date_str}.png" if out_prefix is None else out_dir / f"{out_prefix}.png"
    )
    plt.savefig(out_png)
    print("Saved chart:", out_png)
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="date folder and prefix, e.g. 2026-02")
    p.add_argument("--prefix", required=False, help="optional output prefix")
    args = p.parse_args()
    return analyze(args.date, args.prefix)


if __name__ == "__main__":
    raise SystemExit(main())
