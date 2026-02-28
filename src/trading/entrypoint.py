#!/usr/bin/env python3
"""
Trading execution integration gateway.

This module provides a concrete trading entry point that:
- Pre-checks risk via the Gate (risk/integration_gate.py)
- Branches logic based on the gate decision (ENTER/EXIT/HOLD)
- Simulates a trade execution (since we don't have a live broker here)
- Returns a structured result for tests and downstream pipelines
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from risk.integration_gate import gate_trade_decision

def _mock_order_id() -> str:
    return f"ORD-{int(time.time())}"

def pre_trade_decision(state: Dict[str, Any], *, equity_fraction: float = 0.1, log_path: Optional[str] = None) -> Dict[str, Any]:
    return gate_trade_decision(state, equity_fraction=equity_fraction, log_path=log_path)

def perform_trade(state: Dict[str, Any], asset: str = "BTCUSDT", amount: float = 0.0, price: Optional[float] = None, log_path: Optional[str] = None, protect_hook=None, protect_args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # Pre-trade risk decision
    decision = pre_trade_decision(state, equity_fraction=state.get("equity_fraction", 0.1), log_path=log_path)
    action = decision.get("action", "HOLD")
    result: Dict[str, Any] = {"asset": asset, "amount": amount, "price": price, "action": action}
    # Optional protection order hook (ENTER path)
    if action == "ENTER" and callable(protect_hook):
        try:
            hook_resp = protect_hook(state, asset=asset, amount=amount, price=price, **(protect_args or {}))
            if isinstance(hook_resp, dict):
                ok = bool(hook_resp.get("success", True))
                if ok:
                    pid = hook_resp.get("protect_order_id")
                    if pid:
                        result["protect_order_id"] = pid
                    result["protect_status"] = "created"
                else:
                    # protection failed -> abort enter
                    action = "HOLD"
                    result["action"] = action
                    result["status"] = "no_action"
                    result["protect_status"] = "failed"
                    result["reason"] = "protect_hook_failed"
            else:
                # If hook did not return a dict, treat as failure to create protection
                action = "HOLD"
                result["action"] = action
                result["status"] = "no_action"
                result["protect_status"] = "failed"
                result["reason"] = "protect_hook_invalid_response"
        except Exception:
            # On hook error, do not enter
            action = "HOLD"
            result["action"] = action
            result["status"] = "no_action"
            result["protect_status"] = "error"
            result["reason"] = "protect_hook_error"
    if action == "ENTER":
        # Simulate an order placement
        result.update({
            "status": "ordered",
            "order_id": _mock_order_id(),
            "decision_score": decision.get("score"),
        })
    elif action == "EXIT":
        result.update({
            "status": "closed",
            "order_id": None,
            "decision_score": decision.get("score"),
        })
    else:  # HOLD or unknown
        result.update({
            "status": "no_action",
            "order_id": None,
            "decision_score": decision.get("score"),
        })
    return result
