"""
风险管理器
负责风险控制和检查
"""

import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple


def _decision_reverse_vote(decision_reason: Optional[str], position_side: str) -> Optional[str]:
    """
    根据 decision.reason 解析反转语义。
    返回:
      None / "soft" / "confirm"
    """
    if not decision_reason:
        return None

    text = str(decision_reason)
    side_u = str(position_side or "").upper()

    # 反转待确认
    if "反转待确认" in text:
        if "平多" in text and side_u in ("LONG", "BUY"):
            return "soft"
        if "平空" in text and side_u in ("SHORT", "SELL"):
            return "soft"

    # 反转确认完成
    if "反转平多" in text and side_u in ("LONG", "BUY"):
        return "confirm"
    if "反转平空" in text and side_u in ("SHORT", "SELL"):
        return "confirm"

    return None


@dataclass
class RiskEvent:
    ts: float
    symbol: str
    position_side: str
    regime: str
    level: str
    risk_state: str
    triggers: List[str]
    confirm_count: int
    energy: float
    energy_decline: int
    structure: str
    penetration: float
    ev_direction: str
    ev_score: float
    lw_direction: str
    lw_score: float
    decision_vote: str
    trap_score: float
    close_price: float
    reason: str
    params_tag: Dict[str, Any]


@dataclass
class ExecEvent:
    ts: float
    symbol: str
    position_side: str
    action: str
    reduce_pct: float
    realized_pnl: Optional[float]
    decision_vote: str
    meta: Dict[str, Any]


