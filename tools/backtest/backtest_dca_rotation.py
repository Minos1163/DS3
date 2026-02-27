# -*- coding: utf-8 -*-
"""
DCA 多币种轮动回测（5m/30d）
- 综合牛熊判断：BTC状态 + 交易对自身趋势动态加权
- 信心度统一：confidence = p_win
- 开仓门槛：根据方向和综合牛熊状态动态调整
"""
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import pandas as pd
import os
import sys

# Windows console UTF-8 fix
if sys.platform == 'win32':
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # type: ignore
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')  # type: ignore
    except Exception:
        pass

# ensure project src is importable when running script directly
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.data.klines_downloader import load_or_download

import json
from dataclasses import dataclass, field
from datetime import datetime


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@dataclass
class DCAParams:
    direction: str = "SHORT"
    leverage: float = 7.0
    initial_margin: float = 2.0
    initial_margin_pct: Optional[float] = None
    add_margin: float = 2.0
    add_step_pct: float = 0.008
    max_positions: int = 3
    take_profit_pct: float = 0.015
    max_dca: int = 2
    max_orders: int = 5
    cooldown_seconds: int = 0
    add_price_multiplier: float = 1.0
    add_amount_multiplier: float = 1.05
    max_hold_days: int = 1
    min_daily_volume_usdt: float = 5.0
    symbol_stop_loss_pct: float = 0.15
    total_stop_loss_pct: float = 0.20
    rsi_entry: float = 65.0
    rsi_entry_short: float = 65.0
    rsi_entry_long: float = 30.0
    td_add_count: int = 9
    score_threshold: float = 0.08
    score_threshold_short: float = 0.10
    score_threshold_long: float = 0.10
    score_exit_multiplier: float = 1.0
    low_confidence_floor: float = 0.20
    candidate_top_n: int = 3
    low_confidence_candidate_slots: int = 1
    high_score_candidate_n: int = 2
    low_score_candidate_n: int = 2
    max_long_positions: int = 2
    max_short_positions: int = 2
    candidate_rank_mode: str = "EDGE"
    max_position_pct: float = 0.30
    max_position_pct_add: float = 0.50
    trend_filter_enabled: bool = True
    trend_ema_fast: int = 20
    trend_ema_slow: int = 50
    bull_short_threshold_mult: float = 1.35
    bear_short_threshold_mult: float = 0.9
    bull_long_threshold_mult: float = 1.0
    bear_long_threshold_mult: float = 1.0
    round_trip_fee_pct: float = 0.0008
    round_trip_slippage_pct: float = 0.0006
    funding_rate_estimate: float = 0.0001
    edge_funding_cycles: float = 3.0
    edge_funding_abs_cost: bool = True
    edge_cost_ref_pct: float = 0.002
    edge_loss_realization: float = 0.45
    dynamic_threshold_a: float = 0.015
    dynamic_threshold_b: float = 0.020
    dynamic_threshold_c: float = 0.010
    dynamic_threshold_band: float = 0.08
    dynamic_threshold_vol_ref: float = 0.03
    dynamic_threshold_vol_scale: float = 0.015
    dynamic_threshold_trend_ref: float = 0.004
    short_edge_min: float = 0.0
    # 综合牛熊判断参数
    combined_regime_enabled: bool = True
    combined_regime_btc_weight: float = 0.55
    symbol_regime_enabled: bool = True
    # p_win 阈值参数
    min_p_win_threshold: float = 0.50
    min_p_win_short: float = 0.48
    min_p_win_long: float = 0.48
    bull_min_p_win_short: float = 0.58
    bear_min_p_win_long: float = 0.58
    bull_short_close_mult: float = 0.65
    bear_long_close_mult: float = 0.65


