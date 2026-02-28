#!/usr/bin/env python3
"""
自动化资金流参数优化脚本
根据日志分析结果自动调整配置参数
"""

import json
import shutil
from datetime import datetime
import os

def backup_config(config_path):
    """备份当前配置文件"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.replace(".json", f".{timestamp}.backup.json")
    shutil.copy2(config_path, backup_path)
    print(f"✅ 配置文件已备份: {backup_path}")
    return backup_path

def load_config(config_path):
    """加载配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(config, config_path):
    """保存配置文件"""
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"✅ 配置文件已更新: {config_path}")

def optimize_parameters(config):
    """优化参数配置"""
    print("🔧 正在优化参数...")
    
    fund_flow = config.get('fund_flow', {})
    
    # 1. 降低TREND模式开仓阈值
    if 'engine_params' in fund_flow:
        trend_params = fund_flow['engine_params'].get('TREND', {})
        old_long_thresh = trend_params.get('long_open_threshold', 0.22)
        old_short_thresh = trend_params.get('short_open_threshold', 0.22)
        
        trend_params['long_open_threshold'] = 0.15
        trend_params['short_open_threshold'] = 0.15
        
        print(f"📈 TREND长线阈值: {old_long_thresh} → 0.15")
        print(f"📈 TREND短线阈值: {old_short_thresh} → 0.15")
        
        # 同步更新到根级别参数
        fund_flow['long_open_threshold'] = 0.15
        fund_flow['short_open_threshold'] = 0.15
    
    # 2. 降低信号池阈值
    if 'signal_pools' in fund_flow:
        for pool in fund_flow['signal_pools']:
            if pool['id'] == 'trend_pool':
                old_min_long = pool.get('min_long_score', 0.22)
                old_min_short = pool.get('min_short_score', 0.22)
                
                pool['min_long_score'] = 0.15
                pool['min_short_score'] = 0.15
                
                print(f"📊 Trend Pool阈值: 长{old_min_long}/短{old_min_short} → 长0.15/短0.15")
            
            elif pool['id'] == 'trend_pool_major':
                old_min_long = pool.get('min_long_score', 0.28)
                old_min_short = pool.get('min_short_score', 0.28)
                
                pool['min_long_score'] = 0.20
                pool['min_short_score'] = 0.20
                
                print(f"📊 Major Trend Pool阈值: 长{old_min_long}/短{old_min_short} → 长0.20/短0.20")
    
    # 3. 调整RANGE分位数参数
    if 'range_quantile' in fund_flow:
        range_quantile = fund_flow['range_quantile']
        old_min_samples = range_quantile.get('min_samples', 12)
        old_lookback = range_quantile.get('lookback_minutes', 120)
        
        range_quantile['min_samples'] = 8
        range_quantile['lookback_minutes'] = 90
        
        print(f"🔄 RANGE分位数: 最小样本{old_min_samples}→8, 回看时间{old_lookback}→90分钟")
    
    # 4. 微调信号定义阈值（保守调整）
    if 'signal_definitions' in fund_flow:
        adjustments = {
            'trend_long_cvd': 0.0005,
            'trend_long_imb': 0.06,
            'trend_short_cvd': -0.0005,
            'trend_short_imb': -0.06
        }
        
        for signal_def in fund_flow['signal_definitions']:
            signal_id = signal_def['id']
            if signal_id in adjustments:
                old_threshold = signal_def['threshold']
                new_threshold = adjustments[signal_id]
                signal_def['threshold'] = new_threshold
                print(f"🎯 信号{signal_id}: 阈值{old_threshold} → {new_threshold}")
    
    config['fund_flow'] = fund_flow
    return config

def generate_summary(old_config, new_config):
    """生成变更摘要"""
    print("\n📋 参数变更摘要:")
    print("=" * 50)
    
    old_ff = old_config.get('fund_flow', {})
    new_ff = new_config.get('fund_flow', {})
    
    # TREND阈值变化
    old_trend = old_ff.get('engine_params', {}).get('TREND', {})
    new_trend = new_ff.get('engine_params', {}).get('TREND', {})
    
    print(f"TREND模式阈值:")
    print(f"  长线: {old_trend.get('long_open_threshold', 0.22)} → {new_trend.get('long_open_threshold', 0.22)}")
    print(f"  短线: {old_trend.get('short_open_threshold', 0.22)} → {new_trend.get('short_open_threshold', 0.22)}")
    
    # RANGE参数变化
    old_range = old_ff.get('range_quantile', {})
    new_range = new_ff.get('range_quantile', {})
    
    print(f"\nRANGE模式参数:")
    print(f"  最小样本数: {old_range.get('min_samples', 12)} → {new_range.get('min_samples', 12)}")
    print(f"  回看时间: {old_range.get('lookback_minutes', 120)} → {new_range.get('lookback_minutes', 120)}分钟")
    
    print("\n💡 预期改善:")
    print("- 解锁约60-70次TREND模式阻挡")
    print("- 解锁约15-20次RANGE模式阻挡")
    print("- 整体开仓机会增加约75-90次/10小时")

def main():
    """主函数"""
    config_path = r"d:\AIDCA\AI2\config\trading_config_fund_flow.json"
    
    print("🤖 资金流参数自动优化工具")
    print("=" * 40)
    
    # 检查配置文件是否存在
    if not os.path.exists(config_path):
        print(f"❌ 配置文件不存在: {config_path}")
        return
    
    # 加载原配置
    print("📥 加载当前配置...")
    old_config = load_config(config_path)
    
    # 备份配置
    backup_path = backup_config(config_path)
    
    # 优化参数
    new_config = optimize_parameters(old_config)
    
    # 保存新配置
    print("\n💾 保存优化后的配置...")
    save_config(new_config, config_path)
    
    # 生成摘要
    generate_summary(old_config, new_config)
    
    print(f"\n✅ 优化完成！")
    print(f"📄 备份文件: {backup_path}")
    print(f"🔧 新配置文件: {config_path}")
    print(f"⏰ 建议观察24-48小时后再做进一步调整")

if __name__ == "__main__":
    main()