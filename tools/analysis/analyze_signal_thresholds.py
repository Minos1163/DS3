#!/usr/bin/env python3
"""
分析信号阈值和分数情况，找出开仓阻挡的具体数值原因
"""

import re
from collections import defaultdict, Counter
import statistics

def analyze_signal_scores(log_file_path):
    """分析信号分数和阈值对比"""
    
    print("=== 信号阈值分析 ===\n")
    
    with open(log_file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 收集所有币种的信号分数数据
    symbol_data = defaultdict(lambda: {
        'long_scores': [],
        'short_scores': [],
        'cvd_values': [],
        'imbalance_values': [],
        'adx_values': [],
        'atr_pct_values': []
    })
    
    # 匹配每个币种的数据
    symbol_pattern = r'\[(\w+USDT)\] 决策=HOLD.*?信号评分: long=([0-9.]+), short=([0-9.]+).*?' \
                    r'3\.0评分: score_15m\(L/S\)=([0-9./]+), score_5m\(L/S\)=([0-9./]+), final_score\(L/S\)=([0-9./]+).*?' \
                    r'引擎上下文: engine=(\w+),.*?adx=([0-9.]+), atr_pct=([0-9.]+).*?' \
                    r'资金流: cvd=([+-][0-9.]+),.*?imbalance=([+-][0-9.]+)'
    
    matches = re.findall(symbol_pattern, content, re.DOTALL)
    
    for match in matches:
        symbol = match[0]
        long_score = float(match[1])
        short_score = float(match[2])
        score_15m = match[3]  # 格式: 0.003/0.174
        score_5m = match[4]   # 格式: 0.002/0.074
        final_score = match[5] # 格式: 0.002/0.134
        engine = match[6]
        adx = float(match[7])
        atr_pct = float(match[8])
        cvd = float(match[9])
        imbalance = float(match[10])
        
        data = symbol_data[symbol]
        data['long_scores'].append(long_score)
        data['short_scores'].append(short_score)
        data['cvd_values'].append(cvd)
        data['imbalance_values'].append(imbalance)
        data['adx_values'].append(adx)
        data['atr_pct_values'].append(atr_pct)
    
    # 分析各个币种的情况
    print("=== 各币种信号分数统计 ===")
    for symbol, data in symbol_data.items():
        if not data['long_scores']:
            continue
            
        avg_long = statistics.mean(data['long_scores'])
        avg_short = statistics.mean(data['short_scores'])
        max_long = max(data['long_scores'])
        max_short = max(data['short_scores'])
        
        print(f"\n{symbol}:")
        print(f"  平均分数 - 多头: {avg_long:.3f}, 空头: {avg_short:.3f}")
        print(f"  最高分数 - 多头: {max_long:.3f}, 空头: {max_short:.3f}")
        print(f"  CVD平均值: {statistics.mean(data['cvd_values']):+.4f}")
        print(f"  不平衡度平均值: {statistics.mean(data['imbalance_values']):+.4f}")
        print(f"  ADX平均值: {statistics.mean(data['adx_values']):.2f}")
        print(f"  ATR%平均值: {statistics.mean(data['atr_pct_values'])*100:.2f}%")
    
    # 分析阈值对比
    print("\n=== 阈值对比分析 ===")
    
    # 当前配置中的阈值
    config_thresholds = {
        'trend_long_cvd': 0.0008,
        'trend_long_imb': 0.1,
        'trend_short_cvd': -0.0008,
        'trend_short_imb': -0.1,
        'trend_long_score': 0.22,
        'trend_short_score': 0.22,
        'range_long_score': 0.4,
        'range_short_score': 0.4,
        'adx_min': 18,
        'adx_max': 21,
        'atr_pct_min': 0.0012,
        'atr_pct_max': 0.02
    }
    
    print("当前配置阈值:")
    for key, value in config_thresholds.items():
        if isinstance(value, float) and abs(value) < 1:
            if value > 0:
                print(f"  {key}: {value:.4f}")
            else:
                print(f"  {key}: {value:.4f}")
        else:
            print(f"  {key}: {value}")
    
    # 分析哪些币种接近阈值
    print("\n=== 接近阈值的信号 ===")
    near_threshold_signals = []
    
    for symbol, data in symbol_data.items():
        if not data['long_scores']:
            continue
            
        max_long = max(data['long_scores'])
        max_short = max(data['short_scores'])
        avg_adx = statistics.mean(data['adx_values'])
        avg_atr = statistics.mean(data['atr_pct_values'])
        
        # 检查是否接近阈值
        if max_long >= 0.18:  # 接近0.22阈值
            near_threshold_signals.append({
                'symbol': symbol,
                'type': 'long',
                'max_score': max_long,
                'avg_adx': avg_adx,
                'avg_atr': avg_atr
            })
            
        if max_short >= 0.18:  # 接近0.22阈值
            near_threshold_signals.append({
                'symbol': symbol,
                'type': 'short',
                'max_score': max_short,
                'avg_adx': avg_adx,
                'avg_atr': avg_atr
            })
    
    if near_threshold_signals:
        print("接近开仓阈值的币种:")
        for signal in sorted(near_threshold_signals, key=lambda x: x['max_score'], reverse=True):
            print(f"  {signal['symbol']} ({signal['type']}): "
                  f"最高分数={signal['max_score']:.3f}, "
                  f"平均ADX={signal['avg_adx']:.1f}, "
                  f"平均ATR%={signal['avg_atr']*100:.2f}%")
    else:
        print("没有币种接近开仓阈值")
    
    # 分析资金流指标分布
    print("\n=== 资金流指标分析 ===")
    all_cvd = []
    all_imbalance = []
    
    for data in symbol_data.values():
        all_cvd.extend(data['cvd_values'])
        all_imbalance.extend(data['imbalance_values'])
    
    if all_cvd:
        print(f"CVD分布:")
        print(f"  平均值: {statistics.mean(all_cvd):+.4f}")
        print(f"  标准差: {statistics.stdev(all_cvd):.4f}")
        print(f"  最小值: {min(all_cvd):+.4f}")
        print(f"  最大值: {max(all_cvd):+.4f}")
        
        print(f"\n不平衡度分布:")
        print(f"  平均值: {statistics.mean(all_imbalance):+.4f}")
        print(f"  标准差: {statistics.stdev(all_imbalance):.4f}")
        print(f"  最小值: {min(all_imbalance):+.4f}")
        print(f"  最大值: {max(all_imbalance):+.4f}")

if __name__ == "__main__":
    log_file = r"d:\AIDCA\AI2\logs\2026-02\2026-02-26\runtime.out.00.log"
    analyze_signal_scores(log_file)