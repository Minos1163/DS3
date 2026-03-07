from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional, Tuple

from src.fund_flow.models import ExecutionMode, FundFlowDecision, Operation, TimeInForce
from src.fund_flow.deepseek_weight_router import DeepSeekWeightRouter, WeightMap
from src.fund_flow.weight_router import WeightRouter


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
        self.max_active_symbols = max(1, int(ff.get("max_active_symbols", 1) or 1))
        self.max_symbol_position_portion = max(
            self.default_portion,
            self._to_float(ff.get("max_symbol_position_portion"), max(self.default_portion, 0.1)),
        )
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
        self.trend_pending_adx_min = max(
            0.0,
            self._to_float(regime_cfg.get("trend_pending_adx_min"), 16.5),
        )
        self.trend_pending_adx_slope_min = max(
            0.0,
            self._to_float(regime_cfg.get("trend_pending_adx_slope_min"), 0.8),
        )
        self.trend_pending_ema_expand_min = max(
            0.0,
            self._to_float(regime_cfg.get("trend_pending_ema_expand_min"), 0.0),
        )
        trend_capture_cfg = ff.get("trend_capture", {}) if isinstance(ff.get("trend_capture"), dict) else {}
        self.trend_capture_enabled = bool(trend_capture_cfg.get("enabled", True))
        self.trend_capture_min_score = max(
            0.0,
            self._to_float(trend_capture_cfg.get("min_score"), 0.22),
        )
        self.trend_capture_min_gap = max(
            0.0,
            self._to_float(trend_capture_cfg.get("min_gap"), 0.05),
        )
        self.trend_capture_trial_position_mult = min(
            1.0,
            max(0.1, self._to_float(trend_capture_cfg.get("trial_position_mult"), 0.35)),
        )
        range_veto_cfg = ff.get("range_veto_by_trend", {}) if isinstance(ff.get("range_veto_by_trend"), dict) else {}
        self.range_veto_by_trend_enabled = bool(range_veto_cfg.get("enabled", True))
        self.range_veto_trend_pending_score = max(
            0.0,
            self._to_float(range_veto_cfg.get("trend_pending_score"), 0.18),
        )
        self.range_veto_adx_slope_min = max(
            0.0,
            self._to_float(range_veto_cfg.get("adx_slope_min"), 0.8),
        )
        self.range_veto_oi_price_align_min = min(
            1.0,
            max(0.0, self._to_float(range_veto_cfg.get("oi_price_align_min"), 0.55)),
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
        # 反转平仓降噪: 要求连续确认 + 分差过滤，避免单根K线噪音触发平仓
        self.reverse_close_confirm_bars = max(
            1, int(self._to_float(ff.get("reverse_close_confirm_bars"), 1))
        )
        self.reverse_close_score_buffer = max(
            0.0, self._to_float(ff.get("reverse_close_score_buffer"), 0.02)
        )
        self.reverse_close_min_gap = max(
            0.0, self._to_float(ff.get("reverse_close_min_gap"), 0.08)
        )
        self.reverse_close_no_trade_extra_bars = max(
            0, int(self._to_float(ff.get("reverse_close_no_trade_extra_bars"), 1))
        )
        self.reverse_close_require_direction_lock = bool(
            ff.get("reverse_close_require_direction_lock", False)
        )
        self._reverse_close_streak: Dict[Tuple[str, str], int] = {}
        
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
        score_15m_weight_raw = ff.get("score_15m_weight", fusion_cfg.get("score_15m_weight", 0.6))
        score_5m_weight_raw = ff.get("score_5m_weight", fusion_cfg.get("score_5m_weight"))
        self.score_15m_weight = max(0.0, min(1.0, self._to_float(score_15m_weight_raw, 0.6)))
        if score_5m_weight_raw is None:
            self.score_5m_weight = 1.0 - self.score_15m_weight
        else:
            self.score_5m_weight = max(0.0, min(1.0, self._to_float(score_5m_weight_raw, 1.0 - self.score_15m_weight)))
        self.consistency_window = max(1, min(5, int(self._to_float(fusion_cfg.get("consistency_window"), 3))))
        
        # 15m 分数历史缓存 (用于一致性加权)
        self._score_15m_history: Dict[str, Deque[Dict[str, Any]]] = {}
        self._history_max_seconds = 1800  # 30分钟
        self._trend_pending_state: Dict[str, Dict[str, float]] = {}

        # EV 可靠度跟踪器 (Beta-Binomial)
        # 每个指标维护 (alpha, beta)，提高初始可靠度以产生明确方向判断
        # 关键修复: 确保所有主指标的 reliability_factor > 0，避免EV输出为0
        self._ev_reliability: Dict[str, Tuple[float, float]] = {
            "macd": (16.0, 4.0),   # MACD 初始可靠度 0.80 -> reliability_factor=0.60
            "kdj": (15.0, 5.0),    # KDJ(J) 初始可靠度 0.75 -> reliability_factor=0.50
            "bb": (15.0, 5.0),     # Bollinger 初始可靠度 0.75 -> reliability_factor=0.50
            "cvd": (14.0, 6.0),    # CVD 初始可靠度 0.70 -> reliability_factor=0.40 (提升权重)
            "imbalance": (13.0, 7.0),  # imbalance 初始可靠度 0.65 -> reliability_factor=0.30 (提升权重)
        }
        # 方向判断阈值 - 降低阈值使EV能产生明确方向
        self._direction_neutral_zone = 0.02  # abs(score) < 0.02 视为 FLAT
        self._direction_conflict_penalty = 0.6  # MACD与CVD冲突时乘与此系数
        self._divergence_threshold = 0.15  # EV与LW分歧阈值

        # MACD 双组合参数（默认将 MACD+BB 用作开仓方向指导）
        combo_cfg_raw = ff.get("direction_combo", {})
        combo_cfg = combo_cfg_raw if isinstance(combo_cfg_raw, dict) else {}
        w_kdj_cfg_raw = combo_cfg.get("macd_kdj_weights", {})
        w_kdj_cfg = w_kdj_cfg_raw if isinstance(w_kdj_cfg_raw, dict) else {}
        w_bb_cfg_raw = combo_cfg.get("macd_bb_weights", {})
        w_bb_cfg = w_bb_cfg_raw if isinstance(w_bb_cfg_raw, dict) else {}
        self._combo_weights_macd_kdj: Dict[str, float] = {
            "macd": self._to_float(w_kdj_cfg.get("macd"), 0.40),
            "kdj": self._to_float(w_kdj_cfg.get("kdj"), 0.20),
            "macd_cross": self._to_float(w_kdj_cfg.get("macd_cross"), 0.14),
            "kdj_cross": self._to_float(w_kdj_cfg.get("kdj_cross"), 0.10),
            "macd_hist_mom": self._to_float(w_kdj_cfg.get("macd_hist_mom"), 0.10),
            "kdj_zone": self._to_float(w_kdj_cfg.get("kdj_zone"), 0.06),
        }
        self._combo_weights_macd_bb: Dict[str, float] = {
            "macd": self._to_float(w_bb_cfg.get("macd"), 0.40),
            "bb": self._to_float(w_bb_cfg.get("bb"), 0.20),
            "macd_cross": self._to_float(w_bb_cfg.get("macd_cross"), 0.14),
            "bb_break": self._to_float(w_bb_cfg.get("bb_break"), 0.10),
            "bb_trend": self._to_float(w_bb_cfg.get("bb_trend"), 0.10),
            "macd_hist_mom": self._to_float(w_bb_cfg.get("macd_hist_mom"), 0.06),
        }
        self._combo_bb_squeeze_penalty = max(
            0.20,
            min(1.0, self._to_float(combo_cfg.get("bb_squeeze_penalty"), 0.72)),
        )
        self._combo_align_bonus = max(
            0.0,
            min(0.30, self._to_float(combo_cfg.get("align_bonus"), 0.05)),
        )

        guide_cfg_raw = ff.get("direction_guide", ff.get("macd_bb_direction_guide", {}))
        guide_cfg = guide_cfg_raw if isinstance(guide_cfg_raw, dict) else {}
        self._direction_guide_enabled = bool(guide_cfg.get("enabled", True))
        # 修改默认: 改为 MACD+KDJ 作为主方向指导 (符合MACD+KDJ组合技巧)
        self._direction_guide_model = self._normalize_direction_guide_model(
            guide_cfg.get("model", "MACD_KDJ")  # 默认改为 MACD+KDJ
        )
        
        # ====== 新增: MACD+KDJ+资金流混合配置 ======
        # 根据MACD+KDJ组合技巧:
        # 1. MACD主趋势: MACD>0看多, MACD<0看空
        # 2. KDJ辅买卖点: KDJ超卖(J<20)做多, KDJ超买(J>80)做空
        # 3. 资金流融合: CVD/imbalance 纳入核心评分
        hybrid_cfg_raw = ff.get("macd_kdj_fund_flow_hybrid", {})
        hybrid_cfg = hybrid_cfg_raw if isinstance(hybrid_cfg_raw, dict) else {}
        
        # MACD趋势权重 (主指标)
        self._macd_trend_weight = max(0.0, min(1.0, self._to_float(hybrid_cfg.get("macd_trend_weight"), 0.45)))
        # KDJ区间权重 (辅助指标)
        self._kdj_timing_weight = max(0.0, min(1.0, self._to_float(hybrid_cfg.get("kdj_timing_weight"), 0.25)))
        # 资金流权重 (融合进核心判断)
        self._fund_flow_weight = max(0.0, min(1.0, self._to_float(hybrid_cfg.get("fund_flow_weight"), 0.30)))
        
        # KDJ超买超卖阈值
        self._kdj_oversold_threshold = max(0.0, min(50.0, self._to_float(hybrid_cfg.get("kdj_oversold_threshold"), 25.0)))
        self._kdj_overbought_threshold = min(100.0, max(50.0, self._to_float(hybrid_cfg.get("kdj_overbought_threshold"), 75.0)))
        
        # MACD零轴附近阈值 (横盘判定)
        self._macd_zero_zone_threshold = max(0.0, min(0.1, self._to_float(hybrid_cfg.get("macd_zero_zone_threshold"), 0.02)))
        
        # 背离确认权重
        self._divergence_confirm_weight = max(0.0, min(1.0, self._to_float(hybrid_cfg.get("divergence_confirm_weight"), 0.15)))
        
        # 强趋势KDJ发散权重 (快线偏离慢线时的加分)
        self._kdj_divergence_bonus = max(0.0, min(0.5, self._to_float(hybrid_cfg.get("kdj_divergence_bonus"), 0.10)))
        guide_neutral_zone = self._to_float(guide_cfg.get("neutral_zone"), self._direction_neutral_zone)
        self._direction_guide_neutral_zone = max(0.0, min(0.20, guide_neutral_zone))
        
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

    def _trend_capture_config(self) -> Dict[str, Any]:
        root = getattr(self, "config", None) or {}
        ff = root.get("fund_flow", {}) if isinstance(root, dict) else {}
        regime = ff.get("regime", {}) if isinstance(ff.get("regime"), dict) else {}
        conf = ff.get("ma10_macd_confluence", {}) if isinstance(ff.get("ma10_macd_confluence"), dict) else {}
        gate = ff.get("pretrade_risk_gate", {}) if isinstance(ff.get("pretrade_risk_gate"), dict) else {}
        tc = ff.get("trend_capture", {}) if isinstance(ff.get("trend_capture"), dict) else {}
        range_veto_root = ff.get("range_veto_by_trend", {}) if isinstance(ff.get("range_veto_by_trend"), dict) else {}
        score_fusion = ff.get("score_fusion", {}) if isinstance(ff.get("score_fusion"), dict) else {}

        range_veto_enabled = tc.get("range_veto_by_trend_enabled")
        if range_veto_enabled is None:
            range_veto_enabled = range_veto_root.get("enabled", self.range_veto_by_trend_enabled)
        range_veto_pending_score = tc.get("range_veto_trend_pending_score")
        if range_veto_pending_score is None:
            range_veto_pending_score = range_veto_root.get("trend_pending_score", self.range_veto_trend_pending_score)
        range_veto_capture_score = tc.get("range_veto_trend_capture_score")
        if range_veto_capture_score is None:
            range_veto_capture_score = range_veto_root.get("trend_capture_score", self.trend_capture_min_score)

        score_15m_weight = ff.get("score_15m_weight", score_fusion.get("score_15m_weight", self.score_15m_weight))
        score_5m_weight = ff.get("score_5m_weight", score_fusion.get("score_5m_weight", self.score_5m_weight))

        return {
            "adx_trend_on": self._to_float(regime.get("adx_trend_on"), self.regime_adx_trend_on),
            "adx_range_on": self._to_float(regime.get("adx_range_on"), self.regime_adx_range_on),
            "adx_no_trade_low": self._to_float(regime.get("adx_no_trade_low"), self.regime_no_trade_low),
            "adx_no_trade_high": self._to_float(regime.get("adx_no_trade_high"), self.regime_no_trade_high),
            "atr_pct_min": self._to_float(regime.get("atr_pct_min"), self.regime_atr_pct_min),
            "atr_pct_max": self._to_float(regime.get("atr_pct_max"), self.regime_atr_pct_max),
            "long_open_threshold": self._to_float(ff.get("long_open_threshold"), self.long_open_threshold),
            "short_open_threshold": self._to_float(ff.get("short_open_threshold"), self.short_open_threshold),
            "score_15m_weight": self._to_float(score_15m_weight, self.score_15m_weight),
            "score_5m_weight": self._to_float(score_5m_weight, self.score_5m_weight),
            "tf_exec": str(conf.get("tf_exec", "5m")),
            "tf_anchor": str(conf.get("tf_anchor", "1h")),
            "entry_hard_filter": bool(conf.get("entry_hard_filter", True)),
            "entry_require_macd_trigger": bool(conf.get("entry_require_macd_trigger", False)),
            "entry_allow_macd_early": bool(conf.get("entry_allow_macd_early", True)),
            "entry_soft_penalty_macd_early": self._to_float(conf.get("entry_soft_penalty_macd_early"), 0.03),
            "entry_soft_penalty_no_macd": self._to_float(conf.get("entry_soft_penalty_no_macd"), 0.08),
            "entry_soft_penalty_no_kdj": self._to_float(conf.get("entry_soft_penalty_no_kdj"), 0.04),
            "trend_pending_adx_min": self._to_float(tc.get("trend_pending_adx_min", regime.get("trend_pending_adx_min")), self.trend_pending_adx_min),
            "trend_pending_adx_slope_min": self._to_float(tc.get("trend_pending_adx_slope_min", regime.get("trend_pending_adx_slope_min")), self.trend_pending_adx_slope_min),
            "trend_pending_ema_expand_min": self._to_float(tc.get("trend_pending_ema_expand_min", regime.get("trend_pending_ema_expand_min")), self.trend_pending_ema_expand_min),
            "trend_pending_min_score": self._to_float(tc.get("trend_pending_min_score"), 0.55),
            "trend_capture_enabled": bool(tc.get("enabled", self.trend_capture_enabled)),
            "trend_capture_min_score": self._to_float(tc.get("min_score"), self.trend_capture_min_score),
            "trend_capture_min_gap": self._to_float(tc.get("min_gap"), self.trend_capture_min_gap),
            "trend_capture_trial_position_mult": self._to_float(tc.get("trial_position_mult"), self.trend_capture_trial_position_mult),
            "trend_capture_confirm_position_mult": self._to_float(tc.get("confirm_position_mult"), 0.65),
            "trend_capture_trap_soft_max": self._to_float(tc.get("trap_soft_max"), 0.65),
            "trend_capture_phantom_soft_max": self._to_float(tc.get("phantom_soft_max"), 0.65),
            "trend_capture_spread_soft_max": self._to_float(tc.get("spread_soft_max"), 1.8),
            "range_veto_by_trend_enabled": bool(range_veto_enabled),
            "range_veto_trend_pending_score": self._to_float(range_veto_pending_score, self.range_veto_trend_pending_score),
            "range_veto_trend_capture_score": self._to_float(range_veto_capture_score, self.trend_capture_min_score),
            "pretrade_entry_threshold_std": self._to_float(gate.get("entry_threshold"), 0.25),
            "pretrade_entry_threshold_capture": self._to_float(gate.get("entry_threshold_capture"), 0.21),
            "pretrade_volatility_cap_std": self._to_float(gate.get("volatility_cap"), 0.012),
            "pretrade_volatility_cap_capture": self._to_float(gate.get("volatility_cap_capture"), 0.014),
        }

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

    @staticmethod
    def _normalize_direction_guide_model(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if raw in {"MACD_BB", "MACD+BB", "MACD_BOLL", "MACD_BOLLINGER"}:
            return "MACD_BB"
        if raw in {"MACD_KDJ", "MACD+KDJ"}:
            return "MACD_KDJ"
        if raw in {"EV_PRIMARY", "EV"}:
            return "EV_PRIMARY"
        return "MACD_BB"

    @staticmethod
    def _direction_from_score(score: float, neutral_zone: float) -> str:
        s = float(score)
        z = max(0.0, float(neutral_zone))
        if abs(s) < z:
            return "BOTH"
        return "LONG_ONLY" if s > 0 else "SHORT_ONLY"

    def get_direction_guide_snapshot(self) -> Dict[str, Any]:
        model_map = {
            "MACD_BB": "MACD+BB",
            "MACD_KDJ": "MACD+KDJ",
            "EV_PRIMARY": "EV主方向",
        }
        return {
            "enabled": bool(self._direction_guide_enabled),
            "model": str(self._direction_guide_model),
            "model_label": model_map.get(self._direction_guide_model, self._direction_guide_model),
            "neutral_zone": float(self._direction_guide_neutral_zone),
            "bb_squeeze_penalty": float(self._combo_bb_squeeze_penalty),
            "align_bonus": float(self._combo_align_bonus),
            "macd_kdj_weights": dict(self._combo_weights_macd_kdj),
            "macd_bb_weights": dict(self._combo_weights_macd_bb),
            # 新增: MACD+KDJ+资金流混合配置
            "hybrid_config": {
                "macd_trend_weight": float(self._macd_trend_weight),
                "kdj_timing_weight": float(self._kdj_timing_weight),
                "fund_flow_weight": float(self._fund_flow_weight),
                "kdj_oversold_threshold": float(self._kdj_oversold_threshold),
                "kdj_overbought_threshold": float(self._kdj_overbought_threshold),
                "macd_zero_zone_threshold": float(self._macd_zero_zone_threshold),
                "divergence_confirm_weight": float(self._divergence_confirm_weight),
                "kdj_divergence_bonus": float(self._kdj_divergence_bonus),
            },
        }

    def _clear_reverse_close_streak(self, symbol: str) -> None:
        self._reverse_close_streak.pop((symbol, "LONG"), None)
        self._reverse_close_streak.pop((symbol, "SHORT"), None)

    def _update_reverse_close_streak(self, symbol: str, pos_side: str, triggered: bool) -> int:
        key = (symbol, pos_side)
        if not triggered:
            self._reverse_close_streak.pop(key, None)
            return 0
        streak = int(self._reverse_close_streak.get(key, 0)) + 1
        self._reverse_close_streak[key] = streak
        return streak

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
                + weight_map.trend_oi_delta_weight * max(oi_delta, 0.0)
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
        15m 只保留为状态/诊断信息，5m 承担实际开仓触发分数。
        """
        fused_long = score_5m.get("long_score", 0.0)
        fused_short = score_5m.get("short_score", 0.0)
        primary_direction = "LONG" if fused_long > fused_short else "SHORT"

        return {
            "long_score": min(max(fused_long, 0.0), 1.0),
            "short_score": min(max(fused_short, 0.0), 1.0),
            "fusion_applied": False,
            "score_15m": score_15m,
            "score_5m": score_5m,
            "score_15m_weight": 0.0,
            "score_5m_weight": 1.0,
            "consistency_weight": 1.0,
            "primary_direction": primary_direction,
            "trigger_score_source": "5m_only",
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
            current_sign = 1 if current_cvd > 0 else (-1 if current_cvd < 0 else 0)
            
            for prev in prev_list:
                prev_cvd = self._to_float(prev.get("cvd_ratio", 
                               prev.get("cvd", 0.0)))
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
        # Use 2-arg round for static typing compatibility with float.
        lev = int(round(min_lev + strength * (max_lev - min_lev), 0))
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
            "max_symbol_position_portion": float(self.max_symbol_position_portion),
            "max_active_symbols": int(self.max_active_symbols),
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
        params["max_active_symbols"] = max(1, int(self._to_float(params.get("max_active_symbols"), self.max_active_symbols)))
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
        tp_levels_raw = params.get("take_profit_pct_levels")
        tp_levels: list[float] = []
        if isinstance(tp_levels_raw, list):
            for item in tp_levels_raw:
                v = self._normalize_pct_ratio(item, 0.0)
                if v > 0:
                    tp_levels.append(v)
        params["take_profit_pct_levels"] = tp_levels

        tp_reduce_raw = params.get("take_profit_reduce_pct_levels")
        tp_reduce_levels: list[float] = []
        if isinstance(tp_reduce_raw, list):
            for item in tp_reduce_raw:
                v = max(0.0, min(1.0, self._to_float(item, 0.0)))
                if v > 0:
                    tp_reduce_levels.append(v)
        params["take_profit_reduce_pct_levels"] = tp_reduce_levels

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

    def _build_tp_levels_metadata(
        self,
        *,
        price: float,
        direction: str,
        pct_levels: list[float],
        reduce_levels: list[float],
    ) -> list[Dict[str, float]]:
        if direction not in {"LONG", "SHORT"}:
            return []
        levels: list[Dict[str, float]] = []
        direction_mult = 1.0 if direction == "LONG" else -1.0
        for idx, lvl in enumerate(pct_levels):
            if idx >= len(reduce_levels):
                break
            levels.append(
                {
                    "price": price * (1.0 + direction_mult * lvl),
                    "reduce_pct": reduce_levels[idx],
                }
            )
        return levels

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
        # 新主判：MACD + KDJ 与 MACD + Bollinger 双组合

        # MACD hist
        macd_hist_norm = self._to_float(tf_ctx.get("macd_hist_norm"), 0.0)
        if abs(macd_hist_norm) < 1e-9:
            macd_hist_norm = self._to_float(tf_ctx.get("macd_5m_hist_norm"), 0.0)
        if abs(macd_hist_norm) < 1e-9:
            macd_hist_raw = self._to_float(tf_ctx.get("macd_5m_hist"), self._to_float(tf_ctx.get("macd_hist"), 0.0))
            macd_hist_norm = max(-1.0, min(1.0, macd_hist_raw / 0.003))
        features["macd"] = max(-1.0, min(1.0, macd_hist_norm))

        macd_cross = str(tf_ctx.get("macd_cross", tf_ctx.get("macd_5m_cross", "NONE"))).upper()
        macd_cross_bias = self._to_float(tf_ctx.get("macd_cross_bias"), 0.0)
        if abs(macd_cross_bias) < 1e-9:
            macd_cross_bias = 1.0 if macd_cross == "GOLDEN" else (-1.0 if macd_cross == "DEAD" else 0.0)
        features["macd_cross"] = max(-1.0, min(1.0, macd_cross_bias))

        macd_hist_delta = self._to_float(
            tf_ctx.get("macd_hist_delta"),
            self._to_float(tf_ctx.get("macd_5m_hist_delta"), 0.0),
        )
        if abs(macd_hist_delta) < 1e-9:
            if bool(tf_ctx.get("macd_5m_hist_expand_up", False)):
                macd_hist_delta = 1.0
            elif bool(tf_ctx.get("macd_5m_hist_expand_down", False)):
                macd_hist_delta = -1.0
        features["macd_hist_mom"] = max(-1.0, min(1.0, macd_hist_delta))

        # KDJ(J) 归一化值：优先读取 kdj_j_norm，回退使用 (J-50)/50
        kdj_j_norm = self._to_float(tf_ctx.get("kdj_j_norm"), 0.0)
        if abs(kdj_j_norm) < 1e-9:
            kdj_j = self._to_float(tf_ctx.get("kdj_j"), 50.0)
            kdj_j_norm = (kdj_j - 50.0) / 50.0
        features["kdj"] = max(-1.0, min(1.0, kdj_j_norm))
        kdj_cross = str(tf_ctx.get("kdj_cross", "NONE")).upper()
        kdj_cross_bias = self._to_float(tf_ctx.get("kdj_cross_bias"), 0.0)
        if abs(kdj_cross_bias) < 1e-9:
            kdj_cross_bias = 1.0 if kdj_cross == "GOLDEN" else (-1.0 if kdj_cross == "DEAD" else 0.0)
        features["kdj_cross"] = max(-1.0, min(1.0, kdj_cross_bias))
        kdj_zone = str(tf_ctx.get("kdj_zone", "MID")).upper()
        kdj_zone_bias = 0.0
        if kdj_zone == "LOW":
            kdj_zone_bias = 0.5
        elif kdj_zone == "HIGH":
            kdj_zone_bias = -0.5
        if abs(kdj_j_norm) > 0.7:
            kdj_zone_bias += -0.2 if kdj_j_norm > 0 else 0.2
        features["kdj_zone"] = max(-1.0, min(1.0, kdj_zone_bias))

        # Bollinger 方向特征：
        # 优先用上游归一化字段，回退使用 upper/lower/middle + close(mid_price/last_close)估算
        bb_pos_norm = self._to_float(tf_ctx.get("bb_pos_norm"), 0.0)
        if abs(bb_pos_norm) < 1e-9:
            bb_upper = self._to_float(tf_ctx.get("bb_upper"), 0.0)
            bb_lower = self._to_float(tf_ctx.get("bb_lower"), 0.0)
            bb_middle = self._to_float(tf_ctx.get("bb_middle"), 0.0)
            close_price = self._to_float(tf_ctx.get("last_close"), 0.0)
            if close_price <= 0:
                close_price = self._to_float(tf_ctx.get("mid_price"), 0.0)
            if bb_upper > bb_lower and close_price > 0:
                band_w = max(bb_upper - bb_lower, 1e-12)
                bb_pos_norm = (close_price - (bb_upper + bb_lower) * 0.5) / (band_w * 0.5)
            elif bb_middle > 0 and close_price > 0:
                bb_pos_norm = (close_price - bb_middle) / max(abs(bb_middle) * 0.02, 1e-12)
        bb_pos_norm = max(-1.0, min(1.0, bb_pos_norm))

        bb_width_norm = self._to_float(tf_ctx.get("bb_width_norm"), 0.0)
        if abs(bb_width_norm) < 1e-9:
            bb_upper = self._to_float(tf_ctx.get("bb_upper"), 0.0)
            bb_lower = self._to_float(tf_ctx.get("bb_lower"), 0.0)
            bb_middle = self._to_float(tf_ctx.get("bb_middle"), 0.0)
            if bb_upper > bb_lower and bb_middle > 0:
                bw = (bb_upper - bb_lower) / bb_middle
                # 带宽大说明趋势性更强，小带宽降权
                bb_width_norm = max(-1.0, min(1.0, (bw - 0.01) / 0.05))
        features["bb"] = max(-1.0, min(1.0, 0.75 * bb_pos_norm + 0.25 * bb_width_norm))
        bb_break_bias = self._to_float(tf_ctx.get("bb_break_bias"), 0.0)
        if abs(bb_break_bias) < 1e-9:
            bb_break = str(tf_ctx.get("bb_break", "NONE")).upper()
            if bb_break == "UPPER":
                bb_break_bias = 1.0
            elif bb_break == "LOWER":
                bb_break_bias = -1.0
        features["bb_break"] = max(-1.0, min(1.0, bb_break_bias))
        bb_trend_bias = self._to_float(tf_ctx.get("bb_trend_bias"), 0.0)
        if abs(bb_trend_bias) < 1e-9:
            bb_trend = str(tf_ctx.get("bb_trend", "MID")).upper()
            if bb_trend == "ALONG_UPPER":
                bb_trend_bias = 1.0
            elif bb_trend == "ALONG_LOWER":
                bb_trend_bias = -1.0
        features["bb_trend"] = max(-1.0, min(1.0, bb_trend_bias))
        features["bb_squeeze"] = 1.0 if bool(tf_ctx.get("bb_squeeze", False)) else 0.0

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

    def _score_macd_kdj_fund_flow_hybrid(
        self,
        features: Dict[str, float],
        macd_raw: float = 0.0,
        kdj_j_raw: float = 50.0,
    ) -> Dict[str, Any]:
        """MACD+KDJ+资金流混合评分方法 (V6核心)"""
        components: Dict[str, Any] = {}
        
        # 1. MACD趋势判断 (主指标)
        macd_trend = 1.0 if macd_raw > 0 else (-1.0 if macd_raw < 0 else 0.0)
        macd_in_zero_zone = abs(macd_raw) < self._macd_zero_zone_threshold
        
        if macd_in_zero_zone:
            regime = "RANGE"
            regime_bonus = 0.0
        elif macd_trend > 0:
            regime = "TREND_UP"
            regime_bonus = macd_trend * self._macd_trend_weight
        else:
            regime = "TREND_DOWN"
            regime_bonus = macd_trend * self._macd_trend_weight
        
        components["macd_trend"] = round(macd_trend, 3)
        components["macd_raw"] = round(macd_raw, 3)
        components["regime"] = regime
        
        # 2. KDJ区间判断 (辅助指标)
        kdj_oversold = self._kdj_oversold_threshold
        kdj_overbought = self._kdj_overbought_threshold
        
        if kdj_j_raw < kdj_oversold:
            kdj_entry_signal = "OVERSOLD"
            kdj_signal = -1.0 * (kdj_oversold - kdj_j_raw) / kdj_oversold
            kdj_signal = max(-1.0, min(-0.2, kdj_signal))
        elif kdj_j_raw > kdj_overbought:
            kdj_entry_signal = "OVERBROUGHT"
            kdj_signal = 1.0 * (kdj_j_raw - kdj_overbought) / (100 - kdj_overbought)
            kdj_signal = max(0.2, min(1.0, kdj_signal))
        else:
            kdj_entry_signal = "NEUTRAL"
            kdj_signal = (kdj_j_raw - 50.0) / 50.0 * 0.3
        
        kdj_cross = self._to_float(features.get("kdj_cross"), 0.0)
        kdj_divergence_bonus = 0.0
        if abs(kdj_cross) > 0.5:
            kdj_divergence_bonus = self._kdj_divergence_bonus * abs(kdj_cross)
        
        kdj_score = (kdj_signal + kdj_cross * 0.3 + kdj_divergence_bonus) * self._kdj_timing_weight
        
        components["kdj_j_raw"] = round(kdj_j_raw, 1)
        components["kdj_entry_signal"] = kdj_entry_signal
        components["kdj_signal"] = round(kdj_signal, 3)
        components["kdj_cross"] = round(kdj_cross, 3)
        
        # 3. 资金流融合
        cvd_val = self._to_float(features.get("cvd"), 0.0)
        imb_val = self._to_float(features.get("imbalance"), 0.0)
        fund_flow_score = (cvd_val + imb_val) / 2.0 * self._fund_flow_weight
        
        components["cvd"] = round(cvd_val, 3)
        components["imbalance"] = round(imb_val, 3)
        components["fund_flow_score"] = round(fund_flow_score, 3)
        
        # 4. 计算最终分数
        if regime == "RANGE":
            final_score = kdj_signal * 0.6 + kdj_cross * 0.2 + fund_flow_score * 0.2
        else:
            final_score = regime_bonus + kdj_score + fund_flow_score
        
        # 背离增强
        divergence_bonus = 0.0
        if regime == "TREND_UP" and kdj_entry_signal == "OVERSOLD":
            divergence_bonus = self._divergence_confirm_weight
        elif regime == "TREND_DOWN" and kdj_entry_signal == "OVERBROUGHT":
            divergence_bonus = -self._divergence_confirm_weight
        
        final_score += divergence_bonus
        components["divergence_bonus"] = round(divergence_bonus, 3)
        final_score = max(-1.0, min(1.0, final_score))
        
        # 5. 确定方向
        if abs(final_score) < self._direction_neutral_zone:
            direction = "BOTH"
        elif final_score > 0:
            direction = "LONG_ONLY"
        else:
            direction = "SHORT_ONLY"
        
        fund_flow_confirm = (cvd_val + imb_val) / 2.0
        components["final_score_raw"] = round(final_score, 3)
        
        return {
            "dir": direction,
            "score": round(final_score, 3),
            "components": components,
            "regime": regime,
            "kdj_entry_signal": kdj_entry_signal,
            "fund_flow_confirm": round(fund_flow_confirm, 3),
        }


    def _score_dual_combo(self, features: Dict[str, float]) -> Dict[str, Any]:
        """
        MACD+KDJ vs MACD+BB 双组合对比。
        返回 winner 及两者分数，供 LW/EV 共享。
        """
        macd_val = self._to_float(features.get("macd"), 0.0)
        kdj_val = self._to_float(features.get("kdj"), 0.0)
        bb_val = self._to_float(features.get("bb"), 0.0)
        macd_cross = self._to_float(features.get("macd_cross"), 0.0)
        macd_hist_mom = self._to_float(features.get("macd_hist_mom"), 0.0)
        kdj_cross = self._to_float(features.get("kdj_cross"), 0.0)
        kdj_zone = self._to_float(features.get("kdj_zone"), 0.0)
        bb_break = self._to_float(features.get("bb_break"), 0.0)
        bb_trend = self._to_float(features.get("bb_trend"), 0.0)
        bb_squeeze = self._to_float(features.get("bb_squeeze"), 0.0)
        cvd_val = self._to_float(features.get("cvd"), 0.0)

        w_kdj = self._combo_weights_macd_kdj
        w_bb = self._combo_weights_macd_bb

        # MACD+KDJ: 更偏向拐点确认（方向 + 交叉 + 区间）
        score_macd_kdj = (
            w_kdj.get("macd", 0.0) * macd_val
            + w_kdj.get("kdj", 0.0) * kdj_val
            + w_kdj.get("macd_cross", 0.0) * macd_cross
            + w_kdj.get("kdj_cross", 0.0) * kdj_cross
            + w_kdj.get("macd_hist_mom", 0.0) * macd_hist_mom
            + w_kdj.get("kdj_zone", 0.0) * kdj_zone
        )
        # MACD+BB: 更偏向趋势延续（方向 + 突破 + 轨道运行）
        score_macd_bb = (
            w_bb.get("macd", 0.0) * macd_val
            + w_bb.get("bb", 0.0) * bb_val
            + w_bb.get("macd_cross", 0.0) * macd_cross
            + w_bb.get("bb_break", 0.0) * bb_break
            + w_bb.get("bb_trend", 0.0) * bb_trend
            + w_bb.get("macd_hist_mom", 0.0) * macd_hist_mom
        )

        # 布林压缩区减少趋势分数，避免横盘误判
        squeeze_penalty_applied = False
        if bb_squeeze > 0.5:
            score_macd_bb *= self._combo_bb_squeeze_penalty
            squeeze_penalty_applied = True

        # 与 CVD 同向时略微加分（只用于组合优先级，不直接改方向）
        align_kdj = 1 if (score_macd_kdj * cvd_val > 0 and abs(cvd_val) > 0.05) else 0
        align_bb = 1 if (score_macd_bb * cvd_val > 0 and abs(cvd_val) > 0.05) else 0
        agility_kdj = abs(score_macd_kdj) + self._combo_align_bonus * align_kdj
        agility_bb = abs(score_macd_bb) + self._combo_align_bonus * align_bb

        winner = "MACD+KDJ" if agility_kdj >= agility_bb else "MACD+BB"
        winner_score = score_macd_kdj if winner == "MACD+KDJ" else score_macd_bb
        loser_score = score_macd_bb if winner == "MACD+KDJ" else score_macd_kdj

        # 同向时混合一部分 loser，减少抖动；反向时只用 winner
        mixed_score = winner_score
        if winner_score * loser_score > 0:
            mixed_score = 0.8 * winner_score + 0.2 * loser_score

        mixed_score = max(-1.0, min(1.0, mixed_score))
        score_macd_kdj = max(-1.0, min(1.0, score_macd_kdj))
        score_macd_bb = max(-1.0, min(1.0, score_macd_bb))

        return {
            "winner": winner,
            "winner_score": mixed_score,
            "score_macd_kdj": score_macd_kdj,
            "score_macd_bb": score_macd_bb,
            "align_kdj": align_kdj,
            "align_bb": align_bb,
            "feature_snapshot": {
                "macd": float(macd_val),
                "kdj": float(kdj_val),
                "bb": float(bb_val),
                "macd_cross": float(macd_cross),
                "macd_hist_mom": float(macd_hist_mom),
                "kdj_cross": float(kdj_cross),
                "kdj_zone": float(kdj_zone),
                "bb_break": float(bb_break),
                "bb_trend": float(bb_trend),
                "bb_squeeze": float(bb_squeeze),
            },
            "weights": {
                "macd_kdj": dict(w_kdj),
                "macd_bb": dict(w_bb),
            },
            "settings": {
                "bb_squeeze_penalty": float(self._combo_bb_squeeze_penalty),
                "align_bonus": float(self._combo_align_bonus),
                "neutral_zone": float(self._direction_neutral_zone),
                "squeeze_penalty_applied": bool(squeeze_penalty_applied),
            },
        }

    def _score_lw(
        self,
        features: Dict[str, float],
    ) -> Dict[str, Any]:
        """
        第二层A：线性加权法 (LW) - 当前决策用
        
        V6改进：使用 MACD+KDJ+资金流混合方法
        
        根据MACD+KDJ组合技巧:
        1. MACD主趋势: MACD>0看多, MACD<0看空
        2. KDJ辅买卖点: KDJ超卖(J<25)做多, KDJ超买(J>75)做空
        3. 资金流融合: CVD/imbalance 纳入核心评分

        Returns:
            {
                "dir": "LONG_ONLY"/"SHORT_ONLY"/"BOTH",
                "score": float,  # [-1, 1]
                "components": {"macd": ..., "kdj": ..., ...},
                "conflict": bool,
                "confirmation": float,  # 确认度 [-1, 1]
                "combo_compare": {...},
                "hybrid_result": {...},
            }
        """
        # 获取原始MACD和KDJ值用于混合方法
        macd_raw = self._to_float(features.get("macd"), 0.0) * 0.003  # 反归一化
        kdj_j_raw = self._to_float(features.get("kdj"), 0.0) * 50.0 + 50.0  # 反归一化
        
        # 调用新的MACD+KDJ+资金流混合方法
        hybrid_result = self._score_macd_kdj_fund_flow_hybrid(features, macd_raw, kdj_j_raw)
        
        components: Dict[str, Any] = {}
        combo_compare = self._score_dual_combo(features)
        lw_score = self._to_float(combo_compare.get("winner_score"), 0.0)
        components["combo_macd_kdj"] = round(self._to_float(combo_compare.get("score_macd_kdj"), 0.0), 3)
        components["combo_macd_bb"] = round(self._to_float(combo_compare.get("score_macd_bb"), 0.0), 3)
        components["combo_winner"] = 1.0 if str(combo_compare.get("winner")) == "MACD+KDJ" else -1.0
        components["macd"] = round(self._to_float(features.get("macd"), 0.0), 3)
        components["kdj"] = round(self._to_float(features.get("kdj"), 0.0), 3)
        components["bb"] = round(self._to_float(features.get("bb"), 0.0), 3)
        components["macd_cross"] = round(self._to_float(features.get("macd_cross"), 0.0), 3)
        components["kdj_cross"] = round(self._to_float(features.get("kdj_cross"), 0.0), 3)
        components["bb_break"] = round(self._to_float(features.get("bb_break"), 0.0), 3)

        # ========== 确认/否决指标 (CVD, imbalance) ==========
        # 不参与方向决定，只用于确认或否决
        cvd_val = features.get("cvd", 0.0)
        imb_val = features.get("imbalance", 0.0)

        # ========== 主指标失效检测 ==========
        # 当所有主指标都接近0时，启用CVD/imbalance作为备用方向判断
        primary_indicators_flat = (
            all(abs(features.get(k, 0.0)) < 0.05 for k in ("macd", "kdj", "bb"))
            and abs(features.get("macd_cross", 0.0)) < 0.5
            and abs(features.get("kdj_cross", 0.0)) < 0.5
            and abs(features.get("bb_break", 0.0)) < 0.5
        )

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

        # 使用混合方法的结果作为主要方向判断
        # 混合方法已经包含了MACD主趋势+KDJ辅买卖点+资金流融合
        final_direction = hybrid_result.get("dir", direction)
        final_score = hybrid_result.get("score", lw_score)
        
        # 将混合方法的结果也返回用于参考
        components["hybrid"] = hybrid_result.get("components", {})
        components["hybrid_regime"] = hybrid_result.get("regime", "UNKNOWN")
        components["kdj_entry_signal"] = hybrid_result.get("kdj_entry_signal", "NEUTRAL")
        
        return {
            "dir": final_direction,
            "score": round(final_score, 3),
            "components": components,
            "conflict": has_conflict,
            "confirmation": round(confirmation, 3),
            "combo_compare": combo_compare,
            "active_model": str(combo_compare.get("winner", "MACD+KDJ")),
            "hybrid_result": hybrid_result,
        }

    def _score_ev(
        self,
        features: Dict[str, float],
    ) -> Dict[str, Any]:
        """
        第二层B：期望值法 (EV) - 用于对比评估

        使用在线可靠度 (Beta-Binomial)：
        先计算可靠度加权后的 MACD/KDJ/BB，再做
        MACD+KDJ vs MACD+BB 双组合对比。

        注意：CVD/imbalance 作为确认项，不决定方向

        Returns:
            {
                "dir": "LONG_ONLY"/"SHORT_ONLY"/"BOTH",
                "score": float,
                "components": {...},
                "reliabilities": {"macd": p_macd, ...},
            }
        """
        components: Dict[str, Any] = {}
        reliabilities: Dict[str, float] = {}

        # ========== 可靠度加权后的主指标 ==========
        reliability_weighted: Dict[str, float] = {}
        for key in ("macd", "kdj", "bb"):
            val = features.get(key, 0.0)

            # 获取该指标的可靠度
            alpha, beta = self._ev_reliability.get(key, (10.0, 10.0))
            p_i = alpha / (alpha + beta)  # 可靠度 [0, 1]
            reliabilities[key] = round(p_i, 3)

            # (2*p_i - 1) 将可靠度映射到 [-1, 1]
            # p=0.5 -> 0 (无信息), p=1.0 -> 1 (完全可靠)
            reliability_factor = 2 * p_i - 1

            reliability_weighted[key] = reliability_factor * val
            components[key] = round(reliability_weighted[key], 3)

        ev_combo = self._score_dual_combo(reliability_weighted)
        ev_score = self._to_float(ev_combo.get("winner_score"), 0.0)
        components["combo_macd_kdj"] = round(self._to_float(ev_combo.get("score_macd_kdj"), 0.0), 3)
        components["combo_macd_bb"] = round(self._to_float(ev_combo.get("score_macd_bb"), 0.0), 3)
        components["combo_winner"] = 1.0 if str(ev_combo.get("winner")) == "MACD+KDJ" else -1.0
        components["macd_cross"] = round(self._to_float(features.get("macd_cross"), 0.0), 3)
        components["kdj_cross"] = round(self._to_float(features.get("kdj_cross"), 0.0), 3)
        components["bb_break"] = round(self._to_float(features.get("bb_break"), 0.0), 3)

        # ========== 确认/否决指标 (不参与方向决定) ==========
        cvd_val = features.get("cvd", 0.0)
        imb_val = features.get("imbalance", 0.0)
        components["cvd"] = round(cvd_val, 3)
        components["imbalance"] = round(imb_val, 3)
        # 记录确认指标的可靠度
        for key in ("cvd", "imbalance"):
            alpha, beta = self._ev_reliability.get(key, (10.0, 10.0))
            p_i = alpha / (alpha + beta)
            reliabilities[key] = round(p_i, 3)

        # ========== 主指标失效检测 ==========
        # 当所有主指标都接近0时，启用CVD/imbalance作为备用方向判断
        primary_indicators_flat = (
            all(abs(features.get(k, 0.0)) < 0.05 for k in ("macd", "kdj", "bb"))
            and abs(features.get("macd_cross", 0.0)) < 0.5
            and abs(features.get("kdj_cross", 0.0)) < 0.5
            and abs(features.get("bb_break", 0.0)) < 0.5
        )

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
            "combo_compare": ev_combo,
            "active_model": str(ev_combo.get("winner", "MACD+KDJ")),
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

        # 趋势判定不再依赖 EMA（仅保留 EMA 数值用于观察日志）
        if adx <= 0 or atr_pct <= 0:
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

        lw_combo = lw_result.get("combo_compare", {}) if isinstance(lw_result.get("combo_compare"), dict) else {}
        ev_combo = ev_result.get("combo_compare", {}) if isinstance(ev_result.get("combo_compare"), dict) else {}

        # 开仓方向指导：默认固定使用 MACD+BB（可配置切换）
        guide_model = self._direction_guide_model
        guide_model_map = {
            "MACD_BB": "MACD+BB",
            "MACD_KDJ": "MACD+KDJ",
            "EV_PRIMARY": "EV主方向",
        }
        if guide_model == "MACD_KDJ":
            guide_score = self._to_float(lw_combo.get("score_macd_kdj"), lw_score)
        elif guide_model == "EV_PRIMARY":
            guide_score = ev_score
        else:
            guide_score = self._to_float(ev_combo.get("score_macd_bb"), ev_score)
        guide_direction = self._direction_from_score(guide_score, self._direction_guide_neutral_zone)

        active_model = guide_model_map.get(guide_model, guide_model)
        direction = guide_direction if self._direction_guide_enabled else ev_dir
        active_dir = guide_direction if self._direction_guide_enabled else ev_dir
        active_score = guide_score if self._direction_guide_enabled else ev_score
        final_score = active_score

        # 如果分歧太大且最终指导本身不明确，使用 BOTH
        if need_confirm and abs(active_score) < 0.1:
            direction = "BOTH"

        # 日志对照采用固定映射：
        # LW -> MACD+KDJ，EV -> MACD+BB
        # winner 仍保留动态优胜组合，便于观测两组合强弱
        lw_winner = "MACD+KDJ"
        ev_winner = "MACD+BB"
        winner = str(ev_combo.get("winner", "MACD+KDJ"))
        combo_compare = {
            "active_model": active_model,
            "active_dir": active_dir,
            "active_score": round(active_score, 3),
            "lw_winner": lw_winner,
            "ev_winner": ev_winner,
            "lw_combo_score": round(self._to_float(lw_combo.get("score_macd_kdj"), 0.0), 3),
            "ev_combo_score": round(self._to_float(ev_combo.get("score_macd_bb"), 0.0), 3),
            "winner": winner,
            "lw_dynamic_winner": str(lw_combo.get("winner", "MACD+KDJ")),
            "ev_dynamic_winner": str(ev_combo.get("winner", "MACD+KDJ")),
            "direction_guide_enabled": bool(self._direction_guide_enabled),
            "direction_guide_model": str(guide_model),
            "direction_guide_model_label": active_model,
            "guide_dir": guide_direction,
            "guide_score": round(guide_score, 3),
            "guide_neutral_zone": float(self._direction_guide_neutral_zone),
            "macd_bb_weights": dict(ev_combo.get("weights", {}).get("macd_bb", {}))
            if isinstance(ev_combo.get("weights", {}), dict)
            else {},
            "macd_kdj_weights": dict(lw_combo.get("weights", {}).get("macd_kdj", {}))
            if isinstance(lw_combo.get("weights", {}), dict)
            else {},
            "feature_snapshot": dict(ev_combo.get("feature_snapshot", {}))
            if isinstance(ev_combo.get("feature_snapshot", {}), dict)
            else {},
            "settings": dict(ev_combo.get("settings", {}))
            if isinstance(ev_combo.get("settings", {}), dict)
            else {},
        }

        # 构建日志原因（components 可能包含 dict/bool，避免格式化异常导致策略中断）
        def _fmt_component_value(v: Any) -> str:
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, (int, float)):
                return f"{float(v):+.2f}"
            if isinstance(v, dict):
                return "{...}"
            if isinstance(v, (list, tuple)):
                return "[...]"
            return str(v)

        lw_components = lw_result.get("components", {})
        if not isinstance(lw_components, dict):
            lw_components = {}
        comp_str = ",".join([f"{k}:{_fmt_component_value(v)}" for k, v in lw_components.items()])
        direction_reason = (
            f"dir_lw={lw_dir[:4]} score_lw={lw_score:+.2f} | "
            f"dir_ev={ev_dir[:4]} score_ev={ev_score:+.2f} | "
            f"guide={active_model}:{active_dir[:4]}({active_score:+.2f}) | "
            f"agree={1 if agree else 0} div={divergence:.2f} conf={abs(active_score):.2f} | "
            f"winner={winner} lw={lw_winner} ev={ev_winner} | "
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
                "score": final_score,
                "method": f"DIRECTION_GUIDE_{guide_model}" if self._direction_guide_enabled else "EV_PRIMARY",
                "need_confirm": need_confirm,
            },
            # 兼容旧接口
            "ev_direction": ev_dir,
            "ev_score": ev_score,
            "lw_direction": lw_dir,
            "lw_score": lw_score,
            "guide_direction": active_dir,
            "guide_score": active_score,
            "lw_components": lw_result["components"],
            "legacy_direction": "BOTH",
            "legacy_score": 0.0,
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
        adx_strong = adx >= (self.regime_adx_trend_on + self.direction_lock_soft_adx_buffer)
        return bool(adx_strong)

    def _compute_trend_pending(
        self,
        symbol: str,
        market_flow_context: Dict[str, Any],
        regime_info: Dict[str, Any],
        cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = cfg or self._trend_capture_config()
        timeframes = market_flow_context.get("timeframes")
        tf15 = timeframes.get("15m") if isinstance(timeframes, dict) else {}
        tf15 = tf15 if isinstance(tf15, dict) else {}
        ff_raw = market_flow_context.get("fund_flow_features")
        ff15 = ff_raw.get("15m") if isinstance(ff_raw, dict) and isinstance(ff_raw.get("15m"), dict) else {}
        ff15 = ff15 if isinstance(ff15, dict) else {}

        adx = self._to_float(tf15.get("adx"), self._to_float(regime_info.get("adx"), 0.0))
        atr_pct = self._to_float(tf15.get("atr_pct"), self._to_float(regime_info.get("atr_pct"), 0.0))
        ema_fast = self._to_float(tf15.get("ema_fast"), self._to_float(regime_info.get("ema_fast"), 0.0))
        ema_slow = self._to_float(tf15.get("ema_slow"), self._to_float(regime_info.get("ema_slow"), 0.0))
        ema_spread = (ema_fast - ema_slow) if (ema_fast and ema_slow) else 0.0

        symbol_up = str(symbol or "").upper()
        prev = self._trend_pending_state.get(symbol_up, {})
        adx_prev = self._to_float(tf15.get("adx_prev"), self._to_float(prev.get("adx"), adx))
        ema_spread_prev = self._to_float(tf15.get("ema_spread_prev"), self._to_float(prev.get("ema_spread"), ema_spread))
        adx_slope = adx - adx_prev
        ema_spread_expand = ema_spread - ema_spread_prev
        self._trend_pending_state[symbol_up] = {"adx": adx, "ema_spread": ema_spread}

        oi_delta_ratio = self._to_float(
            ff15.get("oi_delta_ratio"),
            self._to_float(tf15.get("oi_delta_ratio"), self._to_float(market_flow_context.get("oi_delta_ratio"), 0.0)),
        )
        ret_15m = self._to_float(
            tf15.get("ret_period"),
            self._to_float(ff15.get("ret_period"), self._to_float(market_flow_context.get("ret_period"), 0.0)),
        )

        long_align = 1.0 if (ret_15m > 0 and oi_delta_ratio > 0) else 0.0
        short_align = 1.0 if (ret_15m < 0 and oi_delta_ratio > 0) else 0.0
        price_oi_align = max(long_align, short_align)

        adx_min = self._to_float(cfg.get("trend_pending_adx_min"), 16.5)
        adx_slope_min = self._to_float(cfg.get("trend_pending_adx_slope_min"), 0.8)
        ema_expand_min = self._to_float(cfg.get("trend_pending_ema_expand_min"), 0.0)
        atr_min = self._to_float(cfg.get("atr_pct_min"), 0.001)
        atr_max = self._to_float(cfg.get("atr_pct_max"), 0.02)
        pending_min_score = self._to_float(cfg.get("trend_pending_min_score"), 0.55)

        long_score = 0.0
        short_score = 0.0
        if adx >= adx_min and adx_slope >= adx_slope_min and atr_min <= atr_pct <= atr_max:
            if ema_spread > 0 and ema_spread_expand >= ema_expand_min:
                long_score += 0.40
            if ret_15m > 0:
                long_score += 0.20
            if oi_delta_ratio > 0:
                long_score += 0.20
            if long_align > 0:
                long_score += 0.20

            if ema_spread < 0 and ema_spread_expand <= -ema_expand_min:
                short_score += 0.40
            if ret_15m < 0:
                short_score += 0.20
            if oi_delta_ratio > 0:
                short_score += 0.20
            if short_align > 0:
                short_score += 0.20

        side = "NONE"
        score = 0.0
        if long_score >= short_score and long_score >= pending_min_score:
            side = "LONG"
            score = min(1.0, long_score)
        elif short_score > long_score and short_score >= pending_min_score:
            side = "SHORT"
            score = min(1.0, short_score)
        return {
            "trend_pending_side": side,
            "trend_pending_score": round(score, 4),
            "trend_pending_adx": round(adx, 4),
            "trend_pending_adx_slope": round(adx_slope, 4),
            "trend_pending_ema_fast": round(ema_fast, 8),
            "trend_pending_ema_slow": round(ema_slow, 8),
            "trend_pending_ema_spread": round(ema_spread, 8),
            "trend_pending_ema_spread_expand": round(ema_spread_expand, 8),
            "trend_pending_atr_pct": round(atr_pct, 6),
            "trend_pending_price_oi_align": round(price_oi_align, 4),
            # compatibility aliases
            "side": side,
            "score": round(score, 4),
            "adx_slope_15m": round(adx_slope, 4),
            "ema_spread_15m": round(ema_spread, 8),
            "ema_spread_expand_15m": round(ema_spread_expand, 8),
            "price_oi_alignment_15m": round(price_oi_align, 4),
        }

    def _compute_range_veto_by_trend(
        self,
        symbol: str,
        regime_info: Dict[str, Any],
        trend_pending: Dict[str, Any],
        trend_capture: Optional[Dict[str, Any]] = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = symbol
        cfg = cfg or self._trend_capture_config()
        trend_capture = trend_capture or {}
        regime = str(regime_info.get("regime", "NO_TRADE")).upper()
        if not bool(cfg.get("range_veto_by_trend_enabled", True)) or regime != "RANGE":
            return {
                "range_veto_by_trend": False,
                "range_veto_side": "NONE",
                "range_veto_score": 0.0,
                "range_veto_reason": "",
            }

        pending_side = str(trend_pending.get("trend_pending_side", "NONE")).upper()
        pending_score = self._to_float(trend_pending.get("trend_pending_score"), 0.0)
        cap_long = self._to_float(trend_capture.get("trend_capture_score_long"), 0.0)
        cap_short = self._to_float(trend_capture.get("trend_capture_score_short"), 0.0)
        pending_th = self._to_float(cfg.get("range_veto_trend_pending_score"), 0.18)
        capture_th = self._to_float(cfg.get("range_veto_trend_capture_score"), 0.22)

        veto = False
        side = "NONE"
        score = 0.0
        reason = ""
        if pending_side == "LONG" and pending_score >= pending_th and cap_long >= capture_th:
            veto = True
            side = "LONG"
            score = max(pending_score, cap_long)
            reason = f"range_veto_long pending={pending_score:.2f} capture={cap_long:.2f}"
        elif pending_side == "SHORT" and pending_score >= pending_th and cap_short >= capture_th:
            veto = True
            side = "SHORT"
            score = max(pending_score, cap_short)
            reason = f"range_veto_short pending={pending_score:.2f} capture={cap_short:.2f}"
        return {
            "range_veto_by_trend": bool(veto),
            "range_veto_side": side,
            "range_veto_reason": reason,
            "range_veto_score": round(score, 4),
        }

    def _compute_trend_capture(
        self,
        symbol: str,
        market_flow_context: Dict[str, Any],
        regime_info: Dict[str, Any],
        trend_pending: Dict[str, Any],
        cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = symbol
        _ = regime_info
        cfg = cfg or self._trend_capture_config()
        if not bool(cfg.get("trend_capture_enabled", True)):
            return {
                "trend_capture_enabled": False,
                "trend_capture_side": "NONE",
                "trend_capture_score_long": 0.0,
                "trend_capture_score_short": 0.0,
            }

        timeframes = market_flow_context.get("timeframes")
        tf5 = timeframes.get("5m") if isinstance(timeframes, dict) else {}
        tf3 = timeframes.get("3m") if isinstance(timeframes, dict) else {}
        tf5 = tf5 if isinstance(tf5, dict) else {}
        tf3 = tf3 if isinstance(tf3, dict) else {}
        ff_raw = market_flow_context.get("fund_flow_features", {})
        ff = ff_raw if isinstance(ff_raw, dict) else {}
        ff5 = ff.get("5m") if isinstance(ff.get("5m"), dict) else {}
        ff5 = ff5 if isinstance(ff5, dict) else {}
        ms = market_flow_context.get("microstructure_features", {})
        ms = ms if isinstance(ms, dict) else {}

        close_5m = self._to_float(tf5.get("close"), self._to_float(tf5.get("last_close"), 0.0))
        hh_n = self._to_float(tf5.get("hh_n"), self._to_float(tf5.get("high_n"), close_5m))
        ll_n = self._to_float(tf5.get("ll_n"), self._to_float(tf5.get("low_n"), close_5m))
        ema_fast_5m = self._to_float(tf5.get("ema_fast"), 0.0)
        ema_slow_5m = self._to_float(tf5.get("ema_slow"), 0.0)
        ret_5m = self._to_float(tf5.get("ret_period"), self._to_float(ff5.get("ret_period"), self._to_float(market_flow_context.get("ret_period"), 0.0)))
        cvd_mom_5m = self._to_float(tf5.get("cvd_momentum"), self._to_float(ff5.get("cvd_momentum"), self._to_float(market_flow_context.get("cvd_momentum"), 0.0)))
        oi_delta_ratio_5m = self._to_float(tf5.get("oi_delta_ratio"), self._to_float(ff5.get("oi_delta_ratio"), self._to_float(market_flow_context.get("oi_delta_ratio"), 0.0)))
        depth_ratio_5m = self._to_float(tf5.get("depth_ratio"), self._to_float(ff5.get("depth_ratio"), self._to_float(market_flow_context.get("depth_ratio"), 0.0)))
        imbalance_5m = self._to_float(tf5.get("imbalance"), self._to_float(ff5.get("imbalance"), self._to_float(market_flow_context.get("imbalance"), 0.0)))

        micro_delta = self._to_float(
            ms.get("micro_delta"),
            self._to_float(ms.get("micro_delta_norm"), self._to_float(market_flow_context.get("micro_delta"), self._to_float(market_flow_context.get("micro_delta_norm"), 0.0))),
        )
        microprice_bias = self._to_float(
            ms.get("microprice_bias"),
            self._to_float(ms.get("microprice_delta"), self._to_float(market_flow_context.get("microprice_bias"), 0.0)),
        )
        ret_3m = self._to_float(tf3.get("ret_period"), 0.0)

        trap_score = self._to_float(ms.get("trap_score"), self._to_float(market_flow_context.get("trap_score"), 0.0))
        phantom_score = self._to_float(ms.get("phantom_score"), self._to_float(market_flow_context.get("phantom"), 0.0))
        spread_z = self._to_float(ms.get("spread_z"), 0.0)

        breakout_long = close_5m >= hh_n and cvd_mom_5m > 0
        breakout_short = close_5m <= ll_n and cvd_mom_5m < 0
        pullback_resume_long = ema_fast_5m > ema_slow_5m and ret_5m > 0 and cvd_mom_5m > 0
        pullback_resume_short = ema_fast_5m < ema_slow_5m and ret_5m < 0 and cvd_mom_5m < 0

        cvd_align_long = cvd_mom_5m > 0
        cvd_align_short = cvd_mom_5m < 0
        oi_align_long = oi_delta_ratio_5m > 0 and ret_5m > 0
        oi_align_short = oi_delta_ratio_5m > 0 and ret_5m < 0
        depth_align_long = depth_ratio_5m > 0 or imbalance_5m > 0
        depth_align_short = depth_ratio_5m < 0 or imbalance_5m < 0
        micro_confirm_long = micro_delta > 0 and microprice_bias > 0
        micro_confirm_short = micro_delta < 0 and microprice_bias < 0
        micro_reaccel_long = ret_3m > 0 and micro_confirm_long
        micro_reaccel_short = ret_3m < 0 and micro_confirm_short

        score_long = 0.0
        score_short = 0.0
        if breakout_long:
            score_long += 0.10
        if pullback_resume_long:
            score_long += 0.08
        if cvd_align_long:
            score_long += 0.08
        if oi_align_long:
            score_long += 0.08
        if depth_align_long:
            score_long += 0.05
        if micro_confirm_long:
            score_long += 0.04
        if breakout_short:
            score_short += 0.10
        if pullback_resume_short:
            score_short += 0.08
        if cvd_align_short:
            score_short += 0.08
        if oi_align_short:
            score_short += 0.08
        if depth_align_short:
            score_short += 0.05
        if micro_confirm_short:
            score_short += 0.04
        micro_penalty = 0.0
        if trap_score > self._to_float(cfg.get("trend_capture_trap_soft_max"), 0.65):
            micro_penalty += 0.05
        if phantom_score > self._to_float(cfg.get("trend_capture_phantom_soft_max"), 0.65):
            micro_penalty += 0.04
        if spread_z > self._to_float(cfg.get("trend_capture_spread_soft_max"), 1.8):
            micro_penalty += 0.04

        score_long = max(0.0, score_long - micro_penalty)
        score_short = max(0.0, score_short - micro_penalty)

        confirm_3m_long = bool((breakout_long or pullback_resume_long) and micro_reaccel_long)
        confirm_3m_short = bool((breakout_short or pullback_resume_short) and micro_reaccel_short)
        if not confirm_3m_long:
            score_long = 0.0
        if not confirm_3m_short:
            score_short = 0.0

        side = "NONE"
        min_score = self._to_float(cfg.get("trend_capture_min_score"), 0.22)
        if score_long >= score_short and score_long >= min_score:
            side = "LONG"
        elif score_short > score_long and score_short >= min_score:
            side = "SHORT"

        return {
            "trend_capture_enabled": True,
            "trend_capture_side": side,
            "trend_capture_score_long": round(score_long, 4),
            "trend_capture_score_short": round(score_short, 4),
            "trend_capture_breakout_long": bool(breakout_long),
            "trend_capture_breakout_short": bool(breakout_short),
            "trend_capture_pullback_resume_long": bool(pullback_resume_long),
            "trend_capture_pullback_resume_short": bool(pullback_resume_short),
            "trend_capture_cvd_align_long": bool(cvd_align_long),
            "trend_capture_cvd_align_short": bool(cvd_align_short),
            "trend_capture_oi_align_long": bool(oi_align_long),
            "trend_capture_oi_align_short": bool(oi_align_short),
            "trend_capture_depth_align_long": bool(depth_align_long),
            "trend_capture_depth_align_short": bool(depth_align_short),
            "trend_capture_micro_confirm_long": bool(micro_confirm_long),
            "trend_capture_micro_confirm_short": bool(micro_confirm_short),
            "trend_capture_micro_reaccel_long": bool(micro_reaccel_long),
            "trend_capture_micro_reaccel_short": bool(micro_reaccel_short),
            "trend_capture_confirm_3m_long": bool(confirm_3m_long),
            "trend_capture_confirm_3m_short": bool(confirm_3m_short),
        }

    def _compute_entry_confluence_v2(
        self,
        symbol: str,
        market_flow_context: Dict[str, Any],
        cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = cfg or self._trend_capture_config()
        _ = symbol
        timeframes = market_flow_context.get("timeframes")
        tf1h = timeframes.get(str(cfg.get("tf_anchor", "1h"))) if isinstance(timeframes, dict) else {}
        tf5 = timeframes.get(str(cfg.get("tf_exec", "5m"))) if isinstance(timeframes, dict) else {}
        tf1h = tf1h if isinstance(tf1h, dict) else {}
        tf5 = tf5 if isinstance(tf5, dict) else {}
        snap = market_flow_context.get("_ma10_macd_confluence")
        snap = snap if isinstance(snap, dict) else {}
        close_1h = self._to_float(snap.get("last_close_1h"), self._to_float(tf1h.get("last_close"), 0.0))
        ma10_1h = self._to_float(snap.get("ma10_1h"), self._to_float(tf1h.get("ma10"), 0.0))
        anchor_long = bool(snap.get("ma10_1h_bias", 0) > 0 or (ma10_1h > 0 and close_1h >= ma10_1h))
        anchor_short = bool(snap.get("ma10_1h_bias", 0) < 0 or (ma10_1h > 0 and close_1h <= ma10_1h))

        macd_line = self._to_float(snap.get("macd_5m"), self._to_float(tf5.get("macd"), 0.0))
        macd_signal = self._to_float(snap.get("macd_5m_signal"), self._to_float(tf5.get("signal"), 0.0))
        macd_hist = self._to_float(snap.get("macd_5m_hist"), self._to_float(tf5.get("macd_hist"), 0.0))
        macd_hist_prev = self._to_float(tf5.get("prev", {}).get("macd_hist"), self._to_float(snap.get("macd_5m_hist_delta"), macd_hist))
        if abs(macd_hist_prev) == abs(macd_hist):
            macd_hist_prev = macd_hist - self._to_float(snap.get("macd_5m_hist_delta"), 0.0)
        macd_trigger_long = bool(snap.get("macd_trigger_pass_long", False) or (macd_line > macd_signal and macd_hist > 0))
        macd_trigger_short = bool(snap.get("macd_trigger_pass_short", False) or (macd_line < macd_signal and macd_hist < 0))
        macd_early_long = bool(snap.get("macd_early_pass_long", False) or (macd_hist > 0 and macd_hist >= macd_hist_prev))
        macd_early_short = bool(snap.get("macd_early_pass_short", False) or (macd_hist < 0 and macd_hist <= macd_hist_prev))

        k_val = self._to_float(snap.get("kdj_k"), self._to_float(tf5.get("kdj_k"), 50.0))
        d_val = self._to_float(snap.get("kdj_d"), self._to_float(tf5.get("kdj_d"), 50.0))
        j_val = self._to_float(snap.get("kdj_j"), self._to_float(tf5.get("kdj_j"), 50.0))
        kdj_ok_long = bool(snap.get("kdj_support_pass_long", False) or (k_val >= d_val or j_val >= k_val))
        kdj_ok_short = bool(snap.get("kdj_support_pass_short", False) or (k_val <= d_val or j_val <= k_val))

        hard_block_long = False
        hard_block_short = False
        entry_hard_filter = bool(cfg.get("entry_hard_filter", True))
        require_macd_trigger = bool(cfg.get("entry_require_macd_trigger", False))
        allow_macd_early = bool(cfg.get("entry_allow_macd_early", True))
        if entry_hard_filter:
            if (not anchor_long) and macd_trigger_short:
                hard_block_long = True
            if (not anchor_short) and macd_trigger_long:
                hard_block_short = True

        soft_penalty_long = 0.0
        soft_penalty_short = 0.0
        if require_macd_trigger:
            if not bool(macd_trigger_long):
                if allow_macd_early and macd_early_long:
                    soft_penalty_long += self._to_float(cfg.get("entry_soft_penalty_macd_early"), 0.03)
                else:
                    soft_penalty_long += self._to_float(cfg.get("entry_soft_penalty_no_macd"), 0.08)
            if not bool(macd_trigger_short):
                if allow_macd_early and macd_early_short:
                    soft_penalty_short += self._to_float(cfg.get("entry_soft_penalty_macd_early"), 0.03)
                else:
                    soft_penalty_short += self._to_float(cfg.get("entry_soft_penalty_no_macd"), 0.08)
        if not bool(kdj_ok_long):
            soft_penalty_long += self._to_float(cfg.get("entry_soft_penalty_no_kdj"), 0.04)
        if not bool(kdj_ok_short):
            soft_penalty_short += self._to_float(cfg.get("entry_soft_penalty_no_kdj"), 0.04)

        return {
            "confluence_side": "BOTH",
            "confluence_hard_block_long": bool(hard_block_long),
            "confluence_hard_block_short": bool(hard_block_short),
            "confluence_soft_penalty_long": round(soft_penalty_long, 4),
            "confluence_soft_penalty_short": round(soft_penalty_short, 4),
            "confluence_anchor_ma10_long": bool(anchor_long),
            "confluence_anchor_ma10_short": bool(anchor_short),
            "confluence_macd_trigger_long": bool(macd_trigger_long),
            "confluence_macd_trigger_short": bool(macd_trigger_short),
            "confluence_macd_early_long": bool(macd_early_long),
            "confluence_macd_early_short": bool(macd_early_short),
            "confluence_kdj_ok_long": bool(kdj_ok_long),
            "confluence_kdj_ok_short": bool(kdj_ok_short),
        }

    def _compute_ma10_macd_confluence(
        self,
        symbol: str,
        flow_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        cfg = self._trend_capture_config()
        return self._compute_entry_confluence_v2(
            symbol=symbol,
            market_flow_context=flow_context,
            cfg=cfg,
        )

    def _format_trend_capture_reason(self, md: Dict[str, Any], side: str) -> str:
        s = "long" if str(side).upper() == "LONG" else "short"
        score = self._to_float(md.get(f"trend_capture_score_{s}"), 0.0)
        pending_side = str(md.get("trend_pending_side", "NONE"))
        pending_score = self._to_float(md.get("trend_pending_score"), 0.0)
        breakout = int(bool(md.get(f"trend_capture_breakout_{s}", False)))
        pullback = int(bool(md.get(f"trend_capture_pullback_resume_{s}", False)))
        cvd = int(bool(md.get(f"trend_capture_cvd_align_{s}", False)))
        oi = int(bool(md.get(f"trend_capture_oi_align_{s}", False)))
        depth = int(bool(md.get(f"trend_capture_depth_align_{s}", False)))
        micro = int(bool(md.get(f"trend_capture_micro_confirm_{s}", False)))
        reac = int(bool(md.get(f"trend_capture_micro_reaccel_{s}", False)))
        confirm_3m = int(bool(md.get(f"trend_capture_confirm_3m_{s}", False)))
        return (
            f"capture_{s} pending={pending_side}:{pending_score:.2f} capture={score:.2f} "
            f"breakout={breakout} pullback={pullback} cvd={cvd} oi={oi} depth={depth} "
            f"micro={micro} reaccel={reac} confirm3m={confirm_3m}"
        )

    def _resolve_entry_mode(
        self,
        symbol: str,
        regime_info: Dict[str, Any],
        base_scores: Dict[str, Any],
        trend_pending: Dict[str, Any],
        trend_capture: Dict[str, Any],
        confluence: Dict[str, Any],
        range_veto: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> FundFlowDecision:
        regime = str(regime_info.get("regime", "NO_TRADE")).upper()
        base_long = self._to_float(base_scores.get("long_score"), 0.0)
        base_short = self._to_float(base_scores.get("short_score"), 0.0)
        cap_long = self._to_float(trend_capture.get("trend_capture_score_long"), 0.0)
        cap_short = self._to_float(trend_capture.get("trend_capture_score_short"), 0.0)
        pending_side = str(trend_pending.get("trend_pending_side", "NONE")).upper()
        pending_score = self._to_float(trend_pending.get("trend_pending_score"), 0.0)
        pen_long = self._to_float(confluence.get("confluence_soft_penalty_long"), 0.0)
        pen_short = self._to_float(confluence.get("confluence_soft_penalty_short"), 0.0)
        hard_long = bool(confluence.get("confluence_hard_block_long", False))
        hard_short = bool(confluence.get("confluence_hard_block_short", False))

        final_long = base_long * 0.72 + cap_long * 0.28 - pen_long
        final_short = base_short * 0.72 + cap_short * 0.28 - pen_short
        if hard_long:
            final_long = min(final_long, 0.05)
        if hard_short:
            final_short = min(final_short, 0.05)

        open_long_th = self._to_float(cfg.get("long_open_threshold"), self.long_open_threshold)
        open_short_th = self._to_float(cfg.get("short_open_threshold"), self.short_open_threshold)
        capture_open_th = self._to_float(cfg.get("trend_capture_min_score"), self.trend_capture_min_score)
        capture_gap = self._to_float(cfg.get("trend_capture_min_gap"), self.trend_capture_min_gap)
        default_portion = self._to_float(cfg.get("default_target_portion"), self.default_portion)
        leverage = int(self._to_float(cfg.get("default_leverage"), self.default_leverage))

        operation = Operation.HOLD
        side = "NONE"
        entry_mode = "HOLD"
        entry_stage = 0
        entry_size_mult = 0.0
        decision_source = "none"

        if regime == "TREND":
            if final_long >= open_long_th and final_long >= final_short + capture_gap:
                operation = Operation.BUY
                side = "LONG"
                entry_mode = "TREND_STD"
                entry_stage = 2 if cap_long >= capture_open_th else 1
                entry_size_mult = 1.0 if entry_stage == 2 else self._to_float(cfg.get("trend_capture_confirm_position_mult"), 0.65)
                decision_source = "trend_std"
            elif final_short >= open_short_th and final_short >= final_long + capture_gap:
                operation = Operation.SELL
                side = "SHORT"
                entry_mode = "TREND_STD"
                entry_stage = 2 if cap_short >= capture_open_th else 1
                entry_size_mult = 1.0 if entry_stage == 2 else self._to_float(cfg.get("trend_capture_confirm_position_mult"), 0.65)
                decision_source = "trend_std"
        elif regime == "RANGE":
            if bool(range_veto.get("range_veto_by_trend", False)):
                if pending_side == "LONG" and cap_long >= capture_open_th and final_long >= final_short + capture_gap:
                    operation = Operation.BUY
                    side = "LONG"
                    entry_mode = "TREND_CAPTURE"
                    entry_stage = 1
                    entry_size_mult = self._to_float(cfg.get("trend_capture_trial_position_mult"), self.trend_capture_trial_position_mult)
                    decision_source = "range_veto_capture"
                elif pending_side == "SHORT" and cap_short >= capture_open_th and final_short >= final_long + capture_gap:
                    operation = Operation.SELL
                    side = "SHORT"
                    entry_mode = "TREND_CAPTURE"
                    entry_stage = 1
                    entry_size_mult = self._to_float(cfg.get("trend_capture_trial_position_mult"), self.trend_capture_trial_position_mult)
                    decision_source = "range_veto_capture"
                else:
                    decision_source = "range_veto_hold"
            else:
                decision_source = "range_default"
        else:
            if pending_side == "LONG" and pending_score >= 0.70 and cap_long >= (capture_open_th + 0.05):
                operation = Operation.BUY
                side = "LONG"
                entry_mode = "TREND_CAPTURE"
                entry_stage = 1
                entry_size_mult = 0.25
                decision_source = "no_trade_capture"
            elif pending_side == "SHORT" and pending_score >= 0.70 and cap_short >= (capture_open_th + 0.05):
                operation = Operation.SELL
                side = "SHORT"
                entry_mode = "TREND_CAPTURE"
                entry_stage = 1
                entry_size_mult = 0.25
                decision_source = "no_trade_capture"

        md: Dict[str, Any] = {}
        md.update(regime_info or {})
        md.update(base_scores or {})
        md.update(trend_pending or {})
        md.update(trend_capture or {})
        md.update(confluence or {})
        md.update(range_veto or {})
        md.update(
            {
                "entry_mode": entry_mode,
                "entry_stage": entry_stage,
                "entry_size_mult": round(entry_size_mult, 4),
                "base_long_score": round(base_long, 4),
                "base_short_score": round(base_short, 4),
                "final_long_score": round(final_long, 4),
                "final_short_score": round(final_short, 4),
                "decision_source": decision_source,
                "decision_bias": side,
                "decision_conflict_note": str(range_veto.get("range_veto_reason", "")),
            }
        )

        reason_parts = [
            f"{regime}",
            f"mode={entry_mode}",
            f"src={decision_source}",
            f"long={final_long:.3f}",
            f"short={final_short:.3f}",
        ]
        if side in {"LONG", "SHORT"}:
            reason_parts.append(self._format_trend_capture_reason(md, side))
        if bool(range_veto.get("range_veto_by_trend", False)):
            reason_parts.append(str(range_veto.get("range_veto_reason", "")))
        if side == "LONG":
            reason_parts.append(
                f"soft={self._to_float(md.get('confluence_soft_penalty_long'), 0.0):.2f} "
                f"hard={int(bool(md.get('confluence_hard_block_long', False)))} "
                f"size={entry_size_mult:.2f}"
            )
        elif side == "SHORT":
            reason_parts.append(
                f"soft={self._to_float(md.get('confluence_soft_penalty_short'), 0.0):.2f} "
                f"hard={int(bool(md.get('confluence_hard_block_short', False)))} "
                f"size={entry_size_mult:.2f}"
            )

        if operation == Operation.BUY:
            picked_portion = default_portion * entry_size_mult
        elif operation == Operation.SELL:
            picked_portion = default_portion * entry_size_mult
        else:
            picked_portion = 0.0

        return FundFlowDecision(
            operation=operation,
            symbol=symbol,
            target_portion_of_balance=picked_portion,
            leverage=leverage,
            reason=" | ".join([p for p in reason_parts if p]),
            metadata=md,
        )

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
        ai_gate = str(trigger_context.get("ai_gate") or "").strip().lower()
        if ai_gate == "position_review":
            ai_request_mode = "position_review"
        elif ai_gate == "final":
            ai_request_mode = "entry_review"
        else:
            ai_request_mode = "generic"
        
        # 1. 检测市场状态
        regime_info = self._detect_regime(market_flow_context or {})
        regime = str(regime_info.get("regime", "NO_TRADE")).upper()
        direction = str(regime_info.get("direction", "BOTH")).upper()
        trend_cfg = self._trend_capture_config()
        trend_pending = self._compute_trend_pending(symbol, market_flow_context or {}, regime_info, cfg=trend_cfg)
        
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
                request_mode=ai_request_mode,
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
        ff_cfg = self.config.get("fund_flow", {}) if isinstance(self.config.get("fund_flow"), dict) else {}
        base_scores = {"long_score": long_score, "short_score": short_score}
        trend_capture = self._compute_trend_capture(symbol, market_flow_context or {}, regime_info, trend_pending, cfg=trend_cfg)
        confluence_v2 = self._compute_entry_confluence_v2(symbol, market_flow_context or {}, cfg=trend_cfg)
        range_veto = self._compute_range_veto_by_trend(symbol, regime_info, trend_pending, trend_capture, cfg=trend_cfg)

        close_threshold = float(engine_params.get("close_threshold", self.close_threshold))
        current_pos = (portfolio.get("positions") or {}).get(symbol)
        pos_side = str((current_pos or {}).get("side", "")).upper()
        if pos_side not in ("LONG", "SHORT"):
            self._clear_reverse_close_streak(symbol)

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
            "trend_pending_side": trend_pending.get("trend_pending_side", "NONE"),
            "trend_pending_score": trend_pending.get("trend_pending_score", 0.0),
            "adx_slope_15m": trend_pending.get("trend_pending_adx_slope", trend_pending.get("adx_slope_15m", 0.0)),
            "ema_spread_15m": trend_pending.get("trend_pending_ema_spread", trend_pending.get("ema_spread_15m", 0.0)),
            "ema_spread_expand_15m": trend_pending.get("trend_pending_ema_spread_expand", trend_pending.get("ema_spread_expand_15m", 0.0)),
            "price_oi_alignment_15m": trend_pending.get("trend_pending_price_oi_align", trend_pending.get("price_oi_alignment_15m", 0.0)),
            "regime_score": regime_info.get("guide_score", regime_info.get("ev_score", 0.0)),
            "ev_direction": regime_info.get("ev_direction", "BOTH"),
            "ev_score": regime_info.get("ev_score", 0.0),
            "lw_direction": regime_info.get("lw_direction", "BOTH"),
            "lw_score": regime_info.get("lw_score", 0.0),
            "guide_direction": regime_info.get("guide_direction", regime_info.get("ev_direction", "BOTH")),
            "guide_score": regime_info.get("guide_score", regime_info.get("ev_score", 0.0)),
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
            "entry_mode": "HOLD",
            "entry_stage": 0,
            "entry_size_mult": 0.0,
            "ds_confidence": weight_map.confidence if use_router_runtime else 0.0,
            "ds_source": weight_map.reason if use_router_runtime else "local_only",
            "ds_weights_snapshot": weight_map.to_dict() if use_router_runtime else {},
            "weight_router_runtime_enabled": use_router_runtime,
            "ai_weights_runtime_enabled": bool(use_router_runtime and use_ai_weights and self.deepseek_router.ai_enabled),
            "fusion_info": {
                "enabled": bool(fused.get("fusion_applied", False)),
                "score_15m_weight": self._to_float(fused.get("score_15m_weight"), self.score_15m_weight),
                "score_5m_weight": self._to_float(fused.get("score_5m_weight"), self.score_5m_weight),
                "consistency_weight": fused.get("consistency_weight", 1.0),
                "trigger_score_source": str(fused.get("trigger_score_source", "unknown")),
            },
            # 资金流 3.0 一致性指标
            "flow_confirm": flow_confirm,
            "consistency_3bars": consistency_3bars,
            "reverse_close_filter": {
                "confirm_bars": int(self.reverse_close_confirm_bars),
                "score_buffer": float(self.reverse_close_score_buffer),
                "min_gap": float(self.reverse_close_min_gap),
                "no_trade_extra_bars": int(self.reverse_close_no_trade_extra_bars),
                "require_direction_lock": bool(self.reverse_close_require_direction_lock),
            },
        }
        metadata_base.update(trend_pending)
        metadata_base.update(trend_capture)
        metadata_base.update(confluence_v2)
        metadata_base.update(range_veto)
        metadata_base["base_long_score"] = round(self._to_float(base_scores.get("long_score"), 0.0), 4)
        metadata_base["base_short_score"] = round(self._to_float(base_scores.get("short_score"), 0.0), 4)

        reverse_close_threshold = close_threshold + self.reverse_close_score_buffer
        required_reverse_bars = int(self.reverse_close_confirm_bars)
        if regime == "NO_TRADE":
            required_reverse_bars += int(self.reverse_close_no_trade_extra_bars)
        required_reverse_bars = max(1, required_reverse_bars)
        pending_capture_active = (
            bool(trend_cfg.get("trend_capture_enabled", self.trend_capture_enabled))
            and regime == "NO_TRADE"
            and str(trend_pending.get("trend_pending_side", "NONE")).upper() in {"LONG", "SHORT"}
            and self._to_float(trend_pending.get("trend_pending_score"), 0.0) >= self._to_float(trend_cfg.get("trend_capture_min_score"), self.trend_capture_min_score)
        )
        if pending_capture_active:
            pending_side = str(trend_pending.get("trend_pending_side", "NONE")).upper()
            capture_gap_cfg = self._to_float(trend_cfg.get("trend_capture_min_gap"), self.trend_capture_min_gap)
            if pending_side == "LONG" and (long_score - short_score) < capture_gap_cfg:
                pending_capture_active = False
            elif pending_side == "SHORT" and (short_score - long_score) < capture_gap_cfg:
                pending_capture_active = False

        if pos_side == "LONG":
            reverse_trigger = (
                short_score >= reverse_close_threshold
                and (short_score - long_score) >= self.reverse_close_min_gap
            )
            if self.reverse_close_require_direction_lock:
                reverse_trigger = reverse_trigger and direction == "SHORT_ONLY"
            streak = self._update_reverse_close_streak(symbol, pos_side, reverse_trigger)
            if reverse_trigger and streak < required_reverse_bars:
                return FundFlowDecision(
                    operation=Operation.HOLD,
                    symbol=symbol,
                    target_portion_of_balance=0.0,
                    leverage=int(engine_params.get("default_leverage", self.default_leverage)),
                    reason=(
                        f"{regime}反转待确认(平多) {streak}/{required_reverse_bars}, "
                        f"short={short_score:.3f} long={long_score:.3f} "
                        f"thr={reverse_close_threshold:.3f} gap={short_score - long_score:.3f}"
                    ),
                    metadata={
                        **metadata_base,
                        "long_score": long_score,
                        "short_score": short_score,
                        "reverse_close_streak": streak,
                        "reverse_close_required_bars": required_reverse_bars,
                        "reverse_close_triggered": True,
                    },
                )
            if reverse_trigger and streak >= required_reverse_bars:
                self._update_reverse_close_streak(symbol, pos_side, False)
                return FundFlowDecision(
                    operation=Operation.CLOSE,
                    symbol=symbol,
                    target_portion_of_balance=1.0,
                    leverage=int(engine_params.get("default_leverage", self.default_leverage)),
                    max_price=price * 1.001,
                    reason=(
                        f"{regime}反转平多(确认{streak}/{required_reverse_bars}), "
                        f"short={short_score:.3f}>=close={reverse_close_threshold:.3f}"
                    ),
                    metadata={**metadata_base, "long_score": long_score, "short_score": short_score},
                )

        if pos_side == "SHORT":
            reverse_trigger = (
                long_score >= reverse_close_threshold
                and (long_score - short_score) >= self.reverse_close_min_gap
            )
            if self.reverse_close_require_direction_lock:
                reverse_trigger = reverse_trigger and direction == "LONG_ONLY"
            streak = self._update_reverse_close_streak(symbol, pos_side, reverse_trigger)
            if reverse_trigger and streak < required_reverse_bars:
                return FundFlowDecision(
                    operation=Operation.HOLD,
                    symbol=symbol,
                    target_portion_of_balance=0.0,
                    leverage=int(engine_params.get("default_leverage", self.default_leverage)),
                    reason=(
                        f"{regime}反转待确认(平空) {streak}/{required_reverse_bars}, "
                        f"long={long_score:.3f} short={short_score:.3f} "
                        f"thr={reverse_close_threshold:.3f} gap={long_score - short_score:.3f}"
                    ),
                    metadata={
                        **metadata_base,
                        "long_score": long_score,
                        "short_score": short_score,
                        "reverse_close_streak": streak,
                        "reverse_close_required_bars": required_reverse_bars,
                        "reverse_close_triggered": True,
                    },
                )
            if reverse_trigger and streak >= required_reverse_bars:
                self._update_reverse_close_streak(symbol, pos_side, False)
                return FundFlowDecision(
                    operation=Operation.CLOSE,
                    symbol=symbol,
                    target_portion_of_balance=1.0,
                    leverage=int(engine_params.get("default_leverage", self.default_leverage)),
                    min_price=price * 0.999,
                    reason=(
                        f"{regime}反转平空(确认{streak}/{required_reverse_bars}), "
                        f"long={long_score:.3f}>=close={reverse_close_threshold:.3f}"
                    ),
                    metadata={**metadata_base, "long_score": long_score, "short_score": short_score},
                )

        if regime == "NO_TRADE" and not pending_capture_active:
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
        entry_mode = "TREND_CAPTURE" if pending_capture_active else ("RANGE" if regime == "RANGE" else "TREND_STD")
        entry_stage = 1 if pending_capture_active else 2
        entry_size_mult = self._to_float(trend_cfg.get("trend_capture_trial_position_mult"), self.trend_capture_trial_position_mult) if pending_capture_active else 1.0
        if pending_capture_active:
            target_portion *= entry_size_mult
        take_profit_pct = self._normalize_pct_ratio(engine_params.get("take_profit_pct"), self.take_profit_pct)
        stop_loss_pct = self._normalize_pct_ratio(engine_params.get("stop_loss_pct"), self.stop_loss_pct)
        tp_pct_levels_raw = engine_params.get("take_profit_pct_levels")
        tp_pct_levels: list[float] = tp_pct_levels_raw if isinstance(tp_pct_levels_raw, list) else []
        tp_reduce_levels_raw = engine_params.get("take_profit_reduce_pct_levels")
        tp_reduce_levels: list[float] = tp_reduce_levels_raw if isinstance(tp_reduce_levels_raw, list) else []
        tp_enabled = take_profit_pct > 0
        sl_enabled = stop_loss_pct > 0
        tp_long_price = price * (1.0 + take_profit_pct) if tp_enabled else None
        sl_long_price = price * (1.0 - stop_loss_pct) if sl_enabled else None
        tp_short_price = price * (1.0 - take_profit_pct) if tp_enabled else None
        sl_short_price = price * (1.0 + stop_loss_pct) if sl_enabled else None
        resolve_cfg = {
            **trend_cfg,
            **engine_params,
            "default_target_portion": target_portion,
            "default_leverage": default_lev,
            "long_open_threshold": long_threshold,
            "short_open_threshold": short_threshold,
            "trend_capture_min_score": self._to_float(trend_cfg.get("trend_capture_min_score"), self.trend_capture_min_score),
            "trend_capture_min_gap": self._to_float(trend_cfg.get("trend_capture_min_gap"), self.trend_capture_min_gap),
            "trend_capture_trial_position_mult": self._to_float(trend_cfg.get("trend_capture_trial_position_mult"), self.trend_capture_trial_position_mult),
        }

        if regime == "RANGE":
            if bool(range_veto.get("range_veto_by_trend", False)):
                resolved = self._resolve_entry_mode(
                    symbol=symbol,
                    regime_info=regime_info,
                    base_scores=base_scores,
                    trend_pending=trend_pending,
                    trend_capture=trend_capture,
                    confluence=confluence_v2,
                    range_veto=range_veto,
                    cfg=resolve_cfg,
                )
                resolved_md = resolved.metadata if isinstance(resolved.metadata, dict) else {}
                if resolved.operation == Operation.BUY:
                    lev_score = self._to_float(resolved_md.get("final_long_score"), long_score)
                    leverage = self._pick_leverage(
                        lev_score,
                        long_threshold,
                        min_leverage=min_lev,
                        max_leverage=max_lev,
                        default_leverage=default_lev,
                    )
                    return FundFlowDecision(
                        operation=Operation.BUY,
                        symbol=symbol,
                        target_portion_of_balance=resolved.target_portion_of_balance,
                        leverage=leverage,
                        max_price=price * (1.0 + self.entry_slippage),
                        take_profit_price=tp_long_price,
                        stop_loss_price=sl_long_price,
                        time_in_force=TimeInForce.IOC,
                        tp_execution=ExecutionMode.LIMIT,
                        sl_execution=ExecutionMode.LIMIT,
                        reason=resolved.reason,
                        metadata={**metadata_base, **resolved_md, "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage}},
                    )
                if resolved.operation == Operation.SELL:
                    lev_score = self._to_float(resolved_md.get("final_short_score"), short_score)
                    leverage = self._pick_leverage(
                        lev_score,
                        short_threshold,
                        min_leverage=min_lev,
                        max_leverage=max_lev,
                        default_leverage=default_lev,
                    )
                    return FundFlowDecision(
                        operation=Operation.SELL,
                        symbol=symbol,
                        target_portion_of_balance=resolved.target_portion_of_balance,
                        leverage=leverage,
                        min_price=price * (1.0 - self.entry_slippage),
                        take_profit_price=tp_short_price,
                        stop_loss_price=sl_short_price,
                        time_in_force=TimeInForce.IOC,
                        tp_execution=ExecutionMode.LIMIT,
                        sl_execution=ExecutionMode.LIMIT,
                        reason=resolved.reason,
                        metadata={**metadata_base, **resolved_md, "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage}},
                    )
                return FundFlowDecision(
                    operation=Operation.HOLD,
                    symbol=symbol,
                    target_portion_of_balance=0.0,
                    leverage=default_lev,
                    reason=resolved.reason,
                    metadata={**metadata_base, **resolved_md, "long_score": long_score, "short_score": short_score},
                )
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
                        **range_veto,
                        "open_thresholds": {"long": long_threshold, "short": short_threshold},
                        "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                        "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                        "entry_mode": entry_mode,
                        "entry_stage": entry_stage,
                        "entry_size_mult": entry_size_mult,
                        "tp_levels": self._build_tp_levels_metadata(
                            price=price,
                            direction="LONG",
                            pct_levels=tp_pct_levels,
                            reduce_levels=tp_reduce_levels,
                        ),
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
                        **range_veto,
                        "open_thresholds": {"long": long_threshold, "short": short_threshold},
                        "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                        "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                        "entry_mode": entry_mode,
                        "entry_stage": entry_stage,
                        "entry_size_mult": entry_size_mult,
                        "tp_levels": self._build_tp_levels_metadata(
                            price=price,
                            direction="SHORT",
                            pct_levels=tp_pct_levels,
                            reduce_levels=tp_reduce_levels,
                        ),
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
                metadata={**metadata_base, **score_out, **range_meta, **range_veto},
            )

        direction_lock_applied = False
        if self._should_apply_direction_lock(regime, direction, regime_info):
            direction_lock_applied = True
            if direction == "LONG_ONLY":
                short_score = 0.0
            elif direction == "SHORT_ONLY":
                long_score = 0.0

        # ========== 方向一致性检查 ==========
        # 采用 direction_guide 主导压制，LW 只做辅助（弱化但不反客为主）
        guide_score = self._to_float(
            regime_info.get("guide_score"),
            self._to_float(regime_info.get("ev_score"), 0.0),
        )
        guide_dir = str(regime_info.get("guide_direction", regime_info.get("ev_direction", "BOTH"))).upper()
        lw_score_val = self._to_float(regime_info.get("lw_score"), 0.0)
        lw_dir = str(regime_info.get("lw_direction", "BOTH")).upper()
        ev_dir = str(regime_info.get("ev_direction", "BOTH")).upper()

        direction_conflict = False
        
        # 1. guide_score 方向压制（阈值从 0.05 改为 0.02，更严格）
        if guide_score < -0.02 and long_score > short_score:
            # 指导方向偏空，压制多头开仓
            long_score *= 0.15  # 更强的压制力度
            direction_conflict = True
        elif guide_score > 0.02 and short_score > long_score:
            # 指导方向偏多，压制空头开仓
            short_score *= 0.15
            direction_conflict = True

        # 2. EV/LW 方向冲突压制（新增：当 EV 和 LW 方向相反时，强制压制）
        ev_long = ev_dir == "LONG_ONLY"
        ev_short = ev_dir == "SHORT_ONLY"
        lw_long = lw_dir == "LONG_ONLY"
        lw_short = lw_dir == "SHORT_ONLY"
        
        if ev_short and lw_long and long_score > short_score:
            # EV 偏空但 LW 偏多，且要开多 -> 强力压制
            long_score *= 0.25
            direction_conflict = True
        elif ev_long and lw_short and short_score > long_score:
            # EV 偏多但 LW 偏空，且要开空 -> 强力压制
            short_score *= 0.25
            direction_conflict = True

        # 3. LW 辅助一致性过滤（保留原有逻辑，降低阈值）
        if ev_long and lw_short and abs(lw_score_val) >= 0.10:
            long_score *= 0.70
            direction_conflict = True
        elif ev_short and lw_long and abs(lw_score_val) >= 0.10:
            short_score *= 0.70
            direction_conflict = True

        # ========== 方向不明确时禁止开仓 ==========
        # 当 direction 为 BOTH 且主指标都失效时，禁止开新仓
        # 避免在没有明确方向判断的情况下盲目开仓
        primary_flat_lw = bool(regime_info.get("lw", {}).get("components", {}).get("primary_flat", False))
        primary_flat_ev = bool(regime_info.get("ev", {}).get("components", {}).get("primary_flat", False))
        primary_flat = bool(primary_flat_lw and primary_flat_ev)
        if direction == "BOTH" and primary_flat and not pending_capture_active:
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
        resolved = self._resolve_entry_mode(
            symbol=symbol,
            regime_info=regime_info,
            base_scores={"long_score": long_score, "short_score": short_score},
            trend_pending=trend_pending,
            trend_capture=trend_capture,
            confluence=confluence_v2,
            range_veto=range_veto,
            cfg=resolve_cfg,
        )
        resolved_md = resolved.metadata if isinstance(resolved.metadata, dict) else {}
        score_out.update(
            {
                "entry_mode": resolved_md.get("entry_mode", "HOLD"),
                "entry_stage": resolved_md.get("entry_stage", 0),
                "entry_size_mult": resolved_md.get("entry_size_mult", 0.0),
                "final_long_score": resolved_md.get("final_long_score", long_score),
                "final_short_score": resolved_md.get("final_short_score", short_score),
                "decision_source": resolved_md.get("decision_source", "none"),
            }
        )

        if resolved.operation == Operation.BUY:
            lev_score = self._to_float(resolved_md.get("final_long_score"), long_score)
            leverage = self._pick_leverage(
                lev_score,
                long_threshold,
                min_leverage=min_lev,
                max_leverage=max_lev,
                default_leverage=default_lev,
            )
            return FundFlowDecision(
                operation=Operation.BUY,
                symbol=symbol,
                target_portion_of_balance=resolved.target_portion_of_balance,
                leverage=leverage,
                max_price=price * (1.0 + self.entry_slippage),
                take_profit_price=tp_long_price,
                stop_loss_price=sl_long_price,
                time_in_force=TimeInForce.IOC,
                tp_execution=ExecutionMode.LIMIT,
                sl_execution=ExecutionMode.LIMIT,
                reason=resolved.reason,
                metadata={
                    **metadata_base,
                    **score_out,
                    **resolved_md,
                    "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                    "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                },
            )

        if resolved.operation == Operation.SELL:
            lev_score = self._to_float(resolved_md.get("final_short_score"), short_score)
            leverage = self._pick_leverage(
                lev_score,
                short_threshold,
                min_leverage=min_lev,
                max_leverage=max_lev,
                default_leverage=default_lev,
            )
            return FundFlowDecision(
                operation=Operation.SELL,
                symbol=symbol,
                target_portion_of_balance=resolved.target_portion_of_balance,
                leverage=leverage,
                min_price=price * (1.0 - self.entry_slippage),
                take_profit_price=tp_short_price,
                stop_loss_price=sl_short_price,
                time_in_force=TimeInForce.IOC,
                tp_execution=ExecutionMode.LIMIT,
                sl_execution=ExecutionMode.LIMIT,
                reason=resolved.reason,
                metadata={
                    **metadata_base,
                    **score_out,
                    **resolved_md,
                    "leverage_model": {"min": min_lev, "max": max_lev, "picked": leverage},
                    "tp_sl": {"tp_pct": take_profit_pct, "sl_pct": stop_loss_pct, "tp_enabled": tp_enabled, "sl_enabled": sl_enabled},
                },
            )

        return FundFlowDecision(
            operation=Operation.HOLD,
            symbol=symbol,
            target_portion_of_balance=0.0,
            leverage=default_lev,
            reason=resolved.reason or f"{regime}信号不足 long={long_score:.3f} short={short_score:.3f}",
            metadata={**metadata_base, **score_out, **resolved_md},
        )
