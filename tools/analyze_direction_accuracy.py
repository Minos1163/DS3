#!/usr/bin/env python3
"""分析 LW 和 EV 方向判断准确率 - 从 runtime.out.*.log 文件"""
import os
import re
from collections import defaultdict

def analyze_runtime_logs():
    log_dir = r'D:\AIDCA\AI2\logs\2026-02\2026-02-26'
    log_files = [f for f in os.listdir(log_dir) if f.startswith('runtime.out') and f.endswith('.log')]
    
    print(f'找到日志文件: {sorted(log_files)}')
    
    # 存储所有记录
    all_directions = []  # 所有方向判断记录
    trades = []  # 交易决策
    close_results = []  # 平仓结果
    
    for log_file in sorted(log_files):
        filepath = os.path.join(log_dir, log_file)
        current_symbol = None
        current_cycle = None
        current_time = None
        
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        for i, line in enumerate(lines):
            # 解析 cycle 时间
            cycle_match = re.search(r'FUND_FLOW cycle (\d+) @ ([\d-]+ [\d:]+)', line)
            if cycle_match:
                current_cycle = int(cycle_match.group(1))
                current_time = cycle_match.group(2)
            
            # 解析 symbol 和决策
            symbol_match = re.search(r'\[([A-Z]+USDT)\] 决策=(\w+)', line)
            if symbol_match:
                current_symbol = symbol_match.group(1)
                decision = symbol_match.group(2)
                
                if decision in ('BUY', 'SELL'):
                    # 向后查找方向判断
                    lw_dir, lw_score, ev_dir, ev_score = None, None, None, None
                    for j in range(i+1, min(i+10, len(lines))):
                        dir_match = re.search(r'方向判断: dir_lw=(\w+)\(([+-][\d.]+)\) \| dir_ev=(\w+)\(([+-][\d.]+)\)', lines[j])
                        if dir_match:
                            lw_dir = dir_match.group(1)
                            lw_score = float(dir_match.group(2))
                            ev_dir = dir_match.group(3)
                            ev_score = float(dir_match.group(4))
                            break
                    
                    trades.append({
                        'cycle': current_cycle,
                        'time': current_time,
                        'symbol': current_symbol,
                        'decision': decision,
                        'lw_dir': lw_dir,
                        'lw_score': lw_score,
                        'ev_dir': ev_dir,
                        'ev_score': ev_score,
                        'file': log_file
                    })
            
            # 解析方向判断记录（所有）
            dir_match = re.search(r'方向判断: dir_lw=(\w+)\(([+-][\d.]+)\) \| dir_ev=(\w+)\(([+-][\d.]+)\)', line)
            if dir_match and current_symbol:
                all_directions.append({
                    'cycle': current_cycle,
                    'time': current_time,
                    'symbol': current_symbol,
                    'lw_dir': dir_match.group(1),
                    'lw_score': float(dir_match.group(2)),
                    'ev_dir': dir_match.group(3),
                    'ev_score': float(dir_match.group(4))
                })
    
    print(f'\n=== 方向判断统计 ===')
    print(f'总方向判断记录: {len(all_directions)} 条')
    print(f'交易决策记录: {len(trades)} 条')
    
    # 统计 LW 和 EV 方向分布
    lw_dist = defaultdict(int)
    ev_dist = defaultdict(int)
    for d in all_directions:
        lw_dist[d['lw_dir']] += 1
        ev_dist[d['ev_dir']] += 1
    
    print(f'\nLW方向分布: {dict(lw_dist)}')
    print(f'EV方向分布: {dict(ev_dist)}')
    
    # 分析 LW vs EV 一致性
    agree_count = sum(1 for d in all_directions if d['lw_dir'] == d['ev_dir'] or d['lw_dir'] == 'BOTH' or d['ev_dir'] == 'BOTH')
    print(f'\nLW/EV 一致率: {agree_count}/{len(all_directions)} = {agree_count/len(all_directions)*100:.1f}%')
    
    # 显示交易决策
    if trades:
        print(f'\n=== 交易决策记录 ({len(trades)}条) ===')
        for t in trades[:30]:
            lw_tag = f"LW={t['lw_dir']}({t['lw_score']:+.2f})" if t['lw_dir'] else "LW=?"
            ev_tag = f"EV={t['ev_dir']}({t['ev_score']:+.2f})" if t['ev_dir'] else "EV=?"
            print(f"Cycle {t['cycle']}: {t['symbol']} {t['decision']} {lw_tag} {ev_tag}")
    
    # 分析不同方向的得分分布
    print(f'\n=== 得分分布分析 ===')
    lw_long_scores = [d['lw_score'] for d in all_directions if d['lw_dir'] == 'LONG']
    lw_short_scores = [d['lw_score'] for d in all_directions if d['lw_dir'] == 'SHOR']
    lw_both_scores = [d['lw_score'] for d in all_directions if d['lw_dir'] == 'BOTH']
    
    if lw_long_scores:
        print(f'LW LONG 得分: mean={sum(lw_long_scores)/len(lw_long_scores):.3f}, n={len(lw_long_scores)}')
    if lw_short_scores:
        print(f'LW SHORT 得分: mean={sum(lw_short_scores)/len(lw_short_scores):.3f}, n={len(lw_short_scores)}')
    if lw_both_scores:
        print(f'LW BOTH 得分: mean={sum(lw_both_scores)/len(lw_both_scores):.3f}, n={len(lw_both_scores)}')
    
    return trades, all_directions

if __name__ == '__main__':
    analyze_runtime_logs()
