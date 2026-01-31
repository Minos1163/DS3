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

当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 交易规则

### 账户信息
- 币种: {symbol}
- 资金类型: 永续期货合约
- 支持双向交易: 可以做多(买入)或做空(卖出)
- 杠杆范围: 1-100倍（建议3-10倍）

### 决策原则
请基于以下技术指标和市场数据进行理性分析，给出最优交易决策。
考虑趋势、动量、波动率等因素，合理设置止盈止损。

### 仓位管理
- 最小仓位: {self.config['trading'].get('min_position_percent', 10)}%
- 最大仓位: {self.config['trading'].get('max_position_percent', 30)}%
- 预留资金: {self.config['trading'].get('reserve_percent', 20)}%

### 风险控制
- 最大每日亏损: {self.config['risk'].get('max_daily_loss_percent', 10)}%
- 最大连续亏损: {self.config['risk'].get('max_consecutive_losses', 5)}次
- 建议止损: -{self.config['risk'].get('stop_loss_default_percent', 2) * 100}%
- 建议止盈: +{self.config['risk'].get('take_profit_default_percent', 5) * 100}%

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
    "leverage": 1-100,
    "position_percent": 10-30,
    "take_profit_percent": 5.0,
    "stop_loss_percent": -2.0,
    "reason": "1-2句话说明决策理由，包含关键指标和值"
}}

