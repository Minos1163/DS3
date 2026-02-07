import sys
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else 'logs/v5_15m30d_trades_20260202_220341.csv'

df = pd.read_csv(path, parse_dates=['entry_time','exit_time'])

total = len(df)
wins = df[df['pnl'] > 0]
losses = df[df['pnl'] <= 0]
win_count = len(wins)
loss_count = len(losses)
win_rate = win_count / total * 100 if total else 0

largest_loss = df['pnl'].min()
largest_win = df['pnl'].max()

# longest consecutive loss streak
max_streak = 0
curr = 0
for v in df['pnl']:
    if v <= 0:
        curr += 1
        max_streak = max(max_streak, curr)
    else:
        curr = 0

# holding duration (minutes and bars of 15m)
df['hold_min'] = (df['exit_time'] - df['entry_time']).dt.total_seconds() / 60.0
# in case parse failed, handle NaN
if df['hold_min'].isna().all():
    avg_hold_min = None
    avg_hold_bars = None
    avg_hold_bars_wins = None
    avg_hold_bars_losses = None
else:
    avg_hold_min = df['hold_min'].mean()
    avg_hold_bars = avg_hold_min / 15.0 if avg_hold_min is not None else None
    avg_hold_bars_wins = df[df['pnl']>0]['hold_min'].mean() / 15.0 if win_count else None
    avg_hold_bars_losses = df[df['pnl']<=0]['hold_min'].mean() / 15.0 if loss_count else None

# equity curve and drawdown
if 'cumulative_capital' in df.columns:
    equity = df['cumulative_capital'].astype(float)
    running_max = equity.cummax()
    drawdowns = (running_max - equity) / running_max
    max_dd = drawdowns.max()
    dd_idx = drawdowns.idxmax()
    # find peak index before dd_idx
    if dd_idx is not None and dd_idx > 0:
        peak_idx = running_max[:dd_idx+1].idxmax()
    elif dd_idx is not None and dd_idx == 0:
        peak_idx = 0
    else:
        peak_idx = None
else:
    equity = None
    running_max = None
    drawdowns = None
    max_dd = None
    dd_idx = None
    peak_idx = None

# trades causing largest drawdown window
dd_window = None
if peak_idx is not None and dd_idx is not None:
    start = int(peak_idx) if isinstance(peak_idx, (int, float)) else 0
    end = int(dd_idx) if isinstance(dd_idx, (int, float)) else 0
    dd_window = df.iloc[start:end+1]

# print report
print(f"Analyzed: {path}")
print(f"Total trades: {total}, Wins: {win_count}, Losses: {loss_count}, Win rate: {win_rate:.2f}%")
print(f"Largest win: {largest_win:.4f}, Largest loss: {largest_loss:.4f}")
print(f"Longest consecutive loss streak: {max_streak}")
if avg_hold_bars is not None:
    wins_str = f"{avg_hold_bars_wins:.2f}" if avg_hold_bars_wins is not None else "N/A"
    losses_str = f"{avg_hold_bars_losses:.2f}" if avg_hold_bars_losses is not None else "N/A"
    print(f"Average holding: {avg_hold_bars:.2f} bars (wins: {wins_str}, losses: {losses_str})")
if max_dd is not None:
    print(f"Computed max drawdown: {max_dd*100:.2f}% (index {dd_idx}, peak index {peak_idx})")

# show top few trades in dd window
if dd_window is not None and len(dd_window):
    print('\nTrades in peak->trough window:')
    for i, row in dd_window.iterrows():
        print(f"{i}: {row['entry_time']} -> {row['exit_time']}, {row['direction']}, pnl={row['pnl']:.4f}, cumulative={row['cumulative_capital']:.4f}")

# show largest losing trades
print('\nTop 5 largest losing trades:')
for _, r in df.nsmallest(5, 'pnl').iterrows():
    print(f"{r['entry_time']} {r['direction']} pnl={r['pnl']:.4f} pct={r['pnl_pct']:.4f} exit_reason={r['exit_reason']}")

# show consecutive loss segments
print('\nConsecutive loss segments:')
segments = []
seg_start = None
for i, v in enumerate(df['pnl']):
    if v <= 0 and seg_start is None:
        seg_start = i
    if v > 0 and seg_start is not None:
        segments.append((seg_start, i-1))
        seg_start = None
if seg_start is not None:
    segments.append((seg_start, len(df)-1))

for s,e in segments:
    seg_df = df.iloc[s:e+1]
    total_seg = seg_df['pnl'].sum()
    print(f"{s}-{e}: count={len(seg_df)}, sum_pnl={total_seg:.4f}")

# exit code 0
