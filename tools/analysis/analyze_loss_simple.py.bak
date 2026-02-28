"""
简化版亏损分析 - 直接统计平仓事件
"""
import re

def analyze_losses_simple():
    with open('backtest_log_SOLUSDT_20260127_113633.txt', 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    
    # 找所有平仓事件
    close_events = []
    for i, line in enumerate(lines):
        if '❌ [2026-01-' in line and '平仓' in line:
            close_events.append({
                'line': i,
                'text': line,
                'is_loss': True
            })
        elif '✅ [2026-01-' in line and '平仓' in line:
            close_events.append({
                'line': i,
                'text': line,
                'is_loss': False
            })
    
    print(f"找到 {len(close_events)} 个平仓事件\n")
    
    # 分析每个平仓事件
    losses = []
    wins = []
    
    for event in close_events:
        # 提取信息
        time_match = re.search(r'\[2026-01-(\d{2} \d{2}:\d{2}:\d{2})\]', event['text'])
        price_match = re.search(r'@ ([\d.]+)', event['text'])
        pnl_match = re.search(r'盈亏: ([+-][\d.]+)', event['text'])
        pnl_pct_match = re.search(r'([+-][\d.]+)%\)', event['text'])
        
        if time_match and price_match and pnl_match:
            trade = {
                'time': f"01-{time_match.group(1)}",
                'close_price': float(price_match.group(1)),
                'pnl': float(pnl_match.group(1)),
                'pnl_pct': float(pnl_pct_match.group(1)) if pnl_pct_match else 0
            }
            
            # 查找对应的开仓和平仓原因
            for j in range(event['line']-1, max(0, event['line']-100), -1):
                if '开仓价:' in lines[j]:
                    open_price_match = re.search(r'价格: ([\d.]+)', lines[j+1] if j+1 < len(lines) else '')
                    if not open_price_match:
                        open_price_match = re.search(r'价格: ([\d.]+)', lines[j])
                    break
            
            for j in range(event['line'], min(event['line']+5, len(lines))):
                if '原因:' in lines[j]:
                    reason_match = re.search(r'原因: (.+)', lines[j])
                    if reason_match:
                        trade['reason'] = reason_match.group(1)
                    break
            
            if event['is_loss']:
                losses.append(trade)
            else:
                wins.append(trade)
    
    print("="*80)
    print("📊 交易统计")
    print("="*80)
    print(f"总交易: {len(losses) + len(wins)}")
    print(f"亏损: {len(losses)} ({len(losses)/(len(losses)+len(wins))*100:.1f}%)")
    print(f"盈利: {len(wins)} ({len(wins)/(len(losses)+len(wins))*100:.1f}%)")
    
    print(f"\n💰 亏损情况:")
    total_loss = sum(t['pnl'] for t in losses)
    print(f"总亏损: {total_loss:.2f} USDT")
    print(f"平均亏损: {total_loss/len(losses):.2f} USDT ({total_loss/len(losses)/100*100:.2f}%)" if losses else "N/A")
    print(f"最大亏损: {min(t['pnl'] for t in losses):.2f} USDT")
    
    print(f"\n✅ 盈利情况:")
    total_profit = sum(t['pnl'] for t in wins)
    print(f"总盈利: {total_profit:.2f} USDT")
    print(f"平均盈利: {total_profit/len(wins):.2f} USDT ({total_profit/len(wins)/100*100:.2f}%)" if wins else "N/A")
    print(f"最大盈利: {max(t['pnl'] for t in wins):.2f} USDT")
    
    # 分析亏损原因
    print(f"\n" + "="*80)
    print("❌ 亏损原因分析")
    print("="*80)
    
    rsi_loss = [t for t in losses if 'RSI' in t.get('reason', '')]
    tp_loss = [t for t in losses if '止盈' in t.get('reason', '')]
    sl_loss = [t for t in losses if '止损' in t.get('reason', '')]
    
    print(f"\nRSI平仓导致: {len(rsi_loss)}笔 ({len(rsi_loss)/len(losses)*100:.1f}%)")
    if rsi_loss:
        rsi_pnl = sum(t['pnl'] for t in rsi_loss)
        print(f"  总亏损: {rsi_pnl:.2f} USDT")
        print(f"  平均: {rsi_pnl/len(rsi_loss):.2f} USDT")
    
    print(f"\n止盈触发导致: {len(tp_loss)}笔")
    print(f"止损触发导致: {len(sl_loss)}笔")
    
    # 分析RSI值分布
    print(f"\n" + "="*80)
    print("📈 RSI平仓触发的RSI值分布")
    print("="*80)
    
    rsi_values = []
    for loss in rsi_loss:
        reason = loss.get('reason', '')
        rsi_match = re.search(r'RSI回归中性区域\(([\d.]+)\)', reason)
        if rsi_match:
            rsi_values.append(float(rsi_match.group(1)))
    
    if rsi_values:
        print(f"触发RSI值: {rsi_values}")
        print(f"平均RSI: {sum(rsi_values)/len(rsi_values):.1f}")
        print(f"范围: {min(rsi_values):.1f} - {max(rsi_values):.1f}")
        
        # 统计各范围
        in_range = [v for v in rsi_values if 47 <= v <= 53]
        print(f"在47-53范围内: {len(in_range)}/{len(rsi_values)}笔")
    
    # 核心问题
    print(f"\n" + "="*80)
    print("🔍 核心问题")
    print("="*80)
    
    print(f"""
【发现1】RSI平仓触发太频繁
  - 所有{len(losses)}笔亏损中，{len(rsi_loss)}笔（{len(rsi_loss)/len(losses)*100:.0f}%）是RSI平仓导致
  - 问题: 即使是47-53的范围，RSI仍在波动，容易虚假触发
  
【发现2】盈亏比不足
  - 平均亏损: {total_loss/len(losses):.2f} USDT
  - 平均盈利: {total_profit/len(wins):.2f} USDT if wins else 'N/A'
  - 盈亏比: {(total_profit/len(wins))/(abs(total_loss/len(losses))):.1f}:1 if wins and losses else 'N/A'
  - 需要至少2:1的盈亏比才能在30%胜率下盈利
  
【发现3】RSI值在中性区域波动导致频繁平仓
  - RSI 50是完全中性
  - 47-53范围内RSI在振荡
  - 每次振荡都会触发平仓，导致亏损
""")
    
    print(f"\n" + "="*80)
    print("💡 解决方案 (优先级排序)")
    print("="*80)
    
    print(f"""
【方案1】完全禁用RSI平仓 ⭐⭐⭐ (推荐)
  做法: 删除RSI平仓逻辑，仅依赖止盈3%和止损2%
  原理: RSI在中性区频繁振荡，导致虚假平仓
       只有明确的止盈/止损才能平仓
  预期: 
    ✅ 减少虚假平仓，给利润充分奔跑空间
    ✅ 盈利单能达到3%止盈（2笔大单+3.88, +4.99就能覆盖所有损失）
    ❌ 可能出现更大单笔亏损（需要严格止损2%）
  效果: 胜率可能降至25%，但盈亏比提升到3:1以上，最终仍盈利

【方案2】动态RSI平仓：根据浮盈/浮亏调整阈值 ⭐⭐ (次推荐)
  做法:
    - 浮亏时: 放宽RSI范围到45-55（给翻身机会）
    - 浮盈时: 紧缩RSI范围到48-52（快速获利）
    - 严格保护浮亏>1%的单子
  预期: 胜率提升到35%+，但逻辑复杂

【方案3】提升入场信号强度 ⭐ (保守)
  做法: signal_threshold从3改为4，只在4/6强信号时入场
  原理: 弱信号导致更多亏损
  预期: 胜率提升，但交易机会减少50%，收益降低

【方案4】多层保护止损 ⭐ (急救方案)
  做法:
    - 主止损: 2%（现有）
    - 浮亏0.5% + RSI>55: 立即止损（防止做空反向）
    - 浮亏>1% + MACD>0: 立即止损（防止趋势反转）
  预期: 减少最坏情况，但可能止损过多

【最终建议】采用方案1 + 方案2组合
  第一步: 禁用RSI平仓，运行1000根K线回测
  第二步: 如果胜率太低，启用动态RSI平仓保护
  第三步: 监控结果，逐步优化
""")
    
    print(f"\n" + "="*80)
    print("📋 亏损交易详表（前10笔）")
    print("="*80)
    
    losses_sorted = sorted(losses, key=lambda x: x['pnl'])
    for i, loss in enumerate(losses_sorted[:10], 1):
        print(f"{i}. {loss['time']} | 平仓价{loss['close_price']:.2f} | {loss['pnl']:+.2f}USDT ({loss['pnl_pct']:+.2f}%)")
        reason = loss.get('reason', '未知')[:70]
        print(f"   {reason}")

if __name__ == '__main__':
    analyze_losses_simple()
