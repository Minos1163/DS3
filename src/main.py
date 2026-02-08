"""
AI交易机器人主程序
整合所有模块，实现完整的交易流程
"""

import time

from datetime import datetime

from io import StringIO

from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

import csv

import pandas as pd

import tempfile

import shutil

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


import os
import sys
import json

# 添加项目根目录到Python路径（必须在导入src.*之前）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__ or "")))
sys.path.insert(0, PROJECT_ROOT)


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

    def __init__(self, config_path: Optional[str] = None):
        """初始化交易机器人"""
        print("=" * 60)
        print("🚀 AI交易机器人启动中...")
        print("=" * 60)

        # 如果未指定配置路径，使用默认路径 (相对于项目根目录)
        if config_path is None:
            # 获取项目根目录 (src 的上级目录)
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(project_root, "config", "trading_config.json")

        # 保存配置路径
        self.config_path = config_path

        # 加载配置
        self.config = ConfigLoader.load_trading_config(config_path)
        print("✅ 配置加载完成")

        # 初始化配置监控器
        self.config_monitor = ConfigMonitor(config_path)
        print("✅ 配置监控器初始化完成")

        # 加载环境变量 (从项目根目录查找 .env 文件)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(project_root, ".env")
        EnvManager.load_env_file(env_path)
        print("✅ 环境变量加载完成")

        # 初始化日志系统
        self.log_buffer = StringIO()
        self.logs_dir = os.path.join(project_root, "logs")
        self._setup_logs_directory()
        self._redirect_terminal_output()

        # API Key 自检已移除（避免误报影响启动日志）
        self.api_probe_info = None

        # 策略模式
        self.strategy_mode = self.config.get("strategy", {}).get("mode", "AI")
        self.ai_enabled = self.config.get("ai", {}).get("enabled", True)
        self.ai_client = None
        self.prompt_builder = None
        self.decision_parser = None
        self.strategy = None

        # DCA 轮动配置与状态
        self.dca_config_path = os.path.join(project_root, "config", "trading_config.json")
        self.dca_config: Dict[str, Any] = {}
        self.dca_config_mtime: Optional[float] = None
        self.dca_state: Dict[str, Dict[str, Any]] = {}
        self.dca_last_entry_time: Optional[datetime] = None
        self.dca_initial_equity: Optional[float] = None
        self.dca_peak_equity: Optional[float] = None
        self.dca_halt: bool = False
        self.dca_state_path = os.path.join(self.logs_dir, "dca_state.json")
        self.dca_dashboard_path = os.path.join(self.logs_dir, "dca_dashboard.json")
        self.dca_dashboard_csv_path = os.path.join(self.logs_dir, "dca_dashboard.csv")
        self.dca_dashboard_html_path = os.path.join(self.logs_dir, "dca_dashboard.html")
        self._last_dca_snapshot_key: Optional[str] = None
        self._last_open_orders_count: Optional[int] = None

        # 初始化客户端
        self.client = self._init_binance_client()
        self.ai_client = None
        print("✅ API客户端初始化完成")

        # 初始化管理器
        self.market_data = MarketDataManager(self.client)
        self.position_data = PositionDataManager(self.client)
        self.account_data = AccountDataManager(self.client)
        print("✅ 数据管理器初始化完成")

        # 初始化交易执行器和风险管理器
        self.trade_executor = TradeExecutor(self.client, self.config)
        self.position_manager = PositionManager(self.client)
        self.risk_manager = RiskManager(self.config)
        print("✅ 交易执行器初始化完成")

        # AI组件 / 规则策略
        if self.strategy_mode == "DCA_ROTATION":
            self.strategy = None
            if self.ai_enabled:
                self.ai_client = self._init_ai_client()
                self.prompt_builder = PromptBuilder(self.config)
                self.decision_parser = DecisionParser()
                print("✅ DCA轮动策略已启用（AI门禁已开启）")
            else:
                self.ai_client = None
                self.prompt_builder = None
                self.decision_parser = None
                print("✅ DCA轮动策略已启用（AI未启用）")
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

        # 预加载历史K线数据
        print("=" * 60)
        print("📊 预加载历史K线数据...")
        print("=" * 60)
        self._preload_historical_data()

        print("=" * 60)
        print("🎉 AI交易机器人启动成功！")
        print("=" * 60)
        print()

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

    def _redirect_terminal_output(self):
        """将终端输出同步写入日志文件"""
        if getattr(sys.stdout, "_is_terminal_logger", False):
            return
        stdout_logger = TerminalOutputLogger(sys.stdout, self._get_log_file_path)
        stderr_logger = TerminalOutputLogger(sys.stderr, self._get_log_file_path)
        sys.stdout = stdout_logger
        sys.stderr = stderr_logger

    def _preload_historical_data(self):
        """
        预加载历史K线数据
        在启动时为所有交易对下载200根K线，确保有足够的历史数据用于技术分析
        """
        if self.strategy_mode == "DCA_ROTATION":
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

    def _get_dca_symbols(self) -> List[str]:
        """返回 DCA 候选交易对，并根据配置过滤低流动性品种。

        支持在配置中设置 `min_daily_volume_usdt`（单位 USDT），当该值大于0时，
        会调用市场数据获取 24h 成交量与价格，计算估算的 USDT 成交额并过滤掉低于阈值的品种。
        如果配置中未设置该项或为 0，则不进行过滤。
        """
        symbols = self.dca_config.get("symbols", [])
        normalized: List[str] = []
        for s in symbols:
            s = s.upper()
            if not s.endswith("USDT"):
                s = f"{s}USDT"
            normalized.append(s)

        # 读取阈值（单位 USDT），支持在 dca_config 或 dca_config['params'] 中配置，默认 0 表示不过滤
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
        if min_vol_usdt <= 0:
            return normalized

        # 需要 market_data 可用
        filtered: List[str] = []
        for sym in normalized:
            try:
                md = self.market_data.get_realtime_market_data(sym)
                if not md:
                    print(f"⚠️ 无法获取 {sym} 的实时数据，跳过流动性过滤，保守跳过")
                    continue
                price = float(md.get("price", 0) or 0)
                vol = float(md.get("volume_24h", 0) or 0)
                vol_usdt = price * vol
                if vol_usdt >= min_vol_usdt:
                    filtered.append(sym)
                else:
                    print(f"⤫ 过滤低流动性: {sym} 24h≈{vol_usdt:,.2f} USDT < min {min_vol_usdt}")
            except Exception as e:
                print(f"⚠️ 评估 {sym} 流动性失败: {e}")

        if not filtered:
            print("⚠️ 所有候选标的被流动性阈值过滤，返回原始候选列表以避免空列表")
            return normalized

        print(f"✅ 已过滤低流动性交易对，剩余: {len(filtered)}")
        return filtered

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
            self.dca_config_mtime = mtime
            self._apply_data_endpoints()
            print("✅ 已加载 DCA 轮动配置 (trading_config.json)")
        except Exception as e:
            print(f"❌ 读取 DCA 配置失败: {e}")

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

    def _load_dca_state(self) -> None:
        if not os.path.exists(self.dca_state_path):
            return
        try:
            with open(self.dca_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.dca_halt = bool(data.get("dca_halt", False))
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
            if not pos or pos.get("side") != "SHORT":
                self.dca_state.pop(symbol, None)
                continue
            if symbol not in self.dca_state:
                entry_price = float(pos.get("entry_price", 0))
                self.dca_state[symbol] = {
                    "last_dca_price": entry_price,
                    "dca_count": 0,
                    "entry_time": datetime.now(),
                }

    def _write_dca_dashboard(self, positions: Dict[str, Dict[str, Any]]) -> None:
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
                "open_orders": int(self._last_open_orders_count or 0),
                "api_probe": self.api_probe_info,
                "positions": [],
            }

            for symbol, pos in positions.items():
                state = self.dca_state.get(symbol, {})
                payload["positions"].append(
                    {
                        "symbol": symbol,
                        "side": pos.get("side"),
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
            "entry_price",
            "mark_price",
            "pnl_percent",
            "dca_count",
            "last_dca_price",
            "entry_time",
        ]
        # 尝试以更鲁棒的方式写入 CSV：捕获 PermissionError 并重试，创建文件时使用临时文件替换以保证原子性
        max_retries = 5
        backoff = 0.5
        written = False
        rows = []
        for pos in payload.get("positions", []):
            rows.append(
                [
                    payload.get("timestamp"),
                    payload.get("equity"),
                    payload.get("peak_equity"),
                    payload.get("drawdown_pct"),
                    pos.get("symbol"),
                    pos.get("side"),
                    pos.get("entry_price"),
                    pos.get("mark_price"),
                    pos.get("pnl_percent"),
                    pos.get("dca_count"),
                    pos.get("last_dca_price"),
                    pos.get("entry_time"),
                ]
            )

        for attempt in range(1, max_retries + 1):
            try:
                os.makedirs(self.logs_dir, exist_ok=True)
                exists = os.path.exists(self.dca_dashboard_csv_path)
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

    def _write_dca_dashboard_html(self, payload: Dict[str, Any]) -> None:
        rows = []
        for pos in payload.get("positions", []):
            pnl = pos.get("pnl_percent")
            pnl_class = "pnl-pos" if pnl is not None and pnl >= 0 else "pnl-neg"
            rows.append(
                "<tr>"
                f"<td>{pos.get('symbol')}</td>"
                f"<td>{pos.get('side')}</td>"
                f"<td>{pos.get('entry_price')}</td>"
                f"<td>{pos.get('mark_price')}</td>"
                f"<td class='{pnl_class}'>{pos.get('pnl_percent')}</td>"
                f"<td>{pos.get('dca_count')}</td>"
                f"<td>{pos.get('last_dca_price')}</td>"
                f"<td>{pos.get('entry_time')}</td>"
                "</tr>"
            )
        table_rows = "\n".join(rows) if rows else "<tr><td colspan='8'>无持仓</td></tr>"
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

        cancel_orphan = bool(params.get("cancel_orphan_orders", True))
        cancel_side_mismatch = bool(params.get("cancel_side_mismatch_orders", True))
        cancel_unknown = bool(params.get("cancel_unknown_symbol_orders", True))
        self._last_open_orders_count = len(orders)

        for order in orders:
            symbol = order.get("symbol")
            order_id = order.get("orderId")
            if not symbol or not order_id:
                continue

            if symbol not in symbols_set and cancel_unknown:
                self.client.cancel_order(symbol, int(order_id))
                continue

            pos = positions.get(symbol)
            if not pos and cancel_orphan:
                self.client.cancel_order(symbol, int(order_id))
                continue

            if pos and cancel_side_mismatch:
                pos_side = pos.get("side")
                order_pos_side = order.get("positionSide")
                if not order_pos_side:
                    order_side = str(order.get("side", "")).upper()
                    order_pos_side = "LONG" if order_side == "BUY" else "SHORT" if order_side == "SELL" else None
                if order_pos_side and pos_side and order_pos_side != pos_side:
                    self.client.cancel_order(symbol, int(order_id))

    def _reload_dca_config_if_changed(self) -> Dict[str, Any]:
        before_symbols = set(self._get_dca_symbols())
        prev_mtime = self.dca_config_mtime
        self._load_dca_rotation_config(initial=False)
        after_symbols = set(self._get_dca_symbols())
        updated = prev_mtime is None or self.dca_config_mtime != prev_mtime
        symbols_changed = before_symbols != after_symbols
        return {
            "updated": updated,
            "symbols_changed": symbols_changed,
            "removed_symbols": list(before_symbols - after_symbols),
            "added_symbols": list(after_symbols - before_symbols),
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
        klines = self.client.get_klines(symbol, interval, limit=limit)
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

    def _dca_calc_indicators(self, df: pd.DataFrame, bar_minutes: int) -> pd.DataFrame:
        df = df.copy()
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        df["rsi"] = 100 - (100 / (1 + rs))

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
        return df

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

    def _dca_detect_market_regime(self, symbol: str, params: Dict[str, Any]) -> str:
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
        return float(ai_cfg.get("dca_min_confidence", ai_cfg.get("min_confidence", 0.6)))

    def _dca_ai_fail_policy(self) -> str:
        ai_cfg = self.config.get("ai", {})
        return str(ai_cfg.get("dca_fail_policy", "ALLOW")).upper()

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
        min_conf = self._dca_ai_min_confidence()
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

            if side == "SHORT" and action == "SELL_OPEN" and confidence >= min_conf:
                selected.append((symbol, score, price, side))
            if side == "LONG" and action == "BUY_OPEN" and confidence >= min_conf:
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

    def _run_dca_rotation_cycle(self) -> None:
        """DCA rotation cycle optimized for AI token efficiency: only analyze positions + top DCA candidates."""
        update_info = self._reload_dca_config_if_changed()
        if update_info["updated"]:
            print("\n🔔 DCA配置更新，已重新加载")
            if update_info["symbols_changed"]:
                removed = update_info["removed_symbols"]
                added = update_info["added_symbols"]
                if removed:
                    print("\n⚠️  交易对已变更，正在平仓旧交易对...")
                    self.close_positions_for_symbols(removed)
                    for symbol in removed:
                        self.dca_state.pop(symbol, None)
                if added:
                    self._preload_dca_symbols(added)

        symbols = self._get_dca_symbols()
        interval = self.dca_config.get("interval", "5m")
        params = self.dca_config.get("params", {})
        direction = str(params.get("direction", "SHORT")).upper()
        score_threshold = float(params.get("score_threshold", 0.12))
        score_threshold_long = float(params.get("score_threshold_long", score_threshold))
        score_threshold_short = float(params.get("score_threshold_short", score_threshold))
        rsi_entry_short = float(params.get("rsi_entry_short", params.get("rsi_entry", 70)))
        rsi_entry_long = float(params.get("rsi_entry_long", 100 - rsi_entry_short))

        # 硬编码最大持仓数为2（覆盖配置中的max_positions）
        MAX_POSITIONS = 2

        account_summary = self.account_data.get_account_summary() or {}
        equity = float(account_summary.get("equity", 0))
        if equity <= 0:
            print("⚠️  无法获取账户权益，跳过本轮")
            return

        if self.dca_initial_equity is None:
            self.dca_initial_equity = equity
            self.dca_peak_equity = equity

        if self.dca_peak_equity is not None:
            self.dca_peak_equity = max(self.dca_peak_equity, equity)

        positions = self.position_data.get_all_positions()
        self._reconcile_open_orders(positions, set(symbols), params)
        # 每日/总投入止损阈值（默认为 10%）。可以在 config/trading_config.json 中通过
        # "total_stop_loss_pct" 覆盖（值为小数，0.10 表示 10%）。
        total_stop_loss_pct = float(params.get("total_stop_loss_pct", 0.10))
        if self.dca_peak_equity and total_stop_loss_pct > 0:
            drawdown = (self.dca_peak_equity - equity) / self.dca_peak_equity
            if drawdown >= total_stop_loss_pct:
                print("⚠️  触发总投入止损，正在平仓并停止交易")
                self.trade_executor.close_all_positions()
                self.dca_halt = True
                return

        if self.dca_halt:
            print("⚠️  DCA已停止交易（总止损触发）")
            self._save_dca_state()
            self._write_dca_dashboard(positions)
            return

        # 更新持仓：止盈/止损/时间止损/DCA加仓
        force_close_unknown = bool(self.dca_config.get("force_close_unknown_symbols", False))
        force_close_non_short = bool(self.dca_config.get("force_close_non_short", False))
        symbols_set = set(symbols)
        unknown_symbols = [s for s in positions.keys() if s not in symbols_set]
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

        close_candidates: List[str] = []

        for symbol in symbols:
            pos = positions.get(symbol)
            if not pos:
                continue
            if direction != "BOTH" and pos.get("side") != direction:
                continue

            state = self.dca_state.setdefault(
                symbol,
                {
                    "last_dca_price": pos.get("entry_price", 0),
                    "dca_count": 0,
                    "entry_time": now,
                },
            )

            realtime = self.market_data.get_realtime_market_data(symbol)
            current_price = realtime.get("price", 0) if realtime else 0
            if current_price <= 0:
                continue

            entry_price = float(pos.get("entry_price", 0))
            if entry_price <= 0:
                continue

            if pos.get("side") == "SHORT":
                pnl_pct = (entry_price - current_price) / entry_price
            else:
                pnl_pct = (current_price - entry_price) / entry_price
            take_profit_pct = float(params.get("take_profit_pct", 0.015))
            stop_loss_pct = float(params.get("symbol_stop_loss_pct", 0.15))
            max_hold_days = float(params.get("max_hold_days", 1))
            max_hold_minutes = max_hold_days * 24 * 60

            hold_minutes = (now - state.get("entry_time", now)).total_seconds() / 60

            if pnl_pct >= take_profit_pct:
                if pos.get("side") == "SHORT":
                    self.trade_executor.close_short(symbol)
                else:
                    self.trade_executor.close_long(symbol)
                self.dca_state.pop(symbol, None)
                self._save_dca_state()
                self._write_dca_dashboard(positions)
                continue

            if pnl_pct <= -stop_loss_pct:
                if pos.get("side") == "SHORT":
                    self.trade_executor.close_short(symbol)
                else:
                    self.trade_executor.close_long(symbol)
                self.dca_state.pop(symbol, None)
                self._save_dca_state()
                self._write_dca_dashboard(positions)
                continue

            if hold_minutes >= max_hold_minutes:
                if pos.get("side") == "SHORT":
                    self.trade_executor.close_short(symbol)
                else:
                    self.trade_executor.close_long(symbol)
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
            score_threshold_used = threshold_short_adj if pos.get("side") == "SHORT" else threshold_long_adj
            score_used = short_score if pos.get("side") == "SHORT" else long_score
            score_exit_mult = float(params.get("score_exit_multiplier", 1.0))
            if score_used < score_threshold_used * score_exit_mult:
                # 延迟平仓判断，统一批量调用 AI（节省 token 并保证一致性）
                close_candidates.append(symbol)
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

            if state.get("dca_count", 0) < max_dca:
                equity_scale = self._dca_equity_scale(equity, params)
                add_margin = float(params.get("add_margin", 3.65))
                add_mult = float(params.get("add_amount_multiplier", 1.05))
                add_margin = add_margin * equity_scale * (add_mult ** state.get("dca_count", 0))
                threshold_used = threshold_short_adj if pos.get("side") == "SHORT" else threshold_long_adj
                score_used = short_score if pos.get("side") == "SHORT" else long_score
                confidence = score_used / threshold_used if threshold_used > 0 else 1.0
                size_factor = max(0.5, min(1.0, confidence))
                add_margin = add_margin * size_factor
                leverage = int(params.get("leverage", 3))
                quantity = (add_margin * leverage) / current_price
                max_position_pct = float(params.get("max_position_pct_add", params.get("max_position_pct", 0.30)))
                max_position_value = equity * max_position_pct
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
                        state["dca_count"] = state.get("dca_count", 0) + 1
                        state["last_dca_price"] = current_price
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
                        state["dca_count"] = state.get("dca_count", 0) + 1
                        state["last_dca_price"] = current_price
                        self._save_dca_state()
                        self._write_dca_dashboard(positions)

        # =====================================================================
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
        open_candidates_raw: List[Tuple[str, float, float, str]] = []

        # 如果已达最大持仓数，不再寻找新候选
        if len(current_position_symbols) < MAX_POSITIONS:
            min_daily_volume = float(params.get("min_daily_volume_usdt", 30.0))
            for symbol in symbols:
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
                rsi_val = row.get("rsi", 0)
                short_score, long_score = self._dca_score_pair(row, rsi_entry_short, rsi_entry_long)
                if direction in ("SHORT", "BOTH") and rsi_val >= rsi_entry_short and short_score >= threshold_short_adj:
                    open_candidates_raw.append((symbol, short_score, row.get("close", 0), "SHORT"))
                if direction in ("LONG", "BOTH") and rsi_val <= rsi_entry_long and long_score >= threshold_long_adj:
                    open_candidates_raw.append((symbol, long_score, row.get("close", 0), "LONG"))

            # 取DCA评分最高的top N个候选
            open_candidates_raw = sorted(open_candidates_raw, key=lambda x: x[1], reverse=True)[:dca_top_n]

        candidate_symbols = [c[0] for c in open_candidates_raw]
        print(f"📈 DCA候选: {candidate_symbols} (top {dca_top_n})")

        # 3. 合并持仓+候选，准备AI批量分析（总共2-4个交易对）
        symbols_for_ai = list(set(current_position_symbols + candidate_symbols))
        if not symbols_for_ai:
            print("⏭️  无持仓也无候选，跳过本轮")
            return

        print(f"🤖 AI分析目标: {symbols_for_ai} (共{len(symbols_for_ai)}个)")

        # 4. 批量调用AI分析
        all_symbols_data: Dict[str, Any] = {}
        for s in symbols_for_ai:
            market_data = self.get_market_data_for_symbol(s)
            position = positions.get(s)
            all_symbols_data[s] = {"market_data": market_data, "position": position}

        multi_decisions: Dict[str, Dict[str, Any]] = {}
        if self._dca_ai_gate_enabled():
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

        # 5. 处理AI决策：先平仓，再开仓
        # 5.1 检查所有当前持仓，看AI是否建议平仓
        min_conf = self._dca_ai_min_confidence()

        for symbol in current_position_symbols:
            pos = positions.get(symbol)
            if not pos:
                continue

            # 获取AI决策（应该在multi_decisions中）
            decision = multi_decisions.get(symbol)

            # 如果没有AI决策，跳过（保留持仓）
            if not decision:
                print(f"⚠️ {symbol} 无AI决策，保留持仓")
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
                        if pos.get("side") == "SHORT":
                            self.trade_executor.close_short(symbol)
                        else:
                            self.trade_executor.close_long(symbol)
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

        # 5.2 处理开仓决策：仅在持仓数<MAX_POSITIONS时才考虑开仓
        if self.dca_last_entry_time is not None:
            cooldown_seconds = int(params.get("cooldown_seconds", 60))
            if (now - self.dca_last_entry_time).total_seconds() < cooldown_seconds:
                print("⏳ 冷却时间未到，跳过开仓")
                return

        # 统计当前实际持仓数（可能在平仓后已经改变）
        positions_after_close = self.position_data.get_all_positions()
        current_count = len(
            [
                s
                for s in positions_after_close.keys()
                if positions_after_close[s]
                and abs(float(positions_after_close[s].get("amount", positions_after_close[s].get("positionAmt", 0))))
                > 0
            ]
        )

        if current_count >= MAX_POSITIONS:
            print(f"✋ 已达最大持仓数({current_count}/{MAX_POSITIONS})，不再开新仓")
            return

        # 从候选中筛选AI建议开仓的，按confidence排序
        open_actions = []
        for symbol in candidate_symbols:
            decision = multi_decisions.get(symbol)
            if not decision:
                continue
            action = decision.get("action", "HOLD")
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
            open_actions.append((symbol, confidence, decision))

        # 按confidence降序排序
        open_actions.sort(key=lambda x: x[1], reverse=True)

        # 开仓直到达到MAX_POSITIONS
        for symbol, conf, decision in open_actions:
            if current_count >= MAX_POSITIONS:
                print(f"✋ 已达最大持仓数({current_count}/{MAX_POSITIONS})，停止开仓")
                break

            market_data = self.get_market_data_for_symbol(symbol)
            self.save_decision(symbol, decision, market_data)
            try:
                print(f"🚀 开仓: {symbol} (confidence={conf:.2f})")
                self.execute_decision(symbol, decision, market_data)
                # 检查是否成功开仓
                pos_after = self.position_data.get_current_position(symbol)
                if pos_after and abs(float(pos_after.get("amount", pos_after.get("positionAmt", 0)))) > 0:
                    current_count += 1
                    # 记录DCA状态
                    price = market_data.get("realtime", {}).get("price", 0)
                    self.dca_state[symbol] = {
                        "last_dca_price": price,
                        "dca_count": 0,
                        "entry_time": now,
                    }
                    self.dca_last_entry_time = now
                    self._save_dca_state()
                    self._write_dca_dashboard(positions_after_close)
            except Exception as e:
                print(f"❌ 开仓失败: {symbol} - {e}")

        # per-cycle dashboard refresh
        self._write_dca_dashboard(positions)

    def _get_log_file_path(self) -> str:
        """
        获取当前的日志文件路径
        格式: logs/YYYY-MM/YYYY-MM-DD_HH.txt
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
        格式: logs/YYYY-MM/DCA_dashboard_YYYY-MM-DD_HH.csv
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
            year_month = now.strftime("%Y-%m")
            hour_block = (now.hour // 6) * 6
            snapshot_key = f"{year_month}-{now.strftime('%Y-%m-%d')}-{hour_block:02d}"
            if self._last_dca_snapshot_key == snapshot_key:
                return
            snapshot_path = self._get_dca_dashboard_snapshot_path(now)
            if os.path.exists(self.dca_dashboard_csv_path):
                shutil.copyfile(self.dca_dashboard_csv_path, snapshot_path)
                self._last_dca_snapshot_key = snapshot_key
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
            print(f"   仓位: {decision['position_percent']}%")
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
        print(f"   仓位: {decision.get('position_percent', 0)}%")
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

        # ----- 阈值检查（配置可控制） -----
        ai_conf_min = self.config.get("ai", {}).get("min_confidence", 0.6)
        min_pos_pct = self.config.get("trading", {}).get("min_position_percent", 10)

        # 如果信心度太低，不执行（但允许平仓；HOLD 也不阻断）
        if confidence < ai_conf_min and action not in ("CLOSE", "HOLD"):
            print(f"⚠️ {symbol} 信心度太低({confidence:.2f} < {ai_conf_min}), 跳过执行")
            # 记录跳过的决策到交易日志
            self._append_trade_log(
                symbol=symbol,
                action=action,
                decision=decision,
                quantity=0,
                entry_price=market_data["realtime"].get("price", 0),
                result="skipped_low_confidence",
                pnl=None,
            )
            return

        # 如果仓位小于最小阈值且是开仓操作，则视配置决定：跳过或按最小仓位提升
        try:
            pos_pct = float(decision.get("position_percent", 0))
        except Exception:
            pos_pct = 0

        if action in ("BUY_OPEN", "SELL_OPEN") and pos_pct < min_pos_pct:
            # 如果开启 AI 门禁并且配置允许 AI 覆盖最小仓位，则将目标仓位提升到最小值
            ai_cfg = self.config.get("ai", {})
            # 默认为允许：在 AI 门控开启时，允许 AI 将目标仓位提升到最小仓位，以避免一致性跳过
            allow_force_min = bool(ai_cfg.get("allow_force_min_position", True))
            if self._dca_ai_gate_enabled() and allow_force_min:
                print(f"⚠️ {symbol} 目标仓位 {pos_pct}% 小于最小门槛 {min_pos_pct}%，已按配置提升至最小仓位")
                pos_pct = min_pos_pct
                try:
                    decision["position_percent"] = pos_pct
                except Exception:
                    pass
            else:
                print(f"⚠️ {symbol} 目标仓位太小({pos_pct}% < {min_pos_pct}%), 跳过执行")
                self._append_trade_log(
                    symbol=symbol,
                    action=action,
                    decision=decision,
                    quantity=0,
                    entry_price=market_data["realtime"].get("price", 0),
                    result="skipped_small_position",
                    pnl=None,
                )
                return

        # 读取最大仓位（配置项，默认30%）并对目标仓位进行上限约束
        try:
            max_pos_pct = float(self.config.get("trading", {}).get("max_position_percent", 30))
        except Exception:
            max_pos_pct = 30.0

        if pos_pct > max_pos_pct:
            print(f"⚠️ {symbol} 目标仓位({pos_pct}%) 超过最大允许仓位({max_pos_pct}%), 已按上限截断")
            pos_pct = max_pos_pct
            # 同步回 decision 以便日志与后续逻辑一致
            try:
                decision["position_percent"] = pos_pct
            except Exception:
                pass

        # 如果信心度太低，不执行（但允许平仓；HOLD 也不阻断）
        if confidence < 0.5 and action not in ("CLOSE", "HOLD"):
            print(f"⚠️ {symbol} 信心度太低({confidence:.2f})，跳过执行")
            return

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
                res = self._close_position(symbol, decision)
                # 记录平仓到交易日志（如有返回结果与 pnl）
                try:
                    pnl = None
                    if isinstance(res, dict):
                        pnl = res.get("pnl") or res.get("profit")
                    self._append_trade_log(
                        symbol=symbol,
                        action=action,
                        decision=decision,
                        quantity=0,
                        entry_price=current_price,
                        result=(res.get("status") if isinstance(res, dict) else str(res)),
                        pnl=pnl,
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
        position_percent = float(decision.get("position_percent", 0))
        # 限制仓位范围到配置允许的范围 [0, max_position_percent]
        try:
            max_pos_pct = float(self.config.get("trading", {}).get("max_position_percent", 30))
        except Exception:
            max_pos_pct = 30.0
        if position_percent > max_pos_pct:
            print(f"⚠️ {symbol} 目标仓位({position_percent}%) 超过最大允许仓位({max_pos_pct}%), 已按上限截断")
            position_percent = max_pos_pct
            try:
                decision["position_percent"] = position_percent
            except Exception:
                pass
        if position_percent <= 0:
            print(f"⚠️ {symbol} 目标仓位为0，跳过开仓")
            return

        # 检查账户余额
        if total_equity <= 0:
            print(f"⚠️ {symbol} 账户余额为0，无法开仓")
            print("   请确保账户有足够的 USDT 余额")
            return

        # 计算开仓数量
        quantity = self._calculate_order_quantity(symbol, position_percent, total_equity, current_price)
        if quantity <= 0:
            print(f"❌ {symbol} 计算出的数量无效: {quantity}")
            return

        leverage = decision.get("leverage", 1)
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
            else:
                print(f"✅ {symbol} 开多仓成功: {res}")
                self.trade_count += 1
        except Exception as e:
            print(f"❌ {symbol} 开多仓失败: {e}")

    def _open_short(
        self,
        symbol: str,
        decision: Dict[str, Any],
        total_equity: float,
        current_price: float,
    ):
        """开空仓（修正版）"""
        position_percent = float(decision.get("position_percent", 0))
        # 限制仓位范围到配置允许的范围 [0, max_position_percent]
        try:
            max_pos_pct = float(self.config.get("trading", {}).get("max_position_percent", 30))
        except Exception:
            max_pos_pct = 30.0
        if position_percent > max_pos_pct:
            print(f"⚠️ {symbol} 目标仓位({position_percent}%) 超过最大允许仓位({max_pos_pct}%), 已按上限截断")
            position_percent = max_pos_pct
            try:
                decision["position_percent"] = position_percent
            except Exception:
                pass
        if position_percent <= 0:
            print(f"⚠️ {symbol} 目标仓位为0，跳过开空仓")
            return

        # 检查账户余额
        if total_equity <= 0:
            print(f"⚠️ {symbol} 账户余额为0，无法开仓")
            print("   请确保账户有足够的 USDT 余额")
            return

        quantity = self._calculate_order_quantity(symbol, position_percent, total_equity, current_price)
        if quantity <= 0:
            print(f"❌ {symbol} 计算出的数量无效: {quantity}")
            return

        leverage = decision.get("leverage", 1)
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
            else:
                print(f"✅ {symbol} 开空仓成功: {res}")
                self.trade_count += 1
        except Exception as e:
            print(f"❌ {symbol} 开空仓失败: {e}")

    def _calculate_order_quantity(
        self,
        symbol: str,
        position_percent: float,
        total_equity: float,
        current_price: float,
    ) -> float:
        """根据目标仓位与价格计算并校验数量"""
        if position_percent <= 0:
            return 0.0
        if current_price <= 0 or total_equity <= 0:
            return 0.0

        raw_position_value = total_equity * (position_percent / 100)
        if raw_position_value <= 0:
            return 0.0

        raw_quantity = raw_position_value / current_price
        if raw_quantity <= 0:
            return 0.0

        quantity = self.client.format_quantity(symbol, raw_quantity)
        quantity = self.client.ensure_min_notional_quantity(symbol, quantity, current_price)
        return quantity

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
    ):
        """将交易信息追加到 CSV 日志，便于离线统计"""
        try:
            logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
            os.makedirs(logs_dir, exist_ok=True)
            csv_path = os.path.join(logs_dir, "trade_log.csv")
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
                "reason",
            ]
            exists = os.path.exists(csv_path)
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
                        decision.get("reason"),
                    ]
                )
        except Exception as e:
            print(f"⚠️ 写入交易日志失败: {e}")

    def _close_position(self, symbol: str, decision: Dict[str, Any]):
        """平仓"""
        try:
            res = self.trade_executor.close_position(symbol)
            # 检查返回结果中的 status
            if res.get("status") == "error":
                print(f"❌ {symbol} 平仓失败: {res.get('message', '未知错误')}")
            elif res.get("status") != "noop":
                print(f"✅ {symbol} 平仓成功")
                self.trade_count += 1
        except Exception as e:
            print(f"❌ {symbol} 平仓失败: {e}")

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

                # 使用trade_executor的close_position方法
                result = self.trade_executor.close_position(symbol)

                # 检查返回结果中的 status
                if result.get("status") == "error":
                    msg = result.get("message", "未知错误")
                    print(f"   ❌ {symbol} 平仓失败: {msg}")
                elif result.get("status") == "noop":
                    print(f"   ✅ {symbol} 无持仓，无需平仓")
                else:
                    print(f"   ✅ {symbol} 平仓成功")
                    self._write_log(f"平仓: {symbol} (交易对变更)")
                    self.trade_count += 1

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
        if self.strategy_mode == "DCA_ROTATION":
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
        download_delay_seconds = schedule_config.get("download_delay_seconds", 5)
        # 限制 download_delay_seconds 最大为30秒，确保在K线更新后的30s内完成下载/分析
        if download_delay_seconds > 30:
            download_delay_seconds = 30

        # DCA 轮动使用配置的 K 线周期对齐
        if self.strategy_mode == "DCA_ROTATION":
            interval = str(self.dca_config.get("interval", "5m"))
            if interval.endswith("m") and interval[:-1].isdigit():
                interval_seconds = int(interval[:-1]) * 60

        print(f"\n⏱️  交易周期: 每{interval_seconds}秒")
        symbols_list = (
            self._get_dca_symbols()
            if self.strategy_mode == "DCA_ROTATION"
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
                time.time()

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
        if self.strategy_mode == "DCA_ROTATION":
            self._save_dca_state()
        print(f"✅ 本次运行交易次数: {self.trade_count}")
        print(f"✅ 决策记录数量: {len(self.decision_history)}")
        print("🎉 交易机器人已安全退出")
        print("=" * 60)


def main():
    """主函数"""
    # 强制实盘模式：在程序入口处确保 BINANCE_DRY_RUN 未设置为 1
    os.environ["BINANCE_DRY_RUN"] = "0"
    print("⚠️ 强制设置为实盘模式：BINANCE_DRY_RUN=0（将进行真实下单）")
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()
