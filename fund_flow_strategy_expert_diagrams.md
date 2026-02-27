# FUND_FLOW 专家评审流程图版（Mermaid）

> 对应技术说明：`docs/fund_flow_strategy_technical_spec.md`

## 1. 端到端时序图（单轮 run_cycle）

```mermaid
sequenceDiagram
    autonumber
    participant SCH as 调度器(run)
    participant BOT as TradingBot.run_cycle
    participant ACC as AccountDataManager
    participant MKT as MarketDataManager
    participant POS as PositionDataManager
    participant ING as MarketIngestionService
    participant TRG as TriggerEngine
    participant DEC as FundFlowDecisionEngine
    participant EXE as FundFlowExecutionRouter
    participant CLI as BinanceClient/OrderGateway
    participant DB as MarketStorage
    participant ATT as AttributionEngine

    SCH->>BOT: 触发一轮（按3m收线对齐+延迟）
    BOT->>BOT: 热更新配置/信号池版本检查
    BOT->>ACC: get_account_summary()
    ACC-->>BOT: equity/available/margin等
    BOT->>BOT: 账户级风控检查（日亏损/连亏冷却）

    loop 遍历 symbols
        BOT->>MKT: get_realtime_market_data(symbol)
        MKT-->>BOT: ticker/funding/oi/change_15m
        BOT->>CLI: get_order_book(symbol)
        CLI-->>BOT: bids/asks depth
        BOT->>POS: get_current_position(symbol)
        POS-->>BOT: position or None

        alt 有持仓
            BOT->>BOT: 检查TP/SL覆盖与SLA
            alt 缺保护且补挂失败/超时
                BOT->>CLI: emergency flatten (reduce-only/market)
                BOT->>BOT: 标记禁止本轮新增开仓
                BOT-->>BOT: continue
            end
        end

        BOT->>BOT: build_fund_flow_context()
        BOT->>ING: aggregate_from_metrics(15s + 多周期)
        ING-->>BOT: flow_snapshot + timeframes
        BOT->>DB: upsert_market_flow()

        BOT->>TRG: should_trigger(symbol, trigger_type, trigger_id)
        alt 去重命中
            BOT-->>BOT: 跳过symbol
        else 通过
            BOT->>DEC: decide(portfolio, price, flow_context)
            DEC-->>BOT: BUY/SELL/HOLD/CLOSE

            alt BUY/SELL
                BOT->>TRG: evaluate_signal_pool()
                alt signal_pool 不通过
                    BOT-->>BOT: 跳过symbol
                else 通过
                    BOT->>BOT: 风控冷却/仓位上限/DCA/反向仓检查
                    alt 新开仓候选
                        BOT-->>BOT: 入队 pending_new_entries
                    else 立即执行（加仓/平仓）
                        BOT->>ATT: log_decision()
                        BOT->>EXE: execute_decision()
                        EXE->>CLI: 下单与保护单
                        EXE-->>BOT: execution_result
                        BOT->>ATT: log_execution()
                        BOT->>DB: insert_ai_decision_log()
                        BOT->>DB: insert_program_execution_log()
                    end
                end
            else CLOSE/HOLD
                BOT->>ATT: log_decision()
                BOT->>EXE: execute_decision()
                EXE->>CLI: 平仓路径执行
                EXE-->>BOT: execution_result
                BOT->>ATT: log_execution()
                BOT->>DB: 写执行日志
            end
        end
    end

    BOT->>BOT: pending_new_entries 按 score 排序并逐个执行
    BOT->>CLI: 清理无持仓残留保护单
    BOT-->>SCH: 本轮结束
```

## 2. 主决策树（交易对级）

```mermaid
flowchart TD
    A[Start: symbol轮次开始] --> B{价格有效?}
    B -- 否 --> B1[跳过symbol]
    B -- 是 --> C[读取持仓]

    C --> D{存在持仓?}
    D -- 否 --> E[检查是否有待成交开仓单]
    E -->|有| E1[跳过重复开仓]
    E -->|无| F[构建资金流上下文 + 15s聚合 + 多周期映射]

    D -- 是 --> G[保护单覆盖检查 TP+SL]
    G --> H{覆盖完整?}
    H -- 否 --> I[尝试补挂保护单]
    I --> J{补挂成功?}
    J -- 是 --> J1[进入下一symbol]
    J -- 否 --> K[触发风险动作: 立即强平/超时强平]
    K --> K1[标记本轮禁止新开仓]
    K1 --> J1
    H -- 是 --> F

    F --> L[触发去重 should_trigger]
    L --> M{去重通过?}
    M -- 否 --> M1[跳过symbol]
    M -- 是 --> N[决策引擎 decide]
    N --> O{决策类型}

    O -->|HOLD| O1[记录并结束symbol]
    O -->|CLOSE| O2[执行平仓路径]
    O -->|BUY/SELL| P[signal_pool 过滤]

    P --> Q{通过?}
    Q -- 否 --> Q1[跳过symbol]
    Q -- 是 --> R{账户冷却中?}
    R -- 是 --> R1[阻止新开仓]
    R -- 否 --> S{已有持仓?}

    S -- 是 --> T[同向/反向检查 + DCA判定 + 单币仓位上限]
    T --> U{可执行?}
    U -- 否 --> U1[跳过symbol]
    U -- 是 --> V[立即执行下单]

    S -- 否 --> W[加入新开仓候选队列 pending_new_entries]
    V --> X[记录日志+回写DB]
    W --> Y[轮次末排序后执行候选]
    Y --> X
```

