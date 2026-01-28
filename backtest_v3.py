"""
回测系统 V3 - 基于V2的进一步优化
核心改进：
1. 根据持仓方向调整平仓逻辑
2. 避免超低RSI开空仓
3. 优化信号门槛和冷却期
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import os

# 币安SDK
from binance.client import Client
from binance.enums import *


class BacktesterV3:
    def __init__(
        self,
        symbol: str = "SOLUSDT",
        interval: str = "5m",
        days: int = 30,
        api_key: str = None,
        api_secret: str = None,
    ):
        self.symbol = symbol
        self.interval = interval
        self.days = days
        self.df = None
        
        # 币安客户端
        self.client = Client(api_key, api_secret) if api_key and api_secret else None
        
        # ========== 优化参数 V3 ==========
        # 基于V2的回测结果调整
        
        # 信号门槛：从5/6降回4/6（V2门槛太高导致交易量骤降）
        self.short_signal_threshold = 4  # 做空需要4个信号（V2: 5）
        self.long_signal_threshold = 4   # 做多需要4个信号
        
        # 冷却期：从12根降回8根（V2: 12，太保守）
        self.cooldown_bars = 8  # 开仓后冷却8根K线（40分钟）
        
        # 最小持仓时间：从8根增加到10根
        self.min_hold_bars = 10  # 最小持仓10根K线（50分钟）
        self.max_hold_bars = 20  # 最大持仓20根K线（100分钟）- 优化防止长期反向
        
        # RSI参数
        self.rsi_period = 14
        self.rsi_oversold = 30      # 超卖阈值
        self.rsi_overbought = 70     # 超买阈值
        self.rsi_neutral_low = 40    # 中性区下界
        self.rsi_neutral_high = 60   # 中性区上界
        
        # 布林带
        self.bb_period = 20
        self.bb_std = 2
        
        # MACD
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9
        
        # 止损止盈（V4优化：更现实的目标）
        self.stop_loss_percent = 0.8   # 止损0.8%（V3: 1.2%，更紧的保护）
        self.take_profit_percent = 1.2  # 止盈1.2%（V3: 2.5%，更易达到）
        
        # 资金管理
        self.position_size = 0.50    # 每次使用50%资金 (降低风险，防止爆仓)
        self.default_leverage = 3    # 默认杠杆 (3倍，从10倍降低)
        
        # 开仓保护：避免错误位置开仓（V4优化）
        self.min_rsi_for_short = 25  # 做空最小RSI（防止超卖反弹）
        self.max_rsi_for_short = 60  # 做空最大RSI（防止高位开空）- V4新增
        self.min_rsi_for_long = 35   # 做多最小RSI（防止超跌反弹）- V4新增
        self.max_rsi_for_long = 75   # 做多最大RSI（防止超买区开多）
        
        # 平仓时的RSI触发阈值（V4新增）
        self.close_short_rsi = 65   # 做空持仓时，RSI > 65 时平仓（反弹强劲）
        self.close_long_rsi = 35    # 做多持仓时，RSI < 35 时平仓（下跌加强）
        
        # 状态跟踪
        self.position = None  # 'LONG' or 'SHORT'
        self.position_open_bar = -1
        self.position_entry_price = 0
        self.last_close_bar = -1  # 上次平仓的K线索引
        self.balance = 0
        self.trades = []
        
        # 日志
        self.kline_log = []
        self.log_file = None
        self.summary_file = None
    
    def init_logging(self):
        """初始化日志文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # K线操作日志
        self.log_file = f"{log_dir}/backtest_klines_{timestamp}.txt"
        
        # 汇总报告
        self.summary_file = f"{log_dir}/backtest_summary_{timestamp}.txt"
        
        # 初始化K线日志
        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write("=" * 118 + "\n")
            f.write("🔄 优化回测 V3 K线操作日志\n")
            f.write("=" * 118 + "\n")
            f.write(f"交易对: {self.symbol} | 周期: {self.interval} | 回测天数: {self.days}\n")
            f.write(f"初始资金: 100 USDT\n")
            f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 118 + "\n\n")
            f.write("时间                   | 开高低收                                | RSI      | MACD         | 操作                             | 持仓              | 原因                                       \n")
            f.write("-" * 118 + "\n")
    
    def _log_kline(self, index: int, action: str, position: str, reason: str):
        """记录K线操作"""
        if self.df is None or index >= len(self.df):
            return
        
        row = self.df.iloc[index]
        
        ohlc = f"O: {row['open']:.2f} H: {row['high']:.2f} L: {row['low']:.2f} C: {row['close']:.2f}"
        rsi = f"{row['rsi']:.2f}"
        macd = f"{row['macd']:.4f}"
        
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(f"{row.name.strftime('%Y-%m-%d %H:%M:%S')}  | {ohlc:38} | {rsi:8} | {macd:12} | {action:32} | {position:16} | {reason}\n")
    
    def fetch_data(self) -> Optional[pd.DataFrame]:
        """下载历史K线数据"""
        try:
            if self.client:
                print(f"\n{'='*60}")
                print(f"📥 下载历史数据")
                print(f"{'='*60}")
                print(f"交易对: {self.symbol}")
                print(f"周期: {self.interval}")
                print(f"天数: {self.days}")
                
                # 计算需要的K线数量
                # 5分钟K线：一天288根 (24*60/5)，7天约2016根
                # 1小时K线：一天24根，7天168根
                if self.interval == "5m":
                    limit = 3000  # 5分钟K线需要更多数据
                elif self.interval == "1h":
                    limit = 200
                else:
                    limit = 1000
                
                print(f"📥 准备下载 {limit} 根K线...")
                
                klines = self.client.get_historical_klines(
                    symbol=self.symbol,
                    interval=self.interval,
                    limit=limit
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
            else:
                print("❌ 未配置币安API")
                return None
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
        self.df['ema_10'] = close.ewm(span=10, adjust=False).mean()
        self.df['ema_20'] = close.ewm(span=20, adjust=False).mean()
        
        # MACD
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        self.df['macd'] = ema_12 - ema_26
        self.df['macd_signal'] = self.df['macd'].ewm(span=9, adjust=False).mean()
        self.df['macd_hist'] = self.df['macd'] - self.df['macd_signal']
        
        # 布林带
        self.df['bb_middle'] = close.rolling(window=20).mean()
        self.df['bb_std'] = close.rolling(window=20).std()
        self.df['bb_upper'] = self.df['bb_middle'] + 2 * self.df['bb_std']
        self.df['bb_lower'] = self.df['bb_middle'] - 2 * self.df['bb_std']
        
        print(f"✅ 指标计算完成")
    
    def check_short_signals(self, index: int) -> Tuple[int, List[str]]:
        """检查做空信号"""
        if index < 3:
            return 0, []
        
        row = self.df.iloc[index]
        prev_row = self.df.iloc[index-1]
        
        signals = []
        count = 0
        
        # 1. 触及布林带上轨
        if row['high'] >= row['bb_upper'] * 0.995:
            signals.append("触及布林带上轨")
            count += 1
        
        # 2. MACD死叉
        if prev_row['macd'] > prev_row['macd_signal'] and row['macd'] <= row['macd_signal']:
            signals.append("MACD死叉")
            count += 1
        elif row['macd'] < row['macd_signal'] and prev_row['macd_hist'] < 0:
            signals.append("空头排列")
            count += 1
        
        # 3. RSI进入超买区
        if row['rsi'] > 60:
            signals.append("RSI超买")
            count += 1
        elif row['rsi'] > 55:
            signals.append("RSI偏强")
            count += 0.5
        
        # 4. EMA空头排列
        if row['ema_5'] < row['ema_10'] < row['ema_20']:
            signals.append("EMA空头排列")
            count += 1
        elif row['ema_5'] < row['ema_10']:
            signals.append("短期均线下压")
            count += 0.5
        
        # 5. 连续下跌
        if (row['close'] < prev_row['close'] and 
            self.df.iloc[index-2]['close'] > prev_row['close']):
            signals.append("连续下跌")
            count += 1
        elif row['close'] < prev_row['close']:
            price_change = (row['close'] - prev_row['close']) / prev_row['close'] * 100
            if price_change < -0.5:
                signals.append(f"价格下跌{price_change:.2f}%")
                count += 0.5
        
        return min(count, 6), signals
    
    def check_long_signals(self, index: int) -> Tuple[int, List[str]]:
        """检查做多信号"""
        if index < 3:
            return 0, []
        
        row = self.df.iloc[index]
        prev_row = self.df.iloc[index-1]
        
        signals = []
        count = 0
        
        # 1. 触及布林带下轨
        if row['low'] <= row['bb_lower'] * 1.005:
            signals.append("触及布林带下轨")
            count += 1
        
        # 2. MACD金叉
        if prev_row['macd'] < prev_row['macd_signal'] and row['macd'] >= row['macd_signal']:
            signals.append("MACD金叉")
            count += 1
        elif row['macd'] > row['macd_signal'] and prev_row['macd_hist'] > 0:
            signals.append("多头排列")
            count += 1
        
        # 3. RSI进入超卖区
        if row['rsi'] < 40:
            signals.append("RSI超卖")
            count += 1
        elif row['rsi'] < 45:
            signals.append("RSI偏弱")
            count += 0.5
        
        # 4. EMA多头排列
        if row['ema_5'] > row['ema_10'] > row['ema_20']:
            signals.append("EMA多头排列")
            count += 1
        elif row['ema_5'] > row['ema_10']:
            signals.append("短期均线上托")
            count += 0.5
        
        # 5. 连续上涨
        if (row['close'] > prev_row['close'] and 
            self.df.iloc[index-2]['close'] < prev_row['close']):
            signals.append("连续上涨")
            count += 1
        elif row['close'] > prev_row['close']:
            price_change = (row['close'] - prev_row['close']) / prev_row['close'] * 100
            if price_change > 0.5:
                signals.append(f"价格上涨{price_change:.2f}%")
                count += 0.5
        
        return min(count, 6), signals
    
    def make_decision(self, index: int) -> Dict[str, Any]:
        """根据当前指标做出交易决策"""
        if index < 10:
            return {
                'action': 'HOLD',
                'confidence': 0,
                'reason': '数据不足',
                'leverage': self.default_leverage,
                'position_percent': 0
            }
        
        row = self.df.iloc[index]
        prev_row = self.df.iloc[index-1]
        
        rsi = row['rsi']
        macd = row['macd']
        macd_hist = row['macd_hist']
        
        # ===== 止损检查 =====
        if self.position_open_bar >= 0 and index > self.position_open_bar:
            entry_price = self.position_entry_price
            current_price = row['close']
            
            if self.position == 'LONG':
                pnl = (current_price - entry_price) / entry_price * 100
            else:  # SHORT
                pnl = (entry_price - current_price) / entry_price * 100
            
            # 止损
            if pnl <= -self.stop_loss_percent:
                return {
                    'action': 'CLOSE',
                    'confidence': 0.95,
                    'reason': f'触发止损 ({pnl:.2f}%)',
                    'leverage': self.default_leverage,
                    'position_percent': 0
                }
            
            # 止盈
            if pnl >= self.take_profit_percent:
                return {
                    'action': 'CLOSE',
                    'confidence': 0.9,
                    'reason': f'触发止盈 ({pnl:.2f}%)',
                    'leverage': self.default_leverage,
                    'position_percent': 0
                }
        
        # ===== 冷却期检查 =====
        if self.last_close_bar >= 0 and index - self.last_close_bar < self.cooldown_bars:
            return {
                'action': 'HOLD',
                'confidence': 0.5,
                'reason': f'冷却期 ({self.cooldown_bars - (index - self.last_close_bar)}根K线剩余)',
                'leverage': self.default_leverage,
                'position_percent': 0
            }
        
        # ===== 开仓信号 =====
        if self.position is None:
            # 检查做空信号
            short_signal_count, short_reasons = self.check_short_signals(index)
            
            # V3新增：避免超低RSI开空仓
            if rsi < self.min_rsi_for_short and short_signal_count > 0:
                return {
                    'action': 'HOLD',
                    'confidence': 0.3,
                    'reason': f'RSI过低({rsi:.1f})，避免超卖反弹',
                    'leverage': self.default_leverage,
                    'position_percent': 0
                }
            
            # V4新增：避免高位开空仓
            if rsi > self.max_rsi_for_short and short_signal_count > 0:
                return {
                    'action': 'HOLD',
                    'confidence': 0.3,
                    'reason': f'RSI过高({rsi:.1f})，避免在高位做空',
                    'leverage': self.default_leverage,
                    'position_percent': 0
                }
            
            if short_signal_count >= self.short_signal_threshold:
                return {
                    'action': 'SELL_OPEN',
                    'confidence': min(0.6 + short_signal_count * 0.1, 0.95),
                    'reason': f'做空信号({short_signal_count}/6): ' + ', '.join(short_reasons),
                    'leverage': self.default_leverage,
                    'position_percent': self.position_size
                }
            
            # 检查做多信号
            long_signal_count, long_reasons = self.check_long_signals(index)
            
            # V3新增：避免超高RSI开多仓
            if rsi > self.max_rsi_for_long and long_signal_count > 0:
                return {
                    'action': 'HOLD',
                    'confidence': 0.3,
                    'reason': f'RSI过高({rsi:.1f})，避免超买回调',
                    'leverage': self.default_leverage,
                    'position_percent': 0
                }
            
            # V4新增：避免低位做多仓
            if rsi < self.min_rsi_for_long and long_signal_count > 0:
                return {
                    'action': 'HOLD',
                    'confidence': 0.3,
                    'reason': f'RSI过低({rsi:.1f})，避免在低位做多',
                    'leverage': self.default_leverage,
                    'position_percent': 0
                }
            
            if long_signal_count >= self.long_signal_threshold:
                return {
                    'action': 'BUY_OPEN',
                    'confidence': min(0.6 + long_signal_count * 0.1, 0.95),
                    'reason': f'反弹信号({long_signal_count}/6): ' + ', '.join(long_reasons),
                    'leverage': self.default_leverage,
                    'position_percent': self.position_size
                }
        
        # ===== 平仓信号 - V3改进版 =====
        elif self.position is not None:
            hold_bars = index - self.position_open_bar
            
            # 最小持仓时间保护
            if hold_bars < self.min_hold_bars:
                return {
                    'action': 'HOLD',
                    'confidence': 0.6,
                    'reason': f'未达最小持仓时间({self.min_hold_bars}根K线)，继续持有',
                    'leverage': self.default_leverage,
                    'position_percent': 0
                }
            
            # 根据持仓方向使用不同的平仓逻辑
            if self.position == 'SHORT':
                # V4新增：做空持仓中，RSI > 65表示反弹强劲，应该平仓
                if rsi > self.close_short_rsi:
                    return {
                        'action': 'CLOSE',
                        'confidence': 0.85,
                        'reason': f'RSI反弹({rsi:.1f}>{self.close_short_rsi})，做空强制平仓',
                        'leverage': self.default_leverage,
                        'position_percent': 0
                    }
                
                # 做空：RSI>超买时平仓获利，RSI<超卖时平仓止损
                if rsi > self.rsi_overbought or rsi < self.rsi_oversold:
                    action_type = "获利" if rsi > self.rsi_overbought else "止损"
                    return {
                        'action': 'CLOSE',
                        'confidence': 0.8,
                        'reason': f'RSI{rsi:.1f}，做空{action_type}平仓',
                        'leverage': self.default_leverage,
                        'position_percent': 0
                    }
                
                # MACD由负转正：趋势反转
                if hold_bars > 3:
                    prev_hist = self.df['macd_hist'].iloc[index-1]
                    if prev_hist < 0 and macd_hist > 0:
                        return {
                            'action': 'CLOSE',
                            'confidence': 0.85,
                            'reason': f'MACD由负转正({prev_hist:.4f}→{macd_hist:.4f})，趋势反转平仓',
                            'leverage': self.default_leverage,
                            'position_percent': 0
                        }
            
            elif self.position == 'LONG':
                # V4新增：做多持仓中，RSI < 35表示下跌加强，应该平仓
                if rsi < self.close_long_rsi:
                    return {
                        'action': 'CLOSE',
                        'confidence': 0.85,
                        'reason': f'RSI下跌({rsi:.1f}<{self.close_long_rsi})，做多强制平仓',
                        'leverage': self.default_leverage,
                        'position_percent': 0
                    }
                
                # 做多：RSI<超卖时平仓获利，RSI>超买时平仓止损
                if rsi < self.rsi_oversold or rsi > self.rsi_overbought:
                    action_type = "获利" if rsi < self.rsi_oversold else "止损"
                    return {
                        'action': 'CLOSE',
                        'confidence': 0.8,
                        'reason': f'RSI{rsi:.1f}，做多{action_type}平仓',
                        'leverage': self.default_leverage,
                        'position_percent': 0
                    }
                
                # MACD由正转负：趋势反转
                if hold_bars > 3:
                    prev_hist = self.df['macd_hist'].iloc[index-1]
                    if prev_hist > 0 and macd_hist < 0:
                        return {
                            'action': 'CLOSE',
                            'confidence': 0.85,
                            'reason': f'MACD由正转负({prev_hist:.4f}→{macd_hist:.4f})，趋势反转平仓',
                            'leverage': self.default_leverage,
                            'position_percent': 0
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
        
        # ===== 无信号时继续持仓 =====
        return {
            'action': 'HOLD',
            'confidence': 0.5,
            'reason': f'继续持仓等待平仓信号',
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
        print(f"🔄 开始优化回测 V3 (初始资金: {initial_capital} USDT)")
        print(f"{'='*60}")
        
        # 初始化日志
        self.init_logging()
        
        # 初始化状态
        self.position = None
        self.position_open_bar = -1
        self.position_entry_price = 0
        self.last_close_bar = -1
        self.balance = initial_capital
        self.trades = []
        
        total_bars = len(self.df)
        
        for i in range(total_bars):
            if i < 10:  # 跳过前10根，确保指标计算完整
                self._log_kline(i, "⏸ SKIP", "无", "数据不足")
                continue
            
            decision = self.make_decision(i)
            action = decision['action']
            reason = decision['reason']
            
            current_price = self.df['close'].iloc[i]
            
            if action == 'SELL_OPEN':
                if self.position is None:
                    self.position = 'SHORT'
                    self.position_open_bar = i
                    self.position_entry_price = current_price
                    emoji = "📉"
                    print(f"{emoji} [{self.df.index[i]}] 开空仓 @ {current_price:.2f} - {reason}")
                    self._log_kline(i, f"{emoji} 开空仓", "SHORT", reason)
            
            elif action == 'BUY_OPEN':
                if self.position is None:
                    self.position = 'LONG'
                    self.position_open_bar = i
                    self.position_entry_price = current_price
                    emoji = "📈"
                    print(f"{emoji} [{self.df.index[i]}] 开多仓 @ {current_price:.2f} - {reason}")
                    self._log_kline(i, f"{emoji} 开多仓", "LONG", reason)
            
            elif action == 'CLOSE':
                if self.position is not None:
                    entry_price = self.position_entry_price
                    hold_bars = i - self.position_open_bar
                    
                    if self.position == 'LONG':
                        trade_pnl = (current_price - entry_price) / entry_price * 100
                        trade_amount = self.balance * self.position_size
                        profit = trade_amount * trade_pnl / 100
                    else:  # SHORT
                        trade_pnl = (entry_price - current_price) / entry_price * 100
                        trade_amount = self.balance * self.position_size
                        profit = trade_amount * trade_pnl / 100
                    
                    self.balance += profit
                    
                    self.trades.append({
                        'entry_bar': self.position_open_bar,
                        'exit_bar': i,
                        'position': self.position,
                        'entry_price': entry_price,
                        'exit_price': current_price,
                        'pnl_percent': trade_pnl,
                        'pnl_amount': profit,
                        'hold_bars': hold_bars,
                        'reason': reason
                    })
                    
                    emoji = "✅" if profit > 0 else "❌"
                    close_action = f"✅平仓{self.position}" if profit > 0 else f"❌平仓{self.position}"
                    print(f"{emoji} [{self.df.index[i]}] 平仓 {self.position} @ {current_price:.2f} | "
                          f"盈亏: {profit:+.2f} ({trade_pnl:+.2f}%) | 持仓{hold_bars}根 | "
                          f"{reason[:30]}")
                    self._log_kline(i, close_action, "无", f"盈亏{profit:+.2f}")
                    
                    self.position = None
                    self.position_open_bar = -1
                    self.last_close_bar = i
                    self.position_entry_price = 0
            
            else:  # HOLD or other
                self._log_kline(i, "⏸ HOLD", self.position if self.position else "无", reason)
        
        # 强制平仓（如果还有持仓）
        if self.position is not None:
            i = total_bars - 1
            current_price = self.df['close'].iloc[i]
            entry_price = self.position_entry_price
            hold_bars = i - self.position_open_bar
            
            if self.position == 'LONG':
                trade_pnl = (current_price - entry_price) / entry_price * 100
                trade_amount = self.balance * self.position_size
                profit = trade_amount * trade_pnl / 100
            else:
                trade_pnl = (entry_price - current_price) / entry_price * 100
                trade_amount = self.balance * self.position_size
                profit = trade_amount * trade_pnl / 100
            
            self.balance += profit
            
            self.trades.append({
                'entry_bar': self.position_open_bar,
                'exit_bar': i,
                'position': self.position,
                'entry_price': entry_price,
                'exit_price': current_price,
                'pnl_percent': trade_pnl,
                'pnl_amount': profit,
                'hold_bars': hold_bars,
                'reason': '回测结束强制平仓'
            })
            
            emoji = "✅" if profit > 0 else "❌"
            print(f"{emoji} [{self.df.index[i]}] 回测结束平仓 {self.position} @ {current_price:.2f} | "
                  f"盈亏: {profit:+.2f} ({trade_pnl:+.2f}%) | 持仓{hold_bars}根")
            self._log_kline(i, f"{emoji} 回测结束平仓{self.position}", "无", f"盈亏{profit:+.2f}")
        
        # 写入日志结尾
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write("-" * 118 + "\n")
            f.write(f"回测完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        return {
            'initial_capital': initial_capital,
            'final_capital': self.balance,
            'trades': self.trades
        }
    
    def print_summary(self, result: Dict[str, Any]):
        """打印回测汇总"""
        # 确保日志文件已初始化
        if self.summary_file is None:
            self.init_logging()
        
        initial_capital = result['initial_capital']
        final_capital = result['final_capital']
        trades = result['trades']
        
        total_pnl = final_capital - initial_capital
        total_return = (total_pnl / initial_capital) * 100 if initial_capital > 0 else 0
        
        win_trades = [t for t in trades if t['pnl_amount'] > 0]
        lose_trades = [t for t in trades if t['pnl_amount'] <= 0]
        
        avg_win = np.mean([t['pnl_amount'] for t in win_trades]) if win_trades else 0
        avg_loss = np.mean([t['pnl_amount'] for t in lose_trades]) if lose_trades else 0
        max_win = max([t['pnl_amount'] for t in trades]) if trades else 0
        max_loss = min([t['pnl_amount'] for t in trades]) if trades else 0
        
        win_rate = len(win_trades) / len(trades) * 100 if trades else 0
        avg_hold_bars = np.mean([t['hold_bars'] for t in trades]) if trades else 0
        
        print(f"\n{'='*60}")
        print(f"📊 回测汇总报告")
        print(f"{'='*60}")
        print(f"\n【基本信息】")
        print(f"交易对: {self.symbol}")
        print(f"周期: {self.interval}")
        print(f"回测天数: {self.days}")
        print(f"回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        print(f"\n【资金情况】")
        print(f"初始资金: {initial_capital:.2f} USDT")
        print(f"最终资金: {final_capital:.2f} USDT")
        print(f"总盈亏: {total_pnl:+.2f} USDT")
        print(f"收益率: {total_return:+.2f}%")
        
        print(f"\n【交易统计】")
        print(f"总交易数: {len(trades)}")
        print(f"赢利笔数: {len(win_trades)}")
        print(f"亏损笔数: {len(lose_trades)}")
        print(f"胜率: {win_rate:.2f}%")
        
        if len(win_trades) > 0:
            print(f"平均盈利: {avg_win:+.2f} USDT")
        if len(lose_trades) > 0:
            print(f"平均亏损: {avg_loss:+.2f} USDT")
        print(f"最大盈利: {max_win:+.2f} USDT")
        print(f"最大亏损: {max_loss:+.2f} USDT")
        print(f"平均持仓: {avg_hold_bars:.1f} 根K线 ({avg_hold_bars*5:.1f} 分钟)")
        
        print(f"\n【V3 优化参数】")
        print(f"信号门槛: {self.short_signal_threshold}/6 (做空)")
        print(f"冷却期: {self.cooldown_bars} 根K线")
        print(f"最小持仓: {self.min_hold_bars} 根K线")
        print(f"最大持仓: {self.max_hold_bars} 根K线")
        print(f"止损比例: {self.stop_loss_percent}%")
        print(f"止盈比例: {self.take_profit_percent}%")
        print(f"做空最小RSI: {self.min_rsi_for_short}")
        print(f"做多最大RSI: {self.max_rsi_for_long}")
        
        print(f"\n详细K线操作日志: {self.log_file}")
        
        # 保存汇总到文件
        with open(self.summary_file, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("📊 优化回测 V3 汇总报告\n")
            f.write("=" * 80 + "\n")
            f.write(f"\n【基本信息】\n")
            f.write(f"交易对: {self.symbol}\n")
            f.write(f"周期: {self.interval}\n")
            f.write(f"回测天数: {self.days}\n")
            f.write(f"回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"\n【资金情况】\n")
            f.write(f"初始资金: {initial_capital:.2f} USDT\n")
            f.write(f"最终资金: {final_capital:.2f} USDT\n")
            f.write(f"总盈亏: {total_pnl:+.2f} USDT\n")
            f.write(f"收益率: {total_return:+.2f}%\n")
            f.write(f"\n【交易统计】\n")
            f.write(f"总交易数: {len(trades)}\n")
            f.write(f"赢利笔数: {len(win_trades)}\n")
            f.write(f"亏损笔数: {len(lose_trades)}\n")
            f.write(f"胜率: {win_rate:.2f}%\n")
            if len(win_trades) > 0:
                f.write(f"平均盈利: {avg_win:+.2f} USDT\n")
            if len(lose_trades) > 0:
                f.write(f"平均亏损: {avg_loss:+.2f} USDT\n")
            f.write(f"最大盈利: {max_win:+.2f} USDT\n")
            f.write(f"最大亏损: {max_loss:+.2f} USDT\n")
            f.write(f"平均持仓: {avg_hold_bars:.1f} 根K线 ({avg_hold_bars*5:.1f} 分钟)\n")
            f.write(f"\n详细K线操作日志: {self.log_file}\n")


def main():
    """主函数"""
    # 从环境变量读取API密钥
    import os
    from dotenv import load_dotenv
    
    # 加载.env文件
    load_dotenv('.env')
    
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_SECRET')
    
    print("=" * 60)
    print("🚀 开始优化回测 V3：5分钟K线，30天数据，100 USDT")
    print("=" * 60)
    
    # 创建回测器
    backtester = BacktesterV3(
        symbol="SOLUSDT",
        interval="5m",
        days=30,
        api_key=api_key,
        api_secret=api_secret,
    )
    
    print(f"✅ V3 参数已加载")
    print(f"   - 做空信号门槛: {backtester.short_signal_threshold}/6 (V2: 5/6)")
    print(f"   - 冷却期: {backtester.cooldown_bars}根K线 (V2: 12根)")
    print(f"   - 最小持仓时间: {backtester.min_hold_bars}根K线 (V2: 8根)")
    print(f"   - 做空最小RSI: {backtester.min_rsi_for_short} (V2: 无限制)")
    print(f"   - 做多最大RSI: {backtester.max_rsi_for_long} (V2: 无限制)")
    print(f"   - 止损比例: {backtester.stop_loss_percent}% (V2: 1.5%)")
    print(f"   - 止盈比例: {backtester.take_profit_percent}% (V2: 2.0%)")
    
    # 下载历史数据
    backtester.fetch_data()
    
    # 计算技术指标
    backtester.calculate_indicators()
    
    # 运行回测
    result = backtester.run_backtest(initial_capital=100)
    
    # 打印汇总
    backtester.print_summary(result)
    
    return result


if __name__ == "__main__":
    main()
