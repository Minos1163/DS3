"""
技术指标计算
RSI, MACD, EMA, ATR等
"""

from typing import Optional

import pandas as pd


def calculate_rsi(prices: pd.Series, period: int = 14) -> Optional[float]:
    """
    计算RSI指标

    Args:
        prices: 价格序列（通常是收盘价）
        period: RSI周期，默认14

    Returns:
        最新RSI值
    """
    if len(prices) < period + 1:
        return None

    try:
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        return float(rsi.iloc[-1])
    except BaseException:
        return None


def calculate_macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """
    计算MACD指标

    Returns:
        (macd, signal, histogram)
        macd: MACD线
        signal: 信号线
        histogram: MACD柱状图（macd - signal）
    """
    if len(prices) < slow + signal:
        return None, None, None

    try:
        ema_fast = prices.ewm(span=fast, adjust=False).mean()
        ema_slow = prices.ewm(span=slow, adjust=False).mean()

        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return (
            float(macd_line.iloc[-1]),
            float(signal_line.iloc[-1]),
            float(histogram.iloc[-1]),
        )
    except BaseException:
        return None, None, None


def calculate_ema(prices: pd.Series, period: int) -> Optional[float]:
    """
    计算EMA（指数移动平均）

    Args:
        prices: 价格序列
        period: EMA周期

    Returns:
        最新EMA值
    """
    if len(prices) < period:
        return None

    try:
        ema = prices.ewm(span=period, adjust=False).mean()
        return float(ema.iloc[-1])
    except BaseException:
        return None


def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> Optional[float]:
    """
    计算ATR（真实波动幅度）

    Args:
        high: 最高价序列
        low: 最低价序列
        close: 收盘价序列
        period: ATR周期

    Returns:
        最新ATR值
    """
    if len(high) < period + 1:
        return None

    try:
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()

        return float(atr.iloc[-1])
    except BaseException:
        return None


def calculate_volume_ratio(current_volume: float, avg_volume: float) -> float:
    """
    计算成交量比率

    Returns:
        当前成交量相对于平均成交量的百分比
    """
    if avg_volume == 0:
        return 0.0
    return (current_volume / avg_volume) * 100


def calculate_change_percent(current: float, previous: float) -> float:
    """
    计算涨跌百分比

    Returns:
        涨跌百分比
    """
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100


def calculate_sma(prices: pd.Series, period: int) -> Optional[float]:
    """
    计算SMA（简单移动平均）

    Args:
        prices: 价格序列
        period: SMA周期

    Returns:
        最新SMA值
    """
    if len(prices) < period:
        return None

    try:
        sma = prices.rolling(window=period).mean()
        return float(sma.iloc[-1])
    except BaseException:
        return None


def calculate_bollinger_bands(prices: pd.Series, period: int = 20, num_std: float = 2.0) -> tuple:
    """
    计算布林带

    Args:
        prices: 价格序列
        period: 周期
        num_std: 标准差倍数

    Returns:
        (middle, upper, lower)
    """
    if len(prices) < period:
        return None, None, None

    try:
        sma = prices.rolling(window=period).mean()
        std = prices.rolling(window=period).std()

        upper = sma + (std * num_std)
        lower = sma - (std * num_std)

        return (
            float(sma.iloc[-1]),
            float(upper.iloc[-1]),
            float(lower.iloc[-1]),
        )
    except BaseException:
        return None, None, None
