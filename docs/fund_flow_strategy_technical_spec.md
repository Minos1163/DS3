# FUND_FLOW交易策略技术说明（供专家组评审）

## 1. 文档范围与目标
- 文档目标：系统化说明当前脚本中的 `FUND_FLOW` 实盘策略实现，覆盖数据链路、信号链路、执行链路、风控链路与落库结构。
- 代码范围：
  - 入口：`src/main.py`
  - 主运行时：`src/app/fund_flow_bot.py`
  - 策略模块：`src/fund_flow/*.py`
  - 市场/账户/持仓数据管理：`src/data/*.py`
  - 交易网关：`src/api/binance_client.py`、`src/api/market_gateway.py`、`src/trading/order_gateway.py`
  - 配置：`config/trading_config_fund_flow.json`

## 2. 总体执行流程（端到端）
1. 进程启动：`src/main.py` 调用 `TradingBot.main()`。
2. 加载配置与环境变量，初始化日志、客户端、数据管理器、资金流模块。
3. 周期循环（`run()`）：
  - 可按 `decision_timeframe` 对齐K线收线后触发。
  - 每轮进入 `run_cycle()`。
4. 每个交易对在一轮中的处理顺序：
  - 拉取实时行情与盘口。
  - 计算资金流上下文。
  - 15秒聚合+多周期聚合。
  - 触发去重与 `signal_pool` 过滤。
  - 决策引擎输出 `BUY/SELL/HOLD/CLOSE`。
  - 通过执行路由下单（含降级路径、保护单处理）。
  - 写归因日志、执行日志、成交CSV日志。
5. 轮次末尾：
  - 处理候选新开仓（按分数排序+活跃仓位上限）。
  - 清理无持仓残留保护单。

## 3. 数据源与采集方式

### 3.1 交易所接口来源
- 市场数据（`src/api/market_gateway.py`）：
  - K线：`GET /fapi/v1/klines`
  - 24h行情：`GET /fapi/v1/ticker/24hr`
  - 资金费率：`GET /fapi/v1/fundingRate`
  - 持仓量：`GET /fapi/v1/openInterest`
  - 深度：`GET /fapi/v1/depth`
  - 交易规则：`GET /fapi/v1/exchangeInfo`
- 账户与持仓（`src/api/binance_client.py`）：
  - 统一账户（PAPI）：`GET /papi/v1/account`
  - 经典合约账户（FAPI）：`GET /fapi/v2/account`
  - 持仓风险（PAPI/FAPI）：`/papi/v1/um/positionRisk` 或 `/fapi/v2/positionRisk`
  - 开放订单：`/papi/v1/um/openOrders` 或 `/fapi/v1/openOrders`

### 3.2 模式与路由
- 客户端在启动时检测 `ApiCapability` 和 `AccountMode`，决定使用 PAPI 或 FAPI。
- `um_base()` 根据账户能力动态选择基地址，策略代码不直接关心端点差异。

### 3.3 轮询采样方式
- 主采样在 `run_cycle()` 内按交易对逐个进行。
- 调度参数（当前配置）：
  - `schedule.interval_seconds = 60`
  - `schedule.align_to_kline_close = true`
  - `fund_flow.decision_timeframe = "3m"`
  - `schedule.kline_close_delay_seconds = 3`
- 含义：默认每60秒轮询，但已启用3分钟K线收线对齐，实际触发点在每个3m收线后+3秒。

### 3.4 采集失败降级
- 账户摘要请求带重试（3次指数退避）和TTL缓存回退（默认60秒）。
- DB写入失败会触发 `fund_flow_storage` 降级为 `None`（不中断交易主流程）。
- 部分日志写入失败仅告警，不中断交易循环。

## 4. 数据聚合

### 4.1 原始字段构建
在 `TradingBot._build_fund_flow_context()` 中将采集值映射为策略输入：
- `change_15m`、`change_24h`、`funding_rate`、`open_interest`
- 盘口深度派生：`depth_ratio`、`imbalance`
- 盘口资金净差：`ob_delta_notional`、`ob_total_notional`
- 开仓量变化：`oi_delta_ratio = (oi_t - oi_{t-1}) / |oi_{t-1}|`

