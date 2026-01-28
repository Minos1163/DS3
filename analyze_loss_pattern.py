"""
深度分析亏损交易的特征和模式
找出降低亏损的关键方案
"""
import re
from datetime import datetime

def parse_detailed_trades(log_file):
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    trades = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # 查找开仓信息
        if '[2026-01-' in line and '开空仓' in line:
            trade = {}
            
            # 提取开仓时间、价格、信号
            time_match = re.search(r'\[2026-01-(\d{2} \d{2}:\d{2}:\d{2})\]', line)
            price_match = re.search(r'@ ([\d.]+)', line)
            signal_match = re.search(r'做空信号\((\d)/(\d)\)', line)
            
            if time_match and price_match and signal_match:
                trade['open_time'] = f"01-{time_match.group(1)}"
                trade['open_price'] = float(price_match.group(1))
                trade['signal_count'] = f"{signal_match.group(1)}/{signal_match.group(2)}"
                
                # 提取开仓原因
                reason_match = re.search(r'做空信号.*?: (.+)', line)
                if reason_match:
                    trade['open_reason'] = reason_match.group(1)
            
            # 查找对应的平仓
            for j in range(i+1, min(i+200, len(lines))):
                if '平仓 SHORT' in lines[j]:
                    # 提取平仓信息
                    close_line = lines[j]
                    close_time_match = re.search(r'\[2026-01-(\d{2} \d{2}:\d{2}:\d{2})\]', close_line)
                    close_price_match = re.search(r'@ ([\d.]+)', close_line)
                    pnl_match = re.search(r'盈亏: ([+-][\d.]+)', close_line)
                    pnl_pct_match = re.search(r'([+-][\d.]+)%\)', close_line)
                    
                    if close_time_match and close_price_match and pnl_match:
                        trade['close_time'] = f"01-{close_time_match.group(1)}"
                        trade['close_price'] = float(close_price_match.group(1))
                        trade['pnl'] = float(pnl_match.group(1))
                        trade['pnl_pct'] = float(pnl_pct_match.group(1)) if pnl_pct_match else 0
                        
                        # 提取平仓原因
                        reason_line = None
                        for k in range(j, min(j+5, len(lines))):
                            if '原因:' in lines[k]:
                                reason_line = lines[k]
                                break
                        
                        if reason_line:
                            reason_match = re.search(r'原因: (.+)', reason_line)
                            if reason_match:
                                trade['close_reason'] = reason_match.group(1)
                        
                        # 判断盈亏
                        trade['result'] = 'WIN' if trade['pnl'] > 0 else 'LOSS'
                        
                        trades.append(trade)
                    break
            
        i += 1
    
    return trades

