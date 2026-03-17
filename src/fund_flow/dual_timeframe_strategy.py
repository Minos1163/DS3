"""
双周期趋势交易策略 (4H + 15m)

基于 EMA + VWAP + MACD 的双周期交易系统
核心逻辑：4H定方向，15m找入场
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple
import numpy as np


class TrendDirection(Enum):
    """趋势方向"""
    BULLISH = "bullish"    # 多头
    BEARISH = "bearish"    # 空头
    NEUTRAL = "neutral"    # 震荡


class SignalStrength(Enum):
    """信号强度"""
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    VERY_STRONG = "very_strong"


@dataclass
class EMAConfig:
    """EMA 配置"""
    ema21_period: int = 21
    ema55_period: int = 55
    ema200_period: int = 200


@dataclass
class MACDConfig:
    """MACD 配置"""
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9

    # 15m 周期使用更敏感的参数
    fast_period_15m: int = 8
    slow_period_15m: int = 21
    signal_period_15m: int = 5


@dataclass
class VWAPConfig:
    """VWAP 配置"""
    use_anchored: bool = True
    anchor_period: str = "weekly"  # weekly, daily, or session_start


@dataclass
class Signal:
    """交易信号"""
    direction: TrendDirection
    strength: SignalStrength
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    reason: str
    structure: List[str] = field(default_factory=list)


class TechnicalIndicators:
    """技术指标计算工具"""

    @staticmethod
    def calculate_ema(prices: np.ndarray, period: int) -> np.ndarray:
        """计算 EMA"""
        multiplier = 2 / (period + 1)
        ema = np.zeros_like(prices)
        ema[0] = prices[0]

        for i in range(1, len(prices)):
            ema[i] = (prices[i] * multiplier) + (ema[i-1] * (1 - multiplier))

        return ema

    @staticmethod
    def calculate_macd(prices: np.ndarray,
                      fast: int,
                      slow: int,
                      signal: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """计算 MACD"""
        ema_fast = TechnicalIndicators.calculate_ema(prices, fast)
        ema_slow = TechnicalIndicators.calculate_ema(prices, slow)

        macd_line = ema_fast - ema_slow
        signal_line = TechnicalIndicators.calculate_ema(macd_line, signal)
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    @staticmethod
    def calculate_vwap(high: np.ndarray,
                       low: np.ndarray,
                       close: np.ndarray,
                       volume: np.ndarray,
                       anchor_point: int = 0) -> np.ndarray:
        """计算 VWAP"""
        typical_price = (high + low + close) / 3
        tp_volume = typical_price * volume

        cum_tp_volume = np.cumsum(tp_volume)
        cum_volume = np.cumsum(volume)

        vwap = np.zeros_like(close)
        for i in range(anchor_point, len(close)):
            if cum_volume[i] != 0:
                vwap[i] = cum_tp_volume[i] / cum_volume[i]
            else:
                vwap[i] = vwap[i-1] if i > 0 else close[i]

        return vwap


class TrendAnalyzer:
    """趋势分析器"""

    def __init__(self, ema_config: EMAConfig):
        self.ema_config = ema_config

    def analyze_4h_trend(self,
                        close_prices: np.ndarray) -> TrendDirection:
        """
        分析 4H 趋势

        条件：
        - 多头: 价格 > EMA55 且 EMA21 > EMA55
        - 空头: 价格 < EMA55 且 EMA21 < EMA55
        - 震荡: 其他情况
        """
        ema21 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema21_period)
        ema55 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema55_period)
        ema200 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema200_period)

        current_price = close_prices[-1]
        latest_ema21 = ema21[-1]
        latest_ema55 = ema55[-1]
        latest_ema200 = ema200[-1]

        # 判断趋势结构
        if (current_price > latest_ema55 and
            latest_ema21 > latest_ema55 and
            current_price > latest_ema200):
            return TrendDirection.BULLISH

        if (current_price < latest_ema55 and
            latest_ema21 < latest_ema55):
            return TrendDirection.BEARISH

        return TrendDirection.NEUTRAL

    def is_super_trend(self, close_prices: np.ndarray) -> bool:
        """
        判断是否为超级趋势

        条件：价格 > EMA21 > EMA55 > EMA200
        """
        ema21 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema21_period)
        ema55 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema55_period)
        ema200 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema200_period)

        return (close_prices[-1] > ema21[-1] > ema55[-1] > ema200[-1])

    def check_ema_alignment(self, close_prices: np.ndarray) -> Dict[str, bool]:
        """
        检查 EMA 对齐情况

        返回：
        {
            "bullish_alignment": bool,  # 多头排列
            "bearish_alignment": bool,  # 空头排列
            "price_above_ema21": bool,
            "price_above_ema55": bool,
            "price_above_ema200": bool
        }
        """
        ema21 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema21_period)
        ema55 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema55_period)
        ema200 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema200_period)

        return {
            "bullish_alignment": ema21[-1] > ema55[-1] > ema200[-1],
            "bearish_alignment": ema21[-1] < ema55[-1] < ema200[-1],
            "price_above_ema21": close_prices[-1] > ema21[-1],
            "price_above_ema55": close_prices[-1] > ema55[-1],
            "price_above_ema200": close_prices[-1] > ema200[-1]
        }


class MomentumAnalyzer:
    """动能分析器 (MACD)"""

    def __init__(self, macd_config: MACDConfig):
        self.macd_config = macd_config

    def analyze_4h_momentum(self, close_prices: np.ndarray) -> Dict[str, any]:
        """
        分析 4H 动能

        返回：
        {
            "histogram": float,
            "histogram_growing": bool,
            "cross_over": bool,  # 金叉
            "cross_under": bool,  # 死叉
            "positive": bool,
            "divergence": str  # "bullish", "bearish", "none"
        }
        """
        macd_line, signal_line, histogram = TechnicalIndicators.calculate_macd(
            close_prices,
            self.macd_config.fast_period,
            self.macd_config.slow_period,
            self.macd_config.signal_period
        )

        current_hist = histogram[-1]
        prev_hist = histogram[-2]

        # 检查金叉/死叉
        cross_over = (histogram[-2] <= 0 and histogram[-1] > 0)
        cross_under = (histogram[-2] >= 0 and histogram[-1] < 0)

        # 检查柱子增长
        histogram_growing = abs(current_hist) > abs(prev_hist)

        # 检查顶背离
        divergence = self._check_divergence(close_prices, histogram)

        return {
            "histogram": current_hist,
            "histogram_growing": histogram_growing,
            "cross_over": cross_over,
            "cross_under": cross_under,
            "positive": current_hist > 0,
            "divergence": divergence
        }

    def analyze_15m_momentum(self, close_prices: np.ndarray) -> Dict[str, any]:
        """分析 15m 动能（使用更敏感的参数）"""
        macd_line, signal_line, histogram = TechnicalIndicators.calculate_macd(
            close_prices,
            self.macd_config.fast_period_15m,
            self.macd_config.slow_period_15m,
            self.macd_config.signal_period_15m
        )

        current_hist = histogram[-1]
        prev_hist = histogram[-2]

        cross_over = (histogram[-2] <= 0 and histogram[-1] > 0)
        cross_under = (histogram[-2] >= 0 and histogram[-1] < 0)

        # 检查柱子翻红（从负变正）
        bar_turning_red = (histogram[-2] < 0 and histogram[-1] > 0)
        bar_turning_green = (histogram[-2] > 0 and histogram[-1] < 0)

        return {
            "histogram": current_hist,
            "cross_over": cross_over,
            "cross_under": cross_under,
            "bar_turning_red": bar_turning_red,
            "bar_turning_green": bar_turning_green,
            "positive": current_hist > 0
        }

    def _check_divergence(self,
                          prices: np.ndarray,
                          histogram: np.ndarray,
                          lookback: int = 20) -> str:
        """
        检查背离

        顶背离：价格创新高，MACD柱子下降
        底背离：价格创新低，MACD柱子上升
        """
        if len(prices) < lookback + 2:
            return "none"

        recent_prices = prices[-lookback:]
        recent_hist = histogram[-lookback:]

        # 找价格高点
        price_high_idx = np.argmax(recent_prices)
        hist_high_idx = np.argmax(recent_hist)

        # 顶背离：价格在后期创新高，但MACD柱子在前期更高
        if (price_high_idx > hist_high_idx and
            recent_prices[price_high_idx] > recent_prices[hist_high_idx] and
            recent_hist[price_high_idx] < recent_hist[hist_high_idx]):
            return "bearish"

        # 找价格低点
        price_low_idx = np.argmin(recent_prices)
        hist_low_idx = np.argmin(recent_hist)

        # 底背离：价格在后期创新低，但MACD柱子在前期更低
        if (price_low_idx > hist_low_idx and
            recent_prices[price_low_idx] < recent_prices[hist_low_idx] and
            recent_hist[price_low_idx] > recent_hist[hist_low_idx]):
            return "bullish"

        return "none"


class EntryAnalyzer:
    """入场分析器"""

    def __init__(self, ema_config: EMAConfig, vwap_config: VWAPConfig):
        self.ema_config = ema_config
        self.vwap_config = vwap_config

    def check_pullback_to_ema21(self, close_prices: np.ndarray) -> Dict[str, any]:
        """
        检查是否回踩 EMA21

        返回：
        {
            "is_pullback": bool,
            "pullback_depth": float,  # 回踩深度百分比
            "support_strength": float  # 支撑强度 0-1
        }
        """
        ema21 = TechnicalIndicators.calculate_ema(close_prices, self.ema_config.ema21_period)
        current_price = close_prices[-1]
        ema21_value = ema21[-1]

        # 计算距离 EMA21 的距离
        distance = abs(current_price - ema21_value) / ema21_value

        # 检查最近是否从 EMA21 附近反弹
        recent_lows = []
        for i in range(-10, 0):
            if i + len(close_prices) >= 0:
                recent_lows.append(close_prices[i])

        if recent_lows:
            lowest_near_ema = min(recent_lows)
            pullback_depth = abs(lowest_near_ema - ema21_value) / ema21_value

            # 支撑强度：越接近 EMA21 且反弹，支撑越强
            support_strength = max(0, 1 - (pullback_depth * 10))
        else:
            pullback_depth = distance
            support_strength = 0

        # 判断是否为有效回踩（1%-3%范围内）
        is_pullback = 0.01 <= pullback_depth <= 0.03

        return {
            "is_pullback": is_pullback,
            "pullback_depth": pullback_depth,
            "support_strength": support_strength,
            "distance_to_ema21": distance
        }

    def check_vwap_alignment(self,
                           high: np.ndarray,
                           low: np.ndarray,
                           close: np.ndarray,
                           volume: np.ndarray) -> Dict[str, any]:
        """
        检查 VWAP 对齐情况

        返回：
        {
            "above_vwap": bool,
            "distance_to_vwap": float,
            "crossed_vwap": bool  # 是否刚突破 VWAP
        }
        """
        vwap = TechnicalIndicators.calculate_vwap(high, low, close, volume)

        current_price = close[-1]
        vwap_value = vwap[-1]

        distance = abs(current_price - vwap_value) / vwap_value

        # 检查是否刚突破 VWAP
        crossed_vwap = (close[-2] < vwap[-2] and close[-1] > vwap[-1]) or \
                       (close[-2] > vwap[-2] and close[-1] < vwap[-1])

        return {
            "above_vwap": current_price > vwap_value,
            "distance_to_vwap": distance,
            "crossed_vwap": crossed_vwap
        }

    def find_optimal_entry(self,
                         close_prices: np.ndarray,
                         ema21: float,
                         vwap: float,
                         trend: TrendDirection) -> Optional[float]:
        """
        寻找最佳入场点

        条件：
        - 多头：回踩 EMA21 不破，且价格 > VWAP
        - 空头：反弹 EMA21 不破，且价格 < VWAP
        """
        current_price = close_prices[-1]

        if trend == TrendDirection.BULLISH:
            # 多头：回踩 EMA21 后反弹
            if (current_price > vwap and
                abs(current_price - ema21) / ema21 < 0.02):  # 2% 内
                return current_price

        elif trend == TrendDirection.BEARISH:
            # 空头：反弹 EMA21 后下跌
            if (current_price < vwap and
                abs(current_price - ema21) / ema21 < 0.02):
                return current_price

        return None


class DualTimeframeStrategy:
    """双周期交易策略 (4H + 15m)"""

    def __init__(self,
                 ema_config: Optional[EMAConfig] = None,
                 macd_config: Optional[MACDConfig] = None,
                 vwap_config: Optional[VWAPConfig] = None):
        self.ema_config = ema_config or EMAConfig()
        self.macd_config = macd_config or MACDConfig()
        self.vwap_config = vwap_config or VWAPConfig()

        self.trend_analyzer = TrendAnalyzer(self.ema_config)
        self.momentum_analyzer = MomentumAnalyzer(self.macd_config)
        self.entry_analyzer = EntryAnalyzer(self.ema_config, self.vwap_config)

    def analyze(self,
                close_4h: np.ndarray,
                high_15m: np.ndarray,
                low_15m: np.ndarray,
                close_15m: np.ndarray,
                volume_15m: np.ndarray) -> Optional[Signal]:
        """
        双周期分析

        流程：
        1. 4H 判断趋势
        2. 15m 等待回调
        3. MACD 动能确认
        4. VWAP 资金确认
        5. 入场点确认
        """

        # 步骤 1: 4H 趋势判断
        trend_4h = self.trend_analyzer.analyze_4h_trend(close_4h)

        if trend_4h == TrendDirection.NEUTRAL:
            return None  # 趋势不明确，不交易

        is_super_trend = self.trend_analyzer.is_super_trend(close_4h)
        ema_alignment_4h = self.trend_analyzer.check_ema_alignment(close_4h)

        # 步骤 2: 4H 动能分析
        momentum_4h = self.momentum_analyzer.analyze_4h_momentum(close_4h)

        # 检查顶背离（危险信号）
        if momentum_4h["divergence"] == "bearish" and trend_4h == TrendDirection.BULLISH:
            return None  # 顶背离，不做多

        # 步骤 3: 15m 回调检查
        pullback_15m = self.entry_analyzer.check_pullback_to_ema21(close_15m)

        if not pullback_15m["is_pullback"]:
            return None  # 没有有效回调，等待

        # 步骤 4: 15m 动能确认
        momentum_15m = self.momentum_analyzer.analyze_15m_momentum(close_15m)

        if trend_4h == TrendDirection.BULLISH:
            # 多头需要动能启动
            if not (momentum_15m["bar_turning_red"] or momentum_15m["cross_over"]):
                return None
        else:
            # 空头需要动能向下
            if not (momentum_15m["bar_turning_green"] or momentum_15m["cross_under"]):
                return None

        # 步骤 5: VWAP 确认
        vwap_15m = self.entry_analyzer.check_vwap_alignment(
            high_15m, low_15m, close_15m, volume_15m
        )

        if trend_4h == TrendDirection.BULLISH:
            if not vwap_15m["above_vwap"]:
                return None
        else:
            if vwap_15m["above_vwap"]:
                return None

        # 步骤 6: 计算入场点
        ema21_15m = TechnicalIndicators.calculate_ema(close_15m, self.ema_config.ema21_period)[-1]
        vwap_15m_value = TechnicalIndicators.calculate_vwap(high_15m, low_15m, close_15m, volume_15m)[-1]

        entry_price = self.entry_analyzer.find_optimal_entry(
            close_15m, ema21_15m, vwap_15m_value, trend_4h
        )

        if entry_price is None:
            return None

        # 计算止损和止盈
        stop_loss, take_profit = self._calculate_exit_levels(
            close_15m, entry_price, trend_4h
        )

        # 计算风险回报比
        risk_reward = abs(take_profit - entry_price) / abs(stop_loss - entry_price)

        # 确定信号强度
        strength = self._determine_signal_strength(
            is_super_trend,
            momentum_4h["histogram_growing"],
            pullback_15m["support_strength"],
            risk_reward
        )

        # 构建结构说明
        structure = self._build_structure_description(
            trend_4h, is_super_trend, momentum_4h, momentum_15m,
            pullback_15m, vwap_15m, ema_alignment_4h
        )

        # 生成理由
        reason = self._generate_reason(trend_4h, strength, structure)

        return Signal(
            direction=trend_4h,
            strength=strength,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward=risk_reward,
            reason=reason,
            structure=structure
        )

    def _calculate_exit_levels(self,
                              close_prices: np.ndarray,
                              entry_price: float,
                              trend: TrendDirection) -> Tuple[float, float]:
        """计算止损和止盈"""
        # 方法1: 结构止损（最近低点/高点）
        recent_range = np.max(close_prices[-10:]) - np.min(close_prices[-10:])

        if trend == TrendDirection.BULLISH:
            # 多头：止损在最近低点或 EMA55 下方
            stop_loss = max(
                np.min(close_prices[-10:]),
                entry_price * (1 - 0.02)  # 默认 2% 止损
            )

            # 止盈：5%-15% 或前高
            take_profit = min(
                np.max(close_prices[-20:]),
                entry_price * (1 + 0.05)  # 默认 5% 止盈
            )
        else:
            # 空头
            stop_loss = min(
                np.max(close_prices[-10:]),
                entry_price * (1 + 0.02)
            )

            take_profit = max(
                np.min(close_prices[-20:]),
                entry_price * (1 - 0.05)
            )

        return stop_loss, take_profit

    def _determine_signal_strength(self,
                                   is_super_trend: bool,
                                   momentum_growing: bool,
                                   support_strength: float,
                                   risk_reward: float) -> SignalStrength:
        """确定信号强度"""
        score = 0

        if is_super_trend:
            score += 2

        if momentum_growing:
            score += 1

        if support_strength > 0.7:
            score += 1
        elif support_strength > 0.5:
            score += 0.5

        if risk_reward > 3:
            score += 1
        elif risk_reward > 2:
            score += 0.5

        if score >= 3:
            return SignalStrength.VERY_STRONG
        elif score >= 2:
            return SignalStrength.STRONG
        elif score >= 1:
            return SignalStrength.MODERATE
        else:
            return SignalStrength.WEAK

    def _build_structure_description(self,
                                     trend: TrendDirection,
                                     is_super_trend: bool,
                                     momentum_4h: Dict[str, any],
                                     momentum_15m: Dict[str, any],
                                     pullback: Dict[str, any],
                                     vwap: Dict[str, any],
                                     ema_alignment: Dict[str, bool]) -> List[str]:
        """构建结构说明"""
        structure = []

        if is_super_trend:
            structure.append("超级趋势 (EMA21 > EMA55 > EMA200)")

        if trend == TrendDirection.BULLISH:
            structure.append("4H 多头趋势")
        else:
            structure.append("4H 空头趋势")

        if momentum_4h["histogram_growing"]:
            structure.append("4H 动能增强")

        if momentum_15m["bar_turning_red"] or momentum_15m["cross_over"]:
            structure.append("15m MACD 启动")
        elif momentum_15m["bar_turning_green"] or momentum_15m["cross_under"]:
            structure.append("15m MACD 转弱")

        structure.append(f"15m 回踩 EMA21 (深度: {pullback['pullback_depth']*100:.2f}%)")

        if vwap["above_vwap"]:
            structure.append("价格 > VWAP (资金流入)")
        else:
            structure.append("价格 < VWAP (资金流出)")

        return structure

    def _generate_reason(self,
                        trend: TrendDirection,
                        strength: SignalStrength,
                        structure: List[str]) -> str:
        """生成交易理由"""
        trend_text = "多头" if trend == TrendDirection.BULLISH else "空头"
        strength_text = {
            SignalStrength.VERY_STRONG: "极强",
            SignalStrength.STRONG: "强",
            SignalStrength.MODERATE: "中等",
            SignalStrength.WEAK: "弱"
        }[strength]

        reason = f"{trend_text}信号 (强度: {strength_text})。"
        reason += " 结构: " + "; ".join(structure)
        reason += f"。 符合'大周期定趋势，小周期找买点'原则。"

        return reason


def create_strategy_config() -> Dict[str, any]:
    """创建策略配置"""
    return {
        "ema": {
            "ema21_period": 21,
            "ema55_period": 55,
            "ema200_period": 200
        },
        "macd": {
            "4h": {
                "fast_period": 12,
                "slow_period": 26,
                "signal_period": 9
            },
            "15m": {
                "fast_period": 8,
                "slow_period": 21,
                "signal_period": 5
            }
        },
        "vwap": {
            "use_anchored": True,
            "anchor_period": "weekly"
        },
        "entry": {
            "pullback_min_depth": 0.01,  # 最小回踩 1%
            "pullback_max_depth": 0.03,  # 最大回踩 3%
            "ema21_tolerance": 0.02      # EMA21 容差 2%
        },
        "exit": {
            "default_stop_loss_pct": 0.02,    # 默认止损 2%
            "default_take_profit_pct": 0.05,  # 默认止盈 5%
            "min_risk_reward": 2.0             # 最小风险回报比
        }
    }
