import gzip
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.fund_flow.attribution_engine import FundFlowAttributionEngine
from src.fund_flow.log_compaction import compact_decision_payload, compact_flow_context_payload
from src.fund_flow.market_storage import MarketStorage
from src.fund_flow.models import FundFlowDecision, Operation, TimeInForce


def _sample_decision() -> FundFlowDecision:
    metadata = {
        "trigger": "signal_pool",
        "engine": "fund_flow",
        "regime": "TREND",
        "direction_lock": "LONG",
        "signal_pool_id": "pool_a",
        "score_15m": {"long": 2.3456789, "short": -1.2345678, "signal_strength": 0.9876543},
        "score_5m": {"long": 1.3456789, "short": -0.2345678, "signal_strength": 0.8876543},
        "final_score": {"long": 3.6913578, "short": -1.4691356},
        "fusion_info": {"mode": "weighted", "confidence": 0.82, "details": {"retry": False}},
    }
    for idx in range(40):
        metadata[f"very_large_debug_key_{idx}"] = {"nested": list(range(20)), "text": "x" * 80}
    return FundFlowDecision(
        operation=Operation.BUY,
        symbol="BTCUSDT",
        target_portion_of_balance=0.25,
        leverage=3,
        max_price=100.123456789,
        take_profit_price=108.987654321,
        stop_loss_price=96.543219876,
        time_in_force=TimeInForce.IOC,
        reason="signal aligned for breakout continuation",
        metadata=metadata,
    )


def _sample_flow_context() -> dict:
    base = {
        "cvd_ratio": 1.23456789,
        "cvd_momentum": 0.22334455,
        "oi_delta_ratio": 0.55667788,
        "funding_rate": 0.000123456,
        "depth_ratio": 1.123456,
        "imbalance": 0.223344,
        "liquidity_delta_norm": 0.334455,
        "mid_price": 100.123456,
        "microprice": 100.234567,
        "micro_delta_norm": 0.111222,
        "spread_bps": 3.456789,
        "phantom": 0.456789,
        "trap_score": 0.222333,
        "signal_strength": 1.987654,
        "active_timeframe": "15m",
    }
    timeframes = {}
    for tf in ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"):
        timeframes[tf] = {
            "cvd_ratio": 1.0,
            "cvd_momentum": 0.2,
            "oi_delta_ratio": 0.3,
            "funding_rate": 0.0001,
            "depth_ratio": 1.1,
            "imbalance": 0.2,
            "liquidity_delta_norm": 0.3,
            "micro_delta_norm": 0.1,
            "spread_bps": 4.5,
            "phantom": 0.4,
            "trap_score": 0.2,
            "signal_strength": 1.1,
            "debug_blob": list(range(30)),
        }
    base["timeframes"] = timeframes
    return base


def test_compaction_trims_large_decision_and_flow_context():
    decision = _sample_decision()
    flow_context = _sample_flow_context()

    raw_decision = json.dumps(decision.to_dict(), ensure_ascii=False)
    compact_decision = json.dumps(compact_decision_payload(decision), ensure_ascii=False)
    assert len(compact_decision) < len(raw_decision) * 0.5

    compact_flow = compact_flow_context_payload(flow_context)
    assert set(compact_flow["timeframes"].keys()) == {"5m", "15m"}
    assert compact_flow["timeframes_omitted"] == 6


