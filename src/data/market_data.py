"""
市场数据管理器
负责获取和处理市场数据
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from src.api.binance_client import BinanceClient
from src.utils.indicators import (
    calculate_atr,
    calculate_bollinger_bands,
    calculate_ema,
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
        except Exception as e:
            print(f"⚠️ 获取实时市场数据失败 {symbol}: {e}")
            return None

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
