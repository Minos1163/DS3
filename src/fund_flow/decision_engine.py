from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional, Tuple

from src.fund_flow.models import ExecutionMode, FundFlowDecision, Operation, TimeInForce
from src.fund_flow.deepseek_weight_router import DeepSeekWeightRouter, WeightMap
from src.fund_flow.weight_router import WeightRouter, WeightResponse, WEIGHT_KEYS


class FundFlowDecisionEngine:
    """
    双引擎决策层：
    - 15m 做市场状态识别（TREND/RANGE/NO_TRADE）
    - 5m 做执行打分（趋势跟随/区间回归）
    
    V3.0 增强:
    - 集成 WeightRouter 本地校验模块
    - 支持 AI 权重调用 + 本地规则回退
    - 完整的归因日志
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config or {}
        ff = self.config.get("fund_flow", {}) or {}
        risk = self.config.get("risk", {}) or {}
        trading = self.config.get("trading", {}) or {}

        self.default_portion = float(ff.get("default_target_portion", risk.get("max_position_pct", 0.2)))
        self.min_leverage = int(ff.get("min_leverage", trading.get("min_leverage", 2)) or 2)
        max_lev_raw = int(ff.get("max_leverage", trading.get("max_leverage", 20)) or 20)
        self.max_leverage = max(self.min_leverage, min(20, max_lev_raw))
        default_lev_raw = int(ff.get("default_leverage", trading.get("default_leverage", self.min_leverage)) or self.min_leverage)
        self.default_leverage = min(self.max_leverage, max(self.min_leverage, default_lev_raw))

        base_open_threshold = float(ff.get("open_threshold", 0.35))
        self.long_open_threshold = float(ff.get("long_open_threshold", base_open_threshold))
        self.short_open_threshold = float(ff.get("short_open_threshold", base_open_threshold))
        self.open_threshold = self.long_open_threshold
        self.close_threshold = float(ff.get("close_threshold", 0.45))
        self.entry_slippage = float(ff.get("entry_slippage", 0.001))
        self.liquidity_norm_factor_weight = max(
            0.0,
            float(ff.get("liquidity_norm_factor_weight", 0.12)),
        )

        stop_loss_raw = ff.get("stop_loss_pct", risk.get("stop_loss_default_percent", 0.01))
        take_profit_raw = ff.get("take_profit_pct", risk.get("take_profit_default_percent", 0.03))
        self.stop_loss_pct = self._normalize_pct_ratio(stop_loss_raw, 0.01)
        self.take_profit_pct = self._normalize_pct_ratio(take_profit_raw, 0.03)

        self.engine_params_cfg = ff.get("engine_params", {}) if isinstance(ff.get("engine_params"), dict) else {}
        self.active_signal_pool_id = str(ff.get("active_signal_pool_id", "default_pool") or "default_pool")

        regime_cfg = ff.get("regime", {}) if isinstance(ff.get("regime"), dict) else {}
        self.regime_timeframe = str(regime_cfg.get("timeframe", "15m") or "15m").strip().lower()
        self.regime_adx_trend_on = max(0.0, self._to_float(regime_cfg.get("adx_trend_on"), 25.0))
        self.regime_adx_range_on = max(0.0, self._to_float(regime_cfg.get("adx_range_on"), 18.0))
        self.regime_no_trade_low = max(0.0, self._to_float(regime_cfg.get("adx_no_trade_low"), self.regime_adx_range_on))
        self.regime_no_trade_high = max(self.regime_no_trade_low, self._to_float(regime_cfg.get("adx_no_trade_high"), self.regime_adx_trend_on))
        self.regime_atr_pct_min = self._normalize_pct_ratio(
            regime_cfg.get("atr_pct_min", ff.get("trend_gate_atr_pct_min", 0.002)),
            0.002,
        )
        self.regime_atr_pct_max = self._normalize_pct_ratio(
            regime_cfg.get("atr_pct_max", ff.get("trend_gate_atr_pct_max", 0.02)),
            0.02,
        )
        direction_lock_mode = str(regime_cfg.get("direction_lock_mode", "hard") or "hard").strip().lower()
        if direction_lock_mode not in {"hard", "soft", "off"}:
            direction_lock_mode = "hard"
        self.direction_lock_mode = direction_lock_mode
        self.direction_lock_ema_band_pct = self._normalize_pct_ratio(
            regime_cfg.get("direction_lock_ema_band_pct", 0.001),
            0.001,
        )
        self.direction_lock_soft_adx_buffer = max(
            0.0,
            self._to_float(regime_cfg.get("direction_lock_soft_adx_buffer"), 4.0),
        )
        rq_cfg = ff.get("range_quantile", {}) if isinstance(ff.get("range_quantile"), dict) else {}
        self.range_quantile_timeframe = str(rq_cfg.get("timeframe", "5m") or "5m").strip().lower()
        turn_confirm_raw = rq_cfg.get("turn_confirm")
        turn_cfg: Dict[str, Any] = turn_confirm_raw if isinstance(turn_confirm_raw, dict) else {}
        self.range_turn_confirm_enabled = bool(turn_cfg.get("enabled", True))
        turn_mode = str(turn_cfg.get("mode", "2bar_peak_valley") or "2bar_peak_valley").strip().lower()
        if turn_mode not in ("1bar", "2bar_peak_valley"):
            turn_mode = "2bar_peak_valley"
        self.range_turn_confirm_mode = turn_mode
        self.range_turn_confirm_min_delta = max(0.0, self._to_float(turn_cfg.get("min_delta"), 0.0))
        self.range_turn_micro_enabled = bool(turn_cfg.get("micro_turn_enabled", True))
        self.range_turn_phantom_decay_enabled = bool(turn_cfg.get("phantom_decay_enabled", True))
        self.range_turn_trap_decay_enabled = bool(turn_cfg.get("trap_decay_enabled", True))
        self.range_turn_min_pass_count = max(1, int(self._to_float(turn_cfg.get("min_pass_count"), 2)))
        trap_guard_raw = rq_cfg.get("trap_guard")
        trap_guard_cfg = trap_guard_raw if isinstance(trap_guard_raw, dict) else {}
        self.range_trap_guard_enabled = bool(trap_guard_cfg.get("enabled", True))
        trap_guard_q = self._to_float(trap_guard_cfg.get("max_quantile"), 0.70)
        self.range_trap_guard_max_quantile = min(0.95, max(0.50, trap_guard_q))
        
        # DeepSeek Weight Router 配置
        ds_cfg = ff.get("deepseek_weight_router", {}) if isinstance(ff.get("deepseek_weight_router"), dict) else {}
        ai_cfg = ff.get("deepseek_ai", {}) if isinstance(ff.get("deepseek_ai"), dict) else {}
        # 关键：透传 deepseek_ai，确保 DeepSeekAIService 能读取完整 AI 配置。
        self.deepseek_router = DeepSeekWeightRouter(
            {
                "deepseek_weight_router": ds_cfg,
                "deepseek_ai": ai_cfg,
            }
        )
        
        # WeightRouter 本地校验模块 - 从配置读取 default_weights
        dw_cfg = ai_cfg.get("default_weights", {}) if isinstance(ai_cfg.get("default_weights"), dict) else {}
        
        # 字段名与 MarketIngestionService 对齐
        # 配置中: trend_cvd, trend_cvd_momentum, trend_oi_delta, ...
        # 内部使用: cvd, cvd_momentum, oi_delta, funding, depth_ratio, imbalance, liquidity_delta, micro_delta
        self.default_weights_config = {
            "TREND": self._parse_default_weights(dw_cfg, "trend"),
            "RANGE": self._parse_default_weights(dw_cfg, "range"),
        }
        
        self.weight_router = WeightRouter({
            "default_weights": self.default_weights_config,
            "cache_ttl_seconds": int(ds_cfg.get("cache_ttl_seconds", 600)),
        })
        
        # 15m+5m 融合配置
        fusion_cfg = ff.get("score_fusion", {}) if isinstance(ff.get("score_fusion"), dict) else {}
        self.score_fusion_enabled = bool(fusion_cfg.get("enabled", True))
        self.score_15m_weight = max(0.0, min(1.0, self._to_float(fusion_cfg.get("score_15m_weight"), 0.6)))
        self.score_5m_weight = 1.0 - self.score_15m_weight
        self.consistency_window = max(1, min(5, int(self._to_float(fusion_cfg.get("consistency_window"), 3))))
        
        # 15m 分数历史缓存 (用于一致性加权)
        self._score_15m_history: Dict[str, Deque[Dict[str, Any]]] = {}
        self._history_max_seconds = 1800  # 30分钟

        # EV 可靠度跟踪器 (Beta-Binomial)
        # 每个指标维护 (alpha, beta)，提高初始可靠度以产生明确方向判断
        # 关键修复: 确保所有主指标的 reliability_factor > 0，避免EV输出为0
        self._ev_reliability: Dict[str, Tuple[float, float]] = {
            "macd": (16.0, 4.0),   # MACD 初始可靠度 0.80 -> reliability_factor=0.60
            "kdj": (15.0, 5.0),    # KDJ(J) 初始可靠度 0.75 -> reliability_factor=0.50
            # 旧组合保留在线可靠度，用于实盘对照日志
            "bbi": (14.0, 6.0),
            "ema_diff": (13.0, 7.0),
            "ema_slope": (12.0, 8.0),
            "cvd": (14.0, 6.0),    # CVD 初始可靠度 0.70 -> reliability_factor=0.40 (提升权重)
            "imbalance": (13.0, 7.0),  # imbalance 初始可靠度 0.65 -> reliability_factor=0.30 (提升权重)
        }
        # 方向判断阈值 - 降低阈值使EV能产生明确方向
        self._direction_neutral_zone = 0.02  # abs(score) < 0.02 视为 FLAT
        self._direction_conflict_penalty = 0.6  # MACD与CVD冲突时乘与此系数
        self._divergence_threshold = 0.15  # EV与LW分歧阈值
        
        # 启动时打印默认权重摘要（确认配置是否生效）
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[WeightRouter] default_weights(TREND): %s", self.default_weights_config.get("TREND", {}))
        logger.info("[WeightRouter] default_weights(RANGE): %s", self.default_weights_config.get("RANGE", {}))
        logger.info("[WeightRouter] score_fusion enabled=%s, 15m_weight=%.2f, 5m_weight=%.2f",
                    self.score_fusion_enabled, self.score_15m_weight, self.score_5m_weight)
    
    def _parse_default_weights(self, dw_cfg: Dict[str, Any], prefix: str) -> Dict[str, float]:
        """
        从配置解析默认权重，字段名与 MarketIngestionService 对齐
        
        配置字段: trend_cvd, trend_cvd_momentum, ...
        内部字段: cvd, cvd_momentum, oi_delta, funding, depth_ratio, imbalance, liquidity_delta, micro_delta
        """
        # 字段映射: 内部名 -> 配置名
        field_map = {
            "cvd": f"{prefix}_cvd",
            "cvd_momentum": f"{prefix}_cvd_momentum",
            "oi_delta": f"{prefix}_oi_delta",
            "funding": f"{prefix}_funding",
            "depth_ratio": f"{prefix}_depth_ratio",
            "imbalance": f"{prefix}_imbalance",
            "liquidity_delta": f"{prefix}_liquidity_delta",
            "micro_delta": f"{prefix}_micro_delta",
        }
        
        # 已知配置字段集合（用于检测未知字段）
        known_config_keys = set(field_map.values())
        
        # 仅检测当前前缀下的未知字段，避免 trend_/range_ 互相误报
        scoped_keys = {k for k in dw_cfg.keys() if str(k).startswith(f"{prefix}_")}
        unknown_keys = scoped_keys - known_config_keys
        if unknown_keys:
            import logging
            logging.warning(
                "[WeightRouter] deepseek_ai.default_weights has unknown keys for %s: %s",
                prefix.upper(),
                sorted(unknown_keys)[:10]
            )
        
        # 默认值
        defaults = {
            "TREND": {"cvd": 0.24, "cvd_momentum": 0.14, "oi_delta": 0.22, "funding": 0.10, 
                      "depth_ratio": 0.15, "imbalance": 0.10, "liquidity_delta": 0.08, "micro_delta": 0.06},
            "RANGE": {"cvd": 0.10, "cvd_momentum": 0.15, "oi_delta": 0.05, "funding": 0.05,
                      "depth_ratio": 0.10, "imbalance": 0.35, "liquidity_delta": 0.12, "micro_delta": 0.18},
        }
        
        weights = {}
        for internal_name, config_name in field_map.items():
            v = self._to_float(dw_cfg.get(config_name), defaults.get(prefix.upper(), {}).get(internal_name, 0.1))
            weights[internal_name] = v
        
        # 归一化
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}
        
        return weights

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _to_optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _normalize_pct_ratio(value: Any, default_ratio: float) -> float:
        if value is None:
            return abs(float(default_ratio))
        try:
            if isinstance(value, str):
                raw = value.strip()
                if raw.endswith("%"):
                    return abs(float(raw[:-1])) / 100.0
                v = float(raw)
            else:
                v = float(value)
        except Exception:
            return abs(float(default_ratio))
        v = abs(v)
        if v <= 0.05:
            return v
        return v / 100.0

    def _score_trend(self, market_flow_context: Dict[str, Any]) -> Dict[str, float]:
        cvd = self._to_float(market_flow_context.get("cvd_ratio"))
        cvd_mom = self._to_float(market_flow_context.get("cvd_momentum"))
        oi_delta = self._to_float(market_flow_context.get("oi_delta_ratio"))
        funding = self._to_float(market_flow_context.get("funding_rate"))
        depth = self._to_float(market_flow_context.get("depth_ratio"), 1.0) - 1.0
        imbalance = self._to_float(market_flow_context.get("imbalance"))
        liquidity_delta_norm = self._to_float(market_flow_context.get("liquidity_delta_norm"))

        long_score = (
            0.24 * max(cvd, 0.0)
            + 0.14 * max(cvd_mom, 0.0)
            + 0.22 * max(oi_delta, 0.0)
            + 0.10 * max(-funding, 0.0)
            + 0.15 * max(depth, 0.0)
            + 0.15 * max(imbalance, 0.0)
        )
        short_score = (
            0.24 * max(-cvd, 0.0)
            + 0.14 * max(-cvd_mom, 0.0)
            + 0.22 * max(oi_delta, 0.0)
            + 0.10 * max(funding, 0.0)
            + 0.15 * max(-depth, 0.0)
            + 0.15 * max(-imbalance, 0.0)
        )
        if self.liquidity_norm_factor_weight > 0:
            long_score += self.liquidity_norm_factor_weight * max(liquidity_delta_norm, 0.0)
            short_score += self.liquidity_norm_factor_weight * max(-liquidity_delta_norm, 0.0)
        return {
            "long_score": min(max(long_score, 0.0), 1.0),
            "short_score": min(max(short_score, 0.0), 1.0),
        }

    def _score_range(self, market_flow_context: Dict[str, Any]) -> Dict[str, float]:
        imbalance = self._to_float(market_flow_context.get("imbalance"), 0.0)
        cvd_mom = self._to_float(market_flow_context.get("cvd_momentum"), 0.0)
        oi_delta = self._to_float(market_flow_context.get("oi_delta_ratio"), 0.0)
        depth = self._to_float(market_flow_context.get("depth_ratio"), 1.0) - 1.0

        long_score = 0.55 * max(-imbalance, 0.0) + 0.35 * max(-cvd_mom, 0.0) + 0.10 * max(-depth, 0.0)
        short_score = 0.55 * max(imbalance, 0.0) + 0.35 * max(cvd_mom, 0.0) + 0.10 * max(depth, 0.0)
        oi_penalty = min(max(abs(oi_delta), 0.0), 1.0) * 0.20
        long_score = max(0.0, long_score - oi_penalty)
        short_score = max(0.0, short_score - oi_penalty)
        return {
            "long_score": min(max(long_score, 0.0), 1.0),
            "short_score": min(max(short_score, 0.0), 1.0),
        }
    
    def _score_with_weights(
        self,
        market_flow_context: Dict[str, Any],
        weight_map: WeightMap,
        regime: str,
    ) -> Dict[str, float]:
        """使用动态权重计算分数"""
        cvd = self._to_float(market_flow_context.get("cvd_ratio"))
        cvd_mom = self._to_float(market_flow_context.get("cvd_momentum"))
        oi_delta = self._to_float(market_flow_context.get("oi_delta_ratio"))
        funding = self._to_float(market_flow_context.get("funding_rate"))
        depth = self._to_float(market_flow_context.get("depth_ratio"), 1.0) - 1.0
        imbalance = self._to_float(market_flow_context.get("imbalance"))
        liquidity_delta_norm = self._to_float(market_flow_context.get("liquidity_delta_norm"))

        if regime == "TREND":
            long_score = (
                weight_map.trend_cvd_weight * max(cvd, 0.0)
                + weight_map.trend_cvd_momentum_weight * max(cvd_mom, 0.0)
                + weight_map.trend_oi_delta_weight * max(oi_delta, 0.0)
                + weight_map.trend_funding_weight * max(-funding, 0.0)
                + weight_map.trend_depth_weight * max(depth, 0.0)
                + weight_map.trend_imbalance_weight * max(imbalance, 0.0)
                + weight_map.trend_liquidity_norm_weight * max(liquidity_delta_norm, 0.0)
            )
            short_score = (
                weight_map.trend_cvd_weight * max(-cvd, 0.0)
                + weight_map.trend_cvd_momentum_weight * max(-cvd_mom, 0.0)
                + weight_map.trend_oi_delta_weight * max(-oi_delta, 0.0)
                + weight_map.trend_funding_weight * max(funding, 0.0)
                + weight_map.trend_depth_weight * max(-depth, 0.0)
                + weight_map.trend_imbalance_weight * max(-imbalance, 0.0)
                + weight_map.trend_liquidity_norm_weight * max(-liquidity_delta_norm, 0.0)
            )
        else:  # RANGE
            long_score = (
                weight_map.range_imbalance_weight * max(-imbalance, 0.0)
                + weight_map.range_cvd_momentum_weight * max(-cvd_mom, 0.0)
                + weight_map.range_depth_weight * max(-depth, 0.0)
            )
            short_score = (
                weight_map.range_imbalance_weight * max(imbalance, 0.0)
                + weight_map.range_cvd_momentum_weight * max(cvd_mom, 0.0)
                + weight_map.range_depth_weight * max(depth, 0.0)
            )

        return {
            "long_score": min(max(long_score, 0.0), 1.0),
            "short_score": min(max(short_score, 0.0), 1.0),
        }
    
    def _extract_15m_context(self, market_flow_context: Dict[str, Any]) -> Dict[str, Any]:
        """提取 15m 时间框架上下文"""
        timeframes = market_flow_context.get("timeframes")
        if not isinstance(timeframes, dict):
            return {}
        tf_15m = timeframes.get("15m")
        if not isinstance(tf_15m, dict):
            return {}
        return tf_15m
    
    def _extract_5m_context(self, market_flow_context: Dict[str, Any]) -> Dict[str, Any]:
        """提取 5m 时间框架上下文"""
        timeframes = market_flow_context.get("timeframes")
        if not isinstance(timeframes, dict):
            return {}
        tf_5m = timeframes.get("5m")
        if not isinstance(tf_5m, dict):
            return {}
        return tf_5m
    
    def _record_15m_score(
        self,
        symbol: str,
        score_15m: Dict[str, float],
        regime: str,
        ts: Optional[datetime] = None,
    ) -> None:
        """记录 15m 分数历史"""
        ts = ts or datetime.now(timezone.utc)
        symbol_up = symbol.upper()
        
        if symbol_up not in self._score_15m_history:
            self._score_15m_history[symbol_up] = deque()
        
        history = self._score_15m_history[symbol_up]
        history.append({
            "timestamp": ts,
            "long_score": score_15m.get("long_score", 0.0),
            "short_score": score_15m.get("short_score", 0.0),
            "regime": regime,
        })
        
        # 清理过期记录
        cutoff = ts.timestamp() - self._history_max_seconds
        while history and history[0]["timestamp"].timestamp() < cutoff:
            history.popleft()
    
    def _compute_consistency_weight(self, symbol: str, current_direction: str) -> float:
        """
        计算一致性加权因子
        
        连续 N 根 15m 方向一致时增加权重
        """
        history = self._score_15m_history.get(symbol.upper(), deque())
        if len(history) < 2:
            return 1.0
        
        # 检查最近 N 根的方向一致性
        recent = list(history)[-self.consistency_window:]
        if len(recent) < 2:
            return 1.0
        
        consistent_count = 0
        for record in recent:
            if current_direction == "LONG":
                if record.get("long_score", 0) > record.get("short_score", 0):
                    consistent_count += 1
            elif current_direction == "SHORT":
                if record.get("short_score", 0) > record.get("long_score", 0):
                    consistent_count += 1
        
        # 一致性奖励: 每多一根一致增加 10% 权重
        if consistent_count >= self.consistency_window:
            return 1.0 + 0.1 * (consistent_count - self.consistency_window + 1)
        return 1.0
    
    def _fuse_scores(
        self,
        symbol: str,
        score_15m: Dict[str, float],
        score_5m: Dict[str, float],
        regime: str,
    ) -> Dict[str, Any]:
        """
        融合 15m 和 5m 分数
        
        FinalScore = 0.6*Score_15m + 0.4*Score_5m
        一致性加权: 连续方向一致时增强信号
        """
        if not self.score_fusion_enabled:
            return {
                "long_score": score_5m.get("long_score", 0.0),
                "short_score": score_5m.get("short_score", 0.0),
                "fusion_applied": False,
                "score_15m": score_15m,
                "score_5m": score_5m,
            }
        
        # 基础融合
        fused_long = (
            self.score_15m_weight * score_15m.get("long_score", 0.0)
            + self.score_5m_weight * score_5m.get("long_score", 0.0)
        )
        fused_short = (
            self.score_15m_weight * score_15m.get("short_score", 0.0)
            + self.score_5m_weight * score_5m.get("short_score", 0.0)
        )
        
        # 确定主要方向
        primary_direction = "LONG" if fused_long > fused_short else "SHORT"
        
        # 一致性加权
        consistency_weight = self._compute_consistency_weight(symbol, primary_direction)
        
        if consistency_weight > 1.0:
            # 增强主方向分数
            if primary_direction == "LONG":
                fused_long *= consistency_weight
            else:
                fused_short *= consistency_weight
        
        return {
            "long_score": min(max(fused_long, 0.0), 1.0),
            "short_score": min(max(fused_short, 0.0), 1.0),
            "fusion_applied": True,
            "score_15m": score_15m,
            "score_5m": score_5m,
            "score_15m_weight": self.score_15m_weight,
            "score_5m_weight": self.score_5m_weight,
            "consistency_weight": consistency_weight,
            "primary_direction": primary_direction,
        }
    
    def _compute_flow_consistency(
        self,
        market_flow_context: Dict[str, Any],
        tf_15m_ctx: Dict[str, Any],
        tf_5m_ctx: Dict[str, Any],
    ) -> Tuple[float, int]:
        """
        计算资金一致性指标
        
        flow_confirm: CVD、OI、价格方向是否一致（用于 TREND 模式增强）
        consistency_3bars: 最近3根15m的一致性计数
        
        返回: (flow_confirm, consistency_3bars)
        """
        # 从当前上下文获取资金流特征
        fund_flow = market_flow_context.get("fund_flow_features", {})
        if not fund_flow:
            # 回退到从主上下文获取
            fund_flow = market_flow_context
        
        # flow_confirm: 如果上游已经计算好，直接使用
        flow_confirm = self._to_float(fund_flow.get("flow_confirm"), -1.0)
        if flow_confirm < 0:
            # 否则从原始字段计算
            cvd = self._to_float(market_flow_context.get("cvd_ratio", 
                            fund_flow.get("cvd", 0.0)))
            oi_delta = self._to_float(market_flow_context.get("oi_delta_ratio",
                                 fund_flow.get("oi_delta", 0.0)))
            # 使用 15m 收益率作为价格方向参考
            ret_15m = self._to_float(tf_15m_ctx.get("ret_period",
                          tf_15m_ctx.get("ret_15m", 
                          market_flow_context.get("ret_period", 0.0))))
            
            cvd_sign = 1 if cvd > 0 else (-1 if cvd < 0 else 0)
            oi_sign = 1 if oi_delta > 0 else (-1 if oi_delta < 0 else 0)
            ret_sign = 1 if ret_15m > 0 else (-1 if ret_15m < 0 else 0)
            
            if cvd_sign == oi_sign == ret_sign and cvd_sign != 0:
                flow_confirm = 1.0  # 三者一致
            elif cvd_sign == ret_sign or oi_sign == ret_sign:
                flow_confirm = 0.5  # 部分一致
            else:
                flow_confirm = 0.0  # 不一致
        
        # consistency_3bars: 从 15m 历史计算
        consistency_3bars = 0
        if tf_15m_ctx:
            # 获取最近3根的 CVD 和收益率方向
            prev_list = []
            for i in range(1, 4):
                prev_key = f"prev{'' if i == 1 else i}"
                prev = tf_15m_ctx.get(prev_key if i > 1 else "prev", {})
                if prev:
                    prev_list.append(prev)
            
            # 计算一致性
            current_cvd = self._to_float(tf_15m_ctx.get("cvd_ratio", 
                            tf_15m_ctx.get("cvd", 0.0)))
            current_ret = self._to_float(tf_15m_ctx.get("ret_period",
                            tf_15m_ctx.get("ret_15m", 0.0)))
            current_sign = 1 if current_cvd > 0 else (-1 if current_cvd < 0 else 0)
            
            for prev in prev_list:
                prev_cvd = self._to_float(prev.get("cvd_ratio", 
                               prev.get("cvd", 0.0)))
                prev_ret = self._to_float(prev.get("ret_period",
                               prev.get("ret_15m", 0.0)))
                prev_sign = 1 if prev_cvd > 0 else (-1 if prev_cvd < 0 else 0)
                if prev_sign == current_sign and current_sign != 0:
                    consistency_3bars += 1
        
        return flow_confirm, consistency_3bars

    def _pick_leverage(
        self,
        score: float,
        threshold: float,
        min_leverage: int,
        max_leverage: int,
        default_leverage: int,
    ) -> int:
        min_lev = max(1, int(min_leverage))
        max_lev = max(min_lev, int(max_leverage))
        default_lev = min(max_lev, max(min_lev, int(default_leverage)))
        if max_lev == min_lev:
            return min_lev
        s = min(max(float(score), 0.0), 1.0)
        th = min(max(float(threshold), 0.0), 0.99)
        denom = max(1e-6, 1.0 - th)
        strength = max(0.0, min(1.0, (s - th) / denom))
        lev = int(round(min_lev + strength * (max_lev - min_lev)))
        lev = min(max_lev, max(min_lev, lev))
        if lev <= 0:
            return default_lev
        return lev

    def _engine_params_for(self, regime: str) -> Dict[str, Any]:
        normalized_regime = str(regime or "TREND").upper()
        raw = self.engine_params_cfg.get(normalized_regime, {})
        raw_cfg = raw if isinstance(raw, dict) else {}
        base_pool = self.active_signal_pool_id
        if base_pool.upper() == "AUTO":
            base_pool = "default_pool"

        params: Dict[str, Any] = {
            "default_target_portion": float(self.default_portion),
            "add_position_portion": float(self.default_portion),
            "max_symbol_position_portion": 1.0,
            "max_active_symbols": 1,
            "min_leverage": int(self.min_leverage),
            "max_leverage": int(self.max_leverage),
            "default_leverage": int(self.default_leverage),
            "long_open_threshold": float(self.long_open_threshold),
            "short_open_threshold": float(self.short_open_threshold),
            "close_threshold": float(self.close_threshold),
            "take_profit_pct": float(self.take_profit_pct),
            "stop_loss_pct": float(self.stop_loss_pct),
            "dca_max_additions": 0,
            "dca_drawdown_thresholds": [],
            "dca_multipliers": [],
            "signal_pool_id": str(base_pool),
        }
        for key, value in raw_cfg.items():
            params[key] = value

        params["default_target_portion"] = max(0.0, self._to_float(params.get("default_target_portion"), self.default_portion))
        params["add_position_portion"] = max(0.0, self._to_float(params.get("add_position_portion"), params["default_target_portion"]))
        params["max_symbol_position_portion"] = max(
            params["default_target_portion"],
            self._to_float(params.get("max_symbol_position_portion"), max(params["default_target_portion"], 0.1)),
        )
        params["max_active_symbols"] = max(1, int(self._to_float(params.get("max_active_symbols"), 1)))
        params["min_leverage"] = max(1, int(self._to_float(params.get("min_leverage"), self.min_leverage)))
        params["max_leverage"] = max(params["min_leverage"], int(self._to_float(params.get("max_leverage"), self.max_leverage)))
        params["default_leverage"] = min(
            params["max_leverage"],
            max(params["min_leverage"], int(self._to_float(params.get("default_leverage"), self.default_leverage))),
        )
        params["long_open_threshold"] = min(1.0, max(0.0, self._to_float(params.get("long_open_threshold"), self.long_open_threshold)))
        params["short_open_threshold"] = min(1.0, max(0.0, self._to_float(params.get("short_open_threshold"), self.short_open_threshold)))
        params["close_threshold"] = min(1.0, max(0.0, self._to_float(params.get("close_threshold"), self.close_threshold)))
        params["take_profit_pct"] = self._normalize_pct_ratio(params.get("take_profit_pct"), self.take_profit_pct)
        params["stop_loss_pct"] = self._normalize_pct_ratio(params.get("stop_loss_pct"), self.stop_loss_pct)
        params["dca_max_additions"] = max(0, int(self._to_float(params.get("dca_max_additions"), 0)))

        thresholds_raw = params.get("dca_drawdown_thresholds")
        thresholds: list[float] = []
        if isinstance(thresholds_raw, list):
            for x in thresholds_raw:
                v = self._normalize_pct_ratio(x, 0.0)
                if v > 0:
                    thresholds.append(v)
        params["dca_drawdown_thresholds"] = thresholds

        multipliers_raw = params.get("dca_multipliers")
        multipliers: list[float] = []
        if isinstance(multipliers_raw, list):
            for x in multipliers_raw:
                m = self._to_float(x, 1.0)
                if m <= 0:
                    m = 1.0
                multipliers.append(m)
        params["dca_multipliers"] = multipliers
        params["signal_pool_id"] = str(params.get("signal_pool_id", base_pool) or base_pool)
        return params

    # ========== 方向判断三层架构 ==========

    def _compute_direction_features(
        self,
        tf_ctx: Dict[str, Any],
        market_flow_context: Dict[str, Any],
    ) -> Dict[str, float]:
        """
        第一层：计算归一化特征分数 (范围 [-1, 1])

        Args:
            tf_ctx: 时间框架上下文 (包含技术指标)
            market_flow_context: 市场流动上下文 (包含 CVD/imbalance)

        Returns:
            feature_scores: 各指标的方向分数

        归一化方法：
            norm = clip(value / (rolling_std + eps), -3, 3) / 3
            结果范围 [-1, 1]
        """
        features: Dict[str, float] = {}

        # ========== 主指标 (决定方向) ==========
        # 新主判：MACD + KDJ(J)

        # MACD hist (主指标)
        macd_hist_norm = self._to_float(tf_ctx.get("macd_hist_norm"), 0.0)
        features["macd"] = max(-1.0, min(1.0, macd_hist_norm))

        # KDJ(J) 归一化值：优先读取 kdj_j_norm，回退使用 (J-50)/50
        kdj_j_norm = self._to_float(tf_ctx.get("kdj_j_norm"), 0.0)
        if abs(kdj_j_norm) < 1e-9:
            kdj_j = self._to_float(tf_ctx.get("kdj_j"), 50.0)
            kdj_j_norm = (kdj_j - 50.0) / 50.0
        features["kdj"] = max(-1.0, min(1.0, kdj_j_norm))

        # 旧组合分量（MACD+BBI+EMA）保留用于实盘对照日志
        bbi_gap_norm = self._to_float(tf_ctx.get("bbi_gap_norm"), 0.0)
        ema_diff_norm = self._to_float(tf_ctx.get("ema_diff_norm"), 0.0)
        ema_slope_norm = self._to_float(tf_ctx.get("ema_slope_norm"), 0.0)
        features["legacy_bbi"] = max(-1.0, min(1.0, bbi_gap_norm))
        features["legacy_ema_diff"] = max(-1.0, min(1.0, ema_diff_norm))
        features["legacy_ema_slope"] = max(-1.0, min(1.0, ema_slope_norm))

        # ========== 确认/否决指标 (不决定方向，用于确认或否决) ==========

        # CVD (资金流) - 用于确认/否决，而非决定方向
        # 与主指标同向 = 确认，反向 = 否决/警示
        cvd_momentum = self._to_float(market_flow_context.get("cvd_momentum"), 0.0)
        # 使用 tanh 平滑裁剪，避免极端值
        import math
        cvd_norm = math.tanh(cvd_momentum * 300)  # 放大后 tanh
        features["cvd"] = max(-1.0, min(1.0, cvd_norm))

        # imbalance (订单流失衡)
        imbalance = self._to_float(market_flow_context.get("imbalance"), 0.0)
        imb_norm = math.tanh(imbalance * 5)  # tanh 裁剪
        features["imbalance"] = max(-1.0, min(1.0, imb_norm))

        return features

    def _score_legacy_combo(self, features: Dict[str, float]) -> Dict[str, Any]:
        """
        旧方向组合（MACD + BBI + EMA diff + EMA slope），仅用于对照日志。
        """
        legacy_weights = {
            "macd": 0.40,
            "legacy_bbi": 0.30,
            "legacy_ema_diff": 0.18,
            "legacy_ema_slope": 0.12,
        }
        legacy_score = 0.0
        for key, weight in legacy_weights.items():
            legacy_score += self._to_float(features.get(key), 0.0) * weight
        if abs(legacy_score) < self._direction_neutral_zone:
            legacy_dir = "BOTH"
        elif legacy_score > 0:
            legacy_dir = "LONG_ONLY"
        else:
            legacy_dir = "SHORT_ONLY"
        return {"dir": legacy_dir, "score": round(legacy_score, 3)}

    def _score_lw(
        self,
        features: Dict[str, float],
    ) -> Dict[str, Any]:
        """
        第二层A：线性加权法 (LW) - 当前决策用

        规则：
        - MACD + KDJ 双振为主方向判断
        - CVD/imbalance 作为确认/否决项（不决定方向）
        - CVD与主方向冲突时，降低置信度

        Returns:
            {
                "dir": "LONG_ONLY"/"SHORT_ONLY"/"BOTH",
                "score": float,  # [-1, 1]
                "components": {"macd": ..., "kdj": ..., ...},
                "conflict": bool,
                "confirmation": float,  # 确认度 [-1, 1]
                "legacy_combo": {"dir": ..., "score": ...},
            }
        """
        components: Dict[str, float] = {}

        # ========== 方向决定权重 (主指标: MACD + KDJ) ==========
        direction_weights = {
            "macd": 0.55,
            "kdj": 0.45,
        }

        # ========== 计算 方向分数 ==========
        lw_score = 0.0
        for key, weight in direction_weights.items():
            val = features.get(key, 0.0)
            contribution = val * weight
            lw_score += contribution
            components[key] = round(contribution, 3)

        # 双振增强：同向增强，反向衰减
        macd_val = self._to_float(features.get("macd"), 0.0)
        kdj_val = self._to_float(features.get("kdj"), 0.0)
        dual_same = (macd_val * kdj_val) > 0
        dual_strength = min(abs(macd_val), abs(kdj_val))
        if dual_strength >= 0.05:
            if dual_same:
                resonance_boost = min(1.15, 1.0 + 0.12 * dual_strength)
                lw_score *= resonance_boost
                components["dual_boost"] = round(resonance_boost, 3)
            else:
                resonance_penalty = 0.78
                lw_score *= resonance_penalty
                components["dual_penalty"] = round(resonance_penalty, 3)
        components["dual_same"] = 1.0 if dual_same else -1.0

        # ========== 确认/否决指标 (CVD, imbalance) ==========
        # 不参与方向决定，只用于确认或否决
        cvd_val = features.get("cvd", 0.0)
        imb_val = features.get("imbalance", 0.0)
        legacy_combo = self._score_legacy_combo(features)

        # ========== 主指标失效检测 ==========
        # 当所有主指标都接近0时，启用CVD/imbalance作为备用方向判断
        primary_indicators_flat = all(abs(features.get(k, 0.0)) < 0.05 for k in direction_weights.keys())

        if primary_indicators_flat and (abs(cvd_val) > 0.1 or abs(imb_val) > 0.1):
            # 主指标失效，使用CVD和imbalance作为方向判断
            # 这种情况下，CVD/imbalance从"确认指标"升级为"方向决定指标"
            cvd_direction_weight = 0.60
            imbalance_direction_weight = 0.40
            backup_score = cvd_val * cvd_direction_weight + imb_val * imbalance_direction_weight
            lw_score = backup_score
            components["backup_direction"] = True
            components["backup_score"] = round(backup_score, 3)
            # 备用方向模式下，不进行确认度计算
            confirmation = 0.0
            has_conflict = False
        else:
            # 正常模式：计算确认度
            # 计算确认度：与主方向一致为正，冲突为负
            if lw_score > 0:  # 多头方向
                confirmation = (cvd_val + imb_val) / 2  # 平均确认度
            elif lw_score < 0:  # 空头方向
                confirmation = (-cvd_val - imb_val) / 2  # 反向确认
            else:
                confirmation = 0.0

            # ========== 冲突检测 ==========
            # CVD 与主方向冲突时标记
            has_conflict = (lw_score * cvd_val < 0) and (abs(lw_score) > 0.05) and (abs(cvd_val) > 0.1)

            # ========== 确认/否决调整 ==========
            if has_conflict:
                # 冲突时：降低置信度，但不改变方向
                # 使用指数衰减而非乘法惩罚
                confidence_penalty = 0.7 + 0.3 * (1 - abs(cvd_val))  # 惩罚范围 0.7~1.0
                lw_score *= confidence_penalty
                components["conflict_penalty"] = round(confidence_penalty, 3)
            elif abs(confirmation) > 0.3:
                # 强确认时：适当增强信号
                confidence_boost = 1.0 + 0.1 * abs(confirmation)
                lw_score *= min(1.15, confidence_boost)  # 最多增强 15%
                components["confidence_boost"] = round(min(1.15, confidence_boost), 3)

        components["cvd"] = round(cvd_val, 3)
        components["imbalance"] = round(imb_val, 3)
        components["confirmation"] = round(confirmation, 3)
        components["primary_flat"] = primary_indicators_flat

        # ========== 确定方向 ==========
        if abs(lw_score) < self._direction_neutral_zone:
            direction = "BOTH"
        elif lw_score > 0:
            direction = "LONG_ONLY"
        else:
            direction = "SHORT_ONLY"

        return {
            "dir": direction,
            "score": round(lw_score, 3),
            "components": components,
            "conflict": has_conflict,
            "confirmation": round(confirmation, 3),
            "legacy_combo": legacy_combo,
            "active_model": "MACD+KDJ",
        }

    def _score_ev(
        self,
        features: Dict[str, float],
    ) -> Dict[str, Any]:
        """
        第二层B：期望值法 (EV) - 用于对比评估

        使用在线可靠度 (Beta-Binomial)：
        ev_score = Σ( w_i * (2*p_i - 1) * score_i )

        注意：CVD/imbalance 作为确认项，不决定方向

        Returns:
            {
                "dir": "LONG_ONLY"/"SHORT_ONLY"/"BOTH",
                "score": float,
                "components": {...},
                "reliabilities": {"macd": p_macd, ...},
            }
        """
        components: Dict[str, float] = {}
        reliabilities: Dict[str, float] = {}

        # ========== 方向决定权重 (主指标: MACD + KDJ) ==========
        direction_weights = {
            "macd": 0.55,
            "kdj": 0.45,
        }

        ev_score = 0.0
        for key, weight in direction_weights.items():
            val = features.get(key, 0.0)

            # 获取该指标的可靠度
            alpha, beta = self._ev_reliability.get(key, (10.0, 10.0))
            p_i = alpha / (alpha + beta)  # 可靠度 [0, 1]
            reliabilities[key] = round(p_i, 3)

            # (2*p_i - 1) 将可靠度映射到 [-1, 1]
            # p=0.5 -> 0 (无信息), p=1.0 -> 1 (完全可靠)
            reliability_factor = 2 * p_i - 1

            contribution = weight * reliability_factor * val
            ev_score += contribution
            components[key] = round(contribution, 3)

        # 双振一致性处理：同向增强，反向衰减
        macd_val = self._to_float(features.get("macd"), 0.0)
        kdj_val = self._to_float(features.get("kdj"), 0.0)
        dual_same = (macd_val * kdj_val) > 0
        dual_strength = min(abs(macd_val), abs(kdj_val))
        if dual_strength >= 0.05:
            if dual_same:
                ev_boost = min(1.12, 1.0 + 0.10 * dual_strength)
                ev_score *= ev_boost
                components["dual_boost"] = round(ev_boost, 3)
            else:
                ev_penalty = 0.80
                ev_score *= ev_penalty
                components["dual_penalty"] = round(ev_penalty, 3)
        components["dual_same"] = 1.0 if dual_same else -1.0

        # ========== 确认/否决指标 (不参与方向决定) ==========
        cvd_val = features.get("cvd", 0.0)
        imb_val = features.get("imbalance", 0.0)
        components["cvd"] = round(cvd_val, 3)
        components["imbalance"] = round(imb_val, 3)
        legacy_combo = self._score_legacy_combo(features)

        # 记录确认指标的可靠度
        for key in ("cvd", "imbalance"):
            alpha, beta = self._ev_reliability.get(key, (10.0, 10.0))
            p_i = alpha / (alpha + beta)
            reliabilities[key] = round(p_i, 3)

        # ========== 主指标失效检测 ==========
        # 当所有主指标都接近0时，启用CVD/imbalance作为备用方向判断
        primary_indicators_flat = all(abs(features.get(k, 0.0)) < 0.05 for k in direction_weights.keys())

        if primary_indicators_flat and (abs(cvd_val) > 0.1 or abs(imb_val) > 0.1):
            # 主指标失效，使用CVD和imbalance作为方向判断
            # 使用可靠度加权的备用方向判断
            cvd_alpha, cvd_beta = self._ev_reliability.get("cvd", (10.0, 10.0))
            imb_alpha, imb_beta = self._ev_reliability.get("imbalance", (10.0, 10.0))
            cvd_reliability = 2 * (cvd_alpha / (cvd_alpha + cvd_beta)) - 1
            imb_reliability = 2 * (imb_alpha / (imb_alpha + imb_beta)) - 1

            # 归一化权重
            total_rel = abs(cvd_reliability) + abs(imb_reliability)
            if total_rel > 0:
                cvd_weight = abs(cvd_reliability) / total_rel
                imb_weight = abs(imb_reliability) / total_rel
            else:
                cvd_weight = 0.6
                imb_weight = 0.4

            ev_score = cvd_val * cvd_weight + imb_val * imb_weight
            components["backup_direction"] = True
            components["backup_score"] = round(ev_score, 3)
            components["primary_flat"] = True

        # 确定方向
        if abs(ev_score) < self._direction_neutral_zone:
            direction = "BOTH"
        elif ev_score > 0:
            direction = "LONG_ONLY"
        else:
            direction = "SHORT_ONLY"

        return {
            "dir": direction,
            "score": round(ev_score, 3),
            "components": components,
            "reliabilities": reliabilities,
            "legacy_combo": legacy_combo,
            "active_model": "MACD+KDJ",
        }

    def _update_ev_reliability(
        self,
        features: Dict[str, float],
        actual_direction: str,
        prediction_direction: str,
    ) -> None:
        """
        更新 EV 可靠度跟踪器 (Beta-Binomial)

        Args:
            features: 当时的特征分数
            actual_direction: 实际市场方向 ("LONG", "SHORT", "FLAT")
            prediction_direction: 预测方向
        """
        if actual_direction not in ("LONG", "SHORT"):
            return  # 无法判断，不更新

        correct = (actual_direction == "LONG" and prediction_direction == "LONG_ONLY") or \
                  (actual_direction == "SHORT" and prediction_direction == "SHORT_ONLY")

        for key in self._ev_reliability:
            alpha, beta = self._ev_reliability[key]
            # 只有该指标方向与实际方向一致时才算正确
            val = features.get(key, 0.0)
            indicator_long = val > 0.05
            indicator_short = val < -0.05

            indicator_correct = (actual_direction == "LONG" and indicator_long) or \
                               (actual_direction == "SHORT" and indicator_short)

            if indicator_correct:
                alpha += 1.0
            elif abs(val) > 0.05:  # 有明确预测但错误
                beta += 1.0

            # 防止 alpha/beta 过大导致更新太慢
            if alpha + beta > 100:
                alpha = alpha * 0.9
                beta = beta * 0.9

            self._ev_reliability[key] = (alpha, beta)

    def _detect_regime(self, market_flow_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        市场状态检测 + 方向判断 (三层架构)

        返回结构:
        {
            "regime": "TREND"/"RANGE"/"NO_TRADE",
            "direction": "LONG_ONLY"/"SHORT_ONLY"/"BOTH",
            "reason": "...",
            # 基础指标
            "ema_fast": ..., "ema_slow": ..., "adx": ..., "atr_pct": ...,
            # 方向判断详情
            "lw": {"dir": ..., "score": ..., "components": {...}, "conflict": ...},
            "ev": {"dir": ..., "score": ..., "components": {...}, "reliabilities": {...}},
            "final": {"dir": ..., "score": ..., "method": "LW", "need_confirm": ...},
            # 辅助字段
            "ev_direction": ..., "ev_score": ..., "lw_direction": ..., "lw_score": ...,
        }
        """
        timeframes = market_flow_context.get("timeframes")
        if not isinstance(timeframes, dict):
            return {"regime": "NO_TRADE", "direction": "BOTH", "reason": "missing_timeframes"}

        tf = str(self.regime_timeframe or "15m")
        tf_ctx = timeframes.get(tf)
        if not isinstance(tf_ctx, dict):
            return {"regime": "NO_TRADE", "direction": "BOTH", "reason": f"missing_{tf}_context"}

        # ========== 获取基础指标 ==========
        ema_fast = self._to_float(tf_ctx.get("ema_fast"), 0.0)
        ema_slow = self._to_float(tf_ctx.get("ema_slow"), 0.0)
        adx = self._to_float(tf_ctx.get("adx"), 0.0)
        atr_pct = abs(self._to_float(tf_ctx.get("atr_pct"), 0.0))
        # K线 open/close 价格
        last_open = self._to_float(tf_ctx.get("last_open"), 0.0)
        last_close = self._to_float(tf_ctx.get("last_close"), 0.0)

        if ema_fast <= 0 or ema_slow <= 0 or adx <= 0 or atr_pct <= 0:
            return {
                "regime": "NO_TRADE",
                "direction": "BOTH",
                "reason": "missing_regime_metrics",
                "last_open": last_open,
                "last_close": last_close,
            }

        # ========== 第一层：计算归一化特征 ==========
        features = self._compute_direction_features(tf_ctx, market_flow_context)

        # ========== 第二层：两套算法 ==========
        lw_result = self._score_lw(features)
        ev_result = self._score_ev(features)

        # ========== 第三层：汇总与冲突处理 ==========
        lw_dir = lw_result["dir"]
        lw_score = lw_result["score"]
        ev_dir = ev_result["dir"]
        ev_score = ev_result["score"]

        # 分歧度
        divergence = abs(lw_score - ev_score)

        # 一致性检查
        agree = (lw_dir == ev_dir) or (lw_dir == "BOTH" or ev_dir == "BOTH")

        # 是否需要额外确认
        need_confirm = False
        if not agree and divergence > self._divergence_threshold:
            need_confirm = True

        # 最终方向采用 EV（主），LW 仅做辅助观测
        direction = ev_dir

        # 如果分歧太大且 EV 本身不明确，使用 BOTH
        if need_confirm and abs(ev_score) < 0.1:
            direction = "BOTH"

        legacy_combo = lw_result.get("legacy_combo", {}) if isinstance(lw_result.get("legacy_combo"), dict) else {}
        legacy_dir = str(legacy_combo.get("dir", "BOTH"))
        legacy_score = self._to_float(legacy_combo.get("score"), 0.0)
        cvd_for_compare = self._to_float(features.get("cvd"), 0.0)
        agility_new = abs(ev_score)
        agility_old = abs(legacy_score)
        align_new = 1 if (ev_score * cvd_for_compare > 0 and abs(cvd_for_compare) > 0.05) else 0
        align_old = 1 if (legacy_score * cvd_for_compare > 0 and abs(cvd_for_compare) > 0.05) else 0
        winner = "MACD+KDJ" if (agility_new + 0.05 * align_new) >= (agility_old + 0.05 * align_old) else "MACD+BBI+EMA"
        combo_compare = {
            "active_model": "MACD+KDJ",
            "active_dir": ev_dir,
            "active_score": round(ev_score, 3),
            "legacy_model": "MACD+BBI+EMA",
            "legacy_dir": legacy_dir,
            "legacy_score": round(legacy_score, 3),
            "agility_new": round(agility_new, 3),
            "agility_old": round(agility_old, 3),
            "flow_align_new": align_new,
            "flow_align_old": align_old,
            "winner": winner,
        }

        # 构建日志原因
        comp_str = ",".join([f"{k}:{v:+.2f}" for k, v in lw_result["components"].items()])
        direction_reason = (
            f"dir_lw={lw_dir[:4]} score_lw={lw_score:+.2f} | "
            f"dir_ev={ev_dir[:4]} score_ev={ev_score:+.2f} | "
            f"legacy={legacy_dir[:4]}({legacy_score:+.2f}) | "
            f"agree={1 if agree else 0} div={divergence:.2f} conf={abs(ev_score):.2f} | "
            f"winner={winner} agile_new={agility_new:.2f}/{agility_old:.2f} | "
            f"components({comp_str})"
        )

        # ========== 市场状态判断 ==========
        base_result = {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "adx": adx,
            "atr_pct": atr_pct,
            "last_open": last_open,
            "last_close": last_close,
            "lw": lw_result,
            "ev": ev_result,
            "final": {
                "dir": direction,
                "score": ev_score,
                "method": "EV_PRIMARY",
                "need_confirm": need_confirm,
            },
            # 兼容旧接口
            "ev_direction": ev_dir,
            "ev_score": ev_score,
            "lw_direction": lw_dir,
            "lw_score": lw_score,
            "lw_components": lw_result["components"],
            "legacy_direction": legacy_dir,
            "legacy_score": legacy_score,
            "combo_compare": combo_compare,
        }

        if atr_pct < self.regime_atr_pct_min:
            return {
                **base_result,
                "regime": "NO_TRADE",
                "direction": "BOTH",
                "reason": f"atr_pct_low({atr_pct:.4f}<{self.regime_atr_pct_min:.4f}) {direction_reason}",
            }
        if atr_pct > self.regime_atr_pct_max:
            return {
                **base_result,
                "regime": "NO_TRADE",
                "direction": "BOTH",
                "reason": f"atr_pct_high({atr_pct:.4f}>{self.regime_atr_pct_max:.4f}) {direction_reason}",
            }

        if adx >= self.regime_adx_trend_on:
            return {
                **base_result,
                "regime": "TREND",
                "direction": direction,
                "reason": f"adx_trend({adx:.1f}) {direction_reason}",
            }
        if adx <= self.regime_adx_range_on:
            return {
                **base_result,
                "regime": "RANGE",
                "direction": "BOTH",
                "reason": f"adx_range({adx:.1f}) {direction_reason}",
            }

        if self.regime_no_trade_low < adx < self.regime_no_trade_high:
            reason = f"adx_no_trade({adx:.1f}) {direction_reason}"
        else:
            reason = f"adx_mid({adx:.1f}) {direction_reason}"
        return {
            **base_result,
            "regime": "NO_TRADE",
            "direction": "BOTH",
            "reason": reason,
        }

    def _should_apply_direction_lock(self, regime: str, direction: str, regime_info: Dict[str, Any]) -> bool:
        if regime != "TREND":
            return False
        if direction not in ("LONG_ONLY", "SHORT_ONLY"):
            return False
        if self.direction_lock_mode == "off":
            return False
        if self.direction_lock_mode == "hard":
            return True

        adx = self._to_float(regime_info.get("adx"), 0.0)
        ema_fast = self._to_float(regime_info.get("ema_fast"), 0.0)
        ema_slow = self._to_float(regime_info.get("ema_slow"), 0.0)
        denom = abs(ema_slow) if abs(ema_slow) > 1e-12 else 1.0
        ema_gap_pct = abs(ema_fast - ema_slow) / denom
        adx_strong = adx >= (self.regime_adx_trend_on + self.direction_lock_soft_adx_buffer)
        ema_clear = ema_gap_pct >= self.direction_lock_ema_band_pct
        return bool(adx_strong and ema_clear)

    def _extract_range_quantiles(self, market_flow_context: Dict[str, Any]) -> Dict[str, Any]:
        timeframes = market_flow_context.get("timeframes")
        if not isinstance(timeframes, dict):
            return {"ready": False, "reason": "missing_timeframes"}
        tf = str(self.range_quantile_timeframe or "5m")
        tf_ctx = timeframes.get(tf)
        if not isinstance(tf_ctx, dict):
            return {"ready": False, "reason": f"missing_{tf}_context"}
        q = tf_ctx.get("quantiles")
        if not isinstance(q, dict):
            return {"ready": False, "reason": "quantiles_missing"}
        if not bool(q.get("ready", False)):
            return {
                "ready": False,
                "reason": str(q.get("reason") or "quantiles_not_ready"),
                "n": int(self._to_float(q.get("n"), 0)),
            }
        values = q.get("values")
        if not isinstance(values, dict):
            return {"ready": False, "reason": "quantile_values_missing"}
        imb = values.get("imbalance") if isinstance(values.get("imbalance"), dict) else {}
        cvd = values.get("cvd_momentum") if isinstance(values.get("cvd_momentum"), dict) else {}
        if not imb or not cvd:
            return {"ready": False, "reason": "quantile_metric_missing"}
        micro_raw = values.get("micro_delta_last")
        micro: Dict[str, Any] = micro_raw if isinstance(micro_raw, dict) else {}
        phantom_raw = values.get("phantom_mean")
        phantom: Dict[str, Any] = phantom_raw if isinstance(phantom_raw, dict) else {}
        trap_raw = values.get("trap_last")
        trap: Dict[str, Any] = trap_raw if isinstance(trap_raw, dict) else {}
        trap_guard_raw = q.get("trap_guard")
        trap_guard_cfg = trap_guard_raw if isinstance(trap_guard_raw, dict) else {}
        trap_guard_enabled = bool(trap_guard_cfg.get("enabled", self.range_trap_guard_enabled))
        trap_guard_q = self._to_float(trap_guard_cfg.get("max_quantile"), self.range_trap_guard_max_quantile)
        trap_guard_q = min(0.95, max(0.50, trap_guard_q))
        trap_guard = self._to_optional_float(trap.get("guard")) if trap else None
        if trap_guard is None:
            trap_guard = self._to_optional_float(trap.get("hi")) if trap else None
        return {
            "ready": True,
            "n": int(self._to_float(q.get("n"), 0)),
            "imb_hi": self._to_float(imb.get("hi"), 0.0),
            "imb_lo": self._to_float(imb.get("lo"), 0.0),
            "cvd_hi": self._to_float(cvd.get("hi"), 0.0),
            "cvd_lo": self._to_float(cvd.get("lo"), 0.0),
            "micro_hi": self._to_float(micro.get("hi"), 0.0),
            "micro_lo": self._to_float(micro.get("lo"), 0.0),
            "phantom_hi": self._to_float(phantom.get("hi"), 0.0),
            "phantom_lo": self._to_float(phantom.get("lo"), 0.0),
            "trap_hi": self._to_float(trap.get("hi"), 0.0),
            "trap_lo": self._to_float(trap.get("lo"), 0.0),
            "trap_guard_enabled": trap_guard_enabled,
            "trap_guard_max_quantile": trap_guard_q,
            "trap_guard": trap_guard,
            "raw": q,
        }

    def _extract_range_turn_values(self, market_flow_context: Dict[str, Any]) -> Dict[str, Optional[float]]:
        timeframes = market_flow_context.get("timeframes")
        tf_ctx: Dict[str, Any] = {}
        if isinstance(timeframes, dict):
            raw_tf = timeframes.get(self.range_quantile_timeframe)
            if isinstance(raw_tf, dict):
                tf_ctx = raw_tf
        prev_raw = tf_ctx.get("prev")
        prev: Dict[str, Any] = prev_raw if isinstance(prev_raw, dict) else {}
        prev2_raw = tf_ctx.get("prev2")
        prev2: Dict[str, Any] = prev2_raw if isinstance(prev2_raw, dict) else {}
        cvd0 = self._to_optional_float(
            market_flow_context.get("cvd_momentum")
            if market_flow_context.get("cvd_momentum") is not None
            else tf_ctx.get("cvd_momentum")
        )
        micro0 = self._to_optional_float(
            market_flow_context.get("micro_delta_last")
            if market_flow_context.get("micro_delta_last") is not None
            else market_flow_context.get("micro_delta_norm")
            if market_flow_context.get("micro_delta_norm") is not None
            else tf_ctx.get("micro_delta_last")
        )
        phantom0 = self._to_optional_float(
            market_flow_context.get("phantom_mean")
            if market_flow_context.get("phantom_mean") is not None
            else market_flow_context.get("phantom")
            if market_flow_context.get("phantom") is not None
            else tf_ctx.get("phantom_mean")
        )
        trap0 = self._to_optional_float(
            market_flow_context.get("trap_last")
            if market_flow_context.get("trap_last") is not None
            else market_flow_context.get("trap_score")
            if market_flow_context.get("trap_score") is not None
            else tf_ctx.get("trap_last")
        )
        cvd1 = self._to_optional_float(prev.get("cvd_momentum"))
        cvd2 = self._to_optional_float(prev2.get("cvd_momentum"))
        micro1 = self._to_optional_float(prev.get("micro_delta_last"))
        micro2 = self._to_optional_float(prev2.get("micro_delta_last"))
        phantom1 = self._to_optional_float(prev.get("phantom_mean"))
        trap1 = self._to_optional_float(prev.get("trap_last"))
        return {
            "cvd0": cvd0,
            "cvd1": cvd1,
            "cvd2": cvd2,
            "micro0": micro0,
            "micro1": micro1,
            "micro2": micro2,
            "phantom0": phantom0,
            "phantom1": phantom1,
            "trap0": trap0,
            "trap1": trap1,
        }

    def _evaluate_range_turn_confirm(
        self,
        turn_values: Dict[str, Optional[float]],
    ) -> Dict[str, Any]:
        mode = str(self.range_turn_confirm_mode or "2bar_peak_valley")
        min_delta = max(0.0, float(self.range_turn_confirm_min_delta))
        min_pass_count = max(1, int(self.range_turn_min_pass_count))

        def _eval_peak_valley(v0: Optional[float], v1: Optional[float], v2: Optional[float], label: str) -> Dict[str, Any]:
            if v0 is None or v1 is None:
                return {"enabled": True, "ready": False, "turned_up": False, "turned_down": False, "reason": f"{label}_missing_prev"}
            delta_up = float(v0 - v1)
            delta_down = float(v1 - v0)
            if mode == "1bar":
                return {
                    "enabled": True,
                    "ready": True,
                    "delta_up": delta_up,
                    "delta_down": delta_down,
                    "turned_up": bool((v0 > v1) and (delta_up >= min_delta)),
                    "turned_down": bool((v0 < v1) and (delta_down >= min_delta)),
                    "reason": "ok",
                }
            if v2 is None:
                return {"enabled": True, "ready": False, "turned_up": False, "turned_down": False, "reason": f"{label}_missing_prev2"}
            return {
                "enabled": True,
                "ready": True,
                "delta_up": delta_up,
                "delta_down": delta_down,
                "turned_up": bool((v0 > v1) and (v1 < v2) and (delta_up >= min_delta)),
                "turned_down": bool((v0 < v1) and (v1 > v2) and (delta_down >= min_delta)),
                "reason": "ok",
            }

        def _eval_decay(v0: Optional[float], v1: Optional[float], label: str) -> Dict[str, Any]:
            if v0 is None or v1 is None:
                return {"enabled": True, "ready": False, "turned_up": False, "turned_down": False, "reason": f"{label}_missing_prev"}
            decayed = bool(v0 < v1)
            return {"enabled": True, "ready": True, "turned_up": decayed, "turned_down": decayed, "reason": "ok"}

        if not bool(self.range_turn_confirm_enabled):
            return {
                "enabled": False,
                "ready": True,
                "mode": mode,
                "min_delta": min_delta,
                "min_pass_count": min_pass_count,
                "pass_count_long": min_pass_count,
                "pass_count_short": min_pass_count,
                "turned_up": True,
                "turned_down": True,
                "reason": "turn_confirm_disabled",
            }

        cvd_eval = _eval_peak_valley(turn_values.get("cvd0"), turn_values.get("cvd1"), turn_values.get("cvd2"), "cvd")
        micro_eval = (
            _eval_peak_valley(turn_values.get("micro0"), turn_values.get("micro1"), turn_values.get("micro2"), "micro")
            if bool(self.range_turn_micro_enabled)
            else {"enabled": False, "ready": True, "turned_up": False, "turned_down": False, "reason": "micro_disabled"}
        )
        phantom_eval = (
            _eval_decay(turn_values.get("phantom0"), turn_values.get("phantom1"), "phantom")
            if bool(self.range_turn_phantom_decay_enabled)
            else {"enabled": False, "ready": True, "turned_up": False, "turned_down": False, "reason": "phantom_disabled"}
        )
        trap_eval = (
            _eval_decay(turn_values.get("trap0"), turn_values.get("trap1"), "trap")
            if bool(self.range_turn_trap_decay_enabled)
            else {"enabled": False, "ready": True, "turned_up": False, "turned_down": False, "reason": "trap_disabled"}
        )
        components = [("cvd", cvd_eval), ("micro", micro_eval), ("phantom", phantom_eval), ("trap", trap_eval)]
        enabled_components = [item for item in components if bool(item[1].get("enabled", False))]
        required_passes = max(1, min(min_pass_count, len(enabled_components))) if enabled_components else 1
        ready = all(bool(comp.get("ready", False)) for _, comp in enabled_components) if enabled_components else True
        pass_count_long = sum(1 for _, comp in enabled_components if bool(comp.get("turned_up", False)))
        pass_count_short = sum(1 for _, comp in enabled_components if bool(comp.get("turned_down", False)))
        turned_up = bool(ready and (pass_count_long >= required_passes))
        turned_down = bool(ready and (pass_count_short >= required_passes))
        if not ready:
            reason = next((str(comp.get("reason")) for _, comp in enabled_components if not bool(comp.get("ready", False))), "turn_not_ready")
        elif not turned_up and not turned_down:
            reason = "turn_pass_count_insufficient"
        else:
            reason = "ok"
        return {
            "enabled": True,
            "ready": ready,
            "mode": mode,
            "min_delta": min_delta,
            "min_pass_count": required_passes,
            "pass_count_long": pass_count_long,
            "pass_count_short": pass_count_short,
            "turned_up": turned_up,
            "turned_down": turned_down,
            "reason": reason,
            "cvd": cvd_eval,
            "micro": micro_eval,
            "phantom": phantom_eval,
            "trap": trap_eval,
        }

    def decide(
        self,
        symbol: str,
        portfolio: Dict[str, Any],
        price: float,
        market_flow_context: Dict[str, Any],
        trigger_context: Optional[Dict[str, Any]] = None,
        use_weight_router: bool = True,
        use_ai_weights: bool = True,
    ) -> FundFlowDecision:
        _ = portfolio
        trigger_context = trigger_context or {}
        
        # 1. 检测市场状态
        regime_info = self._detect_regime(market_flow_context or {})
        regime = str(regime_info.get("regime", "NO_TRADE")).upper()
        direction = str(regime_info.get("direction", "BOTH")).upper()
        
        # 2. 获取引擎参数
        engine_params = self._engine_params_for(regime if regime in ("TREND", "RANGE") else "TREND")
        
        # 3. 获取动态权重（可按调用场景禁用）
        range_quantiles = self._extract_range_quantiles(market_flow_context or {}) if regime == "RANGE" else {"ready": False}
        use_router_runtime = bool(use_weight_router and self.deepseek_router.enabled)
        if use_router_runtime:
            weight_map = self.deepseek_router.get_weights(
                symbol=symbol,
                regime=regime,
                market_flow_context=market_flow_context or {},
                quantile_context=range_quantiles if regime == "RANGE" else None,
                use_ai=bool(use_ai_weights),
            )
        else:
            weight_map = WeightMap(confidence=0.0, reason="weight_router_bypassed")
        
        # 4. 计算 15m 主资金分数
        tf_15m_ctx = self._extract_15m_context(market_flow_context or {})
        if use_router_runtime and tf_15m_ctx:
            score_15m = self._score_with_weights(tf_15m_ctx, weight_map, regime)
        else:
            score_15m = self._score_trend(tf_15m_ctx) if regime == "TREND" else self._score_range(tf_15m_ctx)
        
        # 5. 计算 5m 执行分数
        tf_5m_ctx = self._extract_5m_context(market_flow_context or {})
        if tf_5m_ctx:
            if use_router_runtime:
                score_5m = self._score_with_weights(tf_5m_ctx, weight_map, regime)
            else:
                score_5m = self._score_trend(tf_5m_ctx) if regime == "TREND" else self._score_range(tf_5m_ctx)
        else:
            # 回退到使用当前上下文
            if use_router_runtime:
                score_5m = self._score_with_weights(market_flow_context or {}, weight_map, regime)
            else:
                score_5m = self._score_trend(market_flow_context or {}) if regime == "TREND" else self._score_range(market_flow_context or {})
        
        # 6. 融合 15m + 5m 分数
        fused = self._fuse_scores(symbol, score_15m, score_5m, regime)
        long_score = fused["long_score"]
        short_score = fused["short_score"]
        
        # 7. 记录 15m 分数历史
        self._record_15m_score(symbol, score_15m, regime)
        
        # 8. 计算 flow_confirm 和 consistency_3bars（资金流 3.0 关键输入）
        flow_confirm, consistency_3bars = self._compute_flow_consistency(
            market_flow_context or {}, tf_15m_ctx, tf_5m_ctx
        )
        
        # 兼容旧逻辑的趋势分数
        trend_score = self._score_trend(market_flow_context or {})
        
        close_threshold = float(engine_params.get("close_threshold", self.close_threshold))
        current_pos = (portfolio.get("positions") or {}).get(symbol)
        pos_side = str((current_pos or {}).get("side", "")).upper()

        metadata_base = {
            "trigger": trigger_context,
            "engine": regime,
            "regime": regime,
            "regime_reason": regime_info.get("reason"),
            "regime_adx": regime_info.get("adx"),
            "regime_atr_pct": regime_info.get("atr_pct"),
            "last_open": regime_info.get("last_open", 0.0),
            "last_close": regime_info.get("last_close", 0.0),
            # 冲突保护所需字段
            "macd_hist_norm": regime_info.get("lw", {}).get("components", {}).get("macd", 0.0) if regime_info.get("lw") else 0.0,
            "cvd_norm": regime_info.get("lw", {}).get("components", {}).get("cvd", 0.0) if regime_info.get("lw") else 0.0,
            "direction_lock": direction,
            "direction_lock_mode": self.direction_lock_mode,
            "direction_lock_ema_band_pct": self.direction_lock_ema_band_pct,
            "direction_lock_soft_adx_buffer": self.direction_lock_soft_adx_buffer,
            "ev_direction": regime_info.get("ev_direction", "BOTH"),
            "ev_score": regime_info.get("ev_score", 0.0),
            "lw_direction": regime_info.get("lw_direction", "BOTH"),
            "lw_score": regime_info.get("lw_score", 0.0),
            "legacy_direction": regime_info.get("legacy_direction", "BOTH"),
            "legacy_score": regime_info.get("legacy_score", 0.0),
            "combo_compare": regime_info.get("combo_compare", {}),
            "selected_pool_id": engine_params.get("signal_pool_id"),
            "signal_pool_id": engine_params.get("signal_pool_id"),
            "params_override": engine_params,
            "close_threshold": close_threshold,
            "range_quantile_ready": bool(range_quantiles.get("ready", False)),
            "range_quantile_n": range_quantiles.get("n"),
            "range_turn_confirm": {
                "enabled": bool(self.range_turn_confirm_enabled),
                "mode": self.range_turn_confirm_mode,
                "min_delta": self.range_turn_confirm_min_delta,
                "micro_turn_enabled": bool(self.range_turn_micro_enabled),
                "phantom_decay_enabled": bool(self.range_turn_phantom_decay_enabled),
                "trap_decay_enabled": bool(self.range_turn_trap_decay_enabled),
                "min_pass_count": int(self.range_turn_min_pass_count),
            },
            "range_trap_guard": {
                "enabled": bool(self.range_trap_guard_enabled),
                "max_quantile": float(self.range_trap_guard_max_quantile),
            },
            # 资金流 3.0 新增字段
            "score_15m": score_15m,
            "score_5m": score_5m,
            "final_score": {"long_score": long_score, "short_score": short_score},
            "ds_confidence": weight_map.confidence if use_router_runtime else 0.0,
            "ds_source": weight_map.reason if use_router_runtime else "local_only",
            "ds_weights_snapshot": weight_map.to_dict() if use_router_runtime else {},
            "weight_router_runtime_enabled": use_router_runtime,
            "ai_weights_runtime_enabled": bool(use_router_runtime and use_ai_weights and self.deepseek_router.ai_enabled),
            "fusion_info": {
                "enabled": self.score_fusion_enabled,
                "score_15m_weight": self.score_15m_weight,
                "score_5m_weight": self.score_5m_weight,
                "consistency_weight": fused.get("consistency_weight", 1.0),
            },
            # 资金流 3.0 一致性指标
            "flow_confirm": flow_confirm,
            "consistency_3bars": consistency_3bars,
        }

        if pos_side == "LONG" and short_score >= close_threshold:
            return FundFlowDecision(
                operation=Operation.CLOSE,
                symbol=symbol,
                target_portion_of_balance=1.0,
                leverage=int(engine_params.get("default_leverage", self.default_leverage)),
                max_price=price * 1.001,
                reason=f"{regime}反转平多, short_score={short_score:.3f}>=close={close_threshold:.3f}",
                metadata={**metadata_base, "long_score": long_score, "short_score": short_score},
            )
        if pos_side == "SHORT" and long_score >= close_threshold:
            return FundFlowDecision(
                operation=Operation.CLOSE,
                symbol=symbol,
                target_portion_of_balance=1.0,
                leverage=int(engine_params.get("default_leverage", self.default_leverage)),
                min_price=price * 0.999,
                reason=f"{regime}反转平空, long_score={long_score:.3f}>=close={close_threshold:.3f}",
                metadata={**metadata_base, "long_score": long_score, "short_score": short_score},
            )

        if regime == "NO_TRADE":
            return FundFlowDecision(
                operation=Operation.HOLD,
                symbol=symbol,
                target_portion_of_balance=0.0,
                leverage=int(engine_params.get("default_leverage", self.default_leverage)),
                reason=f"regime_no_trade: {regime_info.get('reason')}, long={long_score:.3f} short={short_score:.3f}",
                metadata={**metadata_base, "long_score": long_score, "short_score": short_score, "trend_score": trend_score},
            )

        long_threshold = float(engine_params.get("long_open_threshold", self.long_open_threshold))
        short_threshold = float(engine_params.get("short_open_threshold", self.short_open_threshold))
        min_lev = int(engine_params.get("min_leverage", self.min_leverage))
        max_lev = int(engine_params.get("max_leverage", self.max_leverage))
        default_lev = int(engine_params.get("default_leverage", self.default_leverage))
        target_portion = float(engine_params.get("default_target_portion", self.default_portion))
        take_profit_pct = self._normalize_pct_ratio(engine_params.get("take_profit_pct"), self.take_profit_pct)
        stop_loss_pct = self._normalize_pct_ratio(engine_params.get("stop_loss_pct"), self.stop_loss_pct)
        tp_enabled = take_profit_pct > 0
        sl_enabled = stop_loss_pct > 0
        tp_long_price = price * (1.0 + take_profit_pct) if tp_enabled else None
        sl_long_price = price * (1.0 - stop_loss_pct) if sl_enabled else None
        tp_short_price = price * (1.0 - take_profit_pct) if tp_enabled else None
        sl_short_price = price * (1.0 + stop_loss_pct) if sl_enabled else None

        if regime == "RANGE":
            if not bool(range_quantiles.get("ready", False)):
                return FundFlowDecision(
                    operation=Operation.HOLD,
                    symbol=symbol,
                    target_portion_of_balance=0.0,
                    leverage=default_lev,
                    reason=f"range_quantile_not_ready:{range_quantiles.get('reason')}, n={range_quantiles.get('n')}",
                    metadata={**metadata_base, "long_score": long_score, "short_score": short_score, "range_quantiles": range_quantiles},
                )
            imb = self._to_float(market_flow_context.get("imbalance"), 0.0)
            cvd_mom = self._to_float(market_flow_context.get("cvd_momentum"), 0.0)
            oi_delta = self._to_float(market_flow_context.get("oi_delta_ratio"), 0.0)
            imb_hi = self._to_float(range_quantiles.get("imb_hi"), 0.0)
            imb_lo = self._to_float(range_quantiles.get("imb_lo"), 0.0)
            cvd_hi = self._to_float(range_quantiles.get("cvd_hi"), 0.0)
            cvd_lo = self._to_float(range_quantiles.get("cvd_lo"), 0.0)

            long_extreme = imb <= imb_lo and cvd_mom <= cvd_lo
            short_extreme = imb >= imb_hi and cvd_mom >= cvd_hi
            oi_abs_max = self._to_float(engine_params.get("range_oi_delta_abs_max"), 0.0)
            if oi_abs_max > 0 and abs(oi_delta) > oi_abs_max:
                long_extreme = False
                short_extreme = False

            turn_values = self._extract_range_turn_values(market_flow_context or {})
            turn_eval = self._evaluate_range_turn_confirm(turn_values)
            long_turn_ok = bool(turn_eval.get("turned_up", False))
            short_turn_ok = bool(turn_eval.get("turned_down", False))
            trap_last = self._to_optional_float(turn_values.get("trap0"))
            trap_guard_enabled = bool(range_quantiles.get("trap_guard_enabled", self.range_trap_guard_enabled))
            trap_guard = self._to_optional_float(range_quantiles.get("trap_guard"))
            trap_guard_ok = True
            trap_guard_reason = "trap_guard_disabled"
            if trap_guard_enabled:
                if trap_guard is None:
                    trap_guard_ok = False
                    trap_guard_reason = "trap_guard_missing"
                elif trap_last is None:
                    trap_guard_ok = False
                    trap_guard_reason = "trap_last_missing"
                else:
                    trap_guard_ok = bool(trap_last <= trap_guard)
                    trap_guard_reason = (
                        "trap_guard_ok" if trap_guard_ok else f"trap_guard_blocked({float(trap_last):.6f}>{float(trap_guard):.6f})"
                    )
            long_signal = bool(long_extreme and long_turn_ok and trap_guard_ok)
            short_signal = bool(short_extreme and short_turn_ok and trap_guard_ok)

            score_out = {"long_score": long_score, "short_score": short_score}
            cvd0_v = turn_values.get("cvd0")
            cvd1_v = turn_values.get("cvd1")
            cvd2_v = turn_values.get("cvd2")
            micro0_v = turn_values.get("micro0")
            micro1_v = turn_values.get("micro1")
            micro2_v = turn_values.get("micro2")
            phantom0_v = turn_values.get("phantom0")
            phantom1_v = turn_values.get("phantom1")
            trap0_v = turn_values.get("trap0")
            trap1_v = turn_values.get("trap1")
            cvd0_txt = "NA" if cvd0_v is None else f"{float(cvd0_v):.6f}"
            cvd1_txt = "NA" if cvd1_v is None else f"{float(cvd1_v):.6f}"
            cvd2_txt = "NA" if cvd2_v is None else f"{float(cvd2_v):.6f}"
            micro0_txt = "NA" if micro0_v is None else f"{float(micro0_v):.6f}"
            micro1_txt = "NA" if micro1_v is None else f"{float(micro1_v):.6f}"
            micro2_txt = "NA" if micro2_v is None else f"{float(micro2_v):.6f}"
            phantom0_txt = "NA" if phantom0_v is None else f"{float(phantom0_v):.6f}"
            phantom1_txt = "NA" if phantom1_v is None else f"{float(phantom1_v):.6f}"
            trap0_txt = "NA" if trap0_v is None else f"{float(trap0_v):.6f}"
            trap1_txt = "NA" if trap1_v is None else f"{float(trap1_v):.6f}"
            range_meta = {
                "range_quantiles": range_quantiles,
                "range_current": {
                    "imbalance": imb,
                    "cvd_momentum": cvd_mom,
                    "oi_delta_ratio": oi_delta,
                    "micro_delta_last": micro0_v,
                    "phantom_mean": phantom0_v,
                    "trap_last": trap0_v,
                },
                "range_turn": {
                    **turn_eval,
                    "cvd0": cvd0_v,
                    "cvd1": cvd1_v,
                    "cvd2": cvd2_v,
                    "micro0": micro0_v,
                    "micro1": micro1_v,
                    "micro2": micro2_v,
                    "phantom0": phantom0_v,
                    "phantom1": phantom1_v,
                    "trap0": trap0_v,
                    "trap1": trap1_v,
                },
                "range_trap_guard": {
                    "enabled": trap_guard_enabled,
                    "ok": trap_guard_ok,
                    "reason": trap_guard_reason,
                    "value": trap_last,
                    "threshold": trap_guard,
                    "max_quantile": range_quantiles.get("trap_guard_max_quantile"),
                },
            }
            if long_signal and not short_signal:
                leverage = self._pick_leverage(
                    long_score,
                    long_threshold,
                    min_leverage=min_lev,
                    max_leverage=max_lev,
                    default_leverage=default_lev,
                )
                return FundFlowDecision(
                    operation=Operation.BUY,
                    symbol=symbol,
                    target_portion_of_balance=target_portion,
                    leverage=leverage,
                    max_price=price * (1.0 + self.entry_slippage),
                    take_profit_price=tp_long_price,
                    stop_loss_price=sl_long_price,
                    time_in_force=TimeInForce.IOC,
                    tp_execution=ExecutionMode.LIMIT,
                    sl_execution=ExecutionMode.LIMIT,
                    reason=(
                        f"RANGE_LONG ext+turn: imb={imb:.4f}<=qlo={imb_lo:.4f}, "
                        f"cvd={cvd_mom:.6f}<=qlo={cvd_lo:.6f}, "
                        f"turn_pass={turn_eval.get('pass_count_long')}/{turn_eval.get('min_pass_count')}, "
                        f"micro2={micro2_txt}, micro1={micro1_txt}, micro0={micro0_txt}, "
                        f"phantom1={phantom1_txt}, phantom0={phantom0_txt}, "
                        f"trap1={trap1_txt}, trap0={trap0_txt}, guard={trap_guard_reason}, "
                        f"cvd2={cvd2_txt}, cvd1={cvd1_txt}, cvd0={cvd0_txt}, n={range_quantiles.get('n')}"
                    ),
                    metadata={
                        **metadata_base,
                        **score_out,
                        **range_meta,
                        "open_thresholds": {"long": long_threshold, "short": short_threshold},
                        "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                        "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                    },
                )
            if short_signal and not long_signal:
                leverage = self._pick_leverage(
                    short_score,
                    short_threshold,
                    min_leverage=min_lev,
                    max_leverage=max_lev,
                    default_leverage=default_lev,
                )
                return FundFlowDecision(
                    operation=Operation.SELL,
                    symbol=symbol,
                    target_portion_of_balance=target_portion,
                    leverage=leverage,
                    min_price=price * (1.0 - self.entry_slippage),
                    take_profit_price=tp_short_price,
                    stop_loss_price=sl_short_price,
                    time_in_force=TimeInForce.IOC,
                    tp_execution=ExecutionMode.LIMIT,
                    sl_execution=ExecutionMode.LIMIT,
                    reason=(
                        f"RANGE_SHORT ext+turn: imb={imb:.4f}>=qhi={imb_hi:.4f}, "
                        f"cvd={cvd_mom:.6f}>=qhi={cvd_hi:.6f}, "
                        f"turn_pass={turn_eval.get('pass_count_short')}/{turn_eval.get('min_pass_count')}, "
                        f"micro2={micro2_txt}, micro1={micro1_txt}, micro0={micro0_txt}, "
                        f"phantom1={phantom1_txt}, phantom0={phantom0_txt}, "
                        f"trap1={trap1_txt}, trap0={trap0_txt}, guard={trap_guard_reason}, "
                        f"cvd2={cvd2_txt}, cvd1={cvd1_txt}, cvd0={cvd0_txt}, n={range_quantiles.get('n')}"
                    ),
                    metadata={
                        **metadata_base,
                        **score_out,
                        **range_meta,
                        "open_thresholds": {"long": long_threshold, "short": short_threshold},
                        "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                        "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                    },
                )

            return FundFlowDecision(
                operation=Operation.HOLD,
                symbol=symbol,
                target_portion_of_balance=0.0,
                leverage=default_lev,
                reason=(
                    f"RANGE等待ext+turn: imb={imb:.4f} q[{imb_lo:.4f},{imb_hi:.4f}], "
                    f"cvd={cvd_mom:.6f} q[{cvd_lo:.6f},{cvd_hi:.6f}], "
                    f"turn(mode={turn_eval.get('mode')}, ready={turn_eval.get('ready')}, "
                    f"up={long_turn_ok}, down={short_turn_ok}, "
                    f"pass_long={turn_eval.get('pass_count_long')}/{turn_eval.get('min_pass_count')}, "
                    f"pass_short={turn_eval.get('pass_count_short')}/{turn_eval.get('min_pass_count')}, "
                    f"micro2={micro2_txt}, micro1={micro1_txt}, micro0={micro0_txt}, "
                    f"phantom1={phantom1_txt}, phantom0={phantom0_txt}, "
                    f"trap1={trap1_txt}, trap0={trap0_txt}, guard={trap_guard_reason}, "
                    f"cvd2={cvd2_txt}, cvd1={cvd1_txt}, cvd0={cvd0_txt})"
                ),
                metadata={**metadata_base, **score_out, **range_meta},
            )

        direction_lock_applied = False
        if self._should_apply_direction_lock(regime, direction, regime_info):
            direction_lock_applied = True
            if direction == "LONG_ONLY":
                short_score = 0.0
            elif direction == "SHORT_ONLY":
                long_score = 0.0

        # ========== 方向一致性检查 ==========
        # 采用 EV 主导方向压制，LW 只做辅助（弱化但不反客为主）
        ev_score = self._to_float(regime_info.get("ev_score"), 0.0)
        ev_dir = str(regime_info.get("ev_direction", "BOTH")).upper()
        lw_score = self._to_float(regime_info.get("lw_score"), 0.0)
        lw_dir = str(regime_info.get("lw_direction", "BOTH")).upper()

        direction_conflict = False
        if ev_score < -0.05 and long_score > short_score:
            # EV 偏空，压制多头开仓
            long_score *= 0.2
            direction_conflict = True
        elif ev_score > 0.05 and short_score > long_score:
            # EV 偏多，压制空头开仓
            short_score *= 0.2
            direction_conflict = True

        # LW 仅作为辅助一致性过滤：当其与 EV 同向时保留；反向时轻度降权
        ev_long = ev_dir == "LONG_ONLY"
        ev_short = ev_dir == "SHORT_ONLY"
        lw_long = lw_dir == "LONG_ONLY"
        lw_short = lw_dir == "SHORT_ONLY"
        if ev_long and lw_short and abs(lw_score) >= 0.20:
            long_score *= 0.85
            direction_conflict = True
        elif ev_short and lw_long and abs(lw_score) >= 0.20:
            short_score *= 0.85
            direction_conflict = True

        # ========== 方向不明确时禁止开仓 ==========
        # 当 direction 为 BOTH 且主指标都失效时，禁止开新仓
        # 避免在没有明确方向判断的情况下盲目开仓
        primary_flat_lw = bool(regime_info.get("lw", {}).get("components", {}).get("primary_flat", False))
        primary_flat_ev = bool(regime_info.get("ev", {}).get("components", {}).get("primary_flat", False))
        primary_flat = bool(primary_flat_lw and primary_flat_ev)
        if direction == "BOTH" and primary_flat:
            # 主指标失效且方向不明确，禁止开仓
            return FundFlowDecision(
                operation=Operation.HOLD,
                symbol=symbol,
                target_portion_of_balance=0.0,
                leverage=default_lev,
                reason=f"{regime}方向不明确(主指标失效+direction=BOTH)，禁止开仓 long={long_score:.3f} short={short_score:.3f}",
                metadata={
                    **metadata_base,
                    "long_score": long_score,
                    "short_score": short_score,
                    "direction_lock_applied": direction_lock_applied,
                    "blocked_reason": "direction_unclear_primary_flat",
                },
            )

        score_out = {
            "long_score": long_score,
            "short_score": short_score,
            "direction_lock_applied": direction_lock_applied,
            "direction_conflict": direction_conflict,
        }

        if long_score >= long_threshold and long_score > short_score:
            leverage = self._pick_leverage(
                long_score,
                long_threshold,
                min_leverage=min_lev,
                max_leverage=max_lev,
                default_leverage=default_lev,
            )
            return FundFlowDecision(
                operation=Operation.BUY,
                symbol=symbol,
                target_portion_of_balance=target_portion,
                leverage=leverage,
                max_price=price * (1.0 + self.entry_slippage),
                take_profit_price=tp_long_price,
                stop_loss_price=sl_long_price,
                time_in_force=TimeInForce.IOC,
                tp_execution=ExecutionMode.LIMIT,
                sl_execution=ExecutionMode.LIMIT,
                reason=f"{regime}开多, long={long_score:.3f} short={short_score:.3f}",
                metadata={
                    **metadata_base,
                    **score_out,
                    "open_thresholds": {"long": long_threshold, "short": short_threshold},
                    "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                    "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                },
            )

        if short_score >= short_threshold and short_score > long_score:
            leverage = self._pick_leverage(
                short_score,
                short_threshold,
                min_leverage=min_lev,
                max_leverage=max_lev,
                default_leverage=default_lev,
            )
            return FundFlowDecision(
                operation=Operation.SELL,
                symbol=symbol,
                target_portion_of_balance=target_portion,
                leverage=leverage,
                min_price=price * (1.0 - self.entry_slippage),
                take_profit_price=tp_short_price,
                stop_loss_price=sl_short_price,
                time_in_force=TimeInForce.IOC,
                tp_execution=ExecutionMode.LIMIT,
                sl_execution=ExecutionMode.LIMIT,
                reason=f"{regime}开空, short={short_score:.3f} long={long_score:.3f}",
                metadata={
                    **metadata_base,
                    **score_out,
                    "open_thresholds": {"long": long_threshold, "short": short_threshold},
                    "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                    "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                },
            )

        return FundFlowDecision(
            operation=Operation.HOLD,
            symbol=symbol,
            target_portion_of_balance=0.0,
            leverage=default_lev,
            reason=f"{regime}信号不足 long={long_score:.3f} short={short_score:.3f}",
            metadata={**metadata_base, **score_out},
        )
