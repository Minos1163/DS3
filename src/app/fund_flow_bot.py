"""Fund-flow-only trading runtime.

This module is the new runtime entry for the migrated fund-flow strategy.
Legacy DCA/threshold branches are intentionally removed.
"""

from __future__ import annotations

from collections import deque
import atexit
import argparse
import csv
from dataclasses import dataclass
import json
import math
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.api.binance_client import BinanceClient
from src.config.config_loader import ConfigLoader
from src.config.env_manager import EnvManager
from src.data.account_data import AccountDataManager
from src.data.market_data import MarketDataManager
from src.data.position_data import PositionDataManager
from src.fund_flow import (
    FundFlowDecision,
    FundFlowAttributionEngine,
    FundFlowDecisionEngine,
    FundFlowExecutionRouter,
    FundFlowRiskEngine,
    MarketIngestionService,
    MarketStorage,
    Operation as FundFlowOperation,
    TriggerEngine,
)
try:
    from src.risk.enhanced_risk import RiskConfig as _ImportedRiskConfig
except ModuleNotFoundError:
    _ImportedRiskConfig = None

if _ImportedRiskConfig is None:
    @dataclass
    class _FallbackRiskConfig:
        max_drawdown: float = 0.05
        max_exposure_per_trade: float = 0.25
        trailing_atr_mul: float = 2.0
        trend_weight: float = 0.4
        momentum_weight: float = 0.3
        volatility_weight: float = 0.2
        drawdown_weight: float = 0.3
        entry_threshold: float = 0.5
    RiskConfig = _FallbackRiskConfig
else:
    RiskConfig = _ImportedRiskConfig

try:
    from src.risk.integration_gate import gate_trade_decision as _gate_trade_decision_impl
except ModuleNotFoundError:
    _gate_trade_decision_impl = None


