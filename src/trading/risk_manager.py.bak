"""
风险管理器
负责风险控制和检查
"""

from datetime import datetime
from typing import Any, Dict


class RiskManager:
    """风险管理器"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化风险管理器

        Args:
            config: 交易配置
        """
        self.config = config
        self.daily_loss = 0.0  # 今日亏损
        self.daily_start_balance = 0.0  # 今日起始余额
        self.consecutive_losses = 0  # 连续亏损次数
        self.last_reset_date = datetime.now().date()

    def check_position_size(
        self, symbol: str, quantity: float, price: float, total_equity: float
    ) -> tuple[bool, str]:
        """
        检查仓位大小是否超限

        Returns:
            (是否通过, 错误消息)
        """
        trading_config = self.config.get("trading", {})

        min_percent = trading_config.get("min_position_percent", 10) / 100
        max_percent = trading_config.get("max_position_percent", 30) / 100
        reserve_percent = trading_config.get("reserve_percent", 20) / 100

        # 计算持仓价值
        position_value = quantity * price

        # 计算仓位占比
        position_percent = position_value / total_equity if total_equity > 0 else 0

        # 检查最小仓位（允许等于最小值）
        # 为了避免浮点运算的微小误差导致等于边界时被误判为过小，加入微小容差
        tol = 1e-6
        if position_percent + tol < min_percent:
            return False, (
                f"仓位过小（{position_percent * 100:.1f}% < 最小要求{min_percent * 100:.0f}%）"
            )

        # 检查最大仓位
        # 检查最大仓位（也加入容差以避免边界误判）
        if position_percent > max_percent + tol:
            return False, (
                f"仓位过大（{position_percent * 100:.1f}% > 最大限制{max_percent * 100:.0f}%）"
            )

        # 检查预留资金
        used_margin = position_value  # 简化
        if used_margin > total_equity * (1 - reserve_percent):
            return False, f"违反预留资金要求（需保留{reserve_percent * 100}%）"

        return True, ""

    def check_max_daily_loss(self, current_balance: float) -> tuple[bool, str]:
        """
        检查每日最大亏损

        Returns:
            (是否通过, 错误消息)
        """
        # 检查是否需要重置日期
        current_date = datetime.now().date()
        if current_date != self.last_reset_date:
            self.daily_loss = 0.0
            self.daily_start_balance = current_balance
            self.last_reset_date = current_date

        risk_config = self.config.get("risk", {})
        # Accept flexible config formats for max_daily_loss_percent:
        # - Common legacy: 10  (means 10%)
        # - Decimal percentage: 0.2 (user may intend 20%)
        # - Fraction: 0.002 (means 0.2%)
        # Heuristic: if value > 1 -> treat as percent (divide by 100).
        # Otherwise treat the value as the fractional percentage directly (0.2 => 20%).
        raw_max = risk_config.get("max_daily_loss_percent", 10)
        try:
            raw_val = float(raw_max)
        except Exception:
            raw_val = 10.0

        if raw_val > 1.0:
            max_loss_percent = raw_val / 100.0
        else:
            # raw_val <= 1.0: interpret directly as fraction of 1.0 (0.2 => 20%)
            max_loss_percent = raw_val

        if self.daily_start_balance == 0:
            self.daily_start_balance = current_balance

        # 计算今日亏损
        daily_loss = self.daily_start_balance - current_balance
        loss_percent = (
            daily_loss / self.daily_start_balance if self.daily_start_balance > 0 else 0
        )

        if loss_percent >= max_loss_percent:
            return False, f"触发每日最大亏损限制（{loss_percent *
                                        100:.2f}% >= {max_loss_percent *
                                                      100}%）"

        return True, ""

    def check_max_consecutive_losses(self) -> tuple[bool, str]:
        """
        检查最大连续亏损次数

        Returns:
            (是否通过, 错误消息)
        """
        risk_config = self.config.get("risk", {})
        max_consecutive = risk_config.get("max_consecutive_losses", 5)

        if self.consecutive_losses >= max_consecutive:
            return False, f"触发最大连续亏损限制（{
                self.consecutive_losses}次 >= {max_consecutive}次）"

        return True, ""

    def record_trade(self, pnl: float):
        """
        记录交易结果，用于跟踪连续亏损

        Args:
            pnl: 盈亏金额（正数=盈利，负数=亏损）
        """
        if pnl < 0:
            # 亏损
            self.consecutive_losses += 1
        else:
            # 盈利，重置连续亏损
            self.consecutive_losses = 0

    def check_all_risk_limits(
        self,
        symbol: str,
        quantity: float,
        price: float,
        total_equity: float,
        current_balance: float,
    ) -> tuple[bool, list]:
        """
        检查所有风险限制

        Returns:
            (是否通过, 错误消息列表)
        """
        errors = []

        # 检查仓位大小
        ok, msg = self.check_position_size(symbol, quantity, price, total_equity)
        if not ok:
            errors.append(msg)

        # 检查每日亏损
        ok, msg = self.check_max_daily_loss(current_balance)
        if not ok:
            errors.append(msg)

        # 检查连续亏损
        ok, msg = self.check_max_consecutive_losses()
        if not ok:
            errors.append(msg)

        return len(errors) == 0, errors

    def should_close_position(
        self, position: Dict[str, Any], total_equity: float
    ) -> tuple[bool, str]:
        """
        判断是否应该平仓（风控触发）

        例如：持仓亏损超过某个阈值

        Args:
            position: 持仓信息
            total_equity: 总权益

        Returns:
            (是否应该平仓, 原因)
        """
        unrealized_pnl = position.get("unrealized_pnl", 0)

        # 如果亏损超过总权益的5%，建议平仓
        if unrealized_pnl < 0 and abs(unrealized_pnl) > total_equity * 0.05:
            return True, f"持仓亏损过大（{unrealized_pnl:.2f} USDT）"

        return False, ""
