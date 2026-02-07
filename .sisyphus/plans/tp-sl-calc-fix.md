# TP/SL 计算与委托修正计划

## TL;DR

> **Quick Summary**: 统一 TP/SL 百分比语义（AI 与 DCA 均按“百分比”解释），修正 DCA 与 PAPI 条件单的价格计算与 closePosition 行为，并确保平仓时撤该币种全部保护单。
>
> **Deliverables**:
> - 修正 `main.py` 中 DCA TP/SL 百分比计算与 AI 结果一致
> - 修正 `tp_sl.py` 百分比计算与 PAPI 条件单 `closePosition=true`
> - 保护单价格按 tick size 对齐（含 FAPI 保护单）
> - 批量更新 `config/` 中 DCA 相关参数与 `max_stop_loss_abs`
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES – 2 waves
> **Critical Path**: 参数语义统一 → 下单/撤单修正 → 配置批量迁移

---

## Context

### Original Request
- 检查 main.py 和 tp_sl.py 中止盈止损计算错误
- AI 给出的止盈/止损数据需校验并正确转化
- 开仓时正确下止盈止损委托
- 平仓时正确撤单（撤该币种全部 TP/SL）

### Interview Summary
**Key Discussions**:
- AI 输出约定：止盈为正数（如 12 表示 +12%），止损为负数（如 -10 表示 -10%）
- LONG: TP = entry * (1 + tp_pct/100), SL = entry * (1 + sl_pct/100)
- SHORT: TP = entry * (1 - tp_abs/100), SL = entry * (1 + sl_abs/100)
- DCA 参数 `take_profit_pct/symbol_stop_loss_pct` 统一为**百分比**（如 12 表示 12%）
- tick size 对齐（交易所最小价格变动）
- PAPI 条件单必须 `closePosition=true`
- 平仓时撤该币种全部保护单
- `max_stop_loss_abs` 放宽到 10
- 不写自动化测试，人工验证

**Research Findings**:
- `main.py` `_open_long/_open_short` 使用百分比/100 计算 TP/SL（符合期望）
- `main.py` `_calc_tp_sl_prices`（DCA）使用小数比例（未除以100）
- `tp_sl.py` `_calc_by_pct` 使用小数比例（未除以100）
- PAPI 保护单通过 `tp_sl.PapiTpSlManager` 下单（当前未带 closePosition）
- FAPI 保护单在 `order_gateway.place_protection_orders` 已使用 `closePosition=true`
- 平仓时在 `position_state_machine._close` 先 `cancel_all_conditional_orders`，PAPI 再 `cancel_all_open_orders`

### Metis Review
**Identified Gaps** (addressed):
- Metis 工具调用失败（系统错误：JSON Parse error）。已通过自检补齐边界与验收标准。

---

## Work Objectives

### Core Objective
统一 TP/SL 的语义为“百分比”，确保 AI 与 DCA 的价格计算、下单与撤单全流程一致，且满足 PAPI 保护单 closePosition 规则与 tick size 对齐。

### Concrete Deliverables
- 修正 DCA 百分比计算逻辑（`main.py`）
- 修正 PAPI TP/SL 百分比计算与 closePosition（`tp_sl.py`）
- FAPI 保护单也进行 tick size 对齐（`order_gateway.py` 或复用现有 rounding）
- `config/` 中 DCA 参数由小数比例改为百分比（批量迁移）
- `max_stop_loss_abs` 配置批量改为 10

### Definition of Done
- 给定示例：FARTCOINUSDT entry=0.191900、TP=12%、SL=-10%，系统计算价格为 0.214928 / 0.172710（并按 tick size 对齐）
- PAPI 保护单请求参数中包含 `closePosition=true`
- 平仓路径会撤该币种全部保护单
- `config/` 中 DCA 百分比参数完成迁移（小数 → 百分比）

### Must Have
- AI 输出和 DCA 参数统一为“百分比”
- tick size 对齐
- PAPI closePosition 保护单

### Must NOT Have (Guardrails)
- 不引入新的策略逻辑或额外交易信号
- 不改变非 TP/SL 的交易方向/下单逻辑
- 不写自动化测试（仅人工验证）

---

## Verification Strategy (MANDATORY)

