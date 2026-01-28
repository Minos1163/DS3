#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
启动回测脚本
5分钟K线 2天数据 完整AI分析
"""
import os
import sys

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 设置输出编码
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from backtest_ai import AIBacktester

def main():
    """主函数 - 启动回测"""
    print("=" * 60)
    print("开始回测: 5分钟K线 2天数据 启用完整AI分析")
    print("=" * 60)
    
    # 创建回测器 - 5m间隔，2天数据
    backtester = AIBacktester(symbol='SOLUSDT', interval='5m', days=2)
    
    # 下载数据
    print("\n开始下载数据...")
    if backtester.download_data() is None:
        print("错误: 数据下载失败")
        return
    
    # 计算指标
    print("\n开始计算技术指标...")
    backtester.calculate_indicators()
    
    # 运行回测
    print("\n开始运行回测...")
    trades = backtester.run_backtest(initial_capital=10000)
    
    print("\n" + "=" * 60)
    print("回测完成!")
    print("=" * 60)


if __name__ == '__main__':
    main()