### 4.2 15秒桶聚合与多周期聚合
`MarketIngestionService`：
- 先将当前样本按 `window_seconds`（默认15s）对齐到时间桶。
- 每个symbol维护历史队列（默认最长4小时）。
- 对 `1m/3m/5m/15m/30m/1h/2h/4h` 逐窗聚合，得到 `timeframes` 字段。
- 聚合规则：
  - `cvd_ratio`: 窗口求和
  - `cvd_momentum`: 窗口末值-首值
  - `oi_delta_ratio/depth_ratio/imbalance/liquidity_delta_norm`: 均值
  - `funding_rate`: 窗口末值
  - `signal_strength`: 使用同一套权重函数计算

### 4.3 决策周期上下文切换
- `decision_timeframe` 配置存在且对应聚合窗口可用时，使用该周期覆盖原始指标。
- 不存在时回退为 `raw`（未覆盖原始上下文）。

## 5. 资金流指标计算（核心公式）

### 5.1 盘口指标
- `depth_ratio = bid_notional / ask_notional`
- `imbalance = (bid_notional - ask_notional) / (bid_notional + ask_notional)`

### 5.2 CVD代理
- `cvd_ratio = change_15m`（15分钟涨跌幅作为CVD代理）
- `cvd_momentum = change_15m - change_24h / 96`

### 5.3 流动性归一化指标
`liquidity_delta_norm` 在 `_compute_liquidity_delta_norm` 中计算：
- `delta_notional = bid_notional - ask_notional`
- `base_sample = |total_notional|`（无total时取 `|delta_notional|`）
- `ema_t = alpha * base_sample + (1-alpha) * ema_{t-1}`
- `norm = delta_notional / max(min_base, ema_t)`
- 再按 `[-clip, +clip]` 裁剪。

当前相关参数（配置）：
- `liquidity_norm_alpha = 0.2`
- `liquidity_norm_clip = 1.0`
- `liquidity_norm_min_base = 1000`
- `liquidity_norm_factor_weight = 0.12`（进入决策打分）

### 5.4 信号强度 `signal_strength`
在 `MarketIngestionService._calc_signal_strength` 中：
- 由各指标绝对值加权求和后截断到 `[0,1]`。
- 用于标记本轮是 `signal` 触发还是 `scheduled` 触发（严格来说当前实现中，只要非全零，通常为 `signal`）。

## 6. 信号检测与触发机制

### 6.1 触发去重
`TriggerEngine.should_trigger()`：
- 维度：`symbol + trigger_type`
- 去重窗口：`trigger_dedupe_seconds`（当前=10）
- 同 `trigger_id` 重复直接拦截。

### 6.2 signal_pool 过滤
`TriggerEngine.evaluate_signal_pool()` 只对开仓类操作（BUY/SELL）生效：
- 先做分数门槛：
  - 多头看 `min_long_score`（当前0.30）
  - 空头看 `min_short_score`（当前0.27）
- 再做规则门槛（来自 `rules`）：
  - 支持 `AND/OR/min_pass_count`
  - 支持比较符 `>, >=, <, <=, ==, !=, between`
  - 支持按 `timeframe` 取值
- 边沿触发：
  - `edge_trigger_enabled` 开启时，仅上升沿触发通过
  - 可配置 `edge_cooldown_seconds`

### 6.3 当前规则配置（default_pool）
- LONG:
  - `cvd_momentum >= 0.0005`（3m）
  - `imbalance >= 0.08`（3m）
- SHORT:
  - `cvd_momentum <= -0.0005`（3m）
  - `imbalance <= -0.08`（3m）

## 7. 开仓逻辑

### 7.1 决策引擎打分
`FundFlowDecisionEngine._score()`：
- `long_score` 与 `short_score` 分别由：
  - `cvd_ratio/cvd_momentum/oi_delta/funding/depth/imbalance/liquidity_norm`
  - 使用固定权重线性加和并夹紧到 `[0,1]`

### 7.2 开仓触发条件
- 多头开仓：`long_score >= long_open_threshold` 且 `long_score > short_score`
- 空头开仓：`short_score >= short_open_threshold` 且 `short_score > long_score`
- 当前阈值：
  - `long_open_threshold = 0.30`
  - `short_open_threshold = 0.27`

### 7.3 仓位与杠杆
- 决策默认仓位：`default_target_portion = 0.15`
- 杠杆动态：按信号强度在线性区间 `[min_leverage,max_leverage]` 映射（当前2~3）
- 执行前会强制同步交易所该symbol杠杆；`strict_leverage_sync=true` 时同步失败直接拒绝开仓。

