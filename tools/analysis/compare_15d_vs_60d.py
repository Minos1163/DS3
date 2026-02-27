"""
对比15天和60天回测结果
验证策略稳定性和可扩展性
"""
import pandas as pd
from typing import Dict

def calculate_metrics(trades_df: pd.DataFrame, initial_capital: float = 10000) -> Dict:
    """计算详细指标"""
    total_trades = len(trades_df)
    if total_trades == 0:
        return {}

    wins = trades_df[trades_df['pnl'] > 0]
    losses = trades_df[trades_df['pnl'] < 0]

    total_profit = trades_df['pnl'].sum()
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = win_count / total_trades * 100

    avg_profit = wins['pnl'].mean() if len(wins) > 0 else 0
    avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0
    profit_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0

    total_win = wins['pnl'].sum() if len(wins) > 0 else 0
    total_loss = abs(losses['pnl'].sum()) if len(losses) > 0 else 0
    profit_factor = total_win / total_loss if total_loss != 0 else 0

    # 计算最大回撤
    trades_df['cumulative_profit'] = trades_df['pnl'].cumsum()
    trades_df['capital'] = initial_capital + trades_df['cumulative_profit']
    running_max = trades_df['capital'].expanding().max()
    drawdown = (trades_df['capital'] - running_max) / running_max * 100
    max_drawdown = drawdown.min()

    return {
        'total_profit': total_profit,
        'return_pct': total_profit / initial_capital * 100,
        'total_trades': total_trades,
        'win_count': win_count,
        'loss_count': loss_count,
        'win_rate': win_rate,
        'avg_profit': avg_profit,
        'avg_loss': avg_loss,
        'profit_ratio': profit_ratio,
        'profit_factor': profit_factor,
        'max_drawdown': max_drawdown,
        'trades_per_day': 0  # 需要额外计算
    }

def load_trades(file_path: str) -> pd.DataFrame:
    """加载交易记录"""
    df = pd.read_csv(file_path, encoding='utf-8')
    return df

