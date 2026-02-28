import pytest

from src.fund_flow.models import FundFlowDecision, Operation
from src.fund_flow.risk_engine import FundFlowRiskEngine


def _cfg():
    return {
        "trading": {"max_leverage": 10, "default_leverage": 3},
        "fund_flow": {
            "min_open_portion": 0.1,
            "max_open_portion": 1.0,
            "price_deviation_limit_percent": 1.0,
        },
    }


def test_clamp_leverage_out_of_range_fallback():
    engine = FundFlowRiskEngine(_cfg(), symbol_whitelist=["BTCUSDT"])
    assert engine.clamp_leverage(999) == 10
    assert engine.clamp_leverage(0) == 2


def test_validate_open_portion_range():
    engine = FundFlowRiskEngine(_cfg(), symbol_whitelist=["BTCUSDT"])
    with pytest.raises(ValueError):
        engine.validate_target_portion(0.05, Operation.BUY)
    assert engine.validate_target_portion(0.2, Operation.BUY) == 0.2


def test_enforce_price_bounds():
    engine = FundFlowRiskEngine(_cfg(), symbol_whitelist=["BTCUSDT"])
    bounded = engine.enforce_price_bounds(price=102.5, oracle_price=100.0)
    assert bounded == pytest.approx(101.0)


def test_validate_decision_symbol_whitelist():
    engine = FundFlowRiskEngine(_cfg(), symbol_whitelist=["BTCUSDT"])
    decision = FundFlowDecision(operation=Operation.BUY, symbol="ETHUSDT", target_portion_of_balance=0.2)
    with pytest.raises(ValueError):
        engine.validate_decision(decision)
