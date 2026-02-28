"""
市场数据管理器
负责获取和处理市场数据
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from src.api.binance_client import BinanceClient
from src.utils.indicators import (
    calculate_adx,
    calculate_atr,
    calculate_bbi,
    calculate_bollinger_bands,
    calculate_ema,
    calculate_ema_diff_pct,
    calculate_ema_slope,
    calculate_kdj,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
    calculate_volume_ratio,
)


class MarketDataManager:
    """市场数据管理器"""

    def __init__(self, client: BinanceClient):
        """
        初始化市场数据管理器

        Args:
            client: Binance API客户端
        """
        self.client = client

    def get_multi_timeframe_data(self, symbol: str, intervals: List[str]) -> Dict[str, Any]:
        """
        获取多周期K线数据

        Args:
            symbol: 交易对
            intervals: 时间周期列表，如 ['15m', '30m', '1h', '4h', '1d']

        Returns:
            {
            '15m': {'klines': [...], 'dataframe': df, 'indicators': {...}},
                '30m': {...},
                ...
            }
        """
        result = {}

        for interval in intervals:
            try:
                # 获取原始K线数据
                # EMA50需要50根K线，为了足够的精度和安全，获取200根
                klines = self.client.get_klines(symbol, interval, limit=200)

                if not klines:
                    continue

                # 转换为DataFrame
                df = pd.DataFrame(
                    klines,
                    columns=[
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "close_time",
                        "quote_volume",
                        "trades",
                        "taker_buy_base",
                        "taker_buy_quote",
                        "ignore",
                    ],
                )

                # 保留所需列
                df = df[["timestamp", "open", "high", "low", "close", "volume"]]

                # 转换为数值
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                # 计算技术指标
                indicators = self._calculate_indicators(df)

                result[interval] = {
                    "klines": klines,
                    "dataframe": df,
                    "indicators": indicators,
                }

            except Exception as e:
                print(f"⚠️ 获取{interval}周期数据失败 {symbol}: {e}")
                continue

        return result

    def _calculate_indicators(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        计算技术指标

        Returns:
            {
            'rsi': 50.0,
                'macd': {...},
                'ema_20': 115000.0,
                'ema_50': 114000.0,
                'atr': 500.0,
                ...
            }
        """
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        indicators = {}

        # RSI
        rsi = calculate_rsi(close, period=14)
        indicators["rsi"] = rsi

        # MACD
        macd, signal, histogram = calculate_macd(close)
        indicators["macd"] = macd
        indicators["macd_signal"] = signal
        indicators["macd_histogram"] = histogram

        # EMA (确保有足够的数据)
        ema_20 = calculate_ema(close, period=20) if len(close) >= 20 else None
        ema_50 = calculate_ema(close, period=50) if len(close) >= 50 else None
        indicators["ema_20"] = ema_20 if ema_20 is not None else 0
        indicators["ema_50"] = ema_50 if ema_50 is not None else 0

        # SMA
        sma_20 = calculate_sma(close, period=20) if len(close) >= 20 else None
        sma_50 = calculate_sma(close, period=50) if len(close) >= 50 else None
        indicators["sma_20"] = sma_20 if sma_20 is not None else 0
        indicators["sma_50"] = sma_50 if sma_50 is not None else 0

        # 布林带
        bb_middle, bb_upper, bb_lower = calculate_bollinger_bands(close, period=20, num_std=2.0)
        indicators["bollinger_middle"] = bb_middle if bb_middle is not None else 0
        indicators["bollinger_upper"] = bb_upper if bb_upper is not None else 0
        indicators["bollinger_lower"] = bb_lower if bb_lower is not None else 0

        # ATR
        atr = calculate_atr(high, low, close, period=14)
        indicators["atr_14"] = atr

        # Volume
        if len(volume) >= 20:
            avg_volume = volume.tail(20).mean()
            current_volume = volume.iloc[-1]
            indicators["volume_ratio"] = calculate_volume_ratio(current_volume, avg_volume)
            indicators["avg_volume"] = avg_volume

        return indicators

    def get_realtime_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取实时市场数据

        Returns:
            {
            'price': 115000.0,
                'change_24h': 1.23,
                'change_15m': 0.5,
                'volume_24h': 10000.0,
                'high_24h': 116000.0,
                'low_24h': 114000.0,
                'funding_rate': 0.0001,
                'open_interest': 1000000.0
            }
        """
        try:
            # 获取24h行情
            ticker = self.client.get_ticker(symbol)
            if not ticker:
                return None

            # 获取资金费率
            funding_rate = self.client.get_funding_rate(symbol)

            # 获取持仓量
            open_interest = self.client.get_open_interest(symbol)

            # 获取15m K线计算15分钟涨跌幅
            klines_15m = self.client.get_klines(symbol, "15m", limit=2)
            change_15m = 0.0
            if klines_15m and len(klines_15m) >= 2:
                prev_close = float(klines_15m[-2][4])
                current_close = float(klines_15m[-1][4])
                if prev_close > 0:
                    change_15m = ((current_close - prev_close) / prev_close) * 100

            return {
                "price": float(ticker["lastPrice"]),
                "change_24h": float(ticker.get("priceChangePercent", 0)),
                "change_15m": change_15m,
                "volume_24h": float(ticker.get("volume", 0)),
                "high_24h": float(ticker.get("highPrice", 0)),
                "low_24h": float(ticker.get("lowPrice", 0)),
                "funding_rate": funding_rate if funding_rate else 0.0,
                "open_interest": open_interest if open_interest else 0.0,
            }
        except Exception:
            # 错误由调用方汇总处理，此处静默返回
            return None

    def get_trend_filter_metrics(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 120,
    ) -> Dict[str, float]:
        """
        获取趋势过滤指标：
        - ema_fast(EMA20), ema_slow(EMA50)
        - adx(14), atr_pct(ATR14 / last_close)
        - bbi(多空指标), macd, macd_signal, macd_hist
        - ema_slope(EMA20斜率), ema_diff_pct(快慢EMA差值%)
        - last_close(最新价格)
        - 归一化指标: *_norm (范围 [-1, 1])
        """
        try:
            klines = self.client.get_klines(symbol, interval, limit=limit)
            if not klines or len(klines) < 60:
                return {}

            df = pd.DataFrame(
                klines,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_volume",
                    "trades",
                    "taker_buy_base",
                    "taker_buy_quote",
                    "ignore",
                ],
            )
            for col in ("high", "low", "close", "open"):
                df[col] = pd.to_numeric(df[col], errors="coerce")

            close = df["close"]
            high = df["high"]
            low = df["low"]

            # 基础指标
            ema_fast = calculate_ema(close, period=20)
            ema_slow = calculate_ema(close, period=50)
            adx = calculate_adx(high, low, close, period=14)
            atr = calculate_atr(high, low, close, period=14)
            last_close = float(close.iloc[-1]) if len(close) > 0 else 0.0
            last_open = float(df["open"].iloc[-1]) if len(df["open"]) > 0 else 0.0
            atr_pct = (float(atr) / last_close) if (atr is not None and last_close > 0) else None

            # 新增指标
            bbi = calculate_bbi(close, periods=[3, 6, 12, 24])
            macd, macd_signal, macd_hist = calculate_macd(close, fast=12, slow=26, signal=9)
            kdj_k, kdj_d, kdj_j = calculate_kdj(high, low, close, period=9, smooth=3)
            ema_slope = calculate_ema_slope(close, period=20, slope_period=3)
            ema_diff_pct = calculate_ema_diff_pct(close, fast_period=20, slow_period=50)

            if ema_fast is None or ema_slow is None or adx is None or atr_pct is None:
                return {}

            result = {
                "ema_fast": float(ema_fast),
                "ema_slow": float(ema_slow),
                "adx": float(adx),
                "atr_pct": float(atr_pct),
                "last_close": last_close,
                "last_open": last_open,
            }

            # 归一化辅助函数
            def _normalize(value: float, rolling_std_series: pd.Series, eps: float = 1e-9) -> float:
                """使用 rolling_std 归一化到 [-1, 1]"""
                if value is None or rolling_std_series is None or len(rolling_std_series) < 10:
                    return 0.0
                std = float(rolling_std_series.iloc[-1]) if not pd.isna(rolling_std_series.iloc[-1]) else eps
                norm = value / (std + eps)
                return max(-1.0, min(1.0, norm / 3.0))  # clip to [-1, 1]

            # 添加原始指标
            if bbi is not None:
                result["bbi"] = float(bbi)
                # BBI gap 归一化
                bbi_gap_pct = (last_close - float(bbi)) / float(bbi) if float(bbi) > 0 else 0.0
                result["bbi_gap_pct"] = bbi_gap_pct
                # 计算 bbi_gap 滚动标准差用于归一化
                if len(close) >= 30:
                    bbi_series = close.rolling(window=3).mean() + close.rolling(window=6).mean() + \
                                 close.rolling(window=12).mean() + close.rolling(window=24).mean()
                    bbi_series = bbi_series / 4.0
                    bbi_gap_series = (close - bbi_series) / bbi_series.replace(0, pd.NA)
                    bbi_gap_std = bbi_gap_series.rolling(window=20).std()
                    result["bbi_gap_norm"] = _normalize(bbi_gap_pct, bbi_gap_std)
                else:
                    result["bbi_gap_norm"] = max(-1.0, min(1.0, bbi_gap_pct * 100))  # 简单映射

            if macd_hist is not None:
                result["macd_hist"] = float(macd_hist)
                # MACD hist 归一化
                if len(close) >= 30:
                    # 计算 hist 滚动标准差
                    ema12 = close.ewm(span=12, adjust=False).mean()
                    ema26 = close.ewm(span=26, adjust=False).mean()
                    macd_line = ema12 - ema26
                    signal_line = macd_line.ewm(span=9, adjust=False).mean()
                    hist_series = macd_line - signal_line
                    hist_std = hist_series.rolling(window=20).std()
                    result["macd_hist_norm"] = _normalize(float(macd_hist), hist_std)
                else:
                    # 简单归一化
                    norm_val = float(macd_hist) / (last_close * 0.001 + 1e-9)
                    result["macd_hist_norm"] = max(-1.0, min(1.0, norm_val))

            if macd is not None:
                result["macd"] = float(macd)
            if macd_signal is not None:
                result["macd_signal"] = float(macd_signal)

            if kdj_k is not None and kdj_d is not None and kdj_j is not None:
                result["kdj_k"] = float(kdj_k)
                result["kdj_d"] = float(kdj_d)
                result["kdj_j"] = float(kdj_j)
                j_centered = float(kdj_j) - 50.0
                if len(close) >= 30:
                    low_n = low.rolling(window=9).min()
                    high_n = high.rolling(window=9).max()
                    spread = (high_n - low_n).replace(0, pd.NA)
                    rsv_series = ((close - low_n) / spread) * 100.0
                    k_series = rsv_series.ewm(alpha=1.0 / 3.0, adjust=False).mean()
                    d_series = k_series.ewm(alpha=1.0 / 3.0, adjust=False).mean()
                    j_series = 3.0 * k_series - 2.0 * d_series
                    j_centered_std = (j_series - 50.0).rolling(window=20).std()
                    result["kdj_j_norm"] = _normalize(j_centered, j_centered_std)
                else:
                    result["kdj_j_norm"] = max(-1.0, min(1.0, j_centered / 50.0))

            if ema_diff_pct is not None:
                result["ema_diff_pct"] = float(ema_diff_pct)
                # EMA diff 归一化
                if len(close) >= 30:
                    ema20 = close.ewm(span=20, adjust=False).mean()
                    ema50 = close.ewm(span=50, adjust=False).mean()
                    diff_series = (ema20 - ema50) / ema50.replace(0, pd.NA)
                    diff_std = diff_series.rolling(window=20).std()
                    result["ema_diff_norm"] = _normalize(float(ema_diff_pct), diff_std)
                else:
                    result["ema_diff_norm"] = max(-1.0, min(1.0, float(ema_diff_pct) * 100))

            if ema_slope is not None:
                result["ema_slope"] = float(ema_slope)
                # EMA slope 归一化 - 使用 rolling_std（已经是 pct 格式）
                if len(close) >= 30:
                    # 计算 EMA slope 的滚动标准差
                    ema20_slope = close.ewm(span=20, adjust=False).mean()
                    # 计算滚动 slope pct
                    slope_series = ema20_slope.pct_change(periods=3)
                    slope_std = slope_series.rolling(window=20).std()
                    result["ema_slope_norm"] = _normalize(float(ema_slope), slope_std)
                else:
                    result["ema_slope_norm"] = max(-1.0, min(1.0, float(ema_slope) * 100))

            return result
        except Exception:
            return {}

    def format_market_data_for_ai(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        multi_data: Dict[str, Any],
    ) -> str:
        """
        格式化市场数据供AI分析

        Returns:
            格式化的市场数据字符串
        """
        result = f"\n=== {symbol} ===\n"

        # 实时行情
        realtime = market_data.get("realtime", {})
        price = realtime.get("price", 0) or 0
        change_24h = realtime.get("change_24h", 0) or 0
        change_15m = realtime.get("change_15m", 0) or 0
        funding_rate = realtime.get("funding_rate", 0) or 0
        open_interest = realtime.get("open_interest", 0) or 0

        result += f"价格: ${price:,.2f} | "
        result += f"24h: {change_24h:.2f}% | "
        result += f"15m: {change_15m:.2f}%\n"
        result += f"资金费率: {funding_rate:.6f} | "
        result += f"持仓量: {open_interest:,.0f}\n"

        # 多周期K线和指标
        for interval, data in multi_data.items():
            if "indicators" not in data:
                continue

            ind = data["indicators"]
            result += f"\n【{interval}周期】\n"

            # 显示最近几根K线
            klines = data["klines"]
            for i, kline in enumerate(klines[-5:], 1):  # 显示最近5根
                open_p = float(kline[1])
                float(kline[2])
                float(kline[3])
                close_p = float(kline[4])
                change = ((close_p - open_p) / open_p * 100) if open_p > 0 else 0
                body = "🟢" if close_p > open_p else "🔴" if close_p < open_p else "➖"

                result += f"  K{i}: {body} C${close_p:.2f} ({change:+.2f}%)\n"

            # 技术指标
            rsi = ind.get("rsi") or 0
            macd = ind.get("macd") or 0
            ema20 = ind.get("ema_20") or 0
            ema50 = ind.get("ema_50") or 0

            result += f"  指标: RSI={rsi:.1f} "
            result += f"MACD={macd:.2f} "
            result += f"EMA20={ema20:.2f} "
            result += f"EMA50={ema50:.2f}\n"

        return result
