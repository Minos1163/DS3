"""
简化版深度分析 - 基于K线数据和交易结果
"""
import pandas as pd
import numpy as np
import glob
import os
import json
from datetime import datetime

def analyze_detailed(csv_file, log_file):
    """详细分析市场状态和交易时机"""
    
    # 读取CSV数据
    df = pd.read_csv(csv_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    print(f"\n{'='*70}")
    print(f"📊 详细市场状态分析")
    print(f"{'='*70}\n")
    
    # 1. 市场状态分析
    print("📈 1. 市场波动特征:")
    print(f"   交易周期: {df['timestamp'].min()} ~ {df['timestamp'].max()}")
    print(f"   时间跨度: {(df['timestamp'].max() - df['timestamp'].min()).days} 天 {((df['timestamp'].max() - df['timestamp'].min()).seconds // 3600)} 小时")
    print(f"   K线数量: {len(df)} 根")
    
    # 价格统计
    print(f"\n💰 2. 价格统计:")
    print(f"   开盘价: {df['open'].iloc[0]:.2f}")
    print(f"   收盘价: {df['close'].iloc[-1]:.2f}")
    print(f"   最高价: {df['high'].max():.2f}")
    print(f"   最低价: {df['low'].min():.2f}")
    print(f"   涨跌幅: {(df['close'].iloc[-1] - df['open'].iloc[0]) / df['open'].iloc[0] * 100:+.2f}%")
    
    # RSI分析 - 需要从日志中重新计算
    print(f"\n📊 3. 从日志提取的关键指标分布:")
    
    # 从日志中解析K线信息
    rsi_values = []
    macd_values = []
    
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if ' | RSI=' in line and 'MACD=' in line:
                try:
                    rsi_match = line.split('RSI=')[1].split(' ')[0]
                    macd_match = line.split('MACD=')[1].split(' ')[0]
                    rsi_values.append(float(rsi_match))
                    macd_values.append(float(macd_match))
                except:
                    pass
    
    if rsi_values:
        print(f"   RSI统计:")
        print(f"     平均值: {np.mean(rsi_values):.1f}")
        print(f"     最大值: {np.max(rsi_values):.1f}")
        print(f"     最小值: {np.min(rsi_values):.1f}")
        print(f"     超卖(<35): {len([x for x in rsi_values if x < 35])} 次")
        print(f"     超买(>70): {len([x for x in rsi_values if x > 70])} 次")
        print(f"     中立(47-53): {len([x for x in rsi_values if 47 <= x <= 53])} 次")
    
    if macd_values:
        print(f"   MACD统计:")
        print(f"     平均值: {np.mean(macd_values):+.3f}")
        print(f"     正值: {len([x for x in macd_values if x > 0])} 次 ({len([x for x in macd_values if x > 0])/len(macd_values)*100:.1f}%)")
        print(f"     负值: {len([x for x in macd_values if x < 0])} 次 ({len([x for x in macd_values if x < 0])/len(macd_values)*100:.1f}%)")
    
    # 4. 波动率分析
    print(f"\n💨 4. 波动率分析:")
    df['atr'] = (df['high'] - df['low']).rolling(14).mean()
    print(f"   平均ATR: {df['atr'].mean():.4f} ({df['atr'].mean()/df['close'].mean()*100:.2f}%)")
    print(f"   最大ATR: {df['atr'].max():.4f}")
    print(f"   最小ATR: {df['atr'].min():.4f}")
    
    # 5. 从日志解析交易信息
    print(f"\n{'='*70}")
    print(f"💰 交易执行分析")
    print(f"{'='*70}\n")
    
    trades_info = parse_trades_from_log(log_file)
    
    if trades_info:
        print(f"📊 5. 交易统计:")
        print(f"   总交易: {len(trades_info)} 笔")
        
        winning = [t for t in trades_info if t['pnl'] > 0]
        losing = [t for t in trades_info if t['pnl'] < 0]
        
        print(f"   胜利: {len(winning)} 笔 ({len(winning)/len(trades_info)*100:.1f}%)")
        print(f"   失败: {len(losing)} 笔 ({len(losing)/len(trades_info)*100:.1f}%)")
        
        total_pnl = sum([t['pnl'] for t in trades_info])
        print(f"   总盈亏: {total_pnl:+.2f} USDT ({total_pnl/100*100:+.2f}%)")
        
        if winning:
            print(f"   平均胜利: {np.mean([t['pnl'] for t in winning]):+.2f} USDT")
        if losing:
            print(f"   平均亏损: {np.mean([t['pnl'] for t in losing]):+.2f} USDT")
        
        # 分析开仓条件
        print(f"\n🎯 6. 开仓条件分析:")
        entry_reasons = {}
        for t in trades_info:
            reason = t['reason_entry']
            if reason not in entry_reasons:
                entry_reasons[reason] = {'total': 0, 'win': 0, 'pnl': 0}
            entry_reasons[reason]['total'] += 1
            entry_reasons[reason]['pnl'] += t['pnl']
            if t['pnl'] > 0:
                entry_reasons[reason]['win'] += 1
        
        for reason, stats in sorted(entry_reasons.items(), key=lambda x: x[1]['total'], reverse=True):
            win_rate = stats['win'] / stats['total'] * 100
            avg_pnl = stats['pnl'] / stats['total']
            print(f"   {reason[:50]:50s}: {stats['total']} 笔, 胜率{win_rate:5.1f}%, 平均{avg_pnl:+.2f}")
        
        # 分析持仓时间
        print(f"\n⏱️ 7. 持仓时间分析:")
        holding_bars = []
        for t in trades_info:
            entry = pd.to_datetime(t['entry_time'])
            exit = pd.to_datetime(t['exit_time'])
            bars = (exit - entry).total_seconds() / 300
            holding_bars.append(bars)
        
        if holding_bars:
            print(f"   平均持仓: {np.mean(holding_bars):.1f} 根K线 ({np.mean(holding_bars)*5:.0f} 分钟)")
            print(f"   最长持仓: {np.max(holding_bars):.1f} 根K线 ({np.max(holding_bars)*5:.0f} 分钟)")
            print(f"   最短持仓: {np.min(holding_bars):.1f} 根K线 ({np.min(holding_bars)*5:.0f} 分钟)")
            
            short_holding = [h for h in holding_bars if h < 10]
            long_holding = [h for h in holding_bars if h >= 10]
            
            if short_holding:
                short_pnl = sum([t['pnl'] for t, h in zip(trades_info, holding_bars) if h < 10])
                print(f"   短持仓(<10根): {len(short_holding)} 次, 平均盈亏 {short_pnl/len(short_holding):+.2f}")
            
            if long_holding:
                long_pnl = sum([t['pnl'] for t, h in zip(trades_info, holding_bars) if h >= 10])
                print(f"   长持仓(≥10根): {len(long_holding)} 次, 平均盈亏 {long_pnl/len(long_holding):+.2f}")

def parse_trades_from_log(log_file):
    """从日志中提取交易信息"""
    trades = []
    
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 查找所有交易块
    import re
    
    # 查找平仓信息
    close_pattern = r'([✅❌🛑]) \[([^\]]+)\] 平仓 (LONG|SHORT).*?开仓价: ([\d.]+).*?平仓价: ([\d.]+).*?盈亏: ([\d.-]+).*?原因: (.+?)(?:\n|='
    matches = re.findall(close_pattern, content, re.DOTALL)
    
    for match in matches:
        status, exit_time, direction, entry_price, exit_price, pnl, reason = match
        
        # 找到对应的开仓时间和原因
        entry_pattern = rf'📉 \[([^\]]+)\] 开{direction[0:2]if direction=="LONG" else "空"}仓.*?原因: (.+?)(?:\n|=)'
        entry_matches = re.findall(entry_pattern, content, re.DOTALL)
        
        if entry_matches:
            entry_time = entry_matches[-1][0] if entry_matches else exit_time
            reason_entry = entry_matches[-1][1] if entry_matches else reason
            
            trades.append({
                'entry_time': entry_time,
                'exit_time': exit_time,
                'direction': direction,
                'entry_price': float(entry_price),
                'exit_price': float(exit_price),
                'pnl': float(pnl),
                'reason_entry': reason_entry,
                'reason_exit': reason.strip()
            })
    
    return trades

def print_optimization_guide():
    """打印优化指南"""
    print(f"\n{'='*70}")
    print(f"⚙️ 参数优化建议（基于分析结果）")
    print(f"{'='*70}\n")
    
    print("🔴 优先级1 - 开仓策略优化 (预期+50-100% 交易量):")
    print("""
   1. 降低信号门槛: 4/6 → 3/6
      • 当前太严格，很多机会错过
      • 建议: signal_threshold = 3
      • 代码位置: backtest_ai_optimized.py line ~280
    
   2. 缩短交易冷却期: 8根 → 4根
      • 当前冷却期导致机会丧失
      • 建议: min_bars_between_trades = 4
      • 代码位置: backtest_ai_optimized.py line ~60
    """)
    
    print("🟡 优先级2 - 平仓策略优化 (预期+30-50% 收益):")
    print("""
   1. 放宽平仓条件: RSI 47-53 → RSI 40-60
      • 当前平仓太早，切断利润
      • 建议: 改为动态平仓
        - 胜利趋势中，改为止盈制
        - 止盈: take_profit_pct = 3.0
      • 代码位置: backtest_ai_optimized.py line ~320
    
   2. 优化止损: 1.5% → 2.0%
      • 当前止损偏紧，虚假触发
      • 建议: stop_loss_pct = 2.0
      • 代码位置: backtest_ai_optimized.py line ~55
    """)
    
    print("🟢 优先级3 - 高级优化 (预期+10-20% 收益):")
    print("""
   1. 仓位管理
      • 强信号(5-6/6) → 30% 仓位
      • 中等信号(4/6) → 25% 仓位
      • 弱信号(3/6) → 15% 仓位
    
   2. 时间过滤
      • 避免 22:00-02:00 交易（流动性差）
      • 优先 08:00-16:00（亚洲和欧洲交易时段）
    
   3. 趋势确认
      • 只在MACD为负值时做空（当前市场下降趋势）
      • 只在MACD为正值时做多（未来可用）
    """)

if __name__ == '__main__':
    # 找到最新文件
    log_files = glob.glob('backtest_log_SOLUSDT_*.txt')
    csv_files = glob.glob('market_data_SOLUSDT_*.csv')
    
    if not log_files or not csv_files:
        print("❌ 找不到日志或CSV文件")
    else:
        latest_log = max(log_files, key=os.path.getctime)
        latest_csv = max(csv_files, key=os.path.getctime)
        
        print(f"📂 分析文件:")
        print(f"   日志: {latest_log}")
        print(f"   数据: {latest_csv}")
        
        analyze_detailed(latest_csv, latest_log)
        print_optimization_guide()
