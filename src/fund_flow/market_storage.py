from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional


class MarketStorage:
    """
    迁移文档要求的最小落库能力（SQLite）：
    - crypto_klines
    - market_* 聚合表
    - ai_decision_logs / program_execution_logs
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # 运行期间日志目录可能被外部轮转/清理，连接前再次确保目录存在。
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS crypto_klines (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            period TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            environment TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (exchange, symbol, market, period, timestamp, environment)
        );

        CREATE TABLE IF NOT EXISTS market_trades_aggregated (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            cvd_ratio REAL,
            cvd_momentum REAL,
            signal_strength REAL,
            PRIMARY KEY (exchange, symbol, timestamp)
        );

        CREATE TABLE IF NOT EXISTS market_orderbook_snapshots (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            depth_ratio REAL,
            imbalance REAL,
            PRIMARY KEY (exchange, symbol, timestamp)
        );

        CREATE TABLE IF NOT EXISTS market_asset_metrics (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            oi_delta_ratio REAL,
            funding_rate REAL,
            PRIMARY KEY (exchange, symbol, timestamp)
        );

        CREATE TABLE IF NOT EXISTS market_flow_timeframes (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            cvd_ratio REAL,
            cvd_momentum REAL,
            oi_delta_ratio REAL,
            funding_rate REAL,
            depth_ratio REAL,
            imbalance REAL,
            liquidity_delta_norm REAL,
            signal_strength REAL,
            sample_count REAL,
            window_seconds REAL,
            PRIMARY KEY (exchange, symbol, timeframe, timestamp)
        );

        CREATE TABLE IF NOT EXISTS market_sentiment_metrics (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            sentiment_score REAL,
            source TEXT,
            PRIMARY KEY (exchange, symbol, timestamp)
        );

        CREATE TABLE IF NOT EXISTS ai_decision_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            operation TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            trigger_type TEXT,
            trigger_id TEXT,
            order_id TEXT,
            tp_order_id TEXT,
            sl_order_id TEXT,
            realized_pnl REAL,
            exchange TEXT
        );

        CREATE TABLE IF NOT EXISTS program_execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            operation TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            market_context_json TEXT,
            params_snapshot_json TEXT,
            order_id TEXT,
            environment TEXT,
            exchange TEXT
        );

        CREATE TABLE IF NOT EXISTS signal_definitions (
            id TEXT PRIMARY KEY,
            signal_name TEXT NOT NULL,
            side TEXT DEFAULT 'BOTH',
            metric TEXT NOT NULL,
            operator TEXT NOT NULL,
            threshold REAL,
            threshold_max REAL,
            timeframe TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'config',
            extra_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signal_pools (
            id TEXT PRIMARY KEY,
            pool_name TEXT NOT NULL,
            logic TEXT NOT NULL DEFAULT 'AND',
            min_pass_count INTEGER NOT NULL DEFAULT 0,
            min_long_score REAL NOT NULL DEFAULT 0.0,
            min_short_score REAL NOT NULL DEFAULT 0.0,
            scheduled_trigger_bypass INTEGER NOT NULL DEFAULT 1,
            apply_when_position_exists INTEGER NOT NULL DEFAULT 0,
            edge_trigger_enabled INTEGER NOT NULL DEFAULT 1,
            edge_cooldown_seconds INTEGER NOT NULL DEFAULT 0,
            symbols_json TEXT,
            signal_ids_json TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'config',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weight_router_cache (
            cache_key TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            regime TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            weights_json TEXT NOT NULL,
            confidence REAL,
            fallback_used INTEGER,
            regime_view_json TEXT,
            risk_flags_json TEXT,
            reasoning_bullets_json TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_weight_router_cache_symbol 
        ON weight_router_cache(symbol, regime);
        
        CREATE INDEX IF NOT EXISTS idx_weight_router_cache_expires 
        ON weight_router_cache(expires_at);
        """
        with self._connect() as conn:
            conn.executescript(ddl)

    @staticmethod
    def _ts(value: Optional[datetime]) -> str:
        return (value or datetime.utcnow()).isoformat()

    def upsert_kline(
        self,
        *,
        exchange: str,
        symbol: str,
        market: str,
        period: str,
        timestamp: datetime,
        environment: str,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: float,
    ) -> None:
        sql = """
        INSERT INTO crypto_klines (
            exchange, symbol, market, period, timestamp, environment,
            open, high, low, close, volume
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(exchange, symbol, market, period, timestamp, environment)
        DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume;
        """
        with self._connect() as conn:
            conn.execute(
                sql,
                (
                    exchange,
                    symbol,
                    market,
                    period,
                    self._ts(timestamp),
                    environment,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    volume,
                ),
            )

    def upsert_market_flow(self, *, exchange: str, symbol: str, timestamp: datetime, metrics: Dict[str, Any]) -> None:
        ts = self._ts(timestamp)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO market_trades_aggregated(exchange, symbol, timestamp, cvd_ratio, cvd_momentum, signal_strength)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol, timestamp)
                DO UPDATE SET
                    cvd_ratio=excluded.cvd_ratio,
                    cvd_momentum=excluded.cvd_momentum,
                    signal_strength=excluded.signal_strength
                """,
                (
                    exchange,
                    symbol,
                    ts,
                    float(metrics.get("cvd_ratio", 0.0) or 0.0),
                    float(metrics.get("cvd_momentum", 0.0) or 0.0),
                    float(metrics.get("signal_strength", 0.0) or 0.0),
                ),
            )
            conn.execute(
                """
                INSERT INTO market_orderbook_snapshots(exchange, symbol, timestamp, depth_ratio, imbalance)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol, timestamp)
                DO UPDATE SET
                    depth_ratio=excluded.depth_ratio,
                    imbalance=excluded.imbalance
                """,
                (
                    exchange,
                    symbol,
                    ts,
                    float(metrics.get("depth_ratio", 1.0) or 1.0),
                    float(metrics.get("imbalance", 0.0) or 0.0),
                ),
            )
            conn.execute(
                """
                INSERT INTO market_asset_metrics(exchange, symbol, timestamp, oi_delta_ratio, funding_rate)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol, timestamp)
                DO UPDATE SET
                    oi_delta_ratio=excluded.oi_delta_ratio,
                    funding_rate=excluded.funding_rate
                """,
                (
                    exchange,
                    symbol,
                    ts,
                    float(metrics.get("oi_delta_ratio", 0.0) or 0.0),
                    float(metrics.get("funding_rate", 0.0) or 0.0),
                ),
            )
            timeframes = metrics.get("timeframes")
            if isinstance(timeframes, dict):
                for timeframe, tf_metrics in timeframes.items():
                    if not isinstance(tf_metrics, dict):
                        continue
                    conn.execute(
                        """
                        INSERT INTO market_flow_timeframes(
                            exchange, symbol, timeframe, timestamp,
                            cvd_ratio, cvd_momentum, oi_delta_ratio, funding_rate,
                            depth_ratio, imbalance, liquidity_delta_norm, signal_strength,
                            sample_count, window_seconds
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(exchange, symbol, timeframe, timestamp)
                        DO UPDATE SET
                            cvd_ratio=excluded.cvd_ratio,
                            cvd_momentum=excluded.cvd_momentum,
                            oi_delta_ratio=excluded.oi_delta_ratio,
                            funding_rate=excluded.funding_rate,
                            depth_ratio=excluded.depth_ratio,
                            imbalance=excluded.imbalance,
                            liquidity_delta_norm=excluded.liquidity_delta_norm,
                            signal_strength=excluded.signal_strength,
                            sample_count=excluded.sample_count,
                            window_seconds=excluded.window_seconds
                        """,
                        (
                            exchange,
                            symbol,
                            str(timeframe).lower(),
                            ts,
                            float(tf_metrics.get("cvd_ratio", 0.0) or 0.0),
                            float(tf_metrics.get("cvd_momentum", 0.0) or 0.0),
                            float(tf_metrics.get("oi_delta_ratio", 0.0) or 0.0),
                            float(tf_metrics.get("funding_rate", 0.0) or 0.0),
                            float(tf_metrics.get("depth_ratio", 1.0) or 1.0),
                            float(tf_metrics.get("imbalance", 0.0) or 0.0),
                            float(tf_metrics.get("liquidity_delta_norm", 0.0) or 0.0),
                            float(tf_metrics.get("signal_strength", 0.0) or 0.0),
                            float(tf_metrics.get("sample_count", 0.0) or 0.0),
                            float(tf_metrics.get("window_seconds", 0.0) or 0.0),
                        ),
                    )

    def insert_ai_decision_log(
        self,
        *,
        symbol: str,
        operation: str,
        decision_json: str,
        trigger_type: Optional[str],
        trigger_id: Optional[str],
        order_id: Optional[str],
        tp_order_id: Optional[str],
        sl_order_id: Optional[str],
        realized_pnl: Optional[float],
        exchange: Optional[str],
    ) -> None:
        sql = """
        INSERT INTO ai_decision_logs (
            timestamp, symbol, operation, decision_json, trigger_type, trigger_id,
            order_id, tp_order_id, sl_order_id, realized_pnl, exchange
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connect() as conn:
            conn.execute(
                sql,
                (
                    self._ts(None),
                    symbol,
                    operation,
                    decision_json,
                    trigger_type,
                    trigger_id,
                    order_id,
                    tp_order_id,
                    sl_order_id,
                    realized_pnl,
                    exchange,
                ),
            )

    def insert_program_execution_log(
        self,
        *,
        symbol: str,
        operation: str,
        decision_json: str,
        market_context_json: Optional[str],
        params_snapshot_json: Optional[str],
        order_id: Optional[str],
        environment: Optional[str],
        exchange: Optional[str],
    ) -> None:
        sql = """
        INSERT INTO program_execution_logs (
            timestamp, symbol, operation, decision_json, market_context_json,
            params_snapshot_json, order_id, environment, exchange
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connect() as conn:
            conn.execute(
                sql,
                (
                    self._ts(None),
                    symbol,
                    operation,
                    decision_json,
                    market_context_json,
                    params_snapshot_json,
                    order_id,
                    environment,
                    exchange,
                ),
            )

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return default

    @staticmethod
    def _safe_json_loads(value: Any, default: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return default
        return default

    def upsert_signal_registry_from_config(self, fund_flow_cfg: Dict[str, Any]) -> Dict[str, int]:
        ff = fund_flow_cfg if isinstance(fund_flow_cfg, dict) else {}
        now_ts = self._ts(None)

        definitions: List[Dict[str, Any]] = []
        defs_cfg = ff.get("signal_definitions")
        if isinstance(defs_cfg, list) and defs_cfg:
            for idx, item in enumerate(defs_cfg, start=1):
                if not isinstance(item, dict):
                    continue
                sig_id = str(item.get("id") or item.get("signal_id") or f"sig_{idx}").strip()
                metric = str(item.get("metric") or "").strip()
                if not sig_id or not metric:
                    continue
                threshold_raw = item.get("threshold")
                threshold = self._to_float(threshold_raw, 0.0)
                threshold_max = item.get("threshold_max")
                if isinstance(threshold_raw, list) and len(threshold_raw) >= 2:
                    threshold = self._to_float(threshold_raw[0], threshold)
                    threshold_max = self._to_float(threshold_raw[1], threshold)
                elif threshold_max is not None:
                    threshold_max = self._to_float(threshold_max, threshold)
                definitions.append(
                    {
                        "id": sig_id,
                        "signal_name": str(item.get("signal_name") or item.get("name") or sig_id),
                        "side": str(item.get("side", "BOTH")).upper(),
                        "metric": metric,
                        "operator": str(item.get("operator", ">=")).strip(),
                        "threshold": threshold,
                        "threshold_max": threshold_max,
                        "timeframe": str(item.get("timeframe", "")).strip().lower() or None,
                        "enabled": 1 if self._to_bool(item.get("enabled", True), True) else 0,
                        "extra_json": json.dumps(item.get("extra", {}), ensure_ascii=False),
                    }
                )
        else:
            legacy_pool_raw = ff.get("signal_pool")
            legacy_pool: Dict[str, Any] = legacy_pool_raw if isinstance(legacy_pool_raw, dict) else {}
            rules_raw = legacy_pool.get("rules")
            rules: List[Any] = rules_raw if isinstance(rules_raw, list) else []
            for idx, item in enumerate(rules, start=1):
                if not isinstance(item, dict):
                    continue
                metric = str(item.get("metric") or "").strip()
                if not metric:
                    continue
                sig_id = str(item.get("id") or item.get("signal_id") or f"legacy_sig_{idx}").strip()
                threshold_raw = item.get("threshold")
                threshold = self._to_float(threshold_raw, 0.0)
                threshold_max = item.get("threshold_max")
                if isinstance(threshold_raw, list) and len(threshold_raw) >= 2:
                    threshold = self._to_float(threshold_raw[0], threshold)
                    threshold_max = self._to_float(threshold_raw[1], threshold)
                elif threshold_max is not None:
                    threshold_max = self._to_float(threshold_max, threshold)
                definitions.append(
                    {
                        "id": sig_id,
                        "signal_name": str(item.get("name") or sig_id),
                        "side": str(item.get("side", "BOTH")).upper(),
                        "metric": metric,
                        "operator": str(item.get("operator", ">=")).strip(),
                        "threshold": threshold,
                        "threshold_max": threshold_max,
                        "timeframe": str(item.get("timeframe", "")).strip().lower() or None,
                        "enabled": 1 if self._to_bool(item.get("enabled", True), True) else 0,
                        "extra_json": json.dumps(item.get("extra", {}), ensure_ascii=False),
                    }
                )

        pools: List[Dict[str, Any]] = []
        pools_cfg = ff.get("signal_pools")
        if isinstance(pools_cfg, list) and pools_cfg:
            for idx, item in enumerate(pools_cfg, start=1):
                if not isinstance(item, dict):
                    continue
                pool_id = str(item.get("id") or item.get("pool_id") or f"pool_{idx}").strip()
                if not pool_id:
                    continue
                signal_ids = item.get("signal_ids")
                if not isinstance(signal_ids, list) or not signal_ids:
                    signal_ids = [d["id"] for d in definitions]
                symbols = item.get("symbols")
                if not isinstance(symbols, list):
                    symbols = []
                pools.append(
                    {
                        "id": pool_id,
                        "pool_name": str(item.get("pool_name") or item.get("name") or pool_id),
                        "logic": str(item.get("logic", "AND")).upper(),
                        "min_pass_count": int(self._to_float(item.get("min_pass_count", 0), 0.0)),
                        "min_long_score": self._to_float(item.get("min_long_score", 0.0), 0.0),
                        "min_short_score": self._to_float(item.get("min_short_score", 0.0), 0.0),
                        "scheduled_trigger_bypass": 1 if self._to_bool(item.get("scheduled_trigger_bypass", True), True) else 0,
                        "apply_when_position_exists": 1 if self._to_bool(item.get("apply_when_position_exists", False), False) else 0,
                        "edge_trigger_enabled": 1 if self._to_bool(item.get("edge_trigger_enabled", True), True) else 0,
                        "edge_cooldown_seconds": max(0, int(self._to_float(item.get("edge_cooldown_seconds", 0), 0.0))),
                        "symbols_json": json.dumps(symbols, ensure_ascii=False),
                        "signal_ids_json": json.dumps(signal_ids, ensure_ascii=False),
                        "enabled": 1 if self._to_bool(item.get("enabled", True), True) else 0,
                    }
                )
        else:
            legacy_pool_raw = ff.get("signal_pool")
            legacy_pool: Dict[str, Any] = legacy_pool_raw if isinstance(legacy_pool_raw, dict) else {}
            pool_id = str(
                legacy_pool.get("pool_id")
                or legacy_pool.get("id")
                or ff.get("active_signal_pool_id")
                or "default"
            ).strip()
            signal_ids = [d["id"] for d in definitions]
            symbols = legacy_pool.get("symbols")
            if not isinstance(symbols, list):
                symbols = []
            pools.append(
                {
                    "id": pool_id,
                    "pool_name": str(legacy_pool.get("pool_name") or "default_pool"),
                    "logic": str(legacy_pool.get("logic", "AND")).upper(),
                    "min_pass_count": int(self._to_float(legacy_pool.get("min_pass_count", 0), 0.0)),
                    "min_long_score": self._to_float(legacy_pool.get("min_long_score", 0.0), 0.0),
                    "min_short_score": self._to_float(legacy_pool.get("min_short_score", 0.0), 0.0),
                    "scheduled_trigger_bypass": 1 if self._to_bool(legacy_pool.get("scheduled_trigger_bypass", True), True) else 0,
                    "apply_when_position_exists": 1 if self._to_bool(legacy_pool.get("apply_when_position_exists", False), False) else 0,
                    "edge_trigger_enabled": 1 if self._to_bool(legacy_pool.get("edge_trigger_enabled", True), True) else 0,
                    "edge_cooldown_seconds": max(0, int(self._to_float(legacy_pool.get("edge_cooldown_seconds", 0), 0.0))),
                    "symbols_json": json.dumps(symbols, ensure_ascii=False),
                    "signal_ids_json": json.dumps(signal_ids, ensure_ascii=False),
                    "enabled": 1 if self._to_bool(legacy_pool.get("enabled", False), False) else 0,
                }
            )

        with self._connect() as conn:
            conn.execute("DELETE FROM signal_definitions WHERE source='config'")
            conn.execute("DELETE FROM signal_pools WHERE source='config'")
            for item in definitions:
                conn.execute(
                    """
                    INSERT INTO signal_definitions(
                        id, signal_name, side, metric, operator, threshold, threshold_max,
                        timeframe, enabled, source, extra_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'config', ?, ?)
                    """,
                    (
                        item["id"],
                        item["signal_name"],
                        item["side"],
                        item["metric"],
                        item["operator"],
                        item["threshold"],
                        item["threshold_max"],
                        item["timeframe"],
                        item["enabled"],
                        item["extra_json"],
                        now_ts,
                    ),
                )
            for item in pools:
                conn.execute(
                    """
                    INSERT INTO signal_pools(
                        id, pool_name, logic, min_pass_count, min_long_score, min_short_score,
                        scheduled_trigger_bypass, apply_when_position_exists, edge_trigger_enabled,
                        edge_cooldown_seconds, symbols_json, signal_ids_json, enabled, source, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'config', ?)
                    """,
                    (
                        item["id"],
                        item["pool_name"],
                        item["logic"],
                        item["min_pass_count"],
                        item["min_long_score"],
                        item["min_short_score"],
                        item["scheduled_trigger_bypass"],
                        item["apply_when_position_exists"],
                        item["edge_trigger_enabled"],
                        item["edge_cooldown_seconds"],
                        item["symbols_json"],
                        item["signal_ids_json"],
                        item["enabled"],
                        now_ts,
                    ),
                )

        return {"definitions": len(definitions), "pools": len(pools)}

    def get_signal_registry_version(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(updated_at) AS version
                FROM (
                    SELECT MAX(updated_at) AS updated_at FROM signal_definitions
                    UNION ALL
                    SELECT MAX(updated_at) AS updated_at FROM signal_pools
                )
                """
            ).fetchone()
        if row is None:
            return ""
        return str(row["version"] or "")

    def get_active_signal_pool_config(self, active_pool_id: Optional[str] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            if active_pool_id:
                row = conn.execute(
                    "SELECT * FROM signal_pools WHERE id=? AND enabled=1 LIMIT 1",
                    (str(active_pool_id),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM signal_pools WHERE enabled=1 ORDER BY updated_at DESC, id ASC LIMIT 1"
                ).fetchone()
            if row is None:
                return {}

            signal_ids = self._safe_json_loads(row["signal_ids_json"], [])
            if not isinstance(signal_ids, list):
                signal_ids = []

            defs_rows: List[sqlite3.Row] = []
            if signal_ids:
                placeholders = ",".join(["?"] * len(signal_ids))
                defs_rows = conn.execute(
                    f"SELECT * FROM signal_definitions WHERE enabled=1 AND id IN ({placeholders})",
                    tuple(str(x) for x in signal_ids),
                ).fetchall()
            else:
                defs_rows = conn.execute(
                    "SELECT * FROM signal_definitions WHERE enabled=1 ORDER BY id ASC"
                ).fetchall()

        defs_map = {str(r["id"]): r for r in defs_rows}
        ordered_defs: List[sqlite3.Row] = []
        if signal_ids:
            for sid in signal_ids:
                key = str(sid)
                if key in defs_map:
                    ordered_defs.append(defs_map[key])
        else:
            ordered_defs = list(defs_rows)

        rules: List[Dict[str, Any]] = []
        for d in ordered_defs:
            rule: Dict[str, Any] = {
                "id": str(d["id"]),
                "name": str(d["signal_name"]),
                "side": str(d["side"] or "BOTH"),
                "metric": str(d["metric"]),
                "operator": str(d["operator"]),
                "threshold": self._to_float(d["threshold"], 0.0),
                "enabled": bool(int(d["enabled"] or 0)),
            }
            if d["threshold_max"] is not None:
                rule["threshold_max"] = self._to_float(d["threshold_max"], 0.0)
            if d["timeframe"]:
                rule["timeframe"] = str(d["timeframe"]).lower()
            rules.append(rule)

        symbols = self._safe_json_loads(row["symbols_json"], [])
        if not isinstance(symbols, list):
            symbols = []

        return {
            "enabled": bool(int(row["enabled"] or 0)),
            "pool_id": str(row["id"]),
            "pool_name": str(row["pool_name"]),
            "logic": str(row["logic"] or "AND").upper(),
            "min_pass_count": int(row["min_pass_count"] or 0),
            "min_long_score": self._to_float(row["min_long_score"], 0.0),
            "min_short_score": self._to_float(row["min_short_score"], 0.0),
            "scheduled_trigger_bypass": bool(int(row["scheduled_trigger_bypass"] or 0)),
            "apply_when_position_exists": bool(int(row["apply_when_position_exists"] or 0)),
            "edge_trigger_enabled": bool(int(row["edge_trigger_enabled"] or 0)),
            "edge_cooldown_seconds": int(row["edge_cooldown_seconds"] or 0),
            "symbols": symbols,
            "rules": rules,
        }

    # =========================
    # Weight Router Cache Methods
    # =========================

    def save_weight_router_cache(
        self,
        *,
        cache_key: str,
        symbol: str,
        regime: str,
        timestamp: str,
        weights: Dict[str, float],
        confidence: float,
        fallback_used: bool,
        regime_view: Optional[Dict[str, Any]] = None,
        risk_flags: Optional[Dict[str, bool]] = None,
        reasoning_bullets: Optional[List[str]] = None,
        ttl_seconds: int = 600,
    ) -> None:
        """
        保存权重快照到数据库
        
        字段名与 MarketIngestionService 对齐:
        - weights: cvd, cvd_momentum, oi_delta, funding, depth_ratio, imbalance, liquidity_delta, micro_delta
        """
        now = datetime.utcnow()
        expires_at = datetime.utcfromtimestamp(now.timestamp() + ttl_seconds)
        
        sql = """
        INSERT OR REPLACE INTO weight_router_cache(
            cache_key, symbol, regime, timestamp, weights_json, confidence,
            fallback_used, regime_view_json, risk_flags_json, reasoning_bullets_json,
            created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        with self._connect() as conn:
            conn.execute(
                sql,
                (
                    cache_key,
                    symbol.upper(),
                    regime.upper(),
                    timestamp,
                    json.dumps(weights, ensure_ascii=False),
                    float(confidence),
                    1 if fallback_used else 0,
                    json.dumps(regime_view or {}, ensure_ascii=False),
                    json.dumps(risk_flags or {}, ensure_ascii=False),
                    json.dumps(reasoning_bullets or [], ensure_ascii=False),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    def get_weight_router_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """
        从数据库获取权重快照
        
        自动清理过期记录
        """
        now = datetime.utcnow()
        
        with self._connect() as conn:
            # 清理过期记录
            conn.execute(
                "DELETE FROM weight_router_cache WHERE expires_at < ?",
                (now.isoformat(),),
            )
            
            row = conn.execute(
                """
                SELECT * FROM weight_router_cache WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        
        if row is None:
            return None
        
        return {
            "cache_key": str(row["cache_key"]),
            "symbol": str(row["symbol"]),
            "regime": str(row["regime"]),
            "timestamp": str(row["timestamp"]),
            "weights": self._safe_json_loads(row["weights_json"], {}),
            "confidence": self._to_float(row["confidence"], 0.5),
            "fallback_used": bool(row["fallback_used"]),
            "regime_view": self._safe_json_loads(row["regime_view_json"], {}),
            "risk_flags": self._safe_json_loads(row["risk_flags_json"], {}),
            "reasoning_bullets": self._safe_json_loads(row["reasoning_bullets_json"], []),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
        }

    def cleanup_weight_router_cache(self) -> int:
        """清理过期的权重缓存"""
        now = datetime.utcnow()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM weight_router_cache WHERE expires_at < ?",
                (now.isoformat(),),
            )
            return cursor.rowcount
