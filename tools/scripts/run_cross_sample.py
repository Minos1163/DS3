#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fund_flow.decision_engine import FundFlowDecisionEngine


def _load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    return data


def _sample_contexts() -> List[Tuple[str, Dict[str, Any], float]]:
    base = {
        "cvd_ratio": 0.08,
        "cvd_momentum": 0.05,
        "oi_delta_ratio": 0.06,
        "funding_rate": 0.0001,
        "depth_ratio": 1.03,
        "imbalance": 0.12,
        "liquidity_delta_norm": 0.03,
        "timeframes": {
            "15m": {
                "cvd_ratio": 0.08,
                "cvd_momentum": 0.05,
                "oi_delta_ratio": 0.06,
                "funding_rate": 0.0001,
                "depth_ratio": 1.03,
                "imbalance": 0.12,
                "liquidity_delta_norm": 0.03,
                "adx": 28.0,
                "atr_pct": 0.004,
                "ema_fast": 101.0,
                "ema_slow": 99.0,
                "ret_period": 0.002,
                "timestamp_close_utc": "2026-02-25T00:00:00+00:00",
            },
            "5m": {
                "cvd_ratio": 0.06,
                "cvd_momentum": 0.04,
                "oi_delta_ratio": 0.05,
                "funding_rate": 0.0001,
                "depth_ratio": 1.02,
                "imbalance": 0.10,
                "liquidity_delta_norm": 0.02,
                "spread_z": 0.6,
            },
        },
    }

    trend_ctx = copy.deepcopy(base)
    range_ctx = copy.deepcopy(base)
    range_ctx["timeframes"]["15m"]["adx"] = 16.0
    range_ctx["timeframes"]["15m"]["ema_fast"] = 100.1
    range_ctx["timeframes"]["15m"]["ema_slow"] = 100.0
    range_ctx["imbalance"] = -0.08
    range_ctx["cvd_momentum"] = -0.04
    range_ctx["timeframes"]["5m"]["imbalance"] = -0.1
    range_ctx["timeframes"]["5m"]["cvd_momentum"] = -0.05

    return [
        ("TREND_SAMPLE", trend_ctx, 50000.0),
        ("RANGE_SAMPLE", range_ctx, 50000.0),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run cross-regime decision smoke samples.")
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
    # 交叉样本测试只验证逻辑链路，避免触发外部 AI 请求。
    ff = cfg.get("fund_flow", {}) if isinstance(cfg.get("fund_flow"), dict) else {}
    ds_cfg = ff.get("deepseek_weight_router", {}) if isinstance(ff.get("deepseek_weight_router"), dict) else {}
    ai_cfg = ff.get("deepseek_ai", {}) if isinstance(ff.get("deepseek_ai"), dict) else {}
    ds_cfg["ai_enabled"] = False
    ai_cfg["enabled"] = False
    ff["deepseek_weight_router"] = ds_cfg
    ff["deepseek_ai"] = ai_cfg
    cfg["fund_flow"] = ff

    engine = FundFlowDecisionEngine(cfg)
    portfolio = {"positions": {}}

    for tag, ctx, price in _sample_contexts():
        decision = engine.decide(
            symbol="BTCUSDT",
            portfolio=portfolio,
            price=price,
            market_flow_context=ctx,
            trigger_context={"source": "run_cross_sample"},
        )
        print(
            f"{tag}: operation={decision.operation.value}, "
            f"reason={decision.reason}, leverage={decision.leverage}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

