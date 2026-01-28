#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘交易启动脚本 (生产环境)
需要在修改.env文件后运行此脚本
"""

import sys
import os
import time
from datetime import datetime

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_v3 import BacktesterV3
from dotenv import load_dotenv


def check_environment():
    """检查环境是否就绪"""
    print("=" * 70)
    print("🔍 检查实盘环境")
    print("=" * 70)
    
    # 检查.env文件
    if not os.path.exists('.env'):
        print("❌ 错误: .env 文件不存在")
        print("   请按照以下步骤操作:")
        print("   1. 在 Binance 创建新的 API Key")
        print("   2. 复制 API Key 和 Secret")
        print("   3. 在项目根目录创建 .env 文件")
        print("   4. 添加内容:")
        print("      BINANCE_API_KEY=你的API_KEY")
        print("      BINANCE_SECRET=你的SECRET")
        print("      DEEPSEEK_API_KEY=sk-2e9fcf4677dc4ce99785f72156336d80")
        return False
    
    print("✅ .env 文件存在")
    
    # 检查API密钥
    load_dotenv('.env')
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_SECRET')
    
    if not api_key or not api_secret:
        print("❌ 错误: API密钥未配置")
        print("   请检查 .env 文件中的以下内容:")
        print("   - BINANCE_API_KEY")
        print("   - BINANCE_SECRET")
        return False
    
    print(f"✅ API密钥已配置")
    print(f"   - API Key: {api_key[:10]}...{api_key[-4:]}")
    print(f"   - Secret:  {api_secret[:10]}...{api_secret[-4:]}")
    
    # 检查logs目录
    if not os.path.exists('logs'):
        os.makedirs('logs')
        print("✅ 创建 logs 目录")
    else:
        print("✅ logs 目录存在")
    
    print("\n✅ 环境检查完成，可以启动实盘交易")
    return True


def show_parameters(backtester):
    """显示交易参数"""
    print("\n" + "=" * 70)
    print("⚙️  交易参数配置")
    print("=" * 70)
    
    print(f"\n【交易设置】")
    print(f"交易对:           {backtester.symbol}")
    print(f"K线周期:          {backtester.interval}")
    print(f"初始资金:         100 USDT")
    
    print(f"\n【风险管理】")
    print(f"杠杆倍数:         {backtester.default_leverage}x")
    print(f"仓位百分比:       {backtester.position_size*100:.0f}%")
    print(f"止损幅度:         {backtester.stop_loss_percent}%")
    print(f"止盈幅度:         {backtester.take_profit_percent}%")
    print(f"最大持仓:         {backtester.max_hold_bars}根K线 (~{backtester.max_hold_bars*5}分钟)")
    
    print(f"\n【信号参数】")
    print(f"信号门槛:         {backtester.short_signal_threshold}/6")
    print(f"冷却期:           {backtester.cooldown_bars}根K线 (~{backtester.cooldown_bars*5}分钟)")
    print(f"最小持仓:         {backtester.min_hold_bars}根K线 (~{backtester.min_hold_bars*5}分钟)")
    
    print(f"\n【RSI保护】")
    print(f"做空RSI范围:      {backtester.min_rsi_for_short}-{backtester.max_rsi_for_short}")
    print(f"做多RSI范围:      {backtester.min_rsi_for_long}-{backtester.max_rsi_for_long}")
    print(f"做空平仓RSI:      {backtester.close_short_rsi}")
    print(f"做多平仓RSI:      {backtester.close_long_rsi}")


def show_warning():
    """显示风险警告"""
    print("\n" + "=" * 70)
    print("⚠️  风险警告")
    print("=" * 70)
    print("""
⚠️ 重要提示:
1. 🔴 杠杆交易有极大风险，可能导致本金完全亏损
2. 🔴 您的本金可能在极短时间内完全损失
3. 🔴 请仅用您能够承受损失的资金进行交易
4. 🔴 建议从小额（10-100 USDT）开始测试
5. ⚠️ 不要让交易机器人无人监管运行超过1小时
6. ⚠️ 定期检查日志文件和账户余额
7. ⚠️ 如发现异常立即停止交易
8. ⚠️ 请理解算法交易的局限性和不确定性

已确认理解以上风险，同意继续? (y/n): """, end='')


def main():
    """主函数"""
    print("\n" + "=" * 70)
    print("🚀 实盘交易启动程序")
    print("=" * 70)
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # 检查环境
    if not check_environment():
        print("\n❌ 环境检查失败，无法启动")
        return False
    
    # 创建回测器（实际应该是交易执行器）
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_SECRET')
    
    # 类型检查：确保API密钥不为None
    if not api_key or not api_secret:
        print("❌ 错误: API密钥为None，无法创建回测器")
        return False
    
    backtester = BacktesterV3(
        symbol="SOLUSDT",
        interval="5m",
        days=7,
        api_key=api_key,
        api_secret=api_secret,
    )
    
    # 显示参数
    show_parameters(backtester)
    
    # 显示警告
    show_warning()
    user_input = input()
    
    if user_input.lower() != 'y':
        print("\n❌ 已取消启动")
        return False
    
    print("\n" + "=" * 70)
    print("🎯 准备启动实盘交易")
    print("=" * 70)
    print("\n⏳ 正在初始化...")
    print("   - 连接Binance API")
    print("   - 验证账户权限")
    print("   - 准备日志系统")
    
    # 初始化日志
    backtester.init_logging()
    
    # 下载数据
    print("\n📥 下载历史数据...")
    backtester.fetch_data()
    
    if backtester.df is None or len(backtester.df) == 0:
        print("❌ 数据下载失败，启动中止")
        return False
    
    # 计算指标
    print("📊 计算技术指标...")
    backtester.calculate_indicators()
    
    # 运行回测
    print("\n🔄 开始交易执行...")
    print("=" * 70)
    result = backtester.run_backtest(initial_capital=100)
    
    # 打印汇总
    backtester.print_summary(result)
    
    # 显示日志文件位置
    print("\n" + "=" * 70)
    print("✅ 交易完成")
    print("=" * 70)
    print(f"\n详细日志:")
    print(f"  - K线操作日志: {backtester.log_file}")
    print(f"  - 汇总报告:    {backtester.summary_file}")
    print(f"\n请查看日志文件了解详细交易信息")
    
    # 建议
    print("\n💡 建议:")
    if result['final_capital'] > result['initial_capital']:
        profit = result['final_capital'] - result['initial_capital']
        pct = (profit / result['initial_capital']) * 100
        print(f"✅ 本次运行盈利 {profit:.2f} USDT ({pct:.2f}%)")
        print("   建议可以逐步增加资金进行更多测试")
    else:
        loss = result['initial_capital'] - result['final_capital']
        pct = (loss / result['initial_capital']) * 100
        print(f"❌ 本次运行亏损 {loss:.2f} USDT ({pct:.2f}%)")
        print("   建议检查参数或等待更好的市场条件")
    
    return True


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
