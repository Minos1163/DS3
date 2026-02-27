import pytest

from trading.entrypoint import perform_trade


def test_enter(monkeypatch):
    def fake_gate(state, *args, **kwargs):
        return {"action": "ENTER", "enter": True, "exit": False, "score": 0.85, "details": {}}

    import risk.integration_gate as ig
    monkeypatch.setattr(ig, "gate_trade_decision", fake_gate)

    res = perform_trade({"trend": 0.9, "equity_fraction": 0.2}, asset="BTCUSDT", amount=0.01, price=50000.0)
    assert res["action"] == "ENTER"
    assert res["status"] == "ordered"
    assert res.get("order_id") is not None


def test_enter_with_protect_success(monkeypatch):
    def fake_gate(state, *args, **kwargs):
        return {"action": "ENTER", "enter": True, "exit": False, "score": 0.85, "details": {}}

    def fake_protect(state, **kwargs):
        return {"success": True, "protect_order_id": "PROT-001"}

    import risk.integration_gate as ig
    monkeypatch.setattr(ig, "gate_trade_decision", fake_gate)

    res = perform_trade({"trend": 0.9}, asset="BTCUSDT", amount=0.01, price=50000.0, protect_hook=fake_protect)
    assert res["action"] == "ENTER"
    assert res.get("protect_order_id") == "PROT-001"
    assert res["status"] == "ordered"


def test_enter_with_protect_fail(monkeypatch):
    def fake_gate(state, *args, **kwargs):
        return {"action": "ENTER", "enter": True, "exit": False, "score": 0.85, "details": {}}

    def fake_protect(state, **kwargs):
        return {"success": False}

    import risk.integration_gate as ig
    monkeypatch.setattr(ig, "gate_trade_decision", fake_gate)

    res = perform_trade({"trend": 0.9}, asset="BTCUSDT", amount=0.01, price=50000.0, protect_hook=fake_protect)
    assert res["action"] == "HOLD"
    assert res["status"] == "no_action"
    assert res.get("protect_status") == "failed"


def test_exit(monkeypatch):
    def fake_gate(state, *args, **kwargs):
        return {"action": "EXIT", "enter": False, "exit": True, "score": 0.9, "details": {}}

    import risk.integration_gate as ig
    monkeypatch.setattr(ig, "gate_trade_decision", fake_gate)

    res = perform_trade({"trend": -0.4, "equity_fraction": 0.2}, asset="BTCUSDT", amount=0.01, price=52000.0)
    assert res["action"] == "EXIT"
    assert res["status"] == "closed"
    assert res.get("order_id") is None


def test_hold(monkeypatch):
    def fake_gate(state, *args, **kwargs):
        return {"action": "HOLD", "enter": False, "exit": False, "score": 0.4, "details": {}}

    import risk.integration_gate as ig
    monkeypatch.setattr(ig, "gate_trade_decision", fake_gate)

    res = perform_trade({"trend": 0.1, "equity_fraction": 0.2}, asset="ETHUSDT", amount=0.5, price=1800.0)
    assert res["action"] == "HOLD"
    assert res["status"] == "no_action"
