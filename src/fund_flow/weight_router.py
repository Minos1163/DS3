"""
Weight Router - 本地权重校验与调度模块

核心功能:
1. TTL 缓存管理
2. 权重归一化与校验
3. JSON schema 校验
4. 禁词扫描（防止AI越权输出）
5. 失败降级策略
6. 持久化缓存 (MarketStorage)

设计原则:
- 与 MarketIngestionService 字段命名对齐
- 与 DecisionEngine 兼容
- 支持本地规则回退
- 智能缓存策略

字段映射 (与 MarketIngestionService 一致):
- cvd_ratio -> cvd
- cvd_momentum -> cvd_momentum
- oi_delta_ratio -> oi_delta
- funding_rate -> funding
- depth_ratio -> depth_ratio
- imbalance -> imbalance
- liquidity_delta_norm -> liquidity_delta
- micro_delta_last/micro_delta_norm -> micro_delta
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.fund_flow.market_storage import MarketStorage

# =========================
# 常量定义
# =========================

WEIGHT_KEYS = [
    "cvd", "cvd_momentum", "oi_delta", "funding",
    "depth_ratio", "imbalance", "liquidity_delta", "micro_delta"
]

DEFAULT_CONFIDENCE_FALLBACK = 0.25
SUM_EPS = 1e-6
STALE_LIMIT_SECONDS = 30
TTL_SECONDS_DEFAULT = 10 * 60  # 10分钟

# 禁词：防止 AI 越权给方向/动作/阈值/仓位/杠杆建议
BANNED_PATTERNS = [
    r"\bBUY\b", r"\bSELL\b", r"\bLONG\b", r"\bSHORT\b",
    r"\bLEVERAGE\b", r"\bPOSITION\b", r"\bTHRESHOLD\b",
    r"\bSTOP[_\s-]?LOSS\b", r"\bTAKE[_\s-]?PROFIT\b",
    r"\bENTRY\b", r"\bEXIT\b", r"\bCLOSE\b",
    r"open_threshold", r"close_threshold", r"max_leverage", r"min_leverage"
]
BANNED_REGEX = re.compile("|".join(BANNED_PATTERNS), re.IGNORECASE)


# =========================
# TTL 缓存类
# =========================

class TTLCache:
    """简单 TTL 缓存（内存版）"""
    
    def __init__(self, max_entries: int = 1000):
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._max_entries = max_entries
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存值，过期返回 None"""
        item = self._store.get(key)
        if item is None:
            return None
        expire_at, value = item
        if time.time() >= expire_at:
            self._store.pop(key, None)
            return None
        return value
    
    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """设置缓存值"""
        # 清理过期条目
        self._evict_expired()
        
        # LRU 淘汰
        if len(self._store) >= self._max_entries:
            self._evict_lru()
        
        self._store[key] = (time.time() + ttl_seconds, value)
    
    def _evict_expired(self) -> int:
        """清理过期条目"""
        now = time.time()
        expired = [k for k, v in self._store.items() if v[0] < now]
        for k in expired:
            del self._store[k]
        return len(expired)
    
    def _evict_lru(self) -> None:
        """LRU 淘汰"""
        # 简单实现：删除 10% 的条目
        if len(self._store) < 10:
            return
        n_remove = len(self._store) // 10
        keys = list(self._store.keys())[:n_remove]
        for k in keys:
            del self._store[k]
    
    def clear(self) -> int:
        """清空缓存"""
        count = len(self._store)
        self._store.clear()
        return count
    
    def size(self) -> int:
        """缓存大小"""
        return len(self._store)


# 全局缓存实例
_CACHE = TTLCache()


# =========================
# 权重归一化函数
# =========================

