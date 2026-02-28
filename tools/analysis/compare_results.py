"""
对比优化前后的回测结果
"""
import pandas as pd

print("="*80)
print("📊 优化前后对比分析")
print("="*80)

# 读取两次回测的交易记录
trades_v1 = pd.read_csv('logs/backtest_trades_20260201_120116.csv')  # 优化前
trades_v2 = pd.read_csv('logs/backtest_trades_20260201_120557.csv')  # 优化后


def analyze_trades(trades_df, version_name):
    win = trades_df[trades_df['pnl'] > 0]
    loss = trades_df[trades_df['pnl'] <= 0]

    return {
        'name': version_name,
        'total_trades': len(trades_df),
        'win_trades': len(win),
        'loss_trades': len(loss),
        'win_rate': len(win) / len(trades_df) * 100,
        'total_pnl': trades_df['pnl'].sum(),
        'avg_pnl': trades_df['pnl'].mean(),
        'avg_win': win['pnl'].mean() if len(win) > 0 else 0,
        'avg_loss': loss['pnl'].mean() if len(loss) > 0 else 0,
        'profit_factor': abs(win['pnl'].sum() / loss['pnl'].sum()) if len(loss) > 0 and loss['pnl'].sum() != 0 else float('in'),
        'max_win': win['pnl'].max() if len(win) > 0 else 0,
        'max_loss': loss['pnl'].min() if len(loss) > 0 else 0,
    }

v1 = analyze_trades(trades_v1, "优化前 (止盈3%, 止损2%, RSI 30/70)")
v2 = analyze_trades(trades_v2, "优化后 (止盈4%, 止损1.5%, RSI 35/65)")

print(f"\n【{v1['name']}】")
print(f"总交易数: {v1['total_trades']}")
print(f"盈利交易: {v1['win_trades']} ({v1['win_rate']:.1f}%)")
print(f"总盈亏: ${v1['total_pnl']:.2f}")
print(f"平均盈亏: ${v1['avg_pnl']:.2f}")
print(f"平均盈利: ${v1['avg_win']:.2f}")
print(f"平均亏损: ${v1['avg_loss']:.2f}")
print(f"盈亏比: {v1['avg_win'] / abs(v1['avg_loss']):.2f}")
print(f"盈利因子: {v1['profit_factor']:.2f}")

print(f"\n【{v2['name']}】")
print(f"总交易数: {v2['total_trades']}")
print(f"盈利交易: {v2['win_trades']} ({v2['win_rate']:.1f}%)")
print(f"总盈亏: ${v2['total_pnl']:.2f}")
print(f"平均盈亏: ${v2['avg_pnl']:.2f}")
print(f"平均盈利: ${v2['avg_win']:.2f}")
print(f"平均亏损: ${v2['avg_loss']:.2f}")
print(f"盈亏比: {v2['avg_win'] / abs(v2['avg_loss']):.2f}")
print(f"盈利因子: {v2['profit_factor']:.2f}")

print(f"\n{'='*80}")
print("📈 对比结果")
print(f"{'='*80}")

print("\n✅ 改进指标:")
if v2['total_pnl'] > v1['total_pnl']:
    print(f"  总盈亏: ${v1['total_pnl']:.2f} → ${v2['total_pnl']:.2f} (提升 ${v2['total_pnl'] - v1['total_pnl']:.2f}, +{(v2['total_pnl'] - v1['total_pnl']) / v1['total_pnl'] * 100:.1f}%)")

if v2['avg_pnl'] > v1['avg_pnl']:
    print(f"  平均盈亏: ${v1['avg_pnl']:.2f} → ${v2['avg_pnl']:.2f}")

if v2['avg_win'] / abs(v2['avg_loss']) > v1['avg_win'] / abs(v1['avg_loss']):
    print(f"  盈亏比: {v1['avg_win'] / abs(v1['avg_loss']):.2f} → {v2['avg_win'] / abs(v2['avg_loss']):.2f}")

if abs(v2['avg_loss']) < abs(v1['avg_loss']):
    print(f"  平均亏损: ${v1['avg_loss']:.2f} → ${v2['avg_loss']:.2f} (减少 ${abs(v2['avg_loss']) - abs(v1['avg_loss']):.2f})")

print("\n⚠️ 需要注意:")
if v2['total_trades'] > v1['total_trades'] * 1.5:
    print(f"  交易次数: {v1['total_trades']} → {v2['total_trades']} (增加 {v2['total_trades'] - v1['total_trades']}, 交易频率提高)")

if v2['win_rate'] < v1['win_rate']:
    print(f"  胜率: {v1['win_rate']:.1f}% → {v2['win_rate']:.1f}% (下降 {v1['win_rate'] - v2['win_rate']:.1f}%)")

print(f"\n{'='*80}")
print("🎯 结论")
print(f"{'='*80}")

if v2['total_pnl'] > v1['total_pnl']:
    improvement = (v2['total_pnl'] - v1['total_pnl']) / v1['total_pnl'] * 100
    print(f"✅ 优化成功！总收益提升 {improvement:.1f}%")

    if v2['profit_factor'] > v1['profit_factor']:
        print(f"✅ 盈利因子提升: {v1['profit_factor']:.2f} → {v2['profit_factor']:.2f}")

    if abs(v2['avg_loss']) < abs(v1['avg_loss']):
        reduction = (abs(v1['avg_loss']) - abs(v2['avg_loss'])) / abs(v1['avg_loss']) * 100
        print(f"✅ 平均亏损减少 {reduction:.1f}%，风险控制改善")
else:
    print("⚠️ 优化未达预期，建议进一步调整")

print(f"\n{'='*80}")
print("📝 建议")
print(f"{'='*80}")

if v2['total_trades'] > v1['total_trades'] * 1.8:
    print("⚠️ 交易频率过高，建议:")
    print("  1. 增加额外的过滤条件（如成交量确认）")
    print("  2. 提高RSI阈值（如35→38, 65→62）")
    print("  3. 添加趋势过滤（如要求价格在MA20上方/下方）")

if v2['avg_win'] / abs(v2['avg_loss']) < 1.0:
    print("\n⚠️ 盈亏比仍然不足1，建议:")
    print("  1. 进一步提高止盈目标到5%")
    print("  2. 使用移动止损保护利润")
    print("  3. 考虑在盈利2%后移动止损到成本价")

if v2['win_rate'] > 70 and v2['profit_factor'] > 2.0:
    print("\n✅ 策略表现优秀，可以考虑:")
    print("  1. 用更长时间的数据验证稳定性")
    print("  2. 在不同市场环境下测试")
    print("  3. 逐步增加资金使用比例")
