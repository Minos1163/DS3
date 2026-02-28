"""
DeepSeek Weight Router - 动态权重调度层

资金流 3.0 架构核心组件:
- 输入: regime + zscores + micro flags
- 输出: factor weights + confidence
- 约束: 不输出方向，只输出权重和置信度

设计原则:
1. 可解释性: 每次决策记录权重快照
2. 可缓存: weight_map 按 symbol+regime 缓存，TTL 5~15m
3. 不越权: 不输出交易方向，只调整因子权重

V3.0 增强:
- 集成 DeepSeek AI 服务
- 支持本地规则回退
- 智能缓存策略
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
import math
import hashlib
import json
import logging

if TYPE_CHECKING:
    from src.fund_flow.ai_weight_service import DeepSeekAIService, AIWeightResponse

logger = logging.getLogger(__name__)


@dataclass
class WeightMap:
    """因子权重映射"""
    # 趋势模式权重
    trend_cvd_weight: float = 0.24
    trend_cvd_momentum_weight: float = 0.14
    trend_oi_delta_weight: float = 0.22
    trend_funding_weight: float = 0.10
    trend_depth_weight: float = 0.15
    trend_imbalance_weight: float = 0.15
    trend_liquidity_norm_weight: float = 0.12
    
    # 区间模式权重
    range_imbalance_weight: float = 0.55
    range_cvd_momentum_weight: float = 0.35
    range_depth_weight: float = 0.10
    
    # 置信度
    confidence: float = 0.5
    reason: str = "default"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "trend_cvd_weight": self.trend_cvd_weight,
            "trend_cvd_momentum_weight": self.trend_cvd_momentum_weight,
            "trend_oi_delta_weight": self.trend_oi_delta_weight,
            "trend_funding_weight": self.trend_funding_weight,
            "trend_depth_weight": self.trend_depth_weight,
            "trend_imbalance_weight": self.trend_imbalance_weight,
            "trend_liquidity_norm_weight": self.trend_liquidity_norm_weight,
            "range_imbalance_weight": self.range_imbalance_weight,
            "range_cvd_momentum_weight": self.range_cvd_momentum_weight,
            "range_depth_weight": self.range_depth_weight,
            "confidence": self.confidence,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class WeightCacheEntry:
    """权重缓存条目"""
    weight_map: WeightMap
    symbol: str
    regime: str
    created_at: datetime
    expires_at: datetime
    hit_count: int = 0
    
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


class DeepSeekWeightRouter:
    """
    DeepSeek 动态权重调度器
    
    核心功能:
    1. 根据 regime + 市场状态动态调整因子权重
    2. 缓存权重映射，避免频繁调用
    3. 记录权重决策原因，支持归因分析
    4. 支持 AI 调用 + 本地规则回退
    
    约束:
    - 不输出交易方向
    - 只调整因子权重和置信度
    
    V3.0 增强:
    - 集成 DeepSeek AI 服务
    - 智能缓存 (symbol+regime+trap_bucket)
    - 失败降级策略
    """
    
    # 权重调整边界
    MIN_WEIGHT = 0.05
    MAX_WEIGHT = 0.50
    MIN_CONFIDENCE = 0.1
    MAX_CONFIDENCE = 0.95
    
    # 默认缓存 TTL (秒)
    DEFAULT_CACHE_TTL = 300  # 5分钟
    MAX_CACHE_TTL = 900      # 15分钟
    
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        ds_cfg = self.config.get("deepseek_weight_router", {})
        
        # 是否启用
        self.enabled = bool(ds_cfg.get("enabled", False))
        
        # 缓存配置
        self.cache_ttl = min(
            self.MAX_CACHE_TTL,
            max(60, int(ds_cfg.get("cache_ttl_seconds", self.DEFAULT_CACHE_TTL)))
        )
        self.cache_max_entries = max(10, int(ds_cfg.get("cache_max_entries", 100)))
        
        # 权重调整参数
        self.volatility_adjustment_factor = max(
            0.0, 
            min(1.0, float(ds_cfg.get("volatility_adjustment_factor", 0.2)))
        )
        self.microstructure_boost_factor = max(
            0.0,
            min(0.5, float(ds_cfg.get("microstructure_boost_factor", 0.1)))
        )
        
        # 资金流权重调整
        self.flow_trend_weight_boost = max(
            0.0,
            min(0.3, float(ds_cfg.get("flow_trend_weight_boost", 0.1)))
        )
        
        # AI 服务配置
        self.ai_enabled = bool(ds_cfg.get("ai_enabled", False))
        self._ai_service: Optional["DeepSeekAIService"] = None
        
        # 缓存存储
        self._cache: Dict[str, WeightCacheEntry] = {}
        
        # 统计
        self._stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_evictions": 0,
            "ai_calls": 0,
            "ai_successes": 0,
            "fallbacks": 0,
        }
    
    def _get_ai_service(self) -> Optional["DeepSeekAIService"]:
        """延迟初始化 AI 服务"""
        if not self.ai_enabled:
            return None
        if self._ai_service is None:
            try:
                from src.fund_flow.ai_weight_service import DeepSeekAIService
                self._ai_service = DeepSeekAIService(self.config)
            except Exception as e:
                logger.warning(f"Failed to initialize AI service: {e}")
                self._ai_service = None
        return self._ai_service
    
    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default
    
    def _cache_key(self, symbol: str, regime: str, context_hash: str) -> str:
        """生成缓存键"""
        return f"{symbol.upper()}:{regime.upper()}:{context_hash[:8]}"
    
    def _context_hash(self, context: Dict[str, Any]) -> str:
        """计算上下文哈希（用于缓存键）"""
        # 只使用关键指标计算哈希，避免过于敏感
        key_fields = [
            "regime", "direction_lock", "adx_bucket",
            "flow_trend_strength", "micro_trap_active"
        ]
        hash_input = {k: context.get(k) for k in key_fields if context.get(k) is not None}
        hash_str = json.dumps(hash_input, sort_keys=True, default=str)
        return hashlib.md5(hash_str.encode()).hexdigest()
    
    def _evict_expired_cache(self) -> int:
        """清理过期缓存"""
        expired_keys = [k for k, v in self._cache.items() if v.is_expired()]
        for k in expired_keys:
            del self._cache[k]
        if expired_keys:
            self._stats["cache_evictions"] += len(expired_keys)
        return len(expired_keys)
    
    def _evict_lru_if_needed(self) -> None:
        """LRU 淘汰"""
        if len(self._cache) < self.cache_max_entries:
            return
        # 按 hit_count 排序，淘汰最少使用的
        sorted_items = sorted(
            self._cache.items(),
            key=lambda x: (x[1].hit_count, x[1].created_at)
        )
        evict_count = len(self._cache) - self.cache_max_entries + 1
        for k, _ in sorted_items[:evict_count]:
            del self._cache[k]
            self._stats["cache_evictions"] += 1
    
    def _get_cached(self, cache_key: str) -> Optional[WeightMap]:
        """从缓存获取权重"""
        entry = self._cache.get(cache_key)
        if entry is None or entry.is_expired():
            return None
        entry.hit_count += 1
        self._stats["cache_hits"] += 1
        return entry.weight_map
    
    def _set_cache(
        self,
        cache_key: str,
        symbol: str,
        regime: str,
        weight_map: WeightMap,
    ) -> None:
        """设置缓存"""
        self._evict_expired_cache()
        self._evict_lru_if_needed()
        
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.cache_ttl)
        
        self._cache[cache_key] = WeightCacheEntry(
            weight_map=weight_map,
            symbol=symbol,
            regime=regime,
            created_at=now,
            expires_at=expires_at,
        )
    
    def _classify_adx_bucket(self, adx: float) -> str:
        """ADX 分类桶"""
        if adx >= 30:
            return "strong_trend"
        elif adx >= 21:
            return "trend"
        elif adx >= 18:
            return "weak_range"
        else:
            return "range"
    
    def _classify_flow_trend(self, flow_metrics: Dict[str, Any]) -> str:
        """资金趋势分类"""
        cvd = self._to_float(flow_metrics.get("cvd_ratio"), 0.0)
        oi_delta = self._to_float(flow_metrics.get("oi_delta_ratio"), 0.0)
        
        cvd_strength = abs(cvd)
        oi_strength = abs(oi_delta)
        
        if cvd_strength > 0.1 and oi_strength > 0.05:
            if cvd > 0 and oi_delta > 0:
                return "strong_long_flow"
            elif cvd < 0 and oi_delta < 0:
                return "strong_short_flow"
        elif cvd_strength > 0.05:
            if cvd > 0:
                return "moderate_long_flow"
            else:
                return "moderate_short_flow"
        return "neutral_flow"
    
    def _compute_trend_weights(
        self,
        base: WeightMap,
        context: Dict[str, Any],
    ) -> Tuple[Dict[str, float], float, str]:
        """计算趋势模式权重"""
        adjustments: List[str] = []
        
        flow_trend = context.get("flow_trend", "neutral_flow")
        adx_bucket = context.get("adx_bucket", "trend")
        volatility_z = self._to_float(context.get("volatility_z"), 0.0)
        micro_trap_active = bool(context.get("micro_trap_active", False))
        
        weights = {
            "cvd_weight": base.trend_cvd_weight,
            "cvd_momentum_weight": base.trend_cvd_momentum_weight,
            "oi_delta_weight": base.trend_oi_delta_weight,
            "funding_weight": base.trend_funding_weight,
            "depth_weight": base.trend_depth_weight,
            "imbalance_weight": base.trend_imbalance_weight,
            "liquidity_norm_weight": base.trend_liquidity_norm_weight,
        }
        
        # 资金流方向强化
        if flow_trend in ("strong_long_flow", "strong_short_flow"):
            weights["cvd_weight"] += self.flow_trend_weight_boost
            weights["oi_delta_weight"] += self.flow_trend_weight_boost * 0.5
            adjustments.append(f"flow_boost({flow_trend})")
        
        # 强趋势时降低资金费率权重
        if adx_bucket == "strong_trend":
            weights["funding_weight"] *= 0.5
            adjustments.append("strong_trend_funding_reduce")
        
        # 高波动时降低深度权重
        if abs(volatility_z) > 2.0:
            weights["depth_weight"] *= 0.7
            adjustments.append(f"high_vol_depth_reduce(z={volatility_z:.1f})")
        
        # 微结构陷阱激活时，增加流动性权重
        if micro_trap_active:
            weights["liquidity_norm_weight"] += self.microstructure_boost_factor
            adjustments.append("trap_active_liq_boost")
        
        # 归一化权重
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}
        
        # 计算置信度
        confidence = 0.7  # 基础置信度
        if flow_trend in ("strong_long_flow", "strong_short_flow"):
            confidence += 0.15
        if adx_bucket == "strong_trend":
            confidence += 0.1
        if micro_trap_active:
            confidence -= 0.15
        
        confidence = max(self.MIN_CONFIDENCE, min(self.MAX_CONFIDENCE, confidence))
        
        reason = "trend_weights_computed"
        if adjustments:
            reason += ":" + ",".join(adjustments)
        
        return weights, confidence, reason
    
    def _compute_range_weights(
        self,
        base: WeightMap,
        context: Dict[str, Any],
    ) -> Tuple[Dict[str, float], float, str]:
        """计算区间模式权重"""
        adjustments: List[str] = []
        
        extreme_confirmed = bool(context.get("extreme_confirmed", False))
        turn_confirmed = bool(context.get("turn_confirmed", False))
        trap_decay = bool(context.get("trap_decay", False))
        phantom_decay = bool(context.get("phantom_decay", False))
        spread_z = self._to_float(context.get("spread_z"), 0.0)
        
        weights = {
            "imbalance_weight": base.range_imbalance_weight,
            "cvd_momentum_weight": base.range_cvd_momentum_weight,
            "depth_weight": base.range_depth_weight,
        }
        
        # 极端确认时增加不平衡权重
        if extreme_confirmed:
            weights["imbalance_weight"] += 0.1
            adjustments.append("extreme_confirmed")
        
        # 拐头确认时增加 CVD 动量权重
        if turn_confirmed:
            weights["cvd_momentum_weight"] += 0.1
            adjustments.append("turn_confirmed")
        
        # 微结构拐头确认
        micro_turn_count = sum([trap_decay, phantom_decay])
        if micro_turn_count >= 1:
            weights["imbalance_weight"] += 0.05 * micro_turn_count
            adjustments.append(f"micro_turns({micro_turn_count})")
        
        # 高 spread 时降低深度权重
        if abs(spread_z) > 2.0:
            weights["depth_weight"] *= 0.6
            adjustments.append(f"high_spread_reduce(z={spread_z:.1f})")
        
        # 归一化权重
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}
        
        # 计算置信度
        confidence = 0.5  # 区间模式基础置信度较低
        if extreme_confirmed:
            confidence += 0.15
        if turn_confirmed:
            confidence += 0.2
        if micro_turn_count >= 2:
            confidence += 0.1
        
        confidence = max(self.MIN_CONFIDENCE, min(self.MAX_CONFIDENCE, confidence))
        
        reason = "range_weights_computed"
        if adjustments:
            reason += ":" + ",".join(adjustments)
        
        return weights, confidence, reason
    
    def get_weights(
        self,
        symbol: str,
        regime: str,
        market_flow_context: Dict[str, Any],
        quantile_context: Optional[Dict[str, Any]] = None,
        use_ai: bool = True,
    ) -> WeightMap:
        """
        获取动态权重
        
        Args:
            symbol: 交易标的
            regime: 市场状态 (TREND/RANGE/NO_TRADE)
            market_flow_context: 市场资金流上下文
            quantile_context: 分位数上下文（可选）
            use_ai: 是否允许调用 AI（False 时仅使用本地规则/缓存）
        
        Returns:
            WeightMap: 因子权重映射
        """
        self._stats["total_requests"] += 1
        
        # 未启用时返回默认权重
        if not self.enabled:
            return WeightMap(reason="disabled")
        
        regime = str(regime or "NO_TRADE").upper()
        if regime == "NO_TRADE":
            return WeightMap(reason="no_trade_regime")
        
        # 构建上下文
        timeframes = market_flow_context.get("timeframes", {})
        tf_15m = timeframes.get("15m", {}) if isinstance(timeframes, dict) else {}
        tf_5m = timeframes.get("5m", {}) if isinstance(timeframes, dict) else {}
        
        context: Dict[str, Any] = {
            "regime": regime,
            "direction_lock": market_flow_context.get("direction_lock", "BOTH"),
            "adx_bucket": self._classify_adx_bucket(self._to_float(tf_15m.get("adx"), 0.0)),
            "flow_trend": self._classify_flow_trend(market_flow_context),
            "flow_trend_strength": abs(self._to_float(market_flow_context.get("cvd_ratio"), 0.0)),
            "volatility_z": self._to_float(tf_5m.get("volatility_z"), 0.0),
            "spread_z": self._to_float(tf_5m.get("spread_z"), 0.0),
            "micro_trap_active": False,
            "extreme_confirmed": False,
            "turn_confirmed": False,
            "trap_decay": False,
            "phantom_decay": False,
        }
        
        # 从 quantile_context 提取微结构状态
        if quantile_context:
            trap_last = self._to_float(quantile_context.get("trap_last"), 0.0)
            trap_guard = self._to_float(quantile_context.get("trap_guard"), 0.7)
            context["micro_trap_active"] = trap_last > trap_guard
            
            # 拐头确认
            context["turn_confirmed"] = bool(quantile_context.get("turn_confirmed", False))
            context["extreme_confirmed"] = bool(quantile_context.get("extreme_confirmed", False))
            context["trap_decay"] = bool(quantile_context.get("trap_decay", False))
            context["phantom_decay"] = bool(quantile_context.get("phantom_decay", False))
        
        # 检查缓存 - 使用新的缓存键策略
        cache_key = self._smart_cache_key(symbol, regime, context)
        
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        
        self._stats["cache_misses"] += 1
        
        # 尝试调用 AI 服务
        if self.ai_enabled and bool(use_ai):
            weight_map = self._try_ai_weights(symbol, regime, market_flow_context, quantile_context)
            if weight_map is not None:
                self._set_cache(cache_key, symbol, regime, weight_map)
                return weight_map
        
        # 本地规则计算权重
        base = WeightMap()
        
        if regime == "TREND":
            weights, confidence, reason = self._compute_trend_weights(base, context)
            weight_map = WeightMap(
                trend_cvd_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("cvd_weight", 0.24))),
                trend_cvd_momentum_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("cvd_momentum_weight", 0.14))),
                trend_oi_delta_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("oi_delta_weight", 0.22))),
                trend_funding_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("funding_weight", 0.10))),
                trend_depth_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("depth_weight", 0.15))),
                trend_imbalance_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("imbalance_weight", 0.15))),
                trend_liquidity_norm_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("liquidity_norm_weight", 0.12))),
                confidence=confidence,
                reason=reason,
            )
        else:  # RANGE
            weights, confidence, reason = self._compute_range_weights(base, context)
            weight_map = WeightMap(
                range_imbalance_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("imbalance_weight", 0.55))),
                range_cvd_momentum_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("cvd_momentum_weight", 0.35))),
                range_depth_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("depth_weight", 0.10))),
                confidence=confidence,
                reason=reason,
            )
        
        # 缓存结果
        self._set_cache(cache_key, symbol, regime, weight_map)
        
        return weight_map
    
    def _smart_cache_key(self, symbol: str, regime: str, context: Dict[str, Any]) -> str:
        """
        智能缓存键策略
        
        缓存 key：(symbol, regime_name, rounded(trend_strength,1), trap_bucket)
        
        强制刷新条件:
        - regime 切换
        - trap_confirmed 从 false→true
        - spread_z 超过阈值（例如 >2）
        """
        symbol_up = symbol.upper()
        regime_up = regime.upper()
        
        trend_strength = round(self._to_float(context.get("flow_trend_strength"), 0.0), 1)
        trap_active = bool(context.get("micro_trap_active", False))
        spread_z = self._to_float(context.get("spread_z"), 0.0)
        
        # trap_bucket: 0-0.3 low, 0.3-0.6 mid, 0.6-1.0 high
        if trap_active:
            trap_bucket = "high"
        elif spread_z > 2.0:
            trap_bucket = "spread"
        else:
            trap_bucket = "normal"
        
        key_str = f"{symbol_up}:{regime_up}:{trend_strength}:{trap_bucket}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def _try_ai_weights(
        self,
        symbol: str,
        regime: str,
        market_flow_context: Dict[str, Any],
        quantile_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[WeightMap]:
        """尝试使用 AI 获取权重"""
        ai_service = self._get_ai_service()
        if ai_service is None:
            return None
        
        try:
            self._stats["ai_calls"] += 1
            ai_response = ai_service.get_weights(
                symbol=symbol,
                regime=regime,
                market_flow_context=market_flow_context,
                quantile_context=quantile_context,
            )
            
            if ai_response.fallback_used:
                self._stats["fallbacks"] += 1
                return None
            
            # 转换 AI 响应为 WeightMap
            weights = ai_response.weights
            
            if regime == "TREND":
                weight_map = WeightMap(
                    trend_cvd_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("cvd", 0.24))),
                    trend_cvd_momentum_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("cvd_momentum", 0.14))),
                    trend_oi_delta_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("oi_delta", 0.22))),
                    trend_funding_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("funding", 0.10))),
                    trend_depth_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("depth_ratio", 0.15))),
                    trend_imbalance_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("imbalance", 0.10))),
                    trend_liquidity_norm_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("liquidity_delta", 0.08))),
                    confidence=ai_response.confidence,
                    reason=f"ai:{';'.join(ai_response.reasoning_bullets[:3])}",
                )
            else:  # RANGE
                weight_map = WeightMap(
                    range_imbalance_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("imbalance", 0.35))),
                    range_cvd_momentum_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("cvd_momentum", 0.15))),
                    range_depth_weight=max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weights.get("depth_ratio", 0.10))),
                    confidence=ai_response.confidence,
                    reason=f"ai:{';'.join(ai_response.reasoning_bullets[:3])}",
                )
            
            self._stats["ai_successes"] += 1
            return weight_map
            
        except Exception as e:
            logger.warning(f"AI weight call failed: {e}")
            self._stats["fallbacks"] += 1
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        total = self._stats["total_requests"]
        hits = self._stats["cache_hits"]
        hit_rate = hits / total if total > 0 else 0.0
        
        return {
            **self._stats,
            "cache_hit_rate": hit_rate,
            "cache_size": len(self._cache),
        }
    
    def clear_cache(self) -> int:
        """清空缓存"""
        count = len(self._cache)
        self._cache.clear()
        return count


# 导出
__all__ = [
    "WeightMap",
    "WeightCacheEntry",
    "DeepSeekWeightRouter",
]
