"""分析回测日志，统计各种决策信息"""
import re
from collections import Counter

def analyze_log(log_file):
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 统计K线数量
    kline_pattern = r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] O='
    klines = re.findall(kline_pattern, content, re.MULTILINE)
    print(f"\n{'='*60}")
    print(f"📊 回测日志分析报告")
    print(f"{'='*60}\n")
    print(f"✅ K线记录总数: {len(klines)} 根")
    
    # 统计AI决策
    decision_pattern = r'AI决策: (\w+)'
    decisions = re.findall(decision_pattern, content)
    decision_counts = Counter(decisions)
    
    print(f"\n📈 AI决策统计:")
    print(f"   总决策次数: {len(decisions)}")
    for decision, count in decision_counts.most_common():
        print(f"   {decision}: {count} 次")
    
    # 统计交易
    open_long = len(re.findall(r'开多仓', content))
    open_short = len(re.findall(r'开空仓', content))
    close_trade = len(re.findall(r'平仓', content))
    
    print(f"\n💰 交易统计:")
    print(f"   开多仓: {open_long} 次")
    print(f"   开空仓: {open_short} 次")
    print(f"   平仓: {close_trade} 次")
    print(f"   总交易: {open_long + open_short} 笔")
    
    # 分析决策原因
    reason_pattern = r'AI决策: \w+ \(置信度:[\d.]+\) - (.+?)(?:\n|$)'
    reasons = re.findall(reason_pattern, content)
    
    # 统计SELL_OPEN决策的信号强度
    sell_signal_pattern = r'做空信号\((\d+)/6\)'
    sell_signals = re.findall(sell_signal_pattern, content)
    if sell_signals:
        signal_counts = Counter(sell_signals)
        print(f"\n📉 做空信号强度分布:")
        for signal, count in sorted(signal_counts.items(), key=lambda x: int(x[0]), reverse=True):
            print(f"   {signal}/6 指标满足: {count} 次")
    
    # 统计平仓原因
    close_pattern = r'平仓 (LONG|SHORT)\n.*?原因: (.+?)(?:\n|$)'
    closes = re.findall(close_pattern, content, re.DOTALL)
    if closes:
        close_reasons = Counter([reason.split('\n')[0].strip() for _, reason in closes])
        print(f"\n❌ 平仓原因统计:")
        for reason, count in close_reasons.most_common(5):
            print(f"   {reason}: {count} 次")
    
    # 计算交易频率
    if klines:
        trade_frequency = len(klines) / (open_long + open_short) if (open_long + open_short) > 0 else 0
        print(f"\n⏱️ 交易频率:")
        print(f"   平均每 {trade_frequency:.1f} 根K线发生一次交易")
        print(f"   相当于每 {trade_frequency * 5:.1f} 分钟一次交易")
        print(f"   交易率: {(open_long + open_short) / len(klines) * 100:.2f}%")
    
    # 分析为什么交易少
    print(f"\n💡 交易次数少的原因分析:")
    print(f"   1. 信号门槛: 需要至少4/6个指标同时满足才能开仓")
    print(f"   2. 交易冷却: 每次交易后需要等待8根K线(40分钟)")
    print(f"   3. RSI平仓: RSI回到47-53中性区间就会平仓")
    print(f"   4. 市场条件: 当前市场可能不满足做空条件的时间较多")
    
    # 统计仓位情况
    position_lines = re.findall(r'仓位:(\S+)', content)
    position_counts = Counter(position_lines)
    print(f"\n📊 仓位分布:")
    for pos, count in position_counts.most_common():
        percent = count / len(position_lines) * 100 if position_lines else 0
        print(f"   {pos}: {count} 次 ({percent:.1f}%)")

if __name__ == '__main__':
    import glob
    import os
    
    # 找到最新的日志文件
    log_files = glob.glob('backtest_log_SOLUSDT_*.txt')
    if log_files:
        latest_log = max(log_files, key=os.path.getctime)
        print(f"分析文件: {latest_log}")
        analyze_log(latest_log)
    else:
        print("❌ 未找到日志文件")
