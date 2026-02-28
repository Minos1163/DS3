"""
深度分析交易记录，找出优化方向
"""
import pandas as pd

# 读取交易记录
trades_df = pd.read_csv('logs/backtest_trades_20260201_120116.csv')
trades_df['entry_time'] = pd.to_datetime(trades_df['entry_time'])
trades_df['exit_time'] = pd.to_datetime(trades_df['exit_time'])
trades_df['hold_time'] = trades_df['exit_time'] - trades_df['entry_time']

print("="*80)
print("📊 交易记录深度分析")
print("="*80)

# 盈利交易分析
win_trades = trades_df[trades_df['pnl'] > 0]
loss_trades = trades_df[trades_df['pnl'] <= 0]

print(f"\n【盈利交易分析】共 {len(win_trades)} 笔")
print(f"平均持仓时间: {win_trades['hold_time'].mean()}")
print(f"平均盈利: ${win_trades['pnl'].mean():.2f}")
print(f"平均盈利%: {win_trades['pnl_pct'].mean():.2f}%")
print(f"盈利中位数%: {win_trades['pnl_pct'].median():.2f}%")
print(f"最大盈利%: {win_trades['pnl_pct'].max():.2f}%")
print(f"盈利>2%的交易: {len(win_trades[win_trades['pnl_pct'] > 2])} 笔")
print(f"盈利>3%的交易: {len(win_trades[win_trades['pnl_pct'] > 3])} 笔")

print(f"\n【亏损交易分析】共 {len(loss_trades)} 笔")
print(f"平均持仓时间: {loss_trades['hold_time'].mean()}")
print(f"平均亏损: ${loss_trades['pnl'].mean():.2f}")
print(f"平均亏损%: {loss_trades['pnl_pct'].mean():.2f}%")
print(f"亏损中位数%: {loss_trades['pnl_pct'].median():.2f}%")
print(f"最大亏损%: {loss_trades['pnl_pct'].min():.2f}%")
print(f"亏损达到-2%止损的: {len(loss_trades[loss_trades['pnl_pct'] <= -2])} 笔")

# 方向分析
long_trades = trades_df[trades_df['direction'] == 'LONG']
short_trades = trades_df[trades_df['direction'] == 'SHORT']

print("\n【做多 vs 做空】")
print(f"做多交易: {len(long_trades)} 笔, 盈利: {len(long_trades[long_trades['pnl'] > 0])}, 胜率: {len(long_trades[long_trades['pnl'] > 0])/len(long_trades)*100:.1f}%")
print(f"  平均盈亏: ${long_trades['pnl'].mean():.2f}, 平均盈亏%: {long_trades['pnl_pct'].mean():.2f}%")
print(f"做空交易: {len(short_trades)} 笔, 盈利: {len(short_trades[short_trades['pnl'] > 0])}, 胜率: {len(short_trades[short_trades['pnl'] > 0])/len(short_trades)*100:.1f}%")
print(f"  平均盈亏: ${short_trades['pnl'].mean():.2f}, 平均盈亏%: {short_trades['pnl_pct'].mean():.2f}%")

# 关键发现
print("\n【关键发现】")
print(f"1. 盈利交易平均持仓 {win_trades['hold_time'].mean()} vs 亏损交易 {loss_trades['hold_time'].mean()}")
print(f"2. 当前止盈3%，但有 {len(win_trades[win_trades['pnl_pct'] > 3])} 笔交易盈利超过3%")
print(f"3. 当前止损2%，实际平均亏损 {loss_trades['pnl_pct'].mean():.2f}%")
print(f"4. 盈亏比问题：平均盈利 {win_trades['pnl_pct'].mean():.2f}% / 平均亏损 {abs(loss_trades['pnl_pct'].mean()):.2f}% = {win_trades['pnl_pct'].mean() / abs(loss_trades['pnl_pct'].mean()):.2f}")

print("\n【优化建议】")
if win_trades['pnl_pct'].mean() < abs(loss_trades['pnl_pct'].mean()):
    print("⚠️ 平均盈利低于平均亏损！建议：")
    print("  - 方案1: 提高止盈到4-5%，保持止损2%")
    print("  - 方案2: 收紧止损到1.5%，保持止盈3%")
    print("  - 方案3: 使用移动止损，让盈利奔跑")

if len(win_trades[win_trades['pnl_pct'] > 3]) > 5:
    print(f"\n✅ 发现 {len(win_trades[win_trades['pnl_pct'] > 3])} 笔交易盈利超过3%")
    print("  建议提高止盈目标到4-5%，捕捉更大利润")

# 按时间分析
print("\n【时间分析】")
print("盈利交易持仓时间分布:")
print(win_trades['hold_time'].describe())
print("\n亏损交易持仓时间分布:")
print(loss_trades['hold_time'].describe())
