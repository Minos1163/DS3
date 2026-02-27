"""
DeepSeek AI Weight Service - AI动态权重调度服务

核心功能:
1. 调用 DeepSeek API 生成因子权重
2. 严格的 JSON 输出校验
3. 失败降级策略
4. 智能缓存

约束:
- 不输出交易方向
- 不输出阈值/仓位/杠杆
- 只输出权重和置信度
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# 系统提示词 - 固定不变
SYSTEM_PROMPT = """你是"加密货币资金流策略"的权重调度器（Weight Router）。
你的任务：根据输入的市场状态(regime)、资金流特征(z-score/归一化)、微结构风险(trap/phantom/spread等)，输出用于打分的"因子权重"。
注意：你【绝对不能】输出交易方向（LONG/SHORT）、交易动作（BUY/SELL/CLOSE）、阈值、仓位、杠杆、下单价格等任何执行建议。
你只能输出权重、置信度、以及可解释的简短理由。

必须遵守：
1) 输出必须是严格 JSON（不允许 markdown、不允许多余文本、不允许注释）。
2) 权重必须为 0~1 之间的小数，且 weights 总和必须=1（允许 1e-6 误差）。
3) 必须包含：weights、confidence、regime_view、risk_flags、reasoning_bullets、version、fallback_used。
4) 如果输入缺失或异常，必须触发 fallback：输出默认权重（在输入中给出），并将 fallback_used=true。
5) 微结构风险（trap_score/phantom_score/spread_z）高时，降低"动量类"权重，提高"防陷阱/流动性/深度类"权重，并降低 confidence。
6) 若 regime=RANGE，则强调均值回归/极值类（imbalance、micro、phantom、trap），弱化趋势延续类（cvd_momentum、oi_delta）。
7) 若 regime=TREND，则强调趋势确认类（cvd、oi、funding、depth），但当资金不一致(flow_confirm=false)时必须降权并降低 confidence。"""

# 用户提示词模板
USER_PROMPT_TEMPLATE = """请为本次输入生成打分因子权重（weights），并严格按 JSON schema 输出。

【背景】
- 交易框架：15m 负责市场状态识别与主资金评分，5m 负责执行节奏评分；最终分数融合：FinalScore = 0.6*Score_15m + 0.4*Score_5m
- 你只输出权重与置信度，不输出方向/动作/阈值/仓位/杠杆。

【输入】
timestamp_utc: {timestamp_utc}
symbol: {symbol}

regime:
  name: {regime_name}
  trend_strength: {trend_strength}
  adx: {adx}
  atr_pct: {atr_pct}
  ema_bias: {ema_bias}

flow_consistency:
  flow_confirm: {flow_confirm}
  consistency_3bars: {consistency_3bars}

features_15m_z:
  cvd_z: {cvd_z}
  cvd_mom_z: {cvd_mom_z}
  oi_delta_z: {oi_delta_z}
  funding_z: {funding_z}
  depth_ratio_z: {depth_ratio_z}
  imbalance_z: {imbalance_z}
  liquidity_delta_z: {liquidity_delta_z}
  micro_delta_z: {micro_delta_z}

microstructure_risk:
  spread_z: {spread_z}
  trap_score: {trap_score}
  phantom_score: {phantom_score}
  trap_confirmed: {trap_confirmed}
  extreme_vol_cooldown: {extreme_vol_cooldown}

# 风险摘要（用于降低动量权重/降低置信度；不用于输出交易动作）
risk_flags: {risk_flags}

data_quality:
  missing_fields: {missing_fields}
  stale_seconds: {stale_seconds}
  sample_ok: {sample_ok}

default_weights:
  TREND:
    cvd: {dw_trend_cvd}
    cvd_momentum: {dw_trend_cvd_mom}
    oi_delta: {dw_trend_oi}
    funding: {dw_trend_funding}
    depth_ratio: {dw_trend_depth}
    imbalance: {dw_trend_imb}
    liquidity_delta: {dw_trend_liq}
    micro_delta: {dw_trend_micro}
  RANGE:
    cvd: {dw_range_cvd}
    cvd_momentum: {dw_range_cvd_mom}
    oi_delta: {dw_range_oi}
    funding: {dw_range_funding}
    depth_ratio: {dw_range_depth}
    imbalance: {dw_range_imb}
    liquidity_delta: {dw_range_liq}
    micro_delta: {dw_range_micro}

【输出要求】
- 严格输出 JSON，字段必须齐全，且 weights 总和=1。
- weights 必须包含以下键：
  cvd, cvd_momentum, oi_delta, funding, depth_ratio, imbalance, liquidity_delta, micro_delta
