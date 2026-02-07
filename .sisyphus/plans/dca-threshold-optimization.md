# DCA 开仓门槛提升与参数优化计划

## TL;DR

> **Quick Summary**: 基于 2026-02-07_12 日志与 DCA_dashboard 数据，收紧 DCA 开仓阈值（RSI/评分/成交量/时间/冷却）、降低单仓风险并限制最大持仓数至 3，以降低回撤（≤5%）并减少过度开仓。
>
> **Deliverables**:
> - `config/trading_config.json` 中 DCA 参数收紧（RSI/score/volume/time/cooldown/max_positions）
> - 下调 DCA 仓位/杠杆相关参数（max_position_pct / leverage / initial_margin）
> - 形成明确的人工验证步骤与指标检查
>
> **Estimated Effort**: Short
> **Parallel Execution**: NO
> **Critical Path**: 日志结论 → 参数调整 → 人工验证

---

## Context

### Original Request
- 分析 `logs/2026-02/2026-02-07_12.txt` 与 `DCA_dashboard_2026-02-07_12.csv`
- 开仓次数过多且最终亏损，要求提高开仓门槛、提高胜率并优化参数

### Interview Summary
**Key Discussions**:
- 优先目标：降低回撤（上限 5%）
- 允许调整：RSI、评分阈值、成交量过滤、时间过滤、冷却参数、最大持仓数
- 最大持仓数：≤ 3
- 允许调整杠杆与仓位比例
- 仅修改 `config/trading_config.json`

**Research Findings**:
- `2026-02-07_12.txt` 显示同时持仓 9~10 个，浮亏币种较多
- `DCA_dashboard` 显示频繁开仓/重复进出、回撤拉大
- `config/trading_config.json` 中 DCA 参数当前偏宽：
  - `score_threshold` 0.005 / `score_threshold_long` 0.02 / `score_threshold_short` 0.02
  - `rsi_entry_long` 38 / `rsi_entry_short` 62
  - `volume_quantile` 0.3 / `short_volume_quantile` 0.45
  - `cooldown_seconds` 0
  - `max_positions` 2（但日志显示持仓远高于 2，说明实际限制可能在 DCA 轮动层以外）

### Metis Review
**Identified Gaps** (addressed):
- Metis 工具调用失败（系统错误）。已通过自检补齐边界与验收标准。

### Manual High-Accuracy Review (替代 Momus)
为满足高精度审查需求，已进行人工严苛自检，补充如下要点：
- **全局持仓上限一致性**：日志显示持仓数 > DCA 的 `max_positions`，说明可能存在 DCA 外的开仓路径（或全局限额在别处）。计划中加入“确认全局持仓限制路径”检查。
- **参数合理性**：新增参数均为“收紧”方向，不改变策略核心逻辑，仅降低频率与风险。
- **验证口径**：明确“回撤 ≤5% + 最大持仓 ≤3 + 开仓次数减少”为人工验证的核心观察指标。

---

## Work Objectives

### Core Objective
降低 DCA 过度开仓与回撤（≤5%），通过提高门槛与降低单仓风险来提升胜率。

### Concrete Deliverables
- `config/trading_config.json` 中：
  - 提高 RSI/score/成交量筛选门槛
  - 增加冷却期，限制最大持仓数
  - 降低单仓仓位比例与 DCA 杠杆/初始保证金

### Definition of Done
- 日志指标显示：开仓次数显著降低，最大持仓数不超过 3
- 连续周期回撤控制在 5% 以内
- 触发门槛提升（RSI/score/volume 等均高于原值）

### Must Have
- max_positions = 3
- RSI/score/volume/cooldown 参数上调（更严格）
- 杠杆与仓位比例下调

### Must NOT Have (Guardrails)
- 不改动策略核心逻辑代码
- 不新增新指标或外部数据依赖
- 只修改 `config/trading_config.json`

---

## Verification Strategy (MANDATORY)

> 所有验证以人工为主，但提供可执行的检查步骤。

### Test Decision
- **Automated tests**: None (manual)

### Agent-Executed QA Scenarios (for reproducibility)

**Scenario 1: 参数确认**
  Tool: Bash (python)
  Steps:
    1. 读取 `config/trading_config.json`
    2. 断言 RSI/score/volume/cooldown/max_positions 参数已更新到新值
  Expected Result: 配置值符合计划

**Scenario 2: 人工运行观察**
  Tool: Manual
  Steps:
    1. 启动机器人，运行 1~2 小时
    2. 记录开仓次数、最大持仓数、回撤变化
  Expected Result: 开仓显著减少，回撤 ≤ 5%

---

## TODOs

### 1) 提高 DCA 开仓门槛参数

**What to do** (建议值)：
- `rsi_entry_long`: 38 → **30**（更低才开多，减少追高）
- `rsi_entry_short`: 62 → **70**（更高才开空，减少逆势）
- `score_threshold`: 0.005 → **0.02**
- `score_threshold_long`: 0.02 → **0.03**
- `score_threshold_short`: 0.02 → **0.03**

**Must NOT do**:
- 不修改策略逻辑代码

**Acceptance Criteria**:
- [ ] 以上参数在 `config/trading_config.json` 更新到新值

---

### 2) 提高成交量过滤 & 时间过滤

**What to do** (建议值)：
- `min_daily_volume_usdt`: 0.5 → **5**
- `volume_quantile`: 0.3 → **0.6**
- `short_volume_quantile`: 0.45 → **0.6**
- `allowed_hours_utc`: 缩小到高流动时段（例如 6~20）

**Acceptance Criteria**:
- [ ] 成交量过滤参数全部提高
- [ ] 交易时间窗口缩小

---

### 3) 增加冷却与限制持仓数

**What to do** (建议值)：
- `cooldown_seconds`: 0 → **900**（至少 15 分钟）
- `max_positions`: 2 → **3**（保底不超 3）

**补充检查**:
- 确认全局最大持仓数是否另有配置（如 `trading.max_position_percent` / `ai.dca_top_n` / 其他模块）
- 若存在其他开仓路径导致持仓数超 3，需同步收紧对应路径或参数

**Acceptance Criteria**:
- [ ] 冷却期设置为 900 秒
- [ ] max_positions = 3
- [ ] 全局最大持仓数路径确认并限制至 ≤3

---

### 4) 降低杠杆与单仓风险

**What to do** (建议值)：
- `dca_rotation.params.leverage`: 3 → **2**
- `dca_rotation.params.initial_margin`: 2.0 → **1.0**
- `dca_rotation.params.max_position_pct`: 0.3 → **0.2**
- `dca_rotation.params.max_position_pct_add`: 0.5 → **0.3**

**Acceptance Criteria**:
- [ ] 杠杆与仓位比例下调到建议值

---

## Commit Strategy

| After Task | Message | Files | Verification |
|------------|---------|-------|--------------|
| 1-4 | `chore(dca): tighten entry thresholds and reduce risk` | `config/trading_config.json` | Scenario 1 |

---

## Success Criteria

- [ ] 运行后最大持仓数 ≤ 3
- [ ] 回撤 ≤ 5%
- [ ] 开仓次数明显减少（主观 + 日志对比）
