"""
提示词构建器
负责构建AI提示词
"""

from datetime import datetime
from typing import Any, Dict, List, Optional


class PromptBuilder:
    """提示词构建器"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化提示词构建器

        Args:
            config: 交易配置
        """
        self.config = config
        self.ai_config = config.get("ai", {})

    def build_analysis_prompt(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        position: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        构建分析提示词

        Args:
            symbol: 交易对
            market_data: 市场数据
            position: 当前持仓信息
            history: 历史决策记录

        Returns:
            完整的提示词字符串
        """
        prompt = f"""
# 加密货币期货交易分析

当前时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## 交易规则

### 账户信息
- 币种: {symbol}
- 资金类型: 永续期货合约
- 支持双向交易: 可以做多(买入)或做空(卖出)
- 杠杆范围: {self.config.get("fund_flow", {}).get("min_leverage", self.config["trading"].get("min_leverage", 3))}-{self.config.get("fund_flow", {}).get("max_leverage", self.config["trading"].get("max_leverage", 5))}倍（默认{self.config.get("fund_flow", {}).get("default_leverage", self.config["trading"].get("default_leverage", 4))}倍）

### 决策原则
请基于以下技术指标和市场数据进行理性分析，给出最优交易决策。
考虑趋势、动量、波动率等因素，合理设置止盈止损。

### 仓位管理
- 最小仓位: {self.config["trading"].get("min_position_percent", 10)}%
- 最大仓位: {self.config["trading"].get("max_position_percent", 30)}%
- 预留资金: {self.config["trading"].get("reserve_percent", 20)}%

- ### 风险控制
- 最大每日亏损: {self.config["risk"].get("max_daily_loss_percent", 10)}%
- 最大连续亏损: {self.config["risk"].get("max_consecutive_losses", 5)}次
- 建议止损: -{self.config["risk"].get("stop_loss_default_percent", 2) * 100}%
- 建议止盈: +{self.config["risk"].get("take_profit_default_percent", 5) * 100}%

## 市场数据

{self._format_market_data(symbol, market_data)}

## 当前持仓

{self._format_position(position) if position else "无持仓"}

## 历史决策

{self._format_history(history) if history else "无历史记录"}

## 决策要求

请严格按照以下JSON格式回复（不要有任何额外文本）:

{{
    "action": "BUY_OPEN" | "SELL_OPEN" | "CLOSE" | "HOLD",
    "confidence": 0.0-1.0,
    "leverage": {self.config.get("fund_flow", {}).get("default_leverage", self.config["trading"].get("default_leverage", 4))},
    "position_percent": {self.config["trading"].get("max_position_percent", 30)},
    "take_profit_percent": 5.0,
    "stop_loss_percent": -2.0,
    "reason": "1-2句话说明决策理由，包含关键指标和值"
}}

### 字段说明:
- action: BUY_OPEN(开多)/SELL_OPEN(开空)/CLOSE(平仓)/HOLD(持有)
- confidence: 信心度 0.0-1.0
- leverage: 杠杆倍数（范围 {self.config.get("fund_flow", {}).get("min_leverage", self.config["trading"].get("min_leverage", 3))}-{self.config.get("fund_flow", {}).get("max_leverage", self.config["trading"].get("max_leverage", 5))}）
- position_percent: 仓位百分比（范围 {self.config["trading"].get("min_position_percent", 10)}-{self.config["trading"].get("max_position_percent", 30)}）
- take_profit_percent: 止盈百分比（相对于开仓价）
- stop_loss_percent: 止损百分比（相对于开仓价）
- reason: 决策理由（关键指标+值）

请分析市场数据，给出最优决策。
"""
        return prompt.strip()

    def _format_market_data(self, symbol: str, market_data: Dict[str, Any]) -> str:
        """格式化市场数据"""
        realtime = market_data.get("realtime", {})
        multi_data = market_data.get("multi_timeframe", {})

        result = f"### {symbol} 实时行情\n"

        # 确保值不为None
        price = realtime.get("price") or 0
        change_24h = realtime.get("change_24h") or 0
        change_15m = realtime.get("change_15m") or 0
        funding_rate = realtime.get("funding_rate") or 0
        open_interest = realtime.get("open_interest") or 0

        result += f"- 当前价格: ${price:,.2f}\n"
        result += f"- 24h涨跌: {change_24h:.2f}%\n"
        result += f"- 15m涨跌: {change_15m:.2f}%\n"
        result += f"- 资金费率: {funding_rate:.6f}\n"
        result += f"- 持仓量: {open_interest:,.0f}\n"

        # 多周期数据
        for interval, data in multi_data.items():
            if "indicators" not in data:
                continue

            ind = data["indicators"]
            df = data.get("dataframe")

            result += f"\n### {interval}周期\n"

            # 显示最近3根K线
            if df is not None and len(df) >= 3:
                for i, row in df.tail(3).iterrows():
                    close = row["close"]
                    change = ((row["close"] - row["open"]) / row["open"]) * 100
                    result += f"- K线: C${close:.2f} ({change:+.2f}%)\n"

            # 技术指标
            rsi = ind.get("rsi") or 0
            macd = ind.get("macd") or 0
            macd_signal = ind.get("macd_signal") or 0
            macd_hist = ind.get("macd_histogram") or 0
            ema20 = ind.get("ema_20") or 0
            ema50 = ind.get("ema_50") or 0
            atr = ind.get("atr_14") or 0

            result += f"- RSI(14): {rsi:.1f}\n"
            result += f"- MACD: {macd:.2f}, "
            result += f"Signal: {macd_signal:.2f}, "
            result += f"Hist: {macd_hist:.2f}\n"
            result += f"- EMA20: {ema20:.2f}, "
            result += f"EMA50: {ema50:.2f}\n"
            result += f"- ATR(14): {atr:.2f}\n"

            if "volume_ratio" in ind:
                vol_ratio = ind.get("volume_ratio") or 0
                result += f"- 成交量比: {vol_ratio:.1f}%\n"

        return result

    def _format_position(self, position: Dict[str, Any]) -> str:
        """格式化持仓信息"""
        result = f"- 方向: {position.get('side', 'N/A')}\n"
        result += f"- 数量: {position.get('amount', 0)}\n"
        result += f"- 开仓价: ${position.get('entry_price', 0):,.2f}\n"
        result += f"- 当前价: ${position.get('mark_price', 0):,.2f}\n"
        result += f"- 杠杆: {position.get('leverage', 0)}x\n"
        result += f"- 未实现盈亏: {position.get('unrealized_pnl', 0):.2f} USDT "
        result += f"({position.get('pnl_percent', 0):.2f}%)\n"
        return result

    def _format_history(self, history: List[Dict[str, Any]]) -> str:
        """格式化历史决策"""
        if not history:
            return "无历史记录"

        result = ""
        for i, h in enumerate(history[-3:], 1):  # 只显示最近3条
            result += f"\n### 决策{i} ({h.get('timestamp', 'N/A')})\n"
            result += f"- 动作: {h.get('action', 'N/A')}\n"
            result += f"- 信心: {h.get('confidence', 0):.2f}\n"
            result += f"- 理由: {h.get('reason', 'N/A')}\n"

        return result

    def build_multi_symbol_analysis_prompt(
        self,
        all_symbols_data: Dict[str, Any],
        all_positions: Dict[str, Any],
        account_summary: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """构建多币种统一分析提示词"""
        default_lev = self.config.get("fund_flow", {}).get("default_leverage", self.config["trading"].get("default_leverage", 4))
        min_lev = self.config.get("fund_flow", {}).get("min_leverage", self.config["trading"].get("min_leverage", 3))
        max_lev = self.config.get("fund_flow", {}).get("max_leverage", self.config["trading"].get("max_leverage", 5))
        max_pos = self.config["trading"].get("max_position_percent", 60)
        configured_symbols = self.config.get("trading", {}).get("symbols", []) or []
        symbol_text = "、".join(str(s) for s in configured_symbols) if configured_symbols else "按配置文件交易标的"
        sample_buy_symbol = str(configured_symbols[0]) if configured_symbols else "BTCUSDT"
        sample_hold_symbol = str(configured_symbols[1]) if len(configured_symbols) > 1 else sample_buy_symbol

        prompt = f"""
# 合约短线交易决策系统

时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## 主策略
- 交易标的: 以 JSON 配置为准，当前监控 {symbol_text}
- 主周期: 1H，只负责方向过滤
- 入场周期: 15M，只负责起爆共振
- 主方向规则: 价格在 1H EMA30 上方只做多；价格在 1H EMA30 下方只做空；1H EMA30 走平则观望
- 入场规则A: 15M EMA10/EMA30 同向 + MACD 金叉/零轴上 做多；15M EMA10/EMA30 同向 + MACD 死叉/零轴下 做空
- 入场规则B: 15M EMA10/EMA30 同向 + 布林上轨突破且开口扩大 做多；15M EMA10/EMA30 同向 + 布林下轨跌破且开口扩大 做空
- 动态止损: 以 15M EMA30 破位为硬止损锚点，允许适度缓冲，止损可放宽到约 1%-2.5%
- 动态止盈: 先在 5%-10% 区间减仓 1/2，剩余仓位改看 15M EMA10 runner 出场
- 5m/3m: 仅作辅助过滤和置信度参考，不能推翻 1H/15M 主链

仓位: 单币最大{max_pos}% | 杠杆: {min_lev}-{max_lev}x（默认{default_lev}x）

## 市场数据
{self._format_all_symbols_data(all_symbols_data)}

## 账户状态
{self._format_account_summary(account_summary) if account_summary else ""}

## 输出要求
- 只在 1H 方向明确且 15M 至少一套共振成立时开仓
- 做多/做空方向不能与 1H EMA30 方向冲突
- 原因只写 1H 方向 + 15M 共振类型 + 15M EMA30/EMA10 动态风控
- 若条件不全，统一输出 HOLD

## 输出格式 (纯JSON,无任何额外文本)

{{
    "{sample_buy_symbol}": {{
        "action": "BUY_OPEN",
        "reason": "1H站上EMA30,15M EMA+MACD共振做多,止损看15M EMA30,止盈后看15M EMA10",
        "confidence": "HIGH",
        "leverage": {default_lev},
        "position_percent": {max_pos},
        "take_profit_percent": 0,
        "stop_loss_percent": 0
    }},
    "{sample_hold_symbol}": {{
        "action": "HOLD",
        "reason": "1H方向不清晰或15M共振不足",
        "confidence": "LOW",
        "leverage": 0,
        "position_percent": 0,
        "take_profit_percent": 0,
        "stop_loss_percent": 0
    }}
}}

⚠️ 关键要求:
- JSON键: 完整交易对名称
- 严格执行 1H -> 15M 顺序，不允许倒置
- 动态止损锚点统一参考 15M EMA30，盈利后 runner 改看 15M EMA10
- 5m/3m 只能作为辅助说明，不能成为主开仓理由
"""
        return prompt.strip()

    def _format_all_symbols_data(self, all_symbols_data: Dict[str, Any]) -> str:
        """格式化所有币种的市场数据（聚焦 1H/15M 主链，5m 仅辅助）"""
        result_lines: List[str] = []

        for symbol, symbol_data in all_symbols_data.items():
            market_data = symbol_data.get("market_data", {}) or {}
            position = symbol_data.get("position")

            coin_name = symbol.replace("USDT", "")
            realtime = market_data.get("realtime", {}) or {}
            price = realtime.get("price") or 0
            change_24h = realtime.get("change_24h") or 0

            block = [f"=== {coin_name}/USDT ==="]
            block.append(f"价格: ${price:,.2f} | 24h变化: {change_24h:+.2f}%")

            if position:
                pnl_percent = position.get("pnl_percent") or 0
                side = position.get("side", "N/A")
                entry_price = position.get("entry_price") or 0
                mark_price = position.get("mark_price") or 0
                block.append(f"✅ 持仓 {side} @ ${entry_price:.2f} → ${mark_price:.2f} (盈亏{pnl_percent:+.2f}%)")
            else:
                block.append("⭕ 无持仓")

            multi_data = market_data.get("multi_timeframe", {}) or {}
            for interval in ("1h", "15m", "5m"):
                data = multi_data.get(interval) or {}
                ind = data.get("indicators") or {}
                if not ind:
                    block.append(f"[{interval}] 数据缺失")
                    continue

                ema10 = ind.get("ema_10") or 0
                ema30 = ind.get("ema_30") or 0
                macd = ind.get("macd") or 0
                macd_hist = ind.get("macd_histogram") or 0
                bb_middle = ind.get("bollinger_middle") or 0
                bb_upper = ind.get("bollinger_upper") or 0
                bb_lower = ind.get("bollinger_lower") or 0
                trend = "上方" if price > ema30 else "下方" if price < ema30 else "贴近"
                macd_state = "零轴上/偏多" if macd >= 0 or macd_hist >= 0 else "零轴下/偏空"
                if interval in ("1h", "15m"):
                    block.append(
                        f"[{interval}] EMA10/EMA30={ema10:.2f}/{ema30:.2f} | 现价相对EMA30: {trend} | MACD={macd:.4f}({macd_state}) | BB中/上/下={bb_middle:.2f}/{bb_upper:.2f}/{bb_lower:.2f}"
                    )
                else:
                    block.append(f"[{interval}] 辅助观察: MACD={macd:.4f} | BB中轨={bb_middle:.2f}")
            result_lines.append("\n".join(block))

        return "\n\n".join(result_lines)

    def _format_account_summary(self, account_summary: Dict[str, Any]) -> str:
        """格式化账户摘要"""
        if not account_summary:
            return ""
        equity = account_summary.get("equity", 0)
        available = account_summary.get("available_balance", 0)
        unrealized_pnl = account_summary.get("total_unrealized_pnl", 0)

        return f"""
账户余额: {equity:.2f} USDT
可用余额: {available:.2f} USDT
未实现盈亏: {unrealized_pnl:+.2f} USDT
"""

