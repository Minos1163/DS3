#!/usr/bin/env python3
"""
End-to-end style trading execution simulation (no real exchange).
This script demonstrates the ENTER/EXIT/HOLD branches by temporarily
overriding the risk gate decision function and invoking the trading entrypoint.
It also demonstrates a protective order hook for the ENTER path.
"""
from __future__ import annotations

import json
import risk.integration_gate as ig
from trading.entrypoint import perform_trade

def _fake_gate_enter(state, *args, **kwargs):
    return {"action": "ENTER", "enter": True, "exit": False, "score": 0.85, "details": {}}

def _fake_gate_exit(state, *args, **kwargs):
    return {"action": "EXIT", "enter": False, "exit": True, "score": 0.9, "details": {}}

def _fake_gate_hold(state, *args, **kwargs):
    return {"action": "HOLD", "enter": False, "exit": False, "score": 0.4, "details": {}}

def _fake_protect(state, **kwargs):
    return {"success": True, "protect_order_id": "PROT-E2E-1"}

def main():
    # ENTER path with protection hook
    ig.gate_trade_decision = _fake_gate_enter
    res_enter = perform_trade({"trend": 0.9, "equity_fraction": 0.2}, asset="BTCUSDT", amount=0.01, price=50000.0, protect_hook=_fake_protect)
    print("ENTER path:", json.dumps(res_enter, indent=2))

    # EXIT path
    ig.gate_trade_decision = _fake_gate_exit
    res_exit = perform_trade({"trend": -0.2, "equity_fraction": 0.2}, asset="BTCUSDT", amount=0.01, price=51000.0)
    print("EXIT path:", json.dumps(res_exit, indent=2))

    # HOLD path
    ig.gate_trade_decision = _fake_gate_hold
    res_hold = perform_trade({"trend": 0.1, "equity_fraction": 0.2}, asset="ETHUSDT", amount=0.5, price=1800.0)
    print("HOLD path:", json.dumps(res_hold, indent=2))

if __name__ == "__main__":
    main()
