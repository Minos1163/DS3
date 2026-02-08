#!/usr/bin/env python3
"""
Watch for the latest parallel grid CSV to become stable (no writes for a period),
then aggregate, sort and save Top N results and append to reports/experiment_summary.md.

Usage:
    python scripts/watch_and_aggregate_parallel_grid.py --topn 10 --max_dd 20 --poll-interval 5 --stable-seconds 15
"""
import argparse
import glob
import os
import time
from datetime import datetime

import pandas as pd


def find_latest_parallel_csv(pattern="logs/deep_grid_parallel_full_*.csv"):
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def wait_for_file_stable(path, stable_seconds=15, poll_interval=5):
    if not path:
        return None
    print(f"Watching file: {path}")
    while True:
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            print("File removed, re-finding...")
            return None
        age = time.time() - mtime
        if age >= stable_seconds:
            print(f"File stable for {stable_seconds}s (mtime age {age:.1f}s). Proceeding to aggregate.")
            return path
        print(f"File not yet stable: mtime age {age:.1f}s; sleeping {poll_interval}s...")
        time.sleep(poll_interval)


def aggregate_topn(csv_path, topn=10, max_dd_percent=20.0, out_dir="logs"):
    print(f"Reading CSV: {csv_path}")
    df = pd.read_csv(csv_path)
    # Normalize column names
    cols = [c.strip() for c in df.columns]
    df.columns = cols
    if "final_capital" not in df.columns:
        raise RuntimeError("expected column final_capital in CSV")
    if "max_drawdown" not in df.columns:
        print("warning: max_drawdown not present, skipping dd filter")
        filtered = df
    else:
        filtered = df[df["max_drawdown"] <= float(max_dd_percent)]
    top = filtered.sort_values("final_capital", ascending=False).head(int(topn))
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(out_dir, f"deep_grid_parallel_top{topn}_{ts}.csv")
    top.to_csv(out_csv, index=False)
    print(f"Saved top {topn} to {out_csv}")
    return top, out_csv


def append_report(top_df, report_path="reports/experiment_summary.md"):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append("\n")
    lines.append(f"## Parallel grid auto-aggregation ({ts})\n")
    lines.append(f"Top {len(top_df)} results:\n")
    for _, row in top_df.iterrows():
        # Convert row to simple dict-like string
        d = {k: (float(v) if pd.api.types.is_numeric_dtype(type(v)) else v) for k, v in row.items()}
        lines.append(f"- {d}\n")
    with open(report_path, "a", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"Appended summary to {report_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topn", type=int, default=10, help="How many top rows to save")
    parser.add_argument("--max_dd", type=float, default=20.0, help="Max drawdown percent allowed (e.g., 20)")
    parser.add_argument("--poll-interval", type=int, default=5, help="Seconds between polls")
    parser.add_argument("--stable-seconds", type=int, default=15, help="Seconds of file-stability to consider finished")
    parser.add_argument(
        "--pattern", type=str, default="logs/deep_grid_parallel_full_*.csv", help="Glob pattern for parallel CSV"
    )
    parser.add_argument(
        "--report", type=str, default="reports/experiment_summary.md", help="Experiment summary file to append"
    )
    args = parser.parse_args()

    print("Starting watcher for parallel grid CSV...")
    latest = find_latest_parallel_csv(args.pattern)
    while latest is None:
        print("No parallel CSV found yet, waiting 5s...")
        time.sleep(5)
        latest = find_latest_parallel_csv(args.pattern)

    stable = wait_for_file_stable(latest, stable_seconds=args.stable_seconds, poll_interval=args.poll_interval)
    if stable is None:
        print("File disappeared; exiting")
        return
    top_df, _out_csv = aggregate_topn(stable, topn=args.topn, max_dd_percent=args.max_dd)
    append_report(top_df, report_path=args.report)
    print("Aggregation complete. Top results:")
    print(top_df.to_string(index=False))


if __name__ == "__main__":
    main()
