import glob
import pandas as pd
from pathlib import Path

files = sorted(glob.glob("logs/2026-02/DCA_dashboard_2026-02-08_*.csv"))
if not files:
    print("No files")
    raise SystemExit(1)

parts = []
for f in files:
    df = pd.read_csv(f, parse_dates=["timestamp"])
    parts.append(df)

df = pd.concat(parts, ignore_index=True)
# Ensure timestamps sorted
DF = df.copy()
DF["timestamp"] = pd.to_datetime(DF["timestamp"])
DF = DF.sort_values("timestamp")
# Extract equity per timestamp (first occurrence)
equity = DF.groupby("timestamp")["equity"].first().sort_index()
# Pivot pnl_percent per symbol
DF["pnl_percent"] = pd.to_numeric(DF["pnl_percent"], errors="coerce")
pivot = DF.pivot_table(index="timestamp", columns="symbol", values="pnl_percent", aggfunc="first")
# Align pivot with equity index (some timestamps may mismatch)
pivot = pivot.reindex(equity.index)
# Compute diffs
equity_diff = equity.diff()
pnl_diff = pivot.diff()

# For each timestamp where equity decreased, attribute to symbols whose pnl worsened
contrib = {}
contrib_counts = {}
for ts in equity_diff.index[1:]:
    ed = equity_diff.loc[ts]
    if pd.isna(ed) or ed >= 0:
        continue
    row = pnl_diff.loc[ts]
    # select symbols with negative pnl change
    neg = row[row < 0].dropna()
    if neg.empty:
        continue
    weights = neg.abs()
    s = weights.sum()
    if s == 0:
        continue
    # distribute equity drop proportionally
    for sym, w in weights.items():
        share = (w / s) * ed
        contrib[sym] = contrib.get(sym, 0.0) + share
        contrib_counts[sym] = contrib_counts.get(sym, 0) + 1

# Build DataFrame
if not contrib:
    print("No negative contributors found")
    raise SystemExit(0)

res = pd.DataFrame(
    [
        {"symbol": k, "contribution_usdt": v, "count_negative_events": contrib_counts.get(k, 0)}
        for k, v in contrib.items()
    ]
)
res = res.sort_values("contribution_usdt")
res["abs_usdt"] = res["contribution_usdt"].abs()
res["pct_of_total_loss"] = res["abs_usdt"] / res["abs_usdt"].sum() * 100
out = Path("logs/top5_negative_contributors_2026-02-08.csv")
res.to_csv(out, index=False)
print(res.head(10).to_string(index=False))
print(f"Saved: {out}")
