"""Analyze AI parameter grid search results and identify key parameters affecting P&L.

Produces a text report and CSV summaries in `reports/`.
"""
from __future__ import annotations

import os
import glob
from datetime import datetime
import pandas as pd


def latest_grid_file():
    files = sorted(glob.glob(os.path.join("reports", "ai_param_search_*.csv")), reverse=True)
    # prefer the full results file (not the summary) if both exist
    files = [f for f in files if "summary" not in os.path.basename(f)] + [f for f in files if "summary" in os.path.basename(f)]
    return files[0] if files else None


def main():
    src = latest_grid_file()
    if not src:
        print("No ai_param_search_*.csv found in reports/")
        return
    df = pd.read_csv(src)
    out_txt = os.path.join("reports", f"ai_param_search_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    os.makedirs("reports", exist_ok=True)

    # Focus on combos that produced trades
    df_nonzero = df[df["total"] > 0].copy()

    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"Source grid file: {src}\n\n")
        f.write(f"Total combos: {len(df)}\n")
        f.write(f"Combos with trades: {len(df_nonzero)}\n\n")

        if df_nonzero.empty:
            f.write("No combos produced trades.\n")
            print("Wrote analysis to", out_txt)
            return

        # Correlations
        numeric = df_nonzero[["min_conf", "min_atr", "max_hold", "final_equity", "expectancy"]].copy()
        corr = numeric.corr()
        f.write("Correlation matrix (numeric params vs final_equity/expectancy):\n")
        f.write(corr.to_string())
        f.write("\n\n")

        # Grouped summaries
        grp = df_nonzero.groupby(["momentum", "ema_field"]).agg({"final_equity": ["mean", "std", "count"], "expectancy": ["mean"]})
        f.write("Grouped summary by momentum and ema_field:\n")
        f.write(grp.to_string())
        f.write("\n\n")

        # Top combos
        f.write("Top 10 combos by final_equity:\n")
        top_equity = df_nonzero.sort_values("final_equity", ascending=False).head(10)
        f.write(top_equity.to_string(index=False))
        f.write("\n\n")

        f.write("Top 10 combos by expectancy:\n")
        top_exp = df_nonzero.sort_values("expectancy", ascending=False).head(10)
        f.write(top_exp.to_string(index=False))

    # Also write grouped CSVs for easier inspection
    df_nonzero.to_csv(os.path.join("reports", f"ai_param_search_nonzero_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"), index=False)
    print("Wrote analysis to", out_txt)


if __name__ == '__main__':
    main()