### 7.4 持仓状态下的开仓行为
- 已有反向仓：不允许同周期反手，直接跳过。
- 已有同向仓：走加仓逻辑。
- DCA开关开启时：
  - 仅当回撤达到阶段阈值才触发加仓。
  - 当前阈值：`[0.006, 0.012]`
  - 当前倍数：`[0.8, 1.4]`
  - 最大加仓阶段：2

### 7.5 新开仓队列排序与名额
- 无持仓开仓信号先入候选队列，按信号分数降序执行。
- 限制：`max_active_symbols = 3`。

### 7.6 下单执行降级链（开仓）
执行路由 `FundFlowExecutionRouter`：
1. LIMIT IOC 初次下单
2. 若流动性不足：IOC价格滑移重试（`open_ioc_retry_times=2`, step=10bps）
3. 若仍不足且开启：GTC回退（当前 `open_gtc_fallback_enabled=false`）
4. 若仍不足且开启：MARKET回退（当前 `open_market_fallback_enabled=true`）

### 7.7 开仓后保护单
- 入场成交后立即下TP/SL。
- 若保护单不完整：
  - 返回错误 `保护单下发失败，已阻止裸仓`
  - 默认触发回滚平仓（`rollback_on_tp_sl_fail=true`）

## 8. 风控机制

### 8.1 决策前风控（RiskEngine）
- symbol白名单校验
- operation合法性校验
- 杠杆夹紧到 `[min_leverage,max_leverage]`
- 开仓仓位比例范围校验：`[min_open_portion,max_open_portion]`
- 价格偏离钳制：`price_deviation_limit_percent`（当前1%）

### 8.2 账户级熔断
`_refresh_account_risk_guard()`：
- 日内亏损阈值（当前 `max_daily_loss_percent=5`）
- 连续亏损阈值（当前 `max_consecutive_losses=2`）
- 触发后进入冷却期：
  - 日亏损冷却：28800秒
  - 连亏冷却：1800秒
- 冷却期间阻止新开仓。

### 8.3 持仓暴露限制
- `max_symbol_position_portion = 0.25`
- 超上限禁止继续加仓。
- 普通加仓额度受 `add_position_portion = 0.1` 限制。

### 8.4 保护单SLA
- 检测持仓是否同时具备TP+SL。
- 缺失时先尝试补挂。
- 若补挂失败可立即强平（当前开启 `protection_immediate_close_on_repair_fail=true`）。
- 若超时仍缺保护，触发SLA告警并可强平（当前开启 `protection_sla_force_flatten=true`）。
- 超时阈值：30秒，告警冷却15秒。

### 8.5 无持仓残留保护单清理
- 每轮尾部执行。
- 如果symbol无持仓且无待成交开仓单，会撤销残留条件单。

## 9. 平仓逻辑

### 9.1 触发条件（决策层）
- 已有LONG且 `short_score >= close_threshold` -> `CLOSE`
- 已有SHORT且 `long_score >= close_threshold` -> `CLOSE`
- 当前 `close_threshold = 0.25`

### 9.2 执行路径（路由层）
1. 按 `target_portion_of_balance` 计算平仓数量
2. LIMIT IOC 重试平仓（当前4次，步进10bps）
3. 失败后可GTC reduce-only回退（当前开启）
4. 若仍错误且配置开启，MARKET reduce-only强制退出（当前开启）
5. 若是全平且成交，尝试 `cancel_all_open_orders` 清理挂单

### 9.3 重复平仓抑制
- 若检测到待成交平仓单，跳过重复平仓下发。

## 10. 数据库核心表结构（SQLite）
数据库文件：`logs/fund_flow/fund_flow_strategy.db`

### 10.1 行情与指标表
- `crypto_klines`
  - 主键：`(exchange, symbol, market, period, timestamp, environment)`
  - 字段：OHLCV
- `market_trades_aggregated`
  - 主键：`(exchange, symbol, timestamp)`
  - 字段：`cvd_ratio, cvd_momentum, signal_strength`
- `market_orderbook_snapshots`
  - 主键：`(exchange, symbol, timestamp)`
  - 字段：`depth_ratio, imbalance`
- `market_asset_metrics`
  - 主键：`(exchange, symbol, timestamp)`
  - 字段：`oi_delta_ratio, funding_rate`