## 3. 开仓执行降级树（Execution Router）

```mermaid
flowchart TD
    A[Entry BUY/SELL] --> B[风险校验 + 杠杆同步]
    B --> C{通过?}
    C -- 否 --> C1[返回error]
    C -- 是 --> D[计算数量并最小名义额修正]
    D --> E{qty有效?}
    E -- 否 --> E1[返回error]
    E -- 是 --> F[LIMIT IOC 下单]
    F --> G{成功/已开仓阻断?}
    G -- 是 --> H[若成交则下TP/SL]
    G -- 否 --> I{流动性不足?}
    I -- 否 --> I1[返回error]
    I -- 是 --> J[IOC滑移重试]
    J --> K{重试成功?}
    K -- 是 --> H
    K -- 否 --> L{open_gtc_fallback_enabled?}
    L -- 否 --> M{open_market_fallback_enabled?}
    L -- 是 --> L1[GTC回退]
    L1 --> L2{成功?}
    L2 -- 是 --> H
    L2 -- 否 --> M
    M -- 是 --> M1[MARKET回退]
    M1 --> H
    M -- 否 --> I1

    H --> N{TP/SL完整?}
    N -- 是 --> N1[success/pending]
    N -- 否 --> O{rollback_on_tp_sl_fail?}
    O -- 是 --> O1[强制平仓回滚]
    O -- 否 --> O2[保留error]
    O1 --> O2
```

## 4. 平仓执行降级树（Execution Router）

```mermaid
flowchart TD
    A[Entry CLOSE] --> B[检查持仓与可平数量]
    B --> C{可平?}
    C -- 否 --> C1[noop]
    C -- 是 --> D[LIMIT IOC平仓重试 N次]
    D --> E{任一成交?}
    E -- 是 --> E1[success]
    E -- 否 --> F{close_gtc_fallback_enabled?}
    F -- 否 --> G[error]
    F -- 是 --> F1[GTC reduce-only 回退]
    F1 --> H{成交?}
    H -- 是 --> H1[success]
    H -- 否 --> H2[pending/error]
    H2 --> I{close_market_fallback_enabled 且状态error?}
    I -- 否 --> J[返回最终状态]
    I -- 是 --> I1[MARKET reduce-only 强退]
    I1 --> J
    E1 --> K{全平且成交?}
    K -- 是 --> K1[cancel_all_open_orders]
    K -- 否 --> J
    K1 --> J
```

## 5. 信号池规则判定树（规则层）

```mermaid
flowchart TD
    A[BUY/SELL决策输入] --> B{signal_pool.enabled?}
    B -- 否 --> B1[通过]
    B -- 是 --> C{scheduled_trigger_bypass 且 trigger=scheduled?}
    C -- 是 --> C1[通过]
    C -- 否 --> D{symbol在pool作用域内?}
    D -- 否 --> D1[拒绝]
    D -- 是 --> E{已持仓且 apply_when_position_exists=false?}
    E -- 是 --> E1[通过]
    E -- 否 --> F[侧向分数门槛检查 min_long/min_short]
    F --> G{分数达标?}
    G -- 否 --> G1[拒绝]
    G -- 是 --> H[规则集评估 AND/OR/min_pass_count]
    H --> I{condition_met?}
    I -- 否 --> I1[拒绝]
    I -- 是 --> J{edge_trigger_enabled?}
    J -- 否 --> J1[通过]
    J -- 是 --> K[边沿状态机 rising/falling/steady + cooldown]
    K --> L{rising_edge触发?}
    L -- 是 --> L1[通过]
    L -- 否 --> L2[拒绝]
```

