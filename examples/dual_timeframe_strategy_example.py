"""
双周期趋势交易策略使用示例

演示如何使用 DualTimeframeStrategy 和 TrendFollowingEngine
"""

import numpy as np
from typing import Dict, List
from src.fund_flow.dual_timeframe_strategy import (
    DualTimeframeStrategy,
    EMAConfig,
    MACDConfig,
    VWAPConfig,
    create_strategy_config
)
from src.fund_flow.trend_following_engine import (
    TrendFollowingEngine,
    create_trend_following_engine,
    analyze_symbol_with_trend_engine,
    MarketRegime,
    TrendDirection,
    SignalStrength
)


def generate_sample_data(trend: str = "bullish") -> Dict[str, np.ndarray]:
    """
    生成示例数据

    Args:
        trend: "bullish", "bearish", 或 "range"

    Returns:
        包含 4H 和 15m 数据的字典
    """
    # 生成 4H 数据
    if trend == "bullish":
        close_4h = np.linspace(100, 150, 200)  # 上涨趋势
        base_price_15m = 140
        direction = 1
    elif trend == "bearish":
        close_4h = np.linspace(150, 100, 200)  # 下跌趋势
        base_price_15m = 110
        direction = -1
    else:  # range
        close_4h = np.random.normal(125, 2, 200)  # 震荡
        base_price_15m = 125
        direction = 0

    # 添加噪声
    noise_4h = np.random.normal(0, 1, 200)
    close_4h = close_4h + noise_4h

    # 生成 15m 数据
    close_15m = np.linspace(base_price_15m, base_price_15m + (20 * direction), 200)

    # 添加回踩结构
    if trend != "range":
        # 回踩
        close_15m[150:160] -= (2 * direction)
        # 二次启动
        close_15m[160:] += (3 * direction)

    # 添加噪声
    noise_15m = np.random.normal(0, 0.5, 200)
    close_15m = close_15m + noise_15m

    # 生成高低价和成交量
    high_15m = close_15m + 1
    low_15m = close_15m - 1
    volume_15m = np.random.uniform(800, 1200, 200)

    return {
        "4h": {"close": close_4h},
        "15m": {
            "open": close_15m - 0.5,
            "high": high_15m,
            "low": low_15m,
            "close": close_15m,
            "volume": volume_15m
        }
    }


def example_1_basic_strategy():
    """示例 1：使用 DualTimeframeStrategy"""
    print("\n" + "=" * 60)
    print("示例 1：使用 DualTimeframeStrategy")
    print("=" * 60)

    # 创建策略
    strategy = DualTimeframeStrategy()

    # 生成多头市场数据
    print("\n生成多头市场示例数据...")
    data = generate_sample_data("bullish")

    # 分析
    print("分析交易信号...")
    signal = strategy.analyze(
        close_4h=data["4h"]["close"],
        high_15m=data["15m"]["high"],
        low_15m=data["15m"]["low"],
        close_15m=data["15m"]["close"],
        volume_15m=data["15m"]["volume"]
    )

    # 显示结果
    if signal:
        print(f"\n✓ 发现交易信号！")
        print(f"  方向: {signal.direction.value}")
        print(f"  强度: {signal.strength.value}")
        print(f"  入场价: {signal.entry_price:.2f}")
        print(f"  止损价: {signal.stop_loss:.2f}")
        print(f"  止盈价: {signal.take_profit:.2f}")
        print(f"  风险回报比: {signal.risk_reward:.2f}")
        print(f"  理由: {signal.reason}")
        print(f"  结构:")
        for s in signal.structure:
            print(f"    - {s}")
    else:
        print("\n✗ 未发现交易信号")

    # 测试空头市场
    print("\n" + "-" * 40)
    print("生成空头市场示例数据...")
    data = generate_sample_data("bearish")

    signal = strategy.analyze(
        close_4h=data["4h"]["close"],
        high_15m=data["15m"]["high"],
        low_15m=data["15m"]["low"],
        close_15m=data["15m"]["close"],
        volume_15m=data["15m"]["volume"]
    )

    if signal:
        print(f"\n✓ 发现交易信号！")
        print(f"  方向: {signal.direction.value}")
        print(f"  强度: {signal.strength.value}")
        print(f"  入场价: {signal.entry_price:.2f}")
        print(f"  止损价: {signal.stop_loss:.2f}")
        print(f"  止盈价: {signal.take_profit:.2f}")
        print(f"  风险回报比: {signal.risk_reward:.2f}")
    else:
        print("\n✗ 未发现交易信号")

    # 测试震荡市场
    print("\n" + "-" * 40)
    print("生成震荡市场示例数据...")
    data = generate_sample_data("range")

    signal = strategy.analyze(
        close_4h=data["4h"]["close"],
        high_15m=data["15m"]["high"],
        low_15m=data["15m"]["low"],
        close_15m=data["15m"]["close"],
        volume_15m=data["15m"]["volume"]
    )

    if signal:
        print(f"\n✓ 发现交易信号（不应该出现）")
    else:
        print("\n✓ 震荡市场，未发现交易信号（符合预期）")