### 字段说明:
- action: BUY_OPEN(开多)/SELL_OPEN(开空)/CLOSE(平仓)/HOLD(持有)
- confidence: 信心度 0.0-1.0
- leverage: 杠杆倍数 1-100
- position_percent: 仓位百分比 10-30
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
        f"""
        构建多币种统一分析提示词

        Args:
            all_symbols_data: {{symbol: {{market_data, position}}}}
            all_positions: {{symbol: position_info}}
            account_summary: 账户摘要
            history: 历史决策记录

        Returns:
            完整的多币种提示词
        """
        prompt = f"""
    你是一位专业的日内交易员，需要同时分析多个币种并给出每个币种的独立交易决策。

    当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

    ## 交易账户
    - 账户类型: Binance U本位永续合约
    - 支持双向交易: 可以做多(买入)或做空(卖出)
    - 杠杆范围: 1-100倍（建议3-10倍）

    ### 仓位管理
    - 最小仓位: {self.config['trading'].get('min_position_percent', 10)}%
    - 最大仓位: {self.config['trading'].get('max_position_percent', 30)}%
    - 每个币种独立决策，不受其他币种影响

    ### 风险控制
    - 最大止损: -{self.config['risk'].get('stop_loss_default_percent', 2) * 100}%
    - 建议止盈: +{self.config['risk'].get('take_profit_default_percent', 5) * 100}%

    ## 市场数据

    {self._format_all_symbols_data(all_symbols_data)}

    ## 账户状态

    {self._format_account_summary(account_summary) if account_summary else ""}

    ## 历史决策

    {self._format_history(history) if history else "无历史记录"}

    ## 决策要求

    请综合分析市场数据，为每个币种给出独立决策。

    请严格按照以下JSON格式回复（不要有任何额外文本）：
    {{
        "BTCUSDT": {{
            "action": "BUY_OPEN",
            "reason": "多周期上升趋势，RSI44未超买，4hMACD转正",
            "confidence": "HIGH",
            "leverage": 8,
            "position_percent": 20,
            "take_profit_percent": 5.0,
            "stop_loss_percent": -2.0
        }},
        "ETHUSDT": {{
            "action": "SELL_OPEN",
            "reason": "4h RSI超买80，MACD转负，顶部信号",
            "confidence": "MEDIUM",
            "leverage": 5,
            "position_percent": 15,
            "take_profit_percent": 3.0,
            "stop_loss_percent": -1.5
        }},
        "SOLUSDT": {{
            "action": "HOLD",
            "reason": "震荡整理，等待方向突破",
            "confidence": "LOW",
            "leverage": 0,
            "position_percent": 0,
            "take_profit_percent": 0,
            "stop_loss_percent": 0
        }}
    }}

    ### 字段说明
    - action: BUY_OPEN(开多) | SELL_OPEN(开空) | CLOSE(平仓) | HOLD(观望)
    - reason: 1-2句话说明决策理由，包含关键指标和值
    - confidence: HIGH / MEDIUM / LOW
    - leverage: 杠杆倍数 1-100
    - position_percent: 仓位百分比 0-30
    - take_profit_percent: 止盈百分比（如5.0表示止盈5%）
    - stop_loss_percent: 止损百分比（如-2.0表示止损2%）

    注意：
    1. 根据市场趋势灵活选择BUY_OPEN（做多）或SELL_OPEN（做空），不要只做单向交易
    2. 如果action是BUY_OPEN或SELL_OPEN，必须给出合理的止盈止损百分比
    3. BUY_OPEN：take_profit > 0 > stop_loss（上涨止盈，下跌止损）
    4. SELL_OPEN：take_profit < 0 < stop_loss（下跌止盈，上涨止损）
    """
        return prompt.strip()

    def _format_all_symbols_data(self, all_symbols_data: Dict[str, Any]) -> str:
        """格式化所有币种的市场数据"""
        result_lines: List[str] = []

        for symbol, symbol_data in all_symbols_data.items():
            market_data = symbol_data.get("market_data", {}) or {}
            position = symbol_data.get("position")

            coin_name = symbol.replace("USDT", "")

            realtime = market_data.get("realtime", {}) or {}
            price = realtime.get("price") or 0
            change_24h = realtime.get("change_24h") or 0
            change_15m = realtime.get("change_15m") or 0
            funding_rate = realtime.get("funding_rate") or 0
            open_interest = realtime.get("open_interest") or 0

            if funding_rate > 0.0001:
                funding_text = f"多头付费({funding_rate * 100:.4f}%)"
            elif funding_rate < -0.0001:
                funding_text = f"空头付费({abs(funding_rate) * 100:.4f}%)"
            else:
                funding_text = ""

            block = [f"=== {coin_name}/USDT ==="]
            block.append(f"价格: ${price:,.2f} | 24h: {change_24h:+.2f}%")
            block.append(f"15m: {change_15m:+.2f}% | 持仓量: {open_interest:,.0f}")
            block.append(f"资金费率: {funding_rate:.6f} ({funding_text})")

            # 持仓
            if position:
                pos = position
                pnl_percent = pos.get("pnl_percent") or 0
                side = pos.get("side", "N/A")
                amount = pos.get("amount") or 0
                entry_price = pos.get("entry_price") or 0
                unrealized_pnl = pos.get("unrealized_pnl") or 0
                block.append(f"持仓: {side} {amount:.3f} @ ${entry_price:.2f}")
                block.append(f"盈亏: {unrealized_pnl:+.2f} USDT ({pnl_percent:+.2f}%)")
            else:
                block.append("持仓: 无仓位")

            # 多周期指标（简要）
            multi_data = market_data.get("multi_timeframe", {}) or {}
            for interval in ["15m", "30m", "1h", "4h", "1d"]:
                data = multi_data.get(interval) or {}
                ind = data.get("indicators") or {}
                block.append(f"[{interval}] ")
                if not ind:
                    block.append("指标: 暂无数据")
                else:
                    rsi = ind.get("rsi") or 0
                    macd = ind.get("macd") or 0
                    ema20 = ind.get("ema_20") or 0
                    ema50 = ind.get("ema_50") or 0
                    atr = ind.get("atr_14") or 0
                    block.append(
                        f"RSI: {rsi:.1f} | MACD: {macd:.4f} | EMA20: {ema20:.2f} | EMA50: {ema50:.2f}"
                    )
                    block.append(f"ATR: {atr:.2f}")

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