## 6. 会议讨论建议（配合图审）
1. 先看"主决策树"，确认策略行为边界（反向仓、加仓、候选排序）是否符合交易纪律。
2. 再看"开仓/平仓降级树"，确认执行安全优先级是否与团队预期一致。
3. 最后看"信号池判定树"，讨论阈值、规则逻辑和边沿触发是否导致错失/过度过滤。

---

## 7. 资金流指标计算架构

```mermaid
flowchart TB
    subgraph 采集层
        A[交易所API] --> B[MarketGateway]
        B --> C[MarketDataManager]
    end
    
    subgraph 聚合层
        C --> D[MarketIngestionService]
        D --> E[15秒窗口聚合]
        E --> F[多周期聚合 1m/3m/5m/15m/30m/1h/2h/4h]
    end
    
    subgraph 指标计算
        F --> G[7大因子计算]
        G --> H[CVD比率]
        G --> I[CVD动量]
        G --> J[OI变化率]
        G --> K[资金费率]
        G --> L[深度比率]
        G --> M[订单不平衡]
        G --> N[流动性Delta]
    end
    
    subgraph 决策层
        O[DecisionEngine] --> P[市场状态识别 15m]
        P --> Q{TREND/RANGE/NO_TRADE}
        Q -->|TREND| R[趋势打分]
        Q -->|RANGE| S[区间打分]
        Q -->|NO_TRADE| T[HOLD]
        R --> U[分数比较]
        S --> U
    end
    
    subgraph 过滤层
        U --> V[Signal Pool]
        V --> W[分数门槛]
        V --> X[规则集]
        V --> Y[边沿触发]
    end
```

### 7.1 7-Factor权重分配

| 因子 | 趋势权重 | 区间权重 | 计算来源 |
|------|----------|----------|----------|
| CVD比率 | 24% | - | change_15m |
| CVD动量 | 14% | 35% | change_15m - change_24h/96 |
| OI变化率 | 22% | 20% | (OI_t - OI_{t-1}) / |OI_{t-1}| |
| 资金费率 | 10% | - | funding_rate |
| 深度比率 | 15% | 10% | bid_notional / ask_notional |
| 订单不平衡 | 15% | 55% | (bid - ask) / (bid + ask) |
| 流动性Delta | 12% | - | 归一化处理 |

---

## 8. 市场状态识别 (15分钟周期)

```mermaid
stateDiagram-v2
    [*] --> Init
    Init --> CheckATR: ATR存在?
    CheckATR --> ATR_Low: ATR < 0.0012
    ATR_Low --> NO_TRADE: 波动不足
    CheckATR --> ATR_High: ATR > 0.02
    ATR_High --> NO_TRADE: 波动过大
    CheckATR --> CheckADX: ATR正常范围
    
    CheckADX --> ADX_Trend: ADX >= 21
    CheckADX --> ADX_Range: ADX <= 18
    CheckADX --> ADX_Mid: 18 < ADX < 21
    
    ADX_Trend --> CheckEMA: EMA方向
    CheckEMA --> TREND_LONG: EMA20 > EMA50
    CheckEMA --> TREND_SHORT: EMA20 < EMA50
    CheckEMA --> NO_TRADE: EMA走平
    
    ADX_Range --> RANGE: 震荡市场
    
    ADX_Mid --> NO_TRADE: 中性区域
    
    NO_TRADE --> [*]: 不交易
    RANGE --> [*]: 区间回归
    TREND_LONG --> [*]: 顺势做多
    TREND_SHORT --> [*]: 顺势做空
```

### 8.1 状态参数

| 状态 | ADX阈值 | ATR范围 | EMA要求 |
|------|---------|---------|---------|
| TREND | >= 21 | 0.12%~2% | 方向明确 |
| RANGE | <= 18 | 0.1%~2% | 无要求 |
| NO_TRADE | 18~21 | 超出范围 | EMA走平 |

---

## 9. 双层门禁系统

```mermaid
flowchart LR
    subgraph Candidate_Gate[5分钟候选门禁]
        A1[资金流上下文] --> B1[分数计算]
        B1 --> C1{分数 >= 0.20?}
        C1 -->|是| D1[进入候选队列]
        C1 -->|否| E1[拒绝]
    end
    
    subgraph Execution_Gate[1分钟执行门禁]
        D1 --> F1[信号池过滤]
        F1 --> G1{通过?}
        G1 -->|是| H1[执行下单]
        G1 -->|否| I1[拒绝]
    end
    
    subgraph Hard_Mode[硬门禁模式]
        J1[分数 < 阈值] --> K1[强制拦截]
    end
    
    subgraph Assist_Mode[辅助模式]
        L1[分数 < 阈值] --> M1[警告但放行]
    end
```

