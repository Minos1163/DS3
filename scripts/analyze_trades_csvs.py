"""Analyze per-strategy trades CSVs and propose simple filtering rules.

Usage:
    .venv\Scripts\python.exe scripts\analyze_trades_csvs.py --dir logs/parallel_trades_20260131_225224
"""
from __future__ import annotations

import pandas as pd

import argparse
import os

from datetime import datetime


def parse_time(s: str) -> datetime:
    # support 'YYYY-MM-DD HH:MM' and ISO-like 'YYYY-MM-DDTHH:MM:SS'
    if not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def analyze_file(path: str):
    df = pd.read_csv(path)
    df_columns = list(df.columns)
    n = len(df)
    wins = (df["pnl"] > 0).sum()
    losses = (df["pnl"] <= 0).sum()
    win_rate = wins / n * 100 if n else 0
    gross_profit = df.loc[df["pnl"] > 0, "pnl"].sum()
    gross_loss = -df.loc[df["pnl"] < 0, "pnl"].sum()
    expectancy = df["pnl"].mean() if n else 0
    stats = df["pnl"].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).to_dict()

    # holding time (minutes)
    hold_minutes = []
    for a, b in zip(df.get("entry_time", []), df.get("exit_time", [])):
        t0 = parse_time(a)
        t1 = parse_time(b)
        if t0 and t1:
            hold_minutes.append((t1 - t0).total_seconds() / 60.0)
    hold_mean = float(pd.Series(hold_minutes).mean()) if hold_minutes else None

    res = {
        "file": os.path.basename(path),
        "n": int(n),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate_pct": float(win_rate),
        "gross_profit": float(gross_profit),
        "gross_loss": float(gross_loss),
        "expectancy": float(expectancy),
        "pnl_stats": {k: float(v) for k, v in stats.items()},
        "avg_hold_minutes": float(hold_mean) if hold_mean is not None else None,
        "columns": df_columns,
    }

    # if confidence exists, bucket by confidence
    if "confidence" in df_columns:
        bins = [0.0, 0.6, 0.7, 0.8, 0.9, 1.0]
        df["conf_bin"] = pd.cut(df["confidence"], bins=bins, include_lowest=True)
        conf_stats = df.groupby("conf_bin")["pnl"].agg(["count", "mean"]).reset_index()
        conf_stats["conf_bin"] = conf_stats["conf_bin"].astype(str)
        res["confidence_buckets"] = conf_stats.to_dict(orient="records")

    return res


def propose_rules(analyses: list[dict]):
    rules = []
    # rule: require min_conf if any bucket shows positive mean pnl
    for a in analyses:
        name = a["file"]
        if "confidence_buckets" in a:
            for b in a["confidence_buckets"]:
                if b["count"] > 20 and b["mean"] > 0:
                    # find lower bound of that bin
                    rules.append(
                        f"For {name}: require confidence >= {str(b['conf_bin']).split(',')[-1].strip(' ])') if b.get('conf_bin') else 'x'} (see buckets)"
                    )
                    break

    # if strategies have very low win rates, suggest adding momentum filter
    for a in analyses:
        if a["win_rate_pct"] < 20 and a["n"] > 50:
            rules.append(
                f"For {a['file']}: win_rate {a['win_rate_pct']:.1f}% — add momentum filter (e.g. MACD>signal or close>EMA50) or increase min ATR/stop-distance."
            )

    # if average hold time is very long, suggest max_hold
    for a in analyses:
        if a["avg_hold_minutes"] and a["avg_hold_minutes"] > 60 * 24:
            rules.append(
                f"For {a['file']}: average holding time {a['avg_hold_minutes']:.0f} min — consider max holding duration (e.g. 200 candles)."
            )

    # general recommendations
    rules.append(
        "General: add min_volume or min_atr threshold at signal time; require multi-indicator agreement (RSI/MACD/EMA); increase AI min_confidence to 0.7+ and validate by backtesting confidence buckets."
    )
    return rules


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", "-d", required=True)
    args = p.parse_args()

    trades_dir = args.dir
    paths = [os.path.join(trades_dir, f) for f in os.listdir(trades_dir) if f.endswith(".csv")]
    analyses = []
    for path in paths:
        analyses.append(analyze_file(path))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join("reports", f"trade_analysis_{ts}.txt")
    os.makedirs("reports", exist_ok=True)

    rules = propose_rules(analyses)

    with open(out_path, "w", encoding="utf-8") as rf:
        import json

        rf.write("Per-file analyses:\n")
        rf.write(json.dumps(analyses, indent=2, ensure_ascii=False))
        rf.write("\n\nProposed rules:\n")
        for r in rules:
            rf.write("- " + r + "\n")

    print("Wrote analysis to", out_path)


if __name__ == "__main__":
    main()
