from types import SimpleNamespace

from src.app.fund_flow_bot import TradingBot
from src.fund_flow.models import FundFlowDecision, Operation as FundFlowOperation


def _make_bot(active_symbols=None):
    bot = TradingBot.__new__(TradingBot)
    active_symbols = active_symbols or {"ETHUSDT": {}, "SOLUSDT": {}}
    bot.position_data = SimpleNamespace(get_all_positions=lambda: dict(active_symbols))
    bot._opened_symbols_this_cycle = set()
    bot._ai_review_mode_supports_flat_candidates = lambda _mode: False
    bot._decision_signal_score = lambda decision: float((decision.metadata or {}).get("test_score", 0.0))
    bot._executed = []

    def _record_execution(**kwargs):
        bot._executed.append(kwargs)

    bot._execute_and_log_decision = _record_execution
    return bot


def _decision(symbol: str, operation: FundFlowOperation) -> FundFlowDecision:
    return FundFlowDecision(
        operation=operation,
        symbol=symbol,
        target_portion_of_balance=1.0 if operation == FundFlowOperation.CLOSE else 0.2,
        leverage=5,
        reason="test",
        metadata={},
    )


def _decision_with_md(symbol: str, operation: FundFlowOperation, metadata) -> FundFlowDecision:
    return FundFlowDecision(
        operation=operation,
        symbol=symbol,
        target_portion_of_balance=1.0 if operation == FundFlowOperation.CLOSE else 0.2,
        leverage=5,
        reason="test",
        metadata=dict(metadata),
    )


def test_finalize_entries_executes_close_even_when_capacity_is_full():
    bot = _make_bot()
    context = {
        "pending_new_entries": [
            {
                # Simulate legacy close candidate without an explicit symbol field.
                "decision": _decision("BTCUSDT", FundFlowOperation.CLOSE),
                "position": {"side": "LONG", "amount": 1.0},
                "current_price": 100.0,
                "trigger_context": {},
                "portfolio": {},
                "account_summary": {"available_balance": 1000.0},
            },
            {
                "symbol": "XRPUSDT",
                "score": 0.9,
                "max_active_symbols": 2,
                "decision": _decision("XRPUSDT", FundFlowOperation.BUY),
                "position": None,
                "current_price": 2.0,
                "trigger_context": {},
                "portfolio": {},
                "account_summary": {"available_balance": 1000.0},
            },
        ],
        "block_new_entries_due_to_protection_gap": False,
        "protection_gap_symbols": [],
        "max_active_symbols": 2,
        "account_summary": {"available_balance": 1000.0},
        "ai_gate_enabled": False,
        "ai_review_cfg": {},
        "ai_review_mode": "disabled",
    }

    bot._finalize_entries(context)

    assert len(bot._executed) == 1
    assert bot._executed[0]["symbol"] == "BTCUSDT"
    assert bot._executed[0]["decision"].operation == FundFlowOperation.CLOSE


def test_finalize_entries_keeps_close_when_protection_gap_blocks_new_entries():
    bot = _make_bot(active_symbols={"ETHUSDT": {}})
    context = {
        "pending_new_entries": [
            {
                "symbol": "BTCUSDT",
                "score": 1.0,
                "bypass_capacity_guard": True,
                "bypass_ai_final_review": True,
                "decision": _decision("BTCUSDT", FundFlowOperation.CLOSE),
                "position": {"side": "SHORT", "amount": 1.0},
                "current_price": 100.0,
                "trigger_context": {},
                "portfolio": {},
                "account_summary": {"available_balance": 1000.0},
            },
            {
                "symbol": "XRPUSDT",
                "score": 0.8,
                "max_active_symbols": 3,
                "decision": _decision("XRPUSDT", FundFlowOperation.BUY),
                "position": None,
                "current_price": 2.0,
                "trigger_context": {},
                "portfolio": {},
                "account_summary": {"available_balance": 1000.0},
            },
        ],
        "block_new_entries_due_to_protection_gap": True,
        "protection_gap_symbols": ["BTCUSDT"],
        "max_active_symbols": 3,
        "account_summary": {"available_balance": 1000.0},
        "ai_gate_enabled": False,
        "ai_review_cfg": {},
        "ai_review_mode": "disabled",
    }

    bot._finalize_entries(context)

    assert len(bot._executed) == 1
    assert bot._executed[0]["symbol"] == "BTCUSDT"
    assert bot._executed[0]["decision"].operation == FundFlowOperation.CLOSE


def test_finalize_entries_ai_review_rejects_weak_trend_candidate_without_structure():
    bot = _make_bot(active_symbols={})
    bot._ai_review_mode_supports_flat_candidates = lambda _mode: True
    bot.fund_flow_decision_engine = SimpleNamespace(
        decide=lambda **kwargs: _decision_with_md(
            "XLMUSDT",
            FundFlowOperation.BUY,
            {
                "engine": "TREND",
                "ds_source": "ai_weight_router",
                "ds_confidence": 0.82,
                "test_score": 0.068,
                "final": {"need_confirm": True},
            },
        )
    )
    context = {
        "pending_new_entries": [
            {
                "symbol": "XLMUSDT",
                "score": 0.068,
                "max_active_symbols": 2,
                "decision": _decision_with_md(
                    "XLMUSDT",
                    FundFlowOperation.BUY,
                    {"engine": "TREND", "test_score": 0.068, "final": {"need_confirm": True}},
                ),
                "position": None,
                "current_price": 0.165,
                "flow_context": {
                    "regime": "TREND",
                    "flow_confirm": False,
                    "trap_score": 0.52,
                    "trap_confirmed": False,
                    "capture_confirm_3m_side": "NONE",
                    "ma10_bias_1h": "FLAT",
                    "macd_cross_5m": "NONE",
                    "macd_zone_5m": "BELOW_ZERO",
                },
                "trigger_context": {},
                "portfolio": {},
                "account_summary": {"available_balance": 1000.0},
            }
        ],
        "block_new_entries_due_to_protection_gap": False,
        "protection_gap_symbols": [],
        "max_active_symbols": 2,
        "account_summary": {"available_balance": 1000.0},
        "ai_gate_enabled": True,
        "ai_review_cfg": {"enabled": True},
        "ai_review_mode": "flat_candidates",
    }

    bot._finalize_entries(context)

    assert bot._executed == []


def test_ai_entry_guard_blocks_weak_same_side_add():
    bot = TradingBot.__new__(TradingBot)
    allowed, reason = bot._ai_entry_guard(
        decision=_decision_with_md(
            "TRXUSDT",
            FundFlowOperation.BUY,
            {"engine": "TREND", "final": {"need_confirm": False}},
        ),
        local_score=0.076,
        flow_context={
            "regime": "TREND",
            "flow_confirm": True,
            "trap_score": 0.12,
            "trap_confirmed": False,
            "capture_confirm_3m_side": "LONG",
            "ma10_bias_1h": "UP",
            "macd_cross_5m": "GOLDEN",
            "macd_zone_5m": "ABOVE_ZERO",
        },
        ai_review_cfg={"final_same_side_add_min_score": 0.11},
        position={"side": "LONG"},
    )

    assert allowed is False
    assert "same_side_add_score" in reason
