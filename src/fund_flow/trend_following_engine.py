"""
趋势跟踪引擎 (EMA21 回踩二次启动模型)

基于 4H + 15m 双周期的趋势跟踪策略
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
import numpy as np
from datetime import datetime, timezone

from src.fund_flow.dual_timeframe_strategy import (
    DualTimeframeStrategy,
    EMAConfig,
    MACDConfig,
    VWAPConfig,
    TrendDirection,
    SignalStrength,
    Signal,
    TechnicalIndicators
)


class MarketRegime(Enum):
    """市场状态"""
    TRENDING_BULLISH = "trending_bullish"      # 趋势多头
    TRENDING_BEARISH = "trending_bearish"      # 趋势空头
    RANGE_BOUND = "range_bound"                 # 震荡
    CHOPPY = "choppy"                           # 混乱


@dataclass
class MarketState:
    """市场状态"""
    regime: MarketRegime
    trend_direction: TrendDirection
    trend_strength: SignalStrength
    volatility: str  # "low", "normal", "high"
    vwap_alignment: str  # "above", "below", "neutral"
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_trending(self) -> bool:
        """是否为趋势市场"""
        return self.regime in [MarketRegime.TRENDING_BULLISH, MarketRegime.TRENDING_BEARISH]

    def is_bullish(self) -> bool:
        """是否为多头市场"""
        return self.regime == MarketRegime.TRENDING_BULLISH

    def is_bearish(self) -> bool:
        """是否为空头市场"""
        return self.regime == MarketRegime.TRENDING_BEARISH


@dataclass
class TradingContext:
    """交易上下文"""
    symbol: str
    market_state: MarketState
    signal: Optional[Signal] = None
    entry_confidence: float = 0.0
    risk_level: str = "medium"  # "low", "medium", "high"


class MarketRegimeDetector:
    """市场状态检测器"""

    def __init__(self, ema_config: EMAConfig):
        self.ema_config = ema_config

    def detect_regime(self,
                     close_prices_4h: np.ndarray,
                     close_prices_1h: np.ndarray) -> MarketState:
        """
        检测市场状态

        依据：
        - EMA 排列
        - 价格波动性
        - 趋势稳定性
        """
        # 计算 EMA
        ema21 = TechnicalIndicators.calculate_ema(close_prices_4h, self.ema_config.ema21_period)
        ema55 = TechnicalIndicators.calculate_ema(close_prices_4h, self.ema_config.ema55_period)
        ema200 = TechnicalIndicators.calculate_ema(close_prices_4h, self.ema_config.ema200_period)

        current_price = close_prices_4h[-1]

        # 判断趋势方向
        if current_price > ema55[-1] and ema21[-1] > ema55[-1]:
            trend_direction = TrendDirection.BULLISH
        elif current_price < ema55[-1] and ema21[-1] < ema55[-1]:
            trend_direction = TrendDirection.BEARISH
        else:
            trend_direction = TrendDirection.NEUTRAL

        # 判断趋势强度
        if trend_direction == TrendDirection.BULLISH:
            if current_price > ema21[-1] > ema55[-1] > ema200[-1]:
                trend_strength = SignalStrength.VERY_STRONG
            elif current_price > ema21[-1] > ema55[-1]:
                trend_strength = SignalStrength.STRONG
            else:
                trend_strength = SignalStrength.MODERATE
        elif trend_direction == TrendDirection.BEARISH:
            if current_price < ema21[-1] < ema55[-1] < ema200[-1]:
                trend_strength = SignalStrength.VERY_STRONG
            elif current_price < ema21[-1] < ema55[-1]:
                trend_strength = SignalStrength.STRONG
            else:
                trend_strength = SignalStrength.MODERATE
        else:
            trend_strength = SignalStrength.WEAK

        # 检测波动性
        volatility = self._detect_volatility(close_prices_1h)

        # 判断市场状态
        if trend_strength in [SignalStrength.VERY_STRONG, SignalStrength.STRONG]:
            regime = MarketRegime.TRENDING_BULLISH if trend_direction == TrendDirection.BULLISH else MarketRegime.TRENDING_BEARISH
        elif volatility == "high" and trend_strength == SignalStrength.WEAK:
            regime = MarketRegime.CHOPPY
        else:
            regime = MarketRegime.RANGE_BOUND

        # 判断 VWAP 对齐（需要高低价和成交量数据）
        # 这里简化处理
        vwap_alignment = "neutral"

        return MarketState(
            regime=regime,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            volatility=volatility,
            vwap_alignment=vwap_alignment
        )

    def _detect_volatility(self, close_prices: np.ndarray, lookback: int = 20) -> str:
        """检测波动性"""
        if len(close_prices) < lookback:
            return "normal"

        recent_prices = close_prices[-lookback:]
        std_dev = np.std(recent_prices) / np.mean(recent_prices)

        if std_dev < 0.01:
            return "low"
        elif std_dev > 0.03:
            return "high"
        else:
            return "normal"


class EMAPullbackStrategy:
    """
    EMA21 回踩二次启动策略

    核心逻辑：
    1. 确认 4H 趋势
    2. 等待 15m 回踩 EMA21
    3. 观察吸筹结构（震荡）
    4. 确认二次启动信号
    """

    def __init__(self,
                 ema_config: Optional[EMAConfig] = None,
                 macd_config: Optional[MACDConfig] = None,
                 vwap_config: Optional[VWAPConfig] = None):
        self.ema_config = ema_config or EMAConfig()
        self.macd_config = macd_config or MACDConfig()
        self.vwap_config = vwap_config or VWAPConfig()

        self.dual_strategy = DualTimeframeStrategy(
            ema_config, macd_config, vwap_config
        )

    def analyze_entry(self,
                      close_4h: np.ndarray,
                      high_15m: np.ndarray,
                      low_15m: np.ndarray,
                      close_15m: np.ndarray,
                      volume_15m: np.ndarray) -> Optional[Signal]:
        """
        分析入场点

        使用双周期策略进行完整分析
        """
        return self.dual_strategy.analyze(
            close_4h, high_15m, low_15m, close_15m, volume_15m
        )

    def check_pullback_structure(self, close_15m: np.ndarray) -> Dict[str, any]:
        """
        检查回踩结构

        特征：
        - 小K线横盘
        - MACD柱子缩短
        - 成交量萎缩
        """
        ema21 = TechnicalIndicators.calculate_ema(close_15m, self.ema_config.ema21_period)

        # 计算最近的价格波动
        recent_prices = close_15m[-10:]
        price_volatility = np.std(recent_prices) / np.mean(recent_prices)

        # 计算距离 EMA21 的距离
        current_price = close_15m[-1]
        ema21_value = ema21[-1]
        distance = abs(current_price - ema21_value) / ema21_value

        # 判断是否在 EMA21 附近（1%-3%）
        near_ema21 = 0.01 <= distance <= 0.03

        # 计算K线大小（波动）
        if len(close_15m) >= 3:
            recent_body_sizes = []
            for i in range(-3, 0):
                if i + len(close_15m) >= 0:
                    # 假设有高低价数据，这里简化处理
                    recent_body_sizes.append(0.001)  # 占位

            avg_body_size = np.mean(recent_body_sizes)
            is_consolidating = avg_body_size < 0.002  # 小K线
        else:
            is_consolidating = False

        # MACD 动能（需要价格数据）
        macd_line, signal_line, histogram = TechnicalIndicators.calculate_macd(
            close_15m,
            self.macd_config.fast_period_15m,
            self.macd_config.slow_period_15m,
            self.macd_config.signal_period_15m
        )

        # 检查柱子是否在缩短
        if len(histogram) >= 3:
            hist_shortening = (
                abs(histogram[-1]) < abs(histogram[-2]) < abs(histogram[-3])
            )
        else:
            hist_shortening = False

        # 判断是否为有效回踩结构
        valid_structure = (
            near_ema21 and
            is_consolidating and
            hist_shortening
        )

        return {
            "valid_structure": valid_structure,
            "near_ema21": near_ema21,
            "is_consolidating": is_consolidating,
            "hist_shortening": hist_shortening,
            "distance_to_ema21": distance,
            "price_volatility": price_volatility
        }

    def detect_secondary_launch(self,
                                close_15m: np.ndarray) -> Dict[str, any]:
        """
        检测二次启动信号

        特征：
        - MACD柱子翻红
        - MACD金叉
        - 价格突破 VWAP
        """
        macd_line, signal_line, histogram = TechnicalIndicators.calculate_macd(
            close_15m,
            self.macd_config.fast_period_15m,
            self.macd_config.slow_period_15m,
            self.macd_config.signal_period_15m
        )

        # 检查柱子翻红
        bar_turning_red = False
        if len(histogram) >= 2:
            bar_turning_red = (histogram[-2] < 0 and histogram[-1] > 0)

        # 检查金叉
        cross_over = False
        if len(histogram) >= 2:
            cross_over = (histogram[-2] <= 0 and histogram[-1] > 0)

        # 检查 MACD 线金叉
        macd_cross_over = False
        if len(macd_line) >= 2 and len(signal_line) >= 2:
            macd_cross_over = (macd_line[-2] <= signal_line[-2] and
                               macd_line[-1] > signal_line[-1])

        # 判断是否有启动信号
        launch_signal = bar_turning_red or cross_over or macd_cross_over

        return {
            "launch_signal": launch_signal,
            "bar_turning_red": bar_turning_red,
            "cross_over": cross_over,
            "macd_cross_over": macd_cross_over,
            "histogram_value": histogram[-1] if len(histogram) > 0 else 0
        }


class TrendFollowingEngine:
    """
    趋势跟踪引擎

    整合市场状态检测、EMA 回踩策略和信号生成
    """

    def __init__(self,
                 ema_config: Optional[EMAConfig] = None,
                 macd_config: Optional[MACDConfig] = None,
                 vwap_config: Optional[VWAPConfig] = None):
        self.ema_config = ema_config or EMAConfig()
        self.macd_config = macd_config or MACDConfig()
        self.vwap_config = vwap_config or VWAPConfig()

        self.regime_detector = MarketRegimeDetector(self.ema_config)
        self.pullback_strategy = EMAPullbackStrategy(
            ema_config, macd_config, vwap_config
        )

        # 状态缓存
        self.market_states: Dict[str, MarketState] = {}

    def analyze_market(self,
                      symbol: str,
                      close_4h: np.ndarray,
                      close_1h: np.ndarray) -> MarketState:
        """
        分析市场状态

        Returns:
            MarketState: 市场状态
        """
        state = self.regime_detector.detect_regime(close_4h, close_1h)
        self.market_states[symbol] = state
        return state

    def generate_signal(self,
                        symbol: str,
                        close_4h: np.ndarray,
                        high_15m: np.ndarray,
                        low_15m: np.ndarray,
                        close_15m: np.ndarray,
                        volume_15m: np.ndarray) -> Optional[Signal]:
        """
        生成交易信号

        流程：
        1. 分析市场状态
        2. 检查是否为趋势市场
        3. 使用回踩策略分析入场点
        4. 计算置信度和风险等级
        """
        # 分析市场状态（需要 1h 数据）
        if len(close_4h) >= 200 and len(close_15m) >= 200:
            close_1h_for_regime = close_4h  # 简化处理
            market_state = self.analyze_market(symbol, close_4h, close_1h_for_regime)
        else:
            # 数据不足，无法判断状态
            market_state = MarketState(
                regime=MarketRegime.RANGE_BOUND,
                trend_direction=TrendDirection.NEUTRAL,
                trend_strength=SignalStrength.WEAK,
                volatility="normal",
                vwap_alignment="neutral"
            )

        # 只在趋势市场交易
        if not market_state.is_trending():
            return None

        # 使用回踩策略分析
        signal = self.pullback_strategy.analyze_entry(
            close_4h, high_15m, low_15m, close_15m, volume_15m
        )

        if signal is None:
            return None

        # 计算入场置信度
        confidence = self._calculate_confidence(signal, market_state)

        # 确定风险等级
        risk_level = self._determine_risk_level(market_state, signal)

        # 更新信号的理由
        signal.reason = self._enhance_reason(signal.reason, market_state, confidence, risk_level)

        return signal

    def _calculate_confidence(self,
                              signal: Signal,
                              market_state: MarketState) -> float:
        """
        计算入场置信度 (0-1)

        因素：
        - 趋势强度
        - 信号强度
        - 风险回报比
        """
        # 趋势强度得分
        trend_scores = {
            SignalStrength.VERY_STRONG: 0.4,
            SignalStrength.STRONG: 0.3,
            SignalStrength.MODERATE: 0.2,
            SignalStrength.WEAK: 0.1
        }
        trend_score = trend_scores.get(market_state.trend_strength, 0.1)

        # 信号强度得分
        signal_scores = {
            SignalStrength.VERY_STRONG: 0.35,
            SignalStrength.STRONG: 0.3,
            SignalStrength.MODERATE: 0.2,
            SignalStrength.WEAK: 0.1
        }
        signal_score = signal_scores.get(signal.strength, 0.1)

        # 风险回报比得分
        rr_score = min(signal.risk_reward / 4.0, 0.25)  # 最高 4:1

        # 波动性调整
        volatility_multiplier = 1.0
        if market_state.volatility == "high":
            volatility_multiplier = 0.8  # 高波动降低置信度
        elif market_state.volatility == "low":
            volatility_multiplier = 1.1

        confidence = (trend_score + signal_score + rr_score) * volatility_multiplier

        return min(confidence, 1.0)

    def _determine_risk_level(self,
                              market_state: MarketState,
                              signal: Signal) -> str:
        """确定风险等级"""
        if signal.risk_reward < 2.0:
            return "high"
        elif signal.risk_reward < 3.0:
            return "medium"
        else:
            return "low"

    def _enhance_reason(self,
                       base_reason: str,
                       market_state: MarketState,
                       confidence: float,
                       risk_level: str) -> str:
        """增强交易理由"""
        regime_text = {
            MarketRegime.TRENDING_BULLISH: "趋势多头",
            MarketRegime.TRENDING_BEARISH: "趋势空头",
            MarketRegime.RANGE_BOUND: "震荡",
            MarketRegime.CHOPPY: "混乱"
        }[market_state.regime]

        enhanced = f"[{regime_text}] {base_reason}"
        enhanced += f" 置信度: {confidence*100:.1f}%"
        enhanced += f" 风险等级: {risk_level}"

        return enhanced

    def get_market_state(self, symbol: str) -> Optional[MarketState]:
        """获取缓存的市场状态"""
        return self.market_states.get(symbol)


# 便捷函数
def create_trend_following_engine() -> TrendFollowingEngine:
    """创建趋势跟踪引擎（使用默认配置）"""
    return TrendFollowingEngine()


def analyze_symbol_with_trend_engine(engine: TrendFollowingEngine,
                                     symbol: str,
                                     data_4h: Dict[str, List[float]],
                                     data_15m: Dict[str, List[float]]) -> Optional[Signal]:
    """
    使用趋势引擎分析单个交易对

    Args:
        engine: 趋势跟踪引擎实例
        symbol: 交易对名称
        data_4h: 4H 数据，包含 'close' 键
        data_15m: 15m 数据，包含 'open', 'high', 'low', 'close', 'volume' 键

    Returns:
        Signal 或 None
    """
    close_4h = np.array(data_4h['close'])

    high_15m = np.array(data_15m['high'])
    low_15m = np.array(data_15m['low'])
    close_15m = np.array(data_15m['close'])
    volume_15m = np.array(data_15m['volume'])

    return engine.generate_signal(
        symbol, close_4h, high_15m, low_15m, close_15m, volume_15m
    )