def test_attribution_engine_archives_old_logs_and_writes_compact_payload(tmp_path: Path):
    root = tmp_path / "logs"
    old_dir = root / "2026-03" / "2026-03-10"
    old_dir.mkdir(parents=True)
    old_path = old_dir / "fund_flow_attribution.jsonl"
    old_path.write_text('{"ts":"2026-03-10T00:00:00","event":"decision"}\n', encoding="utf-8")
    old_mtime = (datetime.now() - timedelta(days=5)).timestamp()
    old_path.touch()
    old_path.chmod(0o666)

    import os

    os.utime(old_path, (old_mtime, old_mtime))

    engine = FundFlowAttributionEngine(
        str(root / "current"),
        bucket_root_dir=str(root),
        raw_keep_days=1,
    )
    assert not old_path.exists()
    gz_path = Path(str(old_path) + ".gz")
    assert gz_path.exists()
    with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
        assert '"event":"decision"' in handle.read()

    decision = _sample_decision()
    flow_context = _sample_flow_context()
    engine.log_decision(
        decision=decision,
        context={
            "symbol": "BTCUSDT",
            "price": 100.0,
            "portfolio": {"cash": 1000.0, "total_assets": 1200.0, "positions": {"ETHUSDT": {"amount": 1.2}}},
            "flow_context": flow_context,
            "trigger_context": {"trigger_type": "signal_pool", "signal_pool_id": "pool_a", "allow_entry_window": True},
        },
    )

    now = datetime.now()
    current_path = root / now.strftime("%Y-%m") / now.strftime("%Y-%m-%d") / "fund_flow_attribution.jsonl"
    line = current_path.read_text(encoding="utf-8").strip()
    raw_line = json.dumps(
        {
            "ts": "x",
            "event": "decision",
            "decision": decision.to_dict(),
            "context": {
                "symbol": "BTCUSDT",
                "price": 100.0,
                "portfolio": {"cash": 1000.0, "total_assets": 1200.0, "positions": {"ETHUSDT": {"amount": 1.2}}},
                "flow_context": flow_context,
                "trigger_context": {"trigger_type": "signal_pool", "signal_pool_id": "pool_a", "allow_entry_window": True},
            },
        },
        ensure_ascii=False,
    )
    assert len(line) < len(raw_line) * 0.5
    payload = json.loads(line)
    assert payload["decision"]["metadata"]["_omitted_keys"] > 0
    assert set(payload["context"]["flow_context"]["timeframes"].keys()) == {"5m", "15m"}


def test_market_storage_housekeeping_prunes_old_audit_rows(tmp_path: Path):
    db_path = tmp_path / "fund_flow_strategy.db"
    storage = MarketStorage(str(db_path), audit_log_retention_days=1)

    old_ts = (datetime.utcnow() - timedelta(days=3)).isoformat()
    new_ts = datetime.utcnow().isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO ai_decision_logs(timestamp, symbol, operation, decision_json, trigger_type, trigger_id, order_id, tp_order_id, sl_order_id, realized_pnl, exchange)
        VALUES (?, 'BTCUSDT', 'BUY', '{}', NULL, NULL, NULL, NULL, NULL, NULL, 'binance')
        """,
        (old_ts,),
    )
    conn.execute(
        """
        INSERT INTO ai_decision_logs(timestamp, symbol, operation, decision_json, trigger_type, trigger_id, order_id, tp_order_id, sl_order_id, realized_pnl, exchange)
        VALUES (?, 'ETHUSDT', 'SELL', '{}', NULL, NULL, NULL, NULL, NULL, NULL, 'binance')
        """,
        (new_ts,),
    )
    conn.execute(
        """
        INSERT INTO program_execution_logs(timestamp, symbol, operation, decision_json, market_context_json, params_snapshot_json, order_id, environment, exchange)
        VALUES (?, 'BTCUSDT', 'BUY', '{}', '{}', '{}', NULL, 'prod', 'binance')
        """,
        (old_ts,),
    )
    conn.commit()
    conn.close()

    stats = storage.run_housekeeping()
    assert stats["ai_decision_logs"] == 1
    assert stats["program_execution_logs"] == 1

    conn = sqlite3.connect(str(db_path))
    ai_count = conn.execute("SELECT COUNT(*) FROM ai_decision_logs").fetchone()[0]
    exec_count = conn.execute("SELECT COUNT(*) FROM program_execution_logs").fetchone()[0]
    conn.close()
    assert ai_count == 1
    assert exec_count == 0