def main():
    print("\n" + "="*80)
    print("📊 15天 vs 60天回测对比分析")
    print("="*80)

    # 加载15天数据（使用最近的V3结果）
    trades_15d = load_trades('logs/backtest_trades_20260201_120830.csv')

    # 加载60天数据
    trades_60d = load_trades('logs/backtest_trades_20260201_121328.csv')

    # 计算指标
    metrics_15d = calculate_metrics(trades_15d, 10000)
    metrics_60d = calculate_metrics(trades_60d, 10000)

    # 计算每天交易频率
    # 15天数据：2026-01-17至2026-02-01 = 15天
    # 60天数据：2025-12-03至2026-02-01 = 60天
    metrics_15d['trades_per_day'] = metrics_15d['total_trades'] / 15
    metrics_60d['trades_per_day'] = metrics_60d['total_trades'] / 60

    # 打印对比表格
    print("\n" + "="*80)
    print("核心指标对比")
    print("="*80)
    print(f"{'指标':<20} {'15天回测':<20} {'60天回测':<20} {'差异':<20}")
    print("-"*80)

    # 基础指标
    print(f"{'总收益':<20} ${metrics_15d['total_profit']:>7.2f}{'':<12} ${metrics_60d['total_profit']:>7.2f}{'':<12} {metrics_60d['total_profit']-metrics_15d['total_profit']:>+7.2f}")
    print(f"{'收益率':<20} {metrics_15d['return_pct']:>6.2f}%{'':<13} {metrics_60d['return_pct']:>6.2f}%{'':<13} {metrics_60d['return_pct']-metrics_15d['return_pct']:>+6.2f}%")
    print()

    # 交易统计
    print(f"{'总交易数':<20} {metrics_15d['total_trades']:>7}{'':<13} {metrics_60d['total_trades']:>7}{'':<13} {metrics_60d['total_trades']-metrics_15d['total_trades']:>+7}")
    print(f"{'每天交易数':<20} {metrics_15d['trades_per_day']:>7.2f}{'':<13} {metrics_60d['trades_per_day']:>7.2f}{'':<13} {metrics_60d['trades_per_day']-metrics_15d['trades_per_day']:>+7.2f}")
    print(f"{'胜率':<20} {metrics_15d['win_rate']:>6.2f}%{'':<13} {metrics_60d['win_rate']:>6.2f}%{'':<13} {metrics_60d['win_rate']-metrics_15d['win_rate']:>+6.2f}%")
    print()

    # 盈亏分析
    print(f"{'平均盈利':<20} ${metrics_15d['avg_profit']:>7.2f}{'':<12} ${metrics_60d['avg_profit']:>7.2f}{'':<12} ${metrics_60d['avg_profit']-metrics_15d['avg_profit']:>+7.2f}")
    print(f"{'平均亏损':<20} ${metrics_15d['avg_loss']:>7.2f}{'':<12} ${metrics_60d['avg_loss']:>7.2f}{'':<12} ${metrics_60d['avg_loss']-metrics_15d['avg_loss']:>+7.2f}")
    print(f"{'盈亏比':<20} {metrics_15d['profit_ratio']:>7.2f}{'':<13} {metrics_60d['profit_ratio']:>7.2f}{'':<13} {metrics_60d['profit_ratio']-metrics_15d['profit_ratio']:>+7.2f}")
    print(f"{'盈利因子':<20} {metrics_15d['profit_factor']:>7.2f}{'':<13} {metrics_60d['profit_factor']:>7.2f}{'':<13} {metrics_60d['profit_factor']-metrics_15d['profit_factor']:>+7.2f}")
    print()

    # 风险指标
    print(f"{'最大回撤':<20} {metrics_15d['max_drawdown']:>6.2f}%{'':<13} {metrics_60d['max_drawdown']:>6.2f}%{'':<13} {metrics_60d['max_drawdown']-metrics_15d['max_drawdown']:>+6.2f}%")

    print("\n" + "="*80)
    print("稳定性分析")
    print("="*80)

    # 计算一致性评分
    consistency_score = 0
    max_score = 100

    # 1. 收益率一致性（30分）
    return_diff = abs(metrics_60d['return_pct'] - metrics_15d['return_pct'])
    if return_diff < 1:
        return_consistency = 30
    elif return_diff < 3:
        return_consistency = 20
    elif return_diff < 5:
        return_consistency = 10
    else:
        return_consistency = 0
    consistency_score += return_consistency

    # 2. 胜率一致性（25分）
    winrate_diff = abs(metrics_60d['win_rate'] - metrics_15d['win_rate'])
    if winrate_diff < 3:
        winrate_consistency = 25
    elif winrate_diff < 5:
        winrate_consistency = 15
    elif winrate_diff < 10:
        winrate_consistency = 5
    else:
        winrate_consistency = 0
    consistency_score += winrate_consistency

    # 3. 盈亏比一致性（25分）
    ratio_diff = abs(metrics_60d['profit_ratio'] - metrics_15d['profit_ratio'])
    if ratio_diff < 0.05:
        ratio_consistency = 25
    elif ratio_diff < 0.1:
        ratio_consistency = 15
    elif ratio_diff < 0.2:
        ratio_consistency = 5
    else:
        ratio_consistency = 0
    consistency_score += ratio_consistency

    # 4. 交易频率一致性（20分）
    freq_diff = abs(metrics_60d['trades_per_day'] - metrics_15d['trades_per_day'])
    if freq_diff < 0.5:
        freq_consistency = 20
    elif freq_diff < 1:
        freq_consistency = 10
    elif freq_diff < 2:
        freq_consistency = 5
    else:
        freq_consistency = 0
    consistency_score += freq_consistency

    print(f"收益率一致性: {return_consistency}/30 分 (差异: {return_diff:.2f}%)")
    print(f"胜率一致性: {winrate_consistency}/25 分 (差异: {winrate_diff:.2f}%)")
    print(f"盈亏比一致性: {ratio_consistency}/25 分 (差异: {ratio_diff:.2f})")
    print(f"交易频率一致性: {freq_consistency}/20 分 (差异: {freq_diff:.2f}笔/天)")
    print()
    print(f"{'总体一致性得分:':<20} {consistency_score}/{max_score} 分")

    # 评级
    if consistency_score >= 80:
        grade = "优秀 (A)"
        comment = "策略在不同时间周期表现高度一致，稳定性极强"
    elif consistency_score >= 60:
        grade = "良好 (B)"
        comment = "策略稳定性良好，可用于实盘交易"
    elif consistency_score >= 40:
        grade = "一般 (C)"
        comment = "策略稳定性一般，建议进一步优化"
    else:
        grade = "较差 (D)"
        comment = "策略稳定性不足，不建议直接实盘"

    print(f"稳定性评级: {grade}")
    print(f"评价: {comment}")

    print("\n" + "="*80)
    print("关键发现")
    print("="*80)

    findings = []

    # 收益率分析
    if metrics_60d['return_pct'] > metrics_15d['return_pct'] * 0.8:
        findings.append("✅ 60天收益率保持在15天收益率的80%以上，策略可扩展性强")
    else:
        findings.append("⚠️  60天收益率明显低于15天，可能存在过拟合")

    # 胜率分析
    if abs(metrics_60d['win_rate'] - metrics_15d['win_rate']) < 5:
        findings.append("✅ 胜率在两个时间周期内高度一致")
    else:
        findings.append("⚠️  胜率波动较大，策略稳定性需要关注")

    # 交易频率分析
    if abs(metrics_60d['trades_per_day'] - metrics_15d['trades_per_day']) < 1:
        findings.append("✅ 交易频率稳定，不受时间周期影响")
    else:
        findings.append("⚠️  交易频率变化明显，可能与市场环境有关")

    # 盈亏比分析
    if metrics_60d['profit_ratio'] >= metrics_15d['profit_ratio'] * 0.9:
        findings.append("✅ 盈亏比保持稳定，风险控制有效")
    else:
        findings.append("⚠️  盈亏比下降明显，需要优化止盈止损策略")

    # 回撤分析
    if metrics_60d['max_drawdown'] > metrics_15d['max_drawdown'] * 1.5:
        findings.append("⚠️  60天回撤明显增大，风险控制需要加强")
    else:
        findings.append("✅ 回撤控制良好，风险在可接受范围内")

    for i, finding in enumerate(findings, 1):
        print(f"{i}. {finding}")

    print("\n" + "="*80)
    print("实盘建议")
    print("="*80)

    if consistency_score >= 70:
        print("✅ 策略通过60天扩展验证，可以考虑小额实盘测试")
        print("建议:")
        print("  1. 初始资金: $1,000-$3,000")
        print("  2. 仓位降低到15-20%")
        print("  3. 运行1-2周监控实盘表现")
        print("  4. 记录滑点和手续费影响")
        print("  5. 根据实盘反馈微调参数")
    elif consistency_score >= 50:
        print("⚠️  策略稳定性一般，建议进一步测试")
        print("建议:")
        print("  1. 下载更长时间数据（90-120天）")
        print("  2. 在不同市场环境（牛市/熊市/震荡）测试")
        print("  3. 优化参数以提高稳定性")
        print("  4. 暂缓实盘，继续优化")
    else:
        print("❌ 策略稳定性不足，不建议实盘")
        print("建议:")
        print("  1. 重新审视入场条件")
        print("  2. 优化止盈止损策略")
        print("  3. 考虑添加更多过滤条件")
        print("  4. 在模拟环境充分测试")

    print("\n" + "="*80)

if __name__ == '__main__':
    main()
