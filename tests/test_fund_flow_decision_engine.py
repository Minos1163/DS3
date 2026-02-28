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