def normalize_weights(w: Dict[str, float]) -> Dict[str, float]:
    """
    权重归一化
    
    处理:
    1. 缺失键补 0
    2. 清理 NaN/inf/负数
    3. 归一化到总和=1
    """
    w = dict(w)  # 复制
    
    # 缺失键补 0
    for k in WEIGHT_KEYS:
        if k not in w or w[k] is None:
            w[k] = 0.0
    
    # 清理 NaN/inf/负数
    for k in WEIGHT_KEYS:
        x = float(w[k])
        if not math.isfinite(x) or x < 0:
            w[k] = 0.0
        else:
            w[k] = x
    
    s = sum(w[k] for k in WEIGHT_KEYS)
    if s <= 0:
        # 全 0 时，返回均匀分布
        u = 1.0 / len(WEIGHT_KEYS)
        return {k: u for k in WEIGHT_KEYS}
    
    # 归一化
    out = {k: (w[k] / s) for k in WEIGHT_KEYS}
    
    # 修正浮点误差：保证总和=1
    s2 = sum(out.values())
    diff = 1.0 - s2
    if abs(diff) > SUM_EPS:
        kmax = max(out, key=lambda k: out[k])
        out[kmax] = max(0.0, out[kmax] + diff)
    
    return out


def weights_sum_ok(w: Dict[str, float]) -> bool:
    """检查权重和是否接近 1"""
    s = sum(float(w.get(k, 0.0)) for k in WEIGHT_KEYS)
    return abs(1.0 - s) <= 1e-4


# =========================
# JSON schema 校验
# =========================

def validate_schema(obj: Dict[str, Any]) -> Tuple[bool, str]:
    """
    校验 JSON schema
    
    返回: (is_valid, error_message)
    """
    # 必备字段
    required = [
        "version", "symbol", "timestamp_utc", "regime_view",
        "risk_flags", "weights", "confidence", "reasoning_bullets", "fallback_used"
    ]
    for f in required:
        if f not in obj:
            return False, f"missing field: {f}"
    
    if not isinstance(obj["weights"], dict):
        return False, "weights must be dict"
    
    # weights keys
    for k in WEIGHT_KEYS:
        if k not in obj["weights"]:
            return False, f"weights missing key: {k}"
    
    # confidence
    try:
        c = float(obj["confidence"])
    except Exception:
        return False, "confidence not numeric"
    if not (0.0 <= c <= 1.0):
        return False, "confidence out of range"
    
    # reasoning_bullets
    if not isinstance(obj["reasoning_bullets"], list):
        return False, "reasoning_bullets must be list"
    if len(obj["reasoning_bullets"]) > 5:
        return False, "reasoning_bullets too long"
    
    return True, "ok"


# =========================
# 禁词扫描
# =========================

def contains_banned_text(raw_text: str) -> bool:
    """
    检查是否包含禁词
    
    防止 AI 越权输出方向/动作/阈值/杠杆
    """
    if not raw_text:
        return False
    return bool(BANNED_REGEX.search(raw_text))


# =========================
# Fallback 输出构建
# =========================