class ConflictProtectionStats:
    """
    冲突保护行为统计器：
    1) 每种触发类型触发次数
    2) REDUCE -> EXIT 平均间隔
    3) decision_confirm 后 EXIT 平均盈亏
    """

    def __init__(self, store_path: str, keep_last_n: int = 200000) -> None:
        self.store_path = str(store_path or "").strip()
        self.keep_last_n = max(1000, int(keep_last_n))
        self._events: Deque[Tuple[str, Any]] = deque(maxlen=self.keep_last_n)

        self.trigger_counts: Dict[str, int] = {}
        self.state_counts: Dict[str, int] = {}
        self.level_counts: Dict[str, int] = {}
        self.last_reduce_ts: Dict[str, float] = {}
        self.reduce_to_exit_deltas: List[float] = []
        self.confirm_exit_pnls: List[float] = []
        self.confirm_exit_cnt: int = 0

        self._load()

    @staticmethod
    def _inc(counter: Dict[str, int], key: str, delta: int = 1) -> None:
        k = str(key or "")
        counter[k] = int(counter.get(k, 0)) + int(delta)

    @staticmethod
    def _safe_float(x: Any, default: float = 0.0) -> float:
        try:
            return float(x)
        except Exception:
            return default

    @staticmethod
    def _decision_vote_from_text(reason: str) -> str:
        txt = str(reason or "")
        if "decision_vote=confirm" in txt:
            return "confirm"
        if "decision_vote=soft" in txt:
            return "soft"
        if "反转平多" in txt or "反转平空" in txt:
            return "confirm"
        if "反转待确认" in txt:
            return "soft"
        return "none"

    @staticmethod
    def _extract_triggers(risk_out: Dict[str, Any], decision_reason: str = "") -> Tuple[List[str], str]:
        triggers: List[str] = []
        risk_state = str(risk_out.get("risk_state", "")).upper()
        structure = str(risk_out.get("state_structure", "")).upper()
        level = str(risk_out.get("level", "")).lower()
        reason = str(risk_out.get("reason", ""))
        state_reason = str(risk_out.get("state_reason", ""))
        trap_score = float(risk_out.get("trap_score", 0.0) or 0.0)

        if risk_state == "CIRCUIT_EXIT" or structure == "TRAP" or trap_score >= 0.80 or "CIRCUIT_EXIT" in reason:
            triggers.append("TRAP")
        if structure in ("MID_BREAK", "MID_BREAK_DEEP", "BROKEN"):
            triggers.append("MID_BREAK")
        if ("energy_flip=1" in reason) or ("energy_flip=True" in reason):
            triggers.append("ENERGY_FLIP")
        if int(risk_out.get("state_energy_decline", 0) or 0) >= 3:
            triggers.append("ENERGY_WEAK")
        if ("ev_opp=1" in reason or "ev_opp=True" in reason) and ("lw_opp=1" in reason or "lw_opp=True" in reason):
            triggers.append("EV_LW_OPP")

        decision_vote = "none"
        merged_reason = f"{reason} | {state_reason}"
        if "decision_vote=confirm" in merged_reason:
            decision_vote = "confirm"
            triggers.append("DECISION_CONFIRM")
        elif "decision_vote=soft" in merged_reason:
            decision_vote = "soft"
            triggers.append("DECISION_SOFT")
        else:
            decision_vote = ConflictProtectionStats._decision_vote_from_text(decision_reason)
            if decision_vote == "confirm":
                triggers.append("DECISION_CONFIRM")
            elif decision_vote == "soft":
                triggers.append("DECISION_SOFT")

        if level == "conflict_hard":
            triggers.append("CONFLICT_HARD")
        elif level == "conflict_light":
            triggers.append("CONFLICT_LIGHT")

        return sorted(set(triggers)), decision_vote

    @staticmethod
    def _key(symbol: str, position_side: str) -> str:
        return f"{str(symbol).upper()}::{str(position_side).upper()}"

    def _append_jsonl(self, obj: Dict[str, Any]) -> None:
        if not self.store_path:
            return
        try:
            os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
            with open(self.store_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _load(self) -> None:
        if not self.store_path or (not os.path.exists(self.store_path)):
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    typ = obj.get("type")
                    if typ == "risk":
                        self._ingest_risk_obj(obj, persist=False)
                    elif typ == "exec":
                        self._ingest_exec_obj(obj, persist=False)
        except Exception:
            return

    def on_risk_event(
        self,
        symbol: str,
        position_side: str,
        risk_out: Dict[str, Any],
        decision_meta: Optional[Dict[str, Any]] = None,
        decision_reason: str = "",
        ts: Optional[float] = None,
        params_tag: Optional[Dict[str, Any]] = None,
    ) -> None:
        decision_meta = decision_meta if isinstance(decision_meta, dict) else {}
        risk_out = risk_out if isinstance(risk_out, dict) else {}
        event_ts = float(ts) if ts is not None else time.time()
        triggers, decision_vote = self._extract_triggers(risk_out, decision_reason=decision_reason)
        e = RiskEvent(
            ts=event_ts,
            symbol=str(symbol),
            position_side=str(position_side),
            regime=str(decision_meta.get("regime", risk_out.get("market_regime", "")) or ""),
            level=str(risk_out.get("level", "")),
            risk_state=str(risk_out.get("risk_state", "")),
            triggers=triggers,
            confirm_count=int(risk_out.get("state_confirm_count", 0) or 0),
            energy=self._safe_float(risk_out.get("state_energy", risk_out.get("energy", 0.0)), 0.0),
            energy_decline=int(risk_out.get("state_energy_decline", 0) or 0),
            structure=str(risk_out.get("state_structure", "")),
            penetration=self._safe_float(risk_out.get("penetration", 0.0), 0.0),
            ev_direction=str(decision_meta.get("ev_direction", risk_out.get("ev_direction", "")) or ""),
            ev_score=self._safe_float(decision_meta.get("ev_score", risk_out.get("ev_score", 0.0)), 0.0),
            lw_direction=str(decision_meta.get("lw_direction", risk_out.get("lw_direction", "")) or ""),
            lw_score=self._safe_float(decision_meta.get("lw_score", risk_out.get("lw_score", 0.0)), 0.0),
            decision_vote=decision_vote,
            trap_score=self._safe_float(risk_out.get("trap_score", 0.0), 0.0),
            close_price=self._safe_float(risk_out.get("close_price", risk_out.get("last_close", 0.0)), 0.0),
            reason=str(risk_out.get("reason", "")),
            params_tag=params_tag if isinstance(params_tag, dict) else {},
        )
        self._ingest_risk(e, persist=True)

    def on_execution(
        self,
        symbol: str,
        position_side: str,
        action: str,
        reduce_pct: float = 0.0,
        realized_pnl: Optional[float] = None,
        decision_vote: str = "none",
        ts: Optional[float] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        e = ExecEvent(
            ts=float(ts) if ts is not None else time.time(),
            symbol=str(symbol),
            position_side=str(position_side),
            action=str(action).upper(),
            reduce_pct=float(reduce_pct),
            realized_pnl=None if realized_pnl is None else float(realized_pnl),
            decision_vote=str(decision_vote or "none"),
            meta=meta if isinstance(meta, dict) else {},
        )
        self._ingest_exec(e, persist=True)

    def _ingest_risk(self, e: RiskEvent, persist: bool) -> None:
        self._events.append(("risk", e))
        self._inc(self.state_counts, e.risk_state, 1)
        self._inc(self.level_counts, e.level, 1)
        for t in e.triggers:
            self._inc(self.trigger_counts, t, 1)

        key = self._key(e.symbol, e.position_side)
        rs = str(e.risk_state or "").upper()
        if rs == "REDUCE":
            self.last_reduce_ts[key] = e.ts
        elif rs in ("EXIT", "CIRCUIT_EXIT"):
            if key in self.last_reduce_ts:
                dt = e.ts - float(self.last_reduce_ts.get(key, e.ts))
                if dt >= 0:
                    self.reduce_to_exit_deltas.append(dt)
                self.last_reduce_ts.pop(key, None)

        if persist:
            self._append_jsonl({"type": "risk", **asdict(e)})

    def _ingest_exec(self, e: ExecEvent, persist: bool) -> None:
        self._events.append(("exec", e))
        if e.action == "EXIT" and str(e.decision_vote).lower() == "confirm":
            self.confirm_exit_cnt += 1
            if e.realized_pnl is not None:
                self.confirm_exit_pnls.append(float(e.realized_pnl))
        if persist:
            self._append_jsonl({"type": "exec", **asdict(e)})

    def _ingest_risk_obj(self, obj: Dict[str, Any], persist: bool) -> None:
        e = RiskEvent(
            ts=float(obj.get("ts", 0.0)),
            symbol=str(obj.get("symbol", "")),
            position_side=str(obj.get("position_side", "")),
            regime=str(obj.get("regime", "")),
            level=str(obj.get("level", "")),
            risk_state=str(obj.get("risk_state", "")),
            triggers=list(obj.get("triggers", []) or []),
            confirm_count=int(obj.get("confirm_count", 0)),
            energy=float(obj.get("energy", 0.0)),
            energy_decline=int(obj.get("energy_decline", 0)),
            structure=str(obj.get("structure", "")),
            penetration=float(obj.get("penetration", 0.0)),
            ev_direction=str(obj.get("ev_direction", "")),
            ev_score=float(obj.get("ev_score", 0.0)),
            lw_direction=str(obj.get("lw_direction", "")),
            lw_score=float(obj.get("lw_score", 0.0)),
            decision_vote=str(obj.get("decision_vote", "none")),
            trap_score=float(obj.get("trap_score", 0.0)),
            close_price=float(obj.get("close_price", 0.0)),
            reason=str(obj.get("reason", "")),
            params_tag=dict(obj.get("params_tag", {}) or {}),
        )
        self._ingest_risk(e, persist=persist)

    def _ingest_exec_obj(self, obj: Dict[str, Any], persist: bool) -> None:
        realized_pnl_raw = obj.get("realized_pnl", None)
        realized_pnl_val = None if realized_pnl_raw is None else self._safe_float(realized_pnl_raw, 0.0)
        e = ExecEvent(
            ts=float(obj.get("ts", 0.0)),
            symbol=str(obj.get("symbol", "")),
            position_side=str(obj.get("position_side", "")),
            action=str(obj.get("action", "")).upper(),
            reduce_pct=self._safe_float(obj.get("reduce_pct", 0.0), 0.0),
            realized_pnl=realized_pnl_val,
            decision_vote=str(obj.get("decision_vote", "none")),
            meta=dict(obj.get("meta", {}) or {}),
        )
        self._ingest_exec(e, persist=persist)

    @staticmethod
    def _avg(xs: List[float]) -> Optional[float]:
        if not xs:
            return None
        return sum(xs) / max(1, len(xs))

    def summary(self) -> Dict[str, Any]:
        avg_r2e = self._avg(self.reduce_to_exit_deltas)
        avg_confirm_pnl = self._avg(self.confirm_exit_pnls)
        return {
            "counts": {
                "risk_state": dict(self.state_counts),
                "level": dict(self.level_counts),
                "triggers": dict(self.trigger_counts),
            },
            "reduce_to_exit": {
                "n": len(self.reduce_to_exit_deltas),
                "avg_seconds": avg_r2e,
                "avg_minutes": (None if avg_r2e is None else avg_r2e / 60.0),
            },
            "decision_confirm_exit_pnl": {
                "n_exit_confirm": int(self.confirm_exit_cnt),
                "n_with_realized_pnl": len(self.confirm_exit_pnls),
                "avg_pnl": avg_confirm_pnl,
            },
        }

    def pretty_print(self) -> str:
        s = self.summary()
        lines = [
            "=== Conflict Protection Stats ===",
            f"[risk_state] {s['counts']['risk_state']}",
            f"[level]      {s['counts']['level']}",
            f"[reduce->exit] n={s['reduce_to_exit']['n']} avg_min={s['reduce_to_exit']['avg_minutes']}",
            (
                "[decision_confirm exit pnl] "
                f"n_confirm_exit={s['decision_confirm_exit_pnl']['n_exit_confirm']} "
                f"n_pnl={s['decision_confirm_exit_pnl']['n_with_realized_pnl']} "
                f"avg_pnl={s['decision_confirm_exit_pnl']['avg_pnl']}"
            ),
        ]
        trg = s["counts"]["triggers"]
        if isinstance(trg, dict) and trg:
            top = sorted(trg.items(), key=lambda x: x[1], reverse=True)[:12]
            lines.append("[triggers top]")
            for k, v in top:
                lines.append(f"  - {k}: {v}")
        return "\n".join(lines)


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
            # === 每分钟持仓风控状态机（方向+布林带） ===
            "state_confirm_bars": int(conflict_cfg.get("state_confirm_bars", 2)),
            "state_energy_decline_bars": int(conflict_cfg.get("state_energy_decline_bars", 3)),
            "state_structure_break_min": float(conflict_cfg.get("state_structure_break_min", 0.15)),
            "state_structure_break_deep": float(conflict_cfg.get("state_structure_break_deep", 0.35)),
            "state_reduce_pct": float(conflict_cfg.get("state_reduce_pct", 0.20)),
            "state_circuit_cvd_norm": float(conflict_cfg.get("state_circuit_cvd_norm", 0.85)),
            "state_circuit_trap_bars": int(conflict_cfg.get("state_circuit_trap_bars", 2)),
            "state_circuit_trap_hard": float(conflict_cfg.get("state_circuit_trap_hard", 0.90)),
            "state_circuit_trap_hard_bars": int(conflict_cfg.get("state_circuit_trap_hard_bars", 2)),
            "state_circuit_cvd_guard_min": int(conflict_cfg.get("state_circuit_cvd_guard_min", 2)),
            "state_circuit_dirscore_min": float(conflict_cfg.get("state_circuit_dirscore_min", 0.20)),
            "state_ev_min": float(conflict_cfg.get("state_ev_min", 0.55)),
            "state_lw_min": float(conflict_cfg.get("state_lw_min", 0.55)),
            "state_macd_flip": float(conflict_cfg.get("state_macd_flip", 0.05)),
            "state_cvd_flip": float(conflict_cfg.get("state_cvd_flip", 0.05)),
        }
        # 状态机缓存: 连续反向确认 / 连续陷阱 / 能量序列
        self._state_reverse_counters: Dict[Tuple[str, str], int] = {}
        self._state_trap_counters: Dict[Tuple[str, str], int] = {}
        self._state_energy_hist: Dict[Tuple[str, str], Deque[float]] = {}

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
        # 冲突保护行为统计（JSONL 持久化）
        stats_path = str(conflict_cfg.get("stats_store_path", "./logs/risk_conflict_stats.jsonl"))
        stats_keep_n = int(conflict_cfg.get("stats_keep_last_n", 200000))
        self.conflict_stats = ConflictProtectionStats(store_path=stats_path, keep_last_n=stats_keep_n)

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

    def _eval_minute_position_state(
        self,
        symbol: str,
        position_side: str,
        macd_hist_norm: float,
        cvd_norm: float,
        ev_score: float,
        lw_score: float,
        ev_direction: Optional[str] = None,
        lw_direction: Optional[str] = None,
        direction_lock: Optional[str] = None,
        decision_reason: Optional[str] = None,
        mtf_scores: Optional[Dict[str, float]] = None,
        energy: Optional[float] = None,
        bb_upper: Optional[float] = None,
        bb_lower: Optional[float] = None,
        bb_middle: Optional[float] = None,
        close_price: Optional[float] = None,
        trap_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        已持仓每分钟风控状态机：
        HOLD / TIGHTEN / REDUCE / EXIT / CIRCUIT_EXIT
        """
        key = (symbol, str(position_side).upper())
        ps = str(position_side).upper()
        cfg = self._conflict_cfg
        confirm_bars = max(2, int(cfg.get("state_confirm_bars", 2)))
        energy_decline_bars = max(2, int(cfg.get("state_energy_decline_bars", 3)))
        structure_break_min = max(0.05, float(cfg.get("state_structure_break_min", 0.15)))
        structure_break_deep = max(structure_break_min, float(cfg.get("state_structure_break_deep", 0.35)))
        reduce_pct = min(1.0, max(0.05, float(cfg.get("state_reduce_pct", 0.20))))
        circuit_cvd_norm = max(0.5, float(cfg.get("state_circuit_cvd_norm", 0.85)))
        circuit_trap_bars = max(1, int(cfg.get("state_circuit_trap_bars", 2)))
        circuit_trap_hard = max(0.7, float(cfg.get("state_circuit_trap_hard", 0.90)))
        # trap_hard 仅在连续出现后触发，避免单根噪声直接熔断
        circuit_trap_hard_bars = max(
            1,
            int(cfg.get("state_circuit_trap_hard_bars", max(1, min(circuit_trap_bars, 2)))),
        )
        circuit_cvd_guard_min = max(1, int(cfg.get("state_circuit_cvd_guard_min", 2)))
        circuit_dirscore_min = max(0.05, float(cfg.get("state_circuit_dirscore_min", 0.20)))
        ev_min = max(0.0, float(cfg.get("state_ev_min", 0.55)))
        lw_min = max(0.0, float(cfg.get("state_lw_min", 0.55)))
        macd_flip = max(0.0, float(cfg.get("state_macd_flip", 0.05)))
        cvd_flip = max(0.0, float(cfg.get("state_cvd_flip", 0.05)))

        def _dir_num(d: Optional[str]) -> int:
            d_u = str(d or "").upper()
            if d_u in ("LONG", "LONG_ONLY", "BUY", "BULL"):
                return 1
            if d_u in ("SHORT", "SHORT_ONLY", "SELL", "BEAR"):
                return -1
            return 0

        # 1) 多周期方向融合（1m/3m/5m）
        tf_scores = mtf_scores if isinstance(mtf_scores, dict) else {}
        s1 = float(tf_scores.get("1m", 0.0))
        s3 = float(tf_scores.get("3m", 0.0))
        s5 = float(tf_scores.get("5m", 0.0))
        has_mtf = any(abs(v) > 1e-9 for v in (s1, s3, s5))
        dir_score = 0.25 * s1 + 0.30 * s3 + 0.45 * s5 if has_mtf else (0.65 * float(ev_score) + 0.35 * float(lw_score))
        if abs(dir_score) < 0.06:
            dir_label = "NEUTRAL"
        else:
            dir_label = "LONG" if dir_score > 0 else "SHORT"

        # 2) 能量（0~1）：可外部传入，否则内部拼接
        if energy is None:
            e = 0.45 * abs(float(macd_hist_norm)) + 0.35 * abs(float(cvd_norm)) + 0.20 * abs(float(dir_score))
        else:
            e = float(energy)
        e = max(0.0, min(1.0, e))
        hist = self._state_energy_hist.setdefault(key, deque(maxlen=8))
        hist.append(e)
        energy_decline = False
        if len(hist) >= energy_decline_bars:
            tail = list(hist)[-energy_decline_bars:]
            energy_decline = all(tail[i] <= tail[i - 1] for i in range(1, len(tail))) and (tail[0] - tail[-1] >= 0.05)

        # 3) 结构位（布林带）
        structure = "UNKNOWN"
        structure_break = False
        deep_break = False
        penetration = 0.0
        c = float(close_price or 0.0)
        m = float(bb_middle or 0.0)
        u = float(bb_upper or 0.0)
        l = float(bb_lower or 0.0)
        if c > 0 and m > 0 and u > l:
            half = max((u - m), (m - l), 1e-12)
            # penetration>0 表示对持仓不利方向穿越中轨
            if ps == "LONG":
                penetration = (m - c) / half
            else:
                penetration = (c - m) / half
            offset = (c - m) / half
            if ps == "LONG":
                if c >= m:
                    structure = "HEALTHY"
                else:
                    structure = "BROKEN" if penetration >= structure_break_min else "EDGE"
                structure_break = (c < m) and (penetration >= structure_break_min)
            else:
                if c <= m:
                    structure = "HEALTHY"
                else:
                    structure = "BROKEN" if penetration >= structure_break_min else "EDGE"
                structure_break = (c > m) and (penetration >= structure_break_min)
            deep_break = bool(penetration >= structure_break_deep)

        # 4) 熔断：陷阱连发 or CVD 极端反向
        trap_val = float(trap_score or 0.0)
        trap_hit = trap_val >= 0.70
        trap_count = int(self._state_trap_counters.get(key, 0))
        trap_count = trap_count + 1 if trap_hit else 0
        self._state_trap_counters[key] = trap_count

        pos_dir = 1 if ps == "LONG" else -1
        cvd_opp_extreme = (float(cvd_norm) * pos_dir) <= (-1.0 * circuit_cvd_norm)
        opposite_now = (dir_label == "SHORT" and ps == "LONG") or (dir_label == "LONG" and ps == "SHORT")
        cvd_guard_votes = 0
        if structure_break:
            cvd_guard_votes += 1
        if deep_break:
            cvd_guard_votes += 1
        if energy_decline:
            cvd_guard_votes += 1
        if opposite_now and abs(float(dir_score)) >= circuit_dirscore_min:
            cvd_guard_votes += 1
        if trap_hit:
            cvd_guard_votes += 1
        circuit_by_trap = (trap_count >= circuit_trap_bars) or (
            trap_val >= circuit_trap_hard and trap_count >= circuit_trap_hard_bars
        )
        circuit_by_cvd = bool(cvd_opp_extreme and (cvd_guard_votes >= circuit_cvd_guard_min))
        if circuit_by_trap or circuit_by_cvd:
            self._state_reverse_counters[key] = 0
            return {
                "risk_state": "CIRCUIT_EXIT",
                "dir": dir_label,
                "dir_score": round(dir_score, 3),
                "energy": round(e, 3),
                "energy_decline": energy_decline,
                "structure": structure,
                "structure_break": structure_break,
                "penetration": round(penetration, 3),
                "hard_votes": 3,
                "confirm_count": trap_count,
                "reason": (
                    f"熔断 trap={trap_val:.2f} cnt={trap_count}/{circuit_trap_bars} "
                    f"cvd={cvd_norm:+.2f} opp_extreme={int(cvd_opp_extreme)} "
                    f"guard={cvd_guard_votes}/{circuit_cvd_guard_min} trap_hard={circuit_trap_hard:.2f}"
                ),
            }

        # 5) 反向确认计数（硬退出）
        opposite = (dir_label == "SHORT" and ps == "LONG") or (dir_label == "LONG" and ps == "SHORT")
        ev_dir_num = _dir_num(ev_direction)
        lw_dir_num = _dir_num(lw_direction)
        ev_opp = (ev_dir_num == -pos_dir) and (abs(float(ev_score)) >= ev_min)
        lw_opp = (lw_dir_num == -pos_dir) and (abs(float(lw_score)) >= lw_min)
        ev_same = (ev_dir_num == pos_dir) and (abs(float(ev_score)) >= ev_min)
        lw_same = (lw_dir_num == pos_dir) and (abs(float(lw_score)) >= lw_min)
        if ps == "LONG":
            energy_flip = (float(macd_hist_norm) <= -macd_flip) and (float(cvd_norm) <= -cvd_flip)
        else:
            energy_flip = (float(macd_hist_norm) >= macd_flip) and (float(cvd_norm) >= cvd_flip)
        energy_weak = bool(energy_decline)

        hard_votes = 0
        if structure_break:
            hard_votes += 1
        if ev_opp and lw_opp:
            hard_votes += 1
        if energy_flip or (energy_weak and ev_opp):
            hard_votes += 1
        conflict_hard = hard_votes >= 2
        conflict_light = ((ev_opp and lw_same and not structure_break) or (energy_weak and not conflict_hard))

        rev_count = int(self._state_reverse_counters.get(key, 0))
        reverse_vote = _decision_reverse_vote(decision_reason, ps)
        if reverse_vote == "soft":
            hard_votes += 1
            conflict_hard = hard_votes >= 2
            conflict_light = ((ev_opp and lw_same and not structure_break) or (energy_weak and not conflict_hard))
        elif reverse_vote == "confirm":
            hard_votes += 2
            conflict_hard = True
            rev_count = max(rev_count, max(1, confirm_bars - 1))

        if opposite and conflict_hard:
            rev_count += 1
        elif opposite and (structure in ("EDGE", "UNKNOWN") or conflict_light):
            rev_count = max(0, rev_count - 1)
        else:
            rev_count = 0
        self._state_reverse_counters[key] = rev_count

        # 锁方向：锁住时，反向最多 TIGHTEN，不触发 REDUCE/EXIT
        lock_u = str(direction_lock or "").upper()
        locked_opposite = (lock_u == "LONG_ONLY" and ps == "LONG") or (lock_u == "SHORT_ONLY" and ps == "SHORT")
        if locked_opposite and (conflict_hard or conflict_light):
            return {
                "risk_state": "TIGHTEN",
                "dir": dir_label,
                "dir_score": round(dir_score, 3),
                "energy": round(e, 3),
                "energy_decline": energy_decline,
                "structure": structure,
                "structure_break": structure_break,
                "penetration": round(penetration, 3),
                "hard_votes": hard_votes,
                "confirm_count": rev_count,
                "reason": (
                    f"direction_lock={lock_u} 限制反向退出 "
                    f"pen={penetration:.2f} votes={hard_votes} confirm={rev_count}"
                    + (f" decision_vote={reverse_vote}" if reverse_vote else "")
                ),
            }

        if deep_break and (energy_flip or (ev_opp and lw_opp)):
            return {
                "risk_state": "EXIT",
                "dir": dir_label,
                "dir_score": round(dir_score, 3),
                "energy": round(e, 3),
                "energy_decline": energy_decline,
                "structure": structure,
                "structure_break": structure_break,
                "penetration": round(penetration, 3),
                "hard_votes": hard_votes,
                "confirm_count": rev_count,
                "reason": (
                    f"deep_break EXIT pen={penetration:.2f} "
                    f"ev={ev_direction}/{float(ev_score):.2f} lw={lw_direction}/{float(lw_score):.2f} "
                    f"macd={float(macd_hist_norm):+.2f} cvd={float(cvd_norm):+.2f} energy={e:.2f}"
                    + (f" decision_vote={reverse_vote}" if reverse_vote else "")
                ),
            }

        if conflict_hard and rev_count >= confirm_bars:
            return {
                "risk_state": "EXIT",
                "dir": dir_label,
                "dir_score": round(dir_score, 3),
                "energy": round(e, 3),
                "energy_decline": energy_decline,
                "structure": structure,
                "structure_break": structure_break,
                "penetration": round(penetration, 3),
                "hard_votes": hard_votes,
                "confirm_count": rev_count,
                "reason": (
                    f"hard_conflict EXIT votes={hard_votes} confirm={rev_count}/{confirm_bars} "
                    f"pen={penetration:.2f} ev_opp={int(ev_opp)} lw_opp={int(lw_opp)} energy_flip={int(energy_flip)}"
                    + (f" decision_vote={reverse_vote}" if reverse_vote else "")
                ),
            }

        if conflict_hard and rev_count >= max(1, confirm_bars - 1):
            return {
                "risk_state": "REDUCE",
                "dir": dir_label,
                "dir_score": round(dir_score, 3),
                "energy": round(e, 3),
                "energy_decline": energy_decline,
                "structure": structure,
                "structure_break": structure_break,
                "penetration": round(penetration, 3),
                "hard_votes": hard_votes,
                "confirm_count": rev_count,
                "reduce_pct": reduce_pct,
                "reason": (
                    f"hard_conflict REDUCE votes={hard_votes} confirm={rev_count}/{confirm_bars} "
                    f"pen={penetration:.2f}"
                    + (f" decision_vote={reverse_vote}" if reverse_vote else "")
                ),
            }

        if conflict_light or energy_weak or (structure == "EDGE"):
            return {
                "risk_state": "TIGHTEN",
                "dir": dir_label,
                "dir_score": round(dir_score, 3),
                "energy": round(e, 3),
                "energy_decline": energy_decline,
                "structure": structure,
                "structure_break": structure_break,
                "penetration": round(penetration, 3),
                "hard_votes": hard_votes,
                "confirm_count": rev_count,
                "reason": (
                    f"light_conflict/TIGHTEN pen={penetration:.2f} votes={hard_votes} "
                    f"ev_opp={int(ev_opp)} lw_same={int(lw_same)} energy_decline={int(energy_weak)}"
                    + (f" decision_vote={reverse_vote}" if reverse_vote else "")
                ),
            }

        return {
            "risk_state": "HOLD",
            "dir": dir_label,
            "dir_score": round(dir_score, 3),
            "energy": round(e, 3),
            "energy_decline": energy_decline,
            "structure": structure,
            "structure_break": structure_break,
            "penetration": round(penetration, 3),
            "hard_votes": hard_votes,
            "confirm_count": rev_count,
            "reason": (
                f"HOLD pen={penetration:.2f} ev_same={int(ev_same)} lw_same={int(lw_same)} "
                f"energy={e:.2f} trap={trap_val:.2f}"
                + (f" decision_vote={reverse_vote}" if reverse_vote else "")
            ),
        }

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
        mtf_scores: Optional[Dict[str, float]] = None,
        energy: Optional[float] = None,
        bb_upper: Optional[float] = None,
        bb_lower: Optional[float] = None,
        bb_middle: Optional[float] = None,
        close_price: Optional[float] = None,
        trap_score: Optional[float] = None,
        direction_lock: Optional[str] = None,
        decision_reason: Optional[str] = None,
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
            decision_reason: 决策引擎 reason（用于反转语义投票）

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
                "risk_state": str,              # HOLD/TIGHTEN/REDUCE/EXIT/CIRCUIT_EXIT
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
            "risk_state": "HOLD",
            "reason": "",
        }
        result["trap_score"] = float(trap_score or 0.0)
        result["close_price"] = float(close_price or last_close or 0.0)

        def _finalize(out: Dict[str, Any]) -> Dict[str, Any]:
            """统一出口：记录冲突保护统计，不影响主流程。"""
            try:
                decision_meta = {
                    "regime": regime,
                    "ev_direction": ev_direction,
                    "ev_score": float(ev_score or 0.0),
                    "lw_direction": lw_direction,
                    "lw_score": float(lw_score or 0.0),
                }
                params_tag = {
                    "confirm_reduce": max(1, int(cfg.get("state_confirm_bars", 2))) - 1,
                    "confirm_exit": max(1, int(cfg.get("state_confirm_bars", 2))),
                    "mid_break_p1": float(cfg.get("state_structure_break_min", 0.15)),
                    "mid_break_p2": float(cfg.get("state_structure_break_deep", 0.35)),
                    "trap_confirm": int(cfg.get("state_circuit_trap_bars", 2)),
                    "trap_hard": float(cfg.get("state_circuit_trap_hard", 0.90)),
                    "trap_hard_confirm": int(cfg.get("state_circuit_trap_hard_bars", 2)),
                }
                self.conflict_stats.on_risk_event(
                    symbol=symbol,
                    position_side=position_side,
                    risk_out=out,
                    decision_meta=decision_meta,
                    decision_reason=str(decision_reason or ""),
                    ts=ts_now,
                    params_tag=params_tag,
                )
            except Exception:
                pass
            return out

        # ========== 每分钟状态机（方向+布林带） ==========
        state_info = self._eval_minute_position_state(
            symbol=symbol,
            position_side=position_side,
            macd_hist_norm=macd_hist_norm,
            cvd_norm=cvd_norm,
            ev_score=float(ev_score or 0.0),
            lw_score=float(lw_score or 0.0),
            ev_direction=str(ev_direction or "BOTH"),
            lw_direction=str(lw_direction or "BOTH"),
            direction_lock=direction_lock,
            decision_reason=decision_reason,
            mtf_scores=mtf_scores,
            energy=energy,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            bb_middle=bb_middle,
            close_price=close_price,
            trap_score=trap_score,
        )
        risk_state = str(state_info.get("risk_state", "HOLD"))
        result["risk_state"] = risk_state
        result["state_confirm_count"] = int(state_info.get("confirm_count", 0))
        result["state_energy"] = float(state_info.get("energy", 0.0))
        result["state_energy_decline"] = bool(state_info.get("energy_decline", False))
        result["state_structure"] = str(state_info.get("structure", "UNKNOWN"))
        result["state_reason"] = str(state_info.get("reason", ""))

        # 状态机优先级：熔断/退出 > 减仓 > 收紧
        if risk_state in ("CIRCUIT_EXIT", "EXIT"):
            self._conflict_counters[key] = max(self._conflict_counters.get(key, 0), max(2, hard_confirm_bars))
            result.update({
                "level": "conflict_hard",
                "allow_add": False,
                "tighten_trailing": True,
                "reduce_position_pct": 1.0,
                "force_break_even": False,
                "risk_penalty": 1.0,
                "conflict_bars": int(result.get("state_confirm_count", 0)),
                "reason": f"{risk_state}: {result.get('state_reason', '')}",
            })
            self.record_protection_level(symbol, position_side, "conflict_hard", reason=result["reason"])
            return _finalize(result)

        if risk_state == "REDUCE":
            reduce_pct = float(state_info.get("reduce_pct", cfg.get("state_reduce_pct", 0.20)))
            result.update({
                "level": "conflict_hard",
                "allow_add": False,
                "tighten_trailing": True,
                "reduce_position_pct": min(1.0, max(0.05, reduce_pct)),
                "force_break_even": True,
                "breakeven_mode": "profit_only",
                "breakeven_fee_buffer": max(0.0, float(cfg.get("breakeven_fee_buffer", 0.0010))),
                "risk_penalty": min(1.0, max(0.0, abs(float(cvd_norm)))),
                "conflict_bars": int(result.get("state_confirm_count", 0)),
                "reason": f"REDUCE: {result.get('state_reason', '')}",
            })
            self.record_protection_level(symbol, position_side, "conflict_hard", reason=result["reason"])
            return _finalize(result)

        if risk_state == "TIGHTEN":
            result.update({
                "level": "conflict_light",
                "allow_add": False,
                "tighten_trailing": True,
                "risk_penalty": min(0.8, max(0.1, abs(float(cvd_norm)))),
                "conflict_bars": int(result.get("state_confirm_count", 0)),
                "reason": f"TIGHTEN: {result.get('state_reason', '')}",
            })
            self.record_protection_level(symbol, position_side, "conflict_light", reason=result["reason"])
            return _finalize(result)

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
                return _finalize(result)

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
                    return _finalize(result)

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
                return _finalize(result)

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
            return _finalize(result)

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
                return _finalize(result)

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
            return _finalize(result)

        # ========== 无明确信号 ==========
        # neutral: decay instead of hard reset to reduce thrash
        prev = int(self._conflict_counters.get(key, 0))
        if prev > 0:
            self._conflict_counters[key] = max(0, prev - max(1, neutral_decay))
        else:
            self._conflict_counters[key] = 0
        result["reason"] = f"中性: pos={position_side} macd={macd_hist_norm:+.2f} cvd={cvd_norm:+.2f} bars={self._conflict_counters[key]}"
        self.record_protection_level(symbol, position_side, "neutral", reason=result["reason"])
        return _finalize(result)

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

    def record_conflict_execution(
        self,
        symbol: str,
        position_side: str,
        action: str,
        reduce_pct: float = 0.0,
        realized_pnl: Optional[float] = None,
        decision_reason: Optional[str] = None,
        ts: Optional[float] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        记录执行层事件（REDUCE/EXIT），用于:
        - REDUCE -> EXIT 间隔统计
        - decision_confirm 后 EXIT 平均盈亏
        """
        vote = ConflictProtectionStats._decision_vote_from_text(str(decision_reason or ""))
        self.conflict_stats.on_execution(
            symbol=symbol,
            position_side=position_side,
            action=action,
            reduce_pct=float(reduce_pct or 0.0),
            realized_pnl=realized_pnl,
            decision_vote=vote,
            ts=ts,
            meta=meta if isinstance(meta, dict) else {},
        )

    def get_conflict_stats_summary(self) -> Dict[str, Any]:
        """获取冲突保护统计摘要。"""
        return self.conflict_stats.summary()

    def format_conflict_stats_summary(self) -> str:
        """格式化冲突保护统计摘要。"""
        return self.conflict_stats.pretty_print()
