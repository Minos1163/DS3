import time

import src.app.fund_flow_bot as bot_module
from src.app.fund_flow_bot import TradingBot
from src.fund_flow.models import FundFlowDecision, Operation as FundFlowOperation


def _make_bot(**gate_overrides):
    bot = TradingBot.__new__(TradingBot)
    bot.logs_dir = "LOGS"
    bot.config = {
        "fund_flow": {
            "pretrade_risk_gate": {
                "enabled": True,
                "force_exit_on_gate": True,
                "exit_close_ratio": 1.0,
                "exit_score_threshold": 0.08,
                "exit_confirm_bars": 2,
                "exit_min_hold_seconds": 300,
                "exit_require_price_followthrough": True,
                "exit_price_change_min": 0.001,
                "exit_drawdown_override": 0.003,
                **gate_overrides,
            },
            "ma10_macd_confluence": {
                "enabled": True,
                "exit_anchor_enabled": True,
                "exit_anchor_require_hist_expand": True,
                "exit_anchor_skip_on_hard_block": True,
            },
        }
    }
    bot._position_first_seen_ts = {}
    bot._pre_risk_exit_streak_by_pos = {}
    bot._position_extrema_by_pos = {}
    return bot


def _decision(**metadata_overrides):
    metadata = {
        "last_open": 100.0,
        "last_close": 99.0,
        "ma10_5m": 90.0,
        "last_close_5m": 101.0,
        "macd_5m_zone": "ABOVE_ZERO",
        "macd_5m_hist_expand_up": True,
        **metadata_overrides,
    }
    return FundFlowDecision(
        operation=FundFlowOperation.HOLD,
        symbol="BTCUSDT",
        target_portion_of_balance=0.0,
        leverage=3,
        reason="base",
        metadata=metadata,
    )


def _position():
    return {
        "side": "LONG",
        "entry_price": 100.0,
        "amount": 1.0,
        "leverage": 5,
        "margin": 20.0,
    }


def test_pretrade_risk_gate_locks_profit_after_hold_window(monkeypatch):
    bot = _make_bot()
    pos_key = bot._position_track_key("BTCUSDT", "LONG")
    bot._position_first_seen_ts[pos_key] = time.time() - 301
    bot._pre_risk_exit_streak_by_pos[pos_key] = 1

    monkeypatch.setattr(
        bot_module,
        "gate_trade_decision",
        lambda *args, **kwargs: {"action": "EXIT", "score": -0.5, "details": {}},
    )

    decision, gate_meta = bot._apply_pretrade_risk_gate(
        symbol="BTCUSDT",
        decision=_decision(),
        position=_position(),
        flow_context={},
        current_price=101.0,
        account_summary={"equity": 1000.0, "max_leverage": 10.0},
    )

    assert decision.operation == FundFlowOperation.CLOSE
    assert "PRE_RISK_EXIT" in decision.reason
    assert gate_meta["profit_lock_ready"] is True
    assert gate_meta["exit_confirmed"] is True


def test_pretrade_risk_gate_uses_trap_grace_after_hold_window(monkeypatch):
    bot = _make_bot(exit_require_price_followthrough=False)
    pos_key = bot._position_track_key("BTCUSDT", "LONG")
    bot._position_first_seen_ts[pos_key] = time.time() - 301
    bot._pre_risk_exit_streak_by_pos[pos_key] = 3

    monkeypatch.setattr(
        bot_module,
        "gate_trade_decision",
        lambda *args, **kwargs: {"action": "EXIT", "score": -0.5, "details": {}},
    )

    decision, gate_meta = bot._apply_pretrade_risk_gate(
        symbol="BTCUSDT",
        decision=_decision(last_close=99.95),
        position=_position(),
        flow_context={"trap_score": 0.85},
        current_price=99.8,
        account_summary={"equity": 1000.0, "max_leverage": 10.0},
    )

    assert decision.operation == FundFlowOperation.HOLD
    assert "PRE_RISK_TRAP_GRACE" in decision.reason
    assert gate_meta["trap_rebound_window"] is True
    assert gate_meta["exit_confirmed"] is False


def test_pretrade_risk_gate_protects_initial_300_seconds(monkeypatch):
    bot = _make_bot()
    pos_key = bot._position_track_key("BTCUSDT", "LONG")
    bot._position_first_seen_ts[pos_key] = time.time() - 120
    bot._pre_risk_exit_streak_by_pos[pos_key] = 2

    monkeypatch.setattr(
        bot_module,
        "gate_trade_decision",
        lambda *args, **kwargs: {"action": "EXIT", "score": -0.5, "details": {}},
    )

    decision, gate_meta = bot._apply_pretrade_risk_gate(
        symbol="BTCUSDT",
        decision=_decision(last_close=99.7),
        position=_position(),
        flow_context={},
        current_price=99.8,
        account_summary={"equity": 1000.0, "max_leverage": 10.0},
    )

    assert decision.operation == FundFlowOperation.HOLD
    assert "PRE_RISK_COOLDOWN_PROTECT" in decision.reason
    assert gate_meta["post_entry_protection_active"] is True
    assert gate_meta["exit_confirmed"] is False


def test_entry_filter_prefers_engine_confluence_macd_flags():
    bot = _make_bot()
    decision = FundFlowDecision(
        operation=FundFlowOperation.BUY,
        symbol="BTCUSDT",
        target_portion_of_balance=0.2,
        leverage=3,
        reason="base",
        metadata={
            "macd_5m_cross": "NONE",
            "macd_5m_hist_expand_up": False,
            "macd_5m_hist_expand_down": False,
            "macd_trigger_pass_long": False,
            "macd_early_pass_long": False,
            "confluence_macd_trigger_long": True,
            "confluence_macd_early_long": True,
        },
    )

    filtered = bot._apply_ma10_macd_entry_filter("BTCUSDT", decision)

    assert filtered.operation == FundFlowOperation.BUY
    assert filtered.reason == "base"
