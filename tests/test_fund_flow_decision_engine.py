from src.fund_flow.decision_engine import FundFlowDecisionEngine
from src.fund_flow.models import Operation


def _cfg():
    return {
        "trading": {"default_leverage": 2},
        "risk": {"max_position_pct": 0.2},
        "fund_flow": {
            "default_target_portion": 0.2,
            "open_threshold": 0.2,
            "close_threshold": 0.3,
            "entry_slippage": 0.001,
            "deepseek_weight_router": {"enabled": False},
        },
    }


def _trend_context(
    *,
    cvd_ratio: float,
    cvd_momentum: float,
    oi_delta_ratio: float,
    funding_rate: float,
    depth_ratio: float,
    imbalance: float,
    ema_fast: float,
    ema_slow: float,
    adx: float = 30.0,
    atr_pct: float = 0.005,
):
    tf_ctx = {
        "cvd_ratio": cvd_ratio,
        "cvd_momentum": cvd_momentum,
        "oi_delta_ratio": oi_delta_ratio,
        "funding_rate": funding_rate,
        "depth_ratio": depth_ratio,
        "imbalance": imbalance,
    }
    tf_15m = {**tf_ctx, "ema_fast": ema_fast, "ema_slow": ema_slow, "adx": adx, "atr_pct": atr_pct}
    return {"timeframes": {"15m": tf_15m, "5m": dict(tf_ctx)}}


def test_decide_hold_when_long_score_lacks_breakout_or_pullback():
    engine = FundFlowDecisionEngine(_cfg())
    decision = engine.decide(
        symbol="BTCUSDT",
        portfolio={"positions": {}},
        price=100.0,
        market_flow_context=_trend_context(
            cvd_ratio=0.8,
            cvd_momentum=0.6,
            oi_delta_ratio=0.4,
            funding_rate=-0.1,
            depth_ratio=1.2,
            imbalance=0.7,
            ema_fast=101.0,
            ema_slow=100.0,
        ),
        trigger_context={"trigger_type": "signal"},
    )
    assert decision.operation == Operation.HOLD


def test_decide_close_long_when_short_reversal():
    engine = FundFlowDecisionEngine(_cfg())
    decision = engine.decide(
        symbol="BTCUSDT",
        portfolio={"positions": {"BTCUSDT": {"side": "LONG"}}},
        price=100.0,
        market_flow_context=_trend_context(
            cvd_ratio=-0.9,
            cvd_momentum=-0.8,
            oi_delta_ratio=0.5,
            funding_rate=0.2,
            depth_ratio=0.8,
            imbalance=-0.7,
            ema_fast=99.0,
            ema_slow=100.0,
        ),
        trigger_context={"trigger_type": "signal"},
    )
    assert decision.operation == Operation.CLOSE
    assert decision.target_portion_of_balance == 1.0


def test_decide_hold_when_signal_not_enough():
    engine = FundFlowDecisionEngine(_cfg())
    decision = engine.decide(
        symbol="BTCUSDT",
        portfolio={"positions": {}},
        price=100.0,
        market_flow_context={"cvd_ratio": 0.0},
    )
    assert decision.operation == Operation.HOLD


def test_trend_capture_keeps_partial_score_without_micro_confirm():
    cfg = _cfg()
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "partial_confirm_enabled": True,
        "partial_confirm_min_align": 2,
        "partial_confirm_penalty": 0.03,
        "depth_ratio_neutral": 1.0,
        "depth_ratio_buffer": 0.0,
    }
    engine = FundFlowDecisionEngine(cfg)
    capture = engine._compute_trend_capture(
        "BTCUSDT",
        market_flow_context={
            "timeframes": {
                "5m": {
                    "close": 105.0,
                    "hh_n": 105.0,
                    "ll_n": 100.0,
                    "ema_fast": 104.0,
                    "ema_slow": 102.0,
                    "ret_period": 0.01,
                    "cvd_momentum": 0.02,
                    "oi_delta_ratio": 0.0,
                    "depth_ratio": 1.02,
                    "imbalance": 0.03,
                },
                "3m": {
                    "ret_period": 0.0,
                },
            },
            "microstructure_features": {
                "micro_delta": 0.0,
                "microprice_bias": 0.0,
                "trap_score": 0.1,
                "phantom_score": 0.1,
                "spread_z": 0.1,
            },
        },
        regime_info={},
        trend_pending={},
    )
    assert capture["trend_capture_breakout_long"] is True
    assert capture["trend_capture_confirm_3m_long"] is False
    assert capture["trend_capture_score_long"] > 0.0
    assert capture["trend_capture_side"] == "LONG"


