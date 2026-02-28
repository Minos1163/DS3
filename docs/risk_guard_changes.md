# 风控模块改动计划与实现摘要

本次变更聚焦于对最近日志中“趋势信号转向但风控未生效”的根因诊断，以及实现一个更稳健的风控闭环。以下内容包括改动要点、实现要点、验证步骤与关键指标（KPI）。

1. 问题根因总结
- 风控判断被设计为“前置约束不足以覆盖所有行情情况”，在强趋势信号出现时仍然允许进入交易。
- 保护单（止损/止盈）创建与绑定存在时序/稳健性问题，导致交易进入后保护未生效或因未就绪而错失保护。
- 对趋势信号的权重在高波动环境中过于乐观，未对回撤/波动信号进行自适应处理。
- 缺少对趋势反转的快速响应与统一的退出机制。

2. 实现要点（本轮提交的改动）
- 新增风控适配层：risk/integration_gate.py，提供 gate_trade_decision(state_dict, config=None, equity_fraction=...) 的统一入口，输出 action/enter/exit/score。
- 新增风控核心能力：risk/enhanced_risk.py，提供可配置的 RiskConfig、MarketState，以及 evaluate_risk(state_dict, config=None) 的评分与决策分解。
- 新增适配器日志：integration_gate 将交易风控决策落地到日志文件 logs/trading_risk_gate.log，便于审计与回溯。
- 新增日志导出工具：tools/logs_analysis/logs_analysis.py，用于从最近6小时的日志中提取交易相关记录并导出 Excel，便于人工复核与分析。
- 风控日志化输出：在 gate_trade_decision 的执行路径中，记录决策过程中的风险分数、触发条件、信号分量等，提升可观测性。
- 修改了 risk/enhanced_risk.py 的类型处理，增加了鲁棒的类型转换 helpers，确保对外部输入的类型容错性，降低静态检查报错。

3. 验证方案（如何自己验证）
- Step 1: 日志导出验证
  - 运行日志分析脚本，导出最近6小时日志：python3 tools/logs_analysis/logs_analysis.py <log_paths> --hours 6 --output logs/exports/logs_last6h.xlsx
  - 打开 logs_last6h.xlsx，确认包含 BTCUSDT/ETHUSDT 的买卖/开仓/平仓等事件，以及时间戳是否为 UTC。
- Step 2: 风控入口的对接验证
  - 准备一个 sample_state_state_dict，包含 trend、momentum、volatility、drawdown、atr、direction、equity_fraction 等字段；调用 risk.integration_gate.gate_trade_decision(state_dict, log_path="logs/trading_risk_gate.log")，观察返回值与日志。
  - 将 gate_trade_decision 的返回结果映射到实际交易动作：ENTER、EXIT、HOLD。验证在不同 state 下输出是否符合预期。
- Step 3: 日志观测与回放
  - 查看 logs/trading_risk_gate.log，确认每次决策均有日志落地，包含 action、risk_score、state、timestamp 等字段。
- Step 4: 回测与对比
  - 在历史行情数据上回测带有风控门槛的新逻辑，比较关键指标（胜率、夏普、最大回撤、净利润）与未改动时的基线。重点观察在价格急速下跌/波动放大的场景中风控是否更早介入。

4. 预期效果（KPI）
- 风控入场门槛提升后，平均进入交易的成功概率提升，且在回撤阶段能更早触发退出。
- 在高波动市场，保护单（TP/SL）触发率提升，保护单未就绪时的风险暴露显著降低。
- 日志系统记录更加完整，便于后续审计和模型调参。
- 回测指标对齐：最大回撤降低，夏普提升，整体收益稳定性提高。

5. 后续工作
- 将 risk/integration_gate.py 的 gate_trade_decision 集成到实际的交易执行入口，替换原始的风险判断路径。
- 为 risk/enhanced_risk.py 增加更多的阈值组合、跨周期信号融合策略，提升自适应能力。
- 为日志导出工具增加更多字段，如订单ID、成交量、挂单状态、保护单状态等，以便于事后分析。

文档完结。

#### 集成测试导读：交易执行入口 Patch 的对比验证
- Step A 与 Step B 的结合点：风控 gateway 的 pre-trade 调用点应落在下单前的统一入口，确保不会在风险阈值之外进入交易。
- Step C 的日志输出增强点：在每次进入/退出分支时，记录动作、分数、信号分量与当前状态，便于后续对比与诊断。
- Step D 的基线对比：提供一个简单的回测/对比脚本，用于对比风险策略改动前后的关键 KPI（如最大回撤、胜率、净收益、交易成功率等）。
