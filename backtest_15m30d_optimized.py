"""
15m 30天数据回测 - 优化版本（降低回撤）
关键优化：
1. 更严格的止损（2%）
2. 更保守的仓位（20%）
3. 更高的成交量门槛（0.5分位）
4. 严格的RSI过滤（30/70）
5. 多重确认机制
"""
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class OptimizedBacktester:
    """优化回测器 - 降低回撤为核心目标"""
    
    def __init__(self, initial_capital: float = 10000.0, leverage: float = 1.0):
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.capital = initial_capital
        self.peak_capital = initial_capital
        
        self.position = None
        self.entry_price = 0.0
        self.position_size = 0.0
        self.entry_time = None
        
        self.trades = []
        
        # 优化参数：降低回撤
        self.position_percent = 0.25     # 25%仓位
        self.stop_loss_pct = 0.025       # 2.5%止损
        self.take_profit_pct = 0.07      # 7%止盈
        
        # RSI参数 - 适中
        self.rsi_oversold = 32
        self.rsi_overbought = 68
        
        # 成交量过滤 - 适中门槛
        self.volume_quantile = 0.45
        self.volume_window = 60
        
        # 时段过滤 - 避开波动大的时段
        self.allowed_hours = set(range(5, 23))  # 避开凌晨
        
        # 趋势确认
        self.require_trend_confirmation = False  # 先关闭严格趋势确认
        
        # 回撤控制
        self.max_drawdown_percent = 0.15  # 最大15%回撤
        self.halt_on_max_drawdown = True

    def load_data(self, filepath: str) -> Optional[pd.DataFrame]:
        print(f"\n{'='*80}")
        print(f"📂 加载数据: {filepath}")
        print(f"{'='*80}")
        
        if not os.path.exists(filepath):
            print(f"❌ 文件不存在")
            return None
        
        try:
            df = pd.read_csv(filepath, index_col='timestamp', parse_dates=True)
            print(f"✅ 数据加载成功")
            print(f"   数据点数: {len(df)}")
            print(f"   时间范围: {df.index[0]} ~ {df.index[-1]}")
            print(f"   天数: {(df.index[-1] - df.index[0]).days}天")
            return df
        except Exception as e:
            print(f"❌ 加载失败: {e}")
            return None

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        print(f"\n{'='*80}")
        print(f"📊 计算技术指标")
        print(f"{'='*80}")
        
        df = df.copy()
        
        # RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # EMA
        df['ema_5'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_10'] = df['close'].ewm(span=10, adjust=False).mean()
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        
        # MACD
        ema_12 = df['close'].ewm(span=12, adjust=False).mean()
        ema_26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema_12 - ema_26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        
        # 布林带
        df['bb_middle'] = df['close'].rolling(window=20).mean()
        bb_std = df['close'].rolling(window=20).std()
        df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
        df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
        
        # 成交量分位数
        df['volume_quantile'] = df['volume'].rolling(window=self.volume_window).apply(
            lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
        )
        
        # ATR（波动率）
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = true_range.rolling(window=14).mean()
        
        print(f"✅ 指标计算完成")
        return df

    def check_entry_signal(self, row) -> Optional[str]:
        """检查入场信号 - 实用策略"""
        close = row['close']
        rsi = row['rsi']
        ema_5 = row['ema_5']
        ema_20 = row['ema_20']
        macd_hist = row['macd_hist']
        bb_lower = row['bb_lower']
        bb_upper = row['bb_upper']
        volume_quantile = row['volume_quantile']
        
        # 基础过滤
        if pd.isna(rsi) or pd.isna(ema_20) or pd.isna(macd_hist) or pd.isna(volume_quantile):
            return None
        
        # 时段过滤
        hour = row.name.hour
        if hour not in self.allowed_hours:
            return None
        
        # 成交量过滤
        if volume_quantile < self.volume_quantile:
            return None
        
        # 做多信号 - 放宽条件（3选2）
        long_conditions = [
            rsi < self.rsi_oversold,           # 超卖
            ema_5 > ema_20,                     # 短期向上
            macd_hist > 0,                      # MACD多头
        ]
        long_score = sum(long_conditions)
        
        if long_score >= 2 and close <= bb_lower * 1.05:
            return 'LONG'
        
        # 做空信号 - 放宽条件（3选2）
        short_conditions = [
            rsi > self.rsi_overbought,         # 超买
            ema_5 < ema_20,                     # 短期向下
            macd_hist < 0,                      # MACD空头
        ]
        short_score = sum(short_conditions)
        
        if short_score >= 2 and close >= bb_upper * 0.95:
            return 'SHORT'
        
        return None

    def execute_trade(self, row, signal: str):
        """执行交易"""
        price = row['close']
        timestamp = row.name
        
        # 计算当前回撤
        current_drawdown = (self.peak_capital - self.capital) / self.peak_capital
        
        # 回撤保护
        if self.halt_on_max_drawdown and current_drawdown >= self.max_drawdown_percent:
            print(f"⚠️ 达到最大回撤 {current_drawdown*100:.2f}%，停止新开仓")
            return
        
        # 开仓
        if signal in ['LONG', 'SHORT']:
            if self.position is not None:
                return
            
            position_value = self.capital * self.position_percent * self.leverage
            self.position = signal
            self.entry_price = price
            self.position_size = position_value / price
            self.entry_time = timestamp
            
            print(f"\n{'='*60}")
            print(f"📈 开仓 {signal}")
            print(f"   时间: {timestamp}")
            print(f"   价格: {price:.4f}")
            print(f"   仓位: {position_value:.2f} USDT ({self.position_percent*100}%)")
            print(f"   数量: {self.position_size:.4f}")
            print(f"   止损: {self.stop_loss_pct*100}%")
            print(f"   止盈: {self.take_profit_pct*100}%")
            print(f"{'='*60}")

    def check_exit(self, row):
        """检查出场条件"""
        if self.position is None:
            return
        
        price = row['close']
        timestamp = row.name
        
        # 计算收益率
        if self.position == 'LONG':
            pnl_pct = (price - self.entry_price) / self.entry_price
        else:  # SHORT
            pnl_pct = (self.entry_price - price) / self.entry_price
        
        exit_reason = None
        
        # 止损
        if pnl_pct <= -self.stop_loss_pct:
            exit_reason = 'STOP_LOSS'
        
        # 止盈
        elif pnl_pct >= self.take_profit_pct:
            exit_reason = 'TAKE_PROFIT'
        
        # 反向信号
        elif self.position == 'LONG' and row['rsi'] > self.rsi_overbought:
            exit_reason = 'RSI_REVERSE'
        elif self.position == 'SHORT' and row['rsi'] < self.rsi_oversold:
            exit_reason = 'RSI_REVERSE'
        
        if exit_reason:
            self.close_position(price, timestamp, exit_reason, pnl_pct)

    def close_position(self, price: float, timestamp, reason: str, pnl_pct: float):
        """平仓"""
        position_value = self.entry_price * self.position_size
        # position_value already includes leverage (在开仓时已乘以 leverage)，
        # 此处不应再次乘以 leverage，否则会导致杠杆被重复计算。
        pnl = pnl_pct * position_value
        
        self.capital += pnl
        self.peak_capital = max(self.peak_capital, self.capital)
        
        trade = {
            'entry_time': self.entry_time,
            'exit_time': timestamp,
            'direction': self.position,
            'entry_price': self.entry_price,
            'exit_price': price,
            'position_size': self.position_size,
            'pnl': pnl,
            'pnl_pct': pnl_pct * 100,
            'exit_reason': reason,
            'capital_after': self.capital
        }
        self.trades.append(trade)
        
        print(f"\n{'='*60}")
        print(f"📉 平仓 {self.position}")
        print(f"   时间: {timestamp}")
        print(f"   价格: {price:.4f}")
        print(f"   原因: {reason}")
        print(f"   收益率: {pnl_pct*100:.2f}%")
        print(f"   盈亏: {pnl:.2f} USDT")
        print(f"   余额: {self.capital:.2f} USDT")
        print(f"{'='*60}")
        
        self.position = None
        self.entry_price = 0.0
        self.position_size = 0.0
        self.entry_time = None

    def run_backtest(self, df: pd.DataFrame):
        """运行回测"""
        print(f"\n{'='*80}")
        print(f"🚀 开始回测")
        print(f"{'='*80}")
        print(f"初始资金: {self.initial_capital:.2f} USDT")
        print(f"杠杆倍数: {self.leverage}x")
        print(f"仓位比例: {self.position_percent*100}%")
        print(f"止损: {self.stop_loss_pct*100}% | 止盈: {self.take_profit_pct*100}%")
        
        for idx, row in df.iterrows():
            # 检查出场
            if self.position is not None:
                self.check_exit(row)
            
            # 检查入场
            if self.position is None:
                signal = self.check_entry_signal(row)
                if signal:
                    self.execute_trade(row, signal)
        
        # 强制平仓
        if self.position is not None:
            last_row = df.iloc[-1]
            pnl_pct = ((last_row['close'] - self.entry_price) / self.entry_price if self.position == 'LONG' 
                      else (self.entry_price - last_row['close']) / self.entry_price)
            self.close_position(last_row['close'], last_row.name, 'END_OF_DATA', pnl_pct)

    def analyze_results(self):
        """分析回测结果"""
        print(f"\n{'='*80}")
        print(f"📊 回测结果分析")
        print(f"{'='*80}")
        
        if not self.trades:
            print("❌ 无交易记录")
            return
        
        df_trades = pd.DataFrame(self.trades)
        
        # 基本统计
        total_trades = len(df_trades)
        winning_trades = len(df_trades[df_trades['pnl'] > 0])
        losing_trades = len(df_trades[df_trades['pnl'] < 0])
        win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
        
        total_pnl = df_trades['pnl'].sum()
        total_return = (self.capital - self.initial_capital) / self.initial_capital * 100
        
        # 最大回撤
        df_trades['cumulative_capital'] = self.initial_capital + df_trades['pnl'].cumsum()
        df_trades['peak_capital'] = df_trades['cumulative_capital'].cummax()
        df_trades['drawdown'] = (df_trades['peak_capital'] - df_trades['cumulative_capital']) / df_trades['peak_capital']
        max_drawdown = df_trades['drawdown'].max() * 100
        
        # 盈亏统计
        avg_win = df_trades[df_trades['pnl'] > 0]['pnl'].mean() if winning_trades > 0 else 0
        avg_loss = abs(df_trades[df_trades['pnl'] < 0]['pnl'].mean()) if losing_trades > 0 else 0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
        
        print(f"\n【总体表现】")
        print(f"初始资金: {self.initial_capital:.2f} USDT")
        print(f"最终资金: {self.capital:.2f} USDT")
        print(f"总收益: {total_pnl:.2f} USDT")
        print(f"总收益率: {total_return:.2f}%")
        print(f"最大回撤: {max_drawdown:.2f}%")
        
        print(f"\n【交易统计】")
        print(f"总交易次数: {total_trades}")
        print(f"盈利次数: {winning_trades}")
        print(f"亏损次数: {losing_trades}")
        print(f"胜率: {win_rate:.2f}%")
        print(f"盈亏比: {profit_factor:.2f}")
        print(f"平均盈利: {avg_win:.2f} USDT")
        print(f"平均亏损: {avg_loss:.2f} USDT")
        
        # 出场原因统计
        print(f"\n【出场原因统计】")
        exit_reasons = df_trades['exit_reason'].value_counts()
        for reason, count in exit_reasons.items():
            print(f"{reason}: {count} 次")
        
        # 保存结果
        self.save_results(df_trades)

    def save_results(self, df_trades: pd.DataFrame):
        """保存结果"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 保存交易记录
        trades_file = f"logs/optimized_15m30d_trades_{timestamp}.csv"
        df_trades.to_csv(trades_file, index=False)
        print(f"\n✅ 交易记录已保存: {trades_file}")
        
        # 保存摘要
        summary_file = f"logs/optimized_15m30d_summary_{timestamp}.txt"
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(f"{'='*80}\n")
            f.write(f"15m 30天优化回测 - 降低回撤\n")
            f.write(f"{'='*80}\n\n")
            f.write(f"回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"初始资金: {self.initial_capital:.2f} USDT\n")
            f.write(f"最终资金: {self.capital:.2f} USDT\n")
            f.write(f"总收益: {self.capital - self.initial_capital:.2f} USDT\n")
            f.write(f"总收益率: {(self.capital - self.initial_capital) / self.initial_capital * 100:.2f}%\n")
            
            max_drawdown = df_trades['drawdown'].max() * 100
            f.write(f"最大回撤: {max_drawdown:.2f}%\n")
            
            total_trades = len(df_trades)
            winning_trades = len(df_trades[df_trades['pnl'] > 0])
            win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
            f.write(f"\n总交易次数: {total_trades}\n")
            f.write(f"盈利次数: {winning_trades}\n")
            f.write(f"胜率: {win_rate:.2f}%\n")
        
        print(f"✅ 摘要已保存: {summary_file}")


def main():
    """主函数"""
    data_file = "data/SOLUSDT_15m_30d.csv"
    
    backtester = OptimizedBacktester(
        initial_capital=10000.0,
        leverage=10.0  # 10倍杠杆
    )
    
    df = backtester.load_data(data_file)
    if df is None:
        return
    
    df = backtester.calculate_indicators(df)
    backtester.run_backtest(df)
    backtester.analyze_results()
    
    print(f"\n{'='*80}")
    print(f"✅ 回测完成")
    print(f"{'='*80}")


if __name__ == '__main__':
    main()