def test_pick_leverage_uses_discrete_config_levels():
    engine = FundFlowDecisionEngine(_cfg())
    assert engine._pick_leverage(0.11, 0.10, 4, 8, 6) == 4
    assert engine._pick_leverage(0.55, 0.10, 4, 8, 6) == 6
    assert engine._pick_leverage(0.95, 0.10, 4, 8, 6) == 8


def test_resolve_entry_mode_uses_base_score_floor_for_trend_entry():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.085
    cfg["fund_flow"]["short_open_threshold"] = 0.085
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BTCUSDT",
        regime_info={
            "regime": "TREND",
            "cvd_norm": 0.3,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
        },
        base_scores={"long_score": 0.10, "short_score": 0.0},
        trend_pending={"trend_pending_side": "NONE", "trend_pending_score": 0.0},
        trend_capture={
            "trend_capture_score_long": 0.0,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.BUY
    assert resolved.metadata["final_long_score"] >= 0.085


def test_confluence_ignores_ma10_bias_for_hard_block():
    engine = FundFlowDecisionEngine(_cfg())
    confluence = engine._compute_entry_confluence_v2(
        "BTCUSDT",
        market_flow_context={
            "_ma10_macd_confluence": {
                "last_close_1h": 99.0,
                "ma10_1h": 100.0,
                "ma10_1h_bias": -1,
                "macd_5m": 0.5,
                "macd_5m_signal": 0.1,
                "macd_5m_hist": 0.6,
                "macd_5m_hist_delta": 0.1,
                "kdj_k": 55.0,
                "kdj_d": 50.0,
                "kdj_j": 65.0,
            },
            "timeframes": {"5m": {}, "1h": {}},
        },
        cfg=engine._trend_capture_config(),
    )
    assert confluence["confluence_hard_block_long"] is False
    assert confluence["confluence_macd_trigger_long"] is True


def test_resolve_entry_mode_prunes_opposite_short_capture_when_confluence_fallback_turns_long():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["short_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BNBUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 0.5,
            "consistency_3bars": 0,
            "cvd_norm": 0.25,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
            "lw": {"components": {"primary_flat": True, "backup_source": "ma10_macd_confluence_5m"}},
            "ev": {"components": {"primary_flat": True, "backup_source": "ma10_macd_confluence_5m"}},
        },
        base_scores={"long_score": 0.09, "short_score": 0.0},
        trend_pending={"trend_pending_side": "LONG", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_side": "SHORT",
            "trend_capture_score_long": 0.0,
            "trend_capture_score_short": 0.2,
            "trend_capture_breakout_short": True,
            "trend_capture_cvd_align_short": True,
            "trend_capture_depth_align_short": True,
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
            "confluence_macd_trigger_long": True,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.BUY
    assert resolved.metadata["trend_capture_score_short"] == 0.0
    assert resolved.metadata["trend_capture_side"] in {"LONG", "NONE"}
    assert resolved.metadata["trend_capture_directional_prune"] is True
    assert resolved.metadata["trend_capture_pruned_side"] == "SHORT"


def test_resolve_entry_mode_prunes_opposite_long_capture_when_confluence_fallback_turns_short():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["short_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BNBUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "SHORT_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 0.5,
            "consistency_3bars": 0,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
            "lw": {"components": {"primary_flat": True, "backup_source": "ma10_macd_confluence_5m"}},
            "ev": {"components": {"primary_flat": True, "backup_source": "ma10_macd_confluence_5m"}},
        },
        base_scores={"long_score": 0.0, "short_score": 0.09},
        trend_pending={"trend_pending_side": "SHORT", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_side": "LONG",
            "trend_capture_score_long": 0.2,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": True,
            "trend_capture_cvd_align_long": True,
            "trend_capture_depth_align_long": True,
            "trend_capture_breakout_short": True,
            "trend_capture_pullback_resume_short": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
            "confluence_macd_trigger_short": True,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.SELL
    assert resolved.metadata["trend_capture_score_long"] == 0.0
    assert resolved.metadata["trend_capture_side"] in {"SHORT", "NONE"}
    assert resolved.metadata["trend_capture_directional_prune"] is True
    assert resolved.metadata["trend_capture_pruned_side"] == "LONG"


def test_resolve_entry_mode_blocks_short_without_3m_confirm_when_required():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["short_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "short_require_confirm_3m": True,
        "short_min_score_boost": 0.02,
        "short_min_gap_boost": 0.01,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="SOLUSDT",
        regime_info={"regime": "TREND"},
        base_scores={"long_score": 0.0, "short_score": 0.34},
        trend_pending={"trend_pending_side": "NONE", "trend_pending_score": 0.0},
        trend_capture={
            "trend_capture_score_long": 0.0,
            "trend_capture_score_short": 1.0,
            "trend_capture_confirm_3m_short": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert resolved.metadata["decision_source"] == "trend_short_confirm_blocked"
    assert resolved.metadata["short_entry_confirm_3m_required"] is True
    assert resolved.metadata["short_entry_confirm_gate_pass"] is False


def test_resolve_entry_mode_injects_long_confluence_fallback_into_entry_score():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["short_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BNBUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 0.5,
            "consistency_3bars": 0,
            "cvd_norm": 0.25,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
            "lw": {
                "components": {
                    "primary_flat": True,
                    "backup_source": "ma10_macd_confluence_5m",
                    "backup_long_score": 1.0,
                    "backup_short_score": 0.0,
                }
            },
            "ev": {
                "components": {
                    "primary_flat": True,
                    "backup_source": "ma10_macd_confluence_5m",
                    "backup_long_score": 1.0,
                    "backup_short_score": 0.0,
                }
            },
        },
        base_scores={"long_score": 0.0001, "short_score": 0.0384},
        trend_pending={"trend_pending_side": "LONG", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_side": "SHORT",
            "trend_capture_score_long": 0.0,
            "trend_capture_score_short": 0.2,
            "trend_capture_breakout_short": True,
            "trend_capture_cvd_align_short": True,
            "trend_capture_depth_align_short": True,
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.08,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": True,
            "confluence_macd_trigger_long": True,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.BUY
    assert resolved.metadata["trend_capture_score_long"] == 1.0
    assert resolved.metadata["trend_capture_confluence_injected"] is True
    assert resolved.metadata["trend_capture_confluence_injected_side"] == "LONG"
    assert resolved.metadata["trend_capture_confluence_injected_score"] == 1.0
    assert resolved.metadata["trend_capture_injection_confirm_pass"] is True
    assert resolved.metadata["trend_capture_injection_gate_pass"] is True
    assert resolved.metadata["final_long_score"] >= 0.07


def test_resolve_entry_mode_injects_short_confluence_fallback_into_entry_score():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["short_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BNBUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "SHORT_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 0.5,
            "consistency_3bars": 0,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
            "lw": {
                "components": {
                    "primary_flat": True,
                    "backup_source": "ma10_macd_confluence_5m",
                    "backup_long_score": 0.0,
                    "backup_short_score": 1.0,
                }
            },
            "ev": {
                "components": {
                    "primary_flat": True,
                    "backup_source": "ma10_macd_confluence_5m",
                    "backup_long_score": 0.0,
                    "backup_short_score": 1.0,
                }
            },
        },
        base_scores={"long_score": 0.0384, "short_score": 0.0001},
        trend_pending={"trend_pending_side": "SHORT", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_side": "LONG",
            "trend_capture_score_long": 0.2,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": True,
            "trend_capture_cvd_align_long": True,
            "trend_capture_depth_align_long": True,
            "trend_capture_breakout_short": True,
            "trend_capture_pullback_resume_short": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.08,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": True,
            "confluence_hard_block_short": False,
            "confluence_macd_trigger_short": True,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.SELL
    assert resolved.metadata["trend_capture_score_short"] == 1.0
    assert resolved.metadata["trend_capture_confluence_injected"] is True
    assert resolved.metadata["trend_capture_confluence_injected_side"] == "SHORT"
    assert resolved.metadata["trend_capture_confluence_injected_score"] == 1.0
    assert resolved.metadata["trend_capture_injection_confirm_pass"] is True
    assert resolved.metadata["trend_capture_injection_gate_pass"] is True
    assert resolved.metadata["final_short_score"] >= 0.07


def test_resolve_entry_mode_does_not_inject_fallback_when_entry_window_closed():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BNBUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": False,
            "flow_confirm": 1.0,
            "consistency_3bars": 1,
            "lw": {"components": {"primary_flat": True, "backup_source": "ma10_macd_confluence_5m", "backup_long_score": 1.0}},
            "ev": {"components": {"primary_flat": True, "backup_source": "ma10_macd_confluence_5m", "backup_long_score": 1.0}},
        },
        base_scores={"long_score": 0.0001, "short_score": 0.0384},
        trend_pending={"trend_pending_side": "LONG", "trend_pending_score": 0.6},
        trend_capture={"trend_capture_score_long": 0.0, "trend_capture_score_short": 0.2, "trend_capture_side": "SHORT"},
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.08,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": True,
            "confluence_macd_trigger_long": True,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert resolved.metadata["trend_capture_confluence_injected"] is False
    assert resolved.metadata["trend_capture_injection_confirm_pass"] is True
    assert resolved.metadata["trend_capture_injection_gate_pass"] is False


def test_detect_regime_primary_flat_prefers_5m_confluence_fallback_over_cvd():
    engine = FundFlowDecisionEngine(_cfg())
    regime_info = engine._detect_regime(
        {
            "cvd_momentum": -0.8,
            "imbalance": -0.7,
            "timeframes": {
                "15m": {
                    "adx": 20.0,
                    "atr_pct": 0.004,
                    "ema_fast": 101.0,
                    "ema_slow": 100.0,
                    "last_open": 100.0,
                    "last_close": 100.2,
                    "macd_hist_norm": 0.0,
                    "macd_cross": "NONE",
                    "macd_hist_delta": 0.0,
                    "kdj_j": 50.0,
                    "kdj_cross": "NONE",
                    "kdj_zone": "MID",
                    "bb_pos_norm": 0.0,
                    "bb_width_norm": 0.0,
                    "bb_break": "NONE",
                    "bb_trend": "MID",
                    "bb_squeeze": False,
                },
                "5m": {},
                "1h": {},
            },
            "_ma10_macd_confluence": {
                "last_close_1h": 101.5,
                "ma10_1h": 100.0,
                "ma10_1h_bias": 1,
                "macd_5m": 0.6,
                "macd_5m_signal": 0.2,
                "macd_5m_hist": 0.4,
                "macd_5m_hist_delta": 0.1,
                "macd_5m_cross": "NONE",
                "macd_5m_zone": "ABOVE_ZERO",
                "kdj_k": 62.0,
                "kdj_d": 55.0,
                "kdj_j": 76.0,
                "kdj_cross": "NONE",
                "kdj_zone": "HIGH",
            },
        }
    )
    assert regime_info["guide_direction"] == "LONG_ONLY"
    assert regime_info["lw"]["components"]["backup_source"] == "ma10_macd_confluence_5m"
    assert regime_info["ev"]["components"]["backup_source"] == "ma10_macd_confluence_5m"


def test_detect_regime_primary_flat_still_uses_cvd_fallback_when_no_confluence():
    engine = FundFlowDecisionEngine(_cfg())
    regime_info = engine._detect_regime(
        {
            "cvd_momentum": -0.8,
            "imbalance": -0.7,
            "timeframes": {
                "15m": {
                    "adx": 20.0,
                    "atr_pct": 0.004,
                    "ema_fast": 101.0,
                    "ema_slow": 100.0,
                    "last_open": 100.0,
                    "last_close": 99.8,
                    "macd_hist_norm": 0.0,
                    "macd_cross": "NONE",
                    "macd_hist_delta": 0.0,
                    "kdj_j": 50.0,
                    "kdj_cross": "NONE",
                    "kdj_zone": "MID",
                    "bb_pos_norm": 0.0,
                    "bb_width_norm": 0.0,
                    "bb_break": "NONE",
                    "bb_trend": "MID",
                    "bb_squeeze": False,
                },
                "5m": {},
            },
        }
    )
    assert regime_info["guide_direction"] == "SHORT_ONLY"
    assert regime_info["lw"]["components"]["backup_source"] == "cvd_imbalance"


def test_resolve_entry_mode_blocks_long_when_symbol_override_is_short_only():
    cfg = _cfg()
    cfg["fund_flow"]["symbol_side_overrides"] = {"APTUSDT": "SHORT_ONLY"}
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="APTUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 0.6,
            "consistency_3bars": 1,
            "cvd_norm": 0.25,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
        },
        base_scores={"long_score": 0.12, "short_score": 0.01},
        trend_pending={"trend_pending_side": "LONG", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_score_long": 0.12,
            "trend_capture_score_short": 0.0,
            "trend_capture_side": "LONG",
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert resolved.metadata["symbol_side_override_mode"] == "SHORT_ONLY"
    assert resolved.metadata["symbol_side_override_allowed"] is False
    assert resolved.metadata["blocked_operation"] == Operation.BUY.value


def test_decide_blocks_range_long_when_symbol_override_is_short_only(monkeypatch):
    cfg = _cfg()
    cfg["fund_flow"]["symbol_side_overrides"] = {"SUIUSDT": "SHORT_ONLY"}
    engine = FundFlowDecisionEngine(cfg)

    monkeypatch.setattr(engine, "_detect_regime", lambda _ctx: {"regime": "RANGE", "direction": "BOTH", "reason": "test"})
    monkeypatch.setattr(engine, "_compute_trend_pending", lambda *args, **kwargs: {"trend_pending_side": "NONE", "trend_pending_score": 0.0})
    monkeypatch.setattr(
        engine,
        "_engine_params_for",
        lambda _regime: {
            "default_leverage": 2,
            "default_target_portion": 0.2,
            "long_open_threshold": 0.07,
            "short_open_threshold": 0.07,
            "close_threshold": 0.3,
        },
    )
    monkeypatch.setattr(
        engine,
        "_extract_range_quantiles",
        lambda _ctx: {
            "ready": True,
            "imb_hi": 0.4,
            "imb_lo": -0.4,
            "cvd_hi": 0.3,
            "cvd_lo": -0.3,
            "trap_guard_enabled": False,
            "n": 64,
        },
    )
    monkeypatch.setattr(engine, "_extract_15m_context", lambda _ctx: {})
    monkeypatch.setattr(engine, "_extract_5m_context", lambda _ctx: {})
    monkeypatch.setattr(engine, "_score_range", lambda _ctx: {"long_score": 0.6, "short_score": 0.1})
    monkeypatch.setattr(engine, "_score_trend", lambda _ctx: {"long_score": 0.0, "short_score": 0.0})
    monkeypatch.setattr(
        engine,
        "_fuse_scores",
        lambda *_args, **_kwargs: {"long_score": 0.6, "short_score": 0.1, "fusion_applied": False},
    )
    monkeypatch.setattr(engine, "_record_15m_score", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engine, "_compute_flow_consistency", lambda *_args, **_kwargs: (0.0, 0))
    monkeypatch.setattr(engine, "_compute_trend_capture", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(engine, "_compute_entry_confluence_v2", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(engine, "_compute_range_veto_by_trend", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(engine, "_extract_range_turn_values", lambda _ctx: {})
    monkeypatch.setattr(
        engine,
        "_evaluate_range_turn_confirm",
        lambda _vals: {
            "turned_up": True,
            "turned_down": False,
            "pass_count_long": 2,
            "pass_count_short": 0,
            "min_pass_count": 2,
            "mode": "1bar",
            "ready": True,
        },
    )

    decision = engine.decide(
        symbol="SUIUSDT",
        portfolio={"positions": {}},
        price=100.0,
        market_flow_context={
            "imbalance": -0.5,
            "cvd_momentum": -0.4,
            "oi_delta_ratio": 0.0,
        },
        use_weight_router=False,
        use_ai_weights=False,
    )

    assert decision.operation == Operation.HOLD
    assert decision.metadata["symbol_side_override_mode"] == "SHORT_ONLY"
    assert decision.metadata["symbol_side_override_allowed"] is False
    assert decision.metadata["blocked_operation"] == Operation.BUY.value


def test_resolve_entry_mode_blocks_entry_when_feature_snapshot_is_all_zero():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BTCUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 1.0,
            "consistency_3bars": 2,
            "cvd_norm": 0.35,
            "combo_compare": {
                "feature_snapshot": {
                    "macd_hist_sign": 0,
                    "macd_cross_sign": 0,
                    "kdj_cross_sign": 0,
                    "kdj_zone_sign": 0,
                }
            },
        },
        base_scores={"long_score": 0.12, "short_score": 0.0},
        trend_pending={"trend_pending_side": "LONG", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_score_long": 0.12,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert resolved.metadata["decision_source"] == "entry_hard_filter_blocked"
    assert resolved.metadata["entry_hard_filter_blocked"] is True
    assert "feature_snapshot_all_zero" in resolved.metadata["entry_hard_filters"]


def test_resolve_entry_mode_blocks_long_when_pending_side_is_short():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="ETHUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 1.0,
            "consistency_3bars": 2,
            "cvd_norm": 0.28,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
        },
        base_scores={"long_score": 0.12, "short_score": 0.0},
        trend_pending={"trend_pending_side": "SHORT", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_score_long": 0.12,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert resolved.metadata["decision_source"] == "entry_hard_filter_blocked"
    assert "pending_side_short_blocks_long" in resolved.metadata["entry_hard_filters"]


def test_resolve_entry_mode_blocks_long_when_cvd_norm_is_non_positive():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "required_long_cvd_norm": 0.12,
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="SOLUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 1.0,
            "consistency_3bars": 2,
            "cvd_norm": 0.0,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
        },
        base_scores={"long_score": 0.12, "short_score": 0.0},
        trend_pending={"trend_pending_side": "LONG", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_score_long": 0.12,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert resolved.metadata["decision_source"] == "entry_hard_filter_blocked"
    assert "cvd_norm_below_required_long" in resolved.metadata["entry_hard_filters"]


def test_resolve_entry_mode_blocks_entry_without_breakout_or_pullback():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BNBUSDT",
        regime_info={
            "regime": "TREND",
            "guide_direction": "LONG_ONLY",
            "allow_entry_window": True,
            "flow_confirm": 1.0,
            "consistency_3bars": 2,
            "cvd_norm": 0.31,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
        },
        base_scores={"long_score": 0.12, "short_score": 0.0},
        trend_pending={"trend_pending_side": "LONG", "trend_pending_score": 0.6},
        trend_capture={
            "trend_capture_score_long": 0.12,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": False,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert resolved.metadata["decision_source"] == "entry_hard_filter_blocked"
    assert "no_breakout_no_pullback_long" in resolved.metadata["entry_hard_filters"]


def test_resolve_entry_mode_blocks_long_when_strict_trend_requirements_fail():
    cfg = _cfg()
    cfg["fund_flow"]["long_open_threshold"] = 0.07
    cfg["fund_flow"]["trend_capture"] = {
        "trend_only_mode": True,
        "required_flow_confirm": 1.0,
        "required_long_cvd_norm": 0.12,
        "required_price_oi_alignment_15m": 1.0,
        "required_adx_slope": 0.05,
        "required_long_ema_spread_expand": 0.0,
        "min_score": 0.08,
        "min_gap": 0.02,
        "base_score_floor_mult": 0.85,
    }
    engine = FundFlowDecisionEngine(cfg)
    resolved = engine._resolve_entry_mode(
        symbol="BTCUSDT",
        regime_info={
            "regime": "TREND",
            "flow_confirm": 0.5,
            "cvd_norm": 0.10,
            "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
        },
        base_scores={"long_score": 0.15, "short_score": 0.0},
        trend_pending={
            "trend_pending_side": "LONG",
            "trend_pending_score": 0.8,
            "trend_pending_price_oi_align": 0.0,
            "trend_pending_adx_slope": 0.01,
            "trend_pending_ema_spread_expand": 0.0,
        },
        trend_capture={
            "trend_capture_score_long": 0.2,
            "trend_capture_score_short": 0.0,
            "trend_capture_breakout_long": True,
            "trend_capture_pullback_resume_long": False,
        },
        confluence={
            "confluence_soft_penalty_long": 0.0,
            "confluence_soft_penalty_short": 0.0,
            "confluence_hard_block_long": False,
            "confluence_hard_block_short": False,
        },
        range_veto={},
        cfg=engine._trend_capture_config(),
    )
    assert resolved.operation == Operation.HOLD
    assert "flow_confirm_below_required" in resolved.metadata["entry_hard_filters"]
    assert "cvd_norm_below_required_long" in resolved.metadata["entry_hard_filters"]
    assert "price_oi_alignment_below_required" in resolved.metadata["entry_hard_filters"]
    assert "adx_slope_below_required" in resolved.metadata["entry_hard_filters"]
    assert "ema_spread_expand_not_positive_long" in resolved.metadata["entry_hard_filters"]


def test_decide_uses_dynamic_short_term_stop_loss_for_trend_entries():
    cfg = _cfg()
    cfg["fund_flow"].update(
        {
            "default_target_portion": 0.08,
            "long_open_threshold": 0.07,
            "default_leverage": 5,
            "min_leverage": 4,
            "max_leverage": 5,
            "engine_params": {
                "TREND": {
                    "default_target_portion": 0.08,
                    "default_leverage": 5,
                    "min_leverage": 4,
                    "max_leverage": 5,
                    "long_open_threshold": 0.07,
                    "dynamic_stop_loss_enabled": True,
                    "short_stop_loss_min_pct": 0.0035,
                    "short_stop_loss_max_pct": 0.0045,
                    "short_stop_loss_atr_mult": 1.2,
                    "take_profit_pct_levels": [0.0025, 0.0055],
                    "take_profit_reduce_pct_levels": [0.4, 0.3],
                }
            },
            "trend_capture": {
                "trend_only_mode": True,
                "required_flow_confirm": 1.0,
                "required_long_cvd_norm": 0.12,
                "required_price_oi_alignment_15m": 1.0,
                "required_adx_slope": 0.05,
                "required_long_ema_spread_expand": 0.0,
                "min_score": 0.08,
                "min_gap": 0.02,
                "base_score_floor_mult": 0.85,
            },
        }
    )
    engine = FundFlowDecisionEngine(cfg)
    engine._detect_regime = lambda *_args, **_kwargs: {
        "regime": "TREND",
        "direction": "LONG_ONLY",
        "guide_direction": "LONG_ONLY",
        "adx": 25.0,
        "atr_pct": 0.002,
        "cvd_norm": 0.2,
        "combo_compare": {"feature_snapshot": {"macd_hist_sign": 1, "kdj_cross_sign": 1}},
    }
    engine._compute_trend_pending = lambda *_args, **_kwargs: {
        "trend_pending_side": "LONG",
        "trend_pending_score": 0.8,
        "trend_pending_price_oi_align": 1.0,
        "trend_pending_adx_slope": 0.08,
        "trend_pending_ema_spread": 1.0,
        "trend_pending_ema_spread_expand": 0.01,
    }
    engine._compute_flow_consistency = lambda *_args, **_kwargs: (1.0, 2)
    engine._compute_trend_capture = lambda *_args, **_kwargs: {
        "trend_capture_side": "LONG",
        "trend_capture_score_long": 0.43,
        "trend_capture_score_short": 0.0,
        "trend_capture_breakout_long": True,
        "trend_capture_breakout_short": False,
        "trend_capture_pullback_resume_long": False,
        "trend_capture_pullback_resume_short": False,
    }
    engine._compute_entry_confluence_v2 = lambda *_args, **_kwargs: {
        "confluence_side": "LONG",
        "confluence_hard_block_long": False,
        "confluence_hard_block_short": False,
        "confluence_soft_penalty_long": 0.0,
        "confluence_soft_penalty_short": 0.0,
    }
    engine._compute_range_veto_by_trend = lambda *_args, **_kwargs: {}
    decision = engine.decide(
        symbol="BTCUSDT",
        portfolio={"positions": {}},
        price=100.0,
        market_flow_context={
            "timeframes": {
                "15m": {
                    "adx": 25.0,
                    "atr_pct": 0.002,
                    "ema_fast": 101.0,
                    "ema_slow": 100.0,
                    "ret_period": 0.01,
                    "oi_delta_ratio": 0.02,
                },
                "5m": {
                    "close": 105.0,
                    "hh_n": 105.0,
                    "ll_n": 100.0,
                    "ema_fast": 104.0,
                    "ema_slow": 102.0,
                    "ret_period": 0.01,
                    "cvd_momentum": 0.02,
                    "oi_delta_ratio": 0.02,
                    "depth_ratio": 1.02,
                    "imbalance": 0.03,
                },
                "3m": {
                    "ret_period": 0.02,
                },
            },
            "fund_flow_features": {"15m": {"oi_delta_ratio": 0.02}, "5m": {"oi_delta_ratio": 0.02}},
            "microstructure_features": {"micro_delta": 0.02, "microprice_bias": 0.02},
        },
        trigger_context={"trigger_type": "signal"},
        use_weight_router=False,
        use_ai_weights=False,
    )
    assert decision.operation == Operation.BUY
    assert decision.stop_loss_price is not None
    assert round((100.0 - float(decision.stop_loss_price)) / 100.0, 4) == 0.0035
    tp_levels = decision.metadata.get("tp_levels", [])
    assert len(tp_levels) == 2