def build_fallback_output(
    symbol: str,
    timestamp_utc: str,
    regime_name: str,
    default_weights: Dict[str, Dict[str, float]],
    reason: str,
    risk_flags: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """
    构建 fallback 输出
    
    使用默认权重 + 低置信度
    """
    base = default_weights.get(regime_name) or default_weights.get("TREND") or {}
    w = normalize_weights(dict(base))
    
    return {
        "version": "weight-router-v1",
        "symbol": symbol,
        "timestamp_utc": timestamp_utc,
        "regime_view": {
            "name": regime_name,
            "trend_strength": 0.0,
            "notes": f"fallback: {reason}",
        },
        "risk_flags": risk_flags or {
            "trap": False,
            "phantom": False,
            "wide_spread": False,
            "data_stale": True,
        },
        "weights": w,
        "confidence": DEFAULT_CONFIDENCE_FALLBACK,
        "reasoning_bullets": [f"fallback: {reason}"],
        "fallback_used": True,
    }


# =========================
# 缓存键生成
# =========================

def make_cache_key(
    symbol: str,
    regime_name: str,
    trend_strength: float,
    trap_confirmed: bool,
    spread_z: float,
) -> str:
    """
    生成缓存键
    
    桶化减少抖动:
    - trend_strength: 保留 1 位小数
    - spread_z: 分桶
    - trap_confirmed: 布尔
    """
    strength_bucket = round(float(trend_strength), 1)
    
    # spread 分桶
    if spread_z >= 2.5:
        spread_bucket = 3
    elif spread_z >= 1.5:
        spread_bucket = 2
    elif spread_z >= 0.8:
        spread_bucket = 1
    else:
        spread_bucket = 0
    
    trap_bucket = 1 if trap_confirmed else 0
    
    return f"wmap:{symbol}:{regime_name}:ts{strength_bucket}:tb{trap_bucket}:sb{spread_bucket}"


# =========================
# 权重响应数据类
# =========================

@dataclass
class WeightResponse:
    """权重响应"""
    version: str = "weight-router-v1"
    symbol: str = ""
    timestamp_utc: str = ""
    regime_view: Dict[str, Any] = field(default_factory=dict)
    risk_flags: Dict[str, bool] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5
    reasoning_bullets: List[str] = field(default_factory=list)
    fallback_used: bool = False
    cache_key: str = ""
    raw_response: str = ""
    
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
            "cache_key": self.cache_key,
        }
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WeightResponse":
        return cls(
            version=d.get("version", "weight-router-v1"),
            symbol=d.get("symbol", ""),
            timestamp_utc=d.get("timestamp_utc", ""),
            regime_view=d.get("regime_view", {}),
            risk_flags=d.get("risk_flags", {}),
            weights=d.get("weights", {}),
            confidence=float(d.get("confidence", 0.5)),
            reasoning_bullets=d.get("reasoning_bullets", []),
            fallback_used=bool(d.get("fallback_used", False)),
            cache_key=d.get("cache_key", ""),
            raw_response=d.get("raw_response", ""),
        )


# =========================
# 主入口：权重路由器
# =========================

