"""
V5 规则策略（与离线回测一致）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd


class V5Strategy:
    """V5规则策略"""

    def __init__(self, config: Dict[str, Any]):
        strategy = config.get("strategy", {})
        trading = config.get("trading", {})
        risk = config.get("risk", {})

        self.position_percent = strategy.get("position_percent", 30)
        self.leverage = strategy.get("leverage", trading.get("default_leverage", 3))

        self.stop_loss_pct = float(strategy.get("stop_loss_percent", risk.get("stop_loss_default_percent", 1.5)))
        self.take_profit_pct = float(strategy.get("take_profit_percent", risk.get("take_profit_default_percent", 5.0)))

        self.rsi_oversold = strategy.get("rsi_oversold", 32)
        self.rsi_overbought = strategy.get("rsi_overbought", 68)
        self.short_rsi_overbought = strategy.get("short_rsi_overbought", 72)

        self.volume_multiplier = strategy.get("volume_multiplier", 1.10)
        self.use_volume_quantile_filter = strategy.get("use_volume_quantile_filter", True)
        self.volume_quantile = strategy.get("volume_quantile", 0.38)
        self.short_volume_quantile = strategy.get("short_volume_quantile", 0.55)
        self.volume_window = strategy.get("volume_window", 60)

        self.use_time_filter = strategy.get("use_time_filter", True)
        self.allowed_hours = strategy.get("allowed_hours_utc", [5, 22])
        # 可配置的默认持有信心度，默认与AI最小阈值保持一致，避免规则策略频繁被误判为低信心
        self.hold_confidence = strategy.get("hold_confidence", 0.6)

    def decide(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        position: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        df = self._get_15m_dataframe(market_data)
        if df is None or len(df) < max(60, 50):
            return self._hold_decision("15m数据不足")

        df = df.copy()
        self._compute_indicators(df)

        last = df.iloc[-1]
        if self._has_nan(last):
            return self._hold_decision("指标未就绪")

        if self.use_time_filter:
            ts = last.get("timestamp")
            hour = self._extract_utc_hour(ts)
            if hour is not None and hour not in self._expand_allowed_hours():
                return self._hold_decision("不在交易时段")

        close = float(last["close"])
        rsi = float(last["rsi"])
        ema_5 = float(last["ema_5"])
        ema_20 = float(last["ema_20"])
        ma_20 = float(last["ma_20"])
        macd_hist = float(last["macd_hist"])
        bb_lower = float(last["bb_lower"])
        bb_upper = float(last["bb_upper"])
        volume = float(last["volume"])
        volume_ma = float(last["volume_ma"]) if not pd.isna(last["volume_ma"]) else 0.0
        volume_q = float(last["volume_q"]) if not pd.isna(last["volume_q"]) else None
        volume_q_short = float(last["volume_q_short"]) if "volume_q_short" in last and not pd.isna(last["volume_q_short"]) else None

        if self.use_volume_quantile_filter and volume_q is not None:
            volume_ok = volume >= volume_q
        else:
            volume_ok = volume_ma > 0 and volume > (volume_ma * self.volume_multiplier)

        current_price = market_data.get("realtime", {}).get("price") or close

        if position:
            return self._exit_decision_if_needed(position, current_price, rsi)

        # 做多信号
        if (
            rsi < self.rsi_oversold
            and ema_5 > ema_20
            and close > ma_20
            and macd_hist > 0
            and volume_ok
        ):
            return self._entry_decision(
                action="BUY_OPEN",
                reason=f"V5多头: RSI{rsi:.1f}< {self.rsi_oversold}, EMA5>EMA20, MACD柱>0, 量能满足",
            )

        if (
            ema_5 > ema_20
            and macd_hist > 0
            and close <= bb_lower * 1.02
            and close > ma_20 * 0.98
            and volume_ok
        ):
            return self._entry_decision(
                action="BUY_OPEN",
                reason="V5多头: 金叉+MACD向上+接近下轨+趋势确认+量能",
            )

        # 做空信号
        short_volume_ok = volume_ok
        if self.use_volume_quantile_filter and volume_q_short is not None:
            short_volume_ok = volume >= volume_q_short

        if (
            rsi > self.short_rsi_overbought
            and ema_5 < ema_20
            and close < ma_20
            and macd_hist < 0
            and short_volume_ok
        ):
            return self._entry_decision(
                action="SELL_OPEN",
                reason=f"V5空头: RSI{rsi:.1f}> {self.short_rsi_overbought}, EMA5<EMA20, MACD柱<0, 量能满足",
            )

        if (
            ema_5 < ema_20
            and macd_hist < 0
            and close >= bb_upper * 0.995
            and close < ma_20 * 1.01
            and short_volume_ok
        ):
            return self._entry_decision(
                action="SELL_OPEN",
                reason="V5空头: 死叉+MACD向下+接近上轨+趋势确认+量能",
            )

        return self._hold_decision("无入场信号")

    def _exit_decision_if_needed(
        self,
        position: Dict[str, Any],
        current_price: float,
        rsi: float,
    ) -> Dict[str, Any]:
        side = position.get("side")
        entry_price = float(position.get("entry_price", 0))
        if entry_price <= 0:
            return self._hold_decision("持仓价无效")

        if side == "LONG":
            pnl_pct = (current_price - entry_price) / entry_price * 100
            if pnl_pct <= -self.stop_loss_pct or pnl_pct >= self.take_profit_pct:
                return self._close_decision(f"触发止损/止盈 {pnl_pct:.2f}%")
            if rsi > self.rsi_overbought:
                return self._close_decision("RSI超买")

        if side == "SHORT":
            pnl_pct = (entry_price - current_price) / entry_price * 100
            if pnl_pct <= -self.stop_loss_pct or pnl_pct >= self.take_profit_pct:
                return self._close_decision(f"触发止损/止盈 {pnl_pct:.2f}%")
            if rsi < self.rsi_oversold:
                return self._close_decision("RSI超卖")

        return self._hold_decision("持仓中")

    def _entry_decision(self, action: str, reason: str) -> Dict[str, Any]:
        return {
            "action": action,
            "confidence": 0.8,
            "leverage": self.leverage,
            "position_percent": self.position_percent,
            "take_profit_percent": float(self.take_profit_pct),
            "stop_loss_percent": -float(self.stop_loss_pct),
            "reason": reason,
        }

    def _close_decision(self, reason: str) -> Dict[str, Any]:
        return {
            "action": "CLOSE",
            "confidence": 0.8,
            "leverage": self.leverage,
            "position_percent": 0,
            "take_profit_percent": 0.0,
            "stop_loss_percent": 0.0,
            "reason": reason,
        }

    def _hold_decision(self, reason: str) -> Dict[str, Any]:
        return {
            "action": "HOLD",
            "confidence": float(self.hold_confidence),
            "leverage": self.leverage,
            "position_percent": 0,
            "take_profit_percent": 0.0,
            "stop_loss_percent": 0.0,
            "reason": reason,
        }

    def _get_15m_dataframe(self, market_data: Dict[str, Any]) -> Optional[pd.DataFrame]:
        multi = market_data.get("multi_timeframe", {})
        data_15m = multi.get("15m", {})
        return data_15m.get("dataframe")

    def _compute_indicators(self, df: pd.DataFrame) -> None:
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gain / loss
        df["rsi"] = 100 - (100 / (1 + rs))

        df["ema_5"] = close.ewm(span=5, adjust=False).mean()
        df["ema_20"] = close.ewm(span=20, adjust=False).mean()
        df["ma_20"] = close.rolling(window=20).mean()

        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        df["macd_hist"] = macd_line - macd_signal

        sma = close.rolling(window=20).mean()
        std = close.rolling(window=20).std()
        df["bb_upper"] = sma + (std * 2)
        df["bb_lower"] = sma - (std * 2)

        df["volume_ma"] = volume.rolling(window=20).mean()
        df["volume_q"] = volume.rolling(window=self.volume_window).quantile(self.volume_quantile)
        df["volume_q_short"] = volume.rolling(window=self.volume_window).quantile(self.short_volume_quantile)

    def _has_nan(self, row: pd.Series) -> bool:
        required = [
            "rsi",
            "ema_5",
            "ema_20",
            "ma_20",
            "macd_hist",
            "bb_lower",
            "bb_upper",
        ]
        return any(pd.isna(row.get(key)) for key in required)

    def _extract_utc_hour(self, ts: Any) -> Optional[int]:
        if ts is None:
            return None
        try:
            if isinstance(ts, pd.Timestamp):
                return ts.tz_convert("UTC").hour if ts.tzinfo else ts.hour
            if isinstance(ts, str):
                ts = ts.strip()
                if not ts:
                    return None
                dt = pd.to_datetime(ts, errors="coerce")
                if pd.isna(dt):
                    return None
                if isinstance(dt, pd.Timestamp):
                    return dt.tz_convert("UTC").hour if dt.tzinfo else dt.hour
            ts_val = float(ts)
            if ts_val > 1e12:
                ts_val = ts_val / 1000
            dt = datetime.utcfromtimestamp(ts_val)
            return dt.hour
        except Exception:
            return None

    def _expand_allowed_hours(self) -> set:
        if isinstance(self.allowed_hours, list) and len(self.allowed_hours) == 2:
            start, end = self.allowed_hours
            return set(range(int(start), int(end) + 1))
        if isinstance(self.allowed_hours, list):
            return set(int(h) for h in self.allowed_hours)
        return set()