### 9.1 门禁配置

```json
{
  "flow_signal_hard_gate": true,
  "flow_candidate_hard_gate": true,
  "flow_min_score_short": 0.20,
  "flow_min_hits_short": 2
}
```

---

## 10. DCA加仓机制

```mermaid
flowchart TD
    A[开仓入场] --> B[监控持仓]
    B --> C{有持仓?}
    C -->|是| D[计算回撤]
    D --> E{回撤 >= 阈值?}
    E -->|是| F[DCA加仓]
    E -->|否| G[继续监控]
    F --> H[倍数计算]
    H --> I[仓位 = 基础仓位 × 倍数]
    I --> J[执行加仓]
    J --> B
    
    C -->|否| K[结束监控]
```

### 10.1 DCA参数

```json
{
  "dca_martingale_enabled": true,
  "dca_max_additions": 1,
  "dca_drawdown_thresholds": [0.01],
  "dca_multipliers": [1.0]
}
```

---

## 11. 风控防护体系

```mermaid
flowchart TB
    subgraph Account_Level[账户级熔断]
        A[日亏损检测] --> B{max > 5%?}
        B -->|是| C[日亏损冷却 8h]
        D[连亏检测] --> E{连亏 >= 2?}
        E -->|是| F[连亏冷却 30min]
    end
    
    subgraph Position_Level[持仓级保护]
        G[TP/SL覆盖检测] --> H{完整?}
        H -->|否| I[补挂尝试]
        I --> J{成功?}
        J -->|是| K[正常]
        J -->|否| L[SLA超时检测]
        L --> M{超时?}
        M -->|是| N[强制平仓]
    end
    
    subgraph Order_Level[订单级校验]
        O[价格偏离检测] --> P{偏离 > 1%?}
        P -->|是| Q[拒绝开仓]
        O --> R[仓位比例检测]
        R --> S{比例合法?}
        S -->|否| Q
    end
    
    C --> T[阻止新开仓]
    F --> T
    N --> T
    Q --> T
    K --> U[放行]
    T --> V[交易结束]
    U --> V
```

---

## 12. 专家评审核心议题

### 12.1 数据质量优化

1. **CVD代理问题**
   - 当前用 `change_15m` 近似CVD
   - 建议: 接入逐笔成交数据计算真实CVD
   - 影响: 提高短期动量预测准确性

2. **signal_strength定义**
   - 当前 `>0` 即为signal触发
   - 问题: scheduled分支几乎不生效
   - 建议: 重新定义触发判据

### 12.2 参数优化空间

| 参数 | 当前值 | 建议讨论范围 | 说明 |
|------|--------|--------------|------|
| close_threshold | 0.32 | 0.35~0.45 | 平仓敏感性 |
| long_open_threshold | 0.22 | 0.25~0.30 | 开仓严格度 |
| short_open_threshold | 0.22 | 0.25~0.30 | 空头过滤 |
| range_threshold | 0.40 | 0.35~0.50 | 区间模式 |

### 12.3 执行安全

1. **保护单SLA**: 90秒超时是否足够?
2. **GTC回退**: 流动性不足时的成交确定性
3. **极端行情**: 强平策略的滑点控制

### 12.4 策略方向

1. **趋势锁定**: 硬锁定 vs 软锁定 vs 关闭
2. **分位数优化**: 区间模式极端值判定
3. **多周期协同**: 15m/5m/1m信号权重

---

## 13. 改进路线图

### Phase 1: 近期改进 (1-2周)
- [ ] 验证硬门禁效果
- [ ] 分析亏损订单特征
- [ ] 调整平仓阈值

### Phase 2: 中期优化 (1个月)
- [ ] 真实CVD数据接入
- [ ] 分位数系统优化
- [ ] 边沿触发逻辑改进

### Phase 3: 长期演进 (季度)
- [ ] 机器学习因子权重
- [ ] 自适应阈值系统
- [ ] 多市场状态并行

---

## 14. 附录: 关键代码路径

```
src/fund_flow/
├── decision_engine.py    # _score_trend(), _score_range(), _detect_regime()
├── trigger_engine.py     # should_trigger(), evaluate_signal_pool()
├── execution_router.py   # execute_open(), execute_close()
├── risk_engine.py        # validate_operation()
├── market_ingestion.py   # aggregate_from_metrics()
└── attribution_engine.py # log_decision(), log_execution()
```

```
config/
├── trading_config_fund_flow.json   # 资金流策略配置
└── trading_config_vps.json        # VPS运行配置
```
