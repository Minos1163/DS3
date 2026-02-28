#!/usr/bin/env python3
"""
分析最近10小时开仓阻挡原因
"""

import re
from collections import defaultdict, Counter
from datetime import datetime, timedelta
import json

def analyze_logs(log_file_path):
    """分析日志文件中的阻挡原因"""
    
    # 统计数据
    block_reasons = Counter()
    symbol_blocks = defaultdict(list)
    timeframe_blocks = []
    
    print("=== 最近10小时开仓阻挡原因分析 ===\n")
    
    with open(log_file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 查找每个周期的决策记录
    cycle_pattern = r'=== FUND_FLOW cycle \d+ @ (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC ==='
    cycles = re.findall(cycle_pattern, content)
    
    if not cycles:
        print("未找到周期记录")
        return
    
    # 获取最近10小时的时间范围
    latest_time = datetime.strptime(cycles[-1], '%Y-%m-%d %H:%M:%S')
    cutoff_time = latest_time - timedelta(hours=10)
    
    print(f"分析时间范围: {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')} UTC 到 {latest_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
    
    # 分析每个周期
    cycle_blocks = {}  # {cycle_time: [(symbol, reason)]}
    
    for i, cycle_time_str in enumerate(cycles):
        cycle_time = datetime.strptime(cycle_time_str, '%Y-%m-%d %H:%M:%S')
        
        # 如果超出10小时范围则跳过
        if cycle_time < cutoff_time:
            continue
            
        # 获取这个周期的所有内容
        if i < len(cycles) - 1:
            next_cycle_time = cycles[i + 1]
            cycle_pattern_full = rf'=== FUND_FLOW cycle \d+ @ {cycle_time_str} UTC ===.*?(?==== FUND_FLOW cycle \d+ @ {next_cycle_time} UTC ===)'
        else:
            cycle_pattern_full = rf'=== FUND_FLOW cycle \d+ @ {cycle_time_str} UTC ===.*'
            
        cycle_match = re.search(cycle_pattern_full, content, re.DOTALL)
        if not cycle_match:
            continue
            
        cycle_content = cycle_match.group(0)
        cycle_blocks[cycle_time_str] = []
        
        # 查找每个币种的决策原因
        symbol_pattern = r'\[(\w+USDT)\] 决策=HOLD.*?决策原因: ([^\n]+)'
        symbol_matches = re.findall(symbol_pattern, cycle_content, re.DOTALL)
        
        for symbol, reason in symbol_matches:
            block_reasons[reason] += 1
            symbol_blocks[symbol].append(reason)
            cycle_blocks[cycle_time_str].append((symbol, reason))
            timeframe_blocks.append((cycle_time_str, symbol, reason))
    
    # 输出统计结果
    print("=== 阻挡原因统计 ===")
    total_blocks = sum(block_reasons.values())
    print(f"总阻挡次数: {total_blocks}\n")
    
    print("按原因排序:")
    for reason, count in block_reasons.most_common():
        percentage = (count / total_blocks) * 100
        print(f"{count:3d}次 ({percentage:5.1f}%) - {reason}")
    
    print("\n=== 各币种阻挡次数 ===")
    for symbol, reasons in sorted(symbol_blocks.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"{symbol}: {len(reasons)}次阻挡")
        reason_counts = Counter(reasons)
        for reason, count in reason_counts.most_common(3):
            print(f"  - {reason}: {count}次")
        print()
    
    print("=== 时间序列分析 ===")
    print("最近几个周期的阻挡情况:")
    recent_cycles = list(cycle_blocks.keys())[-5:]  # 最近5个周期
    
    for cycle_time in recent_cycles:
        blocks = cycle_blocks[cycle_time]
        print(f"\n{cycle_time} UTC:")
        if blocks:
            for symbol, reason in blocks[:3]:  # 显示前3个阻挡
                print(f"  {symbol}: {reason}")
            if len(blocks) > 3:
                print(f"  ... 还有 {len(blocks) - 3} 个阻挡")
        else:
            print("  无阻挡")
    
    # 分析主要阻挡模式
    print("\n=== 主要阻挡模式分析 ===")
    
    # 1. TREND信号不足分析
    trend_insufficient = [r for r in block_reasons.keys() if 'TREND信号不足' in r]
    if trend_insufficient:
        print("\n1. TREND信号不足相关:")
        total_trend = sum(block_reasons[r] for r in trend_insufficient)
        print(f"   总次数: {total_trend}")
        for reason in trend_insufficient:
            count = block_reasons[reason]
            print(f"   - {reason}: {count}次")
    
    # 2. RANGE分位数不足分析
    range_insufficient = [r for r in block_reasons.keys() if 'range_quantile_not_ready' in r]
    if range_insufficient:
        print("\n2. RANGE分位数准备不足:")
        total_range = sum(block_reasons[r] for r in range_insufficient)
        print(f"   总次数: {total_range}")
        for reason in range_insufficient:
            count = block_reasons[reason]
            print(f"   - {reason}: {count}次")
    
    # 3. ADX区间阻挡分析
    adx_blocks = [r for r in block_reasons.keys() if 'adx_no_trade' in r]
    if adx_blocks:
        print("\n3. ADX区间阻挡:")
        total_adx = sum(block_reasons[r] for r in adx_blocks)
        print(f"   总次数: {total_adx}")
        for reason in adx_blocks:
            count = block_reasons[reason]
            print(f"   - {reason}: {count}次")
    
    # 4. 波动率冷却分析
    volatility_blocks = [r for r in block_reasons.keys() if '极端波动冷却' in r]
    if volatility_blocks:
        print("\n4. 极端波动冷却:")
        total_volatility = sum(block_reasons[r] for r in volatility_blocks)
        print(f"   总次数: {total_volatility}")
        for reason in volatility_blocks:
            count = block_reasons[reason]
            print(f"   - {reason}: {count}次")
    
    # 5. 信号池过滤分析
    signal_pool_blocks = [r for r in block_reasons.keys() if 'signal_pool过滤未通过' in r]
    if signal_pool_blocks:
        print("\n5. 信号池过滤失败:")
        total_signal_pool = sum(block_reasons[r] for r in signal_pool_blocks)
        print(f"   总次数: {total_signal_pool}")
        for reason in signal_pool_blocks:
            count = block_reasons[reason]
            print(f"   - {reason}: {count}次")

if __name__ == "__main__":
    log_file = r"d:\AIDCA\AI2\logs\2026-02\2026-02-26\runtime.out.00.log"
    analyze_logs(log_file)