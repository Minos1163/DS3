"""
AI回测脚本 - 使用AI决策进行回测，支持做多和做空
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
from src.ai.deepseek_client import DeepSeekClient
from src.ai.decision_parser import DecisionParser


class AIBacktester:
    """AI回测引擎"""
    
    def __init__(self, symbol: str = 'SOLUSDT', interval: str = '5m', days: int = 7, use_ai: bool = False):
        """初始化AI回测引擎"""
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
        
        # ===== 优化参数配置 =====
        # 止损止盈设置
        self.stop_loss_pct = 0.8      # 止损百分比（1.5%→0.8%，更紧的止损防止大亏）
        self.take_profit_pct = 1.5    # 止盈百分比（2.5%→1.5%，更积极地锁定利润）
        self.use_atr_stop = True      # 使用ATR动态止损
        self.atr_multiplier = 2.0     # ATR倍数
        
        # 交易信号参数
        self.rsi_oversold = 25        # RSI超卖阈值（35→25，避免反向强势）
        self.rsi_overbought = 75      # RSI超买阈值（65→75，等待更明确的超买信号）
        self.rsi_neutral_low = 48     # RSI中性区间下限（收紧平仓条件）
        self.rsi_neutral_high = 52    # RSI中性区间上限
        
        # 趋势确认参数
        self.trend_confirm_bars = 3   # 需要连续N根K线确认趋势
        self.min_price_change = 0.5   # 最小价格变化百分比
        
        # 仓位管理
        self.default_leverage = 2     # 降低杠杆降低风险（3→2）
        self.position_size = 25       # 增加仓位（20%→25%）
        self.max_hold_bars = 60       # 最大持仓K线数（5分钟K线，约5小时，防止长期持仓亏损）
        
        # 交易频率控制
        self.min_bars_between_trades = 6  # 两次交易之间最少间隔K线数
        self.last_trade_bar = -999    # 上次交易的K线索引
        self.position_open_bar = -999 # 当前持仓开仓K线索引
        
        # 初始化客户端
        print("🚀 初始化AI回测系统...")
        EnvManager.load_env_file('.env')
        
        # 币安客户端
        api_key, api_secret = EnvManager.get_api_credentials()
        self.binance = BinanceClient(api_key=api_key, api_secret=api_secret)
        
        # AI客户端（需要API密钥）
        self.use_ai = use_ai
        if use_ai:
            try:
                deepseek_key = EnvManager.get_deepseek_key()
                if deepseek_key and deepseek_key != 'your_deepseek_api_key_here':
                    self.ai_client = DeepSeekClient(api_key=deepseek_key)
                    print("✅ AI客户端已启用 (DeepSeek)")
                else:
                    self.ai_client = None
                    self.use_ai = False
                    print("⚠️  未配置DeepSeek API，将使用简化策略")
            except Exception as e:
                print(f"⚠️  AI客户端初始化失败: {e}")
                self.ai_client = None
                self.use_ai = False
                print("⚠️  将使用简化策略进行回测")
        else:
            self.ai_client = None
            print("⚠️  AI已禁用，使用简化策略进行回测")
        
        self.decision_parser = DecisionParser()
    
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
            f.write("🔄 AI回测 K线操作日志\n")
            f.write("=" * 150 + "\n")
            f.write(f"交易对: {self.symbol} | 周期: {self.interval} | AI: {'启用' if self.use_ai else '禁用'}\n")
            f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 150 + "\n\n")
            f.write(f"{'时间':<20} | {'开高低收':<35} | {'RSI':<8} | {'MACD':<12} | {'操作':<30} | {'持仓状态':<15} | {'原因':<40}\n")
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
            f.write("📊 AI回测汇总报告\n")
            f.write("=" * 100 + "\n\n")
            
            f.write(f"【基本信息】\n")
            f.write(f"交易对: {self.symbol}\n")
            f.write(f"周期: {self.interval}\n")
            f.write(f"AI状态: {'启用' if self.use_ai else '禁用'}\n")
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
            # 5分钟K线，7天数据 = 7*24*60/5 = 2016根K线，但币安限制1000根
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
    
    def build_ai_prompt(self, index: int) -> str:
        """构建AI分析提示词"""
        if self.df is None:
            return ""
        
        row = self.df.iloc[index]
        recent_df = self.df.iloc[max(0, index-20):index+1]
        
        # 计算趋势
        price_change = (row['close'] - recent_df['close'].iloc[0]) / recent_df['close'].iloc[0] * 100
        
        prompt = f"""你是一个专业的加密货币交易AI。请分析以下市场数据并给出交易决策。

【市场信息】
交易对: {self.symbol}
当前时间: {row.name}
当前价格: {row['close']:.2f} USDT
最近价格变化: {price_change:+.2f}%

【技术指标】
RSI(14): {row['rsi']:.1f}
EMA(5): {row['ema_5']:.2f}
EMA(20): {row['ema_20']:.2f}
EMA(50): {row['ema_50']:.2f}
MACD: {row['macd']:.2f}
MACD信号: {row['macd_signal']:.2f}
布林带上轨: {row['bb_upper']:.2f}
布林带中轨: {row['bb_middle']:.2f}
布林带下轨: {row['bb_lower']:.2f}