- `market_flow_timeframes`
  - 主键：`(exchange, symbol, timeframe, timestamp)`
  - 字段：多周期聚合后的 `cvd/depth/imbalance/liquidity/signal_strength/sample_count/window_seconds`
- `market_sentiment_metrics`
  - 当前预留，主键：`(exchange, symbol, timestamp)`

### 10.2 决策与执行日志表
- `ai_decision_logs`
  - 自增主键 `id`
  - 关键信息：`operation, decision_json, trigger_type, trigger_id, order_id, tp_order_id, sl_order_id`
- `program_execution_logs`
  - 自增主键 `id`
  - 关键信息：`decision_json, market_context_json, params_snapshot_json, order_id, environment`

### 10.3 信号注册表
- `signal_definitions`
  - 信号规则明细（metric/operator/threshold/timeframe）
- `signal_pools`
  - 规则组配置（logic/min_pass_count/min_scores/edge配置/symbol范围/signal_ids）

### 10.4 同步机制
- 启动时将配置中的 `signal_definitions/signal_pools` 全量写入DB（`source='config'`）。
- 运行中通过 `version(updated_at max)` 做热更新检查，检测到变更后刷新 `TriggerEngine` 运行时池配置。

## 11. 关键文件索引
- `src/main.py`
  - 运行入口。
- `src/app/fund_flow_bot.py`
  - 主循环与编排层（采集、聚合、触发、执行、风控、日志、热更新）。
- `src/fund_flow/decision_engine.py`
  - 资金流评分与 `BUY/SELL/HOLD/CLOSE` 决策。
- `src/fund_flow/trigger_engine.py`
  - 触发去重 + signal_pool规则引擎 + 边沿触发。
- `src/fund_flow/risk_engine.py`
  - 决策执行前的统一风控校验。
- `src/fund_flow/execution_router.py`
  - 订单执行与退化路径（IOC/GTC/MARKET）、TP/SL完整性约束、平仓兜底。
- `src/fund_flow/market_ingestion.py`
  - 15s与多周期聚合。
- `src/fund_flow/market_storage.py`
  - SQLite建表与写入逻辑、signal registry 持久化。
- `src/fund_flow/attribution_engine.py`
  - 决策/执行 JSONL 归因日志。
- `src/data/market_data.py`
  - 实时行情/资金费率/持仓量/K线采集。
- `src/data/account_data.py`
  - 账户摘要提取、重试、缓存回退。
- `src/data/position_data.py`
  - 当前持仓与全部持仓解析。
- `src/api/market_gateway.py`
  - 市场类REST端点封装。
- `src/api/binance_client.py`
  - PAPI/FAPI 模式适配、账户/持仓/订单统一入口。
- `src/trading/order_gateway.py`
  - 标准下单、开仓防重、最小名义额处理、openOrders查询。
- `config/trading_config_fund_flow.json`
  - 当前线上策略参数快照。

## 12. 当前参数快照（用于会议）
- 风控：
  - `max_daily_loss_percent=5`
  - `max_consecutive_losses=2`
- 仓位：
  - `default_target_portion=0.15`
  - `add_position_portion=0.1`
  - `max_symbol_position_portion=0.25`
  - `max_active_symbols=3`
- 阈值：
  - `long_open_threshold=0.30`
  - `short_open_threshold=0.27`
  - `close_threshold=0.25`
- 执行退化：
  - `open_ioc_retry_times=2`
  - `open_gtc_fallback_enabled=false`
  - `open_market_fallback_enabled=true`
  - `close_ioc_retry_times=4`
  - `close_gtc_fallback_enabled=true`
  - `close_market_fallback_enabled=true`
- 保护单：
  - `rollback_on_tp_sl_fail=true`
  - `protection_sla_enabled=true`
  - `protection_sla_seconds=30`
  - `protection_sla_force_flatten=true`

## 13. 建议专家组重点审阅议题
1. `cvd_ratio/cvd_momentum` 当前用价格变化近似，是否需要替换为真实逐笔成交驱动的CVD。
2. `signal_strength > 0` 即定义为 `signal` 触发，这会弱化 `scheduled` 分支意义，是否需重定义触发判据。
3. `close_threshold=0.25` 与开仓阈值接近，平仓敏感性与交易频率平衡是否符合目标风格。
4. DCA倍数/阈值与单币上限共同作用下的尾部风险是否充分。
5. 保护单补挂与强平策略在极端行情下的滑点与执行确定性评估。
