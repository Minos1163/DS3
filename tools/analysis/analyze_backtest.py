"""
回测日志分析脚本
分析回测生成的K线日志，找出盈亏点，总结优化建议
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class BacktestAnalyzer:
    """回测分析器"""

    def __init__(self, kline_log_file: str):
        """
        初始化分析器

        Args:
            kline_log_file: K线日志CSV文件路径
        """
        self.kline_log_file = kline_log_file
        self.df = None
        self.trades_df = None

    def load_logs(self):
        """加载日志文件"""
        print(f"\n{'='*80}")
        print("📂 加载回测日志")
        print(f"{'='*80}")
        print(f"文件: {self.kline_log_file}")

        if not os.path.exists(self.kline_log_file):
            print("❌ 文件不存在")
            return False

        try:
            self.df = pd.read_csv(self.kline_log_file, parse_dates=['timestamp'])
            print("✅ 日志加载成功")
            print(f"   K线数量: {len(self.df)}")
            print(f"   时间范围: {self.df['timestamp'].min()} 至 {self.df['timestamp'].max()}")

            # 尝试加载交易日志
            trade_log_file = self.kline_log_file.replace('klines', 'trades')
            if os.path.exists(trade_log_file):
                self.trades_df = pd.read_csv(trade_log_file, parse_dates=['entry_time', 'exit_time'])
                print(f"   交易数量: {len(self.trades_df)}")

            return True
        except Exception as e:
            print(f"❌ 加载失败: {e}")
            return False

    def analyze_capital_curve(self):
        """分析资金曲线"""
        if self.df is None:
            print("⚠️  无数据")
            return None

        print(f"\n{'='*80}")
        print("💰 资金曲线分析")
        print(f"{'='*80}")

        capital = self.df['capital'].to_numpy()
        initial_capital = capital[0]
        final_capital = capital[-1]

        # 最大资金和最小资金
        max_capital = float(np.max(capital))
        min_capital = float(np.min(capital))
        max_capital_idx = int(np.argmax(capital))
        min_capital_idx = int(np.argmin(capital))

        # 最大回撤
        cummax = pd.Series(capital).cummax()
        drawdown = (capital - cummax) / cummax * 100
        max_drawdown = drawdown.min()
        max_drawdown_idx = drawdown.argmin()

        print(f"初始资金: ${initial_capital:,.2f}")
        print(f"最终资金: ${final_capital:,.2f}")
        print(f"总收益: ${final_capital - initial_capital:+,.2f}")
        print(f"收益率: {(final_capital / initial_capital - 1) * 100:+.2f}%")
        print(f"\n最高资金: ${max_capital:,.2f} @ K线 {max_capital_idx} ({self.df.iloc[max_capital_idx]['timestamp']})")
        print(f"最低资金: ${min_capital:,.2f} @ K线 {min_capital_idx} ({self.df.iloc[min_capital_idx]['timestamp']})")
        print(f"最大回撤: {max_drawdown:.2f}% @ K线 {max_drawdown_idx} ({self.df.iloc[max_drawdown_idx]['timestamp']})")

        return {
            'initial_capital': initial_capital,
            'final_capital': final_capital,
            'max_capital': max_capital,
            'min_capital': min_capital,
            'max_drawdown': max_drawdown,
            'max_drawdown_idx': max_drawdown_idx
        }

    def analyze_trades(self):
        """分析交易记录"""
        if self.trades_df is None or len(self.trades_df) == 0:
            print("\n⚠️  无交易记录")
            return None

        print(f"\n{'='*80}")
        print("📊 交易分析")
        print(f"{'='*80}")

        trades = self.trades_df

        # 基本统计
        total_trades = len(trades)
        win_trades = trades[trades['pnl'] > 0]
        loss_trades = trades[trades['pnl'] <= 0]

        win_rate = len(win_trades) / total_trades * 100 if total_trades > 0 else 0

        total_pnl = trades['pnl'].sum()
        avg_pnl = trades['pnl'].mean()
        avg_win = win_trades['pnl'].mean() if len(win_trades) > 0 else 0
        avg_loss = loss_trades['pnl'].mean() if len(loss_trades) > 0 else 0

        profit_factor = abs(win_trades['pnl'].sum() / loss_trades['pnl'].sum()) if len(loss_trades) > 0 and loss_trades['pnl'].sum() != 0 else float('inf')

        print(f"总交易数: {total_trades}")
        print(f"盈利交易: {len(win_trades)} ({len(win_trades)/total_trades*100:.1f}%)")
        print(f"亏损交易: {len(loss_trades)} ({len(loss_trades)/total_trades*100:.1f}%)")
        print(f"胜率: {win_rate:.2f}%")
        print(f"\n总盈亏: ${total_pnl:+,.2f}")
        print(f"平均盈亏: ${avg_pnl:+,.2f}")
        print(f"平均盈利: ${avg_win:+,.2f}")
        print(f"平均亏损: ${avg_loss:+,.2f}")
        print(f"盈亏比: {abs(avg_win / avg_loss) if avg_loss != 0 else 0:.2f}")
        print(f"盈利因子: {profit_factor:.2f}")

        # 最佳和最差交易
        best_trade = trades.loc[trades['pnl'].idxmax()]
        worst_trade = trades.loc[trades['pnl'].idxmin()]

        print("\n最佳交易:")
        print(f"  {best_trade['direction']} | ${best_trade['entry_price']:.2f} → ${best_trade['exit_price']:.2f} | ${best_trade['pnl']:+,.2f} ({best_trade['pnl_pct']:+.2f}%)")
        print(f"  入场: {best_trade['entry_time']} | 出场: {best_trade['exit_time']}")

        print("\n最差交易:")
        print(f"  {worst_trade['direction']} | ${worst_trade['entry_price']:.2f} → ${worst_trade['exit_price']:.2f} | ${worst_trade['pnl']:+,.2f} ({worst_trade['pnl_pct']:+.2f}%)")
        print(f"  入场: {worst_trade['entry_time']} | 出场: {worst_trade['exit_time']}")

        return {
            'total_trades': total_trades,
            'win_trades': len(win_trades),
            'loss_trades': len(loss_trades),
            'win_rate': win_rate,
            'avg_pnl': avg_pnl,
            'profit_factor': profit_factor
        }

    def analyze_indicators(self):
        """分析指标特征"""
        if self.df is None:
            print("⚠️  无数据")
            return

        print(f"\n{'='*80}")
        print("📈 指标分析")
        print(f"{'='*80}")

        # RSI分析
        rsi = self.df['rsi'].dropna()
        print("\n【RSI指标】")
        print(f"平均值: {rsi.mean():.2f}")
        print(f"中位数: {rsi.median():.2f}")
        print(f"最小值: {rsi.min():.2f}")
        print(f"最大值: {rsi.max():.2f}")
        print(f"超卖次数 (<30): {(rsi < 30).sum()}")
        print(f"超买次数 (>70): {(rsi > 70).sum()}")

        # MACD分析
        macd_hist = self.df['macd_hist'].dropna()
        print("\n【MACD柱状图】")
        print(f"平均值: {macd_hist.mean():.4f}")
        print(f"正值次数: {(macd_hist > 0).sum()}")
        print(f"负值次数: {(macd_hist < 0).sum()}")

        # 价格分析
        close = self.df['close']
        print("\n【价格走势】")
        print(f"平均价格: ${close.mean():.2f}")
        print(f"最低价格: ${close.min():.2f}")
        print(f"最高价格: ${close.max():.2f}")
        print(f"价格波动: ${close.max() - close.min():.2f}")
        print(f"平均涨跌幅: {self.df['change_pct'].mean():.4f}%")

    def find_profit_loss_points(self):
        """找出盈亏关键点"""
        print(f"\n{'='*80}")
        print("🔍 盈亏关键点分析")
        print(f"{'='*80}")

        if self.trades_df is None or len(self.trades_df) == 0:
            print("⚠️  无交易数据")
            return

        # 盈利交易的特征
        win_trades = self.trades_df[self.trades_df['pnl'] > 0]
        loss_trades = self.trades_df[self.trades_df['pnl'] <= 0]

        if len(win_trades) > 0 and self.df is not None:
            print(f"\n【盈利交易特征】(共{len(win_trades)}笔)")

            # 找出盈利交易入场时的指标
            win_entries = []
            for _, trade in win_trades.iterrows():
                entry_kline = self.df[self.df['timestamp'] == trade['entry_time']]
                if len(entry_kline) > 0:
                    win_entries.append(entry_kline.iloc[0])

            if win_entries:
                win_df = pd.DataFrame(win_entries)
                print(f"入场时RSI范围: {win_df['rsi'].min():.2f} - {win_df['rsi'].max():.2f} (平均: {win_df['rsi'].mean():.2f})")
                print(f"入场时MACD柱状图范围: {win_df['macd_hist'].min():.4f} - {win_df['macd_hist'].max():.4f}")
                print(f"EMA5 > EMA20: {(win_df['ema_5'] > win_df['ema_20']).sum()} / {len(win_df)}")

        if len(loss_trades) > 0 and self.df is not None:
            print(f"\n【亏损交易特征】(共{len(loss_trades)}笔)")

            # 找出亏损交易入场时的指标
            loss_entries = []
            for _, trade in loss_trades.iterrows():
                entry_kline = self.df[self.df['timestamp'] == trade['entry_time']]
                if len(entry_kline) > 0:
                    loss_entries.append(entry_kline.iloc[0])

            if loss_entries:
                loss_df = pd.DataFrame(loss_entries)
                print(f"入场时RSI范围: {loss_df['rsi'].min():.2f} - {loss_df['rsi'].max():.2f} (平均: {loss_df['rsi'].mean():.2f})")
                print(f"入场时MACD柱状图范围: {loss_df['macd_hist'].min():.4f} - {loss_df['macd_hist'].max():.4f}")
                print(f"EMA5 > EMA20: {(loss_df['ema_5'] > loss_df['ema_20']).sum()} / {len(loss_df)}")

    def generate_optimization_suggestions(self):
        """生成优化建议"""
        print(f"\n{'='*80}")
        print("💡 优化建议")
        print(f"{'='*80}")

        suggestions = []

        if self.trades_df is not None and len(self.trades_df) > 0:
            trades = self.trades_df
            win_rate = len(trades[trades['pnl'] > 0]) / len(trades) * 100

            # 胜率建议
            if win_rate < 40:
                suggestions.append("❌ 胜率过低 (<40%)，建议：")
                suggestions.append("   - 提高开仓门槛，增加更多确认信号")
                suggestions.append("   - 检查RSI阈值是否过于激进")
                suggestions.append("   - 考虑添加成交量确认")
            elif win_rate > 60:
                suggestions.append("✅ 胜率较高 (>60%)，建议：")
                suggestions.append("   - 当前策略表现良好，可以维持")
                suggestions.append("   - 可以适当提高止盈目标")

            # 盈亏比建议
            avg_win = trades[trades['pnl'] > 0]['pnl'].mean() if len(trades[trades['pnl'] > 0]) > 0 else 0
            avg_loss = trades[trades['pnl'] <= 0]['pnl'].mean() if len(trades[trades['pnl'] <= 0]) > 0 else 0
            profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

            if profit_loss_ratio < 1.5:
                suggestions.append("❌ 盈亏比不足 (<1.5)，建议：")
                suggestions.append("   - 提高止盈比例（如从3%提高到4%）")
                suggestions.append("   - 降低止损比例（如从2%降低到1.5%）")
                suggestions.append("   - 使用移动止损锁定利润")

            # 交易频率建议
            if len(trades) < 5:
                suggestions.append("⚠️  交易次数过少，建议：")
                suggestions.append("   - 放宽开仓条件")
                suggestions.append("   - 降低RSI超卖/超买阈值")
            elif len(trades) > 50:
                suggestions.append("⚠️  交易过于频繁，建议：")
                suggestions.append("   - 提高开仓门槛")
                suggestions.append("   - 增加过滤条件")

        # RSI建议
        if self.df is not None:
            rsi = self.df['rsi'].dropna()
            oversold_count = (rsi < 30).sum()
            overbought_count = (rsi > 70).sum()
        else:
            oversold_count = 0
            overbought_count = 0

        if oversold_count > overbought_count * 2:
            suggestions.append("📊 RSI超卖信号远多于超买，建议：")
            suggestions.append("   - 市场可能处于下跌趋势")
            suggestions.append("   - 考虑调整RSI超卖阈值（如从30调整到25）")
            suggestions.append("   - 或增加做空策略")

        # 显示建议
        if suggestions:
            for suggestion in suggestions:
                print(suggestion)
        else:
            print("✅ 策略表现正常，暂无优化建议")

        # 参数推荐
        print(f"\n{'='*80}")
        print("🎯 推荐参数配置")
        print(f"{'='*80}")

        if self.trades_df is not None and len(self.trades_df) > 0:
            win_rate = len(trades[trades['pnl'] > 0]) / len(trades) * 100

            if win_rate < 40:
                print("针对低胜率的参数调整：")
                print("  rsi_oversold: 25 (更严格，从30降低)")
                print("  rsi_overbought: 75 (更严格，从70提高)")
                print("  stop_loss_pct: 0.015 (收紧止损，从2%降低到1.5%)")
                print("  take_profit_pct: 0.04 (提高止盈，从3%提高到4%)")
            elif win_rate > 60:
                print("针对高胜率的参数调整：")
                print("  rsi_oversold: 35 (放宽条件，从30提高)")
                print("  rsi_overbought: 65 (放宽条件，从70降低)")
                print("  stop_loss_pct: 0.02 (保持)")
                print("  take_profit_pct: 0.05 (大幅提高，从3%提高到5%)")
            else:
                print("当前参数表现均衡，建议微调：")
                print("  rsi_oversold: 28-32")
                print("  rsi_overbought: 68-72")
                print("  stop_loss_pct: 0.018-0.022")
                print("  take_profit_pct: 0.035-0.045")

        return suggestions

    def save_analysis_report(self, output_dir: str = 'reports'):
        """保存分析报告"""
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        report_file = os.path.join(output_dir, f'backtest_analysis_{timestamp}.txt')

        # 重定向输出到文件
        from contextlib import redirect_stdout

        with open(report_file, 'w', encoding='utf-8') as f:
            with redirect_stdout(f):
                print(f"{'='*80}")
                print("回测分析报告")
                print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*80}\n")

                self.analyze_capital_curve()
                self.analyze_trades()
                self.analyze_indicators()
                self.find_profit_loss_points()
                self.generate_optimization_suggestions()

        print(f"\n📄 分析报告已保存: {report_file}")
        return report_file

def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("\n使用方法: python analyze_backtest.py <kline_log_file.csv>")
        print("示例: python analyze_backtest.py logs/backtest_klines_20260201_120000.csv")

        # 尝试查找最新的日志文件
        log_dir = 'logs'
        if os.path.exists(log_dir):
            log_files = [f for f in os.listdir(log_dir) if f.startswith('backtest_klines_') and f.endswith('.csv')]
            if log_files:
                log_files.sort(reverse=True)
                latest_log = os.path.join(log_dir, log_files[0])
                print(f"\n找到最新日志文件: {latest_log}")
                print("是否分析此文件？(y/n): ", end='')

                choice = input().lower()
                if choice == 'y':
                    kline_log_file = latest_log
                else:
                    return
            else:
                print("\n未找到日志文件")
                return
        else:
            print("\n日志目录不存在")
            return
    else:
        kline_log_file = sys.argv[1]

    print("\n" + "="*80)
    print("🔬 回测日志分析工具")
    print("="*80)

    # 创建分析器
    analyzer = BacktestAnalyzer(kline_log_file)

    # 加载日志
    if not analyzer.load_logs():
        return

    # 执行分析
    analyzer.analyze_capital_curve()
    analyzer.analyze_trades()
    analyzer.analyze_indicators()
    analyzer.find_profit_loss_points()
    analyzer.generate_optimization_suggestions()

    # 保存报告
    analyzer.save_analysis_report()

    print("\n✅ 分析完成！")

if __name__ == '__main__':
    main()