def analyze_loss_trades(trades):
    """分析所有亏损交易的特征"""
    
    losses = [t for t in trades if t['result'] == 'LOSS']
    wins = [t for t in trades if t['result'] == 'WIN']
    
    print("\n" + "="*80)
    print("亏损交易深度分析")
    print("="*80)
    
    print(f"\n📊 基本统计:")
    print(f"  总交易数: {len(trades)}")
    print(f"  亏损交易: {len(losses)}笔 ({len(losses)/len(trades)*100:.1f}%)")
    print(f"  盈利交易: {len(wins)}笔 ({len(wins)/len(trades)*100:.1f}%)")
    
    print(f"\n💰 亏损情况:")
    total_loss = sum(t['pnl'] for t in losses)
    avg_loss = total_loss / len(losses) if losses else 0
    max_loss = min(t['pnl'] for t in losses) if losses else 0
    min_loss = max(t['pnl'] for t in losses) if losses else 0
    
    print(f"  总亏损: {total_loss:.2f} USDT")
    print(f"  平均亏损: {avg_loss:.2f} USDT ({avg_loss/100*100:.2f}%)")
    print(f"  最大亏损: {max_loss:.2f} USDT")
    print(f"  最小亏损: {min_loss:.2f} USDT")
    
    # 分析入场信号强度
    print(f"\n🎯 入场信号强度分析:")
    signal_loss_map = {}
    for loss in losses:
        signal = loss.get('signal_count', '未知')
        if signal not in signal_loss_map:
            signal_loss_map[signal] = []
        signal_loss_map[signal].append(loss)
    
    for signal, loss_list in sorted(signal_loss_map.items()):
        avg = sum(l['pnl'] for l in loss_list) / len(loss_list)
        print(f"  信号强度 {signal}: {len(loss_list)}笔亏损, 平均亏损 {avg:.2f} USDT")
    
    # 分析平仓原因
    print(f"\n📍 平仓原因分析:")
    close_reason_map = {}
    for loss in losses:
        reason = loss.get('close_reason', '未知')
        # 简化原因描述
        if 'RSI回归' in reason:
            key = 'RSI平仓'
        elif '止损' in reason:
            key = '止损'
        elif '止盈' in reason:
            key = '止盈'
        else:
            key = '其他'
        
        if key not in close_reason_map:
            close_reason_map[key] = []
        close_reason_map[key].append(loss)
    
    for reason, loss_list in sorted(close_reason_map.items(), key=lambda x: len(x[1]), reverse=True):
        avg = sum(l['pnl'] for l in loss_list) / len(loss_list)
        print(f"  {reason}: {len(loss_list)}笔亏损, 平均亏损 {avg:.2f} USDT")
    
    # 分析价格变动
    print(f"\n📈 亏损交易的价格变动分析:")
    avg_move = sum(((t['close_price'] - t['open_price']) / t['open_price'] * 100) for t in losses) / len(losses) if losses else 0
    print(f"  平均价格变动: {avg_move:+.2f}%")
    
    losses_sorted = sorted(losses, key=lambda x: x['pnl'])
    print(f"\n最严重的5笔亏损:")
    for i, loss in enumerate(losses_sorted[:5], 1):
        move = (loss['close_price'] - loss['open_price']) / loss['open_price'] * 100
        signal = loss.get('signal_count', '?')
        reason = loss.get('close_reason', '?')
        print(f"  {i}. {loss['open_time']} → {loss['close_time']} | {loss['pnl']:+.2f} ({loss['pnl_pct']:+.2f}%) | 信号{signal} | 价格{move:+.2f}%")
        print(f"     平仓: {reason[:70]}")
    
    # 核心问题识别
    print(f"\n" + "="*80)
    print("🔍 核心问题识别")
    print("="*80)
    
    # 问题1: RSI平仓触发过于敏感
    rsi_loss = close_reason_map.get('RSI平仓', [])
    if len(rsi_loss) > 0:
        rsi_pct = len(rsi_loss) / len(losses) * 100
        print(f"\n❌ 问题1: RSI平仓过于敏感（导致{len(rsi_loss)}/{len(losses)}笔亏损，{rsi_pct:.1f}%）")
        print(f"   特征: 大部分亏损是在RSI 47-53范围内触发的")
        print(f"   分析: 即使缩小到47-53，RSI仍在波动，容易触发虚假平仓")
        
        # 统计RSI值
        rsi_values = []
        for loss in rsi_loss:
            reason = loss.get('close_reason', '')
            rsi_match = re.search(r'RSI回归中性区域\(([\d.]+)\)', reason)
            if rsi_match:
                rsi_values.append(float(rsi_match.group(1)))
        
        if rsi_values:
            avg_rsi = sum(rsi_values) / len(rsi_values)
            print(f"   平均RSI值: {avg_rsi:.1f}")
    
    # 问题2: 入场信号不够强
    weak_signals = [t for t in losses if '3/6' in t.get('signal_count', '')]
    if len(weak_signals) > 0:
        weak_pct = len(weak_signals) / len(losses) * 100
        print(f"\n⚠️ 问题2: 弱信号入场（{len(weak_signals)}/{len(losses)}笔亏损，{weak_pct:.1f}%）")
        print(f"   特征: 只有3/6信号的交易亏损率高")
    
    # 问题3: 价格反向波动
    reverse_moves = [t for t in losses if t['close_price'] > t['open_price']]  # 做空反向上涨
    if len(reverse_moves) > 0:
        print(f"\n📊 问题3: 做空后价格上涨（{len(reverse_moves)}/{len(losses)}笔亏损）")
        avg_up = sum(((t['close_price'] - t['open_price']) / t['open_price'] * 100) for t in reverse_moves) / len(reverse_moves)
        print(f"   平均上涨: {avg_up:+.2f}%")
    
    return losses, wins

