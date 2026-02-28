from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .enhanced_risk import RiskConfig


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _append_log(path: Optional[str], payload: Dict[str, Any]) -> None:
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def gate_trade_decision(
    state: Dict[str, Any],
    *,
    config: Optional[RiskConfig] = None,
    equity_fraction: float = 0.1,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pre-trade gate:
    - Hard constraints first (drawdown / exposure)
    - Composite entry score second
    """
    cfg = config if isinstance(config, RiskConfig) else RiskConfig()
    direction = str((state or {}).get("direction", "NONE")).upper()

    trend = _clamp01(_safe_float((state or {}).get("trend"), 0.0))
    momentum = _clamp01(_safe_float((state or {}).get("momentum"), 0.0))
    volatility = _clamp01(_safe_float((state or {}).get("volatility"), 0.0))
    drawdown = _clamp01(_safe_float((state or {}).get("drawdown"), 0.0))
    eq_frac = max(0.0, _safe_float(equity_fraction, _safe_float((state or {}).get("equity_fraction"), 0.0)))

    hard_block = False
    hard_reason = ""
    if drawdown >= max(1e-6, float(cfg.max_drawdown)):
        hard_block = True
        hard_reason = f"drawdown_exceeded({drawdown:.4f}>={float(cfg.max_drawdown):.4f})"
    elif eq_frac > max(1e-6, float(cfg.max_exposure_per_trade)):
        hard_block = True
        hard_reason = f"exposure_exceeded({eq_frac:.4f}>{float(cfg.max_exposure_per_trade):.4f})"

    score = (
        float(cfg.trend_weight) * trend
        + float(cfg.momentum_weight) * momentum
        - float(cfg.volatility_weight) * volatility
        - float(cfg.drawdown_weight) * drawdown
    )
    score = max(-1.0, min(1.0, score))

    can_enter_dir = direction in ("LONG", "SHORT")
    action = "HOLD"
    enter = False
    exit_ = False
    if hard_block:
        if can_enter_dir:
            action = "EXIT"
            exit_ = True
        else:
            action = "HOLD"
    else:
        if can_enter_dir and score >= float(cfg.entry_threshold):
            action = "ENTER"
            enter = True
        elif can_enter_dir and score < 0:
            action = "EXIT"
            exit_ = True

    details = {
        "trend": trend,
        "momentum": momentum,
        "volatility": volatility,
        "drawdown": drawdown,
        "equity_fraction": eq_frac,
        "entry_threshold": float(cfg.entry_threshold),
        "hard_block": hard_block,
        "hard_reason": hard_reason,
    }
    result = {
        "action": action,
        "enter": enter,
        "exit": exit_,
        "score": float(score),
        "details": details,
    }

    _append_log(
        log_path,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "state": state,
            "result": result,
        },
    )
    return result

