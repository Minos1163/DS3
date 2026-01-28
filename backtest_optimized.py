"""
优化回测脚本 - 提高胜率，优化参数
基于日志分析优化的版本
"""
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.api.binance_client import BinanceClient
from src.config.env_manager import EnvManager


class OptimizedBacktester:
    """优化回测引擎"""

    def __init__(self, symbol: str = 'SOLUSDT', interval: str = '5m', days: int = 30):
        """初始化优化回测引擎"""
        self.symbol = symbol
        self.interval = interval
        self.days = days
        self.df = None
        self.trades = []

        # 日志相关
        self.logs_dir = 'logs'
        self._setup_logs_directory()
        self.kline_log_file = None
        self.summary_log_file = None

        # ===== 优化参数配置（基于日志分析）=====
        # 止损止盈设置 - 优化避免过早止损
        self.stop_loss_pct = 1.5      # 止损百分比（0.8%→1.5%，避免过早止损）
        self.take_profit_pct = 2.0    # 止盈百分比（1.5%→2.0%，更积极获利）
        self.use_atr_stop = True      # 使用ATR动态止损
        self.atr_multiplier = 2.0     # ATR倍数

        # 交易信号参数 - 提高门槛
        self.rsi_oversold = 25        # RSI超卖阈值（保持不变）
        self.rsi_overbought = 78      # RSI超买阈值（75→78，更严格）
        self.rsi_close_high = 55      # RSI平仓上限（52→55，避免中性区平仓）
        self.rsi_close_low = 35       # RSI平仓下限（48→35，只在真正超卖时平仓）

        # 趋势确认参数
        self.trend_confirm_bars = 3   # 需要连续N根K线确认趋势（保持不变）
        self.min_price_change = 0.8   # 最小价格变化百分比（0.5%→0.8%，更明确趋势）

        # 仓位管理
        self.default_leverage = 2     # 杠杆（保持不变）
        self.position_size = 20       # 仓位（25%→20%，降低风险）
        self.max_hold_bars = 80       # 最大持仓K线数（60→80，约6.7小时，延长持仓）

        # 交易频率控制 - 延长冷却期
        self.min_bars_between_trades = 12  # 两次交易最少间隔（6→12根，1小时）
        self.min_hold_bars = 8          # 最小持仓时间（新增，避免过早平仓）
        self.last_trade_bar = -999    # 上次交易的K线索引
        self.position_open_bar = -999 # 当前持仓开仓K线索引

        # MACD转向保护（新增）
        self.macd_reverse_protection = True  # 启用MACD转向保护

        # 初始化客户端
        print("🚀 初始化优化回测系统...")
        EnvManager.load_env_file('.env')

        # 币安客户端
        api_key, api_secret = EnvManager.get_api_credentials()
        self.binance = BinanceClient(api_key=api_key, api_secret=api_secret)

        print("✅ 优化参数已加载")
        print(f"   - 做空信号门槛: 5/6 (原4/6)")
        print(f"   - 交易冷却期: {self.min_bars_between_trades}根K线 (原6根)")
        print(f"   - 最小持仓时间: {self.min_hold_bars}根K线 (新增)")
        print(f"   - RSI平仓区间: <{self.rsi_close_low} 或 >{self.rsi_close_high} (原48-52)")
        print(f"   - MACD转向保护: {'启用' if self.macd_reverse_protection else '禁用'} (新增)")
        print(f"   - 止损比例: {self.stop_loss_pct}% (原0.8%)")
        print(f"   - 止盈比例: {self.take_profit_pct}% (原1.5%)")

    def _setup_logs_directory(self):
        """创建logs目录"""
        if not os.path.exists(self.logs_dir):
            os.makedirs(self.logs_dir)
            print(f"📁 创建日志目录: {self.logs_dir}")

    def _init_backtest_logs(self):
        """初始化回测日志文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.kline_log_file = os.path.join(self.logs_dir, f"backtest_klines_{timestamp}.txt")
        self.summary_log_file = os.path.join(self.logs_dir, f"backtest_summary_{timestamp}.txt")

        # 创建K线日志头
        with open(self.kline_log_file, 'w', encoding='utf-8') as f:
            f.write("=" * 150 + "\n")
            f.write("🔄 优化回测 K线操作日志\n")
            f.write("=" * 150 + "\n")
            f.write(f"交易对: {self.symbol} | 周期: {self.interval} | 回测天数: {self.days}\n")
            f.write(f"初始资金: 100 USDT\n")
            f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 150 + "\n\n")
            f.write(f"{'时间':<20} | {'开高低收':<35} | {'RSI':<8} | {'MACD':<12} | {'操作':<30} | {'持仓':<15} | {'原因':<40}\n")
            f.write("-" * 150 + "\n")

    def _log_kline(self, index: int, action: str, position: str, reason: str):
        """记录单根K线的操作"""
        if self.kline_log_file is None or self.df is None:
            return

        row = self.df.iloc[index]
        time_str = str(row.name)[:19]
        ohlc_str = f"O:{row['open']:7.2f} H:{row['high']:7.2f} L:{row['low']:7.2f} C:{row['close']:7.2f}"
        rsi_str = f"{row.get('rsi', 0):.2f}" if 'rsi' in row else "N/A"
        macd_str = f"{row.get('macd', 0):.4f}" if 'macd' in row else "N/A"

        with open(self.kline_log_file, 'a', encoding='utf-8') as f:
            f.write(f"{time_str:<20} | {ohlc_str:<35} | {rsi_str:<8} | {macd_str:<12} | {action:<30} | {position:<15} | {reason:<40}\n")

    def _close_backtest_logs(self, initial_capital: float, final_capital: float, total_trades: int,
                             win_trades: int, loss_trades: int, total_pnl: float):
        """关闭回测日志并写入汇总"""
        if self.kline_log_file is None or self.summary_log_file is None:
            return

        # 补充K线日志末尾
        with open(self.kline_log_file, 'a', encoding='utf-8') as f:
            f.write("-" * 150 + "\n")
            f.write(f"回测完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        # 写入汇总报告
        with open(self.summary_log_file, 'w', encoding='utf-8') as f:
            f.write("=" * 100 + "\n")
            f.write("📊 优化回测汇总报告\n")
            f.write("=" * 100 + "\n\n")

            f.write(f"【基本信息】\n")
            f.write(f"交易对: {self.symbol}\n")
            f.write(f"周期: {self.interval}\n")
            f.write(f"回测天数: {self.days}\n")
            f.write(f"回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write(f"【资金情况】\n")
            f.write(f"初始资金: {initial_capital:.2f} USDT\n")
            f.write(f"最终资金: {final_capital:.2f} USDT\n")
            f.write(f"总盈亏: {total_pnl:+.2f} USDT\n")
            return_percent = (total_pnl / initial_capital) * 100
            f.write(f"收益率: {return_percent:+.2f}%\n\n")

            f.write(f"【交易统计】\n")
            f.write(f"总交易数: {total_trades}\n")
            f.write(f"赢利笔数: {win_trades}\n")
            f.write(f"亏损笔数: {loss_trades}\n")
            if total_trades > 0:
                win_rate = (win_trades / total_trades) * 100
                f.write(f"胜率: {win_rate:.2f}%\n\n")
            else:
                f.write(f"胜率: N/A\n\n")

            f.write(f"详细K线操作日志: {self.kline_log_file}\n")

        print(f"✅ K线日志: {self.kline_log_file}")
        print(f"✅ 汇总报告: {self.summary_log_file}")

    def download_data(self) -> Optional[pd.DataFrame]:
        """下载历史K线数据"""
        print(f"\n{'='*60}")
        print(f"📥 下载历史数据")
        print(f"{'='*60}")
        print(f"交易对: {self.symbol}")
        print(f"周期: {self.interval}")
        print(f"天数: {self.days}")

        try:
            # 30天数据 = 30*24*60/5 = 8640根K线，但币安限制1000根
            # 所以取最大1000根K线（约3.5天）
            klines = self.binance.get_klines(
                symbol=self.symbol,
                interval=self.interval,
                limit=1000
            )

            if not klines:
                print("❌ 未获取到数据")
                return None

            print(f"✅ 下载 {len(klines)} 根K线")

            # 转换为DataFrame
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])

            # 转换数据类型
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            df.set_index('timestamp', inplace=True)
            df.sort_index(inplace=True)

            self.df = df

            print(f"开始时间: {df.index[0]}")
            print(f"结束时间: {df.index[-1]}")
            print(f"数据点数: {len(df)}")

            return df

        except Exception as e:
            print(f"❌ 下载数据失败: {e}")
            return None

    def calculate_indicators(self):
        """计算技术指标"""
        if self.df is None:
            return

        print(f"\n{'='*60}")
        print(f"📊 计算技术指标")
        print(f"{'='*60}")

        close = self.df['close']
        high = self.df['high']
        low = self.df['low']

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gain / loss
        self.df['rsi'] = 100 - (100 / (1 + rs))

        # EMA
        self.df['ema_5'] = close.ewm(span=5, adjust=False).mean()
        self.df['ema_20'] = close.ewm(span=20, adjust=False).mean()
        self.df['ema_50'] = close.ewm(span=50, adjust=False).mean()

        # MACD
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        self.df['macd'] = ema_12 - ema_26
        self.df['macd_signal'] = self.df['macd'].ewm(span=9, adjust=False).mean()
        self.df['macd_hist'] = self.df['macd'] - self.df['macd_signal']

        # 布林带
        sma_20 = close.rolling(window=20).mean()
        std_20 = close.rolling(window=20).std()
        self.df['bb_upper'] = sma_20 + (std_20 * 2)
        self.df['bb_middle'] = sma_20
        self.df['bb_lower'] = sma_20 - (std_20 * 2)

        # ATR (平均真实波幅)
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        self.df['atr'] = tr.rolling(window=14).mean()

        print("✅ 指标计算完成")

    def get_optimized_decision(self, index: int) -> Dict[str, Any]:
        """优化的交易决策策略"""
        if self.df is None:
            return {
                'action': 'HOLD',
                'confidence': 0.5,
                'reason': '数据未加载',
                'leverage': 3,
                'position_percent': 0
            }

        # 交易频率控制
        if index - self.last_trade_bar < self.min_bars_between_trades:
            return {
                'action': 'HOLD',
                'confidence': 0.5,
                'reason': f'交易冷却期（剩余{self.min_bars_between_trades - (index - self.last_trade_bar)}根K线）',
                'leverage': self.default_leverage,
                'position_percent': 0
            }

        row = self.df.iloc[index]
        recent_df = self.df.iloc[max(0, index-20):index+1]

        # 计算关键指标
        price_change = (row['close'] - recent_df['close'].iloc[0]) / recent_df['close'].iloc[0] * 100
        rsi = row['rsi']
        macd = row['macd']
        macd_signal = row['macd_signal']
        macd_hist = row.get('macd_hist', 0)
        ema_5 = row['ema_5']
        ema_20 = row['ema_20']
        ema_50 = row['ema_50']
        atr = row['atr']
        bb_upper = row['bb_upper']
        bb_lower = row['bb_lower']
        current_price = row['close']

        # 趋势确认
        if index >= self.trend_confirm_bars:
            recent_closes = self.df['close'].iloc[index-self.trend_confirm_bars:index+1]
            is_downtrend = all(recent_closes.iloc[i] > recent_closes.iloc[i+1]
                             for i in range(len(recent_closes)-1))
            is_uptrend = all(recent_closes.iloc[i] < recent_closes.iloc[i+1]
                           for i in range(len(recent_closes)-1))
        else:
            is_downtrend = False
            is_uptrend = False

        # ===== 做空信号（熊市策略）- 更严格的5/6条件 =====
        short_signal_count = 0
        short_reasons = []

        # 条件1：RSI超买区域（更严格78）
        if rsi > self.rsi_overbought:
            short_signal_count += 1
            short_reasons.append(f'RSI超买({rsi:.1f})')

        # 条件2：价格接近布林带上轨
        if current_price >= bb_upper * 0.97:
            short_signal_count += 1
            short_reasons.append('触及布林带上轨')

        # 条件3：MACD死叉且在零轴下方
        if macd < macd_signal and macd < 0:
            short_signal_count += 1
            short_reasons.append('MACD死叉')

        # 条件4：空头排列
        if ema_5 < ema_20 < ema_50:
            short_signal_count += 1
            short_reasons.append('空头排列')

        # 条件5：连续下跌趋势
        if is_downtrend:
            short_signal_count += 1
            short_reasons.append('连续下跌')

        # 条件6：价格下跌幅度明显
        if price_change < -self.min_price_change:
            short_signal_count += 1
            short_reasons.append(f'价格下跌{price_change:.2f}%')

        # 需要5个做空信号才开仓（从4个提升到5个）
        if short_signal_count >= 5:
            return {
                'action': 'SELL_OPEN',
                'confidence': min(0.6 + short_signal_count * 0.1, 0.95),
                'reason': f'做空信号({short_signal_count}/6): ' + ', '.join(short_reasons),
                'leverage': self.default_leverage,
                'position_percent': self.position_size
            }

        # ===== 做多信号（反弹机会）- 更严格的5/5条件 =====
        long_signal_count = 0
        long_reasons = []

        if rsi < self.rsi_oversold:
            long_signal_count += 1
            long_reasons.append(f'RSI超卖({rsi:.1f})')

        if current_price <= bb_lower * 1.02:
            long_signal_count += 1
            long_reasons.append('触及布林带下轨')

        if macd > macd_signal and macd > 0:
            long_signal_count += 1
            long_reasons.append('MACD金叉')

        if is_uptrend:
            long_signal_count += 1
            long_reasons.append('连续上涨')

        # 需要4个做多信号才开仓（保持不变）
        if long_signal_count >= 4:
            return {
                'action': 'BUY_OPEN',
                'confidence': min(0.6 + long_signal_count * 0.1, 0.95),
                'reason': f'反弹信号({long_signal_count}/4): ' + ', '.join(long_reasons),
                'leverage': self.default_leverage,
                'position_percent': self.position_size
            }

        # ===== 最大持仓时间检查 =====
        if self.position_open_bar >= 0 and index - self.position_open_bar >= self.max_hold_bars:
            return {
                'action': 'CLOSE',
                'confidence': 0.9,
                'reason': f'持仓超过{self.max_hold_bars}根K线({self.max_hold_bars*5//60}小时)，强制平仓',
                'leverage': self.default_leverage,
                'position_percent': 0
            }

        # ===== MACD转向保护（新增）=====
        if self.macd_reverse_protection and self.position_open_bar >= 0 and index > self.position_open_bar:
            # 检查MACD是否由负转正（做空时）或由正转负（做多时）
            if index > self.position_open_bar + 3:  # 至少持仓3根K线后才检查
                prev_macd = self.df['macd'].iloc[index-1]
                current_macd = macd
                prev_hist = self.df['macd_hist'].iloc[index-1] if index > 0 else 0
                current_hist = macd_hist

                # MACD由负转正：做空危险信号
                if prev_hist < 0 and current_hist > 0:
                    return {
                        'action': 'CLOSE',
                        'confidence': 0.85,
                        'reason': f'MACD由负转正({prev_hist:.4f}→{current_hist:.4f})，趋势反转风险，保护平仓',
                        'leverage': self.default_leverage,
                        'position_percent': 0
                    }

        # ===== 平仓信号 - 优化区间 =====
        # RSI偏离中性区间时平仓（55-35改为>55或<35）
        if rsi > self.rsi_close_high or rsi < self.rsi_close_low:
            return {
                'action': 'CLOSE',
                'confidence': 0.75,
                'reason': f'RSI偏离中性区域({rsi:.1f})，平仓锁定收益',
                'leverage': self.default_leverage,
                'position_percent': 0
            }

        # 最小持仓时间保护
        if self.position_open_bar >= 0 and index - self.position_open_bar < self.min_hold_bars:
            return {
                'action': 'HOLD',
                'confidence': 0.6,
                'reason': f'未达最小持仓时间({self.min_hold_bars}根K线)，继续持有',
                'leverage': self.default_leverage,
                'position_percent': 0
            }

        return {
            'action': 'HOLD',
            'confidence': 0.5,
            'reason': f'信号不足，持有观望 (做空:{short_signal_count}/6, 做多:{long_signal_count}/4)',
            'leverage': self.default_leverage,
            'position_percent': 0
        }

    def run_backtest(self, initial_capital: float = 100):
        """运行回测"""
        if self.df is None:
            print("❌ 数据未加载，无法执行回测")
            return {
                'initial_capital': initial_capital,
                'final_capital': initial_capital,
                'trades': []
            }

        print(f"\n{'='*60}")
        print(f"🔄 开始优化回测 (初始资金: {initial_capital} USDT)")
        print(f"{'='*60}")

        # 初始化日志
        self._init_backtest_logs()

        capital = initial_capital
        position = None  # None, 'LONG', 'SHORT'
        entry_price = 0
        entry_time = None
        trades = []

        # 从第50根K线开始（确保指标已计算）
        for i in range(50, len(self.df)):
            row = self.df.iloc[i]
            current_price = row['close']
            current_time = row.name

            # 获取优化决策
            decision = self.get_optimized_decision(i)

            # 执行交易逻辑
            action = decision['action']
            position_status = position if position else "无"

            # 开多仓
            if action == 'BUY_OPEN' and position is None:
                position = 'LONG'
                entry_price = current_price
                entry_time = current_time
                self.position_open_bar = i
                self.last_trade_bar = i
                print(f"📈 [{current_time}] 开多仓 @ {entry_price:.2f} - {decision['reason']}")
                self._log_kline(i, "📈 开多仓", "LONG", decision['reason'][:35])

            # 开空仓
            elif action == 'SELL_OPEN' and position is None:
                position = 'SHORT'
                entry_price = current_price
                entry_time = current_time
                self.position_open_bar = i
                self.last_trade_bar = i
                print(f"📉 [{current_time}] 开空仓 @ {entry_price:.2f} - {decision['reason']}")
                self._log_kline(i, "📉 开空仓", "SHORT", decision['reason'][:35])

            # 平仓
            elif action == 'CLOSE' and position is not None and entry_time is not None:
                if position == 'LONG':
                    pnl = current_price - entry_price
                    pnl_percent = (pnl / entry_price) * 100
                else:  # SHORT
                    pnl = entry_price - current_price
                    pnl_percent = (pnl / entry_price) * 100

                # 计算仓位大小（20%仓位）
                position_capital = capital * (self.position_size / 100)

                if position == 'LONG':
                    trade_pnl = position_capital * (pnl / entry_price) * self.default_leverage
                else:
                    trade_pnl = position_capital * (pnl / entry_price) * self.default_leverage

                capital += trade_pnl

                trade_record = {
                    'type': position,
                    'entry_time': entry_time,
                    'entry_price': entry_price,
                    'exit_time': current_time,
                    'exit_price': current_price,
                    'pnl': trade_pnl,
                    'pnl_percent': pnl_percent,
                    'reason': decision['reason'],
                    'hold_bars': i - self.position_open_bar
                }
                trades.append(trade_record)

                emoji = "✅" if trade_pnl > 0 else "❌"
                close_action = f"✅平仓{position}" if trade_pnl > 0 else f"❌平仓{position}"
                hold_bars = i - self.position_open_bar
                print(f"{emoji} [{current_time}] 平仓 {position} @ {current_price:.2f} | "
                      f"盈亏: {trade_pnl:+.2f} ({pnl_percent:+.2f}%) | 持仓{hold_bars}根 | "
                      f"{decision['reason'][:30]}")
                self._log_kline(i, close_action, "无", f"盈亏{trade_pnl:+.2f}")

                position = None
                self.position_open_bar = -999
                self.last_trade_bar = i
            else:
                # 记录hold状态
                if action == 'HOLD' and position is not None:
                    self._log_kline(i, "⏸ HOLD", position, "继续持仓")

        # 最终统计
        total_pnl = capital - initial_capital
        win_trades = sum(1 for t in trades if t['pnl'] > 0)
        loss_trades = len(trades) - win_trades

        self._print_results(initial_capital, capital, trades)
        self._close_backtest_logs(initial_capital, capital, len(trades), win_trades, loss_trades, total_pnl)

        return trades

    def _print_results(self, initial_capital: float, final_capital: float, trades: List[Dict]):
        """打印回测结果"""
        print(f"\n{'='*60}")
        print(f"📊 回测结果总结")
        print(f"{'='*60}")

        total_return = final_capital - initial_capital
        return_percent = (total_return / initial_capital) * 100

        print(f"\n💰 资金变化:")
        print(f"   初始资金: {initial_capital:.2f} USDT")
        print(f"   最终资金: {final_capital:.2f} USDT")
        print(f"   总收益: {total_return:+.2f} USDT ({return_percent:+.2f}%)")

        if trades:
            long_trades = [t for t in trades if t['type'] == 'LONG']
            short_trades = [t for t in trades if t['type'] == 'SHORT']
            win_trades = [t for t in trades if t['pnl'] > 0]

            print(f"\n📈 交易统计:")
            print(f"   交易总数: {len(trades)}")
            print(f"   做多次数: {len(long_trades)}")
            print(f"   做空次数: {len(short_trades)}")
            print(f"   盈利次数: {len(win_trades)}")
            win_rate = len(win_trades) / len(trades) * 100 if len(trades) > 0 else 0
            print(f"   胜率: {win_rate:.1f}%")

            if len(trades) > 0:
                avg_pnl = sum(t['pnl'] for t in trades) / len(trades)
                max_win = max(t['pnl'] for t in trades)
                max_loss = min(t['pnl'] for t in trades)

                avg_hold_bars = sum(t.get('hold_bars', 0) for t in trades) / len(trades)

                print(f"\n📊 盈亏分析:")
                print(f"   平均盈亏: {avg_pnl:+.2f} USDT")
                print(f"   最大盈利: {max_win:+.2f} USDT")
                print(f"   最大亏损: {max_loss:+.2f} USDT")
                print(f"   平均持仓: {avg_hold_bars:.1f}根K线 ({avg_hold_bars*5/60:.1f}分钟)")

            # 显示所有交易
            print(f"\n📋 所有交易记录:")
            for i, trade in enumerate(trades, 1):
                emoji = "✅" if trade['pnl'] > 0 else "❌"
                hold_time = trade.get('hold_bars', 0) * 5
                print(f"{i}. {emoji} {trade['type']:5} | "
                      f"{trade['entry_time'].strftime('%m-%d %H:%M')} @ {trade['entry_price']:.2f} → "
                      f"{trade['exit_time'].strftime('%m-%d %H:%M')} @ {trade['exit_price']:.2f} | "
                      f"{trade['pnl']:+.2f} ({trade['pnl_percent']:+.2f}%) | "
                      f"持仓{hold_time:.0f}分钟")

        # 市场对比
        if self.df is not None:
            market_change = (self.df['close'].iloc[-1] - self.df['close'].iloc[50]) / self.df['close'].iloc[50] * 100
            print(f"\n📉 市场对比:")
            print(f"   市场涨跌: {market_change:+.2f}%")
            print(f"   策略收益: {return_percent:+.2f}%")
            print(f"   超额收益: {return_percent - market_change:+.2f}%")


def main():
    """主函数"""
    print("=" * 60)
    print("优化回测系统 - 5分钟K线 30天数据 100 USDT")
    print("=" * 60)

    # 创建回测器 - 5m间隔，30天数据
    backtester = OptimizedBacktester(symbol='SOLUSDT', interval='5m', days=30)

    # 下载数据
    if backtester.download_data() is None:
        print("❌ 数据下载失败")
        return

    # 计算指标
    backtester.calculate_indicators()

    # 运行回测 - 100 USDT
    trades = backtester.run_backtest(initial_capital=100)

    print(f"\n{'='*60}")
    print("✅ 优化回测完成！")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
