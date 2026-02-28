from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.fund_flow.models import FundFlowDecision


class FundFlowAttributionEngine:
    """
    决策-执行-结果归因日志，JSONL 持久化。
    
    资金流 3.0 增强:
    - 记录 DeepSeek 权重快照
    - 记录 15m/5m 分数融合信息
    - 支持因子贡献分析
    """

    def __init__(
        self,
        logs_dir: str,
        file_name: str = "fund_flow_attribution.jsonl",
        bucket_root_dir: str | None = None,
    ) -> None:
        self.logs_dir = logs_dir
        self.file_name = file_name
        self.bucket_root_dir = bucket_root_dir
        os.makedirs(self.logs_dir, exist_ok=True)
        self.log_path = os.path.join(self.logs_dir, self.file_name)
        if isinstance(self.bucket_root_dir, str) and self.bucket_root_dir.strip():
            os.makedirs(self.bucket_root_dir, exist_ok=True)
            self._migrate_legacy_bucket_layout()

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _bucket_parts(now_local: datetime) -> tuple[str, str, str]:
        month = now_local.strftime("%Y-%m")
        date = now_local.strftime("%Y-%m-%d")
        hour_bucket = f"{(now_local.hour // 6) * 6:02d}"
        return month, date, hour_bucket

    @staticmethod
    def _parse_iso_to_local(ts: Any) -> datetime | None:
        if not isinstance(ts, str):
            return None
        raw = ts.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
        try:
            return dt.astimezone()
        except Exception:
            return dt

    @staticmethod
    def _safe_mtime_dt(path: str) -> datetime:
        try:
            return datetime.fromtimestamp(os.path.getmtime(path))
        except Exception:
            return datetime.now()

    def _append(self, payload: Dict[str, Any]) -> None:
        payload = {"ts": self._ts(), **payload}
        with open(self._resolve_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _resolve_log_path(self) -> str:
        if not isinstance(self.bucket_root_dir, str) or not self.bucket_root_dir.strip():
            return self.log_path
        now = datetime.now()
        month, date, _hour_bucket = self._bucket_parts(now)
        dir_path = os.path.join(self.bucket_root_dir, month, date)
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, self.file_name)

    def _target_path_from_local_dt(self, dt_local: datetime) -> str:
        if not isinstance(self.bucket_root_dir, str) or not self.bucket_root_dir.strip():
            return self.log_path
        month, date, _hour_bucket = self._bucket_parts(dt_local)
        dir_path = os.path.join(self.bucket_root_dir, month, date)
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, self.file_name)

    def _migrate_legacy_bucket_layout(self) -> None:
        """
        兼容旧目录结构:
          logs/YYYY-MM/{00,06,12,18}/fund_flow_attribution.jsonl
        迁移到新目录结构:
          logs/YYYY-MM/YYYY-MM-DD/fund_flow_attribution.jsonl
        """
        if not isinstance(self.bucket_root_dir, str) or not self.bucket_root_dir.strip():
            return

        try:
            month_dirs = [
                d
                for d in os.listdir(self.bucket_root_dir)
                if os.path.isdir(os.path.join(self.bucket_root_dir, d))
            ]
        except Exception:
            return

        legacy_hours = {"00", "06", "12", "18"}
        for month in month_dirs:
            # 仅处理 YYYY-MM 层，避免误扫 YYYY-MM-DD 层
            if len(month) != 7 or month[4] != "-":
                continue
            month_path = os.path.join(self.bucket_root_dir, month)
            for hour in legacy_hours:
                legacy_dir = os.path.join(month_path, hour)
                legacy_file = os.path.join(legacy_dir, self.file_name)
                if not os.path.exists(legacy_file):
                    continue

                fallback_dt = self._safe_mtime_dt(legacy_file)
                handles: Dict[str, Any] = {}
                try:
                    with open(legacy_file, "r", encoding="utf-8", errors="ignore") as src:
                        for raw_line in src:
                            line = raw_line.rstrip("\n")
                            if not line.strip():
                                continue

                            target_dt = fallback_dt
                            try:
                                payload = json.loads(line)
                            except Exception:
                                payload = None
                            if isinstance(payload, dict):
                                ts_local = self._parse_iso_to_local(payload.get("ts"))
                                if ts_local is not None:
                                    target_dt = ts_local

                            target_path = self._target_path_from_local_dt(target_dt)
                            fp = handles.get(target_path)
                            if fp is None:
                                fp = open(target_path, "a", encoding="utf-8")
                                handles[target_path] = fp
                            fp.write(line + "\n")
                finally:
                    for fp in handles.values():
                        try:
                            fp.flush()
                            fp.close()
                        except Exception:
                            pass

                try:
                    os.remove(legacy_file)
                except Exception:
                    pass
                try:
                    if os.path.isdir(legacy_dir) and not os.listdir(legacy_dir):
                        os.rmdir(legacy_dir)
                except Exception:
                    pass

    def log_decision(self, decision: FundFlowDecision, context: Dict[str, Any]) -> None:
        self._append(
            {
                "event": "decision",
                "decision": decision.to_dict(),
                "context": context,
            }
        )

    def log_execution(self, decision: FundFlowDecision, result: Dict[str, Any]) -> None:
        self._append(
            {
                "event": "execution",
                "decision": decision.to_dict(),
                "result": result,
            }
        )
    
    def log_weight_snapshot(
        self,
        symbol: str,
        regime: str,
        weight_map: Dict[str, Any],
        score_info: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        记录 DeepSeek 权重快照
        
        用于复盘分析:
        - 为何这根15m更信OI/更信imbalance
        - 权重如何随市场状态变化
        """
        self._append(
            {
                "event": "weight_snapshot",
                "symbol": symbol,
                "regime": regime,
                "weight_map": weight_map,
                "score_info": score_info,
                "context": context or {},
            }
        )
    
    def log_score_fusion(
        self,
        symbol: str,
        regime: str,
        score_15m: Dict[str, float],
        score_5m: Dict[str, float],
        final_score: Dict[str, float],
        fusion_info: Dict[str, Any],
        direction_lock: str,
    ) -> None:
        """
        记录分数融合详情
        
        复盘时可分析:
        - 是15m资金没对齐？
        - 还是DeepSeek权重偏了？
        - 还是微结构trap没挡住？
        """
        self._append(
            {
                "event": "score_fusion",
                "symbol": symbol,
                "regime": regime,
                "score_15m": score_15m,
                "score_5m": score_5m,
                "final_score": final_score,
                "fusion_info": fusion_info,
                "direction_lock": direction_lock,
            }
        )
    
    def log_factor_contribution(
        self,
        symbol: str,
        regime: str,
        factor_values: Dict[str, float],
        factor_weights: Dict[str, float],
        contribution: Dict[str, float],
        decision_reason: str,
    ) -> None:
        """
        记录因子贡献分析
        
        用于:
        - 哪个因子对决策贡献最大
        - 因子权重是否合理
        - 发现因子漂移
        """
        self._append(
            {
                "event": "factor_contribution",
                "symbol": symbol,
                "regime": regime,
                "factor_values": factor_values,
                "factor_weights": factor_weights,
                "contribution": contribution,
                "decision_reason": decision_reason,
            }
        )