def example_2_trend_following_engine():
    """示例 2：使用 TrendFollowingEngine"""
    print("\n" + "=" * 60)
    print("示例 2：使用 TrendFollowingEngine")
    print("=" * 60)

    # 创建引擎
    engine = create_trend_following_engine()

    # 生成多头市场数据
    print("\n生成多头市场示例数据...")
    data = generate_sample_data("bullish")

    # 分析市场状态
    print("分析市场状态...")
    market_state = engine.analyze_market(
        "BTCUSDT",
        data["4h"]["close"],
        data["4h"]["close"]  # 简化处理
    )

    print(f"\n市场状态:")
    print(f"  状态: {market_state.regime.value}")
    print(f"  趋势方向: {market_state.trend_direction.value}")
    print(f"  趋势强度: {market_state.trend_strength.value}")
    print(f"  波动性: {market_state.volatility}")

    # 生成交易信号
    print("\n生成交易信号...")
    signal = engine.generate_signal(
        "BTCUSDT",
        data["4h"]["close"],
        data["15m"]["high"],
        data["15m"]["low"],
        data["15m"]["close"],
        data["15m"]["volume"]
    )

    if signal:
        print(f"\n✓ 发现交易信号！")
        print(f"  方向: {signal.direction.value}")
        print(f"  强度: {signal.strength.value}")
        print(f"  入场价: {signal.entry_price:.2f}")
        print(f"  止损价: {signal.stop_loss:.2f}")
        print(f"  止盈价: {signal.take_profit:.2f}")
        print(f"  风险回报比: {signal.risk_reward:.2f}")
        print(f"  理由: {signal.reason}")


def example_3_multiple_symbols():
    """示例 3：分析多个交易对"""
    print("\n" + "=" * 60)
    print("示例 3：分析多个交易对")
    print("=" * 60)

    # 创建引擎
    engine = create_trend_following_engine()

    # 交易对列表
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]

    print(f"\n分析 {len(symbols)} 个交易对...")

    signals = []

    for symbol in symbols:
        # 为每个交易对生成模拟数据
        import random
        trend = random.choice(["bullish", "bearish", "range"])
        data = generate_sample_data(trend)

        # 分析
        signal = analyze_symbol_with_trend_engine(
            engine,
            symbol,
            data["4h"],
            data["15m"]
        )

        if signal:
            signals.append({
                "symbol": symbol,
                "signal": signal,
                "trend": trend
            })

    # 显示结果
    print(f"\n发现 {len(signals)} 个交易信号:")

    for i, sig_info in enumerate(signals, 1):
        print(f"\n{i}. {sig_info['symbol']}")
        print(f"   方向: {sig_info['signal'].direction.value}")
        print(f"   强度: {sig_info['signal'].strength.value}")
        print(f"   入场价: {sig_info['signal'].entry_price:.2f}")
        print(f"   止损价: {sig_info['signal'].stop_loss:.2f}")
        print(f"   止盈价: {sig_info['signal'].take_profit:.2f}")
        print(f"   风险回报比: {sig_info['signal'].risk_reward:.2f}")


