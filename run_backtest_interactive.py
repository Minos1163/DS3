#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键启动回测脚本
5分钟K线 2天数据 完整AI分析
"""
import os
import sys
import subprocess

def print_header(title):
    """打印标题"""
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)

def check_environment():
    """检查环境配置"""
    print_header("环境检查")
    
    # 检查Python版本
    print(f"Python版本: {sys.version}")
    
    # 检查必要的包
    required_packages = ['pandas', 'numpy', 'requests', 'binance']
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package)
            print(f"✓ {package} - 已安装")
        except ImportError:
            print(f"✗ {package} - 未安装")
            missing_packages.append(package)
    
    if missing_packages:
        print(f"\n需要安装缺失的包: {', '.join(missing_packages)}")
        print("执行命令: pip install " + " ".join(missing_packages))
        return False
    
    return True

def check_credentials():
    """检查API凭证"""
    print_header("凭证检查")
    
    if not os.path.exists('.env'):
        print("⚠️  .env 文件不存在")
        print("需要在项目根目录创建 .env 文件，包含:")
        print("BINANCE_API_KEY=your_key")
        print("BINANCE_SECRET=your_secret")
        print("DEEPSEEK_API_KEY=your_key")
        return False
    
    print("✓ .env 文件存在")
    return True

def start_backtest():
    """启动回测"""
    print_header("启动回测: 5分钟K线 2天数据 完整AI分析")
    
    print("\n配置参数:")
    print("  交易对: SOLUSDT")
    print("  周期: 5分钟 (5m)")
    print("  时长: 2天")
    print("  初始资金: 10,000 USDT")
    print("  AI分析: 每根K线")
    print("  K线数量: ~576根")
    print("\n预期执行时间: 3-10分钟")
    print("预期API调用: 576次")
    
    input("\n按 Enter 开始回测...")
    
    try:
        # 运行回测脚本
        subprocess.run([sys.executable, 'backtest_ai.py'], check=True)
        print_header("回测完成!")
        print("✓ 回测执行成功")
        return True
    except subprocess.CalledProcessError as e:
        print_header("回测失败!")
        print(f"✗ 错误: {e}")
        return False
    except Exception as e:
        print_header("执行异常!")
        print(f"✗ 错误: {e}")
        return False

def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("AI交易回测系统")
    print("=" * 60)
    
    # 检查环境
    if not check_environment():
        print("\n环境检查失败，请安装缺失的包后重试")
        return False
    
    # 检查凭证
    if not check_credentials():
        print("\n凭证检查失败，请配置 .env 文件")
        return False
    
    # 启动回测
    success = start_backtest()
    
    if success:
        print("\n" + "=" * 60)
        print("下一步:")
        print("1. 查看输出的回测结果统计")
        print("2. 分析交易序列和胜率")
        print("3. 根据结果调整参数")
        print("4. 进行多次回测找最优配置")
        print("=" * 60 + "\n")
    
    return success

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
