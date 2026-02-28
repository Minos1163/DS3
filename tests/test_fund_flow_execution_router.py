import math
from pathlib import Path

from src.fund_flow.attribution_engine import FundFlowAttributionEngine
from src.fund_flow.execution_router import FundFlowExecutionRouter
from src.fund_flow.models import FundFlowDecision, Operation, TimeInForce
from src.fund_flow.risk_engine import FundFlowRiskEngine


class _DummyBroker:
    @staticmethod
    def get_hedge_mode():
        return True


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.protection_calls = []
        self.cancel_calls = []
        self.broker = _DummyBroker()

    def format_quantity(self, _symbol, qty):
        return round(float(qty), 6)

    def _execute_order_v2(self, params, side, reduce_only):
        self.calls.append({"params": dict(params), "side": side, "reduce_only": reduce_only})
        if not self.responses:
            return {"status": "error", "message": "no fake response"}
        return self.responses.pop(0)

    def _execute_protection_v2(self, symbol, side, tp, sl):
        self.protection_calls.append({"symbol": symbol, "side": side.value, "tp": tp, "sl": sl})
        return {"status": "success", "orders": []}

    def cancel_all_open_orders(self, symbol):
        self.cancel_calls.append(symbol)
        return {"status": "success"}


class _FloorQtyClient(_FakeClient):
    def format_quantity(self, _symbol, qty):
        step = 0.001
        val = math.floor(float(qty) / step) * step
        return round(val, 3)


def _risk_cfg():
    return {
        "trading": {"default_leverage": 2, "max_leverage": 5},
        "fund_flow": {"min_open_portion": 0.1, "max_open_portion": 1.0},
    }


def test_open_ioc_fallback_to_gtc(tmp_path: Path):
    client = _FakeClient(
        responses=[
            {"status": "error", "code": -2010, "msg": "Order would immediately match and take"},
            {"orderId": 123, "status": "NEW"},
        ]
    )
    risk = FundFlowRiskEngine(_risk_cfg(), symbol_whitelist=["BTCUSDT"])
    attr = FundFlowAttributionEngine(str(tmp_path))
    router = FundFlowExecutionRouter(client, risk, attr)

    decision = FundFlowDecision(
        operation=Operation.BUY,
        symbol="BTCUSDT",
        target_portion_of_balance=0.2,
        leverage=2,
        max_price=100.0,
        take_profit_price=110.0,
        stop_loss_price=95.0,
        time_in_force=TimeInForce.IOC,
    )
    result = router.execute_decision(
        decision=decision,
        account_state={"available_balance": 1000.0},
        current_price=100.0,
        position=None,
    )

    assert result["status"] == "pending"
    assert len(client.calls) == 2
    assert client.calls[0]["params"]["timeInForce"] == "IOC"
    assert client.calls[1]["params"]["timeInForce"] == "GTC"
    assert len(client.protection_calls) == 0


def test_close_ioc_retry_then_success(tmp_path: Path):
    client = _FakeClient(
        responses=[
            {"status": "NEW", "executedQty": "0"},
            {"status": "NEW", "executedQty": "0"},
            {"status": "FILLED", "executedQty": "1", "orderId": 888},
        ]
    )
    risk = FundFlowRiskEngine(_risk_cfg(), symbol_whitelist=["BTCUSDT"])
    attr = FundFlowAttributionEngine(str(tmp_path))
    router = FundFlowExecutionRouter(client, risk, attr, close_retry_times=4)

    decision = FundFlowDecision(
        operation=Operation.CLOSE,
        symbol="BTCUSDT",
        target_portion_of_balance=1.0,
        leverage=2,
        min_price=99.0,
    )
    result = router.execute_decision(
        decision=decision,
        account_state={"available_balance": 1000.0},
        current_price=100.0,
        position={"side": "LONG", "amount": 1.0},
    )

    assert result["status"] == "success"
    assert result["retry_index"] == 2
    assert len(client.calls) == 3
    assert client.calls[0]["reduce_only"] is True


def test_close_partial_qty_rounded_to_zero_promotes_to_full_close(tmp_path: Path):
    client = _FloorQtyClient(
        responses=[
            {"status": "FILLED", "executedQty": "0.002", "orderId": 777},
        ]
    )
    risk = FundFlowRiskEngine(_risk_cfg(), symbol_whitelist=["BTCUSDT"])
    attr = FundFlowAttributionEngine(str(tmp_path))
    router = FundFlowExecutionRouter(client, risk, attr, close_retry_times=2)

    decision = FundFlowDecision(
        operation=Operation.CLOSE,
        symbol="BTCUSDT",
        target_portion_of_balance=0.25,
        leverage=2,
        min_price=99.0,
    )
    result = router.execute_decision(
        decision=decision,
        account_state={"available_balance": 1000.0},
        current_price=100.0,
        position={"side": "LONG", "amount": 0.002},
    )

    assert result["status"] == "success"
    assert result["quantity"] == 0.002
    assert result["quantity_info"]["promoted_to_full_close"] is True
    assert result["quantity_info"]["promotion_reason"] == "partial_qty_rounded_to_zero"
    assert len(client.calls) == 1
    assert client.calls[0]["params"]["quantity"] == 0.002
