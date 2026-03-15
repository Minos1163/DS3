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

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @classmethod
    def extract_order_flow_metrics_from_klines(
        cls,
        klines: List[Any],
        rolling_window: int = 4,
        toxicity_window: int = 8,
    ) -> Dict[str, float]:
        """从 Binance K 线的 taker buy/sell 字段提取真实订单流代理。

        说明:
        - Binance K 线自带 taker buy base/quote，可直接推导 taker sell。
        - `orderflow_cvd_ratio` 使用最近 rolling_window 根 K 线的累计主动买卖差 / 累计成交额。
        - `orderflow_cvd_momentum` 使用当前 rolling CVD ratio 相对上一窗口的变化。
        - `vpin` 这里采用时间桶近似: 最近 toxicity_window 根 K 线的 |delta|/volume 均值，
          作为“流毒性/噪声强度”开关输入。
        """
        parsed: List[Dict[str, float]] = []
        for row in klines or []:
            if not isinstance(row, list) or len(row) < 11:
                continue
            total_base = max(0.0, cls._to_float(row[5], 0.0))
            quote_volume = max(0.0, cls._to_float(row[7], 0.0))
            taker_buy_base = max(0.0, cls._to_float(row[9], 0.0))
            taker_buy_quote = max(0.0, cls._to_float(row[10], 0.0))
            taker_sell_base = max(0.0, total_base - taker_buy_base)
            taker_sell_quote = max(0.0, quote_volume - taker_buy_quote)
            delta_base = taker_buy_base - taker_sell_base
            delta_quote = taker_buy_quote - taker_sell_quote
            delta_ratio = (delta_quote / quote_volume) if quote_volume > 0 else 0.0
            imbalance_ratio = (abs(delta_quote) / quote_volume) if quote_volume > 0 else 0.0
            parsed.append(
                {
                    "total_base": total_base,
                    "quote_volume": quote_volume,
                    "taker_buy_base": taker_buy_base,
                    "taker_sell_base": taker_sell_base,
                    "taker_buy_quote": taker_buy_quote,
                    "taker_sell_quote": taker_sell_quote,
                    "delta_base": delta_base,
                    "delta_quote": delta_quote,
                    "delta_ratio": delta_ratio,
                    "imbalance_ratio": imbalance_ratio,
                }
            )

        if not parsed:
            return {
                "quote_volume": 0.0,
                "taker_buy_base": 0.0,
                "taker_sell_base": 0.0,
                "taker_buy_quote": 0.0,
                "taker_sell_quote": 0.0,
                "taker_delta_base": 0.0,
                "taker_delta_quote": 0.0,
                "trade_imbalance": 0.0,
                "volume_imbalance": 0.0,
                "orderflow_cvd_quote": 0.0,
                "orderflow_cvd_ratio": 0.0,
                "orderflow_cvd_momentum": 0.0,
                "vpin": 0.0,
                "flow_toxicity": 0.0,
            }

        rolling_n = max(1, min(len(parsed), int(rolling_window or 4)))
        toxicity_n = max(1, min(len(parsed), int(toxicity_window or 8)))
        current = parsed[-1]
        rolling = parsed[-rolling_n:]
        previous_rolling = parsed[-(rolling_n + 1) : -1] if len(parsed) > 1 else []

        rolling_quote = sum(item["quote_volume"] for item in rolling)
        rolling_delta = sum(item["delta_quote"] for item in rolling)
        prev_quote = sum(item["quote_volume"] for item in previous_rolling)
        prev_delta = sum(item["delta_quote"] for item in previous_rolling)

        rolling_ratio = (rolling_delta / rolling_quote) if rolling_quote > 0 else current["delta_ratio"]
        previous_ratio = (prev_delta / prev_quote) if prev_quote > 0 else 0.0
        toxicity_values = [item["imbalance_ratio"] for item in parsed[-toxicity_n:]]
        vpin = (sum(toxicity_values) / len(toxicity_values)) if toxicity_values else current["imbalance_ratio"]

        return {
            "quote_volume": current["quote_volume"],
            "taker_buy_base": current["taker_buy_base"],
            "taker_sell_base": current["taker_sell_base"],
            "taker_buy_quote": current["taker_buy_quote"],
            "taker_sell_quote": current["taker_sell_quote"],
            "taker_delta_base": current["delta_base"],
            "taker_delta_quote": current["delta_quote"],
            "trade_imbalance": current["delta_ratio"],
            "volume_imbalance": current["imbalance_ratio"],
            "orderflow_cvd_quote": rolling_delta,
            "orderflow_cvd_ratio": rolling_ratio,
            "orderflow_cvd_momentum": rolling_ratio - previous_ratio,
            "vpin": vpin,
            "flow_toxicity": vpin,
        }

    def get_order_flow_snapshot(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 20,
        rolling_window: Optional[int] = None,
        toxicity_window: Optional[int] = None,
    ) -> Dict[str, float]:
        """按指定周期提取订单流快照。

        用途:
        - 15m/5m 仍负责主结构与执行确认
        - 1m 仅用于入场质量评估和风险开关，不参与主方向判定
        """
        try:
            safe_limit = max(2, int(limit or 20))
            klines = self.client.get_klines(symbol, interval, limit=safe_limit)
            if not klines:
                return {}
            rolling_n = rolling_window if rolling_window is not None else min(5, safe_limit)
            toxicity_n = toxicity_window if toxicity_window is not None else min(12, safe_limit)
            metrics = self.extract_order_flow_metrics_from_klines(
                klines,
                rolling_window=rolling_n,
                toxicity_window=toxicity_n,
            )
            prev_close = self._to_float(klines[-2][4], 0.0) if len(klines) >= 2 else 0.0
            last_close = self._to_float(klines[-1][4], 0.0) if len(klines) >= 1 else 0.0
            metrics["close"] = last_close
            metrics["ret_period"] = ((last_close - prev_close) / prev_close) if prev_close > 0 else 0.0
            metrics["interval"] = str(interval or "").lower()
            metrics["bars"] = float(len(klines))
            return metrics
        except Exception:
            return {}

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

            # 获取 15m K 线:
            # - 继续保留 change_15m 兼容字段
            # - 同时利用 taker buy/sell 构造真实订单流变量
            klines_15m = self.client.get_klines(symbol, "15m", limit=8)
            change_15m = 0.0
            if klines_15m and len(klines_15m) >= 2:
                prev_close = self._to_float(klines_15m[-2][4], 0.0)
                current_close = self._to_float(klines_15m[-1][4], 0.0)
                if prev_close > 0:
                    change_15m = ((current_close - prev_close) / prev_close) * 100
            order_flow = self.extract_order_flow_metrics_from_klines(klines_15m)

            return {
                "price": float(ticker["lastPrice"]),
                "change_24h": float(ticker.get("priceChangePercent", 0)),
                "change_15m": change_15m,
                "volume_24h": float(ticker.get("volume", 0)),
                "high_24h": float(ticker.get("highPrice", 0)),
                "low_24h": float(ticker.get("lowPrice", 0)),
                "funding_rate": funding_rate if funding_rate else 0.0,
                "open_interest": open_interest if open_interest else 0.0,
                **order_flow,
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
            bb_middle, bb_upper, bb_lower = calculate_bollinger_bands(close, period=20, num_std=2.0)
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
                    if len(hist_series) >= 3:
                        hist_tail = hist_series.dropna()
                        if len(hist_tail) >= 3:
                            h2 = float(hist_tail.iloc[-3])
                            h1 = float(hist_tail.iloc[-2])
                            h0 = float(hist_tail.iloc[-1])
                            result["macd_hist_delta"] = float(h0 - h1)
                            result["macd_hist_expand"] = bool(abs(h0) > abs(h1) > abs(h2))
                            result["macd_hist_expand_up"] = bool(h0 > h1 > h2)
                            result["macd_hist_expand_down"] = bool(h0 < h1 < h2)
                    macd_line_tail = macd_line.dropna()
                    signal_tail = signal_line.dropna()
                    if len(macd_line_tail) >= 2 and len(signal_tail) >= 2:
                        m1 = float(macd_line_tail.iloc[-2])
                        m0 = float(macd_line_tail.iloc[-1])
                        s1 = float(signal_tail.iloc[-2])
                        s0 = float(signal_tail.iloc[-1])
                        macd_cross = "NONE"
                        if m1 <= s1 and m0 > s0:
                            macd_cross = "GOLDEN"
                        elif m1 >= s1 and m0 < s0:
                            macd_cross = "DEAD"
                        result["macd_cross"] = macd_cross
                        result["macd_cross_bias"] = 1.0 if macd_cross == "GOLDEN" else (-1.0 if macd_cross == "DEAD" else 0.0)
                        result["macd_zone"] = "ABOVE_ZERO" if m0 > 0 else ("BELOW_ZERO" if m0 < 0 else "NEAR_ZERO")
                else:
                    # 简单归一化
                    norm_val = float(macd_hist) / (last_close * 0.001 + 1e-9)
                    result["macd_hist_norm"] = max(-1.0, min(1.0, norm_val))
                    result["macd_hist_delta"] = 0.0
                    result["macd_hist_expand"] = False
                    result["macd_hist_expand_up"] = False
                    result["macd_hist_expand_down"] = False

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
                    k_tail = k_series.dropna()
                    d_tail = d_series.dropna()
                    if len(k_tail) >= 2 and len(d_tail) >= 2:
                        k1 = float(k_tail.iloc[-2])
                        k0 = float(k_tail.iloc[-1])
                        d1 = float(d_tail.iloc[-2])
                        d0 = float(d_tail.iloc[-1])
                        kdj_cross = "NONE"
                        if k1 <= d1 and k0 > d0:
                            kdj_cross = "GOLDEN"
                        elif k1 >= d1 and k0 < d0:
                            kdj_cross = "DEAD"
                        result["kdj_cross"] = kdj_cross
                        result["kdj_cross_bias"] = 1.0 if kdj_cross == "GOLDEN" else (-1.0 if kdj_cross == "DEAD" else 0.0)
                else:
                    result["kdj_j_norm"] = max(-1.0, min(1.0, j_centered / 50.0))
                kdj_zone = "HIGH" if float(kdj_j) >= 80.0 else ("LOW" if float(kdj_j) <= 20.0 else "MID")
                result["kdj_zone"] = kdj_zone

            if bb_middle is not None and bb_upper is not None and bb_lower is not None:
                bb_middle_f = float(bb_middle)
                bb_upper_f = float(bb_upper)
                bb_lower_f = float(bb_lower)
                band = max(bb_upper_f - bb_lower_f, 1e-9)
                width = band / max(abs(bb_middle_f), 1e-9)
                pos_norm = max(-1.0, min(1.0, (last_close - (bb_upper_f + bb_lower_f) * 0.5) / max(band * 0.5, 1e-9)))
                width_norm = max(-1.0, min(1.0, (width - 0.01) / 0.05))
                bb_break = "NONE"
                if last_close > bb_upper_f:
                    bb_break = "UPPER"
                elif last_close < bb_lower_f:
                    bb_break = "LOWER"
                bb_trend = "MID"
                if last_close >= bb_upper_f * 0.995:
                    bb_trend = "ALONG_UPPER"
                elif last_close <= bb_lower_f * 1.005:
                    bb_trend = "ALONG_LOWER"
                result["bb_middle"] = bb_middle_f
                result["bb_upper"] = bb_upper_f
                result["bb_lower"] = bb_lower_f
                result["bb_width"] = float(width)
                result["bb_width_norm"] = float(width_norm)
                result["bb_pos_norm"] = float(pos_norm)
                result["bb_break"] = bb_break
                result["bb_break_bias"] = 1.0 if bb_break == "UPPER" else (-1.0 if bb_break == "LOWER" else 0.0)
                result["bb_trend"] = bb_trend
                result["bb_trend_bias"] = 1.0 if bb_trend == "ALONG_UPPER" else (-1.0 if bb_trend == "ALONG_LOWER" else 0.0)
                result["bb_squeeze"] = bool(width <= 0.02)

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
