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


def test_decide_buy_when_long_score_dominates():
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
    assert decision.operation == Operation.BUY
    assert decision.take_profit_price is not None
    assert decision.stop_loss_price is not None


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
        regime_info={"regime": "TREND"},
        base_scores={"long_score": 0.10, "short_score": 0.0},
        trend_pending={"trend_pending_side": "NONE", "trend_pending_score": 0.0},
        trend_capture={"trend_capture_score_long": 0.0, "trend_capture_score_short": 0.0},
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


def test_confluence_respects_ma10_hard_block_switch():
    cfg = _cfg()
    cfg["fund_flow"]["ma10_macd_confluence"] = {
        "entry_hard_filter": True,
        "entry_hard_block_against_ma10": False,
        "entry_hard_block_reverse_macd": True,
        "block_on_opposite_bias": False,
    }
    engine = FundFlowDecisionEngine(cfg)
    confluence = engine._compute_entry_confluence_v2(
        "BTCUSDT",
        market_flow_context={
            "_ma10_macd_confluence": {
                "last_close_1h": 99.0,
                "ma10_1h": 100.0,
                "ma10_1h_bias": -1,
                "macd_5m": -0.5,
                "macd_5m_signal": 0.1,
                "macd_5m_hist": -0.6,
                "macd_5m_hist_delta": -0.1,
                "kdj_k": 40.0,
                "kdj_d": 45.0,
                "kdj_j": 35.0,
            },
            "timeframes": {"5m": {}, "1h": {}},
        },
        cfg=engine._trend_capture_config(),
    )
    assert confluence["confluence_hard_block_long"] is False