> **UNIVERSAL RULE: ZERO HUMAN INTERVENTION**
> 所有验收标准必须可由执行代理运行命令验证（即使用户选择“手动验证”，此处仍给出可执行 QA 场景）。

### Test Decision
- **Infrastructure exists**: Unknown (not assessed)
- **Automated tests**: None (user requested manual verification)
- **Framework**: N/A

### Agent-Executed QA Scenarios (MANDATORY — ALL tasks)

**Scenario 1: 计算结果校验（Python 直接计算）**
  Tool: Bash (python)
  Preconditions: Repo 可运行 Python
  Steps:
    1. 运行内联脚本，模拟 FARTCOINUSDT entry=0.191900, tp=12, sl=-10
    2. 断言 LONG 计算结果 ≈ 0.214928 / 0.172710
    3. 断言 SHORT 计算结果 ≈ 0.170? / 0.214?（按规则计算）
  Expected Result: 计算值与规则一致
  Evidence: 命令输出

**Scenario 2: PAPI 保护单参数包含 closePosition**
  Tool: Bash (python or log inspection)
  Preconditions: dry_run 或 mock broker 可运行
  Steps:
    1. 构造 TpSlConfig 并调用 PapiTpSlManager.build_* 逻辑
    2. 断言订单参数含 closePosition=true
  Expected Result: closePosition=true 写入下单参数
  Evidence: 输出或日志

**Scenario 3: 平仓撤单**
  Tool: Bash (python)
  Preconditions: 使用 mock 或 dry_run 模式
  Steps:
    1. 调用 `position_state_machine._close`
    2. 断言先调用 cancel_all_conditional_orders 再 cancel_all_open_orders (PAPI)
  Expected Result: 保护单被撤销
  Evidence: 调用记录或日志

---

## Execution Strategy

### Parallel Execution Waves

Wave 1 (Start Immediately):
- Task 1: 统一 TP/SL 百分比计算逻辑（main.py + tp_sl.py）
- Task 2: PAPI closePosition + tick size 对齐

Wave 2 (After Wave 1):
- Task 3: 批量迁移 config（DCA 参数 + max_stop_loss_abs）
- Task 4: 验证与日志输出

Critical Path: Task 1 → Task 2 → Task 3

---

## TODOs

### 1) 统一 TP/SL 百分比语义（AI 与 DCA）

**What to do**:
- 在 `main.py` 的 `_calc_tp_sl_prices()` 中把 DCA 参数解释为**百分比**（除以 100）
- 统一 LONG/SHORT 计算公式与用户规则一致
- 检查 `_open_long/_open_short` 不改变 AI 输出语义（可避免把 TP 变成负数）

**Must NOT do**:
- 不改变下单方向逻辑
- 不修改 AI 决策字段名称

**Recommended Agent Profile**:
- **Category**: unspecified-high
  - Reason: 涉及交易语义与多处逻辑一致性
- **Skills**: []

**Parallelization**:
- **Can Run In Parallel**: YES (Wave 1)

**References**:
- `src/main.py:_calc_tp_sl_prices` — DCA TP/SL 计算逻辑（当前按小数比例）
- `src/main.py:_open_long/_open_short` — AI TP/SL 计算路径

**Acceptance Criteria**:
- [ ] `_calc_tp_sl_prices` 按百分比解释参数（如 12 → 12%）
- [ ] LONG/SHORT 公式符合：LONG TP/SL 使用 +tp/-sl；SHORT TP 下方，SL 上方

**Agent-Executed QA Scenarios**:
- Scenario 1（计算结果校验）必须通过

---

### 2) PAPI 保护单 closePosition + tick size 对齐

**What to do**:
- 在 `tp_sl.py` 构建的 STOP/TAKE_PROFIT 条件单中强制 `closePosition=true`
- 若 `tp_sl.py` 使用百分比计算（`_calc_by_pct`），按百分比解释（/100）
- 对 FAPI 路径的保护单也进行 tick size 对齐（可复用现有 `_round` 或 broker 的格式化方法）

**Must NOT do**:
- 不更换下单接口或 broker
- 不改变开仓下单流程