【市场状态判断】
- 价格趋势: {'上涨' if price_change > 0 else '下跌'} ({price_change:+.2f}%)
- RSI状态: {'超买' if row['rsi'] > 70 else '超卖' if row['rsi'] < 30 else '中性'}
- EMA趋势: {'多头' if row['ema_5'] > row['ema_20'] > row['ema_50'] else '空头' if row['ema_5'] < row['ema_20'] < row['ema_50'] else '震荡'}
- MACD: {'金叉' if row['macd'] > row['macd_signal'] else '死叉'}

【交易决策要求】
请基于以上信息，给出交易决策。注意：
1. 当前市场处于下跌趋势（熊市），可以考虑做空策略
2. 支持的操作：BUY_OPEN(做多开仓)、SELL_OPEN(做空开仓)、CLOSE(平仓)、HOLD(持有)
3. 做空策略：当市场看跌时，使用SELL_OPEN开空仓，价格下跌时获利

请返回JSON格式的决策（不要使用markdown代码块）：
{{
    "action": "BUY_OPEN|SELL_OPEN|CLOSE|HOLD",
    "confidence": 0.0-1.0,
    "reason": "决策理由（中文）",
    "leverage": 1-5,
    "position_percent": 10-30
}}"""
        
        return prompt
    
    def get_simple_decision(self, index: int) -> Dict[str, Any]:
        """优化的简化策略 - 提高胜率，降低亏损"""
        if self.df is None:
            return {
                'action': 'HOLD',
                'confidence': 0.5,
                'reason': '数据未加载',
                'leverage': 3,
                'position_percent': 0
            }
        
        # 交易频率控制：避免过度交易
        if index - self.last_trade_bar < self.min_bars_between_trades:
            return {
                'action': 'HOLD',
                'confidence': 0.5,
                'reason': '交易冷却期，避免过度交易',
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
        ema_5 = row['ema_5']
        ema_20 = row['ema_20']
        ema_50 = row['ema_50']
        atr = row['atr']
        bb_upper = row['bb_upper']
        bb_lower = row['bb_lower']
        current_price = row['close']
        
        # 趋势确认：检查最近N根K线
        if index >= self.trend_confirm_bars:
            recent_closes = self.df['close'].iloc[index-self.trend_confirm_bars:index+1]
            is_downtrend = all(recent_closes.iloc[i] > recent_closes.iloc[i+1] 
                             for i in range(len(recent_closes)-1))
            is_uptrend = all(recent_closes.iloc[i] < recent_closes.iloc[i+1] 
                           for i in range(len(recent_closes)-1))
        else:
            is_downtrend = False
            is_uptrend = False
        
        # ===== 做空信号（熊市策略）=====
        # 更严格的条件组合，提高信号质量
        short_signal_count = 0
        short_reasons = []
        
        # 条件1：RSI超买区域
        if rsi > self.rsi_overbought:
            short_signal_count += 1
            short_reasons.append(f'RSI超买({rsi:.1f})')
        
        # 条件2：价格接近布林带上轨或突破后回落
        if current_price >= bb_upper * 0.98:
            short_signal_count += 1
            short_reasons.append('触及布林带上轨')
        
        # 条件3：MACD死叉且在零轴下方（强烈看跌）
        if macd < macd_signal and macd < 0:
            short_signal_count += 1
            short_reasons.append('MACD死叉')
        
        # 条件4：空头排列（EMA5 < EMA20 < EMA50）
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
        
        # 需要至少4个做空信号才开仓（提高质量，从3个提升到4个）
        if short_signal_count >= 4:
            return {
                'action': 'SELL_OPEN',
                'confidence': min(0.6 + short_signal_count * 0.1, 0.95),
                'reason': f'做空信号({short_signal_count}/6): ' + ', '.join(short_reasons),
                'leverage': self.default_leverage,
                'position_percent': self.position_size
            }
        
        # ===== 做多信号（反弹机会）=====
        # 严格控制做多条件（熊市中少做多）
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
        
        # 熊市中需要更多确认信号（至少4个）
        if long_signal_count >= 4:
            return {
                'action': 'BUY_OPEN',
                'confidence': min(0.6 + long_signal_count * 0.1, 0.95),
                'reason': f'反弹信号({long_signal_count}/4): ' + ', '.join(long_reasons),
                'leverage': self.default_leverage,
                'position_percent': self.position_size
            }
        
        # ===== 最大持仓时间检查 =====
        # 如果持仓超过最大时间，强制平仓（防止长期亏损）
        if self.position_open_bar >= 0 and index - self.position_open_bar >= self.max_hold_bars:
            return {
                'action': 'CLOSE',
                'confidence': 0.9,
                'reason': f'持仓超过{self.max_hold_bars}根K线({self.max_hold_bars*5//60}小时)，触发止损平仓',
                'leverage': self.default_leverage,
                'position_percent': 0
            }
        
        # ===== 平仓信号 =====
        # RSI回归中性区间
        if self.rsi_neutral_low < rsi < self.rsi_neutral_high:
            return {
                'action': 'CLOSE',
                'confidence': 0.7,
                'reason': f'RSI回归中性区域({rsi:.1f})，平仓保护利润',
                'leverage': self.default_leverage,
                'position_percent': 0
            }
        
        return {
            'action': 'HOLD',
            'confidence': 0.5,
            'reason': f'信号不足，持有观望 (做空:{short_signal_count}/3, 做多:{long_signal_count}/4)',
            'leverage': self.default_leverage,
            'position_percent': 0
        }
    
    def run_backtest(self, initial_capital: float = 10000):
        """运行回测"""
        if self.df is None:
            print("❌ 数据未加载，无法执行回测")
            return {
                'initial_capital': initial_capital,
                'final_capital': initial_capital,
                'trades': []
            }
        
        print(f"\n{'='*60}")
        print(f"🔄 开始AI回测 (初始资金: {initial_capital} USDT)")
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
            
            # 获取AI决策 - 每根K线都调用AI分析
            if self.use_ai and self.ai_client is not None:
                try:
                    prompt = self.build_ai_prompt(i)
                    response = self.ai_client.analyze_and_decide(prompt)
                    decision = self.decision_parser.parse_ai_response(response['content'])
                    if decision['action'] != 'HOLD':
                        print(f"\n🤖 [{current_time}] AI决策: {decision['action']} - {decision['reason']}")
                except Exception as e:
                    print(f"⚠️  [{current_time}] AI调用失败: {e}，使用简化策略")
                    decision = self.get_simple_decision(i)
            else:
                decision = self.get_simple_decision(i)
            
            # 执行交易逻辑
            action = decision['action']
            position_status = position if position else "无"
            
            # 开多仓
            if action == 'BUY_OPEN' and position is None:
                position = 'LONG'
                entry_price = current_price
                entry_time = current_time
                self.position_open_bar = i
                print(f"📈 [{current_time}] 开多仓 @ {entry_price:.2f} - {decision['reason']}")
                self._log_kline(i, "📈 开多仓", "LONG", decision['reason'][:35])
            
            # 开空仓
            elif action == 'SELL_OPEN' and position is None:
                position = 'SHORT'
                entry_price = current_price
                entry_time = current_time
                self.position_open_bar = i
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
                
                capital += pnl * (capital / entry_price) * 0.1  # 假设10%仓位
                
                trade_record = {
                    'type': position,
                    'entry_time': entry_time,
                    'entry_price': entry_price,
                    'exit_time': current_time,
                    'exit_price': current_price,
                    'pnl': pnl,
                    'pnl_percent': pnl_percent,
                    'reason': decision['reason']
                }
                trades.append(trade_record)
                
                emoji = "✅" if pnl > 0 else "❌"
                close_action = f"✅平仓{position}" if pnl > 0 else f"❌平仓{position}"
                print(f"{emoji} [{current_time}] 平仓 {position} @ {current_price:.2f} | "
                      f"盈亏: {pnl:+.2f} ({pnl_percent:+.2f}%) - {decision['reason']}")
                self._log_kline(i, close_action, "无", f"盈亏{pnl:+.2f}")
                
                position = None
                self.position_open_bar = -999  # 重置开仓时间标记
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
            print(f"   胜率: {len(win_trades)/len(trades)*100:.1f}%")
            
            avg_pnl = sum(t['pnl'] for t in trades) / len(trades)
            max_win = max(t['pnl'] for t in trades)
            max_loss = min(t['pnl'] for t in trades)
            
            print(f"\n📊 盈亏分析:")
            print(f"   平均盈亏: {avg_pnl:+.2f} USDT")
            print(f"   最大盈利: {max_win:+.2f} USDT")
            print(f"   最大亏损: {max_loss:+.2f} USDT")
            
            # 显示最近的交易
            print(f"\n📋 最近交易记录 (最多5笔):")
            for trade in trades[-5:]:
                emoji = "✅" if trade['pnl'] > 0 else "❌"
                print(f"{emoji} {trade['type']:5} | "
                      f"{trade['entry_time'].strftime('%m-%d %H:%M')} @ {trade['entry_price']:.2f} → "
                      f"{trade['exit_time'].strftime('%m-%d %H:%M')} @ {trade['exit_price']:.2f} | "
                      f"{trade['pnl']:+.2f} ({trade['pnl_percent']:+.2f}%)")
        
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
    print("AI回测系统 - 5分钟K线 2天数据 完整AI分析")
    print("=" * 60)
    
    # 创建回测器 - 5m间隔，7天数据，禁用AI
    backtester = AIBacktester(symbol='SOLUSDT', interval='5m', days=7, use_ai=False)
    
    # 下载数据
    if backtester.download_data() is None:
        print("❌ 数据下载失败")
        return
    
    # 计算指标
    backtester.calculate_indicators()
    
    # 运行回测
    trades = backtester.run_backtest(initial_capital=10000)
    
    print(f"\n{'='*60}")
    print("回测完成！")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