def gate_trade_decision(state_dict: Dict[str, Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    if _gate_trade_decision_impl is not None:
        return _gate_trade_decision_impl(state_dict, *args, **kwargs)
    # Degrade gracefully when optional risk module is not deployed.
    direction = str((state_dict or {}).get("direction", "NONE")).upper()
    action = "ENTER" if direction in ("LONG", "SHORT") else "HOLD"
    return {"action": action, "enter": action == "ENTER", "exit": False, "score": 0.0, "details": {"fallback": True}}
from src.trading.intents import PositionSide as IntentPositionSide
from src.trading.risk_manager import RiskManager


class _DualWriter:
    """Mirror writes to original stream and a persistent log file."""

    def __init__(self, primary: Any, mirror: Any):
        self._primary = primary
        self._mirror = mirror
        self.encoding = getattr(primary, "encoding", "utf-8")

    def write(self, data: str) -> int:
        text = str(data)
        n = 0
        if self._primary is not None:
            try:
                written = self._primary.write(text)
                if isinstance(written, int):
                    n = written
            except Exception:
                n = 0
        try:
            self._mirror.write(text)
            self._mirror.flush()
        except Exception:
            pass
        if self._primary is not None:
            try:
                self._primary.flush()
            except Exception:
                pass
        return n

    def flush(self) -> None:
        if self._primary is not None:
            try:
                self._primary.flush()
            except Exception:
                pass
        try:
            self._mirror.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        if self._primary is None:
            return False
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False


class _SixHourBucketFile:
    """Append-only writer that rotates target file every 6 hours."""

    def __init__(self, root_dir: str, file_name: str):
        self._root_dir = root_dir
        self._file_name = file_name
        self._bucket_key: Optional[str] = None
        self._fp: Optional[Any] = None

    @staticmethod
    def _bucket_parts(now: datetime) -> Tuple[str, str, str]:
        month = now.strftime("%Y-%m")
        date = now.strftime("%Y-%m-%d")
        hour_bucket = f"{(now.hour // 6) * 6:02d}"
        return month, date, hour_bucket

    def _bucket_file_name(self, hour_bucket: str) -> str:
        stem, ext = os.path.splitext(self._file_name)
        if not stem:
            return f"{self._file_name}.{hour_bucket}"
        return f"{stem}.{hour_bucket}{ext}"

    def _ensure_open(self) -> None:
        now = datetime.now()
        month, date, hour_bucket = self._bucket_parts(now)
        key = f"{month}/{date}/{hour_bucket}"
        if self._fp is not None and self._bucket_key == key:
            return
        self.close()
        dir_path = os.path.join(self._root_dir, month, date)
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, self._bucket_file_name(hour_bucket))
        self._fp = open(path, "a", encoding="utf-8", buffering=1)
        self._bucket_key = key

    def current_path(self) -> str:
        self._ensure_open()
        month, date, hour_bucket = self._bucket_parts(datetime.now())
        return os.path.join(self._root_dir, month, date, self._bucket_file_name(hour_bucket))

    def write(self, data: str) -> int:
        self._ensure_open()
        if self._fp is None:
            return 0
        written = self._fp.write(str(data))
        self._fp.flush()
        return int(written) if isinstance(written, int) else 0

    def flush(self) -> None:
        if self._fp is None:
            return
        try:
            self._fp.flush()
        except Exception:
            pass

    def close(self) -> None:
        if self._fp is None:
            return
        try:
            self._fp.flush()
            self._fp.close()
        except Exception:
            pass
        self._fp = None
        self._bucket_key = None


def _configure_console_encoding() -> None:
    # Windows 默认控制台编码常是 gbk，遇到 emoji 日志会抛 UnicodeEncodeError。
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


class TradingBot:
    """Lightweight bot that only runs the FUND_FLOW strategy path."""

    def __init__(self, config_path: Optional[str] = None):
        self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.config_path = self._resolve_config_path(config_path)
        self.config = ConfigLoader.load_trading_config(self.config_path)
        self._config_mtime: float = self._get_config_mtime()

        self._load_env_file()
        self._apply_network_env_from_config()

        self.logs_dir = self._resolve_logs_dir()
        self.log_root_dir = self._resolve_bucket_log_root_dir()
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(self.log_root_dir, exist_ok=True)
        self._migrate_legacy_log_layout()
        self._runtime_out_fp: Optional[_SixHourBucketFile] = None
        self._runtime_err_fp: Optional[_SixHourBucketFile] = None
        self._configure_runtime_log_sink()

        self.client = BinanceClient()
        self.account_data = AccountDataManager(self.client, config_path=self.config_path)
        self.market_data = MarketDataManager(self.client)
        self.position_data = PositionDataManager(self.client)
        self.risk_manager = RiskManager(self.config)

        self.trade_count = 0
        self._prev_open_interest: Dict[str, float] = {}
        self._startup_trend_filter_cache: Dict[str, Dict[str, float]] = {}
        self._liquidity_ema_notional: Dict[str, float] = {}
        self._risk_state_path = os.path.join(self.logs_dir, "fund_flow_risk_state.json")
        self._protection_alert_path = os.path.join(self.logs_dir, "protection_sla_alerts.log")
        self._trade_fill_log_name = "trade_fills_utc.csv"
        self._trade_fill_logged_keys: set[str] = set()
        self._consecutive_losses: int = 0
        self._cooldown_expires: Optional[datetime] = None
        self._cooldown_reason: Optional[str] = None
        self._daily_open_equity: Optional[float] = None
        self._daily_open_date: Optional[str] = None
        self._peak_equity: Optional[float] = None
        self._position_first_seen_ts: Dict[str, float] = {}
        self._position_last_direction_eval_ts: Dict[str, float] = {}
        self._position_extrema_by_pos: Dict[str, Dict[str, float]] = {}
        self._protection_missing_since_ts: Dict[str, float] = {}
        self._protection_last_alert_ts: Dict[str, float] = {}
        self._pre_risk_exit_streak_by_pos: Dict[str, int] = {}
        self._dca_stage_by_pos: Dict[str, int] = {}
        self._opened_symbols_this_cycle: set[str] = set()
        self._volatility_spike_streak_by_symbol: Dict[str, int] = {}
        self._volatility_last_bucket_by_symbol: Dict[str, str] = {}
        self._volatility_cooldown_until_by_symbol: Dict[str, datetime] = {}
        self._volatility_cooldown_reason_by_symbol: Dict[str, str] = {}
        self._prev_imbalance_for_phantom: Dict[str, float] = {}
        self._micro_feature_history: Dict[str, Dict[str, Deque[float]]] = {}
        self._signal_registry_version: str = ""
        self._signal_pool_configs: Dict[str, Dict[str, Any]] = {}
        self._signal_pool_configs_runtime_cache: Dict[str, Dict[str, Any]] = {}
        self._symbol_rotation_offset: int = 0
        self._last_entry_bucket_id: Optional[int] = None
        self._analysis_bucket_state: Dict[str, int] = {}
        self.fund_flow_storage = None
        self._load_risk_state()
        self._init_fund_flow_modules()
        self._preload_market_history_on_startup()

        self._print_startup_summary()

        mode = str(self.config.get("strategy", {}).get("mode", "FUND_FLOW")).upper()
        if mode != "FUND_FLOW":
            print(f"⚠️ 当前 strategy.mode={mode}，仍按 FUND_FLOW 运行（旧模式逻辑已移除）")

    def _configure_runtime_log_sink(self) -> None:
        if isinstance(sys.stdout, _DualWriter) and isinstance(sys.stderr, _DualWriter):
            return
        out_mirror = _SixHourBucketFile(self.log_root_dir, "runtime.out.log")
        err_mirror = _SixHourBucketFile(self.log_root_dir, "runtime.err.log")
        try:
            self._runtime_out_fp = out_mirror
            self._runtime_err_fp = err_mirror
            sys.stdout = _DualWriter(sys.stdout, out_mirror)
            sys.stderr = _DualWriter(sys.stderr, err_mirror)
            atexit.register(self._close_runtime_log_sink)
            print(
                "📝 Runtime日志落盘启用(6H): "
                f"out={out_mirror.current_path()} err={err_mirror.current_path()}"
            )
        except Exception as e:
            print(f"⚠️ 启用Runtime日志落盘失败: {e}")

    def _close_runtime_log_sink(self) -> None:
        for fp in (self._runtime_out_fp, self._runtime_err_fp):
            try:
                if fp:
                    fp.close()
            except Exception:
                pass

    def _get_config_mtime(self) -> float:
        try:
            return float(os.path.getmtime(self.config_path))
        except Exception:
            return 0.0

    def _reload_config_if_changed(self) -> bool:
        current_mtime = self._get_config_mtime()
        if current_mtime <= 0:
            return False
        if current_mtime <= self._config_mtime:
            return False

        old_config = self.config
        old_symbols = ConfigLoader.get_trading_symbols(old_config)
        try:
            new_config = ConfigLoader.load_trading_config(self.config_path)
        except Exception as e:
            # 文件时间已经变化，避免每轮重复刷屏；等待下一次配置文件再次修改后重试
            self._config_mtime = current_mtime
            print(f"⚠️ 检测到配置变更，但重载失败，继续使用旧配置: {e}")
            return False

        self.config = new_config
        self._config_mtime = current_mtime
        self._apply_network_env_from_config()
        self._init_fund_flow_modules()

        new_symbols = ConfigLoader.get_trading_symbols(new_config)
        ts = datetime.fromtimestamp(current_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print("\n" + "=" * 66)
        print(f"♻️ 配置热更新生效 @ {ts}")
        print(f"📄 配置文件: {self.config_path}")
        if set(old_symbols) != set(new_symbols):
            removed = [s for s in old_symbols if s not in new_symbols]
            added = [s for s in new_symbols if s not in old_symbols]
            print(f"📊 交易对更新: {', '.join(new_symbols)}")
            if added:
                print(f"   ➕ 新增: {', '.join(added)}")
            if removed:
                print(f"   ➖ 移除: {', '.join(removed)}")
        else:
            print("✅ 参数更新已生效（交易对未变化）")
        print("=" * 66)
        return True

    def _resolve_config_path(self, config_path: Optional[str]) -> str:
        if config_path:
            return config_path if os.path.isabs(config_path) else os.path.join(self.project_root, config_path)

        env_cfg = os.getenv("TRADING_CONFIG_FILE") or os.getenv("BOT_CONFIG_FILE")
        if env_cfg:
            candidate = env_cfg if os.path.isabs(env_cfg) else os.path.join(self.project_root, env_cfg)
            if os.path.exists(candidate):
                return candidate

        preferred = os.path.join(self.project_root, "config", "trading_config_fund_flow.json")
        fallback = os.path.join(self.project_root, "config", "trading_config_vps.json")
        if os.path.exists(preferred):
            return preferred
        return fallback

    def _load_env_file(self) -> None:
        env_hint = os.getenv("TRADING_BOT_ENV_FILE") or os.getenv("BOT_ENV_FILE") or ".env"
        env_path = env_hint if os.path.isabs(env_hint) else os.path.join(self.project_root, env_hint)
        loaded = EnvManager.load_env_file(env_path)
        if not loaded and env_hint != ".env":
            EnvManager.load_env_file(os.path.join(self.project_root, ".env"))

    def _apply_network_env_from_config(self) -> None:
        network_cfg = self.config.get("network", {}) or {}
        if bool(network_cfg.get("force_direct", False)):
            os.environ["BINANCE_FORCE_DIRECT"] = "1"
        if bool(network_cfg.get("disable_proxy", False)):
            os.environ["BINANCE_DISABLE_PROXY"] = "1"

    def _resolve_logs_dir(self) -> str:
        log_cfg = self.config.get("logging", {}) or {}
        logs_hint = log_cfg.get("dir") or log_cfg.get("logs_dir")
        if isinstance(logs_hint, str) and logs_hint.strip():
            return logs_hint if os.path.isabs(logs_hint) else os.path.join(self.project_root, logs_hint)
        now = datetime.now()
        month = now.strftime("%Y-%m")
        date = now.strftime("%Y-%m-%d")
        return os.path.join(self.project_root, "logs", month, date, "fund_flow")

    def _resolve_bucket_log_root_dir(self) -> str:
        log_cfg = self.config.get("logging", {}) or {}
        logs_hint = (
            log_cfg.get("bucket_root_dir")
            or log_cfg.get("runtime_root_dir")
            or "logs"
        )
        if isinstance(logs_hint, str) and logs_hint.strip():
            return logs_hint if os.path.isabs(logs_hint) else os.path.join(self.project_root, logs_hint)
        return os.path.join(self.project_root, "logs")

    def _resolve_trade_fill_log_path_utc(self, now_utc: Optional[datetime] = None) -> str:
        now_utc = now_utc or datetime.now(timezone.utc)
        month = now_utc.strftime("%Y-%m")
        date = now_utc.strftime("%Y-%m-%d")
        dir_path = os.path.join(self.log_root_dir, month, date)
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, self._trade_fill_log_name)

    def _migrate_legacy_log_layout(self) -> None:
        """
        兼容旧路径:
        - logs/fund_flow/{fund_flow_strategy.db, fund_flow_risk_state.json, protection_sla_alerts.log}
        - logs/order_rejects.log
        迁移到新路径:
        - logs/YYYY-MM/YYYY-MM-DD/fund_flow/...
        - logs/YYYY-MM/YYYY-MM-DD/order_rejects.log
        """
        try:
            legacy_root = os.path.join(self.project_root, "logs")
            today = datetime.now()
            month = today.strftime("%Y-%m")
            date = today.strftime("%Y-%m-%d")
            today_dir = os.path.join(legacy_root, month, date)
            os.makedirs(today_dir, exist_ok=True)

            legacy_ff_dir = os.path.join(legacy_root, "fund_flow")
            target_ff_dir = self.logs_dir
            os.makedirs(target_ff_dir, exist_ok=True)
            ff_files = (
                "fund_flow_strategy.db",
                "fund_flow_risk_state.json",
                "protection_sla_alerts.log",
            )
            if os.path.isdir(legacy_ff_dir):
                for name in ff_files:
                    src = os.path.join(legacy_ff_dir, name)
                    dst = os.path.join(target_ff_dir, name)
                    if not os.path.exists(src):
                        continue
                    if not os.path.exists(dst):
                        shutil.move(src, dst)
                        continue
                    if name.endswith(".log"):
                        try:
                            with open(src, "r", encoding="utf-8", errors="ignore") as sf:
                                content = sf.read()
                            if content:
                                with open(dst, "a", encoding="utf-8") as df:
                                    if not content.endswith("\n"):
                                        content += "\n"
                                    df.write(content)
                            os.remove(src)
                        except Exception:
                            pass
                    else:
                        try:
                            if os.path.getmtime(src) > os.path.getmtime(dst):
                                os.remove(dst)
                                shutil.move(src, dst)
                            else:
                                os.remove(src)
                        except Exception:
                            pass
                try:
                    if not os.listdir(legacy_ff_dir):
                        os.rmdir(legacy_ff_dir)
                except Exception:
                    pass

            legacy_reject = os.path.join(legacy_root, "order_rejects.log")
            if os.path.exists(legacy_reject):
                dst_reject = os.path.join(today_dir, "order_rejects.log")
                if not os.path.exists(dst_reject):
                    shutil.move(legacy_reject, dst_reject)
                else:
                    try:
                        with open(legacy_reject, "r", encoding="utf-8", errors="ignore") as sf:
                            content = sf.read()
                        if content:
                            with open(dst_reject, "a", encoding="utf-8") as df:
                                if not content.endswith("\n"):
                                    content += "\n"
                                df.write(content)
                        os.remove(legacy_reject)
                    except Exception:
                        pass
        except Exception:
            pass

    @staticmethod
    def _normalize_fill_side(side: str) -> str:
        s = str(side or "").upper()
        if s == "BUY":
            return "买入"
        if s == "SELL":
            return "卖出"
        return s or "未知"

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except Exception:
            return default

    def _fetch_order_trade_fills(self, symbol: str, order_id: Optional[int]) -> List[Dict[str, Any]]:
        if not symbol:
            return []
        params: Dict[str, Any] = {"symbol": symbol, "limit": 100}
        if order_id is not None:
            params["orderId"] = int(order_id)

        base = self.client.broker.um_base()
        candidate_paths = ["/papi/v1/um/userTrades"] if "papi" in base else ["/fapi/v1/userTrades"]
        for path in candidate_paths:
            url = f"{base}{path}"
            try:
                resp = self.client.broker.request(
                    "GET",
                    url,
                    params=params,
                    signed=True,
                    allow_error=True,
                )
            except Exception:
                continue
            if int(getattr(resp, "status_code", 500) or 500) >= 400:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            rows: List[Dict[str, Any]] = []
            if isinstance(data, list):
                rows = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                for k in ("rows", "trades", "data"):
                    nested = data.get(k)
                    if isinstance(nested, list):
                        rows = [x for x in nested if isinstance(x, dict)]
                        break
            if order_id is not None and rows:
                rows = [x for x in rows if self._to_int(x.get("orderId"), -1) == int(order_id)]
            if rows:
                return rows
        return []

    def _append_trade_fill_rows(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        headers = [
            "时间(UTC)",
            "合约",
            "方向",
            "价格",
            "数量",
            "成交额",
            "手续费",
            "手续费结算币种",
            "已实现盈亏",
            "计价资产",
            "订单ID",
            "成交ID",
            "来源",
        ]
        # 同一轮重复触发时避免重复写同一笔成交
        dedup_rows: List[Dict[str, Any]] = []
        for row in rows:
            dedup_key = str(row.get("_dedup_key") or "")
            if dedup_key and dedup_key in self._trade_fill_logged_keys:
                continue
            if dedup_key:
                self._trade_fill_logged_keys.add(dedup_key)
            dedup_rows.append(row)
        if not dedup_rows:
            return

        log_path = self._resolve_trade_fill_log_path_utc()
        file_exists = os.path.exists(log_path) and os.path.getsize(log_path) > 0
        with open(log_path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            for row in dedup_rows:
                writer.writerow(row)

    def _write_trade_fill_log(
        self,
        *,
        symbol: str,
        decision: FundFlowDecision,
        execution_result: Dict[str, Any],
    ) -> None:
        if not isinstance(execution_result, dict):
            return
        order = execution_result.get("order")
        if not isinstance(order, dict):
            return
        order_id_val = order.get("orderId")
        order_id = self._to_int(order_id_val, -1)
        if order_id <= 0:
            return

        fills = self._fetch_order_trade_fills(symbol=symbol, order_id=order_id)
        rows: List[Dict[str, Any]] = []
        if fills:
            for fill in fills:
                ts_ms = self._to_int(fill.get("time"), 0)
                if ts_ms > 0:
                    ts_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                side = str(fill.get("side") or order.get("side") or "").upper()
                qty = self._to_float(fill.get("qty"), self._to_float(fill.get("executedQty"), 0.0))
                price = self._to_float(
                    fill.get("price"),
                    self._to_float(order.get("avgPrice"), self._to_float(order.get("price"), 0.0)),
                )
                quote_qty = self._to_float(fill.get("quoteQty"), qty * price)
                fee = self._to_float(fill.get("commission"), 0.0)
                fee_asset = str(fill.get("commissionAsset") or "USDT")
                realized = self._to_float(fill.get("realizedPnl"), 0.0)
                trade_id = str(fill.get("id") or fill.get("tradeId") or "")
                rows.append(
                    {
                        "_dedup_key": f"{symbol}|{order_id}|{trade_id or ts_ms}|{qty}|{price}",
                        "时间(UTC)": ts_utc,
                        "合约": symbol,
                        "方向": self._normalize_fill_side(side),
                        "价格": price,
                        "数量": qty,
                        "成交额": quote_qty,
                        "手续费": fee,
                        "手续费结算币种": fee_asset,
                        "已实现盈亏": realized,
                        "计价资产": "USDT",
                        "订单ID": str(order_id),
                        "成交ID": trade_id,
                        "来源": "user_trades",
                    }
                )
        else:
            # 若 userTrades 临时不可用，回退记录订单回报，避免完全丢单据。
            exec_qty = self._to_float(order.get("executedQty"), 0.0)
            if exec_qty > 0:
                ts_ms = self._to_int(order.get("updateTime") or order.get("transactTime"), 0)
                if ts_ms > 0:
                    ts_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                side = str(order.get("side") or "").upper()
                price = self._to_float(order.get("avgPrice"), self._to_float(order.get("price"), 0.0))
                quote_qty = self._to_float(order.get("cumQuote"), exec_qty * price)
                rows.append(
                    {
                        "_dedup_key": f"{symbol}|{order_id}|fallback|{exec_qty}|{price}",
                        "时间(UTC)": ts_utc,
                        "合约": symbol,
                        "方向": self._normalize_fill_side(side),
                        "价格": price,
                        "数量": exec_qty,
                        "成交额": quote_qty,
                        "手续费": "",
                        "手续费结算币种": "",
                        "已实现盈亏": "",
                        "计价资产": "USDT",
                        "订单ID": str(order_id),
                        "成交ID": "",
                        "来源": "order_fallback",
                    }
                )
        self._append_trade_fill_rows(rows)

    def _print_startup_summary(self) -> None:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        symbols = ConfigLoader.get_trading_symbols(self.config)
        startup_cfg = self._startup_market_preload_config()
        print("=" * 66)
        print("🚀 资金流策略机器人启动")
        print(f"📄 配置文件: {self.config_path}")
        print(f"📁 日志目录: {self.logs_dir}")
        print(f"🗂️ 分桶日志根目录(6H): {self.log_root_dir}")
        print(f"🧾 成交回报日志(UTC): {self._resolve_trade_fill_log_path_utc()}")
        print(f"📊 交易对: {', '.join(symbols)}")
        print(
            "⚙️ 杠杆配置: "
            f"min={ff_cfg.get('min_leverage', 2)}x, "
            f"default={ff_cfg.get('default_leverage', 2)}x, "
            f"max={ff_cfg.get('max_leverage', 20)}x"
        )
        print(
            "🎚️ 止盈止损(生效): "
            f"SL={float(getattr(self.fund_flow_decision_engine, 'stop_loss_pct', 0.01)) * 100:.2f}% , "
            f"TP={float(getattr(self.fund_flow_decision_engine, 'take_profit_pct', 0.03)) * 100:.2f}%"
        )
        print(
            "🧯 账户熔断: "
            f"enabled={self._risk_config().get('enabled')}, "
            f"daily_loss={self._risk_config().get('max_daily_loss_pct'):.2%}, "
            f"max_consecutive_losses={self._risk_config().get('max_consecutive_losses')}"
        )
        sla = self._protection_sla_config()
        dca = self._dca_config()
        print(
            "🛡️ 保护单SLA: "
            f"enabled={sla.get('enabled')}, "
            f"timeout={sla.get('timeout_seconds')}s, "
            f"force_flatten={sla.get('force_flatten_on_breach')}"
        )
        pre_risk = self._pretrade_risk_gate_config()
        print(
            "🧭 前置风控Gate: "
            f"enabled={pre_risk.get('enabled')}, "
            f"entry_threshold={self._to_float(pre_risk.get('entry_threshold'), 0.0):.2f}, "
            f"max_dd={self._to_float(pre_risk.get('max_drawdown'), 0.0):.2%}, "
            f"force_exit={bool(pre_risk.get('force_exit_on_gate', True))}"
        )
        print(
            "📥 启动预热: "
            f"enabled={startup_cfg.get('enabled')}, "
            f"lookback={startup_cfg.get('lookback_minutes')}m, "
            f"interval={startup_cfg.get('kline_interval')}, "
            f"oi_period={startup_cfg.get('oi_period')}"
        )
        cleanup_cfg = self._stale_protection_cleanup_config()
        print(
            "🧹 保护单清理: "
            f"enabled={cleanup_cfg.get('enabled')}, "
            f"post_open_delay={cleanup_cfg.get('delay_seconds')}s"
        )
        print(
            "📉 DCA马丁: "
            f"enabled={dca.get('enabled')}, "
            f"steps={len(dca.get('drawdown_thresholds') or [])}, "
            f"max_additions={dca.get('max_additions')}, "
            f"base_add={dca.get('base_add_portion'):.2f}"
        )
        sp_cfg = getattr(self.fund_flow_trigger_engine, "signal_pool_config", None)
        if not isinstance(sp_cfg, dict):
            sp_cfg = ff_cfg.get("signal_pool", {}) if isinstance(ff_cfg.get("signal_pool", {}), dict) else {}
        decision_tf = str(ff_cfg.get("decision_timeframe") or ff_cfg.get("signal_timeframe") or "raw").strip().lower()
        print(
            "🎯 SignalPool: "
            f"enabled={bool(sp_cfg.get('enabled', False))}, "
            f"logic={str(sp_cfg.get('logic', 'AND')).upper()}, "
            f"rules={len(sp_cfg.get('rules') or [])}, "
            f"edge={bool(sp_cfg.get('edge_trigger_enabled', True))}, "
            f"tf={decision_tf}"
        )
        schedule_cfg = self.config.get("schedule", {}) or {}
        tf_seconds = self._decision_timeframe_seconds()
        print(
            "⏱️ 调度对齐: "
            f"align_to_kline_close={bool(schedule_cfg.get('align_to_kline_close', True))}, "
            f"active={self._is_kline_alignment_active()}, "
            f"tf_seconds={int(tf_seconds) if tf_seconds else 0}, "
            f"kline_close_delay_seconds={self._to_float(schedule_cfg.get('kline_close_delay_seconds', 3), 3.0):.1f}, "
            f"fallback_interval={int(schedule_cfg.get('interval_seconds', 60) or 60)}s, "
            f"symbols_per_cycle={int(schedule_cfg.get('symbols_per_cycle', 0) or 0)}, "
            f"prioritize_positions={bool(schedule_cfg.get('symbols_per_cycle_prioritize_positions', True))}, "
            f"max_cycle_runtime_seconds={self._to_float(schedule_cfg.get('max_cycle_runtime_seconds', 0), 0.0):.1f}, "
            f"symbol_stagger_seconds={self._to_float(schedule_cfg.get('symbol_stagger_seconds', 0), 0.0):.2f}"
        )
        deg_cfg = ff_cfg.get("execution_degradation", {}) if isinstance(ff_cfg.get("execution_degradation", {}), dict) else {}
        print(
            "🧱 执行退化: "
            f"open_ioc_retry={int(deg_cfg.get('open_ioc_retry_times', 1) or 1)}, "
            f"open_gtc={bool(deg_cfg.get('open_gtc_fallback_enabled', True))}, "
            f"open_mkt={bool(deg_cfg.get('open_market_fallback_enabled', False))}, "
            f"close_ioc_retry={int(deg_cfg.get('close_ioc_retry_times', 4) or 4)}, "
            f"close_gtc={bool(deg_cfg.get('close_gtc_fallback_enabled', True))}, "
            f"close_mkt={bool(deg_cfg.get('close_market_fallback_enabled', False))}"
        )
        try:
            guide_snapshot = self.fund_flow_decision_engine.get_direction_guide_snapshot()
        except Exception:
            guide_snapshot = {}
        if isinstance(guide_snapshot, dict) and guide_snapshot:
            guide_model_key = str(guide_snapshot.get("model", "")).upper()
            print(
                "🧭 方向指导: "
                f"enabled={bool(guide_snapshot.get('enabled', True))}, "
                f"model={guide_snapshot.get('model_label', guide_snapshot.get('model', '-'))}, "
                f"neutral_zone={self._to_float(guide_snapshot.get('neutral_zone'), 0.02):.3f}, "
                f"squeeze_penalty={self._to_float(guide_snapshot.get('bb_squeeze_penalty'), 0.72):.2f}"
            )
            if guide_model_key == "MACD_KDJ":
                w_kdj = guide_snapshot.get("macd_kdj_weights", {})
                if isinstance(w_kdj, dict) and w_kdj:
                    print(
                        "   MACD+KDJ权重: "
                        f"macd={self._to_float(w_kdj.get('macd'), 0.0):.2f}, "
                        f"kdj={self._to_float(w_kdj.get('kdj'), 0.0):.2f}, "
                        f"cross={self._to_float(w_kdj.get('macd_cross'), 0.0):.2f}, "
                        f"kdj_cross={self._to_float(w_kdj.get('kdj_cross'), 0.0):.2f}, "
                        f"kdj_zone={self._to_float(w_kdj.get('kdj_zone'), 0.0):.2f}, "
                        f"hist_mom={self._to_float(w_kdj.get('macd_hist_mom'), 0.0):.2f}"
                    )
            else:
                w_bb = guide_snapshot.get("macd_bb_weights", {})
                if isinstance(w_bb, dict) and w_bb:
                    print(
                        "   MACD+BB权重: "
                        f"macd={self._to_float(w_bb.get('macd'), 0.0):.2f}, "
                        f"bb={self._to_float(w_bb.get('bb'), 0.0):.2f}, "
                        f"macd_cross={self._to_float(w_bb.get('macd_cross'), 0.0):.2f}, "
                        f"bb_break={self._to_float(w_bb.get('bb_break'), 0.0):.2f}, "
                        f"bb_trend={self._to_float(w_bb.get('bb_trend'), 0.0):.2f}, "
                        f"hist_mom={self._to_float(w_bb.get('macd_hist_mom'), 0.0):.2f}"
                    )
        print("=" * 66)

    def _position_snapshot_by_symbol(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        target = {str(s).upper() for s in symbols}
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        try:
            positions = self.client.get_all_positions() if hasattr(self.client, "get_all_positions") else []
        except Exception:
            positions = []
        for pos in positions or []:
            if not isinstance(pos, dict):
                continue
            symbol = str(pos.get("symbol") or "").upper()
            if not symbol or (target and symbol not in target):
                continue
            amount = self._to_float(pos.get("positionAmt"), 0.0)
            if abs(amount) <= 0:
                continue
            side_raw = str(pos.get("positionSide") or "").upper()
            if side_raw in ("LONG", "SHORT"):
                side = side_raw
            else:
                side = "LONG" if amount > 0 else "SHORT"
            entry_price = self._to_float(pos.get("entryPrice"), 0.0)
            mark_price = self._to_float(pos.get("markPrice"), 0.0)
            leverage = int(self._to_float(pos.get("leverage"), 0.0))
            unrealized_pnl = self._to_float(
                pos.get("unRealizedProfit", pos.get("unrealizedProfit", 0.0)),
                0.0,
            )
            if entry_price > 0:
                if side == "LONG":
                    pnl_percent = ((mark_price - entry_price) / entry_price) * 100.0
                else:
                    pnl_percent = ((entry_price - mark_price) / entry_price) * 100.0
            else:
                pnl_percent = 0.0
            margin = abs(amount * entry_price / leverage) if leverage > 0 else 0.0
            grouped.setdefault(symbol, []).append(
                {
                    "side": side,
                    "amount": abs(amount),
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "leverage": leverage,
                    "margin": margin,
                    "unrealized_pnl": unrealized_pnl,
                    "pnl_percent": pnl_percent,
                    "liquidation_price": self._to_float(pos.get("liquidationPrice"), 0.0),
                    "notional": abs(amount * mark_price),
                }
            )

        out: Dict[str, Dict[str, Any]] = {}
        for symbol, legs in grouped.items():
            if not legs:
                continue
            primary = max(
                legs,
                key=lambda p: (
                    self._to_float(p.get("notional"), 0.0),
                    self._to_float(p.get("amount"), 0.0),
                ),
            )
            side_set = {
                str(leg.get("side", "")).upper()
                for leg in legs
                if str(leg.get("side", "")).upper() in ("LONG", "SHORT")
            }
            snapshot = {
                "side": str(primary.get("side", "")).upper(),
                "amount": self._to_float(primary.get("amount"), 0.0),
                "entry_price": self._to_float(primary.get("entry_price"), 0.0),
                "mark_price": self._to_float(primary.get("mark_price"), 0.0),
                "leverage": int(self._to_float(primary.get("leverage"), 0.0)),
                "margin": self._to_float(primary.get("margin"), 0.0),
                "unrealized_pnl": self._to_float(primary.get("unrealized_pnl"), 0.0),
                "pnl_percent": self._to_float(primary.get("pnl_percent"), 0.0),
                "liquidation_price": self._to_float(primary.get("liquidation_price"), 0.0),
                "notional": self._to_float(primary.get("notional"), 0.0),
            }
            if len(side_set) > 1:
                snapshot["hedge_conflict"] = True
                snapshot["side"] = "BOTH"
                snapshot["legs"] = list(legs)
            out[symbol] = snapshot
        return out

    def _symbols_for_current_cycle(
        self,
        symbols: List[str],
        position_symbol_set: Optional[set[str]] = None,
    ) -> List[str]:
        if not symbols:
            return []
        schedule_cfg = self.config.get("schedule", {}) or {}
        per_cycle = max(0, int(schedule_cfg.get("symbols_per_cycle", 0) or 0))
        if per_cycle <= 0 or per_cycle >= len(symbols):
            return list(symbols)

        prioritize_positions = bool(schedule_cfg.get("symbols_per_cycle_prioritize_positions", True))
        position_symbol_set = (position_symbol_set or set()) if prioritize_positions else set()
        position_symbols = [s for s in symbols if str(s).upper() in position_symbol_set]
        position_upper = {str(s).upper() for s in position_symbols}
        rotating_pool = [s for s in symbols if str(s).upper() not in position_upper]
        remaining_budget = max(0, per_cycle - len(position_symbols))

        rotating_selected: List[str] = []
        if remaining_budget > 0 and rotating_pool:
            start = self._symbol_rotation_offset % len(rotating_pool)
            end = start + remaining_budget
            if end <= len(rotating_pool):
                rotating_selected = rotating_pool[start:end]
            else:
                rotating_selected = rotating_pool[start:] + rotating_pool[: (end % len(rotating_pool))]
            self._symbol_rotation_offset = (start + remaining_budget) % len(rotating_pool)

        selected = position_symbols + rotating_selected
        if len(selected) < per_cycle:
            for symbol in symbols:
                if symbol in selected:
                    continue
                selected.append(symbol)
                if len(selected) >= per_cycle:
                    break
        return selected

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
    def _median(values: List[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        mid = n // 2
        if n % 2 == 1:
            return float(s[mid])
        return float((s[mid - 1] + s[mid]) / 2.0)

    def _micro_feature_lookback_bars(self) -> int:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        micro_cfg = ff_cfg.get("microstructure", {}) if isinstance(ff_cfg.get("microstructure"), dict) else {}
        lookback = int(self._to_float(micro_cfg.get("zscore_lookback_bars"), 120))
        return max(20, min(720, lookback))

    def _get_micro_feature_history(self, symbol: str) -> Dict[str, Deque[float]]:
        key = str(symbol or "").upper()
        lookback = self._micro_feature_lookback_bars()
        hist = self._micro_feature_history.get(key)
        if isinstance(hist, dict):
            sample = hist.get("imbalance")
            if isinstance(sample, deque) and sample.maxlen == lookback:
                return hist
        new_hist: Dict[str, Deque[float]] = {
            "imbalance": deque(maxlen=lookback),
            "spread_bps": deque(maxlen=lookback),
            "phantom": deque(maxlen=lookback),
            "micro_delta_norm": deque(maxlen=lookback),
        }
        if isinstance(hist, dict):
            for metric in ("imbalance", "spread_bps", "phantom", "micro_delta_norm"):
                old_q = hist.get(metric)
                if isinstance(old_q, deque):
                    for x in list(old_q)[-lookback:]:
                        new_hist[metric].append(self._to_float(x, 0.0))
        self._micro_feature_history[key] = new_hist
        return new_hist

    def _robust_zscore(self, value: float, history: Deque[float]) -> float:
        if not isinstance(history, deque) or len(history) < 12:
            return 0.0
        arr = [self._to_float(x, 0.0) for x in history]
        med = self._median(arr)
        devs = [abs(x - med) for x in arr]
        mad = self._median(devs)
        if mad <= 1e-9:
            return 0.0
        sigma = 1.4826 * mad
        z = (value - med) / sigma
        if z > 6.0:
            return 6.0
        if z < -6.0:
            return -6.0
        return float(z)

    @staticmethod
    def _parse_timeframe_seconds(value: Any) -> Optional[int]:
        tf = str(value or "").strip().lower()
        if not tf or tf == "raw":
            return None
        unit = tf[-1]
        try:
            n = int(tf[:-1])
        except Exception:
            return None
        if n <= 0:
            return None
        if unit == "s":
            return n
        if unit == "m":
            return n * 60
        if unit == "h":
            return n * 3600
        if unit == "d":
            return n * 86400
        return None

    def _decision_timeframe_seconds(self) -> Optional[int]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        tf = ff_cfg.get("decision_timeframe") or ff_cfg.get("signal_timeframe")
        return self._parse_timeframe_seconds(tf)

    def _ai_review_config(self) -> Dict[str, Any]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        ai_cfg_raw = ff_cfg.get("ai_review", {})
        ai_cfg = ai_cfg_raw if isinstance(ai_cfg_raw, dict) else {}
        decision_tf_seconds = self._decision_timeframe_seconds() or 0
        position_tf_seconds = self._parse_timeframe_seconds(ai_cfg.get("position_timeframe")) or 300
        flat_tf_seconds = self._parse_timeframe_seconds(ai_cfg.get("flat_timeframe")) or decision_tf_seconds or 900
        flat_top_n = max(1, int(self._to_float(ai_cfg.get("flat_top_n", 2), 2)))
        return {
            "enabled": bool(ai_cfg.get("enabled", True)),
            "position_timeframe_seconds": max(60, position_tf_seconds),
            "flat_timeframe_seconds": max(60, flat_tf_seconds),
            "flat_top_n": flat_top_n,
        }

    def _is_kline_alignment_active(self) -> bool:
        schedule_cfg = self.config.get("schedule", {}) or {}
        if not bool(schedule_cfg.get("align_to_kline_close", True)):
            return False
        tf_seconds = self._decision_timeframe_seconds()
        return bool(tf_seconds and tf_seconds > 0)

    def _kline_alignment_sleep_seconds(self) -> float:
        if not self._is_kline_alignment_active():
            return 0.0

        schedule_cfg = self.config.get("schedule", {}) or {}
        tf_seconds = self._decision_timeframe_seconds()
        if not tf_seconds or tf_seconds <= 0:
            return 0.0

        delay_seconds = max(0.0, self._to_float(schedule_cfg.get("kline_close_delay_seconds", 3), 3.0))
        now_ts = time.time()
        base_close_ts = math.floor(now_ts / float(tf_seconds)) * float(tf_seconds)
        next_fire_ts = base_close_ts + delay_seconds
        if next_fire_ts <= now_ts + 1e-6:
            next_fire_ts += float(tf_seconds)
        return max(0.0, next_fire_ts - now_ts)

    def _aligned_sleep_seconds_for(self, timeframe_seconds: int) -> float:
        if timeframe_seconds <= 0:
            return 0.0
        schedule_cfg = self.config.get("schedule", {}) or {}
        delay_seconds = max(0.0, self._to_float(schedule_cfg.get("kline_close_delay_seconds", 3), 3.0))
        now_ts = time.time()
        base_close_ts = math.floor(now_ts / float(timeframe_seconds)) * float(timeframe_seconds)
        next_fire_ts = base_close_ts + delay_seconds
        if next_fire_ts <= now_ts + 1e-6:
            next_fire_ts += float(timeframe_seconds)
        return max(0.0, next_fire_ts - now_ts)

    def _should_allow_aligned_cycle(
        self,
        *,
        bucket_key: str,
        timeframe_seconds: int,
        now_ts: Optional[float] = None,
    ) -> bool:
        if timeframe_seconds <= 0:
            return True
        schedule_cfg = self.config.get("schedule", {}) or {}
        if not bool(schedule_cfg.get("align_to_kline_close", True)):
            return True

        delay_seconds = max(0.0, self._to_float(schedule_cfg.get("kline_close_delay_seconds", 3), 3.0))
        ts = float(now_ts if now_ts is not None else time.time())
        interval_seconds = max(1.0, float(int(schedule_cfg.get("interval_seconds", 60) or 60)))
        close_ts = math.floor(ts / float(timeframe_seconds)) * float(timeframe_seconds)
        open_ts = close_ts + delay_seconds

        if ts + 1e-6 < open_ts:
            return False

        window_seconds = min(float(timeframe_seconds), interval_seconds)
        if (ts - open_ts) > window_seconds:
            return False

        bucket_id = int(close_ts // float(timeframe_seconds))
        if self._analysis_bucket_state.get(bucket_key) == bucket_id:
            return False
        self._analysis_bucket_state[bucket_key] = bucket_id
        return True

    def _should_allow_entries_this_cycle(self, now_ts: Optional[float] = None) -> bool:
        """
        开仓窗口门控：
        - 对齐关闭: 每轮都允许开仓/加仓
        - 对齐开启: 仅在 flat_timeframe 收线延迟后的一个轮询窗口内放行一次
          例: 5m + delay=3s 时，仅在 xx:05:03 ~ xx:06:03（默认 interval=60s）放行一次
        """
        if not self._is_kline_alignment_active():
            return True
        ai_review_cfg = self._ai_review_config()
        tf_seconds = int(ai_review_cfg.get("flat_timeframe_seconds", 0) or 0)
        if not tf_seconds or tf_seconds <= 0:
            return True

        schedule_cfg = self.config.get("schedule", {}) or {}
        delay_seconds = max(0.0, self._to_float(schedule_cfg.get("kline_close_delay_seconds", 3), 3.0))
        ts = float(now_ts if now_ts is not None else time.time())
        interval_seconds = max(1.0, float(int(schedule_cfg.get("interval_seconds", 60) or 60)))

        # 以“已收线K线”的 close 时间作为开仓窗口基准，确保非整5m分钟不会开仓。
        close_ts = math.floor(ts / float(tf_seconds)) * float(tf_seconds)
        open_ts = close_ts + delay_seconds

        # 未到开仓延迟时间，不放行。
        if ts + 1e-6 < open_ts:
            return False

        # 仅在一个轮询窗口内放行，避免 xx:06/xx:07 等非整5m分钟触发开仓。
        window_seconds = min(float(tf_seconds), interval_seconds)
        if (ts - open_ts) > window_seconds:
            return False

        bucket_id = int(close_ts // float(tf_seconds))
        if self._last_entry_bucket_id == bucket_id:
            return False
        self._last_entry_bucket_id = bucket_id
        return True

    @staticmethod
    def _normalize_percent_to_ratio(value: Any, default_ratio: float) -> float:
        try:
            v = float(value)
        except Exception:
            return default_ratio
        v = abs(v)
        if v > 1.0:
            return v / 100.0
        return v

    def _estimate_position_portion(self, position: Optional[Dict[str, Any]], account_summary: Dict[str, Any]) -> float:
        if not isinstance(position, dict):
            return 0.0
        equity = self._to_float(account_summary.get("equity"), 0.0)
        if equity <= 0:
            return 0.0
        margin = self._to_float(position.get("margin"), 0.0)
        if margin <= 0:
            amount = self._to_float(position.get("amount"), 0.0)
            entry_price = self._to_float(position.get("entry_price"), 0.0)
            leverage = self._to_float(position.get("leverage"), 0.0)
            if amount > 0 and entry_price > 0 and leverage > 0:
                margin = abs(amount * entry_price / leverage)
        if margin <= 0:
            return 0.0
        portion = margin / equity
        if portion < 0:
            return 0.0
        return portion

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt

    def _risk_config(self) -> Dict[str, Any]:
        risk_cfg = self.config.get("risk", {}) or {}
        ff_cfg = self.config.get("fund_flow", {}) or {}
        max_daily_loss_pct = self._normalize_percent_to_ratio(
            risk_cfg.get("daily_cooldown_pct", risk_cfg.get("max_daily_loss_percent", 0.1)),
            0.1,
        )
        return {
            "enabled": bool(risk_cfg.get("account_circuit_enabled", True)),
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_consecutive_losses": max(1, int(risk_cfg.get("max_consecutive_losses", 3) or 3)),
            "daily_loss_cooldown_seconds": max(
                0,
                int(
                    risk_cfg.get(
                        "daily_loss_cooldown_seconds",
                        ff_cfg.get("daily_loss_cooldown_seconds", 8 * 3600),
                    )
                    or 8 * 3600
                ),
            ),
            "consecutive_loss_cooldown_seconds": max(
                0,
                int(
                    risk_cfg.get(
                        "consecutive_loss_cooldown_seconds",
                        ff_cfg.get("consecutive_loss_cooldown_seconds", 30 * 60),
                    )
                    or 30 * 60
                ),
            ),
            "daily_reset_timezone": str(risk_cfg.get("daily_reset_timezone", "Asia/Tokyo")),
        }

    def _protection_sla_config(self) -> Dict[str, Any]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        enabled = bool(ff_cfg.get("protection_sla_enabled", True))
        timeout_seconds = max(1, int(ff_cfg.get("protection_sla_seconds", 60) or 60))
        force_flatten = bool(ff_cfg.get("protection_sla_force_flatten", True))
        immediate_close_on_repair_fail = bool(ff_cfg.get("protection_immediate_close_on_repair_fail", False))
        alert_cooldown_seconds = max(5, int(ff_cfg.get("protection_sla_alert_cooldown_seconds", 30) or 30))
        # 固定强平：保护单修复失败时始终按100%仓位执行减仓/平仓。
        reduce_ratio = 1.0
        return {
            "enabled": enabled,
            "timeout_seconds": timeout_seconds,
            "force_flatten_on_breach": force_flatten,
            "immediate_close_on_repair_fail": immediate_close_on_repair_fail,
            "alert_cooldown_seconds": alert_cooldown_seconds,
            "repair_fail_reduce_ratio": reduce_ratio,
        }

    def _pretrade_risk_gate_config(self) -> Dict[str, Any]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        gate_cfg = ff_cfg.get("pretrade_risk_gate", {}) if isinstance(ff_cfg.get("pretrade_risk_gate"), dict) else {}
        defaults = RiskConfig()
        volatility_cap = max(1e-6, self._normalize_percent_to_ratio(gate_cfg.get("volatility_cap", 0.01), 0.01))
        volatility_cap_capture = max(
            1e-6,
            self._normalize_percent_to_ratio(
                gate_cfg.get("volatility_cap_capture", gate_cfg.get("volatility_cap", 0.01)),
                self._normalize_percent_to_ratio(gate_cfg.get("volatility_cap", 0.01), 0.01),
            ),
        )
        block_actions_raw = gate_cfg.get("entry_block_actions", ["EXIT", "BLOCK", "AVOID"])
        if not isinstance(block_actions_raw, list):
            block_actions_raw = ["EXIT", "BLOCK", "AVOID"]
        entry_block_actions = [str(x).upper() for x in block_actions_raw if str(x).strip()]
        if not entry_block_actions:
            entry_block_actions = ["EXIT", "BLOCK", "AVOID"]
        return {
            "enabled": bool(gate_cfg.get("enabled", True)),
            "force_exit_on_gate": bool(gate_cfg.get("force_exit_on_gate", True)),
            "entry_block_actions": entry_block_actions,
            "entry_hold_portion_scale": min(1.0, max(0.1, self._to_float(gate_cfg.get("entry_hold_portion_scale", 0.6), 0.6))),
            "entry_hold_leverage_cap": max(1.0, self._to_float(gate_cfg.get("entry_hold_leverage_cap", 2.0), 2.0)),
            "exit_close_ratio": min(1.0, max(0.1, self._to_float(gate_cfg.get("exit_close_ratio", 1.0), 1.0))),
            "exit_score_threshold": min(
                1.0,
                max(0.0, self._to_float(gate_cfg.get("exit_score_threshold", 0.12), 0.12)),
            ),
            "exit_confirm_bars": max(1, int(self._to_float(gate_cfg.get("exit_confirm_bars", 2), 2))),
            "exit_min_hold_seconds": max(0, int(self._to_float(gate_cfg.get("exit_min_hold_seconds", 300), 300))),
            "exit_require_price_followthrough": bool(gate_cfg.get("exit_require_price_followthrough", True)),
            "exit_price_change_min": max(
                0.0,
                self._normalize_percent_to_ratio(gate_cfg.get("exit_price_change_min", 0.0006), 0.0006),
            ),
            "exit_drawdown_override": max(
                0.0,
                self._normalize_percent_to_ratio(gate_cfg.get("exit_drawdown_override", 0.01), 0.01),
            ),
            "momentum_scale": max(1.0, self._to_float(gate_cfg.get("momentum_scale", 300.0), 300.0)),
            "volatility_cap": volatility_cap,
            "volatility_cap_capture": volatility_cap_capture,
            "max_drawdown": max(
                0.001,
                self._normalize_percent_to_ratio(gate_cfg.get("max_drawdown", defaults.max_drawdown), defaults.max_drawdown),
            ),
            "max_exposure_per_trade": min(
                1.0,
                max(
                    0.01,
                    self._normalize_percent_to_ratio(
                        gate_cfg.get("max_exposure_per_trade", defaults.max_exposure_per_trade),
                        defaults.max_exposure_per_trade,
                    ),
                ),
            ),
            "entry_threshold": min(
                1.0,
                max(0.0, self._to_float(gate_cfg.get("entry_threshold", defaults.entry_threshold), defaults.entry_threshold)),
            ),
            "entry_threshold_capture": min(
                1.0,
                max(
                    0.0,
                    self._to_float(
                        gate_cfg.get("entry_threshold_capture", gate_cfg.get("entry_threshold", defaults.entry_threshold)),
                        self._to_float(gate_cfg.get("entry_threshold", defaults.entry_threshold), defaults.entry_threshold),
                    ),
                ),
            ),
            "trend_weight": self._to_float(gate_cfg.get("trend_weight", defaults.trend_weight), defaults.trend_weight),
            "momentum_weight": self._to_float(gate_cfg.get("momentum_weight", defaults.momentum_weight), defaults.momentum_weight),
            "volatility_weight": self._to_float(gate_cfg.get("volatility_weight", defaults.volatility_weight), defaults.volatility_weight),
            "drawdown_weight": self._to_float(gate_cfg.get("drawdown_weight", defaults.drawdown_weight), defaults.drawdown_weight),
        }

    def _stale_protection_cleanup_config(self) -> Dict[str, Any]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        enabled = bool(ff_cfg.get("stale_protection_cleanup_enabled", True))
        delay_seconds = max(0, int(ff_cfg.get("stale_protection_cleanup_delay_seconds", 3) or 3))
        return {"enabled": enabled, "delay_seconds": delay_seconds}

    def _dca_config(self, engine_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        override = engine_override if isinstance(engine_override, dict) else {}
        enabled = bool(ff_cfg.get("dca_martingale_enabled", ff_cfg.get("dca_enabled", False)))
        if "dca_max_additions" in override:
            enabled = int(self._to_float(override.get("dca_max_additions"), 0)) > 0
        base_add_portion = self._normalize_percent_to_ratio(
            override.get("add_position_portion", ff_cfg.get("add_position_portion", ff_cfg.get("default_target_portion", 0.2))),
            0.2,
        )

        thresholds_raw = override.get("dca_drawdown_thresholds", ff_cfg.get("dca_drawdown_thresholds", [0.008, 0.016])) or []
        thresholds: List[float] = []
        if isinstance(thresholds_raw, list):
            for item in thresholds_raw:
                v = self._normalize_percent_to_ratio(item, 0.0)
                if v > 0:
                    thresholds.append(v)
        if not thresholds:
            thresholds = [0.008, 0.016]
        thresholds = sorted(thresholds)

        multipliers_raw = override.get("dca_multipliers", ff_cfg.get("dca_multipliers", [1.0, 2.0])) or []
        multipliers: List[float] = []
        if isinstance(multipliers_raw, list):
            for item in multipliers_raw:
                try:
                    m = float(item)
                except Exception:
                    m = 1.0
                if m <= 0:
                    m = 1.0
                multipliers.append(m)
        if not multipliers:
            multipliers = [1.0] * len(thresholds)
        if len(multipliers) < len(thresholds):
            multipliers.extend([multipliers[-1]] * (len(thresholds) - len(multipliers)))
        elif len(multipliers) > len(thresholds):
            multipliers = multipliers[: len(thresholds)]

        max_additions = int(override.get("dca_max_additions", ff_cfg.get("dca_max_additions", len(thresholds))) or len(thresholds))
        max_additions = max(0, min(max_additions, len(thresholds)))
        min_trigger_interval_seconds = max(0, int(ff_cfg.get("dca_min_trigger_interval_seconds", 0) or 0))
        return {
            "enabled": enabled,
            "base_add_portion": float(base_add_portion),
            "drawdown_thresholds": thresholds,
            "multipliers": multipliers,
            "max_additions": max_additions,
            "min_trigger_interval_seconds": min_trigger_interval_seconds,
        }

    def _extreme_volatility_cooldown_config(self) -> Dict[str, Any]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        timeframe = str(ff_cfg.get("extreme_volatility_cooldown_timeframe", "15m") or "15m").strip().lower()
        if timeframe not in {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"}:
            timeframe = "15m"
        atr_threshold = self._normalize_percent_to_ratio(
            ff_cfg.get("extreme_volatility_cooldown_atr_pct", 0.02),
            0.02,
        )
        return {
            "enabled": bool(ff_cfg.get("extreme_volatility_cooldown_enabled", True)),
            "timeframe": timeframe,
            "atr_pct_threshold": max(0.0, float(atr_threshold)),
            "consecutive_bars": max(
                1,
                int(ff_cfg.get("extreme_volatility_cooldown_consecutive_bars", 2) or 2),
            ),
            "cooldown_seconds": max(
                0,
                int(ff_cfg.get("extreme_volatility_cooldown_seconds", 30 * 60) or 30 * 60),
            ),
        }

    def _ma10_macd_confluence_config(self) -> Dict[str, Any]:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        raw = ff_cfg.get("ma10_macd_confluence", {}) if isinstance(ff_cfg.get("ma10_macd_confluence"), dict) else {}
        tf_exec = str(raw.get("tf_exec", raw.get("exec_tf", "5m")) or "5m").strip().lower()
        tf_anchor = str(raw.get("tf_anchor", raw.get("anchor_tf", "1h")) or "1h").strip().lower()
        allowed_tf = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"}
        if tf_exec not in allowed_tf:
            tf_exec = "5m"
        if tf_anchor not in allowed_tf:
            tf_anchor = "1h"
        return {
            "enabled": bool(raw.get("enabled", True)),
            "tf_exec": tf_exec,
            "tf_anchor": tf_anchor,
            "ma_period": max(2, int(self._to_float(raw.get("ma_period", 10), 10))),
            "macd_fast": max(2, int(self._to_float(raw.get("macd_fast", 12), 12))),
            "macd_slow": max(3, int(self._to_float(raw.get("macd_slow", 26), 26))),
            "macd_signal": max(2, int(self._to_float(raw.get("macd_signal", 9), 9))),
            "kline_limit_exec": max(40, int(self._to_float(raw.get("kline_limit_exec", 160), 160))),
            "kline_limit_anchor": max(20, int(self._to_float(raw.get("kline_limit_anchor", 80), 80))),
            "entry_hard_filter": bool(raw.get("entry_hard_filter", True)),
            "entry_require_macd_trigger": bool(raw.get("entry_require_macd_trigger", True)),
            "entry_allow_macd_early": bool(raw.get("entry_allow_macd_early", True)),
            "entry_macd_early_hist_min": self._to_float(raw.get("entry_macd_early_hist_min", 0.0), 0.0),
            "entry_macd_early_expand_bars": max(1, int(self._to_float(raw.get("entry_macd_early_expand_bars", 2), 2))),
            "entry_soft_penalty_no_macd": min(0.5, max(0.0, self._to_float(raw.get("entry_soft_penalty_no_macd", 0.08), 0.08))),
            "entry_soft_penalty_no_kdj": min(0.5, max(0.0, self._to_float(raw.get("entry_soft_penalty_no_kdj", 0.04), 0.04))),
            "entry_hard_block_against_ma10": bool(raw.get("entry_hard_block_against_ma10", True)),
            "entry_hard_block_reverse_macd": bool(raw.get("entry_hard_block_reverse_macd", True)),
            "block_on_opposite_bias": bool(raw.get("block_on_opposite_bias", True)),
            "buy_cross": str(raw.get("buy_cross", "GOLDEN") or "GOLDEN").upper(),
            "sell_cross": str(raw.get("sell_cross", "DEAD") or "DEAD").upper(),
            "buy_block_zone": str(raw.get("buy_block_zone", "BELOW_ZERO") or "BELOW_ZERO").upper(),
            "sell_block_zone": str(raw.get("sell_block_zone", "ABOVE_ZERO") or "ABOVE_ZERO").upper(),
            "neutral_bias_mode": str(raw.get("neutral_bias_mode", "degrade") or "degrade").strip().lower(),
            "neutral_bias_portion_scale": min(
                1.0,
                max(0.1, self._to_float(raw.get("neutral_bias_portion_scale", 0.6), 0.6)),
            ),
            "neutral_bias_leverage_cap": max(1, int(self._to_float(raw.get("neutral_bias_leverage_cap", 2), 2))),
            "bias_boost": min(0.5, max(0.0, self._to_float(raw.get("bias_boost", 0.12), 0.12))),
            "bias_penalty": min(0.5, max(0.0, self._to_float(raw.get("bias_penalty", 0.10), 0.10))),
            "cross_boost": min(0.5, max(0.0, self._to_float(raw.get("cross_boost", 0.06), 0.06))),
            "zone_boost": min(0.5, max(0.0, self._to_float(raw.get("zone_boost", 0.04), 0.04))),
            "hist_boost": min(0.5, max(0.0, self._to_float(raw.get("hist_boost", 0.03), 0.03))),
            "max_adjust": min(1.0, max(0.0, self._to_float(raw.get("max_adjust", 0.25), 0.25))),
            "exit_anchor_enabled": bool(raw.get("exit_anchor_enabled", True)),
            "exit_anchor_require_hist_expand": bool(raw.get("exit_anchor_require_hist_expand", True)),
            "exit_anchor_skip_on_hard_block": bool(raw.get("exit_anchor_skip_on_hard_block", True)),
        }

    @staticmethod
    def _timeframe_seconds(tf: str) -> int:
        mapping = {
            "1m": 60,
            "3m": 3 * 60,
            "5m": 5 * 60,
            "15m": 15 * 60,
            "30m": 30 * 60,
            "1h": 60 * 60,
            "2h": 2 * 60 * 60,
            "4h": 4 * 60 * 60,
        }
        return int(mapping.get(str(tf).strip().lower(), 15 * 60))

    def _timeframe_bucket_key(self, tf: str, now_ts: Optional[float] = None) -> str:
        sec = max(1, self._timeframe_seconds(tf))
        ts = float(now_ts) if now_ts is not None else time.time()
        bucket = int(ts // sec)
        return f"{str(tf).strip().lower()}:{bucket}"

    @staticmethod
    def _sma(values: List[float], period: int) -> float:
        n = int(period)
        if n <= 0 or not values or len(values) < n:
            return 0.0
        window = values[-n:]
        return float(sum(window) / float(n))

    @staticmethod
    def _ema_series(values: List[float], period: int) -> List[float]:
        n = int(period)
        if n <= 0 or not values:
            return []
        k = 2.0 / (float(n) + 1.0)
        ema: List[float] = []
        prev = float(values[0])
        for raw in values:
            x = float(raw)
            prev = (x - prev) * k + prev
            ema.append(prev)
        return ema

    @staticmethod
    def _extract_closes_from_klines(klines: Any) -> List[float]:
        closes: List[float] = []
        if not isinstance(klines, list):
            return closes
        for item in klines:
            try:
                if isinstance(item, (list, tuple)) and len(item) > 4:
                    closes.append(float(item[4]))
                elif isinstance(item, dict):
                    closes.append(float(item.get("close", item.get("c", 0.0)) or 0.0))
            except Exception:
                continue
        return [x for x in closes if x > 0]

    @staticmethod
    def _extract_ohlc_from_klines(klines: Any) -> Tuple[List[float], List[float], List[float], List[float]]:
        opens: List[float] = []
        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []
        if not isinstance(klines, list):
            return opens, highs, lows, closes
        for item in klines:
            try:
                if isinstance(item, (list, tuple)) and len(item) > 4:
                    o = float(item[1])
                    h = float(item[2])
                    l = float(item[3])
                    c = float(item[4])
                elif isinstance(item, dict):
                    o = float(item.get("open", item.get("o", 0.0)) or 0.0)
                    h = float(item.get("high", item.get("h", 0.0)) or 0.0)
                    l = float(item.get("low", item.get("l", 0.0)) or 0.0)
                    c = float(item.get("close", item.get("c", 0.0)) or 0.0)
                else:
                    continue
                if o > 0 and h > 0 and l > 0 and c > 0 and h >= l:
                    opens.append(o)
                    highs.append(h)
                    lows.append(l)
                    closes.append(c)
            except Exception:
                continue
        return opens, highs, lows, closes

    @staticmethod
    def _clip_unit(value: float) -> float:
        return max(-1.0, min(1.0, float(value)))

    def _macd_state_from_closes(
        self,
        closes: List[float],
        *,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Dict[str, Any]:
        min_len = max(int(slow), int(signal)) + 5
        if len(closes) < min_len:
            return {
                "macd": 0.0,
                "signal": 0.0,
                "hist": 0.0,
                "cross": "NONE",
                "zone": "NEAR_ZERO",
                "hist_expand": False,
                "hist_expand_up": False,
                "hist_expand_down": False,
            }
        ema_fast = self._ema_series(closes, int(fast))
        ema_slow = self._ema_series(closes, int(slow))
        macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
        sig_line = self._ema_series(macd_line, int(signal))
        hist = [m - s for m, s in zip(macd_line, sig_line)]
        if len(macd_line) < 3 or len(sig_line) < 3 or len(hist) < 3:
            return {
                "macd": 0.0,
                "signal": 0.0,
                "hist": 0.0,
                "cross": "NONE",
                "zone": "NEAR_ZERO",
                "hist_expand": False,
                "hist_expand_up": False,
                "hist_expand_down": False,
            }

        m0, s0, h0 = float(macd_line[-1]), float(sig_line[-1]), float(hist[-1])
        m1, s1 = float(macd_line[-2]), float(sig_line[-2])
        cross = "NONE"
        if (m1 <= s1) and (m0 > s0):
            cross = "GOLDEN"
        elif (m1 >= s1) and (m0 < s0):
            cross = "DEAD"

        if m0 > 0:
            zone = "ABOVE_ZERO"
        elif m0 < 0:
            zone = "BELOW_ZERO"
        else:
            zone = "NEAR_ZERO"

        hist_expand_up = hist[-1] > hist[-2] > hist[-3]
        hist_expand_down = hist[-1] < hist[-2] < hist[-3]
        hist_expand = abs(hist[-1]) > abs(hist[-2]) > abs(hist[-3])
        hist_delta = float(hist[-1] - hist[-2])
        tail = hist[-20:] if len(hist) >= 20 else hist
        hist_abs_mean = sum(abs(float(x)) for x in tail) / max(1, len(tail))
        hist_norm = self._clip_unit(h0 / max(hist_abs_mean * 2.5, 1e-9))
        return {
            "macd": m0,
            "signal": s0,
            "hist": h0,
            "hist_norm": hist_norm,
            "hist_delta": hist_delta,
            "cross": cross,
            "zone": zone,
            "hist_expand": bool(hist_expand),
            "hist_expand_up": bool(hist_expand_up),
            "hist_expand_down": bool(hist_expand_down),
        }

    def _kdj_state_from_ohlc(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        *,
        period: int = 9,
        smooth: int = 3,
    ) -> Dict[str, Any]:
        min_len = max(int(period) + 2, 12)
        if len(highs) < min_len or len(lows) < min_len or len(closes) < min_len:
            return {
                "k": 50.0,
                "d": 50.0,
                "j": 50.0,
                "k_norm": 0.0,
                "d_norm": 0.0,
                "j_norm": 0.0,
                "cross": "NONE",
                "zone": "MID",
            }
        p = max(2, int(period))
        alpha = 1.0 / float(max(1, int(smooth)))
        k_series: List[float] = []
        d_series: List[float] = []
        k_prev = 50.0
        d_prev = 50.0
        for i in range(len(closes)):
            if i < p - 1:
                k_series.append(k_prev)
                d_series.append(d_prev)
                continue
            ll = min(lows[i - p + 1 : i + 1])
            hh = max(highs[i - p + 1 : i + 1])
            span = max(hh - ll, 1e-9)
            rsv = (closes[i] - ll) / span * 100.0
            k_prev = (1.0 - alpha) * k_prev + alpha * rsv
            d_prev = (1.0 - alpha) * d_prev + alpha * k_prev
            k_series.append(k_prev)
            d_series.append(d_prev)
        k0 = float(k_series[-1])
        d0 = float(d_series[-1])
        k1 = float(k_series[-2])
        d1 = float(d_series[-2])
        j0 = float(3.0 * k0 - 2.0 * d0)
        cross = "NONE"
        if k1 <= d1 and k0 > d0:
            cross = "GOLDEN"
        elif k1 >= d1 and k0 < d0:
            cross = "DEAD"
        if j0 >= 80.0:
            zone = "HIGH"
        elif j0 <= 20.0:
            zone = "LOW"
        else:
            zone = "MID"
        return {
            "k": k0,
            "d": d0,
            "j": j0,
            "k_norm": self._clip_unit((k0 - 50.0) / 50.0),
            "d_norm": self._clip_unit((d0 - 50.0) / 50.0),
            "j_norm": self._clip_unit((j0 - 50.0) / 50.0),
            "cross": cross,
            "zone": zone,
        }

    def _bollinger_state_from_closes(
        self,
        closes: List[float],
        *,
        period: int = 20,
        num_std: float = 2.0,
    ) -> Dict[str, Any]:
        n = max(5, int(period))
        if len(closes) < n:
            return {
                "middle": 0.0,
                "upper": 0.0,
                "lower": 0.0,
                "width": 0.0,
                "width_norm": 0.0,
                "pos_norm": 0.0,
                "break": "NONE",
                "trend": "MID",
                "squeeze": False,
            }
        window = closes[-n:]
        middle = sum(window) / float(n)
        var = sum((x - middle) ** 2 for x in window) / float(n)
        std = math.sqrt(max(var, 0.0))
        upper = middle + num_std * std
        lower = middle - num_std * std
        close = float(closes[-1])
        band = max(upper - lower, 1e-9)
        width = band / max(abs(middle), 1e-9)
        pos_norm = self._clip_unit((close - (upper + lower) * 0.5) / max(band * 0.5, 1e-9))
        width_norm = self._clip_unit((width - 0.01) / 0.05)
        bb_break = "NONE"
        if close > upper:
            bb_break = "UPPER"
        elif close < lower:
            bb_break = "LOWER"
        trend = "MID"
        if close >= upper * 0.995:
            trend = "ALONG_UPPER"
        elif close <= lower * 1.005:
            trend = "ALONG_LOWER"
        squeeze = bool(width <= 0.02)
        return {
            "middle": float(middle),
            "upper": float(upper),
            "lower": float(lower),
            "width": float(width),
            "width_norm": float(width_norm),
            "pos_norm": float(pos_norm),
            "break": bb_break,
            "trend": trend,
            "squeeze": squeeze,
        }

    def _compute_ma10_macd_confluence(self, symbol: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        tf_exec = str(cfg.get("tf_exec", "5m"))
        tf_anchor = str(cfg.get("tf_anchor", "1h"))
        limit_exec = int(cfg.get("kline_limit_exec", 160))
        limit_anchor = int(cfg.get("kline_limit_anchor", 80))
        ma_period = int(cfg.get("ma_period", 10))
        macd_fast = int(cfg.get("macd_fast", 12))
        macd_slow = int(cfg.get("macd_slow", 26))
        macd_signal = int(cfg.get("macd_signal", 9))
        if macd_slow <= macd_fast:
            macd_slow = macd_fast + 1

        k_exec = self.client.get_klines(symbol=symbol, interval=tf_exec, limit=limit_exec) or []
        k_anchor = self.client.get_klines(symbol=symbol, interval=tf_anchor, limit=limit_anchor) or []
        opens_exec, highs_exec, lows_exec, closes_exec = self._extract_ohlc_from_klines(k_exec)
        closes_anchor = self._extract_closes_from_klines(k_anchor)

        ma10_5m = self._sma(closes_exec, ma_period)
        ma10_1h = self._sma(closes_anchor, ma_period)
        ma10_1h_prev = self._sma(closes_anchor[:-1], ma_period) if len(closes_anchor) > ma_period else 0.0
        slope = float(ma10_1h - ma10_1h_prev) if ma10_1h > 0 and ma10_1h_prev > 0 else 0.0
        last_close_exec = float(closes_exec[-1]) if closes_exec else 0.0
        last_open_exec = float(opens_exec[-1]) if opens_exec else 0.0
        last_close_anchor = float(closes_anchor[-1]) if closes_anchor else 0.0

        if last_close_anchor > ma10_1h and slope > 0:
            bias = 1
        elif last_close_anchor < ma10_1h and slope < 0:
            bias = -1
        else:
            bias = 0

        macd_state = self._macd_state_from_closes(
            closes_exec,
            fast=macd_fast,
            slow=macd_slow,
            signal=macd_signal,
        )
        kdj_state = self._kdj_state_from_ohlc(
            highs_exec,
            lows_exec,
            closes_exec,
            period=9,
            smooth=3,
        )
        bb_state = self._bollinger_state_from_closes(closes_exec, period=20, num_std=2.0)

        bb_break_bias = 0.0
        if bb_state.get("break") == "UPPER":
            bb_break_bias = 1.0
        elif bb_state.get("break") == "LOWER":
            bb_break_bias = -1.0
        bb_trend_bias = 0.0
        if bb_state.get("trend") == "ALONG_UPPER":
            bb_trend_bias = 1.0
        elif bb_state.get("trend") == "ALONG_LOWER":
            bb_trend_bias = -1.0

        macd_cross = str(macd_state.get("cross", "NONE")).upper()
        macd_cross_bias = 1.0 if macd_cross == "GOLDEN" else (-1.0 if macd_cross == "DEAD" else 0.0)
        kdj_cross = str(kdj_state.get("cross", "NONE")).upper()
        kdj_cross_bias = 1.0 if kdj_cross == "GOLDEN" else (-1.0 if kdj_cross == "DEAD" else 0.0)
        early_hist_min = self._to_float(cfg.get("entry_macd_early_hist_min", 0.0), 0.0)
        macd_hist = self._to_float(macd_state.get("hist", 0.0), 0.0)
        macd_early_pass_long = bool(
            cfg.get("entry_allow_macd_early", True)
            and bool(macd_state.get("hist_expand_up", False))
            and macd_hist >= early_hist_min
        )
        macd_early_pass_short = bool(
            cfg.get("entry_allow_macd_early", True)
            and bool(macd_state.get("hist_expand_down", False))
            and macd_hist <= -early_hist_min
        )
        macd_trigger_pass_long = macd_cross == str(cfg.get("buy_cross", "GOLDEN")).upper()
        macd_trigger_pass_short = macd_cross == str(cfg.get("sell_cross", "DEAD")).upper()
        kdj_zone = str(kdj_state.get("zone", "MID")).upper()
        kdj_support_pass_long = kdj_zone != "HIGH" or kdj_cross == "GOLDEN"
        kdj_support_pass_short = kdj_zone != "LOW" or kdj_cross == "DEAD"

        return {
            "ma10_5m": float(ma10_5m),
            "ma10_1h": float(ma10_1h),
            "ma10_1h_slope": float(slope),
            "ma10_1h_bias": int(bias),
            "last_open_5m": float(last_open_exec),
            "last_close_5m": float(last_close_exec),
            "last_close_1h": float(last_close_anchor),
            "macd_5m": float(macd_state.get("macd", 0.0)),
            "macd_5m_signal": float(macd_state.get("signal", 0.0)),
            "macd_5m_hist": float(macd_state.get("hist", 0.0)),
            "macd_5m_hist_norm": float(macd_state.get("hist_norm", 0.0)),
            "macd_5m_hist_delta": float(macd_state.get("hist_delta", 0.0)),
            "macd_5m_cross": str(macd_state.get("cross", "NONE")),
            "macd_5m_zone": str(macd_state.get("zone", "NEAR_ZERO")),
            "macd_5m_hist_expand": bool(macd_state.get("hist_expand", False)),
            "macd_5m_hist_expand_up": bool(macd_state.get("hist_expand_up", False)),
            "macd_5m_hist_expand_down": bool(macd_state.get("hist_expand_down", False)),
            "kdj_k": float(kdj_state.get("k", 50.0)),
            "kdj_d": float(kdj_state.get("d", 50.0)),
            "kdj_j": float(kdj_state.get("j", 50.0)),
            "kdj_k_norm": float(kdj_state.get("k_norm", 0.0)),
            "kdj_d_norm": float(kdj_state.get("d_norm", 0.0)),
            "kdj_j_norm": float(kdj_state.get("j_norm", 0.0)),
            "kdj_cross": str(kdj_state.get("cross", "NONE")),
            "kdj_zone": str(kdj_state.get("zone", "MID")),
            "bb_middle": float(bb_state.get("middle", 0.0)),
            "bb_upper": float(bb_state.get("upper", 0.0)),
            "bb_lower": float(bb_state.get("lower", 0.0)),
            "bb_width": float(bb_state.get("width", 0.0)),
            "bb_width_norm": float(bb_state.get("width_norm", 0.0)),
            "bb_pos_norm": float(bb_state.get("pos_norm", 0.0)),
            "bb_break": str(bb_state.get("break", "NONE")),
            "bb_break_bias": float(bb_break_bias),
            "bb_trend": str(bb_state.get("trend", "MID")),
            "bb_trend_bias": float(bb_trend_bias),
            "bb_squeeze": bool(bb_state.get("squeeze", False)),
            "macd_cross_bias": float(macd_cross_bias),
            "kdj_cross_bias": float(kdj_cross_bias),
            "macd_trigger_pass_long": bool(macd_trigger_pass_long),
            "macd_trigger_pass_short": bool(macd_trigger_pass_short),
            "macd_early_pass_long": bool(macd_early_pass_long),
            "macd_early_pass_short": bool(macd_early_pass_short),
            "kdj_support_pass_long": bool(kdj_support_pass_long),
            "kdj_support_pass_short": bool(kdj_support_pass_short),
            # DecisionEngine 读取的统一别名（避免只写 *_5m 导致主判特征缺失）
            "macd_hist_norm": float(macd_state.get("hist_norm", 0.0)),
            "macd_hist_delta": float(macd_state.get("hist_delta", 0.0)),
            "macd_cross": str(macd_state.get("cross", "NONE")),
            "macd_zone": str(macd_state.get("zone", "NEAR_ZERO")),
        }

    def _inject_confluence_into_flow_context(
        self,
        flow_context: Dict[str, Any],
        confluence: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> None:
        if not isinstance(flow_context, dict) or not isinstance(confluence, dict):
            return
        timeframes_raw = flow_context.get("timeframes")
        timeframes: Dict[str, Any] = timeframes_raw if isinstance(timeframes_raw, dict) else {}
        tf_exec = str(cfg.get("tf_exec", "5m") or "5m").strip().lower()
        tf_ctx_raw = timeframes.get(tf_exec)
        tf_ctx: Dict[str, Any] = tf_ctx_raw if isinstance(tf_ctx_raw, dict) else {}
        tf_ctx.update(
            {
                "last_open": self._to_float(confluence.get("last_open_5m"), self._to_float(tf_ctx.get("last_open"), 0.0)),
                "last_close": self._to_float(confluence.get("last_close_5m"), self._to_float(tf_ctx.get("last_close"), 0.0)),
                "macd_hist_norm": self._to_float(confluence.get("macd_hist_norm"), self._to_float(tf_ctx.get("macd_hist_norm"), 0.0)),
                "macd_hist_delta": self._to_float(confluence.get("macd_hist_delta"), self._to_float(tf_ctx.get("macd_hist_delta"), 0.0)),
                "macd_cross": str(confluence.get("macd_cross", tf_ctx.get("macd_cross", "NONE"))),
                "macd_zone": str(confluence.get("macd_zone", tf_ctx.get("macd_zone", "NEAR_ZERO"))),
                "kdj_k": self._to_float(confluence.get("kdj_k"), self._to_float(tf_ctx.get("kdj_k"), 50.0)),
                "kdj_d": self._to_float(confluence.get("kdj_d"), self._to_float(tf_ctx.get("kdj_d"), 50.0)),
                "kdj_j": self._to_float(confluence.get("kdj_j"), self._to_float(tf_ctx.get("kdj_j"), 50.0)),
                "kdj_k_norm": self._to_float(confluence.get("kdj_k_norm"), self._to_float(tf_ctx.get("kdj_k_norm"), 0.0)),
                "kdj_d_norm": self._to_float(confluence.get("kdj_d_norm"), self._to_float(tf_ctx.get("kdj_d_norm"), 0.0)),
                "kdj_j_norm": self._to_float(confluence.get("kdj_j_norm"), self._to_float(tf_ctx.get("kdj_j_norm"), 0.0)),
                "kdj_cross": str(confluence.get("kdj_cross", tf_ctx.get("kdj_cross", "NONE"))),
                "kdj_zone": str(confluence.get("kdj_zone", tf_ctx.get("kdj_zone", "MID"))),
                "bb_middle": self._to_float(confluence.get("bb_middle"), self._to_float(tf_ctx.get("bb_middle"), 0.0)),
                "bb_upper": self._to_float(confluence.get("bb_upper"), self._to_float(tf_ctx.get("bb_upper"), 0.0)),
                "bb_lower": self._to_float(confluence.get("bb_lower"), self._to_float(tf_ctx.get("bb_lower"), 0.0)),
                "bb_width": self._to_float(confluence.get("bb_width"), self._to_float(tf_ctx.get("bb_width"), 0.0)),
                "bb_width_norm": self._to_float(confluence.get("bb_width_norm"), self._to_float(tf_ctx.get("bb_width_norm"), 0.0)),
                "bb_pos_norm": self._to_float(confluence.get("bb_pos_norm"), self._to_float(tf_ctx.get("bb_pos_norm"), 0.0)),
                "bb_break": str(confluence.get("bb_break", tf_ctx.get("bb_break", "NONE"))),
                "bb_break_bias": self._to_float(confluence.get("bb_break_bias"), self._to_float(tf_ctx.get("bb_break_bias"), 0.0)),
                "bb_trend": str(confluence.get("bb_trend", tf_ctx.get("bb_trend", "MID"))),
                "bb_trend_bias": self._to_float(confluence.get("bb_trend_bias"), self._to_float(tf_ctx.get("bb_trend_bias"), 0.0)),
                "bb_squeeze": bool(confluence.get("bb_squeeze", tf_ctx.get("bb_squeeze", False))),
                "macd_cross_bias": self._to_float(confluence.get("macd_cross_bias"), self._to_float(tf_ctx.get("macd_cross_bias"), 0.0)),
                "kdj_cross_bias": self._to_float(confluence.get("kdj_cross_bias"), self._to_float(tf_ctx.get("kdj_cross_bias"), 0.0)),
            }
        )
        timeframes[tf_exec] = tf_ctx
        flow_context["timeframes"] = timeframes
        flow_context["_ma10_macd_confluence"] = dict(confluence)

    def _apply_ma10_macd_entry_filter(self, symbol: str, decision: FundFlowDecision) -> FundFlowDecision:
        cfg = self._ma10_macd_confluence_config()
        if not bool(cfg.get("enabled", True)) or not bool(cfg.get("entry_hard_filter", True)):
            return decision
        if decision.operation not in (FundFlowOperation.BUY, FundFlowOperation.SELL):
            return decision

        md_raw = getattr(decision, "metadata", None)
        md: Dict[str, Any] = md_raw if isinstance(md_raw, dict) else {}
        bias = int(self._to_float(md.get("ma10_1h_bias"), 0.0))
        macd_cross = str(md.get("macd_5m_cross", md.get("macd_cross", "NONE"))).upper()
        macd_hist_expand_up = bool(md.get("macd_5m_hist_expand_up", False))
        macd_hist_expand_down = bool(md.get("macd_5m_hist_expand_down", False))
        macd_trigger_pass_long = bool(md.get("macd_trigger_pass_long", False))
        macd_trigger_pass_short = bool(md.get("macd_trigger_pass_short", False))
        macd_early_pass_long = bool(md.get("macd_early_pass_long", False))
        macd_early_pass_short = bool(md.get("macd_early_pass_short", False))

        def _to_hold(tag: str) -> FundFlowDecision:
            base_reason = str(decision.reason or "").strip()
            return FundFlowDecision(
                operation=FundFlowOperation.HOLD,
                symbol=symbol,
                target_portion_of_balance=0.0,
                leverage=decision.leverage,
                reason=f"{base_reason} | {tag}" if base_reason else tag,
                metadata=md,
            )

        if bool(cfg.get("entry_hard_block_against_ma10", True)) and bias != 0:
            is_opposite = (
                (bias > 0 and decision.operation == FundFlowOperation.SELL)
                or (bias < 0 and decision.operation == FundFlowOperation.BUY)
            )
            if is_opposite:
                op_token = decision.operation.value if hasattr(decision.operation, "value") else str(decision.operation)
                return _to_hold(f"MA10_BIAS_BLOCK bias={bias} op={op_token}")
        elif bool(cfg.get("block_on_opposite_bias", True)) and bias != 0:
            is_opposite = (
                (bias > 0 and decision.operation == FundFlowOperation.SELL)
                or (bias < 0 and decision.operation == FundFlowOperation.BUY)
            )
            if is_opposite:
                op_token = decision.operation.value if hasattr(decision.operation, "value") else str(decision.operation)
                return _to_hold(f"MA10_BIAS_BLOCK bias={bias} op={op_token}")

        if bool(cfg.get("entry_hard_block_reverse_macd", True)):
            if decision.operation == FundFlowOperation.BUY and macd_cross == "DEAD" and macd_hist_expand_down:
                return _to_hold("MACD_REVERSE_BLOCK side=LONG cross=DEAD")
            if decision.operation == FundFlowOperation.SELL and macd_cross == "GOLDEN" and macd_hist_expand_up:
                return _to_hold("MACD_REVERSE_BLOCK side=SHORT cross=GOLDEN")

        if bool(cfg.get("entry_require_macd_trigger", True)):
            if decision.operation == FundFlowOperation.BUY and not (macd_trigger_pass_long or macd_early_pass_long):
                return _to_hold("MACD_TRIGGER_REQUIRED side=LONG")
            if decision.operation == FundFlowOperation.SELL and not (macd_trigger_pass_short or macd_early_pass_short):
                return _to_hold("MACD_TRIGGER_REQUIRED side=SHORT")
        return decision

    def _update_extreme_volatility_state(self, symbol: str, flow_context: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self._extreme_volatility_cooldown_config()
        symbol_up = str(symbol).upper()
        now = datetime.now(timezone.utc)
        expiry = self._volatility_cooldown_until_by_symbol.get(symbol_up)
        if isinstance(expiry, datetime) and expiry <= now:
            self._volatility_cooldown_until_by_symbol.pop(symbol_up, None)
            self._volatility_cooldown_reason_by_symbol.pop(symbol_up, None)

        if not bool(cfg.get("enabled", True)):
            self._volatility_spike_streak_by_symbol.pop(symbol_up, None)
            self._volatility_last_bucket_by_symbol.pop(symbol_up, None)
            self._volatility_cooldown_until_by_symbol.pop(symbol_up, None)
            self._volatility_cooldown_reason_by_symbol.pop(symbol_up, None)
            return {"enabled": False, "blocked": False}

        tf = str(cfg.get("timeframe", "15m"))
        tf_data = {}
        if isinstance(flow_context, dict):
            timeframes = flow_context.get("timeframes")
            if isinstance(timeframes, dict):
                tf_data = timeframes.get(tf) if isinstance(timeframes.get(tf), dict) else {}

        atr_pct = abs(self._to_float((tf_data or {}).get("atr_pct"), 0.0))
        threshold = float(cfg.get("atr_pct_threshold", 0.02) or 0.02)
        bucket_key = self._timeframe_bucket_key(tf)
        if self._volatility_last_bucket_by_symbol.get(symbol_up) != bucket_key:
            self._volatility_last_bucket_by_symbol[symbol_up] = bucket_key
            if threshold > 0 and atr_pct >= threshold:
                self._volatility_spike_streak_by_symbol[symbol_up] = int(
                    self._volatility_spike_streak_by_symbol.get(symbol_up, 0)
                ) + 1
            else:
                self._volatility_spike_streak_by_symbol[symbol_up] = 0

            streak = int(self._volatility_spike_streak_by_symbol.get(symbol_up, 0) or 0)
            if (
                threshold > 0
                and streak >= int(cfg.get("consecutive_bars", 2))
                and int(cfg.get("cooldown_seconds", 0)) > 0
            ):
                until = now + timedelta(seconds=int(cfg.get("cooldown_seconds", 0)))
                prev_until = self._volatility_cooldown_until_by_symbol.get(symbol_up)
                if not isinstance(prev_until, datetime) or prev_until < until:
                    self._volatility_cooldown_until_by_symbol[symbol_up] = until
                self._volatility_cooldown_reason_by_symbol[symbol_up] = (
                    f"extreme_volatility atr_pct={atr_pct:.4f} >= {threshold:.4f}, "
                    f"streak={streak}"
                )
                print(
                    f"⚠️ {symbol_up} 极端波动冷却触发: "
                    f"atr_pct={atr_pct:.4f}, threshold={threshold:.4f}, "
                    f"streak={streak}, until={self._volatility_cooldown_until_by_symbol[symbol_up].isoformat()}"
                )

        expire_at_raw = self._volatility_cooldown_until_by_symbol.get(symbol_up)
        expire_at: Optional[datetime] = expire_at_raw if isinstance(expire_at_raw, datetime) else None
        blocked = expire_at is not None and expire_at > now
        remaining = int((expire_at - now).total_seconds()) if expire_at is not None and blocked else 0
        return {
            "enabled": True,
            "blocked": bool(blocked),
            "remaining_seconds": max(0, remaining),
            "atr_pct": atr_pct,
            "threshold": threshold,
            "streak": int(self._volatility_spike_streak_by_symbol.get(symbol_up, 0) or 0),
            "reason": self._volatility_cooldown_reason_by_symbol.get(symbol_up),
            "timeframe": tf,
        }

    def _position_drawdown_ratio(self, position: Dict[str, Any], current_price: float) -> float:
        side = str(position.get("side", "")).upper()
        entry_price = self._to_float(position.get("entry_price"), 0.0)
        if current_price <= 0 or entry_price <= 0:
            return 0.0
        if side == "LONG":
            return max(0.0, (entry_price - current_price) / entry_price)
        if side == "SHORT":
            return max(0.0, (current_price - entry_price) / entry_price)
        return 0.0

    def _apply_pretrade_risk_gate(
        self,
        *,
        symbol: str,
        decision: FundFlowDecision,
        position: Optional[Dict[str, Any]],
        flow_context: Dict[str, Any],
        current_price: float,
        account_summary: Dict[str, Any],
    ) -> Tuple[FundFlowDecision, Dict[str, Any]]:
        cfg = self._pretrade_risk_gate_config()
        if not bool(cfg.get("enabled", True)):
            return decision, {"enabled": False, "action": "BYPASS"}

        md_raw = getattr(decision, "metadata", None)
        md: Dict[str, Any] = md_raw if isinstance(md_raw, dict) else {}
        entry_mode = str(md.get("entry_mode", "HOLD")).upper()
        is_capture_entry = (
            decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL)
            and entry_mode == "TREND_CAPTURE"
        )
        active_entry_threshold = self._to_float(
            cfg.get("entry_threshold_capture" if is_capture_entry else "entry_threshold"),
            cfg.get("entry_threshold"),
        )
        active_volatility_cap = max(
            1e-6,
            self._to_float(
                cfg.get("volatility_cap_capture" if is_capture_entry else "volatility_cap"),
                cfg.get("volatility_cap"),
            ),
        )
        long_score = self._to_float(md.get("long_score_adj", md.get("long_score")), 0.0)
        short_score = self._to_float(md.get("short_score_adj", md.get("short_score")), 0.0)
        trend_strength = min(1.0, max(0.0, max(long_score, short_score)))
        cvd_momentum = self._to_float(flow_context.get("cvd_momentum"), self._to_float(md.get("cvd_norm"), 0.0))
        momentum_strength = min(1.0, abs(cvd_momentum) * self._to_float(cfg.get("momentum_scale"), 300.0))
        atr_pct = abs(self._to_float(md.get("regime_atr_pct"), 0.0))
        volatility = min(1.0, atr_pct / active_volatility_cap)
        drawdown = self._position_drawdown_ratio(position, current_price) if isinstance(position, dict) else 0.0
        k_open = self._to_float(md.get("last_open"), 0.0)
        k_close = self._to_float(md.get("last_close"), 0.0)
        price_change = ((k_close - k_open) / k_open) if k_open > 0 else 0.0

        if isinstance(position, dict):
            direction = str(position.get("side", "NONE")).upper()
        elif decision.operation == FundFlowOperation.BUY:
            direction = "LONG"
        elif decision.operation == FundFlowOperation.SELL:
            direction = "SHORT"
        else:
            direction = "NONE"

        if isinstance(position, dict):
            equity_fraction = max(0.0, self._estimate_position_portion(position, account_summary))
        else:
            equity_fraction = max(0.0, self._to_float(decision.target_portion_of_balance, 0.0))

        leverage_available = max(
            1.0,
            self._to_float(
                account_summary.get("max_leverage", account_summary.get("leverage", decision.leverage or 1)),
                decision.leverage or 1,
            ),
        )
        state = {
            "symbol": symbol,
            "trend": trend_strength,
            "momentum": momentum_strength,
            "volatility": volatility,
            "drawdown": drawdown,
            "atr": atr_pct,
            "price_change": price_change,
            "direction": direction,
            "leverage_available": leverage_available,
            "equity_fraction": equity_fraction,
        }

        gate_meta: Dict[str, Any] = {"enabled": True, "state": state, "action": "HOLD", "score": 0.0}
        try:
            gate_result = gate_trade_decision(
                state,
                config=RiskConfig(
                    max_drawdown=self._to_float(cfg.get("max_drawdown"), 0.05),
                    max_exposure_per_trade=self._to_float(cfg.get("max_exposure_per_trade"), 0.25),
                    trend_weight=self._to_float(cfg.get("trend_weight"), 0.4),
                    momentum_weight=self._to_float(cfg.get("momentum_weight"), 0.3),
                    volatility_weight=self._to_float(cfg.get("volatility_weight"), 0.2),
                    drawdown_weight=self._to_float(cfg.get("drawdown_weight"), 0.3),
                    entry_threshold=active_entry_threshold,
                ),
                equity_fraction=equity_fraction,
                log_path=os.path.join(self.logs_dir, "trading_risk_gate.log"),
            )
            gate_action = str(gate_result.get("action", "HOLD")).upper()
            gate_score = self._to_float(gate_result.get("score"), 0.0)
            gate_details_raw = gate_result.get("details")
            gate_details: Dict[str, Any] = gate_details_raw if isinstance(gate_details_raw, dict) else {}
            gate_meta = {
                "enabled": True,
                "state": state,
                "action": gate_action,
                "score": gate_score,
                "profile": "capture" if is_capture_entry else "standard",
                "entry_threshold_used": active_entry_threshold,
                "volatility_cap_used": active_volatility_cap,
                "enter": bool(gate_result.get("enter", False)),
                "exit": bool(gate_result.get("exit", False)),
                "details": gate_details,
            }
            if isinstance(md, dict):
                md["pretrade_risk_gate"] = gate_meta

            pos_side = str(position.get("side", "")).upper() if isinstance(position, dict) else ""
            pos_key = self._position_track_key(symbol, pos_side) if pos_side in ("LONG", "SHORT") else ""
            if pos_key:
                if gate_action == "EXIT":
                    self._pre_risk_exit_streak_by_pos[pos_key] = int(self._pre_risk_exit_streak_by_pos.get(pos_key, 0) or 0) + 1
                else:
                    self._pre_risk_exit_streak_by_pos.pop(pos_key, None)

            if decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL):
                block_actions_cfg = cfg.get("entry_block_actions", ["EXIT", "BLOCK", "AVOID"])
                block_actions = {
                    str(x).upper()
                    for x in (block_actions_cfg if isinstance(block_actions_cfg, list) else ["EXIT", "BLOCK", "AVOID"])
                    if str(x).strip()
                }
                if gate_action in block_actions:
                    base_reason = str(decision.reason or "").strip()
                    block_reason = f"PRE_RISK_BLOCK action={gate_action} score={gate_score:.3f}"
                    return (
                        FundFlowDecision(
                            operation=FundFlowOperation.HOLD,
                            symbol=symbol,
                            target_portion_of_balance=0.0,
                            leverage=decision.leverage,
                            reason=f"{base_reason} | {block_reason}" if base_reason else block_reason,
                            metadata=md if isinstance(md, dict) else {},
                        ),
                        gate_meta,
                    )

                if gate_action == "HOLD":
                    portion_scale = min(1.0, max(0.1, self._to_float(cfg.get("entry_hold_portion_scale"), 0.6)))
                    lev_cap = max(1, int(self._to_float(cfg.get("entry_hold_leverage_cap"), 2)))
                    scaled_portion = max(
                        0.0,
                        min(1.0, self._to_float(decision.target_portion_of_balance, 0.0) * portion_scale),
                    )
                    if scaled_portion <= 0:
                        base_reason = str(decision.reason or "").strip()
                        block_reason = (
                            f"PRE_RISK_BLOCK action=HOLD score={gate_score:.3f} "
                            f"portion_scale={portion_scale:.2f}"
                        )
                        return (
                            FundFlowDecision(
                                operation=FundFlowOperation.HOLD,
                                symbol=symbol,
                                target_portion_of_balance=0.0,
                                leverage=decision.leverage,
                                reason=f"{base_reason} | {block_reason}" if base_reason else block_reason,
                                metadata=md if isinstance(md, dict) else {},
                            ),
                            gate_meta,
                        )
                    requested_lev = int(self._to_float(decision.leverage, 1.0))
                    scaled_lev = max(1, min(requested_lev, lev_cap))
                    base_reason = str(decision.reason or "").strip()
                    degrade_reason = (
                        f"PRE_RISK_DEGRADE action=HOLD score={gate_score:.3f} "
                        f"portion_scale={portion_scale:.2f} lev_cap={lev_cap}"
                    )
                    decision = FundFlowDecision(
                        operation=decision.operation,
                        symbol=symbol,
                        target_portion_of_balance=scaled_portion,
                        leverage=scaled_lev,
                        reason=f"{base_reason} | {degrade_reason}" if base_reason else degrade_reason,
                        metadata=md if isinstance(md, dict) else {},
                    )

            if (
                isinstance(position, dict)
                and decision.operation != FundFlowOperation.CLOSE
                and bool(cfg.get("force_exit_on_gate", True))
                and gate_action == "EXIT"
            ):
                hard_block = bool(gate_details.get("hard_block", False))
                exit_score_threshold = self._to_float(cfg.get("exit_score_threshold"), 0.12)
                exit_confirm_bars = max(1, int(self._to_float(cfg.get("exit_confirm_bars"), 2)))
                exit_min_hold_seconds = max(0, int(self._to_float(cfg.get("exit_min_hold_seconds"), 300)))
                exit_require_price_followthrough = bool(cfg.get("exit_require_price_followthrough", True))
                exit_price_change_min = abs(self._to_float(cfg.get("exit_price_change_min"), 0.0006))
                exit_drawdown_override = abs(self._to_float(cfg.get("exit_drawdown_override"), 0.01))
                exit_streak = int(self._pre_risk_exit_streak_by_pos.get(pos_key, 0) or 0) if pos_key else 0

                hold_seconds = 0
                if pos_key:
                    first_seen = self._position_first_seen_ts.get(pos_key)
                    if first_seen is not None:
                        hold_seconds = max(0, int(time.time() - float(first_seen)))

                score_ok = gate_score <= (-1.0 * exit_score_threshold)
                hold_ok = hold_seconds >= exit_min_hold_seconds
                drawdown_override = drawdown >= exit_drawdown_override
                if direction == "LONG":
                    price_followthrough = price_change <= (-1.0 * exit_price_change_min)
                elif direction == "SHORT":
                    price_followthrough = price_change >= exit_price_change_min
                else:
                    price_followthrough = False

                exit_confirmed = hard_block or (
                    score_ok
                    and exit_streak >= exit_confirm_bars
                    and (drawdown_override or hold_ok)
                    and ((not exit_require_price_followthrough) or drawdown_override or price_followthrough)
                )
                gate_meta["exit_streak"] = exit_streak
                gate_meta["exit_confirmed"] = bool(exit_confirmed)
                gate_meta["exit_hold_seconds"] = hold_seconds
                gate_meta["exit_price_change"] = price_change
                gate_meta["exit_drawdown"] = drawdown
                if isinstance(md, dict):
                    md["pretrade_risk_gate"] = gate_meta

                if not exit_confirmed:
                    gate_meta["action"] = "HOLD"
                    base_reason = str(decision.reason or "").strip()
                    delay_reason = (
                        "PRE_RISK_EXIT_DELAY "
                        f"score={gate_score:.3f}/{-exit_score_threshold:.3f} "
                        f"streak={exit_streak}/{exit_confirm_bars} "
                        f"hold={hold_seconds}s/{exit_min_hold_seconds}s "
                        f"pc={price_change:+.4f}"
                    )
                    return (
                        FundFlowDecision(
                            operation=FundFlowOperation.HOLD,
                            symbol=symbol,
                            target_portion_of_balance=0.0,
                            leverage=decision.leverage,
                            reason=f"{base_reason} | {delay_reason}" if base_reason else delay_reason,
                            metadata=md if isinstance(md, dict) else {},
                        ),
                        gate_meta,
                    )

                confluence_cfg = self._ma10_macd_confluence_config()
                if (
                    bool(confluence_cfg.get("enabled", True))
                    and bool(confluence_cfg.get("exit_anchor_enabled", True))
                    and direction in ("LONG", "SHORT")
                ):
                    skip_on_hard_block = bool(confluence_cfg.get("exit_anchor_skip_on_hard_block", True))
                    skip_anchor = bool(drawdown_override) or (skip_on_hard_block and bool(hard_block))
                    if not skip_anchor:
                        ma10_5m = self._to_float(md.get("ma10_5m"), 0.0)
                        last_close_5m = self._to_float(md.get("last_close_5m"), self._to_float(md.get("last_close"), 0.0))
                        macd_zone = str(md.get("macd_5m_zone", "NEAR_ZERO")).upper()
                        require_hist_expand = bool(confluence_cfg.get("exit_anchor_require_hist_expand", True))
                        if direction == "LONG":
                            hist_ok = bool(md.get("macd_5m_hist_expand_up", md.get("macd_5m_hist_expand", False)))
                            zone_ok = macd_zone != "BELOW_ZERO"
                            structure_ok = ma10_5m > 0 and last_close_5m >= ma10_5m
                        else:
                            hist_ok = bool(md.get("macd_5m_hist_expand_down", md.get("macd_5m_hist_expand", False)))
                            zone_ok = macd_zone != "ABOVE_ZERO"
                            structure_ok = ma10_5m > 0 and last_close_5m <= ma10_5m
                        still_trending = bool(structure_ok and zone_ok and ((not require_hist_expand) or hist_ok))
                        gate_meta["exit_anchor_hold"] = bool(still_trending)
                        if isinstance(md, dict):
                            md["pretrade_risk_gate"] = gate_meta
                        if still_trending:
                            base_reason = str(decision.reason or "").strip()
                            delay_reason = (
                                "MA10_MACD_HOLD_ANCHOR "
                                f"ma10_5m={ma10_5m:.6f} last_close_5m={last_close_5m:.6f} "
                                f"zone={macd_zone} hist_expand={1 if hist_ok else 0}"
                            )
                            return (
                                FundFlowDecision(
                                    operation=FundFlowOperation.HOLD,
                                    symbol=symbol,
                                    target_portion_of_balance=0.0,
                                    leverage=decision.leverage,
                                    reason=f"{base_reason} | {delay_reason}" if base_reason else delay_reason,
                                    metadata=md if isinstance(md, dict) else {},
                                ),
                                gate_meta,
                            )

                close_ratio = self._to_float(cfg.get("exit_close_ratio"), 1.0)
                base_reason = str(decision.reason or "").strip()
                exit_reason = f"PRE_RISK_EXIT action={gate_action} score={gate_score:.3f}"
                return (
                    FundFlowDecision(
                        operation=FundFlowOperation.CLOSE,
                        symbol=symbol,
                        target_portion_of_balance=min(1.0, max(0.1, close_ratio)),
                        leverage=decision.leverage,
                        reason=f"{base_reason} | {exit_reason}" if base_reason else exit_reason,
                        metadata=md if isinstance(md, dict) else {},
                    ),
                    gate_meta,
                )
            return decision, gate_meta
        except Exception as e:
            gate_meta = {"enabled": True, "action": "ERROR", "error": str(e), "state": state}
            if isinstance(md, dict):
                md["pretrade_risk_gate"] = gate_meta
            return decision, gate_meta

    def _build_dca_decision(
        self,
        *,
        symbol: str,
        position: Dict[str, Any],
        current_price: float,
        base_decision: Any,
        trigger_context: Dict[str, Any],
        dca_cfg: Optional[Dict[str, Any]] = None,
    ) -> Optional[FundFlowDecision]:
        cfg = dca_cfg if isinstance(dca_cfg, dict) else self._dca_config()
        if not bool(cfg.get("enabled")):
            return None

        side = str(position.get("side", "")).upper()
        if side not in ("LONG", "SHORT"):
            return None
        pos_key = self._position_track_key(symbol, side)
        current_stage = int(self._dca_stage_by_pos.get(pos_key, 0) or 0)
        max_additions = int(cfg.get("max_additions", 0) or 0)
        if current_stage >= max_additions:
            return None

        thresholds = cfg.get("drawdown_thresholds") or []
        multipliers = cfg.get("multipliers") or []
        if current_stage >= len(thresholds) or current_stage >= len(multipliers):
            return None

        drawdown_ratio = self._position_drawdown_ratio(position, current_price)
        threshold = float(thresholds[current_stage])
        if drawdown_ratio < threshold:
            return None

        base_add_portion = float(cfg.get("base_add_portion", 0.2) or 0.2)
        multiplier = float(multipliers[current_stage])
        target_portion = max(0.0, base_add_portion * multiplier)
        if target_portion <= 0:
            return None

        slippage = float(getattr(self.fund_flow_decision_engine, "entry_slippage", 0.001))
        tp_pct = float(getattr(self.fund_flow_decision_engine, "take_profit_pct", 0.03))
        sl_pct = float(getattr(self.fund_flow_decision_engine, "stop_loss_pct", 0.01))
        leverage = int(self._to_float(position.get("leverage"), getattr(self.fund_flow_decision_engine, "default_leverage", 6)))
        action = FundFlowOperation.BUY if side == "LONG" else FundFlowOperation.SELL

        md = base_decision.metadata if isinstance(getattr(base_decision, "metadata", None), dict) else {}
        metadata = {
            **md,
            "trigger": trigger_context,
            "dca_triggered": True,
            "dca_stage_index": current_stage,
            "dca_stage": current_stage + 1,
            "dca_threshold": threshold,
            "dca_multiplier": multiplier,
            "dca_drawdown": drawdown_ratio,
        }
        reason = (
            f"DCA/马丁触发 stage={current_stage + 1}/{max_additions}, "
            f"drawdown={drawdown_ratio:.4f} >= threshold={threshold:.4f}, "
            f"multiplier={multiplier:.2f}"
        )

        decision = FundFlowDecision(
            operation=action,
            symbol=symbol,
            target_portion_of_balance=target_portion,
            leverage=leverage,
            reason=reason,
            metadata=metadata,
        )
        if action == FundFlowOperation.BUY:
            decision.max_price = current_price * (1.0 + slippage)
            decision.take_profit_price = current_price * (1.0 + tp_pct)
            decision.stop_loss_price = current_price * (1.0 - sl_pct)
        else:
            decision.min_price = current_price * (1.0 - slippage)
            decision.take_profit_price = current_price * (1.0 - tp_pct)
            decision.stop_loss_price = current_price * (1.0 + sl_pct)
        return decision

    def _get_daily_date_label(self) -> str:
        tz_name = self._risk_config().get("daily_reset_timezone", "Asia/Tokyo")
        try:
            tz = ZoneInfo(str(tz_name))
        except Exception:
            tz = ZoneInfo("UTC")
        return datetime.now(tz).strftime("%Y-%m-%d")

    def _load_risk_state(self) -> None:
        if not os.path.exists(self._risk_state_path):
            return
        try:
            with open(self._risk_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._consecutive_losses = int(data.get("consecutive_losses", 0) or 0)
            self._cooldown_reason = data.get("cooldown_reason")
            self._cooldown_expires = self._parse_iso_datetime(data.get("cooldown_expires"))
            self._daily_open_equity = self._to_float(data.get("daily_open_equity"), 0.0) or None
            self._daily_open_date = data.get("daily_open_date")
            self._peak_equity = self._to_float(data.get("peak_equity"), 0.0) or None
            raw_dca_state = data.get("dca_stage_by_pos", {})
            dca_state: Dict[str, int] = {}
            if isinstance(raw_dca_state, dict):
                for k, v in raw_dca_state.items():
                    try:
                        stage = int(v)
                    except Exception:
                        stage = 0
                    if isinstance(k, str) and stage >= 0:
                        dca_state[k] = stage
            self._dca_stage_by_pos = dca_state
        except Exception:
            # 状态文件损坏时忽略，避免启动失败。
            pass

    def _save_risk_state(self) -> None:
        payload = {
            "consecutive_losses": int(self._consecutive_losses or 0),
            "cooldown_reason": self._cooldown_reason,
            "cooldown_expires": self._cooldown_expires.isoformat() if isinstance(self._cooldown_expires, datetime) else None,
            "daily_open_equity": self._daily_open_equity,
            "daily_open_date": self._daily_open_date,
            "peak_equity": self._peak_equity,
            "dca_stage_by_pos": self._dca_stage_by_pos,
            "updated_at": datetime.now().isoformat(),
        }
        try:
            with open(self._risk_state_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _activate_cooldown(self, reason: str, seconds: int) -> None:
        if seconds <= 0:
            return
        now = datetime.now()
        new_expires = now + timedelta(seconds=int(seconds))
        current_expires: Optional[datetime] = self._cooldown_expires if isinstance(self._cooldown_expires, datetime) else None
        if isinstance(current_expires, datetime):
            current_now = datetime.now(current_expires.tzinfo) if current_expires.tzinfo else now
            if current_expires <= current_now:
                current_expires = None
            elif current_expires >= new_expires:
                return
        self._cooldown_reason = reason
        self._cooldown_expires = new_expires
        print(f"⚠️ 触发账户级冷却: reason={reason}, expires={self._cooldown_expires.isoformat()}")
        self._save_risk_state()

    def _cooldown_remaining_seconds(self) -> int:
        if not isinstance(self._cooldown_expires, datetime):
            return 0
        now = datetime.now(self._cooldown_expires.tzinfo) if self._cooldown_expires.tzinfo else datetime.now()
        remain = int((self._cooldown_expires - now).total_seconds())
        if remain > 0:
            return remain
        self._cooldown_expires = None
        self._cooldown_reason = None
        self._save_risk_state()
        return 0

    def _is_cooldown_active(self) -> bool:
        return self._cooldown_remaining_seconds() > 0

    def _refresh_account_risk_guard(self, account_summary: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self._risk_config()
        if not cfg["enabled"]:
            if self._cooldown_expires is not None or self._cooldown_reason is not None:
                self._cooldown_expires = None
                self._cooldown_reason = None
                self._save_risk_state()
            return {"enabled": False, "blocked": False, "reason": None, "remaining_seconds": 0}

        equity = self._to_float(account_summary.get("equity"), 0.0)
        if equity <= 0:
            return {"enabled": cfg["enabled"], "blocked": self._is_cooldown_active(), "reason": self._cooldown_reason, "remaining_seconds": self._cooldown_remaining_seconds()}

        today = self._get_daily_date_label()
        if self._daily_open_date != today or not self._daily_open_equity or self._daily_open_equity <= 0:
            self._daily_open_date = today
            self._daily_open_equity = equity
            self._peak_equity = equity
            self._consecutive_losses = 0
            self._save_risk_state()
        else:
            if not self._peak_equity or equity > self._peak_equity:
                self._peak_equity = equity
                self._save_risk_state()

        if cfg["enabled"] and self._daily_open_equity and self._daily_open_equity > 0:
            daily_loss_pct = (self._daily_open_equity - equity) / self._daily_open_equity
            if daily_loss_pct >= float(cfg["max_daily_loss_pct"]):
                self._activate_cooldown("daily_loss", int(cfg["daily_loss_cooldown_seconds"]))

        return {
            "enabled": cfg["enabled"],
            "blocked": self._is_cooldown_active(),
            "reason": self._cooldown_reason,
            "remaining_seconds": self._cooldown_remaining_seconds(),
            "daily_open_equity": self._daily_open_equity,
            "equity": equity,
        }

    @staticmethod
    def _is_order_filled(order: Any) -> bool:
        if not isinstance(order, dict):
            return False
        status = str(order.get("status", "")).upper()
        if status in ("FILLED", "PARTIALLY_FILLED"):
            return True
        try:
            return float(order.get("executedQty", 0) or 0) > 0
        except Exception:
            return False

    def _extract_close_realized_pnl(self, symbol: str, execution_result: Dict[str, Any]) -> Optional[float]:
        if not isinstance(execution_result, dict):
            return None

        candidates: List[Any] = [
            execution_result.get("realized_pnl"),
            execution_result.get("realizedPnl"),
        ]
        order_info = execution_result.get("order")
        if isinstance(order_info, dict):
            candidates.extend(
                [
                    order_info.get("realizedPnl"),
                    order_info.get("realizedProfit"),
                ]
            )

        for value in candidates:
            if value is None or value == "":
                continue
            try:
                return float(value)
            except Exception:
                continue

        order_id = self._to_int((order_info or {}).get("orderId"), -1) if isinstance(order_info, dict) else -1
        if order_id <= 0:
            return None
        fills = self._fetch_order_trade_fills(symbol=symbol, order_id=order_id)
        if not fills:
            return None

        realized_total = 0.0
        realized_found = False
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            realized_raw = fill.get("realizedPnl")
            if realized_raw is None or realized_raw == "":
                continue
            realized_total += self._to_float(realized_raw, 0.0)
            realized_found = True
        if realized_found:
            return realized_total
        return None

    def _update_loss_streak_after_close(self, symbol: str, execution_result: Dict[str, Any]) -> None:
        order_info = execution_result.get("order") if isinstance(execution_result, dict) else None
        if not self._is_order_filled(order_info):
            return

        realized_pnl = self._extract_close_realized_pnl(symbol=symbol, execution_result=execution_result)
        if realized_pnl is None:
            print(f"ℹ️ {symbol} 平仓已成交，但未获取到已实现盈亏，跳过连续亏损计数")
            return
        execution_result["realized_pnl"] = realized_pnl

        if realized_pnl < 0:
            self._consecutive_losses = int(self._consecutive_losses or 0) + 1
        else:
            self._consecutive_losses = 0

        cfg = self._risk_config()
        if int(self._consecutive_losses) >= int(cfg["max_consecutive_losses"]):
            self._activate_cooldown(
                "consecutive_losses",
                int(cfg["consecutive_loss_cooldown_seconds"]),
            )
        else:
            self._save_risk_state()

    def _init_fund_flow_modules(self) -> None:
        symbol_whitelist = ConfigLoader.get_trading_symbols(self.config)
        ff_cfg = self.config.get("fund_flow", {}) or {}
        self._signal_pool_configs = self._build_signal_pool_configs_from_config(ff_cfg)
        self._signal_pool_configs_runtime_cache = {}
        self.fund_flow_attribution_engine = FundFlowAttributionEngine(
            self.logs_dir,
            bucket_root_dir=self.log_root_dir,
        )
        self.fund_flow_risk_engine = FundFlowRiskEngine(self.config, symbol_whitelist=symbol_whitelist)
        self.fund_flow_decision_engine = FundFlowDecisionEngine(self.config)
        self.fund_flow_execution_router = FundFlowExecutionRouter(
            client=self.client,
            risk_engine=self.fund_flow_risk_engine,
            attribution_engine=self.fund_flow_attribution_engine,
        )
        metric_timeframes = ff_cfg.get("metric_timeframes")
        if not isinstance(metric_timeframes, list):
            metric_timeframes = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"]
        self.fund_flow_ingestion_service = MarketIngestionService(
            window_seconds=int(ff_cfg.get("aggregation_window_seconds", 15) or 15),
            exchange="binance",
            timeframes=metric_timeframes,
            max_history_seconds=int(ff_cfg.get("max_indicator_history_seconds", 4 * 3600) or 4 * 3600),
            range_quantile_config=ff_cfg.get("range_quantile", {}) if isinstance(ff_cfg.get("range_quantile", {}), dict) else {},
        )
        self.fund_flow_storage = None
        sync_result: Dict[str, int] = {"definitions": 0, "pools": 0}
        runtime_pool_cfg = ff_cfg.get("signal_pool", {}) if isinstance(ff_cfg.get("signal_pool"), dict) else {}
        try:
            storage = MarketStorage(db_path=os.path.join(self.logs_dir, "fund_flow_strategy.db"))
            self.fund_flow_storage = storage
            sync_result = storage.upsert_signal_registry_from_config(ff_cfg)
            active_pool_id = ff_cfg.get("active_signal_pool_id")
            runtime_pool_cfg_db = storage.get_active_signal_pool_config(
                active_pool_id=str(active_pool_id) if active_pool_id else None
            )
            if runtime_pool_cfg_db:
                runtime_pool_cfg = runtime_pool_cfg_db
            self._signal_registry_version = storage.get_signal_registry_version()
        except Exception as e:
            self.fund_flow_storage = None
            self._signal_registry_version = ""
            print(f"⚠️ MarketStorage 初始化失败，已降级无DB模式: {e}")

        self.fund_flow_trigger_engine = TriggerEngine(
            dedupe_window_seconds=int(ff_cfg.get("trigger_dedupe_seconds", 10) or 10),
            signal_pool_config=runtime_pool_cfg,
        )
        runtime_pool_id = str(runtime_pool_cfg.get("pool_id") or runtime_pool_cfg.get("id") or "").strip()
        if runtime_pool_id:
            self._signal_pool_configs[runtime_pool_id] = runtime_pool_cfg
        if int(sync_result.get("definitions", 0)) > 0 or int(sync_result.get("pools", 0)) > 0:
            print(
                f"🗂️ signal registry入库完成: definitions={int(sync_result.get('definitions', 0))}, "
                f"pools={int(sync_result.get('pools', 0))}, version={self._signal_registry_version}"
            )

    def _build_signal_pool_configs_from_config(self, ff_cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        definitions_raw = ff_cfg.get("signal_definitions")
        definitions = definitions_raw if isinstance(definitions_raw, list) else []
        defs_by_id: Dict[str, Dict[str, Any]] = {}
        for item in definitions:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "").strip()
            if not sid:
                continue
            defs_by_id[sid] = item

        pools_raw = ff_cfg.get("signal_pools")
        pools = pools_raw if isinstance(pools_raw, list) else []
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            pool_id = str(pool.get("id") or pool.get("pool_id") or "").strip()
            if not pool_id:
                continue
            signal_ids_raw = pool.get("signal_ids")
            signal_ids = signal_ids_raw if isinstance(signal_ids_raw, list) else []
            rules: List[Dict[str, Any]] = []
            if signal_ids:
                for sid_any in signal_ids:
                    sid = str(sid_any).strip()
                    if not sid:
                        continue
                    d = defs_by_id.get(sid)
                    if not isinstance(d, dict):
                        continue
                    if not bool(d.get("enabled", True)):
                        continue
                    rule: Dict[str, Any] = {
                        "id": sid,
                        "name": str(d.get("signal_name") or d.get("name") or sid),
                        "side": str(d.get("side") or "BOTH").upper(),
                        "metric": str(d.get("metric") or ""),
                        "operator": str(d.get("operator") or ">="),
                        "threshold": self._to_float(d.get("threshold"), 0.0),
                        "enabled": bool(d.get("enabled", True)),
                    }
                    tf = str(d.get("timeframe") or "").strip().lower()
                    if tf:
                        rule["timeframe"] = tf
                    if d.get("threshold_max") is not None:
                        rule["threshold_max"] = self._to_float(d.get("threshold_max"), 0.0)
                    rules.append(rule)
            else:
                rules_raw = pool.get("rules")
                if isinstance(rules_raw, list):
                    for idx, r in enumerate(rules_raw, start=1):
                        if not isinstance(r, dict):
                            continue
                        rule = dict(r)
                        if "name" not in rule:
                            rule["name"] = f"rule_{idx}"
                        rules.append(rule)
            out[pool_id] = {
                "enabled": bool(pool.get("enabled", True)),
                "pool_id": pool_id,
                "pool_name": str(pool.get("pool_name") or pool.get("name") or pool_id),
                "logic": str(pool.get("logic", "AND")).upper(),
                "min_pass_count": int(self._to_float(pool.get("min_pass_count"), 0)),
                "min_long_score": self._to_float(pool.get("min_long_score"), 0.0),
                "min_short_score": self._to_float(pool.get("min_short_score"), 0.0),
                "scheduled_trigger_bypass": bool(pool.get("scheduled_trigger_bypass", True)),
                "apply_when_position_exists": bool(pool.get("apply_when_position_exists", False)),
                "edge_trigger_enabled": bool(pool.get("edge_trigger_enabled", True)),
                "edge_cooldown_seconds": int(self._to_float(pool.get("edge_cooldown_seconds"), 0)),
                "symbols": pool.get("symbols") if isinstance(pool.get("symbols"), list) else [],
                "rules": rules,
            }

        legacy_pool = ff_cfg.get("signal_pool")
        if isinstance(legacy_pool, dict):
            legacy_id = str(legacy_pool.get("pool_id") or legacy_pool.get("id") or "default_pool")
            legacy_cfg = dict(legacy_pool)
            legacy_cfg["pool_id"] = legacy_id
            if "pool_name" not in legacy_cfg:
                legacy_cfg["pool_name"] = legacy_id
            out.setdefault(legacy_id, legacy_cfg)
        return out

    def _resolve_runtime_signal_pool_config(self, pool_id: Optional[str]) -> Dict[str, Any]:
        pool_key = str(pool_id or "").strip()
        if not pool_key:
            default_cfg = getattr(self.fund_flow_trigger_engine, "signal_pool_config", None)
            return default_cfg if isinstance(default_cfg, dict) else {}
        if pool_key in self._signal_pool_configs_runtime_cache:
            return self._signal_pool_configs_runtime_cache[pool_key]

        runtime_cfg: Dict[str, Any] = {}
        if self.fund_flow_storage is not None and pool_key.upper() != "AUTO":
            try:
                cfg_db = self.fund_flow_storage.get_active_signal_pool_config(active_pool_id=pool_key)
                if isinstance(cfg_db, dict) and cfg_db:
                    runtime_cfg = cfg_db
            except Exception:
                runtime_cfg = {}

        if not runtime_cfg:
            cfg_local = self._signal_pool_configs.get(pool_key)
            if isinstance(cfg_local, dict):
                runtime_cfg = cfg_local

        if not runtime_cfg:
            default_cfg = getattr(self.fund_flow_trigger_engine, "signal_pool_config", None)
            runtime_cfg = default_cfg if isinstance(default_cfg, dict) else {}

        self._signal_pool_configs_runtime_cache[pool_key] = runtime_cfg
        return runtime_cfg

    def _safe_storage_call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        storage = self.fund_flow_storage
        if storage is None:
            return None
        method = getattr(storage, method_name, None)
        if not callable(method):
            return None
        try:
            return method(*args, **kwargs)
        except Exception as e:
            self.fund_flow_storage = None
            self._signal_registry_version = ""
            print(f"⚠️ storage.{method_name} 失败，已降级无DB模式: {e}")
            return None

    def _refresh_signal_pool_runtime_if_changed(self) -> None:
        if self.fund_flow_storage is None:
            return
        try:
            latest = self.fund_flow_storage.get_signal_registry_version()
        except Exception:
            return
        if not latest or latest == self._signal_registry_version:
            return
        ff_cfg = self.config.get("fund_flow", {}) or {}
        active_pool_id = ff_cfg.get("active_signal_pool_id")
        runtime_pool_cfg = self.fund_flow_storage.get_active_signal_pool_config(
            active_pool_id=str(active_pool_id) if active_pool_id else None
        )
        if not runtime_pool_cfg:
            runtime_pool_cfg = ff_cfg.get("signal_pool", {}) if isinstance(ff_cfg.get("signal_pool"), dict) else {}
        self.fund_flow_trigger_engine.set_signal_pool_config(runtime_pool_cfg)
        self._signal_pool_configs_runtime_cache = {}
        pool_id = str(runtime_pool_cfg.get("pool_id") or runtime_pool_cfg.get("id") or "").strip()
        if pool_id:
            self._signal_pool_configs[pool_id] = runtime_pool_cfg
        self._signal_registry_version = latest
        print(
            "♻️ signal_pool热更新生效: "
            f"pool={runtime_pool_cfg.get('pool_id') or runtime_pool_cfg.get('pool_name') or 'default'}, "
            f"version={latest}"
        )

    def _startup_market_preload_config(self) -> Dict[str, Any]:
        startup_cfg = self.config.get("startup", {}) if isinstance(self.config.get("startup"), dict) else {}
        enabled = self._to_bool(startup_cfg.get("preload_market_data_enabled"), True)
        lookback_minutes = int(self._to_float(startup_cfg.get("preload_market_lookback_minutes"), 120))
        lookback_minutes = max(60, min(120, lookback_minutes))
        kline_interval = str(startup_cfg.get("preload_market_kline_interval", "5m") or "5m").strip().lower()
        if kline_interval not in ("1m", "3m", "5m", "15m"):
            kline_interval = "5m"
        oi_period = str(startup_cfg.get("preload_open_interest_period", kline_interval) or kline_interval).strip().lower()
        if oi_period not in ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"):
            oi_period = "5m"
        regime_cfg = self.config.get("fund_flow", {}).get("regime", {}) if isinstance(self.config.get("fund_flow", {}).get("regime"), dict) else {}
        regime_timeframe = str(regime_cfg.get("timeframe", "15m") or "15m").strip().lower()
        if regime_timeframe not in ("15m", "30m", "1h", "2h", "4h"):
            regime_timeframe = "15m"
        trend_limit = int(self._to_float(startup_cfg.get("preload_trend_kline_limit"), 120))
        trend_limit = max(60, min(240, trend_limit))
        request_sleep_ms = int(self._to_float(startup_cfg.get("preload_request_sleep_ms"), 0))
        request_sleep_ms = max(0, min(1000, request_sleep_ms))
        return {
            "enabled": enabled,
            "lookback_minutes": lookback_minutes,
            "kline_interval": kline_interval,
            "oi_period": oi_period,
            "regime_timeframe": regime_timeframe,
            "trend_limit": trend_limit,
            "request_sleep_ms": request_sleep_ms,
        }

    @staticmethod
    def _interval_minutes(interval: str) -> int:
        mapping = {
            "1m": 1,
            "3m": 3,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1h": 60,
            "2h": 120,
            "4h": 240,
        }
        return int(mapping.get(str(interval or "").strip().lower(), 5))

    @staticmethod
    def _coerce_utc_datetime(value: Any) -> Optional[datetime]:
        try:
            if isinstance(value, datetime):
                return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if isinstance(value, (int, float)):
                ts = float(value)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
        return None

    def _build_open_interest_history_map(
        self,
        symbol: str,
        period: str,
        limit: int,
    ) -> Dict[int, float]:
        out: Dict[int, float] = {}
        try:
            rows = self.client.get_open_interest_hist(symbol, period=period, limit=limit) or []
        except Exception as e:
            print(f"⚠️ {symbol} OI历史预载失败: {e}")
            return out
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = self._coerce_utc_datetime(row.get("timestamp"))
            if ts is None:
                continue
            oi_val = self._to_float(
                row.get("sumOpenInterest"),
                self._to_float(row.get("openInterest"), self._to_float(row.get("sumOpenInterestValue"), 0.0)),
            )
            if oi_val <= 0:
                continue
            out[int(ts.timestamp())] = oi_val
        return out

    @staticmethod
    def _resolve_oi_value_for_timestamp(
        timestamp_s: int,
        ordered_ts: List[int],
        oi_map: Dict[int, float],
    ) -> float:
        if not ordered_ts:
            return 0.0
        candidate = 0
        for ts in ordered_ts:
            if ts > timestamp_s:
                break
            candidate = ts
        if candidate <= 0:
            candidate = ordered_ts[0]
        try:
            return float(oi_map.get(candidate, 0.0))
        except Exception:
            return 0.0

    def _preload_market_history_on_startup(self) -> None:
        cfg = self._startup_market_preload_config()
        if not cfg.get("enabled"):
            return
        if getattr(self, "fund_flow_ingestion_service", None) is None:
            return

        symbols = ConfigLoader.get_trading_symbols(self.config)
        if not symbols:
            return

        lookback_minutes = int(cfg["lookback_minutes"])
        kline_interval = str(cfg["kline_interval"])
        oi_period = str(cfg["oi_period"])
        regime_timeframe = str(cfg["regime_timeframe"])
        trend_limit = int(cfg["trend_limit"])
        request_sleep_ms = int(cfg["request_sleep_ms"])
        interval_minutes = max(1, self._interval_minutes(kline_interval))
        kline_limit = max(16, int(math.ceil(lookback_minutes / interval_minutes)) + 3)
        oi_limit = max(16, min(kline_limit + 2, 500))

        print(
            "📥 启动预载市场数据: "
            f"symbols={len(symbols)}, lookback={lookback_minutes}m, "
            f"interval={kline_interval}, oi_period={oi_period}"
        )

        ok_symbols = 0
        total_snapshots = 0
        for symbol in symbols:
            try:
                klines = self.client.get_klines(symbol, kline_interval, limit=kline_limit) or []
                if not isinstance(klines, list) or len(klines) < 2:
                    print(f"⚠️ {symbol} 预载跳过: {kline_interval} K线不足")
                    continue

                oi_map = self._build_open_interest_history_map(symbol, oi_period, oi_limit)
                oi_ts_list = sorted(oi_map.keys())
                funding_rate = self._to_float(self.client.get_funding_rate(symbol), 0.0)
                trend_filter = self.market_data.get_trend_filter_metrics(symbol, interval=regime_timeframe, limit=trend_limit) or {}
                if isinstance(trend_filter, dict) and trend_filter:
                    self._startup_trend_filter_cache[symbol.upper()] = {
                        key: self._to_float(trend_filter.get(key), 0.0)
                        for key in ("ema_fast", "ema_slow", "adx", "atr_pct", "last_open", "last_close")
                        if trend_filter.get(key) is not None
                    }

                prev_close = self._to_float(klines[0][4], 0.0)
                prev_ret = 0.0
                oi_prev = 0.0
                snapshots_for_symbol = 0

                for row in klines[1:]:
                    if not isinstance(row, list) or len(row) < 7:
                        continue
                    close_price = self._to_float(row[4], 0.0)
                    if close_price <= 0 or prev_close <= 0:
                        prev_close = close_price if close_price > 0 else prev_close
                        continue
                    ts = self._coerce_utc_datetime(row[6])
                    if ts is None:
                        continue
                    ret_period = (close_price - prev_close) / prev_close if prev_close > 0 else 0.0
                    oi_now = self._resolve_oi_value_for_timestamp(int(ts.timestamp()), oi_ts_list, oi_map)
                    oi_delta_ratio = ((oi_now - oi_prev) / abs(oi_prev)) if oi_prev > 0 and oi_now > 0 else 0.0
                    if oi_now > 0:
                        oi_prev = oi_now

                    metrics = {
                        "cvd_ratio": ret_period,
                        "cvd_momentum": ret_period - prev_ret,
                        "oi_delta_ratio": oi_delta_ratio,
                        "funding_rate": funding_rate,
                        "depth_ratio": 1.0,
                        "imbalance": 0.0,
                        "liquidity_delta_norm": 0.0,
                        "mid_price": close_price,
                        "microprice": close_price,
                        "micro_delta_norm": 0.0,
                        "spread_bps": 0.0,
                        "phantom": 0.0,
                        "trap_score": 0.0,
                        "ret_period": ret_period,
                    }
                    self.fund_flow_ingestion_service.aggregate_from_metrics(symbol=symbol, metrics=metrics, ts=ts)
                    prev_close = close_price
                    prev_ret = ret_period
                    snapshots_for_symbol += 1

                current_oi = self._to_float(self.client.get_open_interest(symbol), 0.0)
                if current_oi > 0:
                    self._prev_open_interest[symbol] = current_oi
                elif oi_prev > 0:
                    self._prev_open_interest[symbol] = oi_prev

                if snapshots_for_symbol > 0:
                    ok_symbols += 1
                    total_snapshots += snapshots_for_symbol
                    print(
                        f"   ✅ {symbol}: preload={snapshots_for_symbol} bars, "
                        f"trend={'yes' if symbol.upper() in self._startup_trend_filter_cache else 'no'}, "
                        f"oi_hist={'yes' if oi_map else 'no'}"
                    )
                else:
                    print(f"   ⚠️ {symbol}: 未生成有效预载样本")
            except Exception as e:
                print(f"⚠️ {symbol} 市场预载失败: {e}")
            if request_sleep_ms > 0:
                time.sleep(request_sleep_ms / 1000.0)

        print(
            "✅ 启动预载完成: "
            f"ok_symbols={ok_symbols}/{len(symbols)}, snapshots={total_snapshots}, "
            f"trend_cache={len(self._startup_trend_filter_cache)}"
        )

    def _apply_timeframe_context(
        self,
        raw_context: Dict[str, Any],
        flow_snapshot: Any,
    ) -> Dict[str, Any]:
        out = dict(raw_context or {})
        timeframes = {}
        if hasattr(flow_snapshot, "timeframes") and isinstance(getattr(flow_snapshot, "timeframes"), dict):
            timeframes = dict(getattr(flow_snapshot, "timeframes"))
        out["timeframes"] = timeframes

        # 将 trend_filter 数据注入到对应时间框架的 timeframes 中
        trend_filter = out.pop("trend_filter", None)
        trend_filter_timeframe = out.pop("trend_filter_timeframe", None)
        if isinstance(trend_filter, dict) and trend_filter and isinstance(trend_filter_timeframe, str):
            tf_key = trend_filter_timeframe.strip().lower()
            if tf_key not in timeframes:
                timeframes[tf_key] = {}
            if isinstance(timeframes[tf_key], dict):
                # 注入 trend filter 指标 (ema_fast, ema_slow, adx, atr_pct, last_open, last_close)
                for k in ("ema_fast", "ema_slow", "adx", "atr_pct", "last_open", "last_close"):
                    if k in trend_filter:
                        timeframes[tf_key][k] = trend_filter[k]
            out["timeframes"] = timeframes

        ff_cfg = self.config.get("fund_flow", {}) or {}
        tf = str(ff_cfg.get("decision_timeframe") or ff_cfg.get("signal_timeframe") or "").strip().lower()
        if tf and isinstance(timeframes.get(tf), dict):
            tf_ctx = timeframes[tf]
            for key in (
                "cvd_ratio",
                "cvd_momentum",
                "oi_delta_ratio",
                "funding_rate",
                "depth_ratio",
                "imbalance",
                "liquidity_delta_norm",
                "micro_delta_mean",
                "micro_delta_last",
                "phantom_mean",
                "phantom_max",
                "trap_mean",
                "trap_last",
                "spread_bps_mean",
                "spread_bps_last",
                "signal_strength",
            ):
                if key in tf_ctx:
                    out[key] = self._to_float(tf_ctx.get(key), self._to_float(out.get(key), 0.0))
            out["active_timeframe"] = tf
        else:
            out["active_timeframe"] = "raw"
        return out

    def _extract_orderbook_flow(self, symbol: str) -> Dict[str, float]:
        try:
            ob = self.client.get_order_book(symbol, limit=20) or {}
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            bid_notional = sum(self._to_float(x[0]) * self._to_float(x[1]) for x in bids[:20] if isinstance(x, list))
            ask_notional = sum(self._to_float(x[0]) * self._to_float(x[1]) for x in asks[:20] if isinstance(x, list))
            total = bid_notional + ask_notional
            best_bid = self._to_float(bids[0][0], 0.0) if bids and isinstance(bids[0], list) and len(bids[0]) >= 2 else 0.0
            best_ask = self._to_float(asks[0][0], 0.0) if asks and isinstance(asks[0], list) and len(asks[0]) >= 2 else 0.0
            best_bid_qty = self._to_float(bids[0][1], 0.0) if bids and isinstance(bids[0], list) and len(bids[0]) >= 2 else 0.0
            best_ask_qty = self._to_float(asks[0][1], 0.0) if asks and isinstance(asks[0], list) and len(asks[0]) >= 2 else 0.0
            mid_price = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
            spread_bps = ((best_ask - best_bid) / mid_price) if mid_price > 0 else 0.0
            microprice = (
                (best_ask * best_bid_qty + best_bid * best_ask_qty) / (best_bid_qty + best_ask_qty)
                if (best_bid_qty + best_ask_qty) > 0
                else mid_price
            )
            micro_delta_norm = ((microprice - mid_price) / mid_price) if mid_price > 0 else 0.0
            if total <= 0:
                return {
                    "depth_ratio": 1.0,
                    "imbalance": 0.0,
                    "ob_delta_notional": 0.0,
                    "ob_total_notional": 0.0,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "best_bid_qty": best_bid_qty,
                    "best_ask_qty": best_ask_qty,
                    "mid_price": mid_price,
                    "microprice": microprice,
                    "micro_delta_norm": micro_delta_norm,
                    "spread_bps": spread_bps,
                    "phantom": 0.0,
                    "trap_score": 0.0,
                }
            depth_ratio = (bid_notional / ask_notional) if ask_notional > 0 else 1.0
            imbalance = ((bid_notional - ask_notional) / total) if total > 0 else 0.0
            delta_notional = bid_notional - ask_notional
            prev_imb = self._to_float(self._prev_imbalance_for_phantom.get(symbol), imbalance)
            ff_cfg = self.config.get("fund_flow", {}) or {}
            micro_cfg = ff_cfg.get("microstructure", {}) if isinstance(ff_cfg.get("microstructure"), dict) else {}
            phantom_spread_k = max(0.0, self._to_float(micro_cfg.get("phantom_spread_k"), 100.0))
            phantom_raw = abs(imbalance) * max(0.0, abs(imbalance) - abs(prev_imb))
            phantom = phantom_raw * (1.0 + max(0.0, spread_bps) * phantom_spread_k)
            hist = self._get_micro_feature_history(symbol)
            z_imb = self._robust_zscore(imbalance, hist["imbalance"])
            z_spread = self._robust_zscore(spread_bps, hist["spread_bps"])
            z_phantom = self._robust_zscore(phantom, hist["phantom"])
            z_micro = self._robust_zscore(micro_delta_norm, hist["micro_delta_norm"])
            sign_consistency = 1.0 if (imbalance * micro_delta_norm) > 0 else 0.0
            trap_raw = (
                0.50 * max(z_phantom, 0.0)
                + 0.20 * max(z_spread, 0.0)
                + 0.20 * abs(z_imb)
                + 0.10 * abs(z_micro)
                - 0.25 * sign_consistency
            )
            trap_raw = max(-20.0, min(20.0, trap_raw))
            trap_score = 1.0 / (1.0 + math.exp(-trap_raw))
            hist["imbalance"].append(float(imbalance))
            hist["spread_bps"].append(float(spread_bps))
            hist["phantom"].append(float(phantom))
            hist["micro_delta_norm"].append(float(micro_delta_norm))
            self._prev_imbalance_for_phantom[symbol] = float(imbalance)
            return {
                "depth_ratio": depth_ratio,
                "imbalance": imbalance,
                "ob_delta_notional": delta_notional,
                "ob_total_notional": total,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "best_bid_qty": best_bid_qty,
                "best_ask_qty": best_ask_qty,
                "mid_price": mid_price,
                "microprice": microprice,
                "micro_delta_norm": micro_delta_norm,
                "spread_bps": spread_bps,
                "phantom": phantom,
                "trap_score": trap_score,
            }
        except Exception:
            return {
                "depth_ratio": 1.0,
                "imbalance": 0.0,
                "ob_delta_notional": 0.0,
                "ob_total_notional": 0.0,
                "best_bid": 0.0,
                "best_ask": 0.0,
                "best_bid_qty": 0.0,
                "best_ask_qty": 0.0,
                "mid_price": 0.0,
                "microprice": 0.0,
                "micro_delta_norm": 0.0,
                "spread_bps": 0.0,
                "phantom": 0.0,
                "trap_score": 0.0,
            }

    def _compute_liquidity_delta_norm(self, symbol: str, delta_notional: float, total_notional: float) -> float:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        alpha = self._to_float(ff_cfg.get("liquidity_norm_alpha"), 0.2)
        if alpha <= 0 or alpha > 1:
            alpha = 0.2
        clip = abs(self._to_float(ff_cfg.get("liquidity_norm_clip"), 1.0))
        if clip <= 0:
            clip = 1.0
        min_base = max(1e-6, self._to_float(ff_cfg.get("liquidity_norm_min_base"), 1000.0))

        base_sample = abs(total_notional) if abs(total_notional) > 0 else abs(delta_notional)
        prev_ema = self._to_float(self._liquidity_ema_notional.get(symbol), 0.0)
        ema = base_sample if prev_ema <= 0 else (prev_ema * (1.0 - alpha) + base_sample * alpha)
        self._liquidity_ema_notional[symbol] = ema

        denom = max(min_base, ema)
        norm = delta_notional / denom if denom > 0 else 0.0
        if norm > clip:
            return clip
        if norm < -clip:
            return -clip
        return norm

    def get_market_data_for_symbol(self, symbol: str) -> Dict[str, Any]:
        realtime = self.market_data.get_realtime_market_data(symbol) or {}
        # 从配置获取 regime timeframe，动态获取对应的 trend filter 数据
        ff_cfg = self.config.get("fund_flow", {}) or {}
        regime_cfg = ff_cfg.get("regime", {}) if isinstance(ff_cfg.get("regime"), dict) else {}
        regime_timeframe = str(regime_cfg.get("timeframe", "15m") or "15m").strip().lower()
        trend_filter = self.market_data.get_trend_filter_metrics(symbol, interval=regime_timeframe, limit=120) or {}
        if not trend_filter:
            trend_filter = dict(self._startup_trend_filter_cache.get(symbol.upper(), {}))
        ob_flow = self._extract_orderbook_flow(symbol)
        for k, v in ob_flow.items():
            realtime[k] = v
        return {"realtime": realtime, "trend_filter": trend_filter, "trend_filter_timeframe": regime_timeframe}

    def _build_fund_flow_context(self, symbol: str, market_data: Dict[str, Any]) -> Dict[str, Any]:
        realtime = market_data.get("realtime", {}) if isinstance(market_data, dict) else {}
        # 使用动态的 trend_filter 数据
        trend_filter = market_data.get("trend_filter", {}) if isinstance(market_data, dict) else {}
        trend_filter_timeframe = market_data.get("trend_filter_timeframe", "15m") if isinstance(market_data, dict) else "15m"
        change_15m = self._to_float(realtime.get("change_15m"), 0.0) / 100.0
        change_24h = self._to_float(realtime.get("change_24h"), 0.0) / 100.0
        funding_rate = self._to_float(realtime.get("funding_rate"), 0.0)
        open_interest = self._to_float(realtime.get("open_interest"), 0.0)

        prev_oi = self._prev_open_interest.get(symbol, 0.0)
        oi_delta_ratio = ((open_interest - prev_oi) / abs(prev_oi)) if prev_oi > 0 else 0.0
        self._prev_open_interest[symbol] = open_interest

        cvd_ratio = change_15m
        cvd_momentum = change_15m - (change_24h / 96.0)
        ob_delta_notional = self._to_float(realtime.get("ob_delta_notional"), 0.0)
        ob_total_notional = self._to_float(realtime.get("ob_total_notional"), 0.0)
        liquidity_delta_norm = self._compute_liquidity_delta_norm(symbol, ob_delta_notional, ob_total_notional)
        return {
            "cvd_ratio": cvd_ratio,
            "cvd_momentum": cvd_momentum,
            "oi_delta_ratio": oi_delta_ratio,
            "funding_rate": funding_rate,
            "depth_ratio": self._to_float(realtime.get("depth_ratio"), 1.0),
            "imbalance": self._to_float(realtime.get("imbalance"), 0.0),
            "liquidity_delta_norm": liquidity_delta_norm,
            "mid_price": self._to_float(realtime.get("mid_price"), 0.0),
            "microprice": self._to_float(realtime.get("microprice"), 0.0),
            "micro_delta_norm": self._to_float(realtime.get("micro_delta_norm"), 0.0),
            "spread_bps": self._to_float(realtime.get("spread_bps"), 0.0),
            "phantom": self._to_float(realtime.get("phantom"), 0.0),
            "trap_score": self._to_float(realtime.get("trap_score"), 0.0),
            "ob_delta_notional": ob_delta_notional,
            "ob_total_notional": ob_total_notional,
            "trend_filter": trend_filter if isinstance(trend_filter, dict) else {},
            "trend_filter_timeframe": trend_filter_timeframe,
        }

    def _has_pending_entry_order(self, symbol: str) -> bool:
        """Return True when there is an unfilled opening order for symbol."""
        try:
            orders = self.client.get_open_orders(symbol) or []
        except Exception:
            return False
        if not isinstance(orders, list):
            return False
        for order in orders:
            if not isinstance(order, dict):
                continue
            is_reduce = bool(order.get("reduceOnly", False))
            is_close = bool(order.get("closePosition", False))
            order_type = str(order.get("type", "")).upper()
            strategy_type = str(order.get("strategyType", "")).upper()
            if "TAKE_PROFIT" in order_type or "STOP" in order_type:
                continue
            if "TAKE_PROFIT" in strategy_type or "STOP" in strategy_type:
                continue
            if is_reduce or is_close:
                continue
            status = str(order.get("status", "")).upper()
            if status in ("NEW", "PARTIALLY_FILLED", ""):
                return True
        return False

    def _has_pending_close_order(self, symbol: str) -> bool:
        """Return True when there is an unfilled reduce-only close order for symbol."""
        try:
            orders = self.client.get_open_orders(symbol) or []
        except Exception:
            return False
        if not isinstance(orders, list):
            return False
        for order in orders:
            if not isinstance(order, dict):
                continue
            is_reduce = bool(order.get("reduceOnly", False))
            is_close = bool(order.get("closePosition", False))
            if not (is_reduce or is_close):
                continue
            order_type = str(order.get("type", "")).upper()
            strategy_type = str(order.get("strategyType", "")).upper()
            if "TAKE_PROFIT" in order_type or "STOP" in order_type:
                continue
            if "TAKE_PROFIT" in strategy_type or "STOP" in strategy_type:
                continue
            status = str(order.get("status", "")).upper()
            if status in ("NEW", "PARTIALLY_FILLED", ""):
                return True
        return False

    @staticmethod
    def _position_track_key(symbol: str, side: str) -> str:
        return f"{str(symbol).upper()}:{str(side).upper()}"

    def _update_position_extrema(self, symbol: str, position: Dict[str, Any], current_price: float) -> None:
        side = str(position.get("side", "")).upper()
        if side not in ("LONG", "SHORT") or current_price <= 0:
            return
        entry_price = self._to_float(position.get("entry_price"), 0.0)
        if entry_price <= 0:
            return
        if side == "LONG":
            pnl_ratio = (current_price - entry_price) / entry_price
        else:
            pnl_ratio = (entry_price - current_price) / entry_price
        key = self._position_track_key(symbol, side)
        rec_raw = self._position_extrema_by_pos.get(key)
        rec: Dict[str, float] = rec_raw if isinstance(rec_raw, dict) else {}
        if not rec:
            self._position_extrema_by_pos[key] = {
                "max_favorable_ratio": float(pnl_ratio),
                "max_adverse_ratio": float(pnl_ratio),
                "last_ratio": float(pnl_ratio),
                "updated_ts": float(time.time()),
            }
            return
        rec["max_favorable_ratio"] = max(float(rec.get("max_favorable_ratio", pnl_ratio)), float(pnl_ratio))
        rec["max_adverse_ratio"] = min(float(rec.get("max_adverse_ratio", pnl_ratio)), float(pnl_ratio))
        rec["last_ratio"] = float(pnl_ratio)
        rec["updated_ts"] = float(time.time())
        self._position_extrema_by_pos[key] = rec

    def _clear_dca_tracking_for_symbol(self, symbol: str, keep_key: Optional[str] = None) -> None:
        prefix = f"{str(symbol).upper()}:"
        keys = [k for k in list(self._dca_stage_by_pos.keys()) if k.startswith(prefix) and (keep_key is None or k != keep_key)]
        if not keys:
            return
        for key in keys:
            self._dca_stage_by_pos.pop(key, None)
        self._save_risk_state()

    def _clear_sla_tracking_for_symbol(self, symbol: str, keep_key: Optional[str] = None) -> None:
        prefix = f"{str(symbol).upper()}:"
        for store in (
            self._position_first_seen_ts,
            self._position_last_direction_eval_ts,
            self._position_extrema_by_pos,
            self._protection_missing_since_ts,
            self._protection_last_alert_ts,
            self._pre_risk_exit_streak_by_pos,
        ):
            keys = [k for k in list(store.keys()) if k.startswith(prefix) and (keep_key is None or k != keep_key)]
            for key in keys:
                store.pop(key, None)

    def _emit_protection_sla_alert(self, symbol: str, side: str, detail: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "ts": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "detail": detail,
            "extra": extra or {},
        }
        msg = f"🚨 保护单SLA告警 | symbol={symbol} side={side} | {detail}"
        print(msg)
        try:
            with open(self._protection_alert_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _open_protection_orders(self, symbol: str, side: Optional[str] = None) -> List[Dict[str, Any]]:
        orders: List[Dict[str, Any]] = []
        try:
            raw_open = self.client.get_open_orders(symbol) or []
            if isinstance(raw_open, list):
                orders.extend([x for x in raw_open if isinstance(x, dict)])
        except Exception:
            pass
        try:
            raw_cond = self.client.get_open_conditional_orders(symbol) or []
            if isinstance(raw_cond, list):
                orders.extend([x for x in raw_cond if isinstance(x, dict)])
        except Exception:
            pass

        side_norm = str(side or "").upper()
        if side_norm not in ("LONG", "SHORT"):
            side_norm = ""
        hedge_mode = False
        try:
            hedge_mode = bool(self.client.broker.get_hedge_mode())
        except Exception:
            hedge_mode = False

        filtered: List[Dict[str, Any]] = []
        for order in orders:
            order_type = str(order.get("type") or order.get("strategyType") or "").upper()
            if "TAKE_PROFIT" not in order_type and "STOP" not in order_type:
                continue
            status = str(order.get("status") or order.get("strategyStatus") or "").upper()
            if status in ("CANCELED", "CANCELLED", "EXPIRED", "FILLED"):
                continue
            if side_norm:
                order_side = str(order.get("positionSide") or "").upper()
                expected_close_side = "SELL" if side_norm == "LONG" else "BUY"
                order_close_side = str(order.get("side") or order.get("orderSide") or "").upper()
                if order_close_side in ("BUY", "SELL") and order_close_side != expected_close_side:
                    continue
                if hedge_mode:
                    if order_side != side_norm:
                        continue
                elif order_side and order_side not in (side_norm, "BOTH"):
                    continue
            filtered.append(order)
        return filtered

    def _protection_coverage(self, symbol: str, side: Optional[str] = None) -> Dict[str, Any]:
        orders = self._open_protection_orders(symbol, side=side)
        has_tp = False
        has_sl = False
        for order in orders:
            order_type = str(order.get("type") or order.get("strategyType") or "").upper()
            if "TAKE_PROFIT" in order_type:
                has_tp = True
            if "STOP" in order_type:
                has_sl = True
        return {"has_tp": has_tp, "has_sl": has_sl, "orders": orders}

    def _extract_stop_price_from_order(self, order: Dict[str, Any]) -> float:
        """
        Try best-effort extraction of stop price from various Binance-like payloads.
        """
        for k in ("stopPrice", "triggerPrice", "stop_price", "trigger_price", "price"):
            v = order.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except Exception:
                continue
        return 0.0

    def _get_existing_sl_price(self, orders: Any) -> float:
        """
        Pick a STOP-like order and return its stop price.
        If multiple exist, return the one closest to current market direction doesn't matter;
        we only use it to decide if new SL is tighter.
        """
        best = 0.0
        try:
            for o in (orders or []):
                order_type = str(o.get("type") or o.get("strategyType") or "").upper()
                if "STOP" not in order_type:
                    continue
                p = self._extract_stop_price_from_order(o)
                if p <= 0:
                    continue
                # take the first valid; if multiple, take the last valid
                best = p
        except Exception:
            return 0.0
        return float(best) if best > 0 else 0.0

    def _is_new_sl_tighter(self, side: str, old_sl: float, new_sl: float) -> bool:
        """
        LONG: tighter SL => higher stop price (closer to current price / entry)
        SHORT: tighter SL => lower stop price
        """
        side = str(side).upper()
        if old_sl <= 0:
            return True
        if side == "LONG":
            return new_sl > old_sl
        if side == "SHORT":
            return new_sl < old_sl
        return True

    def _normalize_percent(self, value: Any, default_value: float) -> float:
        try:
            if isinstance(value, str):
                raw = value.strip()
                if raw.endswith("%"):
                    val = float(raw[:-1]) / 100.0
                else:
                    val = float(raw)
            else:
                val = float(value)
        except Exception:
            val = default_value
        # 统一兼容：
        # - 0.006 => 0.6%
        # - 0.6   => 0.6%
        # - 1     => 1%
        if abs(val) > 0.05:
            val = val / 100.0
        return abs(val)

    def _repair_missing_protection(self, symbol: str, position: Dict[str, Any]) -> Dict[str, Any]:
        side = str(position.get("side", "")).upper()
        entry_price = self._to_float(position.get("entry_price"), 0.0)
        qty = self._to_float(position.get("amount"), 0.0)
        if side not in ("LONG", "SHORT"):
            return {"status": "error", "message": f"invalid position side: {side}"}
        if entry_price <= 0 or qty <= 0:
            return {"status": "error", "message": f"invalid entry/qty: entry={entry_price}, qty={qty}"}

        ff_cfg = self.config.get("fund_flow", {}) or {}
        risk_cfg = self.config.get("risk", {}) or {}
        sl_raw = ff_cfg.get("stop_loss_pct", risk_cfg.get("stop_loss_default_percent"))
        tp_raw = ff_cfg.get("take_profit_pct", risk_cfg.get("take_profit_default_percent"))
        sl_pct = self._normalize_percent(sl_raw, 0.01)
        tp_pct = self._normalize_percent(tp_raw, 0.03)

        if side == "LONG":
            stop_loss = entry_price * (1.0 - sl_pct) if sl_pct > 0 else None
            take_profit = entry_price * (1.0 + tp_pct) if tp_pct > 0 else None
            side_enum = IntentPositionSide.LONG
        else:
            stop_loss = entry_price * (1.0 + sl_pct) if sl_pct > 0 else None
            take_profit = entry_price * (1.0 - tp_pct) if tp_pct > 0 else None
            side_enum = IntentPositionSide.SHORT

        return self.client._execute_protection_v2(
            symbol=symbol,
            side=side_enum,
            tp=take_profit,
            sl=stop_loss,
            quantity=qty,
        )

    def _tighten_protection_for_conflict(
        self,
        symbol: str,
        position: Dict[str, Any],
        current_price: float,
        force_break_even: bool = False,
        tighten_ratio: float = 0.5,
        atr_pct: Optional[float] = None,
        min_atr_multiple: float = 1.8,
        cooldown_sec: float = 60.0,
        breakeven_mode: str = "",
        breakeven_fee_buffer: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        冲突保护时收紧止损/保本止损

        Args:
            symbol: 交易对
            position: 持仓信息
            current_price: 当前价格
            force_break_even: 是否强制保本止损
            tighten_ratio: 收紧比例 (0.5 表示止损距离减半)
            atr_pct: 当前周期 ATR 百分比（例如 0.0035）
            min_atr_multiple: 收紧后至少保留的 ATR 距离倍数
            cooldown_sec: 冷却时间（秒），避免频繁撤挂
            breakeven_mode: "profit_only" 时仅盈利后保本；其它值立即按保本价挂单
            breakeven_fee_buffer: 保本手续费buffer（比例），None 时读取配置

        Returns:
            执行结果
        """
        side = str(position.get("side", "")).upper()
        entry_price = self._to_float(position.get("entry_price"), 0.0)
        qty = self._to_float(position.get("amount"), 0.0)

        if side not in ("LONG", "SHORT"):
            return {"status": "error", "message": f"invalid position side: {side}"}
        if entry_price <= 0 or qty <= 0:
            return {"status": "error", "message": f"invalid entry/qty: entry={entry_price}, qty={qty}"}

        ff_cfg = self.config.get("fund_flow", {}) or {}
        risk_cfg = self.config.get("risk", {}) or {}
        conflict_cfg = (
            risk_cfg.get("conflict_protection", {})
            if isinstance(risk_cfg.get("conflict_protection"), dict)
            else {}
        )
        sl_raw = ff_cfg.get("stop_loss_pct", risk_cfg.get("stop_loss_default_percent"))
        sl_pct = self._normalize_percent(sl_raw, 0.01)

        # 允许配置覆盖默认 ATR 保护距离
        atr_multiple = max(
            0.5,
            self._to_float(conflict_cfg.get("tighten_min_atr_multiple"), float(min_atr_multiple)),
        )
        atr_pct_use = abs(self._to_float(atr_pct, 0.0))
        if atr_pct_use <= 0:
            atr_pct_use = abs(self._to_float(conflict_cfg.get("atr_pct_fallback", 0.0), 0.0))
        atr_distance_ratio = min(0.20, atr_pct_use * atr_multiple) if atr_pct_use > 0 else 0.0
        pct_distance_ratio = max(0.0005, sl_pct * max(0.1, float(tighten_ratio)))
        tighten_distance_ratio = max(pct_distance_ratio, atr_distance_ratio)
        atr_guard_applied = tighten_distance_ratio > (pct_distance_ratio + 1e-12)

        # 预先读取当前保护单，确保 cooldown 分支也能输出 old_sl/new_sl
        existing_orders: List[Dict[str, Any]] = []
        old_sl = 0.0
        try:
            existing_orders = self._open_protection_orders(symbol, side=side)
            old_sl = float(self._get_existing_sl_price(existing_orders))
        except Exception:
            existing_orders = []
            old_sl = 0.0

        be_mode = "na"
        be_trigger_price = 0.0
        side_enum = IntentPositionSide.LONG if side == "LONG" else IntentPositionSide.SHORT

        # 计算新的止损价格
        # BREAK-EVEN 仅在已有利润时启用，否则退化为防守止损，避免 entry 附近噪声扫损。
        if force_break_even:
            if breakeven_fee_buffer is None:
                fee_buffer = max(
                    0.0,
                    self._normalize_percent_to_ratio(conflict_cfg.get("breakeven_fee_buffer", 0.0010), 0.0010),
                )
            else:
                fee_buffer = min(0.005, max(0.0, abs(float(breakeven_fee_buffer))))
            arm_buffer = max(
                0.0,
                self._normalize_percent_to_ratio(conflict_cfg.get("breakeven_arm_buffer", 0.0005), 0.0005),
            )
            fallback_ratio = max(
                tighten_distance_ratio,
                self._normalize_percent_to_ratio(conflict_cfg.get("breakeven_fallback_ratio", sl_pct * 0.8), sl_pct * 0.8),
            )
            mode_pref = str(breakeven_mode or conflict_cfg.get("breakeven_mode", "profit_only")).strip().lower()

            if side == "LONG":
                be_trigger_price = entry_price * (1.0 + fee_buffer + arm_buffer)
                if mode_pref != "profit_only" or current_price >= be_trigger_price:
                    stop_loss = entry_price * (1.0 + fee_buffer)
                    be_mode = "armed"
                else:
                    stop_loss = current_price * (1.0 - fallback_ratio)
                    be_mode = "defensive"
            else:
                be_trigger_price = entry_price * (1.0 - fee_buffer - arm_buffer)
                if mode_pref != "profit_only" or current_price <= be_trigger_price:
                    stop_loss = entry_price * (1.0 - fee_buffer)
                    be_mode = "armed"
                else:
                    stop_loss = current_price * (1.0 + fallback_ratio)
                    be_mode = "defensive"
        else:
            if side == "LONG":
                stop_loss = current_price * (1.0 - tighten_distance_ratio)
            else:
                stop_loss = current_price * (1.0 + tighten_distance_ratio)

        # ========== avoid frequent cancel/recreate ==========
        if not hasattr(self, "_sl_tighten_last_ts"):
            self._sl_tighten_last_ts = {}
        now_ts = time.time()
        last_ts = float(self._sl_tighten_last_ts.get((symbol, side), 0.0))
        if (now_ts - last_ts) < float(cooldown_sec):
            return {
                "status": "skipped",
                "message": f"cooldown_active: {symbol} {side} ({now_ts - last_ts:.0f}s < {cooldown_sec:.0f}s)",
                "old_sl": old_sl,
                "new_sl": float(stop_loss),
                "atr_guard_applied": atr_guard_applied,
                "atr_pct": atr_pct_use,
                "min_sl_distance_ratio": tighten_distance_ratio,
                "break_even_mode": be_mode,
                "be_trigger_price": be_trigger_price,
            }

        # 取消现有止损单
        try:
            new_sl = float(stop_loss)

            tighter = self._is_new_sl_tighter(side, old_sl, new_sl)

            if not tighter:
                print(
                    f"🛡️ {symbol} SL未更新(not tighter) | side={side} "
                    f"old_sl={old_sl:.6f} new_sl={new_sl:.6f}"
                )
                return {
                    "status": "skipped",
                    "message": "not_tighter",
                    "old_sl": old_sl,
                    "new_sl": new_sl,
                    "atr_guard_applied": atr_guard_applied,
                    "atr_pct": atr_pct_use,
                    "min_sl_distance_ratio": tighten_distance_ratio,
                    "break_even_mode": be_mode,
                    "be_trigger_price": be_trigger_price,
                }

            for order in existing_orders:
                order_type = str(order.get("type") or order.get("strategyType") or "").upper()
                if "STOP" in order_type:
                    oid = order.get("orderId")
                    if oid:
                        self.client.cancel_order(symbol, oid)
        except Exception as e:
            print(f"⚠️ {symbol} 取消旧止损单失败: {e}")
            old_sl = 0.0

        # 设置新的止损单（不设止盈，保留现有止盈）
        result = self.client._execute_protection_v2(
            symbol=symbol,
            side=side_enum,
            tp=None,  # 不改变止盈
            sl=stop_loss,
            quantity=qty,
        )

        if force_break_even:
            action_desc = "保本止损" if be_mode == "armed" else "防守止损(未到保本触发)"
        else:
            action_desc = f"收紧止损({tighten_ratio:.0%})"
        self._sl_tighten_last_ts[(symbol, side)] = now_ts

        print(
            f"🛡️ {symbol} {action_desc} | side={side} "
            f"entry={entry_price:.6f} "
            f"old_sl={old_sl:.6f} → new_sl={float(stop_loss):.6f} "
            f"qty={qty:.4f} "
            f"atr={atr_pct_use:.4f} min_dist={tighten_distance_ratio:.4f} guard={'Y' if atr_guard_applied else 'N'} "
            f"be_mode={be_mode}"
        )

        if isinstance(result, dict):
            enriched = dict(result)
            enriched.setdefault("old_sl", old_sl)
            enriched["new_sl"] = float(stop_loss)
            enriched["atr_guard_applied"] = atr_guard_applied
            enriched["atr_pct"] = atr_pct_use
            enriched["min_sl_distance_ratio"] = tighten_distance_ratio
            enriched["break_even_mode"] = be_mode
            enriched["be_trigger_price"] = be_trigger_price
            return enriched
        return {
            "status": "unknown",
            "old_sl": old_sl,
            "new_sl": float(stop_loss),
            "atr_guard_applied": atr_guard_applied,
            "atr_pct": atr_pct_use,
            "min_sl_distance_ratio": tighten_distance_ratio,
            "break_even_mode": be_mode,
            "be_trigger_price": be_trigger_price,
            "raw": result,
        }

    def _maybe_log_conflict_protection_stats(self, interval_sec: float = 600.0):
        """定期打印冲突保护统计摘要（每 10 分钟一条）"""
        try:
            if not hasattr(self, "_last_protect_stats_log_ts"):
                self._last_protect_stats_log_ts = 0.0
            now = time.time()
            if (now - float(self._last_protect_stats_log_ts)) < float(interval_sec):
                return
            self._last_protect_stats_log_ts = now
            if getattr(self, "risk_manager", None) is None:
                return
            s = self.risk_manager.format_conflict_protection_stats(top_n=6)
            print(f"🔬 冲突保护统计: {s}")
        except Exception:
            return

    def _emergency_flatten_unprotected(
        self,
        symbol: str,
        position: Dict[str, Any],
        reduce_ratio: float = 1.0,
    ) -> Dict[str, Any]:
        side = str(position.get("side", "")).upper()
        qty_total = self._to_float(position.get("amount"), 0.0)
        if side not in ("LONG", "SHORT") or qty_total <= 0:
            return {"status": "error", "message": f"invalid close input side={side}, qty={qty_total}"}

        try:
            ratio = float(reduce_ratio)
        except Exception:
            ratio = 1.0
        ratio = min(1.0, max(0.1, ratio))
        qty_target = qty_total * ratio

        try:
            qty_target = float(self.client.format_quantity(symbol, qty_target))
        except Exception:
            pass
        if qty_target <= 0:
            return {"status": "error", "message": f"invalid close qty after format: {qty_target}"}

        close_side = "SELL" if side == "LONG" else "BUY"

        base_params: Dict[str, Any] = {
            "symbol": symbol,
            "type": "MARKET",
            "quantity": qty_target,
        }

        hedge_mode = False
        try:
            hedge_mode = bool(self.client.broker.get_hedge_mode())
        except Exception:
            hedge_mode = False

        candidates: List[Tuple[str, Dict[str, Any], bool]] = []
        if ratio >= 0.999:
            p_close = dict(base_params)
            p_close["closePosition"] = True
            if hedge_mode:
                p_close["positionSide"] = side
            candidates.append(("close_position", p_close, True))

        p_reduce = dict(base_params)
        if hedge_mode:
            p_reduce["positionSide"] = side
        candidates.append(("reduce_only_market", p_reduce, True))

        if hedge_mode:
            # 兜底：部分账户/模式下 positionSide 可能导致拒单，提供无 positionSide 变体
            if ratio >= 0.999:
                p_close_no_ps = dict(base_params)
                p_close_no_ps["closePosition"] = True
                candidates.append(("close_position_no_ps", p_close_no_ps, True))
            p_reduce_no_ps = dict(base_params)
            candidates.append(("reduce_only_market_no_ps", p_reduce_no_ps, True))

        errors: List[str] = []
        for mode, params, reduce_only in candidates:
            try:
                order = self.client._execute_order_v2(
                    params=params,
                    side=close_side,
                    reduce_only=reduce_only,
                )
                if isinstance(order, dict):
                    code = order.get("code")
                    if isinstance(code, (int, float)) and float(code) < 0:
                        errors.append(f"{mode}: code={code}, msg={order.get('msg')}")
                        continue
                    if order.get("orderId") is not None:
                        return {"status": "success", "order": order, "mode": mode}
                    if str(order.get("status", "")).lower() == "success":
                        return {"status": "success", "order": order, "mode": mode}

                # 若返回结构不标准，二次确认仓位是否已消失，避免误判。
                latest_pos = self.position_data.get_current_position(symbol)
                if not isinstance(latest_pos, dict):
                    return {
                        "status": "success",
                        "order": order,
                        "mode": mode,
                        "message": "position closed after emergency request",
                    }
                errors.append(f"{mode}: unexpected response={order}")
            except Exception as e:
                errors.append(f"{mode}: {e}")

        return {"status": "error", "message": " | ".join(errors)[:1200]}

    def _decision_signal_score(self, decision: Any) -> float:
        md = decision.metadata if isinstance(getattr(decision, "metadata", None), dict) else {}
        long_score = self._to_float(md.get("long_score"), 0.0)
        short_score = self._to_float(md.get("short_score"), 0.0)
        return max(long_score, short_score)

    def _is_ai_gate_enabled(self) -> bool:
        ff_cfg = self.config.get("fund_flow", {}) or {}
        ds_router_cfg = ff_cfg.get("deepseek_weight_router", {}) if isinstance(ff_cfg.get("deepseek_weight_router"), dict) else {}
        ds_ai_cfg = ff_cfg.get("deepseek_ai", {}) if isinstance(ff_cfg.get("deepseek_ai"), dict) else {}
        api_key = os.environ.get("DEEPSEEK_API_KEY") or str(ds_ai_cfg.get("api_key", "") or "")
        return bool(
            ds_router_cfg.get("enabled", False)
            and ds_router_cfg.get("ai_enabled", False)
            and ds_ai_cfg.get("enabled", False)
            and bool(str(api_key).strip())
        )

    def _update_dca_state_after_execution(self, symbol: str, decision: Any, execution_result: Dict[str, Any]) -> None:
        if not isinstance(decision, FundFlowDecision):
            return
        status = str((execution_result or {}).get("status", "")).lower()
        if status not in ("success", "pending"):
            return

        if decision.operation == FundFlowOperation.CLOSE:
            self._clear_dca_tracking_for_symbol(symbol)
            return

        md_raw = getattr(decision, "metadata", None)
        md: Dict[str, Any] = md_raw if isinstance(md_raw, dict) else {}
        if not bool(md.get("dca_triggered")):
            return

        try:
            stage = int(md.get("dca_stage", 0) or 0)
        except Exception:
            stage = 0
        if stage <= 0:
            return

        side = "LONG" if decision.operation == FundFlowOperation.BUY else "SHORT"
        pos_key = self._position_track_key(symbol, side)
        old_stage = int(self._dca_stage_by_pos.get(pos_key, 0) or 0)
        if stage > old_stage:
            self._dca_stage_by_pos[pos_key] = stage
            self._save_risk_state()

    def _cleanup_stale_protection_orders(self, symbols: List[str]) -> None:
        cfg = self._stale_protection_cleanup_config()
        if not bool(cfg.get("enabled", True)):
            return
        if not symbols:
            return

        opened_count = len(self._opened_symbols_this_cycle)
        delay_seconds = int(cfg.get("delay_seconds", 3) or 0)
        if opened_count > 0 and delay_seconds > 0:
            print(f"⏳ 本轮开仓后等待 {delay_seconds}s，再执行无持仓保护单清理...")
            time.sleep(delay_seconds)

        cleaned_symbols = 0
        cleaned_orders = 0
        for symbol in symbols:
            try:
                position = self.position_data.get_current_position(symbol)
                if isinstance(position, dict):
                    continue
                if self._has_pending_entry_order(symbol):
                    continue

                stale_orders = self._open_protection_orders(symbol)
                if not stale_orders:
                    continue

                # 二次确认，避免临界时刻误清理刚建立仓位后的保护单
                position_confirm = self.position_data.get_current_position(symbol)
                if isinstance(position_confirm, dict):
                    continue

                cancel_result = self.client.cancel_all_conditional_orders(symbol)
                cleaned_symbols += 1
                cleaned_orders += len(stale_orders)
                if isinstance(cancel_result, dict):
                    print(
                        f"🧹 {symbol} 无持仓，清理未触发保护单 {len(stale_orders)} 个 | "
                        f"status={cancel_result.get('status')} failed={cancel_result.get('failed')}"
                    )
                else:
                    print(f"🧹 {symbol} 无持仓，清理未触发保护单 {len(stale_orders)} 个")
            except Exception as e:
                print(f"⚠️ {symbol} 清理未触发保护单失败: {e}")

        if cleaned_symbols > 0:
            print(f"🧹 无持仓保护单清理完成: symbols={cleaned_symbols}, orders~={cleaned_orders}")

    def _execute_and_log_decision(
        self,
        *,
        symbol: str,
        decision: Any,
        account_summary: Dict[str, Any],
        current_price: float,
        position: Optional[Dict[str, Any]],
        flow_context: Dict[str, Any],
        trigger_type: str,
        trigger_id: str,
        trigger_context: Dict[str, Any],
        portfolio: Dict[str, Any],
    ) -> None:
        decision_json = self.fund_flow_execution_router.decision_to_json(decision)

        self.fund_flow_attribution_engine.log_decision(
            decision=decision,
            context={
                "symbol": symbol,
                "price": current_price,
                "portfolio": portfolio,
                "flow_context": flow_context,
                "trigger_context": trigger_context,
            },
        )

        if decision.operation == FundFlowOperation.CLOSE and self._has_pending_close_order(symbol):
            print(f"⏭️ {symbol} 存在待成交平仓单，跳过重复平仓下发")
            return

        execution_result = self.fund_flow_execution_router.execute_decision(
            decision=decision,
            account_state=account_summary,
            current_price=current_price,
            position=position,
            trigger_context=trigger_context,
        )
        if isinstance(execution_result, dict):
            post_hook = self._post_execution_protection_hook(
                symbol=symbol,
                decision=decision,
                execution_result=execution_result,
            )
            if isinstance(post_hook, dict) and post_hook:
                execution_result["post_protection_hook"] = post_hook
        self._update_dca_state_after_execution(symbol=symbol, decision=decision, execution_result=execution_result)
        position_for_log = position
        if decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL, FundFlowOperation.CLOSE):
            try:
                position_for_log = self.position_data.get_current_position(symbol)
            except Exception:
                position_for_log = position
        if decision.operation == FundFlowOperation.CLOSE and execution_result.get("status") == "success":
            self._update_loss_streak_after_close(symbol, execution_result)

        order_info = execution_result.get("order") if isinstance(execution_result, dict) else None
        order_id = str(order_info.get("orderId")) if isinstance(order_info, dict) and order_info.get("orderId") else None
        tp_order_id = None
        sl_order_id = None
        protection = execution_result.get("protection") if isinstance(execution_result, dict) else None
        if isinstance(protection, dict) and isinstance(protection.get("orders"), list):
            for item in protection.get("orders", []):
                if not isinstance(item, dict):
                    continue
                order_type = str(item.get("type") or item.get("strategyType") or "").upper()
                oid = item.get("orderId")
                if oid is None:
                    continue
                if "TAKE_PROFIT" in order_type:
                    tp_order_id = str(oid)
                if "STOP" in order_type:
                    sl_order_id = str(oid)

        self._safe_storage_call(
            "insert_ai_decision_log",
            symbol=symbol,
            operation=decision.operation.value,
            decision_json=decision_json,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            order_id=order_id,
            tp_order_id=tp_order_id,
            sl_order_id=sl_order_id,
            realized_pnl=execution_result.get("realized_pnl") if isinstance(execution_result, dict) else None,
            exchange="binance",
        )
        self._safe_storage_call(
            "insert_program_execution_log",
            symbol=symbol,
            operation=decision.operation.value,
            decision_json=decision_json,
            market_context_json=json.dumps(flow_context, ensure_ascii=False),
            params_snapshot_json=json.dumps({"trigger_context": trigger_context}, ensure_ascii=False),
            order_id=order_id,
            environment=str(self.config.get("environment", {}).get("mode", "production")),
            exchange="binance",
        )
        try:
            self._write_trade_fill_log(
                symbol=symbol,
                decision=decision,
                execution_result=execution_result,
            )
        except Exception as e:
            print(f"⚠️ {symbol} 成交回报写入失败: {e}")

        if execution_result.get("status") == "success" and decision.operation != FundFlowOperation.HOLD:
            self.trade_count += 1
        md = decision.metadata if isinstance(decision.metadata, dict) else {}
        long_score = self._to_float(md.get("long_score"), 0.0)
        short_score = self._to_float(md.get("short_score"), 0.0)
        engine_tag = str(md.get("engine") or md.get("regime") or "")
        selected_pool_id = str(md.get("signal_pool_id") or md.get("selected_pool_id") or "")
        direction_lock = str(md.get("direction_lock") or "")
        regime_adx = self._to_float(md.get("regime_adx"), 0.0)
        regime_atr_pct = self._to_float(md.get("regime_atr_pct"), 0.0)
        leverage_sync = execution_result.get("leverage_sync") if isinstance(execution_result, dict) else None
        lev_req = decision.leverage
        lev_applied = lev_req
        if isinstance(leverage_sync, dict) and leverage_sync.get("status") == "success":
            lev_applied = leverage_sync.get("applied", lev_req)
        status_value = str(execution_result.get("status"))
        if decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL) and status_value in ("success", "pending"):
            self._opened_symbols_this_cycle.add(symbol)
        display_status = "pending(挂单待成交)" if status_value == "pending" else status_value
        current_portion = self._estimate_position_portion(position_for_log, account_summary)

        print(
            f"[{symbol}] 决策={decision.operation.value.upper()} | "
            f"状态={display_status} | "
            f"目标占比={decision.target_portion_of_balance:.2f} | "
            f"当前占比={current_portion:.2f} | "
            f"杠杆(请求/实际)={lev_req}x/{lev_applied}x"
        )
        if decision.operation == FundFlowOperation.CLOSE:
            pre_side = str(position.get("side", "")).upper() if isinstance(position, dict) else ""
            pre_key = self._position_track_key(symbol, pre_side) if pre_side in ("LONG", "SHORT") else ""
            first_seen_ts = self._position_first_seen_ts.get(pre_key) if pre_key else None
            hold_minutes = max(0.0, (time.time() - float(first_seen_ts))) / 60.0 if first_seen_ts is not None else 0.0
            ext_raw = self._position_extrema_by_pos.get(pre_key) if pre_key else None
            ext: Dict[str, float] = ext_raw if isinstance(ext_raw, dict) else {}
            mfe_pct = max(0.0, float(ext.get("max_favorable_ratio", 0.0))) * 100.0
            mae_pct = min(0.0, float(ext.get("max_adverse_ratio", 0.0))) * 100.0

            def _pos_snapshot_text(pos_obj: Any) -> str:
                if not isinstance(pos_obj, dict):
                    return "FLAT:0"
                side = str(pos_obj.get("side") or pos_obj.get("positionSide") or "").upper()
                amt_raw = self._to_float(
                    pos_obj.get("amount", pos_obj.get("positionAmt", 0.0)),
                    0.0,
                )
                amt = abs(amt_raw)
                if side not in ("LONG", "SHORT"):
                    if amt_raw > 0:
                        side = "LONG"
                    elif amt_raw < 0:
                        side = "SHORT"
                    else:
                        side = "FLAT"
                if amt <= 0:
                    return "FLAT:0"
                return f"{side}:{amt:.6f}"

            pre_snap = _pos_snapshot_text(position)
            post_snap = _pos_snapshot_text(position_for_log)
            sync_live_txt = "-"
            position_sync = execution_result.get("position_sync") if isinstance(execution_result, dict) else None
            if isinstance(position_sync, dict):
                live_side = str(position_sync.get("live_side") or "").upper()
                live_size = self._to_float(position_sync.get("live_size"), 0.0)
                if live_size <= 0:
                    sync_live_txt = "FLAT:0"
                else:
                    if live_side not in ("LONG", "SHORT"):
                        live_side = "UNK"
                    sync_live_txt = f"{live_side}:{live_size:.6f}"
            print(
                f"   平仓前后仓位快照: pre={pre_snap} -> post={post_snap} | exch_live={sync_live_txt} | status={display_status}"
            )
            print(
                f"   回合摘要: hold_min={hold_minutes:.1f}, "
                f"mfe={mfe_pct:.2f}%, mae={mae_pct:.2f}%, "
                f"exit_reason={str(decision.reason or '').strip()[:180]}"
            )

            post_amt = self._to_float(
                position_for_log.get("amount", position_for_log.get("positionAmt", 0.0))
                if isinstance(position_for_log, dict)
                else 0.0,
                0.0,
            )
            if pre_key and abs(post_amt) <= 0:
                self._position_extrema_by_pos.pop(pre_key, None)
        print(
            f"   信号评分: long={long_score:.3f}, short={short_score:.3f} | "
            f"触发类型={trigger_type}"
        )
        score_15m_raw = md.get("score_15m")
        score_5m_raw = md.get("score_5m")
        final_score_raw = md.get("final_score")
        score_15m_md: Dict[str, Any] = score_15m_raw if isinstance(score_15m_raw, dict) else {}
        score_5m_md: Dict[str, Any] = score_5m_raw if isinstance(score_5m_raw, dict) else {}
        final_score_md: Dict[str, Any] = final_score_raw if isinstance(final_score_raw, dict) else {}
        ds_confidence = self._to_float(md.get("ds_confidence"), 0.0)

        # 获取 EV 和 LW 方向判断结果
        ev_direction = md.get("ev_direction", "BOTH")
        ev_score = self._to_float(md.get("ev_score"), 0.0)
        lw_direction = md.get("lw_direction", "BOTH")
        lw_score = self._to_float(md.get("lw_score"), 0.0)
        combo_compare_raw = md.get("combo_compare")
        combo_compare: Dict[str, Any] = combo_compare_raw if isinstance(combo_compare_raw, dict) else {}
        lw_components = md.get("lw_components", {})
        final_dir_info = md.get("final", {})
        need_confirm = final_dir_info.get("need_confirm", False) if isinstance(final_dir_info, dict) else False

        print(
            "   3.0评分: "
            f"score_15m(L/S)={self._to_float(score_15m_md.get('long_score'), 0.0):.3f}/{self._to_float(score_15m_md.get('short_score'), 0.0):.3f}, "
            f"score_5m(L/S)={self._to_float(score_5m_md.get('long_score'), 0.0):.3f}/{self._to_float(score_5m_md.get('short_score'), 0.0):.3f}, "
            f"final_score(L/S)={self._to_float(final_score_md.get('long_score'), 0.0):.3f}/{self._to_float(final_score_md.get('short_score'), 0.0):.3f}, "
            f"direction_lock={direction_lock or '-'}, "
            f"ds_confidence={ds_confidence:.3f}"
        )
        # 显示详细的方向判断信息
        def _fmt_lw_component(v: Any) -> str:
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, (int, float)):
                return f"{float(v):+.2f}"
            if isinstance(v, dict):
                return "{...}"
            if isinstance(v, (list, tuple)):
                return "[...]"
            return str(v)

        comp_str = ",".join([f"{k}:{_fmt_lw_component(v)}" for k, v in lw_components.items()]) if lw_components else "-"
        div = abs(lw_score - ev_score)
        agree = (lw_direction == ev_direction) or (lw_direction == "BOTH" or ev_direction == "BOTH")
        confirm_tag = "⚠️需确认" if need_confirm else ""
        
        # K线 open/close 价格
        kline_open = self._to_float(md.get("last_open"), 0.0)
        kline_close = self._to_float(md.get("last_close"), 0.0)
        # 始终显示 K线价格，即使为 0（方便调试）
        if kline_open > 0 or kline_close > 0:
            kline_change_pct = (kline_close - kline_open) / kline_open * 100 if kline_open > 0 else 0.0
            print(f"   K线价格: open={kline_open:.4f} | close={kline_close:.4f} | change={kline_change_pct:+.2f}%")
        else:
            # 调试：显示为什么没有 K线数据
            tf_used = md.get("active_timeframe", "unknown")
            print(f"   K线价格: 未获取到 (tf={tf_used}, open={kline_open}, close={kline_close})")
        
        print(
            f"   方向判断: dir_lw={lw_direction[:4]}({lw_score:+.2f}) | dir_ev={ev_direction[:4]}({ev_score:+.2f}) | "
            f"agree={1 if agree else 0} div={div:.2f} conf={abs(lw_score):.2f} {confirm_tag}"
        )
        if combo_compare:
            # 兼容新旧结构：
            # 新版：active_model/lw_winner/ev_winner/lw_combo_score/ev_combo_score/winner
            # 旧版：active_dir/active_score/legacy_dir/legacy_score/agility_new/agility_old/flow_align_new/flow_align_old
            active_model = str(combo_compare.get("active_model", "MACD+KDJ"))
            lw_winner = str(combo_compare.get("lw_winner", "-"))
            ev_winner = str(combo_compare.get("ev_winner", "-"))
            lw_combo_score = self._to_float(combo_compare.get("lw_combo_score"), 0.0)
            ev_combo_score = self._to_float(combo_compare.get("ev_combo_score"), 0.0)
            has_new_combo = ("lw_winner" in combo_compare) or ("ev_winner" in combo_compare)
            print(
                "   方向对照: "
                f"active_model={active_model} | "
                + (
                    f"LW={lw_winner}({lw_combo_score:+.2f}) | EV={ev_winner}({ev_combo_score:+.2f}) | "
                    if has_new_combo
                    else
                    f"MACD+KDJ={str(combo_compare.get('active_dir', ev_direction))[:4]}({self._to_float(combo_compare.get('active_score'), ev_score):+.2f}) | "
                    f"MACD+BB={str(combo_compare.get('legacy_dir', 'BOTH'))[:4]}({self._to_float(combo_compare.get('legacy_score'), 0.0):+.2f}) | "
                    f"agile={self._to_float(combo_compare.get('agility_new'), abs(ev_score)):.2f}/{self._to_float(combo_compare.get('agility_old'), 0.0):.2f} | "
                    f"flow_align={int(self._to_int(combo_compare.get('flow_align_new'), 0))}/{int(self._to_int(combo_compare.get('flow_align_old'), 0))} | "
                )
                + f"winner={combo_compare.get('winner', '-')}"
            )
            guide_model = str(combo_compare.get("direction_guide_model_label", active_model))
            guide_dir = str(combo_compare.get("guide_dir", combo_compare.get("active_dir", ev_direction)))
            guide_score = self._to_float(combo_compare.get("guide_score"), self._to_float(combo_compare.get("active_score"), ev_score))
            guide_neutral_zone = self._to_float(combo_compare.get("guide_neutral_zone"), 0.02)
            print(
                "   开仓指导: "
                f"model={guide_model}, dir={guide_dir[:8]}, score={guide_score:+.3f}, "
                f"neutral_zone={guide_neutral_zone:.3f}"
            )
            guide_model_key = str(combo_compare.get("direction_guide_model", "")).upper()
            is_kdj_guide = guide_model_key == "MACD_KDJ" or "KDJ" in guide_model.upper()
            kdj_weights_raw = combo_compare.get("macd_kdj_weights", {})
            kdj_weights = kdj_weights_raw if isinstance(kdj_weights_raw, dict) else {}
            bb_weights_raw = combo_compare.get("macd_bb_weights", {})
            bb_weights = bb_weights_raw if isinstance(bb_weights_raw, dict) else {}
            if is_kdj_guide and kdj_weights:
                print(
                    "   MACD+KDJ设置: "
                    f"macd={self._to_float(kdj_weights.get('macd'), 0.0):.2f}, "
                    f"kdj={self._to_float(kdj_weights.get('kdj'), 0.0):.2f}, "
                    f"cross={self._to_float(kdj_weights.get('macd_cross'), 0.0):.2f}, "
                    f"kdj_cross={self._to_float(kdj_weights.get('kdj_cross'), 0.0):.2f}, "
                    f"kdj_zone={self._to_float(kdj_weights.get('kdj_zone'), 0.0):.2f}, "
                    f"hist_mom={self._to_float(kdj_weights.get('macd_hist_mom'), 0.0):.2f}"
                )
            elif bb_weights:
                print(
                    "   MACD+BB设置: "
                    f"macd={self._to_float(bb_weights.get('macd'), 0.0):.2f}, "
                    f"bb={self._to_float(bb_weights.get('bb'), 0.0):.2f}, "
                    f"cross={self._to_float(bb_weights.get('macd_cross'), 0.0):.2f}, "
                    f"bb_break={self._to_float(bb_weights.get('bb_break'), 0.0):.2f}, "
                    f"bb_trend={self._to_float(bb_weights.get('bb_trend'), 0.0):.2f}, "
                    f"hist_mom={self._to_float(bb_weights.get('macd_hist_mom'), 0.0):.2f}"
                )
            feature_raw = combo_compare.get("feature_snapshot", {})
            feature = feature_raw if isinstance(feature_raw, dict) else {}
            if feature:
                if is_kdj_guide:
                    print(
                        "   MACD+KDJ因子: "
                        f"macd={self._to_float(feature.get('macd'), 0.0):+.3f}, "
                        f"kdj={self._to_float(feature.get('kdj'), 0.0):+.3f}, "
                        f"macd_cross={self._to_float(feature.get('macd_cross'), 0.0):+.3f}, "
                        f"kdj_cross={self._to_float(feature.get('kdj_cross'), 0.0):+.3f}, "
                        f"kdj_zone={self._to_float(feature.get('kdj_zone'), 0.0):+.3f}, "
                        f"hist_mom={self._to_float(feature.get('macd_hist_mom'), 0.0):+.3f}"
                    )
                else:
                    print(
                        "   MACD+BB因子: "
                        f"macd={self._to_float(feature.get('macd'), 0.0):+.3f}, "
                        f"bb={self._to_float(feature.get('bb'), 0.0):+.3f}, "
                        f"macd_cross={self._to_float(feature.get('macd_cross'), 0.0):+.3f}, "
                        f"bb_break={self._to_float(feature.get('bb_break'), 0.0):+.3f}, "
                        f"bb_trend={self._to_float(feature.get('bb_trend'), 0.0):+.3f}, "
                        f"hist_mom={self._to_float(feature.get('macd_hist_mom'), 0.0):+.3f}, "
                        f"bb_squeeze={self._to_float(feature.get('bb_squeeze'), 0.0):.0f}"
                    )
            settings_raw = combo_compare.get("settings", {})
            settings = settings_raw if isinstance(settings_raw, dict) else {}
            if settings:
                if is_kdj_guide:
                    print(
                        "   MACD+KDJ补充: "
                        f"align_bonus={self._to_float(settings.get('align_bonus'), 0.05):.2f}, "
                        f"squeeze_penalty_applied={bool(settings.get('squeeze_penalty_applied', False))}"
                    )
                else:
                    print(
                        "   MACD+BB补充: "
                        f"squeeze_penalty={self._to_float(settings.get('bb_squeeze_penalty'), 0.72):.2f}, "
                        f"align_bonus={self._to_float(settings.get('align_bonus'), 0.05):.2f}, "
                        f"squeeze_penalty_applied={bool(settings.get('squeeze_penalty_applied', False))}"
                    )
        print(f"   components(lw): {comp_str}")
        ds_weights_snapshot = md.get("ds_weights_snapshot")
        if isinstance(ds_weights_snapshot, dict) and ds_weights_snapshot:
            ds_weights_text = json.dumps(ds_weights_snapshot, ensure_ascii=False, separators=(",", ":"))
            if len(ds_weights_text) > 320:
                ds_weights_text = ds_weights_text[:317] + "..."
            print(f"   DS权重快照: {ds_weights_text}")
        if engine_tag:
            print(
                "   引擎上下文: "
                f"engine={engine_tag}, pool={selected_pool_id or '-'}, "
                f"direction={direction_lock or '-'}, adx={regime_adx:.2f}, atr_pct={regime_atr_pct:.4f}"
            )
        range_quantiles = md.get("range_quantiles")
        if isinstance(range_quantiles, dict):
            rq_n = self._to_int(range_quantiles.get("n"), 0)
            rq_imb_hi = self._to_float(range_quantiles.get("imb_hi"), 0.0)
            rq_imb_lo = self._to_float(range_quantiles.get("imb_lo"), 0.0)
            rq_cvd_hi = self._to_float(range_quantiles.get("cvd_hi"), 0.0)
            rq_cvd_lo = self._to_float(range_quantiles.get("cvd_lo"), 0.0)
            rq_current_raw = md.get("range_current")
            rq_current: Dict[str, Any] = rq_current_raw if isinstance(rq_current_raw, dict) else {}
            rq_imb = self._to_float(rq_current.get("imbalance"), self._to_float(flow_context.get("imbalance"), 0.0))
            rq_cvd = self._to_float(rq_current.get("cvd_momentum"), self._to_float(flow_context.get("cvd_momentum"), 0.0))
            print(
                "   RANGE分位数: "
                f"n={rq_n}, "
                f"imb_q=[{rq_imb_lo:.4f},{rq_imb_hi:.4f}], "
                f"cvd_q=[{rq_cvd_lo:.6f},{rq_cvd_hi:.6f}], "
                f"current_imb={rq_imb:+.4f}, current_cvd={rq_cvd:+.6f}"
            )
        range_turn = md.get("range_turn")
        if isinstance(range_turn, dict):
            mode = str(range_turn.get("mode") or "-")
            ready = bool(range_turn.get("ready", False))
            up = bool(range_turn.get("turned_up", False))
            down = bool(range_turn.get("turned_down", False))
            cvd0 = range_turn.get("cvd0")
            cvd1 = range_turn.get("cvd1")
            cvd2 = range_turn.get("cvd2")
            cvd0_txt = "NA" if cvd0 is None else f"{self._to_float(cvd0, 0.0):+.6f}"
            cvd1_txt = "NA" if cvd1 is None else f"{self._to_float(cvd1, 0.0):+.6f}"
            cvd2_txt = "NA" if cvd2 is None else f"{self._to_float(cvd2, 0.0):+.6f}"
            print(
                "   RANGE拐头: "
                f"mode={mode}, ready={ready}, up={up}, down={down}, "
                f"cvd2={cvd2_txt}, cvd1={cvd1_txt}, cvd0={cvd0_txt}"
            )
        print(
            "   资金流: "
            f"cvd={self._to_float(flow_context.get('cvd_ratio'), 0.0):+.4f}, "
            f"cvd_mom={self._to_float(flow_context.get('cvd_momentum'), 0.0):+.4f}, "
            f"oi_delta={self._to_float(flow_context.get('oi_delta_ratio'), 0.0):+.4f}, "
            f"funding={self._to_float(flow_context.get('funding_rate'), 0.0):+.6f}, "
            f"depth={self._to_float(flow_context.get('depth_ratio'), 1.0):.4f}, "
            f"imbalance={self._to_float(flow_context.get('imbalance'), 0.0):+.4f}, "
            f"liq_norm={self._to_float(flow_context.get('liquidity_delta_norm'), 0.0):+.4f}"
        )
        if decision.reason:
            print(f"   决策原因: {decision.reason}")
        if isinstance(leverage_sync, dict) and leverage_sync.get("status") == "error":
            print(f"   ⚠️ 杠杆同步失败: {leverage_sync.get('message')}")
        if status_value == "pending":
            order_obj = execution_result.get("order") if isinstance(execution_result, dict) else {}
            if isinstance(order_obj, dict):
                print(
                    "   ⏳ 委托状态: "
                    f"orderId={order_obj.get('orderId')}, "
                    f"status={order_obj.get('status')}, "
                    f"executedQty={order_obj.get('executedQty')}"
                )
            if execution_result.get("message"):
                print(f"   ⏳ 说明: {execution_result.get('message')}")
        if status_value == "error":
            print(f"   ❌ 执行失败详情: {execution_result.get('message')}")
            if execution_result.get("error_code") is not None:
                print(
                    "   ❌ 交易所错误: "
                    f"code={execution_result.get('error_code')}, "
                    f"detail={execution_result.get('error_detail')}"
                )
        protection_obj = execution_result.get("protection") if isinstance(execution_result, dict) else None
        if isinstance(protection_obj, dict):
            print(
                "   🛡️ 保护单: "
                f"status={protection_obj.get('status')}, "
                f"msg={protection_obj.get('message')}, "
                f"orders={len(protection_obj.get('orders') or [])}"
            )
        post_hook_obj = execution_result.get("post_protection_hook") if isinstance(execution_result, dict) else None
        if isinstance(post_hook_obj, dict):
            print(
                "   🪝 执行后保护钩子: "
                f"status={post_hook_obj.get('status')}, "
                f"msg={post_hook_obj.get('message')}"
            )

    def _post_execution_protection_hook(
        self,
        *,
        symbol: str,
        decision: FundFlowDecision,
        execution_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        if decision.operation not in (FundFlowOperation.BUY, FundFlowOperation.SELL):
            return {}
        status = str(execution_result.get("status", "")).lower()
        if status not in ("success", "pending"):
            return {}
        try:
            latest_position = self.position_data.get_current_position(symbol)
        except Exception:
            latest_position = None
        if not isinstance(latest_position, dict):
            return {"status": "skipped", "message": "position_not_visible"}
        side = str(latest_position.get("side", "")).upper()
        if side not in ("LONG", "SHORT"):
            return {"status": "skipped", "message": f"invalid_position_side:{side}"}

        coverage = self._protection_coverage(symbol, side=side)
        covered = bool(coverage.get("has_tp")) and bool(coverage.get("has_sl"))
        if covered:
            return {"status": "ok", "message": "coverage_ready", "coverage": coverage}

        print(
            f"🚨 {symbol} 执行后保护钩子检测缺失保护单: "
            f"has_tp={coverage.get('has_tp')} has_sl={coverage.get('has_sl')}"
        )
        repair = self._repair_missing_protection(symbol, latest_position)
        coverage_after = self._protection_coverage(symbol, side=side)
        covered_after = bool(coverage_after.get("has_tp")) and bool(coverage_after.get("has_sl"))
        if covered_after:
            print(f"   ✅ {symbol} 执行后保护钩子补挂成功")
            return {
                "status": "repaired",
                "message": "protection_repaired",
                "repair": repair,
                "coverage_before": coverage,
                "coverage_after": coverage_after,
            }

        result: Dict[str, Any] = {
            "status": "failed",
            "message": "protection_missing_after_repair",
            "repair": repair,
            "coverage_before": coverage,
            "coverage_after": coverage_after,
        }
        if bool(self._protection_sla_config().get("immediate_close_on_repair_fail", False)):
            flatten = self._emergency_flatten_unprotected(symbol, latest_position, reduce_ratio=1.0)
            result["flatten"] = flatten
            print(
                f"   🧯 {symbol} 执行后保护钩子修复失败，触发强制减仓/平仓: "
                f"status={flatten.get('status')} detail={flatten.get('message') or flatten.get('order')}"
            )
        return result

    def run_cycle(self, allow_new_entries: bool = True, ai_review_mode: str = "disabled") -> None:
        self._run_cycle_impl(allow_new_entries=allow_new_entries, ai_review_mode=ai_review_mode)

    def _run_cycle_impl(self, allow_new_entries: bool = True, ai_review_mode: str = "disabled") -> None:
        context = self._prepare_cycle_context(allow_new_entries=allow_new_entries, ai_review_mode=ai_review_mode)
        if context is None:
            return

        symbols_raw = context.get("symbols")
        symbols = symbols_raw if isinstance(symbols_raw, list) else []
        cycle_start_ts = self._to_float(context.get("cycle_start_ts"), time.time())
        max_cycle_runtime_seconds = max(0.0, self._to_float(context.get("max_cycle_runtime_seconds"), 0.0))

        for idx, symbol in enumerate(symbols):
            if max_cycle_runtime_seconds > 0:
                elapsed_before = time.time() - cycle_start_ts
                if elapsed_before >= max_cycle_runtime_seconds:
                    print(
                        "🛑 轮询预算触发提前结束: "
                        f"elapsed={elapsed_before:.2f}s >= budget={max_cycle_runtime_seconds:.2f}s, "
                        f"processed={idx}/{len(symbols)}"
                    )
                    break
            self._process_symbol(symbol=symbol, idx=idx, context=context)

        self._finalize_entries(context=context)

    def _prepare_cycle_context(
        self,
        allow_new_entries: bool = True,
        ai_review_mode: str = "disabled",
    ) -> Optional[Dict[str, Any]]:
        # 每轮先做配置文件 mtime 检查，发生变更则自动重载并立即生效
        self._reload_config_if_changed()
        self._refresh_signal_pool_runtime_if_changed()
        self._opened_symbols_this_cycle = set()
        cycle_start_ts = time.time()
        schedule_cfg = self.config.get("schedule", {}) or {}
        max_cycle_runtime_seconds = max(
            0.0,
            self._to_float(schedule_cfg.get("max_cycle_runtime_seconds", 0), 0.0),
        )
        symbol_stagger_seconds = max(
            0.0,
            self._to_float(schedule_cfg.get("symbol_stagger_seconds", 0), 0.0),
        )
        now_ts = time.time()
        sla_cfg = self._protection_sla_config()
        ff_cfg = self.config.get("fund_flow", {}) or {}
        ai_review_cfg = self._ai_review_config()
        max_active_symbols = max(1, int(ff_cfg.get("max_active_symbols", 3) or 3))
        max_symbol_position_portion = self._normalize_percent_to_ratio(
            ff_cfg.get("max_symbol_position_portion", 0.6),
            0.6,
        )
        ai_gate_enabled = self._is_ai_gate_enabled()
        add_position_portion = self._normalize_percent_to_ratio(
            ff_cfg.get("add_position_portion", ff_cfg.get("default_target_portion", 0.2)),
            0.2,
        )
        pending_new_entries: List[Dict[str, Any]] = []
        block_new_entries_due_to_protection_gap = False
        protection_gap_symbols: List[str] = []
        repair_fail_reduce_ratio = 1.0
        immediate_close_on_repair_fail = bool(sla_cfg.get("immediate_close_on_repair_fail", False))
        all_symbols = ConfigLoader.get_trading_symbols(self.config)
        position_snapshot = self._position_snapshot_by_symbol(all_symbols)
        if allow_new_entries:
            symbols = self._symbols_for_current_cycle(all_symbols, set(position_snapshot.keys()))
            if len(symbols) < len(all_symbols):
                print(
                    "📉 轮询降载: "
                    f"selected={len(symbols)}/{len(all_symbols)}, "
                    f"rotation_offset={self._symbol_rotation_offset}"
                )
            # 仅在允许新开仓窗口清理“无仓残留保护单”，避免影响开仓。
            self._cleanup_stale_protection_orders(symbols)
        else:
            symbols = [s for s in all_symbols if str(s).upper() in set(position_snapshot.keys())]
            if not symbols:
                print("⏭️ 非开仓窗口且当前无持仓，跳过本轮。")
                return
            print(f"📌 非开仓窗口仅检查持仓: {', '.join(symbols)}")
        account_summary = self.account_data.get_account_summary()
        if not account_summary:
            print("⚠️ 账户信息不可用，跳过本轮")
            return
        risk_guard = self._refresh_account_risk_guard(account_summary)
        risk_guard_enabled = bool(risk_guard.get("enabled", True))
        if risk_guard.get("blocked"):
            print(
                "⏳ 账户级风控冷却中："
                f"remaining={risk_guard.get('remaining_seconds')}s, "
                f"reason={risk_guard.get('reason')}"
            )
        if not allow_new_entries:
            print("⏱️ 非开仓窗口：本轮仅评估平仓/持仓风控（跳过BUY/SELL/DCA）")

        return {
            "cycle_start_ts": cycle_start_ts,
            "max_cycle_runtime_seconds": max_cycle_runtime_seconds,
            "symbol_stagger_seconds": symbol_stagger_seconds,
            "now_ts": now_ts,
            "sla_cfg": sla_cfg,
            "ff_cfg": ff_cfg,
            "ai_review_cfg": ai_review_cfg,
            "ai_review_mode": str(ai_review_mode or "disabled"),
            "max_active_symbols": max_active_symbols,
            "max_symbol_position_portion": max_symbol_position_portion,
            "ai_gate_enabled": ai_gate_enabled,
            "add_position_portion": add_position_portion,
            "pending_new_entries": pending_new_entries,
            "block_new_entries_due_to_protection_gap": block_new_entries_due_to_protection_gap,
            "protection_gap_symbols": protection_gap_symbols,
            "repair_fail_reduce_ratio": repair_fail_reduce_ratio,
            "immediate_close_on_repair_fail": immediate_close_on_repair_fail,
            "all_symbols": all_symbols,
            "position_snapshot": position_snapshot,
            "allow_new_entries": allow_new_entries,
            "symbols": symbols,
            "account_summary": account_summary,
            "risk_guard_enabled": risk_guard_enabled,
        }

    def _prepare_symbol_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        symbols_raw = context.get("symbols")
        symbols = symbols_raw if isinstance(symbols_raw, list) else []
        symbol_stagger_seconds = max(0.0, self._to_float(context.get("symbol_stagger_seconds"), 0.0))
        now_ts = self._to_float(context.get("now_ts"), time.time())
        sla_cfg_raw = context.get("sla_cfg")
        sla_cfg = sla_cfg_raw if isinstance(sla_cfg_raw, dict) else {}
        ff_cfg_raw = context.get("ff_cfg")
        ff_cfg = ff_cfg_raw if isinstance(ff_cfg_raw, dict) else {}
        ai_review_cfg_raw = context.get("ai_review_cfg")
        ai_review_cfg = ai_review_cfg_raw if isinstance(ai_review_cfg_raw, dict) else {}
        ai_review_mode = str(context.get("ai_review_mode") or "disabled")
        max_active_symbols = max(1, int(self._to_float(context.get("max_active_symbols"), 3)))
        max_symbol_position_portion = self._normalize_percent_to_ratio(
            context.get("max_symbol_position_portion", 0.6),
            0.6,
        )
        add_position_portion = self._normalize_percent_to_ratio(context.get("add_position_portion", 0.2), 0.2)
        repair_fail_reduce_ratio = self._to_float(context.get("repair_fail_reduce_ratio"), 1.0)
        immediate_close_on_repair_fail = bool(context.get("immediate_close_on_repair_fail", False))
        allow_new_entries = bool(context.get("allow_new_entries", True))
        risk_guard_enabled = bool(context.get("risk_guard_enabled", True))
        account_summary_raw = context.get("account_summary")
        account_summary = account_summary_raw if isinstance(account_summary_raw, dict) else {}
        position_snapshot_raw = context.get("position_snapshot")
        position_snapshot = position_snapshot_raw if isinstance(position_snapshot_raw, dict) else {}

        pending_new_entries_raw = context.get("pending_new_entries")
        pending_new_entries = pending_new_entries_raw if isinstance(pending_new_entries_raw, list) else []
        if not isinstance(pending_new_entries_raw, list):
            context["pending_new_entries"] = pending_new_entries

        protection_gap_symbols_raw = context.get("protection_gap_symbols")
        protection_gap_symbols = protection_gap_symbols_raw if isinstance(protection_gap_symbols_raw, list) else []
        if not isinstance(protection_gap_symbols_raw, list):
            context["protection_gap_symbols"] = protection_gap_symbols

        block_new_entries_due_to_protection_gap = bool(
            context.get("block_new_entries_due_to_protection_gap", False)
        )

        return {
            "symbols": symbols,
            "symbol_stagger_seconds": symbol_stagger_seconds,
            "now_ts": now_ts,
            "sla_cfg": sla_cfg,
            "ff_cfg": ff_cfg,
            "ai_review_cfg": ai_review_cfg,
            "ai_review_mode": ai_review_mode,
            "max_active_symbols": max_active_symbols,
            "max_symbol_position_portion": max_symbol_position_portion,
            "add_position_portion": add_position_portion,
            "repair_fail_reduce_ratio": repair_fail_reduce_ratio,
            "immediate_close_on_repair_fail": immediate_close_on_repair_fail,
            "allow_new_entries": allow_new_entries,
            "risk_guard_enabled": risk_guard_enabled,
            "account_summary": account_summary,
            "position_snapshot": position_snapshot,
            "pending_new_entries": pending_new_entries,
            "protection_gap_symbols": protection_gap_symbols,
            "block_new_entries_due_to_protection_gap": block_new_entries_due_to_protection_gap,
        }


    def _handle_symbol_protection_and_sla(
        self,
        symbol: str,
        position: Any,
        current_price: float,
        now_ts: float,
        sla_cfg: Dict[str, Any],
        repair_fail_reduce_ratio: float,
        immediate_close_on_repair_fail: bool,
        block_new_entries_due_to_protection_gap: bool,
        protection_gap_symbols: List[str],
    ) -> Tuple[bool, bool]:
        if position is None:
            return False, block_new_entries_due_to_protection_gap

        completed_without_skip = False
        for _ in (0,):
            if bool(position.get("hedge_conflict")) and isinstance(position.get("legs"), list):
                print(f"⚠️ {symbol} 检测到账户双向持仓(hedge)，本轮跳过开/平决策，仅执行逐侧风控修复")
                block_new_entries_due_to_protection_gap = True
                if symbol not in protection_gap_symbols:
                    protection_gap_symbols.append(symbol)
                self._clear_dca_tracking_for_symbol(symbol)
                valid_keys = {
                    self._position_track_key(symbol, "LONG"),
                    self._position_track_key(symbol, "SHORT"),
                }
                prefix = f"{str(symbol).upper()}:"
                for store in (
                    self._position_first_seen_ts,
                    self._position_last_direction_eval_ts,
                    self._protection_missing_since_ts,
                    self._protection_last_alert_ts,
                ):
                    stale_keys = [k for k in list(store.keys()) if k.startswith(prefix) and k not in valid_keys]
                    for key in stale_keys:
                        store.pop(key, None)
            
                for leg in position.get("legs", []):
                    if not isinstance(leg, dict):
                        continue
                    side = str(leg.get("side", "")).upper()
                    if side not in ("LONG", "SHORT"):
                        continue
                    leg_position = dict(leg)
                    leg_position["side"] = side
                    pos_key = self._position_track_key(symbol, side)
                    if pos_key not in self._position_first_seen_ts:
                        self._position_first_seen_ts[pos_key] = now_ts
                    self._update_position_extrema(symbol, leg_position, current_price)
            
                    coverage = self._protection_coverage(symbol, side=side)
                    covered = bool(coverage.get("has_tp")) and bool(coverage.get("has_sl"))
                    if covered:
                        self._protection_missing_since_ts.pop(pos_key, None)
                        self._protection_last_alert_ts.pop(pos_key, None)
                        continue
            
                    if pos_key not in self._protection_missing_since_ts:
                        self._protection_missing_since_ts[pos_key] = now_ts
                    print(
                        f"🚨 {symbol}({side}) 检测到持仓缺少保护单: "
                        f"has_tp={coverage.get('has_tp')} has_sl={coverage.get('has_sl')}"
                    )
                    repair = self._repair_missing_protection(symbol, leg_position)
                    print(
                        f"   🛠️ ({side}) 补挂保护单结果: status={repair.get('status')} "
                        f"msg={repair.get('message')}"
                    )
                    coverage_after = self._protection_coverage(symbol, side=side)
                    covered_after = bool(coverage_after.get("has_tp")) and bool(coverage_after.get("has_sl"))
                    if covered_after:
                        self._protection_missing_since_ts.pop(pos_key, None)
                        self._protection_last_alert_ts.pop(pos_key, None)
                        if str(repair.get("status", "")).lower() == "success":
                            print(f"   ✅ {symbol}({side}) 保护单补挂完成，SLA恢复正常")
                        else:
                            print(f"   ℹ️ {symbol}({side}) 检测到保护单已就绪（跳过本次补挂）")
                        continue
            
                    if str(repair.get("status", "")).lower() != "success":
                        if immediate_close_on_repair_fail:
                            close_res = self._emergency_flatten_unprotected(
                                symbol,
                                leg_position,
                                reduce_ratio=repair_fail_reduce_ratio,
                            )
                            print(
                                f"   🧯 ({side}) 保护单补挂失败，触发强制减仓/平仓(ratio={repair_fail_reduce_ratio:.2f}): "
                                f"status={close_res.get('status')} detail={close_res.get('message') or close_res.get('order')}"
                            )
                            self._emit_protection_sla_alert(
                                symbol=symbol,
                                side=side,
                                detail="protection_repair_failed_immediate_flatten",
                                extra={"repair": repair, "flatten": close_res},
                            )
                            continue
                        print(f"   ⚠️ ({side}) 保护单补挂失败，已按配置跳过立即强平，继续SLA监控")
                        self._emit_protection_sla_alert(
                            symbol=symbol,
                            side=side,
                            detail="protection_repair_failed_no_immediate_close",
                            extra={"repair": repair},
                        )
            
                    first_seen = self._position_first_seen_ts.get(pos_key, now_ts)
                    missing_since = self._protection_missing_since_ts.get(pos_key, now_ts)
                    elapsed_from_open = max(0, int(now_ts - first_seen))
                    elapsed_missing = max(0, int(now_ts - missing_since))
                    timeout_s = int(sla_cfg.get("timeout_seconds", 60))
                    remain = max(0, timeout_s - elapsed_from_open)
                    print(
                        f"   ⏱️ ({side}) SLA监控: elapsed_from_open={elapsed_from_open}s, "
                        f"missing_for={elapsed_missing}s, timeout={timeout_s}s, remain={remain}s"
                    )
            
                    if bool(sla_cfg.get("enabled", True)) and elapsed_from_open >= timeout_s:
                        should_alert = True
                        last_alert = self._protection_last_alert_ts.get(pos_key, 0.0)
                        alert_cd = int(sla_cfg.get("alert_cooldown_seconds", 30))
                        if now_ts - last_alert < alert_cd:
                            should_alert = False
                        if should_alert:
                            self._protection_last_alert_ts[pos_key] = now_ts
                            self._emit_protection_sla_alert(
                                symbol=symbol,
                                side=side,
                                detail="protection_sla_breached",
                                extra={
                                    "elapsed_from_open": elapsed_from_open,
                                    "elapsed_missing": elapsed_missing,
                                    "timeout_seconds": timeout_s,
                                    "coverage_before": coverage,
                                    "coverage_after": coverage_after,
                                    "repair": repair,
                                },
                            )
            
                        if bool(sla_cfg.get("force_flatten_on_breach", True)):
                            close_res = self._emergency_flatten_unprotected(
                                symbol,
                                leg_position,
                                reduce_ratio=repair_fail_reduce_ratio,
                            )
                            print(
                                f"   🧯 ({side}) SLA超时强平: status={close_res.get('status')} "
                                f"detail={close_res.get('message') or close_res.get('order')}"
                            )
                            self._emit_protection_sla_alert(
                                symbol=symbol,
                                side=side,
                                detail="protection_sla_force_flatten",
                                extra={"flatten": close_res},
                            )
                continue
            
            side = str(position.get("side", "")).upper()
            pos_key = self._position_track_key(symbol, side or "BOTH")
            self._clear_sla_tracking_for_symbol(symbol, keep_key=pos_key)
            self._clear_dca_tracking_for_symbol(symbol, keep_key=pos_key)
            if pos_key not in self._position_first_seen_ts:
                self._position_first_seen_ts[pos_key] = now_ts
            self._update_position_extrema(symbol, position, current_price)
            
            coverage = self._protection_coverage(symbol, side=side)
            covered = bool(coverage.get("has_tp")) and bool(coverage.get("has_sl"))
            if covered:
                self._protection_missing_since_ts.pop(pos_key, None)
                self._protection_last_alert_ts.pop(pos_key, None)
            if not covered:
                if pos_key not in self._protection_missing_since_ts:
                    self._protection_missing_since_ts[pos_key] = now_ts
                print(
                    f"🚨 {symbol} 检测到持仓缺少保护单: "
                    f"has_tp={coverage.get('has_tp')} has_sl={coverage.get('has_sl')}"
                )
                repair = self._repair_missing_protection(symbol, position)
                print(
                    f"   🛠️ 补挂保护单结果: status={repair.get('status')} "
                    f"msg={repair.get('message')}"
                )
                coverage_after = self._protection_coverage(symbol, side=side)
                covered_after = bool(coverage_after.get("has_tp")) and bool(coverage_after.get("has_sl"))
                if covered_after:
                    self._protection_missing_since_ts.pop(pos_key, None)
                    self._protection_last_alert_ts.pop(pos_key, None)
                    if str(repair.get("status", "")).lower() == "success":
                        print(f"   ✅ {symbol} 保护单补挂完成，SLA恢复正常")
                    else:
                        print(f"   ℹ️ {symbol} 检测到保护单已就绪（跳过本次补挂）")
                    continue
            
                block_new_entries_due_to_protection_gap = True
                if symbol not in protection_gap_symbols:
                    protection_gap_symbols.append(symbol)
            
                if str(repair.get("status", "")).lower() != "success":
                    if immediate_close_on_repair_fail:
                        close_res = self._emergency_flatten_unprotected(
                            symbol,
                            position,
                            reduce_ratio=repair_fail_reduce_ratio,
                        )
                        print(
                            f"   🧯 保护单补挂失败，触发强制减仓/平仓(ratio={repair_fail_reduce_ratio:.2f}): "
                            f"status={close_res.get('status')} detail={close_res.get('message') or close_res.get('order')}"
                        )
                        self._emit_protection_sla_alert(
                            symbol=symbol,
                            side=side,
                            detail="protection_repair_failed_immediate_flatten",
                            extra={"repair": repair, "flatten": close_res},
                        )
                        continue
                    print("   ⚠️ 保护单补挂失败，已按配置跳过立即强平，继续SLA监控")
                    self._emit_protection_sla_alert(
                        symbol=symbol,
                        side=side,
                        detail="protection_repair_failed_no_immediate_close",
                        extra={"repair": repair},
                    )
            
                first_seen = self._position_first_seen_ts.get(pos_key, now_ts)
                missing_since = self._protection_missing_since_ts.get(pos_key, now_ts)
                elapsed_from_open = max(0, int(now_ts - first_seen))
                elapsed_missing = max(0, int(now_ts - missing_since))
                timeout_s = int(sla_cfg.get("timeout_seconds", 60))
                remain = max(0, timeout_s - elapsed_from_open)
                print(
                    f"   ⏱️ SLA监控: elapsed_from_open={elapsed_from_open}s, "
                    f"missing_for={elapsed_missing}s, timeout={timeout_s}s, remain={remain}s"
                )
            
                if bool(sla_cfg.get("enabled", True)) and elapsed_from_open >= timeout_s:
                    should_alert = True
                    last_alert = self._protection_last_alert_ts.get(pos_key, 0.0)
                    alert_cd = int(sla_cfg.get("alert_cooldown_seconds", 30))
                    if now_ts - last_alert < alert_cd:
                        should_alert = False
                    if should_alert:
                        self._protection_last_alert_ts[pos_key] = now_ts
                        self._emit_protection_sla_alert(
                            symbol=symbol,
                            side=side,
                            detail="protection_sla_breached",
                            extra={
                                "elapsed_from_open": elapsed_from_open,
                                "elapsed_missing": elapsed_missing,
                                "timeout_seconds": timeout_s,
                                "coverage_before": coverage,
                                "coverage_after": coverage_after,
                                "repair": repair,
                            },
                        )
            
                    if bool(sla_cfg.get("force_flatten_on_breach", True)):
                        close_res = self._emergency_flatten_unprotected(
                            symbol,
                            position,
                            reduce_ratio=repair_fail_reduce_ratio,
                        )
                        print(
                            f"   🧯 SLA超时强平: status={close_res.get('status')} "
                            f"detail={close_res.get('message') or close_res.get('order')}"
                        )
                        self._emit_protection_sla_alert(
                            symbol=symbol,
                            side=side,
                            detail="protection_sla_force_flatten",
                            extra={"flatten": close_res},
                        )
                # 风险修复优先，本轮不再对该 symbol 发起新决策
                continue
            completed_without_skip = True

        return (not completed_without_skip), block_new_entries_due_to_protection_gap

    def _execute_symbol_signal_decision(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        position: Any,
        current_price: float,
        account_summary: Dict[str, Any],
        pending_new_entries: List[Dict[str, Any]],
        protection_gap_symbols: List[str],
        block_new_entries_due_to_protection_gap: bool,
        allow_new_entries: bool,
        ff_cfg: Dict[str, Any],
        max_active_symbols: int,
        max_symbol_position_portion: float,
        add_position_portion: float,
        risk_guard_enabled: bool,
        ai_review_mode: str = "disabled",
        ai_review_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        for _ in (0,):
            raw_flow_context = self._build_fund_flow_context(symbol, market_data)
            flow_snapshot = self.fund_flow_ingestion_service.aggregate_from_metrics(symbol=symbol, metrics=raw_flow_context)
            flow_context = self._apply_timeframe_context(raw_flow_context, flow_snapshot)
            volatility_guard = self._update_extreme_volatility_state(symbol, flow_context)
            
            self._safe_storage_call(
                "upsert_market_flow",
                exchange=flow_snapshot.exchange,
                symbol=flow_snapshot.symbol,
                timestamp=flow_snapshot.timestamp,
                metrics=flow_snapshot.to_dict(),
            )
            if position is None and bool(volatility_guard.get("blocked")):
                print(
                    f"⏭️ {symbol} 极端波动冷却中，跳过新开仓: "
                    f"remaining={int(volatility_guard.get('remaining_seconds', 0) or 0)}s, "
                    f"atr_pct={self._to_float(volatility_guard.get('atr_pct'), 0.0):.4f}, "
                    f"threshold={self._to_float(volatility_guard.get('threshold'), 0.0):.4f}, "
                    f"tf={volatility_guard.get('timeframe')}"
                )
                continue
            
            trigger_type = "signal" if flow_snapshot.signal_strength > 0 else "scheduled"
            trigger_id = f"{symbol}:{flow_snapshot.timestamp.isoformat()}"
            if not self.fund_flow_trigger_engine.should_trigger(
                symbol=symbol,
                trigger_type=trigger_type,
                trigger_id=trigger_id,
            ):
                print(f"⏭️ {symbol} 触发去重命中，跳过本轮。trigger_id={trigger_id}")
                continue
            
            positions_payload: Dict[str, Any] = {}
            if isinstance(position, dict):
                positions_payload[symbol] = {
                    "side": position.get("side"),
                    "amount": position.get("amount"),
                    "entry_price": position.get("entry_price"),
                }
            portfolio = {
                "cash": self._to_float(account_summary.get("available_balance"), 0.0),
                "positions": positions_payload,
                "total_assets": self._to_float(account_summary.get("equity"), 0.0),
            }
            trigger_context = {"trigger_type": trigger_type, "signal_pool_id": None}

            confluence_cfg = self._ma10_macd_confluence_config()
            confluence: Dict[str, Any] = {}
            if bool(confluence_cfg.get("enabled", True)):
                try:
                    confluence = self._compute_ma10_macd_confluence(symbol, confluence_cfg)
                    self._inject_confluence_into_flow_context(flow_context, confluence, confluence_cfg)
                except Exception as e:
                    print(f"⚠️ {symbol} MA10+MACD/KDJ/布林 帧内特征注入失败: {e}")
            
            decision = self.fund_flow_decision_engine.decide(
                symbol=symbol,
                portfolio=portfolio,
                price=current_price,
                market_flow_context=flow_context,
                trigger_context=trigger_context,
                use_weight_router=False,
                use_ai_weights=False,
            )
            ai_review_cfg = ai_review_cfg if isinstance(ai_review_cfg, dict) else {}
            ai_review_enabled = bool(ai_review_cfg.get("enabled", True))
            if (
                ai_review_enabled
                and self._is_ai_gate_enabled()
                and isinstance(position, dict)
                and str(ai_review_mode or "").lower() == "positions"
            ):
                ai_trigger_context = dict(trigger_context)
                ai_trigger_context["ai_gate"] = "position_review"
                ai_trigger_context["local_operation"] = decision.operation.value
                ai_decision = self.fund_flow_decision_engine.decide(
                    symbol=symbol,
                    portfolio=portfolio,
                    price=current_price,
                    market_flow_context=flow_context,
                    trigger_context=ai_trigger_context,
                    use_weight_router=True,
                    use_ai_weights=True,
                )
                ai_md_raw = getattr(ai_decision, "metadata", None)
                ai_md = ai_md_raw if isinstance(ai_md_raw, dict) else {}
                ai_source = str(ai_md.get("ds_source") or "-")
                ai_conf = self._to_float(ai_md.get("ds_confidence"), 0.0)
                print(
                    f"🤖 {symbol} 持仓AI复核: local={decision.operation.value.upper()} "
                    f"ai={ai_decision.operation.value.upper()} source={ai_source} conf={ai_conf:.3f}"
                )
                decision = ai_decision
            decision_md_raw = getattr(decision, "metadata", None)
            decision_md: Dict[str, Any] = decision_md_raw if isinstance(decision_md_raw, dict) else {}
            if (not allow_new_entries) and decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL):
                if not isinstance(position, dict):
                    print(f"⏭️ {symbol} 非开仓窗口且无持仓，跳过开仓/加仓信号")
                    continue
                signal_side = "LONG" if decision.operation == FundFlowOperation.BUY else "SHORT"
                current_side = str(position.get("side", "")).upper()
                reason = f"非开仓窗口降级为HOLD（signal={signal_side}, position={current_side or 'NA'}）"
                decision = FundFlowDecision(
                    operation=FundFlowOperation.HOLD,
                    symbol=symbol,
                    target_portion_of_balance=0.0,
                    leverage=decision.leverage,
                    reason=reason,
                    metadata=decision_md,
                )
                print(f"⏭️ {symbol} 非开仓窗口，开仓信号降级为HOLD并继续执行持仓风控")
                decision_md = decision.metadata if isinstance(decision.metadata, dict) else decision_md
            
            if confluence:
                try:
                    decision_md.update(confluence)
                    if not isinstance(getattr(decision, "metadata", None), dict):
                        decision.metadata = decision_md
            
                    # DecisionEngine owns trigger scoring. Bot side only keeps raw values
                    # for diagnostics and lets the hard entry filter enforce safety.
                    long_raw = self._to_float(decision_md.get("long_score"), 0.0)
                    short_raw = self._to_float(decision_md.get("short_score"), 0.0)
                    decision_md["long_score_raw"] = float(long_raw)
                    decision_md["short_score_raw"] = float(short_raw)
                    decision_md["long_score_adj"] = float(long_raw)
                    decision_md["short_score_adj"] = float(short_raw)
                    decision_md["ma10_macd_score_delta"] = {
                        "long": 0.0,
                        "short": 0.0,
                        "disabled": True,
                    }
                except Exception as e:
                    print(f"⚠️ {symbol} MA10+MACD 共振特征计算失败: {e}")
            engine_override_raw = decision_md.get("params_override")
            engine_override: Dict[str, Any] = (
                engine_override_raw if isinstance(engine_override_raw, dict) else {}
            )
            engine_tag_now = str(decision_md.get("engine") or decision_md.get("regime") or "").upper()
            base_pool_id = str(
                decision_md.get("signal_pool_id")
                or decision_md.get("selected_pool_id")
                or ff_cfg.get("active_signal_pool_id")
                or ""
            ).strip()
            selected_pool_id = base_pool_id
            if engine_tag_now == "TREND":
                major_pool_raw = ff_cfg.get("major_symbol_signal_pool")
                major_pool_cfg = major_pool_raw if isinstance(major_pool_raw, dict) else {}
                if major_pool_cfg and self._to_bool(major_pool_cfg.get("enabled", False), False):
                    major_symbols_raw = major_pool_cfg.get("symbols")
                    major_symbols = {
                        str(s).strip().upper()
                        for s in (major_symbols_raw if isinstance(major_symbols_raw, list) else [])
                        if str(s).strip()
                    }
                    major_pool_id = str(major_pool_cfg.get("trend_pool_id") or "").strip()
                    if major_pool_id and symbol.upper() in major_symbols:
                        selected_pool_id = major_pool_id
            runtime_pool_cfg = self._resolve_runtime_signal_pool_config(selected_pool_id)
            if (
                selected_pool_id != base_pool_id
                and (not isinstance(runtime_pool_cfg, dict) or not runtime_pool_cfg)
            ):
                selected_pool_id = base_pool_id
                runtime_pool_cfg = self._resolve_runtime_signal_pool_config(selected_pool_id)
            if engine_tag_now == "RANGE":
                edge_cd_default = int(self._to_float(ff_cfg.get("trigger_dedupe_seconds"), 30.0))
                edge_cd = edge_cd_default
                if isinstance(runtime_pool_cfg, dict) and runtime_pool_cfg:
                    edge_cd = max(
                        0,
                        int(
                            self._to_float(
                                runtime_pool_cfg.get("edge_cooldown_seconds"),
                                float(edge_cd_default),
                            )
                        ),
                    )
                dynamic_pool_id = selected_pool_id or "range_quantile_pool"
                trigger_context["signal_pool_id"] = dynamic_pool_id
                # RANGE 开仓由 DecisionEngine 分位数门控决定；这里仅保留冷却去抖。
                selected_pool_cfg = {
                    "enabled": True,
                    "pool_id": dynamic_pool_id,
                    "id": dynamic_pool_id,
                    "logic": "OR",
                    "min_pass_count": 1,
                    "min_long_score": 0.0,
                    "min_short_score": 0.0,
                    "scheduled_trigger_bypass": False,
                    "apply_when_position_exists": False,
                    "edge_trigger_enabled": True,
                    "edge_cooldown_seconds": edge_cd,
                    "rules": [
                        {
                            "name": "range_dynamic_long_gate",
                            "side": "LONG",
                            "metric": "long_score",
                            "operator": ">=",
                            "threshold": 0.0,
                        },
                        {
                            "name": "range_dynamic_short_gate",
                            "side": "SHORT",
                            "metric": "short_score",
                            "operator": ">=",
                            "threshold": 0.0,
                        },
                    ],
                }
            else:
                selected_pool_cfg = runtime_pool_cfg if isinstance(runtime_pool_cfg, dict) else {}
                if isinstance(selected_pool_cfg, dict) and selected_pool_cfg:
                    trigger_context["signal_pool_id"] = (
                        selected_pool_cfg.get("pool_id")
                        or selected_pool_cfg.get("id")
                        or selected_pool_id
                    )
                else:
                    trigger_context["signal_pool_id"] = selected_pool_id or None
            pool_eval = self.fund_flow_trigger_engine.evaluate_signal_pool(
                symbol=symbol,
                trigger_type=trigger_type,
                market_flow_context=flow_context,
                decision=decision,
                has_position=isinstance(position, dict),
                signal_pool_config=selected_pool_cfg if isinstance(selected_pool_cfg, dict) else None,
            )
            if decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL):
                if not bool(pool_eval.get("passed", True)):
                    edge_raw = pool_eval.get("edge")
                    edge_obj: Dict[str, Any] = edge_raw if isinstance(edge_raw, dict) else {}
                    print(
                        f"⏭️ {symbol} signal_pool过滤未通过，跳过开仓/加仓: "
                        f"pool={trigger_context.get('signal_pool_id')}, "
                        f"reason={pool_eval.get('reason')}, "
                        f"edge={edge_obj.get('reason')}, "
                        f"score={self._to_float(pool_eval.get('score'), 0.0):.3f}"
                    )
                    continue
            
            decision = self._apply_ma10_macd_entry_filter(symbol, decision)
            decision_md_candidate = getattr(decision, "metadata", None)
            if isinstance(decision_md_candidate, dict):
                decision_md = decision_md_candidate
            decision, gate_meta = self._apply_pretrade_risk_gate(
                symbol=symbol,
                decision=decision,
                position=position,
                flow_context=flow_context,
                current_price=current_price,
                account_summary=account_summary,
            )
            gate_action = str(gate_meta.get("action", "BYPASS")).upper()
            if gate_action in ("HOLD", "EXIT", "ERROR"):
                extra = ""
                if gate_action == "EXIT":
                    extra = (
                        f", streak={int(self._to_float(gate_meta.get('exit_streak'), 0)):d}, "
                        f"confirmed={1 if bool(gate_meta.get('exit_confirmed', False)) else 0}, "
                        f"hold={int(self._to_float(gate_meta.get('exit_hold_seconds'), 0)):d}s"
                    )
                print(
                    f"🧭 {symbol} 前置风控Gate: action={gate_action}, "
                    f"score={self._to_float(gate_meta.get('score'), 0.0):.3f}{extra}"
                )
            if risk_guard_enabled and self._is_cooldown_active() and decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL):
                print(
                    f"⏭️ {symbol} 账户级冷却中，阻止新开仓 "
                    f"(remaining={self._cooldown_remaining_seconds()}s, reason={self._cooldown_reason})"
                )
                continue
            if isinstance(position, dict):
                current_side = str(position.get("side", "")).upper()
                current_portion = self._estimate_position_portion(position, account_summary)
                min_open_portion = max(0.01, float(getattr(self.fund_flow_risk_engine, "min_open_portion", 0.1) or 0.1))
                local_max_symbol_position_portion = self._normalize_percent_to_ratio(
                    engine_override.get("max_symbol_position_portion", max_symbol_position_portion),
                    max_symbol_position_portion,
                )
                local_add_position_portion = self._normalize_percent_to_ratio(
                    engine_override.get("add_position_portion", add_position_portion),
                    add_position_portion,
                )
                dca_cfg_local = self._dca_config(engine_override)
            
                if decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL):
                    signal_side = "LONG" if decision.operation == FundFlowOperation.BUY else "SHORT"
                    if current_side != signal_side:
                        print(
                            f"⏭️ {symbol} 已有反向持仓({current_side})，当前策略不做同周期反手，跳过开仓信号"
                        )
                        continue
            
                # DCA/马丁模式：已有持仓时仅按回撤阈值+阶梯倍数触发加仓
                if bool(dca_cfg_local.get("enabled")) and decision.operation != FundFlowOperation.CLOSE:
                    dca_decision = self._build_dca_decision(
                        symbol=symbol,
                        position=position,
                        current_price=current_price,
                        base_decision=decision,
                        trigger_context=trigger_context,
                        dca_cfg=dca_cfg_local,
                    )
                    if dca_decision is not None:
                        decision = dca_decision
                    elif decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL):
                        drawdown = self._position_drawdown_ratio(position, current_price)
                        reason = (
                            f"DCA未触发，保持观望 drawdown={drawdown:.4f}, "
                            f"next_stage={int(self._dca_stage_by_pos.get(self._position_track_key(symbol, current_side), 0) or 0) + 1}"
                        )
                        decision = FundFlowDecision(
                            operation=FundFlowOperation.HOLD,
                            symbol=symbol,
                            target_portion_of_balance=0.0,
                            leverage=decision.leverage,
                            reason=reason,
                            metadata=decision.metadata if isinstance(decision.metadata, dict) else {},
                        )
            
                # ========== 冲突保护检查（只要有持仓就检查；但不覆盖已确定的 CLOSE） ==========
                if current_side in ("LONG", "SHORT") and current_portion > 0 and decision.operation != FundFlowOperation.CLOSE:
                    # 定期打印统计摘要（不影响逻辑）
                    self._maybe_log_conflict_protection_stats(interval_sec=600.0)

                    tf_ctx_all = flow_context.get("timeframes", {}) if isinstance(flow_context, dict) else {}
                    tf_1m = tf_ctx_all.get("1m", {}) if isinstance(tf_ctx_all, dict) else {}
                    tf_3m = tf_ctx_all.get("3m", {}) if isinstance(tf_ctx_all, dict) else {}
                    tf_5m = tf_ctx_all.get("5m", {}) if isinstance(tf_ctx_all, dict) else {}

                    def _tf_dir_score(tf_ctx: Dict[str, Any]) -> float:
                        if not isinstance(tf_ctx, dict):
                            return 0.0
                        macd_v = self._to_float(tf_ctx.get("macd_hist_norm"), 0.0)
                        kdj_v = self._to_float(tf_ctx.get("kdj_j_norm"), 0.0)
                        if abs(kdj_v) < 1e-9:
                            kdj_raw = self._to_float(tf_ctx.get("kdj_j"), 50.0)
                            kdj_v = (kdj_raw - 50.0) / 50.0
                        bb_pos = self._to_float(tf_ctx.get("bb_pos_norm"), 0.0)
                        if abs(bb_pos) < 1e-9:
                            bb_u = self._to_float(tf_ctx.get("bb_upper"), 0.0)
                            bb_l = self._to_float(tf_ctx.get("bb_lower"), 0.0)
                            c = self._to_float(tf_ctx.get("last_close"), self._to_float(tf_ctx.get("mid_price"), 0.0))
                            if bb_u > bb_l and c > 0:
                                half = max((bb_u - bb_l) * 0.5, 1e-12)
                                bb_pos = (c - (bb_u + bb_l) * 0.5) / half
                        return max(-1.0, min(1.0, 0.55 * macd_v + 0.25 * kdj_v + 0.20 * bb_pos))

                    mtf_scores = {
                        "1m": _tf_dir_score(tf_1m),
                        "3m": _tf_dir_score(tf_3m),
                        "5m": _tf_dir_score(tf_5m),
                    }
                    energy_now = max(
                        0.0,
                        min(
                            1.0,
                            0.45 * abs(self._to_float(decision_md.get("macd_hist_norm"), 0.0))
                            + 0.35 * abs(self._to_float(decision_md.get("cvd_norm"), 0.0))
                            + 0.20 * abs(0.25 * mtf_scores["1m"] + 0.30 * mtf_scores["3m"] + 0.45 * mtf_scores["5m"]),
                        ),
                    )

                    bb_upper_5m = self._to_float(tf_5m.get("bb_upper"), 0.0)
                    bb_lower_5m = self._to_float(tf_5m.get("bb_lower"), 0.0)
                    bb_middle_5m = self._to_float(tf_5m.get("bb_middle"), self._to_float(decision_md.get("ma10_5m"), 0.0))
                    close_5m = self._to_float(
                        decision_md.get("last_close_5m"),
                        self._to_float(tf_5m.get("last_close"), self._to_float(tf_5m.get("mid_price"), current_price)),
                    )
                    trap_now = self._to_float(
                        flow_context.get("trap_score") if isinstance(flow_context, dict) else None,
                        self._to_float(tf_5m.get("trap_last"), 0.0),
                    )
            
                    # 获取KDJ J值
                    kdj_j_norm = self._to_float(decision_md.get("kdj_j_norm"), 0.0)
                    if abs(kdj_j_norm) < 1e-9:
                        kdj_j_raw = self._to_float(tf_5m.get("kdj_j"), 50.0)
                        kdj_j_norm = (kdj_j_raw - 50.0) / 50.0
                    
                    # 获取平仓决断权重配置
                    risk_cfg_local = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
                    conflict_cfg_local = (
                        risk_cfg_local.get("conflict_protection", {})
                        if isinstance(risk_cfg_local.get("conflict_protection"), dict)
                        else {}
                    )
                    close_decision_weights = {
                        "fund_flow_weight": self._to_float(conflict_cfg_local.get("close_decision_fund_flow_weight"), 0.55),
                        "macd_weight": self._to_float(conflict_cfg_local.get("close_decision_macd_weight"), 0.30),
                        "kdj_weight": self._to_float(conflict_cfg_local.get("close_decision_kdj_weight"), 0.15),
                    }
                    
                    protection = self.risk_manager.check_position_protection(
                        symbol=symbol,
                        position_side=current_side,
                        macd_hist_norm=self._to_float(decision_md.get("macd_hist_norm"), 0.0),
                        cvd_norm=self._to_float(decision_md.get("cvd_norm"), 0.0),
                        kdj_j_norm=kdj_j_norm,
                        ev_direction=str(decision_md.get("ev_direction", "BOTH")),
                        ev_score=self._to_float(decision_md.get("ev_score"), 0.0),
                        lw_direction=str(decision_md.get("lw_direction", "BOTH")),
                        lw_score=self._to_float(decision_md.get("lw_score"), 0.0),
                        now_ts=time.time(),
                        market_regime=str(decision_md.get("engine") or decision_md.get("regime") or "").upper(),
                        ma10_ltf=self._to_float(decision_md.get("ma10_5m"), 0.0),
                        last_close=self._to_float(
                            decision_md.get("last_close_5m"),
                            self._to_float(decision_md.get("last_close"), 0.0),
                        ),
                        mtf_scores=mtf_scores,
                        energy=energy_now,
                        bb_upper=bb_upper_5m,
                        bb_lower=bb_lower_5m,
                        bb_middle=bb_middle_5m,
                        close_price=close_5m,
                        trap_score=trap_now,
                        direction_lock=str(decision_md.get("direction_lock", "") or ""),
                        close_decision_weights=close_decision_weights,
                    )
                    protection_level = protection.get("level", "neutral")
                    risk_state = str(protection.get("risk_state", "HOLD")).upper()
                    cooldown_active = bool(protection.get("cooldown_active", False))
                    protection_action = "none"
                    if protection_level == "conflict_hard":
                        protection_action = "reduce+breakeven"
                    elif protection_level == "conflict_light":
                        protection_action = "freeze_add+tighten" if bool(protection.get("tighten_trailing", False)) else "freeze_add_only"
                    gate_score_now = self._to_float(gate_meta.get("score"), 0.0)
                    pos_key_runtime = self._position_track_key(symbol, current_side)
                    first_seen_runtime = self._position_first_seen_ts.get(pos_key_runtime)
                    hold_seconds_runtime = (
                        max(0, int(time.time() - float(first_seen_runtime)))
                        if first_seen_runtime is not None
                        else 0
                    )
                    ext_runtime_raw = self._position_extrema_by_pos.get(pos_key_runtime)
                    ext_runtime: Dict[str, float] = ext_runtime_raw if isinstance(ext_runtime_raw, dict) else {}
                    mfe_runtime = max(0.0, float(ext_runtime.get("max_favorable_ratio", 0.0))) * 100.0
                    mae_runtime = min(0.0, float(ext_runtime.get("max_adverse_ratio", 0.0))) * 100.0
                    close_decision = protection.get("close_decision", {})
                    close_score = self._to_float(close_decision.get("score"), 0.0)
                    macd_score = self._to_float(close_decision.get("macd_score"), 0.0)
                    kdj_score = self._to_float(close_decision.get("kdj_score"), 0.0)
                    ff_score = self._to_float(close_decision.get("fund_flow_score"), 0.0)
                    close_weights = close_decision.get("weights", {})
                    print(
                        "🧪 风控摘要 "
                        f"symbol={symbol} engine={str(decision_md.get('engine') or decision_md.get('regime') or '-').upper()} "
                        f"side={current_side} entry={self._to_float(position.get('entry_price'), 0.0):.6f} "
                        f"atr={self._to_float(decision_md.get('regime_atr_pct'), 0.0):.4f} "
                        f"gate_score={gate_score_now:+.3f} protect={protection_level} "
                        f"state={risk_state} "
                        f"bars={int(protection.get('conflict_bars', 0) or 0)} "
                        f"pen={self._to_float(protection.get('penetration'), 0.0):+.2f} "
                        f"votes={int(self._to_int(protection.get('hard_votes'), 0))} "
                        f"hold={hold_seconds_runtime}s mfe={mfe_runtime:.2f}% mae={mae_runtime:.2f}% "
                        f"close_score={close_score:+.3f}(MACD:{macd_score:+.3f}/KDJ:{kdj_score:+.3f}/FF:{ff_score:+.3f}) "
                        f"weights=FF:{self._to_float(close_weights.get('fund_flow'), 0.55):.2f}/M:{self._to_float(close_weights.get('macd'), 0.30):.2f}/K:{self._to_float(close_weights.get('kdj'), 0.15):.2f} "
                        f"action={protection_action}"
                    )
            
                    # 把 allow_add 透传到 metadata，供后续"禁止加仓/禁止新开同向"逻辑使用
                    try:
                        decision_md["risk_protect_level"] = protection_level
                        decision_md["risk_allow_add"] = bool(protection.get("allow_add", True))
                        decision_md["risk_conflict_bars"] = int(protection.get("conflict_bars", 0))
                        decision_md["risk_penalty"] = float(protection.get("risk_penalty", 0.0))
                        decision_md["risk_state"] = risk_state
                        decision_md["risk_state_confirm_count"] = int(protection.get("state_confirm_count", 0))
                        decision_md["risk_state_energy"] = float(protection.get("state_energy", 0.0))
                        decision_md["risk_state_structure"] = str(protection.get("state_structure", "UNKNOWN"))
                        decision_md["risk_state_penetration"] = float(protection.get("penetration", 0.0) or 0.0)
                        decision_md["risk_state_hard_votes"] = int(protection.get("hard_votes", 0) or 0)
                        decision_md["risk_breakeven_mode"] = str(protection.get("breakeven_mode", "") or "")
                        decision_md["risk_breakeven_fee_buffer"] = float(protection.get("breakeven_fee_buffer", 0.0) or 0.0)
                    except Exception:
                        pass
            
                    if protection_level == "conflict_hard":
                        # 重度冲突：减仓、保本止损、禁止加仓
                        reduce_pct = float(protection.get("reduce_position_pct", 0.0))
                        k_open_hard = self._to_float(decision_md.get("last_open"), 0.0)
                        k_close_hard = self._to_float(decision_md.get("last_close"), 0.0)
                        price_change_hard = ((k_close_hard - k_open_hard) / k_open_hard) if k_open_hard > 0 else 0.0
                        gate_cfg_hard = self._pretrade_risk_gate_config()
                        hard_price_change_min = abs(self._to_float(gate_cfg_hard.get("exit_price_change_min"), 0.0012))
                        hard_drawdown_override = abs(self._to_float(gate_cfg_hard.get("exit_drawdown_override"), 0.015))
                        drawdown_hard = self._position_drawdown_ratio(position, current_price)
                        risk_cfg_hard = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
                        conflict_cfg_hard = (
                            risk_cfg_hard.get("conflict_protection", {})
                            if isinstance(risk_cfg_hard.get("conflict_protection"), dict)
                            else {}
                        )
                        hard_exit_min_hold_seconds = max(
                            0,
                            int(self._to_float(conflict_cfg_hard.get("hard_exit_min_hold_seconds", 720), 720)),
                        )
                        directional_eval_interval_seconds = max(
                            0,
                            int(
                                self._to_float(
                                    conflict_cfg_hard.get("directional_eval_interval_seconds", 180),
                                    180,
                                )
                            ),
                        )
                        hard_exit_new_pos_buffer_enabled = bool(
                            conflict_cfg_hard.get("hard_exit_new_pos_buffer_enabled", False)
                        )
                        hard_exit_new_pos_buffer_minutes = max(
                            0.0,
                            self._to_float(conflict_cfg_hard.get("hard_exit_new_pos_buffer_minutes", 0.0), 0.0),
                        )
                        hard_exit_new_pos_hold_mult = max(
                            1.0,
                            self._to_float(conflict_cfg_hard.get("hard_exit_new_pos_hold_mult", 1.5), 1.5),
                        )
                        now_runtime_ts = float(time.time())
                        last_direction_eval_ts = float(self._position_last_direction_eval_ts.get(pos_key_runtime, 0.0) or 0.0)
                        directional_eval_due = bool(
                            directional_eval_interval_seconds <= 0
                            or last_direction_eval_ts <= 0.0
                            or (now_runtime_ts - last_direction_eval_ts) >= directional_eval_interval_seconds
                        )
                        directional_eval_wait_seconds = (
                            0
                            if directional_eval_due or directional_eval_interval_seconds <= 0
                            else max(0, int(directional_eval_interval_seconds - (now_runtime_ts - last_direction_eval_ts)))
                        )
                        ff_cfg_hard = self.config.get("fund_flow", {}) if isinstance(self.config, dict) else {}
                        stop_loss_raw = (
                            ff_cfg_hard.get("stop_loss_pct")
                            if isinstance(ff_cfg_hard, dict)
                            else None
                        )
                        if stop_loss_raw is None:
                            stop_loss_raw = risk_cfg_hard.get("stop_loss_default_percent")
                        if stop_loss_raw is None:
                            stop_loss_raw = getattr(self.fund_flow_decision_engine, "stop_loss_pct", 0.015)
                        max_stop_loss_ratio = max(
                            0.0,
                            self._normalize_percent_to_ratio(stop_loss_raw, 0.015),
                        )
                        max_stop_loss_hit = bool(max_stop_loss_ratio > 0 and drawdown_hard >= max_stop_loss_ratio)
                        if current_side == "LONG":
                            reduce_price_confirmed = price_change_hard <= (-1.0 * hard_price_change_min)
                        else:
                            reduce_price_confirmed = price_change_hard >= hard_price_change_min
                        required_hold_seconds = hard_exit_min_hold_seconds
                        base_reduce_signal = bool(reduce_price_confirmed or drawdown_hard >= hard_drawdown_override)
                        new_pos_buffer_tag = ""
                        # EXIT/CIRCUIT_EXIT 归类为强反向信号，但未到最大止损前仍需满足最短持仓+3分钟评估节流。
                        if risk_state in ("EXIT", "CIRCUIT_EXIT"):
                            reduce_pct = max(reduce_pct, 1.0)
                            new_pos_window_seconds = int(hard_exit_new_pos_buffer_minutes * 60.0)
                            is_new_position_buffer = bool(
                                hard_exit_new_pos_buffer_enabled
                                and new_pos_window_seconds > 0
                                and hold_seconds_runtime <= new_pos_window_seconds
                            )
                            if is_new_position_buffer:
                                required_hold_seconds = max(
                                    required_hold_seconds,
                                    int(hard_exit_min_hold_seconds * hard_exit_new_pos_hold_mult),
                                )
                                new_pos_buffer_tag = f" [new_pos_buffer={new_pos_window_seconds}s]"
                            base_reduce_signal = True
                        if max_stop_loss_hit:
                            reduce_confirmed = True
                            if pos_key_runtime:
                                self._position_last_direction_eval_ts[pos_key_runtime] = now_runtime_ts
                        elif not base_reduce_signal:
                            reduce_confirmed = False
                        else:
                            if directional_eval_due and pos_key_runtime:
                                self._position_last_direction_eval_ts[pos_key_runtime] = now_runtime_ts
                            reduce_confirmed = bool(
                                directional_eval_due
                                and hold_seconds_runtime >= required_hold_seconds
                            )
                        if not reduce_confirmed:
                            print(
                                f"🛡️ {symbol} HARD退出缓冲: hold={hold_seconds_runtime}s/{required_hold_seconds}s, "
                                f"eval_due={int(directional_eval_due)} wait={directional_eval_wait_seconds}s, "
                                f"drawdown={drawdown_hard:.4f}/{max_stop_loss_ratio:.4f}, "
                                f"risk_state={risk_state}"
                                + new_pos_buffer_tag
                            )
                        print(
                            f"🛡️ {symbol} 冲突保护 HARD: {protection.get('reason')} | "
                            f"freeze_add reduce_pos={reduce_pct:.0%} force_break_even cd={'Y' if cooldown_active else 'N'} "
                            f"reduce_confirmed={int(reduce_confirmed)} max_sl_hit={int(max_stop_loss_hit)} "
                            f"eval_int={directional_eval_interval_seconds}s min_hold={required_hold_seconds}s"
                        )
                        # 收紧止损（保本止损）
                        if bool(protection.get("force_break_even", False)):
                            try:
                                # stats: attempt
                                self.risk_manager.record_protection_action(symbol, current_side, "breakeven", "attempt", level=protection_level)
                                r = self._tighten_protection_for_conflict(
                                    symbol=symbol,
                                    position=position,
                                    current_price=current_price,
                                    force_break_even=True,
                                    atr_pct=self._to_float(decision_md.get("regime_atr_pct"), 0.0),
                                    cooldown_sec=60.0,
                                    breakeven_mode=str(decision_md.get("risk_breakeven_mode", "") or ""),
                                    breakeven_fee_buffer=self._to_float(decision_md.get("risk_breakeven_fee_buffer"), 0.0),
                                )
                                if isinstance(r, dict) and r.get("status") == "skipped":
                                    msg = str(r.get("message", ""))
                                    print(f"🛡️ {symbol} 保本止损跳过: {msg}")
                                    if "cooldown_active" in msg:
                                        self.risk_manager.record_protection_action(symbol, current_side, "breakeven", "skipped_cooldown", level=protection_level, detail=r)
                                    elif "not_tighter" in msg or msg == "not_tighter":
                                        self.risk_manager.record_protection_action(symbol, current_side, "breakeven", "skipped_not_tighter", level=protection_level, detail=r)
                                    else:
                                        self.risk_manager.record_protection_action(symbol, current_side, "breakeven", "error", level=protection_level, detail=r)
                                else:
                                    self.risk_manager.record_protection_action(symbol, current_side, "breakeven", "applied", level=protection_level, detail={"new_sl": r.get("new_sl") if isinstance(r, dict) else None})
                            except Exception as e:
                                print(f"⚠️ {symbol} 保本止损失败: {e}")
                                self.risk_manager.record_protection_action(symbol, current_side, "breakeven", "error", level=protection_level, detail={"error": str(e)})
                        # 减仓：CLOSE 的 target_portion_of_balance 在 execution_router 中解释为"持仓比例"
                        if reduce_pct > 0 and reduce_confirmed:
                            # stats: reduce triggered
                            self.risk_manager.record_protection_action(symbol, current_side, "reduce", "triggered", level=protection_level, detail={"reduce_pct": reduce_pct})
                            decision = FundFlowDecision(
                                operation=FundFlowOperation.CLOSE,
                                symbol=symbol,
                                target_portion_of_balance=reduce_pct,
                                leverage=decision.leverage,
                                reason=f"RISK_PROTECT: {protection.get('reason')}",
                                metadata=decision_md,
                            )
                            # 执行减仓
                            pending_new_entries.append({
                                "decision": decision,
                                "position": position,
                                "current_price": current_price,
                                "trigger_context": trigger_context,
                            })
                            continue
                        if reduce_pct > 0 and not reduce_confirmed:
                            print(
                                f"🛡️ {symbol} HARD减仓暂缓: "
                                f"drawdown={drawdown_hard:.4f}/{hard_drawdown_override:.4f}, "
                                f"price_change={price_change_hard:+.4f}, "
                                f"min_move={hard_price_change_min:.4f}"
                            )
                        # 否则禁止加仓
                        continue
            
                    if protection_level == "conflict_light":
                        risk_cfg_light = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
                        conflict_cfg_light = (
                            risk_cfg_light.get("conflict_protection", {})
                            if isinstance(risk_cfg_light.get("conflict_protection"), dict)
                            else {}
                        )
                        light_min_hold_seconds = max(
                            0,
                            int(self._to_float(conflict_cfg_light.get("light_tighten_min_hold_seconds", 600), 600)),
                        )
                        light_min_mfe_ratio = max(
                            0.0,
                            self._normalize_percent_to_ratio(conflict_cfg_light.get("light_tighten_min_mfe", 0.0025), 0.0025),
                        )
                        allow_light_tighten = (
                            hold_seconds_runtime >= light_min_hold_seconds
                            and (mfe_runtime / 100.0) >= light_min_mfe_ratio
                        )
                        engine_light = str(decision_md.get("engine") or decision_md.get("regime") or "").upper()
                        take_profit_only_range = bool(conflict_cfg_light.get("light_take_profit_only_range", True))
                        take_profit_enabled = bool(conflict_cfg_light.get("light_take_profit_enabled", True))
                        take_profit_min_hold_seconds = max(
                            0,
                            int(
                                self._to_float(
                                    conflict_cfg_light.get("light_take_profit_min_hold_seconds", light_min_hold_seconds),
                                    light_min_hold_seconds,
                                )
                            ),
                        )
                        take_profit_min_mfe_ratio = max(
                            0.0,
                            self._normalize_percent_to_ratio(
                                conflict_cfg_light.get("light_take_profit_min_mfe", light_min_mfe_ratio),
                                light_min_mfe_ratio,
                            ),
                        )
                        take_profit_min_pnl_ratio = max(
                            0.0,
                            self._normalize_percent_to_ratio(
                                conflict_cfg_light.get("light_take_profit_min_pnl", 0.0),
                                0.0,
                            ),
                        )
                        take_profit_pct = min(
                            1.0,
                            max(
                                0.0,
                                self._to_float(
                                    conflict_cfg_light.get(
                                        "light_take_profit_pct",
                                        conflict_cfg_light.get("light_reduce_position_pct", 0.5),
                                    ),
                                    0.5,
                                ),
                            ),
                        )
                        entry_price_light = self._to_float(position.get("entry_price"), 0.0)
                        current_pnl_ratio_light = 0.0
                        if entry_price_light > 0 and current_price > 0:
                            if current_side == "LONG":
                                current_pnl_ratio_light = (current_price - entry_price_light) / entry_price_light
                            else:
                                current_pnl_ratio_light = (entry_price_light - current_price) / entry_price_light
                        allow_light_take_profit = (
                            take_profit_enabled
                            and take_profit_pct > 0
                            and ((not take_profit_only_range) or engine_light == "RANGE")
                            and hold_seconds_runtime >= take_profit_min_hold_seconds
                            and (mfe_runtime / 100.0) >= take_profit_min_mfe_ratio
                            and current_pnl_ratio_light >= take_profit_min_pnl_ratio
                            and (not cooldown_active)
                        )
                        print(
                            f"🛡️ {symbol} 冲突保护 LIGHT: {protection.get('reason')} | "
                            f"freeze_add tighten_trailing cd={'Y' if cooldown_active else 'N'} "
                            f"allow_tighten={int(allow_light_tighten)} allow_reduce={int(allow_light_take_profit)}"
                        )
                        if allow_light_take_profit:
                            self.risk_manager.record_protection_action(
                                symbol,
                                current_side,
                                "reduce",
                                "triggered",
                                level=protection_level,
                                detail={
                                    "reduce_pct": take_profit_pct,
                                    "engine": engine_light,
                                    "mfe_ratio": mfe_runtime / 100.0,
                                    "pnl_ratio": current_pnl_ratio_light,
                                },
                            )
                            decision = FundFlowDecision(
                                operation=FundFlowOperation.CLOSE,
                                symbol=symbol,
                                target_portion_of_balance=take_profit_pct,
                                leverage=decision.leverage,
                                reason=(
                                    f"RISK_PROTECT_LIGHT_TP: {protection.get('reason')} "
                                    f"| reduce={take_profit_pct:.0%} "
                                    f"mfe={mfe_runtime/100.0:.4f} pnl={current_pnl_ratio_light:.4f}"
                                ),
                                metadata=decision_md,
                            )
                            pending_new_entries.append({
                                "decision": decision,
                                "position": position,
                                "current_price": current_price,
                                "trigger_context": trigger_context,
                            })
                            continue
                        # 收紧止损
                        if bool(protection.get("tighten_trailing", False)) and allow_light_tighten:
                            try:
                                # stats: attempt
                                self.risk_manager.record_protection_action(symbol, current_side, "tighten", "attempt", level=protection_level)
                                r = self._tighten_protection_for_conflict(
                                    symbol=symbol,
                                    position=position,
                                    current_price=current_price,
                                    force_break_even=False,
                                    tighten_ratio=0.5,
                                    atr_pct=self._to_float(decision_md.get("regime_atr_pct"), 0.0),
                                    cooldown_sec=60.0,
                                )
                                if isinstance(r, dict) and r.get("status") == "skipped":
                                    msg = str(r.get("message", ""))
                                    print(f"🛡️ {symbol} 收紧止损跳过: {msg}")
                                    if "cooldown_active" in msg:
                                        self.risk_manager.record_protection_action(symbol, current_side, "tighten", "skipped_cooldown", level=protection_level, detail=r)
                                    elif msg == "not_tighter":
                                        self.risk_manager.record_protection_action(symbol, current_side, "tighten", "skipped_not_tighter", level=protection_level, detail=r)
                                    else:
                                        self.risk_manager.record_protection_action(symbol, current_side, "tighten", "error", level=protection_level, detail=r)
                                else:
                                    self.risk_manager.record_protection_action(symbol, current_side, "tighten", "applied", level=protection_level, detail={"result": "ok"})
                            except Exception as e:
                                print(f"⚠️ {symbol} 收紧止损失败: {e}")
                                self.risk_manager.record_protection_action(symbol, current_side, "tighten", "error", level=protection_level, detail={"error": str(e)})
                        elif bool(protection.get("tighten_trailing", False)):
                            print(
                                f"🛡️ {symbol} LIGHT收紧止损暂缓: "
                                f"hold={hold_seconds_runtime}s/{light_min_hold_seconds}s, "
                                f"mfe={mfe_runtime/100.0:.4f}/{light_min_mfe_ratio:.4f}"
                            )
                            self.risk_manager.record_protection_action(
                                symbol,
                                current_side,
                                "tighten",
                                "skipped_not_ready",
                                level=protection_level,
                                detail={
                                    "hold_seconds": hold_seconds_runtime,
                                    "hold_threshold": light_min_hold_seconds,
                                    "mfe_ratio": mfe_runtime / 100.0,
                                    "mfe_threshold": light_min_mfe_ratio,
                                },
                            )
                        # 轻度冲突：冻结加仓/新开同向（保留持仓管理/止盈止损继续运行）
                        continue
            
                    if protection_level == "confirm":
                        # 确认增强：不放宽止损，只做"允许加仓/延后出场"的信号
                        print(f"✅ {symbol} 方向确认增强: {protection.get('reason')}")
            
                if decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL):
            
                    remaining = max(0.0, float(local_max_symbol_position_portion) - float(current_portion))
                    if remaining < min_open_portion:
                        print(
                            f"⏭️ {symbol} 已达到单币仓位上限({local_max_symbol_position_portion:.2f})，"
                            f"当前占比={current_portion:.2f}，跳过加仓"
                        )
                        continue
            
                    md = decision.metadata if isinstance(decision.metadata, dict) else {}
                    is_dca = bool(md.get("dca_triggered"))
                    if is_dca:
                        decision.target_portion_of_balance = min(
                            float(decision.target_portion_of_balance),
                            remaining,
                        )
                    else:
                        decision.target_portion_of_balance = min(
                            float(decision.target_portion_of_balance),
                            float(local_add_position_portion),
                            remaining,
                        )
            
                    if decision.target_portion_of_balance < min_open_portion:
                        print(
                            f"⏭️ {symbol} 剩余可加仓比例不足最小下单阈值，"
                            f"remaining={remaining:.3f}, min_open={min_open_portion:.3f}"
                        )
                        continue
                    if is_dca:
                        stage = int(md.get("dca_stage", 0) or 0)
                        mult = self._to_float(md.get("dca_multiplier"), 1.0)
                        dd = self._to_float(md.get("dca_drawdown"), 0.0)
                        th = self._to_float(md.get("dca_threshold"), 0.0)
                        base_reason = str(decision.reason).strip() if decision.reason else ""
                        decision.reason = (
                            f"{base_reason} | DCA执行 stage={stage} drawdown={dd:.4f}/th={th:.4f} "
                            f"mult={mult:.2f} target={decision.target_portion_of_balance:.2f}"
                        ).strip()
                    else:
                        add_reason = (
                            f"加仓模式 current={current_portion:.2f} "
                            f"target={decision.target_portion_of_balance:.2f}"
                        )
                        base_reason = str(decision.reason).strip() if decision.reason else ""
                        decision.reason = f"{base_reason} | {add_reason}" if base_reason else add_reason
            
            if decision.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL) and position is None:
                if block_new_entries_due_to_protection_gap:
                    print(
                        f"⛔ {symbol} 禁止新开仓：存在缺保护持仓 "
                        f"symbols={','.join(protection_gap_symbols)}"
                    )
                    continue
                item_max_active_symbols = max(
                    1,
                    int(
                        self._to_float(
                            engine_override.get("max_active_symbols", max_active_symbols),
                            max_active_symbols,
                        )
                    ),
                )
                pending_new_entries.append(
                    {
                        "symbol": symbol,
                        "score": self._decision_signal_score(decision),
                        "max_active_symbols": item_max_active_symbols,
                        "engine": decision_md.get("engine"),
                        "decision": decision,
                        "account_summary": account_summary,
                        "current_price": current_price,
                        "position": position,
                        "flow_context": flow_context,
                        "trigger_type": trigger_type,
                        "trigger_id": trigger_id,
                        "trigger_context": trigger_context,
                        "portfolio": portfolio,
                    }
                )
                continue

            self._execute_and_log_decision(
                symbol=symbol,
                decision=decision,
                account_summary=account_summary,
                current_price=current_price,
                position=position,
                flow_context=flow_context,
                trigger_type=trigger_type,
                trigger_id=trigger_id,
                trigger_context=trigger_context,
                portfolio=portfolio,
            )

    def _process_symbol_core(self, symbol: str, idx: int, symbol_ctx: Dict[str, Any]) -> None:
        symbols_raw = symbol_ctx.get("symbols")
        symbols = symbols_raw if isinstance(symbols_raw, list) else []
        symbol_stagger_seconds = max(0.0, self._to_float(symbol_ctx.get("symbol_stagger_seconds"), 0.0))
        now_ts = self._to_float(symbol_ctx.get("now_ts"), time.time())
        sla_cfg_raw = symbol_ctx.get("sla_cfg")
        sla_cfg = sla_cfg_raw if isinstance(sla_cfg_raw, dict) else {}
        ff_cfg_raw = symbol_ctx.get("ff_cfg")
        ff_cfg = ff_cfg_raw if isinstance(ff_cfg_raw, dict) else {}
        ai_review_cfg_raw = symbol_ctx.get("ai_review_cfg")
        ai_review_cfg = ai_review_cfg_raw if isinstance(ai_review_cfg_raw, dict) else {}
        ai_review_mode = str(symbol_ctx.get("ai_review_mode") or "disabled")
        max_active_symbols = max(1, int(self._to_float(symbol_ctx.get("max_active_symbols"), 3)))
        max_symbol_position_portion = self._normalize_percent_to_ratio(
            symbol_ctx.get("max_symbol_position_portion", 0.6),
            0.6,
        )
        add_position_portion = self._normalize_percent_to_ratio(symbol_ctx.get("add_position_portion", 0.2), 0.2)
        repair_fail_reduce_ratio = self._to_float(symbol_ctx.get("repair_fail_reduce_ratio"), 1.0)
        immediate_close_on_repair_fail = bool(symbol_ctx.get("immediate_close_on_repair_fail", False))
        allow_new_entries = bool(symbol_ctx.get("allow_new_entries", True))
        risk_guard_enabled = bool(symbol_ctx.get("risk_guard_enabled", True))
        account_summary_raw = symbol_ctx.get("account_summary")
        account_summary = account_summary_raw if isinstance(account_summary_raw, dict) else {}
        position_snapshot_raw = symbol_ctx.get("position_snapshot")
        position_snapshot = position_snapshot_raw if isinstance(position_snapshot_raw, dict) else {}

        pending_new_entries_raw = symbol_ctx.get("pending_new_entries")
        pending_new_entries = pending_new_entries_raw if isinstance(pending_new_entries_raw, list) else []
        protection_gap_symbols_raw = symbol_ctx.get("protection_gap_symbols")
        protection_gap_symbols = protection_gap_symbols_raw if isinstance(protection_gap_symbols_raw, list) else []
        block_new_entries_due_to_protection_gap = bool(
            symbol_ctx.get("block_new_entries_due_to_protection_gap", False)
        )

        for _ in (0,):
            try:
                market_data = self.get_market_data_for_symbol(symbol)
                realtime = market_data.get("realtime", {})
                current_price = self._to_float(realtime.get("price"), 0.0)
                if current_price <= 0:
                    print(f"⚠️ {symbol} 当前价格无效，跳过")
                    continue

                position = position_snapshot.get(symbol)
                if position is None:
                    self._clear_sla_tracking_for_symbol(symbol)
                    self._clear_dca_tracking_for_symbol(symbol)
                if position is None and self._has_pending_entry_order(symbol):
                    print(f"⏭️ {symbol} 存在未成交开仓单，跳过重复开仓决策")
                    continue

                skip_symbol, block_new_entries_due_to_protection_gap = self._handle_symbol_protection_and_sla(
                    symbol=symbol,
                    position=position,
                    current_price=current_price,
                    now_ts=now_ts,
                    sla_cfg=sla_cfg,
                    repair_fail_reduce_ratio=repair_fail_reduce_ratio,
                    immediate_close_on_repair_fail=immediate_close_on_repair_fail,
                    block_new_entries_due_to_protection_gap=block_new_entries_due_to_protection_gap,
                    protection_gap_symbols=protection_gap_symbols,
                )
                if skip_symbol:
                    continue

                self._execute_symbol_signal_decision(
                    symbol=symbol,
                    market_data=market_data,
                    position=position,
                    current_price=current_price,
                    account_summary=account_summary,
                    pending_new_entries=pending_new_entries,
                    protection_gap_symbols=protection_gap_symbols,
                    block_new_entries_due_to_protection_gap=block_new_entries_due_to_protection_gap,
                    allow_new_entries=allow_new_entries,
                    ff_cfg=ff_cfg,
                    max_active_symbols=max_active_symbols,
                    max_symbol_position_portion=max_symbol_position_portion,
                    add_position_portion=add_position_portion,
                    risk_guard_enabled=risk_guard_enabled,
                    ai_review_mode=ai_review_mode,
                    ai_review_cfg=ai_review_cfg,
                )
            except Exception as e:
                print(f"❌ {symbol} 处理异常: {e}")
            finally:
                if symbol_stagger_seconds > 0 and idx < len(symbols) - 1:
                    time.sleep(symbol_stagger_seconds)

        symbol_ctx["block_new_entries_due_to_protection_gap"] = block_new_entries_due_to_protection_gap
        symbol_ctx["pending_new_entries"] = pending_new_entries
        symbol_ctx["protection_gap_symbols"] = protection_gap_symbols

    def _finalize_symbol_context(self, context: Dict[str, Any], symbol_ctx: Dict[str, Any]) -> None:
        context["block_new_entries_due_to_protection_gap"] = bool(
            symbol_ctx.get("block_new_entries_due_to_protection_gap", False)
        )
        pending_new_entries = symbol_ctx.get("pending_new_entries")
        context["pending_new_entries"] = pending_new_entries if isinstance(pending_new_entries, list) else []
        protection_gap_symbols = symbol_ctx.get("protection_gap_symbols")
        context["protection_gap_symbols"] = (
            protection_gap_symbols if isinstance(protection_gap_symbols, list) else []
        )

    def _process_symbol(self, symbol: str, idx: int, context: Dict[str, Any]) -> None:
        symbol_ctx = self._prepare_symbol_context(context)
        self._process_symbol_core(symbol, idx, symbol_ctx)
        self._finalize_symbol_context(context, symbol_ctx)

    def _finalize_entries(self, context: Dict[str, Any]) -> None:
        pending_new_entries_raw = context.get("pending_new_entries")
        pending_new_entries = pending_new_entries_raw if isinstance(pending_new_entries_raw, list) else []
        block_new_entries_due_to_protection_gap = bool(
            context.get("block_new_entries_due_to_protection_gap", False)
        )
        protection_gap_symbols_raw = context.get("protection_gap_symbols")
        protection_gap_symbols = protection_gap_symbols_raw if isinstance(protection_gap_symbols_raw, list) else []
        max_active_symbols = max(1, int(self._to_float(context.get("max_active_symbols"), 3)))
        account_summary_raw = context.get("account_summary")
        account_summary = account_summary_raw if isinstance(account_summary_raw, dict) else {}
        ai_gate_enabled = bool(context.get("ai_gate_enabled", False))
        ai_review_cfg_raw = context.get("ai_review_cfg")
        ai_review_cfg = ai_review_cfg_raw if isinstance(ai_review_cfg_raw, dict) else {}
        ai_review_mode = str(context.get("ai_review_mode") or "disabled").lower()
        ai_flat_top_n = max(1, int(self._to_float(ai_review_cfg.get("flat_top_n", 2), 2)))

        if block_new_entries_due_to_protection_gap and pending_new_entries:
            print(
                "⛔ 本轮禁止新开仓：检测到持仓缺少保护单，已清空候选开仓队列 "
                f"symbols={','.join(protection_gap_symbols)}"
            )
            pending_new_entries = []

        if pending_new_entries:
            pending_new_entries = sorted(
                pending_new_entries,
                key=lambda x: float(x.get("score", 0.0)),
                reverse=True,
            )
            if ai_gate_enabled and ai_review_mode == "flat_candidates" and len(pending_new_entries) > ai_flat_top_n:
                skipped = pending_new_entries[ai_flat_top_n:]
                pending_new_entries = pending_new_entries[:ai_flat_top_n]
                skipped_symbols = [str(item.get("symbol") or "") for item in skipped]
                print(
                    f"🤖 空仓AI候选收敛: 仅分析前{ai_flat_top_n}个标的, "
                    f"跳过={','.join([s for s in skipped_symbols if s])}"
                )
            if ai_gate_enabled and ai_review_mode == "flat_candidates":
                shortlist_parts: List[str] = []
                for rank, item in enumerate(pending_new_entries, start=1):
                    symbol_i = str(item.get("symbol") or "")
                    score_i = float(item.get("score", 0.0))
                    decision_i_raw = item.get("decision")
                    decision_i = decision_i_raw if isinstance(decision_i_raw, FundFlowDecision) else None
                    local_action = decision_i.operation.value.upper() if decision_i else "UNKNOWN"
                    shortlist_parts.append(
                        f"{rank}.{symbol_i}:{local_action}:score={score_i:.3f}"
                    )
                if shortlist_parts:
                    print(
                        f"🤖 空仓AI入围Top{len(shortlist_parts)}: "
                        + " | ".join(shortlist_parts)
                    )
            active_symbols_estimate: set[str] = set()
            try:
                active_positions = self.position_data.get_all_positions()
                if isinstance(active_positions, dict):
                    active_symbols_estimate = {str(s).upper() for s in active_positions.keys()}
            except Exception:
                active_symbols_estimate = set()
            active_symbols_estimate.update(str(s).upper() for s in self._opened_symbols_this_cycle)
            for rank, item in enumerate(pending_new_entries, start=1):
                active_count = len(active_symbols_estimate)
                item_max_active_symbols = max(
                    1,
                    int(self._to_float(item.get("max_active_symbols"), max_active_symbols)),
                )
                if active_count >= item_max_active_symbols:
                    print(
                        f"⏭️ {item.get('symbol')} 候选开仓被跳过："
                        f"持仓交易对已满({active_count}/{item_max_active_symbols})，"
                        f"候选排名={rank}, score={float(item.get('score', 0.0)):.3f}"
                    )
                    continue

                account_summary_i = item.get("account_summary")
                if not isinstance(account_summary_i, dict):
                    account_summary_i = account_summary

                flow_context_i = item.get("flow_context")
                if not isinstance(flow_context_i, dict):
                    flow_context_i = {}

                trigger_context_i = item.get("trigger_context")
                if not isinstance(trigger_context_i, dict):
                    trigger_context_i = {}

                portfolio_i = item.get("portfolio")
                if not isinstance(portfolio_i, dict):
                    portfolio_i = {}

                current_price_i = self._to_float(item.get("current_price"), 0.0)
                decision_i_raw = item.get("decision")
                decision_i = decision_i_raw if isinstance(decision_i_raw, FundFlowDecision) else None
                symbol_i = str(item.get("symbol") or "")
                if decision_i is None:
                    continue
                if ai_gate_enabled and ai_review_mode == "flat_candidates":
                    local_score = float(item.get("score", 0.0))
                    if decision_i.operation in (FundFlowOperation.BUY, FundFlowOperation.SELL) and current_price_i > 0:
                        print(
                            f"🤖 {symbol_i} AI终审请求: rank={rank} "
                            f"local={decision_i.operation.value.upper()} score={local_score:.3f} "
                            f"price={current_price_i:.6f}"
                        )
                        ai_trigger_context = dict(trigger_context_i)
                        ai_trigger_context["ai_gate"] = "final"
                        ai_trigger_context["local_operation"] = decision_i.operation.value
                        ai_trigger_context["candidate_rank"] = rank
                        ai_trigger_context["candidate_score"] = local_score
                        ai_decision = self.fund_flow_decision_engine.decide(
                            symbol=symbol_i,
                            portfolio=portfolio_i,
                            price=current_price_i,
                            market_flow_context=flow_context_i,
                            trigger_context=ai_trigger_context,
                            use_weight_router=True,
                            use_ai_weights=True,
                        )
                        ai_md_raw = getattr(ai_decision, "metadata", None)
                        ai_md = ai_md_raw if isinstance(ai_md_raw, dict) else {}
                        ai_source = str(ai_md.get("ds_source") or "-")
                        ai_conf = self._to_float(ai_md.get("ds_confidence"), 0.0)
                        if ai_decision.operation != decision_i.operation:
                            print(
                                f"⛔ {symbol_i} AI终审未通过: rank={rank} "
                                f"local={decision_i.operation.value.upper()} "
                                f"ai={ai_decision.operation.value.upper()} "
                                f"source={ai_source} conf={ai_conf:.3f}"
                            )
                            continue
                        if ai_source != "ai_weight_router":
                            print(
                                f"⚠️ {symbol_i} AI终审回退本地: rank={rank} "
                                f"local={decision_i.operation.value.upper()} "
                                f"ai={ai_decision.operation.value.upper()} "
                                f"source={ai_source} conf={ai_conf:.3f}"
                            )
                        else:
                            print(
                                f"🤖 {symbol_i} AI终审通过: rank={rank} "
                                f"action={ai_decision.operation.value.upper()} "
                                f"source={ai_source} conf={ai_conf:.3f}"
                            )
                        decision_i = ai_decision
                        item["decision"] = decision_i
                        item["score"] = self._decision_signal_score(decision_i)
                    else:
                        print(
                            f"🤖 {symbol_i} 未进入AI终审: rank={rank} "
                            f"local={decision_i.operation.value.upper()} score={local_score:.3f}"
                        )
                self._execute_and_log_decision(
                    symbol=symbol_i,
                    decision=decision_i,
                    account_summary=account_summary_i,
                    current_price=current_price_i,
                    position=item.get("position"),
                    flow_context=flow_context_i,
                    trigger_type=str(item.get("trigger_type")),
                    trigger_id=str(item.get("trigger_id")),
                    trigger_context=trigger_context_i,
                    portfolio=portfolio_i,
                )
                item_symbol = str(item.get("symbol") or "").upper()
                if item_symbol and item_symbol in {str(s).upper() for s in self._opened_symbols_this_cycle}:
                    active_symbols_estimate.add(item_symbol)


        context["pending_new_entries"] = pending_new_entries

    def run(self) -> None:
        cycles = 0

        while True:
            start = time.time()
            alignment_active = self._is_kline_alignment_active()
            tf_seconds = self._decision_timeframe_seconds() or 0
            symbols_all = ConfigLoader.get_trading_symbols(self.config)
            has_position = bool(self._position_snapshot_by_symbol(symbols_all))
            ai_review_cfg = self._ai_review_config()
            position_tf_seconds = int(ai_review_cfg.get("position_timeframe_seconds", 300))
            flat_tf_seconds = int(ai_review_cfg.get("flat_timeframe_seconds", tf_seconds or 900))
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if has_position:
                position_review_due = self._should_allow_aligned_cycle(
                    bucket_key="position_review",
                    timeframe_seconds=position_tf_seconds,
                    now_ts=start,
                )
                if position_review_due:
                    print(
                        f"\n=== FUND_FLOW cycle {cycles + 1} @ {now_utc} UTC === "
                        f"[mode=POSITION_AI_REVIEW, kline_align={'ON' if alignment_active else 'OFF'}"
                        f", tf={int(position_tf_seconds)}s]"
                    )
                    try:
                        self.run_cycle(allow_new_entries=False, ai_review_mode="positions")
                    except Exception as e:
                        print(f"❌ run_cycle 异常: {e}")
                else:
                    print(
                        f"\n=== FUND_FLOW cycle {cycles + 1} @ {now_utc} UTC === "
                        f"[mode=WAIT_POSITION_AI, kline_align={'ON' if alignment_active else 'OFF'}"
                        f", tf={int(position_tf_seconds)}s]"
                    )
                    next_fire = datetime.now(timezone.utc) + timedelta(
                        seconds=self._aligned_sleep_seconds_for(position_tf_seconds)
                    )
                    print(
                        "⏭️ 当前有持仓，等待下一次 5m AI 持仓复核窗口。"
                        f" 下次复核(UTC)≈{next_fire.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
            else:
                allow_new_entries = self._should_allow_entries_this_cycle(start)
                flat_review_due = self._should_allow_aligned_cycle(
                    bucket_key="flat_ai_review",
                    timeframe_seconds=flat_tf_seconds,
                    now_ts=start,
                )
                if allow_new_entries and flat_review_due:
                    print(
                        f"\n=== FUND_FLOW cycle {cycles + 1} @ {now_utc} UTC === "
                        f"[mode=OPEN_WINDOW_AI_TOP2, kline_align={'ON' if alignment_active else 'OFF'}"
                        f"{', tf=' + str(int(tf_seconds)) + 's' if alignment_active else ''}]"
                    )
                    try:
                        self.run_cycle(allow_new_entries=True, ai_review_mode="flat_candidates")
                    except Exception as e:
                        print(f"❌ run_cycle 异常: {e}")
                else:
                    print(
                        f"\n=== FUND_FLOW cycle {cycles + 1} @ {now_utc} UTC === "
                        f"[mode=WAIT_OPEN_AI, kline_align={'ON' if alignment_active else 'OFF'}"
                        f"{', tf=' + str(int(flat_tf_seconds)) + 's' if alignment_active else ''}]"
                    )
                    next_fire = datetime.now(timezone.utc) + timedelta(
                        seconds=self._aligned_sleep_seconds_for(flat_tf_seconds)
                    )
                    print(
                        "⏭️ 当前无持仓，等待下一次 15m AI 开仓复核窗口。"
                        f" 下次开仓窗口(UTC)≈{next_fire.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
            cycles += 1
            schedule_cfg = self.config.get("schedule", {}) or {}
            interval_seconds = max(1, int(schedule_cfg.get("interval_seconds", 60) or 60))
            max_cycles = int(schedule_cfg.get("max_cycles", 0) or 0)
            if max_cycles > 0 and cycles >= max_cycles:
                print("✅ 达到 max_cycles，退出。")
                return

            elapsed = time.time() - start
            base_sleep_seconds = max(0.0, interval_seconds - elapsed)
            sleep_seconds = base_sleep_seconds
            if alignment_active:
                post_has_position = bool(self._position_snapshot_by_symbol(symbols_all))
                if post_has_position:
                    sleep_seconds = self._aligned_sleep_seconds_for(position_tf_seconds)
                    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"⏳ 调度等待(持仓AI): utc_now={now_utc}, sleep={sleep_seconds:.2f}s "
                        f"(next_5m_review)"
                    )
                else:
                    sleep_seconds = self._aligned_sleep_seconds_for(flat_tf_seconds)
                    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"⏳ 调度等待(空仓AI): utc_now={now_utc}, sleep={sleep_seconds:.2f}s "
                        f"(next_15m_review)"
                    )
                    next_fire = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
                    print(
                        "⏳ 调度等待(无持仓): "
                        f"next_open_window(UTC)={next_fire.strftime('%Y-%m-%d %H:%M:%S')}, "
                        f"sleep={sleep_seconds:.2f}s"
                    )
            time.sleep(sleep_seconds)


def main() -> None:
    _configure_console_encoding()
    parser = argparse.ArgumentParser(description="Fund-flow trading bot")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="仅执行一个周期")
    args = parser.parse_args()

    bot = TradingBot(config_path=args.config)
    if args.once:
        bot.run_cycle()
        return
    bot.run()


if __name__ == "__main__":
    main()