**Recommended Agent Profile**:
- **Category**: unspecified-high
  - Reason: 牵涉交易所参数约束与精度问题
- **Skills**: []

**Parallelization**:
- **Can Run In Parallel**: YES (Wave 1)

**References**:
- `src/trading/tp_sl.py:_build_sl_order/_build_tp_order` — PAPI 条件单构造
- `src/trading/tp_sl.py:_calc_by_pct` — 百分比计算入口
- `src/trading/order_gateway.py:place_protection_orders` — FAPI 保护单入口

**Acceptance Criteria**:
- [ ] PAPI 条件单请求包含 `closePosition=true`
- [ ] stopPrice/price 经过 tick size 对齐
- [ ] `_calc_by_pct` 按百分比解释（/100）

**Agent-Executed QA Scenarios**:
- Scenario 2（closePosition 参数校验）必须通过

---

### 3) 批量迁移 config（DCA 参数 + max_stop_loss_abs）

**What to do**:
- 在 `config/` 目录内搜索 `take_profit_pct` 与 `symbol_stop_loss_pct`
- 将小数比例更新为百分比（例如 0.02 → 2, 0.15 → 15）
- 将所有 `max_stop_loss_abs` 更新为 10

**Must NOT do**:
- 不修改 `config/` 目录外配置

**Recommended Agent Profile**:
- **Category**: quick
  - Reason: 批量配置修改
- **Skills**: []

**Parallelization**:
- **Can Run In Parallel**: YES (Wave 2)

**References**:
- `config/trading_config.json` — 主要配置文件
- `config/trading_config V333-316.json` — 目前含 DCA 小数参数
- `config/*.json` — 需批量扫描更新

**Acceptance Criteria**:
- [ ] `config/` 中所有 `take_profit_pct` 与 `symbol_stop_loss_pct` 为“百分比整数/小数”（>=1）
- [ ] `max_stop_loss_abs` 统一为 10

**Agent-Executed QA Scenarios**:
- Scenario 1（计算结果校验）使用更新后的参数进行确认

---

### 4) 平仓撤单路径验证与日志

**What to do**:
- 核对 `position_state_machine._close` 的撤单路径能覆盖 PAPI 条件单
- 如 PAPI 条件单未被 `cancel_all_conditional_orders` 覆盖，补充或调整撤单逻辑
- 增加清晰日志（便于人工验证）

**Must NOT do**:
- 不引入新的平仓策略逻辑

**Recommended Agent Profile**:
- **Category**: unspecified-high
  - Reason: 影响平仓安全性
- **Skills**: []

**Parallelization**:
- **Can Run In Parallel**: YES (Wave 2)

**References**:
- `src/trading/position_state_machine.py:_close` — 平仓撤单入口
- `src/api/binance_client.py` — cancel_all_conditional_orders 实现

**Acceptance Criteria**:
- [ ] 平仓时撤该币种全部 TP/SL
- [ ] 日志中可看到撤单执行路径

**Agent-Executed QA Scenarios**:
- Scenario 3（平仓撤单）必须通过

---

## Commit Strategy

| After Task | Message | Files | Verification |
|------------|---------|-------|--------------|
| 1-2 | `fix(tp-sl): normalize percent semantics and PAPI closePosition` | `src/main.py`, `src/trading/tp_sl.py`, `src/trading/order_gateway.py` | QA Scenarios 1-2 |
| 3-4 | `chore(config): migrate dca tp/sl percent + widen max_stop_loss_abs` | `config/*.json` | QA Scenario 1-3 |

---

## Success Criteria

### Verification Commands
```bash
python - <<'PY'
entry = 0.191900
tp = 12
sl = -10
long_tp = entry * (1 + tp/100)
long_sl = entry * (1 + sl/100)
print('LONG', long_tp, long_sl)
short_tp = entry * (1 - abs(tp)/100)
short_sl = entry * (1 + abs(sl)/100)
print('SHORT', short_tp, short_sl)
PY
```

### Final Checklist
- [ ] DCA 与 AI TP/SL 语义统一为百分比
- [ ] PAPI 保护单强制 closePosition=true
- [ ] tick size 对齐生效
- [ ] 平仓撤单覆盖该币种全部保护单
- [ ] 配置文件已批量迁移
