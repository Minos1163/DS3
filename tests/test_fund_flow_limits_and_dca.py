from types import SimpleNamespace

from src.app.fund_flow_bot import TradingBot
from src.fund_flow.decision_engine import FundFlowDecisionEngine
from src.fund_flow.models import FundFlowDecision, Operation


def _cfg():
    return {
        "trading": {"default_leverage": 2},
        "risk": {"max_position_pct": 0.2},
        "fund_flow": {
            "default_target_portion": 0.2,
            "max_active_symbols": 2,
            "max_symbol_position_portion": 0.15,
            "open_threshold": 0.2,
            "close_threshold": 0.3,
            "entry_slippage": 0.001,
            "take_profit_pct": 0.0,
            "stop_loss_pct": 0.02,
            "deepseek_weight_router": {"enabled": False},
        },
    }


def test_engine_params_follow_global_max_active_symbols_by_default():
    engine = FundFlowDecisionEngine(_cfg())

    trend_params = engine._engine_params_for("TREND")
    unknown_params = engine._engine_params_for("UNKNOWN")

    assert trend_params["max_active_symbols"] == 2
    assert unknown_params["max_active_symbols"] == 2
    assert unknown_params["max_symbol_position_portion"] >= 0.15


def test_dca_keeps_take_profit_disabled_when_config_is_zero():
    bot = TradingBot.__new__(TradingBot)
    bot._dca_stage_by_pos = {}
    bot._position_track_key = lambda symbol, side: f"{symbol}:{side}"
    bot._position_drawdown_ratio = lambda position, current_price: 0.02
    bot.fund_flow_decision_engine = SimpleNamespace(
        entry_slippage=0.001,
        take_profit_pct=0.0,
        stop_loss_pct=0.02,
        default_leverage=2,
    )

    base_decision = FundFlowDecision(
        operation=Operation.BUY,
        symbol="BTCUSDT",
        target_portion_of_balance=0.1,
        leverage=2,
        metadata={},
    )

    decision = bot._build_dca_decision(
        symbol="BTCUSDT",
        position={"side": "LONG", "leverage": 2},
        current_price=100.0,
        base_decision=base_decision,
        trigger_context={"trigger_type": "signal"},
        dca_cfg={
            "enabled": True,
            "max_additions": 1,
            "drawdown_thresholds": [0.01],
            "multipliers": [1.0],
            "base_add_portion": 0.05,
        },
    )

    assert decision is not None
    assert decision.take_profit_price == 100.0
    assert decision.stop_loss_price == 98.0
