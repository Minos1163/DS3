#!/usr/bin/env python3
"""
快速回测 - 5分钟K线，7天数据（实际约3.5天），禁用AI
"""
import sys
import os

# 添加项目根目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == '__main__':
    print("=" * 70)
    print("🚀 开始回测：5分钟K线，7天数据，禁用AI")
    print("=" * 70)
    
    # 导入并运行
    from backtest_ai import main
    main()
