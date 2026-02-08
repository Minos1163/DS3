import re
import glob
import pandas as pd
from pathlib import Path

ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def clean(s):
    if s is None:
        return s
    s = str(s)
    s = ansi_escape.sub("", s)
    return s.strip()


files = sorted(glob.glob("logs/2026-02/2026-02-08_*.txt"))
rows = []
if not files:
    print("no files")
    raise SystemExit(1)

for f in files:
    with open(f, "r", encoding="utf-8", errors="ignore") as fh:
        lines = fh.readlines()
    ts = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if "📅 交易周期" in line:
            # next tokens contain timestamp
            m = re.search(r"(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if m:
                ts = m.group(1)
            # scan ahead for header
            j = i + 1
            header_idx = None
            for k in range(j, min(j + 30, len(lines))):
                if "交易对 | 方向 | 数量 | 入场价" in lines[k]:
                    header_idx = k
                    break
            if header_idx is None:
                i += 1
                continue
            # table rows start after header line + separator
            row_idx = header_idx + 2
            while row_idx < len(lines) and lines[row_idx].strip():
                text = clean(lines[row_idx])
                # parse by '|'
                parts = [p.strip() for p in text.split("|")]
                if len(parts) >= 9:
                    symbol = parts[0]
                    side = parts[1]
                    quantity = parts[2]
                    entry_price = parts[3]
                    mark_price = parts[4]
                    floating = parts[5]
                    pnl_pct = parts[6]
                    holding_amount = parts[7]
                    leverage = parts[8]
                    # normalize values
                    try:
                        holding_amt = float(holding_amount)
                    except Exception:
                        holding_amt = None
                    # pnl percent may contain +/-, % and color codes already removed
                    pp = re.sub(r"[^0-9+\-\.eE]", "", pnl_pct)
                    try:
                        ppv = float(pp)
                    except Exception:
                        ppv = None
                    # if ppv seems like fraction (e.g., 0.432), assume percent? but sample shows +0.194269... meaning percent value already percent
                    # The DCA CSV used 0.43206384294 which is percent; logs show +0.194269..., so treat as percent number (not *100)
                    rows.append(
                        {
                            "timestamp": ts,
                            "symbol": symbol,
                            "side": side,
                            "quantity": quantity,
                            "entry_price": entry_price,
                            "mark_price": mark_price,
                            "pnl_percent": ppv,
                            "holding_amount": holding_amt,
                            "leverage": leverage,
                            "source_file": Path(f).name,
                        }
                    )
                row_idx += 1
            i = row_idx
        else:
            i += 1

if not rows:
    print("no rows parsed")
    raise SystemExit(0)

df = pd.DataFrame(rows)
# parse timestamps
df["timestamp"] = pd.to_datetime(df["timestamp"])
# compute pnl_usdt = holding_amount * pnl_percent/100


def compute_pnl_usdt(row):
    try:
        if pd.isna(row["holding_amount"]) or pd.isna(row["pnl_percent"]):
            return None
        return row["holding_amount"] * (row["pnl_percent"] / 100.0)
    except Exception:
        return None


df["pnl_usdt"] = df.apply(compute_pnl_usdt, axis=1)
# aggregate by timestamp and symbol (take last occurrence per block)
df2 = df.sort_values(["timestamp"]).dropna(subset=["pnl_usdt"])
# pivot to get pnl_usdt per symbol per timestamp
pivot = df2.pivot_table(index="timestamp", columns="symbol", values="pnl_usdt", aggfunc="first")
# compute diffs
pnl_diff = pivot.diff()
# equity series: find equity lines in same files
# scan files to capture "总权益: X USDT" near blocks
equity = {}
for f in files:
    with open(f, "r", encoding="utf-8", errors="ignore") as fh:
        lines = fh.readlines()
    for i, line in enumerate(lines):
        if "总权益:" in line:
            m = re.search(r"总权益:\s*([0-9]+\.?[0-9]*)\s*USDT", line)
            if m:
                # find timestamp earlier in file near this line
                # search backwards for a line with datetime
                t = None
                for k in range(max(0, i - 50), i + 1):
                    if "📅 交易周期" in lines[k]:
                        mm = re.search(r"(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", lines[k])
                        if mm:
                            t = mm.group(1)
                            break
                if t:
                    equity[pd.to_datetime(t)] = float(m.group(1))

if not equity:
    print("no equity found")
    # but we can still use pnl diffs

equity_series = pd.Series(equity).sort_index()
# align index
common_index = pnl_diff.index.intersection(equity_series.index)
if len(common_index) == 0:
    # try nearest alignment by rounding minutes
    pnl_diff.index = pnl_diff.index.floor("T")
    equity_s_idx = equity_series.index.floor("T")
    common_index = pnl_diff.index.intersection(equity_s_idx)

# Attribute equity decreases to symbols by pnl_usdt negative diffs
contrib = {}
for ts in common_index:
    ed = equity_series.loc[ts]
    # find previous equity
    prev_idx = equity_series.index.get_indexer([ts])[0]
# Instead, compute equity diff from series
if not equity_series.empty:
    e_diff = equity_series.diff()
else:
    e_diff = None

# use pnl_diff to attribute losses across all timestamps available
for ts in pnl_diff.index:
    row = pnl_diff.loc[ts]
    ed = None
    if not equity_series.empty:
        # find nearest equity timestamp <= ts
        eq_idx = equity_series.index.searchsorted(ts)
        if eq_idx > 0:
            ed = equity_series.iloc[eq_idx] - equity_series.iloc[eq_idx - 1]
    # use pnl diffs directly
    neg = row[row < 0].dropna()
    if neg.empty:
        continue
    total_neg = neg.abs().sum()
    if total_neg == 0:
        continue
    # distribute observed equity drop (if available) proportionally; else use pnl_usdt deltas
    for sym, val in neg.items():
        share = val
        contrib[sym] = contrib.get(sym, 0.0) + share

res = pd.DataFrame([{"symbol": k, "contribution_usdt": v} for k, v in contrib.items()])
res = res.sort_values("contribution_usdt")
res["abs_usdt"] = res["contribution_usdt"].abs()
res["pct_of_total_loss"] = res["abs_usdt"] / res["abs_usdt"].sum() * 100
out = Path("logs/precise_negative_contributors_2026-02-08.csv")
res.to_csv(out, index=False)
print(res.head(10).to_string(index=False))
print("Saved:", out)
