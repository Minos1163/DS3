"""
快速回测脚本 - 使用少量样本数据测试
"""
import os
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.config.env_manager import EnvManager


def download_klines_safe(symbol: str = 'SOLUSDT', interval: str = '5m', 
                        days: int = 3, max_retries: int = 3) -> Optional[pd.DataFrame]:
    """
    安全下载K线数据
    """
    print(f"\n{'='*60}")
    print(f"📥 下载历史数据")
    print(f"{'='*60}")
    print(f"交易对: {symbol}")
    print(f"周期: {interval}")
    print(f"天数: {days}")
    
    EnvManager.load_env_file('.env')
    api_key, api_secret = EnvManager.get_api_credentials()
    
    try:
        client = BinanceClient(api_key=api_key, api_secret=api_secret)
        print("✅ 币安客户端连接成功")
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return None
    
    all_klines = []
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days)
    
    print(f"时间范围: {start_time.strftime('%Y-%m-%d')} 至 {end_time.strftime('%Y-%m-%d')}")
    print("开始下载...")
    
    try:
        # 直接获取1000根K线（最近的数据）
        print(f"   下载最近的K线数据...", end='')
        klines = client.get_klines(
            symbol=symbol,
            interval=interval,
            limit=1000
        )
        
        if klines:
            print(f" ✓ ({len(klines)} 根)")
            all_klines.extend(klines)
    except Exception as e:
        print(f" ❌ {e}")
        return None
    
    if not all_klines:
        print("❌ 未获取到任何数据")
        return None
    
    print(f"✅ 共下载 {len(all_klines)} 根K线")
    
    # 转换为DataFrame
    df = pd.DataFrame(all_klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_buy_base',
        'taker_buy_quote', 'ignore'
    ])
    
    # 转换数据类型
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    
    # 设置时间为索引
    df.set_index('timestamp', inplace=True)
    df.sort_index(inplace=True)
    
    print(f"\n数据统计:")
    print(f"   开始时间: {df.index[0]}")
    print(f"   结束时间: {df.index[-1]}")
    print(f"   数据点数: {len(df)}")
    print(f"   收盘价范围: {df['close'].min():.2f} - {df['close'].max():.2f}")
    
    return df


