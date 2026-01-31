"""
AI交易机器人主程序
整合所有模块，实现完整的交易流程
"""

import os
import sys
import time
from datetime import datetime
from io import StringIO
from typing import Any, Callable, Dict, List, Optional, TextIO

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
from src.trading.position_manager import PositionManager
from src.trading.risk_manager import RiskManager
from src.trading.trade_executor import TradeExecutor

# 添加项目根目录到Python路径
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

        # 初始化客户端
        self.client = self._init_binance_client()
        self.ai_client = self._init_ai_client()
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

        # AI组件
        self.prompt_builder = PromptBuilder(self.config)
        self.decision_parser = DecisionParser()
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

    def analyze_all_symbols_with_ai(
        self, all_symbols_data: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """使用AI一次性分析所有币种"""
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
            prompt = self.prompt_builder.build_multi_symbol_analysis_prompt(
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

            response = self.ai_client.analyze_and_decide(prompt)

            # 显示AI推理过程
            reasoning = self.ai_client.get_reasoning(response)

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
            decisions = self.decision_parser.parse_multi_symbol_response(
                response["content"]
            )

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

    def analyze_with_ai(
        self, symbol: str, market_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """使用AI分析并获取决策"""
        try:
            # 获取持仓
            position = self.position_data.get_current_position(symbol)

            # 获取历史决策（最近3条）
            history = [d for d in self.decision_history if d.get("symbol") == symbol][
                -3:
            ]

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

        # 如果信心度太低，不执行
        if confidence < 0.5 and action != "CLOSE":
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
                self._open_short(symbol, decision, total_equity, current_price)

            elif action == "CLOSE":
                # 平仓
                self._close_position(symbol, decision)

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
        if position_percent <= 0:
            print(f"⚠️ {symbol} 目标仓位为0，跳过开仓")
            return

        # 检查账户余额
        if total_equity <= 0:
            print(f"⚠️ {symbol} 账户余额为0，无法开仓")
            print("   请确保账户有足够的 USDT 余额")
            return

        # 计算开仓数量
        quantity = self._calculate_order_quantity(
            symbol, position_percent, total_equity, current_price
        )
        if quantity <= 0:
            print(f"❌ {symbol} 计算出的数量无效: {quantity}")
            return

        leverage = decision.get("leverage", 1)
        take_profit_percent = decision.get("take_profit_percent", 5.0)
        stop_loss_percent = decision.get("stop_loss_percent", -2.0)
        take_profit = current_price * (1 + take_profit_percent / 100)
        stop_loss = current_price * (1 + stop_loss_percent / 100)

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
        if position_percent <= 0:
            print(f"⚠️ {symbol} 目标仓位为0，跳过开空仓")
            return

        # 检查账户余额
        if total_equity <= 0:
            print(f"⚠️ {symbol} 账户余额为0，无法开仓")
            print("   请确保账户有足够的 USDT 余额")
            return

        quantity = self._calculate_order_quantity(
            symbol, position_percent, total_equity, current_price
        )
        if quantity <= 0:
            print(f"❌ {symbol} 计算出的数量无效: {quantity}")
            return

        leverage = decision.get("leverage", 1)
        take_profit_percent = decision.get("take_profit_percent", 5.0)
        stop_loss_percent = decision.get("stop_loss_percent", -2.0)
        take_profit = current_price * (1 - take_profit_percent / 100)  # 做空止盈
        stop_loss = current_price * (1 + abs(stop_loss_percent) / 100)  # 做空止损

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
        quantity = self.client.ensure_min_notional_quantity(
            symbol, quantity, current_price
        )
        return quantity

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
                position_amt = float(position.get("positionAmt", 0))

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
                    f"   现货余额(含LDUSDT): {spot_total:.6f} USDT "
                    f"(USDT: {spot_usdt:.6f}, LDUSDT: {spot_ldusdt:.6f})"
                )
                cycle_log.append(spot_line)
                print(spot_line)
                note_line = (
                    "   提示: LDUSDT 为理财资产，需赎回/划转后才能作为合约保证金"
                )
                cycle_log.append(note_line)
                print(note_line)

        # 方式1：多币种一次性分析（优化）
        if len(symbols) > 1:
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

    def run(self):
        """启动主循环"""
        schedule_config = ConfigLoader.get_schedule_config(self.config)
        # 15分钟周期 (900秒)
        interval_seconds = schedule_config["interval_seconds"]
        download_delay_seconds = schedule_config.get("download_delay_seconds", 5)
        # 限制 download_delay_seconds 最大为30秒，确保在K线更新后的30s内完成下载/分析
        if download_delay_seconds > 30:
            download_delay_seconds = 30

        print(f"\n⏱️  交易周期: 每{interval_seconds}秒 (15分钟)")
        symbols_list = ConfigLoader.get_trading_symbols(self.config)
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
                next_ts = datetime.fromtimestamp(next_boundary).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                print(
                    f"⏳ 等待对齐到下一次K线边界 {next_ts}，再延迟 {download_delay_seconds}s 后开始"
                )
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
                    next_ts = datetime.fromtimestamp(next_boundary).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
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
        print(f"✅ 本次运行交易次数: {self.trade_count}")
        print(f"✅ 决策记录数量: {len(self.decision_history)}")
        print("🎉 交易机器人已安全退出")
        print("=" * 60)


def main():
    """主函数"""
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()
