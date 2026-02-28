"""
风险管理器
负责风险控制和检查
"""

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Optional, Tuple


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

        # ========== MACD/CVD 冲突保护状态 ==========
        # 记录每个交易对的连续冲突次数 - key: (symbol, side)
        self._conflict_counters: Dict[Tuple[str, str], int] = {}
        # cooldown: key: (symbol, side) -> last protect timestamp (epoch seconds)
        self._last_protect_ts: Dict[Tuple[str, str], float] = {}
        # 冲突保护配置阈值（支持 risk.conflict_protection 覆盖）
        risk_cfg = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
        conflict_cfg = risk_cfg.get("conflict_protection", {}) if isinstance(risk_cfg.get("conflict_protection"), dict) else {}
        self._conflict_cfg = {
            "cvd_min": float(conflict_cfg.get("cvd_min", 0.3)),                    # CVD 最小阈值（避免噪声）
            "conflict_hard": float(conflict_cfg.get("conflict_hard", 0.8)),        # 回退路径重度冲突阈值
            "macd_weak": float(conflict_cfg.get("macd_weak", 0.4)),                # MACD 弱信号阈值
            "light_confirm_bars": int(conflict_cfg.get("light_confirm_bars", 2)),  # 连续2根才进入 LIGHT
            "hard_confirm_bars": int(conflict_cfg.get("hard_confirm_bars", 4)),    # 连续4根才进入 HARD
            "same_macd_min": float(conflict_cfg.get("same_macd_min", 0.25)),       # 判定"MACD 与持仓同向"最小强度
            "cooldown_sec": float(conflict_cfg.get("cooldown_sec", 60.0)),         # 同一(symbol,side)保护动作冷却
            "neutral_decay": int(conflict_cfg.get("neutral_decay", 1)),             # 中性时冲突计数衰减步长
            "trend_light_tighten": bool(conflict_cfg.get("trend_light_tighten", False)),  # TREND+LIGHT 默认不收紧止损
            "ev_conflict_light_min": float(conflict_cfg.get("ev_conflict_light_min", 0.12)),
            "ev_conflict_hard_min": float(conflict_cfg.get("ev_conflict_hard_min", 0.30)),
            "ev_conflict_hard_delta": float(conflict_cfg.get("ev_conflict_hard_delta", 0.08)),
            "lw_assist_min": float(conflict_cfg.get("lw_assist_min", 0.18)),
        }

        # ========== 冲突保护行为统计器 ==========
        # 全局累计
        self._protect_stats: Dict[str, Any] = {
            "levels": {"confirm": 0, "neutral": 0, "conflict_light": 0, "conflict_hard": 0},
            "actions": {
                "tighten": {"attempt": 0, "applied": 0, "skipped_cooldown": 0, "skipped_not_tighter": 0, "skipped_not_ready": 0, "error": 0},
                "breakeven": {"attempt": 0, "applied": 0, "skipped_cooldown": 0, "skipped_not_tighter": 0, "skipped_not_ready": 0, "error": 0},
                "reduce": {"triggered": 0},
            },
        }
        # (symbol, side) 维度累计
        self._protect_stats_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # 最近事件（便于复盘）
        self._protect_events: Deque[Dict[str, Any]] = deque(maxlen=200)

    # ========== 冲突保护统计器方法 ==========

    def _get_or_create_key_stats(self, key: Tuple[str, str]) -> Dict[str, Any]:
        """获取或创建 (symbol, side) 维度的统计"""
        if key not in self._protect_stats_by_key:
            self._protect_stats_by_key[key] = {
                "levels": {"confirm": 0, "neutral": 0, "conflict_light": 0, "conflict_hard": 0},
                "actions": {
                    "tighten": {"attempt": 0, "applied": 0, "skipped_cooldown": 0, "skipped_not_tighter": 0, "skipped_not_ready": 0, "error": 0},
                    "breakeven": {"attempt": 0, "applied": 0, "skipped_cooldown": 0, "skipped_not_tighter": 0, "skipped_not_ready": 0, "error": 0},
                    "reduce": {"triggered": 0},
                },
            }
        return self._protect_stats_by_key[key]

    def record_protection_level(
        self,
        symbol: str,
        side: str,
        level: str,
        reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ):
        """记录一次 protection level（confirm/neutral/light/hard）出现"""
        level = str(level or "neutral")
        side_u = str(side).upper()
        key = (symbol, side_u)
        if level not in self._protect_stats["levels"]:
            level = "neutral"
        self._protect_stats["levels"][level] += 1
        ks = self._get_or_create_key_stats(key)
        ks["levels"][level] += 1
        ev: Dict[str, Any] = {"ts": datetime.now().isoformat(), "symbol": symbol, "side": side_u, "type": "level", "level": level, "reason": reason}
        if extra:
            ev["extra"] = extra
        self._protect_events.append(ev)

    def record_protection_action(
        self,
        symbol: str,
        side: str,
        action: str,
        outcome: str,
        level: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ):
        """
        记录保护动作执行结果

        Args:
            symbol: 交易对
            side: 持仓方向
            action: tighten / breakeven / reduce
            outcome: applied | attempt | skipped_cooldown | skipped_not_tighter | skipped_not_ready | error | triggered
            level: 保护级别
            detail: 额外详情
        """
        action = str(action)
        outcome = str(outcome)
        side_u = str(side).upper()
        key = (symbol, side_u)
        ks = self._get_or_create_key_stats(key)

        if action in ("tighten", "breakeven"):
            if outcome not in self._protect_stats["actions"][action]:
                outcome = "error"
            self._protect_stats["actions"][action][outcome] += 1
            ks["actions"][action][outcome] += 1
        elif action == "reduce":
            # reduce 目前只统计 triggered
            self._protect_stats["actions"]["reduce"]["triggered"] += 1
            ks["actions"]["reduce"]["triggered"] += 1

        ev: Dict[str, Any] = {
            "ts": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side_u,
            "type": "action",
            "action": action,
            "outcome": outcome,
        }
        if level:
            ev["level"] = str(level)
        if detail:
            ev["detail"] = detail
        self._protect_events.append(ev)

    def get_conflict_protection_stats(self) -> Dict[str, Any]:
        """返回统计快照（可用于 API/日志）"""
        return {
            "global": self._protect_stats,
            "by_key": self._protect_stats_by_key,
            "recent_events": list(self._protect_events),
        }

    def reset_conflict_protection_stats(self):
        """清空统计（不清空 counters / cooldown 状态）"""
        self._protect_stats["levels"] = {"confirm": 0, "neutral": 0, "conflict_light": 0, "conflict_hard": 0}
        self._protect_stats["actions"] = {
            "tighten": {"attempt": 0, "applied": 0, "skipped_cooldown": 0, "skipped_not_tighter": 0, "skipped_not_ready": 0, "error": 0},
            "breakeven": {"attempt": 0, "applied": 0, "skipped_cooldown": 0, "skipped_not_tighter": 0, "skipped_not_ready": 0, "error": 0},
            "reduce": {"triggered": 0},
        }
        self._protect_stats_by_key.clear()
        self._protect_events.clear()

    def format_conflict_protection_stats(self, top_n: int = 6) -> str:
        """一行摘要，适合定期打印"""
        g = self._protect_stats
        lv = g["levels"]
        act = g["actions"]
        parts = [
            f"LV(confirm={lv['confirm']}, neutral={lv['neutral']}, light={lv['conflict_light']}, hard={lv['conflict_hard']})",
            "ACT("
            f"tighten[a={act['tighten']['attempt']}, ok={act['tighten']['applied']}, cd={act['tighten']['skipped_cooldown']}, nt={act['tighten']['skipped_not_tighter']}, nr={act['tighten']['skipped_not_ready']}, err={act['tighten']['error']}], "
            f"be[a={act['breakeven']['attempt']}, ok={act['breakeven']['applied']}, cd={act['breakeven']['skipped_cooldown']}, nt={act['breakeven']['skipped_not_tighter']}, nr={act['breakeven']['skipped_not_ready']}, err={act['breakeven']['error']}], "
            f"reduce={act['reduce']['triggered']}"
            ")",
        ]

        # top symbols by hard+light count
        keys = []
        for (sym, side), s in self._protect_stats_by_key.items():
            score = int(s["levels"]["conflict_hard"]) * 3 + int(s["levels"]["conflict_light"])
            if score > 0:
                keys.append(((sym, side), score, s))
        keys.sort(key=lambda x: x[1], reverse=True)
        if keys:
            tops = []
            for (sym, side), score, s in keys[: max(1, int(top_n))]:
                tops.append(
                    f"{sym}:{side}(light={s['levels']['conflict_light']},hard={s['levels']['conflict_hard']},"
                    f"reduce={s['actions']['reduce']['triggered']})"
                )
            parts.append("TOP[" + ", ".join(tops) + "]")
        return " | ".join(parts)

    def check_position_size(self, symbol: str, quantity: float, price: float, total_equity: float) -> tuple[bool, str]:
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
            return False, (f"仓位过小（{position_percent * 100:.1f}% < 最小要求{min_percent * 100:.0f}%）")

        # 检查最大仓位
        # 检查最大仓位（也加入容差以避免边界误判）
        if position_percent > max_percent + tol:
            return False, (f"仓位过大（{position_percent * 100:.1f}% > 最大限制{max_percent * 100:.0f}%）")

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
        loss_percent = daily_loss / self.daily_start_balance if self.daily_start_balance > 0 else 0

        if loss_percent >= max_loss_percent:
            return False, f"触发每日最大亏损限制（{loss_percent * 100:.2f}% >= {max_loss_percent * 100}%）"

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
            return False, f"触发最大连续亏损限制（{self.consecutive_losses}次 >= {max_consecutive}次）"

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

    def should_close_position(self, position: Dict[str, Any], total_equity: float) -> tuple[bool, str]:
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

    # ========== MACD/CVD 冲突保护机制 ==========

    def check_position_protection(
        self,
        symbol: str,
        position_side: str,
        macd_hist_norm: float,
        cvd_norm: float,
        ev_direction: Optional[str] = None,
        ev_score: Optional[float] = None,
        lw_direction: Optional[str] = None,
        lw_score: Optional[float] = None,
        macd_strength: Optional[float] = None,
        now_ts: Optional[float] = None,
        market_regime: Optional[str] = None,
        ma10_ltf: Optional[float] = None,
        last_close: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        检查持仓保护状态（基于 MACD/CVD 冲突）

        用于保护已有持仓，而非决定方向。
        - 确认增强：MACD 同向 + CVD 同向 → 允许加仓/持有
        - 轻度冲突：MACD 同向 + CVD 反向（中等） → 冻结加仓（TREND 默认不收紧止损）
        - 重度冲突：MACD 同向 + CVD 反向（强）+ 连续 → 减仓/提前保本

        Args:
            symbol: 交易对
            position_side: 持仓方向 "LONG" / "SHORT"
            macd_hist_norm: MACD hist 归一化值 [-1, 1]
            cvd_norm: CVD 归一化值 [-1, 1]
            ev_direction: EV 方向 ("LONG_ONLY"/"SHORT_ONLY"/"BOTH")
            ev_score: EV 分数
            lw_direction: LW 方向 ("LONG_ONLY"/"SHORT_ONLY"/"BOTH")
            lw_score: LW 分数
            macd_strength: MACD 强度（可选，默认用 abs(macd_hist_norm)）
            now_ts: 当前时间戳（可选，用于 cooldown 检查）
            ma10_ltf: 低周期 MA10（可选，用于趋势结构判定）
            last_close: 低周期最新收盘价（可选，用于趋势结构判定）

        Returns:
            {
                "level": "confirm" / "neutral" / "conflict_light" / "conflict_hard",
                "allow_add": bool,              # 是否允许加仓
                "tighten_trailing": bool,       # 是否收紧 trailing
                "reduce_position_pct": float,   # 减仓比例 (0.0 ~ 1.0)
                "force_break_even": bool,       # 是否强制保本止损
                "breakeven_mode": str,          # "profit_only" / "emergency_tighten"
                "breakeven_fee_buffer": float,  # 手续费buffer（比例）
                "risk_penalty": float,          # 风险惩罚系数 [0, 1]
                "conflict_bars": int,           # 连续冲突次数
                "cooldown_active": bool,        # 是否处于 cooldown 期间
                "reason": str,                  # 原因说明
            }
        """
        import time
        cfg = self._conflict_cfg
        cvd_min = cfg["cvd_min"]
        conflict_hard = cfg["conflict_hard"]
        macd_weak = cfg["macd_weak"]
        light_confirm_bars = max(1, int(cfg.get("light_confirm_bars", 2)))
        hard_confirm_bars = max(light_confirm_bars, int(cfg.get("hard_confirm_bars", 4)))
        same_macd_min = cfg.get("same_macd_min", 0.25)
        cooldown_sec = float(cfg.get("cooldown_sec", 60.0))
        neutral_decay = int(cfg.get("neutral_decay", 1))
        trend_light_tighten = bool(cfg.get("trend_light_tighten", False))

        regime = str(market_regime or "").upper()
        is_trend = regime == "TREND"
        ma10_ref = abs(float(ma10_ltf or 0.0))
        close_ref = float(last_close or 0.0)

        def _trend_structure_intact() -> bool:
            if (not is_trend) or ma10_ref <= 0 or close_ref <= 0:
                return False
            if position_side == "LONG":
                return close_ref >= ma10_ref
            if position_side == "SHORT":
                return close_ref <= ma10_ref
            return False

        key = (symbol, str(position_side).upper())
        # caller can pass loop timestamp; otherwise fallback to "now"
        ts_now = float(now_ts) if now_ts is not None else time.time()

        # 默认返回值
        result = {
            "level": "neutral",
            "allow_add": True,
            "tighten_trailing": False,
            "reduce_position_pct": 0.0,
            "force_break_even": False,
            "breakeven_mode": "",
            "breakeven_fee_buffer": 0.0,
            "risk_penalty": 0.0,
            "conflict_bars": 0,
            "cooldown_active": False,
            "reason": "",
        }

        # 计算方向
        macd_dir = 1 if macd_hist_norm > 0 else -1 if macd_hist_norm < 0 else 0
        cvd_dir = 1 if cvd_norm > 0 else -1 if cvd_norm < 0 else 0
        pos_dir = 1 if position_side == "LONG" else -1
        ev_dir_raw = str(ev_direction or "BOTH").upper()
        lw_dir_raw = str(lw_direction or "BOTH").upper()
        ev_dir = 1 if ev_dir_raw == "LONG_ONLY" else -1 if ev_dir_raw == "SHORT_ONLY" else 0
        lw_dir = 1 if lw_dir_raw == "LONG_ONLY" else -1 if lw_dir_raw == "SHORT_ONLY" else 0
        ev_score_abs = abs(float(ev_score or 0.0))
        lw_score_abs = abs(float(lw_score or 0.0))

        # MACD 强度
        strength = macd_strength if macd_strength is not None else abs(macd_hist_norm)

        # cooldown check (applies only when we'd take a protection action)
        last_ts = float(self._last_protect_ts.get(key, 0.0))
        cooldown_active = (ts_now - last_ts) < cooldown_sec

        # ========== EV 主导持仓保护 ==========
        # 经验结论：持仓后以 EV 为主，LW 只做辅助确认/降噪，MACD/CVD 作为回退。
        ev_confirm_min = 0.10
        ev_conflict_light_min = max(0.0, float(cfg.get("ev_conflict_light_min", 0.12)))
        ev_conflict_hard_min = max(ev_conflict_light_min, float(cfg.get("ev_conflict_hard_min", 0.30)))
        ev_conflict_hard_delta = max(0.0, float(cfg.get("ev_conflict_hard_delta", 0.08)))
        lw_assist_min = max(0.0, float(cfg.get("lw_assist_min", 0.18)))

        if ev_dir != 0:
            same_ev = ev_dir == pos_dir
            against_ev = ev_dir == -pos_dir

            if same_ev and ev_score_abs >= ev_confirm_min:
                self._conflict_counters[key] = 0
                result.update({
                    "level": "confirm",
                    "allow_add": True,
                    "tighten_trailing": False,
                    "reason": (
                        f"EV确认: pos={position_side} ev={ev_dir_raw}({float(ev_score or 0.0):+.2f}) "
                        f"lw={lw_dir_raw}({float(lw_score or 0.0):+.2f})"
                    ),
                })
                self.record_protection_level(symbol, position_side, "confirm", reason=result["reason"])
                return result

            if against_ev and ev_score_abs >= ev_conflict_light_min:
                if _trend_structure_intact():
                    prev = int(self._conflict_counters.get(key, 0) or 0)
                    self._conflict_counters[key] = max(0, prev - 1)
                else:
                    self._conflict_counters[key] = int(self._conflict_counters.get(key, 0) or 0) + 1
                conflict_bars = self._conflict_counters[key]
                conflict_score = ev_score_abs
                # LW 与 EV 同向（都反持仓）时，提升冲突等级；反之降级
                if lw_dir == ev_dir and lw_score_abs >= lw_assist_min:
                    conflict_score += 0.08
                elif lw_dir == pos_dir and lw_score_abs >= lw_assist_min:
                    conflict_score -= 0.08
                conflict_score = max(0.0, min(1.0, conflict_score))

                # 第 1 根冲突先观察，避免开仓后立即被噪声扰动出局
                if conflict_bars < light_confirm_bars:
                    result.update({
                        "level": "neutral",
                        "allow_add": True,
                        "tighten_trailing": False,
                        "risk_penalty": conflict_score,
                        "conflict_bars": conflict_bars,
                        "reason": (
                            f"EV冲突待确认: pos={position_side} ev={ev_dir_raw}({float(ev_score or 0.0):+.2f}) "
                            f"lw={lw_dir_raw}({float(lw_score or 0.0):+.2f}) bars={conflict_bars}/{light_confirm_bars}"
                        ),
                    })
                    self.record_protection_level(
                        symbol,
                        position_side,
                        "neutral",
                        reason=result.get("reason", ""),
                        extra={"source": "ev_pending", "conflict_bars": conflict_bars, "risk_penalty": conflict_score},
                    )
                    return result

                is_hard_conflict = (
                    conflict_bars >= hard_confirm_bars
                    and (
                        conflict_score >= ev_conflict_hard_min
                        or conflict_score >= (ev_conflict_light_min + ev_conflict_hard_delta)
                    )
                )
                result["cooldown_active"] = bool(cooldown_active)
                if is_hard_conflict:
                    result.update({
                        "level": "conflict_hard",
                        "allow_add": False,
                        "tighten_trailing": True,
                        "reduce_position_pct": 0.25,
                        "force_break_even": True,
                        "breakeven_mode": "profit_only",
                        "breakeven_fee_buffer": max(0.0, float(cfg.get("breakeven_fee_buffer", 0.0010))),
                        "risk_penalty": conflict_score,
                        "conflict_bars": conflict_bars,
                        "reason": (
                            f"EV重度冲突: pos={position_side} ev={ev_dir_raw}({float(ev_score or 0.0):+.2f}) "
                            f"lw={lw_dir_raw}({float(lw_score or 0.0):+.2f}) bars={conflict_bars} "
                            f"cd={'Y' if cooldown_active else 'N'}"
                        ),
                    })
                else:
                    result.update({
                        "level": "conflict_light",
                        "allow_add": False,
                        "tighten_trailing": (not is_trend) or trend_light_tighten,
                        "risk_penalty": conflict_score,
                        "conflict_bars": conflict_bars,
                        "reason": (
                            f"EV轻度冲突: pos={position_side} ev={ev_dir_raw}({float(ev_score or 0.0):+.2f}) "
                            f"lw={lw_dir_raw}({float(lw_score or 0.0):+.2f}) bars={conflict_bars} "
                            f"cd={'Y' if cooldown_active else 'N'}"
                        ),
                    })
                if not cooldown_active:
                    self._last_protect_ts[key] = ts_now
                self.record_protection_level(
                    symbol,
                    position_side,
                    result["level"],
                    reason=result.get("reason", ""),
                    extra={"source": "ev_primary", "conflict_bars": conflict_bars, "risk_penalty": conflict_score},
                )
                return result

        # ========== 确认增强 ==========
        # stronger same_macd threshold -> less noise-induced protection flips
        same_macd = (macd_dir == pos_dir) and (abs(macd_hist_norm) >= same_macd_min)
        same_cvd = (cvd_dir == pos_dir) and (abs(cvd_norm) > cvd_min)

        if same_macd and same_cvd:
            # 确认增强：允许加仓、持有更稳
            self._conflict_counters[key] = 0  # 重置冲突计数
            result.update({
                "level": "confirm",
                "allow_add": True,
                "tighten_trailing": False,
                "reason": f"确认增强: pos={position_side} macd={macd_dir:+.1f} cvd={cvd_dir:+.1f}",
            })
            self.record_protection_level(symbol, position_side, "confirm", reason=result["reason"])
            return result

        # ========== 冲突检测 ==========
        # MACD 与持仓同向，但 CVD 与持仓反向
        conflict = same_macd and (cvd_dir != pos_dir) and (abs(cvd_norm) > cvd_min)

        if conflict:
            # 更新冲突计数
            if _trend_structure_intact():
                prev = int(self._conflict_counters.get(key, 0) or 0)
                self._conflict_counters[key] = max(0, prev - 1)
            else:
                self._conflict_counters[key] = int(self._conflict_counters.get(key, 0) or 0) + 1
            conflict_bars = self._conflict_counters[key]

            # 风险惩罚系数
            risk_penalty = min(1.0, abs(cvd_norm))

            # 判断冲突级别
            is_hard_conflict = (
                abs(cvd_norm) > conflict_hard
                and strength < macd_weak
                and conflict_bars >= hard_confirm_bars
            )

            # If cooldown is active, we still report the conflict status
            # but advise caller to avoid repeated tighten/re-hang actions.
            result["cooldown_active"] = bool(cooldown_active)

            # 第 1 根冲突仅观察，不立刻进入保护动作
            if conflict_bars < light_confirm_bars:
                result.update({
                    "level": "neutral",
                    "allow_add": True,
                    "tighten_trailing": False,
                    "risk_penalty": risk_penalty,
                    "conflict_bars": conflict_bars,
                    "reason": (
                        f"冲突待确认: pos={position_side} macd={macd_hist_norm:+.2f} "
                        f"cvd={cvd_norm:+.2f} bars={conflict_bars}/{light_confirm_bars}"
                    ),
                })
                self.record_protection_level(
                    symbol,
                    position_side,
                    "neutral",
                    reason=result.get("reason", ""),
                    extra={"source": "macd_cvd_pending", "conflict_bars": conflict_bars, "risk_penalty": risk_penalty},
                )
                return result

            if is_hard_conflict:
                # ========== 重度冲突 ==========
                result.update({
                    "level": "conflict_hard",
                    "allow_add": False,
                    "tighten_trailing": True,
                    "reduce_position_pct": 0.25,  # 减仓 25%
                    "force_break_even": True,
                    "breakeven_mode": "profit_only",
                    "breakeven_fee_buffer": max(0.0, float(cfg.get("breakeven_fee_buffer", 0.0010))),
                    "risk_penalty": risk_penalty,
                    "conflict_bars": conflict_bars,
                    "reason": (
                        f"重度冲突: pos={position_side} macd={macd_hist_norm:+.2f} "
                        f"cvd={cvd_norm:+.2f} bars={conflict_bars} cd={'Y' if cooldown_active else 'N'}"
                    ),
                })
            else:
                # ========== 轻度冲突 ==========
                result.update({
                    "level": "conflict_light",
                    "allow_add": False,
                    "tighten_trailing": (not is_trend) or trend_light_tighten,
                    "risk_penalty": risk_penalty,
                    "conflict_bars": conflict_bars,
                    "reason": (
                        f"轻度冲突: pos={position_side} macd={macd_hist_norm:+.2f} "
                        f"cvd={cvd_norm:+.2f} bars={conflict_bars} cd={'Y' if cooldown_active else 'N'}"
                    ),
                })

            # record last protect timestamp only when we are in conflict state
            # (caller can still decide to skip action when cooldown_active=True)
            if not cooldown_active:
                self._last_protect_ts[key] = ts_now
            self.record_protection_level(
                symbol,
                position_side,
                result["level"],
                reason=result.get("reason", ""),
                extra={"conflict_bars": conflict_bars, "cooldown_active": cooldown_active, "risk_penalty": risk_penalty},
            )
            return result

        # ========== 无明确信号 ==========
        # neutral: decay instead of hard reset to reduce thrash
        prev = int(self._conflict_counters.get(key, 0))
        if prev > 0:
            self._conflict_counters[key] = max(0, prev - max(1, neutral_decay))
        else:
            self._conflict_counters[key] = 0
        result["reason"] = f"中性: pos={position_side} macd={macd_hist_norm:+.2f} cvd={cvd_norm:+.2f} bars={self._conflict_counters[key]}"
        self.record_protection_level(symbol, position_side, "neutral", reason=result["reason"])
        return result

    def get_conflict_counter(self, symbol: str, side: Optional[str] = None) -> int:
        """获取指定交易对的连续冲突次数"""
        if side:
            return self._conflict_counters.get((symbol, str(side).upper()), 0)
        # legacy interface: sum both sides if needed
        total = 0
        for (sym, _side), v in self._conflict_counters.items():
            if sym == symbol:
                total += int(v)
        return total

    def reset_conflict_counter(self, symbol: str, side: Optional[str] = None):
        """重置指定交易对的冲突计数"""
        if side:
            self._conflict_counters[(symbol, str(side).upper())] = 0
            self._last_protect_ts.pop((symbol, str(side).upper()), None)
            return
        # reset both sides
        for s in ("LONG", "SHORT"):
            self._conflict_counters[(symbol, s)] = 0
            self._last_protect_ts.pop((symbol, s), None)

    def reset_all_conflict_counters(self):
        """重置所有冲突计数"""
        self._conflict_counters.clear()
        self._last_protect_ts.clear()
