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
1. 先看“主决策树”，确认策略行为边界（反向仓、加仓、候选排序）是否符合交易纪律。
2. 再看“开仓/平仓降级树”，确认执行安全优先级是否与团队预期一致。
3. 最后看“信号池判定树”，讨论阈值、规则逻辑和边沿触发是否导致错失/过度过滤。