def suggest_solutions(losses, trades):
    """提出优化方案"""
    
    print(f"\n" + "="*80)
    print("💡 优化方案建议")
    print("="*80)
    
    print(f"""
【方案A】禁用RSI平仓 - 仅依赖止盈止损
  做法: 删除RSI平仓逻辑，让交易只由止盈3%和止损2%管理
  优势: 
    ✅ 避免RSI虚假信号导致的亏损
    ✅ 让利润充分奔跑，只有明确的止盈止损才平仓
  劣势:
    ❌ 可能持仓时间更长
    ❌ 单个亏损可能变大
  预期: 胜率可能降至20-25%，但单笔盈亏比提升到3:1以上
  
【方案B】强化入场信号 - 提升信号质量
  做法: signal_threshold从3改为4，只在强信号(4/6以上)时入场
  优势:
    ✅ 减少弱信号导致的亏损
    ✅ 入场质量更高
  劣势:
    ❌ 交易机会大幅减少
    ❌ 收益可能下降
  预期: 胜率可能提升到40%+，但交易数量减少50%
  
【方案C】动态RSI平仓 - 根据浮亏/浮盈调整
  做法: 
    - 如果浮亏超过0.5%，放宽RSI平仓到45-55
    - 如果浮盈超过1%，紧缩RSI平仓到48-52（快速获利）
    - 严格保护浮亏大单（>1% RSI不动，等待止损）
  优势:
    ✅ 保护亏损头寸，让它有机会翻身
    ✅ 快速获利，避免利润回吐
  劣势:
    ❌ 逻辑复杂
    ❌ 需要更多测试
  预期: 胜率提升到35-40%，风险系数改善
  
【方案D】添加价格确认 - 多周期联动
  做法:
    - 只在MACD柱状体变负时做空（强确认）
    - 价格跌破EMA20时做空（趋势确认）
    - RSI<40 时禁用做空（防止抄底亏损）
  优势:
    ✅ 减少逆向波动导致的亏损
    ✅ 确保真实下跌趋势
  劣势:
    ❌ 信号更少，机会更少
  预期: 胜率提升到35%+，交易数量减少30%
  
【方案E】保守止损策略 - 宁可少赚也要少亏
  做法:
    - 止损从2%改为1.5%（更严格）
    - RSI平仓禁用（完全依赖止盈止损）
    - 第一笔亏损触发后，下一个交易提升到4/6信号
  优势:
    ✅ 快速止损，保护本金
    ✅ 亏损笔数减少
  劣势:
    ❌ 可能被虚假止损触发
    ❌ 利润回吐时容易止损
  预期: 胜率35-40%，但单笔亏损更小
""")

if __name__ == '__main__':
    trades = parse_detailed_trades('backtest_log_SOLUSDT_20260127_113633.txt')
    
    print(f"\n✅ 成功解析 {len(trades)} 笔交易")
    
    losses, wins = analyze_loss_trades(trades)
    suggest_solutions(losses, trades)
    
    # 输出具体的亏损交易列表
    print(f"\n" + "="*80)
    print("📋 所有亏损交易详表")
    print("="*80)
    
    losses_sorted = sorted(losses, key=lambda x: x['pnl'])
    for i, loss in enumerate(losses_sorted, 1):
        print(f"\n{i}. 时间: {loss['open_time']} → {loss['close_time']}")
        print(f"   价格: {loss['open_price']:.2f} → {loss['close_price']:.2f}")
        move = (loss['close_price'] - loss['open_price']) / loss['open_price'] * 100
        print(f"   变动: {move:+.2f}% | 盈亏: {loss['pnl']:+.2f} USDT ({loss['pnl_pct']:+.2f}%)")
        print(f"   信号: {loss.get('signal_count', '?')} | 入场: {loss.get('open_reason', '?')[:60]}")
        print(f"   平仓: {loss.get('close_reason', '?')[:60]}")
