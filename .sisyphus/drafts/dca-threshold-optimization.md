# Draft: DCA 开仓门槛与参数优化（2026-02-07_12）

## Requirements (confirmed)
- 分析 logs/2026-02 下的 2026-02-07_12.txt 与 DCA_dashboard_2026-02-07_12.csv
- 发现开仓次数过多且最终亏损
- 提高开仓门槛、提高胜率、优化参数
- 优先级：优先减少回撤（B）
- 允许调整参数：RSI、评分阈值、交易量过滤、时间过滤、冷却参数、最大持仓数
- 最大持仓数目标：最多 3 仓
- 最大回撤容忍：5%
- 允许调整：杠杆与单仓仓位比例

## Technical Decisions
- 待确定：具体优化目标与约束（如开仓次数上限、胜率目标）

## Research Findings
- 待分析文件内容

## Open Questions
- 目标指标优先级（胜率 vs 回撤 vs 交易次数）
- 允许修改哪些参数（DCA 阈值 / RSI / volume / score / cooldown 等）
- 是否需要分币种或全局统一参数
- 目标回撤上限/容忍区间？
- 是否允许调整杠杆或仓位比例？

## Scope Boundaries
- INCLUDE: DCA 开仓门槛与相关参数优化
- EXCLUDE: 策略核心逻辑重构（除非必要）
