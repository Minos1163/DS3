#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fund_flow.decision_engine import FundFlowDecisionEngine
from src.fund_flow.execution_router import FundFlowExecutionRouter


def _load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate fund-flow execution setup.")
    parser.add_argument(
        "--config",
        type=str,
        default=str(ROOT / "config" / "trading_config_fund_flow.json"),
        help="Path to config JSON.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"FAIL: config not found: {config_path}")
        return 2

    cfg = _load_config(config_path)
    ff = cfg.get("fund_flow", {}) if isinstance(cfg.get("fund_flow"), dict) else {}
    degr = ff.get("execution_degradation", {}) if isinstance(ff.get("execution_degradation"), dict) else {}

    engine = FundFlowDecisionEngine(cfg)
    _ = FundFlowExecutionRouter  # import-level smoke check

    open_market_fb = bool(degr.get("open_market_fallback_enabled", False))
    close_market_fb = bool(degr.get("close_market_fallback_enabled", False))
    strict_lev = bool(ff.get("strict_leverage_sync", True))
    rollback_tp_sl = bool(ff.get("rollback_on_tp_sl_fail", True))

    print("OK: decision engine init passed")
    print(
        "CONFIG: "
        f"open_market_fallback_enabled={open_market_fb}, "
        f"close_market_fallback_enabled={close_market_fb}, "
        f"strict_leverage_sync={strict_lev}, "
        f"rollback_on_tp_sl_fail={rollback_tp_sl}, "
        f"deepseek_router_enabled={engine.deepseek_router.enabled}, "
        f"deepseek_router_ai_enabled={engine.deepseek_router.ai_enabled}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

