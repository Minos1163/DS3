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
        # ensure float literals to avoid type-checker operator issues with Series
        rsi = 100.0 - (100.0 / (1.0 + rs))

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


def calculate_kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 9,
    smooth: int = 3,
) -> tuple:
    """
    计算 KDJ 指标。

    Returns:
        (k, d, j)
    """
    if len(high) < period or len(low) < period or len(close) < period:
        return None, None, None

    try:
        low_n = low.rolling(window=period).min()
        high_n = high.rolling(window=period).max()
        spread = (high_n - low_n).replace(0, pd.NA)
        rsv = ((close - low_n) / spread) * 100.0
        alpha = 1.0 / float(max(1, smooth))
        k = rsv.ewm(alpha=alpha, adjust=False).mean()
        d = k.ewm(alpha=alpha, adjust=False).mean()
        j = 3.0 * k - 2.0 * d
        k_val = k.iloc[-1]
        d_val = d.iloc[-1]
        j_val = j.iloc[-1]
        if pd.isna(k_val) or pd.isna(d_val) or pd.isna(j_val):
            return None, None, None
        return float(k_val), float(d_val), float(j_val)
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


def calculate_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> Optional[float]:
    """
    计算ADX（平均趋向指数）

    Args:
        high: 最高价序列
        low: 最低价序列
        close: 收盘价序列
        period: ADX周期

    Returns:
        最新ADX值
    """
    if len(high) < period + 1 or len(low) < period + 1 or len(close) < period + 1:
        return None

    try:
        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.rolling(window=period).mean()
        plus_di = 100.0 * (plus_dm.rolling(window=period).mean() / atr.replace(0, pd.NA))
        minus_di = 100.0 * (minus_dm.rolling(window=period).mean() / atr.replace(0, pd.NA))

        di_sum = (plus_di + minus_di).replace(0, pd.NA)
        dx = ((plus_di - minus_di).abs() / di_sum) * 100.0
        adx = dx.rolling(window=period).mean()
        value = adx.iloc[-1]
        if pd.isna(value):
            return None
        return float(value)
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


def calculate_bbi(prices: pd.Series, periods: Optional[list] = None) -> Optional[float]:
    """
    计算BBI（多空指标）
    BBI = (MA3 + MA6 + MA12 + MA24) / 4
    价格在BBI上方为多头市场，下方为空头市场

    Args:
        prices: 价格序列
        periods: 均线周期列表，默认[3, 6, 12, 24]

    Returns:
        最新BBI值
    """
    if periods is None:
        periods = [3, 6, 12, 24]

    min_len = max(periods)
    if len(prices) < min_len:
        return None

    try:
        ma_sum = 0.0
        for p in periods:
            ma = prices.rolling(window=p).mean()
            ma_sum += float(ma.iloc[-1])
        return ma_sum / len(periods)
    except BaseException:
        return None


def calculate_ema_slope(prices: pd.Series, period: int = 20, slope_period: int = 3) -> Optional[float]:
    """
    计算EMA斜率（用于判断趋势强度）

    Args:
        prices: 价格序列
        period: EMA周期
        slope_period: 斜率计算周期（用几根K线计算斜率）

    Returns:
        斜率百分比（每根K线的变化率）
    """
    if len(prices) < period + slope_period:
        return None

    try:
        ema = prices.ewm(span=period, adjust=False).mean()
        # 取最近slope_period根K线的EMA值
        recent_ema = ema.iloc[-slope_period:]
        if len(recent_ema) < 2:
            return None
        # 计算斜率：(最新EMA - slope_period根前EMA) / slope_period根前EMA
        slope = (float(recent_ema.iloc[-1]) - float(recent_ema.iloc[0])) / float(recent_ema.iloc[0])
        return slope
    except BaseException:
        return None


def calculate_ema_diff_pct(prices: pd.Series, fast_period: int = 20, slow_period: int = 50) -> Optional[float]:
    """
    计算快慢EMA差值百分比

    Args:
        prices: 价格序列
        fast_period: 快EMA周期
        slow_period: 慢EMA周期

    Returns:
        (快EMA - 慢EMA) / 慢EMA 百分比
    """
    if len(prices) < slow_period:
        return None

    try:
        ema_fast = prices.ewm(span=fast_period, adjust=False).mean()
        ema_slow = prices.ewm(span=slow_period, adjust=False).mean()

        fast_val = float(ema_fast.iloc[-1])
        slow_val = float(ema_slow.iloc[-1])

        if slow_val == 0:
            return None

        return (fast_val - slow_val) / slow_val
    except BaseException:
        return None
