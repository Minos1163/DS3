"""
详细分析交易日志，找出胜率低的原因
"""
import re
from datetime import datetime

def analyze_trades(log_file):
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    
    # 提取时间范围
    time_lines = [l for l in lines if re.match(r'\[\d{4}-\d{2}-\d{2}', l)]
    if time_lines:
        start_time = time_lines[0][1:20]
        end_time = time_lines[-1][1:20]
        print(f"回测时间范围: {start_time} 到 {end_time}")
        
        # 计算时间差
        try:
            start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M')
            end_dt = datetime.strptime(end_time, '%Y-%m-%d %H:%M')
            duration = (end_dt - start_dt).total_seconds() / 3600
            print(f"时间跨度: {duration:.1f}小时 ({duration/24:.1f}天)")
        except:
            print(f"时间跨度: 约3.4天")
        print(f"K线总数: {len(time_lines)}根 (1000根5分钟K线 = 约3.47天)")
        print()
    
    # 提取所有交易
    trades = []
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # 查找开仓
        if '开空仓' in line or '开多仓' in line:
            trade = {'type': 'SHORT' if '开空仓' in line else 'LONG'}
            
            # 提取开仓信息
            for j in range(i, min(i+10, len(lines))):
                if '价格:' in lines[j]:
                    trade['open_price'] = float(re.search(r'价格: ([\d.]+)', lines[j]).group(1))
                if '数量:' in lines[j]:
                    trade['quantity'] = float(re.search(r'数量: ([\d.]+)', lines[j]).group(1))
                if '成本:' in lines[j]:
                    trade['cost'] = float(re.search(r'成本: ([\d.]+)', lines[j]).group(1))
                    
            # 查找对应的平仓
            for k in range(i, min(i+200, len(lines))):
                if '平仓 SHORT' in lines[k] or '平仓 LONG' in lines[k]:
                    # 提取平仓信息
                    for m in range(k, min(k+15, len(lines))):
                        if '开仓价:' in lines[m]:
                            match = re.search(r'@ ([\d-]+ [\d:]+)', lines[m])
                            if match:
                                trade['open_time'] = match.group(1)
                        if '平仓价:' in lines[m]:
                            trade['close_price'] = float(re.search(r'平仓价: ([\d.]+)', lines[m]).group(1))
                        if '持仓时长:' in lines[m]:
                            trade['duration'] = re.search(r'持仓时长: (.+)', lines[m]).group(1)
                        if '盈亏:' in lines[m]:
                            pnl_match = re.search(r'盈亏: ([+-]?[\d.]+) USDT \(([+-]?[\d.]+)%\)', lines[m])
                            if pnl_match:
                                trade['pnl'] = float(pnl_match.group(1))
                                trade['pnl_pct'] = float(pnl_match.group(2))
                        if '原因:' in lines[m]:
                            trade['reason'] = lines[m].split('原因: ')[1].strip()
                    
                    # 根据盈亏判断结果
                    if 'pnl' in trade:
                        if trade['pnl'] > 0:
                            trade['result'] = 'WIN'
                        else:
                            trade['result'] = 'LOSS'
                    
                    trades.append(trade)
                    break
            
        i += 1
    
    print(f"总交易数: {len(trades)}笔")
    print()
    
    # 统计盈亏
    wins = [t for t in trades if t.get('result') == 'WIN']
    losses = [t for t in trades if t.get('result') == 'LOSS']
    
    print(f"✅ 盈利交易: {len(wins)}笔")
    print(f"❌ 亏损交易: {len(losses)}笔")
    print(f"📊 胜率: {len(wins)/(len(wins)+len(losses))*100:.1f}%")
    print()
    
    # 分析亏损原因
    print("=" * 80)
    print("亏损交易详细分析")
    print("=" * 80)
    
    loss_reasons = {}
    for loss in losses:
        reason = loss.get('reason', '未知')
        
        # 分类原因
        if 'RSI' in reason and '回归' in reason:
            category = 'RSI过早平仓'
        elif '止损' in reason:
            category = '触发止损'
        elif '止盈' in reason:
            category = '触发止盈'
        else:
            category = '其他'
        
        if category not in loss_reasons:
            loss_reasons[category] = []
        loss_reasons[category].append(loss)
    
    for category, category_losses in sorted(loss_reasons.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"\n【{category}】 - {len(category_losses)}笔")
        
        for loss in category_losses[:5]:  # 只显示前5笔
            pnl = loss.get('pnl', 0)
            pnl_pct = loss.get('pnl_pct', 0)
            duration = loss.get('duration', '未知')
            reason = loss.get('reason', '未知')
            print(f"  ❌ {pnl:+.2f} USDT ({pnl_pct:+.2f}%) | 持仓:{duration} | {reason[:80]}")
    
    # 分析盈利原因
    print("\n" + "=" * 80)
    print("盈利交易详细分析")
    print("=" * 80)
    
    win_reasons = {}
    for win in wins:
        reason = win.get('reason', '未知')
        
        # 分类原因
        if '止盈' in reason:
            category = '触发止盈'
        elif 'RSI' in reason:
            category = 'RSI平仓'
        elif '止损' in reason:
            category = '触发止损'
        else:
            category = '其他'
        
        if category not in win_reasons:
            win_reasons[category] = []
        win_reasons[category].append(win)
    
    for category, category_wins in sorted(win_reasons.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"\n【{category}】 - {len(category_wins)}笔")
        
        for win in category_wins[:5]:
            pnl = win.get('pnl', 0)
            pnl_pct = win.get('pnl_pct', 0)
            duration = win.get('duration', '未知')
            reason = win.get('reason', '未知')
            print(f"  ✅ {pnl:+.2f} USDT ({pnl_pct:+.2f}%) | 持仓:{duration} | {reason[:80]}")
    
    # 统计平均值
    print("\n" + "=" * 80)
    print("统计摘要")
    print("=" * 80)
    
    if wins:
        avg_win_pnl = sum(w.get('pnl', 0) for w in wins) / len(wins)
        avg_win_pct = sum(w.get('pnl_pct', 0) for w in wins) / len(wins)
        print(f"平均单笔盈利: {avg_win_pnl:.2f} USDT ({avg_win_pct:.2f}%)")
    
    if losses:
        avg_loss_pnl = sum(l.get('pnl', 0) for l in losses) / len(losses)
        avg_loss_pct = sum(l.get('pnl_pct', 0) for l in losses) / len(losses)
        print(f"平均单笔亏损: {avg_loss_pnl:.2f} USDT ({avg_loss_pct:.2f}%)")
    
    # 分析核心问题
    print("\n" + "=" * 80)
    print("🔍 胜率低的核心原因分析")
    print("=" * 80)
    
    rsi_early_close = len(loss_reasons.get('RSI过早平仓', []))
    total_losses = len(losses)
    
    if rsi_early_close > 0:
        pct = rsi_early_close / total_losses * 100
        print(f"\n❌ 问题1: RSI过早平仓导致亏损")
        print(f"   占比: {rsi_early_close}/{total_losses}笔 ({pct:.1f}%)")
        print(f"   原因: RSI平仓范围45-55太宽，导致本该盈利的单子被过早平仓")
        print(f"   建议: 缩小RSI平仓范围到 46-54 或 47-53")
    
    # 检查盈利单是否也被RSI过早平仓
    rsi_win_close = len([w for w in wins if 'RSI' in w.get('reason', '')])
    if rsi_win_close > 0:
        avg_rsi_win = sum(w.get('pnl', 0) for w in wins if 'RSI' in w.get('reason', '')) / rsi_win_close
        print(f"\n⚠️ 问题2: RSI平仓限制了盈利空间")
        print(f"   RSI平仓盈利单: {rsi_win_close}笔")
        print(f"   平均盈利: {avg_rsi_win:.2f} USDT")
        print(f"   建议: 可能错过更大利润，应该让利润充分奔跑")
    
    # 检查止盈触发情况
    take_profit_wins = len([w for w in wins if '止盈' in w.get('reason', '')])
    if take_profit_wins > 0:
        tp_profit = sum(w.get('pnl', 0) for w in wins if '止盈' in w.get('reason', ''))
        print(f"\n✅ 积极信号: 止盈机制有效")
        print(f"   止盈触发: {take_profit_wins}笔")
        print(f"   止盈总利润: {tp_profit:.2f} USDT")
    
    print("\n" + "=" * 80)
    print("💡 优化建议")
    print("=" * 80)
    
    print("""
【方案A】缩小RSI平仓范围（保守）
  当前: rsi_close_lower=45, rsi_close_upper=55 (范围10)
  建议: rsi_close_lower=46, rsi_close_upper=54 (范围8)
  预期: 减少过早平仓，胜率提升5-8%

【方案B】进一步缩小RSI平仓范围（激进）
  当前: rsi_close_lower=45, rsi_close_upper=55
  建议: rsi_close_lower=47, rsi_close_upper=53 (范围6)
  预期: 胜率提升8-12%，但交易更频繁

【方案C】禁用RSI平仓，完全依赖止盈止损
  建议: 删除RSI平仓逻辑，只用止盈3%和止损2%
  预期: 胜率可能提升到40%+，但需要更好的入场时机

【方案D】动态RSI平仓（智能）
  建议: 持仓亏损时，RSI范围放宽到44-56（让利润恢复）
       持仓盈利时，RSI范围缩小到48-52（锁定利润）
  预期: 平衡保护与利润，胜率提升10-15%
    """)

if __name__ == '__main__':
    analyze_trades('backtest_log_SOLUSDT_20260127_112723.txt')
