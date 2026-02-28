"""
AI交易机器人主程序
整合所有模块，实现完整的交易流程
"""

import argparse
import time
import math

from collections import deque
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from io import StringIO

from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

import csv

import pandas as pd

import numpy as np

import tempfile

import shutil

import os
import sys
# Ensure project root is on sys.path so `from src.*` imports work when running
# the script directly (must be before importing `src.*` packages).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__ or "")))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.ai.decision_parser import DecisionParser

from src.ai.deepseek_client import DeepSeekClient

from src.ai.prompt_builder import PromptBuilder

from src.api.binance_client import BinanceClient

from src.config.config_loader import ConfigLoader

from src.config.config_monitor import ConfigMonitor

from src.config.env_manager import EnvManager

from src.data.account_data import AccountDataManager

from src.data.market_data import MarketDataManager

from src.data.position_data import PositionDataManager

from src.data.klines_downloader import set_custom_endpoints

from src.trading.position_manager import PositionManager

from src.trading.risk_manager import RiskManager

from src.trading.trade_executor import TradeExecutor

from src.strategy import V5Strategy


import json


class TerminalOutputLogger:
    def __init__(self, original: TextIO, log_path_provider: Callable[[], str]):
        self.original = original
        self.log_path_provider = log_path_provider
        self._is_terminal_logger = True

    def write(self, message: str) -> None:
        self.original.write(message)
        self.original.flush()
        if message:
            try:
                log_path = self.log_path_provider()
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(message)
            except Exception as exc:
                self.original.write(f"⚠️ 终端日志写入失败: {exc}\n")

    def flush(self) -> None:
        self.original.flush()


class TradingBot:
    """交易机器人主类"""

    # --- 为静态分析（Pylance）预声明常用方法，避免“属性未知”诊断 ---
    # 这些仅为类型声明，不改变运行时逻辑；实际实现位于类中下方。
    _get_log_file_path: Callable[[], str]
    _normalize_position_side: Callable[[Any], Optional[str]]
    _dca_get_total_stop_loss_cooldown_seconds: Callable[[Dict[str, Any]], int]
    _sync_dca_dashboard_snapshot: Callable[[], None]
    _append_trade_log: Callable[..., None]

    def run(self) -> None:
        """占位 run 方法（在类后方若存在完整实现会覆盖本方法）。
        这个占位符用于避免在某些部署/打包场景中因方法定义顺序或文件裁剪导致的 AttributeError。
        """
        # 如果被意外调用，提供友好错误提示，而不是 AttributeError
        raise RuntimeError("TradingBot.run 方法尚未就绪 — 可能是文件被裁剪或加载顺序异常")

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        """轻量实例方法 stub：尽量将 value 转为 float，失败返回 default。
        这只是为了让静态分析器（Pylance）识别该方法引用 self._to_float(...)；
        文件下方仍保留完整实现（staticmethod），运行时后者会覆盖本实现。
        """
        try:
            return float(value)
        except Exception:
            return default



    MULTI_TIMEFRAME_LIMITS = {
        "15m": 200,
        "30m": 100,
        "1h": 50,
        "4h": 50,
        "1d": 50,
    }
    MULTI_TF_TREND_FACTOR = 0.06

    strategy_mode: str
    dca_config_path: str
    dca_config: Dict[str, Any]
    dca_config_mtime: Optional[float]
    dca_state: Dict[str, Dict[str, Any]]
    dca_last_entry_time: Optional[datetime]
    dca_initial_equity: Optional[float]
    dca_peak_equity: Optional[float]
    dca_halt: bool
    api_probe_info: Optional[Dict[str, Any]]

    def _is_dual_engine_mode(self) -> bool:
        """双引擎交易模式：兼容旧值 DCA_ROTATION 与新值 DUAL_ENGINE。"""
        mode = str(getattr(self, "strategy_mode", "") or "").upper()
        return mode in ("DCA_ROTATION", "DUAL_ENGINE")

    def __init__(self, config_path: Optional[str] = None):
        """初始化交易机器人"""
        print("=" * 60)
        print("🚀 AI交易机器人启动中...")
        print("=" * 60)

        # 如果未指定配置路径，按优先级选择：
        # 1) TRADING_CONFIG_FILE / BOT_CONFIG_FILE
        # 2) config/trading_config_vps.json
        if config_path is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            # 优先使用环境变量指定的配置文件；默认使用 config/trading_config_vps.json
            default_cfg = os.path.join(project_root, "config", "trading_config_vps.json")
            env_cfg = os.getenv("TRADING_CONFIG_FILE") or os.getenv("BOT_CONFIG_FILE")
            if env_cfg:
                config_path = env_cfg if os.path.isabs(env_cfg) else os.path.join(project_root, env_cfg)
                if not os.path.exists(config_path):
                    print(f"⚠️ 指定配置文件不存在: {config_path}，回退到默认配置: {default_cfg}")
                    config_path = default_cfg
            else:
                config_path = default_cfg

            # 最后确保配置文件存在，否则抛出友好错误
            if not os.path.exists(config_path):
                raise FileNotFoundError(f"配置文件不存在: {config_path}")

        # 保存配置路径
        self.config_path = config_path

        # 加载配置
        self.config = ConfigLoader.load_trading_config(config_path)
        print("✅ 配置加载完成")

        # 初始化配置监控器
        self.config_monitor = ConfigMonitor(config_path)
        print("✅ 配置监控器初始化完成")

        # 加载环境变量（支持按环境切换）
        # 优先级：
        # 1) TRADING_BOT_ENV_FILE / BOT_ENV_FILE
        # 2) 项目根目录 .env
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_hint = (
            os.getenv("TRADING_BOT_ENV_FILE")
            or os.getenv("BOT_ENV_FILE")
            or ".env"
        )
        env_path = env_hint if os.path.isabs(env_hint) else os.path.join(project_root, env_hint)
        loaded = EnvManager.load_env_file(env_path)
        if (not loaded) and env_hint != ".env":
            fallback_env = os.path.join(project_root, ".env")
            if EnvManager.load_env_file(fallback_env):
                env_path = fallback_env
                loaded = True
        if loaded:
            print(f"✅ 环境变量加载完成: {env_path}")
        else:
            print("⚠️ 环境变量未加载（将仅使用系统环境变量）")
        self._apply_network_env_from_config()

        # 初始化日志系统
        self.log_buffer = StringIO()
        self.logs_dir = self._resolve_logs_dir(project_root)
        self._setup_logs_directory()
        self._redirect_terminal_output()

        # API Key 自检已移除（避免误报影响启动日志）
        self.api_probe_info = None

        # 策略模式
        self.strategy_mode = str(self.config.get("strategy", {}).get("mode", "AI")).upper()
        self.ai_enabled = self.config.get("ai", {}).get("enabled", True)
        self.ai_client = None
        self.prompt_builder = None
        self.decision_parser = None
        self.strategy = None

        # DCA 轮动配置与状态
        self.dca_config_path = self.config_path
        self.dca_config: Dict[str, Any] = {}
        self.dca_config_mtime: Optional[float] = None
        self.dca_state: Dict[str, Dict[str, Any]] = {}
        self.dca_last_entry_time: Optional[datetime] = None
        # 连续亏损计数与由亏损触发的冷却期限（仅在达到阈值时触发）
        self.consecutive_losses: int = 0
        # 连续止损亏损计数（用于触发开仓减仓）
        self.consecutive_stopouts: int = 0
        self.dca_cooldown_expires: Optional[datetime] = None
        self.dca_cooldown_reason: Optional[str] = None
        # 当天开盘权益（用于更精确的当天亏损判定）
        self.dca_day_open_equity: Optional[float] = None
        self.dca_day_open_date: Optional[str] = None
        self.dca_day_open_tz: Optional[str] = None
        self.dca_initial_equity: Optional[float] = None
        self.dca_peak_equity: Optional[float] = None
        # 兼容历史状态字段：不再作为永久停机开关使用
        self.dca_halt: bool = False
        # 双引擎调度：默认按 5m 执行层 + N 分钟方向刷新
        self._dual_engine_exec_interval_seconds: int = 300
        self._dual_engine_direction_interval_seconds: int = 300
        self._dual_engine_direction_bucket: Optional[int] = None
        self._dual_engine_refresh_direction_this_cycle: bool = True
        # 每个执行周期递增，用于资金流快照按周期刷新（避免跨周期复用）
        self._dca_flow_cycle_id: int = 0
        # 严格 5m 决策 + 1m 执行：缓存上一轮方向刷新得到的开仓计划
        self._dca_open_plan_cache: List[Dict[str, Any]] = []
        self._dca_open_plan_cache_bucket: Optional[int] = None
        self._dca_open_plan_cache_created_at: Optional[str] = None
        self.dca_state_path = os.path.join(self.logs_dir, "dca_state.json")
        self.dca_dashboard_path = os.path.join(self.logs_dir, "dca_dashboard.json")
        self.dca_dashboard_csv_path = os.path.join(self.logs_dir, "dca_dashboard.csv")
        self.dca_dashboard_html_path = os.path.join(self.logs_dir, "dca_dashboard.html")
        self._last_dca_snapshot_key: Optional[str] = None
        self._last_open_orders_count: Optional[int] = None
        # 本次进程内 _get_dca_symbols 缓存，避免在短时间内重复触发网络/日志密集型筛选
        # cache: {"symbols": List[str], "ts": float}
        self._dca_symbols_cache: Dict[str, Any] = {"symbols": None, "ts": 0.0}
        self._multi_tf_cache: Dict[str, Dict[str, pd.DataFrame]] = {}
        self._multi_tf_trend_factor = float(self.MULTI_TF_TREND_FACTOR)
        # 执行层拒绝后的symbol级冷却，避免1m循环内重复尝试同一标的
        self._execution_reject_cooldown_until: Dict[str, datetime] = {}
        self._last_positions_for_reconcile: Dict[str, Dict[str, Any]] = {}
        # BTC 牛熊状态缓存：{"regime": "BULL/BEAR/NEUTRAL", "score": float, "ts": float, "details": dict}
        self._btc_regime_cache: Dict[str, Any] = {"regime": "NEUTRAL", "score": 0.0, "ts": 0.0, "details": {}}
        # 交易对牛熊状态缓存：按 symbol 存储，避免同周期重复计算
        self._symbol_regime_cache: Dict[str, Dict[str, Any]] = {}
        # 综合牛熊缓存：按 symbol 存储，给开仓/平仓共用
        self._combined_regime_cache: Dict[str, Dict[str, Any]] = {}
        # 上一次牛熊状态，用于检测转换
        self._last_regime: str = "NEUTRAL"
        # 牛熊转换后的缓冲计数器（避免频繁调仓）
        self._regime_transition_counter: int = 0
        # 【大趋势系统】防止频繁转换
        self._major_regime: str = "NEUTRAL"  # 大趋势状态（仅基于4H）
        self._major_regime_confirm_count: int = 0  # 大趋势确认计数
        self._last_major_transition_time: float = 0.0  # 上次大趋势转换时间
        self._pending_major_regime: Optional[str] = None  # 待确认的大趋势
        # 【机构级趋势评分系统】
        self._trend_score_cache: Dict[str, Any] = {
            "ts": 0.0,  # 综合趋势评分
            "ts_macro": 0.0,  # 宏观层评分
            "ts_market": 0.0,  # 市场层评分
            "ts_asset": {},  # 各交易对评分
            "regime": "NEUTRAL",  # 趋势状态
            "is_oscillation": False,  # 是否震荡市
        }
        self._market_breadth_cache: Dict[str, Any] = {"ts": 0.0, "breadth": 0.0, "dispersion": 0.0}
        self._transition_confirm_state: Dict[str, Any] = {
            "structure_break": False,
            "volume_confirmed": False,
            "btc_confirmed": False,
        }
        # 【牛熊切换状态机】上下文初始化（与 _init_regime_sm_context 保持一致）
        self._regime_sm_ctx: Dict[str, Any] = {
            "_ver": 1,
            "regime": "RANGE",
            "last_switch_ts": 0.0,
            "lock_until_ts": 0.0,
            "flip_times": [],
            "bull_confirm": 0,
            "bear_confirm": 0,
            "last_bos": 0,
            "last_bos_ts": 0.0,
            "last_bos_event_ts_used": None,
            # 【整点缓存】BOS/ATR/VolRatio 只在整点后更新一次
            "cached_bos": 0,
            "cached_bos_ts": 0.0,
            "cached_vol_ratio": 1.0,
            "cached_atr_1h": 0.0,
            "cached_1h_close_time": 0,
            "cache_ttl_sec": 3600,
        }

        # 初始化客户端
        self.client = self._init_binance_client()
        self.ai_client = None
        print("✅ API客户端初始化完成")

        # 初始化管理器
        self.market_data = MarketDataManager(self.client)
        self.position_data = PositionDataManager(self.client)
        self.account_data = AccountDataManager(self.client, config_path=self.config_path)
        print("✅ 数据管理器初始化完成")

        # 初始化交易执行器和风险管理器
        self.trade_executor = TradeExecutor(self.client, self.config)
        self.position_manager = PositionManager(self.client)
        self.risk_manager = RiskManager(self.config)
        print("✅ 交易执行器初始化完成")

        # AI组件 / 规则策略
        if self._is_dual_engine_mode():
            self.strategy = None
            if self.ai_enabled:
                self.ai_client = self._init_ai_client()
                self.prompt_builder = PromptBuilder(self.config)
                self.decision_parser = DecisionParser()
                print("✅ 双引擎策略已启用（震荡套利 + 趋势跟随，AI门禁已开启）")
            else:
                self.ai_client = None
                self.prompt_builder = None
                self.decision_parser = None
                print("✅ 双引擎策略已启用（震荡套利 + 趋势跟随，AI未启用）")
            self._load_dca_rotation_config(initial=True)
            self._load_dca_state()
        elif self.strategy_mode == "V5_RULE":
            self.strategy = V5Strategy(self.config)
            self.prompt_builder = None
            self.decision_parser = None
            print("✅ V5规则策略已启用")
        else:
            self.strategy = None
            self.prompt_builder = PromptBuilder(self.config)
            self.decision_parser = DecisionParser()
            self.ai_client = self._init_ai_client()
            print("✅ AI组件初始化完成")

        # 状态追踪
        self.decision_history: List[Dict[str, Any]] = []
        self.trade_count = 0
        self._dca_live_funding_cache: Dict[str, Dict[str, Any]] = {}
        self._dca_flow_signal_cache: Dict[str, Dict[str, Any]] = {}
        self._dca_flow_prev_oi: Dict[str, Dict[str, float]] = {}
        self._dca_flow_oi_history: Dict[str, List[float]] = {}
        self._flow_confirm_cache: Dict[str, Dict[str, Any]] = {}  # 资金流信号确认缓存
        # 微结构(15s)缓存：按symbol存环形缓冲区，用于聚合到1m/5m并识别盘口陷阱
        self._ms_ring: Dict[str, Any] = {}  # sym -> deque
        self._ms_state: Dict[str, Dict[str, Any]] = {}  # sym -> {last_ts, last_bid_notional, last_ask_notional}
        self._ms_last_agg_trade_id: Dict[str, int] = {}  # sym -> last aggTradeId


        # 预加载历史K线数据
        print("=" * 60)
        print("📊 预加载历史K线数据...")
        print("=" * 60)
        self._preload_historical_data()

        print("=" * 60)
        print("🎉 AI交易机器人启动成功！")
        print("=" * 60)
        print()

    def _apply_network_env_from_config(self) -> None:
        """从配置文件的 network 节点导入网络相关环境变量。"""
        network_cfg = self.config.get("network", {})
        if not isinstance(network_cfg, dict) or not network_cfg:
            return

        bool_mapping = {
            "force_direct": "BINANCE_FORCE_DIRECT",
            "disable_proxy": "BINANCE_DISABLE_PROXY",
            "proxy_fallback": "BINANCE_PROXY_FALLBACK",
            "close_use_proxy": "BINANCE_CLOSE_USE_PROXY",
        }
        str_mapping = {
            "proxy": "BINANCE_PROXY",
            "http_proxy": "BINANCE_HTTP_PROXY",
            "https_proxy": "BINANCE_HTTPS_PROXY",
            "close_proxy": "BINANCE_CLOSE_PROXY",
        }

        for key, env_key in bool_mapping.items():
            if key in network_cfg:
                os.environ[env_key] = "1" if bool(network_cfg.get(key)) else "0"

        for key, env_key in str_mapping.items():
            if key in network_cfg:
                value = network_cfg.get(key)
                if value is None or str(value).strip() == "":
                    os.environ.pop(env_key, None)
                else:
                    os.environ[env_key] = str(value).strip()

        endpoints = (
            network_cfg.get("futures_endpoints")
            or network_cfg.get("fapi_endpoints")
        )
        if endpoints is not None:
            if isinstance(endpoints, list):
                merged = [str(x).strip() for x in endpoints if str(x).strip()]
                if merged:
                    os.environ["BINANCE_FUTURES_ENDPOINTS"] = ",".join(merged)
                else:
                    os.environ.pop("BINANCE_FUTURES_ENDPOINTS", None)
            elif str(endpoints).strip():
                os.environ["BINANCE_FUTURES_ENDPOINTS"] = str(endpoints).strip()
            else:
                os.environ.pop("BINANCE_FUTURES_ENDPOINTS", None)

        print(
            "✅ 已从配置导入网络设置: "
            f"FORCE_DIRECT={os.getenv('BINANCE_FORCE_DIRECT', '')}, "
            f"DISABLE_PROXY={os.getenv('BINANCE_DISABLE_PROXY', '')}, "
            f"CLOSE_USE_PROXY={os.getenv('BINANCE_CLOSE_USE_PROXY', '')}"
        )

    def _init_binance_client(self) -> BinanceClient:
        """初始化Binance客户端（正式网）"""
        api_key, api_secret = EnvManager.get_api_credentials()
        if not api_key or not api_secret:
            raise ValueError("API凭证未配置")

        return BinanceClient(api_key=api_key, api_secret=api_secret)

    def _init_ai_client(self) -> DeepSeekClient:
        """初始化DeepSeek客户端"""
        api_key = EnvManager.get_deepseek_key()
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 未配置")

        model = self.config.get("ai", {}).get("model", "deepseek-reasoner")
        return DeepSeekClient(api_key=api_key, model=model)

    def _setup_logs_directory(self):
        """创建日志目录结构"""
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
        except Exception as e:
            print(f"⚠️ 日志目录创建失败: {e}")

    def _resolve_logs_dir(self, project_root: str) -> str:
        """
        解析日志根目录。
        优先级：
        1) 环境变量 TRADING_LOGS_DIR / BOT_LOGS_DIR
        2) 配置项 logging.logs_dir / logging.dir
        3) Linux 默认 /root/AIBOT/logs
        4) 其他系统默认 <project_root>/logs
        """
        env_dir = os.getenv("TRADING_LOGS_DIR") or os.getenv("BOT_LOGS_DIR")
        cfg_logging = self.config.get("logging", {}) if isinstance(self.config, dict) else {}
        cfg_dir = None
        if isinstance(cfg_logging, dict):
            cfg_dir = cfg_logging.get("logs_dir") or cfg_logging.get("dir")

        candidate = env_dir or cfg_dir
        if candidate:
            raw = str(candidate).strip()
            if raw:
                if os.path.isabs(raw):
                    return os.path.normpath(raw)
                return os.path.normpath(os.path.join(project_root, raw))

        if os.name != "nt":
            return "/root/AIBOT/logs"
        return os.path.join(project_root, "logs")

    def _redirect_terminal_output(self):
        """将终端输出同步写入日志文件"""
        if getattr(sys.stdout, "_is_terminal_logger", False):
            return
        # 使用惰性 provider：在写入时再通过 getattr 获取 _get_log_file_path，
        # 并提供回退实现，避免在实例初始化阶段因方法未绑定而抛出 AttributeError。
        def _log_path_provider():
            try:
                fn = getattr(self, "_get_log_file_path", None)
                if callable(fn):
                    # 强制转换为 str 以满足静态类型期望
                    return str(fn())
            except Exception:
                pass
            # 回退：构造一个简单的按6小时分割的日志文件路径
            try:
                now = datetime.now()
                year_month = now.strftime("%Y-%m")
                hour_block = (now.hour // 6) * 6
                month_dir = os.path.join(self.logs_dir, year_month)
                os.makedirs(month_dir, exist_ok=True)
                log_filename = f"{now.strftime('%Y-%m-%d')}_{hour_block:02d}.txt"
                return os.path.join(month_dir, log_filename)
            except Exception:
                return os.path.join(self.logs_dir, "latest.log")

        stdout_logger = TerminalOutputLogger(sys.stdout, _log_path_provider)
        stderr_logger = TerminalOutputLogger(sys.stderr, _log_path_provider)
        sys.stdout = stdout_logger
        sys.stderr = stderr_logger

    def _preload_historical_data(self):
        """
        预加载历史K线数据
        在启动时为所有交易对下载200根K线，确保有足够的历史数据用于技术分析
        """
        if self._is_dual_engine_mode():
            symbols = self._get_dca_symbols()
            interval = self.dca_config.get("interval", "5m")
            intervals = [interval]
            # DCA + AI 门禁：额外预加载 AI 所需周期
            if self.ai_enabled:
                ai_intervals = ["15m", "30m", "1h", "4h", "1d"]
                for itv in ai_intervals:
                    if itv not in intervals:
                        intervals.append(itv)
        else:
            symbols = ConfigLoader.get_trading_symbols(self.config)
            intervals = ["15m", "30m", "1h", "4h", "1d"]

        print(f"📥 正在为 {len(symbols)} 个交易对预加载历史数据...")
        print(f"   时间周期: {', '.join(intervals)}")
        print("   每个周期: 200根K线")

        for symbol in symbols:
            try:
                print(f"\n   {symbol}:")
                for interval in intervals:
                    # 获取200根K线
                    klines = self.client.get_klines(symbol, interval, limit=200)

                    if klines:
                        print(f"      ✅ {interval:4s} - {len(klines)}根K线")
                    else:
                        print(f"      ⚠️  {interval:4s} - 获取失败")

                print(f"   ✅ {symbol} 历史数据加载完成")

            except Exception as e:
                print(f"   ❌ {symbol} 历史数据加载失败: {e}")

        print("\n✅ 所有交易对历史数据预加载完成")
        print("💡 系统已准备好进行技术分析\n")

    @staticmethod
    def _normalize_dca_symbol(symbol: Any) -> str:
        s = str(symbol or "").strip().upper()
        if not s:
            return ""
        if not s.endswith("USDT"):
            s = f"{s}USDT"
        return s

    def _get_dca_config_symbols(self) -> List[str]:
        """返回配置中的 DCA 交易对池（仅标准化，不做动态过滤）。"""
        raw = self.dca_config.get("symbols", []) or []
        out: List[str] = []
        seen: set[str] = set()
        for sym in raw:
            ns = self._normalize_dca_symbol(sym)
            if not ns or ns in seen:
                continue
            seen.add(ns)
            out.append(ns)
        return out

    def _get_dca_symbols(self) -> List[str]:
        """返回 DCA 候选交易对，并根据配置过滤低流动性品种。

        优化策略（提升胜率至80%+）：
        1. 只交易BTC/ETH/SOL主流币（高流动性、低噪音）
        2. 流动性过滤：24h成交额 >= 1M USDT
        3. 成交量比过滤：15m成交量比 > 150%（放量确认）
        4. 按成交额降序保留前3个（聚焦最优标的）
        """
        # 使用进程内缓存避免在短时间内重复触发大量网络请求与日志输出
        params = self.dca_config.get("params", {}) or {}

        def _interval_to_seconds(interval_str: str) -> int:
            try:
                s = str(interval_str).strip().lower()
                if s.endswith("m") and s[:-1].isdigit():
                    return int(s[:-1]) * 60
                if s.endswith("h") and s[:-1].isdigit():
                    return int(s[:-1]) * 3600
                if s.endswith("d") and s[:-1].isdigit():
                    return int(s[:-1]) * 86400
                # fallback to 30s
                return 30
            except Exception:
                return 30

        # 默认缓存策略：按 K 线周期缓存（例如 interval="5m" -> 缓存 5分钟）
        explicit_cache = params.get("symbols_cache_seconds", None)
        if explicit_cache is not None:
            try:
                cache_secs = int(explicit_cache)
            except Exception:
                cache_secs = 30
        else:
            interval = str(self.dca_config.get("interval", "5m") or "5m")
            cache_secs = _interval_to_seconds(interval)

        now_ts = time.time()
        cached = self._dca_symbols_cache.get("symbols")
        cached_ts = float(self._dca_symbols_cache.get("ts") or 0.0)
        if cached and (now_ts - cached_ts) < float(cache_secs):
            # 返回缓存的符号列表（避免重复筛选日志）
            return list(cached)

        symbols = self.dca_config.get("symbols", [])
        normalized: List[str] = []
        for s in symbols:
            s = s.upper()
            if not s.endswith("USDT"):
                s = f"{s}USDT"
            normalized.append(s)

        # 是否强制主流币白名单（可在配置中覆盖）。默认不强制。
        enforce_mainstream = bool(self.dca_config.get("enforce_mainstream", False))
        if enforce_mainstream:
            mainstream_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
            normalized = [s for s in normalized if s in mainstream_symbols]
            if not normalized:
                print("⚠️ 配置中无主流币(BTC/ETH/SOL)，使用白名单")
                normalized = list(mainstream_symbols)
            print(f"🎯 主流币策略：聚焦 {', '.join(normalized)}")
        else:
            print(f"🎯 使用配置交易对池，共 {len(normalized)} 个候选：{', '.join(normalized)}")

        # 实盘下的筛选策略：允许通过配置控制行为，既可完全禁用细筛（不做任何筛选），
        # 也可仅保留最低流动性防护（防止极小币种）而跳过 15m 成交量/短期价格筛选。
        live_mode = False
        live_min_override = None
        try:
            dry_run_env = os.getenv("BINANCE_DRY_RUN", "")
        except Exception:
            dry_run_env = ""
        if dry_run_env == "0":
            params_local = self.dca_config.get("params", {}) or {}
            disable_live_filter = bool(params_local.get("dca_disable_live_filter", False))
            live_min_override = params_local.get("live_min_daily_volume_usdt", None)
            if disable_live_filter:
                print("⚠️ 实盘模式：dca_disable_live_filter=True，跳过所有细筛，直接使用配置的交易对池")
                try:
                    self._dca_symbols_cache["symbols"] = list(normalized)
                    self._dca_symbols_cache["ts"] = time.time()
                except Exception:
                    pass
                return normalized
            # 否则进入 live_mode：保留最低流动性防护，跳过 15m 细筛
            live_mode = True

        # 读取阈值（单位 USDT）
        min_vol_usdt = 0.0
        try:
            min_vol_usdt = float(self.dca_config.get("min_daily_volume_usdt", 0) or 0)
        except Exception:
            min_vol_usdt = 0.0
        if min_vol_usdt <= 0:
            try:
                params = self.dca_config.get("params", {}) or {}
                min_vol_usdt = float(params.get("min_daily_volume_usdt", 0) or 0)
            except Exception:
                min_vol_usdt = 0.0
        # 默认下限设置为 10k USDT，允许在配置中设置更低值以适配小市值筛选
        if min_vol_usdt <= 0:
            min_vol_usdt = 10_000.0
        enforced_min = max(min_vol_usdt, 0.0)
        min_vol_usdt = enforced_min
        # 若处于 live_mode 且配置提供了 live_min_daily_volume_usdt，则以该值为准
        if live_mode and live_min_override is not None:
            try:
                mv = float(live_min_override)
                if mv > 0:
                    min_vol_usdt = mv
            except Exception:
                pass

        # 【优化2+3】流动性过滤 + 成交量比过滤（15m > 150%）
        verbose_live_filter = os.getenv("BINANCE_VERBOSE_LIVE_FILTER") == "1"
        live_mode_passed: List[tuple[str, float]] = []
        live_mode_rejected: List[tuple[str, float]] = []
        filtered_pairs: List[tuple[str, float]] = []  # (symbol, vol_usdt)
        failed_data_symbols: List[str] = []  # 收集获取数据失败的交易对
        for sym in normalized:
            try:
                # 获取24h流动性数据
                md = self.market_data.get_realtime_market_data(sym)
                if not md:
                    failed_data_symbols.append(sym)
                    continue
                price = float(md.get("price", 0) or 0)
                vol = float(md.get("volume_24h", 0) or 0)
                vol_usdt = price * vol
                
                # 硬筛：先按流动性（24h成交额）过滤，未通过则直接跳过
                if vol_usdt < min_vol_usdt:
                    if live_mode and not verbose_live_filter:
                        live_mode_rejected.append((sym, vol_usdt))
                    else:
                        print(f"⤫ 过滤低流动性: {sym} 24h≈{vol_usdt:,.2f} USDT < min {min_vol_usdt}")
                    continue

                # 若处于 live_mode，则跳过 15m 细筛，仅保留最低流动性防护
                if live_mode:
                    live_mode_passed.append((sym, vol_usdt))
                    if verbose_live_filter:
                        print(f"✅ {sym} 通过实盘最低流动性防护: 24h≈{vol_usdt/1e6:.2f}M USDT (live_mode)")
                    filtered_pairs.append((sym, vol_usdt))
                    continue

                # 在通过流动性硬筛后，进行细筛：15m量比或15m价格变动
                vol_ratio = 0.0
                try:
                    multi_data = self.market_data.get_multi_timeframe_data(sym, ["15m"])
                    if "15m" in multi_data:
                        indicators = multi_data["15m"].get("indicators", {})
                        vol_ratio = float(indicators.get("volume_ratio", 0) or 0)
                except Exception as e:
                    print(f"⚠️ 获取 {sym} 成交量比失败: {e}，将按价格变动判断")

                try:
                    min_15m_ratio = float(self.dca_config.get("params", {}).get("min_15m_vol_ratio", 100.0) or 100.0)
                except Exception:
                    min_15m_ratio = 100.0

                change_15m = float(md.get("change_15m", 0) or 0)
                try:
                    min_price_change = float(self.dca_config.get("params", {}).get("min_price_change_pct", 0.8) or 0.8)
                except Exception:
                    min_price_change = 0.8

                pass_15m = vol_ratio > min_15m_ratio
                pass_price_move = abs(change_15m) >= float(min_price_change)

                if not (pass_15m or pass_price_move):
                    print(f"⤫ 细筛未通过: {sym} (15m量比{vol_ratio:.1f}% <= {min_15m_ratio}%, 15m变动{change_15m:.2f}% < {min_price_change}%)")
                    continue

                reasons = []
                if pass_15m:
                    reasons.append(f"15m量比{vol_ratio:.1f}%")
                if pass_price_move:
                    reasons.append(f"15m变动{change_15m:.2f}%")
                print(f"✅ {sym} 通过过滤: 24h≈{vol_usdt/1e6:.2f}M USDT, {', '.join(reasons)}")
                filtered_pairs.append((sym, vol_usdt))
                    
            except Exception as e:
                print(f"⚠️ 评估 {sym} 失败: {e}")

        # 汇总打印获取数据失败的交易对
        if failed_data_symbols:
            print(f"⚠️ 获取实时数据失败 {len(failed_data_symbols)} 个交易对: {', '.join(failed_data_symbols)}")

        if live_mode and not verbose_live_filter:
            if live_mode_passed:
                passed_symbols = ", ".join(sym for sym, _ in live_mode_passed)
                passed_vols = [vol for _sym, vol in live_mode_passed]
                print(
                    f"✅ 实盘最低流动性防护通过 {len(live_mode_passed)}/{len(normalized)} 个交易对: {passed_symbols}"
                )
                print(
                    f"   24h成交额范围: {min(passed_vols)/1e6:.2f}M ~ {max(passed_vols)/1e6:.2f}M USDT"
                )
            if live_mode_rejected:
                rejected_symbols = ", ".join(sym for sym, _ in live_mode_rejected)
                print(
                    f"⤫ 实盘最低流动性防护未通过 {len(live_mode_rejected)}/{len(normalized)} 个交易对: {rejected_symbols}"
                )

        if not filtered_pairs:
            print("⚠️ 所有候选标的被过滤（成交量不足），本周期无符合条件的交易对")
            print("   → 策略执行: 等待高波动时段或成交量放大")
            # 缓存空结果以避免重复查询
            self._dca_symbols_cache["symbols"] = []
            self._dca_symbols_cache["ts"] = time.time()
            return []  # 返回空列表，让系统跳过交易

        # 使用评分优先的选择：先为每个通过过滤的交易对计算 DCA 评分（short/long），
        # 然后按评分降序排序，必要时以成交额作为二次排序键以保证流动性优先。
        # 【优化】分别收集多单和空单候选
        long_candidates: List[tuple[str, float, float]] = []  # (symbol, vol_usdt, long_score)
        short_candidates: List[tuple[str, float, float]] = []  # (symbol, vol_usdt, short_score)
        # 读取用于评分的阈值（与策略一致）
        try:
            params = self.dca_config.get("params", {}) or {}
            rsi_entry_short = float(params.get("rsi_entry_short", params.get("rsi_entry", 70)))
            rsi_entry_long = float(params.get("rsi_entry_long", 100 - rsi_entry_short))
        except Exception:
            rsi_entry_short = 70.0
            rsi_entry_long = 30.0

        # 计算条形时长（分钟），用于指标计算
        bar_minutes = 5
        try:
            interval = str(self.dca_config.get("interval", "5m") or "5m")
            if interval.endswith("m") and interval[:-1].isdigit():
                bar_minutes = int(interval[:-1])
        except Exception:
            bar_minutes = 5

        for sym, vol_usdt in filtered_pairs:
            try:
                # 获取 K 线并计算指标以获得与 _dca_score_pair 兼容的 row
                df = self._dca_get_klines_df(sym, interval, limit=200)
                if df is not None and len(df) >= max(50, 20):
                    df = self._dca_calc_indicators(df, bar_minutes)
                    row = df.iloc[-1]
                    short_score, long_score = self._dca_score_pair(row, rsi_entry_short, rsi_entry_long)
                    # 分别收集多单和空单候选
                    if long_score > 0:
                        long_candidates.append((sym, vol_usdt, long_score))
                    if short_score > 0:
                        short_candidates.append((sym, vol_usdt, short_score))
            except Exception as e:
                print(f"⚠️ 为 {sym} 计算评分失败: {e}")

        # 分别按评分降序排序
        long_candidates.sort(key=lambda x: (x[2], x[1]), reverse=True)
        short_candidates.sort(key=lambda x: (x[2], x[1]), reverse=True)
        
        # 读取配置：多单和空单各选择多少个
        try:
            top_n_per_side = int(self.dca_config.get("params", {}).get("top_n_per_side", 4) or 4)
        except Exception:
            top_n_per_side = 4
        top_n_per_side = max(1, min(top_n_per_side, 8))  # 每边最多8个

        # 选择多单候选
        long_selected = [s for s, _v, _sc in long_candidates[:top_n_per_side]]
        # 选择空单候选
        short_selected = [s for s, _v, _sc in short_candidates[:top_n_per_side]]
        
        # 合并为最终候选列表（去重）
        selected = list(dict.fromkeys(long_selected + short_selected))

        # 打印分方向的选择结果
        if long_selected:
            long_scores = {s: f"{sc:.3f}" for s, _v, sc in long_candidates[:top_n_per_side]}
            print(f"📈 多单候选 {len(long_selected)} 个: {', '.join(long_selected)} (score: {long_scores})")
        if short_selected:
            short_scores = {s: f"{sc:.3f}" for s, _v, sc in short_candidates[:top_n_per_side]}
            print(f"📉 空单候选 {len(short_selected)} 个: {', '.join(short_selected)} (score: {short_scores})")

        if len(selected) < 4:
            print(f"⚠️ 过滤后仅剩 {len(selected)} 个交易对，少于建议的4个。若需更多，请降低过滤阈值或扩大 symbols 列表。")

        # 更新缓存
        try:
            self._dca_symbols_cache["symbols"] = list(selected)
            self._dca_symbols_cache["ts"] = time.time()
        except Exception:
            pass
        return selected

    def _load_dca_rotation_config(self, initial: bool = False) -> None:
        if not os.path.exists(self.dca_config_path):
            if initial:
                print(f"⚠️ 未找到交易配置文件: {self.dca_config_path}")
            return
        try:
            mtime = os.path.getmtime(self.dca_config_path)
            if not initial and self.dca_config_mtime is not None and mtime == self.dca_config_mtime:
                return
            with open(self.dca_config_path, "r", encoding="utf-8") as f:
                trading_cfg = json.load(f)
            self.dca_config = trading_cfg.get("dca_rotation", {})
            self._apply_oscillation_overrides_from_risk()
            self._apply_flow_profile_template()
            self._apply_dual_engine_runtime_params()
            self.dca_config_mtime = mtime
            self._apply_data_endpoints()
            print(f"✅ 已加载 DCA 轮动配置 ({os.path.basename(self.dca_config_path)})")
            if initial:
                self._print_risk_summary()
        except Exception as e:
            print(f"❌ 读取 DCA 配置失败: {e}")

    def _apply_dual_engine_runtime_params(self) -> None:
        """应用双引擎运行时参数（执行层周期等）。"""
        params = self.dca_config.get("params", {}) if isinstance(self.dca_config, dict) else {}
        if not isinstance(params, dict):
            return
        raw_exec = params.get("execution_cycle_seconds", params.get("dual_engine_exec_interval_seconds", 300))
        try:
            exec_interval = int(raw_exec if raw_exec is not None else 300)
        except Exception:
            exec_interval = 300
        exec_interval = max(5, min(300, exec_interval))
        self._dual_engine_exec_interval_seconds = exec_interval

    def _apply_oscillation_overrides_from_risk(self) -> None:
        """
        将 risk.oscillation 下的同层参数覆盖到 dca_rotation.params，
        让震荡开仓门禁、出场参数和 RANGE/RANGE_LOCK ratio
        可在 risk 顶层统一控制。
        """
        risk_cfg = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
        if not isinstance(risk_cfg, dict):
            return
        osc_cfg = risk_cfg.get("oscillation", {})
        if not isinstance(osc_cfg, dict) or not osc_cfg:
            return

        params = self.dca_config.setdefault("params", {})
        if not isinstance(params, dict):
            return

        applied: List[str] = []

        entry_gate = osc_cfg.get("entry_gate", {})
        if isinstance(entry_gate, dict):
            for key in (
                "min_score_long",
                "min_score_short",
                "max_score_short",
                "score_gap_gate_enabled",
                "min_score_gap_short",
                "min_score_gap_long",
            ):
                if key in entry_gate and entry_gate.get(key) is not None:
                    params[key] = entry_gate.get(key)
                    applied.append(f"entry_gate.{key}")

        exit_cfg = osc_cfg.get("exit", {})
        if isinstance(exit_cfg, dict):
            for key in (
                "take_profit_pct",
                "symbol_stop_loss_pct",
                "break_even_trigger_pct",
                "trailing_stop_trigger_pct",
                "trailing_stop_pct",
            ):
                if key in exit_cfg and exit_cfg.get(key) is not None:
                    params[key] = exit_cfg.get(key)
                    applied.append(f"exit.{key}")

        osc_mode = params.setdefault("oscillation_mode", {})
        if isinstance(osc_mode, dict):
            ratio_keys = (
                "take_profit_ratio",
                "stop_loss_ratio",
                "break_even_trigger_ratio",
                "trailing_trigger_ratio",
                "trailing_stop_ratio",
                "trailing_stop_after_be_ratio",
            )
            for key in ratio_keys:
                if key in osc_cfg and osc_cfg.get(key) is not None:
                    osc_mode[key] = osc_cfg.get(key)
                    applied.append(f"ratio.{key}")

        if applied:
            print(f"✅ 已应用 risk.oscillation 覆盖到 DCA 参数: {', '.join(applied)}")

    def _apply_flow_profile_template(self) -> None:
        """
        资金流模板覆盖：
        - dca_rotation.params.flow_profile: 当前模板名（如 loose / strict）
        - dca_rotation.params.flow_profiles: 模板字典
        模板值会覆盖到 params 同名字段，支持按模板一键切换门禁强度。
        """
        params = self.dca_config.setdefault("params", {})
        if not isinstance(params, dict):
            return
        profile_name = str(params.get("flow_profile", "") or "").strip().lower()
        profiles = params.get("flow_profiles", {})
        if not profile_name or not isinstance(profiles, dict):
            return
        profile_cfg = profiles.get(profile_name)
        if not isinstance(profile_cfg, dict):
            print(f"⚠️ flow_profile={profile_name} 未匹配到 flow_profiles 模板，忽略")
            return

        applied_keys: List[str] = []
        for key, value in profile_cfg.items():
            if key in ("flow_thresholds", "flow_factor_weights") and isinstance(value, dict):
                base_cfg = params.get(key, {})
                merged_cfg = dict(base_cfg) if isinstance(base_cfg, dict) else {}
                merged_cfg.update(value)
                params[key] = merged_cfg
            else:
                params[key] = value
            applied_keys.append(str(key))
        print(
            f"✅ 已应用资金流模板: {profile_name} "
            f"(keys={','.join(applied_keys)})"
        )

    def _apply_data_endpoints(self) -> None:
        endpoints = self.dca_config.get("download_endpoints", {})
        if not endpoints:
            return
        spot = endpoints.get("spot", [])
        futures = endpoints.get("futures", [])
        if spot:
            set_custom_endpoints("spot", spot)
        if futures:
            set_custom_endpoints("futures", futures)
        print("✅ 已配置 K 线下载端点")

    def _print_risk_summary(self) -> None:
        """打印风险摘要，包括核心风控参数和 score 过滤阈值"""
        params = self.dca_config.get("params", {}) or {}
        risk_cfg = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
        if not isinstance(risk_cfg, dict):
            risk_cfg = {}
        osc_cfg = risk_cfg.get("oscillation", {}) if isinstance(risk_cfg.get("oscillation", {}), dict) else {}
        trend_cfg = risk_cfg.get("trend", {}) if isinstance(risk_cfg.get("trend", {}), dict) else {}
        trend_exit = trend_cfg.get("exit", {}) if isinstance(trend_cfg.get("exit", {}), dict) else {}

        # Score 过滤阈值
        min_score_long = float(params.get("min_score_long", 0.1))
        max_score_short = float(params.get("max_score_short", 0.0))
        min_score_short = float(params.get("min_score_short", min_score_long))
        disable_osc_long_entries = self._coerce_bool(params.get("disable_osc_long_entries", True), True)
        # score-gap 门禁：可配置开关
        score_gap_gate_enabled = bool(params.get("score_gap_gate_enabled", False))
        min_score_gap_short = float(params.get("min_score_gap_short", 0.0) or 0.0)
        stopout_size_reduce_enabled = bool(params.get("stopout_size_reduce_enabled", False))
        try:
            stopout_size_reduce_after = int(params.get("stopout_size_reduce_after", 2) or 2)
        except Exception:
            stopout_size_reduce_after = 2
        try:
            stopout_size_reduce_factor = float(params.get("stopout_size_reduce_factor", 0.5) or 0.5)
        except Exception:
            stopout_size_reduce_factor = 0.5
        stopout_size_reduce_factor = self._clamp_value(stopout_size_reduce_factor, 0.05, 1.0)
        trend_gate_tighten_cfg = (
            params.get("trend_gate_tighten", {})
            if isinstance(params.get("trend_gate_tighten", {}), dict)
            else {}
        )
        range_gate_tighten_cfg = (
            params.get("range_gate_tighten", {})
            if isinstance(params.get("range_gate_tighten", {}), dict)
            else {}
        )
        try:
            _v = trend_gate_tighten_cfg.get("min_score_delta", 0.01)
            trend_score_tighten = float(0.01 if _v is None else _v)
        except Exception:
            trend_score_tighten = 0.01
        try:
            _v = trend_gate_tighten_cfg.get("confirm_rsi_delta", 1.0)
            trend_rsi_tighten = float(1.0 if _v is None else _v)
        except Exception:
            trend_rsi_tighten = 1.0
        try:
            _v = range_gate_tighten_cfg.get("rsi_delta", 1.0)
            range_rsi_tighten = float(1.0 if _v is None else _v)
        except Exception:
            range_rsi_tighten = 1.0
        try:
            _v = range_gate_tighten_cfg.get("bb_touch_delta", 0.002)
            range_bb_tighten = float(0.002 if _v is None else _v)
        except Exception:
            range_bb_tighten = 0.002
        try:
            _v = range_gate_tighten_cfg.get("vol_q_delta", 0.03)
            range_vol_tighten = float(0.03 if _v is None else _v)
        except Exception:
            range_vol_tighten = 0.03

        # 持仓限制
        max_positions = int(params.get("max_positions", 4))
        max_long_positions = int(params.get("max_long_positions", 2))
        max_short_positions = int(params.get("max_short_positions", 2))

        # 仓位和杠杆
        leverage = int(params.get("leverage", 10))
        max_position_pct = float(params.get("max_position_pct", 0.28))

        # 止盈止损
        take_profit_pct = float(params.get("take_profit_pct", 0.012))
        symbol_stop_loss_pct = float(params.get("symbol_stop_loss_pct", 0.03))
        total_stop_loss_pct = float(params.get("total_stop_loss_pct", 0.12))
        total_stop_loss_cooldown_seconds = self._dca_get_total_stop_loss_cooldown_seconds(params)
        exec_cfg = params.get("execution_layer", {}) if isinstance(params.get("execution_layer", {}), dict) else {}
        exec_enabled = self._coerce_bool(exec_cfg.get("enabled", True), True)
        exec_tf = str(exec_cfg.get("timeframe", "1m") or "1m")
        flow_enabled = self._coerce_bool(params.get("flow_signal_enabled", False), False)
        # 与执行层一致：资金流无数据强制拦截
        flow_allow_no_data_pass = False
        flow_hard_gate = self._coerce_bool(params.get("flow_signal_hard_gate", False), False)
        flow_affect_confidence = self._coerce_bool(params.get("flow_affect_confidence", False), False)
        flow_profile = str(params.get("flow_profile", "") or "").strip().lower()
        flow_only_gate_mode = self._coerce_bool(params.get("flow_only_gate_mode", False), False)
        dual_engine_direction_gate_enabled = self._coerce_bool(
            params.get("dual_engine_direction_gate_enabled", not flow_only_gate_mode),
            (not flow_only_gate_mode),
        )
        dual_engine_quota_gate_enabled = self._coerce_bool(
            params.get("dual_engine_quota_gate_enabled", not flow_only_gate_mode),
            (not flow_only_gate_mode),
        )
        try:
            flow_ttl = int(params.get("flow_signal_interval_seconds", 15) or 15)
        except Exception:
            flow_ttl = 15
        flow_ttl = max(5, min(300, flow_ttl))
        try:
            _v = params.get("flow_min_hits_short", params.get("flow_min_hits", 2))
            flow_hits_short = int(2 if _v is None else _v)
        except Exception:
            flow_hits_short = 2
        try:
            _v = params.get("flow_min_hits_long", params.get("flow_min_hits", 2))
            flow_hits_long = int(2 if _v is None else _v)
        except Exception:
            flow_hits_long = 2
        try:
            _v = params.get("flow_min_score_short", params.get("flow_min_score", 0.35))
            flow_score_short = float(0.35 if _v is None else _v)
        except Exception:
            flow_score_short = 0.35
        try:
            _v = params.get("flow_min_score_long", params.get("flow_min_score", 0.35))
            flow_score_long = float(0.35 if _v is None else _v)
        except Exception:
            flow_score_long = 0.35

        osc_mode = params.get("oscillation_mode", {}) if isinstance(params.get("oscillation_mode", {}), dict) else {}
        osc_ratio_src = "risk.oscillation.*_ratio" if any(
            k in osc_cfg
            for k in (
                "take_profit_ratio",
                "stop_loss_ratio",
                "break_even_trigger_ratio",
                "trailing_trigger_ratio",
                "trailing_stop_ratio",
                "trailing_stop_after_be_ratio",
            )
        ) else "dca_rotation.params.oscillation_mode"

        def _ratio_text(key: str) -> str:
            cfg = osc_mode.get(key)
            if isinstance(cfg, dict):
                rg = cfg.get("RANGE")
                rl = cfg.get("RANGE_LOCK", cfg.get("RANGE"))
                return f"RANGE={rg}, RANGE_LOCK={rl}"
            return str(cfg)

        osc_entry_src = "risk.oscillation.entry_gate" if osc_cfg.get("entry_gate") else "dca_rotation.params"
        osc_exit_src = "risk.oscillation.exit" if osc_cfg.get("exit") else "dca_rotation.params"
        trend_exit_src = "risk.trend.exit" if trend_exit else "params(base)"

        print("\n" + "=" * 50)
        print("📊 RISK SUMMARY - 风险摘要")
        print("=" * 50)
        print(f"{'[仓位控制]':<20}")
        print(f"  leverage            = {leverage}x")
        print(f"  max_position_pct    = {max_position_pct * 100:.1f}%")
        print(f"{'[持仓限制]':<20}")
        print(f"  max_positions       = {max_positions}")
        print(f"  max_long_positions  = {max_long_positions}")
        print(f"  max_short_positions = {max_short_positions}")
        print(f"{'[止盈止损]':<20}")
        print(f"  take_profit_pct     = {take_profit_pct * 100:.2f}%")
        print(f"  symbol_stop_loss    = {symbol_stop_loss_pct * 100:.2f}%")
        print(f"  total_stop_loss     = {total_stop_loss_pct * 100:.1f}%")
        print(f"  total_stop_cooldown = {total_stop_loss_cooldown_seconds}s")
        print(f"{'[执行层]':<20}")
        print(f"  execution_layer     = {'on' if exec_enabled else 'off'} ({exec_tf})")
        print(f"{'[开仓门槛]':<20}")
        print(f"  flow_profile        = {flow_profile or '(none)'}")
        print(f"  flow_only_mode      = {'on' if flow_only_gate_mode else 'off'}")
        print(f"  dual_dir_gate       = {'on' if dual_engine_direction_gate_enabled else 'off'}")
        print(f"  dual_quota_gate     = {'on' if dual_engine_quota_gate_enabled else 'off'}")
        print("  p_win/edge_gate     = removed")
        print(
            "  score_gap_gate      = "
            f"{'on' if score_gap_gate_enabled else 'off'} "
            f"(short_score-th>={min_score_gap_short:.4f})"
        )
        print(
            "  disable_osc_long    = "
            f"{'on' if disable_osc_long_entries else 'off'} "
            "(RANGE禁LONG+osc_long)"
        )
        print(
            "  gate_tighten        = "
            f"trend(score+{trend_score_tighten:.3f},rsi±{trend_rsi_tighten:.1f}), "
            f"range(rsi±{range_rsi_tighten:.1f},bb-{range_bb_tighten:.3f},vq-{range_vol_tighten:.2f})"
        )
        print(
            "  stopout_size_reduce = "
            f"{'on' if stopout_size_reduce_enabled else 'off'} "
            f"(after={stopout_size_reduce_after}, factor={stopout_size_reduce_factor:.2f})"
        )
        print(
            "  flow_signal_gate    = "
            f"{'on' if flow_enabled else 'off'} "
            f"(ttl={flow_ttl}s, short>={flow_score_short:.2f}/{flow_hits_short}hits, "
            f"long>={flow_score_long:.2f}/{flow_hits_long}hits, "
            f"no_data_pass={'on' if flow_allow_no_data_pass else 'off'}, "
            f"mode={'hard' if flow_hard_gate else 'assist'}, "
            f"affect_conf={'on' if flow_affect_confidence else 'off'})"
        )
        print(f"{'[Score 过滤]':<20}")
        print(f"  min_score_long      = {min_score_long:.2f}  (做多最低分数)")
        print(f"  min_score_short     = {min_score_short:.2f}  (做空最低分数，基于short_score)")
        print(f"  max_score_short     = {max_score_short:.2f}  (兼容旧参数)")
        print(f"{'[震荡参数来源]':<20}")
        print(f"  entry_gate_source   = {osc_entry_src}")
        print(f"  exit_source         = {osc_exit_src}")
        print(f"  ratio_source        = {osc_ratio_src}")
        print(f"{'[震荡ratio]':<20}")
        print(f"  take_profit_ratio   = {_ratio_text('take_profit_ratio')}")
        print(f"  stop_loss_ratio     = {_ratio_text('stop_loss_ratio')}")
        print(f"  break_even_ratio    = {_ratio_text('break_even_trigger_ratio')}")
        print(f"  trailing_trig_ratio = {_ratio_text('trailing_trigger_ratio')}")
        print(f"  trailing_stop_ratio = {_ratio_text('trailing_stop_ratio')}")
        print(f"  trail_after_be      = {_ratio_text('trailing_stop_after_be_ratio')}")
        print(f"{'[趋势出场基线]':<20}")
        print(f"  trend_exit_source   = {trend_exit_src}")
        print(f"  trend_tp_pct        = {trend_exit.get('take_profit_pct', '(fallback)')}")
        print(f"  trend_sl_pct        = {trend_exit.get('symbol_stop_loss_pct', '(fallback)')}")
        print(f"  trend_be_trig_pct   = {trend_exit.get('break_even_trigger_pct', '(fallback)')}")
        print(f"  trend_trig_pct      = {trend_exit.get('trailing_stop_trigger_pct', '(fallback)')}")
        print(f"  trend_trail_pct     = {trend_exit.get('trailing_stop_pct', '(fallback)')}")
        print("=" * 50 + "\n")

    def _load_dca_state(self) -> None:
        if not os.path.exists(self.dca_state_path):
            return
        try:
            with open(self.dca_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.dca_halt = bool(data.get("dca_halt", False))
            if self.dca_halt:
                # 旧版本曾将总回撤止损写为永久停机。迁移到新版后自动清理，避免持续锁死。
                print("⚠️ 检测到旧状态 dca_halt=True，已自动清理并改为冷却恢复模式")
                self.dca_halt = False
            # 恢复连续亏损计数和冷却信息（由亏损触发的冷却）
            self.consecutive_losses = int(data.get("consecutive_losses", 0) or 0)
            self.consecutive_stopouts = int(data.get("consecutive_stopouts", 0) or 0)
            cooldown_expires = data.get("dca_cooldown_expires")
            if cooldown_expires:
                try:
                    self.dca_cooldown_expires = datetime.fromisoformat(cooldown_expires)
                except Exception:
                    self.dca_cooldown_expires = None
            self.dca_cooldown_reason = data.get("dca_cooldown_reason")
            # 恢复当天开盘权益
            self.dca_day_open_equity = data.get("dca_day_open_equity")
            self.dca_day_open_date = data.get("dca_day_open_date")
            self.dca_day_open_tz = data.get("dca_day_open_tz")
            self.dca_initial_equity = data.get("dca_initial_equity")
            self.dca_peak_equity = data.get("dca_peak_equity")
            self._last_dca_snapshot_key = data.get("last_dca_snapshot_key")
            last_entry = data.get("dca_last_entry_time")
            if last_entry:
                self.dca_last_entry_time = datetime.fromisoformat(last_entry)
            state = data.get("dca_state", {})
            cleaned = {}
            for symbol, s in state.items():
                entry_time = s.get("entry_time")
                if entry_time:
                    try:
                        s["entry_time"] = datetime.fromisoformat(entry_time)
                    except Exception:
                        s["entry_time"] = datetime.now()
                cleaned[symbol] = s
            self.dca_state = cleaned
            print("✅ 已恢复 DCA 状态")
        except Exception as e:
            print(f"⚠️ DCA 状态恢复失败: {e}")

    def _save_dca_state(self) -> None:
        try:
            last_entry_time = self.dca_last_entry_time
            payload = {
                "dca_halt": self.dca_halt,
                "consecutive_losses": int(self.consecutive_losses or 0),
                "consecutive_stopouts": int(self.consecutive_stopouts or 0),
                "dca_cooldown_expires": (
                    self.dca_cooldown_expires.isoformat() if isinstance(self.dca_cooldown_expires, datetime) else None
                ),
                "dca_cooldown_reason": self.dca_cooldown_reason,
                "dca_day_open_equity": self.dca_day_open_equity,
                "dca_day_open_tz": self.dca_day_open_tz,
                "dca_day_open_date": self.dca_day_open_date,
                "dca_initial_equity": self.dca_initial_equity,
                "dca_peak_equity": self.dca_peak_equity,
                "dca_last_entry_time": last_entry_time.isoformat() if isinstance(last_entry_time, datetime) else None,
                "last_dca_snapshot_key": self._last_dca_snapshot_key,
                "dca_state": {},
            }
            for symbol, s in self.dca_state.items():
                entry_time = s.get("entry_time")
                payload["dca_state"][symbol] = {
                    **s,
                    "entry_time": entry_time.isoformat() if isinstance(entry_time, datetime) else None,
                }
            os.makedirs(self.logs_dir, exist_ok=True)
            with open(self.dca_state_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ DCA 状态保存失败: {e}")

    def _reconcile_dca_state(self, positions: Dict[str, Dict[str, Any]]) -> None:
        current_symbols = set(positions.keys())
        state_symbols = set(self.dca_state.keys())

        # remove stale states (no position)
        for symbol in list(state_symbols - current_symbols):
            self.dca_state.pop(symbol, None)

        # add missing states for existing positions
        for symbol in current_symbols:
            pos = positions.get(symbol)
            if not pos:
                self.dca_state.pop(symbol, None)
                continue
            side = str(pos.get("side", "")).upper()
            if side not in ("LONG", "SHORT"):
                self.dca_state.pop(symbol, None)
                continue

            st = self.dca_state.get(symbol)
            if isinstance(st, dict):
                old_side = str(st.get("side", "")).upper()
                if old_side in ("LONG", "SHORT") and old_side != side:
                    # 反手后重置，避免沿用旧方向的 DCA 轨迹
                    self.dca_state.pop(symbol, None)
                    st = None

            if symbol not in self.dca_state:
                entry_price = float(pos.get("entry_price", 0))
                self.dca_state[symbol] = {
                    "side": side,
                    "engine": "UNKNOWN",
                    "entry_regime": None,
                    "last_dca_price": entry_price,
                    "dca_count": 0,
                    "entry_time": datetime.now(),
                    "peak_pnl_pct": 0.0,
                    "be_active": False,
                }
            else:
                self.dca_state[symbol]["side"] = side
                # 兼容历史 state，确保字段齐全
                st2 = self.dca_state.get(symbol)
                if isinstance(st2, dict):
                    st2.setdefault("last_dca_price", float(pos.get("entry_price", 0) or 0))
                    st2.setdefault("dca_count", 0)
                    st2.setdefault("entry_time", datetime.now())
                    st2.setdefault("peak_pnl_pct", 0.0)
                    st2.setdefault("be_active", False)
                    st2.setdefault("entry_regime", None)
                    if str(st2.get("engine", "")).upper() not in ("RANGE", "TREND"):
                        st2["engine"] = "UNKNOWN"

    def _write_dca_dashboard(
        self,
        positions: Dict[str, Dict[str, Any]],
        event: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            account_summary = self.account_data.get_account_summary() or {}
            equity = float(account_summary.get("equity", 0))
            peak = self.dca_peak_equity or equity
            drawdown = (peak - equity) / peak if peak > 0 else 0.0

            payload = {
                "timestamp": datetime.now().isoformat(),
                "equity": equity,
                "peak_equity": peak,
                "drawdown_pct": round(drawdown * 100, 2),
                "day_open_equity": self.dca_day_open_equity,
                "day_open_date": self.dca_day_open_date,
                "day_open_tz": self.dca_day_open_tz,
                "dca_cooldown_expires": (
                    self.dca_cooldown_expires.isoformat() if isinstance(self.dca_cooldown_expires, datetime) else None
                ),
                "dca_cooldown_reason": self.dca_cooldown_reason,
                "consecutive_losses": int(self.consecutive_losses or 0),
                "open_orders": int(self._last_open_orders_count or 0),
                "api_probe": self.api_probe_info,
                "event": event if isinstance(event, dict) else None,
                "positions": [],
            }

            for symbol, pos in positions.items():
                state = self.dca_state.get(symbol, {})
                payload["positions"].append(
                    {
                        "symbol": symbol,
                        "side": pos.get("side"),
                        "engine": state.get("engine"),
                        "entry_price": pos.get("entry_price"),
                        "mark_price": pos.get("mark_price"),
                        "pnl_percent": pos.get("pnl_percent"),
                        "dca_count": state.get("dca_count", 0),
                        "last_dca_price": state.get("last_dca_price"),
                        "entry_time": self._fmt_dt(state.get("entry_time")),
                    }
                )

            with open(self.dca_dashboard_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self._write_dca_dashboard_csv(payload)
            self._write_dca_dashboard_html(payload)
        except Exception as e:
            print(f"⚠️ DCA 看板写入失败: {e}")

    def _write_dca_dashboard_csv(self, payload: Dict[str, Any]) -> None:
        header = [
            "timestamp",
            "equity",
            "peak_equity",
            "drawdown_pct",
            "symbol",
            "side",
            "engine",
            "entry_price",
            "mark_price",
            "pnl_percent",
            "dca_count",
            "last_dca_price",
            "entry_time",
            "event_type",
            "event_symbol",
            "event_side",
            "event_status",
            "event_quantity",
            "event_price",
            "event_pnl",
            "event_pnl_percent",
            "event_reason",
        ]
        # 尝试以更鲁棒的方式写入 CSV：捕获 PermissionError 并重试，创建文件时使用临时文件替换以保证原子性
        max_retries = 5
        backoff = 0.5
        written = False
        rows = []
        raw_event = payload.get("event")
        event: Dict[str, Any]
        if isinstance(raw_event, dict):
            event = raw_event
        else:
            event = {}
        event_type = str(event.get("type", "") or "")
        event_symbol = str(event.get("symbol", "") or "")
        event_side = str(event.get("side", "") or "")
        event_status = str(event.get("status", "") or "")
        event_quantity = event.get("quantity")
        event_price = event.get("price")
        event_pnl = event.get("pnl")
        event_pnl_percent = event.get("pnl_percent")
        event_reason = str(event.get("reason", "") or "")
        for pos in payload.get("positions", []):
            rows.append(
                [
                    payload.get("timestamp"),
                    payload.get("equity"),
                    payload.get("peak_equity"),
                    payload.get("drawdown_pct"),
                    pos.get("symbol"),
                    pos.get("side"),
                    pos.get("engine"),
                    pos.get("entry_price"),
                    pos.get("mark_price"),
                    pos.get("pnl_percent"),
                    pos.get("dca_count"),
                    pos.get("last_dca_price"),
                    pos.get("entry_time"),
                    event_type,
                    event_symbol,
                    event_side,
                    event_status,
                    event_quantity,
                    event_price,
                    event_pnl,
                    event_pnl_percent,
                    event_reason,
                ]
            )
        if not rows and event:
            rows.append(
                [
                    payload.get("timestamp"),
                    payload.get("equity"),
                    payload.get("peak_equity"),
                    payload.get("drawdown_pct"),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    event_type,
                    event_symbol,
                    event_side,
                    event_status,
                    event_quantity,
                    event_price,
                    event_pnl,
                    event_pnl_percent,
                    event_reason,
                ]
            )

        for attempt in range(1, max_retries + 1):
            try:
                os.makedirs(self.logs_dir, exist_ok=True)
                exists = os.path.exists(self.dca_dashboard_csv_path)
                if exists:
                    try:
                        with open(self.dca_dashboard_csv_path, "r", newline="", encoding="utf-8") as rf:
                            first_row = next(csv.reader(rf), None)
                        if first_row != header:
                            legacy_path = self.dca_dashboard_csv_path + ".legacy.%s" % datetime.now().strftime("%Y%m%dT%H%M%S")
                            shutil.move(self.dca_dashboard_csv_path, legacy_path)
                            print(f"ℹ️ DCA 看板 CSV 表头已升级，旧文件已备份: {legacy_path}")
                            exists = False
                    except Exception:
                        pass
                # 如果文件不存在，先写入临时文件再替换，避免并发创建时的竞争
                if not exists:
                    dir_name = os.path.dirname(self.dca_dashboard_csv_path)
                    fd, tmp_path = tempfile.mkstemp(prefix="dca_dashboard_", dir=dir_name, text=True)
                    os.close(fd)
                    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(header)
                        for r in rows:
                            writer.writerow(r)
                    # 原子替换（在同一文件系统上）
                    shutil.move(tmp_path, self.dca_dashboard_csv_path)
                else:
                    # 直接以追加方式写入
                    with open(self.dca_dashboard_csv_path, "a", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        for r in rows:
                            writer.writerow(r)
                written = True
                break
            except PermissionError as pe:
                print(f"⚠️ DCA 看板写入被拒绝（第{attempt}次）：{pe}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            except Exception as e:
                print(f"⚠️ DCA 看板写入异常（第{attempt}次）：{e}")
                try:
                    import traceback

                    traceback.print_exc()
                except Exception:
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

        if not written:
            # 最后回退：保存到一个错误文件以免数据丢失
            try:
                err_path = self.dca_dashboard_csv_path + ".err.%s" % datetime.now().strftime("%Y%m%dT%H%M%S")
                with open(err_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(header)
                    for r in rows:
                        writer.writerow(r)
                print(f"❌ DCA 看板写入失败，已保存到备份: {err_path}")
            except Exception as e:
                print(f"❌ 无法保存 DCA 看板备份: {e}")
        else:
            self._sync_dca_dashboard_snapshot()

    def _record_dca_trade_event(
        self,
        *,
        event_type: str,
        symbol: str,
        side: Optional[str] = None,
        status: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        pnl: Optional[float] = None,
        pnl_percent: Optional[float] = None,
        reason: str = "",
    ) -> None:
        """开仓/平仓后立刻写入一次 DCA 看板快照事件。"""
        if not self._is_dual_engine_mode():
            return
        try:
            latest_positions = self.position_data.get_all_positions() or {}
            event_payload: Dict[str, Any] = {
                "timestamp": datetime.now().isoformat(),
                "type": str(event_type or ""),
                "symbol": str(symbol or ""),
                "side": str(side or ""),
                "status": str(status or ""),
                "quantity": quantity,
                "price": price,
                "pnl": pnl,
                "pnl_percent": pnl_percent,
                "reason": str(reason or ""),
            }
            self._write_dca_dashboard(latest_positions, event=event_payload)
        except Exception as e:
            print(f"⚠️ DCA 事件快照写入失败: {e}")

    def _write_dca_dashboard_html(self, payload: Dict[str, Any]) -> None:
        rows = []
        for pos in payload.get("positions", []):
            pnl = pos.get("pnl_percent")
            pnl_class = "pnl-pos" if pnl is not None and pnl >= 0 else "pnl-neg"
            rows.append(
                "<tr>"
                f"<td>{pos.get('symbol')}</td>"
                f"<td>{pos.get('side')}</td>"
                f"<td>{pos.get('engine')}</td>"
                f"<td>{pos.get('entry_price')}</td>"
                f"<td>{pos.get('mark_price')}</td>"
                f"<td class='{pnl_class}'>{pos.get('pnl_percent')}</td>"
                f"<td>{pos.get('dca_count')}</td>"
                f"<td>{pos.get('last_dca_price')}</td>"
                f"<td>{pos.get('entry_time')}</td>"
                "</tr>"
            )
        table_rows = "\n".join(rows) if rows else "<tr><td colspan='9'>无持仓</td></tr>"
        api_probe = payload.get("api_probe") or {}
        api_probe_line = ""
        if api_probe:
            api_probe_line = (
                f"<div>API: spot={api_probe.get('spot')} | futures={api_probe.get('usdt_futures')} "
                f"| papi={api_probe.get('papi')} | base={api_probe.get('recommended_base_url')}</div>"
            )

        html = f"""
<!doctype html>
<html lang="zh">
<head>
    <meta charset="utf-8" />
    <meta http-equiv="refresh" content="10" />
    <title>DCA 实盘看板</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #0f172a; color: #e2e8f0; }}
        .summary {{ display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }}
        .card {{ padding: 12px 16px; border: 1px solid #1e293b; border-radius: 10px; background: #111827; }}
        table {{ border-collapse: collapse; width: 100%; background: #0b1220; }}
        th, td {{ border: 1px solid #1e293b; padding: 8px; text-align: center; }}
        th {{ background: #111827; }}
        .pnl-pos {{ color: #22c55e; font-weight: 600; }}
        .pnl-neg {{ color: #ef4444; font-weight: 600; }}
    </style>
</head>
<body>
    <h2>DCA 实盘看板</h2>
    <div>更新时间: {payload.get("timestamp")}</div>
    {api_probe_line}
    <div class="summary">
        <div class="card">权益: {payload.get("equity")}</div>
        <div class="card">峰值权益: {payload.get("peak_equity")}</div>
        <div class="card">回撤(%): {payload.get("drawdown_pct")}</div>
        <div class="card">挂单数: {payload.get("open_orders")}</div>
    </div>
    <table>
        <thead>
            <tr>
                <th>交易对</th>
                <th>方向</th>
                <th>引擎</th>
                <th>入场价</th>
                <th>标记价</th>
                <th>盈亏%</th>
                <th>DCA次数</th>
                <th>最近加仓价</th>
                <th>入场时间</th>
            </tr>
        </thead>
        <tbody>
            {table_rows}
        </tbody>
    </table>
</body>
</html>
"""
        with open(self.dca_dashboard_html_path, "w", encoding="utf-8") as f:
            f.write(html)
        # 使用原子写入以避免并发/权限问题：先写入临时文件再替换
        try:
            dir_name = os.path.dirname(self.dca_dashboard_html_path) or "."
            fd, tmp_path = tempfile.mkstemp(prefix="dca_dashboard_", suffix=".html", dir=dir_name, text=True)
            os.close(fd)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(html)
            shutil.move(tmp_path, self.dca_dashboard_html_path)
        except Exception as e:
            print(f"⚠️ DCA HTML 写入失败，尝试直接写入: {e}")
            try:
                with open(self.dca_dashboard_html_path, "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception as e2:
                print(f"❌ 无法写入 DCA HTML: {e2}")

    @staticmethod
    def _fmt_dt(value: Any) -> Optional[str]:
        if isinstance(value, datetime):
            return value.isoformat()
        return None

    def _build_positions_snapshot(self, positions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        snapshot: Dict[str, Dict[str, Any]] = {}
        if not isinstance(positions, dict):
            return snapshot
        for symbol, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            amount = self._to_float(pos.get("amount", pos.get("positionAmt", 0)))
            if amount <= 0:
                continue
            side = self._normalize_position_side(pos.get("side"))
            if side is None:
                amt_signed = self._to_float(pos.get("positionAmt", 0))
                side = "LONG" if amt_signed > 0 else "SHORT" if amt_signed < 0 else "UNKNOWN"
            snapshot[str(symbol)] = {
                "side": side,
                "amount": amount,
                "entry_price": self._to_float(pos.get("entry_price", pos.get("entryPrice", 0))),
                "mark_price": self._to_float(pos.get("mark_price", pos.get("markPrice", 0))),
            }
        return snapshot

    def _refresh_last_positions_snapshot(self, positions: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        try:
            src = positions if isinstance(positions, dict) else (self.position_data.get_all_positions() or {})
            self._last_positions_for_reconcile = self._build_positions_snapshot(src)
        except Exception:
            self._last_positions_for_reconcile = {}

    def _detect_external_closes_and_cleanup(
        self,
        positions: Dict[str, Dict[str, Any]],
        params: Dict[str, Any],
    ) -> None:
        """
        检测"交易所侧触发平仓"（例如 TP/SL 条件单触发）：
        - 补打一条平仓日志/事件，避免看不到平仓信息
        - 立即清理同交易对残留未触发委托
        """
        prev = self._last_positions_for_reconcile or {}
        if not prev:
            self._refresh_last_positions_snapshot(positions)
            return

        current = self._build_positions_snapshot(positions)
        removed_symbols: List[str] = []
        state_changed = False

        for symbol, prev_pos in prev.items():
            prev_amt = self._to_float(prev_pos.get("amount", 0))
            if prev_amt <= 0:
                continue
            prev_side = self._normalize_position_side(prev_pos.get("side")) or "UNKNOWN"

            cur_pos = current.get(symbol)
            is_closed = False
            if not cur_pos:
                is_closed = True
            else:
                cur_amt = self._to_float(cur_pos.get("amount", 0))
                cur_side = self._normalize_position_side(cur_pos.get("side"))
                if cur_amt <= 0:
                    is_closed = True
                elif prev_side in ("LONG", "SHORT") and cur_side in ("LONG", "SHORT") and prev_side != cur_side:
                    is_closed = True

            if not is_closed:
                continue

            entry_price = self._to_float(prev_pos.get("entry_price", 0))
            close_price = self._to_float(prev_pos.get("mark_price", 0))
            pnl: Optional[float] = None
            pnl_percent: Optional[float] = None
            if entry_price > 0 and close_price > 0:
                if prev_side == "LONG":
                    pnl = (close_price - entry_price) * prev_amt
                    pnl_percent = ((close_price - entry_price) / entry_price) * 100.0
                elif prev_side == "SHORT":
                    pnl = (entry_price - close_price) * prev_amt
                    pnl_percent = ((entry_price - close_price) / entry_price) * 100.0

            pnl_text = "N/A"
            if pnl is not None and pnl_percent is not None:
                pnl_text = f"{pnl:+.4f} USDT ({pnl_percent:+.2f}%)"
            elif pnl is not None:
                pnl_text = f"{pnl:+.4f} USDT (N/A)"
            print(
                f"✅ 平仓(外部触发) | {symbol} | {prev_side} | 数量 {prev_amt:.6f} | "
                f"开仓价 {entry_price:.6f} | 平仓价 {close_price:.6f} | 已实现收益 {pnl_text}"
            )

            decision_stub: Dict[str, Any] = {"reason": "external_close_detected"}
            try:
                self._append_trade_log(
                    symbol=symbol,
                    action="CLOSE",
                    decision=decision_stub,
                    quantity=prev_amt,
                    entry_price=entry_price if entry_price > 0 else close_price,
                    result="external_close_detected",
                    pnl=pnl,
                    pnl_percent=pnl_percent,
                )
            except Exception:
                pass

            self._record_dca_trade_event(
                event_type="CLOSE_EXTERNAL",
                symbol=symbol,
                side=prev_side,
                status="external_close_detected",
                quantity=prev_amt,
                price=close_price if close_price > 0 else None,
                pnl=pnl,
                pnl_percent=pnl_percent,
                reason="external_close_detected",
            )

            if bool(params.get("order_reconcile_enabled", True)):
                self._cleanup_symbol_orders(symbol, reason="external_close_detected")
            removed_symbols.append(symbol)
            if symbol in self.dca_state:
                self.dca_state.pop(symbol, None)
                state_changed = True

        if state_changed:
            self._save_dca_state()

        for symbol in removed_symbols:
            current.pop(symbol, None)
        self._last_positions_for_reconcile = current

    def _reconcile_open_orders(
        self,
        positions: Dict[str, Dict[str, Any]],
        symbols_set: set,
        params: Dict[str, Any],
    ) -> None:
        if not params.get("order_reconcile_enabled", True):
            return
        try:
            orders = self.client.get_open_orders()
        except Exception as e:
            print(f"⚠️ 获取挂单失败: {e}")
            return

        if not isinstance(orders, list):
            return

        conditional_orders: List[Dict[str, Any]] = []
        get_open_conditional_orders = getattr(self.client, "get_open_conditional_orders", None)
        if callable(get_open_conditional_orders):
            try:
                raw_cond = get_open_conditional_orders()
                if isinstance(raw_cond, list):
                    conditional_orders = [o for o in raw_cond if isinstance(o, dict)]
            except Exception as e:
                print(f"⚠️ 获取条件挂单失败: {e}")
                conditional_orders = []

        cancel_orphan = bool(params.get("cancel_orphan_orders", True))
        cancel_side_mismatch = bool(params.get("cancel_side_mismatch_orders", True))
        cancel_unknown = bool(params.get("cancel_unknown_symbol_orders", True))
        cancel_untriggered_exit_orphans = bool(params.get("cancel_untriggered_exit_orphans", True))
        all_orders: List[Dict[str, Any]] = [o for o in orders if isinstance(o, dict)] + conditional_orders
        self._last_open_orders_count = len(all_orders)

        symbols_need_cleanup: set[str] = set()

        for order in all_orders:
            symbol = order.get("symbol")
            order_id = order.get("orderId")
            if not symbol:
                continue

            if symbol not in symbols_set and cancel_unknown:
                symbols_need_cleanup.add(str(symbol))
                continue

            pos = positions.get(symbol)
            # 仅清理未触发的止盈/止损单：当其交易对已无持仓时撤单
            if not pos and cancel_untriggered_exit_orphans and self._is_untriggered_exit_order(order):
                symbols_need_cleanup.add(str(symbol))
                continue
            if not pos and cancel_orphan:
                symbols_need_cleanup.add(str(symbol))
                continue

            if pos and cancel_side_mismatch:
                pos_side = pos.get("side")
                order_pos_side = order.get("positionSide")
                if not order_pos_side:
                    order_side = str(order.get("side", "")).upper()
                    order_pos_side = "LONG" if order_side == "BUY" else "SHORT" if order_side == "SELL" else None
                if order_pos_side and pos_side and order_pos_side != pos_side and order_id is not None:
                    self._safe_cancel_order(symbol, order_id, reason="side_mismatch")

        # 对需要清理的交易对执行"条件单 + 普通挂单"全量清理，避免单条撤单遗漏
        for sym in sorted(symbols_need_cleanup):
            self._cleanup_symbol_orders(sym, reason="reconcile_orphan")

    def _safe_cancel_order(self, symbol: str, order_id: Any, reason: str = "") -> bool:
        try:
            self.client.cancel_order(str(symbol), int(order_id))
            return True
        except Exception as e:
            if reason:
                print(f"⚠️ 撤单失败 {symbol}#{order_id} ({reason}): {e}")
            else:
                print(f"⚠️ 撤单失败 {symbol}#{order_id}: {e}")
            return False

    def _cleanup_symbol_orders(self, symbol: str, reason: str = "") -> None:
        """
        尽力清理某个交易对的未触发委托：
        1) 条件单（TP/SL）
        2) 普通挂单
        并做一次校验，若仍有残留则再重试一轮。
        """
        sym = str(symbol or "").upper()
        if not sym:
            return

        max_pass = 2
        for idx in range(max_pass):
            try:
                self.client.cancel_all_conditional_orders(sym)
            except Exception as e:
                print(f"⚠️ 撤销条件单失败 {sym} (pass={idx+1}/{max_pass}): {e}")
            try:
                self.client.cancel_all_open_orders(sym)
            except Exception as e:
                print(f"⚠️ 撤销普通挂单失败 {sym} (pass={idx+1}/{max_pass}): {e}")

            # 校验是否仍有残留挂单
            remaining_open: List[Any] = []
            remaining_cond: List[Any] = []
            try:
                raw_open = self.client.get_open_orders(sym)
                remaining_open = raw_open if isinstance(raw_open, list) else []
            except Exception:
                remaining_open = []
            get_open_conditional_orders = getattr(self.client, "get_open_conditional_orders", None)
            if callable(get_open_conditional_orders):
                try:
                    raw_cond = get_open_conditional_orders(sym)
                    remaining_cond = raw_cond if isinstance(raw_cond, list) else []
                except Exception:
                    remaining_cond = []

            remaining_cnt = len(remaining_open) + len(remaining_cond)
            if remaining_cnt <= 0:
                if reason:
                    print(f"🧹 已清理未触发委托: {sym} ({reason})")
                return
            time.sleep(0.2)

        # 两轮后仍有残留，给出明确日志
        ro: List[Any] = []
        try:
            raw_open = self.client.get_open_orders(sym)
            ro = raw_open if isinstance(raw_open, list) else []
        except Exception:
            ro = []
        rc: List[Any] = []
        get_open_conditional_orders = getattr(self.client, "get_open_conditional_orders", None)
        if callable(get_open_conditional_orders):
            try:
                raw_cond = get_open_conditional_orders(sym)
                rc = raw_cond if isinstance(raw_cond, list) else []
            except Exception:
                rc = []
        print(f"⚠️ 未触发委托仍有残留: {sym} open={len(ro)} conditional={len(rc)}")

    @staticmethod
    def _is_untriggered_exit_order(order: Dict[str, Any]) -> bool:
        """判断是否为未触发的平仓类条件单（TP/SL）。"""
        try:
            status = str(order.get("status", "")).upper()
            if status and status not in ("NEW", "PARTIALLY_FILLED"):
                return False
            order_type = str(order.get("type", order.get("origType", ""))).upper()
            exit_types = {
                "STOP",
                "STOP_MARKET",
                "STOP_LOSS",
                "STOP_LOSS_LIMIT",
                "TAKE_PROFIT",
                "TAKE_PROFIT_MARKET",
                "TRAILING_STOP_MARKET",
            }
            is_exit_type = order_type in exit_types
            reduce_only_raw = order.get("reduceOnly", False)
            close_position_raw = order.get("closePosition", False)
            reduce_only = reduce_only_raw if isinstance(reduce_only_raw, bool) else str(reduce_only_raw).lower() == "true"
            close_position = close_position_raw if isinstance(close_position_raw, bool) else str(close_position_raw).lower() == "true"
            return bool(is_exit_type or reduce_only or close_position)
        except Exception:
            return False

    def _reload_dca_config_if_changed(self) -> Dict[str, Any]:
        # Avoid calling _get_dca_symbols() here because it performs
        # market-data lookups and verbose logging. Instead compare the
        # raw configured symbol lists (fast, non-verbose) before/after
        # reloading the config.
        def _normalize_list(lst: List[str]) -> List[str]:
            out: List[str] = []
            for s in (lst or []):
                ss = str(s).upper()
                if not ss.endswith("USDT"):
                    ss = f"{ss}USDT"
                out.append(ss)
            return out

        prev_symbols = set(_normalize_list(self.dca_config.get("symbols", [])))
        prev_mtime = self.dca_config_mtime
        # reload config (this updates self.dca_config and self.dca_config_mtime)
        self._load_dca_rotation_config(initial=False)
        new_symbols = set(_normalize_list(self.dca_config.get("symbols", [])))

        updated = prev_mtime is None or self.dca_config_mtime != prev_mtime
        symbols_changed = prev_symbols != new_symbols
        return {
            "updated": updated,
            "symbols_changed": symbols_changed,
            "removed_symbols": list(prev_symbols - new_symbols),
            "added_symbols": list(new_symbols - prev_symbols),
        }

    def _preload_dca_symbols(self, symbols: List[str]) -> None:
        interval = self.dca_config.get("interval", "5m")
        print(f"📥 预读 {len(symbols)} 个币种的 {interval} K线(200根)...")
        for symbol in symbols:
            try:
                klines = self.client.get_klines(symbol, interval, limit=200)
                if klines:
                    print(f"   ✅ {symbol} - {len(klines)} 根")
                else:
                    print(f"   ⚠️ {symbol} - 获取失败")
            except Exception as e:
                print(f"   ❌ {symbol} - 预读失败: {e}")

    def _dca_get_klines_df(self, symbol: str, interval: str, limit: int = 200) -> Optional[pd.DataFrame]:
        try:
            klines = self.client.get_klines(symbol, interval, limit=limit)
        except Exception as e:
            print(f"⚠️ 获取K线失败: {symbol} {interval} limit={limit} err={e}")
            return None
        if not klines:
            return None
        df = pd.DataFrame(
            klines,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trades",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df

    def _dca_fetch_multi_timeframes(self, symbol: str) -> Dict[str, pd.DataFrame]:
        cache: Dict[str, pd.DataFrame] = {}
        for tf, limit in self.MULTI_TIMEFRAME_LIMITS.items():
            df = self._dca_get_klines_df(symbol, tf, limit=limit)
            if df is not None:
                cache[tf] = df
        self._multi_tf_cache[symbol] = cache
        return cache

    def _dca_trend_strength(self, symbol: str) -> float:
        cache = self._multi_tf_cache.get(symbol)
        if not cache:
            return 0.0
        score = 0.0
        count = 0
        for df in cache.values():
            if len(df) < 10:
                continue
            ema = df["close"].ewm(span=20, adjust=False).mean().iloc[-1]
            price = df["close"].iloc[-1]
            if pd.isna(ema) or pd.isna(price):
                continue
            count += 1
            if price > ema:
                score += 1.0
            elif price < ema:
                score -= 1.0
        if count == 0:
            return 0.0
        return score / count

    def _dca_calc_indicators(self, df: pd.DataFrame, bar_minutes: int) -> pd.DataFrame:
        df = df.copy()
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        # use float literals to avoid static type issues with Series operations
        df["rsi"] = 100.0 - (100.0 / (1.0 + rs))

        df["bb_middle"] = df["close"].rolling(window=20).mean()
        bb_std = df["close"].rolling(window=20).std()
        df["bb_upper"] = df["bb_middle"] + (bb_std * 2)
        df["bb_lower"] = df["bb_middle"] - (bb_std * 2)

        df["volume_quantile"] = (
            df["volume"].rolling(window=60).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)
        )

        df["quote_volume"] = df["volume"] * df["close"]
        bars_24h = int(24 * 60 / bar_minutes)
        df["quote_volume_24h"] = df["quote_volume"].rolling(window=bars_24h).sum()

        close = df["close"]
        cond_up = close > close.shift(4)
        cond_down = close < close.shift(4)
        td_up = []
        td_down = []
        count = 0
        count_down = 0
        for val in cond_up.fillna(False):
            if val:
                count += 1
            else:
                count = 0
            td_up.append(count)
        for val in cond_down.fillna(False):
            if val:
                count_down += 1
            else:
                count_down = 0
            td_down.append(count_down)
        df["td_up"] = td_up
        df["td_down"] = td_down

        df["momentum_5"] = close.pct_change(5)
        # 24h实现波动率（按当前 bar 周期折算）
        ret_1 = close.pct_change()
        df["volatility_24h"] = ret_1.rolling(window=max(20, bars_24h)).std() * (bars_24h ** 0.5)
        # 趋势强度（用于动态阈值）
        df["ema_fast_20"] = close.ewm(span=20, adjust=False).mean()
        df["ema_slow_50"] = close.ewm(span=50, adjust=False).mean()
        return df

    @staticmethod
    def _tf_to_bar_minutes(timeframe: str) -> int:
        """将 K 线周期字符串转换为分钟数。"""
        s = str(timeframe or "").strip().lower()
        if not s:
            return 1
        try:
            if s.endswith("m") and s[:-1].isdigit():
                return max(1, int(s[:-1]))
            if s.endswith("h") and s[:-1].isdigit():
                return max(1, int(s[:-1]) * 60)
            if s.endswith("d") and s[:-1].isdigit():
                return max(1, int(s[:-1]) * 24 * 60)
            if s.isdigit():
                return max(1, int(s))
        except Exception:
            pass
        return 1

    def _get_execution_reject_cooldown_seconds(self, params: Dict[str, Any]) -> int:
        """执行层拒绝后的symbol级冷却秒数（默认120秒）。"""
        exec_cfg = params.get("execution_layer", {}) if isinstance(params.get("execution_layer", {}), dict) else {}
        try:
            cooldown_seconds = int(exec_cfg.get("reject_cooldown_seconds", 120) or 120)
        except Exception:
            cooldown_seconds = 120
        return max(0, cooldown_seconds)

    def _get_execution_reject_cooldown_remaining(self, symbol: str) -> int:
        """返回symbol执行层拒绝冷却剩余秒数；无冷却返回0。"""
        sym = str(symbol or "").upper()
        if not sym:
            return 0
        expires = self._execution_reject_cooldown_until.get(sym)
        if not isinstance(expires, datetime):
            return 0
        now_ts = datetime.now()
        if now_ts >= expires:
            self._execution_reject_cooldown_until.pop(sym, None)
            return 0
        return max(1, int((expires - now_ts).total_seconds()))

    def _mark_execution_reject_cooldown(self, symbol: str, params: Dict[str, Any], reason: str) -> int:
        """标记执行层拒绝冷却。返回生效秒数。"""
        sym = str(symbol or "").upper()
        if not sym:
            return 0
        cooldown_seconds = self._get_execution_reject_cooldown_seconds(params)
        if cooldown_seconds <= 0:
            self._execution_reject_cooldown_until.pop(sym, None)
            return 0
        expires = datetime.now() + timedelta(seconds=cooldown_seconds)
        self._execution_reject_cooldown_until[sym] = expires
        print(
            f"⏳ {sym} 执行层拒绝后冷却 {cooldown_seconds}s "
            f"(reason={reason}, expires={expires.isoformat()})"
        )
        return cooldown_seconds

    def _dca_execution_layer_confirm(
        self,
        symbol: str,
        action: str,
        params: Dict[str, Any],
        engine: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        执行层微观确认（默认 1m）：
        - 决策仍来自上层周期（如 5m）
        - 仅在下单前过滤明显逆向的 1m 结构
        """
        if str(action or "").upper() not in ("BUY_OPEN", "SELL_OPEN"):
            return True, "non_open_action", {}

        exec_cfg = params.get("execution_layer", {}) if isinstance(params.get("execution_layer", {}), dict) else {}
        enabled = self._coerce_bool(exec_cfg.get("enabled", True), True)
        if not enabled:
            return True, "execution_layer_disabled", {}

        tf = str(exec_cfg.get("timeframe", "1m") or "1m").strip().lower()
        bar_minutes = self._tf_to_bar_minutes(tf)
        allow_no_data_pass = self._coerce_bool(exec_cfg.get("allow_no_data_pass", True), True)
        min_bars = max(60, int(exec_cfg.get("min_bars", 80) or 80))
        block_score = max(1, int(exec_cfg.get("opposite_block_score", 2) or 2))
        pullback_eps = max(0.0, float(exec_cfg.get("pullback_eps", 0.0015) or 0.0015))
        long_rsi_overbought = float(exec_cfg.get("long_rsi_overbought", 65) or 65)
        short_rsi_oversold = float(exec_cfg.get("short_rsi_oversold", 35) or 35)
        resolved_regime = str(regime or "").upper()
        resolved_engine = self._resolve_dual_engine(
            engine if engine is not None else self._map_regime_to_engine(resolved_regime or "RANGE")
        )
        if resolved_engine == "UNKNOWN":
            resolved_engine = self._map_regime_to_engine(resolved_regime or "RANGE")
        if resolved_engine == "UNKNOWN":
            resolved_engine = "RANGE"
        try:
            range_price_ema_flag_weight = float(exec_cfg.get("range_price_ema_flag_weight", 0.5) or 0.5)
        except Exception:
            range_price_ema_flag_weight = 0.5
        range_price_ema_flag_weight = self._clamp_value(range_price_ema_flag_weight, 0.0, 1.0)
        price_ema_flag_weight = range_price_ema_flag_weight if resolved_engine == "RANGE" else 1.0

        df = self._dca_get_klines_df(symbol, tf, limit=min_bars)
        if df is None or len(df) < min_bars:
            reason = f"execution_{tf}_data_insufficient"
            return (allow_no_data_pass, reason, {"timeframe": tf, "bars": 0 if df is None else len(df)})

        ind = self._dca_calc_indicators(df, bar_minutes)
        if ind is None or len(ind) < 50:
            reason = f"execution_{tf}_indicators_insufficient"
            return (allow_no_data_pass, reason, {"timeframe": tf, "bars": 0 if ind is None else len(ind)})

        row = ind.iloc[-1]
        price = self._to_float(row.get("close"), 0.0)
        ema_fast = self._to_float(row.get("ema_fast_20"), 0.0)
        ema_slow = self._to_float(row.get("ema_slow_50"), 0.0)
        rsi = self._to_float(row.get("rsi"), 50.0)

        opposite_flags: List[str] = []
        opposite_score = 0.0
        act = str(action).upper()

        if act == "BUY_OPEN":
            if ema_fast > 0 and ema_slow > 0 and ema_fast < ema_slow:
                opposite_flags.append("ema_down")
                opposite_score += 1.0
            if price > 0 and ema_fast > 0 and price < ema_fast * (1.0 - pullback_eps):
                opposite_flags.append("below_ema_fast")
                opposite_score += price_ema_flag_weight
            if rsi >= long_rsi_overbought:
                opposite_flags.append("rsi_hot")
                opposite_score += 1.0
        else:  # SELL_OPEN
            if ema_fast > 0 and ema_slow > 0 and ema_fast > ema_slow:
                opposite_flags.append("ema_up")
                opposite_score += 1.0
            if price > 0 and ema_fast > 0 and price > ema_fast * (1.0 + pullback_eps):
                opposite_flags.append("above_ema_fast")
                opposite_score += price_ema_flag_weight
            if rsi <= short_rsi_oversold:
                opposite_flags.append("rsi_cold")
                opposite_score += 1.0

        details = {
            "timeframe": tf,
            "engine": resolved_engine,
            "regime": resolved_regime,
            "price": round(price, 8),
            "ema_fast": round(ema_fast, 8),
            "ema_slow": round(ema_slow, 8),
            "rsi": round(rsi, 4),
            "opposite_flags": opposite_flags,
            "block_score": block_score,
            "flag_score": round(opposite_score, 3),
            "price_ema_flag_weight": round(price_ema_flag_weight, 3),
        }
        if opposite_score >= float(block_score):
            return False, f"execution_{tf}_opposite({','.join(opposite_flags)})", details
        return True, f"execution_{tf}_ok", details

    def _oscillation_entry_signal(self, symbol: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        RANGE / RANGE_LOCK 下的均值回归入场信号。
        信号规则：BB下轨+RSI低做多，BB上轨+RSI高做空；中轨附近不做；高量能疑似突破不做。
        """
        osc_cfg = params.get("oscillation_mode", {}) or {}
        entry_cfg = osc_cfg.get("entry", {}) or {}

        tf = str(entry_cfg.get("timeframe", "15m"))
        rsi_low = float(entry_cfg.get("rsi_low", 30))
        rsi_high = float(entry_cfg.get("rsi_high", 70))
        bb_touch = float(entry_cfg.get("bb_touch", 1.0))
        vol_q_max = float(entry_cfg.get("vol_q_max", 0.65))
        min_conf = float(entry_cfg.get("min_conf", 0.60))
        mid_band = float(entry_cfg.get("mid_band", 0.002))

        bar_minutes = 15
        if tf.endswith("m") and tf[:-1].isdigit():
            bar_minutes = int(tf[:-1])
        elif tf.endswith("h") and tf[:-1].isdigit():
            bar_minutes = int(tf[:-1]) * 60

        df = self._dca_get_klines_df(symbol, tf, limit=120)
        if df is None or len(df) < 80:
            return {"action": "HOLD", "confidence": 0.0, "reason": "osc_no_data"}

        ind = self._dca_calc_indicators(df, bar_minutes)
        if ind is None or len(ind) < 2:
            return {"action": "HOLD", "confidence": 0.0, "reason": "osc_no_indicators"}

        last = ind.iloc[-1]
        try:
            price = float(last.get("close", 0))
            rsi = float(last.get("rsi", 50))
            bb_upper = float(last.get("bb_upper", price))
            bb_lower = float(last.get("bb_lower", price))
            bb_middle = float(last.get("bb_middle", price))
            vol_q = float(last.get("volume_quantile", 0.5))
        except Exception:
            return {"action": "HOLD", "confidence": 0.0, "reason": "osc_bad_value"}

        if (
            price <= 0
            or pd.isna(price)
            or pd.isna(rsi)
            or pd.isna(bb_upper)
            or pd.isna(bb_lower)
            or pd.isna(bb_middle)
            or pd.isna(vol_q)
        ):
            return {"action": "HOLD", "confidence": 0.0, "reason": "osc_nan_value"}

        # 量能过强时，优先视作突破阶段，避免做均值回归逆势单
        if vol_q > vol_q_max:
            return {
                "action": "HOLD",
                "confidence": 0.0,
                "reason": f"osc_skip_breakout(vq={vol_q:.2f})",
            }

        bb_touch = max(0.95, min(1.10, bb_touch))
        touch_lower = price <= bb_lower * bb_touch
        touch_upper = price >= bb_upper * (2.0 - bb_touch)

        if touch_lower and rsi <= rsi_low:
            conf = max(min_conf, min(0.95, min_conf + (rsi_low - rsi) / 100.0))
            return {
                "action": "BUY_OPEN",
                "confidence": conf,
                "reason": f"osc_long(bb_low+rsi={rsi:.1f},vq={vol_q:.2f})",
            }

        if touch_upper and rsi >= rsi_high:
            conf = max(min_conf, min(0.95, min_conf + (rsi - rsi_high) / 100.0))
            return {
                "action": "SELL_OPEN",
                "confidence": conf,
                "reason": f"osc_short(bb_up+rsi={rsi:.1f},vq={vol_q:.2f})",
            }

        if bb_middle > 0 and abs(price - bb_middle) / bb_middle < mid_band:
            return {"action": "HOLD", "confidence": 0.0, "reason": "osc_mid_no_trade"}

        return {"action": "HOLD", "confidence": 0.0, "reason": "osc_no_trigger"}

    def _dca_score_pair(self, row: pd.Series, rsi_entry_short: float, rsi_entry_long: float) -> Tuple[float, float]:
        if pd.isna(row.get("rsi")) or pd.isna(row.get("bb_upper")):
            return 0.0, 0.0
        rsi = row["rsi"]
        close = row["close"]
        bb_upper = row["bb_upper"]
        bb_lower = row["bb_lower"]
        vq = row.get("volume_quantile", 0)
        momentum = row.get("momentum_5", 0)

        # short scores
        rsi_score_s = max(0.0, min(1.0, (rsi - rsi_entry_short) / (100 - rsi_entry_short)))
        bb_score_s = max(0.0, min(1.0, (close - bb_upper) / (bb_upper * 0.02)))
        momentum_score_s = max(0.0, min(1.0, momentum / 0.01))

        # long scores
        rsi_score_l = max(0.0, min(1.0, (rsi_entry_long - rsi) / max(1.0, rsi_entry_long)))
        bb_score_l = max(0.0, min(1.0, (bb_lower - close) / (bb_lower * 0.02)))
        momentum_score_l = max(0.0, min(1.0, (-momentum) / 0.01))

        volume_score = max(0.0, min(1.0, vq if pd.notna(vq) else 0.0))

        short_score = 0.4 * rsi_score_s + 0.2 * bb_score_s + 0.2 * momentum_score_s + 0.2 * volume_score
        long_score = 0.4 * rsi_score_l + 0.2 * bb_score_l + 0.2 * momentum_score_l + 0.2 * volume_score
        return short_score, long_score

    def _dca_equity_scale(self, equity: float, params: Dict[str, Any]) -> float:
        if self.dca_initial_equity is None or self.dca_initial_equity <= 0:
            return 1.0
        reinvest_pct = float(params.get("profit_reinvest_pct", 100)) / 100.0
        growth = (equity - self.dca_initial_equity) / self.dca_initial_equity
        scale = 1.0 + growth * reinvest_pct
        return max(0.5, scale)

    def _dca_position_value(self, pos: Dict[str, Any], price: float) -> float:
        try:
            amt = abs(float(pos.get("amount", pos.get("positionAmt", 0))))
        except Exception:
            amt = 0.0
        return amt * price

    @staticmethod
    def _dca_signal_grade_from_hits(hit_count: int) -> str:
        if hit_count >= 4:
            return "SUPER"
        if hit_count >= 3:
            return "BEST"
        if hit_count >= 2:
            return "WARNING"
        return "NONE"

    @staticmethod
    def _dca_signal_grade_label(grade: str) -> str:
        g = str(grade or "").upper()
        if g == "SUPER":
            return "🚀 超级信号"
        if g == "BEST":
            return "🔥 最佳信号"
        if g == "WARNING":
            return "★ 预警信号"
        return "无有效共振"

    def _dca_normalize_regime_factor_weights(self, raw_cfg: Any) -> Dict[str, float]:
        default_weights = {"ema": 0.35, "macd": 0.25, "dmi": 0.20, "vol": 0.20}
        cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
        keys = ("ema", "macd", "dmi", "vol")
        weights: Dict[str, float] = {}
        total = 0.0
        for key in keys:
            try:
                val = float(cfg.get(key, default_weights[key]))
            except Exception:
                val = float(default_weights[key])
            val = max(0.0, val)
            weights[key] = val
            total += val
        if total <= 1e-9:
            return default_weights
        return {k: (weights[k] / total) for k in keys}

    def _dca_calc_regime_factor_scores(
        self,
        df: pd.DataFrame,
        factor_weights: Dict[str, float],
        hit_threshold: float,
    ) -> Optional[Dict[str, Any]]:
        if df is None or len(df) < 55:
            return None
        try:
            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]

            ema_fast = close.ewm(span=20, adjust=False).mean()
            ema_slow = close.ewm(span=50, adjust=False).mean()
            last_close = float(close.iloc[-1])
            last_fast = float(ema_fast.iloc[-1])
            last_slow = float(ema_slow.iloc[-1])
            if pd.isna(last_close) or pd.isna(last_fast) or pd.isna(last_slow):
                return None

            # 因子1: EMA 结构 + 斜率
            if last_close > last_fast > last_slow:
                ema_score = 1.0
            elif last_close < last_fast < last_slow:
                ema_score = -1.0
            elif last_close > last_slow:
                ema_score = 0.35
            elif last_close < last_slow:
                ema_score = -0.35
            else:
                ema_score = 0.0
            if len(ema_fast) >= 5 and len(ema_slow) >= 5:
                fast_prev = float(ema_fast.iloc[-5])
                slow_prev = float(ema_slow.iloc[-5])
                fast_slope = (last_fast - fast_prev) / fast_prev if fast_prev != 0 else 0.0
                slow_slope = (last_slow - slow_prev) / slow_prev if slow_prev != 0 else 0.0
                ema_score += fast_slope * 5.0 + slow_slope * 2.0
            ema_score = self._clamp_value(ema_score, -1.0, 1.0)

            # 因子2: MACD 动量
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            hist = macd_line - signal_line
            last_macd = float(macd_line.iloc[-1])
            last_signal = float(signal_line.iloc[-1])
            last_hist = float(hist.iloc[-1])
            hist_prev = float(hist.iloc[-3]) if len(hist) >= 3 else last_hist
            hist_scale = float(hist.abs().rolling(window=30, min_periods=5).mean().iloc[-1])
            if pd.isna(hist_scale) or hist_scale <= 1e-12:
                hist_scale = max(1e-9, abs(last_hist))
            hist_mom = self._clamp_value((last_hist - hist_prev) / (hist_scale * 3.0), -0.35, 0.35)
            if last_macd > last_signal:
                macd_score = 0.7 if last_hist >= 0 else 0.3
            elif last_macd < last_signal:
                macd_score = -0.7 if last_hist <= 0 else -0.3
            else:
                macd_score = 0.0
            macd_score = self._clamp_value(macd_score + hist_mom, -1.0, 1.0)

            # 因子3: DMI 趋势方向 + ADX 强度
            plus_dm = high.diff()
            minus_dm = -low.diff()
            plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
            minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
            tr1 = high - low
            tr2 = (high - close.shift(1)).abs()
            tr3 = (low - close.shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(window=14).mean()
            plus_di = 100.0 * (plus_dm.rolling(window=14).mean() / (atr + 1e-10))
            minus_di = 100.0 * (minus_dm.rolling(window=14).mean() / (atr + 1e-10))
            dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
            adx = dx.rolling(window=14).mean()
            last_plus_di = float(plus_di.iloc[-1]) if not pd.isna(plus_di.iloc[-1]) else 0.0
            last_minus_di = float(minus_di.iloc[-1]) if not pd.isna(minus_di.iloc[-1]) else 0.0
            last_adx = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 0.0
            dmi_raw = self._clamp_value((last_plus_di - last_minus_di) / 25.0, -1.0, 1.0)
            adx_mult = self._clamp_value(last_adx / 25.0, 0.0, 1.0)
            dmi_score = self._clamp_value(dmi_raw * (0.5 + 0.5 * adx_mult), -1.0, 1.0)

            # 因子4: VOL 量价共振
            vol_ma20 = volume.rolling(window=20).mean()
            last_vol = float(volume.iloc[-1])
            last_vol_ma = float(vol_ma20.iloc[-1]) if not pd.isna(vol_ma20.iloc[-1]) else 0.0
            vol_ratio = last_vol / last_vol_ma if last_vol_ma > 0 else 1.0
            prev_close = float(close.iloc[-2]) if len(close) >= 2 else last_close
            prev_close_4 = float(close.iloc[-4]) if len(close) >= 4 else prev_close
            ret_1 = (last_close - prev_close) / prev_close if prev_close > 0 else 0.0
            ret_4 = (last_close - prev_close_4) / prev_close_4 if prev_close_4 > 0 else ret_1
            price_impulse = 0.7 * ret_1 + 0.3 * ret_4
            if abs(price_impulse) < 1e-12:
                vol_score = 0.0
            else:
                direction = 1.0 if price_impulse > 0 else -1.0
                expansion = self._clamp_value((vol_ratio - 1.0) / 1.2, 0.0, 1.0)
                strength = 0.25 + 0.75 * expansion
                if vol_ratio < 0.85:
                    strength *= 0.6
                vol_score = direction * self._clamp_value(strength, 0.05, 1.0)
            vol_score = self._clamp_value(vol_score, -1.0, 1.0)

            factor_scores = {
                "ema": float(ema_score),
                "macd": float(macd_score),
                "dmi": float(dmi_score),
                "vol": float(vol_score),
            }
            tf_score = 0.0
            for key, score in factor_scores.items():
                tf_score += score * float(factor_weights.get(key, 0.0))
            tf_score = self._clamp_value(tf_score, -1.5, 1.5)

            bull_hits = sum(1 for v in factor_scores.values() if v >= hit_threshold)
            bear_hits = sum(1 for v in factor_scores.values() if v <= -hit_threshold)
            hit_count = max(bull_hits, bear_hits)
            signal_grade = self._dca_signal_grade_from_hits(hit_count)

            return {
                "tf_score": float(tf_score),
                "factor_scores": factor_scores,
                "bull_hits": int(bull_hits),
                "bear_hits": int(bear_hits),
                "signal_grade": signal_grade,
                "signal_label": self._dca_signal_grade_label(signal_grade),
                "close": float(last_close),
                "ema_fast": float(last_fast),
                "ema_slow": float(last_slow),
                "macd_line": float(last_macd),
                "macd_signal": float(last_signal),
                "dmi_plus": float(last_plus_di),
                "dmi_minus": float(last_minus_di),
                "adx": float(last_adx),
                "vol_ratio": float(vol_ratio),
            }
        except Exception:
            return None

    def _dca_detect_btc_regime(self, params: Dict[str, Any]) -> Tuple[str, float, Dict[str, Any]]:
        """
        基于 BTC 多时间周期 K 线判断市场牛熊状态（多因子：EMA+MACD+DMI+VOL）。
        """
        if not bool(params.get("btc_regime_enabled", True)):
            return "NEUTRAL", 0.0, {}

        cache_ttl = max(30, int(params.get("btc_regime_update_seconds", 60) or 60))
        now_ts = time.time()
        if (
            self._btc_regime_cache.get("ts", 0) > 0
            and (now_ts - self._btc_regime_cache.get("ts", 0)) < cache_ttl
        ):
            return (
                self._btc_regime_cache.get("regime", "NEUTRAL"),
                self._btc_regime_cache.get("score", 0.0),
                self._btc_regime_cache.get("details", {}),
            )

        timeframes = params.get("btc_regime_timeframes", ["1m", "3m", "5m", "15m", "1h", "4h"])
        if isinstance(timeframes, str):
            timeframes = [tf.strip() for tf in timeframes.split(",")]

        tf_weights = {
            "1m": 0.5,
            "3m": 0.8,
            "5m": 1.0,
            "15m": 1.5,
            "1h": 2.0,
            "4h": 3.0,
        }
        factor_weights = self._dca_normalize_regime_factor_weights(
            params.get("btc_regime_factor_weights", params.get("regime_factor_weights", {}))
        )
        hit_threshold = self._clamp_value(
            float(params.get("combined_signal_factor_hit_threshold", 0.20) or 0.20),
            0.05,
            0.9,
        )

        details: Dict[str, Any] = {}
        total_score = 0.0
        valid_weight = 0.0
        factor_weighted_sum = {k: 0.0 for k in ("ema", "macd", "dmi", "vol")}

        for tf in timeframes:
            weight = float(tf_weights.get(tf, 1.0))
            try:
                df = self._dca_get_klines_df("BTCUSDT", tf, limit=120)
                if df is None:
                    continue
                calc = self._dca_calc_regime_factor_scores(df, factor_weights, hit_threshold)
                if not calc:
                    continue
                tf_score = float(calc["tf_score"])
                total_score += tf_score * weight
                valid_weight += weight
                for key, val in (calc.get("factor_scores", {}) or {}).items():
                    if key in factor_weighted_sum:
                        factor_weighted_sum[key] += float(val) * weight
                details[tf] = {
                    "score": round(tf_score, 3),
                    "close": round(float(calc.get("close", 0.0)), 2),
                    "ema_fast": round(float(calc.get("ema_fast", 0.0)), 2),
                    "ema_slow": round(float(calc.get("ema_slow", 0.0)), 2),
                    "factor_scores": {k: round(float(v), 3) for k, v in (calc.get("factor_scores", {}) or {}).items()},
                    "signal_grade": str(calc.get("signal_grade", "NONE")),
                    "bull_hits": int(calc.get("bull_hits", 0) or 0),
                    "bear_hits": int(calc.get("bear_hits", 0) or 0),
                    "vol_ratio": round(float(calc.get("vol_ratio", 1.0) or 1.0), 2),
                    "adx": round(float(calc.get("adx", 0.0) or 0.0), 2),
                }
            except Exception as e:
                details[tf] = {"error": str(e)}

        avg_score = (total_score / valid_weight) if valid_weight > 0 else 0.0
        if avg_score >= 0.35:
            regime = "BULL"
        elif avg_score <= -0.35:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

        factor_scores_avg = {
            k: (factor_weighted_sum[k] / valid_weight if valid_weight > 0 else 0.0)
            for k in factor_weighted_sum
        }
        bull_hits = sum(1 for v in factor_scores_avg.values() if v >= hit_threshold)
        bear_hits = sum(1 for v in factor_scores_avg.values() if v <= -hit_threshold)
        dominant_hits = max(bull_hits, bear_hits)
        signal_grade = self._dca_signal_grade_from_hits(dominant_hits)

        self._btc_regime_cache = {
            "regime": regime,
            "score": round(avg_score, 3),
            "ts": now_ts,
            "details": details,
            "factor_scores": {k: round(v, 3) for k, v in factor_scores_avg.items()},
            "bull_hits": int(bull_hits),
            "bear_hits": int(bear_hits),
            "signal_grade": signal_grade,
            "signal_label": self._dca_signal_grade_label(signal_grade),
            "hit_threshold": round(hit_threshold, 3),
        }
        return regime, round(avg_score, 3), details

    def _dca_detect_btc_major_regime(self, params: Dict[str, Any]) -> Tuple[str, str]:
        """
        检测 BTC 大趋势（仅基于4H周期）。
        用于决定方向性交易，防止短期噪音导致频繁转换。
        
        返回: (major_regime, action)
        - major_regime: 当前确认的大趋势
        - action: "CONFIRMED"(已确认), "PENDING"(待确认), "BLOCKED"(被阻止)
        """
        # 获取4H周期的趋势
        try:
            df = self._dca_get_klines_df("BTCUSDT", "4h", limit=100)
            if df is None or len(df) < 55:
                return self._major_regime, "NO_DATA"
            
            close = df["close"]
            ema_fast = close.ewm(span=20, adjust=False).mean()
            ema_slow = close.ewm(span=50, adjust=False).mean()
            
            last_close = float(close.iloc[-1])
            last_fast = float(ema_fast.iloc[-1])
            last_slow = float(ema_slow.iloc[-1])
            
            if last_close > last_fast > last_slow:
                detected_regime = "BULL"
            elif last_close < last_fast < last_slow:
                detected_regime = "BEAR"
            else:
                detected_regime = "NEUTRAL"
        except Exception:
            return self._major_regime, "ERROR"
        
        now_ts = time.time()
        
        # 检查是否与当前大趋势相同
        if detected_regime == self._major_regime:
            # 趋势一致，重置确认计数
            self._major_regime_confirm_count = 0
            self._pending_major_regime = None
            return self._major_regime, "CONFIRMED"
        
        # 趋势发生变化，检查是否需要阻止
        min_transition_interval = int(params.get("major_regime_min_interval_seconds", 3600))  # 默认1小时
        if (self._last_major_transition_time > 0 and 
            (now_ts - self._last_major_transition_time) < min_transition_interval):
            # 距离上次转换时间太短，阻止转换
            return self._major_regime, f"BLOCKED({int(min_transition_interval - (now_ts - self._last_major_transition_time))}s剩余)"
        
        # 累积确认计数
        if detected_regime != self._pending_major_regime:
            # 新的待确认趋势，重置计数
            self._pending_major_regime = detected_regime
            self._major_regime_confirm_count = 1
            return self._major_regime, f"PENDING(1/{params.get('major_regime_confirm_count', 2)})"
        else:
            self._major_regime_confirm_count += 1
            required_count = int(params.get("major_regime_confirm_count", 2))
            if self._major_regime_confirm_count >= required_count:
                # 达到确认次数，执行转换
                old_regime = self._major_regime
                self._major_regime = detected_regime
                self._last_major_transition_time = now_ts
                self._major_regime_confirm_count = 0
                self._pending_major_regime = None
                return self._major_regime, f"TRANSITIONED({old_regime}->{detected_regime})"
            else:
                return self._major_regime, f"PENDING({self._major_regime_confirm_count}/{required_count})"

    # =====================================================================
    # 机构级多周期趋势评分系统（Trend Scoring System）
    # =====================================================================

    def _calc_adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """计算 ADX（平均趋向指数）"""
        try:
            high = df["high"]
            low = df["low"]
            close = df["close"]

            # +DM 和 -DM
            plus_dm = high.diff()
            minus_dm = -low.diff()

            plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
            minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

            # TR
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            # Smoothed
            atr = tr.rolling(window=period).mean()
            plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
            minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

            # DX
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
            adx = dx.rolling(window=period).mean()

            return float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 0.0
        except Exception:
            return 0.0

    def _calc_btc_4h_trend_score(self, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算 BTC 4H 趋势因子评分
        因子A: 均线斜率 (权重 0.4)
        因子B: 布林轨道位置 (权重 0.3)
        因子C: ADX 趋势强度过滤 (权重 0.3)
        """
        details: Dict[str, Any] = {"ma_slope": 0.0, "bb_pos": 0.0, "adx": 0.0}
        try:
            df = self._dca_get_klines_df("BTCUSDT", "4h", limit=100)
            if df is None or len(df) < 60:
                return 0.0, details

            close = df["close"]
            ema50 = close.ewm(span=50, adjust=False).mean()

            # 因子A: 均线斜率趋势
            ema50_t = float(ema50.iloc[-1])
            ema50_t10 = float(ema50.iloc[-10])
            ma_slope = (ema50_t - ema50_t10) / ema50_t10 if ema50_t10 != 0 else 0
            if ma_slope > 0.005:
                score_a = 1.0
            elif ma_slope > 0:
                score_a = 0.5
            elif ma_slope < -0.005:
                score_a = -1.0
            elif ma_slope < 0:
                score_a = -0.5
            else:
                score_a = 0.0
            details["ma_slope"] = round(ma_slope, 4)

            # 因子B: 布林轨道位置
            sma20 = close.rolling(window=20).mean()
            std20 = close.rolling(window=20).std()
            upper = sma20 + 2 * std20
            lower = sma20 - 2 * std20
            last_close = float(close.iloc[-1])
            last_upper = float(upper.iloc[-1])
            last_lower = float(lower.iloc[-1])
            last_middle = float(sma20.iloc[-1])
            bb_width = last_upper - last_lower
            bb_pos = (last_close - last_middle) / bb_width if bb_width != 0 else 0
            if bb_pos > 0.5:
                score_b = 1.0
            elif bb_pos > 0:
                score_b = 0.5
            elif bb_pos > -0.5:
                score_b = -0.5
            else:
                score_b = -1.0
            details["bb_pos"] = round(bb_pos, 3)

            # 因子C: ADX 趋势强度
            adx = self._calc_adx(df, period=14)
            details["adx"] = round(adx, 2)
            adx_mult = min(1.0, adx / 25.0) if adx >= 20 else adx / 25.0

            # 获取权重
            factors = params.get("btc_4h_factors", {})
            w_a = float(factors.get("ma_slope", 0.4))
            w_b = float(factors.get("bb_position", 0.3))
            w_c = float(factors.get("adx_filter", 0.3))

            total_score = (w_a * score_a + w_b * score_b + w_c * score_b * adx_mult)
            return round(total_score, 3), details
        except Exception as e:
            details["error"] = str(e)
            return 0.0, details

    def _calc_btc_1h_trend_score(self, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算 BTC 1H 趋势因子评分（用于捕捉牛熊切换）
        因子D: 结构破坏 (权重 0.6)
        因子E: 成交量确认 (权重 0.4)
        """
        details: Dict[str, Any] = {"structure_break": 0.0, "volume_ratio": 0.0}
        try:
            df = self._dca_get_klines_df("BTCUSDT", "1h", limit=50)
            if df is None or len(df) < 30:
                return 0.0, details

            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]

            # 因子D: 结构破坏检测
            # 寻找最近的高低点
            recent_high = float(high.iloc[-20:].max())
            recent_low = float(low.iloc[-20:].min())
            last_close = float(close.iloc[-1])
            prev_close = float(close.iloc[-2])

            score_d = 0.0
            # 上升趋势跌破前低
            if last_close < recent_low and prev_close >= recent_low:
                score_d = -1.0
                details["structure_break"] = "bearish_break"
            # 下降趋势突破前高
            elif last_close > recent_high and prev_close <= recent_high:
                score_d = 1.0
                details["structure_break"] = "bullish_break"

            # 因子E: 成交量确认
            vol_ma20 = float(volume.iloc[-20:].mean())
            last_vol = float(volume.iloc[-1])
            vol_ratio = last_vol / vol_ma20 if vol_ma20 > 0 else 0
            details["volume_ratio"] = round(vol_ratio, 2)

            if vol_ratio > 1.5:
                score_e = 1.0 if score_d != 0 else 0.5
            elif vol_ratio > 1.0:
                score_e = 0.5 if score_d != 0 else 0.3
            else:
                score_e = 0.0

            # 获取权重
            factors = params.get("btc_1h_factors", {})
            w_d = float(factors.get("structure_break", 0.6))
            w_e = float(factors.get("volume_confirm", 0.4))

            total_score = w_d * score_d + w_e * score_e * (1 if score_d != 0 else 0)
            return round(total_score, 3), details
        except Exception as e:
            details["error"] = str(e)
            return 0.0, details

    def _calc_macro_trend_score(self, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算宏观层趋势评分
        TS_macro = 0.65 * TS_BTC4H + 0.35 * TS_BTC1H
        """
        details = {}

        # 获取权重
        macro_weights = params.get("macro_weights", {})
        w_4h = float(macro_weights.get("btc_4h", 0.65))
        w_1h = float(macro_weights.get("btc_1h", 0.35))

        ts_4h, details_4h = self._calc_btc_4h_trend_score(params)
        ts_1h, details_1h = self._calc_btc_1h_trend_score(params)

        details["btc_4h"] = details_4h
        details["btc_1h"] = details_1h
        details["ts_4h"] = ts_4h
        details["ts_1h"] = ts_1h

        ts_macro = w_4h * ts_4h + w_1h * ts_1h
        return round(ts_macro, 3), details

    def _calc_market_breadth_score(self, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算市场广度评分（Top20币种一致性）
        因子F: 上涨币比例 (权重 0.7)
        因子G: 强弱分化程度 (权重 0.3)
        """
        details: Dict[str, Any] = {"breadth": 0.0, "dispersion": 0.0}

        try:
            # 获取主流币列表
            top_symbols = params.get("breadth_symbols", [
                "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT",
                "DOTUSDT", "LTCUSDT", "NEARUSDT", "BCHUSDT", "UNIUSDT",
                "APTUSDT", "ARBUSDT", "OPUSDT", "ATOMUSDT", "SUIUSDT"
            ])

            returns = []
            for symbol in top_symbols[:20]:
                try:
                    df = self._dca_get_klines_df(symbol, "1h", limit=24)
                    if df is not None and len(df) >= 2:
                        ret = (float(df["close"].iloc[-1]) - float(df["close"].iloc[0])) / float(df["close"].iloc[0])
                        returns.append(ret)
                except Exception:
                    continue

            if len(returns) < 5:
                return 0.0, details

            # 因子F: 上涨币比例
            up_count = sum(1 for r in returns if r > 0)
            breadth = up_count / len(returns)
            details["breadth"] = round(breadth, 2)

            if breadth > 0.7:
                score_f = 1.0
            elif breadth > 0.5:
                score_f = 0.5
            elif breadth > 0.3:
                score_f = 0.0
            else:
                score_f = -1.0

            # 因子G: 强弱分化程度
            import statistics
            dispersion = statistics.stdev(returns) if len(returns) > 1 else 0
            details["dispersion"] = round(dispersion, 4)

            # 归一化分化度（高分化=低一致性=趋势衰减）
            dispersion_norm = min(1.0, dispersion / 0.1)  # 10%波动视为高分化
            score_g = 1.0 - dispersion_norm

            # 获取权重
            factors = params.get("market_factors", {})
            w_f = float(factors.get("breadth", 0.7))
            w_g = float(factors.get("dispersion", 0.3))

            ts_market = w_f * score_f + w_g * score_g
            self._market_breadth_cache = {"ts": ts_market, "breadth": breadth, "dispersion": dispersion}
            return round(ts_market, 3), details
        except Exception as e:
            details["error"] = str(e)
            return 0.0, details

    def _calc_asset_trend_score(self, symbol: str, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算交易对自身趋势评分
        TS_asset = 0.7 * TS_30M + 0.3 * TS_15M
        """
        details = {}

        # 获取权重
        asset_weights = params.get("asset_factors", {})
        w_30m = float(asset_weights.get("30m", 0.7))
        w_15m = float(asset_weights.get("15m", 0.3))

        # 30M 评分
        ts_30m, details_30m = self._calc_asset_30m_score(symbol, params)
        # 15M 评分
        ts_15m, details_15m = self._calc_asset_15m_score(symbol, params)

        details["30m"] = details_30m
        details["15m"] = details_15m
        details["ts_30m"] = ts_30m
        details["ts_15m"] = ts_15m

        ts_asset = w_30m * ts_30m + w_15m * ts_15m
        return round(ts_asset, 3), details

    def _calc_asset_30m_score(self, symbol: str, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算交易对30M趋势评分
        因子H: 相对强弱 RS (权重 0.4)
        因子I: 趋势结构 (权重 0.3)
        因子J: 布林带突破质量 (权重 0.3)
        """
        details: Dict[str, Any] = {"rs": 0.0, "structure": 0.0, "bb_breakout": 0.0}
        try:
            df = self._dca_get_klines_df(symbol, "30m", limit=100)
            df_btc = self._dca_get_klines_df("BTCUSDT", "30m", limit=100)

            if df is None or len(df) < 55:
                return 0.0, details

            close = df["close"]
            ema20 = close.ewm(span=20, adjust=False).mean()
            ema50 = close.ewm(span=50, adjust=False).mean()

            # 因子H: 相对强弱 RS (vs BTC)
            score_h = 0.0
            if df_btc is not None and len(df_btc) >= 2:
                asset_ret = (float(close.iloc[-1]) - float(close.iloc[-24])) / float(close.iloc[-24])
                btc_ret = (float(df_btc["close"].iloc[-1]) - float(df_btc["close"].iloc[-24])) / float(df_btc["close"].iloc[-24])
                rs = asset_ret / btc_ret if btc_ret != 0 else 1.0
                details["rs"] = round(rs, 2)

                if rs > 1.2:
                    score_h = 1.0
                elif rs > 1.0:
                    score_h = 0.5
                elif rs < 0.8:
                    score_h = -1.0
                elif rs < 1.0:
                    score_h = -0.5

            # 因子I: 趋势结构
            last_ema20 = float(ema20.iloc[-1])
            last_ema50 = float(ema50.iloc[-1])
            if last_ema20 > last_ema50:
                score_i = 1.0
                details["structure"] = "bullish"
            elif last_ema20 < last_ema50:
                score_i = -1.0
                details["structure"] = "bearish"
            else:
                score_i = 0.0
                details["structure"] = "neutral"

            # 因子J: 布林带突破质量
            sma20 = close.rolling(window=20).mean()
            std20 = close.rolling(window=20).std()
            upper = sma20 + 2 * std20
            lower = sma20 - 2 * std20
            last_close = float(close.iloc[-1])
            prev_close = float(close.iloc[-2])
            last_middle = float(sma20.iloc[-1])
            last_upper = float(upper.iloc[-1])
            last_lower = float(lower.iloc[-1])

            score_j = 0.0
            # 突破上轨且回踩中轨不破
            if prev_close > last_upper and last_close > last_middle:
                score_j = 1.0
                details["bb_breakout"] = "bullish_breakout"
            # 假突破回落
            elif prev_close > last_upper and last_close < last_middle:
                score_j = -1.0
                details["bb_breakout"] = "fake_breakout"
            # 跌破下轨且反弹不破中轨
            elif prev_close < last_lower and last_close < last_middle:
                score_j = -1.0
                details["bb_breakout"] = "bearish_breakout"
            elif prev_close < last_lower and last_close > last_middle:
                score_j = 1.0
                details["bb_breakout"] = "bullish_reversal"

            # 获取权重
            factors = params.get("asset_30m_factors", {})
            w_h = float(factors.get("relative_strength", 0.4))
            w_i = float(factors.get("trend_structure", 0.3))
            w_j = float(factors.get("bb_breakout", 0.3))

            ts_30m = w_h * score_h + w_i * score_i + w_j * score_j
            return round(ts_30m, 3), details
        except Exception as e:
            details["error"] = str(e)
            return 0.0, details

    def _calc_asset_15m_score(self, symbol: str, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算交易对15M入场节奏评分
        因子K: 回踩确认 (权重 0.7)
        因子L: 波动率过滤 (权重 0.3)
        """
        details: Dict[str, Any] = {"pullback": 0.0, "volatility": 0.0}
        try:
            # ATR timeframe：优先读取 risk.atr_timeframe，其次 position_sizing.atr_timeframe，默认 15m
            risk_cfg = params.get("risk", {}) if isinstance(params, dict) else {}
            sizing_cfg = params.get("position_sizing", {}) if isinstance(params, dict) else {}
            atr_tf = str(risk_cfg.get("atr_timeframe") or sizing_cfg.get("atr_timeframe") or "15m")
            df = self._dca_get_klines_df(symbol, atr_tf, limit=50)
            details["atr_timeframe"] = atr_tf
            if df is None or len(df) < 30:
                return 0.0, details

            close = df["close"]
            high = df["high"]
            low = df["low"]

            ema20 = close.ewm(span=20, adjust=False).mean()
            last_close = float(close.iloc[-1])
            last_ema20 = float(ema20.iloc[-1])
            prev_ema20 = float(ema20.iloc[-2])

            # 计算 RSI
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / (loss + 1e-10)
            rsi = 100 - (100 / (1 + rs))
            last_rsi = float(rsi.iloc[-1])

            # 因子K: 回踩确认
            score_k = 0.0
            # 趋势向上 + 回踩EMA20不破 + RSI>50
            if last_ema20 > prev_ema20 and last_close > last_ema20 and last_rsi > 50:
                score_k = 1.0
                details["pullback"] = "bullish_pullback"
            # 趋势向下 + 反弹不破EMA20 + RSI<50
            elif last_ema20 < prev_ema20 and last_close < last_ema20 and last_rsi < 50:
                score_k = -1.0
                details["pullback"] = "bearish_pullback"

            # 因子L: 波动率过滤 (ATR)
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = float(tr.rolling(window=14).mean().iloc[-1])
            atr_pct = atr / last_close if last_close > 0 else 0
            details["volatility"] = round(atr_pct, 4)

            # ATR 过大降低评分
            score_l = 1.0
            if atr_pct > 0.03:  # 波动率 > 3%
                score_l = 0.5
            elif atr_pct < 0.005:  # 波动率 < 0.5%
                score_l = 0.7

            # 获取权重
            factors = params.get("asset_15m_factors", {})
            w_k = float(factors.get("pullback_confirm", 0.7))
            w_l = float(factors.get("volatility_filter", 0.3))

            ts_15m = w_k * score_k + w_l * score_l
            return round(ts_15m, 3), details
        except Exception as e:
            details["error"] = str(e)
            return 0.0, details

    def _detect_oscillation_market(self, params: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        检测是否为震荡市场
        条件: ATR下降 + 布林带收窄 + ADX<20
        """
        details: Dict[str, Any] = {"atr_decline": False, "bb_narrow": False, "adx_low": False}
        try:
            df = self._dca_get_klines_df("BTCUSDT", "4h", limit=50)
            if df is None or len(df) < 30:
                return False, details

            # 获取阈值
            osc_cfg = params.get("oscillation_detection", {})
            atr_threshold = float(osc_cfg.get("atr_decline_threshold", 0.1))
            bb_threshold = float(osc_cfg.get("bb_width_threshold", 0.05))
            adx_threshold = float(osc_cfg.get("adx_threshold", 20))

            close = df["close"]
            high = df["high"]
            low = df["low"]

            # 1. ATR 是否下降
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(window=14).mean()
            atr_current = float(atr.iloc[-1])
            atr_prev = float(atr.iloc[-5])
            atr_decline = (atr_prev - atr_current) / atr_prev if atr_prev > 0 else 0
            details["atr_decline"] = atr_decline > atr_threshold

            # 2. 布林带宽度
            sma20 = close.rolling(window=20).mean()
            std20 = close.rolling(window=20).std()
            upper = sma20 + 2 * std20
            lower = sma20 - 2 * std20
            bb_width = (float(upper.iloc[-1]) - float(lower.iloc[-1])) / float(sma20.iloc[-1]) if sma20.iloc[-1] > 0 else 0
            details["bb_width"] = round(bb_width, 4)
            details["bb_narrow"] = bb_width < bb_threshold

            # 3. ADX
            adx = self._calc_adx(df, period=14)
            details["adx"] = round(adx, 2)
            details["adx_low"] = adx < adx_threshold

            # 综合判断（至少2个条件满足）
            osc_count = sum([details["atr_decline"], details["bb_narrow"], details["adx_low"]])
            is_oscillation = osc_count >= 2

            return is_oscillation, details
        except Exception as e:
            details["error"] = str(e)
            return False, details

    def _calc_trend_score(self, symbol: str, params: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        计算综合趋势评分
        TS = 0.45 * TS_macro + 0.25 * TS_market + 0.30 * TS_asset
        """
        details = {}

        # 获取权重
        weights = params.get("trend_score_weights", {})
        w_macro = float(weights.get("macro", 0.45))
        w_market = float(weights.get("market", 0.25))
        w_asset = float(weights.get("asset", 0.30))

        # 计算各层评分
        ts_macro, details_macro = self._calc_macro_trend_score(params)
        ts_market, details_market = self._calc_market_breadth_score(params)
        ts_asset, details_asset = self._calc_asset_trend_score(symbol, params)

        details["macro"] = details_macro
        details["market"] = details_market
        details["asset"] = details_asset
        details["ts_macro"] = ts_macro
        details["ts_market"] = ts_market
        details["ts_asset"] = ts_asset

        # 综合评分
        ts = w_macro * ts_macro + w_market * ts_market + w_asset * ts_asset

        # 检测震荡市
        is_oscillation, osc_details = self._detect_oscillation_market(params)
        details["oscillation"] = osc_details
        details["is_oscillation"] = is_oscillation

        # 更新缓存
        self._trend_score_cache = {
            "ts": ts,
            "ts_macro": ts_macro,
            "ts_market": ts_market,
            "ts_asset": {symbol: ts_asset},
            "is_oscillation": is_oscillation,
            "details": details,
        }

        return round(ts, 3), details

    def _get_regime_from_ts(self, ts: float, params: Dict[str, Any]) -> Tuple[str, str]:
        """
        根据趋势评分获取市场状态
        TS > 0.65: 强牛趋势
        0.3 ~ 0.65: 弱牛
        -0.3 ~ 0.3: 震荡
        -0.65 ~ -0.3: 弱熊
        TS < -0.65: 强熊趋势
        """
        thresholds = params.get("regime_thresholds", {})
        strong_bull = float(thresholds.get("strong_bull", 0.65))
        weak_bull = float(thresholds.get("weak_bull", 0.30))
        weak_bear = float(thresholds.get("weak_bear", -0.30))
        strong_bear = float(thresholds.get("strong_bear", -0.65))

        if ts >= strong_bull:
            return "STRONG_BULL", "强牛趋势"
        elif ts >= weak_bull:
            return "WEAK_BULL", "弱牛"
        elif ts >= weak_bear:
            return "NEUTRAL", "震荡"
        elif ts >= strong_bear:
            return "WEAK_BEAR", "弱熊"
        else:
            return "STRONG_BEAR", "强熊趋势"

    def _check_transition_confirm(self, params: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        检查趋势转换的三重确认条件
        1. 结构破坏
        2. 成交量确认
        3. BTC确认
        """
        confirm_cfg = params.get("transition_confirm", {})
        vol_ratio_min = float(confirm_cfg.get("volume_ratio_min", 1.5))
        adx_min = float(confirm_cfg.get("adx_min", 20))
        structure_required = bool(confirm_cfg.get("structure_break_required", True))

        state: Dict[str, Any] = {
            "structure_break": False,
            "volume_confirmed": False,
            "btc_confirmed": False,
            "all_confirmed": False,
        }

        try:
            # 获取 BTC 1H 数据检查结构破坏
            df = self._dca_get_klines_df("BTCUSDT", "1h", limit=50)
            if df is not None and len(df) >= 30:
                high = df["high"]
                low = df["low"]
                close = df["close"]
                volume = df["volume"]

                recent_high = float(high.iloc[-20:].max())
                recent_low = float(low.iloc[-20:].min())
                last_close = float(close.iloc[-1])
                prev_close = float(close.iloc[-2])

                # 结构破坏
                if (last_close < recent_low and prev_close >= recent_low) or \
                   (last_close > recent_high and prev_close <= recent_high):
                    state["structure_break"] = True

                # 成交量确认
                vol_ma = float(volume.iloc[-20:].mean())
                last_vol = float(volume.iloc[-1])
                if vol_ma > 0 and last_vol / vol_ma >= vol_ratio_min:
                    state["volume_confirmed"] = True

                # BTC 确认（检查 BTC 4H 方向一致）
                df_4h = self._dca_get_klines_df("BTCUSDT", "4h", limit=60)
                if df_4h is not None and len(df_4h) >= 55:
                    adx = self._calc_adx(df_4h, period=14)
                    if adx >= adx_min:
                        state["btc_confirmed"] = True

            # 综合判断
            if structure_required:
                state["all_confirmed"] = (state["structure_break"] and
                                          state["volume_confirmed"] and
                                          state["btc_confirmed"])
            else:
                state["all_confirmed"] = state["volume_confirmed"] and state["btc_confirmed"]

            self._transition_confirm_state = state
            return state["all_confirmed"], state
        except Exception as e:
            state["error"] = str(e)
            return False, state

    def _calc_position_size_by_atr(
        self,
        symbol: str,
        params: Dict[str, Any],
        regime: str = "RANGE",
    ) -> Tuple[float, Dict[str, Any]]:
        """
        根据波动率计算仓位大小（机构级风险预算）

        核心公式：
        - risk_amount = equity × risk_pct × regime_risk_mult × meme_risk_mult
        - atr_notional = risk_amount / (ATR × stop_factor)

        Args:
            symbol: 交易对
            params: 配置参数
            regime: 状态机状态（用于获取风险倍数）

        Returns:
            Tuple[float, Dict]: (atr_notional, details)
        """
        details: Dict[str, Any] = {}
        try:
            # 获取参数
            sizing_cfg = params.get("position_sizing", {})
            risk_pct = float(sizing_cfg.get("risk_per_trade_pct", 0.015))
            atr_mult = float(sizing_cfg.get("atr_stop_multiplier", 2.0))
            meme_mult = float(sizing_cfg.get("meme_stop_multiplier", 3.0))
            meme_risk_mult = float(sizing_cfg.get("meme_risk_mult", 1.0))
            # risk 层配置兼容：优先 self.config.risk，其次 params.risk（如存在）
            risk_cfg: Dict[str, Any] = {}
            if isinstance(getattr(self, "config", {}), dict):
                risk_raw = self.config.get("risk", {})
                if isinstance(risk_raw, dict):
                    risk_cfg = risk_raw
            params_risk = params.get("risk", {}) if isinstance(params, dict) else {}
            if isinstance(params_risk, dict):
                merged_risk = dict(risk_cfg)
                merged_risk.update(params_risk)
                risk_cfg = merged_risk
            # 若启用 risk.use_atr_stop_loss，则使用 risk.atr_multiplier 覆盖
            if bool(risk_cfg.get("use_atr_stop_loss", False)):
                atr_mult = float(risk_cfg.get("atr_multiplier", atr_mult))

            # 【关键】状态机风险倍数 - 直接作用在 risk_amount 层
            risk_mult_cfg = params.get("risk_mult", {})
            default_risk_mult = {
                "BULL_STRONG": 1.0,
                "BULL_WEAK": 0.6,
                "BEAR_STRONG": 1.0,
                "BEAR_WEAK": 0.6,
                "RANGE": 0.5,
                "RANGE_LOCK": 0.35,
            }
            regime_risk_mult = float(risk_mult_cfg.get(regime, default_risk_mult.get(regime, 1.0)))
            details["regime"] = regime
            details["regime_risk_mult"] = regime_risk_mult

            # 获取账户权益
            account_summary = self.account_data.get_account_summary() or {}
            equity = float(account_summary.get("equity", 100))
            details["equity"] = equity

            # 获取 ATR：优先 risk.atr_timeframe，其次 position_sizing.atr_timeframe
            atr_tf = str(risk_cfg.get("atr_timeframe") or sizing_cfg.get("atr_timeframe") or "15m")
            details["atr_timeframe"] = atr_tf
            df = self._dca_get_klines_df(symbol, atr_tf, limit=50)
            if df is None or len(df) < 30:
                # 默认仓位（保守）
                details["fallback"] = True
                return 3.5, details

            high = df["high"]
            low = df["low"]
            close = df["close"]

            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = float(tr.rolling(window=14).mean().iloc[-1])
            last_close = float(close.iloc[-1])

            details["atr"] = round(atr, 6)
            details["close"] = last_close

            # 判断是否为 MEME 币
            meme_keywords = ["DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF", "BOME", "MEME", "DOGS", "HIPPO", "GOAT"]
            is_meme = any(kw in symbol.upper() for kw in meme_keywords)
            stop_factor = meme_mult if is_meme else atr_mult
            details["is_meme"] = is_meme
            details["stop_factor"] = stop_factor

            # 【核心】计算风险金额 - regime_risk_mult 作用在这一层
            risk_amount = equity * risk_pct * regime_risk_mult
            if is_meme:
                risk_amount *= meme_risk_mult
            details["risk_amount"] = round(risk_amount, 2)

            # 计算仓位（量纲正确：risk_amount/stop_distance = quantity，再乘价格得 notional）
            stop_distance = atr * stop_factor
            details["stop_distance"] = round(stop_distance, 6)
            if stop_distance > 0:
                atr_qty = risk_amount / stop_distance  # 币的数量
            else:
                atr_qty = risk_amount / (last_close * 0.02)  # 默认 2% 止损
            details["atr_qty"] = round(atr_qty, 6)

            # 【关键修正】把 quantity 转换成名义价值（USDT）
            atr_notional = atr_qty * last_close

            # 限制仓位大小：对齐 max_position_pct（优先 params，其次 risk）
            max_pos_raw = self._to_float(params.get("max_position_pct"), default=0.0)
            if max_pos_raw <= 0:
                max_pos_raw = self._to_float(risk_cfg.get("max_position_pct"), default=0.30)
            max_pos_ratio = max_pos_raw if 0 < max_pos_raw <= 1.0 else max_pos_raw / 100.0
            max_pos_ratio = max(0.01, min(0.95, max_pos_ratio))
            details["max_position_pct_cap"] = round(max_pos_ratio, 4)
            atr_notional = max(1.0, min(atr_notional, equity * max_pos_ratio))
            details["atr_notional"] = round(atr_notional, 2)

            return round(atr_notional, 2), details
        except Exception as e:
            details["error"] = str(e)
            return 3.5, details

    # =========================================================================
    # 【牛熊切换状态机】完整实现：滞回 + 去抖 + 冷却 + flip限制
    # =========================================================================

    def _get_regime_sm_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """获取状态机参数（从配置读取，带默认值）"""
        sm_cfg = params.get("regime_state_machine", {})
        return {
            # 滞回阈值（TS范围 [-1, +1]）
            "T_ENTER": float(sm_cfg.get("T_ENTER", 0.35)),
            "T_EXIT": float(sm_cfg.get("T_EXIT", 0.15)),
            "T_RANGE": float(sm_cfg.get("T_RANGE", 0.20)),
            "T_STRONG": float(sm_cfg.get("T_STRONG", 0.65)),
            "T_STRONG_EXIT": float(sm_cfg.get("T_STRONG_EXIT", 0.55)),
            # 去抖确认
            "CONFIRM_BARS": int(sm_cfg.get("CONFIRM_BARS", 3)),
            # 量能确认
            "V_CONFIRM": float(sm_cfg.get("V_CONFIRM", 1.5)),
            # 冷却时间（秒）
            "COOLDOWN_SEC": int(sm_cfg.get("COOLDOWN_SEC", 20 * 60)),
            # flip限制
            "FLIP_LIMIT": int(sm_cfg.get("FLIP_LIMIT", 2)),
            "FLIP_WINDOW_SEC": int(sm_cfg.get("FLIP_WINDOW_SEC", 60 * 60)),
            "RANGE_LOCK_SEC": int(sm_cfg.get("RANGE_LOCK_SEC", 90 * 60)),
            # BOS检测参数
            "BOS_PIVOT_L": int(sm_cfg.get("BOS_PIVOT_L", 2)),
            "BOS_ATR_K": float(sm_cfg.get("BOS_ATR_K", 0.15)),
            "BOS_VALID_WINDOW_SEC": int(sm_cfg.get("BOS_VALID_WINDOW_SEC", 60 * 60)),
        }

    def _init_regime_sm_context(self) -> Dict[str, Any]:
        """初始化状态机上下文（强约束结构，预置所有字段避免 KeyError）"""
        return {
            # 版本号（便于后续迁移/升级）
            "_ver": 1,
            # 状态机核心状态
            "regime": "RANGE",  # 当前状态
            "last_switch_ts": 0.0,  # 上次切换时间戳
            "lock_until_ts": 0.0,  # 锁定到期时间
            "flip_times": [],  # flip时间队列
            "bull_confirm": 0,  # 牛确认计数
            "bear_confirm": 0,  # 熊确认计数
            "last_bos": 0,  # 最近BOS信号 (+1/-1/0)
            "last_bos_ts": 0.0,  # BOS事件时间戳
            "last_bos_event_ts_used": None,  # 已使用的BOS事件时间戳（去重）
            # 【整点缓存】BOS/ATR/VolRatio 只在整点后更新一次
            "cached_bos": 0,
            "cached_bos_ts": 0.0,
            "cached_vol_ratio": 1.0,
            "cached_atr_1h": 0.0,
            "cached_1h_close_time": 0,
            # 缓存 TTL（可选）
            "cache_ttl_sec": 3600,
        }

    def _detect_btc_bos_1h(self, params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """
        检测 BTC 1H 结构破坏（BOS - Break of Structure）
        基于 Pivot/Swing 点检测 + ATR 缓冲 + 收盘确认
        优化：只使用已确认的pivot + 事件去重 + BOS有效期

        Returns:
            Tuple[int, Dict]: (BOS, details)
            BOS: +1 上破, -1 下破, 0 无
        """
        details = {}
        sm_params = self._get_regime_sm_params(params)
        pivot_l = sm_params["BOS_PIVOT_L"]
        atr_k = sm_params["BOS_ATR_K"]
        bos_valid_window = sm_params["BOS_VALID_WINDOW_SEC"]
        now_ts = time.time()

        try:
            # 获取 BTC 1H K线（增加到300根，覆盖更长时间）
            df = self._dca_get_klines_df("BTCUSDT", "1h", limit=300)
            if df is None or len(df) < 50:
                return 0, {"error": "insufficient data"}

            high = df["high"]
            low = df["low"]
            close = df["close"]

            # 计算 ATR(14)
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = float(tr.rolling(window=14).mean().iloc[-1])
            buffer = atr_k * atr

            # ===== 关键优化：只扫描到 len-1-L，保证右侧L根已存在且不会再变 =====
            end_idx = len(high) - 1 - pivot_l
            last_swing_high = None
            last_swing_low = None

            for i in range(pivot_l, end_idx + 1):
                # Pivot High: 当前高点 > 左右各L根的高点
                is_pivot_high = True
                for k in range(1, pivot_l + 1):
                    if high.iloc[i] <= high.iloc[i - k] or high.iloc[i] <= high.iloc[i + k]:
                        is_pivot_high = False
                        break
                if is_pivot_high:
                    last_swing_high = (i, float(high.iloc[i]))

                # Pivot Low: 当前低点 < 左右各L根的低点
                is_pivot_low = True
                for k in range(1, pivot_l + 1):
                    if low.iloc[i] >= low.iloc[i - k] or low.iloc[i] >= low.iloc[i + k]:
                        is_pivot_low = False
                        break
                if is_pivot_low:
                    last_swing_low = (i, float(low.iloc[i]))

            details["swing_high"] = last_swing_high[1] if last_swing_high else None
            details["swing_low"] = last_swing_low[1] if last_swing_low else None
            details["swing_high_idx"] = last_swing_high[0] if last_swing_high else None
            details["swing_low_idx"] = last_swing_low[0] if last_swing_low else None
            details["atr"] = round(atr, 2)
            details["buffer"] = round(buffer, 2)

            # 检测 BOS（使用最新收盘价）
            last_close = float(close.iloc[-1])
            bos = 0
            bos_event_key = None

            if last_swing_high and last_close > last_swing_high[1] + buffer:
                # ===== 事件去重：同一方向同一swing只触发一次 =====
                swing_idx = last_swing_high[0]
                swing_price_rounded = round(last_swing_high[1], 1)
                bos_event_key = (+1, swing_idx, swing_price_rounded)

                # 检查是否是新的BOS事件
                last_bos_key = self._regime_sm_ctx.get("last_bos_key")
                if bos_event_key != last_bos_key:
                    bos = +1  # 新的上破结构事件
                    details["bos_type"] = "bullish_break"
                    details["break_level"] = last_swing_high[1]
                    details["bos_event_time"] = now_ts
                    self._regime_sm_ctx["last_bos_key"] = bos_event_key
                    self._regime_sm_ctx["last_bos"] = +1
                    self._regime_sm_ctx["last_bos_ts"] = now_ts
                else:
                    # 同一个BOS事件，检查是否在有效期内
                    last_bos_ts = self._regime_sm_ctx.get("last_bos_ts", 0)
                    if now_ts - last_bos_ts <= bos_valid_window:
                        bos = +1  # 有效期内的BOS
                        details["bos_type"] = "bullish_break_valid"
                        details["break_level"] = last_swing_high[1]
                    else:
                        bos = 0  # 过期
                        details["bos_type"] = "bullish_break_expired"

            elif last_swing_low and last_close < last_swing_low[1] - buffer:
                swing_idx = last_swing_low[0]
                swing_price_rounded = round(last_swing_low[1], 1)
                bos_event_key = (-1, swing_idx, swing_price_rounded)

                last_bos_key = self._regime_sm_ctx.get("last_bos_key")
                if bos_event_key != last_bos_key:
                    bos = -1  # 新的下破结构事件
                    details["bos_type"] = "bearish_break"
                    details["break_level"] = last_swing_low[1]
                    details["bos_event_time"] = now_ts
                    self._regime_sm_ctx["last_bos_key"] = bos_event_key
                    self._regime_sm_ctx["last_bos"] = -1
                    self._regime_sm_ctx["last_bos_ts"] = now_ts
                else:
                    last_bos_ts = self._regime_sm_ctx.get("last_bos_ts", 0)
                    if now_ts - last_bos_ts <= bos_valid_window:
                        bos = -1
                        details["bos_type"] = "bearish_break_valid"
                        details["break_level"] = last_swing_low[1]
                    else:
                        bos = 0
                        details["bos_type"] = "bearish_break_expired"
            else:
                details["bos_type"] = "none"

            # 检查BOS有效期（用于状态机判断）
            last_bos_ts = self._regime_sm_ctx.get("last_bos_ts", 0)
            if now_ts - last_bos_ts > bos_valid_window:
                # BOS已过期，返回0
                if bos != 0:
                    details["bos_expired"] = True
                bos = 0

            details["bos_valid"] = bos != 0
            details["last_bos_ts"] = self._regime_sm_ctx.get("last_bos_ts", 0)

            return bos, details

        except Exception as e:
            details["error"] = str(e)
            return 0, details

    def _should_update_1h_cache(self, ctx: Dict[str, Any], grace_seconds: int = 60) -> bool:
        """
        检查是否需要更新1H缓存（整点后grace_seconds秒内才更新）

        Args:
            ctx: 状态机上下文
            grace_seconds: 整点后多少秒内允许更新（默认60秒）

        Returns:
            bool: 是否需要更新
        """
        now = datetime.now()
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        current_hour_ts = current_hour_start.timestamp()

        # 检查是否在整点后grace_seconds秒内
        seconds_since_hour = (now - current_hour_start).total_seconds()
        if seconds_since_hour > grace_seconds:
            # 不在更新窗口内，使用缓存
            return False

        # 检查是否已经为当前小时更新过
        cached_hour_ts = ctx.get("cached_1h_close_time", 0)
        if cached_hour_ts >= current_hour_ts:
            # 已经更新过，不需要重复更新
            return False

        return True

    def _update_btc_1h_indicators(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        更新 BTC 1H 指标缓存（BOS、ATR、VolRatio）
        只在整点后调用一次，避免频繁请求

        Returns:
            Dict: 包含 bos, atr_1h, vol_ratio, close_time 等指标
        """
        result = {
            "bos": 0,
            "bos_details": {},
            "atr_1h": 0.0,
            "vol_ratio": 1.0,
            "close_time": 0,
            "updated": False,
        }

        try:
            # 检查是否需要更新
            ctx = self._regime_sm_ctx
            if not self._should_update_1h_cache(ctx):
                # 使用缓存
                result["bos"] = ctx.get("cached_bos", 0)
                result["atr_1h"] = ctx.get("cached_atr_1h", 0.0)
                result["vol_ratio"] = ctx.get("cached_vol_ratio", 1.0)
                result["close_time"] = ctx.get("cached_1h_close_time", 0)
                result["updated"] = False
                return result

            # ===== 需要更新：拉取 BTC 1H K线 =====
            df_1h = self._dca_get_klines_df("BTCUSDT", "1h", limit=300)
            if df_1h is None or len(df_1h) < 50:
                return result

            high = df_1h["high"]
            low = df_1h["low"]
            close = df_1h["close"]
            volume = df_1h["volume"]

            # 计算 ATR(14)
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr_1h = float(tr.rolling(window=14).mean().iloc[-1])
            result["atr_1h"] = atr_1h

            # 计算 VolRatio（BTC 1H）
            vol_ma = float(volume.iloc[-20:].mean())
            last_vol = float(volume.iloc[-1])
            vol_ratio = last_vol / vol_ma if vol_ma > 0 else 1.0
            result["vol_ratio"] = vol_ratio

            # 获取最后收盘时间
            now = datetime.now()
            current_hour_start = now.replace(minute=0, second=0, microsecond=0)
            close_time = int(current_hour_start.timestamp())
            result["close_time"] = close_time

            # 检测 BOS
            bos, bos_details = self._detect_btc_bos_1h(params)
            result["bos"] = bos
            result["bos_details"] = bos_details

            # 更新缓存
            ctx["cached_bos"] = bos
            ctx["cached_atr_1h"] = atr_1h
            ctx["cached_vol_ratio"] = vol_ratio
            ctx["cached_1h_close_time"] = close_time
            ctx["cached_bos_ts"] = time.time()
            result["updated"] = True

            print(f"🕐 【整点更新】1H指标已刷新: BOS={bos}, ATR={atr_1h:.2f}, VolRatio={vol_ratio:.2f}")

            return result

        except Exception as e:
            result["error"] = str(e)
            return result

    def _prune_flip_times(self, ctx: Dict[str, Any], now: float, sm_params: Dict[str, Any]) -> None:
        """清理过期的flip时间记录"""
        window = sm_params["FLIP_WINDOW_SEC"]
        ctx["flip_times"] = [t for t in ctx["flip_times"] if now - t <= window]

    def _hit_flip_limit(self, ctx: Dict[str, Any], now: float, sm_params: Dict[str, Any]) -> bool:
        """检查是否达到flip限制"""
        self._prune_flip_times(ctx, now, sm_params)
        return len(ctx["flip_times"]) >= sm_params["FLIP_LIMIT"]

    def _can_switch(self, ctx: Dict[str, Any], now: float, sm_params: Dict[str, Any]) -> bool:
        """检查是否允许切换（冷却期检查）"""
        if now < ctx["lock_until_ts"]:
            return False
        if now - ctx["last_switch_ts"] < sm_params["COOLDOWN_SEC"]:
            return False
        return True

    def _update_confirm_counters(self, ctx: Dict[str, Any], ts: float, sm_params: Dict[str, Any]) -> None:
        """更新去抖确认计数器"""
        t_enter = sm_params["T_ENTER"]
        # 牛确认
        if ts >= +t_enter:
            ctx["bull_confirm"] = ctx.get("bull_confirm", 0) + 1
        else:
            ctx["bull_confirm"] = 0
        # 熊确认
        if ts <= -t_enter:
            ctx["bear_confirm"] = ctx.get("bear_confirm", 0) + 1
        else:
            ctx["bear_confirm"] = 0

    def _decide_regime_state_machine(
        self,
        ts: float,
        bos: int,
        vol_ratio: float,
        adx_4h: float,
        params: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
        bos_event_ts: Optional[float] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        牛熊切换状态机核心逻辑（增强版）

        Args:
            ts: 趋势分数 [-1, +1]
            bos: 结构破坏信号 (+1/-1/0)
            vol_ratio: 量能比
            adx_4h: 4H ADX值（可为 None）
            params: 配置参数
            ctx: 状态机上下文（可选，用于持久化）
            bos_event_ts: BOS事件时间戳（秒级，用于判定新鲜度和去重）

        Returns:
            Tuple[str, str, Dict]: (regime, action, details)
            regime: 新状态
            action: "HOLD" / "TRANSITIONED" / "RANGE_LOCK"
            details: 详细信息
        """
        sm_params = self._get_regime_sm_params(params)
        now = time.time()

        # ctx init
        if ctx is None:
            if not hasattr(self, "_regime_sm_ctx") or not self._regime_sm_ctx:
                self._regime_sm_ctx = self._init_regime_sm_context()
            ctx = self._regime_sm_ctx

        # ----- BOS事件新鲜度和去重判定 -----
        BOS_VALID_WINDOW_SEC = sm_params.get("BOS_VALID_WINDOW_SEC", 3600)
        bos_fresh = (bos_event_ts is not None) and ((now - bos_event_ts) <= BOS_VALID_WINDOW_SEC)
        bos_unused = (bos_event_ts is not None) and (bos_event_ts != ctx.get("last_bos_event_ts_used"))

        # ----- ADX兼容 None -----
        adx_ok = adx_4h is not None
        adx_chop = adx_ok and (adx_4h < 20)

        details = {
            "ts": ts,
            "bos": bos,
            "bos_event_ts": bos_event_ts,
            "bos_fresh": bos_fresh,
            "bos_unused": bos_unused,
            "vol_ratio": vol_ratio,
            "adx_4h": adx_4h,
            "bull_confirm": ctx.get("bull_confirm", 0),
            "bear_confirm": ctx.get("bear_confirm", 0),
            "regime_old": ctx.get("regime"),
        }

        old_regime = ctx["regime"]

        # ===== 0. RANGE_LOCK 到期自动解锁 =====
        if ctx["regime"] == "RANGE_LOCK":
            if now >= ctx.get("lock_until_ts", 0):
                ctx["regime"] = "RANGE"
                details["action"] = "UNLOCK_RANGE"
            else:
                details["action"] = "RANGE_LOCK_HOLD"
                return ctx["regime"], "HOLD", details

        # ===== 1. flip限制检查 -> RANGE_LOCK =====
        if self._hit_flip_limit(ctx, now, sm_params):
            ctx["regime"] = "RANGE_LOCK"
            ctx["lock_until_ts"] = now + sm_params["RANGE_LOCK_SEC"]
            details["action"] = "FLIP_LIMIT_TRIGGERED"
            details["flip_count"] = len(ctx["flip_times"])
            return ctx["regime"], "RANGE_LOCK", details

        # ===== 2. 更新去抖计数器 =====
        self._update_confirm_counters(ctx, ts, sm_params)

        confirm_bars = sm_params["CONFIRM_BARS"]
        v_confirm = sm_params["V_CONFIRM"]

        # 预判：是否已经"准备好大切换"（避免被RANGE提前截断）
        ready_bull = (
            ctx["bull_confirm"] >= confirm_bars
            and bos == +1
            and bos_fresh
            and bos_unused
            and vol_ratio >= v_confirm
        )
        ready_bear = (
            ctx["bear_confirm"] >= confirm_bars
            and bos == -1
            and bos_fresh
            and bos_unused
            and vol_ratio >= v_confirm
        )

        # ===== 3. RANGE 判定（不算大切换） =====
        is_range = (abs(ts) <= sm_params["T_RANGE"]) or adx_chop

        # 如果已经ready_bull/ready_bear，则允许绕过range判定
        if is_range and not (ready_bull or ready_bear):
            if ctx["regime"] != "RANGE_LOCK":
                ctx["regime"] = "RANGE"
                details["action"] = "ENTER_RANGE"
            return ctx["regime"], "HOLD", details

        # ===== 4. 检查是否允许大切换（冷却/锁定） =====
        can_switch = self._can_switch(ctx, now, sm_params)

        # ===== 4.5 滞回退出（T_EXIT）：趋势衰减时更平滑 =====
        T_EXIT = sm_params.get("T_EXIT", 0.15)
        if ctx["regime"] in ("BULL_WEAK", "BULL_STRONG") and ts < +T_EXIT and not ready_bull:
            ctx["regime"] = "RANGE"
            details["action"] = "BULL_EXIT_TO_RANGE"
            return ctx["regime"], "HOLD", details
        if ctx["regime"] in ("BEAR_WEAK", "BEAR_STRONG") and ts > -T_EXIT and not ready_bear:
            ctx["regime"] = "RANGE"
            details["action"] = "BEAR_EXIT_TO_RANGE"
            return ctx["regime"], "HOLD", details

        # ===== 5. 如果在冷却期，只做强弱升级降级 =====
        if not can_switch:
            if ctx["regime"] in ("BULL_WEAK", "BULL_STRONG"):
                is_strong = (ts >= sm_params["T_STRONG"]) and (adx_4h is None or adx_4h >= 25)
                ctx["regime"] = "BULL_STRONG" if is_strong else "BULL_WEAK"
            elif ctx["regime"] in ("BEAR_WEAK", "BEAR_STRONG"):
                is_strong = (ts <= -sm_params["T_STRONG"]) and (adx_4h is None or adx_4h >= 25)
                ctx["regime"] = "BEAR_STRONG" if is_strong else "BEAR_WEAK"
            details["action"] = "COOLDOWN_HOLD"
            return ctx["regime"], "HOLD", details

        # ===== 6. 大切换：熊 -> 牛 =====
        if ready_bull:
            old_regime = ctx["regime"]
            ctx["last_switch_ts"] = now
            is_strong = (ts >= sm_params["T_STRONG"]) and (adx_4h is None or adx_4h >= 25)
            ctx["regime"] = "BULL_STRONG" if is_strong else "BULL_WEAK"
            ctx["last_bos_event_ts_used"] = bos_event_ts

            # flip只统计 BULL<->BEAR
            if old_regime.startswith("BEAR"):
                ctx["flip_times"].append(now)

            details["action"] = "BULL_TRANSITION"
            return ctx["regime"], "TRANSITIONED", details

        # ===== 7. 大切换：牛 -> 熊 =====
        if ready_bear:
            old_regime = ctx["regime"]
            ctx["last_switch_ts"] = now
            is_strong = (ts <= -sm_params["T_STRONG"]) and (adx_4h is None or adx_4h >= 25)
            ctx["regime"] = "BEAR_STRONG" if is_strong else "BEAR_WEAK"
            ctx["last_bos_event_ts_used"] = bos_event_ts

            if old_regime.startswith("BULL"):
                ctx["flip_times"].append(now)

            details["action"] = "BEAR_TRANSITION"
            return ctx["regime"], "TRANSITIONED", details

        # ===== 8. 强弱升级降级（同向） =====
        if ctx["regime"] in ("BULL_WEAK", "BULL_STRONG"):
            is_strong = (ts >= sm_params["T_STRONG"]) and (adx_4h is None or adx_4h >= 25)
            ctx["regime"] = "BULL_STRONG" if is_strong else "BULL_WEAK"
        elif ctx["regime"] in ("BEAR_WEAK", "BEAR_STRONG"):
            is_strong = (ts <= -sm_params["T_STRONG"]) and (adx_4h is None or adx_4h >= 25)
            ctx["regime"] = "BEAR_STRONG" if is_strong else "BEAR_WEAK"
        else:
            # 非趋势态则维持/回到RANGE
            ctx["regime"] = "RANGE"

        details["action"] = "HOLD"
        return ctx["regime"], "HOLD", details

    def _get_regime_position_limits_sm(self, regime: str, params: Dict[str, Any]) -> Tuple[int, int]:
        """
        根据状态机状态获取持仓上限（区分强弱态）

        Args:
            regime: 状态机状态 (BULL_STRONG/BULL_WEAK/RANGE/BEAR_WEAK/BEAR_STRONG/RANGE_LOCK)
            params: 配置参数

        Returns:
            Tuple[int, int]: (max_long, max_short)
        """
        max_positions = int(params.get("max_positions", 4))

        # fallback（兼容旧配置）
        bull_max_long = int(params.get("bull_max_long", 4))
        bear_max_short = int(params.get("bear_max_short", 4))

        # 强弱态区分的持仓上限
        bull_strong_max_long = int(params.get("bull_strong_max_long", bull_max_long))
        bull_weak_max_long = int(params.get("bull_weak_max_long", max(1, bull_max_long // 2)))

        bear_strong_max_short = int(params.get("bear_strong_max_short", bear_max_short))
        bear_weak_max_short = int(params.get("bear_weak_max_short", max(1, bear_max_short // 2)))

        if regime == "BULL_STRONG":
            max_long, max_short = bull_strong_max_long, 0
        elif regime == "BULL_WEAK":
            max_long, max_short = bull_weak_max_long, 0
        elif regime == "BEAR_STRONG":
            max_long, max_short = 0, bear_strong_max_short
        elif regime == "BEAR_WEAK":
            max_long, max_short = 0, bear_weak_max_short
        elif regime == "RANGE":
            # RANGE 下严格跟随主配置，不再使用 oscillation_mode.max_long/max_short
            max_long = int(params.get("max_long_positions", 2))
            max_short = int(params.get("max_short_positions", 2))
        elif regime == "RANGE_LOCK":
            # RANGE_LOCK 允许使用 lock 专用上限，未配置时回退主配置
            osc_mode = params.get("oscillation_mode", {})
            max_long = int(osc_mode.get("range_lock_max_long", params.get("max_long_positions", 2)))
            max_short = int(osc_mode.get("range_lock_max_short", params.get("max_short_positions", 2)))
        else:
            max_long = int(params.get("neutral_max_long", 2))
            max_short = int(params.get("neutral_max_short", 2))

        # 确保不超过总持仓限制
        max_long = max(0, min(max_positions, max_long))
        max_short = max(0, min(max_positions, max_short))

        return max_long, max_short

    def _get_regime_risk_mult(self, regime: str, params: Dict[str, Any]) -> float:
        """
        根据状态机状态获取风险倍数

        Args:
            regime: 状态机状态
            params: 配置参数

        Returns:
            float: 风险倍数 (0.35 ~ 1.0)
        """
        risk_mult_config = params.get("risk_mult", {})
        default_mult = {
            "BULL_STRONG": 1.0,
            "BULL_WEAK": 0.6,
            "BEAR_STRONG": 1.0,
            "BEAR_WEAK": 0.6,
            "RANGE": 0.5,
            "RANGE_LOCK": 0.35,
        }
        return float(risk_mult_config.get(regime, default_mult.get(regime, 1.0)))

    def _map_regime_to_engine(self, regime: str) -> str:
        """将状态机 regime 映射为交易引擎：RANGE / TREND。"""
        r = str(regime or "").upper()
        if r in ("RANGE", "RANGE_LOCK", "NEUTRAL", "UNKNOWN", ""):
            return "RANGE"
        if (
            "BULL" in r
            or "BEAR" in r
            or r in ("STRONG_BULL", "STRONG_BEAR", "WEAK_BULL", "WEAK_BEAR", "TREND")
        ):
            return "TREND"
        return "RANGE"

    @staticmethod
    def _resolve_dual_engine(engine: Any, fallback: str = "TREND") -> str:
        """双引擎模式归一化：支持 RANGE/TREND/UNKNOWN。"""
        e = str(engine or "").upper()
        if e in ("RANGE", "TREND", "UNKNOWN"):
            return e
        fb = str(fallback or "TREND").upper()
        if fb in ("RANGE", "TREND", "UNKNOWN"):
            return fb
        return "TREND"

    def _get_engine_params(
        self,
        params: Dict[str, Any],
        *,
        regime: Optional[str] = None,
        engine: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取引擎参数（最小侵入式默认值，可被 params.engine_mode 覆盖）。
        """
        resolved_engine = str(engine or self._map_regime_to_engine(regime or "RANGE")).upper()
        defaults: Dict[str, Dict[str, float]] = {
            "RANGE": {
                "tp_mult": 0.55,
                "sl_mult": 0.85,
                "be_mult": 0.80,
                "trig_mult": 0.85,
                "trail_mult": 0.90,
                "score_exit_sensitivity": 1.00,
                "max_dca_cap": 1.0,
                "position_mult": 0.80,
            },
            "SWING": {
                "tp_mult": 1.00,
                "sl_mult": 0.75,
                "be_mult": 0.90,
                "trig_mult": 1.00,
                "trail_mult": 1.00,
                "score_exit_sensitivity": 0.60,
                "max_dca_cap": 2.0,
                "position_mult": 1.00,
            },
            "TREND": {
                "tp_mult": 1.80,
                "sl_mult": 0.60,
                "be_mult": 1.25,
                "trig_mult": 1.30,
                "trail_mult": 1.30,
                "score_exit_sensitivity": 0.30,
                "max_dca_cap": 0.0,
                "position_mult": 1.20,
            },
        }
        if resolved_engine not in defaults:
            resolved_engine = "SWING"

        engine_mode = params.get("engine_mode", {}) if isinstance(params, dict) else {}
        engine_cfg = engine_mode.get(resolved_engine, {}) if isinstance(engine_mode, dict) else {}
        base = defaults[resolved_engine]

        out: Dict[str, Any] = {"engine": resolved_engine}
        for key, default_val in base.items():
            raw_val = engine_cfg.get(key, default_val) if isinstance(engine_cfg, dict) else default_val
            try:
                out[key] = float(raw_val)
            except Exception:
                out[key] = float(default_val)
        out["max_dca_cap"] = max(0, int(round(out.get("max_dca_cap", base["max_dca_cap"]))))
        out["score_exit_sensitivity"] = max(0.0, min(1.0, float(out.get("score_exit_sensitivity", 1.0))))
        out["position_mult"] = max(0.2, min(2.0, float(out.get("position_mult", 1.0))))
        return out

    def _direction_allowed_by_engine(self, *, engine: str, regime: str, side: str) -> bool:
        """方向约束：
        - TREND 引擎强制顺势
        - 弱趋势(BULL_WEAK/BEAR_WEAK)默认也强制顺势（可配置关闭）
        - 其他状态允许双向
        """
        e = str(engine or "").upper()
        r = str(regime or "").upper()
        s = str(side or "").upper()
        if r in ("BULL_WEAK", "BEAR_WEAK"):
            weak_lock_enabled = True
            try:
                dca_params = self.dca_config.get("params", {}) if isinstance(getattr(self, "dca_config", {}), dict) else {}
                weak_lock_enabled = bool(dca_params.get("weak_trend_direction_lock", True))
            except Exception:
                weak_lock_enabled = True
            if weak_lock_enabled:
                if r == "BULL_WEAK" and s != "LONG":
                    return False
                if r == "BEAR_WEAK" and s != "SHORT":
                    return False
        if e != "TREND":
            return True
        if "BULL" in r and s != "LONG":
            return False
        if "BEAR" in r and s != "SHORT":
            return False
        return True

    def _pick_regime_ratio(self, cfg: Any, regime: str, default: float) -> float:
        """
        按 regime 从配置中取 ratio（兼容旧配置格式）

        Args:
            cfg: 配置值，可能是 number（旧配置）或 dict（新配置）
            regime: 当前状态机状态
            default: 默认值

        Returns:
            float: 对应 regime 的 ratio 值
        """
        try:
            if isinstance(cfg, (int, float)):
                return float(cfg)
            if isinstance(cfg, dict):
                # 允许只配 RANGE_LOCK / RANGE 任意一个
                if regime in cfg:
                    return float(cfg[regime])
                # RANGE_LOCK 没配时，fallback 到 RANGE
                if "RANGE" in cfg and regime == "RANGE_LOCK":
                    return float(cfg["RANGE"])
                return float(cfg.get("default", default))
        except Exception:
            pass
        return float(default)

    def _get_exit_thresholds_by_regime(
        self,
        params: Dict[str, Any],
        sm_regime: str,
        *,
        engine_override: Optional[str] = None,
        entry_regime: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        统一计算 TP/SL/BE/Trailing 阈值（regime-aware）
        - 返回 dict：effective 阈值 + ratios 全在同一层
        - 提供 debug_string 便于日志
        - RANGE/RANGE_LOCK 下应用 oscillation_mode 的 ratio
        """
        # ---------- base ----------
        engine = self._resolve_dual_engine(engine_override or self._map_regime_to_engine(sm_regime))
        if engine == "UNKNOWN":
            engine = self._resolve_dual_engine(self._map_regime_to_engine(sm_regime))
        engine_params = self._get_engine_params(params, regime=sm_regime, engine=engine)
        base_tp = float(params.get("take_profit_pct", 0.015))
        base_sl = float(params.get("symbol_stop_loss_pct", 0.15))
        base_be_trig = float(params.get("break_even_trigger_pct", 0.05))

        # trailing: 新字段优先，fallback 到旧字段
        base_tr_trig_raw = params.get("trailing_stop_trigger_pct", None)
        if base_tr_trig_raw is None:
            base_tr_trig_raw = params.get("trailing_start_pct", 0.0)
        base_tr_trig = float(base_tr_trig_raw)
        base_tr_sl = float(params.get("trailing_stop_pct", 0.0))

        # ---------- risk-level exit overrides ----------
        risk_cfg = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
        if not isinstance(risk_cfg, dict):
            risk_cfg = {}
        osc_cfg = risk_cfg.get("oscillation", {}) if isinstance(risk_cfg.get("oscillation", {}), dict) else {}
        trend_cfg = risk_cfg.get("trend", {}) if isinstance(risk_cfg.get("trend", {}), dict) else {}
        osc_exit_cfg = osc_cfg.get("exit", {}) if isinstance(osc_cfg.get("exit", {}), dict) else {}
        trend_exit_cfg = trend_cfg.get("exit", {}) if isinstance(trend_cfg.get("exit", {}), dict) else {}
        exit_source = "params"

        def _pick_exit_val(cfg: Dict[str, Any], key: str, default_val: float) -> float:
            if not isinstance(cfg, dict):
                return default_val
            raw = cfg.get(key, default_val)
            if raw is None:
                return default_val
            try:
                return float(raw)
            except Exception:
                return default_val

        if engine == "TREND" and trend_exit_cfg:
            base_tp = _pick_exit_val(trend_exit_cfg, "take_profit_pct", base_tp)
            base_sl = _pick_exit_val(trend_exit_cfg, "symbol_stop_loss_pct", base_sl)
            base_be_trig = _pick_exit_val(trend_exit_cfg, "break_even_trigger_pct", base_be_trig)
            base_tr_trig = _pick_exit_val(
                trend_exit_cfg,
                "trailing_stop_trigger_pct",
                _pick_exit_val(trend_exit_cfg, "trailing_start_pct", base_tr_trig),
            )
            base_tr_sl = _pick_exit_val(trend_exit_cfg, "trailing_stop_pct", base_tr_sl)
            exit_source = "risk.trend.exit"
        elif engine == "RANGE" and osc_exit_cfg:
            base_tp = _pick_exit_val(osc_exit_cfg, "take_profit_pct", base_tp)
            base_sl = _pick_exit_val(osc_exit_cfg, "symbol_stop_loss_pct", base_sl)
            base_be_trig = _pick_exit_val(osc_exit_cfg, "break_even_trigger_pct", base_be_trig)
            base_tr_trig = _pick_exit_val(
                osc_exit_cfg,
                "trailing_stop_trigger_pct",
                _pick_exit_val(osc_exit_cfg, "trailing_start_pct", base_tr_trig),
            )
            base_tr_sl = _pick_exit_val(osc_exit_cfg, "trailing_stop_pct", base_tr_sl)
            exit_source = "risk.oscillation.exit"

        fee = float(params.get("round_trip_fee_pct", 0.0))
        slip = float(params.get("round_trip_slippage_pct", 0.0))
        be_buf = fee + slip

        # ---------- ratios (default 1.0) ----------
        osc_mode = params.get("oscillation_mode", {}) or {}
        tp_ratio = 1.0
        sl_ratio = 1.0
        be_ratio = 1.0
        tr_trig_ratio = 1.0
        tr_sl_ratio = 1.0
        tr_sl_after_be_ratio = 1.0

        if engine == "RANGE":
            reg_for_ratio = str(entry_regime or sm_regime or "").upper()
            ratio_regime = "RANGE_LOCK" if reg_for_ratio == "RANGE_LOCK" else "RANGE"
            tp_ratio = self._pick_regime_ratio(osc_mode.get("take_profit_ratio"), ratio_regime, 1.0)
            sl_ratio = self._pick_regime_ratio(osc_mode.get("stop_loss_ratio"), ratio_regime, 1.0)
            be_ratio = self._pick_regime_ratio(osc_mode.get("break_even_trigger_ratio"), ratio_regime, 1.0)
            tr_trig_ratio = self._pick_regime_ratio(osc_mode.get("trailing_trigger_ratio"), ratio_regime, 1.0)
            tr_sl_ratio = self._pick_regime_ratio(osc_mode.get("trailing_stop_ratio"), ratio_regime, 1.0)
            tr_sl_after_be_ratio = self._pick_regime_ratio(
                osc_mode.get("trailing_stop_after_be_ratio"), ratio_regime, 1.0
            )

        # ---------- effective ----------
        tp = base_tp * tp_ratio
        sl = base_sl * sl_ratio
        be_trig = base_be_trig * be_ratio
        tr_trig = base_tr_trig * tr_trig_ratio
        tr_sl = base_tr_sl * tr_sl_ratio

        # ---------- engine overlay ----------
        tp *= float(engine_params.get("tp_mult", 1.0))
        sl *= float(engine_params.get("sl_mult", 1.0))
        be_trig *= float(engine_params.get("be_mult", 1.0))
        tr_trig *= float(engine_params.get("trig_mult", 1.0))
        tr_sl *= float(engine_params.get("trail_mult", 1.0))

        # ---------- one-layer output ----------
        out: Dict[str, Any] = {
            "regime": sm_regime,
            "engine": engine,

            # base（保留用于 debug/回测核对）
            "base_take_profit_pct": base_tp,
            "base_stop_loss_pct": base_sl,
            "base_break_even_trigger_pct": base_be_trig,
            "base_trailing_trigger_pct": base_tr_trig,
            "base_trailing_stop_pct": base_tr_sl,

            # effective（主逻辑使用）
            "take_profit_pct": tp,
            "stop_loss_pct": sl,
            "break_even_trigger_pct": be_trig,
            "break_even_buffer_pct": be_buf,
            "trailing_trigger_pct": tr_trig,
            "trailing_stop_pct": tr_sl,

            # ratios（同层）
            "take_profit_ratio": tp_ratio,
            "stop_loss_ratio": sl_ratio,
            "break_even_trigger_ratio": be_ratio,
            "trailing_trigger_ratio": tr_trig_ratio,
            "trailing_stop_ratio": tr_sl_ratio,
            "trailing_stop_after_be_ratio": tr_sl_after_be_ratio,
            "score_exit_sensitivity": float(engine_params.get("score_exit_sensitivity", 1.0)),
            "engine_max_dca_cap": int(engine_params.get("max_dca_cap", 3)),
            "engine_position_mult": float(engine_params.get("position_mult", 1.0)),
            "exit_base_source": exit_source,

            # debug string（同层）
            "debug_string": (
                f"🎚 exit regime={sm_regime} engine={engine} src={exit_source} | "
                f"TP={tp:.4f} (base={base_tp:.4f}×{tp_ratio:.2f}) | "
                f"SL={sl:.4f} (base={base_sl:.4f}×{sl_ratio:.2f}) | "
                f"BE_trig={be_trig:.4f} (base={base_be_trig:.4f}×{be_ratio:.2f}) "
                f"BE_buf={be_buf:.4f} | "
                f"TRIG={tr_trig:.4f} (base={base_tr_trig:.4f}×{tr_trig_ratio:.2f}) | "
                f"TRAIL={tr_sl:.4f} (base={base_tr_sl:.4f}×{tr_sl_ratio:.2f}) | "
                f"TRAIL_after_BE×{tr_sl_after_be_ratio:.2f} | "
                f"score_exit_sens={float(engine_params.get('score_exit_sensitivity', 1.0)):.2f}"
            ),
        }

        if verbose:
            print(out["debug_string"])

        return out

    def _ensure_dca_state(
        self,
        symbol: str,
        entry_price: float,
        now: datetime,
        side: Optional[str] = None,
        current_price: Optional[float] = None,
        engine: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        确保 dca_state[symbol] 存在且字段齐全

        Args:
            symbol: 交易对
            entry_price: 入场价格（来自 pos）
            now: 当前时间
            current_price: 当前价格（可选）

        Returns:
            Dict: 确保字段齐全的 state
        """
        state = self.dca_state.get(symbol)
        if not isinstance(state, dict):
            state = {}

        normalized_side = str(side if side is not None else state.get("side", "")).upper()
        if normalized_side in ("LONG", "SHORT"):
            old_side = str(state.get("side", "")).upper()
            if old_side in ("LONG", "SHORT") and old_side != normalized_side:
                # 方向变化后重置状态，避免带入旧方向轨迹
                state = {}
            state["side"] = normalized_side

        if "entry_time" not in state:
            state["entry_time"] = now

        if "last_dca_price" not in state or state.get("last_dca_price") is None:
            state["last_dca_price"] = float(current_price if current_price is not None else entry_price)

        if "dca_count" not in state:
            state["dca_count"] = 0

        if "peak_pnl_pct" not in state or state.get("peak_pnl_pct") is None:
            state["peak_pnl_pct"] = 0.0

        if "be_active" not in state:
            state["be_active"] = False

        current_engine = str(state.get("engine", "") or "").upper()
        requested_engine = str(engine or "").upper()
        if current_engine not in ("RANGE", "TREND"):
            if requested_engine in ("RANGE", "TREND"):
                state["engine"] = requested_engine
            else:
                state["engine"] = "UNKNOWN"
        elif requested_engine in ("RANGE", "TREND"):
            state["engine"] = requested_engine
        state.setdefault("entry_regime", None)

        self.dca_state[symbol] = state
        return state

    def _tag_dca_engine_on_open(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        decision: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """在开仓成功后写入 dca_state 的 engine/entry_regime，保证双引擎出入场一致。"""
        try:
            if now is None:
                now = datetime.now()
            st = self.dca_state.get(symbol)
            if not isinstance(st, dict):
                st = {}
            st["side"] = str(side or "").upper()
            st.setdefault("entry_time", now)
            st.setdefault("last_dca_price", float(entry_price or 0))
            st.setdefault("dca_count", 0)
            st.setdefault("peak_pnl_pct", 0.0)
            st.setdefault("be_active", False)

            eng = None
            entry_reg = None
            if isinstance(decision, dict):
                eng = decision.get("engine")
                entry_reg = decision.get("entry_regime") or decision.get("regime")
            eng_up = str(eng or "").upper()
            if eng_up not in ("RANGE", "TREND"):
                eng_up = self._map_regime_to_engine(str(entry_reg or "").upper() or "RANGE")
            if eng_up not in ("RANGE", "TREND"):
                eng_up = "RANGE"
            st["engine"] = eng_up
            if entry_reg is not None:
                st["entry_regime"] = str(entry_reg).upper()
            else:
                st.setdefault("entry_regime", None)

            self.dca_state[symbol] = st
            self._save_dca_state()
        except Exception:
            return

    def _update_peak_pnl_pct(self, state: Dict[str, Any], pnl_pct: float) -> float:
        """
        更新 peak_pnl_pct（用于 Trailing 止损）

        在当前 pnl_pct 定义下，LONG/SHORT 盈利都为正（越大越好），
        所以统一用 peak = max(pnl_pct)。

        Args:
            state: dca_state 字典
            pnl_pct: 当前盈亏比例

        Returns:
            float: 更新后的 peak_pnl_pct
        """
        peak = float(state.get("peak_pnl_pct", pnl_pct))
        peak = max(peak, pnl_pct)
        state["peak_pnl_pct"] = peak
        return peak

    def _on_dca_add_fill(
        self,
        state: Dict[str, Any],
        current_price: float,
        pnl_pct_after: Optional[float] = None,
        side: Optional[str] = None,
    ) -> None:
        """
        DCA 加仓成交后调用：
        - dca_count += 1
        - last_dca_price = current_price
        - peak_pnl_pct 重置（避免旧 peak 导致 trailing 误触发）

        Args:
            state: dca_state 字典
            current_price: 加仓成交价格
            pnl_pct_after: 加仓后的 pnl_pct（可选，不传则重置为 0）
            side: 方向（可选，用于日志）
        """
        prev_cnt = int(state.get("dca_count", 0))
        state["dca_count"] = prev_cnt + 1
        state["last_dca_price"] = float(current_price)
        # 重置 peak：保守做法，避免刚加仓就触发 trailing
        state["peak_pnl_pct"] = float(pnl_pct_after) if pnl_pct_after is not None else 0.0
        # 加仓后重置 BE 状态，等待新的盈亏路径重新触发
        state["be_active"] = False

        if side:
            print(
                f"➕ DCA加仓成交 {side} | dca_count {prev_cnt}->{state['dca_count']} | "
                f"last_dca_price={current_price:.4f} | peak重置={state['peak_pnl_pct']:.4f}"
            )

    def _check_trailing_stop_by_pnl(
        self,
        state: Dict[str, Any],
        pnl_pct: float,
        trailing_trigger_pct: float,
        trailing_stop_pct: float,
        regime: str = "UNKNOWN",
    ) -> Tuple[bool, Optional[str]]:
        """
        基于 pnl_pct 的 trailing 止损判断

        - 启动条件：peak >= trailing_trigger_pct
        - 触发条件：peak - pnl >= trailing_stop_pct

        适用于当前 pnl_pct 定义（LONG/SHORT 盈利都为正，越大越好）

        Args:
            state: dca_state 字典
            pnl_pct: 当前盈亏比例
            trailing_trigger_pct: 触发阈值
            trailing_stop_pct: 回撤阈值
            regime: 当前市场状态（用于日志）

        Returns:
            Tuple[bool, Optional[str]]: (是否触发, 触发原因)
        """
        if trailing_trigger_pct <= 0 or trailing_stop_pct <= 0:
            return False, None

        peak = self._update_peak_pnl_pct(state, pnl_pct)

        if peak < trailing_trigger_pct:
            return False, None

        drawdown = peak - pnl_pct
        if drawdown >= trailing_stop_pct:
            reason = (
                f"锁利移动止损触发(regime={regime}, "
                f"trigger={trailing_trigger_pct*100:.2f}%, "
                f"trail={trailing_stop_pct*100:.2f}%, "
                f"peak={peak*100:.2f}%, "
                f"回撤={drawdown*100:.2f}% >= {trailing_stop_pct*100:.2f}%)"
            )
            return True, reason

        return False, None

    def _get_regime_open_threshold(self, regime: str, params: Dict[str, Any]) -> Dict[str, float]:
        """
        根据状态机状态获取开仓门槛（弱态更严格）

        Args:
            regime: 状态机状态
            params: 配置参数

        Returns:
            Dict[str, float]: {"min_ts_asset": x, "min_vol_ratio": y}
        """
        open_threshold_config = params.get("regime_open_threshold", {})
        default_threshold = {
            "BULL_STRONG": {"min_ts_asset": 0.30, "min_vol_ratio": 1.3},
            "BULL_WEAK": {"min_ts_asset": 0.45, "min_vol_ratio": 1.5},
            "BEAR_STRONG": {"min_ts_asset": -0.30, "min_vol_ratio": 1.3},
            "BEAR_WEAK": {"min_ts_asset": -0.45, "min_vol_ratio": 1.5},
            "RANGE": {"min_ts_asset": 0.0, "min_vol_ratio": 1.5},
            "RANGE_LOCK": {"min_ts_asset": 0.0, "min_vol_ratio": 2.0},
        }
        raw = open_threshold_config.get(regime, default_threshold.get(regime, default_threshold["RANGE"]))
        if not isinstance(raw, dict):
            raw = default_threshold["RANGE"]
        return {
            "min_ts_asset": float(raw.get("min_ts_asset", 0.0) or 0.0),
            "min_vol_ratio": float(raw.get("min_vol_ratio", 1.5) or 1.5),
        }

    def _dca_detect_symbol_regime(self, symbol: str, params: Dict[str, Any]) -> Tuple[str, float, Dict[str, Any]]:
        """
        基于单个交易对的 K 线判断其自身趋势状态（多因子：EMA+MACD+DMI+VOL）。
        """
        if not bool(params.get("symbol_regime_enabled", True)):
            return "NEUTRAL", 0.0, {}

        symbol_key = str(symbol or "").upper()
        cache_ttl = max(15, int(params.get("symbol_regime_update_seconds", 45) or 45))
        now_ts = time.time()
        cache_item = self._symbol_regime_cache.get(symbol_key, {})
        if (
            cache_item
            and float(cache_item.get("ts", 0.0) or 0.0) > 0
            and (now_ts - float(cache_item.get("ts", 0.0) or 0.0)) < cache_ttl
        ):
            return (
                str(cache_item.get("regime", "NEUTRAL")),
                float(cache_item.get("score", 0.0) or 0.0),
                cache_item.get("details", {}) if isinstance(cache_item.get("details", {}), dict) else {},
            )

        timeframes = params.get("symbol_regime_timeframes", ["5m", "15m", "1h"])
        if isinstance(timeframes, str):
            timeframes = [tf.strip() for tf in timeframes.split(",")]

        factor_weights = self._dca_normalize_regime_factor_weights(
            params.get("symbol_regime_factor_weights", params.get("regime_factor_weights", {}))
        )
        hit_threshold = self._clamp_value(
            float(params.get("combined_signal_factor_hit_threshold", 0.20) or 0.20),
            0.05,
            0.9,
        )

        details: Dict[str, Any] = {}
        total_score = 0.0
        valid_weight = 0.0
        factor_weighted_sum = {k: 0.0 for k in ("ema", "macd", "dmi", "vol")}

        tf_weights = {
            "5m": 1.0,
            "15m": 1.5,
            "1h": 2.0,
        }

        for tf in timeframes:
            weight = float(tf_weights.get(tf, 1.0))
            try:
                df = self._dca_get_klines_df(symbol_key, tf, limit=120)
                if df is None:
                    continue
                calc = self._dca_calc_regime_factor_scores(df, factor_weights, hit_threshold)
                if not calc:
                    continue

                tf_score = float(calc["tf_score"])
                total_score += tf_score * weight
                valid_weight += weight
                for key, val in (calc.get("factor_scores", {}) or {}).items():
                    if key in factor_weighted_sum:
                        factor_weighted_sum[key] += float(val) * weight

                details[tf] = {
                    "score": round(tf_score, 3),
                    "close": round(float(calc.get("close", 0.0)), 6),
                    "ema_fast": round(float(calc.get("ema_fast", 0.0)), 6),
                    "ema_slow": round(float(calc.get("ema_slow", 0.0)), 6),
                    "factor_scores": {k: round(float(v), 3) for k, v in (calc.get("factor_scores", {}) or {}).items()},
                    "signal_grade": str(calc.get("signal_grade", "NONE")),
                    "bull_hits": int(calc.get("bull_hits", 0) or 0),
                    "bear_hits": int(calc.get("bear_hits", 0) or 0),
                    "vol_ratio": round(float(calc.get("vol_ratio", 1.0) or 1.0), 2),
                    "adx": round(float(calc.get("adx", 0.0) or 0.0), 2),
                }
            except Exception as e:
                details[tf] = {"error": str(e)}
                continue

        avg_score = (total_score / valid_weight) if valid_weight > 0 else 0.0
        if avg_score >= 0.35:
            regime = "BULL"
        elif avg_score <= -0.35:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

        factor_scores_avg = {
            k: (factor_weighted_sum[k] / valid_weight if valid_weight > 0 else 0.0)
            for k in factor_weighted_sum
        }
        bull_hits = sum(1 for v in factor_scores_avg.values() if v >= hit_threshold)
        bear_hits = sum(1 for v in factor_scores_avg.values() if v <= -hit_threshold)
        dominant_hits = max(bull_hits, bear_hits)
        signal_grade = self._dca_signal_grade_from_hits(dominant_hits)

        self._symbol_regime_cache[symbol_key] = {
            "regime": regime,
            "score": round(avg_score, 3),
            "ts": now_ts,
            "details": details,
            "factor_scores": {k: round(v, 3) for k, v in factor_scores_avg.items()},
            "bull_hits": int(bull_hits),
            "bear_hits": int(bear_hits),
            "signal_grade": signal_grade,
            "signal_label": self._dca_signal_grade_label(signal_grade),
            "hit_threshold": round(hit_threshold, 3),
        }

        return regime, round(avg_score, 3), details

    def _dca_get_combined_regime(
        self, symbol: str, params: Dict[str, Any]
    ) -> Tuple[str, float, Dict[str, Any]]:
        """
        综合判断交易对的牛熊状态：BTC 市场状态 + 交易对自身状态动态加权。
        
        核心逻辑：
        1. BTC 决定市场整体情绪（主流币跟随度高）
        2. 交易对自身状态反映独立行情（庄家操控、消息驱动等）
        3. 动态权重：当两者方向一致时，强化信号；当矛盾时，保守处理
        
        Returns:
            Tuple[str, float, Dict]: (combined_regime, combined_score, details)
        """
        symbol_key = str(symbol or "").upper()
        cache_ttl = max(10, int(params.get("combined_regime_update_seconds", 45) or 45))
        now_ts = time.time()
        cache_item = self._combined_regime_cache.get(symbol_key, {})
        if (
            cache_item
            and float(cache_item.get("ts", 0.0) or 0.0) > 0
            and (now_ts - float(cache_item.get("ts", 0.0) or 0.0)) < cache_ttl
        ):
            return (
                str(cache_item.get("regime", "NEUTRAL")),
                float(cache_item.get("score", 0.0) or 0.0),
                cache_item.get("details", {}) if isinstance(cache_item.get("details", {}), dict) else {},
            )

        # 获取 BTC / 交易对状态
        btc_regime, btc_score, _btc_details = self._dca_detect_btc_regime(params)
        symbol_regime, symbol_score, _symbol_details = self._dca_detect_symbol_regime(symbol_key, params)

        # 获取权重配置（默认降低 BTC 干扰）
        try:
            btc_weight = float(params.get("combined_regime_btc_weight", 0.35))
        except Exception:
            btc_weight = 0.35
        try:
            btc_weight_min = float(params.get("combined_regime_btc_weight_min", 0.10))
        except Exception:
            btc_weight_min = 0.10
        try:
            btc_weight_max = float(params.get("combined_regime_btc_weight_max", 0.55))
        except Exception:
            btc_weight_max = 0.55
        try:
            match_boost = float(params.get("combined_regime_match_boost", 0.05))
        except Exception:
            match_boost = 0.05
        try:
            mismatch_penalty = float(params.get("combined_regime_mismatch_penalty", 0.15))
        except Exception:
            mismatch_penalty = 0.15

        # 基础清洗
        btc_weight_min = self._clamp_value(btc_weight_min, 0.0, 0.9)
        btc_weight_max = self._clamp_value(btc_weight_max, btc_weight_min, 1.0)
        btc_weight = self._clamp_value(btc_weight, btc_weight_min, btc_weight_max)
        match_boost = max(0.0, match_boost)
        mismatch_penalty = max(0.0, mismatch_penalty)
        symbol_weight = 1.0 - btc_weight

        # 动态权重调整
        direction_match = (btc_score * symbol_score) > 0  # 同向
        if direction_match and abs(btc_score) > 0.2 and abs(symbol_score) > 0.2:
            btc_weight = min(btc_weight_max, btc_weight + match_boost)
            symbol_weight = 1.0 - btc_weight
        elif not direction_match and abs(symbol_score) > abs(btc_score):
            btc_weight = max(btc_weight_min, btc_weight - mismatch_penalty)
            symbol_weight = 1.0 - btc_weight

        # 综合分数（方向）
        combined_score = btc_score * btc_weight + symbol_score * symbol_weight
        if combined_score >= 0.35:
            combined_regime = "BULL"
        elif combined_score <= -0.35:
            combined_regime = "BEAR"
        else:
            combined_regime = "NEUTRAL"

        # 综合信号分级（质量）：EMA+MACD+DMI+VOL 的共振命中数
        hit_threshold = self._clamp_value(
            float(params.get("combined_signal_factor_hit_threshold", 0.20) or 0.20),
            0.05,
            0.9,
        )
        btc_meta = self._btc_regime_cache if isinstance(self._btc_regime_cache, dict) else {}
        symbol_meta = self._symbol_regime_cache.get(symbol_key, {}) if isinstance(self._symbol_regime_cache, dict) else {}
        btc_factors = btc_meta.get("factor_scores", {}) if isinstance(btc_meta.get("factor_scores", {}), dict) else {}
        symbol_factors = (
            symbol_meta.get("factor_scores", {})
            if isinstance(symbol_meta.get("factor_scores", {}), dict)
            else {}
        )
        if not bool(params.get("btc_regime_enabled", True)):
            btc_factors = {}
        if not bool(params.get("symbol_regime_enabled", True)):
            symbol_factors = {}
        factor_scores: Dict[str, float] = {}
        for key in ("ema", "macd", "dmi", "vol"):
            try:
                btc_val = float(btc_factors.get(key, 0.0) or 0.0)
            except Exception:
                btc_val = 0.0
            try:
                sym_val = float(symbol_factors.get(key, 0.0) or 0.0)
            except Exception:
                sym_val = 0.0
            mixed_val = btc_val * btc_weight + sym_val * symbol_weight
            factor_scores[key] = round(self._clamp_value(mixed_val, -1.0, 1.0), 3)

        bull_hits = sum(1 for v in factor_scores.values() if v >= hit_threshold)
        bear_hits = sum(1 for v in factor_scores.values() if v <= -hit_threshold)
        dominant_hits = max(bull_hits, bear_hits)
        signal_grade = self._dca_signal_grade_from_hits(dominant_hits)
        signal_label = self._dca_signal_grade_label(signal_grade)

        details = {
            "btc_regime": btc_regime,
            "btc_score": btc_score,
            "btc_weight": round(btc_weight, 2),
            "symbol_regime": symbol_regime,
            "symbol_score": symbol_score,
            "symbol_weight": round(symbol_weight, 2),
            "direction_match": direction_match,
            "combined_score": round(combined_score, 3),
            "btc_weight_min": round(btc_weight_min, 2),
            "btc_weight_max": round(btc_weight_max, 2),
            "factor_scores": factor_scores,
            "bull_hits": int(bull_hits),
            "bear_hits": int(bear_hits),
            "signal_hits": int(dominant_hits),
            "signal_grade": signal_grade,
            "signal_label": signal_label,
            "signal_hit_threshold": round(hit_threshold, 3),
        }

        self._combined_regime_cache[symbol_key] = {
            "regime": combined_regime,
            "score": round(combined_score, 3),
            "details": details,
            "ts": now_ts,
        }
        return combined_regime, round(combined_score, 3), details

    def _dca_get_regime_position_limits(self, regime: str, params: Dict[str, Any]) -> Tuple[int, int]:
        """
        根据牛熊状态获取多空持仓上限。
        
        Args:
            regime: "BULL" / "BEAR" / "NEUTRAL"
            params: 配置参数
        
        Returns:
            Tuple[int, int]: (max_long_positions, max_short_positions)
        """
        max_positions = int(params.get("max_positions", 6))
        
        if regime == "BULL":
            max_long = int(params.get("bull_max_long", 4))
            max_short = int(params.get("bull_max_short", 2))
        elif regime == "BEAR":
            max_long = int(params.get("bear_max_long", 2))
            max_short = int(params.get("bear_max_short", 4))
        else:  # NEUTRAL
            max_long = int(params.get("neutral_max_long", 3))
            max_short = int(params.get("neutral_max_short", 3))
        
        # 确保不超过总持仓限制
        max_long = max(0, min(max_positions, max_long))
        max_short = max(0, min(max_positions, max_short))
        
        return max_long, max_short

    def _dca_detect_market_regime(self, symbol: str, params: Dict[str, Any]) -> str:
        """
        检测市场牛熊状态。
        使用综合判断：BTC 市场状态 + 交易对自身状态动态加权。
        """
        # 使用综合判断（BTC + 交易对自身）
        if bool(params.get("combined_regime_enabled", True)):
            regime, _score, _details = self._dca_get_combined_regime(symbol, params)
            return regime
        
        # 降级1：仅使用 BTC 多周期判断
        if bool(params.get("btc_regime_enabled", True)):
            regime, _score, _details = self._dca_detect_btc_regime(params)
            return regime
        
        # 降级2：使用原逻辑（交易对自身的 4H K 线）
        if not params.get("trend_filter_enabled", True):
            return "NEUTRAL"

        timeframe = str(params.get("trend_timeframe", "4h"))
        ema_fast = int(params.get("trend_ema_fast", 20))
        ema_slow = int(params.get("trend_ema_slow", 50))
        limit = max(ema_slow + 10, 120)

        df = self._dca_get_klines_df(symbol, timeframe, limit=limit)
        if df is None or len(df) < ema_slow + 5:
            return "NEUTRAL"

        close = df["close"]
        ema_fast_series = close.ewm(span=ema_fast, adjust=False).mean()
        ema_slow_series = close.ewm(span=ema_slow, adjust=False).mean()

        last_close = float(close.iloc[-1])
        last_fast = float(ema_fast_series.iloc[-1])
        last_slow = float(ema_slow_series.iloc[-1])

        if pd.isna(last_close) or pd.isna(last_fast) or pd.isna(last_slow):
            return "NEUTRAL"

        if last_close > last_fast > last_slow:
            return "BULL"
        if last_close < last_fast < last_slow:
            return "BEAR"
        return "NEUTRAL"

    def _dca_apply_regime_thresholds(
        self,
        score_threshold_short: float,
        score_threshold_long: float,
        regime: str,
        params: Dict[str, Any],
    ) -> Tuple[float, float]:
        bull_long_mult = float(params.get("bull_long_threshold_mult", 0.9))
        bull_short_mult = float(params.get("bull_short_threshold_mult", 1.1))
        bear_long_mult = float(params.get("bear_long_threshold_mult", 1.1))
        bear_short_mult = float(params.get("bear_short_threshold_mult", 0.9))

        if regime == "BULL":
            return score_threshold_short * bull_short_mult, score_threshold_long * bull_long_mult
        if regime == "BEAR":
            return score_threshold_short * bear_short_mult, score_threshold_long * bear_long_mult
        return score_threshold_short, score_threshold_long

    @staticmethod
    def _clamp_value(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        return default

    def _dca_normalize_flow_factor_weights(self, raw_cfg: Any) -> Dict[str, float]:
        default_weights = {
            "cvd": 0.24,
            "cvd_mom": 0.14,
            "oi_delta": 0.18,
            "oi_level": 0.08,
            "funding": 0.10,
            "depth": 0.14,
            "imbalance": 0.12,
        }
        keys = tuple(default_weights.keys())
        cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
        weights: Dict[str, float] = {}
        total = 0.0
        for key in keys:
            try:
                val = float(cfg.get(key, default_weights[key]))
            except Exception:
                val = float(default_weights[key])
            val = max(0.0, val)
            weights[key] = val
            total += val
        if total <= 1e-9:
            return default_weights
        return {k: (weights[k] / total) for k in keys}

    @staticmethod
    def _dca_format_flow_brief(snapshot: Dict[str, Any], side: str) -> str:
        if not isinstance(snapshot, dict):
            return "flow=na"
        side_up = str(side or "SHORT").upper()
        score_key = "short_score" if side_up == "SHORT" else "long_score"
        hits_key = "short_hits" if side_up == "SHORT" else "long_hits"
        score_val = float(snapshot.get(score_key, 0.0) or 0.0)
        hits_val = int(snapshot.get(hits_key, 0) or 0)
        total_factors = int(snapshot.get("factor_count", 7) or 7)
        cvd_ratio = float(snapshot.get("cvd_ratio", 0.0) or 0.0)
        oi_delta_ratio = float(snapshot.get("oi_delta_ratio", 0.0) or 0.0)
        funding_rate = float(snapshot.get("funding_rate", 0.0) or 0.0)
        depth_ratio = float(snapshot.get("depth_ratio", 1.0) or 1.0)
        imbalance = float(snapshot.get("order_imbalance", 0.0) or 0.0)
        return (
            f"score={score_val:.2f}, hits={hits_val}/{total_factors}, "
            f"cvd={cvd_ratio:+.3f}, oiΔ={oi_delta_ratio:+.2%}, "
            f"funding={funding_rate:+.5f}, depth={depth_ratio:.3f}, imb={imbalance:+.3f}"
        )


# =========================
# 微结构(15s)采集与聚合
# =========================
def _ms_get_ring(self, sym: str, maxlen: int) -> Any:
    ring = self._ms_ring.get(sym)
    if not isinstance(ring, deque):
        ring = deque(maxlen=maxlen)
        self._ms_ring[sym] = ring
    else:
        # deque 的 maxlen 不能动态修改，必要时重建
        if ring.maxlen != maxlen:
            old = list(ring)
            ring = deque(old[-maxlen:], maxlen=maxlen)
            self._ms_ring[sym] = ring
    return ring

def _ms_get_fapi_base(self) -> str:
    # 允许通过环境变量注入多个 endpoint（逗号分隔），这里只取第一个
    raw = os.getenv("BINANCE_FUTURES_ENDPOINTS", "").strip()
    if raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            return parts[0].rstrip("/")
    return "https://fapi.binance.com"

def _ms_fetch_agg_trades(self, sym: str, start_ms: int, end_ms: int, limit: int = 1000) -> List[Dict[str, Any]]:
    """获取期货 aggTrades（无需签名的公共接口）。若失败返回空列表。"""
    try:
        base = self._ms_get_fapi_base()
        url = f"{base}/fapi/v1/aggTrades"
        params = {"symbol": sym, "startTime": int(start_ms), "endTime": int(end_ms), "limit": int(limit)}
        # requests 会读取系统代理环境变量；如你配置里禁用代理，这里尽量跟随
        timeout = float(getattr(self, "network_timeout", 6.0) or 6.0)
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

@staticmethod
def _ms_parse_book_metrics(order_book: Dict[str, Any], depth_levels: int) -> Dict[str, float]:
    bid_notional = 0.0
    ask_notional = 0.0
    bid1 = ask1 = bid1_qty = ask1_qty = 0.0
    bids = order_book.get("bids", []) if isinstance(order_book, dict) else []
    asks = order_book.get("asks", []) if isinstance(order_book, dict) else []
    if isinstance(bids, list) and bids:
        try:
            bid1 = float(bids[0][0]); bid1_qty = float(bids[0][1])
        except Exception:
            bid1 = bid1_qty = 0.0
    if isinstance(asks, list) and asks:
        try:
            ask1 = float(asks[0][0]); ask1_qty = float(asks[0][1])
        except Exception:
            ask1 = ask1_qty = 0.0

    for item in (bids[:depth_levels] if isinstance(bids, list) else []):
        try:
            p = float(item[0]); q = float(item[1])
            if p > 0 and q > 0:
                bid_notional += p * q
        except Exception:
            continue
    for item in (asks[:depth_levels] if isinstance(asks, list) else []):
        try:
            p = float(item[0]); q = float(item[1])
            if p > 0 and q > 0:
                ask_notional += p * q
        except Exception:
            continue

    mid = 0.0
    spread = 0.0
    if bid1 > 0 and ask1 > 0:
        mid = (bid1 + ask1) / 2.0
        spread = (ask1 - bid1) / mid if mid > 0 else 0.0

    microprice = 0.0
    denom = bid1_qty + ask1_qty
    if bid1 > 0 and ask1 > 0 and denom > 1e-12:
        microprice = (ask1 * bid1_qty + bid1 * ask1_qty) / denom
    else:
        microprice = mid

    total_depth = bid_notional + ask_notional
    book_imb = (bid_notional - ask_notional) / total_depth if total_depth > 1e-12 else 0.0
    depth_ratio = (bid_notional / ask_notional) if ask_notional > 1e-12 else 1.0

    return {
        "bid_notional": float(bid_notional),
        "ask_notional": float(ask_notional),
        "mid": float(mid),
        "spread": float(spread),
        "microprice": float(microprice),
        "book_imb": float(book_imb),
        "depth_ratio": float(depth_ratio),
        "bid1": float(bid1),
        "ask1": float(ask1),
        "bid1_qty": float(bid1_qty),
        "ask1_qty": float(ask1_qty),
    }

def _ms_update_microstructure_ring(self, sym: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """每次调用尽量产出一个新的 15s 微结构条目（按间隔节流），并存入 ring buffer。"""
    enabled = self._coerce_bool(params.get("microstructure_enabled", True), True)
    if not enabled:
        return None

    try:
        interval = int(params.get("microstructure_interval_seconds", 15) or 15)
    except Exception:
        interval = 15
    interval = max(5, min(60, interval))

    try:
        depth_levels = int(params.get("microstructure_depth_levels", params.get("flow_depth_levels", 20)) or 20)
    except Exception:
        depth_levels = 20
    depth_levels = max(5, min(100, depth_levels))

    try:
        ring_maxlen = int(params.get("microstructure_ring_maxlen", 60) or 60)  # 60*15s=15m
    except Exception:
        ring_maxlen = 60
    ring_maxlen = max(20, min(400, ring_maxlen))
    ring = self._ms_get_ring(sym, ring_maxlen)

    now_ts = time.time()
    st = self._ms_state.get(sym, {})
    last_ts = float(st.get("last_ts", 0.0) or 0.0) if isinstance(st, dict) else 0.0
    if last_ts > 0 and (now_ts - last_ts) < interval:
        return None

    # 1) 采 order book
    try:
        order_book = self.client.get_order_book(sym, limit=depth_levels)
    except Exception:
        order_book = None
    if not isinstance(order_book, dict):
        return None
    bm = self._ms_parse_book_metrics(order_book, depth_levels)

    # 2) 采 aggTrades（15s窗口）
    end_ms = int(now_ts * 1000)
    start_ms = int((last_ts * 1000) if last_ts > 0 else (end_ms - interval * 1000))
    start_ms = max(0, start_ms)

    trades = self._ms_fetch_agg_trades(sym, start_ms=start_ms, end_ms=end_ms, limit=1000)

    buy_quote = 0.0
    sell_quote = 0.0
    trade_count = 0
    last_trade_id = None

    # aggTrades: p=price, q=qty, m=buyerIsMaker (True -> sell taker)
    for t in trades:
        if not isinstance(t, dict):
            continue
        try:
            price = float(t.get("p", 0.0) or 0.0)
            qty = float(t.get("q", 0.0) or 0.0)
            if price <= 0 or qty <= 0:
                continue
            quote = price * qty
            buyer_is_maker = bool(t.get("m", False))
            if buyer_is_maker:
                sell_quote += quote
            else:
                buy_quote += quote
            trade_count += 1
            last_trade_id = t.get("a", last_trade_id)
        except Exception:
            continue

    total_quote = buy_quote + sell_quote
    trade_imb = (buy_quote - sell_quote) / total_quote if total_quote > 1e-12 else 0.0

    # 3) phantom（撤单代理）
    prev_bid = float(st.get("last_bid_notional", 0.0) or 0.0) if isinstance(st, dict) else 0.0
    prev_ask = float(st.get("last_ask_notional", 0.0) or 0.0) if isinstance(st, dict) else 0.0
    depth_delta_bid = bm["bid_notional"] - prev_bid if prev_bid > 0 else 0.0
    depth_delta_ask = bm["ask_notional"] - prev_ask if prev_ask > 0 else 0.0

    phantom_bid = max(0.0, -depth_delta_bid)  # bid 深度减少的量（名义）
    phantom_ask = max(0.0, -depth_delta_ask)
    # 用成交进行“解释”，剩余视为撤单/消失
    phantom_bid = max(0.0, phantom_bid - buy_quote)
    phantom_ask = max(0.0, phantom_ask - sell_quote)

    # 4) trap score / flag
    trap_flag, trap_reason, trap_score = self._ms_calc_trap(
        sym=sym,
        book_imb=bm["book_imb"],
        trade_imb=trade_imb,
        spread=bm["spread"],
        depth_delta_bid=depth_delta_bid,
        depth_delta_ask=depth_delta_ask,
        phantom_bid=phantom_bid,
        phantom_ask=phantom_ask,
        params=params,
    )

    entry = {
        "ts": float(now_ts),
        "interval": int(interval),
        "depth_levels": int(depth_levels),
        "bid_notional": float(bm["bid_notional"]),
        "ask_notional": float(bm["ask_notional"]),
        "depth_ratio": float(bm["depth_ratio"]),
        "book_imb": float(bm["book_imb"]),
        "mid": float(bm["mid"]),
        "spread": float(bm["spread"]),
        "microprice": float(bm["microprice"]),
        "buy_quote": float(buy_quote),
        "sell_quote": float(sell_quote),
        "trade_imb": float(trade_imb),
        "trade_count": int(trade_count),
        "depth_delta_bid": float(depth_delta_bid),
        "depth_delta_ask": float(depth_delta_ask),
        "phantom_bid": float(phantom_bid),
        "phantom_ask": float(phantom_ask),
        "trap_flag": bool(trap_flag),
        "trap_reason": str(trap_reason),
        "trap_score": float(trap_score),
    }
    ring.append(entry)

    self._ms_state[sym] = {
        "last_ts": float(now_ts),
        "last_bid_notional": float(bm["bid_notional"]),
        "last_ask_notional": float(bm["ask_notional"]),
        "last_trade_id": int(last_trade_id) if last_trade_id is not None and str(last_trade_id).isdigit() else st.get("last_trade_id"),
    }
    return entry


def _ms_adaptive_thresholds(self, sym: str, params: Dict[str, Any]) -> Dict[str, float]:
    """基于过去一段时间的 15s ring，自适应计算 spread_max / book_imb_strong 的分位数阈值。"""
    enabled = self._coerce_bool(params.get("ms_adaptive_enabled", True), True)
    if not enabled:
        return {}
    try:
        horizon = int(params.get("ms_adaptive_horizon_seconds", 1800) or 1800)  # 30min
    except Exception:
        horizon = 1800
    horizon = max(300, min(7200, horizon))

    try:
        q_spread = float(params.get("ms_adaptive_spread_q", 0.90) or 0.90)
    except Exception:
        q_spread = 0.90
    try:
        q_book = float(params.get("ms_adaptive_book_imb_q", 0.85) or 0.85)
    except Exception:
        q_book = 0.85
    q_spread = max(0.50, min(0.99, q_spread))
    q_book = max(0.50, min(0.99, q_book))

    ring = self._ms_ring.get(sym)
    if not isinstance(ring, deque) or not ring:
        return {}

    now_ts = time.time()
    spreads: List[float] = []
    book_abs: List[float] = []
    for e in reversed(ring):
        try:
            ts = float(e.get("ts", 0.0) or 0.0)
        except Exception:
            ts = 0.0
        if ts <= 0 or (now_ts - ts) > horizon:
            break
        try:
            sp = float(e.get("spread", 0.0) or 0.0)
            bi = float(e.get("book_imb", 0.0) or 0.0)
        except Exception:
            continue
        if sp >= 0:
            spreads.append(sp)
        book_abs.append(abs(bi))

    if len(spreads) < 8 or len(book_abs) < 8:
        return {}

    # 分位数：用 numpy（已有依赖）更稳健
    try:
        spread_q = float(np.quantile(np.array(spreads, dtype=float), q_spread))
        book_q = float(np.quantile(np.array(book_abs, dtype=float), q_book))
    except Exception:
        # 回退：排序取位点
        spreads_s = sorted(spreads)
        book_s = sorted(book_abs)
        spread_q = spreads_s[int((len(spreads_s) - 1) * q_spread)]
        book_q = book_s[int((len(book_s) - 1) * q_book)]

    # clamp：避免极端导致阈值过低/过高
    try:
        spread_min = float(params.get("ms_adaptive_spread_min", 0.0003) or 0.0003)
    except Exception:
        spread_min = 0.0003
    try:
        spread_max_cap = float(params.get("ms_adaptive_spread_max_cap", 0.0040) or 0.0040)
    except Exception:
        spread_max_cap = 0.0040
    try:
        book_min = float(params.get("ms_adaptive_book_imb_min", 0.20) or 0.20)
    except Exception:
        book_min = 0.20
    try:
        book_max_cap = float(params.get("ms_adaptive_book_imb_max_cap", 0.75) or 0.75)
    except Exception:
        book_max_cap = 0.75

    spread_q = self._clamp_value(spread_q, spread_min, spread_max_cap)
    book_q = self._clamp_value(book_q, book_min, book_max_cap)

    return {"spread_max": float(spread_q), "book_strong": float(book_q)}

def _ms_calc_trap(
    self,
    sym: str,
    book_imb: float,
    trade_imb: float,
    spread: float,
    depth_delta_bid: float,
    depth_delta_ask: float,
    phantom_bid: float,
    phantom_ask: float,
    params: Dict[str, Any],
) -> Tuple[bool, str, float]:
    """把 15s 的盘口/成交差异映射成 trap_flag/trap_reason/trap_score。"""
    # 阈值：优先使用分位数自适应（过去30min），否则用固定阈值
    adaptive = self._ms_adaptive_thresholds(sym, params)
    if adaptive:
        book_strong = float(adaptive.get("book_strong", 0.35) or 0.35)
        spread_max = float(adaptive.get("spread_max", params.get("ms_trap_spread_max", 0.0012) or 0.0012) or 0.0012)
    else:
        try:
            book_strong = float(params.get("ms_trap_book_imb_strong", 0.35) or 0.35)
        except Exception:
            book_strong = 0.35

    try:
        trade_weak = float(params.get("ms_trap_trade_imb_weak", 0.10) or 0.10)
    except Exception:
        trade_weak = 0.10
    try:
        spread_max = float(params.get("ms_trap_spread_max", 0.0012) or 0.0012)  # 0.12%
    except Exception:
        spread_max = 0.0012
    try:
        phantom_ratio_th = float(params.get("ms_trap_phantom_ratio", 0.60) or 0.60)
    except Exception:
        phantom_ratio_th = 0.60

    score = 0.0
    reasons: List[str] = []

    # 1) 盘口强但成交弱/反向（spoof / 诱导）
    if abs(book_imb) >= book_strong and (abs(trade_imb) <= trade_weak or (book_imb * trade_imb) < 0):
        score += 0.45
        reasons.append("book_strong_trade_weak")

    # 2) 深度回落 + 撤单代理显著
    # 用 phantom/(phantom + opposite_trade) 近似撤单占比
    denom_bid = phantom_bid + max(1.0, 0.0)  # 防0
    denom_ask = phantom_ask + max(1.0, 0.0)
    # 如果 bid 深度下降或 ask 深度下降且 phantom 大，说明“挂单消失”
    if depth_delta_bid < 0 and phantom_bid > 0:
        score += 0.25
        reasons.append("bid_vanish")
    if depth_delta_ask < 0 and phantom_ask > 0:
        score += 0.25
        reasons.append("ask_vanish")

    # 3) 点差异常扩大（流动性陷阱）
    if spread >= spread_max:
        score += 0.20
        reasons.append("spread_wide")

    score = self._clamp_value(score, 0.0, 1.0)
    flag = score >= float(params.get("ms_trap_score_threshold", 0.55) or 0.55)
    reason = "|".join(reasons) if reasons else "-"
    return bool(flag), reason, float(score)


def _ms_consecutive_trap(self, sym: str, required: int, params: Dict[str, Any]) -> Tuple[bool, int]:
    """判断最近 required 个 15s 条目是否连续触发 trap（用于 hard gate 连续性确认）。"""
    ring = self._ms_ring.get(sym)
    if not isinstance(ring, deque) or not ring:
        return False, 0
    try:
        interval = int(params.get("microstructure_interval_seconds", 15) or 15)
    except Exception:
        interval = 15
    # 允许一定的时间漂移：required * interval * 1.8
    now_ts = time.time()
    max_age = float(required * interval) * 1.8

    cnt = 0
    last_ts = None
    for e in reversed(ring):
        try:
            ts = float(e.get("ts", 0.0) or 0.0)
        except Exception:
            ts = 0.0
        if ts <= 0 or (now_ts - ts) > max_age:
            break
        if last_ts is not None and abs(last_ts - ts) > (interval * 2.2):
            # 间隔断开，视为不连续
            break
        last_ts = ts
        if bool(e.get("trap_flag", False)):
            cnt += 1
            if cnt >= required:
                return True, cnt
        else:
            break
    return False, cnt

def _ms_aggregate(self, sym: str, horizon_seconds: int, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ring = self._ms_ring.get(sym)
    if not isinstance(ring, deque) or not ring:
        return None
    now_ts = time.time()
    horizon_seconds = max(15, int(horizon_seconds))
    items = [x for x in list(ring) if isinstance(x, dict) and (now_ts - float(x.get("ts", 0.0) or 0.0)) <= horizon_seconds]
    if not items:
        return None

    # 成交不平衡用量加权
    buy_sum = sum(float(x.get("buy_quote", 0.0) or 0.0) for x in items)
    sell_sum = sum(float(x.get("sell_quote", 0.0) or 0.0) for x in items)
    total = buy_sum + sell_sum
    trade_imb = (buy_sum - sell_sum) / total if total > 1e-12 else 0.0

    # book_imb / microprice bias 用中位数抗尖刺
    try:
        book_imbs = [float(x.get("book_imb", 0.0) or 0.0) for x in items]
        micro_bias = []
        for x in items:
            mid = float(x.get("mid", 0.0) or 0.0)
            mp = float(x.get("microprice", 0.0) or 0.0)
            micro_bias.append((mp - mid) / mid if mid > 0 else 0.0)
        book_imb_med = float(pd.Series(book_imbs).median()) if book_imbs else 0.0
        micro_bias_med = float(pd.Series(micro_bias).median()) if micro_bias else 0.0
    except Exception:
        book_imb_med = 0.0
        micro_bias_med = 0.0

    spread_max = max(float(x.get("spread", 0.0) or 0.0) for x in items)

    phantom_bid_sum = sum(max(0.0, float(x.get("phantom_bid", 0.0) or 0.0)) for x in items)
    phantom_ask_sum = sum(max(0.0, float(x.get("phantom_ask", 0.0) or 0.0)) for x in items)

    # depth_ratio 用中位数
    try:
        depth_ratios = [float(x.get("depth_ratio", 1.0) or 1.0) for x in items]
        depth_ratio_med = float(pd.Series(depth_ratios).median()) if depth_ratios else 1.0
    except Exception:
        depth_ratio_med = 1.0

    # trap 聚合：用 max_score + 触发次数
    trap_score_max = max(float(x.get("trap_score", 0.0) or 0.0) for x in items)
    trap_hits = sum(1 for x in items if bool(x.get("trap_flag", False)))
    trap_flag = trap_score_max >= float(params.get("ms_trap_score_threshold", 0.55) or 0.55) or trap_hits >= max(2, int(len(items) * 0.4))

    # 汇总 reason（只保留最近一次触发）
    trap_reason = "-"
    for x in reversed(items):
        if bool(x.get("trap_flag", False)):
            trap_reason = str(x.get("trap_reason", "-") or "-")
            break

    return {
        "horizon": int(horizon_seconds),
        "trade_imb": float(trade_imb),
        "book_imb": float(book_imb_med),
        "spread_max": float(spread_max),
        "micro_bias": float(micro_bias_med),
        "depth_ratio": float(depth_ratio_med),
        "phantom_bid": float(phantom_bid_sum),
        "phantom_ask": float(phantom_ask_sum),
        "trap_score": float(trap_score_max),
        "trap_hits": int(trap_hits),
        "trap_flag": bool(trap_flag),
        "trap_reason": str(trap_reason),
    }

    def _dca_get_flow_signal_snapshot(self, symbol: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sym = str(symbol or "").upper()
        if not sym:
            return None

        try:
            ttl = int(params.get("flow_signal_interval_seconds", 15) or 15)
        except Exception:
            ttl = 15
        ttl = max(1, min(300, ttl))

        now_ts = time.time()
        current_cycle_id = int(getattr(self, "_dca_flow_cycle_id", 0) or 0)
        is_dual_engine_mode = self._is_dual_engine_mode()
        cache_item = self._dca_flow_signal_cache.get(sym)
        if isinstance(cache_item, dict):
            try:
                data = cache_item.get("data")
                cache_cycle_id = int(cache_item.get("cycle_id", -1) or -1)
                if is_dual_engine_mode:
                    # 双引擎模式按执行周期刷新：同一周期内可复用，跨周期强制重算
                    if cache_cycle_id == current_cycle_id and isinstance(data, dict):
                        return data
                else:
                    if (now_ts - float(cache_item.get("ts", 0.0) or 0.0)) <= ttl and isinstance(data, dict):
                        return data
            except Exception:
                pass

        timeframe = str(params.get("flow_signal_timeframe", "1m") or "1m").strip().lower()
        try:
            cvd_window_bars = int(params.get("flow_cvd_window_bars", 5) or 5)
        except Exception:
            cvd_window_bars = 5
        cvd_window_bars = max(3, min(60, cvd_window_bars))
        try:
            depth_levels = int(params.get("flow_depth_levels", 20) or 20)
        except Exception:
            depth_levels = 20
        depth_levels = max(5, min(100, depth_levels))
        try:
            oi_history_size = int(params.get("flow_oi_history_size", 240) or 240)
        except Exception:
            oi_history_size = 240
        oi_history_size = max(20, min(2000, oi_history_size))

        limit = max(cvd_window_bars * 2 + 5, 40)
        try:
            klines = self.client.get_klines(sym, timeframe, limit=limit)
        except Exception:
            klines = []
        if not klines:
            return None

        try:
            flow_df = pd.DataFrame(
                klines,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_volume",
                    "trades",
                    "taker_buy_base",
                    "taker_buy_quote",
                    "ignore",
                ],
            )
        except Exception:
            return None

        for col in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote"):
            flow_df[col] = pd.to_numeric(flow_df[col], errors="coerce")
        flow_df["quote_volume"] = flow_df["quote_volume"].fillna(flow_df["volume"] * flow_df["close"])
        flow_df["taker_buy_quote"] = flow_df["taker_buy_quote"].fillna(0.0)
        flow_df = flow_df.dropna(subset=["close", "volume", "quote_volume"])
        if len(flow_df) < max(cvd_window_bars + 2, 8):
            return None

        tail_cur = flow_df.tail(cvd_window_bars)
        quote_cur = float(tail_cur["quote_volume"].sum() or 0.0)
        if quote_cur <= 1e-9:
            return None
        taker_buy_cur = float(tail_cur["taker_buy_quote"].sum() or 0.0)
        cvd_cur = taker_buy_cur - max(0.0, quote_cur - taker_buy_cur)
        cvd_ratio = cvd_cur / quote_cur

        cvd_prev_ratio = 0.0
        if len(flow_df) >= cvd_window_bars * 2:
            tail_prev = flow_df.iloc[-(cvd_window_bars * 2): -cvd_window_bars]
            quote_prev = float(tail_prev["quote_volume"].sum() or 0.0)
            if quote_prev > 1e-9:
                taker_buy_prev = float(tail_prev["taker_buy_quote"].sum() or 0.0)
                cvd_prev = taker_buy_prev - max(0.0, quote_prev - taker_buy_prev)
                cvd_prev_ratio = cvd_prev / quote_prev
        cvd_momentum = cvd_ratio - cvd_prev_ratio

        oi_value = 0.0
        try:
            oi_value = float(self.client.get_open_interest(sym) or 0.0)
        except Exception:
            oi_value = 0.0
        prev_oi_item = self._dca_flow_prev_oi.get(sym, {})
        prev_oi = float(prev_oi_item.get("oi", 0.0) or 0.0) if isinstance(prev_oi_item, dict) else 0.0
        oi_delta = (oi_value - prev_oi) if prev_oi > 1e-9 else 0.0
        oi_delta_ratio = (oi_delta / prev_oi) if prev_oi > 1e-9 else 0.0
        self._dca_flow_prev_oi[sym] = {"oi": float(oi_value), "ts": float(now_ts)}

        oi_hist = self._dca_flow_oi_history.setdefault(sym, [])
        if oi_value > 0:
            oi_hist.append(float(oi_value))
            if len(oi_hist) > oi_history_size:
                del oi_hist[:-oi_history_size]
        oi_median = float(pd.Series(oi_hist).median()) if len(oi_hist) >= 8 else 0.0
        oi_bias_ratio = ((oi_value - oi_median) / oi_median) if oi_median > 1e-9 else 0.0

        funding_rate = 0.0
        try:
            funding_rate = float(self.client.get_funding_rate(sym) or 0.0)
        except Exception:
            funding_rate = 0.0

        bid_notional = 0.0
        ask_notional = 0.0
        depth_ratio = 1.0
        order_imbalance = 0.0
        try:
            order_book = self.client.get_order_book(sym, limit=depth_levels)
        except Exception:
            order_book = None
        if isinstance(order_book, dict):
            bids = order_book.get("bids", [])
            asks = order_book.get("asks", [])
            if isinstance(bids, list) and isinstance(asks, list):
                for item in bids[:depth_levels]:
                    try:
                        price = float(item[0])
                        qty = float(item[1])
                        if price > 0 and qty > 0:
                            bid_notional += price * qty
                    except Exception:
                        continue
                for item in asks[:depth_levels]:
                    try:
                        price = float(item[0])
                        qty = float(item[1])
                        if price > 0 and qty > 0:
                            ask_notional += price * qty
                    except Exception:
                        continue
        total_depth = bid_notional + ask_notional
        if ask_notional > 1e-12:
            depth_ratio = bid_notional / ask_notional

        if total_depth > 1e-12:
            order_imbalance = (bid_notional - ask_notional) / total_depth

        # ===== 微结构(15s)采集与 1m/5m 聚合 =====
        ms_entry = None
        ms_agg_1m = None
        ms_agg_5m = None
        try:
            ms_entry = self._ms_update_microstructure_ring(sym, params)
            ms_agg_1m = self._ms_aggregate(sym, 60, params)
            ms_agg_5m = self._ms_aggregate(sym, 300, params)
        except Exception:
            ms_entry = None
            ms_agg_1m = None
            ms_agg_5m = None

        # 根据引擎（RANGE/TREND）选择聚合窗口，并把更稳健的 book 指标喂给 flow_gate
        engine_hint = str(params.get("_flow_engine_hint", "") or "").strip().upper()
        ms_chosen = None
        if engine_hint.startswith("TREND"):
            ms_chosen = ms_agg_5m or ms_agg_1m
        elif engine_hint.startswith("RANGE"):
            ms_chosen = ms_agg_1m or ms_agg_5m
        else:
            ms_chosen = ms_agg_1m or ms_agg_5m

        ms_trap_flag = False
        ms_trap_reason = "-"
        ms_trap_score = 0.0
        if isinstance(ms_chosen, dict):
            # 用聚合后的 book_imb / depth_ratio 替换单次 order book（更抗尖刺）
            if self._coerce_bool(params.get("microstructure_override_depth_imbalance", True), True):
                try:
                    order_imbalance = float(ms_chosen.get("book_imb", order_imbalance) or order_imbalance)
                except Exception:
                    pass
                try:
                    depth_ratio = float(ms_chosen.get("depth_ratio", depth_ratio) or depth_ratio)
                except Exception:
                    pass

            ms_trap_flag = bool(ms_chosen.get("trap_flag", False))
            ms_trap_reason = str(ms_chosen.get("trap_reason", "-") or "-")
            try:
                ms_trap_score = float(ms_chosen.get("trap_score", 0.0) or 0.0)
            except Exception:
                ms_trap_score = 0.0


        threshold_cfg = params.get("flow_thresholds", {}) if isinstance(params.get("flow_thresholds", {}), dict) else {}
        try:
            cvd_th = float(threshold_cfg.get("cvd_ratio", 0.03) or 0.03)
        except Exception:
            cvd_th = 0.03
        try:
            cvd_mom_th = float(threshold_cfg.get("cvd_momentum", 0.015) or 0.015)
        except Exception:
            cvd_mom_th = 0.015
        try:
            oi_delta_th = float(threshold_cfg.get("oi_delta_ratio", 0.0025) or 0.0025)
        except Exception:
            oi_delta_th = 0.0025
        try:
            oi_level_th = float(threshold_cfg.get("oi_level_ratio", 0.01) or 0.01)
        except Exception:
            oi_level_th = 0.01
        try:
            funding_th = float(threshold_cfg.get("funding_rate", 0.0006) or 0.0006)
        except Exception:
            funding_th = 0.0006
        try:
            depth_th = float(threshold_cfg.get("depth_ratio", 1.05) or 1.05)
        except Exception:
            depth_th = 1.05
        try:
            imbalance_th = float(threshold_cfg.get("order_imbalance", 0.06) or 0.06)
        except Exception:
            imbalance_th = 0.06
        cvd_th = max(1e-6, abs(cvd_th))
        cvd_mom_th = max(1e-6, abs(cvd_mom_th))
        oi_delta_th = max(1e-6, abs(oi_delta_th))
        oi_level_th = max(1e-6, abs(oi_level_th))
        funding_th = max(1e-6, abs(funding_th))
        imbalance_th = max(1e-6, abs(imbalance_th))
        depth_th = max(1.001, depth_th)
        depth_scale = max(1e-6, depth_th - 1.0)
        depth_ratio_safe = max(depth_ratio, 1e-9)

        factor_short = {
            "cvd": self._clamp_value((-cvd_ratio) / cvd_th, 0.0, 1.0),
            "cvd_mom": self._clamp_value((-cvd_momentum) / cvd_mom_th, 0.0, 1.0),
            "oi_delta": self._clamp_value(oi_delta_ratio / oi_delta_th, 0.0, 1.0),
            "oi_level": self._clamp_value(abs(oi_bias_ratio) / oi_level_th, 0.0, 1.0),
            "funding": self._clamp_value(funding_rate / funding_th, 0.0, 1.0),
            "depth": self._clamp_value(((1.0 / depth_ratio_safe) - 1.0) / depth_scale, 0.0, 1.0),
            "imbalance": self._clamp_value((-order_imbalance) / imbalance_th, 0.0, 1.0),
        }
        factor_long = {
            "cvd": self._clamp_value(cvd_ratio / cvd_th, 0.0, 1.0),
            "cvd_mom": self._clamp_value(cvd_momentum / cvd_mom_th, 0.0, 1.0),
            "oi_delta": self._clamp_value(oi_delta_ratio / oi_delta_th, 0.0, 1.0),
            "oi_level": self._clamp_value(abs(oi_bias_ratio) / oi_level_th, 0.0, 1.0),
            "funding": self._clamp_value((-funding_rate) / funding_th, 0.0, 1.0),
            "depth": self._clamp_value((depth_ratio - 1.0) / depth_scale, 0.0, 1.0),
            "imbalance": self._clamp_value(order_imbalance / imbalance_th, 0.0, 1.0),
        }
        factor_weights = self._dca_normalize_flow_factor_weights(params.get("flow_factor_weights", {}))
        # Range / Trend 引擎可指定不同的资金流权重（更贴合：震荡看盘口/资金费率，趋势看CVD/OI扩张）
        engine_hint = str(params.get("_flow_engine_hint", "") or "").strip().upper()
        if engine_hint.startswith("RANGE"):
            w = params.get("flow_factor_weights_range", {})
            if isinstance(w, dict) and w:
                factor_weights = self._dca_normalize_flow_factor_weights(w)
        elif engine_hint.startswith("TREND"):
            w = params.get("flow_factor_weights_trend", {})
            if isinstance(w, dict) and w:
                factor_weights = self._dca_normalize_flow_factor_weights(w)
        
        # 优化5：资金流因子权重动态调整（根据市场状态）
        # 熊市做空时，增加CVD和Funding权重；牛市做多时，增加depth和imbalance权重
        try:
            btc_regime_raw = str(getattr(self, "_dca_btc_regime", "NEUTRAL") or "NEUTRAL").upper()
            regime_key = btc_regime_raw.split("_")[0] if "_" in btc_regime_raw else btc_regime_raw  # BULL_STRONG -> BULL
        except Exception:
            regime_key = "NEUTRAL"
        
        # 动态权重配置键名：flow_factor_weights_bull_short, flow_factor_weights_bear_short, etc.
        dynamic_weights_key = None
        if regime_key == "BEAR":
            dynamic_weights_key = "flow_factor_weights_bear"
        elif regime_key == "BULL":
            dynamic_weights_key = "flow_factor_weights_bull"
        
        if dynamic_weights_key:
            dynamic_weights = params.get(dynamic_weights_key, {})
            if isinstance(dynamic_weights, dict) and dynamic_weights:
                factor_weights = self._dca_normalize_flow_factor_weights(dynamic_weights)
        
        short_score = 0.0
        long_score = 0.0
        for key, weight in factor_weights.items():
            short_score += float(factor_short.get(key, 0.0)) * float(weight)
            long_score += float(factor_long.get(key, 0.0)) * float(weight)
        short_score = self._clamp_value(float(short_score), 0.0, 1.0)
        long_score = self._clamp_value(float(long_score), 0.0, 1.0)
        # 资金流方向一致性惩罚：避免“指标相互矛盾”的假信号（例如 OI↑ 但 CVD↓）
        try:
            consistency_enabled = self._coerce_bool(params.get("flow_consistency_enabled", True), True)
        except Exception:
            consistency_enabled = True
        long_consistency = 1.0
        short_consistency = 1.0
        long_mismatch: List[str] = []
        short_mismatch: List[str] = []
        if consistency_enabled:
            try:
                step = float(params.get("flow_consistency_penalty_step", 0.15) or 0.15)
            except Exception:
                step = 0.15
            step = self._clamp_value(step, 0.05, 0.30)
            try:
                floor = float(params.get("flow_consistency_floor", 0.40) or 0.40)
            except Exception:
                floor = 0.40
            floor = self._clamp_value(floor, 0.10, 0.90)

            # LONG 方向期望：CVD↑、CVD_mom↑、OI_delta↑、盘口 bid 强、imbalance>0
            if float(cvd_ratio) < 0:
                long_mismatch.append("cvd_ratio<0")
            if float(cvd_momentum) < 0:
                long_mismatch.append("cvd_mom<0")
            if float(oi_delta_ratio) < 0:
                long_mismatch.append("oi_delta<0")
            if float(order_imbalance) < 0:
                long_mismatch.append("imb<0")
            if float(depth_ratio) < 1.0:
                long_mismatch.append("depth<1")

            # SHORT 方向期望：CVD↓、CVD_mom↓、OI_delta↓、盘口 ask 强、imbalance<0
            if float(cvd_ratio) > 0:
                short_mismatch.append("cvd_ratio>0")
            if float(cvd_momentum) > 0:
                short_mismatch.append("cvd_mom>0")
            if float(oi_delta_ratio) > 0:
                short_mismatch.append("oi_delta>0")
            if float(order_imbalance) > 0:
                short_mismatch.append("imb>0")
            if float(depth_ratio) > 1.0:
                short_mismatch.append("depth>1")

            long_consistency = max(floor, 1.0 - step * float(len(long_mismatch)))
            short_consistency = max(floor, 1.0 - step * float(len(short_mismatch)))
            long_score = self._clamp_value(float(long_score) * float(long_consistency), 0.0, 1.0)
            short_score = self._clamp_value(float(short_score) * float(short_consistency), 0.0, 1.0)
        hit_threshold = self._clamp_value(float(params.get("flow_hit_threshold", 0.2) or 0.2), 0.05, 0.95)
        short_hits = sum(1 for v in factor_short.values() if float(v) >= hit_threshold)
        long_hits = sum(1 for v in factor_long.values() if float(v) >= hit_threshold)

        dominant_side = "NEUTRAL"
        if short_score > long_score + 0.03:
            dominant_side = "SHORT"
        elif long_score > short_score + 0.03:
            dominant_side = "LONG"
        signal_grade = self._dca_signal_grade_from_hits(max(short_hits, long_hits))

        snapshot = {
            "symbol": sym,
            "timestamp": now_ts,
            "timeframe": timeframe,
            "window_bars": int(cvd_window_bars),
            "factor_count": int(len(factor_short)),
            "cvd_ratio": float(cvd_ratio),
            "cvd_prev_ratio": float(cvd_prev_ratio),
            "cvd_momentum": float(cvd_momentum),
            "oi": float(oi_value),
            "oi_delta": float(oi_delta),
            "oi_delta_ratio": float(oi_delta_ratio),
            "oi_bias_ratio": float(oi_bias_ratio),
            "funding_rate": float(funding_rate),
            "depth_ratio": float(depth_ratio),
            "order_imbalance": float(order_imbalance),
            "bid_notional": float(bid_notional),
            "ask_notional": float(ask_notional),
            "thresholds": {
                "cvd_ratio": float(cvd_th),
                "cvd_momentum": float(cvd_mom_th),
                "oi_delta_ratio": float(oi_delta_th),
                "oi_level_ratio": float(oi_level_th),
                "funding_rate": float(funding_th),
                "depth_ratio": float(depth_th),
                "order_imbalance": float(imbalance_th),
                "hit_threshold": float(hit_threshold),
            },
            "factor_short": factor_short,
            "factor_long": factor_long,
            "short_hits": int(short_hits),
            "long_hits": int(long_hits),
            "short_fired": [k for k, v in factor_short.items() if float(v) >= hit_threshold],
            "long_fired": [k for k, v in factor_long.items() if float(v) >= hit_threshold],
            "short_score": float(short_score),
            "long_score": float(long_score),
            "dominant_side": dominant_side,
            "signal_grade": signal_grade,
            "signal_label": self._dca_signal_grade_label(signal_grade),
            "flow_engine_hint": engine_hint,
            "ms_entry": ms_entry if isinstance(ms_entry, dict) else None,
            "ms_agg_1m": ms_agg_1m if isinstance(ms_agg_1m, dict) else None,
            "ms_agg_5m": ms_agg_5m if isinstance(ms_agg_5m, dict) else None,
            "ms_trap_flag": bool(ms_trap_flag),
            "ms_trap_reason": str(ms_trap_reason),
            "ms_trap_score": float(ms_trap_score),
            "long_consistency": float(long_consistency),
            "short_consistency": float(short_consistency),
            "long_mismatch": long_mismatch,
            "short_mismatch": short_mismatch,
            "source": "live",
        }
        self._dca_flow_signal_cache[sym] = {
            "ts": now_ts,
            "data": snapshot,
            "cycle_id": current_cycle_id,
        }
        return snapshot

    def _dca_flow_gate_check(
        self,
        symbol: str,
        side: str,
        params: Dict[str, Any],
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        enabled = self._coerce_bool(params.get("flow_signal_enabled", False), False)
        if not enabled:
            return True, "flow_disabled", None

        # 真实执行层强制策略：资金流缺失即拦截（不允许 no-data 放行）
        snapshot = self._dca_get_flow_signal_snapshot(symbol, params)
        if not isinstance(snapshot, dict):
            return False, "资金流门禁阻断：无可用资金流数据", None


        # 微结构陷阱门禁：基于15s->1m/5m聚合的盘口/成交差异，防止追进“挂单诱导”
        try:
            trap_flag = bool(snapshot.get("ms_trap_flag", False))
        except Exception:
            trap_flag = False
        if trap_flag:
            trap_reason = str(snapshot.get("ms_trap_reason", "-") or "-")
            try:
                trap_score = float(snapshot.get("ms_trap_score", 0.0) or 0.0)
            except Exception:
                trap_score = 0.0
            hard_gate = self._coerce_bool(params.get("ms_trap_hard_gate", True), True)
            if hard_gate:
                # 连续性确认：需要最近 N 个 15s 连续触发才 hard gate（默认 2）
                try:
                    req_n = int(params.get("ms_trap_confirm_consecutive", 2) or 2)
                except Exception:
                    req_n = 2
                req_n = max(1, min(5, req_n))
                ok_consec, got_n = self._ms_consecutive_trap(symbol, req_n, params)
                if ok_consec:
                    return False, f"微结构陷阱阻断(连续{got_n}): score={trap_score:.2f}, reason={trap_reason}", snapshot
                # 不满足连续确认 -> soft
                trap_soft_note = f"微结构陷阱(未连续确认): score={trap_score:.2f}, reason={trap_reason}"

            # soft: 不阻断，但把提示写进 reason（后续会拼到 decision reason）
            trap_soft_note = f"微结构陷阱提示: score={trap_score:.2f}, reason={trap_reason}"
        else:
            trap_soft_note = ""


        side_up = str(side or "SHORT").upper()
        try:
            _v = params.get("flow_min_hits", 2)
            min_hits_common = int(2 if _v is None else _v)
        except Exception:
            min_hits_common = 2
        min_hits_common = max(1, min(7, min_hits_common))
        try:
            _v = params.get("flow_min_score", 0.35)
            min_score_common = float(0.35 if _v is None else _v)
        except Exception:
            min_score_common = 0.35
        min_score_common = self._clamp_value(min_score_common, 0.0, 1.0)

        if side_up == "LONG":
            try:
                _v = params.get("flow_min_hits_long", min_hits_common)
                min_hits = int(min_hits_common if _v is None else _v)
            except Exception:
                min_hits = min_hits_common
            try:
                _v = params.get("flow_min_score_long", min_score_common)
                min_score = float(min_score_common if _v is None else _v)
            except Exception:
                min_score = min_score_common
            side_hits = int(snapshot.get("long_hits", 0) or 0)
            side_score = float(snapshot.get("long_score", 0.0) or 0.0)
            fired = snapshot.get("long_fired", [])
        else:
            try:
                _v = params.get("flow_min_hits_short", min_hits_common)
                min_hits = int(min_hits_common if _v is None else _v)
            except Exception:
                min_hits = min_hits_common
            try:
                _v = params.get("flow_min_score_short", min_score_common)
                min_score = float(min_score_common if _v is None else _v)
            except Exception:
                min_score = min_score_common
            side_hits = int(snapshot.get("short_hits", 0) or 0)
            side_score = float(snapshot.get("short_score", 0.0) or 0.0)
            fired = snapshot.get("short_fired", [])

        min_hits = max(1, min(7, min_hits))
        min_score = self._clamp_value(min_score, 0.0, 1.0)
        if side_hits < min_hits or side_score < min_score:
            reason = (
                f"资金流门禁未通过({side_up}): score={side_score:.2f}<{min_score:.2f} "
                f"或 hits={side_hits}/7<{min_hits}; {self._dca_format_flow_brief(snapshot, side_up)})"
            )
            if trap_soft_note:
                reason = f"{reason} | {trap_soft_note}"
            return False, reason, snapshot

        fired_txt = ",".join(fired) if isinstance(fired, list) and fired else "-"
        reason = (
            f"资金流门禁通过({side_up}): score={side_score:.2f}, hits={side_hits}/7, factors={fired_txt}"
        )
        if trap_soft_note:
            reason = f"{reason} | {trap_soft_note}"
        
        # 优化6：资金流信号确认延迟（要求连续N个周期满足条件）
        try:
            flow_confirm_bars = int(params.get("flow_confirm_bars", 1) or 1)
        except Exception:
            flow_confirm_bars = 1
        flow_confirm_bars = max(1, min(5, flow_confirm_bars))
        
        if flow_confirm_bars > 1:
            # 检查连续满足条件的周期数
            confirm_key = f"{symbol}_{side_up}"
            confirm_cache = getattr(self, "_flow_confirm_cache", {})
            if not isinstance(confirm_cache, dict):
                confirm_cache = {}
                self._flow_confirm_cache = confirm_cache
            
            now_ts = time.time()
            cached = confirm_cache.get(confirm_key, {})
            cached_count = int(cached.get("count", 0) or 0)
            cached_ts = float(cached.get("ts", 0.0) or 0.0)
            
            # 如果距离上次检查超过300秒（一个执行周期），重置计数
            if (now_ts - cached_ts) > 300:
                cached_count = 0
            
            cached_count += 1
            self._flow_confirm_cache[confirm_key] = {"count": cached_count, "ts": now_ts, "score": side_score}
            
            if cached_count < flow_confirm_bars:
                reason = (
                    f"资金流门禁确认中({side_up}): {cached_count}/{flow_confirm_bars}周期, "
                    f"score={side_score:.2f}, hits={side_hits}/7"
                )
                return False, reason, snapshot
            
            reason = (
                f"资金流门禁确认通过({side_up}): 连续{cached_count}周期满足, "
                f"score={side_score:.2f}, hits={side_hits}/7, factors={fired_txt}"
            )
        
        return True, reason, snapshot

    def _dca_flow_candidate_gate(
        self,
        symbol: str,
        side: str,
        params: Dict[str, Any],
        *,
        stage: str = "CANDIDATE",
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        资金流候选门禁（用于"候选筛选/减少AI调用/提高胜率"）
        - 与执行层门禁解耦：候选阶段可以选择 soft/hard gate
        - flow_only_gate_mode 下：候选必须通过资金流，否则不入 open_candidates_raw
        """
        enabled = self._coerce_bool(params.get("flow_signal_enabled", False), False)
        if not enabled:
            return True, f"{stage}:flow_disabled", None

        # 默认：候选阶段不强制；通过 flow_candidate_hard_gate 控制
        cand_hard_gate = self._coerce_bool(params.get("flow_candidate_hard_gate", False), False)
                # 候选阶段可指定更宽松的资金流模板（默认 normal），避免用执行层 strict 阈值导致过度过滤
        cand_profile = str(params.get("flow_candidate_profile", "normal") or "normal").strip().lower()
        local_params = params
        try:
            current_profile = str(params.get("flow_profile", "") or "").strip().lower()
        except Exception:
            current_profile = ""
        if cand_profile and cand_profile != current_profile:
            local_params = dict(params)
            self._dca_apply_flow_profile_override(local_params, cand_profile)

        ok, reason, snapshot = self._dca_flow_gate_check(symbol=symbol, side=side, params=local_params)

        if ok:
            return True, f"{stage}:{reason}", snapshot

        # 候选阶段 soft gate：允许入池，但把原因写进 reason，后续可用作排序/复盘
        if not cand_hard_gate:
            return True, f"{stage}:SOFT_ALLOW|{reason}", snapshot
        return False, f"{stage}:HARD_BLOCK|{reason}", snapshot

def _dca_apply_flow_profile_override(self, params: Dict[str, Any], profile_name: str) -> None:
    """在不修改全局配置的前提下，把 flow_profiles[profile_name] 覆盖合并到 params（用于候选阶段/执行阶段的不同强度）。"""
    if not isinstance(params, dict):
        return
    name = str(profile_name or "").strip().lower()
    if not name:
        return
    profiles = params.get("flow_profiles") or {}
    if not isinstance(profiles, dict):
        return
    profile_cfg = profiles.get(name)
    if not isinstance(profile_cfg, dict):
        return
    for key, value in profile_cfg.items():
        if key in ("flow_thresholds", "flow_factor_weights") and isinstance(value, dict):
            base_cfg = params.get(key, {})
            merged = dict(base_cfg) if isinstance(base_cfg, dict) else {}
            merged.update(value)
            params[key] = merged
        else:
            params[key] = value

    def _dca_get_live_funding_rate(self, symbol: str, params: Dict[str, Any]) -> Optional[float]:
        if not bool(params.get("edge_use_live_funding", False)):
            return None
        ttl = max(10, int(params.get("edge_funding_cache_seconds", 120) or 120))
        now_ts = time.time()
        cache_item = self._dca_live_funding_cache.get(symbol)
        if cache_item:
            try:
                if (now_ts - float(cache_item.get("ts", 0.0))) <= float(ttl):
                    return float(cache_item.get("rate", 0.0))
            except Exception:
                pass
        try:
            rate = float(self.client.get_funding_rate(symbol) or 0.0)
            self._dca_live_funding_cache[symbol] = {"rate": rate, "ts": now_ts}
            return rate
        except Exception:
            return None

    def _dca_estimate_costs(
        self,
        symbol: str,
        side: str,
        params: Dict[str, Any],
    ) -> Tuple[float, float, float, float, float]:
        fee_cost = float(params.get("round_trip_fee_pct", params.get("fee_pct", 0.0008)) or 0.0008)
        slippage_cost = float(
            params.get("round_trip_slippage_pct", params.get("slippage_pct", 0.0006)) or 0.0006
        )
        hold_days = float(params.get("max_hold_days", 1.0) or 1.0)
        funding_cycles = float(params.get("edge_funding_cycles", max(1.0, hold_days * 24.0 / 8.0)) or 1.0)
        funding_cycles = self._clamp_value(funding_cycles, 0.5, 12.0)

        live_rate = self._dca_get_live_funding_rate(symbol, params)
        funding_rate = live_rate
        if funding_rate is None:
            funding_rate = float(params.get("funding_rate_estimate", 0.0001) or 0.0001)

        if bool(params.get("edge_funding_abs_cost", True)):
            funding_cost = abs(funding_rate) * funding_cycles
        else:
            side_up = str(side or "SHORT").upper()
            if side_up == "SHORT":
                funding_cost = max(0.0, -funding_rate) * funding_cycles
            else:
                funding_cost = max(0.0, funding_rate) * funding_cycles

        total_cost = max(0.0, fee_cost + slippage_cost + funding_cost)
        cost_ref = float(
            params.get(
                "edge_cost_ref_pct",
                fee_cost + slippage_cost + abs(float(params.get("funding_rate_estimate", 0.0001) or 0.0001)),
            )
            or 0.002
        )
        cost_ref = max(cost_ref, 1e-6)
        cost_z = (total_cost - cost_ref) / cost_ref
        cost_z = self._clamp_value(float(cost_z), -3.0, 3.0)
        return fee_cost, funding_cost, slippage_cost, total_cost, cost_z

    def _dca_dynamic_threshold(
        self,
        base_threshold: float,
        regime: str,
        side: str,
        row: pd.Series,
        params: Dict[str, Any],
        cost_z: float,
    ) -> Tuple[float, float, float]:
        base = self._clamp_value(float(base_threshold), 0.01, 0.95)
        volatility = float(row.get("volatility_24h", 0.0) or 0.0)
        vol_ref = max(1e-6, float(params.get("dynamic_threshold_vol_ref", 0.03) or 0.03))
        vol_scale = max(1e-6, float(params.get("dynamic_threshold_vol_scale", vol_ref * 0.5) or (vol_ref * 0.5)))
        volatility_z = self._clamp_value((volatility - vol_ref) / vol_scale, -3.0, 3.0)

        ema_fast = float(row.get("ema_fast_20", row.get("close", 0.0)) or 0.0)
        ema_slow = float(row.get("ema_slow_50", row.get("close", 0.0)) or 0.0)
        trend_raw = (ema_fast - ema_slow) / max(abs(ema_slow), 1e-9)
        trend_ref = max(1e-6, float(params.get("dynamic_threshold_trend_ref", 0.004) or 0.004))
        side_sign = 1.0 if str(side or "SHORT").upper() == "SHORT" else -1.0
        trend_component = side_sign * trend_raw / trend_ref
        regime_bias = 0.0
        if regime == "BULL":
            regime_bias = 1.0
        elif regime == "BEAR":
            regime_bias = -1.0
        if side_sign < 0:
            regime_bias = -regime_bias
        trend_z = self._clamp_value(0.7 * trend_component + 0.3 * regime_bias, -3.0, 3.0)

        coef_a = float(params.get("dynamic_threshold_a", 0.015) or 0.015)
        coef_b = float(params.get("dynamic_threshold_b", 0.020) or 0.020)
        coef_c = float(params.get("dynamic_threshold_c", 0.010) or 0.010)
        threshold = base + coef_a * volatility_z + coef_b * trend_z + coef_c * cost_z
        band = max(0.0, float(params.get("dynamic_threshold_band", 0.08) or 0.08))
        threshold = self._clamp_value(threshold, max(0.01, base - band), min(0.95, base + band))
        threshold = self._clamp_value(threshold, 0.01, 0.95)
        return threshold, volatility_z, trend_z

    def _dca_ai_gate_enabled(self) -> bool:
        ai_cfg = self.config.get("ai", {})
        return bool(
            ai_cfg.get("enabled", False)
            and ai_cfg.get("dca_gate", False)
            and self.ai_client is not None
            and self.prompt_builder is not None
            and self.decision_parser is not None
        )

    def _dca_ai_min_confidence(self) -> float:
        ai_cfg = self.config.get("ai", {})
        return float(ai_cfg.get("dca_min_confidence", ai_cfg.get("min_confidence", 0.4)))

    def _dca_ai_fail_policy(self) -> str:
        ai_cfg = self.config.get("ai", {})
        return str(ai_cfg.get("dca_fail_policy", "ALLOW")).upper()

    def _dca_get_cooldown_seconds(self, params: Dict[str, Any]) -> int:
        """获取冷却秒数；<=0 视为禁用冷却。"""
        try:
            cooldown_seconds = int(params.get("cooldown_seconds", 60))
        except Exception:
            cooldown_seconds = 60
        return max(0, cooldown_seconds)

    def _dca_get_total_stop_loss_cooldown_seconds(self, params: Dict[str, Any]) -> int:
        """获取总回撤止损触发后的冷却秒数；默认 4 小时。"""
        try:
            cooldown_seconds = int(params.get("total_stop_loss_cooldown_seconds", 4 * 3600))
        except Exception:
            cooldown_seconds = 4 * 3600
        return max(0, cooldown_seconds)

    def _is_dca_cooldown_active(self, params: Dict[str, Any]) -> bool:
        """判断当前是否处于冷却中。"""
        if self.dca_cooldown_expires is None:
            return False

        cooldown_reason = str(self.dca_cooldown_reason or "").strip().lower()
        if cooldown_reason == "total_stop_loss":
            cooldown_enabled = self._dca_get_total_stop_loss_cooldown_seconds(params) > 0
        else:
            cooldown_enabled = self._dca_get_cooldown_seconds(params) > 0
        if not cooldown_enabled:
            # 对应冷却被禁用时，清理历史冷却状态，避免误阻止开仓
            self.dca_cooldown_expires = None
            self.dca_cooldown_reason = None
            return False

        now_ts = datetime.now()
        try:
            if now_ts < self.dca_cooldown_expires:
                expires_in = int((self.dca_cooldown_expires - now_ts).total_seconds())
                reason = f"（原因: {self.dca_cooldown_reason})" if self.dca_cooldown_reason else ""
                print(f"⏳ 由于风险保护，冷却中，{expires_in}s 后恢复新开仓 {reason}")
                return True
        except Exception:
            pass

        # 冷却过期或时间异常，清理状态
        self.dca_cooldown_expires = None
        self.dca_cooldown_reason = None
        self.consecutive_losses = 0
        self.consecutive_stopouts = 0
        return False

    def _dca_ai_decide_open(
        self,
        candidates: List[Tuple[str, float, float, str]],
    ) -> Tuple[Optional[Tuple[str, float, float, str]], bool, Dict[str, Dict[str, Any]]]:
        ai_cfg = self.config.get("ai", {})
        if not self._dca_ai_gate_enabled() or not bool(ai_cfg.get("dca_open_gate", True)):
            return None, False, {}

        # 静态类型检查友好：再次检查 AI 组件是否存在，避免 Pylance 报错
        if self.prompt_builder is None or self.ai_client is None or self.decision_parser is None:
            return None, False, {}

        top_n = max(1, int(ai_cfg.get("dca_top_n", 4)))
        sorted_candidates = sorted(candidates, key=lambda x: x[1], reverse=True)[:top_n]

        # 准备批量请求 AI：构建多币种数据（只包含候选币种 + 当前持仓信息）
        all_symbols_data: Dict[str, Any] = {}
        for symbol, score, price, side in sorted_candidates:
            market_data = self.get_market_data_for_symbol(symbol)
            position = self.position_data.get_current_position(symbol)
            all_symbols_data[symbol] = {"market_data": market_data, "position": position}

        # 调用 AI（一次性）并解析多币种响应
        pb = self.prompt_builder
        ai = self.ai_client
        dp = self.decision_parser
        if pb is None or ai is None or dp is None:
            multi_decisions = {}
        else:
            try:
                prompt = pb.build_multi_symbol_analysis_prompt(
                    all_symbols_data=all_symbols_data,
                    all_positions=self.position_data.get_all_positions(),
                    account_summary=self.account_data.get_account_summary(),
                    history=self.decision_history,
                )
                resp = ai.analyze_and_decide(prompt)
                multi_decisions = dp.parse_multi_symbol_response(resp.get("content", ""))
            except Exception as e:
                print(f"❌ AI批量分析失败: {e}")
                multi_decisions = {}

        # 按 DCA 评分优先选择：允许 AI 为候选币种返回 HOLD / OPEN / CLOSE
        selected = []
        for symbol, score, price, side in sorted_candidates:
            dec = multi_decisions.get(symbol, {})
            action = dec.get("action", "HOLD")
            confidence = dec.get("confidence", 0.0)
            # 把字符串形式的 confidence 转为数字（兼容旧格式）
            if isinstance(confidence, str):
                conf_str = confidence.upper()
                if conf_str == "HIGH":
                    confidence = 0.8
                elif conf_str == "MEDIUM":
                    confidence = 0.6
                elif conf_str == "LOW":
                    confidence = 0.4
                else:
                    try:
                        confidence = float(confidence)
                    except Exception:
                        confidence = 0.5

            if side == "SHORT" and action == "SELL_OPEN":
                selected.append((symbol, score, price, side))
            if side == "LONG" and action == "BUY_OPEN":
                selected.append((symbol, score, price, side))

        # 从 AI 筛选结果中取前 K 个（默认 2）作为最终可下单目标
        max_choose = int(ai_cfg.get("dca_select_top_k", 2))
        if selected:
            chosen = selected[:max_choose]
            # 返回第一个被选中的作为优先开仓目标，同时返回整个 multi_decisions 以便后续使用
            return chosen[0], True, multi_decisions

        # 若 AI 未选中任何目标，返回 None 并标记为已使用（表示 AI 已评估但没有推荐开仓）
        return None, True, multi_decisions

    def _dca_ai_should_close(self, symbol: str) -> Optional[bool]:
        ai_cfg = self.config.get("ai", {})
        if not self._dca_ai_gate_enabled() or not bool(ai_cfg.get("dca_close_gate", True)):
            return None

        min_conf = self._dca_ai_min_confidence()
        fail_policy = self._dca_ai_fail_policy()
        market_data = self.get_market_data_for_symbol(symbol)
        decision = self.analyze_with_ai(symbol, market_data)
        action = decision.get("action", "HOLD")
        confidence = decision.get("confidence", 0.0)
        if isinstance(confidence, str):
            conf_str = confidence.upper()
            if conf_str == "HIGH":
                confidence = 0.8
            elif conf_str == "MEDIUM":
                confidence = 0.6
            elif conf_str == "LOW":
                confidence = 0.4
            else:
                confidence = 0.5

        if action == "CLOSE" and confidence >= min_conf:
            return True

        if fail_policy == "ALLOW":
            return True
        return False

    def _dca_cycle_handle_config_update(self, update_info: Dict[str, Any]) -> None:
        if not update_info.get("updated", False):
            return
        print("\n🔔 双引擎配置更新，已重新加载")
        # 配置变更后清空旧的 5m 开仓计划缓存，避免按过期计划执行
        self._dca_open_plan_cache = []
        self._dca_open_plan_cache_bucket = None
        self._dca_open_plan_cache_created_at = None
        if update_info.get("symbols_changed", False):
            removed = update_info.get("removed_symbols", []) or []
            added = update_info.get("added_symbols", []) or []
            if removed:
                print("\n⚠️  交易对已变更，正在平仓旧交易对...")
                self.close_positions_for_symbols(removed)
                for symbol in removed:
                    self.dca_state.pop(symbol, None)
            if added:
                self._preload_dca_symbols(added)

    def _dca_cycle_handle_empty_symbols(self, symbols: List[str]) -> bool:
        # 【优化：严格过滤模式】如果没有符合条件的交易对，跳过本周期
        if symbols:
            return False
        print("⏭️  无符合条件的交易对（成交量不足/信号不明确），跳过本周期")
        print("   → 等待：高波动时段 或 成交量放大 或 趋势明确")
        # 仍然检查并更新现有持仓（止盈止损）
        positions = self.position_data.get_all_positions()
        if positions:
            print(f"   → 注意：仍有{len(positions)}个持仓，继续监控止盈止损")
        self._save_dca_state()
        self._refresh_last_positions_snapshot(positions)
        return True

    def _run_dca_rotation_cycle(self) -> None:
        """双引擎循环：震荡套利 + 趋势跟随（AI可选）。"""
        self._dca_flow_cycle_id = int(getattr(self, "_dca_flow_cycle_id", 0) or 0) + 1
        update_info = self._reload_dca_config_if_changed()
        self._dca_cycle_handle_config_update(update_info)

        # 动态候选池（用于新开仓筛选）
        symbols = self._get_dca_symbols()
        # 配置池（用于 unknown 判定和订单对账，不受动态筛选影响）
        configured_symbols = self._get_dca_config_symbols()
        configured_symbols_set = set(configured_symbols)
        
        if self._dca_cycle_handle_empty_symbols(symbols):
            return
        
        interval = self.dca_config.get("interval", "5m")
        params = self.dca_config.get("params", {})
        direction_refresh_cycle = bool(getattr(self, "_dual_engine_refresh_direction_this_cycle", True))
        if self._is_dual_engine_mode():
            if direction_refresh_cycle:
                print("🧭 双引擎方向刷新：更新方向状态并执行开平仓")
            else:
                print("⏱️ 双引擎执行盯盘：沿用上次方向，仅做1m执行与风控")
        strategy_cfg = self.config.get("strategy", {}) if isinstance(self.config, dict) else {}
        strategy_dca_enabled = self._coerce_bool(
            strategy_cfg.get("dca_enabled", False) if isinstance(strategy_cfg, dict) else False,
            default=False,
        )
        params_dca_enabled = self._coerce_bool(params.get("dca_enabled", strategy_dca_enabled), default=strategy_dca_enabled)
        dca_add_enabled = params_dca_enabled and (not self._is_dual_engine_mode())
        if not dca_add_enabled:
            try:
                max_dca_cfg = int(params.get("max_dca", 0) or 0)
            except Exception:
                max_dca_cfg = 0
            if max_dca_cfg > 0:
                print(f"ℹ️ 已禁用DCA加仓（mode={self.strategy_mode}, dca_enabled={params_dca_enabled}），忽略 max_dca={max_dca_cfg}")
        direction = str(params.get("direction", "SHORT")).upper()
        score_threshold = float(params.get("score_threshold", 0.12))
        score_threshold_long = float(params.get("score_threshold_long", score_threshold))
        score_threshold_short = float(params.get("score_threshold_short", score_threshold))
        rsi_entry_short = float(params.get("rsi_entry_short", params.get("rsi_entry", 70)))
        rsi_entry_long = float(params.get("rsi_entry_long", 100 - rsi_entry_short))

        # 使用配置中的最大持仓数（默认2）
        try:
            MAX_POSITIONS = int(params.get("max_positions", 2))
        except Exception:
            MAX_POSITIONS = 2
        MAX_POSITIONS = max(1, min(10, MAX_POSITIONS))

        account_summary = self.account_data.get_account_summary() or {}
        equity = float(account_summary.get("equity", 0))
        if equity <= 0:
            print("⚠️  无法获取账户权益，跳过本轮")
            self._refresh_last_positions_snapshot()
            return

        # 设置当天开盘权益（用于当天亏损判定）。
        # 要求：仅在收到交易日开盘后的首个账户快照时设置（优先使用 account_summary.update_time 字段与配置时区日界点比较）。
        try:
            default_tz = "Asia/Shanghai"
            shanghai_tz = ZoneInfo(default_tz)
            today_str = datetime.now(shanghai_tz).date().isoformat()
            # 若日期变化，重置当天开盘权益等待首个快照
            if self.dca_day_open_date != today_str:
                self.dca_day_open_equity = None
                self.dca_day_open_date = today_str

            # 如果尚未设置当天开盘权益，尝试基于 account_summary.update_time 判断是否为开盘后的快照
            if self.dca_day_open_equity is None:
                update_time_ms = 0
                try:
                    update_time_ms = int(account_summary.get("update_time", 0) or 0)
                except Exception:
                    try:
                        update_time_ms = int(account_summary.get("updateTime", 0) or 0)
                    except Exception:
                        update_time_ms = 0

                # 支持可配置的日界点时区（接受 IANA TZ 名称 day_open_tz），默认 Asia/Shanghai
                day_open_tz = str(params.get("day_open_tz", default_tz) or default_tz)
                try:
                    day_open_grace_seconds = int(params.get("day_open_grace_seconds", 300))
                except Exception:
                    day_open_grace_seconds = 300

                # 计算指定时区当天 00:00 的毫秒时间戳
                start_ms = 0
                start_of_day_tz = None
                now_in_tz = None
                try:
                    try:
                        tz = ZoneInfo(day_open_tz)
                    except Exception:
                        print(f"⚠️ 无效时区 day_open_tz={day_open_tz}，回退到 {default_tz}")
                        day_open_tz = default_tz
                        tz = ZoneInfo(default_tz)
                    now_in_tz = datetime.now(tz)
                    start_of_day_tz = now_in_tz.replace(hour=0, minute=0, second=0, microsecond=0)
                    start_ms = int(start_of_day_tz.timestamp() * 1000)
                except Exception:
                    start_ms = 0

                # 若 update_time 可用且位于日界点之后，则采用该快照作为当天开盘权益
                if update_time_ms and start_ms and update_time_ms >= start_ms:
                    self.dca_day_open_equity = equity
                    self.dca_day_open_tz = day_open_tz
                    print(f"ⓘ 设置当天开盘权益（来自账户快照，update_time={update_time_ms}，tz={day_open_tz}）: {equity}")
                else:
                    # 降级策略：当 update_time 不可用时，仅在当前本地时间超过日界点+宽限才允许降级设置
                    if update_time_ms == 0:
                        try:
                            if now_in_tz is None:
                                now_in_tz = datetime.now(ZoneInfo(day_open_tz))
                            if start_of_day_tz is None:
                                start_of_day_tz = now_in_tz.replace(hour=0, minute=0, second=0, microsecond=0)
                            if now_in_tz >= (start_of_day_tz + timedelta(seconds=day_open_grace_seconds)):
                                self.dca_day_open_equity = equity
                                self.dca_day_open_tz = day_open_tz
                                print(f"ⓘ 设置当天开盘权益（降级且满足宽限 {day_open_grace_seconds}s，tz={day_open_tz}）: {equity}")
                            else:
                                print(f"ⓘ 暂不设置当天开盘权益（等待首个开盘后快照或宽限期 {day_open_grace_seconds}s，tz={day_open_tz}）")
                        except Exception:
                            # 出错时保守降级设置
                            self.dca_day_open_equity = equity
                            self.dca_day_open_tz = day_open_tz
                            print(f"ⓘ 设置当天开盘权益（降级，遇到异常，tz={day_open_tz}）: {equity}")

            # 仍保留历史会话初始权益用于其他用途
            if self.dca_initial_equity is None:
                self.dca_initial_equity = equity
            self.dca_peak_equity = max(self.dca_peak_equity or equity, equity)
        except Exception:
            # 回退到原始行为
            if self.dca_initial_equity is None:
                self.dca_initial_equity = equity
                self.dca_peak_equity = equity

        if self.dca_peak_equity is not None:
            self.dca_peak_equity = max(self.dca_peak_equity, equity)

        positions = self.position_data.get_all_positions()
        self._detect_external_closes_and_cleanup(positions, params)
        self._reconcile_open_orders(positions, configured_symbols_set, params)
        # 每日/总投入止损阈值（默认为 10%）。可以在 config/trading_config_vps.json 中通过
        # "total_stop_loss_pct" 覆盖（值为小数，0.10 表示 10%）。
        total_stop_loss_pct = float(params.get("total_stop_loss_pct", 0.10))
        total_stop_loss_cooldown_seconds = self._dca_get_total_stop_loss_cooldown_seconds(params)
        if self.dca_peak_equity and total_stop_loss_pct > 0:
            drawdown = (self.dca_peak_equity - equity) / self.dca_peak_equity
            if drawdown >= total_stop_loss_pct:
                drawdown_pct = drawdown * 100
                threshold_pct = total_stop_loss_pct * 100
                peak_equity = float(self.dca_peak_equity)
                print(
                    "⚠️ 触发总投入止损："
                    f"peak={peak_equity:.4f}, equity={equity:.4f}, "
                    f"drawdown={drawdown_pct:.2f}% >= threshold={threshold_pct:.2f}%"
                )
                self.trade_executor.close_all_positions()
                now_ts = datetime.now()
                if total_stop_loss_cooldown_seconds > 0:
                    self.dca_cooldown_expires = now_ts + timedelta(seconds=total_stop_loss_cooldown_seconds)
                    self.dca_cooldown_reason = "total_stop_loss"
                    print(
                        "⏳ 总回撤止损后进入冷却："
                        f"{total_stop_loss_cooldown_seconds}s，恢复时间 {self.dca_cooldown_expires.isoformat()}"
                    )
                else:
                    self.dca_cooldown_expires = None
                    self.dca_cooldown_reason = None
                    print("ⓘ total_stop_loss_cooldown_seconds<=0，跳过冷却，下一轮可直接尝试新开仓")
                # 重置峰值，避免冷却结束后因旧峰值持续超阈值而重复触发
                self.dca_peak_equity = equity
                # 风险事件后清空5m开仓计划缓存，避免按过期计划再次开仓
                self._dca_open_plan_cache = []
                self._dca_open_plan_cache_bucket = None
                self._dca_open_plan_cache_created_at = None
                # 兼容旧状态字段，确保不会被历史永久停机逻辑拦截
                self.dca_halt = False
                self._save_dca_state()
                self._write_dca_dashboard(
                    {},
                    event={
                        "timestamp": now_ts.isoformat(),
                        "type": "RISK_TOTAL_STOP",
                        "reason": "total_stop_loss",
                        "peak_equity": round(peak_equity, 8),
                        "equity": round(float(equity), 8),
                        "drawdown_pct": round(drawdown_pct, 4),
                        "threshold_pct": round(threshold_pct, 4),
                        "cooldown_seconds": int(total_stop_loss_cooldown_seconds),
                        "cooldown_expires": (
                            self.dca_cooldown_expires.isoformat()
                            if isinstance(self.dca_cooldown_expires, datetime)
                            else None
                        ),
                    },
                )
                self._refresh_last_positions_snapshot({})
                return

        if self.dca_halt:
            # 历史兼容：旧版本可能遗留 dca_halt=True，新版自动清理并继续。
            print("⚠️ 检测到遗留 dca_halt=True，已自动清理并继续执行")
            self.dca_halt = False
            self._save_dca_state()

        # 更新持仓：止盈/止损/时间止损/DCA加仓
        force_close_unknown = bool(self.dca_config.get("force_close_unknown_symbols", False))
        force_close_non_short = bool(self.dca_config.get("force_close_non_short", False))
        unknown_symbols = [
            s for s in positions.keys() if self._normalize_dca_symbol(s) not in configured_symbols_set
        ]
        if unknown_symbols:
            print(f"⚠️  发现非配置交易对持仓: {', '.join(unknown_symbols)}")
            if force_close_unknown:
                self.close_positions_for_symbols(unknown_symbols)
                for s in unknown_symbols:
                    positions.pop(s, None)

        if force_close_non_short:
            if direction == "BOTH":
                allowed_sides = {"LONG", "SHORT"}
            else:
                allowed_sides = {direction}
            non_short = [s for s, p in positions.items() if p.get("side") not in allowed_sides]
            if non_short:
                print(f"⚠️  发现非做空持仓: {', '.join(non_short)}")
                self.close_positions_for_symbols(non_short)
                for s in non_short:
                    positions.pop(s, None)
        self._reconcile_dca_state(positions)
        now = datetime.now()
        bar_minutes = 5 if interval.endswith("m") and interval[:-1].isdigit() else 5
        if interval.endswith("m"):
            bar_minutes = int(interval[:-1])

        close_candidates: List[Tuple[str, str]] = []

        # 获取当前 BTC 牛熊状态（用于持仓评估）
        btc_regime, _btc_score, _btc_details = self._dca_detect_btc_regime(params)

        # 【状态机 regime】用于止盈止损调整
        regime_sm_enabled_local = bool(params.get("regime_state_machine", {}).get("enabled", True))
        if regime_sm_enabled_local and hasattr(self, "_regime_sm_ctx"):
            sm_regime = self._regime_sm_ctx.get("regime", "RANGE")
        else:
            # 退化：使用 BTC regime 或震荡判断
            sm_regime = "RANGE" if btc_regime == "NEUTRAL" else btc_regime
        cycle_engine = self._map_regime_to_engine(sm_regime)
        cycle_trade_engine = self._resolve_dual_engine(cycle_engine)
        risk_cfg_local = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
        if not isinstance(risk_cfg_local, dict):
            risk_cfg_local = {}
        risk_osc_exit = risk_cfg_local.get("oscillation", {})
        risk_osc_exit = (
            risk_osc_exit.get("exit", {})
            if isinstance(risk_osc_exit, dict) and isinstance(risk_osc_exit.get("exit", {}), dict)
            else {}
        )
        risk_trend_exit = risk_cfg_local.get("trend", {})
        risk_trend_exit = (
            risk_trend_exit.get("exit", {})
            if isinstance(risk_trend_exit, dict) and isinstance(risk_trend_exit.get("exit", {}), dict)
            else {}
        )

        # 持仓管理应覆盖所有当前持仓，避免持仓因本轮未入候选池而失管
        for symbol in list(positions.keys()):
            pos = positions.get(symbol)
            if not pos:
                continue
            if direction != "BOTH" and pos.get("side") != direction:
                continue

            realtime = self.market_data.get_realtime_market_data(symbol)
            current_price = realtime.get("price", 0) if realtime else 0
            if current_price <= 0:
                continue

            entry_price = float(pos.get("entry_price", 0))
            # 确保 state 字段齐全
            pos_side = str(pos.get("side", "SHORT")).upper()
            state = self._ensure_dca_state(
                symbol,
                entry_price,
                now,
                side=pos_side,
                current_price=current_price,
            )
            if isinstance(state, dict):
                if str(state.get("engine", "")).upper() not in ("RANGE", "TREND"):
                    state["engine"] = cycle_trade_engine
                if state.get("entry_regime") in (None, ""):
                    state["entry_regime"] = str(sm_regime).upper()
            if entry_price <= 0:
                continue
            state_engine = self._resolve_dual_engine(state.get("engine", cycle_trade_engine), fallback=cycle_trade_engine)
            if state_engine == "UNKNOWN":
                state_engine = cycle_trade_engine
            state["engine"] = state_engine
            
            # 【综合牛熊状态判断】使用 BTC + 交易对自身状态动态加权
            # 这样可以检测独立行情，避免被 BTC 误判
            combined_regime, combined_score, combined_details = self._dca_get_combined_regime(symbol, params)
            
            # 【牛熊状态平仓优先级调整】
            # 牛市持有空单：提高平仓优先级（降低平仓阈值）
            # 熊市持有多单：提高平仓优先级
            regime_close_multiplier = 1.0  # 默认不调整
            regime_close_hint = ""
            
            if combined_regime == "BULL" and pos_side == "SHORT":
                # 牛市持有空单 - 逆势持仓，更容易触发平仓
                regime_close_multiplier = float(params.get("bull_short_close_mult", 0.65) or 0.65)
                regime_close_hint = f"🐂 综合判断牛市，持有空单，平仓阈值降至 {regime_close_multiplier:.2f}x"
            elif combined_regime == "BEAR" and pos_side == "LONG":
                # 熊市持有多单 - 逆势持仓，更容易触发平仓
                regime_close_multiplier = float(params.get("bear_long_close_mult", 0.65) or 0.65)
                regime_close_hint = f"🐻 综合判断熊市，持有多单，平仓阈值降至 {regime_close_multiplier:.2f}x"
            
            # 打印综合判断详情（帮助用户理解判断逻辑）
            btc_w = combined_details.get("btc_weight", 0.6)
            sym_w = combined_details.get("symbol_weight", 0.4)
            btc_r = combined_details.get("btc_regime", "NEUTRAL")
            sym_r = combined_details.get("symbol_regime", "NEUTRAL")
            dir_match = combined_details.get("direction_match", True)
            sig_label = str(combined_details.get("signal_label", "无有效共振"))
            bull_hits = int(combined_details.get("bull_hits", 0) or 0)
            bear_hits = int(combined_details.get("bear_hits", 0) or 0)
            print(
                f"   📊 {symbol} 综合: {combined_regime}({combined_score:+.2f}) = "
                f"BTC({btc_r})×{btc_w:.0%} + 自身({sym_r})×{sym_w:.0%} | "
                f"方向{'一致' if dir_match else '背离'} | 信号={sig_label} (多{bull_hits}/空{bear_hits})"
            )
            if regime_close_hint:
                print(f"   ⚠️ {regime_close_hint}")

            if pos.get("side") == "SHORT":
                pnl_pct = (entry_price - current_price) / entry_price
            else:
                pnl_pct = (current_price - entry_price) / entry_price

            # 【集中计算阈值】统一获取 TP/SL/BE/Trailing 阈值（regime-aware）
            thr = self._get_exit_thresholds_by_regime(
                params,
                sm_regime,
                engine_override=state_engine,
                entry_regime=(state.get("entry_regime") if isinstance(state, dict) else None),
                verbose=True,
            )
            tp = thr["take_profit_pct"]
            sl = thr["stop_loss_pct"]
            be_trig = thr["break_even_trigger_pct"]
            be_buf = thr["break_even_buffer_pct"]
            tr_trig = thr["trailing_trigger_pct"]
            tr_sl = thr["trailing_stop_pct"]

            max_hold_days = float(params.get("max_hold_days", 1))
            max_hold_minutes = max_hold_days * 24 * 60
            max_hold_bars_cfg = 0
            try:
                if state_engine == "TREND":
                    max_hold_bars_cfg = int(risk_trend_exit.get("max_hold_bars", 0) or 0)
                else:
                    max_hold_bars_cfg = int(risk_osc_exit.get("max_hold_bars", 0) or 0)
            except Exception:
                max_hold_bars_cfg = 0
            if max_hold_bars_cfg > 0:
                max_hold_minutes = max_hold_bars_cfg * max(1, int(bar_minutes))

            hold_minutes = (now - state.get("entry_time", now)).total_seconds() / 60

            # 状态标记：是否已启动保本止损（状态化，触发后保持）
            be_active = bool(state.get("be_active", False))

            # ---- 触发判断（优先级：TP > BE(底线) > Trailing(锁利) > SL）----

            # 1) TP 止盈
            if pnl_pct >= tp:
                self._close_position(
                    symbol,
                    {"action": "CLOSE", "reason": f"dca_take_profit(engine={state_engine})"},
                    side=pos.get("side"),
                )
                self.dca_state.pop(symbol, None)
                self._save_dca_state()
                self._write_dca_dashboard(positions)
                continue

            # 2) BE 保本止损（底线：盈利超过阈值后，止损抬到成本附近）
            if be_trig > 0:
                # 达到 BE 触发线后，持续生效（直到本仓位结束或加仓重置）
                if (not be_active) and pnl_pct >= be_trig:
                    be_active = True
                    state["be_active"] = True
                    self._save_dca_state()

                if be_active and pnl_pct <= -be_buf:
                    stop_reason = f"保本底线触发(回撤 <= {-be_buf*100:.2f}%, 当前{pnl_pct*100:.2f}%)"
                    print(f"🛑 {symbol} {stop_reason}")
                    self._close_position(
                        symbol,
                        {"action": "CLOSE", "reason": stop_reason},
                        side=pos.get("side"),
                    )
                    self.dca_state.pop(symbol, None)
                    self._save_dca_state()
                    self._write_dca_dashboard(positions)
                    continue
            else:
                # 关闭 BE 功能时，防止残留状态影响 SL/Trailing
                if be_active:
                    state["be_active"] = False
                be_active = False

            # 3) Trailing 锁利（允许早于保本线触发；BE启用后按ratio二次调整回撤阈值）
            tr_after_be_ratio = float(thr.get("trailing_stop_after_be_ratio", 1.0) or 1.0)
            tr_sl_eff = tr_sl * (tr_after_be_ratio if be_active else 1.0)
            trig, tr_reason = self._check_trailing_stop_by_pnl(
                state, pnl_pct, tr_trig, tr_sl_eff, regime=state_engine
            )
            if trig:
                suffix = " | BE已启用" if be_active else ""
                stop_reason = (tr_reason or "锁利移动止损触发") + suffix
                print(f"🛑 {symbol} {stop_reason}")
                self._close_position(
                    symbol,
                    {"action": "CLOSE", "reason": stop_reason},
                    side=pos.get("side"),
                )
                self.dca_state.pop(symbol, None)
                self._save_dca_state()
                self._write_dca_dashboard(positions)
                continue

            # 4) 普通 SL 止损（仅当 BE 未启用）
            if (not be_active) and pnl_pct <= -sl:
                stop_reason = f"普通止损触发(亏损{pnl_pct*100:.2f}% <= -{sl*100:.2f}%)"
                print(f"🛑 {symbol} {stop_reason}")
                self._close_position(
                    symbol,
                    {"action": "CLOSE", "reason": stop_reason},
                    side=pos.get("side"),
                )
                self.dca_state.pop(symbol, None)
                self._save_dca_state()
                self._write_dca_dashboard(positions)
                continue

            if hold_minutes >= max_hold_minutes:
                self._close_position(
                    symbol,
                    {"action": "CLOSE", "reason": f"dca_max_hold_time(engine={state_engine})"},
                    side=pos.get("side"),
                )
                self.dca_state.pop(symbol, None)
                self._save_dca_state()
                self._write_dca_dashboard(positions)
                continue

            # 评分与加仓/平仓逻辑（按当前交易对独立计算）
            df = self._dca_get_klines_df(symbol, interval, limit=200)
            if df is None or len(df) < 50:
                continue
            df = self._dca_calc_indicators(df, bar_minutes)
            row = df.iloc[-1]
            regime = self._dca_detect_market_regime(symbol, params)
            threshold_short_adj, threshold_long_adj = self._dca_apply_regime_thresholds(
                score_threshold_short,
                score_threshold_long,
                regime,
                params,
            )
            short_score, long_score = self._dca_score_pair(row, rsi_entry_short, rsi_entry_long)
            _fee_c, _fund_c, _slip_c, _total_c, cost_z = self._dca_estimate_costs(symbol, pos_side, params)
            base_threshold_used = threshold_short_adj if pos_side == "SHORT" else threshold_long_adj
            score_threshold_used, _vol_z, _trend_z = self._dca_dynamic_threshold(
                base_threshold=base_threshold_used,
                regime=regime,
                side=pos_side,
                row=row,
                params=params,
                cost_z=cost_z,
            )
            score_used = short_score if pos_side == "SHORT" else long_score
            
            # 应用牛熊状态平仓乘数
            score_exit_mult = float(params.get("score_exit_multiplier", 1.0)) * regime_close_multiplier
            score_exit_mult *= float(thr.get("score_exit_sensitivity", 1.0))
            try:
                score_exit_min_hold_bars_range = int(params.get("score_exit_min_hold_bars_range", 3) or 3)
            except Exception:
                score_exit_min_hold_bars_range = 3
            try:
                score_exit_min_hold_bars_trend = int(params.get("score_exit_min_hold_bars_trend", 1) or 1)
            except Exception:
                score_exit_min_hold_bars_trend = 1
            try:
                score_exit_early_loss_guard_pct = float(params.get("score_exit_early_loss_guard_pct", 0.004) or 0.004)
            except Exception:
                score_exit_early_loss_guard_pct = 0.004
            score_exit_min_hold_bars_range = max(0, score_exit_min_hold_bars_range)
            score_exit_min_hold_bars_trend = max(0, score_exit_min_hold_bars_trend)
            score_exit_early_loss_guard_pct = self._clamp_value(score_exit_early_loss_guard_pct, 0.0, 0.10)

            score_exit_min_hold_bars = (
                score_exit_min_hold_bars_range if state_engine == "RANGE" else score_exit_min_hold_bars_trend
            )
            hold_bars = hold_minutes / max(1, int(bar_minutes))
            score_exit_in_grace = hold_bars < score_exit_min_hold_bars
            score_exit_force_by_loss = (
                score_exit_early_loss_guard_pct > 0 and pnl_pct <= -score_exit_early_loss_guard_pct
            )
            
            if score_used < score_threshold_used * score_exit_mult:
                if score_exit_in_grace and not score_exit_force_by_loss:
                    print(
                        f"⏸️ {symbol} 评分平仓宽限中："
                        f"hold={hold_bars:.1f}/{score_exit_min_hold_bars} bars, "
                        f"pnl={pnl_pct:.2%}，暂不平仓"
                    )
                    continue
                # 打印平仓原因
                close_reason = f"评分低于阈值 (score={score_used:.3f} < th={score_threshold_used:.3f}*{score_exit_mult:.2f})"
                if score_exit_force_by_loss:
                    close_reason += f" [宽限失效: pnl<={-score_exit_early_loss_guard_pct:.2%}]"
                if regime_close_hint:
                    print(f"⚠️ {symbol} {regime_close_hint}")
                    close_reason += f" [{regime_close_hint}]"
                print(f"🔻 {symbol} {close_reason}")
                close_candidates.append((symbol, close_reason))
                continue

            # DCA 加仓条件
            td_up = row.get("td_up", 0)
            td_down = row.get("td_down", 0)
            add_step_pct = float(params.get("add_step_pct", 0.008))
            add_price_multiplier = float(params.get("add_price_multiplier", 1.0))
            last_dca_price = state.get("last_dca_price", entry_price)
            short_trigger = last_dca_price * (1 + add_step_pct * add_price_multiplier)
            long_trigger = last_dca_price * (1 - add_step_pct * add_price_multiplier)
            max_dca = int(params.get("max_dca", 3))
            engine_max_dca_cap = int(thr.get("engine_max_dca_cap", max_dca))
            max_dca = min(max_dca, engine_max_dca_cap)

            # 双引擎实盘：默认禁用DCA加仓；即便开启，也只允许 RANGE 持仓触发。
            if (not dca_add_enabled) or state_engine != "RANGE":
                max_dca = 0
            if state.get("dca_count", 0) < max_dca:
                equity_scale = self._dca_equity_scale(equity, params)
                add_margin = float(params.get("add_margin", 3.65))
                add_mult = float(params.get("add_amount_multiplier", 1.05))
                add_margin = add_margin * equity_scale * (add_mult ** state.get("dca_count", 0))
                
                # 【统一信心度】使用 score-threshold 映射为加仓信心度
                threshold_used = score_threshold_used
                score_used = short_score if pos_side == "SHORT" else long_score
                score_excess = (score_used - threshold_used) / max(1e-6, (1.0 - threshold_used))
                confidence = 0.5 + 0.35 * math.tanh(score_excess * 2.0)
                confidence = max(0.05, min(0.95, confidence))
                
                # 根据 confidence 调整加仓量：信心度高则加仓量正常，信心度低则减少
                size_factor = max(0.3, min(1.0, confidence * 1.5))  # confidence=0.5 → 0.75, confidence=0.7 → 1.0
                add_margin = add_margin * size_factor
                leverage = int(params.get("leverage", 3))
                quantity = (add_margin * leverage) / current_price
                # max_position_pct(_add) supports ratio (0.5) or percent (50)
                max_position_raw = float(params.get("max_position_pct_add", params.get("max_position_pct", 0.30)))
                max_position_ratio = max_position_raw / 100.0 if max_position_raw > 1.0 else max_position_raw
                max_position_value = equity * max_position_ratio
                current_value = self._dca_position_value(pos, current_price)
                if current_value + quantity * current_price > max_position_value:
                    continue

                if pos.get("side") == "SHORT":
                    if td_up >= int(params.get("td_add_count", 9)) and current_price >= short_trigger:
                        tp_price, sl_price = self._calc_tp_sl_prices("SHORT", current_price, params)
                        self.trade_executor.open_short(
                            symbol,
                            quantity=quantity,
                            leverage=leverage,
                            take_profit=tp_price,
                            stop_loss=sl_price,
                        )
                        # 加仓后更新 state 并重置 peak
                        self._on_dca_add_fill(state, current_price, side="SHORT")
                        self._save_dca_state()
                        self._write_dca_dashboard(positions)
                else:
                    if td_down >= int(params.get("td_add_count", 9)) and current_price <= long_trigger:
                        tp_price, sl_price = self._calc_tp_sl_prices("LONG", current_price, params)
                        self.trade_executor.open_long(
                            symbol,
                            quantity=quantity,
                            leverage=leverage,
                            take_profit=tp_price,
                            stop_loss=sl_price,
                        )
                        # 加仓后更新 state 并重置 peak
                        self._on_dca_add_fill(state, current_price, side="LONG")
                        self._save_dca_state()
                        self._write_dca_dashboard(positions)

        # =====================================================================
        def _run_decision_and_execution() -> None:
            # 核心优化：仅对（当前持仓 + DCA筛选的top候选）共2-4个交易对调用AI
            # =====================================================================
    
            # 1. 获取当前实际持仓交易对（最多2个）
            current_position_symbols = [
                s
                for s in positions.keys()
                if positions[s] and abs(float(positions[s].get("amount", positions[s].get("positionAmt", 0)))) > 0
            ][:MAX_POSITIONS]
    
            print(f"\n📊 当前持仓: {current_position_symbols} ({len(current_position_symbols)}/{MAX_POSITIONS})")
    
            # 2. DCA策略筛选候选交易对（只取top N个）
            dca_top_n = max(1, int(self.config.get("ai", {}).get("dca_top_n", 2)))
            # tuple: (symbol, score, price, side, quote_volume_24h, threshold)
            open_candidates_raw: List[Tuple[str, float, float, str, float, float]] = []
            open_candidate_reason: Dict[str, str] = {}
            selected_high: List[Dict[str, Any]] = []
            selected_low: List[Dict[str, Any]] = []
            scored_pool: List[Dict[str, Any]] = []
            flow_only_gate_mode = self._coerce_bool(params.get("flow_only_gate_mode", False), False)
            symbols_for_candidate = symbols if direction_refresh_cycle else []
            if not direction_refresh_cycle:
                print("♻️ 非方向刷新周期：跳过5m候选重算，尝试复用上一轮开仓计划")
    
            # 如果已达最大持仓数，不再寻找新候选
            if len(current_position_symbols) < MAX_POSITIONS:
                min_daily_volume = float(params.get("min_daily_volume_usdt", 30.0))
                try:
                    trend_pullback_lookback = int(params.get("trend_pullback_lookback", 6) or 6)
                except Exception:
                    trend_pullback_lookback = 6
                trend_pullback_lookback = max(2, min(20, trend_pullback_lookback))
                # 先收集全量评分，再按"高分开多 + 低分开空"组装候选
                scored_pool = []
                for symbol in symbols_for_candidate:
                    if symbol in current_position_symbols:
                        continue
                    df = self._dca_get_klines_df(symbol, interval, limit=200)
                    if df is None or len(df) < 50:
                        continue
                    df = self._dca_calc_indicators(df, bar_minutes)
                    row = df.iloc[-1]
                    if row.get("quote_volume_24h", 0) < min_daily_volume:
                        continue
                    regime = self._dca_detect_market_regime(symbol, params)
                    threshold_short_adj, threshold_long_adj = self._dca_apply_regime_thresholds(
                        score_threshold_short,
                        score_threshold_long,
                        regime,
                        params,
                    )
                    short_score, long_score = self._dca_score_pair(row, rsi_entry_short, rsi_entry_long)
                    qv24 = float(row.get("quote_volume_24h", 0) or 0)
                    threshold_short_dyn = float(threshold_short_adj)
                    threshold_long_dyn = float(threshold_long_adj)
                    if direction in ("SHORT", "BOTH"):
                        threshold_short_dyn, _vol_z_s, _trend_z_s = self._dca_dynamic_threshold(
                            base_threshold=threshold_short_adj,
                            regime=regime,
                            side="SHORT",
                            row=row,
                            params=params,
                            cost_z=0.0,
                        )
                    if direction in ("LONG", "BOTH"):
                        threshold_long_dyn, _vol_z_l, _trend_z_l = self._dca_dynamic_threshold(
                            base_threshold=threshold_long_adj,
                            regime=regime,
                            side="LONG",
                            row=row,
                            params=params,
                            cost_z=0.0,
                        )
                    # 仅纳入可交易方向；统一使用线性 score 做排序
                    if direction in ("LONG", "BOTH", "SHORT"):
                        scored_pool.append(
                            {
                                "symbol": symbol,
                                "score": float(long_score),  # 兼容旧字段：默认使用 long_score
                                "score_long": float(long_score),
                                "score_short": float(short_score),
                                "price": float(row.get("close", 0) or 0),
                                "quote_vol_24h": qv24,
                                "threshold_long": float(threshold_long_dyn),
                                "threshold_short": float(threshold_short_dyn),
                                "rsi": float(row.get("rsi", 50) or 50),
                                "bb_upper": float(
                                    row.get("bb_upper", row.get("bb_upperband", row.get("bb_upper_band", 0))) or 0
                                ),
                                "bb_lower": float(
                                    row.get("bb_lower", row.get("bb_lowerband", row.get("bb_lower_band", 0))) or 0
                                ),
                                "bb_middle": float(
                                    row.get("bb_middle", row.get("bb_middleband", row.get("bb_mid", 0))) or 0
                                ),
                                "volume_quantile": float(row.get("volume_quantile", 0.5) or 0.5),
                                "ema_fast": float(row.get("ema_fast_20", row.get("ema_fast", 0)) or 0),
                                "ema_slow": float(row.get("ema_slow_50", row.get("ema_slow", 0)) or 0),
                                "low_min_k": float(df["low"].tail(trend_pullback_lookback).min() or 0),
                                "high_max_k": float(df["high"].tail(trend_pullback_lookback).max() or 0),
                                "close_prev": float(df["close"].iloc[-2]) if len(df) >= 2 else float(row.get("close", 0) or 0),
                                "rsi_prev": float(df["rsi"].iloc[-2]) if ("rsi" in df.columns and len(df) >= 2) else float(row.get("rsi", 50) or 50),
                            }
                        )
    
                # 新规则：2个最高分做多 + 2个最低分做空
                try:
                    high_pick_n = int(params.get("high_score_candidate_n", 2) or 2)
                except Exception:
                    high_pick_n = 2
                try:
                    low_pick_n = int(params.get("low_score_candidate_n", 2) or 2)
                except Exception:
                    low_pick_n = 2
                high_pick_n = max(0, min(5, high_pick_n))
                low_pick_n = max(0, min(5, low_pick_n))
    
                # 候选排序：
                # - 趋势/默认：LONG 按 score_long 排序，SHORT 按 score_short 排序
                # - RANGE/RANGE_LOCK：按距离布林带上下轨的极值排序
                if str(sm_regime).upper() in ("RANGE", "RANGE_LOCK"):
                    def _dist_to_bb_lower(it: Dict[str, Any]) -> float:
                        price = float(it.get("price", 0) or 0)
                        lo = float(it.get("bb_lower", 0) or 0)
                        if price <= 0 or lo <= 0:
                            return 1e9
                        # 越小越贴近/跌破下轨（多头均值回归优先）
                        return (price - lo) / price
    
                    def _dist_to_bb_upper(it: Dict[str, Any]) -> float:
                        price = float(it.get("price", 0) or 0)
                        up = float(it.get("bb_upper", 0) or 0)
                        if price <= 0 or up <= 0:
                            return 1e9
                        # 越小越贴近/突破上轨（空头均值回归优先）
                        return (up - price) / price
    
                    ranked_desc = sorted(
                        scored_pool,
                        key=lambda x: (_dist_to_bb_lower(x), -float(x.get("quote_vol_24h", 0) or 0)),
                    )
                    ranked_asc = sorted(
                        scored_pool,
                        key=lambda x: (_dist_to_bb_upper(x), -float(x.get("quote_vol_24h", 0) or 0)),
                    )
                else:
                    ranked_desc = sorted(
                        scored_pool,
                        key=lambda x: (float(x.get("score_long", x.get("score", 0) or 0)), x["quote_vol_24h"]),
                        reverse=True,
                    )
                    ranked_asc = sorted(
                        scored_pool,
                        key=lambda x: (float(x.get("score_short", 0) or 0), x["quote_vol_24h"]),
                        reverse=True,
                    )
    
    
    
    
                selected_high = ranked_desc[:high_pick_n] if direction in ("LONG", "BOTH") else []
                selected_high_syms = {it["symbol"] for it in selected_high}
                selected_low = []
                if direction in ("SHORT", "BOTH"):
                    for it in ranked_asc:
                        if it["symbol"] in selected_high_syms:
                            continue
                        selected_low.append(it)
                        if len(selected_low) >= low_pick_n:
                            break
    
                min_score_long_base = float(params.get("min_score_long", 0.1))
                if cycle_trade_engine == "RANGE":
                    min_score_long_base = max(min_score_long_base, 0.12)
                elif cycle_trade_engine == "TREND":
                    min_score_long_base = max(-1.0, min_score_long_base - 0.03)
                try:
                    min_score_short_base = float(params.get("min_score_short", min_score_long_base) or min_score_long_base)
                except Exception:
                    min_score_short_base = float(min_score_long_base)
                trend_gate_tighten_cfg = (
                    params.get("trend_gate_tighten", {})
                    if isinstance(params.get("trend_gate_tighten", {}), dict)
                    else {}
                )
                try:
                    trend_score_raw = trend_gate_tighten_cfg.get("min_score_delta", 0.01)
                    trend_score_tighten = float(0.01 if trend_score_raw is None else trend_score_raw)
                except Exception:
                    trend_score_tighten = 0.01
                trend_score_tighten = max(0.0, trend_score_tighten)
                if cycle_trade_engine == "TREND" and trend_score_tighten > 0:
                    min_score_long_base = min(1.0, min_score_long_base + trend_score_tighten)
                    min_score_short_base = min(1.0, min_score_short_base + trend_score_tighten)
    
                # RANGE / RANGE_LOCK：均值回归门禁（优先于 score 过滤）
                osc_mode = params.get("oscillation_mode", {}) or {}
                osc_entry = (
                    osc_mode.get("entry", {})
                    if isinstance(osc_mode.get("entry", {}), dict)
                    else {}
                )
                osc_rsi_low = float(osc_entry.get("rsi_low", 30))
                osc_rsi_high = float(osc_entry.get("rsi_high", 70))
                osc_bb_touch = float(osc_entry.get("bb_touch", 1.0))
                osc_vol_q_max = float(osc_entry.get("vol_q_max", 0.65))
                range_gate_tighten_cfg = (
                    params.get("range_gate_tighten", {})
                    if isinstance(params.get("range_gate_tighten", {}), dict)
                    else {}
                )
                try:
                    range_rsi_raw = range_gate_tighten_cfg.get("rsi_delta", 1.0)
                    range_rsi_tighten = float(1.0 if range_rsi_raw is None else range_rsi_raw)
                except Exception:
                    range_rsi_tighten = 1.0
                try:
                    range_bb_raw = range_gate_tighten_cfg.get("bb_touch_delta", 0.002)
                    range_bb_tighten = float(0.002 if range_bb_raw is None else range_bb_raw)
                except Exception:
                    range_bb_tighten = 0.002
                try:
                    range_vol_raw = range_gate_tighten_cfg.get("vol_q_delta", 0.03)
                    range_vol_tighten = float(0.03 if range_vol_raw is None else range_vol_raw)
                except Exception:
                    range_vol_tighten = 0.03
                range_rsi_tighten = max(0.0, range_rsi_tighten)
                range_bb_tighten = max(0.0, range_bb_tighten)
                range_vol_tighten = max(0.0, range_vol_tighten)
                osc_rsi_low = max(0.0, osc_rsi_low - range_rsi_tighten)
                osc_rsi_high = min(100.0, osc_rsi_high + range_rsi_tighten)
                osc_bb_touch = self._clamp_value(osc_bb_touch - range_bb_tighten, 0.90, 1.10)
                osc_vol_q_max = self._clamp_value(osc_vol_q_max - range_vol_tighten, 0.05, 1.0)
    
                def _osc_mean_reversion_ok(it: Dict[str, Any], side: str) -> Tuple[bool, str]:
                    try:
                        price = float(it.get("price", 0) or 0)
                        rsi = float(it.get("rsi", 50) or 50)
                        up = float(it.get("bb_upper", 0) or 0)
                        lo = float(it.get("bb_lower", 0) or 0)
                        vq = float(it.get("volume_quantile", 0.5) or 0.5)
                        if price <= 0 or (up <= 0 and lo <= 0):
                            return False, "osc_no_bb"
                        if vq > osc_vol_q_max:
                            return False, f"osc_skip_breakout(vq={vq:.2f})"
                        if side == "LONG":
                            if lo > 0 and price <= lo * osc_bb_touch and rsi <= osc_rsi_low:
                                return True, f"osc_long(bb_low+rsi={rsi:.1f},vq={vq:.2f})"
                            return False, f"osc_not_triggered_long(rsi={rsi:.1f})"
                        if up > 0 and price >= up * (2 - osc_bb_touch) and rsi >= osc_rsi_high:
                            return True, f"osc_short(bb_up+rsi={rsi:.1f},vq={vq:.2f})"
                        return False, f"osc_not_triggered_short(rsi={rsi:.1f})"
                    except Exception:
                        return False, "osc_err"
    
                trend_cfg_local = risk_cfg_local.get("trend", {}) if isinstance(risk_cfg_local, dict) else {}
                trend_entry_cfg: Dict[str, Any] = {}
                if isinstance(trend_cfg_local, dict):
                    if isinstance(trend_cfg_local.get("entry", {}), dict):
                        trend_entry_cfg = trend_cfg_local.get("entry", {}) or {}
                    elif isinstance(trend_cfg_local.get("entry_gate", {}), dict):
                        trend_entry_cfg = trend_cfg_local.get("entry_gate", {}) or {}
                trend_pullback_touch = float(trend_entry_cfg.get("pullback_touch", 1.005) or 1.005)
                trend_confirm_rsi_long = float(trend_entry_cfg.get("confirm_rsi_long", 52) or 52)
                trend_confirm_rsi_short = float(trend_entry_cfg.get("confirm_rsi_short", 48) or 48)
                try:
                    trend_rsi_raw = trend_gate_tighten_cfg.get("confirm_rsi_delta", 1.0)
                    trend_rsi_tighten = float(1.0 if trend_rsi_raw is None else trend_rsi_raw)
                except Exception:
                    trend_rsi_tighten = 1.0
                trend_rsi_tighten = max(0.0, trend_rsi_tighten)
                trend_confirm_rsi_long = min(100.0, trend_confirm_rsi_long + trend_rsi_tighten)
                trend_confirm_rsi_short = max(0.0, trend_confirm_rsi_short - trend_rsi_tighten)
    
                def _trend_pullback_ok(it: Dict[str, Any], side: str) -> Tuple[bool, str]:
                    try:
                        price = float(it.get("price", 0) or 0)
                        bbm = float(it.get("bb_middle", 0) or 0)
                        ema_f = float(it.get("ema_fast", 0) or 0)
                        ema_s = float(it.get("ema_slow", 0) or 0)
                        low_min = float(it.get("low_min_k", 0) or 0)
                        high_max = float(it.get("high_max_k", 0) or 0)
                        close_prev = float(it.get("close_prev", price) or price)
                        rsi = float(it.get("rsi", 50) or 50)
                        rsi_prev = float(it.get("rsi_prev", rsi) or rsi)
                        if price <= 0 or ema_f <= 0 or ema_s <= 0:
                            return False, "trend_no_ema"
                        if side == "LONG":
                            if not (ema_f > ema_s and price > ema_f):
                                return False, f"trend_not_up(ema_f={ema_f:.4g},ema_s={ema_s:.4g})"
                            touch_ref = bbm if bbm > 0 else ema_f
                            if touch_ref > 0 and low_min > touch_ref * trend_pullback_touch:
                                return False, f"trend_no_pullback(low_min={low_min:.4g}>ref={touch_ref:.4g})"
                            if not (price > close_prev and rsi >= trend_confirm_rsi_long and rsi >= rsi_prev):
                                return False, f"trend_no_confirm(p={price:.4g},prev={close_prev:.4g},rsi={rsi:.1f}->{rsi_prev:.1f})"
                            return True, f"trend_pullback_ok(ref={touch_ref:.4g},low_min={low_min:.4g},rsi={rsi:.1f})"
                        if not (ema_f < ema_s and price < ema_f):
                            return False, f"trend_not_down(ema_f={ema_f:.4g},ema_s={ema_s:.4g})"
                        touch_ref = bbm if bbm > 0 else ema_f
                        if touch_ref > 0 and high_max < touch_ref / max(trend_pullback_touch, 1e-6):
                            return False, f"trend_no_pullback(high_max={high_max:.4g}<ref={touch_ref:.4g})"
                        if not (price < close_prev and rsi <= trend_confirm_rsi_short and rsi <= rsi_prev):
                            return False, f"trend_no_confirm(p={price:.4g},prev={close_prev:.4g},rsi={rsi:.1f}->{rsi_prev:.1f})"
                        return True, f"trend_pullback_ok(ref={touch_ref:.4g},high_max={high_max:.4g},rsi={rsi:.1f})"
                    except Exception:
                        return False, "trend_err"
    
                open_candidates_raw = []
                trend_gate_logged = False
                range_gate_logged = False
                disable_osc_long_entries = self._coerce_bool(params.get("disable_osc_long_entries", True), True)
                flow_candidate_gate_enabled = self._coerce_bool(
                    params.get("flow_candidate_gate_enabled", True), True
                )
                if flow_only_gate_mode:
                    print("   🌊 FLOW_ONLY候选模式：已禁用RANGE/TREND门禁，候选仅交给资金流门禁最终裁决")
                    for it in selected_high:
                        sym = it["symbol"]
                        # FLOW_ONLY: 必须通过资金流门禁，否则不入候选
                        if flow_candidate_gate_enabled:
                            params_flow = dict(params)
                            params_flow["_flow_engine_hint"] = str(cycle_trade_engine or "").upper()
                            flow_ok, flow_rsn, flow_snap = self._dca_flow_candidate_gate(
                                sym, "LONG", params_flow, stage="CANDIDATE"
                            )
                            if isinstance(flow_snap, dict):
                                flow_brief = self._dca_format_flow_brief(flow_snap, "LONG")
                            else:
                                flow_brief = "flow=na"
                            if not flow_ok:
                                open_candidate_reason[sym] = f"flow_only_block|{flow_rsn}|{flow_brief}"
                                print(f"   ⏸️ {sym} FLOW_ONLY 候选被资金流拦截：{flow_rsn}")
                                continue
                            open_candidate_reason[sym] = f"flow_only_pass|{flow_rsn}|{flow_brief}"
                        else:
                            open_candidate_reason[sym] = "flow_only_long_candidate"
                        open_candidates_raw.append(
                            (
                                sym,
                                float(it.get("score_long", it.get("score", 0) or 0)),
                                float(it["price"]),
                                "LONG",
                                float(it["quote_vol_24h"]),
                                float(it["threshold_long"]),
                            )
                        )
                    for it in selected_low:
                        sym = it["symbol"]
                        if flow_candidate_gate_enabled:
                            params_flow = dict(params)
                            params_flow["_flow_engine_hint"] = str(cycle_trade_engine or "").upper()
                            flow_ok, flow_rsn, flow_snap = self._dca_flow_candidate_gate(
                                sym, "SHORT", params_flow, stage="CANDIDATE"
                            )
                            if isinstance(flow_snap, dict):
                                flow_brief = self._dca_format_flow_brief(flow_snap, "SHORT")
                            else:
                                flow_brief = "flow=na"
                            if not flow_ok:
                                open_candidate_reason[sym] = f"flow_only_block|{flow_rsn}|{flow_brief}"
                                print(f"   ⏸️ {sym} FLOW_ONLY 候选被资金流拦截：{flow_rsn}")
                                continue
                            open_candidate_reason[sym] = f"flow_only_pass|{flow_rsn}|{flow_brief}"
                        else:
                            open_candidate_reason[sym] = "flow_only_short_candidate"
                        open_candidates_raw.append(
                            (
                                sym,
                                float(it.get("score_short", 0) or 0),
                                float(it["price"]),
                                "SHORT",
                                float(it["quote_vol_24h"]),
                                float(it["threshold_short"]),
                            )
                        )
                else:
                    for it in selected_high:
                        sym = it["symbol"]
    
                        # RANGE 引擎：均值回归门禁
                        if cycle_trade_engine == "RANGE":
                            if not range_gate_logged:
                                print(
                                    "   🎯 RANGE开仓门禁：均值回归触发 "
                                    f"(rsi<={osc_rsi_low:.1f}/>={osc_rsi_high:.1f}, "
                                    f"bb_touch={osc_bb_touch:.3f}, vol_q<={osc_vol_q_max:.2f})"
                                )
                                if disable_osc_long_entries:
                                    print("   ⛔ RANGE策略已禁用 LONG + osc_long（仅允许 SHORT + osc_short）")
                                range_gate_logged = True
                            if disable_osc_long_entries:
                                rsn = "osc_long_disabled_by_policy"
                                open_candidate_reason[sym] = rsn
                                print(f"   ⏸️ {sym} RANGE多头候选已禁用：{rsn}")
                                continue
                            ok, rsn = _osc_mean_reversion_ok(it, "LONG")
                            open_candidate_reason[sym] = rsn
                            if not ok:
                                print(f"   ⏸️ {sym} RANGE均值回归未触发：{rsn}，跳过")
                                continue
                            score_val_l = float(it.get("score_long", it.get("score", 0) or 0))
                            if score_val_l < min_score_long_base:
                                print(
                                    f"   ⏸️ {sym} RANGE多头评分不足："
                                    f"score={score_val_l:.3f} < min_score_long={min_score_long_base:.3f} → skip"
                                )
                                continue
                            print(f"   ✅ {sym} RANGE均值回归入场：{rsn}")
                        else:
                            # TREND 侧保持 score / pullback 门禁
                            if not trend_gate_logged:
                                print(
                                    "   ⓘ TREND候选按score+pullback门禁筛选 "
                                    f"(min_score_long={min_score_long_base:.3f}, "
                                    f"min_score_short={min_score_short_base:.3f}, "
                                    f"confirm_rsi_long>={trend_confirm_rsi_long:.1f}, "
                                    f"confirm_rsi_short<={trend_confirm_rsi_short:.1f})"
                                )
                                trend_gate_logged = True
    
                            # 过滤低评分候选（score 越高越偏多，只保留足够高的 score）
                            score_val_l = float(it.get("score_long", it.get("score", 0) or 0))
                            if score_val_l < min_score_long_base:
                                print(f"   ⏸️ {sym} LONG score={score_val_l:.3f} < min_score_long={min_score_long_base:.3f} → skip")
                                continue
                            if cycle_trade_engine == "TREND":
                                trend_ok, trend_rsn = _trend_pullback_ok(it, "LONG")
                                if not trend_ok:
                                    print(f"   ⏸️ {sym} TREND回调确认不满足：{trend_rsn}，跳过")
                                    continue
                                open_candidate_reason[sym] = trend_rsn
                                print(f"   ✅ {sym} TREND回调确认入场：{trend_rsn}")

                        # 通过 RANGE/TREND 门禁后，再走资金流候选门禁（可 soft/hard）
                        if flow_candidate_gate_enabled:
                            params_flow = dict(params)
                            params_flow["_flow_engine_hint"] = str(cycle_trade_engine or "").upper()
                            flow_ok, flow_rsn, flow_snap = self._dca_flow_candidate_gate(
                                sym, "LONG", params_flow, stage="CANDIDATE"
                            )
                            flow_brief = self._dca_format_flow_brief(flow_snap, "LONG") if isinstance(flow_snap, dict) else "flow=na"
                            if not flow_ok:
                                open_candidate_reason[sym] = f"flow_block|{flow_rsn}|{flow_brief}"
                                print(f"   ⏸️ {sym} 候选被资金流拦截：{flow_rsn}")
                                continue
                            base_rsn = open_candidate_reason.get(sym, "")
                            open_candidate_reason[sym] = f"{base_rsn}|flow_ok|{flow_rsn}|{flow_brief}" if base_rsn else f"flow_ok|{flow_rsn}|{flow_brief}"

                        open_candidates_raw.append(
                            (
                                sym,
                                float(it.get("score_long", it.get("score", 0) or 0)),
                                float(it["price"]),
                                "LONG",
                                float(it["quote_vol_24h"]),
                                float(it["threshold_long"]),
                            )
                        )
                    for it in selected_low:
                        sym = it["symbol"]
    
                        # RANGE 引擎：均值回归门禁
                        if cycle_trade_engine == "RANGE":
                            if not range_gate_logged:
                                print(
                                    "   🎯 RANGE开仓门禁：均值回归触发 "
                                    f"(rsi<={osc_rsi_low:.1f}/>={osc_rsi_high:.1f}, "
                                    f"bb_touch={osc_bb_touch:.3f}, vol_q<={osc_vol_q_max:.2f})"
                                )
                                range_gate_logged = True
                            ok, rsn = _osc_mean_reversion_ok(it, "SHORT")
                            open_candidate_reason[sym] = rsn
                            if not ok:
                                print(f"   ⏸️ {sym} RANGE均值回归未触发：{rsn}，跳过")
                                continue
                            score_val_s = float(it.get("score_short", 0) or 0)
                            if score_val_s < min_score_short_base:
                                print(
                                    f"   ⏸️ {sym} RANGE空头评分不足："
                                    f"score_short={score_val_s:.3f} < min_score_short={min_score_short_base:.3f} → skip"
                                )
                                continue
                            print(f"   ✅ {sym} RANGE均值回归入场：{rsn}")
                        else:
                            # TREND 侧保持 score / pullback 门禁
                            if not trend_gate_logged:
                                print(
                                    "   ⓘ TREND候选按score+pullback门禁筛选 "
                                    f"(min_score_long={min_score_long_base:.3f}, "
                                    f"min_score_short={min_score_short_base:.3f}, "
                                    f"confirm_rsi_long>={trend_confirm_rsi_long:.1f}, "
                                    f"confirm_rsi_short<={trend_confirm_rsi_short:.1f})"
                                )
                                trend_gate_logged = True
    
                            # 过滤空头低评分候选（score_short 越高越偏空）
                            score_val_s = float(it.get("score_short", 0) or 0)
                            if score_val_s < min_score_short_base:
                                print(f"   ⏸️ {sym} SHORT score_short={score_val_s:.3f} < min_score_short={min_score_short_base:.3f} → skip")
                                continue
                            if cycle_trade_engine == "TREND":
                                trend_ok, trend_rsn = _trend_pullback_ok(it, "SHORT")
                                if not trend_ok:
                                    print(f"   ⏸️ {sym} TREND回调确认不满足：{trend_rsn}，跳过")
                                    continue
                                open_candidate_reason[sym] = trend_rsn
                                print(f"   ✅ {sym} TREND回调确认入场：{trend_rsn}")

                        # 资金流候选门禁检查（SHORT）
                        if flow_candidate_gate_enabled:
                            params_flow = dict(params)
                            params_flow["_flow_engine_hint"] = str(cycle_trade_engine or "").upper()
                            flow_ok, flow_rsn, flow_snap = self._dca_flow_candidate_gate(
                                sym, "SHORT", params_flow, stage="CANDIDATE"
                            )
                            flow_brief = self._dca_format_flow_brief(flow_snap, "SHORT") if isinstance(flow_snap, dict) else "flow=na"
                            if not flow_ok:
                                open_candidate_reason[sym] = f"flow_block|{flow_rsn}|{flow_brief}"
                                print(f"   ⏸️ {sym} 候选被资金流拦截：{flow_rsn}")
                                continue
                            base_rsn = open_candidate_reason.get(sym, "")
                            open_candidate_reason[sym] = f"{base_rsn}|flow_ok|{flow_rsn}|{flow_brief}" if base_rsn else f"flow_ok|{flow_rsn}|{flow_brief}"

                        open_candidates_raw.append(
                            (
                                sym,
                                float(it.get("score_short", 0) or 0),
                                float(it["price"]),
                                "SHORT",
                                float(it["quote_vol_24h"]),
                                float(it["threshold_short"]),
                            )
                        )

                # 最终候选上限：不超过 dca_top_n
                open_candidates_raw = open_candidates_raw[:dca_top_n]
    
            # 严格模式：非方向刷新周期不生成新候选，只复用 5m 周期缓存计划
            if not direction_refresh_cycle:
                cached_plan = self._dca_open_plan_cache if isinstance(self._dca_open_plan_cache, list) else []
                current_bucket = getattr(self, "_dual_engine_direction_bucket", None)
                cached_bucket = getattr(self, "_dca_open_plan_cache_bucket", None)
                if (
                    isinstance(cached_plan, list)
                    and cached_plan
                    and current_bucket is not None
                    and cached_bucket is not None
                    and int(cached_bucket) != int(current_bucket)
                ):
                    print(
                        "⚠️ 检测到5m缓存计划窗口已过期，已丢弃："
                        f"cache_bucket={cached_bucket}, current_bucket={current_bucket}"
                    )
                    cached_plan = []
                open_candidates_raw = []
                open_candidate_reason = {}
                if cached_plan:
                    print(f"♻️ 复用5m缓存开仓计划: {len(cached_plan)} 条")
                for item in cached_plan:
                    if not isinstance(item, dict):
                        continue
                    sym = str(item.get("symbol", "")).upper()
                    if not sym or sym in current_position_symbols:
                        continue
                    decision_cached = item.get("decision", {})
                    if not isinstance(decision_cached, dict):
                        continue
                    action_cached = str(decision_cached.get("action", "")).upper()
                    if action_cached not in ("BUY_OPEN", "SELL_OPEN"):
                        continue
                    side_cached = "LONG" if action_cached == "BUY_OPEN" else "SHORT"
                    if (
                        (not flow_only_gate_mode)
                        and cycle_trade_engine == "RANGE"
                        and disable_osc_long_entries
                        and side_cached == "LONG"
                    ):
                        print(f"⏸️ {sym} 跳过缓存计划：RANGE 禁止 LONG + osc_long")
                        continue
                    score_cached = float(item.get("score", 0.0) or 0.0)
                    price_cached = float(item.get("price", 0.0) or 0.0)
                    qv_cached = float(item.get("quote_vol_24h", 0.0) or 0.0)
                    threshold_cached = float(item.get("threshold", 0.0) or 0.0)
                    open_candidates_raw.append(
                        (
                            sym,
                            score_cached,
                            price_cached,
                            side_cached,
                            qv_cached,
                            threshold_cached,
                        )
                    )
                    cached_reason = str(
                        decision_cached.get("entry_reason")
                        or item.get("entry_reason")
                        or ""
                    ).strip()
                    if cached_reason:
                        open_candidate_reason[sym] = cached_reason
                open_candidates_raw = open_candidates_raw[:dca_top_n]
                if not open_candidates_raw:
                    print("⏭️ 当前无可执行的5m缓存开仓计划")
    
            candidate_symbols = [c[0] for c in open_candidates_raw]
            candidate_score_info = [f"{c[0]}:{c[1]:.3f}:{c[3]}" for c in open_candidates_raw]
            candidate_mode_label = "FLOW_ONLY候选" if flow_only_gate_mode else "双引擎候选"
            print(f"📈 {candidate_mode_label}: {candidate_symbols} (top {dca_top_n})")
            if candidate_score_info:
                print(f"   线性评分: {', '.join(candidate_score_info)}")
            # RANGE/RANGE_LOCK：纯日志增强，打印 BB 距离（不改变交易行为）
            if str(sm_regime).upper() in ("RANGE", "RANGE_LOCK"):
                def _safe_f(v: Any, d: float = 0.0) -> float:
                    try:
                        return float(v)
                    except Exception:
                        return float(d)
    
                def _dist_lower(it: Dict[str, Any]) -> float:
                    p = _safe_f(it.get("price", 0), 0.0)
                    lo = _safe_f(it.get("bb_lower", 0), 0.0)
                    if p <= 0 or lo <= 0:
                        return 9999.0
                    return (p - lo) / p
    
                def _dist_upper(it: Dict[str, Any]) -> float:
                    p = _safe_f(it.get("price", 0), 0.0)
                    up = _safe_f(it.get("bb_upper", 0), 0.0)
                    if p <= 0 or up <= 0:
                        return 9999.0
                    return (up - p) / p
    
                if selected_high:
                    high_dbg: List[str] = []
                    for it in selected_high:
                        p = _safe_f(it.get("price", 0), 0.0)
                        lo = _safe_f(it.get("bb_lower", 0), 0.0)
                        d = _dist_lower(it)
                        high_dbg.append(f"{it.get('symbol')} dL={d:.4f} p={p:.6g} lo={lo:.6g}")
                    print(f"   RANGE BB距下轨: {', '.join(high_dbg)}")
    
                if selected_low:
                    low_dbg: List[str] = []
                    for it in selected_low:
                        p = _safe_f(it.get("price", 0), 0.0)
                        up = _safe_f(it.get("bb_upper", 0), 0.0)
                        d = _dist_upper(it)
                        low_dbg.append(f"{it.get('symbol')} dU={d:.4f} p={p:.6g} up={up:.6g}")
                    print(f"   RANGE BB距上轨: {', '.join(low_dbg)}")
            if open_candidate_reason:
                reason_info = [f"{sym}:{open_candidate_reason.get(sym, '')}" for sym in candidate_symbols if sym in open_candidate_reason]
                if reason_info:
                    print(f"   {cycle_trade_engine} 入场触发: {', '.join(reason_info)}")
    
            # 3. 合并持仓+候选，准备AI批量分析（总共2-4个交易对）
            symbols_for_ai = list(set(current_position_symbols + candidate_symbols))
            multi_decisions: Dict[str, Dict[str, Any]] = {}
            if not symbols_for_ai:
                # 注意：不要提前 return，后续仍需推进状态机/风控与缓存维护。
                print("⏭️  无持仓也无候选，本轮跳过AI分析，但继续执行状态机与风控流程")
            # 如果 AI 可用则用于决策，否则使用规则化 DCA 决策直接开仓/平仓
            elif self._dca_ai_gate_enabled():
                print(f"🤖 AI分析目标: {symbols_for_ai} (共{len(symbols_for_ai)}个)")
    
                # 4. 批量调用AI分析
                all_symbols_data: Dict[str, Any] = {}
                for s in symbols_for_ai:
                    market_data = self.get_market_data_for_symbol(s)
                    position = positions.get(s)
                    all_symbols_data[s] = {"market_data": market_data, "position": position}
    
                pb = self.prompt_builder
                ai = self.ai_client
                dp = self.decision_parser
                if pb and ai and dp:
                    try:
                        prompt = pb.build_multi_symbol_analysis_prompt(
                            all_symbols_data=all_symbols_data,
                            all_positions=positions,
                            account_summary=self.account_data.get_account_summary(),
                            history=self.decision_history,
                        )
                        resp = ai.analyze_and_decide(prompt)
                        content = resp.get("content", "")
    
                        # 调试：打印AI返回的内容（截断）
                        print(f"📄 AI返回内容（前500字符）: {content[:500]}...")
    
                        multi_decisions = dp.parse_multi_symbol_response(content)
                        print(f"✅ AI返回{len(multi_decisions)}个决策")
                    except Exception as e:
                        print(f"❌ AI批量分析失败: {e}")
                        multi_decisions = {}
                else:
                    print("⚠️ AI组件未完全初始化，跳过 AI 分析")
                    multi_decisions = {}
            else:
                # AI 被禁用：使用规则化双引擎决策直接对候选构建开仓建议
                print(f"⚙️ AI已禁用，使用规则双引擎决策处理候选: {candidate_symbols}")
                multi_decisions = {}
                # open_candidates_raw 包含 (symbol, score, price, side, quote_vol_24h, threshold)

                # 获取趋势评分开关
                trend_scoring_enabled_local = bool(params.get("trend_scoring_enabled", True))
                if cycle_trade_engine == "RANGE":
                    print("ⓘ RANGE规则门禁：均值回归触发")
                else:
                    print("ⓘ TREND规则门禁：score + pullback")
                for tup in (open_candidates_raw or []):
                    try:
                        sym, score_val, price_val, side_val, _qv, threshold_val = tup
                    except Exception:
                        continue
                    is_short = (side_val or "SHORT").upper() == "SHORT"
                    
                    action = "SELL_OPEN" if is_short else "BUY_OPEN"
                    
                    score_excess = (float(score_val) - float(threshold_val)) / max(1e-6, (1.0 - float(threshold_val)))
                    confidence = 0.5 + 0.35 * math.tanh(score_excess * 2.0)
                    confidence = self._clamp_value(confidence, 0.05, 0.95)
                    
                    take_profit = float(params.get("take_profit_pct", params.get("take_profit", 0.02)))
                    stop_loss = float(params.get("symbol_stop_loss_pct", params.get("symbol_stop_loss", 0.15)))
                    try:
                        leverage = int(float(params.get("leverage", 3)))
                    except Exception:
                        leverage = 5
                    leverage = max(5, min(12, leverage))
    
                    # 【ATR仓位计算】使用波动率动态调整仓位
                    if trend_scoring_enabled_local:
                        atr_position, atr_details = self._calc_position_size_by_atr(sym, params)
                        # 将ATR仓位转换为比例形式（0~1）
                        # atr_position 是 USDT 名义价值
                        account_summary = self.account_data.get_account_summary() or {}
                        equity = float(account_summary.get("equity", 100))
                        max_position_raw = float(params.get("max_position_pct", 0.30))
                        max_position_ratio = max_position_raw if 0 < max_position_raw <= 1.0 else max_position_raw / 100.0
                        max_position_ratio = max(0.01, min(0.95, max_position_ratio))
                        if equity > 0:
                            # ATR 计算返回 USDT 名义价值，转为权益比例
                            position_ratio = atr_position / equity
                            position_ratio = min(max_position_ratio, max(0.05, position_ratio))
                        else:
                            position_ratio = 0.15
                        print(f"   📊 {sym} ATR仓位: {position_ratio * 100:.1f}% (ATR={atr_details.get('atr', 0):.6f})")
                    else:
                        # 直接使用配置的 0~1 比例值（如 0.45 表示 45%）
                        # 兼容：如果配置值 > 1，视为百分比，自动转换
                        position_raw = float(params.get("max_position_pct", 0.30))
                        max_position_ratio = position_raw if 0 < position_raw <= 1.0 else position_raw / 100.0
                        max_position_ratio = max(0.01, min(0.95, max_position_ratio))
                        position_ratio = max_position_ratio
    
    
                    # 获取趋势强度（用于决策记录，不再影响 confidence）
                    self._dca_fetch_multi_timeframes(sym)
                    trend_strength = self._dca_trend_strength(sym)
                    normalized_trend = trend_strength if (action == "BUY_OPEN") else -trend_strength
    
                    decision = {
                        "action": action,
                        "confidence": confidence,
                        "leverage": leverage,
                        "engine": cycle_trade_engine,
                        "entry_regime": str(sm_regime).upper(),
                        "entry_reason": open_candidate_reason.get(sym, ""),
                        "position_percent": position_ratio,  # 统一存储 0~1 比例
                        "position_percent_base": position_ratio,
                        "position_percent_cap": max_position_ratio,
                        "take_profit_percent": take_profit,
                        "stop_loss_percent": -abs(stop_loss),
                        "reason": (
                            f"规则双引擎候选(engine={cycle_trade_engine},sm={str(sm_regime).upper()},"
                            f"score={score_val:.3f},th={float(threshold_val):.3f},"
                            f"score_th_gap={float(score_val - threshold_val):.3f}"
                            + (f"|{open_candidate_reason.get(sym, '')}" if open_candidate_reason.get(sym) else "")
                            + ")"
                        ),
                        "trend_strength": normalized_trend,
                    }
                    multi_decisions[sym] = decision
    
            # 将本次 5m 决策的开仓计划缓存，供后续 1m 执行层复用
            if direction_refresh_cycle:
                refreshed_plan: List[Dict[str, Any]] = []
                for sym, score_val, price_val, side_val, qv_val, threshold_val in (open_candidates_raw or []):
                    decision_live = multi_decisions.get(sym)
                    if not isinstance(decision_live, dict):
                        continue
                    action_live = str(decision_live.get("action", "")).upper()
                    if action_live not in ("BUY_OPEN", "SELL_OPEN"):
                        continue
                    refreshed_plan.append(
                        {
                            "symbol": str(sym).upper(),
                            "score": float(score_val),
                            "price": float(price_val),
                            "target_side": str(side_val).upper(),
                            "quote_vol_24h": float(qv_val),
                            "threshold": float(threshold_val),
                            "entry_reason": str(open_candidate_reason.get(sym, "")),
                            "decision": dict(decision_live),
                        }
                    )
                self._dca_open_plan_cache = refreshed_plan
                self._dca_open_plan_cache_bucket = getattr(self, "_dual_engine_direction_bucket", None)
                self._dca_open_plan_cache_created_at = datetime.now().isoformat()
                print(
                    "💾 已缓存5m开仓计划: "
                    f"{len(refreshed_plan)} 条 (bucket={self._dca_open_plan_cache_bucket})"
                )
    
            # 5. 处理AI决策：先平仓，再开仓
            # 5.1 检查所有当前持仓，看AI是否建议平仓
            min_conf = self._dca_ai_min_confidence()
            if self._dca_ai_gate_enabled():
                for symbol in current_position_symbols:
                    pos = positions.get(symbol)
                    if not pos:
                        continue
    
                    # 获取AI决策（应该在multi_decisions中）
                    decision = multi_decisions.get(symbol)
    
                    # 若无 AI 决策，维持规则双引擎处理结果，不额外打印告警
                    if not decision:
                        continue
    
                    action = decision.get("action", "HOLD")
                    confidence = decision.get("confidence", 0.0)
    
                    # 标准化confidence
                    if isinstance(confidence, str):
                        conf_str = confidence.upper()
                        conf_map = {"HIGH": 0.8, "MEDIUM": 0.6, "LOW": 0.4}
                        confidence = conf_map.get(conf_str, 0.5)
                    try:
                        confidence = float(confidence)
                    except Exception:
                        confidence = 0.5
    
                    # 判断是否执行平仓
                    if action == "CLOSE" and confidence >= min_conf:
                        print(f"🔻 AI建议平仓: {symbol} (confidence={confidence:.2f})")
    
                        market_data_for_close = self.get_market_data_for_symbol(symbol)
                        self.save_decision(symbol, decision, market_data_for_close)
    
                        try:
                            # execute_decision会根据action=CLOSE执行平仓
                            self.execute_decision(symbol, decision, market_data_for_close)
                        except Exception as e:
                            print(f"⚠️ execute_decision失败，尝试直接平仓: {e}")
                            # 回退为直接平仓
                            try:
                                self._close_position(symbol, decision, side=pos.get("side"))
                            except Exception as e2:
                                print(f"❌ 直接平仓也失败: {e2}")
    
                        # 清理 DCA 状态并写盘
                        try:
                            self.dca_state.pop(symbol, None)
                            self._save_dca_state()
                            self._write_dca_dashboard(positions)
                        except Exception:
                            pass
                    elif action == "HOLD":
                        print(f"⏸️ {symbol} AI建议持仓 (confidence={confidence:.2f})")
                    else:
                        print(f"ℹ️ {symbol} AI决策={action} (confidence={confidence:.2f})，保留持仓")
            elif current_position_symbols:
                print("⚙️ AI已禁用，当前持仓按规则双引擎处理（本轮已完成加仓/平仓判断）")
    
            # 5.1 规则评分平仓执行：此前只收集候选，现改为立即执行
            if close_candidates:
                closed_by_score: List[str] = []
                seen_close_symbols = set()
                for symbol, close_reason in close_candidates:
                    sym = str(symbol or "").upper()
                    if not sym or sym in seen_close_symbols:
                        continue
                    seen_close_symbols.add(sym)
                    positions_live = self.position_data.get_all_positions() or {}
                    pos_live = positions_live.get(sym)
                    if not pos_live:
                        continue
                    amt_live = abs(float(pos_live.get("amount", pos_live.get("positionAmt", 0)) or 0))
                    if amt_live <= 0:
                        continue
                    side_live = str(pos_live.get("side", "")).upper()
                    if side_live not in ("LONG", "SHORT"):
                        continue
                    print(f"🔻 规则评分平仓执行: {sym} ({close_reason})")
                    try:
                        self._close_position(sym, {"action": "CLOSE", "reason": close_reason}, side=side_live)
                    except Exception as e:
                        print(f"⚠️ 规则评分平仓失败 {sym}: {e}")
                        continue
                    self.dca_state.pop(sym, None)
                    closed_by_score.append(sym)
    
                if closed_by_score:
                    self._save_dca_state()
                    try:
                        self._write_dca_dashboard(self.position_data.get_all_positions() or {})
                    except Exception:
                        pass
                    print(f"✅ 规则评分平仓完成: {closed_by_score}")
    
            # 5.2 处理开仓决策：仅在持仓数<MAX_POSITIONS时才考虑开仓
            allow_open_new = True
            # 只有在由连续亏损/当天亏损触发且仍在有效期内的冷却才阻止新开仓
            if self._is_dca_cooldown_active(params):
                allow_open_new = False
    
            # 统计当前实际持仓数（可能在平仓后已经改变）
            positions_after_close = self.position_data.get_all_positions()
            current_count = 0
            current_long_count = 0
            current_short_count = 0
            for s, p in positions_after_close.items():
                if not p:
                    continue
                amt = abs(float(p.get("amount", p.get("positionAmt", 0)) or 0))
                if amt <= 0:
                    continue
                current_count += 1
                side = str(p.get("side", "")).upper()
                if side == "LONG":
                    current_long_count += 1
                elif side == "SHORT":
                    current_short_count += 1
    
            # 多空持仓上限（根据牛熊状态动态调整）：
            # 【牛熊切换状态机】滞回 + 去抖 + 冷却 + flip限制
            regime_sm_enabled = bool(params.get("regime_state_machine", {}).get("enabled", True))
    
            if regime_sm_enabled:
                # ===== 使用状态机 =====
                if direction_refresh_cycle:
                    # 1. 计算趋势分数 TS（仅在方向刷新周期更新）
                    ts, ts_details = self._calc_trend_score("BTCUSDT", params)
    
                    # 2. 更新 BTC 1H 指标（整点后更新一次，其他时间使用缓存）
                    indicators_1h = self._update_btc_1h_indicators(params)
                    bos = int(indicators_1h["bos"])
                    vol_ratio = float(indicators_1h["vol_ratio"])
    
                    # 3. 计算 4H ADX（独立计算）
                    df_4h = self._dca_get_klines_df("BTCUSDT", "4h", limit=60)
                    adx_4h = 25.0
                    if df_4h is not None and len(df_4h) >= 55:
                        adx_4h = self._calc_adx(df_4h, period=14)
    
                    # 4. 运行状态机
                    # 使用 1H close_time 作为 bos_event_ts（BOS事件时间戳）
                    bos_event_ts = indicators_1h.get("close_time", 0) if bos != 0 else None
                    new_regime, action, _sm_details = self._decide_regime_state_machine(
                        ts=ts,
                        bos=bos,
                        vol_ratio=vol_ratio,
                        adx_4h=adx_4h,
                        params=params,
                        ctx=self._regime_sm_ctx,
                        bos_event_ts=bos_event_ts,
                    )
                else:
                    # 非方向刷新周期：沿用上一轮状态机结果，不推进确认/切换计数
                    ts = float(self._trend_score_cache.get("ts", 0.0) or 0.0)
                    ts_details_raw = self._trend_score_cache.get("details", {})
                    ts_details = ts_details_raw if isinstance(ts_details_raw, dict) else {}
                    bos = int(self._regime_sm_ctx.get("cached_bos", 0) or 0)
                    vol_ratio = float(self._regime_sm_ctx.get("cached_vol_ratio", 1.0) or 1.0)
                    adx_4h = float(self._trend_score_cache.get("adx_4h", 25.0) or 25.0)
                    indicators_1h = {"updated": False}
                    new_regime = str(self._regime_sm_ctx.get("regime", "RANGE") or "RANGE")
                    action = "HOLD"
                    _sm_details = {}
                    print("   ⏱️ 非方向刷新周期：状态机方向冻结，确认计数不推进")
    
                bos = max(-1, min(1, int(bos)))
    
                # 更新缓存
                new_engine = self._map_regime_to_engine(new_regime)
                self._trend_score_cache["ts"] = ts
                self._trend_score_cache["regime"] = new_regime
                self._trend_score_cache["engine"] = new_engine
                self._trend_score_cache["details"] = ts_details
                self._trend_score_cache["adx_4h"] = adx_4h
    
                # 打印状态机详情
                ctx = self._regime_sm_ctx
                cache_status = "✨已刷新" if indicators_1h.get("updated") else "📦使用缓存"
                print(f"\n📈 【牛熊状态机】Regime={new_regime} | Engine={new_engine} | TS={ts:+.3f} | {cache_status}")
                print(f"   ├─ BOS: {['无', '上破↑', '下破↓'][bos+1]} | VolRatio={vol_ratio:.2f} | ADX4H={adx_4h:.1f}")
                print(f"   ├─ 确认计数: 牛={ctx['bull_confirm']}, 熊={ctx['bear_confirm']}")
                print(f"   ├─ flip队列: {len(ctx['flip_times'])}次 (限制={params.get('regime_state_machine', {}).get('FLIP_LIMIT', 2)})")
                print(f"   └─ 宏观层: TS_macro={ts_details.get('ts_macro', 0):+.3f}")
    
                # 状态转换处理
                positions_changed = False
                if action == "TRANSITIONED":
                    print(f"\n🔄 【状态机转换】→ {new_regime}")
                    # 平掉方向不符的持仓
                    if new_regime in ("BEAR_STRONG", "BEAR_WEAK"):
                        for sym, pos in list(positions_after_close.items()):
                            if pos and str(pos.get("side", "")).upper() == "LONG":
                                print(f"🐻 状态机转熊，平掉多单: {sym}")
                                self._close_position(sym, {"action": "CLOSE", "reason": "sm_bear_close_long"}, side="LONG")
                                self.dca_state.pop(sym, None)
                                positions_changed = True
                    elif new_regime in ("BULL_STRONG", "BULL_WEAK"):
                        for sym, pos in list(positions_after_close.items()):
                            if pos and str(pos.get("side", "")).upper() == "SHORT":
                                print(f"🐂 状态机转牛，平掉空单: {sym}")
                                self._close_position(sym, {"action": "CLOSE", "reason": "sm_bull_close_short"}, side="SHORT")
                                self.dca_state.pop(sym, None)
                                positions_changed = True
                    self._last_regime = new_regime.split("_")[0]  # BULL_STRONG -> BULL
                    self._save_dca_state()
    
                elif action == "RANGE_LOCK":
                    print("\n🔒 【flip超限】进入 RANGE_LOCK，强制震荡模式")
                    # 将持仓硬修剪到 range-lock 结构（默认 2多2空）
                    osc_mode = params.get("oscillation_mode", {}) or {}
                    lock_max_long = int(osc_mode.get("range_lock_max_long", 2))
                    lock_max_short = int(osc_mode.get("range_lock_max_short", 2))
    
                    side_positions: Dict[str, List[Tuple[str, float]]] = {"LONG": [], "SHORT": []}
                    for sym, pos in list(positions_after_close.items()):
                        if not pos:
                            continue
                        amt = abs(float(pos.get("amount", pos.get("positionAmt", 0)) or 0))
                        if amt <= 0:
                            continue
                        side = str(pos.get("side", "")).upper()
                        if side not in ("LONG", "SHORT"):
                            continue
                        pnl_pct = pos.get("pnl_percent")
                        pnl_val = self._to_float(pnl_pct, default=float("nan"))
                        if pnl_pct is None or not math.isfinite(pnl_val):
                            entry = float(pos.get("entry_price", pos.get("entryPrice", 0)) or 0)
                            mark = float(pos.get("mark_price", pos.get("markPrice", 0)) or 0)
                            if entry > 0 and mark > 0:
                                pnl_val = (mark - entry) / entry if side == "LONG" else (entry - mark) / entry
                            else:
                                pnl_val = 0.0
                        side_positions[side].append((sym, pnl_val))
    
                    trim_plan: List[Tuple[str, str, float]] = []
                    for side, keep in (("LONG", lock_max_long), ("SHORT", lock_max_short)):
                        items = sorted(side_positions.get(side, []), key=lambda x: x[1])  # 先平较差仓位
                        excess = max(0, len(items) - max(0, keep))
                        for sym, pnl_val in items[:excess]:
                            trim_plan.append((sym, side, pnl_val))
    
                    for sym, side, pnl_val in trim_plan:
                        print(f"🔧 RANGE_LOCK修剪: 平{side} {sym} (pnl={pnl_val:+.4f})")
                        try:
                            self._close_position(
                                sym,
                                {"action": "CLOSE", "reason": f"range_lock_trim_{side.lower()}"},
                                side=side,
                            )
                            self.dca_state.pop(sym, None)
                            positions_changed = True
                        except Exception as e:
                            print(f"⚠️ RANGE_LOCK修剪失败 {sym}: {e}")
                    if trim_plan:
                        self._save_dca_state()
    
                if positions_changed:
                    positions_after_close = self.position_data.get_all_positions() or {}
                    current_count = 0
                    current_long_count = 0
                    current_short_count = 0
                    for p in positions_after_close.values():
                        if not p:
                            continue
                        amt = abs(float(p.get("amount", p.get("positionAmt", 0)) or 0))
                        if amt <= 0:
                            continue
                        current_count += 1
                        side = str(p.get("side", "")).upper()
                        if side == "LONG":
                            current_long_count += 1
                        elif side == "SHORT":
                            current_short_count += 1
    
                # 映射到简化regime（用于兼容后续逻辑）
                if new_regime in ("BULL_STRONG", "BULL_WEAK"):
                    effective_regime = "BULL"
                elif new_regime in ("BEAR_STRONG", "BEAR_WEAK"):
                    effective_regime = "BEAR"
                else:
                    effective_regime = "NEUTRAL"
    
                global_regime = effective_regime
                regime_score = ts
                is_oscillation = new_regime in ("RANGE", "RANGE_LOCK")
    
            else:
                # ===== 使用旧的regime检测（向后兼容）=====
                trend_scoring_enabled = bool(params.get("trend_scoring_enabled", True))
    
                if trend_scoring_enabled:
                    if direction_refresh_cycle:
                        ts, ts_details = self._calc_trend_score("BTCUSDT", params)
                        ts_regime, ts_regime_label = self._get_regime_from_ts(ts, params)
                        is_oscillation, osc_details = self._detect_oscillation_market(params)
    
                        self._trend_score_cache["ts"] = ts
                        self._trend_score_cache["regime"] = ts_regime
                        self._trend_score_cache["engine"] = self._map_regime_to_engine(ts_regime)
                        self._trend_score_cache["is_oscillation"] = is_oscillation
                        self._trend_score_cache["details"] = ts_details
                    else:
                        ts = float(self._trend_score_cache.get("ts", 0.0) or 0.0)
                        ts_regime = str(self._trend_score_cache.get("regime", "NEUTRAL") or "NEUTRAL")
                        ts_regime_label = "CACHED"
                        is_oscillation = bool(self._trend_score_cache.get("is_oscillation", False))
                        ts_details_raw = self._trend_score_cache.get("details", {})
                        ts_details = ts_details_raw if isinstance(ts_details_raw, dict) else {}
                        print("   ⏱️ 非方向刷新周期：沿用趋势评分缓存，不执行趋势转换确认")
    
                    if ts_regime in ("STRONG_BULL", "WEAK_BULL"):
                        effective_regime = "BULL"
                    elif ts_regime in ("STRONG_BEAR", "WEAK_BEAR"):
                        effective_regime = "BEAR"
                    else:
                        effective_regime = "NEUTRAL"
    
                    global_regime = effective_regime
                    regime_score = ts
    
                    print(f"\n📈 【机构级趋势评分】TS={ts:+.3f} | {ts_regime} ({ts_regime_label})")
                    print(f"   ├─ 宏观层: TS_macro={ts_details.get('ts_macro', 0):+.3f}")
                    print(f"   ├─ 市场层: TS_market={ts_details.get('ts_market', 0):+.3f}")
                    print(f"   └─ 震荡市: {'是' if is_oscillation else '否'}")
    
                    if direction_refresh_cycle and effective_regime != self._last_regime:
                        confirmed, confirm_state = self._check_transition_confirm(params)
                        if confirmed:
                            print(f"\n🔄 【趋势转换确认】{self._last_regime} → {effective_regime}")
                            if effective_regime == "BEAR":
                                for sym, pos in list(positions_after_close.items()):
                                    if pos and str(pos.get("side", "")).upper() == "LONG":
                                        print(f"🐻 趋势转熊，平掉多单: {sym}")
                                        self._close_position(sym, {"action": "CLOSE", "reason": "trend_score_bear_close_long"}, side="LONG")
                                        self.dca_state.pop(sym, None)
                            elif effective_regime == "BULL":
                                for sym, pos in list(positions_after_close.items()):
                                    if pos and str(pos.get("side", "")).upper() == "SHORT":
                                        print(f"🐂 趋势转牛，平掉空单: {sym}")
                                        self._close_position(sym, {"action": "CLOSE", "reason": "trend_score_bull_close_short"}, side="SHORT")
                                        self.dca_state.pop(sym, None)
                            self._last_regime = effective_regime
                            self._save_dca_state()
                        else:
                            print(f"   ⏳ 趋势转换待确认（当前: {self._last_regime} → 候选: {effective_regime}）")
                            effective_regime = self._last_regime
                else:
                    if direction_refresh_cycle:
                        global_regime, regime_score, regime_details = self._dca_detect_btc_regime(params)
                        major_regime, major_action = self._dca_detect_btc_major_regime(params)
    
                        print(f"\n📈 BTC 牛熊判断: {global_regime} (score={regime_score:+.3f})")
                        for tf, info in regime_details.items():
                            if "error" not in info:
                                print(f"   {tf}: score={info.get('score', 0):+.2f}, EMA20={info.get('ema_fast', 0):.2f}, EMA50={info.get('ema_slow', 0):.2f}")
                        print(f"   🔶 大趋势(4H): {major_regime} [{major_action}]")
    
                        if "TRANSITIONED" in major_action:
                            print(f"\n🔄 大趋势转换确认: {major_regime}")
                            if major_regime == "BEAR":
                                for sym, pos in list(positions_after_close.items()):
                                    if pos and str(pos.get("side", "")).upper() == "LONG":
                                        print(f"🐻 大趋势转熊，平掉多单: {sym}")
                                        self._close_position(sym, {"action": "CLOSE", "reason": "major_regime_bear_close_long"}, side="LONG")
                                        self.dca_state.pop(sym, None)
                            elif major_regime == "BULL":
                                for sym, pos in list(positions_after_close.items()):
                                    if pos and str(pos.get("side", "")).upper() == "SHORT":
                                        print(f"🐂 大趋势转牛，平掉空单: {sym}")
                                        self._close_position(sym, {"action": "CLOSE", "reason": "major_regime_bull_close_short"}, side="SHORT")
                                        self.dca_state.pop(sym, None)
                            self._last_regime = major_regime
                            self._save_dca_state()
                    else:
                        cache_regime_details = self._btc_regime_cache.get("details", {})
                        regime_details = cache_regime_details if isinstance(cache_regime_details, dict) else {}
                        global_regime = str(self._btc_regime_cache.get("regime", "NEUTRAL") or "NEUTRAL")
                        regime_score = float(self._btc_regime_cache.get("score", 0.0) or 0.0)
                        major_regime = str(self._last_regime or "NEUTRAL")
                        major_action = "HOLD(CACHED)"
                        print(f"\n📈 BTC 牛熊判断(缓存): {global_regime} (score={regime_score:+.3f})")
                        print("   ⏱️ 非方向刷新周期：沿用缓存，不触发大趋势转换")
    
                    effective_regime = major_regime
                    is_oscillation = False
    
            # 【持仓上限】根据状态机/震荡市模式调整
            current_regime = str(sm_regime)
            current_engine = self._map_regime_to_engine(current_regime)
            if regime_sm_enabled:
                max_long_positions, max_short_positions = self._get_regime_position_limits_sm(
                    self._regime_sm_ctx.get("regime", "RANGE"), params
                )
                # 【风险倍数】根据状态机状态调整
                current_regime = self._regime_sm_ctx.get("regime", "RANGE")
                current_engine = self._map_regime_to_engine(current_regime)
                regime_risk_mult = self._get_regime_risk_mult(current_regime, params)
            elif is_oscillation:
                osc_mode = params.get("oscillation_mode", {})
                max_long_positions = int(osc_mode.get("max_long", 2))
                max_short_positions = int(osc_mode.get("max_short", 2))
                current_regime = "RANGE"
                current_engine = "RANGE"
                regime_risk_mult = 0.5
                print(f"   📊 震荡市模式：持仓上限调整为 多={max_long_positions}, 空={max_short_positions}")
            else:
                max_long_positions, max_short_positions = self._dca_get_regime_position_limits(effective_regime, params)
                current_regime = str(effective_regime)
                current_engine = self._map_regime_to_engine(current_regime)
                regime_risk_mult = 1.0
            risk_trade_engine = self._resolve_dual_engine(current_engine)
            risk_engine_params_live = self._get_engine_params(params, regime=current_regime, engine=risk_trade_engine)
    
            # 开仓与风险解耦：
            # - 开仓门禁沿用 5m 决策快照（cycle_*）
            # - 风险参数按当前状态机即时收缩（risk_*）
            entry_regime = str(sm_regime or "RANGE").upper()
            entry_trade_engine = cycle_trade_engine
            flow_only_gate_mode = self._coerce_bool(params.get("flow_only_gate_mode", False), False)
            dual_engine_direction_gate_enabled = self._coerce_bool(
                params.get("dual_engine_direction_gate_enabled", not flow_only_gate_mode),
                (not flow_only_gate_mode),
            )
            dual_engine_quota_gate_enabled = self._coerce_bool(
                params.get("dual_engine_quota_gate_enabled", not flow_only_gate_mode),
                (not flow_only_gate_mode),
            )
            if risk_trade_engine != entry_trade_engine:
                print(
                    "ℹ️ 风险/开仓引擎分离："
                    f"entry={entry_trade_engine}({entry_regime}) | "
                    f"risk={risk_trade_engine}({str(current_regime).upper()})"
                )
    
            # 在转换缓冲期内，保持原有持仓限制不变（避免频繁调仓）
            if self._regime_transition_counter > 0:
                self._regime_transition_counter -= 1
                print(f"   🔄 牛熊转换缓冲期 (剩余 {self._regime_transition_counter} 周期)，当前持仓上限保持不变")
                try:
                    max_long_positions = int(params.get("max_long_positions", max_long_positions))
                except Exception:
                    pass
                try:
                    max_short_positions = int(params.get("max_short_positions", max_short_positions))
                except Exception:
                    pass
            
            # 限制最大值
            max_long_positions = max(0, min(MAX_POSITIONS, max_long_positions))
            max_short_positions = max(0, min(MAX_POSITIONS, max_short_positions))
    
            # 宏观偏多保护：RANGE 引擎在 TS 高于阈值时压缩空单配额
            macro_ts = self._to_float(self._trend_score_cache.get("ts", regime_score), 0.0)
            try:
                range_short_lock_enabled = bool(params.get("range_short_lock_enabled", True))
            except Exception:
                range_short_lock_enabled = True
            try:
                range_short_lock_ts_threshold = float(params.get("range_short_lock_ts_threshold", 0.35) or 0.35)
            except Exception:
                range_short_lock_ts_threshold = 0.35
            try:
                range_short_lock_max_short = int(params.get("range_short_lock_max_short", 0) or 0)
            except Exception:
                range_short_lock_max_short = 0
            range_short_lock_active = (
                risk_trade_engine == "RANGE"
                and range_short_lock_enabled
                and macro_ts >= range_short_lock_ts_threshold
            )
            if range_short_lock_active:
                prev_max_short = max_short_positions
                range_short_lock_max_short = max(0, min(MAX_POSITIONS, range_short_lock_max_short))
                max_short_positions = min(max_short_positions, range_short_lock_max_short)
                if max_short_positions < prev_max_short:
                    print(
                        "   🧭 宏观偏多保护触发: "
                        f"TS={macro_ts:+.3f} >= {range_short_lock_ts_threshold:.3f}, "
                        f"RANGE空单上限 {prev_max_short} -> {max_short_positions}"
                    )
    
            # 宏观偏空保护：RANGE 引擎在 TS 低于阈值时压缩多单配额（与 short_lock 对称）
            try:
                range_long_lock_enabled = bool(params.get("range_long_lock_enabled", True))
            except Exception:
                range_long_lock_enabled = True
            try:
                range_long_lock_ts_threshold = float(params.get("range_long_lock_ts_threshold", -0.35) or -0.35)
            except Exception:
                range_long_lock_ts_threshold = -0.35
            try:
                range_long_lock_max_long = int(params.get("range_long_lock_max_long", 0) or 0)
            except Exception:
                range_long_lock_max_long = 0
            range_long_lock_active = (
                risk_trade_engine == "RANGE"
                and range_long_lock_enabled
                and macro_ts <= range_long_lock_ts_threshold
            )
            if range_long_lock_active:
                prev_max_long = max_long_positions
                range_long_lock_max_long = max(0, min(MAX_POSITIONS, range_long_lock_max_long))
                max_long_positions = min(max_long_positions, range_long_lock_max_long)
                if max_long_positions < prev_max_long:
                    print(
                        "   🧭 宏观偏空保护触发: "
                        f"TS={macro_ts:+.3f} <= {range_long_lock_ts_threshold:.3f}, "
                        f"RANGE多单上限 {prev_max_long} -> {max_long_positions}"
                    )
    
            print(f"   📊 持仓上限: 多单={max_long_positions}, 空单={max_short_positions}")
            if flow_only_gate_mode:
                print(
                    f"   📊 引擎: entry={entry_trade_engine} / risk={risk_trade_engine} | 风险倍数: {regime_risk_mult:.2f} | "
                    "FLOW_ONLY开仓门禁(资金流)"
                )
            else:
                print(
                    f"   📊 引擎: entry={entry_trade_engine} / risk={risk_trade_engine} | 风险倍数: {regime_risk_mult:.2f} | "
                    +
                    (
                        "RANGE门禁(均值回归)"
                        if entry_trade_engine == "RANGE"
                        else "TREND门禁(score+pullback)"
                    )
                )
            try:
                max_positions_range = int(params.get("max_positions_range", max(1, MAX_POSITIONS // 2)))
            except Exception:
                max_positions_range = max(1, MAX_POSITIONS // 2)
            try:
                max_positions_trend = int(params.get("max_positions_trend", MAX_POSITIONS))
            except Exception:
                max_positions_trend = MAX_POSITIONS
            max_positions_range = max(0, min(MAX_POSITIONS, max_positions_range))
            max_positions_trend = max(0, min(MAX_POSITIONS, max_positions_trend))
    
            range_open_count = 0
            trend_open_count = 0
            for sym, pos in positions_after_close.items():
                if not pos:
                    continue
                amt = abs(float(pos.get("amount", pos.get("positionAmt", 0)) or 0))
                if amt <= 0:
                    continue
                st = self.dca_state.get(sym, {})
                st_engine = self._resolve_dual_engine(
                    st.get("engine", cycle_trade_engine) if isinstance(st, dict) else cycle_trade_engine
                )
                if st_engine == "UNKNOWN":
                    st_engine = cycle_trade_engine
                if st_engine == "RANGE":
                    range_open_count += 1
                else:
                    trend_open_count += 1
            if dual_engine_quota_gate_enabled:
                print(f"   📊 双引擎配额: RANGE={range_open_count}/{max_positions_range}, TREND={trend_open_count}/{max_positions_trend}")
            else:
                print("   📊 双引擎配额: disabled (FLOW_ONLY)")
    
            if current_count >= MAX_POSITIONS:
                print(f"✋ 已达最大持仓数({current_count}/{MAX_POSITIONS})，不再开新仓（不影响已有仓位管理）")
                allow_open_new = False
    
            # 从候选中筛选AI建议开仓的，按confidence排序
            open_actions = []
            candidate_rule_map = {
                sym: {
                    "score": score,
                    "target_side": side,
                    "threshold": _th,
                }
                for sym, score, _p, side, _qv, _th in open_candidates_raw
            }
            score_gap_gate_enabled = bool(params.get("score_gap_gate_enabled", False))
            try:
                min_score_gap_short = float(params.get("min_score_gap_short", 0.0) or 0.0)
            except Exception:
                min_score_gap_short = 0.0
            try:
                min_score_gap_long = float(params.get("min_score_gap_long", 0.0) or 0.0)
            except Exception:
                min_score_gap_long = 0.0
            flow_signal_enabled = self._coerce_bool(params.get("flow_signal_enabled", False), False)
            flow_signal_allow_no_data_pass = False
            flow_signal_hard_gate = self._coerce_bool(params.get("flow_signal_hard_gate", False), False)
            flow_affect_confidence = self._coerce_bool(params.get("flow_affect_confidence", False), False)
            try:
                flow_signal_interval_seconds = int(params.get("flow_signal_interval_seconds", 15) or 15)
            except Exception:
                flow_signal_interval_seconds = 15
            flow_signal_interval_seconds = max(5, min(300, flow_signal_interval_seconds))
            try:
                _v = params.get("flow_min_hits_short", params.get("flow_min_hits", 2))
                flow_min_hits_short = int(2 if _v is None else _v)
            except Exception:
                flow_min_hits_short = 2
            try:
                _v = params.get("flow_min_hits_long", params.get("flow_min_hits", 2))
                flow_min_hits_long = int(2 if _v is None else _v)
            except Exception:
                flow_min_hits_long = 2
            try:
                _v = params.get("flow_min_score_short", params.get("flow_min_score", 0.35))
                flow_min_score_short = float(0.35 if _v is None else _v)
            except Exception:
                flow_min_score_short = 0.35
            try:
                _v = params.get("flow_min_score_long", params.get("flow_min_score", 0.35))
                flow_min_score_long = float(0.35 if _v is None else _v)
            except Exception:
                flow_min_score_long = 0.35
            stopout_size_reduce_enabled = bool(params.get("stopout_size_reduce_enabled", False))
            try:
                stopout_size_reduce_after = int(params.get("stopout_size_reduce_after", 2) or 2)
            except Exception:
                stopout_size_reduce_after = 2
            try:
                stopout_size_reduce_factor = float(params.get("stopout_size_reduce_factor", 0.5) or 0.5)
            except Exception:
                stopout_size_reduce_factor = 0.5
            stopout_size_reduce_factor = self._clamp_value(stopout_size_reduce_factor, 0.05, 1.0)
            if flow_only_gate_mode:
                print("ⓘ 开仓门禁模式: FLOW_ONLY（资金流门禁生效）")
            else:
                if entry_trade_engine == "RANGE":
                    print("ⓘ 下单前RANGE门禁：均值回归（候选阶段已校验）")
                else:
                    print("ⓘ 下单前TREND门禁: score+pullback")
            print(
                "ⓘ 双擎方向门禁: "
                f"{'on' if dual_engine_direction_gate_enabled else 'off'}"
            )
            print(
                "ⓘ 双擎配额门禁: "
                f"{'on' if dual_engine_quota_gate_enabled else 'off'}"
            )
            if score_gap_gate_enabled:
                print(f"ⓘ score-gap硬门禁: long(score-th)>={min_score_gap_long:.4f}, short(score-th)>={min_score_gap_short:.4f}")
            if stopout_size_reduce_enabled:
                print(
                    f"ⓘ 连续止损减仓: after={stopout_size_reduce_after}, "
                    f"factor={stopout_size_reduce_factor:.2f}, "
                    f"current_stopouts={int(self.consecutive_stopouts or 0)}"
                )
            if flow_signal_enabled:
                print(
                    "ⓘ 资金流门禁: "
                    f"on(ttl={flow_signal_interval_seconds}s, "
                    f"short>={flow_min_score_short:.2f}/{flow_min_hits_short}hits, "
                    f"long>={flow_min_score_long:.2f}/{flow_min_hits_long}hits, "
                    f"no_data_pass={'on' if flow_signal_allow_no_data_pass else 'off'}, "
                    f"mode={'hard' if flow_signal_hard_gate else 'assist'}, "
                    f"affect_conf={'on' if flow_affect_confidence else 'off'})"
                )
            if allow_open_new:
                for symbol in candidate_symbols:
                    decision = multi_decisions.get(symbol)
                    if not decision:
                        continue
                    # RANGE/RANGE_LOCK 的均值回归门禁已在候选阶段执行，这里不再重复判定。
    
                    action = str(decision.get("action", "HOLD")).upper()
                    if action not in ["BUY_OPEN", "SELL_OPEN"]:
                        continue
                    confidence = decision.get("confidence", 0.0)
                    if isinstance(confidence, str):
                        conf_str = confidence.upper()
                        conf_map = {"HIGH": 0.8, "MEDIUM": 0.6, "LOW": 0.4}
                        confidence = conf_map.get(conf_str, 0.5)
                    try:
                        confidence = float(confidence)
                    except Exception:
                        confidence = 0.5
    
                    rule = candidate_rule_map.get(symbol, {})
                    target_side = str(rule.get("target_side", "")).upper()
                    if action == "BUY_OPEN":
                        target_side = "LONG"
                    elif action == "SELL_OPEN":
                        target_side = "SHORT"
                    disable_osc_long_entries = self._coerce_bool(params.get("disable_osc_long_entries", True), True)
                    if (
                        (not flow_only_gate_mode)
                        and entry_trade_engine == "RANGE"
                        and disable_osc_long_entries
                        and target_side == "LONG"
                    ):
                        print(f"⏸️ {symbol} 跳过开仓：RANGE 禁止 LONG + osc_long")
                        continue
    
                    try:
                        score_val = float(rule.get("score", 0) or 0)
                    except Exception:
                        score_val = 0.0
                    try:
                        threshold_val = float(rule.get("threshold", 0) or 0)
                    except Exception:
                        threshold_val = 0.0
                    score_gap_val = score_val - threshold_val
                    if score_gap_gate_enabled:
                        if target_side == "SHORT" and score_gap_val < min_score_gap_short:
                            print(
                                f"⏸️ {symbol} 跳过开仓：score-th={score_gap_val:.4f} < "
                                f"min_score_gap_short={min_score_gap_short:.4f}"
                            )
                            continue
                        if target_side == "LONG" and score_gap_val < min_score_gap_long:
                            print(
                                f"⏸️ {symbol} 跳过开仓：score-th={score_gap_val:.4f} < "
                                f"min_score_gap_long={min_score_gap_long:.4f}"
                            )
                            continue
                    flow_snapshot: Optional[Dict[str, Any]] = None
                    if flow_signal_enabled:
                        flow_params = dict(params)
                        try:
                            eng = decision.get("engine") or decision.get("entry_trade_engine") or decision.get("trade_engine") or decision.get("engine_tag")
                        except Exception:
                            eng = None
                        if eng:
                            flow_params["_flow_engine_hint"] = str(eng).upper()
                        flow_ok, flow_reason, flow_snapshot = self._dca_flow_gate_check(
                            symbol=symbol,
                            side=target_side,
                            params=flow_params,
                        )
                        if isinstance(flow_snapshot, dict):
                            print(f"🌊 {symbol} 资金流: {self._dca_format_flow_brief(flow_snapshot, target_side)}")
                        if not flow_ok and flow_signal_hard_gate:
                            print(f"⏸️ {symbol} 跳过开仓：{flow_reason}")
                            continue
                        if not flow_ok and (not flow_signal_hard_gate):
                            print(f"⚠️ {symbol} 资金流仅作辅助，不拦截开仓：{flow_reason}")
                        # 资金流作为日志层：把 flow brief 写进 reason，便于离线复盘
                        try:
                            base_reason = str(decision.get("reason", "") or "")
                        except Exception:
                            base_reason = ""
                        flow_brief = (
                            self._dca_format_flow_brief(flow_snapshot, target_side)
                            if isinstance(flow_snapshot, dict)
                            else "flow=na"
                        )
                        tag = f"flow({target_side})[{flow_brief}]"
                        if base_reason:
                            decision["reason"] = f"{base_reason} | {tag}"
                        else:
                            decision["reason"] = tag
                    # 执行层拒绝冷却：避免1m循环里反复尝试同一symbol
                    cooldown_left = self._get_execution_reject_cooldown_remaining(symbol)
                    if cooldown_left > 0:
                        print(f"⏸️ {symbol} 执行层拒绝冷却中，剩余 {cooldown_left}s，跳过本轮开仓")
                        continue
    
                    if dual_engine_direction_gate_enabled:
                        if not self._direction_allowed_by_engine(
                            engine=entry_trade_engine,
                            regime=entry_regime,
                            side=target_side,
                        ):
                            print(f"⏸️ {symbol} 跳过开仓：engine={entry_trade_engine} 锁方向，{entry_regime} 不允许 {target_side}")
                            continue
    
                    if target_side == "LONG":
                        if action != "BUY_OPEN":
                            print(f"⏸️ {symbol} 跳过开多：action={action} ≠ BUY_OPEN")
                            continue
                    elif target_side == "SHORT":
                        if action != "SELL_OPEN":
                            print(f"⏸️ {symbol} 跳过开空：action={action} ≠ SELL_OPEN")
                            continue
    
                    open_actions.append((symbol, confidence, score_gap_val, decision))
    
            # 优先按 confidence 排序，相同置信度按 score-threshold 排序
            open_actions.sort(key=lambda x: (x[1], x[2]), reverse=True)
    
            # 开仓直到达到MAX_POSITIONS
            for symbol, conf, score_gap_val, decision in open_actions:
                if current_count >= MAX_POSITIONS:
                    print(f"✋ 已达最大持仓数({current_count}/{MAX_POSITIONS})，停止开仓")
                    break
                if dual_engine_quota_gate_enabled:
                    if entry_trade_engine == "RANGE" and range_open_count >= max_positions_range:
                        print(f"✋ RANGE 引擎配额已满({range_open_count}/{max_positions_range})，停止 RANGE 开仓")
                        break
                    if entry_trade_engine == "TREND" and trend_open_count >= max_positions_trend:
                        print(f"✋ TREND 引擎配额已满({trend_open_count}/{max_positions_trend})，停止 TREND 开仓")
                        break
                action = str(decision.get("action", "")).upper()
                if action == "BUY_OPEN" and current_long_count >= max_long_positions:
                    print(f"⏸️ {symbol} 跳过开多：多单数量已达上限({current_long_count}/{max_long_positions})")
                    continue
                if action == "SELL_OPEN" and current_short_count >= max_short_positions:
                    print(f"⏸️ {symbol} 跳过开空：空单数量已达上限({current_short_count}/{max_short_positions})")
                    continue
    
                # 双引擎开仓附带 TP/SL：按引擎选择参数，避免趋势仓沿用震荡小止盈
                exit_cfg_open = risk_trend_exit if risk_trade_engine == "TREND" else risk_osc_exit
                try:
                    tp_cfg_open = float(exit_cfg_open.get("take_profit_pct", 0) or 0)
                except Exception:
                    tp_cfg_open = 0.0
                try:
                    sl_cfg_open = float(exit_cfg_open.get("symbol_stop_loss_pct", 0) or 0)
                except Exception:
                    sl_cfg_open = 0.0
                if tp_cfg_open > 0:
                    decision["take_profit_percent"] = tp_cfg_open
                if sl_cfg_open > 0:
                    decision["stop_loss_percent"] = -abs(sl_cfg_open)
    
                market_data = self.get_market_data_for_symbol(symbol)
                self.save_decision(symbol, decision, market_data)
                try:
                    # 【风险倍数】根据状态机状态调整仓位
                    decision["regime_risk_mult"] = regime_risk_mult
                    decision["regime"] = current_regime  # 风险 regime（用于 ATR sizing）
                    decision["risk_regime"] = current_regime
                    decision["risk_engine"] = risk_trade_engine
                    decision["engine"] = entry_trade_engine
                    decision["entry_regime"] = decision.get("entry_regime") or entry_regime
                    decision["entry_reason"] = decision.get("entry_reason") or open_candidate_reason.get(symbol, "")
                    base_reason = str(decision.get("reason", "") or "")
                    reason_prefix = (
                        f"entry_engine={entry_trade_engine} entry_regime={entry_regime} "
                        f"risk_engine={risk_trade_engine} risk_regime={str(current_regime).upper()}"
                    )
                    if base_reason:
                        decision["reason"] = (
                            f"{reason_prefix} | {base_reason}"
                            if not ("entry_engine=" in base_reason and "risk_engine=" in base_reason)
                            else base_reason
                        )
                    else:
                        decision["reason"] = reason_prefix
                    try:
                        pos_raw = float(decision.get("position_percent", 0) or 0)
                    except Exception:
                        pos_raw = 0.0
                    pos_ratio = pos_raw / 100.0 if pos_raw > 1.0 else pos_raw
                    try:
                        base_raw = float(decision.get("position_percent_base", pos_ratio) or pos_ratio)
                    except Exception:
                        base_raw = pos_ratio
                    base_ratio = base_raw / 100.0 if base_raw > 1.0 else base_raw
                    try:
                        cap_raw = float(decision.get("position_percent_cap", params.get("max_position_pct", 0.30)) or params.get("max_position_pct", 0.30))
                    except Exception:
                        cap_raw = float(params.get("max_position_pct", 0.30) or 0.30)
                    pos_cap = cap_raw / 100.0 if cap_raw > 1.0 else cap_raw
                    pos_cap = max(0.01, min(0.95, pos_cap))
                    engine_mult = float(risk_engine_params_live.get("position_mult", 1.0))
                    pos_ratio *= engine_mult
                    if (
                        stopout_size_reduce_enabled
                        and stopout_size_reduce_after > 0
                        and int(self.consecutive_stopouts or 0) >= stopout_size_reduce_after
                        and pos_ratio > 0
                    ):
                        reduced_ratio = pos_ratio * stopout_size_reduce_factor
                        print(
                            f"⚠️ {symbol} 连续止损{int(self.consecutive_stopouts or 0)}次，"
                            f"仓位按{stopout_size_reduce_factor:.2f}倍缩放: "
                            f"{pos_ratio * 100:.1f}% -> {reduced_ratio * 100:.1f}%"
                        )
                        pos_ratio = reduced_ratio
                        decision["stopout_size_reduced"] = True
                    if pos_ratio > 0:
                        pos_ratio = max(0.01, min(pos_cap, pos_ratio))
                        decision["position_percent"] = pos_ratio
                        decision["position_percent_base"] = base_ratio
                        decision["position_percent_cap"] = pos_cap
                        decision["engine_position_mult"] = engine_mult
                    print(
                        f"🚀 开仓: {symbol} (confidence={conf:.2%}, score_gap={score_gap_val:.4f}, risk_mult={regime_risk_mult:.2f}, "
                        f"entry={entry_regime}/{entry_trade_engine}, risk={str(current_regime).upper()}/{risk_trade_engine})"
                    )
                    self.execute_decision(symbol, decision, market_data)
                    # 检查是否成功开仓
                    pos_after = self.position_data.get_current_position(symbol)
                    if pos_after and abs(float(pos_after.get("amount", pos_after.get("positionAmt", 0)))) > 0:
                        # 已成功开仓的计划从缓存移除，避免同一 5m 窗口重复尝试
                        if isinstance(self._dca_open_plan_cache, list) and self._dca_open_plan_cache:
                            self._dca_open_plan_cache = [
                                item for item in self._dca_open_plan_cache
                                if str(item.get("symbol", "")).upper() != str(symbol).upper()
                            ]
                        current_count += 1
                        pos_side = str(pos_after.get("side", "")).upper()
                        if pos_side == "LONG":
                            current_long_count += 1
                        elif pos_side == "SHORT":
                            current_short_count += 1
                        state_side = pos_side
                        if state_side not in ("LONG", "SHORT"):
                            state_side = "LONG" if action == "BUY_OPEN" else "SHORT"
                        price = market_data.get("realtime", {}).get("price", 0)
                        self._tag_dca_engine_on_open(
                            symbol,
                            side=state_side,
                            entry_price=float(price or 0),
                            decision=decision,
                            now=now,
                        )
                        if entry_trade_engine == "RANGE":
                            range_open_count += 1
                        else:
                            trend_open_count += 1
                        self.dca_last_entry_time = now
                        self._write_dca_dashboard(positions_after_close)
                except Exception as e:
                    print(f"❌ 开仓失败: {symbol} - {e}")
    
            # per-cycle dashboard refresh
            self._write_dca_dashboard(positions)
            self._refresh_last_positions_snapshot()
    

        _run_decision_and_execution()

    def _get_log_file_path(self) -> str:
        """
        获取当前的日志文件路径
        格式: <logs_dir>/YYYY-MM/YYYY-MM-DD_HH.txt
        每6小时一个文件，每天4个文件
        """
        now = datetime.now()
        year_month = now.strftime("%Y-%m")

        # 计算6小时时段 (00:00-05:59, 06:00-11:59, 12:00-17:59, 18:00-23:59)
        hour_block = (now.hour // 6) * 6

        month_dir = os.path.join(self.logs_dir, year_month)
        os.makedirs(month_dir, exist_ok=True)

        log_filename = f"{now.strftime('%Y-%m-%d')}_{hour_block:02d}.txt"
        log_path = os.path.join(month_dir, log_filename)

        return log_path

    def _get_dca_dashboard_snapshot_path(self, when: Optional[datetime] = None) -> str:
        """
        获取 DCA Dashboard CSV 快照路径
        格式: <logs_dir>/YYYY-MM/DCA_dashboard_YYYY-MM-DD_HH.csv
        与日志文件时间分段一致（每6小时一个文件）
        """
        now = when or datetime.now()
        year_month = now.strftime("%Y-%m")
        hour_block = (now.hour // 6) * 6
        month_dir = os.path.join(self.logs_dir, year_month)
        os.makedirs(month_dir, exist_ok=True)
        snapshot_name = f"DCA_dashboard_{now.strftime('%Y-%m-%d')}_{hour_block:02d}.csv"
        return os.path.join(month_dir, snapshot_name)

    def _sync_dca_dashboard_snapshot(self, when: Optional[datetime] = None) -> None:
        """将当前 dca_dashboard.csv 复制为按时间段命名的快照文件"""
        try:
            now = when or datetime.now()
            snapshot_path = self._get_dca_dashboard_snapshot_path(now)
            if os.path.exists(self.dca_dashboard_csv_path):
                shutil.copyfile(self.dca_dashboard_csv_path, snapshot_path)
                # 保留最近一次快照标记，便于调试（不再用于跳过写入）
                self._last_dca_snapshot_key = snapshot_path
        except Exception as e:
            print(f"⚠️ DCA 看板快照写入失败: {e}")

    def _write_log(self, message: str):
        """
        写入日志到文件
        """
        try:
            log_path = self._get_log_file_path()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(message + "\n")
        except Exception as e:
            print(f"⚠️ 日志写入失败: {e}")

    def get_market_data_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """获取单个币种的市场数据"""
        # 多周期K线 (15m为主要交易周期)
        intervals = ["15m", "30m", "1h", "4h", "1d"]
        multi_timeframe = self.market_data.get_multi_timeframe_data(symbol, intervals)

        # 实时行情
        realtime = self.market_data.get_realtime_market_data(symbol)

        return {
            "symbol": symbol,
            "realtime": realtime or {},
            "multi_timeframe": multi_timeframe,
        }

    def analyze_all_symbols_with_ai(self, all_symbols_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """使用AI一次性分析所有币种"""
        if not self.ai_client or not self.prompt_builder or not self.decision_parser:
            return {}
        # 为静态检查友好，保存局部引用并显式断言
        pb = self.prompt_builder
        ai = self.ai_client
        dp = self.decision_parser
        if pb is None or ai is None or dp is None:
            return {}
        try:
            # 收集所有币种的持仓
            all_positions = {}
            for symbol in all_symbols_data.keys():
                position = self.position_data.get_current_position(symbol)
                if position:
                    all_positions[symbol] = position

            # 获取账户摘要
            account_summary = self.account_data.get_account_summary() or {}

            # 获取历史决策
            history = self.decision_history[-3:] if self.decision_history else []

            # 构建多币种提示词
            prompt = pb.build_multi_symbol_analysis_prompt(
                all_symbols_data=all_symbols_data,
                all_positions=all_positions,
                account_summary=account_summary,
                history=history,
            )

            # 调用AI
            print("\n🤖 调用AI一次性分析所有币种...")
            print(f"\n{'=' * 60}")
            print("📤 发送给AI的完整提示词:")
            print(f"{'=' * 60}")
            print(prompt)
            print(f"{'=' * 60}\n")

            response = ai.analyze_and_decide(prompt)

            # 显示AI推理过程
            try:
                reasoning = ai.get_reasoning(response)
            except Exception:
                reasoning = None

            if reasoning:
                print(f"\n{'=' * 60}")
                print("🧠 AI思维链（详细分析）")
                print(f"{'=' * 60}")
                print(reasoning)
                print(f"{'=' * 60}\n")

            # 显示AI原始回复
            print(f"\n{'=' * 60}")
            print("🤖 AI原始回复:")
            print(f"{'=' * 60}")
            print(response["content"])
            print(f"{'=' * 60}\n")

            # 解析决策
            decisions = self.decision_parser.parse_multi_symbol_response(response["content"])

            # 显示所有决策
            print(f"\n{'=' * 60}")
            print("📊 AI多币种决策总结:")
            print(f"{'=' * 60}")
            for symbol, decision in decisions.items():
                print(f"   {symbol}: {decision['action']} - {decision['reason']}")
            print(f"{'=' * 60}\n")

            return decisions

        except Exception as e:
            print(f"❌ AI分析失败: {e}")
            import traceback

            traceback.print_exc()
            return {}

    def analyze_with_ai(self, symbol: str, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """使用AI分析并获取决策"""
        if not self.ai_client or not self.prompt_builder or not self.decision_parser:
            return DecisionParser._get_default_decision()
        try:
            # 获取持仓
            position = self.position_data.get_current_position(symbol)

            # 获取历史决策（最近3条）
            history = [d for d in self.decision_history if d.get("symbol") == symbol][-3:]

            # 构建提示词
            prompt = self.prompt_builder.build_analysis_prompt(
                symbol=symbol,
                market_data=market_data,
                position=position,
                history=history,
            )

            # 调用AI
            print(f"\n🤖 调用AI分析 {symbol}...")
            response = self.ai_client.analyze_and_decide(prompt)

            # 解析决策
            decision = self.decision_parser.parse_ai_response(response["content"])

            # 显示AI推理过程
            reasoning = self.ai_client.get_reasoning(response)
            if reasoning:
                print(f"\n💭 {symbol} AI推理:")
                print(reasoning)

            # 显示决策
            print(f"\n📊 {symbol} AI决策:")
            print(f"   动作: {decision['action']}")
            print(f"   信心: {decision['confidence']:.2f}")
            print(f"   杠杆: {decision['leverage']}x")
            print(f"   仓位: {decision['position_percent'] * 100:.1f}%")
            print(f"   理由: {decision['reason']}")

            return decision

        except Exception as e:
            print(f"❌ AI分析失败 {symbol}: {e}")
            return self.decision_parser._get_default_decision()

    def analyze_with_strategy(self, symbol: str, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """使用规则策略分析并获取决策"""
        if not self.strategy:
            return DecisionParser._get_default_decision()
        position = self.position_data.get_current_position(symbol)
        decision = self.strategy.decide(symbol, market_data, position)

        print(f"\n📊 {symbol} V5策略决策:")
        print(f"   动作: {decision['action']}")
        print(f"   信心: {decision['confidence']:.2f}")
        print(f"   杠杆: {decision['leverage']}x")
        print(f"   仓位: {decision.get('position_percent', 0) * 100:.1f}%")
        print(f"   理由: {decision['reason']}")

        return decision

    def execute_decision(
        self,
        symbol: str,
        decision: Dict[str, Any],
        market_data: Dict[str, Any],
    ):
        """执行AI决策"""
        action = decision.get("action", "HOLD")
        confidence = decision.get("confidence", 0.5)

        # 确保 confidence 是数字
        if isinstance(confidence, str):
            conf_str = confidence.upper()
            if conf_str == "HIGH":
                confidence = 0.8
            elif conf_str == "MEDIUM":
                confidence = 0.6
            elif conf_str == "LOW":
                confidence = 0.4
            else:
                confidence = 0.5

        # ----- 开仓前执行检查 -----
        # 注：开仓门禁已在候选阶段完成；
        # 这里继续做方向锁、执行层确认与仓位风控。
        try:
            min_pos_raw = float(self.config.get("trading", {}).get("min_position_percent", 10))
        except Exception:
            min_pos_raw = 10.0
        min_pos_ratio = min_pos_raw / 100.0 if min_pos_raw > 1.0 else min_pos_raw
        params_local = self.dca_config.get("params", {}) or {}
        current_regime = str(decision.get("entry_regime", decision.get("regime", "RANGE")) or "RANGE").upper()
        current_engine = self._resolve_dual_engine(decision.get("engine") or self._map_regime_to_engine(current_regime))
        risk_regime = str(decision.get("risk_regime", decision.get("regime", current_regime)) or current_regime).upper()
        risk_engine = self._resolve_dual_engine(
            decision.get("risk_engine") or self._map_regime_to_engine(risk_regime),
            fallback=current_engine,
        )
        if risk_engine == "UNKNOWN":
            risk_engine = current_engine

        if action in ("BUY_OPEN", "SELL_OPEN"):
            desired_side = "LONG" if action == "BUY_OPEN" else "SHORT"
            if not self._direction_allowed_by_engine(
                engine=current_engine,
                regime=current_regime,
                side=desired_side,
            ):
                print(f"⚠️ {symbol} engine={current_engine} 锁方向，{current_regime} 不允许 {desired_side}，跳过执行")
                self._append_trade_log(
                    symbol=symbol,
                    action=action,
                    decision=decision,
                    quantity=0,
                    entry_price=market_data["realtime"].get("price", 0),
                    result="skipped_engine_direction_lock",
                    pnl=None,
                    pnl_percent=None,
                )
                return

            cooldown_left = self._get_execution_reject_cooldown_remaining(symbol)
            if cooldown_left > 0:
                print(f"⏸️ {symbol} 执行层拒绝冷却中，剩余 {cooldown_left}s，跳过执行")
                self._append_trade_log(
                    symbol=symbol,
                    action=action,
                    decision=decision,
                    quantity=0,
                    entry_price=market_data["realtime"].get("price", 0),
                    result="skipped_execution_layer_cooldown",
                    pnl=None,
                    pnl_percent=None,
                )
                return

            if self._is_dual_engine_mode():
                exec_ok, exec_reason, exec_meta = self._dca_execution_layer_confirm(
                    symbol=symbol,
                    action=action,
                    params=params_local,
                    engine=current_engine,
                    regime=current_regime,
                )
                if not exec_ok:
                    print(f"⚠️ {symbol} 执行层过滤未通过: {exec_reason}")
                    self._mark_execution_reject_cooldown(symbol, params_local, exec_reason)
                    self._append_trade_log(
                        symbol=symbol,
                        action=action,
                        decision=decision,
                        quantity=0,
                        entry_price=market_data["realtime"].get("price", 0),
                        result="skipped_execution_layer_filter",
                        pnl=None,
                        pnl_percent=None,
                    )
                    return

                if exec_meta:
                    print(
                        f"   ✅ {symbol} 执行层确认({exec_meta.get('timeframe')}): "
                        f"rsi={exec_meta.get('rsi')}, flags={exec_meta.get('opposite_flags')}"
                    )

        # 如果仓位小于最小阈值且是开仓操作，则视配置决定：跳过或按最小仓位提升
        try:
            pos_raw = float(decision.get("position_percent", 0))
        except Exception:
            pos_raw = 0.0
        pos_ratio = pos_raw / 100.0 if pos_raw > 1.0 else pos_raw
        # 统一写回 ratio，避免后续日志/计算发生单位歧义
        decision["position_percent"] = pos_ratio

        # 【风险倍数说明】
        # regime_risk_mult 已在 _calc_position_size_by_atr 的 risk_amount 层处理
        # 这里不再对 pos_pct 做二次压缩，避免双重收缩
        regime_risk_mult = float(decision.get("regime_risk_mult", 1.0))
        # 仅打印信息，不做额外调整
        if regime_risk_mult < 1.0:
            print(
                f"   📊 状态机风险倍数: {regime_risk_mult:.2f} "
                f"(entry={current_regime}/{current_engine}, risk={risk_regime}/{risk_engine}, 已在ATR sizing层应用)"
            )

        if action in ("BUY_OPEN", "SELL_OPEN") and pos_ratio < min_pos_ratio:
            # 如果开启 AI 门禁并且配置允许 AI 覆盖最小仓位，则将目标仓位提升到最小值
            ai_cfg = self.config.get("ai", {})
            # 默认为允许：在 AI 门控开启时，允许 AI 将目标仓位提升到最小仓位，以避免一致性跳过
            allow_force_min = bool(ai_cfg.get("allow_force_min_position", True))
            if self._dca_ai_gate_enabled() and allow_force_min:
                print(
                    f"⚠️ {symbol} 目标仓位 {pos_ratio * 100:.1f}% 小于最小门槛 {min_pos_ratio * 100:.1f}%，已按配置提升至最小仓位"
                )
                pos_ratio = min_pos_ratio
                try:
                    decision["position_percent"] = pos_ratio
                except Exception:
                    pass
            else:
                print(
                    f"⚠️ {symbol} 目标仓位太小({pos_ratio * 100:.1f}% < {min_pos_ratio * 100:.1f}%), 跳过执行"
                )
                self._append_trade_log(
                    symbol=symbol,
                    action=action,
                    decision=decision,
                    quantity=0,
                    entry_price=market_data["realtime"].get("price", 0),
                    result="skipped_small_position",
                    pnl=None,
                    pnl_percent=None,
                )
                return

        # 读取最大仓位并对目标仓位进行上限约束：
        # 优先 decision.position_percent_cap，其次 dca params.max_position_pct，最后 trading.max_position_percent
        try:
            max_pos_raw = float(decision.get("position_percent_cap", 0) or 0)
        except Exception:
            max_pos_raw = 0.0
        if max_pos_raw <= 0:
            try:
                dca_params = self.dca_config.get("params", {}) if isinstance(getattr(self, "dca_config", {}), dict) else {}
                max_pos_raw = float(dca_params.get("max_position_pct", 0) or 0)
            except Exception:
                max_pos_raw = 0.0
        if max_pos_raw <= 0:
            try:
                max_pos_raw = float(self.config.get("trading", {}).get("max_position_percent", 30))
            except Exception:
                max_pos_raw = 30.0
        max_pos_ratio = max_pos_raw / 100.0 if max_pos_raw > 1.0 else max_pos_raw

        if pos_ratio > max_pos_ratio:
            print(
                f"⚠️ {symbol} 目标仓位({pos_ratio * 100:.1f}%) 超过最大允许仓位({max_pos_ratio * 100:.1f}%), 已按上限截断"
            )
            pos_ratio = max_pos_ratio
            # 同步回 decision 以便日志与后续逻辑一致
            try:
                decision["position_percent"] = pos_ratio
            except Exception:
                pass

        try:
            # 获取账户信息
            account_summary = self.account_data.get_account_summary()
            if not account_summary:
                print(f"⚠️ {symbol} 无法获取账户信息")
                return

            total_equity = account_summary["equity"]

            # 获取当前价格
            current_price = market_data["realtime"].get("price", 0)
            if current_price == 0:
                print(f"⚠️ {symbol} 无法获取当前价格")
                return

            if action == "BUY_OPEN":
                # 开多仓
                self._open_long(symbol, decision, total_equity, current_price)

            elif action == "SELL_OPEN":
                # 开空仓
                # 对于开空，规范化 take_profit_percent 签名（用户输入常为正数，语义上对空应为负）
                try:
                    tp_pct = float(decision.get("take_profit_percent", 0))
                except Exception:
                    tp_pct = 0.0
                if tp_pct > 0:
                    decision["take_profit_percent"] = -abs(tp_pct)

                self._open_short(symbol, decision, total_equity, current_price)

            elif action == "CLOSE":
                # 平仓
                close_side = self._normalize_position_side(decision.get("position_side"))
                if close_side is None:
                    close_side = self._normalize_position_side(decision.get("side"))
                if close_side is None:
                    try:
                        current_pos = self.position_data.get_current_position(symbol)
                    except Exception:
                        current_pos = None
                    if isinstance(current_pos, dict):
                        close_side = self._normalize_position_side(current_pos.get("side"))
                res = self._close_position(symbol, decision, side=close_side)
                # 记录平仓到交易日志（如有返回结果与 pnl）
                try:
                    pnl = None
                    pnl_percent = None
                    quantity = 0.0
                    entry_price_for_log = current_price
                    if isinstance(res, dict):
                        pnl = res.get("pnl")
                        if pnl is None:
                            pnl = res.get("profit")
                        pnl_percent = res.get("pnl_percent")
                        quantity = float(res.get("quantity", 0) or 0)
                        entry_price_for_log = float(res.get("entry_price", current_price) or current_price)
                        result_text = str(res.get("status") or "unknown")
                    else:
                        result_text = str(res)
                    self._append_trade_log(
                        symbol=symbol,
                        action=action,
                        decision=decision,
                        quantity=quantity,
                        entry_price=entry_price_for_log,
                        result=result_text,
                        pnl=pnl,
                        pnl_percent=pnl_percent,
                    )
                except Exception:
                    pass

            elif action == "HOLD":
                # 持有
                print(f"💤 {symbol} 保持现状")

        except Exception as e:
            print(f"❌ 执行决策失败 {symbol}: {e}")

    def _open_long(
        self,
        symbol: str,
        decision: Dict[str, Any],
        total_equity: float,
        current_price: float,
    ):
        """开多仓（修正版）"""
        position_ratio = float(decision.get("position_percent", 0))
        # position_ratio 应为 0~1 比例，兼容旧配置（>1 视为百分比）
        if position_ratio > 1.0:
            position_ratio = position_ratio / 100.0
        
        # 限制仓位范围到配置允许的范围 [min_ratio, max_ratio]
        try:
            min_pos_ratio = float(self.config.get("trading", {}).get("min_position_percent", 10)) / 100.0
        except Exception:
            min_pos_ratio = 0.10
        try:
            max_pos_ratio = float(self.config.get("trading", {}).get("max_position_percent", 50)) / 100.0
        except Exception:
            max_pos_ratio = 0.50
        
        if position_ratio < min_pos_ratio and position_ratio > 0:
            print(f"⚠️ {symbol} 目标仓位({position_ratio * 100:.1f}%) 低于最小仓位({min_pos_ratio * 100:.1f}%), 已提升到最小值")
            position_ratio = min_pos_ratio
            decision["position_percent"] = position_ratio
        if position_ratio > max_pos_ratio:
            print(f"⚠️ {symbol} 目标仓位({position_ratio * 100:.1f}%) 超过最大允许仓位({max_pos_ratio * 100:.1f}%), 已按上限截断")
            position_ratio = max_pos_ratio
            try:
                decision["position_percent"] = position_ratio
            except Exception:
                pass
        if position_ratio <= 0:
            print(f"⚠️ {symbol} 目标仓位为0，跳过开仓")
            return

        # 检查账户余额
        if total_equity <= 0:
            print(f"⚠️ {symbol} 账户余额为0，无法开仓")
            print("   请确保账户有足够的 USDT 余额")
            return

        # 计算开仓数量（ATR风险闭环）
        params_local = self.dca_config.get("params", {}) if hasattr(self, "dca_config") else {}
        current_regime = str(decision.get("regime", "RANGE"))
        quantity, qty_details = self._calculate_order_quantity(
            symbol, position_ratio, total_equity, current_price,
            params=params_local, regime=current_regime
        )
        if qty_details.get("atr_details"):
            atr_d = qty_details["atr_details"]
            print(f"   📊 ATR sizing: atr_notional={qty_details.get('atr_notional', 'n/a')}, "
                  f"risk_amount={atr_d.get('risk_amount', 'n/a')}, regime_risk_mult={atr_d.get('regime_risk_mult', 1.0)}")
        if qty_details.get("final_notional_source") == "atr_capped":
            print(f"   📊 最终仓位被ATR风险预算约束: {qty_details.get('final_notional')} USDT")
        if quantity <= 0:
            print(f"❌ {symbol} 计算出的数量无效: {quantity}")
            return

        try:
            leverage = int(float(decision.get("leverage", 1)))
        except Exception:
            leverage = 5
        if self._is_dual_engine_mode():
            leverage = max(5, min(12, leverage))
        # 默认遵循用户建议：建议止盈 +14%，最大止损 0.6%
        take_profit_percent = decision.get("take_profit_percent", 14.0)
        stop_loss_percent = decision.get("stop_loss_percent", -0.6)

        def _normalize_pct(val: Any, default: float) -> float:
            try:
                v = float(val)
            except Exception:
                return default
            if v == 0:
                return 0.0
            sign = -1.0 if v < 0 else 1.0
            v = abs(v)
            if v > 1.0:
                v = v / 100.0
            return sign * v

        tp_pct = _normalize_pct(take_profit_percent, 0.14)
        sl_pct = _normalize_pct(stop_loss_percent, -0.006)
        # 支持基于 ATR 的止损（使用 ConfigLoader.get_atr_config 统一读取）
        atr_cfg = ConfigLoader.get_atr_config(self.config)
        use_atr = bool(atr_cfg.get("use_atr_stop_loss", False))
        atr_multiplier = float(atr_cfg.get("atr_multiplier", 3.0))
        atr_tf = str(atr_cfg.get("atr_timeframe", self.config.get("strategy", {}).get("interval", "1h")))
        if use_atr:
            try:
                multi = self.market_data.get_multi_timeframe_data(symbol, [atr_tf])
                atr_val = None
                if multi and atr_tf in multi and "indicators" in multi[atr_tf]:
                    atr_val = multi[atr_tf]["indicators"].get("atr_14")
                if atr_val and atr_val > 0:
                    # long: SL = price - atr * mult
                    sl_price_atr = current_price - atr_val * atr_multiplier
                    computed_sl_pct = (sl_price_atr / current_price) - 1.0
                    # only use ATR SL if it's a meaningful move (not tiny)
                    if abs(computed_sl_pct) > abs(sl_pct):
                        sl_pct = computed_sl_pct
                        try:
                            decision["stop_loss_percent"] = sl_pct
                        except Exception:
                            pass
            except Exception:
                pass
        # 强制最大止损绝对值（使用 ConfigLoader 统一规范化为分数，例如 0.006 表示 0.6%）
        try:
            max_sl_abs = ConfigLoader.get_max_stop_loss_abs(self.config)
        except Exception:
            max_sl_abs = 0.006
        if abs(sl_pct) > max_sl_abs:
            print(f"⚠️ {symbol} 止损阈值 {sl_pct * 100:.2f}% 超过最大允许 {max_sl_abs * 100:.2f}%, 已截断")
            sl_pct = -abs(max_sl_abs)
            try:
                decision["stop_loss_percent"] = sl_pct
            except Exception:
                pass
        take_profit = current_price * (1 + tp_pct)
        stop_loss = current_price * (1 + sl_pct)

        # 风险检查
        ok, errors = self.risk_manager.check_all_risk_limits(
            symbol,
            quantity,
            current_price,
            total_equity,
            total_equity,
        )
        if not ok:
            print(f"❌ {symbol} 风控检查失败:")
            for err in errors:
                print(f"   - {err}")
            return

        try:
            # ⚠️ 强制传递数量给 TradeExecutor
            res = self.trade_executor.open_long(
                symbol=symbol,
                quantity=quantity,
                leverage=leverage,
                take_profit=take_profit,
                stop_loss=stop_loss,
            )
            # 检查返回结果中的 status
            if res.get("status") == "error":
                print(f"❌ {symbol} 开多仓失败: {res.get('message', '未知错误')}")
                try:
                    self._append_trade_log(
                        symbol=symbol,
                        action="BUY_OPEN",
                        decision=decision,
                        quantity=0.0,
                        entry_price=current_price,
                        result=str(res.get("status", "error")),
                        pnl=None,
                        pnl_percent=None,
                    )
                except Exception:
                    pass
                self._record_dca_trade_event(
                    event_type="OPEN_LONG",
                    symbol=symbol,
                    side="LONG",
                    status=str(res.get("status", "error")),
                    quantity=quantity,
                    price=current_price,
                    reason=str(decision.get("reason", "")),
                )
            else:
                print(f"✅ {symbol} 开多仓成功: {self._summarize_open_result(res)}")
                if os.getenv("BINANCE_VERBOSE_OPEN_RESULT") == "1":
                    print(f"   details: {res}")
                self.trade_count += 1
                try:
                    self._tag_dca_engine_on_open(symbol, side="LONG", entry_price=current_price, decision=decision)
                except Exception:
                    pass
                try:
                    self._append_trade_log(
                        symbol=symbol,
                        action="BUY_OPEN",
                        decision=decision,
                        quantity=quantity,
                        entry_price=current_price,
                        result=str(res.get("status", "success")),
                        pnl=None,
                        pnl_percent=None,
                    )
                except Exception:
                    pass
                self._record_dca_trade_event(
                    event_type="OPEN_LONG",
                    symbol=symbol,
                    side="LONG",
                    status=str(res.get("status", "success")),
                    quantity=quantity,
                    price=current_price,
                    reason=str(decision.get("reason", "")),
                )
        except Exception as e:
            print(f"❌ {symbol} 开多仓失败: {e}")
            try:
                self._append_trade_log(
                    symbol=symbol,
                    action="BUY_OPEN",
                    decision=decision,
                    quantity=0.0,
                    entry_price=current_price,
                    result="error",
                    pnl=None,
                    pnl_percent=None,
                )
            except Exception:
                pass
            self._record_dca_trade_event(
                event_type="OPEN_LONG",
                symbol=symbol,
                side="LONG",
                status="error",
                quantity=quantity,
                price=current_price,
                reason=f"{decision.get('reason', '')} | {e}",
            )

    def _open_short(
        self,
        symbol: str,
        decision: Dict[str, Any],
        total_equity: float,
        current_price: float,
    ):
        """开空仓（修正版）"""
        position_ratio = float(decision.get("position_percent", 0))
        # position_ratio 应为 0~1 比例，兼容旧配置（>1 视为百分比）
        if position_ratio > 1.0:
            position_ratio = position_ratio / 100.0
        
        # 限制仓位范围到配置允许的范围 [min_ratio, max_ratio]
        try:
            min_pos_ratio = float(self.config.get("trading", {}).get("min_position_percent", 10)) / 100.0
        except Exception:
            min_pos_ratio = 0.10
        try:
            max_pos_ratio = float(self.config.get("trading", {}).get("max_position_percent", 50)) / 100.0
        except Exception:
            max_pos_ratio = 0.50
        
        if position_ratio < min_pos_ratio and position_ratio > 0:
            print(f"⚠️ {symbol} 目标仓位({position_ratio * 100:.1f}%) 低于最小仓位({min_pos_ratio * 100:.1f}%), 已提升到最小值")
            position_ratio = min_pos_ratio
            decision["position_percent"] = position_ratio
        if position_ratio > max_pos_ratio:
            print(f"⚠️ {symbol} 目标仓位({position_ratio * 100:.1f}%) 超过最大允许仓位({max_pos_ratio * 100:.1f}%), 已按上限截断")
            position_ratio = max_pos_ratio
            try:
                decision["position_percent"] = position_ratio
            except Exception:
                pass
        if position_ratio <= 0:
            print(f"⚠️ {symbol} 目标仓位为0，跳过开空仓")
            return

        # 检查账户余额
        if total_equity <= 0:
            print(f"⚠️ {symbol} 账户余额为0，无法开仓")
            print("   请确保账户有足够的 USDT 余额")
            return

        # 计算开仓数量（ATR风险闭环）
        params_local = self.dca_config.get("params", {}) if hasattr(self, "dca_config") else {}
        current_regime = str(decision.get("regime", "RANGE"))
        quantity, qty_details = self._calculate_order_quantity(
            symbol, position_ratio, total_equity, current_price,
            params=params_local, regime=current_regime
        )
        if qty_details.get("atr_details"):
            atr_d = qty_details["atr_details"]
            print(f"   📊 ATR sizing: atr_notional={qty_details.get('atr_notional', 'n/a')}, "
                  f"risk_amount={atr_d.get('risk_amount', 'n/a')}, regime_risk_mult={atr_d.get('regime_risk_mult', 1.0)}")
        if qty_details.get("final_notional_source") == "atr_capped":
            print(f"   📊 最终仓位被ATR风险预算约束: {qty_details.get('final_notional')} USDT")
        if quantity <= 0:
            print(f"❌ {symbol} 计算出的数量无效: {quantity}")
            return

        try:
            leverage = int(float(decision.get("leverage", 1)))
        except Exception:
            leverage = 5
        if self._is_dual_engine_mode():
            leverage = max(5, min(12, leverage))
        # 默认遵循用户建议：建议止盈 +14%，最大止损 0.6%
        take_profit_percent = decision.get("take_profit_percent", 14.0)
        stop_loss_percent = decision.get("stop_loss_percent", -0.6)

        def _normalize_pct(val: Any, default: float) -> float:
            try:
                v = float(val)
            except Exception:
                return default
            if v == 0:
                return 0.0
            sign = -1.0 if v < 0 else 1.0
            v = abs(v)
            if v > 1.0:
                v = v / 100.0
            return sign * v

        tp_pct = _normalize_pct(take_profit_percent, 0.14)
        sl_pct = _normalize_pct(stop_loss_percent, -0.006)
        # 对于空头，止损的语义可能为正或负，统一取绝对值并限制在 max_sl_abs
        max_sl_abs_raw = self.config.get("trading", {}).get("max_stop_loss_abs", 0.6)
        max_sl_abs = _normalize_pct(max_sl_abs_raw, 0.006)
        if abs(sl_pct) > max_sl_abs:
            print(f"⚠️ {symbol} 止损阈值 {sl_pct * 100:.2f}% 超过最大允许 {max_sl_abs * 100:.2f}%, 已截断")
            sl_pct = max_sl_abs if sl_pct > 0 else -max_sl_abs
            try:
                decision["stop_loss_percent"] = sl_pct
            except Exception:
                pass
        tp_abs = abs(tp_pct)
        # 做空止盈位在当前价下方
        take_profit = current_price * (1 - tp_abs)
        # 做空止损位在当前价上方
        stop_loss = current_price * (1 + abs(sl_pct))

        # 风险检查
        ok, errors = self.risk_manager.check_all_risk_limits(
            symbol,
            quantity,
            current_price,
            total_equity,
            total_equity,
        )
        if not ok:
            print(f"❌ {symbol} 风控检查失败:")
            for err in errors:
                print(f"   - {err}")
            return

        try:
            res = self.trade_executor.open_short(
                symbol=symbol,
                quantity=quantity,
                leverage=leverage,
                take_profit=take_profit,
                stop_loss=stop_loss,
            )
            # 检查返回结果中的 status
            if res.get("status") == "error":
                print(f"❌ {symbol} 开空仓失败: {res.get('message', '未知错误')}")
                try:
                    self._append_trade_log(
                        symbol=symbol,
                        action="SELL_OPEN",
                        decision=decision,
                        quantity=0.0,
                        entry_price=current_price,
                        result=str(res.get("status", "error")),
                        pnl=None,
                        pnl_percent=None,
                    )
                except Exception:
                    pass
                self._record_dca_trade_event(
                    event_type="OPEN_SHORT",
                    symbol=symbol,
                    side="SHORT",
                    status=str(res.get("status", "error")),
                    quantity=quantity,
                    price=current_price,
                    reason=str(decision.get("reason", "")),
                )
            else:
                print(f"✅ {symbol} 开空仓成功: {self._summarize_open_result(res)}")
                if os.getenv("BINANCE_VERBOSE_OPEN_RESULT") == "1":
                    print(f"   details: {res}")
                self.trade_count += 1
                try:
                    self._tag_dca_engine_on_open(symbol, side="SHORT", entry_price=current_price, decision=decision)
                except Exception:
                    pass
                try:
                    self._append_trade_log(
                        symbol=symbol,
                        action="SELL_OPEN",
                        decision=decision,
                        quantity=quantity,
                        entry_price=current_price,
                        result=str(res.get("status", "success")),
                        pnl=None,
                        pnl_percent=None,
                    )
                except Exception:
                    pass
                self._record_dca_trade_event(
                    event_type="OPEN_SHORT",
                    symbol=symbol,
                    side="SHORT",
                    status=str(res.get("status", "success")),
                    quantity=quantity,
                    price=current_price,
                    reason=str(decision.get("reason", "")),
                )
        except Exception as e:
            print(f"❌ {symbol} 开空仓失败: {e}")
            try:
                self._append_trade_log(
                    symbol=symbol,
                    action="SELL_OPEN",
                    decision=decision,
                    quantity=0.0,
                    entry_price=current_price,
                    result="error",
                    pnl=None,
                    pnl_percent=None,
                )
            except Exception:
                pass
            self._record_dca_trade_event(
                event_type="OPEN_SHORT",
                symbol=symbol,
                side="SHORT",
                status="error",
                quantity=quantity,
                price=current_price,
                reason=f"{decision.get('reason', '')} | {e}",
            )

    def _summarize_open_result(self, res: Dict[str, Any]) -> str:
        """精简开仓结果日志，避免整包对象刷屏。"""
        if not isinstance(res, dict):
            return str(res)
        status = str(res.get("status", "unknown"))
        raw_open = res.get("open")
        open_part: Dict[str, Any] = raw_open if isinstance(raw_open, dict) else {}
        open_status = open_part.get("status") or open_part.get("strategyStatus") or open_part.get("warning") or "n/a"
        order_id = open_part.get("orderId", "n/a")
        qty = open_part.get("origQty", open_part.get("executedQty", "n/a"))
        filled = open_part.get("executedQty", "n/a")

        raw_protection = res.get("protection")
        protection_part: Dict[str, Any] = raw_protection if isinstance(raw_protection, dict) else {}
        protection_status = "n/a"
        protection_orders = 0
        if protection_part:
            protection_status = str(protection_part.get("status", "n/a"))
            nested = protection_part.get("orders")
            if isinstance(nested, dict):
                nested_status = nested.get("status")
                if nested_status:
                    protection_status = str(nested_status)
                nested_orders = nested.get("orders")
                if isinstance(nested_orders, list):
                    protection_orders = len(nested_orders)
            elif isinstance(nested, list):
                protection_orders = len(nested)

        return (
            f"status={status}, open_status={open_status}, orderId={order_id}, "
            f"qty={qty}, filled={filled}, protection={protection_status}, "
            f"protection_orders={protection_orders}"
        )

    def _calculate_order_quantity(
        self,
        symbol: str,
        position_percent: float,
        total_equity: float,
        current_price: float,
        params: Optional[Dict[str, Any]] = None,
        regime: str = "RANGE",
    ) -> Tuple[float, Dict[str, Any]]:
        """
        根据目标仓位与价格计算并校验数量（机构级风险闭环）

        核心逻辑：
        - atr_notional = ATR sizing 计算的风险预算（硬约束）
        - pct_notional = position_ratio × equity（策略上限/意愿）
        - final_notional = min(atr_notional, pct_notional)

        参数说明：
        - position_ratio: 0~1 比例，如 0.45 表示 45%

        Returns:
            Tuple[float, Dict]: (quantity, details)
        """
        details: Dict[str, Any] = {}
        if position_percent <= 0:
            return 0.0, details
        if current_price <= 0 or total_equity <= 0:
            return 0.0, details

        # 统一：position_ratio 应为 0~1 比例
        # 兼容：如果传入 > 1，视为百分比，自动转换
        position_ratio = position_percent / 100.0 if position_percent > 1.0 else position_percent

        # 策略上限名义价值（position_ratio 方式）
        pct_notional = total_equity * position_ratio
        details["pct_notional"] = round(pct_notional, 2)
        details["position_ratio"] = position_ratio

        # ATR 风险预算名义价值（如果提供了 params）
        atr_notional = None
        if params:
            atr_notional, atr_details = self._calc_position_size_by_atr(symbol, params, regime)
            details["atr_details"] = atr_details
            details["atr_notional"] = round(atr_notional, 2)

        # 最终名义价值：min(atr_notional, pct_notional)
        if atr_notional is not None and atr_notional > 0:
            final_notional = min(atr_notional, pct_notional)
            details["final_notional_source"] = "atr_capped" if atr_notional < pct_notional else "pct_capped"
        else:
            final_notional = pct_notional
            details["final_notional_source"] = "pct_only"
        details["final_notional"] = round(final_notional, 2)

        if final_notional <= 0:
            return 0.0, details

        raw_quantity = final_notional / current_price
        if raw_quantity <= 0:
            return 0.0, details

        # 先按原始数量做最小名义价值校正，再格式化，避免先floor到0后无法恢复
        quantity = self.client.ensure_min_notional_quantity(symbol, raw_quantity, current_price)
        quantity = self.client.format_quantity(symbol, quantity)
        details["quantity"] = quantity
        details["raw_quantity"] = raw_quantity
        return quantity, details

    def _calc_tp_sl_prices(
        self,
        side: str,
        current_price: float,
        params: Dict[str, Any],
    ) -> Tuple[Optional[float], Optional[float]]:
        """基于 DCA 参数计算 TP/SL 价格（如未配置则返回 None）"""
        try:
            tp_pct = float(params.get("take_profit_pct", 0))
        except Exception:
            tp_pct = 0.0
        try:
            sl_pct = float(params.get("symbol_stop_loss_pct", 0))
        except Exception:
            sl_pct = 0.0
        try:
            rr_ratio = float(params.get("rr_ratio", 1.0))
        except Exception:
            rr_ratio = 1.0
        rr_force = bool(params.get("rr_force", False))

        if current_price <= 0 or (tp_pct <= 0 and sl_pct <= 0):
            return None, None

        side = str(side).upper()

        # 先计算止损
        if side == "SHORT":
            sl = current_price * (1 + sl_pct) if sl_pct > 0 else None
        else:
            sl = current_price * (1 - sl_pct) if sl_pct > 0 else None

        # 强制 RR：若开启 rr_force 且 sl 可用，则用 RR 反算 TP
        if rr_force and sl is not None:
            risk = abs(current_price - sl)
            if side == "SHORT":
                tp = current_price - risk * rr_ratio
            else:
                tp = current_price + risk * rr_ratio
        else:
            # 非强制：按配置的 tp_pct 计算
            if side == "SHORT":
                tp = current_price * (1 - tp_pct) if tp_pct > 0 else None
            else:
                tp = current_price * (1 + tp_pct) if tp_pct > 0 else None

        if tp is not None and tp <= 0:
            tp = None
        if sl is not None and sl <= 0:
            sl = None
        return tp, sl

    def _append_trade_log(
        self,
        symbol: str,
        action: str,
        decision: Dict[str, Any],
        quantity: float,
        entry_price: float,
        result: str,
        pnl: Optional[float],
        pnl_percent: Optional[float] = None,
    ):
        """将交易信息追加到 CSV 日志，便于离线统计"""
        try:
            now = datetime.now()
            month_dir = os.path.join(self.logs_dir, now.strftime("%Y-%m"))
            os.makedirs(month_dir, exist_ok=True)
            csv_path = os.path.join(month_dir, "trade_log.csv")
            header = [
                "timestamp",
                "symbol",
                "action",
                "confidence",
                "leverage",
                "position_percent",
                "quantity",
                "entry_price",
                "take_profit",
                "stop_loss",
                "result",
                "pnl",
                "pnl_percent",
                "reason",
            ]
            exists = os.path.exists(csv_path)
            if exists:
                try:
                    with open(csv_path, "r", newline="", encoding="utf-8") as rf:
                        rows = list(csv.reader(rf))
                    if rows:
                        current_header = rows[0]
                        if "pnl_percent" not in current_header:
                            expected_len = len(header)
                            rows[0] = header
                            for idx in range(1, len(rows)):
                                row = list(rows[idx])
                                if len(row) < expected_len:
                                    row.extend([""] * (expected_len - len(row)))
                                rows[idx] = row[:expected_len]
                            with open(csv_path, "w", newline="", encoding="utf-8") as wf:
                                writer = csv.writer(wf)
                                writer.writerows(rows)
                except Exception as migrate_err:
                    print(f"⚠️ trade_log.csv 迁移失败: {migrate_err}")
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not exists:
                    writer.writerow(header)
                writer.writerow(
                    [
                        datetime.now().isoformat(),
                        symbol,
                        action,
                        decision.get("confidence"),
                        decision.get("leverage"),
                        decision.get("position_percent"),
                        quantity,
                        entry_price,
                        decision.get("take_profit_percent"),
                        decision.get("stop_loss_percent"),
                        result,
                        pnl,
                        pnl_percent,
                        decision.get("reason"),
                    ]
                )
        except Exception as e:
            print(f"⚠️ 写入交易日志失败: {e}")

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _normalize_position_side(value: Any) -> Optional[str]:
        raw = str(value or "").upper()
        if raw in ("LONG", "SHORT"):
            return raw
        if raw in ("BUY", "BULL", "LONG_OPEN"):
            return "LONG"
        if raw in ("SELL", "BEAR", "SHORT_OPEN"):
            return "SHORT"
        return None

    def _snapshot_position_for_close(self, symbol: str, side: Optional[str] = None) -> Optional[Dict[str, Any]]:
        side_query = self._normalize_position_side(side)

        def _from_exchange(raw_pos: Optional[Dict[str, Any]], fallback_side: Optional[str]) -> Optional[Dict[str, Any]]:
            if not isinstance(raw_pos, dict):
                return None
            position_amt = self._to_float(raw_pos.get("positionAmt", 0))
            if abs(position_amt) <= 0:
                return None
            position_side = self._normalize_position_side(raw_pos.get("positionSide")) or fallback_side
            if position_side not in ("LONG", "SHORT"):
                position_side = "LONG" if position_amt > 0 else "SHORT"
            return {
                "side": position_side,
                "amount": abs(position_amt),
                "entry_price": self._to_float(raw_pos.get("entryPrice", 0)),
                "mark_price": self._to_float(raw_pos.get("markPrice", 0)),
            }

        query_sides: List[Optional[str]]
        if side_query:
            query_sides = [side_query]
        else:
            query_sides = [None, "LONG", "SHORT"]

        for q_side in query_sides:
            try:
                raw = self.client.get_position(symbol, side=q_side) if q_side else self.client.get_position(symbol)
            except Exception:
                raw = None
            snapshot = _from_exchange(raw, q_side)
            if snapshot:
                return snapshot

        # 兜底：从 PositionDataManager 补充，避免平仓日志缺少关键字段
        try:
            pos = self.position_data.get_current_position(symbol)
        except Exception:
            pos = None
        if isinstance(pos, dict):
            amount = self._to_float(pos.get("amount", pos.get("positionAmt", 0)))
            if amount > 0:
                side_fallback = self._normalize_position_side(pos.get("side")) or side_query or "UNKNOWN"
                return {
                    "side": side_fallback,
                    "amount": amount,
                    "entry_price": self._to_float(pos.get("entry_price", pos.get("entryPrice", 0))),
                    "mark_price": self._to_float(pos.get("mark_price", pos.get("markPrice", 0))),
                }
        return None

    def _parse_close_metrics(
        self,
        symbol: str,
        pre_position: Optional[Dict[str, Any]],
        close_result: Any,
    ) -> Dict[str, Any]:
        status = "unknown"
        message = ""
        order_wrapper: Dict[str, Any] = {}
        order: Dict[str, Any] = {}
        if isinstance(close_result, dict):
            status = str(close_result.get("status", "unknown"))
            message = str(close_result.get("message", "") or "")
            if isinstance(close_result.get("order"), dict):
                order_wrapper = close_result.get("order") or {}
                order = order_wrapper
                if isinstance(order_wrapper.get("order"), dict):
                    # 兼容 {"status":"closed","order":{"status":"success","order":{...}}}
                    order = order_wrapper.get("order") or {}
                if str(order_wrapper.get("status", "")).lower() == "error" and status not in ("error", "noop"):
                    status = "error"
                    if not message:
                        message = str(order_wrapper.get("message", "") or "")
                if str(order.get("status", "")).lower() == "error" and status not in ("error", "noop"):
                    status = "error"
                    if not message:
                        message = str(order.get("message", "") or "")

            if status == "success":
                msg_l = message.lower()
                if "no " in msg_l and "position" in msg_l:
                    status = "noop"
                if "position is zero" in msg_l:
                    status = "noop"

        side = (pre_position or {}).get("side")
        if not side and order:
            order_side = self._normalize_position_side(order.get("positionSide"))
            if order_side in ("LONG", "SHORT"):
                side = order_side
            else:
                side_hint = self._normalize_position_side(order_wrapper.get("positionSide"))
                if side_hint in ("LONG", "SHORT"):
                    side = side_hint
                elif str(order.get("side", "")).upper() == "SELL":
                    side = "LONG"
                elif str(order.get("side", "")).upper() == "BUY":
                    side = "SHORT"
                elif str(order_wrapper.get("side", "")).upper() == "SELL":
                    side = "LONG"
                elif str(order_wrapper.get("side", "")).upper() == "BUY":
                    side = "SHORT"

        qty = self._to_float(order.get("executedQty", 0))
        if qty <= 0:
            qty = self._to_float(order.get("cumQty", 0))
        if qty <= 0:
            qty = self._to_float(order.get("origQty", 0))
        if qty <= 0:
            qty = self._to_float(order_wrapper.get("executedQty", 0))
        if qty <= 0:
            qty = self._to_float(order_wrapper.get("cumQty", 0))
        if qty <= 0:
            qty = self._to_float(order_wrapper.get("origQty", 0))
        if qty <= 0 and pre_position:
            qty = self._to_float(pre_position.get("amount", 0))

        close_price = self._to_float(order.get("avgPrice", 0))
        if close_price <= 0:
            close_price = self._to_float(order.get("price", 0))
        if close_price <= 0:
            cum_quote = self._to_float(order.get("cumQuote", 0))
            if cum_quote > 0 and qty > 0:
                close_price = cum_quote / qty
        if close_price <= 0:
            close_price = self._to_float(order_wrapper.get("avgPrice", 0))
        if close_price <= 0:
            close_price = self._to_float(order_wrapper.get("price", 0))
        if close_price <= 0:
            cum_quote_wrap = self._to_float(order_wrapper.get("cumQuote", 0))
            if cum_quote_wrap > 0 and qty > 0:
                close_price = cum_quote_wrap / qty
        if close_price <= 0 and pre_position:
            close_price = self._to_float(pre_position.get("mark_price", 0))

        entry_price = self._to_float((pre_position or {}).get("entry_price", 0))

        pnl = None
        for key in ("realizedPnl", "realized_pnl", "pnl", "profit"):
            raw_val = order.get(key)
            if raw_val is None and isinstance(close_result, dict):
                raw_val = close_result.get(key)
            if raw_val is None:
                raw_val = order_wrapper.get(key)
            if raw_val is not None:
                try:
                    pnl = float(raw_val)
                    break
                except Exception:
                    pass

        pnl_percent = None
        if pnl is None and side in ("LONG", "SHORT") and entry_price > 0 and close_price > 0 and qty > 0:
            if side == "LONG":
                pnl = (close_price - entry_price) * qty
                pnl_percent = ((close_price - entry_price) / entry_price) * 100.0
            else:
                pnl = (entry_price - close_price) * qty
                pnl_percent = ((entry_price - close_price) / entry_price) * 100.0
        elif pnl is not None and entry_price > 0 and qty > 0:
            pnl_percent = (pnl / (entry_price * qty)) * 100.0

        return {
            "status": status,
            "symbol": symbol,
            "side": side or "UNKNOWN",
            "quantity": qty,
            "entry_price": entry_price,
            "close_price": close_price if close_price > 0 else None,
            "pnl": pnl,
            "pnl_percent": pnl_percent,
            "message": message,
            "raw": close_result,
        }

    def _print_close_summary(self, close_info: Dict[str, Any]) -> None:
        status = str(close_info.get("status", "unknown"))
        symbol = close_info.get("symbol", "")
        if status == "error":
            print(f"❌ {symbol} 平仓失败: {close_info.get('message') or '未知错误'}")
            return
        if status == "noop":
            print(f"✅ {symbol} 无持仓，无需平仓")
            return

        side = str(close_info.get("side", "UNKNOWN"))
        quantity = self._to_float(close_info.get("quantity", 0))
        qty_text = f"{quantity:.6f}" if quantity > 0 else "N/A"
        entry_price = close_info.get("entry_price")
        entry_text = f"{float(entry_price):.6f}" if entry_price is not None and float(entry_price) > 0 else "N/A"
        close_price = close_info.get("close_price")
        price_text = f"{float(close_price):.6f}" if close_price is not None else "N/A"
        pnl = close_info.get("pnl")
        pnl_pct = close_info.get("pnl_percent")
        if pnl is None:
            pnl_text = "N/A"
        elif pnl_pct is None:
            pnl_text = f"{float(pnl):+.4f} USDT (N/A)"
        else:
            pnl_text = f"{float(pnl):+.4f} USDT ({float(pnl_pct):+.2f}%)"
        print(f"✅ 平仓 | {symbol} | {side} | 数量 {qty_text} | 开仓价 {entry_text} | 平仓价 {price_text} | 已实现收益 {pnl_text}")

    def _close_position(self, symbol: str, decision: Dict[str, Any], side: Optional[str] = None):
        """平仓并返回可用于日志记录的详情"""
        pre_position = self._snapshot_position_for_close(symbol, side=side)
        side_upper = self._normalize_position_side(side)
        if side_upper is None and pre_position:
            side_upper = self._normalize_position_side(pre_position.get("side"))
        try:
            if side_upper == "SHORT":
                res = self.trade_executor.close_short(symbol)
            elif side_upper == "LONG":
                res = self.trade_executor.close_long(symbol)
            else:
                res = self.trade_executor.close_position(symbol)
            close_info = self._parse_close_metrics(symbol, pre_position, res)
            self._print_close_summary(close_info)
            self._record_dca_trade_event(
                event_type="CLOSE",
                symbol=symbol,
                side=str(close_info.get("side", side_upper or "")),
                status=str(close_info.get("status", "unknown")),
                quantity=self._to_float(close_info.get("quantity", 0)),
                price=self._to_float(close_info.get("close_price", 0)),
                pnl=close_info.get("pnl"),
                pnl_percent=close_info.get("pnl_percent"),
                reason=str(decision.get("reason", "")),
            )
            if close_info.get("status") not in ("error", "noop"):
                self.trade_count += 1
            else:
                # 未实际平仓时，不更新风险冷却状态
                return close_info

            # 平仓后立即清理不再属于当前持仓的未触发止盈/止损单
            try:
                params_local = self.dca_config.get("params", {}) if isinstance(self.dca_config, dict) else {}
                if bool(params_local.get("order_reconcile_enabled", True)):
                    # 先对当前交易对做强清理，避免因持仓同步延迟导致残留
                    self._cleanup_symbol_orders(symbol, reason="post_close")
                    latest_positions = self.position_data.get_all_positions() or {}
                    configured_symbols = set(self._get_dca_config_symbols())
                    self._reconcile_open_orders(latest_positions, configured_symbols, params_local)
            except Exception:
                pass

            # 更新连续亏损计数与按亏损触发的冷却逻辑
            try:
                params = self.dca_config.get("params", {}) if isinstance(self.dca_config, dict) else {}
                max_consec = int(params.get("max_consecutive_losses", 3))
                # 连续亏损冷却时间（默认30分钟）
                consec_cooldown_seconds = int(params.get("consecutive_loss_cooldown_seconds", 1800))
                # 当天亏损冷却时间（默认8小时）
                daily_cooldown_seconds = int(params.get("daily_loss_cooldown_seconds", 28800))
                daily_cooldown_pct = float(params.get("daily_cooldown_pct", 0.12))
                close_reason = str(decision.get("reason", "") or "")
                default_stopout_keywords = ["普通止损触发", "保本底线触发", "锁利移动止损触发", "stop_loss", "trailing_stop"]
                stopout_keywords_cfg = params.get("stopout_reason_keywords", default_stopout_keywords)
                if isinstance(stopout_keywords_cfg, list):
                    stopout_keywords = [str(k) for k in stopout_keywords_cfg if str(k).strip()]
                else:
                    stopout_keywords = default_stopout_keywords

                pnl_pct = close_info.get("pnl_percent")
                # 仅在有实际 pnl_pct 时进行判定
                if pnl_pct is not None:
                    try:
                        pnl_pct_val = float(pnl_pct)
                        if pnl_pct_val < 0:
                            self.consecutive_losses = int(self.consecutive_losses or 0) + 1
                        else:
                            self.consecutive_losses = 0
                        is_stopout = any(kw in close_reason for kw in stopout_keywords)
                        if is_stopout and pnl_pct_val < 0:
                            self.consecutive_stopouts = int(self.consecutive_stopouts or 0) + 1
                        else:
                            self.consecutive_stopouts = 0
                    except Exception:
                        pass

                # 若达到连续亏损阈值，则启动冷却（阻止新开仓，但不平仓已有仓位）
                if consec_cooldown_seconds > 0 and max_consec > 0 and int(self.consecutive_losses or 0) >= max_consec:
                    self.dca_cooldown_expires = datetime.now() + timedelta(seconds=consec_cooldown_seconds)
                    self.dca_cooldown_reason = "consecutive_losses"
                    print(f"⚠️ 连续亏损 {self.consecutive_losses} 次，触发冷却 {consec_cooldown_seconds//60}分钟（仅阻止新开仓）")

                # 检查相对于本次进程初始权益的当天/会话亏损阈值（如配置 daily_cooldown_pct）
                try:
                    account_summary = self.account_data.get_account_summary() or {}
                    equity = float(account_summary.get("equity", 0))
                    # 使用当天开盘权益进行当天亏损判定（若可用）
                    if self.dca_day_open_equity is not None and daily_cooldown_pct > 0:
                        try:
                            loss_pct = (self.dca_day_open_equity - equity) / self.dca_day_open_equity
                        except Exception:
                            loss_pct = 0.0
                        if daily_cooldown_seconds > 0 and loss_pct >= daily_cooldown_pct:
                            # 按日亏损触发冷却
                            self.dca_cooldown_expires = datetime.now() + timedelta(seconds=daily_cooldown_seconds)
                            self.dca_cooldown_reason = "daily_loss"
                            print(f"⚠️ 当天亏损 {loss_pct:.2%} >= {daily_cooldown_pct:.2%}，触发冷却 {daily_cooldown_seconds//3600}小时（仅阻止新开仓）")
                except Exception:
                    pass
            except Exception:
                # 保证关闭流程不受影响，错误时忽略冷却逻辑
                pass

            return close_info
        except Exception as e:
            print(f"❌ {symbol} 平仓失败: {e}")
            self._record_dca_trade_event(
                event_type="CLOSE",
                symbol=symbol,
                side=str(side_upper or ""),
                status="error",
                quantity=self._to_float((pre_position or {}).get("amount", 0)),
                price=None,
                pnl=None,
                pnl_percent=None,
                reason=f"{decision.get('reason', '')} | {e}",
            )
            return {
                "status": "error",
                "symbol": symbol,
                "side": str(side).upper() if side else "UNKNOWN",
                "quantity": 0.0,
                "entry_price": self._to_float((pre_position or {}).get("entry_price", 0)),
                "close_price": None,
                "pnl": None,
                "pnl_percent": None,
                "message": str(e),
                "raw": None,
            }

    def close_positions_for_symbols(self, symbols: List[str]):
        """
        平仓指定的交易对

        Args:
            symbols: 需要平仓的交易对列表
        """
        for symbol in symbols:
            try:
                print(f"\n🔄 正在平仓 {symbol}...")

                # 获取当前持仓
                position = self.position_data.get_current_position(symbol)

                if not position:
                    print(f"   ✅ {symbol} 无持仓，无需平仓")
                    continue

                # 获取持仓数量
                position_amt = float(position.get("amount", position.get("positionAmt", 0)))

                if position_amt == 0:
                    print(f"   ✅ {symbol} 持仓为0，无需平仓")
                    continue

                result = self._close_position(
                    symbol,
                    {"action": "CLOSE", "reason": "symbols_changed"},
                    side=position.get("side"),
                )
                if result.get("status") not in ("error", "noop"):
                    self._write_log(f"平仓: {symbol} (交易对变更)")

            except Exception as e:
                print(f"   ❌ {symbol} 平仓异常: {e}")
                import traceback

                traceback.print_exc()

    def save_decision(
        self,
        symbol: str,
        decision: Dict[str, Any],
        market_data: Dict[str, Any],
    ):
        """保存决策历史"""
        decision_record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "action": decision["action"],
            "confidence": decision["confidence"],
            "leverage": decision["leverage"],
            "position_percent": decision["position_percent"],
            "engine": decision.get("engine"),
            "entry_regime": decision.get("entry_regime"),
            "entry_reason": decision.get("entry_reason"),
            "reason": decision["reason"],
            "price": market_data["realtime"].get("price", 0),
        }
        self.decision_history.append(decision_record)

        # 只保留最近100条
        if len(self.decision_history) > 100:
            self.decision_history = self.decision_history[-100:]

    def run_cycle(self):
        """执行一个交易周期"""
        cycle_log = []

        cycle_start_line = "=" * 60
        cycle_log.append(cycle_start_line)
        print(cycle_start_line)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cycle_info = f"📅 交易周期 #{self.trade_count + 1} - {timestamp}"
        cycle_log.append(cycle_info)
        print(cycle_info)

        cycle_sep = "=" * 60
        cycle_log.append(cycle_sep)
        print(cycle_sep)

        # 【优化：时间过滤】可配置：避开低波动时段（北京时间 08:00-16:00）
        shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        shanghai_hour = shanghai_now.hour
        use_time_filter = bool(self.config.get("strategy", {}).get("use_time_filter", True))
        if use_time_filter and 8 <= shanghai_hour < 16:
            skip_msg = f"⏸️  当前北京时间 {shanghai_now.strftime('%H:%M')} 处于低波动时段(08:00-16:00)，跳过交易"
            cycle_log.append(skip_msg)
            print(skip_msg)
            self.trade_count += 1
            return

        # ===== 检查配置文件更新 =====
        update_info = self.config_monitor.check_for_updates()

        if update_info["updated"]:
            # 配置文件已更新
            print("\n🔔 检测到配置文件更新！")

            # 如果交易对发生变化，先平仓旧的交易对
            if update_info["symbols_changed"] and update_info["removed_symbols"]:
                print("\n⚠️  交易对已变更，正在平仓旧交易对...")
                self.close_positions_for_symbols(update_info["removed_symbols"])

            # 应用新配置
            self.config_monitor.apply_updates(update_info)

            # 重新加载配置到内存
            self.config = ConfigLoader.load_trading_config(self.config_path)
            print("✅ 配置已重新加载，后续将使用新配置执行")

        # 获取交易币种列表（使用更新后的配置）
        symbols = ConfigLoader.get_trading_symbols(self.config)

        # 打印当前 BTC 牛熊状态（全局市场情绪）
        params = self.config.get("dca_rotation", {}).get("params", {})
        if bool(params.get("btc_regime_enabled", True)):
            regime, score, details = self._dca_detect_btc_regime(params)
            regime_emoji = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "🔄"}.get(regime, "❓")
            print(f"\n{regime_emoji} BTC全球市场状态: {regime} (score={score:+.3f})")
            # 打印各周期详情
            for tf in ["1m", "3m", "5m", "15m", "1h", "4h"]:
                if tf in details and "error" not in details[tf]:
                    d = details[tf]
                    print(f"   {tf}: score={d.get('score', 0):+.2f}, EMA20={d.get('ema_fast', 0):.2f}, EMA50={d.get('ema_slow', 0):.2f}")

        # 显示账户摘要
        account_summary = self.account_data.get_account_summary()
        if account_summary:
            acct_title = "\n💰 账户信息:"
            cycle_log.append(acct_title)
            print(acct_title)

            # ============ 统一账户正确显示逻辑 ============
            # 直接使用 account_summary 返回的字段
            equity = account_summary.get("equity", 0.0)
            available_balance = account_summary.get("available_balance", 0.0)
            unrealized_pnl = account_summary.get("total_unrealized_pnl", 0.0)
            margin_ratio = account_summary.get("margin_ratio", 0.0)

            equity_line = f"   总权益: {equity:.2f} USDT"
            cycle_log.append(equity_line)
            print(equity_line)

            # 显示可用保证金 (统一账户 = 钱包余额 - 占用保证金)
            margin_line = f"   可用保证金: {available_balance:.2f} USDT"
            cycle_log.append(margin_line)
            print(margin_line)

            unrealized_line = f"   未实现盈亏: {unrealized_pnl:.2f} USDT"
            cycle_log.append(unrealized_line)
            print(unrealized_line)

            margin_ratio_line = f"   保证金率: {margin_ratio:.2f}%"
            cycle_log.append(margin_ratio_line)
            print(margin_ratio_line)

            spot_total = account_summary.get("spot_total_balance", 0.0)
            spot_usdt = account_summary.get("spot_usdt_balance", 0.0)
            spot_ldusdt = account_summary.get("spot_ldusdt_balance", 0.0)
            if spot_total > 0:
                spot_line = (
                    f"   现货余额(含LDUSDT): {spot_total:.6f} USDT (USDT: {spot_usdt:.6f}, LDUSDT: {spot_ldusdt:.6f})"
                )
                cycle_log.append(spot_line)
                print(spot_line)
                note_line = "   提示: LDUSDT 为理财资产，需赎回/划转后才能作为合约保证金"
                cycle_log.append(note_line)
                print(note_line)

            # 显示持仓概览（支持自定义排序）
            sort_by = self.config.get("trading", {}).get("position_sort_by", "pnl")
            self._print_positions_snapshot(cycle_log, sort_by=sort_by)

        # 规则策略模式（单币种逐个分析）
        if self._is_dual_engine_mode():
            self._run_dca_rotation_cycle()

        # 规则策略模式（单币种逐个分析）
        elif self.strategy_mode == "V5_RULE":
            for symbol in symbols:
                symbol_sep = f"\n--- {symbol} ---"
                cycle_log.append(symbol_sep)
                print(symbol_sep)

                market_data = self.get_market_data_for_symbol(symbol)
                decision = self.analyze_with_strategy(symbol, market_data)
                self.save_decision(symbol, decision, market_data)
                self.execute_decision(symbol, decision, market_data)

        # 方式1：多币种一次性分析（优化）
        elif len(symbols) > 1:
            # 收集所有币种的数据
            all_symbols_data = {}
            for symbol in symbols:
                market_data = self.get_market_data_for_symbol(symbol)
                position = self.position_data.get_current_position(symbol)

                all_symbols_data[symbol] = {
                    "market_data": market_data,
                    "position": position,
                }

            # 一次性AI分析所有币种
            all_decisions = self.analyze_all_symbols_with_ai(all_symbols_data)

            # 执行每个币种的决策
            for symbol, decision in all_decisions.items():
                symbol_sep = f"\n--- {symbol} ---"
                cycle_log.append(symbol_sep)
                print(symbol_sep)

                market_data = all_symbols_data[symbol]["market_data"]
                self.execute_decision(symbol, decision, market_data)

        else:
            # 方式2：单个币种分析（保持兼容）
            for symbol in symbols:
                symbol_sep = f"\n--- {symbol} ---"
                cycle_log.append(symbol_sep)
                print(symbol_sep)

                # 获取市场数据
                market_data = self.get_market_data_for_symbol(symbol)

                # AI分析
                decision = self.analyze_with_ai(symbol, market_data)

                # 保存决策
                self.save_decision(symbol, decision, market_data)

                # 执行决策
                self.execute_decision(symbol, decision, market_data)

        # 写入日志文件
        for log_line in cycle_log:
            self._write_log(log_line)

    def _print_positions_snapshot(self, cycle_log: List[str], sort_by: str = "pnl") -> None:
        """
        输出当前持仓信息到终端

        Args:
            cycle_log: 日志列表
            sort_by: 排序方式，可选值: "pnl"(按浮盈亏), "pnl%"(按盈亏%), "notional"(按持仓金额)
        """
        try:
            positions = self.position_data.get_all_positions()
        except Exception as e:
            warn_line = f"⚠️  获取持仓信息失败: {e}"
            cycle_log.append(warn_line)
            print(warn_line)
            return

        title = "\n📌 当前持仓:"
        cycle_log.append(title)
        print(title)

        if not positions:
            empty_line = "   无持仓"
            cycle_log.append(empty_line)
            print(empty_line)
            return

        header = "   交易对 | 方向 | 数量 | 入场价 | 标记价 | 浮盈亏 | 盈亏% | 持仓金额 | 杠杆"
        sep = "   " + "-" * (len(header) - 3)
        cycle_log.append(sep)
        print(sep)
        cycle_log.append(header)
        print(header)
        cycle_log.append(sep)
        print(sep)

        use_color = self._use_ansi_color()
        reset = "\033[0m"
        green = "\033[32m"
        red = "\033[31m"
        bright_yellow = "\033[93m"

        def _format_number(value: float, use_plus: bool = True) -> str:
            if value > 0 and use_plus:
                return f"+{value}"
            return str(value)

        def _colorize(value: float, text: str, threshold: Optional[float] = None) -> str:
            if not use_color:
                return text
            if threshold is not None and abs(value) >= threshold:
                return f"{bright_yellow}{text}{reset}"
            color = green if value >= 0 else red
            return f"{color}{text}{reset}"

        # 根据 sort_by 排序
        if sort_by == "pnl%":
            sorted_positions = sorted(
                positions.items(),
                key=lambda item: float(item[1].get("pnl_percent", 0) or 0),
                reverse=True,
            )
        elif sort_by == "notional":
            sorted_positions = sorted(
                positions.items(),
                key=lambda item: float(item[1].get("notional", 0) or 0),
                reverse=True,
            )
        else:  # "pnl" 默认
            sorted_positions = sorted(
                positions.items(),
                key=lambda item: float(item[1].get("unrealized_pnl", 0) or 0),
                reverse=True,
            )

        pnl_threshold_pct = 5.0
        for symbol, pos in sorted_positions:
            side = pos.get("side", "")
            amount = pos.get("amount", 0)
            entry_price = pos.get("entry_price", 0)
            mark_price = pos.get("mark_price", 0)
            unrealized_pnl = pos.get("unrealized_pnl", 0)
            pnl_percent = pos.get("pnl_percent", 0)
            notional = pos.get("notional", 0)
            leverage = pos.get("leverage", 0)

            try:
                pnl_value = float(unrealized_pnl)
            except Exception:
                pnl_value = 0.0
            try:
                pnl_pct_value = float(pnl_percent)
            except Exception:
                pnl_pct_value = 0.0

            pnl_text = _format_number(pnl_value, use_plus=True)
            pnl_pct_text = _format_number(pnl_pct_value, use_plus=True) + "%"
            pnl_colored = _colorize(pnl_value, f"{pnl_value:.4f}")
            pnl_pct_colored = _colorize(pnl_pct_value, pnl_pct_text, threshold=pnl_threshold_pct)

            plain_line = (
                f"   {symbol} | {side} | {amount:.6f} | "
                f"{entry_price:.6f} | {mark_price:.6f} | "
                f"{pnl_text} | {pnl_pct_text} | {notional:.2f} | {leverage}x"
            )

            colored_line = (
                f"   {symbol} | {side} | {amount:.6f} | "
                f"{entry_price:.6f} | {mark_price:.6f} | "
                f"{pnl_colored} | {pnl_pct_colored} | {notional:.2f} | {leverage}x"
            )

            cycle_log.append(plain_line)
            print(colored_line)

    @staticmethod
    def _use_ansi_color() -> bool:
        if os.getenv("NO_COLOR"):
            return False
        try:
            stdout = sys.__stdout__
            return bool(stdout and stdout.isatty())
        except Exception:
            return False

    def run(self):
        """启动主循环"""
        schedule_config = ConfigLoader.get_schedule_config(self.config)
        # 默认周期
        interval_seconds = schedule_config["interval_seconds"]
        dual_engine_mode = self._is_dual_engine_mode()
        direction_interval_seconds = interval_seconds
        download_delay_seconds = schedule_config.get("download_delay_seconds", 5)
        # 限制 download_delay_seconds 最大为30秒，确保在K线更新后的30s内完成下载/分析
        if download_delay_seconds > 30:
            download_delay_seconds = 30

        # 双引擎执行层循环；方向刷新按 dca_rotation.interval（默认 5m）
        if dual_engine_mode:
            interval_seconds = self._dual_engine_exec_interval_seconds
            interval_raw = str(self.dca_config.get("interval", "5m")).strip().lower()
            if interval_raw.endswith("m") and interval_raw[:-1].isdigit():
                direction_interval_seconds = max(60, int(interval_raw[:-1]) * 60)
            elif interval_raw.endswith("h") and interval_raw[:-1].isdigit():
                direction_interval_seconds = max(60, int(interval_raw[:-1]) * 3600)
            self._dual_engine_direction_interval_seconds = direction_interval_seconds
            self._dual_engine_direction_bucket = None
            self._dual_engine_refresh_direction_this_cycle = True
            print(
                "\n⏱️  双引擎调度: "
                f"执行层每{interval_seconds}秒 | 方向刷新每{direction_interval_seconds}秒"
            )
        else:
            print(f"\n⏱️  交易周期: 每{interval_seconds}秒")
        symbols_list = (
            self._get_dca_symbols()
            if dual_engine_mode
            else ConfigLoader.get_trading_symbols(self.config)
        )
        print(f"📊 交易币种: {', '.join(symbols_list)}")
        print(f"📁 日志目录: {self.logs_dir}")
        print("📋 日志格式: logs/YYYY-MM/YYYY-MM-DD_HH.txt (每6小时一个文件，每天4个)")
        print("\n按 Ctrl+C 停止运行\n")

        def _next_kline_boundary(ts: float) -> float:
            """返回下一个对齐到 interval_seconds 的时间戳（单位: 秒）"""
            # 计算下一个整周期边界
            rem = ts % interval_seconds
            if rem == 0:
                return ts
            return ts - rem + interval_seconds

        try:
            # 启动时先对齐到最近的K线边界，并在边界后等待 download_delay_seconds 再开始第一次分析
            now = time.time()
            next_boundary = _next_kline_boundary(now)
            wait_until = next_boundary + download_delay_seconds
            initial_sleep = max(0, wait_until - now)
            if initial_sleep > 0:
                next_ts = datetime.fromtimestamp(next_boundary).strftime("%Y-%m-%d %H:%M:%S")
                print(f"⏳ 等待对齐到下一次K线边界 {next_ts}，再延迟 {download_delay_seconds}s 后开始")
                time.sleep(initial_sleep)

            while True:
                cycle_now = time.time()
                if dual_engine_mode:
                    direction_bucket = int(cycle_now) // max(60, direction_interval_seconds)
                    refresh_direction = (self._dual_engine_direction_bucket != direction_bucket)
                    self._dual_engine_refresh_direction_this_cycle = refresh_direction
                    direction_minutes = max(1, direction_interval_seconds // 60)
                    if refresh_direction:
                        self._dual_engine_direction_bucket = direction_bucket
                        print(f"\n🧭 方向刷新周期：更新{direction_minutes}m决策并执行")
                    else:
                        exec_minutes = max(1, int(interval_seconds // 60))
                        print(f"\n⏱️ 执行层周期：沿用{direction_minutes}m方向，继续{exec_minutes}m执行层")

                # 执行交易周期（在K线更新后的短延迟内运行）
                self.run_cycle()

                # 计算下一个K线边界并在边界后 download_delay_seconds 秒开始下一次
                now = time.time()
                next_boundary = _next_kline_boundary(now)
                # 如果当前正好位于边界并且距离边界0s, 则 next_boundary == now ; 我们要确保等待到下一个边界
                if next_boundary - now < 1e-6:
                    next_boundary += interval_seconds

                sleep_until = next_boundary + download_delay_seconds
                sleep_time = sleep_until - time.time()
                if sleep_time > 0:
                    next_ts = datetime.fromtimestamp(next_boundary).strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"\n💤 对齐等待：下次K线边界 {next_ts}，在其后 {download_delay_seconds}s 开始 (睡眠 {sleep_time:.0f}s)"
                    )
                    time.sleep(sleep_time)
                else:
                    # 如果已经超过计划时间，直接立即进入下一轮（不再sleep）
                    print("⚠️ 已错过预定的对齐时间，立即开始下一周期")

        except KeyboardInterrupt:
            print("\n\n⚠️ 收到中断信号，正在安全退出...")
            self.shutdown()

    def shutdown(self):
        """优雅关闭"""
        print("\n" + "=" * 60)
        print("🛑 交易机器人正在关闭...")
        print("=" * 60)
        if self._is_dual_engine_mode():
            self._save_dca_state()
        print(f"✅ 本次运行交易次数: {self.trade_count}")
        print(f"✅ 决策记录数量: {len(self.decision_history)}")
        print("🎉 交易机器人已安全退出")
        print("=" * 60)


def main():
    """主函数"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置文件路径（支持相对路径，默认读取项目 config/trading_config_vps.json）",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="实盘模式标记（当前入口默认即为实盘）",
    )
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_cfg = os.path.join(project_root, "config", "trading_config_vps.json")
    config_hint = args.config or os.getenv("TRADING_CONFIG_FILE") or os.getenv("BOT_CONFIG_FILE")
    if config_hint:
        config_path = config_hint if os.path.isabs(config_hint) else os.path.join(project_root, config_hint)
    else:
        config_path = default_cfg

    # 本入口固定实盘：统一设置 BINANCE_DRY_RUN=0
    os.environ["BINANCE_DRY_RUN"] = "0"
    if args.live:
        print("⚠️ 已显式使用 --live：BINANCE_DRY_RUN=0（将进行真实下单）")
    else:
        print("⚠️ 默认实盘模式：BINANCE_DRY_RUN=0（将进行真实下单）")
    print(f"📄 使用配置文件: {config_path}")

    # ==============================
    # 启动风险摘要（强烈建议保留）
    # ==============================
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        risk_cfg = cfg.get("risk", {}) or {}
        dca_cfg = ((cfg.get("dca_rotation", {}) or {}).get("params", {}) or {})
        flow_profile_name = str(dca_cfg.get("flow_profile", "") or "").strip().lower()
        flow_profiles = dca_cfg.get("flow_profiles", {})
        if flow_profile_name and isinstance(flow_profiles, dict) and isinstance(flow_profiles.get(flow_profile_name), dict):
            profile_cfg = flow_profiles.get(flow_profile_name, {})
            for _k, _v in profile_cfg.items():
                if _k in ("flow_thresholds", "flow_factor_weights") and isinstance(_v, dict):
                    _base = dca_cfg.get(_k, {})
                    _merged = dict(_base) if isinstance(_base, dict) else {}
                    _merged.update(_v)
                    dca_cfg[_k] = _merged
                else:
                    dca_cfg[_k] = _v
        dca_osc_mode = dca_cfg.get("oscillation_mode", {}) if isinstance(dca_cfg.get("oscillation_mode", {}), dict) else {}
        osc_cfg = risk_cfg.get("oscillation", {}) or {}
        osc_entry = osc_cfg.get("entry_gate", {}) if isinstance(osc_cfg.get("entry_gate", {}), dict) else {}
        osc_exit = osc_cfg.get("exit", {}) if isinstance(osc_cfg.get("exit", {}), dict) else {}
        trend_cfg = risk_cfg.get("trend", {}) or {}
        trend_exit = trend_cfg.get("exit", {}) if isinstance(trend_cfg.get("exit", {}), dict) else {}

        # 兼容命名：有的配置把单笔初始保证金写在 dca_rotation.params.add_margin
        initial_margin = risk_cfg.get(
            "initial_margin",
            dca_cfg.get("initial_margin", dca_cfg.get("add_margin")),
        )

        # 兼容不同配置层级：优先 risk，其次 dca_rotation.params
        max_positions = risk_cfg.get("max_positions", dca_cfg.get("max_positions"))
        max_long_positions = risk_cfg.get("max_long_positions", dca_cfg.get("max_long_positions"))
        max_short_positions = risk_cfg.get("max_short_positions", dca_cfg.get("max_short_positions"))
        total_stop_loss_pct = dca_cfg.get("total_stop_loss_pct", risk_cfg.get("total_stop_loss_pct"))
        total_stop_loss_cooldown_seconds = dca_cfg.get("total_stop_loss_cooldown_seconds")
        exec_layer_cfg = dca_cfg.get("execution_layer", {}) if isinstance(dca_cfg.get("execution_layer", {}), dict) else {}
        exec_layer_enabled = exec_layer_cfg.get("enabled", True)
        exec_layer_tf = exec_layer_cfg.get("timeframe", "1m")
        max_position_pct = risk_cfg.get("max_position_pct", dca_cfg.get("max_position_pct"))
        leverage = risk_cfg.get("leverage", dca_cfg.get("leverage"))
        osc_min_score_long = osc_entry.get("min_score_long", dca_cfg.get("min_score_long"))
        osc_max_score_short = osc_entry.get("max_score_short", dca_cfg.get("max_score_short"))
        osc_score_gap_gate_enabled = bool(dca_cfg.get("score_gap_gate_enabled", osc_entry.get("score_gap_gate_enabled", False)))
        osc_min_score_gap_short = dca_cfg.get("min_score_gap_short", osc_entry.get("min_score_gap_short"))
        osc_disable_long = bool(dca_cfg.get("disable_osc_long_entries", True))
        flow_signal_enabled = bool(dca_cfg.get("flow_signal_enabled", False))
        # 真实执行层：资金流无数据强制拦截
        flow_allow_no_data_pass = False
        flow_signal_hard_gate = bool(dca_cfg.get("flow_signal_hard_gate", False))
        flow_affect_confidence = bool(dca_cfg.get("flow_affect_confidence", False))
        flow_interval_seconds = dca_cfg.get("flow_signal_interval_seconds", 15)
        flow_min_hits_short = dca_cfg.get("flow_min_hits_short", dca_cfg.get("flow_min_hits", 2))
        flow_min_hits_long = dca_cfg.get("flow_min_hits_long", dca_cfg.get("flow_min_hits", 2))
        flow_min_score_short = dca_cfg.get("flow_min_score_short", dca_cfg.get("flow_min_score", 0.35))
        flow_min_score_long = dca_cfg.get("flow_min_score_long", dca_cfg.get("flow_min_score", 0.35))
        flow_only_gate_mode = bool(dca_cfg.get("flow_only_gate_mode", False))
        dual_engine_direction_gate_enabled = bool(dca_cfg.get("dual_engine_direction_gate_enabled", not flow_only_gate_mode))
        dual_engine_quota_gate_enabled = bool(dca_cfg.get("dual_engine_quota_gate_enabled", not flow_only_gate_mode))
        stopout_size_reduce_enabled = bool(dca_cfg.get("stopout_size_reduce_enabled", False))
        stopout_size_reduce_after = dca_cfg.get("stopout_size_reduce_after", 2)
        stopout_size_reduce_factor = dca_cfg.get("stopout_size_reduce_factor", 0.5)
        osc_take_profit_pct = osc_exit.get("take_profit_pct", dca_cfg.get("take_profit_pct"))
        osc_symbol_stop_loss_pct = osc_exit.get("symbol_stop_loss_pct", dca_cfg.get("symbol_stop_loss_pct"))
        osc_break_even_trigger_pct = osc_exit.get("break_even_trigger_pct", dca_cfg.get("break_even_trigger_pct"))
        osc_trailing_trigger_pct = osc_exit.get("trailing_stop_trigger_pct", dca_cfg.get("trailing_stop_trigger_pct"))
        osc_trailing_stop_pct = osc_exit.get("trailing_stop_pct", dca_cfg.get("trailing_stop_pct"))
        osc_take_profit_ratio = osc_cfg.get("take_profit_ratio", dca_osc_mode.get("take_profit_ratio"))
        osc_stop_loss_ratio = osc_cfg.get("stop_loss_ratio", dca_osc_mode.get("stop_loss_ratio"))
        osc_break_even_ratio = osc_cfg.get("break_even_trigger_ratio", dca_osc_mode.get("break_even_trigger_ratio"))
        osc_trailing_trigger_ratio = osc_cfg.get("trailing_trigger_ratio", dca_osc_mode.get("trailing_trigger_ratio"))
        osc_trailing_stop_ratio = osc_cfg.get("trailing_stop_ratio", dca_osc_mode.get("trailing_stop_ratio"))
        osc_trailing_after_be_ratio = osc_cfg.get(
            "trailing_stop_after_be_ratio",
            dca_osc_mode.get("trailing_stop_after_be_ratio"),
        )
        osc_entry_src = "risk.oscillation.entry_gate" if osc_entry else "dca_rotation.params"
        osc_exit_src = "risk.oscillation.exit" if osc_exit else "dca_rotation.params"
        osc_ratio_src = "risk.oscillation.*_ratio" if any(
            k in osc_cfg
            for k in (
                "take_profit_ratio",
                "stop_loss_ratio",
                "break_even_trigger_ratio",
                "trailing_trigger_ratio",
                "trailing_stop_ratio",
                "trailing_stop_after_be_ratio",
            )
        ) else "dca_rotation.params.oscillation_mode"
        trend_exit_src = "risk.trend.exit" if trend_exit else "(fallback params)"

        print("\n================ 风险摘要确认 ================")
        print(f"初始保证金 (initial_margin/add_margin): {initial_margin}")
        print(f"最大总持仓数 (max_positions): {max_positions}")
        print(f"最大多头持仓数 (max_long_positions): {max_long_positions}")
        print(f"最大空头持仓数 (max_short_positions): {max_short_positions}")
        print(f"单标的最大仓位 (max_position_pct): {max_position_pct}")
        print(f"杠杆 (leverage): {leverage}")
        print(f"总回撤止损 (total_stop_loss_pct): {total_stop_loss_pct}")
        print(f"总回撤止损冷却秒数 (total_stop_loss_cooldown_seconds): {total_stop_loss_cooldown_seconds}")
        print(f"执行层确认 (execution_layer): enabled={exec_layer_enabled}, timeframe={exec_layer_tf}")
        print("------------------------------------------------")
        print("震荡开仓门禁（RANGE/RANGE_LOCK）:")
        print(f"  source: {osc_entry_src}")
        print(f"  flow_profile: {flow_profile_name or '(none)'}")
        print(f"  flow_only_mode: {'on' if flow_only_gate_mode else 'off'}")
        print(f"  dual_dir_gate: {'on' if dual_engine_direction_gate_enabled else 'off'}")
        print(f"  dual_quota_gate: {'on' if dual_engine_quota_gate_enabled else 'off'}")
        print("  p_win_gate: removed")
        print("  edge_gate: removed")
        print(
            "  score_gap_gate: "
            f"{'on' if osc_score_gap_gate_enabled else 'off'} "
            f"(short(score-th)>={osc_min_score_gap_short})"
        )
        print(f"  disable_osc_long: {'on' if osc_disable_long else 'off'} (RANGE禁LONG+osc_long)")
        print(
            "  stopout_size_reduce: "
            f"{'on' if stopout_size_reduce_enabled else 'off'} "
            f"(after={stopout_size_reduce_after}, factor={stopout_size_reduce_factor})"
        )
        print(
            "  flow_signal_gate: "
            f"{'on' if flow_signal_enabled else 'off'} "
            f"(ttl={flow_interval_seconds}s, short>={flow_min_score_short}/{flow_min_hits_short}hits, "
            f"long>={flow_min_score_long}/{flow_min_hits_long}hits, "
            f"no_data_pass={'on' if flow_allow_no_data_pass else 'off'}, "
            f"mode={'hard' if flow_signal_hard_gate else 'assist'}, "
            f"affect_conf={'on' if flow_affect_confidence else 'off'})"
        )
        print(f"  min_score_long: {osc_min_score_long}")
        print(f"  max_score_short: {osc_max_score_short}")
        print("震荡出场基线（RANGE/RANGE_LOCK）:")
        print(f"  source: {osc_exit_src}")
        print(f"  take_profit_pct: {osc_take_profit_pct}")
        print(f"  symbol_stop_loss_pct: {osc_symbol_stop_loss_pct}")
        print(f"  break_even_trigger_pct: {osc_break_even_trigger_pct}")
        print(f"  trailing_stop_trigger_pct: {osc_trailing_trigger_pct}")
        print(f"  trailing_stop_pct: {osc_trailing_stop_pct}")
        print("震荡 ratio（RANGE / RANGE_LOCK）:")
        print(f"  source: {osc_ratio_src}")
        print(f"  take_profit_ratio: {osc_take_profit_ratio}")
        print(f"  stop_loss_ratio: {osc_stop_loss_ratio}")
        print(f"  break_even_trigger_ratio: {osc_break_even_ratio}")
        print(f"  trailing_trigger_ratio: {osc_trailing_trigger_ratio}")
        print(f"  trailing_stop_ratio: {osc_trailing_stop_ratio}")
        print(f"  trailing_stop_after_be_ratio: {osc_trailing_after_be_ratio}")
        print("趋势出场基线（TREND）:")
        print(f"  source: {trend_exit_src}")
        print(f"  take_profit_pct: {trend_exit.get('take_profit_pct', '(fallback)')}")
        print(f"  symbol_stop_loss_pct: {trend_exit.get('symbol_stop_loss_pct', '(fallback)')}")
        print(f"  break_even_trigger_pct: {trend_exit.get('break_even_trigger_pct', '(fallback)')}")
        print(f"  trailing_stop_trigger_pct: {trend_exit.get('trailing_stop_trigger_pct', '(fallback)')}")
        print(f"  trailing_stop_pct: {trend_exit.get('trailing_stop_pct', '(fallback)')}")
        print("================================================\n")

    except Exception as e:
        print(f"⚠️ 风险摘要读取失败: {e}")

    bot = TradingBot(config_path=config_path)
    bot.run()


if __name__ == "__main__":
    main()
