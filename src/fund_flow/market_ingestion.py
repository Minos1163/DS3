from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Deque, Dict, Iterable, List, Optional


@dataclass
class MarketFlowSnapshot:
    exchange: str
    symbol: str
    timestamp: datetime
    cvd_ratio: float
    cvd_momentum: float
    oi_delta_ratio: float
    funding_rate: float
    depth_ratio: float
    imbalance: float
    signal_strength: float
    liquidity_delta_norm: float = 0.0
    timeframes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 资金流 3.0: 分离微结构特征和资金流特征
    microstructure_features: Dict[str, float] = field(default_factory=dict)
    fund_flow_features: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "cvd_ratio": self.cvd_ratio,
            "cvd_momentum": self.cvd_momentum,
            "oi_delta_ratio": self.oi_delta_ratio,
            "funding_rate": self.funding_rate,
            "depth_ratio": self.depth_ratio,
            "imbalance": self.imbalance,
            "signal_strength": self.signal_strength,
            "liquidity_delta_norm": self.liquidity_delta_norm,
            "timeframes": self.timeframes or {},
            "microstructure_features": self.microstructure_features or {},
            "fund_flow_features": self.fund_flow_features or {},
        }


class MarketIngestionService:
    """
    15s 聚合窗口的最小实现：
    - 输入原始 market flow 指标
    - 输出标准化快照
    
    资金流 3.0 增强:
    - L2: 特征工程层分离
      - microstructure_features_15s: spread_z, book_imb, trade_imb, microprice_delta, phantom_score, trap_score
      - fund_flow_features: CVD(真实成交驱动), OI_delta(方向分离), Funding, Liquidity_delta_norm
    - 多周期聚合器: 15s -> 1m/5m/15m rolling/ema/zscore
    """

    TIMEFRAME_SECONDS: Dict[str, int] = {
        "1m": 60,
        "3m": 3 * 60,
        "5m": 5 * 60,
        "15m": 15 * 60,
        "30m": 30 * 60,
        "1h": 60 * 60,
        "2h": 2 * 60 * 60,
        "4h": 4 * 60 * 60,
    }

    def __init__(
        self,
        window_seconds: int = 15,
        exchange: str = "binance",
        timeframes: Optional[Iterable[str]] = None,
        max_history_seconds: int = 4 * 60 * 60,
        range_quantile_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.window_seconds = max(1, int(window_seconds))
        self.exchange = exchange
        self.max_history_seconds = max(300, int(max_history_seconds or 4 * 60 * 60))
        requested = list(timeframes or self.TIMEFRAME_SECONDS.keys())
        self.timeframe_seconds: Dict[str, int] = {}
        for tf in requested:
            key = str(tf).strip().lower()
            sec = self.TIMEFRAME_SECONDS.get(key)
            if sec:
                self.timeframe_seconds[key] = sec
        if not self.timeframe_seconds:
            self.timeframe_seconds = dict(self.TIMEFRAME_SECONDS)
        self._history: Dict[str, Deque[Dict[str, Any]]] = {}
        self.range_quantile_cfg = self._normalize_range_quantile_config(range_quantile_config)

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _calc_signal_strength(self, metrics: Dict[str, float]) -> float:
        signal_strength = (
            abs(self._to_float(metrics.get("cvd_ratio"), 0.0)) * 0.25
            + abs(self._to_float(metrics.get("cvd_momentum"), 0.0)) * 0.15
            + abs(self._to_float(metrics.get("oi_delta_ratio"), 0.0)) * 0.25
            + abs(self._to_float(metrics.get("funding_rate"), 0.0)) * 0.10
            + abs(self._to_float(metrics.get("depth_ratio"), 1.0) - 1.0) * 0.10
            + abs(self._to_float(metrics.get("imbalance"), 0.0)) * 0.15
            + abs(self._to_float(metrics.get("liquidity_delta_norm"), 0.0)) * 0.10
        )
        return min(max(signal_strength, 0.0), 1.0)

    def _normalize_metrics(self, metrics: Dict[str, Any]) -> Dict[str, float]:
        return {
            "cvd_ratio": self._to_float(metrics.get("cvd_ratio"), 0.0),
            "cvd_momentum": self._to_float(metrics.get("cvd_momentum"), 0.0),
            "oi_delta_ratio": self._to_float(metrics.get("oi_delta_ratio"), 0.0),
            "funding_rate": self._to_float(metrics.get("funding_rate"), 0.0),
            "depth_ratio": self._to_float(metrics.get("depth_ratio"), 1.0),
            "imbalance": self._to_float(metrics.get("imbalance"), 0.0),
            "liquidity_delta_norm": self._to_float(metrics.get("liquidity_delta_norm"), 0.0),
            "mid_price": self._to_float(metrics.get("mid_price"), 0.0),
            "microprice": self._to_float(metrics.get("microprice"), 0.0),
            "micro_delta_norm": self._to_float(metrics.get("micro_delta_norm"), 0.0),
            "spread_bps": self._to_float(metrics.get("spread_bps"), 0.0),
            "spread_z": self._to_float(metrics.get("spread_z"), 0.0),
            "trade_imbalance": self._to_float(metrics.get("trade_imbalance"), 0.0),
            "phantom": self._to_float(metrics.get("phantom"), 0.0),
            "trap_score": self._to_float(metrics.get("trap_score"), 0.0),
            "ret_period": self._to_float(metrics.get("ret_period"), 0.0),
            "ret_15m": self._to_float(metrics.get("ret_15m"), 0.0),
            "mid_return": self._to_float(metrics.get("mid_return"), 0.0),
        }
    
    def _extract_microstructure_features(self, metrics: Dict[str, Any]) -> Dict[str, float]:
        """
        提取 15s 微结构特征
        
        L2: 特征工程层 - 微结构特征
        - spread_z: 买卖价差 z-score (滚动标准化)
        - spread_bps: 原始价差 bps
        - book_imb: 订单簿不平衡
        - trade_imb: 成交不平衡 (买卖成交量比例)
        - microprice_delta: 微价格变化
        - phantom_score: 幽灵订单评分
        - trap_score: 陷阱评分
        """
        spread_bps = self._to_float(metrics.get("spread_bps"), 0.0)
        imbalance = self._to_float(metrics.get("imbalance"), 0.0)
        micro_delta = self._to_float(metrics.get("micro_delta_norm"), 0.0)
        phantom = self._to_float(metrics.get("phantom"), 0.0)
        trap = self._to_float(metrics.get("trap_score"), 0.0)
        # trade_imb: 真正的成交不平衡，不是 CVD
        trade_imb = self._to_float(metrics.get("trade_imbalance"), 0.0)
        
        # spread_z: 从历史统计计算真正的 z-score
        spread_z = self._to_float(metrics.get("spread_z"), 0.0)
        if spread_z == 0.0 and spread_bps > 0:
            # 如果上游没有提供 spread_z，使用简化计算（需在上游改进）
            # 这里用 spread_bps 相对于合理基准的近似
            spread_z = spread_bps / 10.0  # 兜底逻辑，应从上游传入
        
        return {
            "spread_z": spread_z,
            "spread_bps": spread_bps,
            "book_imb": imbalance,
            "trade_imb": trade_imb,  # 真正的成交不平衡
            "microprice_delta": micro_delta,
            "phantom_score": phantom,
            "trap_score": trap,
        }
    
    def _extract_fund_flow_features(self, metrics: Dict[str, Any]) -> Dict[str, float]:
        """
        提取真实资金流特征
        
        L2: 特征工程层 - 资金流特征
        
        内部统一字段名（与 WeightRouter/DecisionEngine 对齐）:
        - cvd: 真实成交驱动 (Cumulative Volume Delta)
        - cvd_momentum: CVD 动量
        - oi_delta: OI 变化率 (统一字段)
        - oi_delta_long: 多头 OI 变化 (基于价格方向)
        - oi_delta_short: 空头 OI 变化 (基于价格方向)
        - funding: 资金费率
        - liquidity_delta: 流动性变化归一化
        - depth_ratio: 深度比率
        - ret_period: 周期收益率 (用于 OI 方向分离)
        - flow_confirm: 资金一致性 (CVD/OI/价格方向是否一致)
        """
        cvd = self._to_float(metrics.get("cvd_ratio"), 0.0)
        cvd_mom = self._to_float(metrics.get("cvd_momentum"), 0.0)
        oi_delta = self._to_float(metrics.get("oi_delta_ratio"), 0.0)
        funding = self._to_float(metrics.get("funding_rate"), 0.0)
        liq_delta = self._to_float(metrics.get("liquidity_delta_norm"), 0.0)
        depth = self._to_float(metrics.get("depth_ratio"), 1.0)
        
        # 获取价格收益率用于 OI 方向分离（更可靠）
        ret_period = self._to_float(metrics.get("ret_period"), 
                                    self._to_float(metrics.get("ret_15m"), 
                                    self._to_float(metrics.get("mid_return"), 0.0)))
        
        # OI 方向分离：基于价格方向（不是 CVD 方向）
        # 原因：CVD 正不等于价格上涨，尤其在流动性薄、假单/对敲时会错
        if ret_period > 0:
            # 价格上涨：OI 增加为多头增仓，OI 减少为空头平仓
            oi_delta_long = max(0.0, oi_delta) if oi_delta > 0 else 0.0
            oi_delta_short = max(0.0, -oi_delta) if oi_delta < 0 else 0.0
        elif ret_period < 0:
            # 价格下跌：OI 增加为空头增仓，OI 减少为多头平仓
            oi_delta_long = max(0.0, -oi_delta) if oi_delta < 0 else 0.0
            oi_delta_short = max(0.0, oi_delta) if oi_delta > 0 else 0.0
        else:
            oi_delta_long = 0.0
            oi_delta_short = 0.0
        
        # 资金一致性：CVD、OI、价格方向是否一致
        cvd_sign = 1 if cvd > 0 else (-1 if cvd < 0 else 0)
        oi_sign = 1 if oi_delta > 0 else (-1 if oi_delta < 0 else 0)
        ret_sign = 1 if ret_period > 0 else (-1 if ret_period < 0 else 0)
        # 三者一致时 flow_confirm = 1，部分一致 = 0.5，不一致 = 0
        if cvd_sign == oi_sign == ret_sign and cvd_sign != 0:
            flow_confirm = 1.0
        elif cvd_sign == ret_sign or oi_sign == ret_sign:
            flow_confirm = 0.5
        else:
            flow_confirm = 0.0
        
        return {
            # === 内部统一字段（给 WeightRouter/DecisionEngine 用）===
            "cvd": cvd,
            "cvd_momentum": cvd_mom,
            "oi_delta": oi_delta,  # 统一为 oi_delta
            "funding": funding,
            "depth_ratio": depth,
            "liquidity_delta": liq_delta,  # 统一为 liquidity_delta
            # === 方向分离字段（保留用于分析）===
            "oi_delta_long": oi_delta_long,
            "oi_delta_short": oi_delta_short,
            "ret_period": ret_period,
            # === 资金一致性（新增）===
            "flow_confirm": flow_confirm,
        }

    def _normalize_range_quantile_config(self, cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        raw = cfg if isinstance(cfg, dict) else {}
        timeframe = str(raw.get("timeframe", "5m") or "5m").strip().lower()
        if timeframe not in self.timeframe_seconds:
            timeframe = "5m"
        lookback_minutes = max(5, int(self._to_float(raw.get("lookback_minutes"), 60)))
        min_samples = max(6, int(self._to_float(raw.get("min_samples"), 12)))
        q_hi = self._to_float(raw.get("q_hi"), 0.90)
        q_lo = self._to_float(raw.get("q_lo"), 0.10)
        q_hi = min(0.99, max(0.50, q_hi))
        q_lo = min(0.49, max(0.01, q_lo))
        metrics_raw = raw.get("metrics")
        metrics_list = metrics_raw if isinstance(metrics_raw, list) else ["imbalance", "cvd_momentum"]
        metrics: List[str] = []
        allowed = {
            "imbalance",
            "cvd_momentum",
            "oi_delta_ratio",
            "depth_ratio",
            "micro_delta_last",
            "micro_delta_mean",
            "phantom_mean",
            "phantom_max",
            "trap_last",
            "trap_mean",
            "spread_bps_last",
            "spread_bps_mean",
        }
        for m in metrics_list:
            key = str(m).strip()
            if key in allowed and key not in metrics:
                metrics.append(key)
        if "imbalance" not in metrics:
            metrics.append("imbalance")
        if "cvd_momentum" not in metrics:
            metrics.append("cvd_momentum")
        winsor_clip = max(0.0, self._to_float(raw.get("winsor_clip"), 3.0))
        turn_confirm_raw = raw.get("turn_confirm")
        turn_raw: Dict[str, Any] = turn_confirm_raw if isinstance(turn_confirm_raw, dict) else {}
        turn_mode = str(turn_raw.get("mode", "2bar_peak_valley") or "2bar_peak_valley").strip().lower()
        if turn_mode not in ("1bar", "2bar_peak_valley"):
            turn_mode = "2bar_peak_valley"
        turn_min_delta = max(0.0, self._to_float(turn_raw.get("min_delta"), 0.0))
        trap_guard_raw = raw.get("trap_guard")
        trap_guard_cfg = trap_guard_raw if isinstance(trap_guard_raw, dict) else {}
        trap_guard_q = self._to_float(trap_guard_cfg.get("max_quantile"), 0.70)
        trap_guard_q = min(0.95, max(0.50, trap_guard_q))
        return {
            "enabled": bool(raw.get("enabled", True)),
            "timeframe": timeframe,
            "lookback_minutes": lookback_minutes,
            "min_samples": min_samples,
            "q_hi": q_hi,
            "q_lo": q_lo,
            "metrics": metrics,
            "winsor_clip": winsor_clip,
            "turn_confirm": {
                "enabled": bool(turn_raw.get("enabled", True)),
                "mode": turn_mode,
                "min_delta": turn_min_delta,
                "micro_turn_enabled": bool(turn_raw.get("micro_turn_enabled", True)),
                "phantom_decay_enabled": bool(turn_raw.get("phantom_decay_enabled", True)),
                "trap_decay_enabled": bool(turn_raw.get("trap_decay_enabled", True)),
                "min_pass_count": max(1, int(self._to_float(turn_raw.get("min_pass_count"), 2))),
            },
            "trap_guard": {
                "enabled": bool(trap_guard_cfg.get("enabled", True)),
                "max_quantile": trap_guard_q,
            },
        }

    def _normalize_trend_filter_15m(self, metrics: Dict[str, Any]) -> Dict[str, float]:
        raw = metrics.get("trend_filter_15m")
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, float] = {}
        for key in ("ema_fast", "ema_slow", "adx", "atr_pct"):
            if raw.get(key) is None:
                continue
            out[key] = self._to_float(raw.get(key), 0.0)
        return out

    def _upsert_history_entry(self, symbol: str, ts: datetime, normalized: Dict[str, float]) -> None:
        symbol_up = symbol.upper()
        if symbol_up not in self._history:
            self._history[symbol_up] = deque()
        q = self._history[symbol_up]
        if q and isinstance(q[-1], dict) and q[-1].get("timestamp") == ts:
            q[-1] = {"timestamp": ts, **normalized}
        else:
            q.append({"timestamp": ts, **normalized})
        cutoff = ts.timestamp() - float(self.max_history_seconds)
        while q and float(q[0]["timestamp"].timestamp()) < cutoff:
            q.popleft()

    def _window_entries(self, symbol: str, ts: datetime, window_seconds: int) -> List[Dict[str, Any]]:
        q = self._history.get(symbol.upper()) or deque()
        cutoff = ts.timestamp() - float(window_seconds)
        return [item for item in q if float(item["timestamp"].timestamp()) >= cutoff]

    def _aggregate_series_by_timeframe(
        self,
        symbol: str,
        ts: datetime,
        timeframe: str,
        lookback_seconds: int,
    ) -> List[Dict[str, Any]]:
        tf = str(timeframe or "").strip().lower()
        tf_seconds = int(self.timeframe_seconds.get(tf, 300))
        q = self._history.get(symbol.upper()) or deque()
        cutoff = ts.timestamp() - float(max(1, lookback_seconds))
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for item in q:
            item_ts = float(item["timestamp"].timestamp())
            if item_ts < cutoff:
                continue
            bucket = int(item_ts // tf_seconds) * tf_seconds
            grouped.setdefault(bucket, []).append(item)
        out: List[Dict[str, Any]] = []
        for bucket in sorted(grouped.keys()):
            agg = self._aggregate_window(grouped[bucket])
            agg["bucket_ts"] = float(bucket)
            out.append(agg)
        return out

    @staticmethod
    def _median(values: List[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        mid = n // 2
        if n % 2 == 1:
            return float(s[mid])
        return float((s[mid - 1] + s[mid]) / 2.0)

    def _winsorize(self, values: List[float], clip: float) -> List[float]:
        if clip <= 0 or len(values) < 5:
            return list(values)
        med = self._median(values)
        devs = [abs(v - med) for v in values]
        mad = self._median(devs)
        if mad <= 0:
            return list(values)
        robust_sigma = 1.4826 * mad
        lower = med - clip * robust_sigma
        upper = med + clip * robust_sigma
        return [min(max(v, lower), upper) for v in values]

    @staticmethod
    def _percentile(values: List[float], q: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return float(values[0])
        qv = min(1.0, max(0.0, float(q)))
        s = sorted(values)
        n = len(s)
        pos = qv * (n - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return float(s[lo])
        frac = pos - lo
        return float(s[lo] * (1.0 - frac) + s[hi] * frac)

    def _attach_range_quantiles(self, symbol: str, ts: datetime, timeframes: Dict[str, Dict[str, Any]]) -> None:
        cfg = self.range_quantile_cfg if isinstance(self.range_quantile_cfg, dict) else {}
        if not bool(cfg.get("enabled", False)):
            return
        tf = str(cfg.get("timeframe", "5m"))
        tf_ctx = timeframes.get(tf)
        if not isinstance(tf_ctx, dict):
            return

        tf_seconds = int(self.timeframe_seconds.get(tf, 300))
        lookback_minutes = int(cfg.get("lookback_minutes", 60) or 60)
        min_samples = int(cfg.get("min_samples", 12) or 12)
        lookback_seconds = max(
            lookback_minutes * 60,
            min_samples * tf_seconds,
        )
        series = self._aggregate_series_by_timeframe(
            symbol=symbol,
            ts=ts,
            timeframe=tf,
            lookback_seconds=lookback_seconds,
        )
        quantiles: Dict[str, Any] = {
            "ready": False,
            "reason": "",
            "n": len(series),
            "timeframe": tf,
            "lookback_minutes": int(lookback_seconds // 60),
            "q_hi": self._to_float(cfg.get("q_hi"), 0.90),
            "q_lo": self._to_float(cfg.get("q_lo"), 0.10),
            "values": {},
            "trap_guard": cfg.get("trap_guard") if isinstance(cfg.get("trap_guard"), dict) else {},
        }

        if len(series) >= 2:
            prev = series[-2]
            tf_ctx["prev"] = {
                "bucket_ts": self._to_float(prev.get("bucket_ts"), 0.0),
                "cvd_momentum": self._to_float(prev.get("cvd_momentum"), 0.0),
                "imbalance": self._to_float(prev.get("imbalance"), 0.0),
                "micro_delta_last": self._to_float(prev.get("micro_delta_last"), 0.0),
                "phantom_mean": self._to_float(prev.get("phantom_mean"), 0.0),
                "trap_last": self._to_float(prev.get("trap_last"), 0.0),
            }
        if len(series) >= 3:
            prev2 = series[-3]
            tf_ctx["prev2"] = {
                "bucket_ts": self._to_float(prev2.get("bucket_ts"), 0.0),
                "cvd_momentum": self._to_float(prev2.get("cvd_momentum"), 0.0),
                "imbalance": self._to_float(prev2.get("imbalance"), 0.0),
                "micro_delta_last": self._to_float(prev2.get("micro_delta_last"), 0.0),
                "phantom_mean": self._to_float(prev2.get("phantom_mean"), 0.0),
                "trap_last": self._to_float(prev2.get("trap_last"), 0.0),
            }

        if len(series) < min_samples:
            quantiles["reason"] = "insufficient_samples"
            tf_ctx["quantiles"] = quantiles
            return

        metrics_raw = cfg.get("metrics")
        metrics = metrics_raw if isinstance(metrics_raw, list) else ["imbalance", "cvd_momentum"]
        winsor_clip = self._to_float(cfg.get("winsor_clip"), 3.0)
        q_hi = self._to_float(cfg.get("q_hi"), 0.90)
        q_lo = self._to_float(cfg.get("q_lo"), 0.10)
        trap_guard_raw = cfg.get("trap_guard")
        trap_guard_cfg: Dict[str, Any] = trap_guard_raw if isinstance(trap_guard_raw, dict) else {}
        q_guard = self._to_float(trap_guard_cfg.get("max_quantile"), 0.70)
        q_guard = min(0.95, max(0.50, q_guard))

        for m in metrics:
            arr = [self._to_float(x.get(str(m)), 0.0) for x in series if x.get(str(m)) is not None]
            arr = self._winsorize(arr, winsor_clip)
            if len(arr) < min_samples:
                quantiles["reason"] = f"insufficient_{m}"
                tf_ctx["quantiles"] = quantiles
                return
            quantiles["values"][str(m)] = {
                "lo": self._percentile(arr, q_lo),
                "hi": self._percentile(arr, q_hi),
                "guard": self._percentile(arr, q_guard),
            }

        quantiles["ready"] = True
        quantiles["reason"] = "ok"
        tf_ctx["quantiles"] = quantiles

    def _aggregate_window(self, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not entries:
            base = {
                "cvd_ratio": 0.0,
                "cvd_momentum": 0.0,
                "oi_delta_ratio": 0.0,
                "funding_rate": 0.0,
                "depth_ratio": 1.0,
                "imbalance": 0.0,
                "liquidity_delta_norm": 0.0,
                "mid_price": 0.0,
                "microprice": 0.0,
                "micro_delta_norm": 0.0,
                "spread_bps": 0.0,
                "phantom": 0.0,
                "trap_score": 0.0,
                "micro_delta_mean": 0.0,
                "micro_delta_last": 0.0,
                "phantom_mean": 0.0,
                "phantom_max": 0.0,
                "trap_mean": 0.0,
                "trap_last": 0.0,
                "spread_bps_mean": 0.0,
                "spread_bps_last": 0.0,
            }
            base["signal_strength"] = self._calc_signal_strength(base)
            return base

        n = float(len(entries))
        cvd_sum = sum(self._to_float(e.get("cvd_ratio"), 0.0) for e in entries)
        oi_avg = sum(self._to_float(e.get("oi_delta_ratio"), 0.0) for e in entries) / n
        depth_avg = sum(self._to_float(e.get("depth_ratio"), 1.0) for e in entries) / n
        imbalance_avg = sum(self._to_float(e.get("imbalance"), 0.0) for e in entries) / n
        liq_avg = sum(self._to_float(e.get("liquidity_delta_norm"), 0.0) for e in entries) / n
        mid_avg = sum(self._to_float(e.get("mid_price"), 0.0) for e in entries) / n
        microprice_avg = sum(self._to_float(e.get("microprice"), 0.0) for e in entries) / n
        micro_delta_mean = sum(self._to_float(e.get("micro_delta_norm"), 0.0) for e in entries) / n
        spread_bps_mean = sum(self._to_float(e.get("spread_bps"), 0.0) for e in entries) / n
        phantom_values = [self._to_float(e.get("phantom"), 0.0) for e in entries]
        phantom_mean = sum(phantom_values) / n
        phantom_max = max(phantom_values) if phantom_values else 0.0
        trap_values = [self._to_float(e.get("trap_score"), 0.0) for e in entries]
        trap_mean = (sum(trap_values) / n) if trap_values else 0.0
        first = entries[0]
        last = entries[-1]
        cvd_mom = self._to_float(last.get("cvd_ratio"), 0.0) - self._to_float(first.get("cvd_ratio"), 0.0)
        funding_last = self._to_float(last.get("funding_rate"), 0.0)
        micro_delta_last = self._to_float(last.get("micro_delta_norm"), 0.0)
        spread_bps_last = self._to_float(last.get("spread_bps"), 0.0)
        trap_last = self._to_float(last.get("trap_score"), 0.0)
        mid_last = self._to_float(last.get("mid_price"), 0.0)
        microprice_last = self._to_float(last.get("microprice"), 0.0)

        # 计算 ret_period（窗口内价格变化率）
        mid_first = self._to_float(first.get("mid_price"), 0.0)
        ret_period = 0.0
        if mid_first > 0 and mid_last > 0:
            ret_period = (mid_last - mid_first) / mid_first
        
        out = {
            "cvd_ratio": cvd_sum,
            "cvd_momentum": cvd_mom,
            "oi_delta_ratio": oi_avg,
            "funding_rate": funding_last,
            "depth_ratio": depth_avg,
            "imbalance": imbalance_avg,
            "liquidity_delta_norm": liq_avg,
            "mid_price": mid_last if mid_last > 0 else mid_avg,
            "microprice": microprice_last if microprice_last > 0 else microprice_avg,
            "micro_delta_norm": micro_delta_mean,
            "spread_bps": spread_bps_last,
            "phantom": phantom_mean,
            "trap_score": trap_last,
            "micro_delta_mean": micro_delta_mean,
            "micro_delta_last": micro_delta_last,
            "phantom_mean": phantom_mean,
            "phantom_max": phantom_max,
            "trap_mean": trap_mean,
            "trap_last": trap_last,
            "spread_bps_mean": spread_bps_mean,
            "spread_bps_last": spread_bps_last,
            "ret_period": ret_period,  # 新增：周期收益率
        }
        out["signal_strength"] = self._calc_signal_strength(out)
        return out

    def aggregate_from_metrics(
        self,
        symbol: str,
        metrics: Dict[str, Any],
        ts: Optional[datetime] = None,
    ) -> MarketFlowSnapshot:
        ts = ts or datetime.now(timezone.utc)
        bucket_ts = datetime.fromtimestamp(
            int(ts.timestamp() // self.window_seconds) * self.window_seconds,
            tz=timezone.utc,
        )
        normalized = self._normalize_metrics(metrics or {})
        trend_filter_15m = self._normalize_trend_filter_15m(metrics or {})
        self._upsert_history_entry(symbol, bucket_ts, normalized)

        tf_out: Dict[str, Dict[str, Any]] = {}
        for tf, sec in self.timeframe_seconds.items():
            entries = self._window_entries(symbol, bucket_ts, sec)
            agg = self._aggregate_window(entries)
            if tf == "15m" and trend_filter_15m:
                agg.update(trend_filter_15m)
            agg["sample_count"] = float(len(entries))
            agg["window_seconds"] = float(sec)
            agg["timestamp_close_utc"] = bucket_ts.isoformat()
            tf_out[tf] = agg

        # helper: zscore (lightweight) for ingestion-side spread_z
        def _zscore(vals: List[float], x: float, eps: float = 1e-9) -> float:
            if not vals or len(vals) < 12:
                return 0.0
            mu = sum(vals) / len(vals)
            var = sum((v - mu) ** 2 for v in vals) / max(1, (len(vals) - 1))
            sd = math.sqrt(var) + eps
            z = (x - mu) / sd
            # winsor clip
            if z > 5.0:
                z = 5.0
            if z < -5.0:
                z = -5.0
            return float(z)
        
        # 为 15m 添加 history 数组（用于 z-score 计算）
        tf_15m = tf_out.get("15m", {})
        if tf_15m:
            tf_15m["history"] = self._build_15m_history(symbol, bucket_ts)
            tf_out["15m"] = tf_15m
        
        # 为 5m 添加 spread_bps 历史（用于 spread_z 计算）
        tf_5m = tf_out.get("5m", {})
        if tf_5m:
            tf_5m["history_spread_bps"] = self._build_spread_history(symbol, bucket_ts)
            tf_out["5m"] = tf_5m
        
        self._attach_range_quantiles(symbol=symbol, ts=bucket_ts, timeframes=tf_out)

        signal_strength = self._calc_signal_strength(normalized)

        # 资金流 3.0: 提取分离特征（传入 tf_15m 的 ret_period）
        # 建议：尽量用 normalized（同一套字段规范），避免 raw metrics 偶发缺键/类型漂移
        microstructure_features = self._extract_microstructure_features(normalized or {})
        fund_flow_features = self._extract_fund_flow_features(normalized or {})

        # === 统一 ret_period：优先用 15m 聚合输出 ===
        ret_15m = 0.0
        if isinstance(tf_15m, dict):
            ret_15m = self._to_float(tf_15m.get("ret_period"), 0.0)
        if "ret_period" not in fund_flow_features:
            fund_flow_features["ret_period"] = ret_15m
        else:
            # 若已存在但为0，而 15m 有值，则用 15m 覆盖（更符合“主周期收益”语义）
            if self._to_float(fund_flow_features.get("ret_period"), 0.0) == 0.0 and ret_15m != 0.0:
                fund_flow_features["ret_period"] = ret_15m

        # === ingestion 侧计算 spread_z（真 zscore），写入 microstructure_features ===
        # 说明：ai_weight_service 会优先读取 ms["spread_z"]，这样就不需要 /10 伪 z
        spread_bps_last = 0.0
        if isinstance(tf_5m, dict):
            spread_bps_last = self._to_float(tf_5m.get("spread_bps_last"), 0.0)
        hist_spread = tf_5m.get("history_spread_bps", []) if isinstance(tf_5m, dict) else []
        if isinstance(hist_spread, list) and spread_bps_last > 0:
            # 用历史的最后 N 个点计算 zscore
            vals = [self._to_float(v, 0.0) for v in hist_spread if v is not None]
            microstructure_features["spread_bps"] = self._to_float(
                microstructure_features.get("spread_bps"), spread_bps_last
            )
            microstructure_features["spread_z"] = _zscore(vals[-60:], microstructure_features["spread_bps"])

        return MarketFlowSnapshot(
            exchange=self.exchange,
            symbol=symbol.upper(),
            timestamp=bucket_ts,
            cvd_ratio=normalized["cvd_ratio"],
            cvd_momentum=normalized["cvd_momentum"],
            oi_delta_ratio=normalized["oi_delta_ratio"],
            funding_rate=normalized["funding_rate"],
            depth_ratio=normalized["depth_ratio"],
            imbalance=normalized["imbalance"],
            signal_strength=signal_strength,
            liquidity_delta_norm=normalized["liquidity_delta_norm"],
            timeframes=tf_out,
            microstructure_features=microstructure_features,
            fund_flow_features=fund_flow_features,
        )
    
    def _build_15m_history(self, symbol: str, ts: datetime, max_bars: int = 120) -> List[Dict[str, float]]:
        """
        构建 15m 历史数组（用于 z-score 和 consistency 计算）
        
        返回最近 max_bars 根 15m 聚合数据的关键字段
        """
        tf_seconds = self.timeframe_seconds.get("15m", 900)
        lookback_seconds = max_bars * tf_seconds
        
        # 从内存历史获取 15m 聚合序列
        series = self._aggregate_series_by_timeframe(
            symbol=symbol,
            ts=ts,
            timeframe="15m",
            lookback_seconds=lookback_seconds,
        )
        
        # 提取关键字段
        history = []
        for agg in series[-max_bars:]:
            history.append({
                "cvd": self._to_float(agg.get("cvd_ratio"), 0.0),
                "cvd_momentum": self._to_float(agg.get("cvd_momentum"), 0.0),
                "oi_delta": self._to_float(agg.get("oi_delta_ratio"), 0.0),
                "funding": self._to_float(agg.get("funding_rate"), 0.0),
                "depth_ratio": self._to_float(agg.get("depth_ratio"), 1.0),
                "imbalance": self._to_float(agg.get("imbalance"), 0.0),
                "liquidity_delta": self._to_float(agg.get("liquidity_delta_norm"), 0.0),
                "micro_delta": self._to_float(agg.get("micro_delta_norm"), 0.0),
                "ret_period": self._to_float(agg.get("ret_period"), 0.0),
            })
        
        return history
    
    def _build_spread_history(self, symbol: str, ts: datetime, max_bars: int = 60) -> List[float]:
        """
        构建 spread_bps 历史（用于 spread_z 计算）
        """
        tf_seconds = self.timeframe_seconds.get("5m", 300)
        lookback_seconds = max_bars * tf_seconds
        
        series = self._aggregate_series_by_timeframe(
            symbol=symbol,
            ts=ts,
            timeframe="5m",
            lookback_seconds=lookback_seconds,
        )
        
        return [self._to_float(agg.get("spread_bps"), 0.0) for agg in series[-max_bars:]]

    def aggregate_batch(
        self,
        symbol: str,
        metrics_batch: Iterable[Dict[str, Any]],
        ts: Optional[datetime] = None,
    ) -> MarketFlowSnapshot:
        metrics_batch = list(metrics_batch)
        if not metrics_batch:
            return self.aggregate_from_metrics(symbol=symbol, metrics={}, ts=ts)
        merged: Dict[str, float] = {
            "cvd_ratio": 0.0,
            "cvd_momentum": 0.0,
            "oi_delta_ratio": 0.0,
            "funding_rate": 0.0,
            "depth_ratio": 0.0,
            "imbalance": 0.0,
            "liquidity_delta_norm": 0.0,
            "micro_delta_norm": 0.0,
            "spread_bps": 0.0,
            "phantom": 0.0,
            "trap_score": 0.0,
        }
        for item in metrics_batch:
            for k in merged:
                merged[k] += float(item.get(k, 0.0) or 0.0)
        n = float(len(metrics_batch))
        merged["depth_ratio"] = merged["depth_ratio"] / n if n > 0 else 1.0
        for k in (
            "cvd_ratio",
            "cvd_momentum",
            "oi_delta_ratio",
            "funding_rate",
            "imbalance",
            "liquidity_delta_norm",
            "micro_delta_norm",
            "spread_bps",
            "phantom",
            "trap_score",
        ):
            merged[k] = merged[k] / n if n > 0 else 0.0
        return self.aggregate_from_metrics(symbol=symbol, metrics=merged, ts=ts)
