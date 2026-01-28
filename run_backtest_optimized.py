#!/usr/bin/env python3
"""
优化回测 - 5分钟K线，30天数据，100 USDT，优化参数提高胜率
"""
import sys
import os

# 添加项目根目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == '__main__':
    print("=" * 70)
    print("🚀 开始优化回测：5分钟K线，30天数据，100 USDT")
    print("=" * 70)

    # 导入并运行
    from backtest_optimized import main
    main()