def example_4_custom_config():
    """示例 4：使用自定义配置"""
    print("\n" + "=" * 60)
    print("示例 4：使用自定义配置")
    print("=" * 60)

    # 创建自定义配置
    ema_config = EMAConfig(
        ema21_period=21,
        ema55_period=55,
        ema200_period=200
    )

    macd_config = MACDConfig(
        fast_period=12,
        slow_period=26,
        signal_period=9,
        fast_period_15m=8,
        slow_period_15m=21,
        signal_period_15m=5
    )

    vwap_config = VWAPConfig(
        use_anchored=True,
        anchor_period="weekly"
    )

    # 创建策略
    strategy = DualTimeframeStrategy(ema_config, macd_config, vwap_config)

    print("\n使用自定义配置分析...")
    print(f"  EMA21: {ema_config.ema21_period}")
    print(f"  EMA55: {ema_config.ema55_period}")
    print(f"  EMA200: {ema_config.ema200_period}")
    print(f"  MACD (4H): {macd_config.fast_period}, {macd_config.slow_period}, {macd_config.signal_period}")
    print(f"  MACD (15m): {macd_config.fast_period_15m}, {macd_config.slow_period_15m}, {macd_config.signal_period_15m}")
    print(f"  VWAP: {'Anchored' if vwap_config.use_anchored else 'Regular'}")

    # 生成数据并分析
    data = generate_sample_data("bullish")
    signal = strategy.analyze(
        close_4h=data["4h"]["close"],
        high_15m=data["15m"]["high"],
        low_15m=data["15m"]["low"],
        close_15m=data["15m"]["close"],
        volume_15m=data["15m"]["volume"]
    )

    if signal:
        print(f"\n✓ 发现交易信号！")
        print(f"  方向: {signal.direction.value}")
        print(f"  强度: {signal.strength.value}")
        print(f"  入场价: {signal.entry_price:.2f}")
        print(f"  止损价: {signal.stop_loss:.2f}")
        print(f"  止盈价: {signal.take_profit:.2f}")


def example_5_strategy_config():
    """示例 5：查看策略配置"""
    print("\n" + "=" * 60)
    print("示例 5：策略配置")
    print("=" * 60)

    config = create_strategy_config()

    print("\n默认策略配置:")
    print(f"  EMA 配置:")
    print(f"    - EMA21: {config['ema']['ema21_period']}")
    print(f"    - EMA55: {config['ema']['ema55_period']}")
    print(f"    - EMA200: {config['ema']['ema200_period']}")

    print(f"\n  MACD 配置 (4H):")
    print(f"    - 快线: {config['macd']['4h']['fast_period']}")
    print(f"    - 慢线: {config['macd']['4h']['slow_period']}")
    print(f"    - 信号线: {config['macd']['4h']['signal_period']}")

    print(f"\n  MACD 配置 (15m):")
    print(f"    - 快线: {config['macd']['15m']['fast_period']}")
    print(f"    - 慢线: {config['macd']['15m']['slow_period']}")
    print(f"    - 信号线: {config['macd']['15m']['signal_period']}")

    print(f"\n  VWAP 配置:")
    print(f"    - 使用锚定: {config['vwap']['use_anchored']}")
    print(f"    - 锚定周期: {config['vwap']['anchor_period']}")

    print(f"\n  入场条件:")
    print(f"    - 最小回踩: {config['entry']['pullback_min_depth']*100:.1f}%")
    print(f"    - 最大回踩: {config['entry']['pullback_max_depth']*100:.1f}%")
    print(f"    - EMA21 容差: {config['entry']['ema21_tolerance']*100:.1f}%")

    print(f"\n  止盈止损:")
    print(f"    - 默认止损: {config['exit']['default_stop_loss_pct']*100:.1f}%")
    print(f"    - 默认止盈: {config['exit']['default_take_profit_pct']*100:.1f}%")
    print(f"    - 最小风险回报比: {config['exit']['min_risk_reward']:.1f}")


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("双周期趋势交易策略使用示例")
    print("=" * 60)

    # 运行所有示例
    example_1_basic_strategy()
    example_2_trend_following_engine()
    example_3_multiple_symbols()
    example_4_custom_config()
    example_5_strategy_config()

    print("\n" + "=" * 60)
    print("所有示例运行完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