class WeightRouter:
    """
    权重路由器
    
    整合:
    1. 本地规则计算
    2. AI 调用（可选）
    3. 校验与降级
    4. 缓存管理 (内存 + MarketStorage 持久化)
    
    字段名与 MarketIngestionService 对齐:
    - cvd, cvd_momentum, oi_delta, funding, depth_ratio, imbalance, liquidity_delta, micro_delta
    """
    
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        storage: Optional["MarketStorage"] = None,
    ) -> None:
        self.config = config or {}
        self._storage = storage
        
        # 默认权重表 - 字段名与 MarketIngestionService 对齐
        self.default_weights = {
            "TREND": {
                "cvd": 0.24,
                "cvd_momentum": 0.14,
                "oi_delta": 0.22,
                "funding": 0.10,
                "depth_ratio": 0.15,
                "imbalance": 0.10,
                "liquidity_delta": 0.08,
                "micro_delta": 0.06,
            },
            "RANGE": {
                "cvd": 0.10,
                "cvd_momentum": 0.15,
                "oi_delta": 0.05,
                "funding": 0.05,
                "depth_ratio": 0.10,
                "imbalance": 0.35,
                "liquidity_delta": 0.12,
                "micro_delta": 0.18,
            },
            "NO_TRADE": {
                "cvd": 0.125,
                "cvd_momentum": 0.125,
                "oi_delta": 0.125,
                "funding": 0.125,
                "depth_ratio": 0.125,
                "imbalance": 0.125,
                "liquidity_delta": 0.125,
                "micro_delta": 0.125,
            },
        }
        
        # 从配置加载默认权重
        dw_cfg = self.config.get("default_weights", {})
        if dw_cfg:
            for regime in ["TREND", "RANGE"]:
                if regime in dw_cfg:
                    self.default_weights[regime] = normalize_weights(dw_cfg[regime])
        
        # 缓存配置
        self.cache_ttl = int(self.config.get("cache_ttl_seconds", TTL_SECONDS_DEFAULT))
        self._cache = _CACHE
        
        # 统计
        self._stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "db_cache_hits": 0,
            "fallbacks": 0,
            "validations_passed": 0,
            "validations_failed": 0,
            "db_saves": 0,
        }
    
    def set_storage(self, storage: Optional["MarketStorage"]) -> None:
        """设置 MarketStorage 实例"""
        self._storage = storage
    
    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default
    
    def get_weight_map(
        self,
        *,
        symbol: str,
        timestamp_utc: str,
        regime_name: str,
        trend_strength: float,
        stale_seconds: int,
        missing_fields: List[str],
        sample_ok: bool,
        trap_confirmed: bool,
        spread_z: float,
        ai_response: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> WeightResponse:
        """
        获取权重映射
        
        流程:
        1. 输入校验 → 必要时 fallback
        2. 缓存检查
        3. AI 响应处理（如果提供）
        4. 校验与归一化
        5. 缓存存储
        """
        self._stats["total_requests"] += 1
        regime_name = str(regime_name or "NO_TRADE").upper()
        trend_strength = self._to_float(trend_strength, 0.0)
        spread_z = self._to_float(spread_z, 0.0)
        stale_seconds = int(stale_seconds) if stale_seconds is not None else 0
        
        # 0) 输入校验：数据质量不行直接 fallback
        if (not sample_ok) or (stale_seconds > STALE_LIMIT_SECONDS) or (missing_fields and len(missing_fields) > 0):
            self._stats["fallbacks"] += 1
            return WeightResponse.from_dict(build_fallback_output(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                default_weights=self.default_weights,
                reason=f"bad_input(sample_ok={sample_ok}, stale={stale_seconds}, missing={missing_fields})",
                risk_flags={
                    "trap": bool(trap_confirmed),
                    "phantom": False,
                    "wide_spread": spread_z >= 2.5,
                    "data_stale": True,
                },
            ))
        
        # 1) NO_TRADE：直接 fallback
        if regime_name == "NO_TRADE":
            self._stats["fallbacks"] += 1
            return WeightResponse.from_dict(build_fallback_output(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                default_weights=self.default_weights,
                reason="regime=NO_TRADE",
                risk_flags={
                    "trap": bool(trap_confirmed),
                    "phantom": False,
                    "wide_spread": spread_z >= 2.5,
                    "data_stale": False,
                },
            ))
        
        # 2) 缓存命中 - 内存缓存
        cache_key = make_cache_key(symbol, regime_name, trend_strength, trap_confirmed, spread_z)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._stats["cache_hits"] += 1
            cached_response = WeightResponse.from_dict(cached)
            cached_response.cache_key = cache_key
            return cached_response
        
        # 2.1) 缓存命中 - 数据库缓存 (MarketStorage)
        if self._storage is not None:
            try:
                db_cached = self._storage.get_weight_router_cache(cache_key)
                if db_cached is not None:
                    self._stats["db_cache_hits"] += 1
                    # 写入内存缓存
                    self._cache.set(cache_key, db_cached, self.cache_ttl)
                    cached_response = WeightResponse.from_dict(db_cached)
                    cached_response.cache_key = cache_key
                    return cached_response
            except Exception:
                pass  # 忽略数据库错误
        
        # 3) 如果没有 AI 响应，使用本地规则计算
        if ai_response is None:
            return self._compute_local_weights(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                trend_strength=trend_strength,
                trap_confirmed=trap_confirmed,
                spread_z=spread_z,
                cache_key=cache_key,
                ttl_seconds=ttl_seconds,
            )
        
        # 4) 处理 AI 响应
        return self._process_ai_response(
            ai_response=ai_response,
            symbol=symbol,
            timestamp_utc=timestamp_utc,
            regime_name=regime_name,
            trap_confirmed=trap_confirmed,
            spread_z=spread_z,
            cache_key=cache_key,
            ttl_seconds=ttl_seconds,
        )
    
    def _compute_local_weights(
        self,
        symbol: str,
        timestamp_utc: str,
        regime_name: str,
        trend_strength: float,
        trap_confirmed: bool,
        spread_z: float,
        cache_key: str,
        ttl_seconds: Optional[int],
    ) -> WeightResponse:
        """本地规则计算权重"""
        base = self.default_weights.get(regime_name, self.default_weights["TREND"])
        weights = normalize_weights(dict(base))
        
        # 根据市场状态调整权重
        confidence = 0.5
        
        if regime_name == "TREND":
            # 趋势模式：强调 CVD/OI
            if trend_strength > 0.6:
                weights["cvd"] = min(0.30, weights["cvd"] + 0.05)
                weights["oi_delta"] = min(0.30, weights["oi_delta"] + 0.05)
                confidence += 0.1
        elif regime_name == "RANGE":
            # 区间模式：强调 imbalance/micro
            if trap_confirmed:
                weights["micro_delta"] = min(0.25, weights["micro_delta"] + 0.05)
                weights["liquidity_delta"] = min(0.18, weights["liquidity_delta"] + 0.03)
                confidence -= 0.1
        
        # spread 调整
        if spread_z >= 2.5:
            weights["depth_ratio"] = min(0.20, weights["depth_ratio"] + 0.05)
            confidence *= 0.8
        
        weights = normalize_weights(weights)
        confidence = max(0.1, min(0.9, confidence))
        
        response = WeightResponse(
            version="weight-router-v1",
            symbol=symbol,
            timestamp_utc=timestamp_utc,
            regime_view={
                "name": regime_name,
                "trend_strength": trend_strength,
                "notes": "local_rule_computed",
            },
            risk_flags={
                "trap": bool(trap_confirmed),
                "phantom": False,
                "wide_spread": spread_z >= 2.5,
                "data_stale": False,
            },
            weights=weights,
            confidence=confidence,
            reasoning_bullets=["local_rule"],
            fallback_used=False,
            cache_key=cache_key,
        )
        
        # 缓存 - 内存
        actual_ttl = ttl_seconds or self.cache_ttl
        self._cache.set(cache_key, response.to_dict(), actual_ttl)
        
        # 缓存 - 数据库 (MarketStorage)
        self._save_to_storage(response, actual_ttl)
        
        return response
    
    def _save_to_storage(self, response: WeightResponse, ttl_seconds: int) -> None:
        """保存权重快照到 MarketStorage"""
        if self._storage is None:
            return
        try:
            self._storage.save_weight_router_cache(
                cache_key=response.cache_key,
                symbol=response.symbol,
                regime=response.regime_view.get("name", "TREND"),
                timestamp=response.timestamp_utc,
                weights=response.weights,
                confidence=response.confidence,
                fallback_used=response.fallback_used,
                regime_view=response.regime_view,
                risk_flags=response.risk_flags,
                reasoning_bullets=response.reasoning_bullets,
                ttl_seconds=ttl_seconds,
            )
            self._stats["db_saves"] += 1
        except Exception:
            pass  # 忽略存储错误
    
    def _process_ai_response(
        self,
        ai_response: str,
        symbol: str,
        timestamp_utc: str,
        regime_name: str,
        trap_confirmed: bool,
        spread_z: float,
        cache_key: str,
        ttl_seconds: Optional[int],
    ) -> WeightResponse:
        """处理 AI 响应"""
        
        # 1) 禁词扫描
        if contains_banned_text(ai_response):
            self._stats["fallbacks"] += 1
            self._stats["validations_failed"] += 1
            return WeightResponse.from_dict(build_fallback_output(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                default_weights=self.default_weights,
                reason="banned_text_detected",
                risk_flags={
                    "trap": bool(trap_confirmed),
                    "phantom": False,
                    "wide_spread": spread_z >= 2.5,
                    "data_stale": False,
                },
            ))
        
        # 2) JSON 解析
        try:
            obj = json.loads(ai_response)
        except Exception:
            self._stats["fallbacks"] += 1
            self._stats["validations_failed"] += 1
            return WeightResponse.from_dict(build_fallback_output(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                default_weights=self.default_weights,
                reason="json_parse_fail",
            ))
        
        # 3) schema 校验
        ok, msg = validate_schema(obj)
        if not ok:
            self._stats["fallbacks"] += 1
            self._stats["validations_failed"] += 1
            return WeightResponse.from_dict(build_fallback_output(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                default_weights=self.default_weights,
                reason=f"schema_invalid:{msg}",
            ))
        
        # 4) 权重合法性 & 归一化
        try:
            w = {k: float(obj["weights"][k]) for k in WEIGHT_KEYS}
        except Exception:
            self._stats["fallbacks"] += 1
            self._stats["validations_failed"] += 1
            return WeightResponse.from_dict(build_fallback_output(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                default_weights=self.default_weights,
                reason="weights_not_numeric",
            ))
        
        w_norm = normalize_weights(w)
        obj["weights"] = w_norm
        
        # 5) confidence & 风险下压
        conf = float(obj.get("confidence", 0.5))
        if trap_confirmed:
            conf *= 0.75
            obj["risk_flags"]["trap"] = True
        if spread_z >= 2.5:
            conf *= 0.80
            obj["risk_flags"]["wide_spread"] = True
        
        obj["confidence"] = max(0.0, min(1.0, conf))
        
        # 6) 最终兜底：归一化后仍不合格
        if not weights_sum_ok(obj["weights"]):
            self._stats["fallbacks"] += 1
            return WeightResponse.from_dict(build_fallback_output(
                symbol=symbol,
                timestamp_utc=timestamp_utc,
                regime_name=regime_name,
                default_weights=self.default_weights,
                reason="weights_sum_invalid_after_norm",
            ))
        
        self._stats["validations_passed"] += 1
        
        # 7) 构建响应
        response = WeightResponse(
            version=obj.get("version", "weight-router-v1"),
            symbol=symbol,
            timestamp_utc=obj.get("timestamp_utc", timestamp_utc),
            regime_view=obj.get("regime_view", {}),
            risk_flags=obj.get("risk_flags", {}),
            weights=obj["weights"],
            confidence=float(obj.get("confidence", 0.5)),
            reasoning_bullets=obj.get("reasoning_bullets", []),
            fallback_used=bool(obj.get("fallback_used", False)),
            cache_key=cache_key,
            raw_response=ai_response,
        )
        
        # 8) 缓存
        actual_ttl = ttl_seconds or self.cache_ttl
        if regime_name == "RANGE":
            actual_ttl = min(actual_ttl, 8 * 60)
        elif regime_name == "TREND":
            actual_ttl = min(actual_ttl, 15 * 60)
        
        # 缓存 - 内存
        self._cache.set(cache_key, response.to_dict(), actual_ttl)
        
        # 缓存 - 数据库 (MarketStorage)
        self._save_to_storage(response, actual_ttl)
        
        return response
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        total = self._stats["total_requests"]
        return {
            **self._stats,
            "cache_hit_rate": self._stats["cache_hits"] / total if total > 0 else 0,
            "fallback_rate": self._stats["fallbacks"] / total if total > 0 else 0,
            "cache_size": self._cache.size(),
        }
    
    def clear_cache(self) -> int:
        """清空缓存"""
        return self._cache.clear()


# 导出
__all__ = [
    "TTLCache",
    "WeightRouter",
    "WeightResponse",
    "WEIGHT_KEYS",
    "normalize_weights",
    "weights_sum_ok",
    "validate_schema",
    "contains_banned_text",
    "build_fallback_output",
    "make_cache_key",
]