def calculate_indicators_safe(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """计算技术指标"""
    if df is None or len(df) == 0:
        return None
    
    print(f"\n{'='*60}")
    print(f"📊 计算技术指标")
    print(f"{'='*60}")
    
    try:
        close = df['close']
        high = df['high']
        low = df['low']
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # EMA
        df['ema_5'] = close.ewm(span=5, adjust=False).mean()
        df['ema_20'] = close.ewm(span=20, adjust=False).mean()
        
        # 简单移动平均
        df['sma_20'] = close.rolling(window=20).mean()
        
        # MACD
        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        df['macd'] = macd_line
        df['macd_signal'] = macd_line.ewm(span=9, adjust=False).mean()
        
        # ATR
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=14).mean()
        
        # 布林带
        sma = close.rolling(window=20).mean()
        std = close.rolling(window=20).std()
        df['bb_upper'] = sma + (std * 2)
        df['bb_middle'] = sma
        df['bb_lower'] = sma - (std * 2)
        
        print("✅ 指标计算完成")
        print(f"   RSI, EMA, SMA, MACD, ATR, 布林带")
        
        return df
        
    except Exception as e:
        print(f"❌ 指标计算失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def analyze_and_backtest(df: pd.DataFrame, symbol: str = 'SOLUSDT') -> str:
    """分析数据并进行简单回测"""
    if df is None or len(df) < 50:
        return "❌ 数据不足，无法进行分析"
    
    print(f"\n{'='*60}")
    print(f"🔍 数据分析与回测")
    print(f"{'='*60}")
    
    # 基本统计
    close = df['close']
    start_price = close.iloc[0]
    end_price = close.iloc[-1]
    price_change = end_price - start_price
    price_change_pct = (price_change / start_price) * 100
    
    # K线统计
    bullish = sum(df['close'] > df['open'])
    bearish = sum(df['close'] <= df['open'])
    
    # RSI统计
    rsi = df['rsi'].dropna()
    oversold_count = sum(rsi < 30)
    overbought_count = sum(rsi > 70)
    
    # 最大回撤
    cummax = close.cummax()
    drawdown = (close - cummax) / cummax * 100
    max_drawdown = drawdown.min()
    
    # 波动率
    returns = close.pct_change().dropna()
    volatility = returns.std() * np.sqrt(288) * 100  # 年化波动率 (5分钟一根K线，一天288根)
    
    # 简单交易策略：RSI < 30买入，RSI > 70卖出
    position = None
    entry_price = 0
    entry_time = None
    trades = []
    
    for i in range(30, len(df)):
        current = df.iloc[i]
        rsi = current['rsi']
        curr_price = current['close']
        curr_time = current.name
        
        # 买入
        if position is None and rsi < 30:
            position = 'LONG'
            entry_price = curr_price
            entry_time = curr_time
        
        # 卖出
        elif position == 'LONG' and rsi > 70 and entry_time is not None:
            pnl = curr_price - entry_price
            pnl_pct = (pnl / entry_price) * 100
            trades.append({
                'entry': entry_time,
                'entry_price': entry_price,
                'exit': curr_time,
                'exit_price': curr_price,
                'pnl': pnl,
                'pnl_pct': pnl_pct
            })
            position = None
    
    # 生成报告
    report = f"""
{'='*60}
📈 {symbol} 快速分析报告
{'='*60}

【数据范围】
开始时间: {df.index[0]}
结束时间: {df.index[-1]}
K线数量: {len(df)}
时间跨度: {(df.index[-1] - df.index[0]).total_seconds() / 3600:.0f} 小时

【价格走势】
起始价格: {start_price:.2f} USDT
结束价格: {end_price:.2f} USDT
价格变化: {price_change:+.2f} USDT ({price_change_pct:+.2f}%)
最高价: {close.max():.2f} USDT
最低价: {close.min():.2f} USDT
最大回撤: {max_drawdown:.2f}%

【K线分析】
看涨K线: {bullish} ({100*bullish/len(df):.1f}%)
看跌K线: {bearish} ({100*bearish/len(df):.1f}%)

【技术指标】
超卖信号 (RSI<30): {oversold_count} 次
超买信号 (RSI>70): {overbought_count} 次
年化波动率: {volatility:.2f}%

【RSI策略回测】
交易信号总数: {len(trades)}
"""
    
    if trades:
        win_trades = sum(1 for t in trades if t['pnl'] > 0)
        total_pnl = sum(t['pnl'] for t in trades)
        avg_pnl = total_pnl / len(trades) if trades else 0
        
        report += f"""
胜率: {win_trades}/{len(trades)} ({100*win_trades/len(trades):.1f}%)
总盈亏: {total_pnl:+.2f} USDT
平均盈亏: {avg_pnl:+.2f} USDT

【最近的交易】
"""
        for trade in trades[-5:]:
            direction = "✅" if trade['pnl'] > 0 else "❌"
            report += f"{direction} {trade['entry'].strftime('%m-%d %H:%M')} 买 @ {trade['entry_price']:.2f} → {trade['exit'].strftime('%m-%d %H:%M')} 卖 @ {trade['exit_price']:.2f} : {trade['pnl']:+.2f} ({trade['pnl_pct']:+.2f}%)\n"
    
    report += f"{'='*60}\n"
    
    return report


def main():
    """主函数"""
    print("=" * 60)
    print("🚀 SOLUSDT 快速回测分析")
    print("=" * 60)
    
    # 下载最近3天的5分钟数据
    df = download_klines_safe(symbol='SOLUSDT', interval='5m', days=3)
    
    if df is None or len(df) == 0:
        print("❌ 数据下载失败，无法进行回测")
        return
    
    # 计算指标
    df = calculate_indicators_safe(df)
    
    if df is None:
        print("❌ 指标计算失败")
        return
    
    # 分析数据
    report = analyze_and_backtest(df, symbol='SOLUSDT')
    print(report)
    
    # 保存报告
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    report_file = f"backtest_report_SOLUSDT_{timestamp}.txt"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"📄 报告已保存: {report_file}")
    
    csv_file = f"backtest_data_SOLUSDT_{timestamp}.csv"
    df.to_csv(csv_file)
    print(f"💾 数据已保存: {csv_file}")
    
    # 显示数据预览
    print(f"\n【最近K线数据】")
    print(df[['close', 'volume', 'rsi', 'ema_5', 'ema_20']].tail(10).to_string())


if __name__ == '__main__':
    main()