class DCARotationBacktester:

    def __init__(self, symbols: List[str], interval: str = "5m", days: int = 30, initial_capital: float = 100.0,
                 params: Optional[DCAParams] = None, enable_15m_filters: bool = True,
                 fee_pct: float = 0.0, slippage_pct: float = 0.0,
                 bar_log_enabled: bool = False):
        self.symbols = symbols
        self.interval = interval
        self.days = days
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.peak_equity = initial_capital
        self.params = params or DCAParams()
        self.bar_minutes = self._interval_minutes(interval)

        self.data: Dict[str, pd.DataFrame] = {}
        self.mtf_15: Dict[str, pd.DataFrame] = {}
        self.enable_15m_filters = enable_15m_filters
        self.fee_pct = float(fee_pct or 0.0)
        self.slippage_pct = float(slippage_pct or 0.0)
        self.filter_min_15m_vol_ratio = 20.0
        self.filter_min_price_change_pct = 0.8
        self.positions: Dict[str, Dict] = {}
        self.trades: List[Dict] = []
        self.candidate_logs: List[Dict] = []
        self.last_entry_time: Optional[pd.Timestamp] = None
        self.halt_trading = False
        self.last_equity: float = initial_capital
        self.data_start: Optional[pd.Timestamp] = None
        self.data_end: Optional[pd.Timestamp] = None
        self.total_bars: int = 0
        self.bar_log_enabled: bool = bool(bar_log_enabled)
        self.bar_logs: List[Dict] = []
        # BTC 数据用于综合牛熊判断
        self.btc_data: Optional[pd.DataFrame] = None
        # 缓存每个时间戳的综合牛熊状态
        self.regime_cache: Dict[pd.Timestamp, Tuple[str, float, Dict]] = {}

    def _load_csv(self, symbol: str) -> Optional[pd.DataFrame]:
        candidates = [
            f"{symbol}USDT",
            symbol,
        ]
        for sym in candidates:
            try:
                df, path = load_or_download(sym, self.interval, self.days)
                if df is not None and len(df) > 0:
                    return df
            except Exception as e:
                print(f"[WARN] {sym} 数据下载失败: {e}")
                continue
        print(f"[WARN] 跳过交易对: {symbol} (无法获取数据)")
        return None

    @staticmethod
    def _interval_minutes(interval: str) -> int:
        if interval.endswith("m") and interval[:-1].isdigit():
            return int(interval[:-1])
        return 5

    def load_data(self) -> None:
        self.data_start = None
        self.data_end = None
        self.total_bars = 0
        loaded_symbols = []
        
        for symbol in self.symbols:
            df = self._load_csv(symbol)
            if df is None or len(df) == 0:
                continue
            ind = self.calculate_indicators(df)
            self.data[symbol] = ind
            loaded_symbols.append(symbol)
            try:
                self.mtf_15[symbol] = self._compute_15m_metrics(ind)
            except Exception:
                self.mtf_15[symbol] = pd.DataFrame()
            self.total_bars += len(df)
            start = df.index.min()
            end = df.index.max()
            if self.data_start is None or start < self.data_start:
                self.data_start = start
            if self.data_end is None or end > self.data_end:
                self.data_end = end
        
        # 加载 BTC 数据用于牛熊判断
        if bool(getattr(self.params, "combined_regime_enabled", True)):
            try:
                btc_df, _ = load_or_download("BTCUSDT", self.interval, self.days)
                if btc_df is not None and len(btc_df) > 0:
                    self.btc_data = self.calculate_indicators(btc_df)
                    print(f"[OK] BTC数据加载成功: {len(self.btc_data)} 根K线")
            except Exception as e:
                print(f"[WARN] BTC数据加载失败: {e}")
        
        if not self.data:
            raise RuntimeError("没有可用的数据，无法回测")
        
        print(f"[OK] 成功加载 {len(loaded_symbols)} 个交易对: {loaded_symbols}")

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # RSI(14)
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        df["rsi"] = 100 - (100 / (1 + rs))

        # Bollinger Bands(20)
        df["bb_middle"] = df["close"].rolling(window=20).mean()
        bb_std = df["close"].rolling(window=20).std()
        df["bb_upper"] = df["bb_middle"] + (bb_std * 2)
        df["bb_lower"] = df["bb_middle"] - (bb_std * 2)

        # Volume quantile (60)
        df["volume_quantile"] = df["volume"].rolling(window=60).apply(
            lambda x: float(np.mean(x <= x[-1])), raw=True
        )

        # 24h quote volume
        df["quote_volume"] = df["volume"] * df["close"]
        bars_24h = int(24 * 60 / self.bar_minutes)
        df["quote_volume_24h"] = df["quote_volume"].rolling(window=bars_24h).sum()

        # TD Sequential (up count)
        close = df["close"]
        cond_up = close > close.shift(4)
        td_up = []
        count = 0
        for val in cond_up.fillna(False):
            if val:
                count += 1
            else:
                count = 0
            td_up.append(count)
        df["td_up"] = td_up

        # TD Sequential (down count)
        cond_down = close < close.shift(4)
        td_down = []
        count = 0
        for val in cond_down.fillna(False):
            if val:
                count += 1
            else:
                count = 0
            td_down.append(count)
        df["td_down"] = td_down

        # momentum (5 bars)
        df["momentum_5"] = close.pct_change(5)
        # 24h realized volatility
        ret_1 = close.pct_change()
        df["volatility_24h"] = ret_1.rolling(window=max(20, bars_24h)).std() * (bars_24h ** 0.5)

        # trend regime EMA
        df["ema_trend_fast"] = close.ewm(span=self.params.trend_ema_fast, adjust=False).mean()
        df["ema_trend_slow"] = close.ewm(span=self.params.trend_ema_slow, adjust=False).mean()
        df["ema_fast_20"] = close.ewm(span=20, adjust=False).mean()
        df["ema_slow_50"] = close.ewm(span=50, adjust=False).mean()
        return df

    def _compute_15m_metrics(self, df: pd.DataFrame) -> pd.DataFrame:
        """从5m数据聚合出15m的 volume_ratio 和 change_15m 指标。"""
        d = df.copy()
        if not isinstance(d.index, pd.DatetimeIndex):
            if 'timestamp' in d.columns:
                d = d.set_index(pd.to_datetime(d['timestamp']))
            else:
                d = d.set_index(pd.to_datetime(d.index))

        close_15 = d['close'].resample('15T').last()
        vol_15 = d['volume'].resample('15T').sum()

        df15 = pd.DataFrame({'close': close_15, 'vol_15': vol_15})
        df15['change_15m'] = df15['close'].pct_change() * 100.0
        df15['vol_median'] = df15['vol_15'].rolling(8, min_periods=1).median().replace(0, 1)
        df15['volume_ratio'] = df15['vol_15'] / df15['vol_median'] * 100.0
        df15 = df15.dropna()
        return df15

    def _detect_btc_regime(self, row: pd.Series, timestamp: pd.Timestamp) -> Tuple[str, float, Dict[str, Any]]:
        """
        检测 BTC 市场状态（使用当前 K 线数据）
        """
        if self.btc_data is None or timestamp not in self.btc_data.index:
            return "NEUTRAL", 0.0, {}
        
        btc_row = self._row_at_timestamp(self.btc_data, timestamp)
        close = btc_row.get("close")
        ema_fast = btc_row.get("ema_fast_20")
        ema_slow = btc_row.get("ema_slow_50")
        
        if pd.isna(close) or pd.isna(ema_fast) or pd.isna(ema_slow):
            return "NEUTRAL", 0.0, {}
        
        # 计算趋势分数
        if close > ema_fast > ema_slow:
            score = 1.0
        elif close < ema_fast < ema_slow:
            score = -1.0
        elif close > ema_slow:
            score = 0.3
        elif close < ema_slow:
            score = -0.3
        else:
            score = 0.0
        
        if score >= 0.35:
            regime = "BULL"
        elif score <= -0.35:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
        
        return regime, score, {"close": close, "ema_fast": ema_fast, "ema_slow": ema_slow}

    def _detect_symbol_regime(self, symbol: str, row: pd.Series, timestamp: pd.Timestamp) -> Tuple[str, float, Dict[str, Any]]:
        """
        检测单个交易对自身趋势状态
        """
        if not bool(getattr(self.params, "symbol_regime_enabled", True)):
            return "NEUTRAL", 0.0, {}
        
        close = row.get("close")
        ema_fast = row.get("ema_fast_20")
        ema_slow = row.get("ema_slow_50")
        
        if pd.isna(close) or pd.isna(ema_fast) or pd.isna(ema_slow):
            return "NEUTRAL", 0.0, {}
        
        # 计算趋势分数
        if close > ema_fast > ema_slow:
            score = 1.0
        elif close < ema_fast < ema_slow:
            score = -1.0
        elif close > ema_slow:
            score = 0.3
        elif close < ema_slow:
            score = -0.3
        else:
            score = 0.0
        
        if score >= 0.35:
            regime = "BULL"
        elif score <= -0.35:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
        
        return regime, score, {"close": close, "ema_fast": ema_fast, "ema_slow": ema_slow}

    def _get_combined_regime(
        self, symbol: str, row: pd.Series, timestamp: pd.Timestamp
    ) -> Tuple[str, float, Dict[str, Any]]:
        """
        综合判断交易对的牛熊状态：BTC 市场状态 + 交易对自身状态动态加权。
        """
        # 获取 BTC 状态
        btc_regime, btc_score, btc_details = self._detect_btc_regime(row, timestamp)
        
        # 获取交易对自身状态
        symbol_regime, symbol_score, symbol_details = self._detect_symbol_regime(symbol, row, timestamp)
        
        # 获取权重配置
        btc_weight = float(getattr(self.params, "combined_regime_btc_weight", 0.55) or 0.55)
        symbol_weight = 1.0 - btc_weight
        
        # 动态权重调整
        direction_match = (btc_score * symbol_score) > 0
        
        if direction_match and abs(btc_score) > 0.2 and abs(symbol_score) > 0.2:
            btc_weight = min(0.8, btc_weight + 0.15)
            symbol_weight = 1.0 - btc_weight
        elif not direction_match and abs(symbol_score) > abs(btc_score):
            btc_weight = max(0.3, btc_weight - 0.2)
            symbol_weight = 1.0 - btc_weight
        
        # 计算综合分数
        combined_score = btc_score * btc_weight + symbol_score * symbol_weight
        
        # 判断综合牛熊
        if combined_score >= 0.35:
            combined_regime = "BULL"
        elif combined_score <= -0.35:
            combined_regime = "BEAR"
        else:
            combined_regime = "NEUTRAL"
        
        details = {
            "btc_regime": btc_regime,
            "btc_score": btc_score,
            "btc_weight": btc_weight,
            "symbol_regime": symbol_regime,
            "symbol_score": symbol_score,
            "symbol_weight": symbol_weight,
            "direction_match": direction_match,
        }
        
        return combined_regime, combined_score, details

    def _detect_market_regime(self, row: pd.Series) -> str:
        """保留旧方法以兼容"""
        if not self.params.trend_filter_enabled:
            return "NEUTRAL"
        close = row.get("close")
        ema_fast = row.get("ema_trend_fast")
        ema_slow = row.get("ema_trend_slow")
        if pd.isna(close) or pd.isna(ema_fast) or pd.isna(ema_slow):
            return "NEUTRAL"
        if close > ema_fast > ema_slow:
            return "BULL"
        if close < ema_fast < ema_slow:
            return "BEAR"
        return "NEUTRAL"

    def _apply_regime_threshold(self, base_threshold: float, regime: str, side: str = "SHORT") -> float:
        side_up = (side or "").upper()
        if side_up == "LONG":
            if regime == "BULL":
                return base_threshold * self.params.bull_long_threshold_mult
            if regime == "BEAR":
                return base_threshold * self.params.bear_long_threshold_mult
            return base_threshold
        if regime == "BULL":
            return base_threshold * self.params.bull_short_threshold_mult
        if regime == "BEAR":
            return base_threshold * self.params.bear_short_threshold_mult
        return base_threshold

    @staticmethod
    def _ratio(v: float) -> float:
        x = float(v)
        return x / 100.0 if x > 1.0 else x

    @staticmethod
    def _clamp_value(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _estimate_costs(self, side: str = "SHORT") -> Tuple[float, float, float, float, float]:
        fee_cost = float(getattr(self.params, "round_trip_fee_pct", 0.0008) or 0.0008)
        slippage_cost = float(getattr(self.params, "round_trip_slippage_pct", 0.0006) or 0.0006)
        funding_rate = float(getattr(self.params, "funding_rate_estimate", 0.0001) or 0.0001)
        funding_cycles = float(getattr(self.params, "edge_funding_cycles", 3.0) or 3.0)
        funding_cycles = self._clamp_value(funding_cycles, 0.5, 12.0)

        if bool(getattr(self.params, "edge_funding_abs_cost", True)):
            funding_cost = abs(funding_rate) * funding_cycles
        else:
            side_up = str(side or "SHORT").upper()
            if side_up == "SHORT":
                funding_cost = max(0.0, -funding_rate) * funding_cycles
            else:
                funding_cost = max(0.0, funding_rate) * funding_cycles

        total_cost = max(0.0, fee_cost + slippage_cost + funding_cost)
        cost_ref = float(getattr(self.params, "edge_cost_ref_pct", 0.002) or 0.002)
        cost_ref = max(cost_ref, 1e-6)
        cost_z = self._clamp_value((total_cost - cost_ref) / cost_ref, -3.0, 3.0)
        return fee_cost, funding_cost, slippage_cost, total_cost, cost_z

    def _dynamic_threshold(
        self,
        base_threshold: float,
        regime: str,
        side: str,
        row: pd.Series,
        cost_z: float,
    ) -> Tuple[float, float, float]:
        base = self._clamp_value(float(base_threshold), 0.01, 0.95)
        volatility = float(row.get("volatility_24h", 0.0) or 0.0)
        vol_ref = max(1e-6, float(getattr(self.params, "dynamic_threshold_vol_ref", 0.03) or 0.03))
        vol_scale = max(1e-6, float(getattr(self.params, "dynamic_threshold_vol_scale", 0.015) or 0.015))
        volatility_z = self._clamp_value((volatility - vol_ref) / vol_scale, -3.0, 3.0)

        ema_fast = float(row.get("ema_fast_20", row.get("close", 0.0)) or 0.0)
        ema_slow = float(row.get("ema_slow_50", row.get("close", 0.0)) or 0.0)
        trend_raw = (ema_fast - ema_slow) / max(abs(ema_slow), 1e-9)
        trend_ref = max(1e-6, float(getattr(self.params, "dynamic_threshold_trend_ref", 0.004) or 0.004))
        side_sign = 1.0 if str(side or "SHORT").upper() == "SHORT" else -1.0
        trend_component = side_sign * trend_raw / trend_ref
        regime_bias = 1.0 if regime == "BULL" else -1.0 if regime == "BEAR" else 0.0
        if side_sign < 0:
            regime_bias = -regime_bias
        trend_z = self._clamp_value(0.7 * trend_component + 0.3 * regime_bias, -3.0, 3.0)

        coef_a = float(getattr(self.params, "dynamic_threshold_a", 0.015) or 0.015)
        coef_b = float(getattr(self.params, "dynamic_threshold_b", 0.020) or 0.020)
        coef_c = float(getattr(self.params, "dynamic_threshold_c", 0.010) or 0.010)
        threshold = base + coef_a * volatility_z + coef_b * trend_z + coef_c * cost_z
        band = max(0.0, float(getattr(self.params, "dynamic_threshold_band", 0.08) or 0.08))
        threshold = self._clamp_value(threshold, max(0.01, base - band), min(0.95, base + band))
        threshold = self._clamp_value(threshold, 0.01, 0.95)
        return threshold, volatility_z, trend_z

    def _expected_edge(
        self,
        score: float,
        threshold: float,
        trend_z: float,
        cost_z: float,
        fee_cost: float,
        funding_cost: float,
        slippage_cost: float,
    ) -> Tuple[float, float, float, float]:
        """计算期望收益和 p_win"""
        threshold_safe = max(1e-6, min(0.99, float(threshold)))
        score_excess = (float(score) - threshold_safe) / max(1e-6, (1.0 - threshold_safe))
        # 【统一】p_win 公式
        p_win = 0.5 + 0.35 * np.tanh(score_excess * 2.0)
        p_win = p_win - 0.06 * max(0.0, trend_z) - 0.05 * max(0.0, cost_z) + 0.03 * max(0.0, -trend_z)
        p_win = self._clamp_value(float(p_win), 0.05, 0.95)

        take_profit_pct = abs(float(getattr(self.params, "take_profit_pct", 0.02) or 0.02))
        stop_loss_pct = abs(float(getattr(self.params, "symbol_stop_loss_pct", 0.15) or 0.15))
        loss_realization = self._clamp_value(float(getattr(self.params, "edge_loss_realization", 0.45) or 0.45), 0.15, 1.0)

        avg_win = take_profit_pct * (1.0 + 0.5 * max(0.0, float(score) - threshold_safe))
        avg_loss = (stop_loss_pct * loss_realization) * (1.0 + 0.35 * max(0.0, trend_z) + 0.25 * max(0.0, cost_z))
        avg_win = self._clamp_value(avg_win, take_profit_pct * 0.6, take_profit_pct * 1.8)
        avg_loss = self._clamp_value(avg_loss, stop_loss_pct * 0.2, stop_loss_pct * 1.2)

        edge = p_win * avg_win - (1.0 - p_win) * avg_loss - fee_cost - funding_cost - slippage_cost
        return float(edge), p_win, avg_win, avg_loss

    def score_symbol_pair(self, row: pd.Series) -> Tuple[float, float]:
        if pd.isna(row.get("rsi")) or pd.isna(row.get("bb_upper")):
            return 0.0, 0.0

        rsi = row["rsi"]
        close = row["close"]
        bb_upper = row["bb_upper"]
        bb_lower = row["bb_lower"]
        vq = row.get("volume_quantile", 0)
        momentum = row.get("momentum_5", 0)

        rsi_short = float(getattr(self.params, "rsi_entry_short", self.params.rsi_entry))
        rsi_long = float(getattr(self.params, "rsi_entry_long", 100.0 - rsi_short))

        rsi_score_s = max(0.0, min(1.0, (rsi - rsi_short) / max(1e-9, (100 - rsi_short))))
        bb_score_s = max(0.0, min(1.0, (close - bb_upper) / max(1e-9, (bb_upper * 0.02))))
        momentum_score_s = max(0.0, min(1.0, momentum / 0.01))

        rsi_score_l = max(0.0, min(1.0, (rsi_long - rsi) / max(1.0, rsi_long)))
        bb_score_l = max(0.0, min(1.0, (bb_lower - close) / max(1e-9, (bb_lower * 0.02))))
        momentum_score_l = max(0.0, min(1.0, (-momentum) / 0.01))

        volume_score = max(0.0, min(1.0, vq if pd.notna(vq) else 0.0))
        short_score = 0.4 * rsi_score_s + 0.2 * bb_score_s + 0.2 * momentum_score_s + 0.2 * volume_score
        long_score = 0.4 * rsi_score_l + 0.2 * bb_score_l + 0.2 * momentum_score_l + 0.2 * volume_score
        return short_score, long_score

    def score_symbol(self, row: pd.Series, side: str = "SHORT") -> float:
        short_score, long_score = self.score_symbol_pair(row)
        return short_score if str(side).upper() == "SHORT" else long_score

    def _equity(self, snapshot: Dict[str, float]) -> float:
        return self.cash + sum(snapshot.values())

    def _available_cash(self) -> float:
        used = sum(pos["margin_total"] for pos in self.positions.values())
        return max(0.0, self.cash - used)

    def _build_snapshot_pnl(self, timestamp: pd.Timestamp) -> Dict[str, float]:
        snapshot = {}
        for symbol, pos in self.positions.items():
            df = self.data[symbol]
            if timestamp not in df.index:
                continue
            close = self._row_at_timestamp(df, timestamp)["close"]
            pos_dir = str(pos.get("direction", self.params.direction)).upper()
            if pos_dir == "LONG":
                pnl = (close - pos["avg_price"]) * pos["size"]
            else:
                pnl = (pos["avg_price"] - close) * pos["size"]
            snapshot[symbol] = pos["margin_total"] + pnl
        return snapshot

    def _row_at_timestamp(self, df: pd.DataFrame, timestamp: pd.Timestamp) -> pd.Series:
        ts = pd.to_datetime(timestamp)
        row = df.loc[ts]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        return row

    def _can_open_new(self, timestamp: pd.Timestamp) -> bool:
        if self.halt_trading:
            return False
        if len(self.positions) >= self.params.max_positions:
            return False
        if self.last_entry_time is None:
            return True
        delta = (timestamp - self.last_entry_time).total_seconds()
        return delta >= self.params.cooldown_seconds

    def _append_bar_log(
        self,
        timestamp: pd.Timestamp,
        symbol: str,
        row: pd.Series,
        action: str,
        reason: str = "",
        extra: Optional[Dict[str, float]] = None,
    ) -> None:
        if not self.bar_log_enabled:
            return
        pos = self.positions.get(symbol) or {}
        side = str(pos.get("direction", "")).upper()
        short_score, long_score = self.score_symbol_pair(row)
        rec = {
            "timestamp": pd.to_datetime(timestamp),
            "symbol": symbol,
            "action": action,
            "reason": reason,
            "price": float(row.get("close", 0.0) or 0.0),
            "rsi": float(row.get("rsi", 0.0) or 0.0),
            "short_score": float(short_score),
            "long_score": float(long_score),
            "position_side": side,
            "position_size": float(pos.get("size", 0.0) or 0.0),
            "position_margin": float(pos.get("margin_total", 0.0) or 0.0),
            "equity": float(self.last_equity),
            "cash": float(self.cash),
            "peak_equity": float(self.peak_equity),
            "drawdown_pct": float((self.peak_equity - self.last_equity) / self.peak_equity * 100.0) if self.peak_equity > 0 else 0.0,
        }
        if extra:
            rec.update(extra)
        self.bar_logs.append(rec)

    def _get_min_p_win(self, side: str, combined_regime: str) -> float:
        """根据方向和综合牛熊状态获取最小 p_win 阈值"""
        min_p_win_default = float(getattr(self.params, "min_p_win_threshold", 0.50) or 0.50)
        min_p_win_short = float(getattr(self.params, "min_p_win_short", min_p_win_default) or min_p_win_default)
        min_p_win_long = float(getattr(self.params, "min_p_win_long", min_p_win_default) or min_p_win_default)
        
        if side.upper() == "SHORT":
            min_conf = min_p_win_short
            if combined_regime == "BULL":
                min_conf = float(getattr(self.params, "bull_min_p_win_short", min_conf * 1.2) or min_conf * 1.2)
        else:  # LONG
            min_conf = min_p_win_long
            if combined_regime == "BEAR":
                min_conf = float(getattr(self.params, "bear_min_p_win_long", min_conf * 1.2) or min_conf * 1.2)
        
        return min_conf

    def _open_position(self, symbol: str, timestamp: pd.Timestamp, price: float, direction_override: Optional[str] = None) -> bool:
        equity_scale = max(0.5, self.last_equity / self.initial_capital)
        val = getattr(self.params, "initial_margin_pct", None)
        if val is not None:
            margin = float(val) * max(self.last_equity, self.initial_capital)
        else:
            margin = self.params.initial_margin * equity_scale
        if self._available_cash() < margin:
            return False
        pos_dir = (direction_override or self.params.direction or "").upper()
        if pos_dir == "LONG":
            exec_price = price * (1.0 + self.slippage_pct)
        else:
            exec_price = price * (1.0 - self.slippage_pct)
        size = margin * self.params.leverage / exec_price
        max_position_value = self.last_equity * self._ratio(self.params.max_position_pct)
        if size * price > max_position_value:
            size = max_position_value / price
            if size <= 0:
                return False
        self.positions[symbol] = {
            "entry_time": timestamp,
            "last_dca_time": timestamp,
            "avg_price": exec_price,
            "size": size,
            "margin_total": margin,
            "dca_count": 0,
            "last_dca_price": price,
            "direction": (direction_override or self.params.direction),
        }
        fee = size * exec_price * self.fee_pct
        self.cash -= (margin + fee)
        self.last_entry_time = timestamp
        return True

    def _add_dca(self, symbol: str, timestamp: pd.Timestamp, price: float) -> bool:
        pos = self.positions[symbol]
        if pos["dca_count"] >= self.params.max_dca:
            return False
        equity_scale = max(0.5, self.last_equity / self.initial_capital)
        add_margin = self.params.add_margin * equity_scale * (self.params.add_amount_multiplier ** pos["dca_count"])
        if self._available_cash() < add_margin:
            return False
        row = self._row_at_timestamp(self.data[symbol], timestamp)
        pos_dir = pos.get("direction", self.params.direction)
        score = self.score_symbol(row, pos_dir)
        
        # 【统一】获取综合牛熊状态
        combined_regime, combined_score, _ = self._get_combined_regime(symbol, row, timestamp)
        
        if str(pos_dir).upper() == "LONG":
            base_threshold = float(getattr(self.params, "score_threshold_long", self.params.score_threshold))
        else:
            base_threshold = float(getattr(self.params, "score_threshold_short", self.params.score_threshold))
        base_threshold = base_threshold if base_threshold > 0 else 1.0
        threshold_regime = self._apply_regime_threshold(base_threshold, combined_regime, pos_dir)
        _fee_c, _fund_c, _slip_c, _total_c, cost_z = self._estimate_costs(pos_dir)
        threshold, _vol_z, _trend_z = self._dynamic_threshold(
            base_threshold=threshold_regime,
            regime=combined_regime,
            side=str(pos_dir or "SHORT"),
            row=row,
            cost_z=cost_z,
        )
        
        # 【统一】使用 p_win 计算信心度
        threshold_safe = max(1e-6, min(0.99, float(threshold)))
        score_excess = (float(score) - threshold_safe) / max(1e-6, (1.0 - threshold_safe))
        confidence = 0.5 + 0.35 * np.tanh(score_excess * 2.0)
        confidence = self._clamp_value(float(confidence), 0.05, 0.95)
        
        size_factor = max(0.3, min(1.0, confidence * 1.5))
        add_margin = add_margin * size_factor
        
        pos_dir = (pos.get("direction", self.params.direction) or "").upper()
        if pos_dir == "LONG":
            exec_price = price * (1.0 + self.slippage_pct)
        else:
            exec_price = price * (1.0 - self.slippage_pct)
        size = add_margin * self.params.leverage / exec_price
        max_position_value = self.last_equity * self._ratio(self.params.max_position_pct_add)
        current_value = pos["size"] * price
        if current_value + size * price > max_position_value:
            size = max(0.0, max_position_value - current_value) / price
            if size <= 0:
                return False
        new_size = pos["size"] + size
        pos["avg_price"] = (pos["avg_price"] * pos["size"] + exec_price * size) / new_size
        pos["size"] = new_size
        pos["margin_total"] += add_margin
        pos["dca_count"] += 1
        pos["last_dca_price"] = price
        pos["last_dca_time"] = timestamp
        fee = size * exec_price * self.fee_pct
        self.cash -= (add_margin + fee)
        return True

    def _close_position(self, symbol: str, timestamp: pd.Timestamp, price: float, reason: str) -> bool:
        pos = self.positions[symbol]
        pos_dir = pos.get("direction", self.params.direction)
        if pos_dir == "LONG":
            exec_price = price * (1.0 - self.slippage_pct)
        else:
            exec_price = price * (1.0 + self.slippage_pct)
        if pos_dir == "SHORT" or pos_dir == "BOTH":
            pnl = (pos["avg_price"] - exec_price) * pos["size"]
        else:
            pnl = (exec_price - pos["avg_price"]) * pos["size"]
        fee = pos["size"] * exec_price * self.fee_pct
        self.cash += pos["margin_total"] + pnl - fee
        trade = {
            "symbol": symbol,
            "entry_time": pos["entry_time"],
            "exit_time": timestamp,
            "direction": pos.get("direction", self.params.direction),
            "entry_price": pos["avg_price"],
            "exit_price": exec_price,
            "size": pos["size"],
            "pnl": pnl,
            "pnl_pct": pnl / pos["margin_total"] * 100 if pos["margin_total"] > 0 else 0.0,
            "dca_count": pos["dca_count"],
            "reason": reason,
        }
        self.trades.append(trade)
        del self.positions[symbol]
        return True

    def _maybe_exit_or_add(self, symbol: str, row: pd.Series, timestamp: pd.Timestamp) -> Tuple[str, str]:
        pos = self.positions[symbol]
        price = row["close"]
        pos_dir = pos.get("direction", self.params.direction)
        if pos_dir == "SHORT" or pos_dir == "BOTH":
            pnl_pct = (pos["avg_price"] - price) / pos["avg_price"]
        else:
            pnl_pct = (price - pos["avg_price"]) / pos["avg_price"]
        hold_minutes = (timestamp - pos["entry_time"]).total_seconds() / 60
        max_hold_minutes = self.params.max_hold_days * 24 * 60

        # 【统一】使用综合牛熊状态调整平仓阈值
        combined_regime, combined_score, _ = self._get_combined_regime(symbol, row, timestamp)
        close_mult = 1.0
        if combined_regime == "BULL" and pos_dir == "SHORT":
            close_mult = float(getattr(self.params, "bull_short_close_mult", 0.65) or 0.65)
        elif combined_regime == "BEAR" and pos_dir == "LONG":
            close_mult = float(getattr(self.params, "bear_long_close_mult", 0.65) or 0.65)
        
        adjusted_tp = self.params.take_profit_pct * close_mult

        if pnl_pct >= adjusted_tp:
            self._close_position(symbol, timestamp, price, "TAKE_PROFIT")
            return "CLOSE", "TAKE_PROFIT"

        if pnl_pct <= -self.params.symbol_stop_loss_pct:
            self._close_position(symbol, timestamp, price, "STOP_LOSS")
            return "CLOSE", "STOP_LOSS"

        if hold_minutes >= max_hold_minutes:
            self._close_position(symbol, timestamp, price, "TIME_STOP")
            return "CLOSE", "TIME_STOP"

        td_up = row.get("td_up", 0)
        td_down = row.get("td_down", 0)
        trigger_up = pos["last_dca_price"] * (1 + self.params.add_step_pct * self.params.add_price_multiplier)
        trigger_down = pos["last_dca_price"] * (1 - self.params.add_step_pct * self.params.add_price_multiplier)
        if str(pos_dir).upper() == "LONG":
            if td_down >= self.params.td_add_count and price <= trigger_down:
                if self._add_dca(symbol, timestamp, price):
                    return "ADD", "DCA_ADD_LONG"
        else:
            if td_up >= self.params.td_add_count and price >= trigger_up:
                if self._add_dca(symbol, timestamp, price):
                    return "ADD", "DCA_ADD_SHORT"
        return "HOLD", ""

    def run_backtest(self) -> None:
        self.load_data()
        all_index = pd.DatetimeIndex([])
        for df in self.data.values():
            all_index = all_index.union(pd.DatetimeIndex(df.index))
        all_times = all_index.sort_values()

        for timestamp in all_times:
            bar_actions: Dict[str, Dict[str, Any]] = {}
            for symbol in list(self.positions.keys()):
                df = self.data[symbol]
                if timestamp not in df.index:
                    continue
                row = self._row_at_timestamp(df, timestamp)
                action, reason = self._maybe_exit_or_add(symbol, row, timestamp)
                bar_actions[symbol] = {"action": action, "reason": reason}

            snapshot = self._build_snapshot_pnl(timestamp)
            equity = self._equity(snapshot)
            self.peak_equity = max(self.peak_equity, equity)
            self.last_equity = equity
            drawdown = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0
            if drawdown >= self.params.total_stop_loss_pct:
                for symbol in list(self.positions.keys()):
                    df = self.data[symbol]
                    if timestamp not in df.index:
                        continue
                    price = self._row_at_timestamp(df, timestamp)["close"]
                    if self._close_position(symbol, timestamp, price, "TOTAL_STOP"):
                        bar_actions[symbol] = {"action": "CLOSE", "reason": "TOTAL_STOP"}
                self.halt_trading = True

            if self._can_open_new(timestamp):
                scored_pool: List[Dict[str, Any]] = []
                direction_cfg = str(self.params.direction).upper()
                rsi_entry_short = float(getattr(self.params, "rsi_entry_short", self.params.rsi_entry))
                rsi_entry_long = float(getattr(self.params, "rsi_entry_long", 100.0 - rsi_entry_short))
                score_threshold_short = float(getattr(self.params, "score_threshold_short", self.params.score_threshold))
                score_threshold_long = float(getattr(self.params, "score_threshold_long", self.params.score_threshold))
                short_edge_min = float(getattr(self.params, "short_edge_min", 0.0) or 0.0)
                rank_mode = str(getattr(self.params, "candidate_rank_mode", "LINEAR") or "LINEAR").upper()
                
                for symbol, df in self.data.items():
                    if symbol in self.positions:
                        continue
                    if timestamp not in df.index:
                        continue
                    row = self._row_at_timestamp(df, timestamp)
                    qv24 = float(row.get("quote_volume_24h", 0) or 0)
                    if qv24 < self.params.min_daily_volume_usdt:
                        continue

                    # 【统一】获取综合牛熊状态
                    combined_regime, combined_score, combined_details = self._get_combined_regime(symbol, row, timestamp)
                    
                    short_score, long_score = self.score_symbol_pair(row)
                    rsi_val = float(row.get("rsi", 0) or 0)
                    
                    # 根据综合牛熊状态调整阈值
                    threshold_short_regime = self._apply_regime_threshold(score_threshold_short, combined_regime, "SHORT")
                    threshold_long_regime = self._apply_regime_threshold(score_threshold_long, combined_regime, "LONG")
                    
                    edge_s = 0.0
                    p_win_s = 0.0
                    threshold_short_dyn = float(threshold_short_regime)
                    edge_l = 0.0
                    p_win_l = 0.0
                    threshold_long_dyn = float(threshold_long_regime)

                    if direction_cfg in ("SHORT", "BOTH"):
                        fee_s, funding_s, slippage_s, _cost_s, cost_z_s = self._estimate_costs("SHORT")
                        threshold_short_dyn, _vol_z_s, trend_z_s = self._dynamic_threshold(
                            base_threshold=threshold_short_regime,
                            regime=combined_regime,
                            side="SHORT",
                            row=row,
                            cost_z=cost_z_s,
                        )
                        edge_s, p_win_s, _avg_win_s, _avg_loss_s = self._expected_edge(
                            score=short_score,
                            threshold=threshold_short_dyn,
                            trend_z=trend_z_s,
                            cost_z=cost_z_s,
                            fee_cost=fee_s,
                            funding_cost=funding_s,
                            slippage_cost=slippage_s,
                        )

                    if direction_cfg in ("LONG", "BOTH"):
                        fee_l, funding_l, slippage_l, _cost_l, cost_z_l = self._estimate_costs("LONG")
                        threshold_long_dyn, _vol_z_l, trend_z_l = self._dynamic_threshold(
                            base_threshold=threshold_long_regime,
                            regime=combined_regime,
                            side="LONG",
                            row=row,
                            cost_z=cost_z_l,
                        )
                        edge_l, p_win_l, _avg_win_l, _avg_loss_l = self._expected_edge(
                            score=long_score,
                            threshold=threshold_long_dyn,
                            trend_z=trend_z_l,
                            cost_z=cost_z_l,
                            fee_cost=fee_l,
                            funding_cost=funding_l,
                            slippage_cost=slippage_l,
                        )
                    
                    # 【统一】使用 p_win 作为信心度
                    # confidence = p_win
                    scored_pool.append(
                        {
                            "symbol": symbol,
                            "long_score": float(long_score),
                            "short_score": float(short_score),
                            "price": float(row.get("close", 0) or 0),
                            "quote_vol_24h": float(qv24),
                            "edge_long": float(edge_l),
                            "edge_short": float(edge_s),
                            "threshold_long": float(threshold_long_dyn),
                            "threshold_short": float(threshold_short_dyn),
                            "p_win_long": float(p_win_l),
                            "p_win_short": float(p_win_s),
                            "combined_regime": combined_regime,
                            "combined_score": combined_score,
                        }
                    )

                if scored_pool:
                    high_pick_n = max(0, int(getattr(self.params, "high_score_candidate_n", 2) or 2))
                    low_pick_n = max(0, int(getattr(self.params, "low_score_candidate_n", 2) or 2))
                    top_n = max(1, int(getattr(self.params, "candidate_top_n", 3) or 3))

                    selected_sorted: List[Tuple[str, str, float, float, float, float, float, float, str]] = []
                    
                    # EDGE 模式：按 edge 排序
                    if rank_mode == "EDGE":
                        # 多单候选：按 edge_long 排序
                        ranked_long = sorted(scored_pool, key=lambda x: (x["edge_long"], x["quote_vol_24h"]), reverse=True)
                        # 空单候选：按 edge_short 排序
                        ranked_short = sorted(scored_pool, key=lambda x: (x["edge_short"], x["quote_vol_24h"]), reverse=True)
                        
                        selected_high = ranked_long[:high_pick_n] if direction_cfg in ("LONG", "BOTH") else []
                        selected_high_syms = {it["symbol"] for it in selected_high}
                        selected_short: List[Dict[str, Any]] = []
                        if direction_cfg in ("SHORT", "BOTH"):
                            for it in ranked_short:
                                if it["symbol"] in selected_high_syms:
                                    continue
                                if float(it["edge_short"]) <= short_edge_min:
                                    continue
                                selected_short.append(it)
                                if len(selected_short) >= low_pick_n:
                                    break

                        for it in selected_high:
                            selected_sorted.append(
                                (
                                    str(it["symbol"]),
                                    "LONG",
                                    float(it["long_score"]),
                                    float(it["p_win_long"]),  # confidence = p_win
                                    float(it["quote_vol_24h"]),
                                    float(it["edge_long"]),
                                    float(it["threshold_long"]),
                                    float(it["p_win_long"]),
                                    str(it["combined_regime"]),
                                )
                            )
                        for it in selected_short:
                            selected_sorted.append(
                                (
                                    str(it["symbol"]),
                                    "SHORT",
                                    float(it["short_score"]),
                                    float(it["p_win_short"]),  # confidence = p_win
                                    float(it["quote_vol_24h"]),
                                    float(it["edge_short"]),
                                    float(it["threshold_short"]),
                                    float(it["p_win_short"]),
                                    str(it["combined_regime"]),
                                )
                            )
                    else:
                        # LINEAR 模式
                        ranked_desc = sorted(scored_pool, key=lambda x: (x["long_score"], x["quote_vol_24h"]), reverse=True)
                        ranked_asc = sorted(scored_pool, key=lambda x: (x["long_score"], -x["quote_vol_24h"]))
                        selected_high = ranked_desc[:high_pick_n] if direction_cfg in ("LONG", "BOTH") else []
                        selected_high_syms = {it["symbol"] for it in selected_high}
                        selected_low: List[Dict[str, Any]] = []
                        if direction_cfg in ("SHORT", "BOTH"):
                            for it in ranked_asc:
                                if it["symbol"] in selected_high_syms:
                                    continue
                                selected_low.append(it)
                                if len(selected_low) >= low_pick_n:
                                    break

                        for it in selected_high:
                            selected_sorted.append(
                                (
                                    str(it["symbol"]),
                                    "LONG",
                                    float(it["long_score"]),
                                    float(it["p_win_long"]),
                                    float(it["quote_vol_24h"]),
                                    float(it["edge_long"]),
                                    float(it["threshold_long"]),
                                    float(it["p_win_long"]),
                                    str(it["combined_regime"]),
                                )
                            )
                        for it in selected_low:
                            selected_sorted.append(
                                (
                                    str(it["symbol"]),
                                    "SHORT",
                                    float(it["long_score"]),
                                    float(it["p_win_short"]),
                                    float(it["quote_vol_24h"]),
                                    float(it["edge_short"]),
                                    float(it["threshold_short"]),
                                    float(it["p_win_short"]),
                                    str(it["combined_regime"]),
                                )
                            )
                    
                    selected_sorted = selected_sorted[:top_n]
                    selected_sorted = sorted(selected_sorted, key=lambda x: x[3], reverse=True)

                    try:
                        for s_sym, s_side, s_score, s_pwin, _s_qv, s_edge, s_threshold, s_conf, s_regime in selected_sorted:
                            if len(self.positions) >= self.params.max_positions:
                                break
                            if s_sym in self.positions:
                                continue
                            
                            side_up = str(s_side).upper()
                            
                            # 【统一】使用 p_win 阈值判断
                            min_p_win = self._get_min_p_win(s_side, s_regime)
                            
                            if float(s_pwin) < min_p_win:
                                bar_actions[s_sym] = {
                                    "action": "SKIP",
                                    "reason": f"LOW_PWIN ({s_pwin:.2%} < {min_p_win:.2%})",
                                    "extra": {
                                        "p_win": float(s_pwin),
                                        "min_p_win": float(min_p_win),
                                        "regime": s_regime,
                                    },
                                }
                                continue
                            
                            if float(s_edge) <= 0:
                                bar_actions[s_sym] = {
                                    "action": "SKIP",
                                    "reason": f"NEGATIVE_EDGE ({s_edge:.4f})",
                                    "extra": {
                                        "edge": float(s_edge),
                                        "p_win": float(s_pwin),
                                    },
                                }
                                continue
                            
                            # 持仓方向上限
                            long_count = sum(1 for p in self.positions.values() if str(p.get("direction", "")).upper() == "LONG")
                            short_count = sum(1 for p in self.positions.values() if str(p.get("direction", "")).upper() in ("SHORT", "BOTH"))
                            max_long = max(0, int(getattr(self.params, "max_long_positions", max(1, int(self.params.max_positions // 2))) or 0))
                            max_short = max(0, int(getattr(self.params, "max_short_positions", self.params.max_positions) or 0))
                            if side_up == "LONG" and long_count >= max_long:
                                bar_actions[s_sym] = {"action": "SKIP", "reason": "MAX_LONG_REACHED"}
                                continue
                            if side_up == "SHORT" and short_count >= max_short:
                                bar_actions[s_sym] = {"action": "SKIP", "reason": "MAX_SHORT_REACHED"}
                                continue
                            
                            df_sel = self.data[s_sym]
                            row_sel = self._row_at_timestamp(df_sel, timestamp)
                            opened = self._open_position(
                                s_sym,
                                timestamp,
                                float(row_sel["close"]),
                                direction_override=s_side,
                            )
                            if opened:
                                bar_actions[s_sym] = {
                                    "action": "OPEN",
                                    "reason": f"OPEN_{str(s_side).upper()}",
                                    "extra": {
                                        "p_win": float(s_pwin),
                                        "edge": float(s_edge),
                                        "threshold": float(s_threshold),
                                        "regime": s_regime,
                                    },
                                }
                            self.candidate_logs.append({
                                "timestamp": pd.to_datetime(timestamp),
                                "symbol": s_sym,
                                "side": s_side,
                                "selected": True,
                                "opened": bool(opened),
                                "p_win": float(s_pwin),
                                "edge": float(s_edge),
                                "threshold": float(s_threshold),
                                "regime": s_regime,
                            })
                    except Exception as e:
                        print(f"[WARN] 开仓处理异常: {e}")

            for symbol, df in self.data.items():
                if timestamp not in df.index:
                    continue
                row = self._row_at_timestamp(df, timestamp)
                event = bar_actions.get(symbol, {})
                action = str(event.get("action", "HOLD"))
                reason = str(event.get("reason", ""))
                extra = event.get("extra") if isinstance(event.get("extra"), dict) else None
                self._append_bar_log(
                    timestamp=timestamp,
                    symbol=symbol,
                    row=row,
                    action=action,
                    reason=reason,
                    extra=extra,
                )

    def summarize(self) -> str:
        if not self.trades:
            return "无交易记录"
        df = pd.DataFrame(self.trades)
        _total_pnl = df["pnl"].sum()
        final_equity = self.initial_capital + _total_pnl
        total_return = (final_equity - self.initial_capital) / self.initial_capital * 100

        df["equity"] = self.initial_capital + df["pnl"].cumsum()
        df["peak"] = df["equity"].cummax()
        df["dd"] = (df["peak"] - df["equity"]) / df["peak"]
        max_dd = df["dd"].max() * 100

        wins = len(df[df["pnl"] > 0])
        win_rate = wins / len(df) * 100

        summary = [
            f"初始资金: {self.initial_capital:.2f} USDT",
            f"最终资金: {final_equity:.2f} USDT",
            f"总收益: {_total_pnl:+.2f} USDT ({total_return:+.2f}%)",
            f"最大回撤: {max_dd:.2f}%",
            f"总交易: {len(df)} | 胜率: {win_rate:.2f}%",
        ]
        return "\n".join(summary)

    def metrics(self) -> Dict[str, float]:
        if not self.trades:
            return {
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "trades_per_day": 0.0,
            }
        df = pd.DataFrame(self.trades)
        _total_pnl = df["pnl"].sum()
        final_equity = self.initial_capital + _total_pnl
        total_return = (final_equity - self.initial_capital) / self.initial_capital * 100
        df["equity"] = self.initial_capital + df["pnl"].cumsum()
        df["peak"] = df["equity"].cummax()
        df["dd"] = (df["peak"] - df["equity"]) / df["peak"]
        max_dd = df["dd"].max() * 100
        wins = len(df[df["pnl"] > 0])
        win_rate = wins / len(df) * 100
        trades_per_day = len(df) / max(1, self.days)
        return {
            "total_return_pct": total_return,
            "max_drawdown_pct": max_dd,
            "total_trades": float(len(df)),
            "win_rate_pct": win_rate,
            "trades_per_day": trades_per_day,
        }

    def save_results(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs("logs", exist_ok=True)
        trades_file = f"logs/dca_rotation_trades_{timestamp}.csv"
        summary_file = f"logs/dca_rotation_summary_{timestamp}.txt"
        if self.trades:
            df = pd.DataFrame(self.trades)
            df.to_csv(trades_file, index=False)
        else:
            print("[WARN] 无交易记录，交易明细为空")
        if getattr(self, "candidate_logs", None):
            try:
                cand_df = pd.DataFrame(self.candidate_logs)
                cand_file = f"logs/dca_candidate_logs_{timestamp}.csv"
                cand_df.to_csv(cand_file, index=False)
                print(f"[OK] 候选记录: {cand_file}")
            except Exception:
                pass
        if self.bar_log_enabled and getattr(self, "bar_logs", None):
            try:
                bar_df = pd.DataFrame(self.bar_logs)
                bar_file = f"logs/dca_bar_actions_{timestamp}.csv"
                bar_df.to_csv(bar_file, index=False)
                print(f"[OK] K线行为记录: {bar_file}")
            except Exception as exc:
                print(f"[WARN] 保存K线行为记录失败: {exc}")
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write("DCA 多币种轮动回测 (综合牛熊判断 + p_win统一)\n")
            f.write(self.summarize())
            if self.data_start is not None and self.data_end is not None:
                f.write(f"\n数据时间范围: {self.data_start} 至 {self.data_end}\n")
            if self.total_bars:
                f.write(f"K线总根数: {self.total_bars}\n")
        if self.trades:
            print(f"[OK] 交易记录: {trades_file}")
        print(f"[OK] 摘要: {summary_file}")


def load_run_config(config_path: str) -> Tuple[List[str], str, int, float, DCAParams]:
    symbols = ["ETH", "SOL", "BTC", "BNB"]
    interval = "5m"
    days = 30
    initial_capital = 100.0
    params = DCAParams()

    if not os.path.exists(config_path):
        return symbols, interval, days, initial_capital, params

    with open(config_path, "r", encoding="utf-8") as f:
        root_cfg = json.load(f)
    
    # 支持 trading_config_vps.json 格式
    cfg = root_cfg
    if "dca_rotation" in cfg:
        cfg = cfg.get("dca_rotation", {})

    symbols = cfg.get("symbols", symbols)
    # 去掉 USDT 后缀（回测脚本会自动添加）
    symbols = [s.replace("USDT", "") for s in symbols]
    interval = cfg.get("interval", interval)
    days = int(cfg.get("days", days))
    initial_capital = float(cfg.get("initial_capital", initial_capital))

    params_data = cfg.get("params", {})
    allowed_keys = set(DCAParams.__annotations__.keys())
    filtered = {k: v for k, v in params_data.items() if k in allowed_keys}
    params = DCAParams(**{**params.__dict__, **filtered})
    
    return symbols, interval, days, initial_capital, params


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default=os.path.join(PROJECT_ROOT, "config", "trading_config_vps.json"), help='config path')
    parser.add_argument('--fee_pct', type=float, default=0.00075, help='transaction fee fraction')
    parser.add_argument('--slippage_pct', type=float, default=0.0005, help='slippage fraction')
    parser.add_argument('--bar_log', action='store_true', help='enable per-bar action log output')
    parser.add_argument('--grid', action='store_true', help='run grid search optimization')
    parser.add_argument('--workers', type=int, default=1, help='parallel workers for grid search')
    args = parser.parse_args()
    
    config_path = args.config
    symbols, interval, days, initial_capital, base_params = load_run_config(config_path)
    
    if not args.grid:
        # 单次回测
        print(f"[BACKTEST] 回测配置:")
        print(f"   交易对: {len(symbols)} 个")
        print(f"   周期: {interval}, 天数: {days}")
        print(f"   初始资金: {initial_capital} USDT")
        print(f"   杠杆: {base_params.leverage}x")
        print(f"   方向: {base_params.direction}")
        print(f"   综合牛熊: enabled={base_params.combined_regime_enabled}, btc_weight={base_params.combined_regime_btc_weight}")
        print(f"   p_win阈值: short={base_params.min_p_win_short}, long={base_params.min_p_win_long}")
        
        backtester = DCARotationBacktester(
            symbols=symbols,
            interval=interval,
            days=days,
            initial_capital=initial_capital,
            params=base_params,
            fee_pct=args.fee_pct,
            slippage_pct=args.slippage_pct,
            bar_log_enabled=bool(args.bar_log),
        )
        backtester.run_backtest()
        print("\n" + "="*50)
        print(backtester.summarize())
        print("="*50)
        backtester.save_results()
    else:
        # 网格搜索
        run_grid_search(symbols, interval, days, initial_capital, base_params, args)


def run_grid_search(
    symbols: List[str],
    interval: str,
    days: int,
    initial_capital: float,
    base_params: DCAParams,
    args: Any,
) -> None:
    """运行参数网格搜索"""
    from itertools import product
    import time
    
    # 定义参数网格 - 针对盈利200%+、回撤<20%、交易300-600优化
    # 第一阶段粗搜索：约500组参数
    param_grid = {
        # p_win 阈值：较低阈值增加交易频率
        'min_p_win_threshold': [0.35, 0.40, 0.45],
        'min_p_win_short': [0.35, 0.42],
        'min_p_win_long': [0.35, 0.42],
        # 牛熊调整
        'bull_min_p_win_short': [0.50, 0.60],
        'bear_min_p_win_long': [0.50, 0.60],
        # 止盈止损
        'take_profit_pct': [0.015, 0.022],
        'symbol_stop_loss_pct': [0.12, 0.16],
        # 加仓
        'max_dca': [2, 3],
        # 评分阈值
        'score_threshold': [0.06, 0.10],
        'score_threshold_short': [0.08],
        'score_threshold_long': [0.08],
    }
    
    # 计算总组合数
    total_combinations = 1
    for values in param_grid.values():
        total_combinations *= len(values)
    
    print(f"[GRID] 参数网格搜索")
    print(f"   参数维度: {len(param_grid)}")
    print(f"   总组合数: {total_combinations}")
    print(f"   目标: 盈利>200%, 回撤<20%, 交易300-600")
    print(f"   预计耗时: {total_combinations * 2 // 60} 分钟 (估算)")
    print("")
    
    # 生成所有参数组合
    keys = list(param_grid.keys())
    value_lists = [param_grid[k] for k in keys]
    combinations = list(product(*value_lists))
    
    # 结果存储
    results: List[Dict[str, Any]] = []
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("logs", exist_ok=True)
    results_file = f"logs/grid_search_results_{timestamp_str}.csv"
    
    # 写入CSV头
    header = list(keys) + ['total_return_pct', 'max_drawdown_pct', 'total_trades', 'win_rate_pct', 'final_equity', 'passes_filter']
    with open(results_file, 'w', encoding='utf-8', newline='') as f:
        f.write(','.join(header) + '\n')
    
    # 筛选后的最佳结果
    best_results: List[Dict[str, Any]] = []
    
    start_time = time.time()
    
    for i, combo in enumerate(combinations):
        # 创建参数对象
        params_dict = base_params.__dict__.copy()
        for j, key in enumerate(keys):
            params_dict[key] = combo[j]
        params = DCAParams(**params_dict)
        
        # 运行回测
        try:
            backtester = DCARotationBacktester(
                symbols=symbols,
                interval=interval,
                days=days,
                initial_capital=initial_capital,
                params=params,
                fee_pct=args.fee_pct,
                slippage_pct=args.slippage_pct,
                bar_log_enabled=False,
            )
            backtester.run_backtest()
            met = backtester.metrics()
            
            # 检查是否符合筛选条件
            passes = (
                met['total_return_pct'] >= 200.0 and
                met['max_drawdown_pct'] < 20.0 and
                300 <= met['total_trades'] <= 600
            )
            
            result = {
                **{k: combo[j] for j, k in enumerate(keys)},
                'total_return_pct': met['total_return_pct'],
                'max_drawdown_pct': met['max_drawdown_pct'],
                'total_trades': met['total_trades'],
                'win_rate_pct': met['win_rate_pct'],
                'final_equity': initial_capital + backtester.trades[-1]['pnl'] if backtester.trades else initial_capital,
                'passes_filter': passes,
            }
            results.append(result)
            
            # 写入CSV
            with open(results_file, 'a', encoding='utf-8', newline='') as f:
                row = [str(result.get(k, '')) for k in header]
                f.write(','.join(row) + '\n')
            
            if passes:
                best_results.append(result)
                print(f"[PASS #{len(best_results)}] 返回={met['total_return_pct']:.1f}%, 回撤={met['max_drawdown_pct']:.1f}%, 交易={met['total_trades']:.0f}")
            
            # 进度显示
            if (i + 1) % 10 == 0 or i == 0:
                elapsed = time.time() - start_time
                eta = elapsed / (i + 1) * (total_combinations - i - 1)
                print(f"[{i+1}/{total_combinations}] 返回={met['total_return_pct']:.1f}%, 回撤={met['max_drawdown_pct']:.1f}%, 交易={met['total_trades']:.0f} | ETA: {eta/60:.1f}分钟")
                
        except Exception as e:
            print(f"[ERROR] 组合 {i+1} 失败: {e}")
            continue
    
    # 输出最佳结果
    print("\n" + "="*60)
    print(f"[GRID SEARCH COMPLETE] 总组合: {len(results)}, 通过筛选: {len(best_results)}")
    print(f"结果文件: {results_file}")
    print("="*60)
    
    if best_results:
        # 按收益排序
        best_results.sort(key=lambda x: x['total_return_pct'], reverse=True)
        print("\n[TOP 10 符合条件的结果]")
        print("-"*60)
        for i, r in enumerate(best_results[:10]):
            print(f"#{i+1} 返回={r['total_return_pct']:.1f}%, 回撤={r['max_drawdown_pct']:.1f}%, 交易={r['total_trades']:.0f}, 胜率={r['win_rate_pct']:.1f}%")
            # 打印关键参数
            param_str = ", ".join([f"{k}={r[k]}" for k in keys[:5]])
            print(f"    参数: {param_str}")
        
        # 保存最佳参数配置
        best = best_results[0]
        best_config = {
            "grid_search_best": {
                "total_return_pct": best['total_return_pct'],
                "max_drawdown_pct": best['max_drawdown_pct'],
                "total_trades": best['total_trades'],
                "win_rate_pct": best['win_rate_pct'],
                "params": {k: best[k] for k in keys},
            }
        }
        best_file = f"logs/grid_search_best_{timestamp_str}.json"
        with open(best_file, 'w', encoding='utf-8') as f:
            json.dump(best_config, f, indent=2, ensure_ascii=False)
        print(f"\n最佳参数已保存: {best_file}")
    else:
        print("\n[WARN] 没有找到符合条件的结果")
        # 显示最接近的结果
        if results:
            results.sort(key=lambda x: (x['total_return_pct'], -x['max_drawdown_pct']), reverse=True)
            print("\n[TOP 10 最接近的结果]")
            for i, r in enumerate(results[:10]):
                print(f"#{i+1} 返回={r['total_return_pct']:.1f}%, 回撤={r['max_drawdown_pct']:.1f}%, 交易={r['total_trades']:.0f}")


if __name__ == "__main__":
    main()
