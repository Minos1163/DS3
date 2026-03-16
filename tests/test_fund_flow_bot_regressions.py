from pathlib import Path
import re

from src.app.fund_flow_bot import TradingBot


def test_trading_bot_does_not_use_missing_private_config_attr():
    source = Path("src/app/fund_flow_bot.py").read_text(encoding="utf-8")
    assert re.search(r"self\._config(?![A-Za-z0-9_])", source) is None


def test_soften_conflict_exit_for_small_mae_downgrades_to_reduce():
    bot = TradingBot.__new__(TradingBot)
    out = bot._soften_conflict_exit_for_small_mae(
        protection={
            "risk_state": "CIRCUIT_EXIT",
            "reason": "熔断 test",
            "state_deep_break": False,
            "reduce_position_pct": 1.0,
            "force_break_even": False,
        },
        drawdown_ratio=0.0013,
        conflict_cfg_hard={"hard_exit_min_mae": 0.002, "state_reduce_pct": 0.35},
    )

    assert out["softened"] is True
    assert out["risk_state"] == "REDUCE"
    assert out["force_break_even"] is True
    assert out["force_reduce_signal"] is True
    assert out["reduce_pct"] == 0.35


def test_soften_conflict_exit_for_small_mae_keeps_circuit_exit_on_deep_break():
    bot = TradingBot.__new__(TradingBot)
    out = bot._soften_conflict_exit_for_small_mae(
        protection={
            "risk_state": "CIRCUIT_EXIT",
            "reason": "熔断 test",
            "state_deep_break": True,
            "reduce_position_pct": 1.0,
            "force_break_even": False,
        },
        drawdown_ratio=0.0013,
        conflict_cfg_hard={"hard_exit_min_mae": 0.002, "state_reduce_pct": 0.35},
    )

    assert out["softened"] is False
    assert out["risk_state"] == "CIRCUIT_EXIT"
