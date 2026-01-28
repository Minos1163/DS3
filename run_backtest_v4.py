#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行优化回测 V4 (基于V3的进一步改进)
改进内容:
- max_hold_bars: 60 → 20 (防止长期反向持仓)
- take_profit_percent: 2.5% → 1.2% (更现实的目标)
- stop_loss_percent: 1.2% → 0.8% (更紧的止损)
- 新增 max_rsi_for_short: 60 (防止高位做空)
- 新增 min_rsi_for_long: 35 (防止低位做多)
- 新增 close_short_rsi: 65 (RSI反弹时强制平仓)
- 新增 close_long_rsi: 35 (RSI下跌时强制平仓)
"""

import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_v3 import BacktesterV3


def main():
    """主函数"""
    from dotenv import load_dotenv
    
    # 加载.env文件
    load_dotenv('.env')  # 从根目录加载
    
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_SECRET')  # 修复：使用 BINANCE_SECRET 而非 BINANCE_API_SECRET
    
    # 类型检查：确保API密钥不为None
    if not api_key or not api_secret:
        print("❌ 错误: API密钥未配置，请检查 .env 文件")
        return
    
    print("=" * 60)
    print("🚀 开始优化回测 V4：5分钟K线，7天数据，100 USDT")
    print("=" * 60)
    
    # 创建回测器
    backtester = BacktesterV3(
        symbol="SOLUSDT",
        interval="5m",
        days=7,  # 改为 7 天 (从 30 天)
        api_key=api_key,
        api_secret=api_secret,
    )
    
    print(f"✅ V4 参数已加载 (5分钟K线, 7天数据)")
    print(f"   - 预期数据量: ~2000根K线 (从原来的1000根增加)")
    print(f"   - 冷却期: {backtester.cooldown_bars}根K线 (V3: 8根)")
    print(f"   - 最小持仓时间: {backtester.min_hold_bars}根K线 (V3: 10根)")
    print(f"   - 最大持仓时间: {backtester.max_hold_bars}根K线 (V3: 60根) ⭐ 优化")
    print(f"   - 做空最小RSI: {backtester.min_rsi_for_short} (V3: 25)")
    print(f"   - 做空最大RSI: {backtester.max_rsi_for_short} (V3: 无) ⭐ 新增")
    print(f"   - 做多最小RSI: {backtester.min_rsi_for_long} (V3: 无) ⭐ 新增")
    print(f"   - 做多最大RSI: {backtester.max_rsi_for_long} (V3: 75)")
    print(f"   - 做空平仓RSI: {backtester.close_short_rsi} (V3: 无) ⭐ 新增")
    print(f"   - 做多平仓RSI: {backtester.close_long_rsi} (V3: 无) ⭐ 新增")
    print(f"   - 止损比例: {backtester.stop_loss_percent}% (V3: 1.2%) ⭐ 优化")
    print(f"   - 止盈比例: {backtester.take_profit_percent}% (V3: 2.5%) ⭐ 优化")
    
    # 下载历史数据
    backtester.fetch_data()
    
    # 计算技术指标
    backtester.calculate_indicators()
    
    # 运行回测
    result = backtester.run_backtest(initial_capital=100)
    
    # 打印汇总
    backtester.print_summary(result)
    
    return result


if __name__ == "__main__":
    main()