- confidence 范围 0~1。
- reasoning_bullets 最多 5 条，每条不超过 20 个字。
- 如果 sample_ok=false 或 stale_seconds>30 或 missing_fields 非空：fallback_used=true，并直接使用 default_weights 对应 regime 的权重（并归一化）。"""

# 禁止输出的词列表
FORBIDDEN_WORDS = [
    "BUY", "SELL", "LONG", "SHORT",
    "close_threshold", "leverage", "position",
    "开多", "开空", "平仓", "买入", "卖出",
    "threshold", "stop_loss", "take_profit"
]

# 权重键列表
REQUIRED_WEIGHT_KEYS = [
    "cvd", "cvd_momentum", "oi_delta", "funding",
    "depth_ratio", "imbalance", "liquidity_delta", "micro_delta"
]


@dataclass
class AIWeightResponse:
    """AI 权重响应"""
    version: str = "weight-router-v1"
    symbol: str = ""
    timestamp_utc: str = ""
    regime_view: Dict[str, Any] = field(default_factory=dict)
    risk_flags: Dict[str, bool] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5
    reasoning_bullets: List[str] = field(default_factory=list)
    fallback_used: bool = False
    raw_response: str = ""
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "symbol": self.symbol,
            "timestamp_utc": self.timestamp_utc,
            "regime_view": self.regime_view,
            "risk_flags": self.risk_flags,
            "weights": self.weights,
            "confidence": self.confidence,
            "reasoning_bullets": self.reasoning_bullets,
            "fallback_used": self.fallback_used,
            "error": self.error,
        }


@dataclass
class DefaultWeights:
    """默认权重配置"""
    trend_cvd: float = 0.24
    trend_cvd_momentum: float = 0.14
    trend_oi_delta: float = 0.22
    trend_funding: float = 0.10
    trend_depth_ratio: float = 0.15
    trend_imbalance: float = 0.10
    trend_liquidity_delta: float = 0.08
    trend_micro_delta: float = 0.06
    
    range_cvd: float = 0.10
    range_cvd_momentum: float = 0.15
    range_oi_delta: float = 0.05
    range_funding: float = 0.05
    range_depth_ratio: float = 0.10
    range_imbalance: float = 0.35
    range_liquidity_delta: float = 0.12
    range_micro_delta: float = 0.18


class DeepSeekAIService:
    """
    DeepSeek AI 权重调度服务
    
    功能:
    1. 调用 DeepSeek API 生成权重
    2. 严格校验输出
    3. 失败降级
    4. 智能缓存
    """
    
    # API 配置
    DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
    DEFAULT_MODEL = "deepseek-chat"
    DEFAULT_TIMEOUT = 15
    DEFAULT_MAX_RETRIES = 2
    
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        ai_cfg = self.config.get("deepseek_ai", {})
        
        # API 配置
        self.api_key = os.environ.get("DEEPSEEK_API_KEY") or ai_cfg.get("api_key", "")
        self.api_url = ai_cfg.get("api_url", self.DEFAULT_API_URL)
        self.model = ai_cfg.get("model", self.DEFAULT_MODEL)
        self.timeout = int(ai_cfg.get("timeout", self.DEFAULT_TIMEOUT))
        self.max_retries = int(ai_cfg.get("max_retries", self.DEFAULT_MAX_RETRIES))
        
        # 功能开关
        self.enabled = bool(ai_cfg.get("enabled", False)) and bool(self.api_key)
        
        # 默认权重
        dw_cfg = ai_cfg.get("default_weights", {})
        self.default_weights = DefaultWeights(
            trend_cvd=float(dw_cfg.get("trend_cvd", 0.24)),
            trend_cvd_momentum=float(dw_cfg.get("trend_cvd_momentum", 0.14)),
            trend_oi_delta=float(dw_cfg.get("trend_oi_delta", 0.22)),
            trend_funding=float(dw_cfg.get("trend_funding", 0.10)),
            trend_depth_ratio=float(dw_cfg.get("trend_depth_ratio", 0.15)),
            trend_imbalance=float(dw_cfg.get("trend_imbalance", 0.10)),
            trend_liquidity_delta=float(dw_cfg.get("trend_liquidity_delta", 0.08)),
            trend_micro_delta=float(dw_cfg.get("trend_micro_delta", 0.06)),
            range_cvd=float(dw_cfg.get("range_cvd", 0.10)),
            range_cvd_momentum=float(dw_cfg.get("range_cvd_momentum", 0.15)),
            range_oi_delta=float(dw_cfg.get("range_oi_delta", 0.05)),
            range_funding=float(dw_cfg.get("range_funding", 0.05)),
            range_depth_ratio=float(dw_cfg.get("range_depth_ratio", 0.10)),
            range_imbalance=float(dw_cfg.get("range_imbalance", 0.35)),
            range_liquidity_delta=float(dw_cfg.get("range_liquidity_delta", 0.12)),
            range_micro_delta=float(dw_cfg.get("range_micro_delta", 0.18)),
        )
        
        # 缓存配置
        self.cache_ttl = int(ai_cfg.get("cache_ttl", 300))  # 5分钟
        self._cache: Dict[str, Tuple[AIWeightResponse, float]] = {}
        
        # 统计
        self._stats = {
            "total_requests": 0,
            "api_calls": 0,
            "cache_hits": 0,
            "fallbacks": 0,
            "errors": 0,
        }
        
        # HTTP 客户端 (延迟初始化)
        self._http_client = None
    
    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default
    
    def _get_http_client(self):
        """延迟初始化 HTTP 客户端"""
        if self._http_client is None:
            try:
                import requests
                self._http_client = requests
            except ImportError:
                logger.warning("requests not installed, AI calls will fail")
        return self._http_client
    
    def _build_user_prompt(self, context: Dict[str, Any]) -> str:
        """构建用户提示词"""
        regime = context.get("regime", "NO_TRADE")
        dw = self.default_weights

        def _b(x: Any) -> str:
            """确保 bool 以 true/false 输出（而不是 Python 的 True/False）"""
            return "true" if bool(x) else "false"

        def _f(x: Any, default: float = 0.0, ndigits: int = 6) -> float:
            """浮点归一精度，减少 prompt 抖动"""
            try:
                v = float(x)
            except Exception:
                v = float(default)
            # 防御：inf/nan
            if v != v or v == float("inf") or v == float("-inf"):
                v = float(default)
            return round(v, ndigits)

        # 构建缺失字段列表
        missing = context.get("missing_fields", [])
        if not isinstance(missing, list):
            missing = []
        missing_str = json.dumps(missing) if missing else "[]"

        # risk_flags：优先用 context["risk_flags"]（你在 _build_context 已提供 dict）
        rf = context.get("risk_flags", {})
        if not isinstance(rf, dict):
            rf = {}
        # 兼容：如果没给 dict，则用平铺字段推断一次
        if not rf:
            stale_seconds_tmp = int(self._to_float(context.get("stale_seconds"), 0))
            rf = {
                "trap": bool(context.get("trap_flag", False)),
                "phantom": bool(context.get("phantom_flag", False)),
                "wide_spread": bool(context.get("wide_spread", False)),
                "data_stale": bool(stale_seconds_tmp > 30),
            }
        risk_flags_str = json.dumps(rf, ensure_ascii=False)

        return USER_PROMPT_TEMPLATE.format(
            timestamp_utc=context.get("timestamp_utc", datetime.now(timezone.utc).isoformat()),
            symbol=context.get("symbol", "UNKNOWN"),
            regime_name=regime,
            trend_strength=_f(context.get("trend_strength"), 0.0, 4),
            adx=_f(context.get("adx"), 0.0, 4),
            atr_pct=_f(context.get("atr_pct"), 0.0, 6),
            ema_bias=context.get("ema_bias", "FLAT"),
            flow_confirm=_b(context.get("flow_confirm", False)),
            consistency_3bars=int(self._to_float(context.get("consistency_3bars"), 0)),
            cvd_z=_f(context.get("cvd_z"), 0.0, 4),
            cvd_mom_z=_f(context.get("cvd_mom_z"), 0.0, 4),
            oi_delta_z=_f(context.get("oi_delta_z"), 0.0, 4),
            funding_z=_f(context.get("funding_z"), 0.0, 6),
            depth_ratio_z=_f(context.get("depth_ratio_z"), 0.0, 4),
            imbalance_z=_f(context.get("imbalance_z"), 0.0, 4),
            liquidity_delta_z=_f(context.get("liquidity_delta_z"), 0.0, 4),
            micro_delta_z=_f(context.get("micro_delta_z"), 0.0, 4),
            spread_z=_f(context.get("spread_z"), 0.0, 4),
            trap_score=_f(context.get("trap_score"), 0.0, 4),
            phantom_score=_f(context.get("phantom_score"), 0.0, 4),
            trap_confirmed=_b(context.get("trap_confirmed", False)),
            extreme_vol_cooldown=_b(context.get("extreme_vol_cooldown", False)),
            risk_flags=risk_flags_str,
            missing_fields=missing_str,
            stale_seconds=int(self._to_float(context.get("stale_seconds"), 0)),
            sample_ok=_b(context.get("sample_ok", True)),
            dw_trend_cvd=dw.trend_cvd,
            dw_trend_cvd_mom=dw.trend_cvd_momentum,
            dw_trend_oi=dw.trend_oi_delta,
            dw_trend_funding=dw.trend_funding,
            dw_trend_depth=dw.trend_depth_ratio,
            dw_trend_imb=dw.trend_imbalance,
            dw_trend_liq=dw.trend_liquidity_delta,
            dw_trend_micro=dw.trend_micro_delta,
            dw_range_cvd=dw.range_cvd,
            dw_range_cvd_mom=dw.range_cvd_momentum,
            dw_range_oi=dw.range_oi_delta,
            dw_range_funding=dw.range_funding,
            dw_range_depth=dw.range_depth_ratio,
            dw_range_imb=dw.range_imbalance,
            dw_range_liq=dw.range_liquidity_delta,
            dw_range_micro=dw.range_micro_delta,
        )
    
    def _make_structured_cache_key(self, context: Dict[str, Any]) -> str:
        """
        基于“决策结构”构建稳定 cache key
        避免 prompt 文本微小变化导致缓存失效
        """
        symbol = str(context.get("symbol", "UNKNOWN")).upper()
        regime = str(context.get("regime_name") or context.get("regime") or "NO_TRADE").upper()

        trend_strength = self._to_float(context.get("trend_strength"), 0.0)
        spread_z = self._to_float(context.get("spread_z"), 0.0)
        consistency = int(self._to_float(context.get("consistency_3bars"), 0))

        rf = context.get("risk_flags", {})
        if not isinstance(rf, dict):
            rf = {}

        flow_confirm = bool(context.get("flow_confirm", False))
        trap_flag = bool(context.get("trap_flag", rf.get("trap", False)))
        phantom_flag = bool(context.get("phantom_flag", rf.get("phantom", False)))
        wide_spread = bool(context.get("wide_spread", rf.get("wide_spread", False)))
        sample_ok = bool(context.get("sample_ok", True))

        # === 桶化 ===
        trend_bucket = round(trend_strength, 1)
        if spread_z >= 2.5:
            spread_bucket = 3
        elif spread_z >= 1.5:
            spread_bucket = 2
        elif spread_z >= 0.8:
            spread_bucket = 1
        else:
            spread_bucket = 0

        # consistency 只取 0~3
        consistency_bucket = max(0, min(3, consistency))

        raw_key = (
            f"{symbol}|"
            f"{regime}|"
            f"ts{trend_bucket}|"
            f"sp{spread_bucket}|"
            f"cf{int(flow_confirm)}|"
            f"c3{consistency_bucket}|"
            f"tp{int(trap_flag)}|"
            f"ph{int(phantom_flag)}|"
            f"ws{int(wide_spread)}|"
            f"ok{int(sample_ok)}"
        )

        return hashlib.md5(raw_key.encode("utf-8")).hexdigest()

    def _cache_key(self, context: Dict[str, Any]) -> str:
        """兼容旧调用：转到结构化 cache key"""
        return self._make_structured_cache_key(context)
    
    def _get_cached(self, cache_key: str) -> Optional[AIWeightResponse]:
        """从缓存获取"""
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        response, expires_at = entry
        if time.time() > expires_at:
            del self._cache[cache_key]
            return None
        self._stats["cache_hits"] += 1
        return response

    def _get_cache_ttl_for_context(self, context: Dict[str, Any]) -> int:
        """按 regime 返回动态 TTL（秒）: TREND 15m / RANGE 10m / 其他 5m"""
        regime = str(context.get("regime_name") or context.get("regime") or "NO_TRADE").upper()
        if regime == "TREND":
            return 15 * 60
        if regime == "RANGE":
            return 10 * 60
        return 5 * 60

    def _set_cache(
        self,
        cache_key: str,
        response: AIWeightResponse,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """设置缓存"""
        ttl = int(ttl_seconds) if ttl_seconds is not None else int(self.cache_ttl)
        ttl = max(1, ttl)
        expires_at = time.time() + ttl
        self._cache[cache_key] = (response, expires_at)
        
        # 清理过期缓存
        now = time.time()
        expired_keys = [k for k, v in self._cache.items() if v[1] < now]
        for k in expired_keys:
            del self._cache[k]
    
    def _should_fallback(self, context: Dict[str, Any]) -> Tuple[bool, str]:
        """判断是否应该直接使用降级策略"""
        # 数据质量检查
        sample_ok = context.get("sample_ok", True)
        stale_seconds = self._to_float(context.get("stale_seconds"), 0)
        missing_fields = context.get("missing_fields", [])
        
        if not sample_ok:
            return True, "sample_not_ok"
        if stale_seconds > 30:
            return True, f"data_stale({stale_seconds}s)"
        if missing_fields and isinstance(missing_fields, list) and len(missing_fields) > 0:
            return True, f"missing_fields:{','.join(missing_fields[:3])}"
        
        return False, ""
    
    def _get_default_weights(self, regime: str) -> Dict[str, float]:
        """获取默认权重并归一化"""
        dw = self.default_weights
        
        if regime == "TREND":
            weights = {
                "cvd": dw.trend_cvd,
                "cvd_momentum": dw.trend_cvd_momentum,
                "oi_delta": dw.trend_oi_delta,
                "funding": dw.trend_funding,
                "depth_ratio": dw.trend_depth_ratio,
                "imbalance": dw.trend_imbalance,
                "liquidity_delta": dw.trend_liquidity_delta,
                "micro_delta": dw.trend_micro_delta,
            }
        elif regime == "RANGE":
            weights = {
                "cvd": dw.range_cvd,
                "cvd_momentum": dw.range_cvd_momentum,
                "oi_delta": dw.range_oi_delta,
                "funding": dw.range_funding,
                "depth_ratio": dw.range_depth_ratio,
                "imbalance": dw.range_imbalance,
                "liquidity_delta": dw.range_liquidity_delta,
                "micro_delta": dw.range_micro_delta,
            }
        else:
            # NO_TRADE 或其他情况使用趋势权重
            weights = {
                "cvd": dw.trend_cvd,
                "cvd_momentum": dw.trend_cvd_momentum,
                "oi_delta": dw.trend_oi_delta,
                "funding": dw.trend_funding,
                "depth_ratio": dw.trend_depth_ratio,
                "imbalance": dw.trend_imbalance,
                "liquidity_delta": dw.trend_liquidity_delta,
                "micro_delta": dw.trend_micro_delta,
            }
        
        # 归一化
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}
        
        return weights
    
    def _create_fallback_response(self, context: Dict[str, Any], reason: str) -> AIWeightResponse:
        """创建降级响应"""
        regime = context.get("regime", "TREND")
        weights = self._get_default_weights(regime)
        
        return AIWeightResponse(
            version="weight-router-v1",
            symbol=str(context.get("symbol", "")),
            timestamp_utc=context.get("timestamp_utc", datetime.now(timezone.utc).isoformat()),
            regime_view={
                "name": regime,
                "trend_strength": self._to_float(context.get("trend_strength"), 0.0),
                "notes": f"fallback:{reason}",
            },
            risk_flags={
                "trap": context.get("trap_confirmed", False),
                "phantom": self._to_float(context.get("phantom_score"), 0) > 0.5,
                "wide_spread": self._to_float(context.get("spread_z"), 0) > 2.0,
                "data_stale": reason.startswith("data_stale") or reason.startswith("missing"),
            },
            weights=weights,
            confidence=0.25,
            reasoning_bullets=[f"降级原因:{reason}"],
            fallback_used=True,
            error=reason,
        )
    
    def _validate_response(self, response_text: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        校验 AI 响应
        
        返回: (is_valid, parsed_json, error_message)
        """
        # 1. 尝试解析 JSON
        try:
            # 清理可能的 markdown 标记
            cleaned = response_text.strip()
            if cleaned.startswith("```"):
                # 移除 markdown 代码块
                lines = cleaned.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines)
            
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            return False, None, f"json_parse_error:{str(e)}"
        
        if not isinstance(parsed, dict):
            return False, None, "response_not_dict"
        
        # 2. 检查禁词
        response_lower = response_text.lower()
        for word in FORBIDDEN_WORDS:
            if word.lower() in response_lower:
                return False, parsed, f"forbidden_word:{word}"
        
        # 3. 检查必需字段
        weights = parsed.get("weights")
        if not isinstance(weights, dict):
            return False, parsed, "missing_weights"
        
        # 4. 检查权重键
        missing_keys = [k for k in REQUIRED_WEIGHT_KEYS if k not in weights]
        if missing_keys:
            return False, parsed, f"missing_weight_keys:{','.join(missing_keys)}"
        
        # 5. 检查权重值范围
        for k, v in weights.items():
            if not isinstance(v, (int, float)):
                return False, parsed, f"weight_not_number:{k}"
            if v < 0 or v > 1:
                return False, parsed, f"weight_out_of_range:{k}={v}"
        
        # 6. 检查权重和
        weight_sum = sum(weights.values())
        if abs(weight_sum - 1.0) > 1e-4:
            # 尝试归一化
            if weight_sum > 0:
                parsed["weights"] = {k: v / weight_sum for k, v in weights.items()}
            else:
                return False, parsed, f"weight_sum_invalid:{weight_sum}"
        
        # 7. 检查 confidence
        confidence = parsed.get("confidence")
        if confidence is not None:
            if not isinstance(confidence, (int, float)):
                parsed["confidence"] = 0.5
            elif confidence < 0 or confidence > 1:
                parsed["confidence"] = max(0, min(1, confidence))
        
        # 8. 确保 fallback_used 字段存在
        if "fallback_used" not in parsed:
            parsed["fallback_used"] = False
        
        return True, parsed, ""
    
    def _call_api(self, user_prompt: str) -> Tuple[bool, str, str]:
        """
        调用 DeepSeek API
        
        返回: (success, response_text, error_message)
        """
        http = self._get_http_client()
        if http is None:
            return False, "", "http_client_not_available"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,  # 低温度，更确定性输出
            "max_tokens": 500,
        }
        
        for attempt in range(self.max_retries):
            try:
                response = http.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices", [])
                    if choices and len(choices) > 0:
                        content = choices[0].get("message", {}).get("content", "")
                        if content:
                            self._stats["api_calls"] += 1
                            return True, content, ""
                    return False, "", "empty_response"
                elif response.status_code == 429:
                    # 速率限制，等待重试
                    time.sleep(1 + attempt)
                    continue
                else:
                    return False, "", f"http_error:{response.status_code}"
                    
            except Exception as e:
                logger.warning(f"DeepSeek API call failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 + attempt)
                else:
                    return False, "", f"exception:{str(e)}"
        
        return False, "", "max_retries_exceeded"
    
    def get_weights(
        self,
        symbol: str,
        regime: str,
        market_flow_context: Dict[str, Any],
        quantile_context: Optional[Dict[str, Any]] = None,
    ) -> AIWeightResponse:
        """
        获取动态权重
        
        Args:
            symbol: 交易标的
            regime: 市场状态
            market_flow_context: 市场资金流上下文
            quantile_context: 分位数上下文
        
        Returns:
            AIWeightResponse: 权重响应
        """
        self._stats["total_requests"] += 1
        
        # 构建上下文
        context = self._build_context(
            symbol, regime, market_flow_context, quantile_context
        )
        
        # 检查是否应该直接降级
        should_fallback, fallback_reason = self._should_fallback(context)
        if should_fallback:
            self._stats["fallbacks"] += 1
            return self._create_fallback_response(context, fallback_reason)
        
        # 检查缓存
        cache_key = self._make_structured_cache_key(context)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        
        # 如果未启用 AI，直接返回默认权重
        if not self.enabled:
            self._stats["fallbacks"] += 1
            return self._create_fallback_response(context, "ai_disabled")
        
        # 调用 AI
        user_prompt = self._build_user_prompt(context)
        success, response_text, error = self._call_api(user_prompt)
        
        if not success:
            self._stats["errors"] += 1
            self._stats["fallbacks"] += 1
            return self._create_fallback_response(context, f"api_error:{error}")
        
        # 校验响应
        is_valid, parsed, validation_error = self._validate_response(response_text)
        
        if not is_valid:
            self._stats["errors"] += 1
            self._stats["fallbacks"] += 1
            logger.warning(f"AI response validation failed: {validation_error}")
            return self._create_fallback_response(context, f"validation_error:{validation_error}")
        
        # parsed 已验证通过，必定是 dict
        parsed_dict: Dict[str, Any] = parsed if isinstance(parsed, dict) else {}
        
        # 构建成功响应
        response = AIWeightResponse(
            version=parsed_dict.get("version", "weight-router-v1"),
            symbol=symbol,
            timestamp_utc=context.get("timestamp_utc", datetime.now(timezone.utc).isoformat()),
            regime_view=parsed_dict.get("regime_view", {}),
            risk_flags=parsed_dict.get("risk_flags", {}),
            weights=parsed_dict.get("weights", {}),
            confidence=float(parsed_dict.get("confidence", 0.5)),
            reasoning_bullets=parsed_dict.get("reasoning_bullets", []),
            fallback_used=bool(parsed_dict.get("fallback_used", False)),
            raw_response=response_text,
        )
        
        # 缓存结果
        self._set_cache(
            cache_key,
            response,
            ttl_seconds=self._get_cache_ttl_for_context(context),
        )
        
        return response
    
    def _build_context(
        self,
        symbol: str,
        regime: str,
        market_flow_context: Dict[str, Any],
        quantile_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构建完整上下文（V3.0 增强：语义正确的输入）"""
        timeframes = market_flow_context.get("timeframes", {})
        tf_15m = timeframes.get("15m", {}) if isinstance(timeframes, dict) else {}
        tf_5m = timeframes.get("5m", {}) if isinstance(timeframes, dict) else {}
        
        # 优先从结构化输出取值
        ms = market_flow_context.get("microstructure_features", {})
        if not isinstance(ms, dict):
            ms = {}
        ff = market_flow_context.get("fund_flow_features", {})
        if not isinstance(ff, dict):
            ff = {}
        
        # 统一字段（优先 fund_flow_features，否则用旧字段兜底）
        cvd = self._to_float(ff.get("cvd"), self._to_float(market_flow_context.get("cvd_ratio"), 0.0))
        cvd_mom = self._to_float(ff.get("cvd_momentum"), self._to_float(market_flow_context.get("cvd_momentum"), 0.0))
        oi_delta = self._to_float(ff.get("oi_delta"), self._to_float(market_flow_context.get("oi_delta_ratio"), 0.0))
        funding = self._to_float(ff.get("funding"), self._to_float(market_flow_context.get("funding_rate"), 0.0))
        depth = self._to_float(ff.get("depth_ratio"), self._to_float(market_flow_context.get("depth_ratio"), 1.0))
        imbalance = self._to_float(market_flow_context.get("imbalance"), 0.0)
        liq_delta = self._to_float(ff.get("liquidity_delta"), self._to_float(market_flow_context.get("liquidity_delta_norm"), 0.0))
        
        # 微结构指标（优先 microstructure_features）
        trap_score = self._to_float(ms.get("trap_score"), self._to_float(tf_5m.get("trap_last"), 
                               self._to_float(market_flow_context.get("trap_score"), 0.0)))
        phantom_score = self._to_float(ms.get("phantom_score"), self._to_float(tf_5m.get("phantom_mean"),
                                 self._to_float(market_flow_context.get("phantom"), 0.0)))
        spread_bps = self._to_float(ms.get("spread_bps"), self._to_float(tf_5m.get("spread_bps_last"), 0.0))
        
        # 获取 15m 历史用于 z-score 计算
        hist15 = tf_15m.get("history", [])
        if not isinstance(hist15, list):
            hist15 = []
        
        # z-score 计算函数
        def _zscore_from_hist(key: str, x: float, hist: list, min_n: int = 30, eps: float = 1e-9) -> float:
            if not isinstance(hist, list) or len(hist) < min_n:
                return 0.0
            vals = []
            for row in hist[-min_n:]:
                if isinstance(row, dict):
                    raw_v = row.get(key)
                    if raw_v is not None:
                        try:
                            vals.append(float(raw_v))
                        except (TypeError, ValueError):
                            pass
            if len(vals) < max(12, min_n // 2):
                return 0.0
            mu = sum(vals) / len(vals)
            var = sum((v - mu) ** 2 for v in vals) / max(1, (len(vals) - 1))
            sd = (var ** 0.5) + eps
            z = (x - mu) / sd
            # winsor clip，防止极端值
            return float(max(-5.0, min(5.0, z)))
        
        # 计算 z-scores
        cvd_z = _zscore_from_hist("cvd", cvd, hist15)
        cvd_mom_z = _zscore_from_hist("cvd_momentum", cvd_mom, hist15)
        oi_delta_z = _zscore_from_hist("oi_delta", oi_delta, hist15)
        funding_z = _zscore_from_hist("funding", funding, hist15)
        depth_ratio_z = _zscore_from_hist("depth_ratio", depth, hist15)
        imbalance_z = _zscore_from_hist("imbalance", imbalance, hist15)
        liquidity_delta_z = _zscore_from_hist("liquidity_delta", liq_delta, hist15)
        micro_delta = self._to_float(ff.get("micro_delta"), self._to_float(tf_5m.get("micro_delta_last"), 0.0))
        micro_delta_z = _zscore_from_hist("micro_delta", micro_delta, hist15)
        
        # spread_z：优先用 microstructure_features 输出，否则从历史计算
        spread_z = self._to_float(ms.get("spread_z"), 0.0)
        if spread_z == 0.0 and spread_bps > 0:
            # fallback：用 5m history 算
            hist_spread = tf_5m.get("history_spread_bps", [])
            if isinstance(hist_spread, list) and len(hist_spread) >= 30:
                spread_z = _zscore_from_hist("__spread__", spread_bps, 
                             [{"__spread__": v} for v in hist_spread], min_n=30)
        
        # 从 quantile_context 获取额外信息
        trap_confirmed = False
        if quantile_context:
            trap_guard = self._to_float(quantile_context.get("trap_guard"), 0.7)
            trap_confirmed = trap_score > trap_guard

        # 额外：极端缺口情况（spread_z 很高但 trap_guard 未提供时，也可强制 trap_confirmed）
        # 这不是 hard gate，只是给 DeepSeek 一个“风险提示”语义
        if (not trap_confirmed) and spread_z >= 3.0 and trap_score >= 0.8:
            trap_confirmed = True
        
        # flow_confirm：必须引入价格方向
        def _sgn(x: float) -> int:
            return 1 if x > 0 else (-1 if x < 0 else 0)
        
        ret_period = self._to_float(ff.get("ret_period"), self._to_float(tf_15m.get("ret_period"), 0.0))
        cvd_s = _sgn(cvd)
        oi_s = _sgn(oi_delta)
        ret_s = _sgn(ret_period)
        
        # 资金一致性：CVD、OI、价格方向至少"两两一致"，且不能全为0
        flow_confirm = (ret_s != 0) and (
            (cvd_s == ret_s and oi_s == ret_s) or
            (cvd_s == ret_s and oi_s == 0) or
            (oi_s == ret_s and cvd_s == 0)
        )
        
        # consistency_3bars：从 15m 历史计算
        def _consistency_3bars(hist: list) -> int:
            if not isinstance(hist, list) or len(hist) < 3:
                return 0
            cnt = 0
            for row in hist[-3:]:
                if not isinstance(row, dict):
                    continue
                r = _sgn(self._to_float(row.get("ret_period"), 0.0))
                c = _sgn(self._to_float(row.get("cvd"), 0.0))
                if r != 0 and c != 0 and r == c:
                    cnt += 1
            return cnt
        
        consistency_3bars = _consistency_3bars(hist15)
        
        # EMA 偏向
        ema_fast = self._to_float(tf_15m.get("ema_fast"), 0)
        ema_slow = self._to_float(tf_15m.get("ema_slow"), 0)
        if ema_fast > ema_slow * 1.001:
            ema_bias = "UP"
        elif ema_fast < ema_slow * 0.999:
            ema_bias = "DOWN"
        else:
            ema_bias = "FLAT"
        
        # 趋势强度
        adx = self._to_float(tf_15m.get("adx"), 0)
        trend_strength = min(1.0, max(0.0, (adx - 15) / 25)) if adx > 15 else 0
        
        # risk_flags 自动推断
        wide_spread = spread_z >= 2.5
        trap_flag = bool(trap_confirmed) or (trap_score >= 0.8)
        phantom_flag = phantom_score >= 0.8

        # 数据质量检查
        missing_fields: List[str] = []
        # 重要：0.0 不等于缺失（很多时候真实值就是0）
        # 缺失判定应基于“字段不存在/None/样本不足”而不是数值为0
        # 这里用最小集合：关键历史不足也算“不可用”
        if not isinstance(hist15, list) or len(hist15) < 12:
            missing_fields.append("hist15_insufficient")
        # 若 fund_flow_features/microstructure_features 块缺失关键键，才算缺失
        if "cvd" not in ff and "cvd_ratio" not in market_flow_context:
            missing_fields.append("cvd")
        if "oi_delta" not in ff and "oi_delta_ratio" not in market_flow_context:
            missing_fields.append("oi_delta")
        if "adx" not in tf_15m:
            missing_fields.append("adx")
        
        # stale_seconds：用当前时间与 tf_15m close 时间差
        stale_seconds = 0
        tf_15m_ts = tf_15m.get("bucket_ts") or tf_15m.get("timestamp_close_utc")
        if tf_15m_ts:
            try:
                if isinstance(tf_15m_ts, str):
                    ts_close = datetime.fromisoformat(tf_15m_ts.replace("Z", "+00:00"))
                elif isinstance(tf_15m_ts, (int, float)):
                    ts_close = datetime.fromtimestamp(tf_15m_ts, tz=timezone.utc)
                else:
                    ts_close = tf_15m_ts
                stale_seconds = int((datetime.now(timezone.utc) - ts_close).total_seconds())
            except Exception:
                pass

        # 防御：如果解析失败/未来时间戳导致负数，归零
        if stale_seconds < 0:
            stale_seconds = 0

        # sample_ok：数据新鲜 + 关键字段存在 + 历史足够做 zscore
        # 注意：adx==0 可能是指标尚未形成，不强制失败；但缺失 adx 键则失败
        sample_ok = (len(missing_fields) == 0) and (stale_seconds <= 30)
        
        # 极端波动冷却
        atr_pct = self._to_float(tf_15m.get("atr_pct"), 0)
        extreme_vol_cooldown = atr_pct > 0.02
        
        return {
            "symbol": symbol,
            "regime": regime,
            "regime_name": regime,  # prompt 兼容
            "timestamp_utc": tf_15m.get("timestamp_close_utc") or datetime.now(timezone.utc).isoformat(),
            "trend_strength": trend_strength,
            "adx": adx,
            "atr_pct": atr_pct,
            "ema_bias": ema_bias,
            "flow_confirm": flow_confirm,
            "consistency_3bars": consistency_3bars,
            # 真正的 z-scores
            "cvd_z": cvd_z,
            "cvd_mom_z": cvd_mom_z,
            "oi_delta_z": oi_delta_z,
            "funding_z": funding_z,
            "depth_ratio_z": depth_ratio_z,
            "imbalance_z": imbalance_z,
            "liquidity_delta_z": liquidity_delta_z,
            "micro_delta_z": micro_delta_z,
            # 微结构风险
            "spread_z": spread_z,
            "spread_bps": spread_bps,
            "trap_score": trap_score,
            "phantom_score": phantom_score,
            "trap_confirmed": trap_confirmed,
            "extreme_vol_cooldown": extreme_vol_cooldown,
            # risk_flags（自动推断）
            "wide_spread": wide_spread,
            "trap_flag": trap_flag,
            "phantom_flag": phantom_flag,
            # 额外：按 prompt 输出风格提供 risk_flags dict（不破坏你已有平铺字段）
            "risk_flags": {
                "trap": bool(trap_flag),
                "phantom": bool(phantom_flag),
                "wide_spread": bool(wide_spread),
                "data_stale": bool(stale_seconds > 30),
            },
            # 数据质量（不再写死）
            "missing_fields": missing_fields,
            "stale_seconds": stale_seconds,
            "sample_ok": sample_ok,
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        total = self._stats["total_requests"]
        hits = self._stats["cache_hits"]
        fallbacks = self._stats["fallbacks"]
        
        return {
            **self._stats,
            "cache_hit_rate": hits / total if total > 0 else 0,
            "fallback_rate": fallbacks / total if total > 0 else 0,
            "cache_size": len(self._cache),
            "enabled": self.enabled,
        }
    
    def clear_cache(self) -> int:
        """清空缓存"""
        count = len(self._cache)
        self._cache.clear()
        return count


# 导出
__all__ = [
    "AIWeightResponse",
    "DefaultWeights",
    "DeepSeekAIService",
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
]
